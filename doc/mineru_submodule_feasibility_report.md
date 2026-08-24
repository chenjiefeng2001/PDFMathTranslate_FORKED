# 将 MinerU 以子模块引入本仓库的可行性分析报告

> **实施状态（2026-08-24）**：P0 与 P1 均已完成并经真机验证 —— Windows × Py3.13 × mineru 3.4.5 pipeline 后端，`MagicPdfAdapter._parse_mineru` 端到端跑通（样例 PDF 解析出 2 块 / 683 字符；进度事件含起始页计数与 `model init cost` 组件加载）。**两条路径均已实测**：进程内直连（pip 安装 mineru）与 `PDF2ZH_MINERU_PYTHON` 隔离子进程（`pdf2zh-setup-mineru` 从 `vendor/MinerU` pin 源码构建 venv）。实测发现并固化三处：① 3.4.5 pipeline 运行时 `import six` 但未声明（magicpdf extra 已补）；② `do_parse` 产物实际写入 `{output_dir}/{stem}/{parse_method}/` 子目录（适配器递归定位 `*_middle.json`）；③ 子进程管道需显式 UTF-8 解码（Windows locale 默认 cp936 会炸）。真实 `do_parse` 签名完全覆盖适配器所传 kwargs，防御性签名过滤设计得到验证。CI 保持 checkout 不初始化子模块（引擎测试 skip-by-default，避免每 job +百 MB 克隆开销）——如需 CI 覆盖引擎路径再单独开启。

> **结论摘要**：**技术上可行，协议上无障碍，仓库内也有成熟先例**（`vendor/PDFMathTranslate-next` 子模块 + 隔离 venv 安装模式可直接复用）。但调查发现两个更关键的事实：① MinerU 自 3.1.0（2026-04）起已从 AGPLv3 切换为基于 Apache 2.0 的宽松许可证，且官方支持 Python 3.10–3.13 —— 本项目当前"Py3.13 只能退回 magic-pdf 1.x"的门控已经过时；② 现有 `magicpdf_adapter._parse_mineru` 面向的 `mineru.document.Document.parse(callback=...)` 是**臆测 API，上游从未存在过**（真实入口是 `mineru.cli.common.do_parse`），MinerU 路径在本项目里从未真正可用。因此建议分三层推进：**P0（推荐，不 vendor）**：升级 pip 依赖至 mineru≥3.1 并重写 `_parse_mineru` 对接官方 `do_parse`（middle.json 可直接复用现有归一化层）；**P1（可选）**：引入 submodule 仅作为"可审计、可精确重建"的源码锚点，沿用 precise 式隔离 venv 安装；**P2（按需）**：只有当确需携带本地源码补丁时才建 fork 补丁分支。不建议 subtree/源码快照 vendor。上游发版节奏约每 4–5 天一个 release，长期跟随维护的成本主要取决于补丁策略而非子模块机制本身。

## 一、背景与动机

### 1.1 本项目当前的 MinerU/magic-pdf 消费方式

| 环节 | 现状 | 位置 |
|---|---|---|
| 引擎选择 | 双后端自动选择：`mineru` 2.x 优先（Py≤3.12），`magic-pdf` 1.3.12 兜底（Py3.13） | `pdf2zh/engine_env.py`（`MINERU_MIN_PY/MAX_PY` 门控、`probe_mineru/probe_magicpdf`） |
| 依赖声明 | `magicpdf` extra 条件依赖：`mineru>=2.0; python_version<'3.13'`、`magic-pdf>=1.3.12,<2; python_version>='3.13'` | `pyproject.toml:68-73` |
| 运行时调用 | `MagicPdfAdapter` 懒导入，产出归一化 block 列表供 `MagicPdfBridge` 消费 | `pdf2zh/magicpdf_adapter.py` |
| 桌面分发 | 冻结包不内置 MinerU/torch（NSIS 2GB 上限），提供 `/api/selftest/magicpdf` 探测与安装指引 | `pdf2zh/services/api.py:363` |
| 已知工程债 | magic-pdf 与 babeldoc 的 pymupdf 版本冲突（文档化绕过）；torch×onnxruntime CUDA DLL 加载顺序问题（`_preload_torch` 规避） | `pyproject.toml:108-113`、`magicpdf_cli.py:_preload_torch` |

