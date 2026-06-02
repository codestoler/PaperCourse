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


if __name__ == "__main__":
    unittest.main()
