"""P6.3 — C_layout：DocLayout 真实区域类别注入（规范书 §5.3 遗留项 3）。

``FormulaConfidenceEngine.layout_score`` 的 C_layout 特征原本只能拿到
``None``（中性 0.5 不参与加分）。本模块把主链路 ``conv.layout[pageid]``
掩码（DocLayout 模型逐像素类别图，converter 同源消费）以
``layout_class_fn`` 形式注入 ``ReconstructionPipeline``，让公式置信度
打分获得真实版面信息。

坐标映射与 converter 保持一致：PDF 点坐标直接作为掩码像素坐标
（``cx, cy = int(child.x0), int(child.y0)``，layout 掩码以页面点尺度
渲染）。段落级类别取段落 bbox 中心采样。
"""
from __future__ import annotations

import re
from typing import Optional


def layout_mask_class_fn(conv, ltpage):
    """从 ``conv.layout[pageid]`` 掩码构造注入函数。

    返回 ``fn(para) -> Optional[int]``：段落到 DocLayout 类别索引；
    掩码缺失 / 越界 / 异常时返回 None（置信度引擎回退中性 0.5）。
    """
    layouts = getattr(conv, "layout", None)

    def _cls(para) -> Optional[int]:
        try:
            if layouts is None:
                return None
            mask = layouts.get(para.page_id)
            if mask is None:
                return None
            h, w = mask.shape
            cx = min(max(int((para.x0 + para.x1) / 2.0), 0), w - 1)
            cy = min(max(int((para.y0 + para.y1) / 2.0), 0), h - 1)
            return int(mask[cy, cx])
        except Exception:  # noqa: BLE001
            return None

    return _cls


# 文本启发式布局类别（无 DocLayout 掩码时的兜底注入，遗留项 3 fallback）：
# 基于字形字体名 / 数学符号 / 行结构启发式分类，使 C_layout 在离线
# 环境也能获得非中性输入（不依赖 ONNX 模型）。
_MATH_FONT_RE = re.compile(
    r"(math|cmsy|cmmi|cmex|msam|msbm|eufm|stix|ams|sym|symbol|greeksym)",
    re.IGNORECASE,
)
_SYMBOL_RE = re.compile(r"[∫∑∏√≤≥≠≈≡∞±×÷→←∈⊂⊃∪∩∀∃∂∇ℕℤℚℝℂ]")


def heuristic_layout_class(para) -> str:
    """段落 → 布局类别名称（'formula' | 'title' | 'plain text'）。

    规则：
      1. 行内任一字形字体命中数学字体关键词，且该行数学符号密度高 → formula；
      2. 段落字号统一且 ≥ 18pt（独立标题段），或字号有显著分差且最大字号
         > 1.25× 段中位字号（标题行 + 正文行混合段）→ title；
      3. 其余 → plain text。
    """
    glyphs = [g for line in getattr(para, "lines", []) for g in line.glyphs]
    if not glyphs:
        return "plain text"
    sizes = [g.font_size for g in glyphs]
    med = sorted(sizes)[len(sizes) // 2] if sizes else 12.0
    for line in getattr(para, "lines", []):
        line_text = line.text
        if not line_text:
            continue
        math_fonts = [g for g in line.glyphs if _MATH_FONT_RE.search(g.font_name or "")]
        sym_hits = len(_SYMBOL_RE.findall(line_text))
        if math_fonts and (sym_hits >= 2 or len(math_fonts) >= 3):
            return "formula"
    uni_sizes = {round(s, 1) for s in sizes}
    if len(uni_sizes) == 1 and sizes[0] >= 18.0 and len(glyphs) <= 24:
        return "title"
    if (max(sizes) > med * 1.25 and max(sizes) > min(sizes) * 1.15
            and len(sizes) >= 3):
        return "title"
    return "plain text"


__all__ = ["layout_mask_class_fn", "heuristic_layout_class"]