### 1.2 仓库内的子模块先例（关键参考）

```
.gitmodules:
  [submodule "vendor/PDFMathTranslate-next"]
    path = pdf2zh/kernel/PDFMathTranslate-next.git
    url  = https://github.com/PDFMathTranslate/PDFMathTranslate-next.git
```

消费模式（`pdf2zh/kernel/precise.py`）：**不进主进程依赖树** —— 提供 `pdf2zh-setup-precise` CLI，在隔离 venv 中从子模块目录 `pip install`，内核经子进程调用。该模式解决了"重依赖引擎不污染主环境"的全部痛点，是 MinerU 引入的最佳模板。

注意：CI（`actions/checkout@v6`）目前**未初始化子模块**，next 内核在 CI 中实际处于跳过状态 —— 若引入新子模块且要求 CI 覆盖，需要补 `submodules: <target>` 配置。

## 二、上游调查（截至 2026-08）

### 2.1 版本与节奏

- 当前稳定版 **3.4.5**（2026-08-14），v4.0.0 alpha 进行中；累计 213 个 release，**平均 4–5 天一个 release**，3.x 补丁版本曾达到每月 15 个（3.1.0→3.1.15 仅一个月）；
- 78k stars，团队商业实体（OpenDataLab）持续投入，维护活跃度极高；
- 同团队的旧线 **magic-pdf 1.x 已停更**（最后版本 1.3.12，2025-05-24，距今 15 个月），被 MinerU 2/3.x 取代。

### 2.2 许可证（重大变化）

| 时点 | 许可证 | 说明 |
|---|---|---|
| ≤3.0.x（2026-03 及以前，含全部 2.x） | **AGPLv3** | 根源是其依赖 ultralytics(YOLO)/doclayout_yolo 为 AGPL |
| ≥**3.1.0**（2026-04-18 起） | **MinerU Open Source License**（Apache 2.0 + 附加条款） | 官方声明已逐步移除 AGPL 组件 |

附加条款全文要点（LICENSE.md，已核验原文）：
1. **商业许可门槛**：MAU > 1 亿 或 月收入 > $2000 万才需购买商业许可 —— 对本项目完全不触发；
2. **在线服务标识义务**：基于 MinerU 向第三方提供在线服务须显著标明 —— GUI 本地工具不受影响，若未来部署公共服务在 about/文档注明即可；
3. 违反则自动终止授权。

对本项目（本身 AGPL-3.0）：Apache 系代码并入 AGPL 项目方向合法；历史 AGPL 版本并入亦无冲突（同栈协议）。**结论：无论引入哪个版本的 MinerU，协议层面均无障碍**，仅需在关于页/third-party 声明中保留署名与许可文本。模型权重需单独留意：旧 PDF-Extract-Kit/YOLO 系权重历史上为 AGPL，layoutreader 曾为 CC BY-NC-SA（禁商用）；3.x 新模型随主协议走，但用户缓存目录中残留旧权重时的合规状态由用户侧自担（本项目仅做解析编排，不分发权重）。

### 2.3 Python 支持与打包形态

- `requires-python = ">=3.10,<3.14"`，官方矩阵 **3.10–3.13**；Windows 上个别后端因 `ray` 依赖限 3.10–3.12（涉及 vlm-engine/hybrid 本地推理，**pipeline 后端不受影响**）。⚠️ 本项目主力环境 Py3.13 + Windows 是否跑通 pipeline 需目标机实测；
- 单包 setuptools 结构，**wheel 仅 1.5MB**（纯 Python）；重依赖全在 extras：`pipeline`（torch/torchvision/transformers/onnxruntime）、`vlm`、`vllm/lmdeploy/mlx`、`gradio` 等；
- 模型权重不入库，运行时从 HuggingFace/ModelScope 下载（`MINERU_MODEL_SOURCE=modelscope` 可切国内源）。

