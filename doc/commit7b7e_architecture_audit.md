# Commit 7B + 7E-1 + 7E-2 + 7E-3 — 统一 Layout Contract 审计

> 目标：把「渲染端自己判断 fit / overflow / 换行」收敛为「语义层几何 +
> 约束 → 统一的 ``lay_out`` 决策 → 渲染端只消费已定版结果」。
>
> 7A 已统一 TranslationUnit / render_payload.kind；本 commit 补上「布局怎么
> 定版」这一层（7B/7C），并让普通段落（flow，7E-1）、列表（7E-2）、
> TOC（7E-3）三条路径真正共享同一个 Layout Contract。
> 7A 报告见 ``doc/commit7a_architecture_audit.md``。

## 1. 现状盘点（改动前）

- **渲染端自判 fit**：``magicpdf_renderer._insert_text_wrapped`` 在绘制时
  自己按词换行、自己算行高、自己决定停在哪 —— 布局决策散落在渲染器里，
  不可测试、不可被其它渲染路径复用。
- **语义层没有「几何刚性」词汇**：list/toc/code 各有自己的渲染特判，
  但没有统一的「这段几何多硬（能换行/能收缩/必须保持）」表达。
- **flow 没有侧信道**：7A 统一了 list/toc 的 TranslationUnit 与
  render_payload，但普通段落仍走 ``_insert_text_wrapped`` 旧路径 ——
  ``render_payload.kind == "flow"`` 时 commands 为空。

## 2. 7B — Layout 原语 / 约束层（`pdf2zh/semantic/layout/`）

| 模块 | 职责 |
|---|---|
| `primitives.py` | `FlowText` / `FixedAnchor` / `FixedColumn` / `PreservedRegion` / `Continuation` —— 纯几何载体，编码「原始几何 + 刚性」 |
| `constraints.py` | `FixedX` / `FixedY` / `FixedWidth` / `MaxWidth` / `MaxHeight` / `PreserveBBox` + `resolve_geometry` |
| `measure.py` | 统一度量入口 `measure_text`（字体感知 + CJK 估算回退） |
| `mapping.py` | 既有载荷（list/toc/flow）→ 原语的无脑透传工厂 |
| `wrap.py` | `wrap_lines` / `shrink_to_fit` / `clip_text` 纯机制（7C） |
| `overflow.py` | `OverflowPolicy` + `LayoutResult` + `lay_out` 单一决策引擎（7C） |

要点：

- **布局决策单一化**：`lay_out(primitive, ...) → LayoutResult` 是唯一做
  fit/wrap/shrink/clip 决策的地方；渲染端只读 ``LayoutResult`` 写绘制命令。
- **Code 永不换行**：`PreservedRegion` 恒为 `PRESERVE`，几何不可变，
  溢出只上报不移动（7C 架构测试锁定）。
- **SHRINK 机制就绪但默认不自动应用**：`FixedAnchor`（list content_x /
  TOC title_x）默认只上报 overflow，由宿主决定 —— 本 commit 不自动缩字号。
- **CLIP 是最后手段且永不静默**：任何 clip 都带 ``overflow=True`` +
  ``policy=clip``。

## 3. 7E-1 — FlowText 渲染侧信道（段落 → LayoutResult → PDF）

```
Block(translated, bbox, font_size)
    ↓  flow_sidechannel.flow_text_from_block / build_block_flow_payload
FlowText(origin / max_width / max_height 原样透传)
    ↓  semantic.renderer.flow.render_flow_text（只调 lay_out）
LayoutResult → commands
    ↓
magicpdf_renderer._render_flow_commands（只做 y 翻转 + 逐行写入，不重排版）
```

- **几何只透传**：origin 取块 ``(x0, baseline|y1)``，宽/高取 bbox 差；
  首行基线来自块首行（缺失回退 y1）。绝不从 level / index / 页宽重推断。
- **布局不重判**：`render_flow_text` 内部只调 ``lay_out``；`wrap_lines` /
  `shrink_to_fit` / `clip_text` 不在侧信道与渲染器里出现（架构测试锁定）。
- **失败可观测降级**：布局层任何异常 → ``layout_ok=False`` 载荷（溢出 +
  CLIP 哨兵），渲染端计数 ``flow_legacy_fallback`` 后走旧换行路径，绝不
  静默、绝不抛出。
