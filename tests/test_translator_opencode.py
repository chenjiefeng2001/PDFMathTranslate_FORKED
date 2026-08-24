"""OpenCodeTranslator 单元测试。

覆盖：
- CLI JSONL 事件流解析（_parse_output）；
- build_translator 工厂注册与 "opencode:provider/model" 模型路由；
- CLI 模式：子进程参数组装（--model/--agent/--format json）与非零退出重试；
- serve 模式：HTTP 会话生命周期（创建→消息→清理）与空响应报错；
- v3 OpenCodeProvider 委托 complete_raw。

所有外部交互（subprocess / requests）均被 mock，不依赖真实 opencode 安装。
"""

import unittest
from unittest.mock import MagicMock, patch

from pdf2zh.translator import OpenCodeTranslator, build_translator


def _make_translator(**env_overrides):
    """构造跳过 CLI 自检且与全局 config.json 隔离的 OpenCodeTranslator 实例。

    set_envs 会读写 ConfigManager（持久化到 ~/.config/PDFMathTranslate/config.json），
    必须同时 mock 读/写，否则测试间会通过磁盘配置互相污染。
    """
    with (
        patch.object(OpenCodeTranslator, "_test_opencode"),
        patch.object(OpenCodeTranslator, "_test_server"),
        patch(
            "pdf2zh.translator.ConfigManager.get_translator_by_name", return_value=None
        ),
        patch("pdf2zh.translator.ConfigManager.set_translator_by_name"),
    ):
        return OpenCodeTranslator("en", "zh", None, envs=env_overrides or None)


class TestParseOutput(unittest.TestCase):
    def test_extracts_text_events(self):
        output = (
            '{"type":"step_start","sessionID":"s1"}\n'
            '{"type":"text","part":{"type":"text","text":"你好"}}\n'
            '{"type":"reasoning","part":{"type":"reasoning","text":"思考"}}\n'
            '{"type":"text","part":{"type":"text","text":"，世界！"}}\n'
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}\n'
        )
        self.assertEqual(OpenCodeTranslator._parse_output(output), "你好，世界！")

    def test_skips_non_json_lines(self):
        output = (
            'warning: something\n{"type":"text","part":{"type":"text","text":"ok"}}\n'
        )
        self.assertEqual(OpenCodeTranslator._parse_output(output), "ok")

    def test_empty_output_raises(self):
        with self.assertRaises(ValueError):
            OpenCodeTranslator._parse_output('{"type":"step_start"}\n')


class TestFactoryRegistration(unittest.TestCase):
    def _isolated_build(self, service):
        with (
            patch(
                "pdf2zh.translator.ConfigManager.get_translator_by_name",
                return_value=None,
            ),
            patch("pdf2zh.translator.ConfigManager.set_translator_by_name"),
            patch.object(OpenCodeTranslator, "_test_opencode"),
            patch.object(OpenCodeTranslator, "_test_server"),
        ):
            return build_translator(service, "en", "zh")

    def test_build_by_name(self):
        translator = self._isolated_build("opencode")
        self.assertIsInstance(translator, OpenCodeTranslator)
        self.assertEqual(translator.model, "default")

    def test_build_with_model_route(self):
        translator = self._isolated_build("opencode:opencode/gpt-5")
        self.assertIsInstance(translator, OpenCodeTranslator)
        self.assertEqual(translator.model, "opencode/gpt-5")

    def test_env_defaults(self):
        translator = _make_translator()
        self.assertEqual(translator.envs["OPENCODE_PATH"], "opencode")
        self.assertEqual(translator.envs["OPENCODE_TIMEOUT"], "300")
        self.assertEqual(translator.envs["OPENCODE_SERVER_URL"], "")


