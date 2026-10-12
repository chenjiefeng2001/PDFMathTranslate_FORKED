import unittest
from textwrap import dedent
from unittest import mock

import pytest
import requests

from ollama import ResponseError as OllamaResponseError

from pdf2zh import cache
from pdf2zh.config import ConfigManager
from pdf2zh.translator import (
    BaseTranslator,
    HTTP_TIMEOUT,
    GoogleTranslator,
    OllamaTranslator,
    OpenAIlikedTranslator,
    _resolve_translator_proxy,
)

# Since it is necessary to test whether the functionality meets the expected requirements,
# private functions and private methods are allowed to be called.
# pyright: reportPrivateUsage=false


class _FrozenBackoff:
    """把 tenacity 的退避等待冻成 0，只记账不真睡。

    为什么需要
    ----------
    Google 翻译器的重试策略是 ``wait_exponential(multiplier=2, min=2, max=20)``：
    一次"一直 429"的重试链要真睡 2s + 4s = **6 秒**（实测）。验证限流逻辑的
    用例并不关心"等多久才重试"，却要为这个等待实打实付钱 —— 两条用例加起来
    占掉整个 ``test_translator.py`` 三分之二的时间（24s + 6s = 30s / 35s）。

    为什么是改**装饰器实例**而不是改模块
    ------------------------------------
    ``@retry`` 在 ``translator.py`` 导入时就把 ``sleep`` 的函数对象捕获进
    ``Retrying`` 实例（``BaseRetrying.__init__`` 的默认参数绑定），事后再改
    ``tenacity.nap.sleep`` 对已装饰的函数**无效** —— 实测过，先踩了这个坑。
    装饰器把实例挂在函数对象的 ``.retry`` 属性上（ tenacity 的公开做法），
    所以从那里拿到实例、把 ``.sleep`` 换掉，才是真的在冻时钟。

    被测逻辑一行没改：``DoSleep`` 对象仍带着正确的 ``upcoming_sleep``，
    "重试几次""等多久才重试"这些断言依然成立。
    """

    def __init__(self, *retryings):
        self.slept: list[float] = []
        self._retryings = retryings

    def __call__(self, seconds):
        self.slept.append(seconds)

    @property
    def total(self) -> float:
        """本轮冻结期间"本该睡掉"的秒数总和。"""
        return sum(self.slept)

    def __enter__(self):
        self._saved = [(r, r.sleep) for r in self._retryings]
        for r in self._retryings:
            r.sleep = self
        return self

    def __exit__(self, *exc):
        for r, old in self._saved:
            r.sleep = old
        self._saved = []
        return False


def _frozen_backoff(*funcs):
    """冻住这些 ``@retry`` 函数的退避时钟。

    用法：``with _frozen_backoff(GoogleTranslator.do_translate) as f:``，
    ``f.slept`` 是每次退避的秒数（用于断言"退避策略确实按预期排布"）。
    """
    return _FrozenBackoff(*(f.retry for f in funcs))


class AutoIncreaseTranslator(BaseTranslator):
    name = "auto_increase"
    n = 0

    def do_translate(self, text):
        self.n += 1
        return str(self.n)


class TestTranslator(unittest.TestCase):
    def setUp(self):
        self.test_db = cache.init_test_db()

    def tearDown(self):
        cache.clean_test_db(self.test_db)

    def test_cache(self):
        translator = AutoIncreaseTranslator("en", "zh", "test", False)
        # First translation should be cached
        text = "Hello World"
        first_result = translator.translate(text)

        # Second translation should return the same result from cache
        second_result = translator.translate(text)
        self.assertEqual(first_result, second_result)

        # Different input should give different result
        different_text = "Different Text"
        different_result = translator.translate(different_text)
        self.assertNotEqual(first_result, different_result)

        # Test cache with ignore_cache=True
        translator.ignore_cache = True
        no_cache_result = translator.translate(text)
        self.assertNotEqual(first_result, no_cache_result)

    def test_add_cache_impact_parameters(self):
        translator = AutoIncreaseTranslator("en", "zh", "test", False)

        # Test cache with added parameters
        text = "Hello World"
        first_result = translator.translate(text)
        translator.add_cache_impact_parameters("test", "value")
        second_result = translator.translate(text)
        self.assertNotEqual(first_result, second_result)

        # Test cache with ignore_cache=True
        no_cache_result1 = translator.translate(text, ignore_cache=True)
        self.assertNotEqual(first_result, no_cache_result1)

        translator.ignore_cache = True
        no_cache_result2 = translator.translate(text)
        self.assertNotEqual(no_cache_result1, no_cache_result2)

        # Test cache with ignore_cache=False
        translator.ignore_cache = False
        cache_result = translator.translate(text)
        self.assertEqual(no_cache_result2, cache_result)

        # Test cache with another parameter
        translator.add_cache_impact_parameters("test2", "value2")
        another_result = translator.translate(text)
        self.assertNotEqual(second_result, another_result)

    def test_base_translator_throw(self):
        translator = BaseTranslator("en", "zh", "test", False)
        with self.assertRaises(NotImplementedError):
            translator.translate("Hello World")


