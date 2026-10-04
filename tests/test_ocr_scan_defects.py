"""扫描件 OCR 翻译链路的四个缺陷回归。

每个缺陷都在真实 MinerU OCR 输出上复现过（真扫描件 = 文本层为空、整页一图），
修复前均能稳定触发，且 **4447 个既有测试无一覆盖**。

1. 译文被静默截断 —— ``pymupdf.Page.insert_text`` 对超出页面右边界的部分
   静默丢弃并仍返回成功。根因是排版按 helv 度量、渲染对含 CJK 的行按
   china-ss 绘制（拉丁 advance ≈ 1em），同一行宽度差约 2 倍。实测一行
   60 字符在 x=300 处只剩 30 个字符，句子中间被砍，无日志无指标。
   修法：``_draw_line`` 用**绘制字体**核算宽度，放不下先缩字号，仍放不下
   才显式裁剪并计入 ``stats["fit_clipped"]`` + warning。

2. 正文被误判为公式 → 永不翻译 —— ``structure.py`` 的公式规则要求
   「≥2 个数学符号 + alnum < 0.85」，而数学符号集含 ``()``、``isalnum()``
   不计空格，于是 20 句普通英文散文有 15 句被判成 formula（置信度 0.9），
   整块落入 ``KEEP_KINDS`` 被保留。修法：``_looks_like_display_formula``
   要求真正的运算符证据，并显式排除「读起来是句子」的文本。

3. 静默丢页 —— ``render_plan_to_pdf`` 只按 render_plan 里的页建页。某页
   OCR/布局一个块都没检出时计划里没有它，而扫描件该页内容来自背景层，
   于是整页从产物里消失（实测 3 页 → 2 页，退出码 0，无告警）。
   修法：页集合 = 计划页 ∪ page_sizes 的键。

4. ``/Length`` 损坏 + 字体广播失效 —— ``_protect_math_fonts`` 用
   ``xref_set_key(xref, "/Length", xref_get_key(xref, "/Length")[1])``
   企图阻止子集化。PyMuPDF 1.28.2 上带前导斜杠读键返回 ``('null','null')``，
   写出的是 ``/Length 58  / << /Length null >> >>``（重复键 + 无键子字典），
   阅读器报「文件已损坏」；同一处 ``"/BaseFont"`` 读键同样为 null，所以整个
   函数空转、声称的保护从未存在。字体广播那侧，
   ``xref_set_key(page_xref, "Resources/Font/noto", ...)`` 必然抛
   ``path to 'noto' has indirects`` 且被 ``except: pass`` 吞掉，第 1 页起
   全是悬空字体引用。修法：只读的 ``find_math_fonts`` +
   ``broadcast_page_font``（失败退回 ``page.insert_font``）。
"""

import io
import re
import unittest

import pikepdf
import pymupdf

from pdf2zh.font_cache import broadcast_page_font, find_math_fonts
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
from pdf2zh.v3.render_payload import KEEP_KINDS
from pdf2zh.v3.structure import BlockRole, _looks_like_display_formula

#: 一份与平台无关的字体字节：MuPDF 内置 Helvetica。
#:
#: 旧实现写死 ``ARIAL = r"C:\Windows\Fonts\arial.ttf"``。本机（Windows）永远
#: 通过，Linux CI 上却必然 ``FzErrorSystem: cannot open C:\Windows\Fonts\...``，
#: 4 个字体测试在那里全红 —— 平台写死的测试等于没测。用 ``fontbuffer`` 直接把
#: 字节喂给 ``insert_font``，连临时文件都不需要。
HELVETICA = pymupdf.Font("helv").buffer

#: 判定一个异常是否只是「资源没下载到」，而不是被测不变量被破坏。
#:
#: 端到端那条测试要真跑 ``translate_stream``，转换器初始化时会去拉 BabelDOC 的
#: embedding 资源。CI 上这是外网：``ubuntu-24.04-arm`` 那条腿遇到过
#: ``httpx.ConnectError: All connection attempts failed`` 三次重试全败，于是
#: ``RuntimeError: asset coroutine failed`` 把测试判成 FAILED —— 一次网络抖动
#: 被报成了产品缺陷。网络问题必须 skip，断言问题必须 fail。
_ASSET_FETCH_MARKERS = (
    "connect",
    "network",
    "asset coroutine",
    "retry",
    "timeout",
    "timed out",
    "resolve",
    "download",
    "temporary failure in name resolution",
)


