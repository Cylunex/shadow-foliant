import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import platform_llm  # noqa: E402
from core.llm_router import _platform_token_budget  # noqa: E402


class PlatformLLMTests(unittest.TestCase):
    def tearDown(self):
        platform_llm.close_clients()

    def test_metadata_only_bridge_preserves_chat_messages(self):
        fake = SimpleNamespace()
        fake.create = lambda **payload: {
            "model": "example-model",
            "choices": [{"message": {"content": "ok"}}],
        }
        config = SimpleNamespace(api="chat-completions", model="example-model")
        with patch.dict(
            os.environ,
            {
                "SHADOW_LLM_ENABLED": "true",
                "SHADOW_LLM_REGISTRY_FILE": "/example/registry.yml",
                "SHADOW_PLATFORM_SECRETS_DIR": "/example/secrets",
            },
            clear=False,
        ), patch.object(platform_llm, "_client", return_value=(fake, config)):
            text, provider = platform_llm.call(
                [{"role": "user", "content": "private body"}],
                temperature=0.2,
                max_tokens=100,
                thinking=False,
                call_type="technical_analysis",
            )
        self.assertEqual(text, "ok")
        self.assertTrue(provider.startswith("shadow:chat-default:"))

    def test_responses_payload_is_explicitly_converted_by_foliant(self):
        payload = platform_llm._provider_payload(
            "responses",
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "body"},
            ],
            temperature=0.4,
            max_tokens=200,
            thinking=False,
        )
        self.assertEqual(payload["instructions"], "rules")
        self.assertEqual(payload["input"], [{"role": "user", "content": "body"}])
        self.assertNotIn("model", payload)

    def test_platform_chat_budget_leaves_room_after_reasoning(self):
        with patch.dict(os.environ, {"SHADOW_LLM_MIN_OUTPUT_TOKENS": "2000"}):
            self.assertEqual(_platform_token_budget(400, False), 2000)
            self.assertEqual(_platform_token_budget(3000, False), 3000)
        self.assertEqual(_platform_token_budget(400, True), 8000)

    def test_bounded_router_never_retries_platform_failure_via_legacy(self):
        from core.llm_router import LLMRouter
        platform = SimpleNamespace(configured=lambda: True, call=Mock(side_effect=RuntimeError('unavailable')))
        router = object.__new__(LLMRouter)
        router.providers = [object()]
        with patch.dict(sys.modules, {'platform_llm': platform}):
            result = router.call([{'role': 'user', 'content': 'public facts'}],
                                 call_type='case_review', max_tokens=600, timeout=20)
        self.assertEqual(result, ('', 'none'))
        self.assertEqual(platform.call.call_count, 1)
        self.assertEqual(platform.call.call_args.kwargs['max_tokens'], 600)
        self.assertEqual(platform.call.call_args.kwargs['timeout'], 20)
        self.assertTrue(platform.call.call_args.kwargs['single_attempt'])

    def test_bounded_sdk_has_one_target_no_retry_and_request_timeout(self):
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Config:
            timeout_seconds: int = 90
            fallbacks: tuple = ('secondary',)
        sdk = SimpleNamespace(JsonlUsageSink=Mock(), NullUsageSink=Mock(),
                              LLMClient=Mock(), RetryPolicy=Mock())
        with patch.dict(sys.modules, {'shadow_sdk': sdk}):
            platform_llm._single_attempt_client(Config(), timeout=20)
        targets = sdk.LLMClient.call_args.args[0]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].timeout_seconds, 20)
        self.assertEqual(targets[0].fallbacks, ())
        sdk.RetryPolicy.assert_called_once_with(max_retries=0)

    def test_bounded_bridge_closes_temporary_client_on_empty_response(self):
        fake = Mock()
        fake.create.return_value = {'choices': [{'message': {'content': ''}}]}
        config = SimpleNamespace(api='chat-completions', model='example-model')
        with patch.object(platform_llm, 'configured', return_value=True), \
                patch.object(platform_llm, '_client', return_value=(object(), config)), \
                patch.object(platform_llm, '_single_attempt_client', return_value=fake):
            with self.assertRaises(platform_llm.PlatformLLMUnavailable):
                platform_llm.call([], temperature=0, max_tokens=600, thinking=False,
                                  call_type='case_review', single_attempt=True, timeout=20)
        fake.create.assert_called_once()
        fake.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
