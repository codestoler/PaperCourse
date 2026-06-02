#!/usr/bin/env python3
"""Compile slide PDFs through page rendering plus Z.AI vision MCP analysis."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import fitz
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_graph.compiler import compile_course
from agent_graph.io import ensure_dir, slugify, write_json
from agent_graph.llm import load_env


VISION_PROMPT = """你正在把一页课程 PPT 转换为可教学的课程素材。
请直接阅读图片中的文字、公式、图表、例子和视觉关系，输出中文 Markdown。
要求：
- 保留本页标题和所有关键公式，公式用 LaTeX。
- 解释图表、流程、箭头、坐标系、颜色或排版隐含的关系。
- 区分定义、定理、算法步骤、例题、结论和教师提示。
- 不要泛泛描述“这是一页幻灯片”，只输出可用于课程编译的内容。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="PDF/PPT source files. PDF is supported locally; PPT requires prior PDF conversion.")
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--vault-root", default="course-vault")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit pages per source for smoke tests; 0 means all pages")
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--refresh-vision", action="store_true", help="Ignore cached per-page vision analysis")
    parser.add_argument("--use-llm", action="store_true", help="Use the text LLM planner after LVM parsing")
    parser.add_argument("--max-llm-chunks", type=int, default=90)
    args = parser.parse_args()

    parsed_sources = asyncio.run(
        compile_lvm_sources(
            [Path(source) for source in args.sources],
            Path(args.vault_root),
            args.course_id,
            max_pages=args.max_pages,
            dpi=args.dpi,
            refresh_vision=args.refresh_vision,
        )
    )
    profile = {"use_llm": args.use_llm, "max_llm_chunks": args.max_llm_chunks}
    state = compile_course([str(path) for path in parsed_sources], args.course_id, args.vault_root, args.version, profile=profile)
    print(f"status={state['next_action']}")
    print(f"lessons={len(state['lessons'])}")
    print(f"validation_ok={state['validation_report'].get('ok')}")
    print(f"parsed_sources={len(parsed_sources)}")
    if state["errors"]:
        print(f"errors={state['errors']}")
        return 1
    return 0 if state["next_action"] == "done" else 2


async def compile_lvm_sources(
    sources: list[Path],
    vault_root: Path,
    course_id: str,
    max_pages: int = 0,
    dpi: int = 144,
    refresh_vision: bool = False,
) -> list[Path]:
    env = vision_env()
    parsed_sources: list[Path] = []
    server = StdioServerParameters(command="npx", args=["-y", "@z_ai/mcp-server"], env=env)
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for source in sources:
                parsed_sources.append(
                    await process_source_with_lvm(
                        source,
                        vault_root,
                        course_id,
                        session,
                        max_pages=max_pages,
                        dpi=dpi,
                        refresh_vision=refresh_vision,
                    )
                )
    return parsed_sources


async def process_source_with_lvm(
    source: Path,
    vault_root: Path,
    course_id: str,
    session: ClientSession,
    max_pages: int,
    dpi: int,
    refresh_vision: bool,
) -> Path:
    if not source.exists():
        raise FileNotFoundError(source)
    if source.suffix.lower() != ".pdf":
        raise RuntimeError(f"PROBLEM: compile-LVM currently supports PDF input directly; convert first: {source}")

    parsed_dir = ensure_dir(vault_root / "parsed" / "lvm" / source_cache_id(source))
    pages_dir = ensure_dir(parsed_dir / "pages")
    page_images = render_pdf_pages(source, pages_dir, dpi=dpi, max_pages=max_pages)
    analyses: list[dict[str, Any]] = []
    for page in page_images:
        analysis_path = parsed_dir / f"page-{page['page']:03d}.json"
        if analysis_path.exists() and not refresh_vision:
            analyses.append(json.loads(analysis_path.read_text(encoding="utf-8")))
            continue
        result = await analyze_slide_page(session, page["image"])
        analysis = {
            "source": str(source),
            "page": page["page"],
            "image": str(page["image"]),
            "analysis": result,
        }
        write_json(analysis_path, analysis)
        analyses.append(analysis)
        print(f"lvm_page={source.name}#{page['page']}")

    write_json(parsed_dir / "page_analysis.json", analyses)
    full_md = render_lvm_markdown(source, analyses)
    (parsed_dir / "full.md").write_text(full_md, encoding="utf-8")
    write_json(
        parsed_dir / "lvm_manifest.json",
        {
            "source": str(source),
            "pages": len(analyses),
            "mcp_server": "@z_ai/mcp-server",
            "tool": "analyze_image",
            "prompt": VISION_PROMPT,
        },
    )
    return parsed_dir


def render_pdf_pages(source: Path, pages_dir: Path, dpi: int, max_pages: int) -> list[dict[str, Any]]:
    document = fitz.open(source)
    count = min(len(document), max_pages) if max_pages else len(document)
    pages: list[dict[str, Any]] = []
    for index in range(count):
        page_no = index + 1
        image_path = pages_dir / f"page-{page_no:03d}.png"
        if not image_path.exists():
            page = document.load_page(index)
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            pixmap.save(image_path)
        pages.append({"page": page_no, "image": image_path})
    document.close()
    return pages


async def analyze_slide_page(session: ClientSession, image_path: Path) -> str:
    result = await session.call_tool(
        "analyze_image",
        {
            "image_source": str(image_path.resolve()),
            "prompt": VISION_PROMPT,
        },
    )
    texts = [item.text for item in result.content if getattr(item, "type", "") == "text"]
    return "\n\n".join(texts).strip()


def render_lvm_markdown(source: Path, analyses: list[dict[str, Any]]) -> str:
    lines = [f"# {source.stem}", ""]
    for item in analyses:
        image_rel = Path(item["image"]).as_posix()
        heading = page_heading(item["page"], str(item["analysis"]))
        lines.extend(
            [
                f"## {heading}",
                "",
                f"![Page {item['page']}]({image_rel})",
                "",
                str(item["analysis"]).strip(),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def page_heading(page: int, analysis: str) -> str:
    for line in analysis.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title and title.lower() not in {"page", f"page {page}", "视觉与排版说明", "教学素材属性"}:
            return f"Page {page}: {title}"
    for line in analysis.splitlines():
        stripped = line.strip().strip("-* ")
        if stripped and len(stripped) <= 60 and not stripped.startswith(("视觉", "页面", "排版")):
            return f"Page {page}: {stripped}"
    return f"Page {page}: slide content"


def source_cache_id(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"{slugify(source.stem)}-{digest.hexdigest()[:12]}"


def vision_env() -> dict[str, str]:
    values = load_env()
    env = os.environ.copy()
    api_key = values.get("Z_AI_API_KEY") or values.get("GLM_API_KEY") or values.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeError("PROBLEM: Z_AI_API_KEY/GLM_API_KEY is missing; vision MCP cannot run")
    env["Z_AI_API_KEY"] = api_key
    env.setdefault("Z_AI_MODE", "ZHIPU")
    env.setdefault("Z_AI_TIMEOUT", values.get("Z_AI_TIMEOUT", "300000"))
    if values.get("Z_AI_VISION_MODEL"):
        env["Z_AI_VISION_MODEL"] = values["Z_AI_VISION_MODEL"]
    return env


if __name__ == "__main__":
    raise SystemExit(main())
