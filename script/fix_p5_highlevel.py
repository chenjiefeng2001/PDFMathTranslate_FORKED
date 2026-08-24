"""Fix 5+6: Add skip_subset_fonts to translate_patch and pass to TranslateConverter"""

with open("pdf2zh/high_level.py", "r", encoding="utf-8") as f:
    content = f.read()

# Fix 5: Add to signature
old_sig = """    ignore_cache: bool = False,
    # 2.0 additions
    text_metrics: dict = None,
    font_resolver: object = None,
    layout_graph: object = None,
    collision_resolver: object = None,
    translation_cache: object = None,
    **kwarg: Any,
) -> None:"""

new_sig = """    ignore_cache: bool = False,
    skip_subset_fonts: bool = False,
    # 2.0 additions
    text_metrics: dict = None,
    font_resolver: object = None,
    layout_graph: object = None,
    collision_resolver: object = None,
    translation_cache: object = None,
    **kwarg: Any,
) -> None:"""

assert old_sig in content, "Fix 5: sig not found"
content = content.replace(old_sig, new_sig, 1)

# Fix 6: Pass to converter
old_pass = """        ignore_cache,
        text_metrics=text_metrics,
        font_resolver=font_resolver,
        layout_graph=layout_graph,
        collision_resolver=collision_resolver,
        translation_cache=translation_cache,"""

new_pass = """        ignore_cache,
        skip_subset_fonts=skip_subset_fonts,
        text_metrics=text_metrics,
        font_resolver=font_resolver,
        layout_graph=layout_graph,
        collision_resolver=collision_resolver,
        translation_cache=translation_cache,"""

assert old_pass in content, "Fix 6: pass not found"
content = content.replace(old_pass, new_pass, 1)

with open("pdf2zh/high_level.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Fix 5+6 applied")
