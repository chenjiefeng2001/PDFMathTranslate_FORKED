"""Fix 3: Add CJK-western mixed line height support in converter.py"""
with open('pdf2zh/converter.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''            # === 2.0: TextMetrics line height (M1) ===
            line_height = default_line_height
            tm_line = self.text_metrics.get(fcur) if self.text_metrics else None
            if tm_line:
                ascent = getattr(tm_line, 'ascent', 0.8)
                descent = getattr(tm_line, 'descent', -0.2)
                line_height = max(ascent - descent, 1.0) if np.isfinite(ascent) and np.isfinite(descent) else default_line_height'''

new = '''            # === 2.0: TextMetrics line height (M1) ===
            line_height = default_line_height
            # CJK/western mixed line height
            if any("\u4e00" <= c <= "\u9fff" or "\u3000" <= c <= "\u303f" for c in new):
                line_height = max(default_line_height, 1.3)
            tm_line = self.text_metrics.get(fcur) if self.text_metrics else None
            if tm_line:
                ascent = getattr(tm_line, 'ascent', 0.8)
                descent = getattr(tm_line, 'descent', -0.2)
                line_height = max(ascent - descent, 1.0) if np.isfinite(ascent) and np.isfinite(descent) else default_line_height'''

assert old in content, 'Fix 3 text not found!'
content = content.replace(old, new, 1)
with open('pdf2zh/converter.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("Fix 3 applied")
