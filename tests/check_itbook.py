"""Quick profile of itbook-export.pdf for benchmark planning."""

import os
import pikepdf

path = (
    r"C:\Users\14977\source\repos\PDFMathTranslate_FORKED\tests\file\itbook-export.pdf"
)
pdf = pikepdf.open(path)

print(f"Pages: {len(pdf.pages)}")
print(f"Size: {os.path.getsize(path) / 1024:.0f} KB")

# First page
page = pdf.pages[0]
print(f"\nFirst page keys: {list(page.keys())}")
mb = page.get("/MediaBox")
if mb:
    w = float(mb[2]) - float(mb[0])
    h = float(mb[3]) - float(mb[1])
    print(f"Page size: {w:.0f} x {h:.0f} pts")

res = page.get("/Resources")
if res:
    print(f"Resources keys: {list(res.keys())}")
    fd = res.get("/Font")
    if fd:
        print(f"Font dict keys: {list(fd.keys())}")

# Count unique fonts
all_fonts = set()
font_names = []
for p in pdf.pages:
    res = p.get("/Resources")
    if res:
        fd = res.get("/Font")
        if fd:
            for key in fd:
                ref = fd[key]
                try:
                    all_fonts.add(ref.objgen)
                    if len(font_names) < 5:
                        font_names.append(f"{key} -> {ref.objgen}")
                except:
                    pass

print(f"\nUnique font objects: {len(all_fonts)}")
for fn in font_names:
    print(f"  {fn}")

# TOC/Outlines
has_outlines = "/Outlines" in pdf.Root
print(f"\nHas outlines/TOC: {has_outlines}")

# Count images
img_count = 0
for objnum in range(1, len(pdf.objects)):
    try:
        obj = pdf.objects.get(objnum)
        if isinstance(obj, pikepdf.Stream):
            if obj.get("/Subtype") == "/Image":
                img_count += 1
    except:
        pass

print(f"Image XObjects: ~{img_count}")

# Page size distribution
sizes = {}
for p in pdf.pages[:20]:
    mb = p.get("/MediaBox")
    if mb:
        w = round(float(mb[2]) - float(mb[0]))
        h = round(float(mb[3]) - float(mb[1]))
        key = f"{w}x{h}"
        sizes[key] = sizes.get(key, 0) + 1

print(f"\nPage sizes (first 20): {sizes}")

pdf.close()
