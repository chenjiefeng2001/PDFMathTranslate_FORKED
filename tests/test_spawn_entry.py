"""Entry-point spawn-child protections (Windows multiprocessing).

Guards the CLI/GUI entry so a multiprocessing ``spawn`` child, which re-executes
the entry script as ``__main__`` with ``--multiprocessing-fork`` (and the
``-I`` flag by some frozen launchers) in its argv, never re-runs argparse with
those bootstrap flags -- the historical cause of
``error: unrecognized arguments: -I --multiprocessing-fork`` and the
consequent immediate ``BrokenProcessPool`` of every ``ProcessPoolExecutor``
worker (see doc/event_driven_frontend_report.md / code_bug_fix_report.md).
"""


class TestSpawnChildDetection:
    def test_recognizes_spawn_marker_anywhere_in_argv(self):
        from pdf2zh.pdf2zh import is_spawn_child

        assert (
            is_spawn_child(["pdf2zh.int", "-I", "--multiprocessing-fork", "fd=1"])
            is True
        )
        assert (
            is_spawn_child(["python", "-I", "pdf2zh.int", "--multiprocessing-fork"])
            is True
        )
        assert is_spawn_child(["-c", "--multiprocessing-fork", "fd=8"]) is True

    def test_regular_cli_invocations_are_not_children(self):
        from pdf2zh.pdf2zh import is_spawn_child

        assert is_spawn_child([]) is False
        assert is_spawn_child(["pdf2zh.int", "-i", "doc.pdf"]) is False
        assert is_spawn_child(["pdf2zh", "gui"]) is False

    def test_main_returns_zero_without_parsing_for_child(self):
        from pdf2zh.pdf2zh import main

        # Exactly the argv shape from the production log; must not raise
        # SystemExit/SyntaxError from argparse and must not run the app.
        assert main(["pdf2zh.int", "-I", "--multiprocessing-fork"]) == 0

    def test_spawn_child_yields_to_invokes_freeze_support(self, monkeypatch):
        import multiprocessing

        from pdf2zh.pdf2zh import spawn_child_yields_to

        calls = []

        def fake_freezer():
            calls.append(1)
            raise RuntimeError("stop here")

        monkeypatch.setattr(multiprocessing, "freeze_support", fake_freezer)
        assert spawn_child_yields_to(["pdf2zh.int", "x.pdf"]) is False
        assert calls == []
        assert (
            spawn_child_yields_to(["pdf2zh.int", "-I", "--multiprocessing-fork"])
            is True
        )
        assert calls == [1]

    def test_wrapper_script_guards_spawn_reexec(self):
        with open("script/build/pdf2zh.int", encoding="utf-8") as fh:
            text = fh.read()
        assert "__name__" in text
        assert "spawn_child_yields_to" in text

    def test_pystand_template_keeps_guard(self):
        # PyStand regenerates script/build/pdf2zh.int from _pystand_static.int
        # on every exe launch; the guard must live in the template or the
        # packaged app silently loses the spawn fix (regression seen twice).
        with open("script/_pystand_static.int", encoding="utf-8") as fh:
            text = fh.read()
        assert "spawn_child_yields_to" in text
        assert "SystemExit" in text
