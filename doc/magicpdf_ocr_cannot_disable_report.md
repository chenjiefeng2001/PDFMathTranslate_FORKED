# MinerU（magic-pdf）OCR 无法关闭 —— 根因分析与三态开关修复报告

> **日期**：2026-08-19
> **范围**：PDFMathTranslate_FORKED（pdf2zh 1.9.x 系）
> **触发问题**：用户反馈 **minerU OCR 不支持关闭**——无论走哪条解析链路，即使 GUI 中**未勾选** MagicPDF OCR，只要翻译前预检命中扫描/损坏信号，系统仍会自动开启 OCR；对「扫描版但自带文本层」（非纯 OCR 版本）的 PDF，强制 OCR 反而导致**文本错位**。
> **结论摘要**：根因是 magic-pdf 链路的 OCR 只有二态（`--magicpdf-ocr` bool），而**自动预检强制开启逻辑（`magicpdf_cli.py` 的 `preflight_scan_check` + `pdf2zh.py` 的 legacy 自动切换）无视用户显式选择**，即用户没有「显式关闭 OCR」的途径。本次将 magic-pdf OCR 升级为**三态 `auto/on/off`**（与 BabelDOC 的 `--babeldoc-ocr` 对齐）：`off` 时预检命中也**绝不**强制开启，彻底解决「无法关闭」问题。BabelDOC 链路本已具备 `off` 选项（GUI `ocr_mode` / `PDF2ZH_BABELDOC_OCR`），无需改动。

---

## 1. 问题定义

用户实际遇到两类场景：

| 场景 | 用户预期 | 实际行为 |
|---|---|---|
| 纯扫描件（无文本层） | 自动 OCR | 预检命中 → 自动 OCR（符合预期） |
| **扫描版但自带文本层**（非 OCR 版本，文本层损坏或 ToUnicode 缺失） | **不 OCR，走文本层** | 预检命中扫描/损坏信号 → **被强制 OCR** → OCR 结果与文本层冲突，**文本错位** |

关键矛盾：`scan_damaged_text_investigation_report.md`（2026-08-16）引入的「翻译前文本层质量预检 + 自动 OCR 兜底」解决的是**第一类**问题，但把**第二类**（有文本层的扫描版）也一并强制 OCR——这类 PDF 用户希望保留文本层路径，且 OCR 会导致错位。**系统缺少一个尊重用户显式选择的「关闭 OCR」开关。**

---

## 2. 根因分析：三条链路强制开启 OCR

### 2.1 magic-pdf 解析链路（`pdf2zh/magicpdf_cli.py`）

```python
# 修改前（magicpdf_cli.py run_magicpdf_main）
ocr = bool(getattr(parsed_args, "magicpdf_ocr", False))   # 用户未选 → False
...
for path in files:
    if not ocr:                                            # ← 仍会执行预检！
        decision = preflight_scan_check(path)
        if decision.is_scanned:
            ocr = True                                     # ← 强制开启，用户无法关闭
```

即使 GUI「MagicPDF OCR」未勾选（`magicpdf_ocr=False`），只要预检命中 `font_to_unicode >= 0.60` 等损坏信号，`ocr` 就被改写为 `True`。**这是「MinerU OCR 不支持关闭」的直接根源**——bool 开关的 `False` 只是「不主动开启」，不代表「禁止开启」。

### 2.2 legacy 自动切换链路（`pdf2zh/pdf2zh.py::_try_auto_switch_magicpdf`）

legacy 内核翻译前预检命中扫描/损坏信号，且 magic-pdf/MinerU 可用时：

```python
parsed_args.parse_engine = "magicpdf"
parsed_args.magicpdf_ocr = True      # ← 强制切换引擎并开启 OCR
```

即使用户在 GUI 中明确选择了 legacy 引擎、也未勾选 OCR，也会被**自动切换并强制 OCR**。

### 2.3 BabelDOC 链路（`pdf2zh/babeldoc_ocr_mode.py::resolve_ocr_flags`）

