# 换行符契约

## 现象

pymupdf 的 `Page.insert_text` 会把传入文本里的 `\n` 当成一次换行：它不会在
段落边界停住，而是在当前基线下方**额外**画出一行。实测 mp2e p23 上，同一个
译文块里的段落换行导致上一行之下 0.91pt 处幽灵地多出一行，而 P1 的任何缩放
退让都救不回来（问题不在宽度）。

## 约定

**任何路径不得把带 `\n` 的原始译文直接交给 `insert_text`。** 落笔前必须按
`str.splitlines()` 逐行绘制，空行作为占位推进基线。这是所有渲染路径的统一
契约，不只命中 `magicpdf_renderer`：

| 路径 | 位置 | 处理 |
|---|---|---|
| `magicpdf_renderer` 流式块 | `_layout()`（`pdf2zh/v3/magicpdf_renderer.py`） | 按 `\n` 拆段，段末强制 `_flush()` |
| `V4PDFRenderer` | `pdf2zh/v3/pdf_renderer.py` | 已改为逐行落笔 |
| `OverlayRenderer` | `pdf2zh/overlay_renderer.py` | 已改为逐行落笔 |
| `DualPatcher.apply_to_pdf` | `pdf2zh/patch/dual_patcher.py` | 已改为逐行落笔 |

## 审计方法

- `doc/7p4_overlap_pairs.py` 的容差取 `_OVERLAP_MIN_PT`（渲染器同口径），
  不再硬编码 1pt。
- 叠印判定只比较**不同 SPAN** 的字形 bbox；同行拆分 / 基线对齐不产生误报。
- pymupdf 的 `get_text("dict")["blocks"][i]["lines"]` 把纵向只差 <1pt 的
  span 也分到不同 line —— 不能用 line 索引当"同行"判据。

## 测试

`tests/test_newline_contract.py` 捕获三条路径的 `insert_text` 调用参数，断言
其中没有任何一条文本含 `\n`，且每段非空行各画一条。
