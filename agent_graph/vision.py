"""Optional vision-model adapter for image understanding."""

from __future__ import annotations

import json
import os
import asyncio
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .llm import load_env, parse_json_object


IMAGE_UNDERSTANDING_PROMPT = """你是课程编译器中的轻量级图片理解 Agent。
请只根据图片本身和给定的邻近文本，返回严格 JSON，不要输出 Markdown。

识别目标包括：函数图像、公式图片、几何/结构示意图、流程图、实验装置图、表格截图、图文混排和无法确认的图片。
不要编造图片中不存在的公式、数字、标签或结论。不能可靠判断时必须设置 needs_confirmation=true。
"""


FORMULA_IMAGE_RECOGNITION_PROMPT = """你是课程编译器中的 Formula Image Recognition Agent。
请结合单张图片、所在页上下文、页面截图路径说明和相邻文本，识别图片中的公式、推导或数学/物理/化学方程。

要求：
- 只在图片和上下文均支持时输出可编辑 Markdown / LaTeX。
- 校验公式符号、上下标、矩阵/分段/积分/求和结构，不确定处不要猜。
- 判断公式角色：定义、定理、推导、例题公式或注释公式。
- 置信度不足时必须 needs_human_review=true 且 preserve_original_image=true，避免错误公式直接写入课程正文。
- 返回严格 JSON，不要输出 Markdown 之外的自由文本。
"""