def _looks_like_asset_fetch_failure(exc: BaseException) -> bool:
    """沿 ``__cause__`` / ``__context__`` 链找网络取资源失败的痕迹。"""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = f"{type(cur).__name__}: {cur}".lower()
        if any(marker in text for marker in _ASSET_FETCH_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _flow_entry(text, x, box_width, font_size, page=0, page_height=842.0):
    return {
        "block_id": f"p{page}_flow",
        "page": page,
        "kind": "paragraph",
        "text": "source text",
        "translated": text,
        "render_path": "translate_refit",
        "src_box": [x, page_height - 100.0, x + box_width, page_height - 80.0],
        "dst_box": [x, page_height - 100.0, x + box_width, page_height - 80.0],
        "font_size": font_size,
        "render_payload": {
            "kind": "flow",
            "commands": [
                {
                    "text": text,
                    "x": x,
                    "y": page_height - 80.0,
                    "font_size": font_size,
                    "overflow": False,
                }
            ],
            "entries": [],
        },
    }


class TestNoSilentTruncation(unittest.TestCase):
    """缺陷 1：``insert_text`` 静默截断。"""

    # 真实 repro：排版按 helv 量得 252.6pt（放得下），渲染按 china-ss 画成
    # 607.2pt，597pt 的页面右侧放不下 → 尾巴被 pymupdf 悄悄扔掉。
    REAL = "【译】This page carries a /Rotate entry just like a phone scan."

    def test_pymupdf_really_does_truncate_silently(self):
        """先锁住底层行为：insert_text 越界丢字却返回成功。

        如果哪天 PyMuPDF 改了语义（不再静默丢弃），这个测试会失败并提醒
        我们重新评估 :func:`_fit_line_to_page` 的必要性。
        """
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        rc = page.insert_text(
            (300, 100), self.REAL, fontsize=10.12, fontname="china-ss"
        )
        drawn = page.get_text().strip()
        doc.close()
        self.assertEqual(rc, 1, "insert_text 报告成功")
        self.assertLess(
            len(drawn),
            len(self.REAL),
            "预期 PyMuPDF 静默截断；若不再截断，本测试需重新评估",
        )

    def test_wide_line_is_shrunk_and_fully_drawn(self):
        """放得下（缩字号后）就必须完整落笔，不能少一个字符。"""
        for x in (97.0, 200.0, 83.0):
            with self.subTest(x=x):
                pdf, stats = render_plan_to_pdf(
                    [_flow_entry(self.REAL, x, 300.0, 10.12)],
                    page_sizes={0: [595, 842]},
                )
                doc = pymupdf.open(stream=pdf, filetype="pdf")
                drawn = doc[0].get_text().replace("\n", "")
                doc.close()
                self.assertEqual(
                    drawn,
                    self.REAL,
                    f"x={x}: 译文被截断成 {drawn!r}",
                )
                self.assertEqual(stats.get("fit_shrunk"), 1)
                self.assertNotIn("fit_clipped", stats)

    def test_unavoidable_overflow_is_reported_not_silent(self):
        """缩到下限仍放不下 → 显式裁剪 + 计入 fit_clipped（可观测）。"""
        x = 300.0
        pdf, stats = render_plan_to_pdf(
            [_flow_entry(self.REAL, x, 300.0, 10.12)], page_sizes={0: [595, 842]}
        )
        drawn = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
        drawn = drawn.replace("\n", "")
        self.assertEqual(stats.get("fit_clipped"), 1, "必须有可观测计数")
        self.assertLess(len(drawn), len(self.REAL))
        # 裁剪后必须完整落在页面内，不能再由 pymupdf 二次截断
        words = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text("words")
        for w in words:
            self.assertLessEqual(w[2], 595.0 + 0.5, f"字形越出页面右边界: {w}")

    def test_line_that_fits_is_untouched(self):
        """本来就放得下的行不得被缩放，也不得产生任何告警计数。"""
        pdf, stats = render_plan_to_pdf(
            [_flow_entry("【译】短句", 72.0, 400.0, 11.0)],
            page_sizes={0: [595, 842]},
        )
        self.assertNotIn("fit_shrunk", stats)
        self.assertNotIn("fit_clipped", stats)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc[0].get_text().strip(), "【译】短句")
        doc.close()

    def test_all_draw_paths_share_the_guard(self):
        """list / toc / flow / legacy wrapped 三条路径也必须过宽度闸门。

        四条路径曾各自直接调 ``insert_text``；只修 flow 会留下同样的坑。
        """
        text = open("pdf2zh/v3/magicpdf_renderer.py", encoding="utf-8").read()
        # 渲染器内直接出现的 insert_text 只应存在于 _draw_line 内部
        self.assertEqual(
            text.count("page.insert_text("),
            1,
            "insert_text 必须收敛到 _draw_line 一处",
        )
        self.assertIn("def _draw_line(", text)
        for helper in ("_render_list_commands", "_render_flow_commands"):
            self.assertIn("_draw_line(", text, helper)

    # ---- 纵向：legacy wrapped 路径的静默丢行 ----

    @staticmethod
    def _wrapped_entry(text, box, font_size, page=0):
        x0, y0, x1, y1 = box
        return {
            "block_id": f"p{page}_head",
            "page": page,
            "kind": "heading",
            "text": "original heading",
            "translated": text,
            "render_path": "translate_refit",
            "src_box": [x0, y0, x1, y1],
            "dst_box": [x0, y0, x1, y1],
            "font_size": font_size,
            "render_payload": {"kind": "heading", "commands": [], "entries": []},
        }

    def test_wrapped_block_shrinks_instead_of_dropping_lines(self):
        """译文比原文多一行时必须缩字号整段画完，而不是只画第一行。

        真实 repro（真扫描件 + 真 MinerU）：标题块
        ``'Chapter One: The Scanning Machine'`` 在 203x15pt 的源框里只留下
        ``'Chapter One:'``，后半句无声消失。
        """
        text = "【译】Chapter One: The Scanning Machine"
        pdf, stats = render_plan_to_pdf(
            [self._wrapped_entry(text, [69, 699, 272, 714], 11.05)],
            page_sizes={0: [612, 792]},
        )
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        drawn = doc[0].get_text().replace("\n", "").replace(" ", "")
        doc.close()
        self.assertEqual(
            drawn,
            text.replace(" ", ""),
            f"标题被纵向截断成 {drawn!r}",
        )
        self.assertNotIn("wrap_truncated", stats)
        self.assertEqual(stats.get("fit_shrunk"), 1, "必须靠缩字号才放得下")

    def test_wrapped_block_unfittable_is_reported(self):
        """缩到下限仍放不下 → 记入 wrap_truncated + warning（不再静默）。"""
        text = "【译】" + "very long translated heading " * 12
        with self.assertLogs("pdf2zh.v3.magicpdf_renderer", level="WARNING") as cap:
            pdf, stats = render_plan_to_pdf(
                [self._wrapped_entry(text, [69, 699, 200, 706], 14.0)],
                page_sizes={0: [612, 792]},
            )
        self.assertEqual(stats.get("wrap_truncated"), 1)
        self.assertTrue(any("dropped" in m for m in cap.output), cap.output)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_heading_block_kind_reaches_a_draw_path(self):
        """``render_payload.kind == "heading"`` 不是 list/toc/flow，会落到
        legacy wrapped 路径 —— 那条路径曾经完全不检查高度。锁住它确实会画。"""
        text = "【译】A somewhat longer heading than the original"
        pdf, stats = render_plan_to_pdf(
            [self._wrapped_entry(text, [60, 700, 400, 740], 12.0)],
            page_sizes={0: [612, 792]},
        )
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        drawn = doc[0].get_text().replace("\n", "").replace(" ", "")
        doc.close()
        self.assertEqual(drawn, text.replace(" ", ""))