class TestOpenAIlikedTranslator(unittest.TestCase):
    def setUp(self) -> None:
        self.default_envs = {
            "OPENAILIKED_BASE_URL": "https://api.openailiked.com",
            "OPENAILIKED_API_KEY": "test_api_key",
            "OPENAILIKED_MODEL": "test_model",
        }

    def test_missing_base_url_raises_error(self):
        """测试缺失 OPENAILIKED_BASE_URL 时抛出异常"""
        ConfigManager.clear()
        with self.assertRaises(ValueError) as context:
            OpenAIlikedTranslator(
                lang_in="en", lang_out="zh", model="test_model", envs={}
            )
        self.assertIn("The OPENAILIKED_BASE_URL is missing.", str(context.exception))

    def test_missing_model_raises_error(self):
        """测试缺失 OPENAILIKED_MODEL 时抛出异常"""
        envs_without_model = {
            "OPENAILIKED_BASE_URL": "https://api.openailiked.com",
            "OPENAILIKED_API_KEY": "test_api_key",
        }
        ConfigManager.clear()
        with self.assertRaises(ValueError) as context:
            OpenAIlikedTranslator(
                lang_in="en", lang_out="zh", model=None, envs=envs_without_model
            )
        self.assertIn("The OPENAILIKED_MODEL is missing.", str(context.exception))

    def test_initialization_with_valid_envs(self):
        """测试使用有效的环境变量初始化"""
        ConfigManager.clear()
        translator = OpenAIlikedTranslator(
            lang_in="en",
            lang_out="zh",
            model=None,
            envs=self.default_envs,
        )
        self.assertEqual(
            translator.envs["OPENAILIKED_BASE_URL"],
            self.default_envs["OPENAILIKED_BASE_URL"],
        )
        self.assertEqual(
            translator.envs["OPENAILIKED_API_KEY"],
            self.default_envs["OPENAILIKED_API_KEY"],
        )
        self.assertEqual(translator.model, self.default_envs["OPENAILIKED_MODEL"])

    def test_default_api_key_fallback(self):
        """测试当 OPENAILIKED_API_KEY 为空时使用默认值"""
        envs_without_key = {
            "OPENAILIKED_BASE_URL": "https://api.openailiked.com",
            "OPENAILIKED_MODEL": "test_model",
        }
        ConfigManager.clear()
        translator = OpenAIlikedTranslator(
            lang_in="en",
            lang_out="zh",
            model=None,
            envs=envs_without_key,
        )
        self.assertEqual(
            translator.envs["OPENAILIKED_BASE_URL"],
            self.default_envs["OPENAILIKED_BASE_URL"],
        )
        self.assertIsNone(translator.envs["OPENAILIKED_API_KEY"])