class VisionImageClient:
    """Small MCP client wrapper around the configured Z.AI vision server."""

    def __init__(self, env: dict[str, str], timeout_ms: str = "300000") -> None:
        self.env = env
        self.timeout_ms = timeout_ms
        try:
            self.timeout_seconds = max(1.0, int(timeout_ms) / 1000)
        except ValueError:
            self.timeout_seconds = 300.0
        self.cache_identity = {
            "provider": "z_ai_mcp",
            "server": "@z_ai/mcp-server",
            "model": env.get("Z_AI_VISION_MODEL", ""),
        }

    @classmethod
    def from_env(cls) -> "VisionImageClient | None":
        values = load_env()
        api_key = values.get("Z_AI_API_KEY") or values.get("GLM_API_KEY") or values.get("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            return None
        env = os.environ.copy()
        env["Z_AI_API_KEY"] = api_key
        env.setdefault("Z_AI_MODE", "ZHIPU")
        timeout_ms = values.get("Z_AI_TIMEOUT", "300000")
        env.setdefault("Z_AI_TIMEOUT", timeout_ms)
        if values.get("Z_AI_VISION_MODEL"):
            env["Z_AI_VISION_MODEL"] = values["Z_AI_VISION_MODEL"]
        return cls(env, timeout_ms=timeout_ms)

    async def analyze_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        server = StdioServerParameters(command="npx", args=["-y", "@z_ai/mcp-server"], env=self.env)
        results: list[dict[str, Any]] = []
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                for record in records:
                    results.append(await self._safe_analyze_one(session, record))
        return results

    async def analyze_formula_many(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        server = StdioServerParameters(command="npx", args=["-y", "@z_ai/mcp-server"], env=self.env)
        results: list[dict[str, Any]] = []
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                for record in records:
                    results.append(await self._safe_analyze_formula_one(session, record))
        return results

    async def _safe_analyze_one(self, session: ClientSession, record: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._analyze_one(session, record), timeout=self.timeout_seconds)
        except Exception as exc:  # pragma: no cover - depends on external MCP/model behavior
            return {
                "image_type": record.get("image_type", "unknown"),
                "summary": record.get("summary", ""),
                "caption": record.get("caption", "待确认图片"),
                "needs_confirmation": True,
                "confidence": 0.0,
                "reason": f"vision_mcp_failed: {exc}",
            }

    async def _safe_analyze_formula_one(self, session: ClientSession, record: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._analyze_formula_one(session, record), timeout=self.timeout_seconds)
        except Exception as exc:  # pragma: no cover - depends on external MCP/model behavior
            return {
                "image_id": record.get("id", ""),
                "is_formula": True,
                "recognized_text": "",
                "latex": "",
                "markdown": "",
                "context_check": "uncertain",
                "confidence": 0.0,
                "needs_human_review": True,
                "preserve_original_image": True,
                "caption": record.get("caption", "待确认公式图片"),
                "reason": f"formula_vision_mcp_failed: {exc}",
            }

    async def _analyze_one(self, session: ClientSession, record: dict[str, Any]) -> dict[str, Any]:
        image_path = Path(str(record.get("path", "")))
        prompt = _image_prompt(record)
        result = await session.call_tool(
            "analyze_image",
            {
                "image_source": str(image_path.resolve()),
                "prompt": prompt,
            },
        )
        text = "\n\n".join(item.text for item in result.content if getattr(item, "type", "") == "text").strip()
        return parse_json_object(text)

    async def _analyze_formula_one(self, session: ClientSession, record: dict[str, Any]) -> dict[str, Any]:
        image_path = Path(str(record.get("path", "")))
        prompt = _formula_image_prompt(record)
        result = await session.call_tool(
            "analyze_image",
            {
                "image_source": str(image_path.resolve()),
                "prompt": prompt,
            },
        )
        text = "\n\n".join(item.text for item in result.content if getattr(item, "type", "") == "text").strip()
        return parse_json_object(text)


def _image_prompt(record: dict[str, Any]) -> str:
    context = {
        "image_id": record.get("id", ""),
        "mineru_type": record.get("image_type", ""),
        "source_chunk_id": record.get("source_chunk_id", ""),
        "page_idx": record.get("page_idx"),
        "bbox": record.get("bbox", []),
        "neighbor_summary": record.get("summary", ""),
        "associated_knowledge_points": record.get("associated_knowledge_points", []),
    }
    schema = {
        "image_type": "function_graph|formula_image|geometry_diagram|structure_diagram|flowchart|experimental_setup|table_screenshot|mixed_text_image|unknown",
        "content_summary": "...",
        "associated_knowledge_points": ["..."],
        "summary": "...",
        "suggested_insert_after": "...",
        "suggested_insert_position": "after_source_block|replace_image_marker|pending_confirmation",
        "preserve_original_image": True,
        "needs_caption": True,
        "caption": "...",
        "needs_confirmation": False,
        "confidence": 0.0,
        "reason": "...",
    }
    return (
        f"{IMAGE_UNDERSTANDING_PROMPT}\n\n"
        f"Return JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Parser and neighbor context:\n{json.dumps(context, ensure_ascii=False)}"
    )


def _formula_image_prompt(record: dict[str, Any]) -> str:
    context = {
        "image_id": record.get("id", ""),
        "source_chunk_id": record.get("source_chunk_id", ""),
        "page_idx": record.get("page_idx"),
        "bbox": record.get("bbox", []),
        "image_type": record.get("image_type", ""),
        "mineru_type": record.get("mineru_type", ""),
        "mineru_content": record.get("mineru_content", ""),
        "caption": record.get("caption", ""),
        "summary": record.get("summary", ""),
        "associated_knowledge_points": record.get("associated_knowledge_points", []),
        "neighbor_text": record.get("neighbor_text", ""),
        "page_context": record.get("page_context", ""),
        "page_screenshot_path": record.get("page_screenshot_path", ""),
    }
    schema = {
        "image_id": record.get("id", ""),
        "is_formula": True,
        "formula_role": "definition|theorem|derivation|example_formula|annotation_formula|unknown",
        "recognized_text": "...",
        "latex": "...",
        "markdown": "$$\\n...\\n$$",
        "context_check": "consistent|partially_supported|unsupported|uncertain",
        "confidence": 0.0,
        "needs_human_review": True,
        "preserve_original_image": True,
        "caption": "...",
        "reason": "...",
    }
    return (
        f"{FORMULA_IMAGE_RECOGNITION_PROMPT}\n\n"
        f"Return JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Image, page, and neighboring context:\n{json.dumps(context, ensure_ascii=False)}"
    )
