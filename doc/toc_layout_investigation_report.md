# 书籍目录（TOC）排版处理现状调查与根因分析报告

> 版本：v1.1
> 日期：2026-08-19
> 范围：全仓库 PDF 处理代码（排除 `kernel/PDFMathTranslate-next.git` 子模块）中与"书籍目录"相关的所有路径
> 验证方式：代码静态分析（`rg` 全仓检索 outline / bookmark / toc / catalog / 目录）+ 主链路排版算法级推演 + 用户运行日志取证 + 单测回归
>
> v1.1 变更：新增第 9 章 —— BabelDOC 链路的「一行多目录条目合并」根因（v1.0 报告落地实现的 TOC protect 在该场景下只保护最后一个条目，前 N-1 个条目点线/页码仍被整体翻译 → 排版错乱），并记录 V1.24 修复实现与验证。

---

## 1. 执行摘要

**当前后端对"书籍目录"没有任何专门处理路径。** 目录页（书内 Table of Contents）与普通正文页走同一条 legacy 排版主链路（`TranslateConverter.receive_layout`），PDF 书签（`/Outlines` 目录树）则自始至终未被读取、翻译或重建。目录页"排版错误"不是某个目录专属 bug，而是**原位覆写式排版架构对目录页特殊结构（标题 + 点线 + 右对齐页码列）的必然副作用**。

**结论先行：**

| 级别 | 根因 | 一句话描述 |
| :-: | :-: | :-- |
| P0 | **目录行整段合并翻译** | 标题、点线（dot leaders）、页码因相邻且同布局类别被合并为一个段落整行翻译，点线与页码的语义被翻译器破坏。 |
| P0 | **译文超宽强制折行** | `converter.py:594` 对任何译文宽度超出段落右边界 `x1` 的段落一律折行；中文译文比原英文目录行宽，单行目录条目被折成多行，目录的缩进/点线/页码对齐结构随之瓦解。 |
| P0 | **书签（Outlines）完全缺失** | 翻译产出的 PDF 既无翻译后的书签标题，也无任何书签（`render_adapter.py:168` 的 Catalog 是最小模板，不含 `/Outlines`）。 |
| P1 | **行距压缩放大重叠** | 目录页行距小、条目密集，译文行数多于原文时触发行高压缩（`converter.py:680-686`），压缩至下限仍溢出时相邻目录条目文字直接重叠。 |

v3 的 `CatalogChannel`（`pdf2zh/v3/multi_channel_rewriter.py`）仅是把语义为 heading/reference/abstract 的 chunk 路由到 PromptManager 的**翻译通道**，不感知目录页排版结构，且无 LLM 后端时恒返回原文；v3 全部增量模块未在主链路接线（`use_v4_*` 全默认 `False`），与目录排版无关。

---

## 2. 调查范围与方法

- 全仓检索关键词：`outline`、`bookmark`、`toc`、`catalog`、`目录`、`/Outlines`，排除 `kernel/PDFMathTranslate-next.git` 子模块与 `doc/` 已有报告。
- 主链路代码走读：`converter.py`（段落构建/翻译/重排）、`layout_graph.py`（阅读顺序 DAG）、`paragraph_layout.py`（段落布局引擎）、`overlay_renderer.py`、`doclayout.py`（YOLO 布局分类）、`high_level.py`/`backend.py`/`translator.py`/`services`/`kernel`（调度与翻译）。
- 检索结论：**上述核心文件中不存在任何目录识别、目录条目解析、书签读写逻辑**（`layout_graph.py`、`paragraph_layout.py`、`converter.py` 中 grep 目录/TOC/heading 均零命中）。

---

## 3. 事实：两种目录形态的后端处理现状

### 3.1 PDF 书签（`/Outlines` 目录树）——完全未处理

- 检索：全仓唯一命中 `/Outlines`/Catalog 相关的位置是 `pdf2zh/v3/render_adapter.py:168` 的新 PDF 模板：

  ```
  "<< /Type /Catalog /Pages 2 0 R >>"
  ```

  这是一个**最小 Catalog**，连 `/Outlines` 条目都没有——即 v3 引擎生成的 PDF 天然无书签。
- v1 主管线（`high_level.py` 逐页 patch 内容流）只替换页面 `/Contents`，从未触碰文档级 `/Outlines` 树：**输入 PDF 的书签既不会被翻译，也不会被保留**。

### 3.2 书内目录页（Table of Contents）——无识别，按正文处理