`auto` 模式下传入 `source_path` 命中扫描信号时强制 `ocr_workaround=True`。**但**该链路已有三态（`--babeldoc-ocr auto|on|off`、GUI `ocr_mode` radio、环境变量 `PDF2ZH_BABELDOC_OCR`），用户显式选 `off` 即完全关闭（`skip_scanned_detection=True`）。故**无需改动**，仅需文档说明。

---

## 3. 修复方案：magic-pdf OCR 三态化（auto/on/off）

与 BabelDOC 的 `--babeldoc-ocr` 对齐，将 magic-pdf OCR 从二态升级为三态：

| 模式 | CLI | GUI | 行为 |
|---|---|---|---|
| `auto`（默认） | `--magicpdf-ocr-mode auto`（缺省） | Radio「自动（预检决定）」 | 预检命中扫描/损坏信号才自动开启 OCR（历史行为保留） |
| `on` | `--magicpdf-ocr-mode on` / `--magicpdf-ocr` | Radio「强制 OCR」 | 强制对所有 PDF 执行 OCR |
| `off` | `--magicpdf-ocr-mode off` | Radio「关闭 OCR」 | **显式关闭，预检命中也绝不强制开启**（新增能力） |

### 3.1 兼容性设计

- `--magicpdf-ocr`（历史 bool 开关）**保留**，等价 `--magicpdf-ocr-mode on`，且优先于 mode（`resolve_magicpdf_ocr_mode` 中 bool=True 直接返回 `"on"`）；
- 新参数 `--magicpdf-ocr-mode {auto,on,off}`，缺省 `auto`；
- 服务层 `TranslationRequest` 新增 `magicpdf_ocr_mode: str = "auto"` 字段，`magicpdf_ocr: bool` 保留兼容；
- GUI worker 新增 `_magicpdf_ocr_mode/_magicpdf_ocr_bool` 归一化辅助函数，兼容旧调用方传入 bool；
- `on_translate` 快照/重试仍为 25 元素，位置与长度不变。

### 3.2 修改点清单

| 文件 | 修改 |
|---|---|
| `pdf2zh/pdf2zh.py` | 新增 `--magicpdf-ocr-mode` 参数；新增 `resolve_magicpdf_ocr_mode()`；`_try_auto_switch_magicpdf` 在 `off` 时跳过自动切换 |
| `pdf2zh/magicpdf_cli.py` | `run_magicpdf_main` 用三态决定 `ocr`；`off` 时跳过预检强制 |
| `pdf2zh/services/runtime_service.py` | `TranslationRequest` 新增 `magicpdf_ocr_mode`；`_execute_magicpdf` 透传至 ns |
| `pdf2zh/gui/components/config_panel.py` | MagicPDF OCR 由 Checkbox 改为三态 Radio（默认 `auto`） |
| `pdf2zh/gui/worker.py` | 参数 `magicpdf_ocr: str = "auto"` + 归一化辅助函数 |
| `pdf2zh/gui/app.py` | `on_translate` 参数类型 `bool → str` |
| `pdf2zh/gui/i18n.py` | 新增 `config_magicpdf_ocr_auto/on/off` 文案，更新 info |
| `tests/test_magicpdf_ocr_mode.py` | **新增 15 项三态专项测试** |
| `tests/test_magicpdf_cli.py` / `test_parse_engine_switch.py` | 适配三态字段 |

---

## 4. 验证

### 4.1 单元测试（全部通过）

```
tests/test_magicpdf_ocr_mode.py  15 passed  # 新增
tests/test_magicpdf_cli.py                 # 适配后通过
tests/test_parse_engine_switch.py  9 passed # 适配后通过
tests/test_babeldoc_ocr_mode.py            # 回归通过
tests/test_text_quality_gate.py            # 回归通过
```

### 4.2 关键行为验证

