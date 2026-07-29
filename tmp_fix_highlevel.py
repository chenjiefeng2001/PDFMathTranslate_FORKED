with open('pdf2zh/high_level.py', encoding='utf-8') as f:
    content = f.read()

old = '''if parallel_pages and page_count > 5:
        obj_patch = _translate_parallel(
            fp, dict(locals()),
            workers=parallel_workers,
        )
    else:
        obj_patch = translate_patch(fp, **dict(locals()))'''

new = '''if parallel_pages and page_count > 5:
        try:
            obj_patch = _translate_parallel(
                fp, dict(locals()),
                workers=parallel_workers,
            )
        except Exception as parallel_err:
            logger.warning(
                "Parallel page processing failed ({}), falling back to serial: {}".format(
                    type(parallel_err).__name__, str(parallel_err)[:120],
                ),
            )
            obj_patch = translate_patch(fp, **dict(locals()))
    else:
        obj_patch = translate_patch(fp, **dict(locals()))'''

assert old in content, 'Old text not found!'
content = content.replace(old, new, 1)

# Also fix: wrap subset_fonts in try/except to handle MuPDF xref errors
old2 = '''if not skip_subset_fonts:
        doc_zh.subset_fonts(fallback=True)
        doc_en.subset_fonts(fallback=True)'''

new2 = '''if not skip_subset_fonts:
        try:
            doc_zh.subset_fonts(fallback=True)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
        try:
            doc_en.subset_fonts(fallback=True)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])'''

assert old2 in content, 'Old2 text not found!'
content = content.replace(old2, new2, 1)

with open('pdf2zh/high_level.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('SUCCESS: high_level.py patched')
