"""Fix 4: Protect against zero-width math fonts after subsetting"""
with open('pdf2zh/converter.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''                    elif fcur_ == self.noto_name:
                        adv = self.noto.char_lengths(ch, size)[0]
                    else:
                        font_obj = self.fontmap.get(fcur_); adv = font_obj.char_width(ord(ch)) if font_obj else 0 * size'''

new = '''                    elif fcur_ == self.noto_name:
                        adv = self.noto.char_lengths(ch, size)[0]
                    else:
                        font_obj = self.fontmap.get(fcur_)
                        if font_obj:
                            adv = font_obj.char_width(ord(ch))
                            if adv <= 0 and not self.skip_subset_fonts:
                                adv = size * 0.5
                        else:
                            adv = size * 0.5'''

assert old in content, 'Fix 4: not found'
content = content.replace(old, new, 1)
with open('pdf2zh/converter.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fix 4 applied")