| 场景 | 结果 |
|---|---|
| `--magicpdf-ocr-mode off` + 预检命中扫描信号 | `adapter.parse(ocr=False)`——**不开启 OCR** ✅ |
| `--magicpdf-ocr-mode on` | `adapter.parse(ocr=True)` ✅ |
| `--magicpdf-ocr-mode auto` + 预检命中 | `adapter.parse(ocr=True)`（历史行为保留）✅ |
| `--magicpdf-ocr`（历史 bool） | 等价 `on`，优先于 mode ✅ |
| `--magicpdf-ocr-mode off` + legacy 预检命中 + magicpdf 可用 | **不自动切换**，保持 legacy ✅ |
| GUI 三态 Radio | `auto`→`auto`、`on`→`magicpdf_ocr=True`、`off`→`off` 正确透传 ✅ |

---

## 5. 「允许切换 BabelDOC 为 magic-pdf」可行性结论（承接既有文档）

用户同时关注「允许切换 BabelDOC 更换为 magic-pdf 的可能性」。该问题已由以下文档完整覆盖，**本次不重复实现**：

- `doc/babeldoc_to_magicpdf_feasibility_report.md`（2026-08-16）：BabelDOC 是「解析+翻译+排版+渲染」一体化引擎，magic-pdf 是纯解析引擎，**不具备对等替换关系**；可行边界是「解析层替换」+「解析能力增强」。
- `doc/babeldoc_to_magicpdf_switch_report.md` + `switch_landing_report.md`：`--parse-engine auto|legacy|babeldoc|magicpdf` 切换已落地，含环境阻断修复。
- 本报告补充：**切换后 OCR 行为现在可由用户完全控制**（三态），这是该切换链路可靠性的关键一环。

| 切换诉求 | 现状 |
|---|---|
| CLI 切换引擎 | ✅ `--parse-engine magicpdf`（已支持） |
| GUI 切换引擎 | ✅ 「解析引擎」Radio（auto/legacy/babeldoc/magicpdf） |
| 切换后 OCR 可关闭 | ✅ **本次修复**：`--magicpdf-ocr-mode off` / GUI「关闭 OCR」 |
| 引擎不可用时降级 | ✅ 熔断降级 legacy（`_fallback_legacy`） |

---

## 6. 使用说明

### CLI

```bash
# 显式关闭 magic-pdf OCR（有文本层的扫描版首选）
pdf2zh input.pdf --parse-engine magicpdf --magicpdf-ocr-mode off

# 强制 OCR（纯扫描件）
pdf2zh input.pdf --parse-engine magicpdf --magicpdf-ocr-mode on

# 默认 auto：预检命中扫描/损坏信号才自动开启
pdf2zh input.pdf --parse-engine magicpdf
```

### GUI

「解析引擎」= Magic-PDF/MinerU 时，「MagicPDF OCR」Radio 提供三选一：
**自动（预检决定）** / **强制 OCR** / **关闭 OCR**。默认「自动（预检决定）」。

### BabelDOC 链路（已有能力，无需升级）

GUI `ocr_mode` Radio 选 **off**，或 CLI `--babeldoc-ocr off`，或环境变量 `PDF2ZH_BABELDOC_OCR=off`，即可关闭 BabelDOC 的扫描检测与 OCR workaround。

---

## 7. 边界与后续建议

1. **`auto` 仍可能误判**：对「文本层损坏但视觉正常」的扫描版，预检可能命中损坏信号而自动开启 OCR。需要完全走文本层的用户应显式选 `off`；
2. **预检判定收紧**（后续可做）：`scanned_detection.preflight_scan_check` 可将「有文本层但仅 ToUnicode 损坏」与「纯扫描无文本层」区分开（如引入 glyph 覆盖率 / 文本对象数量阈值），`auto` 模式下只对后者自动 OCR，减少误判；
3. **文档同步**：`doc/scan_damaged_text_investigation_report.md` 中的「自动开启 OCR」描述已随本报告更新为三态语义。
