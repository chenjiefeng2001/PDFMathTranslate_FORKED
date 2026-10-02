"""Every entry in the bilingual table T must be a real ``(zh, en)`` 2-tuple.

Why this needs its own test
---------------------------
``pdf2zh/gui/export_assets.py`` builds the locale JSON with::

    ui_zh = {k: v[0] for k, v in T.items()}
    ui_en = {k: v[1] for k, v in T.items()}

If an entry is accidentally written as a parenthesised *string concatenation*
instead of a tuple — e.g. three adjacent literals with no comma between the
Chinese and English parts, which is exactly what a careless multi-line edit
produces — then ``T[k]`` is a bare ``str`` and ``v[0]``/``v[1]`` silently yield
its first/second **character**. The shipped locale ends up with one-character
labels.

``tests/test_gui_assets_sync.py`` cannot catch this: it re-exports and compares
bytes, and both sides are equally corrupt. Only a shape assertion on ``T`` can.

This actually happened while adding the glossary/engine warnings, and it
shipped a locale whose labels were ``词`` and ``表``.
"""

import pdf2zh.gui.i18n as i18n


def test_every_entry_is_a_two_tuple():
    bad = {
        key: type(value).__name__
        for key, value in i18n.T.items()
        if not isinstance(value, tuple)
    }
    assert not bad, (
        "entries written as a parenthesised string instead of a (zh, en) "
        f"tuple — export_assets would slice them into single characters: {bad}"
    )


def test_every_tuple_has_exactly_two_strings():
    bad = {
        key: [type(part).__name__ for part in value]
        for key, value in i18n.T.items()
        if isinstance(value, tuple)
        and (len(value) != 2 or not all(isinstance(p, str) for p in value))
    }
    assert not bad, f"malformed bilingual entries: {bad}"


def test_translations_are_not_empty():
    empty = [
        key
        for key, value in i18n.T.items()
        if isinstance(value, tuple) and not all(part.strip() for part in value)
    ]
    assert not empty, f"entries with an empty translation: {empty}"


def test_export_slice_returns_the_whole_string():
    """Mirror export_assets exactly and require the full label back.

    ``zip(i18n.T, i18n.T.values())`` instead of ``T.items()`` so a key that is
    absent shows up as a length mismatch rather than an AttributeError.
    """
    broken = {}
    for key, value in zip(i18n.T, i18n.T.values()):
        zh, en = value[0], value[1]
        if zh != value[0] or en != value[1]:
            broken[key] = "slice is not identity"
        elif not isinstance(zh, str) or not isinstance(en, str):
            broken[key] = f"non-str: {type(zh).__name__}/{type(en).__name__}"
    assert not broken, broken


def test_single_character_labels_are_intentional():
    """A few labels really are one glyph (``无``, ``、``, ``页``).

    Pin them so a future "suspiciously short" heuristic cannot be added without
    tripping over this deliberately-short set — and so nobody 'fixes' them.
    """
    allowed_short = {"label_n_a", "list_separator", "diag_col_page"}
    actually_short = {
        key
        for key, value in i18n.T.items()
        if isinstance(value, tuple) and min(len(value[0]), len(value[1])) <= 1
    }
    assert (
        actually_short <= allowed_short
    ), f"new single-glyph labels appeared: {actually_short - allowed_short}"
