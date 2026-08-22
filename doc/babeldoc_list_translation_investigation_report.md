# BabelDOC 列表翻译问题调查报告

> 日期：2026-08-21
> 范围：BabelDOC 0.6.4 布局引擎 + `pdf2zh/babeldoc_list_split.py` / `pdf2zh/babeldoc_toc_protect.py` 补丁
> 结论：列表**段落拆分补丁覆盖不足** + **编号前缀无保护** + **长列表项续行被割裂** 三层根因叠加，导致"列表翻译依旧有问题"。

---

## 1. 摘要

BabelDOC 0.6.4 的 `ParagraphFinder.process_independent_paragraphs` 原生只把
**行首为 bullet 符号**（`BULLET_POINT_PATTERN`，见 §3.1）的行拆成独立段落，
**不识别数字/字母编号列表**（`1. XXX`、`a. XXX`）。doclayout 版面模型通常把
连续列表识别为**单个 `plain text` 布局框**，所有列表项字符合并成一个
`PdfParagraph` —— 整段进入翻译器，翻译后重排时编号与正文混排、页面排版错乱。

fork 侧已有一个补丁 `pdf2zh/babeldoc_list_split.py`（在
`process_independent_paragraphs` 之后追加"编号行拆分"），CLI / GUI（legacy 与
next-kernel 两条 adapter 路径）均已挂载。但通过探针实测，该补丁仍存在以下
**未覆盖的真实列表样式**：

| # | 列表样式 | 是否拆分 | 后果 |
|---|---------|:---:|------|
| 1 | `1. First`（编号后带空格） | ✅ | 独立翻译、编号保留 |
| 2 | `1.First`（**编号后无空格**） | ❌ | 多个列表项合段整体翻译，编号被翻译引擎改写/丢弃 |
| 3 | `- dash item` / `– en-dash`（**连字符列表**） | ❌ | 同上，且 `-` 是英文文档最常见的列表标记 |
| 4 | `1）第一项` / `1．第一项` / `1・第一项`（全角/日文编号，编号后无空白） | ❌ | 同上 |
| 5 | `(i)` 罗马数字 / `①` 带圈数字 / `[1]` 方括号 / `(一)` 中文数字 / `第1条` | ❌ | 不识别（覆盖有限） |
| 6 | 长列表项跨物理行（首行 + 缩进续行） | ⚠️ 割裂 | 续行被拆成独立段落，列表项正文被割裂 |
| 7 | 编号前缀（`1.`/`a.`） | ⚠️ 无保护 | 编号是普通文本，随列表项进翻译器；LLM 改写/丢弃时列表失去编号 |

其中 **#2 / #3 / #4 / #6 是"依旧有问题"的最可能直接原因**。

---

## 2. 调查方法与实验证据

### 2.1 探针脚本

仓库根目录已有三份调试探针（本次调查复用并实测）：

| 脚本 | 作用 |
|------|------|
| `_tmp_babeldoc_probe.py` | 生成含多种列表样式的测试 PDF，跑 BabelDOC 到 ParagraphFinder 阶段，dump 段落结构 |
| `_tmp_fake_translate_probe.py` | 用 fake translator（`FAKE_MODE=keep/fullstop/drop`）跑完整 BabelDOC 翻译，dump 译后 mono PDF 文本 |
| `_tmp_list_probe.py` | 验证 `_LIST_ITEM_PREFIX_RE` 正则对 38 种行首模式的覆盖 |

运行命令（Windows PowerShell，UTF-8）：

```powershell
$env:PYTHONIOENCODING='utf-8'
python _tmp_babeldoc_probe.py        # 段落结构
$env:FAKE_MODE='keep'; python _tmp_fake_translate_probe.py   # 译后文本
```

> ⚠️ 探针 PDF 用 PyMuPDF `helv`（Helvetica）字体生成，**无 CJK/全角 glyph**，
> 中文字符在 PDF 文本层被替换为 `·`（U+00B7）。因此探针输出里"中文顿号列表
> 变成 `1····`"是探针 PDF 自身的假象，**不是产品 bug**；真实 PDF 带正确字体时
> 无此问题。本报告所有结论均基于 ASCII 列表样式（不受该假象干扰）推导。

### 2.2 段落结构探针结果（`_tmp_babeldoc_probe.py`）

对同一测试 PDF，应用 `apply_babeldoc_list_split()` + `apply_babeldoc_toc_protect()`
后的段落结构（节选）：

```
P01 [plain text] '1. First item'                              # ✅ 已拆为独立段
P02 [plain text] '2. Second item'                             # ✅
P03 [plain text] '3. Third item'                              # ✅
P04 [plain text] '1.First item without space | 2.Second item without space'   # ❌ 未拆
P06 [plain text] 'a. sub item one'                            # ✅
P07 [plain text] 'b. sub item two'                            # ✅
P08 [plain text] '- dash item one | - dash item two'          # ❌ 未拆
P09 [plain text] '1. This is a very long list item ... par'   # ⚠️ 长列表项首行
P10 [fallback_line] 'agraph'                                  # ⚠️ 被割裂的尾部
P11 [plain text] 'continued indented second line'             # ⚠️ 续行独立成段
P12 [plain text] '2. Second item'                             # ✅
```