### 2.4 编程 API（对适配器影响最大）

官方编程入口（`mineru/cli/common.py`）：

```python
from mineru.cli.common import do_parse, aio_do_parse, read_fn

do_parse(
    output_dir="./results",
    pdf_file_names=[stem],
    pdf_bytes_list=[pdf_bytes],
    p_lang_list=["ch"],
    backend="pipeline",          # 本地 OCR/版面/公式/表格模型
    parse_method="auto",         # auto/txt/ocr
    start_page_id=..., end_page_id=...,   # 页码范围（pypdfium2 预切片）
    f_dump_middle_json=True,     # ← middle.json！
)
```

- 输出四件套：Markdown / **middle-json** / content-list / model-json；
- **不存在** `mineru.document.Document` 类 —— 我们适配器 `_parse_mineru` 中的 `Document.parse(pdf_path, dpi=200, language="ch", callback=None)` 是当时"best-effort 臆测"，任何装了真实 mineru 的环境都会 ImportError→被包装成 `MagicPdfNotInstalledError`，即 **MinerU 路径从未真正工作过**；
- 关键收益：`do_parse(f_dump_middle_json=True)` 产出的 middle.json 与 magic-pdf 1.x 同构，**现有 `_normalize_blocks`/`MagicPdfBridge` 全链可直接复用**，重写适配器的成本远低于预期。

## 三、"引入并继续维护"的三种含义拆解

| 含义 | 实际需求 | 对应手段 |
|---|---|---|
| A. 固定版本、可审计、离线可重建 | 锁定经过验证的上游 commit，任何人 clone 即得一致源码 | **git submodule pin**（或纯 pip pin） |
| B. 携带本地补丁并长期演进 | 进度回调 hook、Windows 兼容补丁、裁剪等改源码需求 | **fork + 补丁分支 + submodule** |
| C. 只是想要"能用起来"（Py3.13、细粒度进度、不再黑盒） | 升级依赖 + 重写适配器即可 | **pip pin 升级，无需 vendor** |

先回答 C：本次细粒度进度工作（P1）暴露的问题大多不需要 vendor 就能解决 —— 上游 3.x 已原生支持 Py3.13（解除我们的门控动机）、进度可经日志探针获得（同 magic-pdf 手法）、API 重写是适配器层工作。**vendor 解决的是 A/B 类诉求**。

## 四、引入方式对比

| 方案 | 补丁自由度 | 升级成本 | 主仓体积/复杂度 | CI 影响 | 分发物影响 | 适用场景 |
|---|---|---|---|---|---|---|
| **pip pin（现状强化）** | ✗（只能 monkey-patch） | 低（改版本号） | 零 | 零 | 无 | 默认推荐起步 |
| **git submodule（上游直连）** | △（补丁只能在适配层） | 中（bump commit + 回归） | +浅克隆几十 MB 开发态；不影响发布物 | 需补 submodules 配置 | 无（引擎仍是用户侧安装） | 方案 A 诉求 |
| **fork + 补丁分支 + submodule** | ✓ | **高**（持续 rebase 上游 4–5 天一发的节奏） | 同上 + 一个 fork 仓库维护 | 同上 | 无 | 方案 B 诉求（确需改源码） |
| git subtree | ✓ | 高（合并噪音大，历史污染） | 明显增大 | 大 | 无 | 不推荐 |
| 源码快照 vendor | ✓ | 极高（手工搬运） | 大幅增大 | 大 | 无 | 不推荐 |
| 运行时引导下载（git clone/pip 到用户机） | △ | 低 | 零 | 零 | 不变（现状 selftest 指引的延伸） | 桌面包场景补充 |

## 五、技术可行性细节（若采用 submodule）