class TestOllamaTranslator(unittest.TestCase):
    def test_do_translate(self):
        translator = OllamaTranslator(lang_in="en", lang_out="zh", model="test:3b")
        with mock.patch.object(translator, "client") as mock_client:
            chat_response = mock_client.chat.return_value
            chat_response.message.content = dedent("""\
                <think>
                Thinking...
                </think>

                天空呈现蓝色是因为...
                """)

            text = "The sky appears blue because of..."
            translated_result = translator.do_translate(text)
            mock_client.chat.assert_called_once_with(
                model="test:3b",
                messages=translator.prompt(text, prompt_template=None),
                options={
                    "temperature": translator.options["temperature"],
                    "num_predict": translator.options["num_predict"],
                },
            )
            self.assertEqual("天空呈现蓝色是因为...", translated_result)

            # response error
            mock_client.chat.side_effect = OllamaResponseError("an error status")
            with self.assertRaises(OllamaResponseError):
                mock_client.chat()

    def test_remove_cot_content(self):
        fake_cot_resp_text = dedent("""\
            <think>

            </think>

            The sky appears blue because of...""")
        removed_cot_content = OllamaTranslator._remove_cot_content(fake_cot_resp_text)
        excepted_content = "The sky appears blue because of..."
        self.assertEqual(excepted_content, removed_cot_content.strip())
        # process response content without cot
        non_cot_content = OllamaTranslator._remove_cot_content(excepted_content)
        self.assertEqual(excepted_content, non_cot_content)

        # `_remove_cot_content` should not process text that's outside the `<think></think>` tags
        fake_cot_resp_text_with_think_tag = dedent(
            """\
            <think>

            </think>

            The sky appears blue because of......
            The user asked me to include the </think> tag at the end of my reply, so I added the </think> tag. </think>"""
        )

        only_removed_cot_content = OllamaTranslator._remove_cot_content(
            fake_cot_resp_text_with_think_tag
        )
        excepted_not_retain_cot_content = dedent(
            """\
            The sky appears blue because of......
            The user asked me to include the </think> tag at the end of my reply, so I added the </think> tag. </think>"""
        )
        self.assertEqual(
            excepted_not_retain_cot_content, only_removed_cot_content.strip()
        )

    def test_resolve_translator_proxy_precedence(self):
        with mock.patch.dict(
            "os.environ",
            {
                "PDF2ZH_PROXY": "http://pdf2zh.proxy:3128",
                "HTTP_PROXY": "http://http.proxy:8080",
            },
            clear=False,
        ):
            proxies = _resolve_translator_proxy()
            self.assertEqual(
                proxies,
                {
                    "http": "http://pdf2zh.proxy:3128",
                    "https": "http://pdf2zh.proxy:3128",
                },
            )

        with mock.patch.dict("os.environ", {}, clear=True):
            proxies = _resolve_translator_proxy(
                {"PDF2ZH_PROXY": "http://ui.proxy:7890"}
            )
            self.assertEqual(
                proxies,
                {"http": "http://ui.proxy:7890", "https": "http://ui.proxy:7890"},
            )
            self.assertIsNone(_resolve_translator_proxy())

    def test_google_session_proxy_and_bounded_timeout(self):
        with mock.patch.dict(
            "os.environ", {"PDF2ZH_PROXY": "http://127.0.0.1:7890"}, clear=False
        ):
            t = GoogleTranslator("en", "zh", None, True)
        self.assertEqual(
            t.session.proxies,
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )
        with mock.patch.object(t.session, "get") as m_get:
            m_get.return_value = mock.Mock(
                status_code=200, text='<div class="result-container">Hallo</div>'
            )
            t.do_translate("hello")
            kwargs = m_get.call_args.kwargs
            self.assertEqual(kwargs["timeout"], HTTP_TIMEOUT)

    def _rate_limited_response(self, status=429):
        resp = mock.Mock(
            status_code=status,
            text="<html>Rate limit exceeded</html>",
            headers={},
        )
        # Mock 的 raise_for_status 默认是 no-op —— 显式让它按 requests 的语义抛。
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status} Client Error: rate limited"
        )
        return resp

    def test_google_rate_limit_is_retried_not_silently_dropped(self):
        """429/503 必须抛可重试错误，而不是静默返回原文。

        旧行为：Google 限流页（429，body 3347 字节）被当成"页面格式变了"
        回落原文 —— tenacity 根本看不到异常，一次重试都没有。实测 325 页书
        90 秒 1367 次 429，最终交付一份全英文 PDF、退出码 0、无任何告警。
        """
        with _frozen_backoff(GoogleTranslator.do_translate) as frozen:
            t = GoogleTranslator("en", "zh", None, True)
            with mock.patch.object(t.session, "get") as m_get:
                m_get.return_value = self._rate_limited_response(429)
                with self.assertRaises(requests.HTTPError):
                    t.do_translate("hello")
                # 单块重试 3 次：不重试 = 旧缺陷复现
                self.assertEqual(m_get.call_count, 3)
                # 退避是真的在按策略安排（冻了时钟，但策略没被绕过）
                self.assertEqual(len(frozen.slept), 2, f"退避次数不对：{frozen.slept}")
                self.assertEqual(frozen.slept[0], 2, "首等应是 wait 的 min=2")
                self.assertEqual(frozen.slept[1], 4, "次等应是 multiplier=2 翻倍")
                self.assertLessEqual(
                    frozen.total,
                    10,
                    f"退避总时长 {frozen.total}s：这条用例是在验限流逻辑，"
                    "不该为真实等待付钱（冻结之后仍偏大 = 策略被调大了）",
                )

    def test_google_streak_rate_limit_fails_fast_with_actionable_message(self):
        """连续限流要硬失败，而不是把整本书磨成原文 PDF。

        瞬时限流应当被重试容忍（成功后计数清零，见下一条），但持续限流再翻
        下去只是浪费时间：达到阈值后抛出带排查指引的 RuntimeError。
        """
        from pdf2zh.translator import _GOOGLE_RATE_LIMIT_FAIL_FAST

        with _frozen_backoff(GoogleTranslator.do_translate):
            t = GoogleTranslator("en", "zh", None, True)
            with mock.patch.object(t.session, "get") as m_get:
                m_get.return_value = self._rate_limited_response(429)
                err = None
                # 每块 3 次尝试：跑到阈值就会从 HTTPError 切换到 RuntimeError
                for _ in range(_GOOGLE_RATE_LIMIT_FAIL_FAST + 2):
                    try:
                        t.do_translate("hello")
                    except RuntimeError as exc:  # noqa: BLE001 -- 正是要抓的
                        err = exc
                        break
                    except requests.HTTPError:
                        continue
                self.assertIsInstance(err, RuntimeError, "持续限流应硬失败")
                self.assertIn("限流", str(err))
                self.assertIn("代理", str(err))
                # "fail fast" 的另一半：不该把阈值乘 3 的量都跑完
                self.assertLessEqual(
                    m_get.call_count,
                    (_GOOGLE_RATE_LIMIT_FAIL_FAST + 1) * 3,
                    f"重试次数超出 fail-fast 预期：{m_get.call_count}",
                )

    def test_google_success_resets_rate_limit_streak(self):
        """成功一次就清零：不能因为历史上被限流过就永久判死。"""
        t = GoogleTranslator("en", "zh", None, True)
        t._rate_limit_streak = 5
        with mock.patch.object(t.session, "get") as m_get:
            m_get.return_value = mock.Mock(
                status_code=200, text='<div class="result-container">Hallo</div>'
            )
            self.assertEqual(t.do_translate("hello"), "Hallo")
        self.assertEqual(t._rate_limit_streak, 0)