class TestProseIsNotFormula(unittest.TestCase):
    """缺陷 2：正文被误判为公式 → 落入 KEEP_KINDS → 永不翻译。"""

    PROSE = [
        "The quick brown fox jumps over the lazy dog. 0123456789 (test).",
        "As shown in Figure 3 (left), the encoder converges within 12 epochs.",
        "See Smith et al. (2019) for a detailed treatment of the method.",
        "The model was trained on 2001-2010 data and evaluated on 2011-2015.",
        "This section describes the algorithm (Algorithm 1) in detail.",
        "Note that f(x) = x^2 + 1 for all x in the interval [0, 1].",
        "The temperature was set to 0.7 (see Appendix B for details).",
        "We compare against the baseline of 45.2 +/- 1.3 on the dev set.",
        "Figure 7(a) shows the effect of varying the number of layers.",
        "A detailed derivation is provided in Appendix A (pages 30-45).",
        "It is well known that E = mc^2, but the units matter here.",
        "The second edition (ISBN 978-3-16-148410-0) appeared in 2017.",
        "Our approach reduces inference latency by roughly 40% (2.1x).",
        "The upper bound is O(n log n), as shown by Smith (2018).",
        "Equation (4) defines the loss as a function of the parameters.",
        "Coordinates therefore start at (28, 36) rather than (0, 0).",
        "Ranges like 2001-2010 and 45.2 +/- 1.3 show up too.",
        "Equations (1) and (2) are referenced in the running text.",
        "\u672c\u4e66\u7b2c 1~3 \u7ae0\u4ecb\u7ecd\u4e86\u7b97\u6cd5\u7684\u57fa\u7840\u6982\u5ff5\u3002",
        "\u56fe 2~4 \u7ed9\u51fa\u4e86 1~2 \u4e2a\u5b8c\u6574\u7684\u4f8b\u5b50\u3002",
    ]

    MATH = [
        "E = mc^2",
        "x^2 + y^2 = r^2",
        "\\int_0^1 f(x) dx",
        "a + b = c",
        "n \u2192 \u221e",
        "2001-2010",
        "45.2 +/- 1.3",
        "A = \\frac{1}{2}",
        "\\sum_{i=1}^{n} x_i",
    ]

    def test_prose_is_never_a_display_formula(self):
        for text in self.PROSE:
            with self.subTest(text=text[:40]):
                self.assertFalse(
                    _looks_like_display_formula(text),
                    f"散文被误判为公式: {text!r}",
                )

    def test_real_math_is_still_a_formula(self):
        for text in self.MATH:
            with self.subTest(text=text):
                self.assertTrue(
                    _looks_like_display_formula(text),
                    f"真公式未被识别: {text!r}",
                )

    def test_prose_role_is_not_formula_end_to_end(self):
        """经 StructureClassifier 走一遍，role 也不能是 FORMULA。"""
        from pdf2zh.v3.geometry import Char, Line, Paragraph, Word
        from pdf2zh.v3.structure import StructureClassifier

        classifier = StructureClassifier()
        for text in self.PROSE:
            with self.subTest(text=text[:40]):
                chars = [
                    Char(
                        text=ch,
                        x0=70.0 + 6.0 * i,
                        y0=700.0,
                        x1=70.0 + 6.0 * (i + 1),
                        y1=712.0,
                        size=11.0,
                        font="Body",
                    )
                    for i, ch in enumerate(text)
                ]
                line = Line(words=[Word(chars=chars)])
                para = Paragraph(lines=[line])
                classified = classifier.classify_paragraph(
                    para, page=None, body_font_size=11.0
                )
                self.assertNotEqual(classified.role, BlockRole.FORMULA, text[:50])

    def test_formula_kind_is_still_preserved(self):
        """真正的公式仍必须保留 —— 修复不能把保护语义一起废掉。"""
        self.assertIn("formula", KEEP_KINDS)
        self.assertTrue(_looks_like_display_formula("x^2 + y^2 = r^2"))