### 5.1 布局与消费模式（复用 next 先例）

```
vendor/MinerU                    # .gitmodules 指向 opendatalab/MinerU（或自家 fork）
pdf2zh/kernel/mineru_kernel.py   # 新内核适配器：仿 precise.py
  ├─ _SUBMODULE_DIR 存在性检查 → 缺失时报 "git submodule update --init vendor/MinerU"
  ├─ 隔离 venv 创建 + pip install "<submodule_dir>[pipeline]"（torch 等重依赖只进 venv）
  └─ 子进程调用（CLI `mineru -p in.pdf -o out -b pipeline` 或 python -c do_parse）
     → 消费 out/{stem}_middle.json → 复用 MagicPdfAdapter.from_middle_json 归一化
```

**强烈建议子进程隔离而非进程内 import**，理由：
1. 本项目已有 torch×onnxruntime CUDA DLL 加载顺序前科（`_preload_torch` 是 magic-pdf 专用防御），mineru 的 torch 栈在同进程共存风险同类；
2. pymupdf 版本冲突有文档化前科（babeldoc vs magic-pdf），venv 天然免疫;
3. `do_parse` 以文件为输出界面，文件交换零序列化成本；
4. 子进程崩溃不拖垮服务进程（解析期 OOM 风险现实存在，官方磁盘要求 20GB+）。

### 5.2 各面影响清单

| 面 | 影响 | 说明 |
|---|---|---|
| 主包体积 | **零** | 子模块只在开发/源码部署形态存在；wheel/NSIS 不含 |
| 用户安装 | 不变 | 仍走 `pip install pdf2zh[magicpdf]` 或 setup 引导；submodule 仅改变"我们从哪构建" |
| CI | 小改 | checkout 加 `submodules: vendor/MinerU`（或递归）；GPU 类测试仍需 runner 有模型权重，建议保持引擎测试 skip-by-default |
| 测试 | 小增 | 仿 test_granular_progress_p1 的 fake 注入测适配器逻辑；真实引擎路径标记 slow/optional |
| 协议合规 | 一行声明 | 关于页 + THIRD_PARTY 文件列 MinerU Open Source License 文本 |

## 六、风险与维护成本

| 风险 | 等级 | 缓解 |
|---|---|---|
| 上游 4–5 天/release，补丁分支漂移 | **高（仅方案 B）** | 补丁优先做成适配层 monkey-patch（本项目对 transformers/magic-pdf 已有成熟手法）；源码补丁小步提交、月度 rebase；CI 加"上游 weekly 冲突探测"job |
| API 大版本漂移（2.x Document 臆测已是一次教训） | 中 | 只对接官方承诺的 `do_parse` 稳定入口；middle.json 消费端做容错归一化（现有 `_normalize_block` 已高度防御） |
| Windows × Py3.13 × pipeline 未实测 | 中 | P0 第一步就是在目标机验证；不行再评估门控收紧范围 |
| 权重下载网络（HF 不通） | 低 | `MINERU_MODEL_SOURCE=modelscope` 写入 setup 引导与文档 |
| venv 安装体积（torch 系数 GB） | 低 | 与现状 magic-pdf 路线相同量级，非新增负担 |
| 双引擎并存的心智负担 | 低 | engine_env 自动选择逻辑保留，magic-pdf 兜底可在 mineru 3.x 验证后退役 |

## 七、建议方案（分层实施）

### P0 —— 不 vendor，先把 MinerU 路径修通（推荐立即做，预估 1–2 天）
1. `engine_env.py`：`MINERU_MAX_PY` 放宽至 3.13（保留 `PDF2ZH_MINERU_PREFER=0` 逃生口）；`probe_mineru` 增加 `import mineru.cli.common` 探测；
2. `pyproject.toml` magicpdf extra：`mineru>=3.1,<4; platform 三元组不限` 替代现条件 pin；magic-pdf 1.x 移入"legacy 兜底"注释级保留；
3. 重写 `_parse_mineru`：懒导入 `do_parse/read_fn` → 临时输出目录拿 `{stem}_middle.json` → `MagicPdfAdapter.from_middle_json` 复用现有归一化/桥接全链；OCR 态映射 `parse_method`；页码范围映射 `start/end_page_id`；
4. 细粒度进度沿用 P1 探针思路挂 loguru sink（mineru 同样用 loguru），Batch/组件正则按 3.x 日志实测调整；
5. 目标机（Win + Py3.13）实测 pipeline 后端，回填 README。