- `converter.py:receive_layout`（L197-436）按字符流构建段落，段落划分依据只有：布局类别（`layout[cy,cx]`）、坐标相邻性、字号。目录行（标题 + 点线 + 页码）相邻且通常同类别（正文文本类），**天然合并为一个段落**；layout 模型（doclayout YOLO）把目录页当作普通文本页，不产生目录语义类别。
- `layout_graph.py` 只有多栏检测与空间/拓扑阅读顺序，无目录概念。
- `paragraph_layout.py` 只有通用换行/对齐，无目录概念。
- 结论：目录页走的是与正文完全相同的"段落级原位覆写"路径，没有任何结构性保护。

---

## 4. 排版错误机制（根因链）

以典型目录行为例：

```
1.  Introduction .......... 3
```

在 `receive_layout` 中：该行所有字符（含点线与页码）坐标相邻、布局类别相同 → 合并为一个 `Paragraph`，`x0=行首`、`x1=页码右端`，整行作为一段文本送去翻译。随后 C 段排版（L515-651）按段落锚定重排：

1. **整行语义被翻译破坏**：点线（`.` 序列）与右对齐页码混在段落文本中，机器翻译后点线可能丢失或被改写（如变成中文省略号），页码与标题的对齐信息在译文里不复存在。
2. **译文宽度膨胀触发强制折行**（`converter.py:594` `x + adv > x1 + 0.1*size` 一律换行）：中文每字符宽度≈字号，原文点线区被中文撑宽后超出 `x1` → 单行条目折成两行以上，第二行从 `x0` 重新起行——**缩进层级、点线引导、页码右对齐列全部消失**。
3. **行距压缩加剧重叠**（L680-686）：目录页原文行距通常接近字号（密排），译文行数增多后按 `height` 压缩行距，压缩到下限仍溢出的目录条目与下一行条目字面盒相接/重叠。
4. **无结构锚点**：目录条目间没有独立的几何或语义标识，`CollisionResolver`（push-down 碰撞避让）只能把溢出段整体下移，无法恢复"标题-点线-页码"的列结构。

一句话：**目录页的"行"被当作"段落"翻译重排，行内三要素（标题/点线/页码）失去相对结构，再叠加中文膨胀折行与行距压缩，最终表现为目录排版错乱（错行、折行、对齐丢失、行间重叠）。**

---

## 5. 影响范围

- 所有含**目录页**的书籍类 PDF（专著、教材、论文集前端目录）。
- 所有依赖 **PDF 书签**导航的文档：翻译后书签整体消失。
- 不含目录、无书签的论文单篇 PDF 不受影响（与正文排版问题同源，但无目录专属损失）。

---

## 6. 修复建议（按优先级）

| 优先级 | 方案 | 说明 |
| :-: | :-: | :-- |
| P0 | **目录行结构感知**（v1 管线内） | 在 `receive_layout` 段落构建阶段识别目录行尾的"点线+右对齐页码"模式（正则匹配行尾 `[.\s]+\d+` 或检测行内右对齐数字簇），将页码列从翻译段落中切出，译文只译标题部分并按原 x0 左对齐、原 x1 右对齐页码、点线保留原样。 |
| P0 | **目录条目禁折行/按点线断行** | 被识别的目录行在 C 段排版中不参与通用 wrap（或在点线前断行），防止单条目折成多行。 |
| P0 | **书签读写** | 新增 outline 处理：读取输入 PDF `/Outlines` 树 → 翻译书签标题 → 为目标 PDF 重建 `/Outlines`（需在文档级对象中插入并挂到 Catalog）；若后续实现流式重排导致分页变化，还需书签目标页重映射。 |
| P1 | **目录页行距不压缩** | 识别目录页后跳过行高压缩逻辑（L680-686），以行距优先、允许下移腾位。 |
| P2 | **v3 目录语义接线** | 将 `CatalogChannel` 接入真实 LLM 翻译 heading 类 chunk，并在 document_ir/constraint_graph 中为目录页增加专门语义角色与排版约束（当前 `constraint_graph.py:798` 的元素类型映射仅做 `"toc": "toc"` 透传，无布局语义）。 |

---

## 7. 附录：关键代码定位

- 段落合并（目录行整段化）：`pdf2zh/converter.py:340-349`
- 译文强制折行：`pdf2zh/converter.py:594`
- 行高压缩与溢出标记：`pdf2zh/converter.py:680-686`
- 段落原位锚定排版：`pdf2zh/converter.py:515-651`
- 最小 Catalog 模板（无 Outlines）：`pdf2zh/v3/render_adapter.py:168`
- 目录语义翻译通道（仅 v3，无后端恒原文）：`pdf2zh/v3/multi_channel_rewriter.py:78-89`
- 元素类型映射（toc 透传）：`pdf2zh/v3/constraint_graph.py:798`
- 主链路开关（v3 未接线）：`pdf2zh/services/runtime_service.py` `use_v4_*` 默认 `False`

