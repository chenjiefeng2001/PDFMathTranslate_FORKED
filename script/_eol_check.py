import io
import os

tmp = os.environ["TEMP"]
raw = io.open(os.path.join(tmp, "bp_run.log"), "rb").read()
s = raw.decode("utf-8", errors="replace")
out = []
for l in s.splitlines():
    clean = l.replace("\x00", "").replace("\u001b", "")
    if any(k in clean for k in ("POST ", "FINAL", "FILES")):
        out.append(clean[:170])
io.open(os.path.join(tmp, "bp_filtered.txt"), "w", encoding="utf-8").write(
    "\n".join(out[-16:])
)
print("filtered lines:", len(out))