### P1 —— 引入 submodule 作为源码锚点（可选，半天）
- `.gitmodules` 增加 `vendor/MinerU` → 上游直连，pin 到 P0 验证通过的 release tag（如 `3.4.5-released`）；
- 提供 `pdf2zh-setup-mineru`（仿 `pdf2zh-setup-precise`）：从子模块构建隔离 venv；
- CI checkout 补 submodule 配置；此形态下**不改上游一行代码**，升级 = bump pin + 回归，成本可控。

### P2 —— fork + 补丁分支（仅当出现必须改源的硬需求再启动）
- 触发条件示例：需要深度进度回调（官方 callback 面不够用）、裁剪依赖树、紧急修复上游未发的 bug；
- fork 至个人账号，分支策略 `upstream-pin` + `patches/*`，submodule URL 指向 fork；补丁提交遵循"一个补丁一个主题"以便 rebase。

### 不做
- subtree / 源码快照 vendor（合并成本与本仓 diff 噪音不可接受）；
- 把 mineru 及其 extras 塞进发布 wheel / NSIS 包（2GB 上限 + torch 体积直接否决）。

## 八、结论

1. **能不能？能。** 协议（Apache 系附加条款 + 本仓 AGPL，双向兼容）、机制（next 子模块先例完整）、工程（middle.json 归一化链路可整体复用）三方面均无硬阻碍。
2. **该不该现在做 submodule？不必急。** 当前所有可见收益（Py3.13、去黑盒、修通 MinerU 路径）都能由 P0 的依赖升级 + 适配器重写达成；submodule 的增量价值（精确可复现构建）值得要但不紧迫。
3. **真正的坑不在"引入"而在"对接"。** 现有 `_parse_mineru` 的臆测 API 问题比引入方式问题严重得多 —— 无论是否 vendor，都必须按 `do_parse` 重写并在真实环境验证。
4. 若启动 submodule，**强烈绑定隔离 venv + 子进程模式**，绝不进程内 import；补丁管理优先 monkey-patch，源码 fork 是最后手段。

## 九、证据索引

| 主题 | 来源 |
|---|---|
| 上游仓库/README（版本、矩阵、许可变更公告） | https://github.com/opendatalab/MinerU |
| 新许可证全文（门槛/标识条款） | https://github.com/opendatalab/MinerU/blob/master/LICENSE.md |
| PyPI 元数据（3.4.5、requires-python<3.14、extras、wheel 1.5MB） | https://pypi.org/project/mineru/ |
| 发版节奏（213 releases、3.1.x 月内 15 个补丁） | https://releasealert.dev/github/opendatalab/MinerU |
| 官方编程 API（do_parse/aio_do_parse/read_fn） | https://opendatalab.github.io/MinerU/usage 及 `mineru/cli/common.py` |
| AGPL 历史根源（ultralytics/doclayout_yolo/layoutreader） | https://github.com/opendatalab/MinerU/issues/4060 及关联讨论 |
| magic-pdf 1.x 停更（最后 1.3.12 @2025-05-24） | https://pypi.org/project/magic-pdf/ |
| 本仓子模块先例 | `.gitmodules`、`pdf2zh/kernel/precise.py`、`pyproject.toml`（pdf2zh-setup-precise 入口） |
| 本仓引擎门控/兜底现状 | `pdf2zh/engine_env.py:26-103`、`pyproject.toml:68-73`、`magicpdf_adapter.py:851+` |
