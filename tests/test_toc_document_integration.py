"""Commit 6B 集成测试：TOC 接入 Document Model + translation plan。

覆盖 ``pdf2zh.v3.toc_sidechannel`` + ``document_model`` 集成：

1.  TOC 块进入 Document Model（正式 semantic block，非 debug-only）；
2.  TOC title 会翻译；
3.  printed page number 不进入 translator；
4.  dot leader 不进入 translator；
5.  numbering 不进入 translator；
6.  destination_page 不进入 translator；
7.  translated title 保留原始几何 metadata（title_x/page_x/indent/bbox）；
8.  TOCEntry 能关联对应 Heading（heading_ref）；
9.  TOCEntry 找不到 Heading 时仍正常工作（heading_ref=None）；
10. page_number != destination_page 两个字段独立；
11. multi-level TOC 保持 level；
12. multi-page TOC 保持 entry 顺序；
13. 既有 List/Code/Style 路径不回归（由既有测试 + 此处不做破坏性改动保证）。
"""

import unittest

from pdf2zh.semantic.models import TOCEntryNode
from pdf2zh.v3.canonical_page import BlockModel, PageModel
from pdf2zh.v3.document_model import (
    DocumentModel,
    render_plan_from_model,
    toc_records_from_model,
    translate_document,
)
from pdf2zh.v3.toc_sidechannel import (
    attach_toc_entries,
    entry_to_dict,
    resolve_toc_headings,
    translate_toc_entries,
)


def tent(
    title,
    level=0,
    page_number="1",
    dest=None,
    title_x=72.0,
    page_x=540.0,
    indent=72.0,
    leader="..............",
    bbox=None,
):
    return TOCEntryNode(
        title=title,
        level=level,
        page_number=page_number,
        destination_page=dest,
        title_x=title_x,
        page_x=page_x,
        indent=indent,
        dot_leader=leader,
        leader_present=bool(leader),
        bbox=bbox or (40.0, 40.0, 540.0, 200.0),
    )


def build_model(entries, headings=None, page_num=1):
    """一页：宿主块承载 TOC 条目 + 可选的 heading 块。"""
    page = PageModel(page_num=page_num)
    host = BlockModel(
        text="TOC placeholder", kind="paragraph", x0=40, y0=40, x1=500, y1=200
    )
    page.blocks.append(host)
    for h in headings or []:
        page.blocks.append(
            BlockModel(text=h, kind="heading", x0=40, y0=300, x1=300, y1=315)
        )
    attach_toc_entries(page, [entry_to_dict(e) for e in entries])
    model = DocumentModel()
    model.pages = [page]
    return model


def spy_translator():
    calls = []
    return calls, (lambda s: calls.append(s) or ("译_" + s))