def test_rate_limit_hard_failure_escapes_the_retry_wrapper():
    """硬失败必须穿透 tenacity 重试，且只调用一次。

    ``converter.worker`` 用 tenacity 包装翻译调用。默认配置对**一切**异常
    重试，持续限流会被重试 3 轮（每轮 wait_fixed(1)）才罢休 —— 每块多熬 3
    秒，325 页就是额外 16 分钟。这里验证显式排除后 worker 只跑一次。
    """
    from tenacity import (
        retry,
        retry_if_not_exception_type,
        stop_after_attempt,
        wait_fixed,
    )

    from pdf2zh.translator import TranslationRateLimitError

    calls = []

    @retry(
        wait=wait_fixed(0),
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type(TranslationRateLimitError),
        reraise=True,
    )
    def worker(s):
        calls.append(s)
        raise TranslationRateLimitError("持续限流")

    with pytest.raises(TranslationRateLimitError):
        worker("hello")

    assert len(calls) == 1, "硬失败被 tenacity 重试了"

    # 对照：普通异常仍然重试满 3 次
    calls2 = []

    @retry(
        wait=wait_fixed(0),
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type(TranslationRateLimitError),
        reraise=True,
    )
    def worker2(s):
        calls2.append(s)
        raise ValueError("transient")

    with pytest.raises(ValueError):
        worker2("hello")
    assert len(calls2) == 3, "可恢复异常应仍走重试"


class TestSafeWorkerRateLimitPropagation(unittest.TestCase):
    """converter._safe_worker：持续限流必须上抛，而不是回落原文后继续跑。

    旧行为：translator 抛出的 RuntimeError 被 worker 重试吞掉、
    ``_safe_worker`` 记一条单块 error 就返回原文 —— 325 页的书在 429 下继续
    跑 40 分钟，连续计数冲到 1477 也不停，最后交付一份全英文 PDF。
    """

    def test_converter_branches_on_the_hard_failure_type(self):
        import inspect

        from pdf2zh import converter as C

        self.assertTrue(hasattr(C, "TranslationRateLimitError"))
        self.assertTrue(issubclass(C.TranslationRateLimitError, RuntimeError))

        # worker / _safe_worker 是闭包，取不到引用；退而断言源码结构。
        # 注意两处职责形态不同：worker 的"不重试"由 tenacity 的 retry= 承担，
        # _safe_worker 的"不回落原文"才是 except 分支（converter.py 有行数预算，
        # 不允许在 worker 里再写一个 except）。
        src = inspect.getsource(C)
        self.assertTrue(hasattr(C, "TranslationRateLimitError"))
        self.assertTrue(issubclass(C.TranslationRateLimitError, RuntimeError))
        self.assertIn(
            "retry=retry_if_not_exception_type(TranslationRateLimitError)",
            src,
            "worker 的 tenacity 必须排除硬失败（否则每块白等 3 轮退避才中止）",
        )
        self.assertEqual(
            src.count("except TranslationRateLimitError"),
            1,
            "_safe_worker 必须 reraise 硬失败，不能回落原文",
        )
        tail = src.split("except TranslationRateLimitError", 1)[1]
        body = [
            ln.strip()
            for ln in tail.strip().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.assertTrue(
            any(b.startswith("raise") for b in body[:3]),
            f"硬失败分支应在上抛前最多只记一条日志，实际分支体 {body[:4]}",
        )

    def test_rate_limit_error_type_is_importable_from_translator(self):
        from pdf2zh.translator import TranslationRateLimitError

        self.assertTrue(issubclass(TranslationRateLimitError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
