# Commit 7B + 7E-1 — Layout 原语层 + FlowText 渲染侧信道审计

> 目标：把「渲染端自己判断 fit / overflow / 换行」收敛为「语义层几何 +
> 约束 → 统一的 ``lay_out`` 决策 → 渲染端只消费已定版结果」。
>
> 7A 已统一 TranslationUnit / render_payload.kind；本 commit 补上「布局怎么
> 定版」这一层（7B/7C），并让普通段落（flow）真正走这条链路（7E-1）。
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

## 4. 顺带修复：source_pdf 背景层 Windows 文件锁

7E-1 引入的 ``source_pdf`` 背景层（保留原 PDF 图形/保留块）在源 PDF 打不开
时（测试用占位文件）会**锁住源文件**：pymupdf 打开失败抛出的
``FileDataError`` 的 traceback 持有 C 层文件句柄，一旦异常对象被日志记录
（pytest 捕获 / 长驻 handler）保留，句柄直到测试结束才释放 → 临时目录
清理失败（``PermissionError: [WinError 32]``）。

修复：背景加载失败路径只把异常**字符串化**后记日志，随后立即 ``del exc``，
保证没有任何引用持有 traceback → 句柄随异常析构释放。新增
``test_source_pdf_no_file_lock`` 回归（非法/合法源 PDF 渲染后均可删除）。

## 5. 验收

- [x] `tests/test_layout_*.py`（7B/7C：primitives 21 / measure 10 /
      constraints 10 / wrap 17 / overflow 14 / architecture 9 = **81**）通过
- [x] `tests/test_flow_sidechannel.py`（**19**）通过 —— 几何透传、译文优先、
      失败降级、架构断言、document_model 集成
- [x] `tests/test_magicpdf_renderer.py`（**14**，含 flow 命令绘制 / legacy
      降级 / 文件锁回归）通过
- [x] `tests/test_toc_render_sidechannel.py`（**6**）通过 —— 补齐 6C 适配层
      直接覆盖
- [x] `tests/test_pdf_eval_*.py` + `pdf_eval_build.py`（**21**）通过
- [x] 全量回归：`tests/`（不含 v3）**1640 passed, 3 skipped**；
      `tests/v3` **1573 passed**
- [x] converter.py 行数 1094 未增（7A 预算内，本 commit 未触碰）
- [ ] 后续：list/toc 渲染载荷逐步迁移到 ``lay_out`` 定版（7E-2+），
      不再由各自 renderer 内部判断 fit
