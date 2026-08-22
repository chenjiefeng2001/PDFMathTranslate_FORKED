"""专业词表库（pdf2zh.glossary_store）单元测试。

覆盖：CSV 解析校验（缺列/空行/坏行定位）、目标语过滤、库导入导出往返、
名称安全化、babeldoc Glossary 装载与语言过滤、CLI 管理入口。

词表库目录通过 monkeypatch ``store_dir`` 指到 tmp_path，绝不触碰真实
``~/.config``。
"""
from __future__ import annotations

import csv
import json

import pytest

from pdf2zh import glossary_store as gs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    d = tmp_path / "glossaries"
    monkeypatch.setattr(gs, "store_dir", lambda: d)
    d.mkdir()
    return d


def _write_csv(path, rows, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding=encoding) as fh:
        writer = csv.writer(fh)
        writer.writerow(["source", "target", "tgt_lng"])
        writer.writerows(rows)
    return path


class TestParseCsv:
    def test_valid_rows(self, tmp_path):
        p = _write_csv(tmp_path / "g.csv", [
            ["kernel", "内核", ""],
            ["deadlock detection", "死锁检测", "zh-CN"],
        ])
        entries = gs.parse_csv(p)
        assert len(entries) == 2
        assert entries[0] == {"source": "kernel", "target": "内核",
                              "tgt_lng": ""}

    def test_missing_required_column(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("src,tgt\na,b\n", encoding="utf-8")
        with pytest.raises(gs.GlossaryError, match="缺少必需列"):
            gs.parse_csv(p)

    def test_blank_row_skipped_and_empty_target_rejected(self, tmp_path):
        p = tmp_path / "mixed.csv"
        p.write_text(
            "source,target\n\nkernel,\n", encoding="utf-8"
        )
        with pytest.raises(gs.GlossaryError, match="第 3 行"):
            gs.parse_csv(p)

    def test_not_found(self, tmp_path):
        with pytest.raises(gs.GlossaryError, match="不存在"):
            gs.parse_csv(tmp_path / "nope.csv")

    def test_gb18030_decoded(self, tmp_path):
        p = _write_csv(tmp_path / "gb.csv",
                       [["thread", "线程", ""]], encoding="gb18030")
        assert gs.parse_csv(p)[0]["target"] == "线程"


class TestFilterEntries:
    def test_language_normalization(self):
        entries = [
            {"source": "a", "target": "甲", "tgt_lng": "zh-CN"},
            {"source": "b", "target": "乙", "tgt_lng": "zh_tw"},
            {"source": "c", "target": "丙", "tgt_lng": ""},
        ]
        out = gs.filter_entries_for(entries, "zh-TW")
        assert [e["source"] for e in out] == ["b", "c"]


class TestStoreOps:
    def test_import_export_round_trip(self, store, tmp_path):
        src = _write_csv(tmp_path / "my terms.csv",
                         [["cache", "缓存", ""]])
        dest = gs.import_to_store(src)
        assert dest == store / "my_terms.csv"
        exported = gs.export_from_store("my_terms", tmp_path / "out.csv")
        data = exported.read_text(encoding="utf-8")
        assert data.startswith("\ufeff")  # BOM：Excel 直开不乱码
        rows = list(csv.DictReader(data.lstrip("\ufeff").splitlines()))
        assert rows[0]["source"] == "cache"

    def test_import_rejects_bad_csv(self, store, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("x,y\n1,2\n", encoding="utf-8")
        with pytest.raises(gs.GlossaryError):
            gs.import_to_store(bad)

    def test_export_missing(self, store, tmp_path):
        with pytest.raises(gs.GlossaryError, match="不存在"):
            gs.export_from_store("ghost", tmp_path / "o.csv")

    def test_list_store_counts_and_errors(self, store, tmp_path):
        good = _write_csv(store / "good.csv", [["a", "甲", ""]])
        del good
        (store / "broken.csv").write_text("no_header\n", encoding="utf-8")
        items = {i["name"]: i for i in gs.list_store()}
        assert items["good"]["entries"] == 1
        assert items["broken"]["entries"] is None
        assert "error" in items["broken"]

    def test_resolve_store_names_traversal_safe(self, store):
        (store / "ok.csv").write_text(
            "source,target\nk,K\n", encoding="utf-8")
        assert gs.resolve_store_names(["ok"]) == [str(store / "ok.csv")]
        # 不存在 → 报错而非拼出穿越路径（safe_name 已剥离 ../）
        with pytest.raises(gs.GlossaryError):
            gs.resolve_store_names(["../../etc/passwd"])

    def test_safe_name(self):
        assert gs.safe_name("../../etc/passwd") != "../../etc/passwd"
        assert "/" not in gs.safe_name("a/b\\c")
        assert gs.safe_name("  ") == "glossary"


class TestLoadBabeldocGlossaries:
    def test_empty_input(self):
        assert gs.load_babeldoc_glossaries([], "zh-CN") == []
        assert gs.load_babeldoc_glossaries(None, "zh-CN") == []

    def test_load_filters_by_target_lang(self, tmp_path):
        p = _write_csv(tmp_path / "g.csv", [
            ["kernel", "内核", "zh-CN"],
            ["Kernel boot", "核心引导", "en"],
            ["cache", "缓存", ""],  # 无语言标注 → 全语种生效
        ])
        gloss = gs.load_babeldoc_glossaries([str(p)], "zh-CN")[0]
        lookup_keys = set(gloss.normalized_lookup.keys())
        assert "kernel" in lookup_keys
        assert "kernel boot" not in lookup_keys
        assert "cache" in lookup_keys

    def test_precheck_fails_fast(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("wrong,header\n", encoding="utf-8")
        with pytest.raises(gs.GlossaryError):
            gs.load_babeldoc_glossaries([str(bad)], "zh-CN")


class TestCli:
    def test_import_list_export(self, store, tmp_path, capsys):
        src = _write_csv(tmp_path / "cli.csv", [["stack", "栈", ""]])
        assert gs.main(["import", str(src)]) == 0
        imported = json.loads(capsys.readouterr().out)
        assert imported == [str(store / "cli.csv")]
        assert gs.main(["list"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed[0]["name"] == "cli"
        assert gs.main(["export", "cli", str(tmp_path / "e.csv")]) == 0
        capsys.readouterr()
        assert (tmp_path / "e.csv").exists()
