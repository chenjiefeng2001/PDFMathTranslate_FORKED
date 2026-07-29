import sys
with open('pdf2zh/pdfinterp.py', encoding='utf-8') as f:
    content = f.read()

old = '            except Exception:\n                pass\n        elif subtype is LITERAL_IMAGE'
new = '''            except Exception as e:
                log.warning(
                    "XObject {} form processing failed ({}: {}). "
                    "Restoring state and clearing XObject stream to prevent overlay.",
                    xobjid, type(e).__name__, str(e)[:120],
                )
                try:
                    if self.device._stack:
                        self.device.cur_item = self.device._stack.pop()
                except Exception as restore_err:
                    log.debug("Failed to restore cur_item: %s", restore_err)
                xobj_objid = self.xobjmap[xobjid].objid
                if xobj_objid not in self.obj_patch:
                    self.obj_patch[xobj_objid] = ""
        elif subtype is LITERAL_IMAGE'''.replace('{}', '%s')

assert old in content, 'Old text not found!'
content = content.replace(old, new, 1)

with open('pdf2zh/pdfinterp.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('SUCCESS: pdfinterp.py patched')
