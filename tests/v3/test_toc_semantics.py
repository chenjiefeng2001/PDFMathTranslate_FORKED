# -*- coding: utf-8 -*-
"""V8.7 — TOC Semantic Rendering 纯逻辑单元测试。

覆盖：
- TOC Grammar 解析（Chapter/Section/Part/Appendix/Contents/Index + PLAIN）
- 结构词模板渲染（第X章 / 第X节 / 第X篇 / 附录X / 目录 / 索引）
- 翻译边界：结构词不送翻译器、剩余标题才送、leader/page 永不翻译
- 语言族回退（zh / cht / en / 未知 → en 恒等）
- compose_toc_title 恒等契约（None / PLAIN → 原样返回）
"""
import unittest

from pdf2zh.v3.toc_semantics import (
    TOCKind,
    TOCEntry,
    parse_toc_entry,
    toc_structure_prefix,
    TOCTranslationPolicy,
    compose_toc_title,
    render_toc_line,
)


class TestParseTocEntry(unittest.TestCase):
    def test_chapter(self):
        e = parse_toc_entry("Chapter 3")
        self.assertEqual(e.kind, TOCKind.CHAPTER)
        self.assertEqual(e.number, "3")
        self.assertEqual(e.level, 1)
        self.assertEqual(e.title, "")
        self.assertTrue(e.matched)

    def test_chapter_no_space(self):
        e = parse_toc_entry("Chapter3")
        self.assertEqual(e.kind, TOCKind.CHAPTER)
        self.assertEqual(e.number, "3")

    def test_section_with_remainder(self):
        e = parse_toc_entry("Section 3.2 Experimental Setup", page="42")
        self.assertEqual(e.kind, TOCKind.SECTION)
        self.assertEqual(e.number, "3.2")
        self.assertEqual(e.level, 2)
        self.assertEqual(e.title, "Experimental Setup")
        self.assertEqual(e.page, "42")

    def test_subsection(self):
        e = parse_toc_entry("Subsection 4.1.2 Method")
        self.assertEqual(e.kind, TOCKind.SUBSECTION)
        self.assertEqual(e.number, "4.1.2")
        self.assertEqual(e.title, "Method")

    def test_part_roman(self):
        e = parse_toc_entry("Part III Foundations")
        self.assertEqual(e.kind, TOCKind.PART)
        self.assertEqual(e.number, "III")
        self.assertEqual(e.title, "Foundations")

    def test_appendix_letter(self):
        e = parse_toc_entry("Appendix A: Technical Details")
        self.assertEqual(e.kind, TOCKind.APPENDIX)
        self.assertEqual(e.number, "A")
        self.assertEqual(e.title, "Technical Details")

    def test_contents(self):
        e = parse_toc_entry("Table of Contents")
        self.assertEqual(e.kind, TOCKind.CONTENTS)
        self.assertTrue(e.matched)
        self.assertEqual(e.title, "")

    def test_index(self):
        e = parse_toc_entry("Index")
        self.assertEqual(e.kind, TOCKind.INDEX)
        self.assertTrue(e.matched)

    def test_plain_untouched(self):
        e = parse_toc_entry("Intro")
        self.assertEqual(e.kind, TOCKind.PLAIN)
        self.assertFalse(e.matched)
        self.assertEqual(e.title, "Intro")

    def test_cjk_title_not_matched(self):
        e = parse_toc_entry("实验装置与测量")
        self.assertEqual(e.kind, TOCKind.PLAIN)
        self.assertFalse(e.matched)
        self.assertEqual(e.title, "实验装置与测量")

    def test_empty(self):
        e = parse_toc_entry("")
        self.assertEqual(e.kind, TOCKind.UNKNOWN)

    def test_to_dict(self):
        e = parse_toc_entry("Section 2 Results", page="12")
        d = e.to_dict()
        self.assertEqual(d["kind"], "section")
        self.assertEqual(d["number"], "2")
        self.assertEqual(d["title"], "Results")
        self.assertEqual(d["page"], "12")
        self.assertTrue(d["matched"])