- **y-up 坐标**：v3 坐标系首行锚定块顶、换行负步进；渲染端翻转后落位正确。
- **document_model 接线**：``render_plan_from_model`` 对 paragraph 块产出
  ``render_payload = {kind: "flow", commands: [...], entries: []}``；
  ``translate_document`` 后 commands 由**译文**定版。

## 4. 7E-2 — List Layout Integration（列表纳入统一 Layout Contract）

```
ListItemNode
    ↓  semantic/layout/list_layout.layout_list_item
FixedAnchor(marker) + FlowText(content) + FlowText(continuation)
    ↓  lay_out()（唯一 fit/wrap 决策引擎）
ListLayoutResult
    ↓  semantic/renderer/list.ListRenderer（draw-only）
magicpdf render_payload.kind == "list" → 既有命令绘制路径
```

- **marker 是 FixedAnchor**：永不 wrap、永不翻译；content 按 content_width
  wrap（长英文 / CJK / 混合 / 换行 / 超长 token 均有专项测试）；
  continuation 钉在 content_x。
- **几何零重算**：marker_x / content_x / continuation_x 全部来自语义节点
  原始几何，Layout 层绝不根据 level / index / 编号重推（架构测试锁定）。
- **ListRenderer 变 draw-only**：不再调 detect_list / parse_list /
  calculate_level / calculate_indent / wrap_lines / measure_text；只把
  ListLayoutResult 画成命令。
- **y-up 延续行方向修正**：换行向下递减，与 flow 侧信道一致（修正了既有
  测试中错误的 y 方向断言）。
- **7D evaluator 新增**：``list_marker_x_accuracy`` /
  ``list_wrap_integrity``（两个 item 合并 / marker 丢失 / continuation 被
  当新 item / 下一项被吞）/ ``list_nested_geometry_accuracy``。
- **回归铁律**：垃圾翻译器 ``return "TRANSLATED"`` 时输出仍为
  ``1. TRANSLATED`` / ``2. TRANSLATED`` —— marker 与 translator 四层解耦。
- **Definition of Done 全绿**：普通 / 嵌套 / CJK / 长译文 wrap /
  continuation ``x == content_x``；真实 PDF ``get_text("words")`` 验证
  嵌套 content_x 逐级递增；未做自动缩字号（SHRINK 留待 7F）。

## 5. 7E-3 — TOC 纳入统一 Layout Contract（golden 验证）

```
TOCEntryNode / entry dict
    ↓  semantic/layout/toc_layout.layout_toc_entry
FixedAnchor(number) + FixedAnchor(title) + FixedColumn(page)
    + flexible leader 区域
    ↓  lay_out()（唯一 fit/overflow 决策引擎）
TocEntryLayoutResult
    ↓  toc_layout_commands → TocRenderer（draw-only，golden 保持）
PDF commands
```

- **7E-3 不是重写 TOC Renderer**：现有 ``TocRenderer`` 作为 golden
  implementation 保留；adapter 生成的几何与它逐字一致（title_x / page_x /
  indent / bbox 透传；leader 按译文实际宽度重新生成到原 page_x；page
  number PRESERVE；无 leader 条目绝不凭空加 ``....``）。
- **title_x ≠ f(level)**：title_x 逐字来自节点；嵌套条目 title_x 真实
  递增且与 level 无函数关系（同 level 不同 x 的用例锁定）。
- **标题变长 → leader 缩短 → page_x 不动**；实在放不下进入明确
  overflow 状态（不静默）。
- **多行 TOC**：continuation 锚在 ``title_x + size``、y-up 下向下步进，
  page number 不因 wrap 跑掉。
- **统一 measure**：默认走 ``layout.measure.measure_text``；
  ``TocRenderer.measure_width`` injection seam 保留（既有测试不破坏）。
- **translator 只收 title**：``translated_calls == ["Introduction"]`` ——
  numbering / dot leader / page number / 几何绝不进 translator。
- **架构断言**：layout adapter 不调 detect_toc / parse_toc / looks_like，
  不出现 ``level *`` / ``index *`` 几何推导，fit 决策全走 ``lay_out``；
  draw-only 路径（TocRenderer）同样无检测 / 无重排版。
  ``build_page_toc_plan`` 是 golden 组合链（detect → parse → render），
  明确不在断言范围。
