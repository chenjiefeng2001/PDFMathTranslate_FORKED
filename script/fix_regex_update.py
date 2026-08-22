"""Fix regex: add MSAM and generalize STIX"""
with open('pdf2zh/converter.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add MSAM next to MSBM
old1 = 'r"EUFM|MSBM|CMSY|CMEX|CMMI|S[0-9]|"'
new1 = 'r"EUFM|MSBM|MSAM|CMSY|CMEX|CMMI|S[0-9]|"'
assert old1 in content, 'Fix regex: MSBM not found'
content = content.replace(old1, new1)

# Generalize STIX pattern to also match STIXGeneral etc.
old2 = 'r"STIX.*Math|XITS.*|Cambria\\\\s*Math|Asana\\\\s*Math|LMMath|MnSymbol|"'
new2 = 'r"STIX.*|XITS.*|Cambria\\\\s*Math|Asana\\\\s*Math|LMMath|MnSymbol|"'
if old2 not in content:
    # Try without the double backslash
    old2 = 'r"STIX.*Math|XITS.*|Cambria\\s*Math|Asana\\s*Math|LMMath|MnSymbol|"'
    new2 = 'r"STIX.*|XITS.*|Cambria\\s*Math|Asana\\s*Math|LMMath|MnSymbol|"'
assert old2 in content, f'Fix regex: STIX variant not found\nLooking for:\n{repr(old2)}'
content = content.replace(old2, new2)

with open('pdf2zh/converter.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Regex patterns updated')