class TestTocTemplates(unittest.TestCase):
    def test_zh_templates(self):
        cases = [
            (TOCKind.CHAPTER, "3", "第3章"),
            (TOCKind.SECTION, "3.2", "第3.2节"),
            (TOCKind.PART, "I", "第I篇"),
            (TOCKind.APPENDIX, "A", "附录A"),
            (TOCKind.CONTENTS, "", "目录"),
            (TOCKind.INDEX, "", "索引"),
        ]
        for kind, number, expected in cases:
            e = TOCEntry(raw="x", kind=kind, matched=True, number=number)
            self.assertEqual(toc_structure_prefix(e, "zh-CN"), expected)

    def test_en_template_is_identity(self):
        e = TOCEntry(raw="Chapter 5", kind=TOCKind.CHAPTER, matched=True, number="5")
        self.assertEqual(toc_structure_prefix(e, "en"), "Chapter 5")

    def test_cht_maps_to_zh(self):
        e = TOCEntry(raw="Chapter 2", kind=TOCKind.CHAPTER, matched=True, number="2")
        self.assertEqual(toc_structure_prefix(e, "cht"), "第2章")

    def test_unknown_lang_falls_back_to_en_identity(self):
        e = TOCEntry(raw="Chapter 2", kind=TOCKind.CHAPTER, matched=True, number="2")
        self.assertEqual(toc_structure_prefix(e, "fr"), "Chapter 2")

    def test_plain_has_no_prefix(self):
        e = parse_toc_entry("Intro")
        self.assertEqual(toc_structure_prefix(e, "zh-CN"), "")


class TestTocTranslationPolicy(unittest.TestCase):
    def test_structure_only_is_local(self):
        e = parse_toc_entry("Chapter 3")
        d = TOCTranslationPolicy("zh-CN").decide(e)
        self.assertTrue(d["local_only"])
        self.assertFalse(d["translate_title"])
        self.assertEqual(d["structure_prefix"], "第3章")

    def test_remainder_flags_translation(self):
        e = parse_toc_entry("Section 3.2 Experimental Setup")
        d = TOCTranslationPolicy("zh-CN").decide(e)
        self.assertTrue(d["translate_title"])
        self.assertEqual(d["structure_prefix"], "第3.2节")

    def test_leader_page_always_kept(self):
        e = parse_toc_entry("Chapter 3", page="42", leader="...")
        d = TOCTranslationPolicy("zh-CN").decide(e)
        self.assertTrue(d["keep_leader"])
        self.assertTrue(d["keep_page"])

    def test_compose_with_remainder(self):
        e = parse_toc_entry("Section 3.2 Experimental Setup")
        self.assertEqual(
            TOCTranslationPolicy("zh-CN").compose(e, "实验设置"),
            "第3.2节 实验设置",
        )

    def test_compose_local_only(self):
        e = parse_toc_entry("Chapter 3")
        self.assertEqual(TOCTranslationPolicy("zh-CN").compose(e, ""), "第3章")

    def test_compose_plain_strips_nothing(self):
        e = parse_toc_entry("Intro")
        self.assertEqual(TOCTranslationPolicy("zh-CN").compose(e, "介绍"), "介绍")


class TestComposeTocTitleIdentity(unittest.TestCase):
    """converter 钩子恒等契约：None / PLAIN / 未匹配 → 原样返回，零改动。"""

    def test_none_passthrough(self):
        self.assertEqual(compose_toc_title(None, "实验说明", "zh-CN"), "实验说明")

    def test_plain_passthrough(self):
        e = parse_toc_entry("Intro")
        self.assertEqual(compose_toc_title(e, "介绍", "zh-CN"), "介绍")

    def test_matched_composes(self):
        e = parse_toc_entry("Chapter 2 Overview")
        self.assertEqual(compose_toc_title(e, "概述", "zh-CN"), "第2章 概述")

    def test_empty_translation_keeps_prefix(self):
        e = parse_toc_entry("Appendix A")
        self.assertEqual(compose_toc_title(e, "", "zh-CN"), "附录A")


class TestRenderTocLine(unittest.TestCase):
    def test_full_line(self):
        e = parse_toc_entry("Section 2 Results", page="12", leader="............")
        out = render_toc_line(e, "结果", "zh-CN")
        self.assertTrue(out.startswith("第2节 结果"))
        self.assertTrue(out.endswith("12"))
        self.assertIn("............", out)


if __name__ == "__main__":
    unittest.main()