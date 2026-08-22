# 双轨收尾测试报告：BabelDOC ↔ magic-pdf 双引擎 E2E + 全量回归

- **日期**：2026-08-22 ｜ 分支：main（基于 1eac5e5，工作区未提交改动之上）
- **范围**：上一轮修复（xobject_strip ncs / magicpdf_adapter pages_filter / magicpdf_renderer 数值断言）后的收尾验证；BabelDOC 与 magic-pdf 双轨真实 PDF 端到端翻译
- **结论**：**双轨 E2E 全部通过，全量回归 2652 passed / 0 failed**。过程中定位并修复 4 个真实缺陷（torch DLL 顺序冲突、熔断乒乓、CLI dual 模式不一致、输入校验缺失），新增 9 条定向测试，ruff 告警维持基线 28 条零新增。

---

## 1. 全量回归

```
python -m pytest tests/ -q -p no:cacheprovider
→ 2652 passed, 6 skipped, 0 failed （145s）
```

基线对照：收尾前 2643 passed（含 7 个既有失败已在上一轮修复）；本轮新增 9 条定向测试后全绿。
ruff（改动文件）：28 条告警 = HEAD 基线逐条对应，**零新增**。

## 2. 双轨 E2E 实测（真实 PDF → 真实引擎）

样张：A4 合成文档（标题 + 3 段英文 + 数字 4.2/3.9），`fitz` 生成。

| 轨道 | 命令 | 产物 | 验证 |
|---|---|---|---|
| BabelDOC | `--parse-engine babeldoc -s google` | `{stem}.zh.mono.pdf` / `{stem}.zh.dual.pdf` | mono：120 中文字符、数字保留 ✅；dual：**交替页 2 页**（p0 原文 en=349，p1 译文 zh=120）✅ |
| magic-pdf | `--parse-engine magicpdf -s google` | `magicpdf/{stem}_magicpdf.json` + `_document.json` + `_render_plan.json` + `_mono.pdf` | mono：中文文本层 ✅、`4.2`/`3.9` 保留 ✅；**device status: torch=2.13.0+cu126, torch_cuda=True, device-mode=cuda, effective=cuda**（真 GPU）✅ |

magic-pdf 全链路（解析 → OCR(auto) → 翻译 → 公式侧通道 → RenderTakeover fixup → 渲染）无降级、无 Traceback；预检命中扫描信号自动开 OCR 为既有设计行为。

## 3. 过程中发现并修复的缺陷

### 3.1 onnxruntime CUDA 会话污染进程 DLL 环境，阻断 torch 导入 🔴

**现象**：CLI 显式 `--parse-engine magicpdf` 必然失败：`magic-pdf import failed: [WinError 127] Error loading "torch\lib\cudnn_cnn64_9.dll"` → 熔断降级 legacy。

**根因**：`main()` 全局入口无条件执行 `OnnxModel.load_available()`（创建 ORT CUDA/TensorRT 会话做执行级探测）。Windows 上 ORT 先加载自带 cuDNN DLL 后，同进程再 `import torch` 时加载器解析到已驻留的冲突 DLL 必然失败。实测复现：

```
resolve_providers(None) → ['CUDAExecutionProvider','CPUExecutionProvider']  # 先建会话
import torch                                                             # → WinError 127
# 反向顺序（先 torch 后会话）全部正常
```

而 magic-pdf 1.3.12 全部子模型为 PyTorch 实现——该顺序冲突使其在 CLI 进程内**永远不可用**（此前报告归因为「环境缺 cudnn DLL」，实际为代码级加载顺序缺陷）。

**修复**：
- 版面分析模型从 CLI 全局入口下沉到消费它的轨道懒加载：新增 `_ensure_doclayout_model(parsed_args)`（幂等，尊重 `--onnx` 与已加载单例），仅在 `_run_legacy_kernel()` 与 `yadt_main()` 开头调用（legacy 内核自身已有同款兜底，行为不变）；
- `run_magicpdf_main()` 入口新增 `_preload_torch()` 兜底：先于任何 ORT 会话把 torch 驻留 `sys.modules`，覆盖 API/GUI 服务进程等其它进入形态。

**效果**：magic-pdf 轨 device status 从 `torch=-, torch_cuda=False` 变为 `torch=2.13.0+cu126, effective=cuda`。

### 3.2 熔断降级乒乓循环 🟡

**现象**：magicpdf 解析失败降级 legacy 后，`_run_legacy_kernel` 的文本层预检再次命中扫描信号 → 自动切回 magicpdf → 再失败 → 再降级（一次运行内两次完整引擎冷启动，日志成对重复）。

**修复**：`_fallback_legacy()` 打 `parsed_args._magicpdf_fallback = True` 防重入标记；`_try_auto_switch_magicpdf()` 看到标记直接返回 False（本进程中 magic-pdf 已被证实不可用，不再切回）。

### 3.3 CLI babeldoc 轨 dual 输出与 adapter 不一致 🟡

**现象**：CLI `--parse-engine babeldoc` 的 dual.pdf 为 BabelDOC 默认 side-by-side 同页合并（页宽 1190≈2×595），而 RuntimeService 的 `babeldoc_adapter` 路由为交替页模式（`use_alternating_pages_dual=True`）——同一功能两个入口行为分叉。

**修复**：`yadt_main` 的 `YadtConfig` 补传 `use_alternating_pages_dual=True`。实测 dual 由 1 页 side-by-side 变为标准 2 页交替（原文页/译文页）。

### 3.4 CLI 输入存在性缺失 🟢

**现象**：不存在的输入文件在下游 `open()` 才暴露（目录输入报 PermissionError、不存在报 FileNotFoundError 且带深层调用栈）。

**修复**：`main()` 在解析引擎路由前校验非 `--dir` 模式下所有输入 `os.path.isfile`，失败即抛带文件名的明确 `FileNotFoundError`。

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `pdf2zh/pdf2zh.py` | 移除全局入口的 `OnnxModel.load_available()`；新增 `_ensure_doclayout_model`（legacy/yadt 轨懒加载）；`_try_auto_switch_magicpdf` 尊重 `_magicpdf_fallback` 标记；`yadt_main` 补 `use_alternating_pages_dual=True`；`main()` 新增输入存在性校验 |
| `pdf2zh/magicpdf_cli.py` | 新增 `_preload_torch()` 并在 `run_magicpdf_main` 入口调用；`_fallback_legacy` 打防重入标记 |
| `tests/test_cli.py` | +4：输入校验 ×2、模型懒加载幂等/`--onnx` 重建 ×2 |
| `tests/test_magicpdf_cli.py` | +5：torch 预载成功/失败容错/调用次序、防乒乓标记/跳过 ×2 组 |
| `tests/test_kernel.py` | 3 个内核路由用例补 `os.path.isfile` 补丁（意图为路由验证，配合 3.4 的新校验） |

## 5. 遗留事项

1. **serve/服务进程形态的同类风险**：RuntimeService 进程若先跑过 BabelDOC/legacy 轨再切 magicpdf，仍可能触发 DLL 顺序问题（`_preload_torch` 已兜底大部分场景；如复现可考虑 magicpdf 解析改独立子进程）。
2. **preflight 对合成样张的误报**：`font_to_unicode: 1.000 >= 0.60` 将简单合成 PDF 判为扫描/损坏信号并自动开 OCR，属阈值偏敏感（不影响正确性，多付 OCR 开销），可后续单独调优。
