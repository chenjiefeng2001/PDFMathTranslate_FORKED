"""Fix 2: Add skip_subset_fonts to TranslateConverter.__init__ in converter.py"""
with open('pdf2zh/converter.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''    ) -> None:
        super().__init__(rsrcmgr)
        self.vfont = vfont
        self.vchar = vchar
        self.thread = thread
        self.layout = layout
        self.noto_name = noto_name
        self.noto = noto'''

new = '''        skip_subset_fonts: bool = False,
    ) -> None:
        super().__init__(rsrcmgr)
        self.vfont = vfont
        self.vchar = vchar
        self.thread = thread
        self.layout = layout
        self.noto_name = noto_name
        self.noto = noto
        self.skip_subset_fonts = skip_subset_fonts'''

assert old in content, 'Fix 2 text not found!'
content = content.replace(old, new, 1)
with open('pdf2zh/converter.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fix 2 applied")
