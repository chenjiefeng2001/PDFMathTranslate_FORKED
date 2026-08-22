"""Post-install patch: Gradio route_utils.py long path support on Windows.

Replaces move_uploaded_files_to_cache() with a version that handles
Windows MAX_PATH limit (>260 chars) by truncating long filenames
and using the \\\\?\\ prefix.

Usage: python patch_gradio_longpath.py <path_to_route_utils.py>
"""
import sys
import pathlib


def patch_file(path: str) -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"ERROR: {p} not found")
        return False

    # Read current content
    content = p.read_text(encoding="utf-8")

    # Define the new function
    new_function = '''def move_uploaded_files_to_cache(files, destinations):
    for file, dest in zip(files, destinations, strict=False):
        try:
            shutil.move(file, dest)
        except (OSError, FileNotFoundError) as _path_err:
            _log = logging.getLogger("gradio.route_utils")
            _log.warning("File move failed (possibly long path), trying fallback: %s", _path_err)
            try:
                _dest_dir = os.path.dirname(dest)
                if not os.path.exists(_dest_dir):
                    os.makedirs(_dest_dir, exist_ok=True)
                if len(dest) > 240:
                    _name, _ext = os.path.splitext(os.path.basename(dest))
                    _short_name = _name[:50] + _ext
                    dest = os.path.join(_dest_dir, _short_name)
                if len(dest) > 240:
                    dest = "\\\\\\\\?\\\\" + os.path.abspath(dest)
                shutil.move(file, dest)
            except Exception as _inner_err:
                _log.error("File move fallback also failed: %%s, src=%%s, dst=%%s", _inner_err, file, dest)
'''

    # Try different variants of the original function signature
    variants = [
        f"def move_uploaded_files_to_cache(files: list[str], destinations: list[str]) -> None:\n    for file, dest in zip(files, destinations, strict=False):\n        shutil.move(file, dest)",
        f"def move_uploaded_files_to_cache(files: list[str], destinations: list[str]) -> None:\n    for file, dest in zip(files, destinations, strict=True):\n        shutil.move(file, dest)",
        f"def move_uploaded_files_to_cache(files: list[str], destinations: list[str]):\n    for file, dest in zip(files, destinations, strict=False):\n        shutil.move(file, dest)",
        f"def move_uploaded_files_to_cache(files, destinations):\n    for file, dest in zip(files, destinations, strict=False):\n        shutil.move(file, dest)",
        f"def move_uploaded_files_to_cache(files, destinations):\n    for file, dest in zip(files, destinations, strict=True):\n        shutil.move(file, dest)",
    ]

    for variant in variants:
        if variant in content:
            content = content.replace(variant, new_function, 1)
            p.write_text(content, encoding="utf-8")
            print(f"Patched {p.name} (matched variant {variants.index(variant) + 1})")
            return True

    # Fallback: try to find shutil.move line and add try/except around it
    if "shutil.move(file, dest)" in content and "move_uploaded_files_to_cache" in content:
        print("WARNING: Could not match function signature exactly. Trying regex replacement...")
        import re
        # Match the function body indentation and wrap the shutil.move line
        pattern = r'(    for file, dest in zip\(files, destinations[^)]+\):\n[ \t]+)shutil\.move\(file, dest\)'
        replacement = (
            r'\1try:\n'
            r'\1    shutil.move(file, dest)\n'
            r'\1except (OSError, FileNotFoundError) as _path_err:\n'
            r'\1    _log = logging.getLogger("gradio.route_utils")\n'
            r'\1    _log.warning("File move failed (possibly long path), trying fallback: %s", _path_err)\n'
            r'\1    try:\n'
            r'\1        _dest_dir = os.path.dirname(dest)\n'
            r'\1        if not os.path.exists(_dest_dir):\n'
            r'\1            os.makedirs(_dest_dir, exist_ok=True)\n'
            r'\1        if len(dest) > 240:\n'
            r'\1            _name, _ext = os.path.splitext(os.path.basename(dest))\n'
            r'\1            _short_name = _name[:50] + _ext\n'
            r'\1            dest = os.path.join(_dest_dir, _short_name)\n'
            r'\1        if len(dest) > 240:\n'
            r'\1            dest = "\\\\\\\\?\\\\" + os.path.abspath(dest)\n'
            r'\1        shutil.move(file, dest)\n'
            r'\1    except Exception as _inner_err:\n'
            r'\1        _log.error("File move fallback also failed: %%s, src=%%s, dst=%%s", _inner_err, file, dest)'
        )
        content, count = re.subn(pattern, replacement, content, count=1)
        if count > 0:
            p.write_text(content, encoding="utf-8")
            print(f"Patched {p.name} (regex fallback)")
            return True
        else:
            print("ERROR: Regex replacement also failed")
            return False

    print("ERROR: Could not find move_uploaded_files_to_cache function")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python patch_gradio_longpath.py <path_to_route_utils.py>")
        sys.exit(1)
    success = patch_file(sys.argv[1])
    sys.exit(0 if success else 1)
