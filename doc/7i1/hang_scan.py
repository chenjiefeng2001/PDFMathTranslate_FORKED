"""7I-1-B: locate the First Non-Terminating Stage of build_document_model on the
multiprocessor book. CHILD scans pages flushing per-page lines; a reader THREAD
drains stdout and stamps the last-line time; the MAIN loop watches idle time and
kills the trailing child on the first hang (tolerant of a slow-but-progressing page)."""

import subprocess
import sys
import threading
import time

BOOK = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"
GUARD = (
    float(sys.argv[sys.argv.index("--guard") + 1]) if "--guard" in sys.argv else 45.0
)

WORKER = r"""
import logging, sys, time
from pdfminer.high_level import extract_pages
from pdf2zh.v3.document_model import build_document_model
lt = list(extract_pages(r"{book}"))
print("PAGES_TOTAL %d" % len(lt), flush=True)
for i in range(len(lt)):
    t0 = time.time()
    m = build_document_model([lt[i]])
    dt = time.time() - t0
    nb = len(getattr(m.pages[0],'blocks',[]) or []) if m.pages else 0
    print("PAGE %d dt=%.2f blocks=%d" % (i, dt, nb), flush=True)
print("CHILD_DONE", flush=True)
""".format(book=BOOK)


def main():
    proc = subprocess.Popen(
        [sys.executable, "-c", WORKER],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    state = {"last_line_at": time.time(), "last_page": -1, "done": False}

    def reader():
        for line in proc.stdout:
            state["last_line_at"] = time.time()
            line = line.rstrip("\n")
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if line.startswith("PAGE "):
                try:
                    state["last_page"] = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            if "CHILD_DONE" in line:
                break
        state["done"] = True

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    while not state["done"]:
        th.join(2.0)
        idle = time.time() - state["last_line_at"]
        if idle > GUARD:
            print(
                f"HANG-SUSPECT last_page={state['last_page']} idle>{GUARD:.0f}s -> kill "
                f"child, first-hang-page={state['last_page'] + 1}"
            )
            proc.kill()
            proc.wait()
            return
    rc = proc.wait()
    print(f"SCAN_COMPLETE last_page={state['last_page']} rc={rc}")
    print("HANGSCAN_DONE")


if __name__ == "__main__":
    main()