class TestNoSilentPageLoss(unittest.TestCase):
    """缺陷 3：render_plan 里没有的页被静默丢出产物。"""

    @staticmethod
    def _source_pdf(n_pages=3, width=595.0, height=842.0):
        import os
        import tempfile

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "scan.pdf")
        doc = pymupdf.open()
        for _ in range(n_pages):
            doc.new_page(width=width, height=height)
        doc.save(path)
        doc.close()
        return path

    def test_page_without_plan_entries_is_still_emitted(self):
        """3 页源、page_sizes 齐全、plan 只覆盖 2 页 → 必须输出 3 页。"""
        sizes = {0: [595, 842], 1: [595, 842], 2: [595, 842]}
        plan = [_flow_entry("T", 72.0, 300.0, 12.0, page=p) for p in (0, 2)]
        pdf, stats = render_plan_to_pdf(plan, page_sizes=sizes)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 3)
        doc.close()
        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["pages_without_entries"], 1, "空页必须可观测")

    def test_background_preserves_full_source_page_count(self):
        """带背景层时页数必须与源一致（扫描件内容全靠背景层承载）。"""
        src = self._source_pdf(4)
        sizes = {i: [595, 842] for i in range(4)}
        plan = [_flow_entry("T", 72.0, 300.0, 12.0, page=1)]
        pdf, _stats = render_plan_to_pdf(plan, page_sizes=sizes, source_pdf=src)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 4)
        doc.close()

    def test_empty_plan_still_emits_all_known_pages(self):
        sizes = {i: [595, 842] for i in range(3)}
        pdf, stats = render_plan_to_pdf([], page_sizes=sizes)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 3)
        doc.close()
        self.assertEqual(stats["pages_without_entries"], 3)

    def test_empty_plan_without_page_sizes_stays_one_page(self):
        """向后兼容：连 page_sizes 都没有时仍产 1 页（pymupdf 无 0 页 PDF）。"""
        pdf, stats = render_plan_to_pdf([], page_sizes={})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 1)
        doc.close()
        self.assertEqual(stats["pages"], 1)

    def test_per_page_sizes_are_respected(self):
        """page_sizes 决定每页尺寸，混排页面不能被统一成默认 A4。"""
        sizes = {0: [842, 595], 1: [419, 595]}
        pdf, _ = render_plan_to_pdf([], page_sizes=sizes)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(
            [(round(p.rect.width), round(p.rect.height)) for p in doc],
            [(842, 595), (419, 595)],
        )
        doc.close()