---

## 8. 实现状态（2026-08-04 长线实现落地）

P0/P1 已按本报告落地实现，P2（v3 目录语义接线）保持待办（v3 引擎未在主链路接线）。

| 方案 | 状态 | 实现位置 |
| :-: | :-: | :-- |
| 目录行结构感知（P0） | 已实现 | 新增 `pdf2zh/toc.py`（`detect_toc_line` 识别"标题+点线+页码"）；`converter.py:receive_layout` 段落构建阶段切出标题单独翻译 |
| 目录条目禁折行/按点线断行（P0） | 已实现 | `converter.py` C 段排版 `toc_mode`：标题以 `x1_bound=inf` 渲染（禁折行），点线从标题结束处原位填充，页码按段落右边界右对齐 |
| 书签读写（P0） | 已实现 | `high_level.py:_apply_bookmarks`：fitz `get_toc` 读 outline → 用与正文一致的翻译器（`build_translator`，已抽至 `translator.py`）翻译标题 → `set_toc` 写回 mono（页码不变）与 dual（页码 2n-1 指向英文页）文档 |
| 目录页行距不压缩（P1） | 已实现 | `converter.py` 行高压缩循环对 `toc_mode` 跳过（不触发 QA 溢出标记） |

工程处理：
- `build_translator` 从 converter 内联循环抽为 `pdf2zh/translator.py` 模块级工厂，正文与书签共用。
- 目录逻辑独立为 `pdf2zh/toc.py`，保持 converter.py 满足 v3 strangulation 门控（<850 行，现 838 行）。
- 新增回归测试：`tests/test_converter_toc.py`（检测 + 标题单独翻译 + 禁折行/页码右对齐/不压缩，10 例）、`tests/test_high_level_bookmarks.py`（outline 读写与 mono/dual 页码映射，4 例）。
- 全量回归：**1483 passed, 1 skipped, 8 warnings, 0 failed**（基线 1469）。
- 未实现 P2（v3 CatalogChannel 目录语义 LLM 翻译与排版约束），留待 v3 引擎接入主链路时一并落地。
---

## 9. V1.24：BabelDOC 链路「一行多目录条目合并」根因与修复（2026-08-19）

### 9.1 症状与取证

用户运行 BabelDOC 引擎翻译带紧凑目录的书籍 PDF（Matrix Algebra），TOC protect 日志显示"识别到了"，但输出 PDF 目录排版依旧错乱。关键运行日志：

```
BabelDOC TOC protect: split line
  '13.62 Symmetry: implicit treatment . . . .. . 388
   13.63 Symmetry: explicit treatment . . . .. . 389
   13.64 Treatment of positive definiteness . . . .. . 389
   *13.65 Information matrix . . . .. . 390'
  -> title='13.62 Symmetry: implicit treatment . . . .. . 388
            13.63 Symmetry: explicit treatment . . . .. . 389
            13.64 Treatment of positive definiteness . . . .. . 389
            *13.65 Information matrix '
     formula='. . . .. . 390'
  (score=0.8359)
```

**一条 `PdfLine` 里塞了 4 个目录条目**（同一物理行被 BabelDOC 的 `ParagraphFinder` 合并成一条行），而 protect 只把**最后一个**条目的点线+页码拆成公式，前 3 个条目的标题、点线、页码全部留在 `title` 中被当作一整段送进翻译器。

### 9.2 根因链

1. **BabelDOC 行合并**：`ParagraphFinder.process_page` 把同一物理行上的多个文本 span 合并为一个 `PdfLine`（紧凑型目录、多列目录拍平后常见），其 `pdf_character` 文本形如
   `13.62 ... 388 13.63 ... 389 13.64 ... 389 *13.65 ... 390`。
2. **`toc.py` 单条目检测的行尾锚定**：`TOC_LEADER_RE`（`pdf2zh/toc.py:35-37`）以 `$` 锚定行尾，
   `detect_toc_line` 到 `_detect_leader` 用 `.search()` 只能命中**最后一个** `点线+页码`（只有它后面是行尾）；
   `babeldoc_toc_protect._toc_split_index` 同样返回最后一个匹配位置。
