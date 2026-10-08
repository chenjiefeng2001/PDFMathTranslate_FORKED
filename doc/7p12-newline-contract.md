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

## 7p11 无缓存实跑的审计误判与修复

`doc/7p11-nocache-verify50/`（无缓存，98 次 OpenCode 失败）的第一次审计曾误报
「REAL content loss: 2」，实际是以下两处**审计口径**问题的合成效果，不是渲染丢了
字：

1. 翻译失败后回退为原文的两个英文块（`p46_7`, `p48_5`）被原样渲染，并被
   CJK 字体的连写字形 / 破折号表现搞击中：
   - `fl` → `ﬂ`（U+FB02 连写字）
   - `-` → `−`（U+2212 减号，CJK 字体风格）
   而 `norm_nospace` 只去空白，两个字节序列被判成不相等。

2. pymupdf 的 `longest_common_substring` 对 LCS 足够敏感，但它在上述字符处断开，
   恰好让 LCS 长度低于 60% 阈值，触发「content loss」桶。

修复：`doc/7p5_audit.py` 的 `norm_nospace` 增加 `unicodedata.normalize("NFKC")`
并统一 `‐‑‒–—−` 为 `-`。同一产物重跑后 `REAL content loss: 0`。

注意：翻译失败回退块会以英文重新落笔，但**不是**原样保留（`translated == text`
的块仍走 translate_refit）；这就是 7p11 实跑里出现英文样本的根因。若未来要让
回退块走 preserve 通道，`render_fixup` 的 `{'keep', 'preserve', 'shift_down'}`
统计依据需要相应调整。