class TestFontResourcesAndLengthIntegrity(unittest.TestCase):
    """缺陷 4：``/Length`` 损坏 + 字体广播失效。"""

    def test_find_math_fonts_is_read_only(self):
        doc = pymupdf.open()
        for i in range(3):
            doc.new_page(width=400, height=400).insert_text((20, 40), f"p{i}")
        math_xref = doc.get_new_xref()
        doc.update_object(
            math_xref, "<< /Type /Font /Subtype /Type1 /BaseFont /CMR10 >>"
        )
        stream = doc[0].get_contents()[0]
        before = doc.xref_object(stream)

        found = find_math_fonts(doc)

        self.assertEqual(doc.xref_object(stream), before, "诊断函数写坏了文档")
        self.assertNotIn("/Length null", before)
        self.assertEqual(before.count("/Length"), 1, "流字典不得出现重复 /Length")
        self.assertIn(math_xref, [x for x, _ in found], "数学字体未被报告")
        self.assertTrue(any("CMR10" in n for _x, n in found))
        doc.close()

    def test_removed_buggy_pattern_really_was_corrupting(self):
        """锁住被删掉的那行代码曾经造成的损坏。

        这正是旧 ``_protect_math_fonts`` 写出来的东西：重复的 ``/Length``
        加一个无键子字典。留着这个断言是为了让「为什么不能恢复那行代码」
        留在测试里。
        """
        doc = pymupdf.open()
        doc.new_page(width=200, height=200).insert_text((20, 40), "x")
        stream = doc[0].get_contents()[0]
        self.assertEqual(doc.xref_get_key(stream, "/Length")[0], "null")
        doc.xref_set_key(stream, "/Length", doc.xref_get_key(stream, "/Length")[1])
        broken = doc.xref_object(stream)
        doc.close()
        self.assertIn("/Length null", broken)
        self.assertGreater(broken.count("/Length"), 1)

    def test_protect_math_fonts_never_writes_xref(self):
        """``_protect_math_fonts`` 必须是纯只读诊断，不得写任何 xref。

        旧实现写的是 ``xref_set_key(xref, "/Length", ...)``。它之所以从未真的
        损坏过文件，纯粹是因为**读**侧的 ``"/BaseFont"``/``"/Length"`` 同样带
        前导斜杠、在 PyMuPDF 1.28.2 上一律返回 null，整个循环空转 —— 也就是
        说损坏是**潜伏**的：任何人一旦「顺手修好读侧」，写侧立刻生效。

        所以这里不能靠端到端行为断言（旧代码本就不改变行为），必须直接断言
        「这个函数一个 xref 写调用都没有」。
        """
        import ast

        for path, cls_name in (
            ("pdf2zh/high_level.py", "_protect_math_fonts"),
            ("pdf2zh/assembler/mupdf_assembler.py", "_protect_math_fonts"),
        ):
            with self.subTest(path=path):
                tree = ast.parse(open(path, encoding="utf-8").read())
                fns = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == cls_name
                ]
                self.assertTrue(fns, f"{path}: 找不到 {cls_name}")
                for fn in fns:
                    writes = [
                        n
                        for n in ast.walk(fn)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr
                        in ("xref_set_key", "update_object", "update_stream", "set_key")
                    ]
                    self.assertEqual(
                        [w.func.attr for w in writes],
                        [],
                        f"{path}:{fn.lineno} {cls_name} 仍在写 xref",
                    )

    def test_no_multilevel_xref_set_key_anywhere(self):
        """``xref_set_key`` 不支持多级路径（PyMuPDF 1.28.2 会抛
        ``path to '<name>' has indirects``），源码里不得再出现这种调用。"""
        import ast

        for path in ("pdf2zh/high_level.py", "pdf2zh/assembler/mupdf_assembler.py"):
            with self.subTest(path=path):
                tree = ast.parse(open(path, encoding="utf-8").read())
                for node in ast.walk(tree):
                    if not (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "xref_set_key"
                        and node.args
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                    ):
                        continue
                    self.assertNotIn(
                        "/",
                        node.args[1].value.strip("/"),
                        f"{path}:{node.lineno} 多级路径 xref_set_key("
                        f"{node.args[1].value!r}) 必然抛异常",
                    )

    def test_high_level_no_longer_writes_length_or_broadcasts_by_path(self):
        """源码级守卫：坏写法不得回潮。

        - ``xref_set_key(..., "/Length", ...)``（写出重复键）
        - ``xref_set_key(..., "Resources/Font/...", ...)``（多级路径必抛异常）

        只看**代码**：两处的修复说明里都要引述这些坏字符串，所以必须先剥掉
        注释与字符串字面量，否则守卫会被文档本身触发。
        """
        import io as _io
        import tokenize

        for path in (
            "pdf2zh/high_level.py",
            "pdf2zh/assembler/mupdf_assembler.py",
        ):
            with open(path, "rb") as fh:
                code = fh.read()
            kept = [
                tok.string
                for tok in tokenize.tokenize(_io.BytesIO(code).readline)
                if tok.type not in (tokenize.COMMENT, tokenize.STRING)
            ]
            src = "".join(kept)
            with self.subTest(path=path):
                self.assertNotIn('"/Length"', src, f"{path} 仍在写 /Length")
                self.assertNotIn(
                    "Resources/Font/", src, f"{path} 仍在用多级路径广播字体"
                )
                self.assertNotIn("'/Length'", src, f"{path} 仍在写 /Length")

    def test_broadcast_registers_font_on_every_page(self):
        doc = pymupdf.open()
        for i in range(6):
            doc.new_page(width=595, height=842).insert_text((72, 100), f"p{i}")
        font_xref = doc[0].insert_font("noto", fontbuffer=HELVETICA)
        for i in range(1, 6):
            self.assertTrue(
                broadcast_page_font(doc, doc[i].xref, font_xref, "noto"),
                f"page {i} 登记失败",
            )
        for i in range(6):
            self.assertNotEqual(
                doc.xref_get_key(doc[i].xref, "Resources/Font/noto")[0],
                "null",
                f"page {i} 缺 /noto",
            )
        out = doc.write(deflate=True, garbage=3)
        doc.close()
        with pikepdf.open(io.BytesIO(out)) as pdf:
            self.assertEqual(len(pdf.pages), 6)
            for i, page in enumerate(pdf.pages):
                keys = {str(k) for k in page.obj["/Resources"]["/Font"].keys()}
                self.assertIn("/noto", keys, f"p{i}")

    def test_broadcast_creates_font_dict_when_absent(self):
        """页面连 /Resources 都没有（或被写成 null）时也要能登记。"""
        for broken in (None, "null"):
            with self.subTest(resources=broken):
                doc = pymupdf.open()
                doc.new_page(width=300, height=300)
                font_xref = doc[0].insert_font("noto", fontbuffer=HELVETICA)
                page_xref = doc[0].xref
                if broken is None:
                    # 整键删掉：直接改页面对象文本（PyMuPDF 没有 del-key API）
                    body = re.sub(
                        r"\s*/Resources\s+\d+ 0 R", "", doc.xref_object(page_xref)
                    )
                    doc.update_object(page_xref, body)
                else:
                    doc.xref_set_key(page_xref, "Resources", "null")
                self.assertEqual(doc.xref_get_key(page_xref, "Resources")[0], "null")

                self.assertTrue(broadcast_page_font(doc, page_xref, font_xref, "noto"))
                self.assertNotEqual(
                    doc.xref_get_key(page_xref, "Resources/Font/noto")[0], "null"
                )
                out = doc.write(deflate=True, garbage=3)
                doc.close()
                with pikepdf.open(io.BytesIO(out)) as pdf:
                    keys = {
                        str(k) for k in pdf.pages[0].obj["/Resources"]["/Font"].keys()
                    }
                    self.assertIn("/noto", keys)

    def test_broadcast_is_idempotent(self):
        doc = pymupdf.open()
        doc.new_page(width=300, height=300)
        font_xref = doc[0].insert_font("noto", fontbuffer=HELVETICA)
        for _ in range(3):
            self.assertTrue(broadcast_page_font(doc, doc[0].xref, font_xref, "noto"))
        self.assertEqual(
            doc.xref_get_key(doc[0].xref, "Resources/Font/noto")[1],
            f"{font_xref} 0 R",
        )
        doc.close()

    def test_no_content_stream_font_is_dangling(self):
        """端到端：内容流用到的每个 ``Tf`` 字体都必须在该页资源里。"""
        doc = pymupdf.open()
        for i in range(5):
            doc.new_page(width=595, height=842).insert_text(
                (72, 100 + i * 20), f"E = mc^2 page {i}"
            )
        math_xref = doc.get_new_xref()
        doc.update_object(
            math_xref, "<< /Type /Font /Subtype /Type1 /BaseFont /CMR10 >>"
        )
        font_xref = doc[0].insert_font("noto", fontbuffer=HELVETICA)
        for i in range(1, 5):
            broadcast_page_font(doc, doc[i].xref, font_xref, "noto")
        out = doc.write(deflate=True, garbage=3)
        doc.close()

        with pikepdf.open(io.BytesIO(out)) as pdf:
            for i, page in enumerate(pdf.pages):
                names = {
                    str(k).lstrip("/") for k in page.obj["/Resources"]["/Font"].keys()
                }
                contents = page.obj["/Contents"]
                streams = (
                    [c for c in contents]
                    if isinstance(contents, pikepdf.Array)
                    else [contents]
                )
                raw = b"".join(bytes(s.read_bytes()) for s in streams)
                used = {
                    m.decode()
                    for m in re.findall(rb"/([A-Za-z0-9#+._-]+)\s+[\d.]+\s+Tf", raw)
                }
                self.assertEqual(
                    used - names,
                    set(),
                    f"page {i} 有悬空字体引用 {used - names}",
                )


