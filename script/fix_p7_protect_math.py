"""Fix 7: Add _protect_math_fonts and change fallback=True to fallback=False"""
with open('pdf2zh/high_level.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_subset = '''    if not skip_subset_fonts:
        try:
            doc_zh.subset_fonts(fallback=True)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
        try:
            doc_en.subset_fonts(fallback=True)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])'''

new_subset = '''    def _protect_math_fonts(doc):
        """保护已知数学字体不被 MuPDF subset_fonts 子集化破坏宽度"""
        try:
            xreflen = doc.xref_length()
            for xref in range(1, xreflen):
                try:
                    subtype_res = doc.xref_get_key(xref, "/Subtype")
                    if subtype_res[0] == "name" and "Type3" in str(subtype_res[1]):
                        # Type3 字体跳过子集化
                        doc.xref_set_key(xref, "/Length", doc.xref_get_key(xref, "/Length")[1])
                except Exception:
                    pass
                try:
                    basefont_res = doc.xref_get_key(xref, "/BaseFont")
                    if basefont_res[0] == "name":
                        bf = str(basefont_res[1])
                        math_patterns = [
                            "CM", "CMSY", "CMEX", "CMMI", "EUFM", "MSBM", "MSAM",
                            "STIX", "XITS", "MnSymbol", "rsfs", "txsy", "wasy", "stmary",
                            "Symbol", "MT", "BL", "RM", "EU", "LA", "RS"
                        ]
                        for mp in math_patterns:
                            if mp in bf:
                                doc.xref_set_key(xref, "/Length", doc.xref_get_key(xref, "/Length")[1])
                                break
                except Exception:
                    pass
        except Exception:
            pass

    if not skip_subset_fonts:
        # 在子集化前保护数学字体
        _protect_math_fonts(doc_zh)
        _protect_math_fonts(doc_en)
        try:
            doc_zh.subset_fonts(fallback=False)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
        try:
            doc_en.subset_fonts(fallback=False)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])'''

assert old_subset in content, 'Fix 7: subset block not found'
content = content.replace(old_subset, new_subset, 1)
with open('pdf2zh/high_level.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fix 7 applied")