### 2.3 译后文本探针结果（`_tmp_fake_translate_probe.py`，FAKE_MODE=keep）

fake translator 保留行首编号前缀，输出（节选）：

```
1. 苹果 香蕉          # ✅ 编号保留、独立成行
2. 苹果 香蕉
3. 苹果 香蕉
苹果 香蕉 橙子 葡萄 西瓜 草莓 ...   # ❌ '1.First ...' 合段翻译，'1.' 编号丢失
苹果 香蕉 橙子 葡萄 西瓜 草莓 香... # ❌ '- dash ...' 合段翻译，'-' 标记丢失
1. 苹果 ... 柠檬 ... 苹果          # ⚠️ 长列表项编号保留，但续行被割裂成独立行
2. 苹果 香蕉
```

---
---

## 3. 根因分析

### 3.1 BabelDOC 原生只拆 bullet，不拆编号（上游限制）

`babeldoc` 0.6.4
`format/pdf/document_il/midend/paragraph_finder.py:841-927` 的
`process_independent_paragraphs` 有三条拆段分支：

1. 前一行含 **≥20 个连续点**（`\.{20,}`）→ 目录条目拆段；
2. **短行拆分**：`split_short_lines` 开启且前一行宽 < `median_width × short_line_split_factor` → 拆段；
3. **行首为 bullet**：`is_bullet_point(行首字符)` → 拆段。

bullet 判定 `is_bullet_point` 用的是 `BULLET_POINT_PATTERN`
（`format/pdf/document_il/utils/layout_helper.py:50-51`）：

```
[■•⚫⬤◆◇○●◦‣⁃▪▫∗†‡¹²³⁴⁵⁶⁷⁸⁹⁰₁₂₃₄₅₆₇₈₉₀ᵃᵇᶜ...·]
```

**不含 `-`（连字符）/ `–`（en-dash），也不含数字编号**。这就是 fork 侧
`babeldoc_list_split.py` 补丁存在的根本原因。

### 3.2 补丁正则覆盖不足（直接根因 #2/#3/#4/#5）

`pdf2zh/babeldoc_list_split.py:55-57` 的匹配模式：

```python
_LIST_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:\(\d{1,4}\)|\d{1,3}[.．)]\s|\d{1,3}、|[A-Za-z][.、．.)]\s)"
)
```

具体缺陷（`_tmp_list_probe.py` 逐条实测）：

| 行首文本 | 匹配 | 原因 |
|---------|:---:|------|
| `1. First item` | ✅ | `\d{1,3}[.．)]\s` |
| `1、第一项` | ✅ | 顿号分支不要求空白 |
| `1.First no space` | ❌ | `[.．)]` 后强制 `\s`；但 `1.` 后接**字母**时绝不可能是小数/年份，可安全匹配 |
| `- dash item` | ❌ | 模式不含 `-`/`–` |
| `1）第一项` / `1．第一项` / `1・第一项` | ❌ | 编号后无空白（`）` 全角右括号不在 `[.．)]` 内；`・` U+30FB 不在内） |
| `(i) first` | ❌ | `\(\d{1,4}\)` 只匹配数字，不匹配罗马数字 |
| `① first` / `[1] ref` / `(一) 第一` / `第1条` | ❌ | 均不在模式内 |
| `2024.12 data` / `1.5 million` / `7.3.2 minor` | ❌（正确拒绝） | 年份/小数/多级编号，`\s` 门控起作用 |

> 说明：`\s` 门控的初衷是避免把 `1.5`、`2024.12` 误判为列表编号，方向正确；
> 但把"编号后直接跟字母/中文"（`1.First`、`1）第一项`）也一并拒绝了。
> 正确做法是"数字+点 后跟**非数字**字符"即视为编号（`1.5` 的 `1.` 后跟数字 `5`，
> 仍可排除）。

### 3.3 长列表项续行被 BabelDOC 短行拆分逻辑割裂（直接根因 #6）

探针 P09/P10/P11/P12 展示：一个跨 3 个物理行的列表项
（`1. This is a very long list item ...` + 缩进续行 + `2. Second item`），
被 BabelDOC 的"短行拆分"（§3.1 分支 2）拆成：

- P09：列表项首行到 `par`（编号 `1.` 开头，补丁把它当作列表项段落）；
- P10：`fallback_line` 的 `agraph`（首行尾部的换行片段）；
- P11：缩进续行 `continued indented second line`（行宽远小于中位数 → 被拆出）；
- P12：`2. Second item`（补丁拆分）。