class TestAssetFetchFailureClassifier(unittest.TestCase):
    """``_looks_like_asset_fetch_failure`` 自身也要被测，否则它可能退化成永假。"""

    def test_connect_error_chain_counts(self):
        try:
            try:
                raise ConnectionError("All connection attempts failed")
            except ConnectionError as inner:
                raise RuntimeError("asset coroutine failed: RetryError") from inner
        except RuntimeError as exc:
            self.assertTrue(_looks_like_asset_fetch_failure(exc))

    def test_plain_failure_does_not_count(self):
        for exc in (
            ValueError("bad page"),
            AssertionError(""),
            RuntimeError("document has 0 pages"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(_looks_like_asset_fetch_failure(exc))


class TestTranslateStreamMathFontIntegrity(unittest.TestCase):
    """端到端跑一遍 ``translate_stream`` —— ``/Length`` 损坏正是在这里发生。

    ``_protect_math_fonts`` 是 ``translate_stream`` 内部的闭包，只测
    ``font_cache.find_math_fonts`` 抓不到「有人又把 /Length 写回去」，所以必须
    真跑一次翻译链，断言产物里没有重复键、且每页字体资源都能解析。
    """

    @classmethod
    def setUpClass(cls):
        import pdf2zh.converter as cv
        import pdf2zh.high_level as hl
        from pdf2zh.doclayout import ModelInstance, OnnxModel

        class _Translator:
            lang_out = "zh-CN"

            def translate(self, text, **_kw):
                stripped = (text or "").strip()
                return f"T:{stripped}" if stripped else text

        def _fake_build(
            service, lang_in, lang_out, envs=None, prompt=None, ignore_cache=False
        ):
            return _Translator()

        cls._orig = {
            "hl": hl.build_translator,
            "cv": cv.build_translator,
            "model": ModelInstance.value,
        }
        hl.build_translator = _fake_build
        cv.build_translator = _fake_build
        model = OnnxModel.load_available()
        if model is None:
            raise unittest.SkipTest("DocLayout onnx unavailable")
        ModelInstance.value = model

    @classmethod
    def tearDownClass(cls):
        import pdf2zh.converter as cv
        import pdf2zh.high_level as hl
        from pdf2zh.doclayout import ModelInstance

        hl.build_translator = cls._orig["hl"]
        cv.build_translator = cls._orig["cv"]
        ModelInstance.value = cls._orig["model"]

    def _math_pdf(self, n_pages=4):
        import os
        import tempfile

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "math.pdf")
        doc = pymupdf.open()
        for i in range(n_pages):
            doc.new_page(width=612, height=792).insert_text(
                (72, 100 + i * 30),
                f"E = mc^2 and x_1 + x_2 = y on page {i}",
                fontsize=11,
            )
        math_xref = doc.get_new_xref()
        doc.update_object(
            math_xref, "<< /Type /Font /Subtype /Type1 /BaseFont /CMR10 >>"
        )
        doc.save(path)
        doc.close()
        return path

    def test_translate_stream_output_has_no_corrupt_length_and_no_dangling_font(self):
        import pdf2zh.high_level as hl
        from pdf2zh.doclayout import ModelInstance

        src = self._math_pdf()
        with open(src, "rb") as fh:
            data = fh.read()
        try:
            dual, mono = hl.translate_stream(
                data,
                lang_in="en",
                lang_out="zh-CN",
                service="google",
                thread=1,
                envs={},
                ignore_cache=True,
                parallel_pages=False,
                model=ModelInstance.value,
            )
        except Exception as exc:  # noqa: BLE001 -- 只把「取不到资源」当 skip
            if _looks_like_asset_fetch_failure(exc):
                raise unittest.SkipTest(f"BabelDOC assets unreachable: {exc}") from exc
            raise
        for tag, blob in (("mono", mono), ("dual", dual)):
            with self.subTest(output=tag):
                self.assertNotIn(
                    b"/Length null",
                    blob,
                    f"{tag}: 出现 '/Length null' —— 重复键 + 无键子字典",
                )
                with pikepdf.open(io.BytesIO(blob)) as pdf:
                    for i, page in enumerate(pdf.pages):
                        names = {
                            str(k).lstrip("/")
                            for k in page.obj["/Resources"]["/Font"].keys()
                        }
                        contents = page.obj["/Contents"]
                        streams = (
                            [c for c in contents]
                            if isinstance(contents, pikepdf.Array)
                            else [contents]
                        )
                        raw = b"".join(bytes(s.read_bytes()) for s in streams)
                        used = {
                            m.decode()
                            for m in re.findall(
                                rb"/([A-Za-z0-9#+._-]+)\s+[\d.]+\s+Tf", raw
                            )
                        }
                        self.assertEqual(
                            used - names,
                            set(),
                            f"{tag} page {i}: 悬空字体 {used - names}",
                        )
                # MuPDF 也必须能重新打开
                doc = pymupdf.open(stream=blob, filetype="pdf")
                self.assertGreaterEqual(doc.page_count, 1)
                doc.close()


if __name__ == "__main__":
    unittest.main()
