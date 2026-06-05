from __future__ import annotations

import unittest
from unittest.mock import patch

from agent_graph.llm import LLMClient, _anthropic_user_blocks, parse_json_object


class LLMClientTests(unittest.TestCase):
    def test_glm_anthropic_takes_priority_over_siliconflow_llm(self) -> None:
        with patch(
            "agent_graph.llm.load_env",
            return_value={
                "GLM_ANTHROPIC_URL": "https://open.bigmodel.cn/api/anthropic",
                "GLM_API_KEY": "glm-key",
                "GLM_MODEL": "GLM-4.7",
                "LLM_CONNECT_TIMEOUT": "12",
                "LLM_BASE_URL": "https://api.siliconflow.cn/v1",
                "LLM_API_KEY": "llm-key",
                "LLM_MODEL": "other-model",
            },
        ):
            client = LLMClient.from_env()

        self.assertIsNotNone(client)
        self.assertEqual(client.provider, "anthropic")
        self.assertEqual(client.base_url, "https://open.bigmodel.cn/api/anthropic")
        self.assertEqual(client.model, "GLM-4.7")
        self.assertEqual(client.connect_timeout, 12)

    def test_siliconflow_is_not_implicit_fallback(self) -> None:
        with patch(
            "agent_graph.llm.load_env",
            return_value={
                "LLM_BASE_URL": "https://api.siliconflow.cn/v1",
                "LLM_API_KEY": "llm-key",
                "LLM_MODEL": "other-model",
                "LLM_ALLOW_GENERIC_FALLBACK": "1",
            },
        ):
            client = LLMClient.from_env()

        self.assertIsNone(client)

    def test_anthropic_blocks_cache_source_chunks_prefix(self) -> None:
        blocks = _anthropic_user_blocks("Task intro\n\nSource chunks:\n- id: chunk-1\n  content: source")

        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("cache_control", blocks[1])
        self.assertTrue(blocks[1]["text"].startswith("Source chunks:\n"))

    def test_anthropic_blocks_cache_units_without_empty_separator(self) -> None:
        blocks = _anthropic_user_blocks("Return JSON.\n\nUnits:\n[{\"id\":\"unit-001\"}]")

        self.assertEqual(blocks[0]["text"], "Return JSON.")
        self.assertEqual(blocks[1]["text"], "Units:\n[{\"id\":\"unit-001\"}]")
        self.assertIn("cache_control", blocks[1])

        fallback = _anthropic_user_blocks("Return JSON without a cache marker.")
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0]["text"], "Return JSON without a cache marker.")

    def test_parse_json_repairs_raw_latex_backslashes(self) -> None:
        parsed = parse_json_object(r'{"body_markdown":"公式 \langle x,y \rangle = \Phi(x) + \theta"}')

        self.assertEqual(parsed["body_markdown"], r"公式 \langle x,y \rangle = \Phi(x) + \theta")

    def test_parse_json_keeps_markdown_newline_escapes(self) -> None:
        parsed = parse_json_object('{"body_markdown":"第一行\\n- 第二行"}')

        self.assertEqual(parsed["body_markdown"], "第一行\n- 第二行")

    def test_parse_json_keeps_newline_before_lowercase_formula_symbol(self) -> None:
        parsed = parse_json_object(
            r'{"body_markdown":"$$\begin{bmatrix}\nd_0 \\\nd_1\end{bmatrix} + \rangle + \theta + \frac{1}{2}$$"}'
        )

        self.assertIn("\nd_0", parsed["body_markdown"])
        self.assertNotIn(r"\nd_0", parsed["body_markdown"])
        self.assertIn(r"\rangle", parsed["body_markdown"])
        self.assertIn(r"\theta", parsed["body_markdown"])
        self.assertIn(r"\frac", parsed["body_markdown"])

    def test_complete_json_retries_bad_json(self) -> None:
        class FakeResponse:
            def __init__(self, body: str) -> None:
                self.body = body
                self.text = body

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                import json

                return json.loads(self.body)

        responses = [
            FakeResponse('{"choices":[{"message":{"content":"not json"}}]}'),
            FakeResponse('{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}'),
        ]

        with patch("agent_graph.llm.shutil.which", return_value=None), patch("agent_graph.llm.requests.post", side_effect=responses) as post:
            client = LLMClient("https://example.test/v1", "key", "model", timeout=300, connect_timeout=9, retries=1, retry_backoff_seconds=0)
            result = client.complete_json("system", "user")

        self.assertTrue(result["ok"])
        self.assertEqual(client.last_metadata["attempts"], 2)
        self.assertEqual(post.call_args.kwargs["timeout"], (9, 300))

    def test_complete_json_curl_uses_config_file_for_headers(self) -> None:
        completed = __import__("subprocess").CompletedProcess(["curl"], 0, '{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}\n200', "")
        with patch("agent_graph.llm.shutil.which", return_value="/usr/bin/curl"), patch("agent_graph.llm.subprocess.run", return_value=completed) as run:
            client = LLMClient("https://example.test/v1", "secret-key", "model", timeout=300, connect_timeout=9, retries=0)
            result = client.complete_json("system", "user")

        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[0], "curl")
        self.assertNotIn("secret-key", " ".join(command))
        self.assertEqual(run.call_args.kwargs["timeout"], 314)


if __name__ == "__main__":
    unittest.main()
