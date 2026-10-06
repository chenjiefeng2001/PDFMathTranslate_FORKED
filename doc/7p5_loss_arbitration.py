"""裁决矛盾：日志说"丢了行"，但审计说"一字未少"。

``render_plan`` 里 ``render_payload.lines`` 与 ``translated`` 完全一致（0 丢字），
可日志有两条 ``line(s) dropped``（1 行 / 5 行）。两者必有一方在说谎。

第三方真相是**渲染后的 PDF**：把译文片段拿到输出 mono PDF 的页上去搜。搜得到就是
日志误报（多半是 shrink 之前的中间尝试）；搜不到就是真丢字。

跑法::

    python doc/7p5_loss_arbitration.py doc/7p5-audit50
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

NONSPACE = re.compile(r"\S")


def norm(s: str) -> str:
    return "".join(NONSPACE.findall(s))


def main(out_dir: str) -> int:
    d = Path(out_dir)
    plan_f = next(d.glob("output/**/*_render_plan.json"))
    pdf_f = next(d.glob("output/**/*_mono.pdf"))
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    log = (d / "run.log").read_text(encoding="utf-8", errors="replace")

    print(f"plan : {plan_f.name}")
    print(f"pdf  : {pdf_f.name}")

    warnings = re.findall(
        r"wrapped block needs (\d+) line\(s\) but only (\d+) fit in ([\d.]+)pt; "
        r"(\d+) line\(s\) dropped: '([^']*)'",
        log,
    )
    print(f"\ndropped-line warnings: {len(warnings)}")

    # 把每条告警的文本片段在 render_plan 里定位到具体块/页
    targets = []
    for need, fit, box, dropped, snippet in warnings:
        key = norm(snippet)[:20]
        hit = None
        for b in plan:
            if norm(str(b.get("translated") or "")).startswith(key[:12]):
                hit = b
                break
        targets.append((int(need), int(fit), float(box), int(dropped), snippet, hit))
        print(
            f"\n  warn: needs={need} fit={fit} box={box}pt dropped={dropped}"
            f"  snippet={snippet[:46]!r}"
        )
        if hit:
            print(f"    -> plan block {hit['block_id']} on page {hit['page']}")
        else:
            print("    -> NOT FOUND in render plan (already logged as a gap)")

    # 渲染后 PDF 的每页文本
    doc = pymupdf.open(str(pdf_f))
    page_text = {i: norm(doc[i].get_text()) for i in range(doc.page_count)}
    print(f"\noutput PDF pages: {doc.page_count}")

    print("\n" + "=" * 78)
    print("ARBITRATION: is the text actually in the rendered PDF?")
    print("=" * 78)
    for need, fit, box, dropped, snippet, hit in targets:
        if not hit:
            print(f"  {snippet[:40]!r}: block not in plan -> cannot arbitrate")
            continue
        pno = int(hit["page"])
        want = norm(str(hit.get("translated") or ""))
        rendered = page_text.get(pno, "")
        # 整块在不在
        whole = want in rendered
        # 告警里点名的首行在不在
        head = norm(snippet)[:12]
        head_ok = head in rendered
        # 落笔行数
        lines = (hit.get("render_payload") or {}).get("lines") or []
        print(f"\n  block {hit['block_id']} (page {pno})")
        print(f"    dropped-line warning claimed {dropped} line(s) lost")
        print(f"    payload.lines rendered      : {len(lines)} line(s)")
        print(f"    full translation in PDF     : {whole}")
        print(f"    warned snippet in PDF       : {head_ok}")
        print(f"    translated chars            : {len(want)}")
        verdict = "REAL LOSS" if not whole else "FALSE ALARM (text is in the PDF)"
        print(f"    -> {verdict}")

    # 顺带全量核对：每个译文的最后 8 个非空白字符是否出现在对应页
    missing = []
    for b in plan:
        want = norm(str(b.get("translated") or ""))
        if len(want) < 8:
            continue
        tail = want[-8:]
        if tail not in page_text.get(int(b["page"]), ""):
            missing.append((int(b["page"]), b["block_id"], len(want), tail))
    print("\n" + "=" * 78)
    print(f"blocks whose translation TAIL is absent from its page: {len(missing)}")
    print("=" * 78)
    for pno, bid, n, tail in missing[:15]:
        print(f"  page {pno:>3} {bid:<10} ({n} chars) tail={tail!r}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "doc/7p5-audit50"))
