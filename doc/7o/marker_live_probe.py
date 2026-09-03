"""Live marker 探针 — 场景 5（forced marker）真机验证（需 pdf2zh-setup-marker 已建 venv）。

与 ingestion_corpus_probe / random_corpus_probe 同标准：生产入口
run_magicpdf_main（仅 translator 用 echo 桩），--trace 全开，验证
ingest.select 故事链（selected=marker / reason=forced_backend / 无 fallback）
+ audit PASS。venv 未构建时明确报错退出（绝不误报）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingestion_corpus_probe import EchoTranslator, make_ns  # noqa: E402

from pdf2zh.v3.flight_recorder import read_events  # noqa: E402

PDF = "tests/file/translate.cli.plain.text.pdf"


def main() -> int:
    from pdf2zh.kernel import marker_env
    from pdf2zh.magicpdf_cli import run_magicpdf_main

    python = marker_env.marker_python_override()
    if not python:
        print(
            "no isolated marker venv — run `pdf2zh-setup-marker` first",
            file=sys.stderr,
        )
        return 2
    print(f"marker interpreter: {python}")

    from unittest.mock import patch

    with tempfile.TemporaryDirectory(prefix="marker_live_") as tmp:
        trace_jsonl = Path(tmp) / "trace" / "translate.cli.plain.text_events.jsonl"
        ns = make_ns(PDF, tmp, "marker")
        with patch(
            "pdf2zh.translator.build_translator", return_value=EchoTranslator()
        ):
            code = run_magicpdf_main(ns)

        problems: list[str] = []
        if not trace_jsonl.exists():
            print(f"rc={code}, no trace file at {trace_jsonl}")
            return 1
        events = list(read_events(str(trace_jsonl)))
        names = [e["event"] for e in events]

        def check(cond, msg):
            if not cond:
                problems.append(msg)

        check(names and names[0] == "run.begin" and names[-1] == "run.end",
              "run begin/end")
        check("ingest.raw.begin" in names, "raw begin")
        sel = [e for e in events if e["event"] == "ingest.select"]
        check(len(sel) == 1, f"exactly one ingest.select (got {len(sel)})")
        if sel:
            d = sel[0]["payload"]["decision"]
            print("select:", json.dumps(d, ensure_ascii=False)[:400])
            check(d["selected_backend"] == "marker",
                  f"selected={d['selected_backend']}")
            check(d["reason"] == "forced_backend", f"reason={d['reason']}")
            check(d["fallback_attempted"] is False, "no fallback attempted")
            # 摄入门（canonical invariants）必须全绿——渲染层 MEDIUM 证据
            # （echo 桩译文触发 CLIP_READABILITY 等）不进门，见契约 §2.3。
            check(d["quality"] == "PASS", f"ingest gate quality={d['quality']}")
            check(d["failed_rules"] == [],
                  f"ingest gate failed_rules={d['failed_rules']}")
        check(any(n.startswith("plan.") for n in names), "plan events present")

        summary = Path(tmp) / "audit" / "summary.json"
        check(summary.exists(), "audit summary.json written")
        if summary.exists():
            s = json.loads(summary.read_text(encoding="utf-8"))
            sev = s.get("by_severity", {})
            print("audit:", {k: s.get(k) for k in
                             ("qualification", "by_severity", "by_rule",
                              "total_events")})
            # 渲染层允许 MEDIUM（echo 桩真机常态），但绝不许 HIGH（FAIL）。
            check(s.get("qualification") in ("PASS", "PASS_WITH_MEDIUM"),
                  f"qualification={s.get('qualification')}")
            check(sev.get("HIGH", 0) == 0, f"HIGH severity fails: {sev}")

        blocks = sum(1 for n in names if n == "ingest.block")
        print(f"rc={code} events={len(events)} blocks={blocks}")
        print("VERDICT:", "OK" if not problems else "FAILED: " + "; ".join(problems))
        return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
