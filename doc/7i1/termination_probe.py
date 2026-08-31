"""7I-1 stage-A probe: empirically test GeometryEngine termination on adversarial
inputs WITHOUT touching pdf2zh/ code.

Hypothesis audit for First Non-Terminating Stage candidates:
  1. build_words:  baseline-row cluster is O(n^2) worst case (scan all prior rows
                   per char), but finite.
  2. reading_order._xy_cut: strict partition each recursion -> terminating.
  3. _detect_columns: nested qualifier scan, finite.

Use a subprocess with a hard timeout so a genuine non-termination surfaces as
a TIMEOUT instead of hanging this process.
"""
import subprocess, sys, textwrap, time


PROBE = r"""
import sys, time
from pdf2zh.v3.geometry import Char, GeometryEngine, GeometryConfig
from pdf2zh.v3.canonical_page import build_page_model

def mkpage(n_chars, n_distinct_baselines, cols=2, seed=0):
    # adversarial: many chars on a few nearly-identical baselines (forces the
    # O(n^2) row-scan and deep _xy_cut), arranged so column detection sees
    # interleaved columns (the recursion-heavy branch).
    chars = []
    rnd = seed
    baseline_pitch = 0.7  # sub-size pitch -> build_words may not de-dup reliably
    x = 0.0
    for i in range(n_chars):
        bl = 800.0 - baseline_pitch * (i % n_distinct_baselines)
        # two columns with big horizontal gap
        col = i % cols
        x0 = col * 400.0 + (rnd % 50)
        rnd = (rnd * 1103515245 + 12345) & 0x7fffffff
        chars.append(Char(
            text=chr(65 + (rnd % 26)),
            x0=x0, y0=bl, x1=x0+3.0, y1=bl+4.0,
            size=5.0, font="F", page_num=0,
        ))
    return chars

def trial(case, args):
    t0 = time.time()
    chars = mkpage(*args)
    eng = GeometryEngine(GeometryConfig())
    g = eng.build_page(chars, page_num=0)
    return ("ok", len(g.paragraphs), round(time.time()-t0, 3))

for case, args in [
    ("words-o2-small",  ( 2000, 40, 1)),
    ("words-o2-med",    ( 5000, 60, 2)),
    ("words-o2-large",  (10000, 80, 2)),
    ("cols-deep",       ( 4000, 25, 3)),
]:
    r = trial(case, args)
    print(case, "->", r, flush=True)
print("PROBE_DONE")
"""
out = subprocess.run(
    [sys.executable, "-c", PROBE],
    capture_output=True, text=True, timeout=240,
)
print(out.stdout)
print(out.stderr[-4000:] if out.stderr else "")
print("exit", out.returncode)