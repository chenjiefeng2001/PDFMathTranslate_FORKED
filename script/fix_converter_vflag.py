"""Fix vflag function in converter.py - improve math font regex and font name extraction"""
with open('pdf2zh/converter.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''        def vflag(font: str, char: str):    # 匹配公式（和角标）字体
            if isinstance(font, bytes):     # 不一定能 decode，直接转 str
                try:
                    font = font.decode('utf-8')  # 尝试使用 UTF-8 解码
                except UnicodeDecodeError:
                    font = ""
            font = font.split("+")[-1]      # 字体名截断
            if re.match(r"\\(cid:", char):
                return True
            # 基于字体名规则的判定
            if self.vfont:
                if re.match(self.vfont, font):
                    return True
            else:
                if re.match(                                            # latex 字体
                    r"(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)",
                    font,
                ):
                    return True
            # 基于字符集规则的判定
            if self.vchar:
                if re.match(self.vchar, char):
                    return True
            else:
                if (
                    char
                    and char != " "                                     # 非空格
                    and (
                        unicodedata.category(char[0])
                        in ["Lm", "Mn", "Sk", "Sm", "Zl", "Zp", "Zs"]   # 文字修饰符、数学符号、分隔符号
                        or ord(char[0]) in range(0x370, 0x400)          # 希腊字母
                    )
                ):
                    return True
            return False'''

new = '''        def _extract_font_name(font: str) -> str:
            """从 PDF 字体引用中提取规范字体名（改进版）"""
            if isinstance(font, bytes):
                try:
                    font = font.decode('utf-8')
                except UnicodeDecodeError:
                    return ""
            # 处理 /ABCDEF+CMMI10 格式（取最后一个 + 之后的部分）
            if "+" in font:
                font = font.split("+")[-1]
            return font

        def vflag(font: str, char: str):    # 匹配公式（和角标）字体
            font = _extract_font_name(font)  # 字体名截断（改进版）
            if re.match(r"\\(cid:", char):
                return True
            # 基于字体名规则的判定
            if self.vfont:
                if re.match(self.vfont, font):
                    return True
            else:
                # 扩展默认公式字体正则，覆盖 Springer/Elsevier/AMS/LaTeX 等常见数学字体
                if re.match(
                    r"(CM[^R]|MS[BM]|XY|MT|BL|RM|EU[FM]|LA|RS|LINE|LCIRCLE|"
                    r"TeX-|rsfs|txsy|wasy|stmary|"
                    r".*Mono|.*Code|.*Ital|.*Sym|.*Math|"
                    r"EUFM|MSBM|CMSY|CMEX|CMMI|S[0-9]|"
                    r"STIX.*Math|XITS.*Math|Cambria\s*Math|Asana\s*Math|LMMath|MnSymbol|"
                    r"bb[0-9]?|bbold|cal[0-9]?|frak[0-9]?|mathscr)",
                    font,
                ):
                    return True
            # 基于字符集规则的判定
            if self.vchar:
                if re.match(self.vchar, char):
                    return True
            else:
                if (
                    char
                    and char != " "                                     # 非空格
                    and (
                        unicodedata.category(char[0])
                        in ["Lm", "Mn", "Sk", "Sm", "Zl", "Zp", "Zs"]   # 文字修饰符、数学符号、分隔符号
                        or ord(char[0]) in range(0x370, 0x400)          # 希腊字母
                    )
                ):
                    return True
            return False'''

if old not in content:
    print("ERROR: old vflag text not found!")
    # Debug: find the vflag function
    idx = content.find('def vflag')
    if idx >= 0:
        print(f"Found vflag at position {idx}")
        print(repr(content[idx:idx+800]))
    else:
        print("vflag def not found at all!")
else:
    content = content.replace(old, new, 1)
    with open('pdf2zh/converter.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: vflag function updated")
