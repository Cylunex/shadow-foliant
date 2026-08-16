import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import platform_llm  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