`babeldoc_list_split._split_list_items_in_paragraphs` 只做"**从编号行起拆分**"，
没有能力把**缩进续行合并回所属列表项**，因此列表项正文在段落级被割裂，
翻译重排后各行互相独立、顺序依赖 BabelDOC 阅读顺序，观感上"列表乱了"。

### 3.4 编号前缀无保护，依赖翻译引擎自觉保留（直接根因 #7）

拆段成功 ≠ 编号保留。编号前缀（`1.`、`a.`）是**普通文本**，随列表项正文一起
发给翻译引擎。实测三种命运：

- `FAKE_MODE=keep`：编号保留（`1.`）；
- `FAKE_MODE=fullstop`：编号被改写为全角（`1.` → `1。`），排版尚可接受；
- `FAKE_MODE=drop`：编号被完全丢弃（LLM 重写风格）。

真实 LLM 翻译时，中文模型常把 `1.` 写成 `1、` 或直接丢弃，重排后列表失去编号。
`babeldoc_list_split` 只解决"拆段"，**不解决"编号保护"**。

对比：`babeldoc_toc_protect.py` 已用"**假公式占位符**"机制保护目录点线/页码
（构造 `PdfFormula` + 假 `formula_layout_id`/`line_id`，翻译阶段以占位符原样保留、
重排阶段原位渲染）——列表编号保护完全可复用同一机制，但目前**没有实现**。

### 3.5 补丁挂载点已覆盖全部入口（排除挂载遗漏）

| 入口 | 挂载位置 |
|------|---------|
| CLI `--parse-engine babeldoc` | `pdf2zh/pdf2zh.py:641`（list_split）、`:643`（toc_protect） |
| GUI legacy adapter | `pdf2zh/babeldoc_adapter.py:274-283` |
| GUI next-kernel adapter | `pdf2zh/babeldoc_next_adapter.py:451-455` |

`RuntimeService._execute_babeldoc`（`runtime_service.py:1778`）优先
`run_babeldoc_next_translation`（next 内核）、fallback `run_babeldoc_translation`
（legacy），两条路径都先应用补丁再驱动 `ParagraphFinder`，**挂载无遗漏**。

---


## 4. 影响面评估

| 列表样式 | 影响 | 严重度 |
|---------|------|:---:|
| 无空格编号 `1.First` | 合段翻译、编号丢失 | 高 |
| 连字符列表 `-` / `–` | 最常见列表标记，整段翻译、标记丢失 | 高 |
| 全角/中文编号 `1）`、`1．`、`1・` | 中文/日文文档常见 | 中 |
| 长列表项续行 | 列表项正文割裂、重排错乱 | 中 |
| 编号前缀无保护 | 真实 LLM 改写/丢弃编号 | 高（依赖引擎） |
| `(i)`/`①`/`[1]`/`(一)`/`第1条` 等 | 覆盖有限 | 低 |

---

## 5. 修复建议（方向，未实施）

1. **放宽 `_LIST_ITEM_PREFIX_RE` 的空白门控**：
   - `\d{1,3}[.．)]\s` → 改为"编号后跟**非数字**字符即匹配"（`1.5`/`2024.12`
     后跟数字仍排除；`1.First`、`1）第一项` 被覆盖）；
   - 新增 `-`、`–`、`—` 连字符分支；
   - 新增全角 `）`、`．`、`・` 字符。
   - 同时收紧误伤面：多级编号 `7.3.2` / `1.1.1` 仍应排除（保留"编号段不超过
     一级"的约束），避免误拆小节标题。

2. **列表编号前缀"公式保护"**（复用 `babeldoc_toc_protect.py` 的假公式机制）：
   把列表项行首的编号前缀（`1.`/`a.`/`•`）构造成 `PdfFormula` 占位符，
   不参与翻译、重排时原位保留 —— 彻底摆脱"LLM 是否自觉保留编号"的不确定性。

3. **长列表项续行重组**：在 `_split_list_items_in_paragraphs` 拆分后，识别
   "缩进续行"（x 起始 > 编号行 x 起始、且无编号前缀的行）并合并回所属列表项
   段落，避免 BabelDOC 短行拆分把续行割裂成独立段落。

4. 顺带修复 `_tmp_list_probe.py` 的 GBK 终端编码崩溃（`print` 遇 `•`/`・` 抛
   `UnicodeEncodeError`），保证后续调试可用。

---

## 6. 验证情况

- 探针复现：#2/#3/#4/#6 均可由 `_tmp_babeldoc_probe.py` 段落 dump + `_tmp_fake_translate_probe.py`
  译后文本稳定复现（§2.2 / §2.3）。
- 现有回归测试 `tests/test_babeldoc_list_split.py`（12+ 用例）覆盖的是
  `1. XXX` / `2) XXX` / `(1) XXX` / `a. XXX` 等**带空格/括号**编号样式，
  **没有覆盖** `1.First`（无空格）、`-`（连字符）、全角编号、长列表续行这四类，
  因此测试全绿而真实文档仍出问题。

