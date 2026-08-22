# OpenCode 接入集成报告

> 日期：2026-08-21 ｜ 分支：main（基于 1eac5e5）｜ opencode CLI 版本：1.18.20

## 一、目标

将 [opencode](https://opencode.ai)（开源 AI coding agent CLI）作为 pdf2zh 的翻译引擎接入
（方案 A），并确保完整翻译管线对接正确。

## 二、改动清单

### 1. `pdf2zh/translator.py` — 新增 `OpenCodeTranslator`（约 L1423）

双模式翻译引擎，遵循 `BaseTranslator` 约定（envs 四层合并 / TranslationCache / prompt 模板）：

| 模式 | 触发条件 | 实现 |
|---|---|---|
| CLI 子进程 | 默认 | `opencode run --format json`，提示词经 stdin 传入；解析 JSONL 事件流中 `type=text` 的 part |
| serve 常驻 | 设置 `OPENCODE_SERVER_URL` | HTTP API：`POST /session` → `POST /session/:id/message` → 提取响应 parts → `DELETE /session` |

配置项：

| envs 键 | 默认 | 说明 |
|---|---|---|
| `OPENCODE_PATH` | `opencode` | CLI 路径；经 `shutil.which` 解析（Windows npm `.cmd` shim 兼容） |
| `OPENCODE_MODEL` | 空 | `provider/model`（如 `opencode/gpt-5`）；留空用默认模型 |
| `OPENCODE_AGENT` | 空 | 可选 agent 名 |
| `OPENCODE_TIMEOUT` | `300` | 单次请求超时（秒） |
| `OPENCODE_SERVER_URL` | 空 | serve 模式服务地址（如 `http://127.0.0.1:4096`） |

其他要点：
- tenacity 重试（3 次指数退避，`reraise=True` 保留原始异常）
- 构造时自检：CLI 模式跑 `--version`；serve 模式探 `/global/health`
- `complete_raw(messages)`：发送原始对话（不经翻译提示词包装），供 v3 Provider 复用
- 注册进 `build_translator` 工厂（支持 `"opencode:provider/model"` 路由语法）

### 2. `pdf2zh/config.py` — 修复 `_remove_circular_references` 既有 bug

原实现对所有对象记录 `id()`，而 CPython 驻留的重复字符串（如 translator name 与 envs 值同为
`"opencode"`、两个 `""` 字面量）被误判为循环引用改写为 `null`，导致配置损坏。现仅对
dict/list 做循环检测（不可变标量不可能成环）。

### 3. `pdf2zh/mcp_server.py` — MCP 工具参数化

`translate_pdf` 新增 `engine: str = "google"` 参数并透传给 `translate_stream(service=engine)`，
MCP 客户端（含 opencode 自身）可指定任意已注册引擎。

### 4. `pdf2zh/v3/translator.py` — 新增 `OpenCodeProvider`

实现 v3 `LLMProvider` 抽象，内部委托 `OpenCodeTranslator.complete_raw()`，与旧版共享传输层
（子进程/serve 双模式保持同步）。v3 特性开关开启后即可使用。

### 5. `pdf2zh/gui.py` — service_map 注册 `"OpenCode"`

### 6. `pdf2zh/pdf2zh.py` — CLI `--babeldoc` 路径注册 OpenCodeTranslator

`yadt_main` 使用独立于 `build_translator` 工厂的硬编码引擎列表；补入 `OpenCodeTranslator`
后 CLI `pdf2zh x.pdf --babeldoc -s opencode` 可直接运行。

### 7. `pdf2zh/high_level.py` — 修复整页空白的渲染阻断 bug（详见第四节）

移除 4 处 `doc.write(..., clean=True, ...)` 中的 `clean=True`（L912/913、L1301/1309）。

## 二点五、BabelDOC 管线的 OpenCode 支持

BabelDOC 有两条管线，支持状态与打通方式不同：

| 路径 | 入口 | 引擎解析 | opencode 支持 |
|---|---|---|---|
| legacy adapter | `babeldoc_adapter.run_babeldoc_translation`、CLI `yadt_main` | `build_translator` 工厂 / yadt_main 引擎列表 | ✅ 直接支持 |
| pdf2zh_next 内核 | `babeldoc_next_adapter.run_babeldoc_next_translation`（RuntimeService 优先尝试） | `_build_engine_settings` 硬编码映射表 | ❌ 无映射 → `BabeldocNextUnavailableError` |

RuntimeService 的分发逻辑（runtime_service.py L1843+）保证最终可达：

1. `pdf2zh_next` 未安装（本机实测如此）→ 直接走 legacy adapter → `build_translator("opencode")` ✅
2. `pdf2zh_next` 已安装 → next 内核对 opencode raise → 捕获后自动回退 legacy adapter ✅

即**任何环境形态下 babeldoc 管线都能落到 opencode**，无需改动 next 内核映射表
（vendored 子仓库，避免维护分叉；如需原生支持可后续在 `_build_engine_settings`
中仿照 `claude`→`ClaudeCodeSettings` 增加 opencode 映射）。

实测结果见第三节 3.4。

## 三、测试结果

### 3.1 单元测试（tests/test_translator_opencode.py，12 个，全部通过）

覆盖：JSONL 解析、工厂注册与模型路由、CLI 参数组装、非零退出重试、serve 会话生命周期、
空响应报错、ConfigManager 隔离。不依赖真实 opencode 安装。

```
12 passed in 14.50s
```

### 3.2 真实模型调用（文本级）

| 用例 | 结果 | 耗时 |
|---|---|---|
| 默认模型（CLI 模式） | PASS → 「你好，世界！这是对 OpenCode 翻译引擎的测试。」 | ~10s |
| `opencode:opencode/gpt-5` | PASS | ~11s |
| `opencode:opencode/claude-haiku-4-5` | PASS | ~10s |

已知限制：
- `gpt-5-nano` 会原样回显不翻译（弱模型指令遵循问题，非接入缺陷）
- serve HTTP API 下显式指定非默认模型会静默返回空响应（finish=None，1.18.20 实测；
  CLI 模式无此问题）。serve 模式建议留空 `OPENCODE_MODEL` 用服务端默认模型——
  空响应已被显式检测并抛出带指引的错误

### 3.3 端到端管线测试（真实 PDF → opencode 引擎 → 输出验证）

输入：A4 合成文档（标题 + 3 段英文，布局模型判定为 plain text/title，无公式干扰）。
验证方式：输出 PDF 文本层提取中文 + 渲染 PNG 人工核对。

| 用例 | 结果 | 耗时 | 文本层 | 视觉渲染 |
|---|---|---|---|---|
| CLI 子进程模式 | **PASS** | 15.7s | 中文提取成功 | 标题+三段译文排版正常，数字 4.2/3.9 保留 |
| serve 常驻模式 | **PASS** | 10.0s | 中文提取成功 | 同上 |

serve 模式省去每段翻译的 CLI 冷启动，单页 4 段节省约 35% 耗时；长文档收益更大。

### 3.4 BabelDOC 管线实测（CLI `--babeldoc -s opencode`）

输入同 3.3。命令：`pdf2zh sample.pdf --babeldoc -s opencode -li en -lo zh`

| 输出 | 结果 |
|---|---|
| `sample.zh.mono.pdf` | **PASS**：387 字符中文文本层，渲染正常（标题+三段译文+数字保留，含 BabelDOC 标准页眉） |
| `sample.zh.dual.pdf` | **PASS**：1380 字符，中英对照 |

RuntimeService 回退链路程序化验证：
- `_build_engine_settings('opencode')` 在 next 内核环境 raise `BabeldocNextUnavailableError`（预期）
- 本机（pdf2zh_next 未安装）：`build_translator('opencode')` → `OpenCodeTranslator`，
  经 `make_babeldoc_translator` 包装为 YADT translator 成功

### 3.5 回归测试

```
1049 passed, 6 skipped, 7 failed（全部为预先存在，与本报告改动无关）
```

失败项归因（均已验证与本次改动无关）：
- `test_magicpdf_adapter/renderer`（4+1）：环境缺 magic-pdf/torch cudnn DLL（WinError 127）；
  其中 `test_pages_filter` 在工作区未提交的 magicpdf_adapter.py 上失败、HEAD 版本通过
- `test_xobject_strip`（3）：HEAD 提交中即存在的 `PDFPageInterpreterEx.ncs` 属性缺失 bug

ruff 关键规则（F/E9）扫描：本次改动行零新增问题（16 项告警均为既有代码）。

## 四、顺带发现并修复的既有缺陷

### 4.1 渲染阻断 bug（high_level.py，影响所有引擎）

**现象**：任何翻译引擎（google/opencode）输出的 mono/dual PDF 整页空白，文本层为空。

**定位过程**：converter 产生的指令流完好（`BT q 1 1 1 rg ... re f Q /noto Tf Tm [...] TJ`），
最终 PDF 内容流被破坏（操作数与算子错位、`Tf/Tm` 丢失、`TJ` 脱离 BT/ET 文本块）→
A/B 对照锁定 `doc.write(clean=True)` 触发的 MuPDF 内容流消毒器为破坏源。

**修复**：移除 4 处 `clean=True`。A/B 验证：clean=True → text_len=0；clean=False →
text_len=275 且视觉正常。

### 4.2 配置持久化损坏 bug（config.py）

见第二节第 2 条。该 bug 会把任何「值与其他字符串对象重复」的 envs 写成 null，
此前若发生过会导致对应 translator 配置静默失效。

## 五、使用指南

```bash
# 方式一：CLI 子进程模式（零前置准备）
pdf2zh input.pdf -s opencode -li en -lo zh
# 指定模型
pdf2zh input.pdf -s "opencode:opencode/gpt-5"

# 方式二：serve 常驻模式（推荐长文档，免去每段冷启动）
opencode serve --port 4096
# 环境变量或 config.json 中设置 OPENCODE_SERVER_URL=http://127.0.0.1:4096
pdf2zh input.pdf -s opencode

# BabelDOC 管线（legacy adapter 直接支持；RuntimeService 自动回退到该路径）
pdf2zh input.pdf --babeldoc -s opencode
```

GUI：旧版 Gradio 下拉框选择 "OpenCode"；新 GUI 的 babeldoc 模式经 RuntimeService
回退链路生效。

## 六、遗留事项与建议

1. **新 GUI ENGINES 列表未注册 opencode**：`gui/components/config_panel.py` 的 ENGINES
   需同时打通 `babeldoc_next_adapter._build_engine_settings()` 映射。当前新 GUI 选择
   opengine=opencode 时经 RuntimeService 回退链路（next 内核失败 → legacy adapter）
   仍可工作；如需 next 内核原生支持，可仿照 `claude`→`ClaudeCodeSettings` 增加映射。
2. **serve 模式显式模型的静默失败**：疑似 opencode 1.18.20 服务端问题，建议关注上游修复；
   当前已有防御性报错。
3. **每段 ~10s 的 agent 开销**：opencode 是 agent 框架而非纯 LLM API，单次调用携带系统
   提示词/工具定义（~8k tokens）。大批量生产翻译仍建议直连 OpenAI 兼容端点
   （`openailiked` 引擎）；opencode 适合作为多模型聚合入口或需要工具增强的场景。
4. **test_xobject_strip 的 ncs 缺失** 与 **magicpdf_adapter 工作区修改导致的 test_pages_filter
   失败** 为独立问题，建议另行处理。