class TestTocDocumentIntegration(unittest.TestCase):
    # ── 1. TOC 进入 Document Model ─────────────────────────────
    def test_toc_block_enters_document_model(self):
        entries = [
            tent("1 Introduction", page_number="1"),
            tent("2 Method", page_number="5"),
        ]
        model = build_model(entries)
        page = model.pages[0]
        toc = [b for b in page.blocks if b.kind == "toc"]
        self.assertTrue(toc, "attach 后应有 kind==toc 的块")
        self.assertEqual(len(toc), 1)
        self.assertEqual(len(toc[0].metadata["toc_entries"]), 2)
        self.assertIn("toc_entries", toc[0].metadata)
        # 是模型的一部分（可 JSON 序列化）
        self.assertIsInstance(model, DocumentModel)

    # ── 2. 标题翻译；3-6. 非标题字段绝不进 translator ──────────
    def test_title_translated_others_preserved(self):
        entries = [
            tent(
                "2.1 Dataset",
                page_number="15",
                dest=9,
                leader="...............",
                page_x=540.0,
                title_x=96.0,
            )
        ]
        calls, tr = spy_translator()
        model = build_model(entries)
        translate_document(model, tr)
        # translator 只收到标题余量 “Dataset”
        self.assertEqual(calls, ["Dataset"])
        self.assertNotIn("15", calls)  # page number
        self.assertNotIn("..............", calls)  # leader
        self.assertNotIn("9", calls)  # destination
        self.assertTrue(
            not any(("2.1" in c or c.strip() == "2.1") for c in calls)
        )  # numbering
        entry = model.pages[0].blocks[0].metadata["toc_entries"][0]
        self.assertEqual(entry["translated_title"], "译_Dataset")
        self.assertEqual(entry["page_number"], "15")
        self.assertEqual(entry["destination_page"], 9)
        self.assertEqual(entry["dot_leader"], "...............")
        self.assertEqual(entry["number"].strip(), "2.1")
        # 构成后的可渲染标题 = 编号 + 译文
        self.assertEqual(entry["translated"], "2.1 译_Dataset")

    def test_numbering_not_passed_to_translator(self):
        calls, tr = spy_translator()
        model = build_model([tent("1.1.1 Deep", page_number="7")])
        translate_document(model, tr)
        self.assertEqual(calls, ["Deep"])

    # ── 7. translated title 保留原始几何 metadata ──────────────
    def test_translated_keeps_geometry_metadata(self):
        entries = [
            tent(
                "Method",
                page_number="5",
                title_x=72.0,
                page_x=530.0,
                indent=72.0,
                bbox=(40, 40, 540, 200),
            )
        ]
        calls, tr = spy_translator()
        model = build_model(entries)
        translate_document(model, tr)
        e = model.pages[0].blocks[0].metadata["toc_entries"][0]
        self.assertEqual(e["translated_title"], "译_Method")
        self.assertEqual(e["title_x"], 72.0)
        self.assertEqual(e["page_x"], 530.0)
        self.assertEqual(e["indent"], 72.0)
        self.assertEqual(e["bbox"], [40.0, 40.0, 540.0, 200.0])

    # ── 8. TOCEntry → Heading 关联 ─────────────────────────────
    def test_entry_associates_heading(self):
        entries = [tent("1. Introduction", page_number="1")]
        headings = ["Introduction", "Another Heading"]
        model = build_model(entries, headings=headings)
        calls, tr = spy_translator()
        translate_document(model, tr)
        entry = model.pages[0].blocks[0].metadata["toc_entries"][0]
        # heading 块是第 1 块（宿主）之后的第 0 个 heading
        self.assertEqual(entry["heading_ref"], "p1_1")

    # ── 9. 无匹配 Heading 时正常工作 ───────────────────────────
    def test_unmatched_entry_works(self):
        entries = [tent("Unmatched Topic", page_number="5")]
        model = build_model(entries, headings=["Something Else"])
        calls, tr = spy_translator()
        translate_document(model, tr)
        e = model.pages[0].blocks[0].metadata["toc_entries"][0]
        self.assertIsNone(e["heading_ref"])
        self.assertEqual(e["translated_title"], "译_Unmatched Topic")
        self.assertEqual(e["page_number"], "5")

    # ── 10. page_number != destination_page 独立 ───────────────
    def test_page_number_and_destination_independent(self):
        e = entry_to_dict(tent("Intro", page_number="3", dest=41))
        self.assertEqual(e["page_number"], "3")
        self.assertEqual(e["destination_page"], 41)
        self.assertNotEqual(e["page_number"], e["destination_page"])
        # 翻译后仍不混
        calls, tr = spy_translator()
        out = translate_toc_entries([e], tr)
        self.assertEqual(out[0]["page_number"], "3")
        self.assertEqual(out[0]["destination_page"], 41)

    # ── 11. multi-level 保持 level ─────────────────────────────
    def test_multi_level_preserved(self):
        entries = [
            tent("1 Intro", level=0, page_number="1"),
            tent("1.1 Background", level=1, page_number="2", title_x=96.0),
            tent("1.1.1 Deep", level=2, page_number="3", title_x=120.0),
        ]
        calls, tr = spy_translator()
        out = translate_toc_entries([entry_to_dict(e) for e in entries], tr)
        self.assertEqual([e["level"] for e in out], [0, 1, 2])

    # ── 12. multi-page 保持 entry 顺序 ─────────────────────────
    def test_multipage_entry_order(self):
        def page_with(entries, pno):
            return build_model(entries, page_num=pno)

        m1 = build_model([tent("A First", page_number="1")], page_num=1)
        m2 = build_model([tent("B Second", page_number="2")], page_num=2)
        m1.pages.append(m2.pages[0])
        m1.metadata["page_order"] = [1, 2]
        calls, tr = spy_translator()
        translate_document(m1, tr)
        order = []
        for p in m1.pages:
            for b in p.blocks:
                if b.kind == "toc":
                    order.extend(e["title_only"] for e in b.metadata["toc_entries"])
        self.assertEqual(order, ["A First", "B Second"])

    # ── render plan 携带 toc_entries ───────────────────────────
    def test_render_plan_carries_toc_entries(self):
        entries = [
            tent(
                "1. Introduction",
                page_number="1",
                dest=3,
                title_x=72.0,
                page_x=540.0,
                indent=72.0,
            )
        ]
        model = build_model(entries)
        calls, tr = spy_translator()
        translate_document(model, tr)
        plan = render_plan_from_model(model)
        toc = [p for p in plan if p["kind"] == "toc"][0]
        self.assertEqual(toc["toc_entries"][0]["translated_title"], "译_Introduction")
        self.assertEqual(toc["toc_entries"][0]["page_number"], "1")
        self.assertEqual(toc["toc_entries"][0]["destination_page"], 3)
        self.assertEqual(toc["toc_entries"][0]["title_x"], 72.0)

    # ── toc_records_from_model：结构化条目 → 记录 ──────────────
    def test_toc_records_structured(self):
        entries = [tent("2.1 Dataset", page_number="15", dest=17)]
        model = build_model(entries)
        calls, tr = spy_translator()
        translate_document(model, tr)
        records = toc_records_from_model(model)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["number"], "2.1")
        self.assertEqual(r["title"], "Dataset")
        self.assertEqual(r["translated_title"], "译_Dataset")
        self.assertEqual(r["page_number"], "15")
        self.assertEqual(r["destination_page"], 17)
        self.assertTrue(r["block_id"].startswith("p1_"))


if __name__ == "__main__":
    unittest.main()