3. **拆分错位**：`_try_protect_line` 按该索引切分，`title` = 行首到最后一个点线前（**包含前 N-1 个条目的点线与页码**），`formula` = 只有最后一个条目的点线+页码。
4. **排版破坏**：前 N-1 个条目的点线/页码作为普通文本进翻译器，点线被改写、页码与标题的对齐信息丢失、4 个条目被译成一段连续文本，重排时折行/挤压，目录列结构瓦解。

> legacy 主链路不受此 bug 影响：`receive_layout` 逐物理行建 sstk，且已有 `v3/toc_analyzer.split_merged_toc_paragraphs`（V1.17-3）处理「无点线目录行被并块」场景；v3 引擎未在主链路接线。本问题专属于 **BabelDOC 保护链路**（`pdf2zh/babeldoc_toc_protect.py`）。

### 9.3 修复实现（V1.24，`pdf2zh/babeldoc_toc_protect.py`）

在单条目保护之前新增**多条目合并行检测与逐条拆分**：

| 新增 | 作用 |
| :-: | :-- |
| `_MERGED_ENTRY_HEAD` | 宽泛条目标题头（允许 `*`/`#` 等前缀 + 章节编号 + 空白 + 文本），比 `toc.py._TOC_HEAD_RE` 多覆盖 `*13.60` 这类星号标记条目 |
| `_merged_entry_re()` | 非行尾锚定的「点线 + 页码」多匹配正则（字符集与 `toc.py` 同步，延迟构建缓存） |
| `_build_offset_track()` | 带文本偏移的点线/数字字符几何记录（`char_unicode` 可能为空串，字符与文本索引非一一对应） |
| `_char_offsets()` / `_offset_to_char()` | 文本偏移与字符索引映射（二分） |
| `_split_merged_toc_line()` | 一行内所有「点线+页码」候选，逐条验证（编号标题头 + 几何页码 + `_score_toc` 置信度不低于 0.30），返回条数不少于 2 才拆分 |
| `_try_protect_merged_line()` | 把合并行的字符按条目区间切成多条 `[标题 PdfLine, 点线+页码 PdfFormula]`（复用假 `formula_layout_id` / 假 `line_id` 防转回文本、防误合并） |

接线：`_protect_toc_lines_in_page` 对每个 `PdfLine` composition 先尝试 `_try_protect_merged_line`，未命中再回退原 `_try_protect_line`（单条目逻辑零改动）。

### 9.4 验证

- **修复前后对比**（真实日志文本 `13.62 ... 388 13.63 ... 389 13.64 ... 389 *13.65 ... 390` 复现）：

  ```
  修复前：title='13.62 Symmetry: implicit treatment ... 388 13.63 ... 389 13.64 ... 389 *13.65 Information matrix '
          formula='. . . .. . 390'          <-- 前 3 条点线/页码进翻译器
  修复后：title='13.62 Symmetry: implicit treatment'  page=388
          title='13.63 Symmetry: explicit treatment'  page=389
          title='13.64 Treatment of positive definiteness'  page=389
          title='*13.65 Information matrix'            page=390
  ```

- 新增回归测试 `tests/test_babeldoc_toc_protect.py::TestMergedTocLine`（5 例）：多条目逐条切分、单条目不误拆、正文行不误拆、页级段落拆分、假 formula_layout_id 标记。
- 全量 TOC/babeldoc 相关回归：`test_babeldoc_toc_protect` + `test_converter_toc` + `test_v17_toc_analyzer` + `test_v18_toc_render_split` + `test_babeldoc_list_split` + `test_babeldoc_ocr_mode` + `test_babeldoc_onnx_backend` + `test_onnx_backend_switch` = **121 passed, 1 skipped, 0 failed**。

### 9.5 残留风险与后续建议

- 多条目拆分的条目标题头验证要求「章节编号开头」；个别无编号目录条目（如纯文本章节名 + 点线页码）在合并行中会被跳过，该行回退单条目逻辑（仍只保护最后一个）。此类目录建议后续在 `_MERGED_ENTRY_HEAD` 中补充结构词（chapter/section 等）分支。
- 双栏目录被拍平成单行时，左栏条目与右栏条目可能共享同一物理行；本修复按「编号标题 + 点线 + 页码」逐条切分，若左右栏条目都满足结构则均可正确拆分，但页码右缘几何验证依赖 `visual_bbox`（`_build_offset_track`），OCR 无几何字符仍会保守跳过。
- P2（v3 CatalogChannel 目录语义接线）维持待办，与本修复无重叠。