- **7D evaluator 新增**：``toc_leader_integrity``（有 leader 时
  title_end < leader < page_x；无 leader 时 leader_count == 0）/
  ``toc_continuation_x_accuracy``（多行条目延续列保持）。

## 6. 顺带修复：source_pdf 背景层 Windows 文件锁

7E-1 引入的 ``source_pdf`` 背景层（保留原 PDF 图形/保留块）在源 PDF 打不开
时（测试用占位文件）会**锁住源文件**：pymupdf 打开失败抛出的
``FileDataError`` 的 traceback 持有 C 层文件句柄，一旦异常对象被日志记录
（pytest 捕获 / 长驻 handler）保留，句柄直到测试结束才释放 → 临时目录
清理失败（``PermissionError: [WinError 32]``）。

修复：背景加载失败路径只把异常**字符串化**后记日志，随后立即 ``del exc``，
保证没有任何引用持有 traceback → 句柄随异常析构释放。新增
``test_source_pdf_no_file_lock`` 回归（非法/合法源 PDF 渲染后均可删除）。

## 7. 验收

- [x] `tests/test_layout_*.py`（7B/7C：primitives 21 / measure 10 /
      constraints 10 / wrap 17 / overflow 14 / architecture 9 = **81**）通过
- [x] `tests/test_layout_list.py`（**17**）+ `test_layout_list_nested.py`（**6**）
      通过 —— 7E-2 列表 wrap（长英文 / CJK / 混合 / 换行 / 超长 token）
- [x] `tests/test_layout_toc.py`（**14**）+ `test_layout_toc_architecture.py`
      （**8**）+ `test_toc_layout_integration.py`（**11**）通过 —— 7E-3 TOC
      adapter / 架构断言 / 集成
- [x] `tests/test_flow_sidechannel.py`（**19**）通过 —— 几何透传、译文优先、
      失败降级、架构断言、document_model 集成
- [x] `tests/test_magicpdf_renderer.py`（**14**，含 flow 命令绘制 / legacy
      降级 / 文件锁回归）通过；`test_magicpdf_list_layout.py`（**4**）真实
      PDF 几何验证（嵌套 content_x 递增、continuation_x == content_x）
- [x] `tests/test_list_layout_integration.py`（**8**）+ `test_semantic_list_*`
      通过 —— 7E-2 集成（translator 不接 marker、垃圾翻译器回归）
- [x] `tests/test_toc_render_sidechannel.py`（**6**）通过 —— 补齐 6C 适配层
      直接覆盖
- [x] `tests/test_pdf_eval_metrics.py`（**13**，含 7E-2/7E-3 新增 7 个指标
      用例）+ `pdf_eval_build.py` 通过
- [x] 全量回归：`tests/`（不含 v3）**1714 passed, 3 skipped**；
      `tests/v3` **1573 passed**
- [x] converter.py 行数 1094 未增（7A 预算内，7E-2/7E-3 未触碰）
- [x] 新文件 ruff 零告警（metrics.py 的 C401 与 test 的 RUF012 为 7D 既有）
- [x] **7E-2（List Layout Integration）**：``semantic/layout/list_layout.py``
      把 ``ListItemNode`` → ``FixedAnchor(marker) / FlowText(content) /
      FlowText(continuation)`` → ``lay_out`` → ``ListLayoutResult``；
      ``ListRenderer`` 改为 draw-only；magicpdf 继续用
      ``render_payload.kind == "list"``；y-up 延续行方向修正；7D evaluator
      新增 ``list_marker_x_accuracy`` / ``list_wrap_integrity`` /
      ``list_nested_geometry_accuracy``。
- [x] **7E-3（TOC 纳入统一 Layout Contract）**：``semantic/layout/toc_layout.py``
      把 ``TOCEntryNode`` → ``FixedAnchor(number/title) + FixedColumn(page)
      + flexible leader`` → ``lay_out`` → ``TocEntryLayoutResult``；现有
      ``TocRenderer`` 作为 golden 保持、改为消费已定版结果；measure 统一到
      ``measure_text``（injection seam 保留）；7D evaluator 新增
      ``toc_leader_integrity`` / ``toc_continuation_x_accuracy``。
- [ ] 后续：**7E-Audit** —— Semantic → Translation → Layout → Renderer
      全链路审计（找 renderer 里的二次测量 / 几何再推导 / legacy 第二套
      执行路径），通过后再进入 **7F：Adaptive Layout / Overflow Recovery**