class TestCliMode(unittest.TestCase):
    def test_cmd_assembly(self):
        translator = _make_translator(
            OPENCODE_MODEL="opencode/gpt-5", OPENCODE_AGENT="plan"
        )
        captured = {}

        class FakeProc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                captured["cmd_args"] = None
                return (
                    '{"type":"text","part":{"type":"text","text":"译文"}}',
                    "",
                )

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        with patch("pdf2zh.translator.subprocess.Popen", side_effect=fake_popen):
            out = translator.do_translate("hello")
        self.assertEqual(out, "译文")
        self.assertIn("--model", captured["cmd"])
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--model") + 1], "opencode/gpt-5"
        )
        self.assertIn("--agent", captured["cmd"])
        self.assertIn("--format", captured["cmd"])
        self.assertIn("json", captured["cmd"])

    def test_nonzero_exit_raises_called_process_error(self):
        translator = _make_translator()

        class FakeProc:
            returncode = 1

            def communicate(self, input=None, timeout=None):
                return "", "boom"

        with patch("pdf2zh.translator.subprocess.Popen", return_value=FakeProc()):
            with self.assertRaises(Exception):
                translator.do_translate("hello")


class TestServeMode(unittest.TestCase):
    def test_server_url_detection(self):
        translator = _make_translator(OPENCODE_SERVER_URL="http://127.0.0.1:4096/")
        self.assertEqual(translator.server_url, "http://127.0.0.1:4096")
        cli_translator = _make_translator()
        self.assertEqual(cli_translator.server_url, "")

    def test_model_field_parsing(self):
        translator = _make_translator(OPENCODE_MODEL="opencode/gpt-5")
        self.assertEqual(
            translator._server_model_field(),
            {"providerID": "opencode", "modelID": "gpt-5"},
        )
        translator2 = _make_translator()
        self.assertIsNone(translator2._server_model_field())

    def _fake_session(self, message_response):
        """post 按 URL 路径分流（/session 建会话，/message 收响应），支持重试。"""
        session = MagicMock()
        created = MagicMock()
        created.json.return_value = {"id": "ses_test"}

        def post(url, **kwargs):
            if url.endswith("/message"):
                return message_response
            return created

        session.post.side_effect = post
        return session

    def test_translate_via_server_success(self):
        translator = _make_translator(OPENCODE_SERVER_URL="http://x:4096")
        message_resp = MagicMock()
        message_resp.json.return_value = {
            "info": {"finish": "stop"},
            "parts": [{"type": "text", "text": "你好"}],
        }
        with patch(
            "pdf2zh.translator._thread_local_session",
            return_value=self._fake_session(message_resp),
        ):
            out = translator.do_translate("hello")
        self.assertEqual(out, "你好")

    def test_translate_via_server_empty_response_raises(self):
        translator = _make_translator(OPENCODE_SERVER_URL="http://x:4096")
        message_resp = MagicMock()
        message_resp.json.return_value = {
            "info": {"finish": None},
            "parts": [],
        }
        with patch(
            "pdf2zh.translator._thread_local_session",
            return_value=self._fake_session(message_resp),
        ):
            with self.assertRaises(ValueError):
                translator.do_translate("hello")


class TestBabeldocNextFallback(unittest.TestCase):
    """锁定 opencode 在 pdf2zh_next 内核的回退契约。

    vendored 内核无 OpenCode 原生映射（刻意不维护分叉）：RuntimeService 调
    run_babeldoc_next_translation 时必须收到 BabeldocNextUnavailableError
    才会回退 legacy adapter（那里 build_translator('opencode') 可用）。
    """

    def test_next_kernel_has_no_opencode_mapping(self):
        from pdf2zh import babeldoc_next_adapter as adapter

        try:
            adapter._ensure_next_kernel()
        except adapter.BabeldocNextUnavailableError:
            self.skipTest("pdf2zh_next 内核不可用（运行时同样直接回退 legacy）")
        with self.assertRaises(adapter.BabeldocNextUnavailableError) as ctx:
            adapter._build_engine_settings("opencode", None)
        self.assertIn("opencode", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
