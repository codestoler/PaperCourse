"""Graph node implementations for the local-first compiler."""

from __future__ import annotations

import json
import asyncio
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .io import course_dir, ensure_dir, read_json, slugify, write_json
from .llm import LLMClient
from .runtime import append_runtime_event, runtime_event
from .source_tools import SourceLocator, SourceRevisionTool, stable_source_id
from .state import CourseCompileState
from .vision import VisionImageClient

OCR_PARAGRAPH_MARKERS = "□■▪▫◆◇◻◼"


def _review_feedback_for_prompt(profile: dict[str, Any], max_items: int = 5, max_chars: int = 1800) -> str:
    feedback = profile.get("review_feedback", [])
    if not isinstance(feedback, list) or not feedback:
        return ""
    lines = ["Human review feedback to honor in this rerun:"]
    for item in feedback[-max_items:]:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip()
        node = str(item.get("node", "")).strip()
        target = str(item.get("target_node", "")).strip()
        comment = str(item.get("feedback", "") or item.get("comment", "")).strip()
        lines.append(f"- action={action or 'review'}; node={node or 'unknown'}; target={target or node or 'unknown'}; feedback={comment or '(no comment)'}")
    if len(lines) == 1:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "\n...[truncated]"
    return text + "\n\n"


def _append_external_event(state: CourseCompileState, vault_root: Path | str, stage: str, status: str, **fields: Any) -> None:
    course_path = course_dir(Path(vault_root), state["course_id"])
    event = runtime_event(stage=stage, status=status, **fields)
    append_runtime_event(course_path, event, state.get("compile_profile", {}).get("progress_jsonl"))
    state.setdefault("internal_run_log", []).append({"node": stage, "status": status, **fields})


def parse_sources(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Parse Markdown files into source-backed chunks.

    `course-vault/raw` is reserved for original user inputs such as PDFs.
    Markdown produced by MinerU or other parsers is an intermediate artifact and
    must not be copied back into raw.
    """

    vault = Path(vault_root)
    compile_dir = ensure_dir(course_dir(vault, state["course_id"]) / "parsed_chunks")
    chunks: list[dict[str, Any]] = []

    for source_name in state["source_files"]:
        source_path = Path(source_name)
        if not source_path.exists():
            state["errors"].append({"node": "parse_sources", "message": f"Missing source: {source_name}"})
            continue
        if not source_path.is_dir() and source_path.suffix.lower() not in {".md", ".markdown"}:
            state["errors"].append({"node": "parse_sources", "message": f"Unsupported source type: {source_path.suffix}"})
            continue

        markdown_path = _resolve_markdown_source(source_path)
        text = markdown_path.read_text(encoding="utf-8")
        source_id = _source_id_for_chunks(source_path, markdown_path)
        source_chunks = _split_markdown(text, source_id)
        _attach_image_refs_to_chunks(source_path, markdown_path, source_chunks)
        chunks.extend(source_chunks)
        write_json(compile_dir / f"{slugify(source_path.stem)}.json", source_chunks)

    state["parsed_chunks"] = chunks
    state["next_action"] = "understand_images"
    return state


def understand_images(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Create lightweight, structured image understanding records from parser metadata."""

    course_path = course_dir(Path(vault_root), state["course_id"])
    profile = state.get("compile_profile", {})
    chunk_by_id = {chunk["id"]: chunk for chunk in state.get("parsed_chunks", [])}
    images: list[dict[str, Any]] = []
    seen: set[str] = set()

    for chunk in state.get("parsed_chunks", []):
        for ref in chunk.get("image_refs", []):
            key = str(ref.get("id") or ref.get("path") or ref.get("markdown_path"))
            if not key or key in seen:
                continue
            seen.add(key)
            record = _understand_image_ref(ref, chunk, chunk_by_id)
            images.append(record)

    vision_meta: dict[str, Any] = {"enabled": False, "analyzed": 0, "cache_hits": 0, "cache_misses": 0}
    if profile.get("use_vision_image_understanding"):
        images, vision_meta = _refine_images_with_vision(images, course_path, profile)

    images, formula_meta = _recognize_formula_images(images, course_path, profile)
    formula_records = [image["formula_recognition"] for image in images if image.get("formula_recognition")]
    pending = [
        image
        for image in images
        if image.get("needs_confirmation") or image.get("formula_recognition", {}).get("needs_human_review")
    ]

    image_understanding = {
        "images": images,
        "formula_images": formula_records,
        "pending_confirmation": pending,
        "summary": {
            "total": len(images),
            "recognized": sum(1 for image in images if not image.get("needs_confirmation")),
            "needs_confirmation": len(pending),
            "strategy": "mineru_metadata_and_neighbor_text+vision_mcp" if vision_meta.get("enabled") else "mineru_metadata_and_neighbor_text",
            "vision": vision_meta,
            "formula_recognition": formula_meta,
        },
    }
    state["image_understanding"] = image_understanding
    write_json(course_path / "image_understanding.json", image_understanding)
    (course_path / "image_understanding.md").write_text(_render_image_understanding_markdown(image_understanding), encoding="utf-8")
    formula_report = {"formulas": formula_records, "summary": formula_meta}
    write_json(course_path / "formula_image_recognition.json", formula_report)
    (course_path / "formula_image_recognition.md").write_text(_render_formula_image_recognition_markdown(formula_report), encoding="utf-8")
    state["next_action"] = "build_source_index"
    return state


def build_source_index(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Build compact context packs so long sources can be planned without loading everything at once."""

    profile = state.get("compile_profile", {})
    learn_by_doing = _is_learn_by_doing(profile)
    if not profile.get("use_source_index"):
        state["source_index"] = {}
        state["next_action"] = "synthesize_source_brief"
        return state

    course_path = course_dir(Path(vault_root), state["course_id"])
    valid_chunk_ids = {chunk["id"] for chunk in state["parsed_chunks"]}
    chunk_batches = _chunk_batches_for_index(
        state["parsed_chunks"],
        max_chunks=int(profile.get("source_index_batch_chunks", 32)),
        max_chars=int(profile.get("source_index_batch_chars", 16000)),
    )
    client = LLMClient.from_env() if profile.get("use_llm_source_index", profile.get("use_llm")) else None
    packs: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    metadata: list[dict[str, Any]] = []

    for batch_index, batch in enumerate(chunk_batches, start=1):
        fallback_pack = _fallback_source_index_pack(batch_index, batch, learn_by_doing=learn_by_doing)
        if client is None:
            packs.append(fallback_pack)
            continue

        system = (
            "You are a source-indexing agent for a course compiler. Summarize one bounded context pack. "
            "Preserve exact chunk ids. Do not decide the full course outline here. Return strict JSON only."
        )
        if learn_by_doing:
            system += " For software manuals, extract reusable tasks, runnable workflows, examples, and failure modes for a learn-by-doing tutorial."
        user = (
            "Analyze this context pack and return JSON with this schema:\n"
            "{\"pack\":{\"pack_id\":\"pack-001\",\"title\":\"...\",\"summary\":\"...\","
            "\"key_concepts\":[\"...\"],\"methods\":[{\"name\":\"...\",\"purpose\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
            "\"examples\":[{\"title\":\"...\",\"lesson\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
            "\"tasks\":[{\"title\":\"...\",\"outcome\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
            "\"workflows\":[{\"title\":\"...\",\"steps\":[\"...\"],\"source_chunk_ids\":[\"...\"]}],"
            "\"failure_modes\":[{\"symptom\":\"...\",\"fix\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
            "\"candidate_lessons\":[{\"title\":\"...\",\"reason\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
            "\"source_chunk_ids\":[\"...\"]}}\n\n"
            "Requirements:\n"
            "- This is a map step for long materials such as 200-page books; be compact and source-grounded.\n"
            "- Identify concepts, methods, formulas, examples, and possible lesson topics present in this pack only.\n"
            "- Do not invent or rewrite chunk ids.\n\n"
            f"{_learn_by_doing_source_index_requirements() if learn_by_doing else ''}"
            f"Context pack id: pack-{batch_index:03d}\n"
            f"Source chunks:\n{_chunks_for_context_prompt(batch, max_chars=int(profile.get('source_index_chunk_chars', 760)))}"
        )
        cache_path = _source_index_cache_path(course_path, client, system, user) if client else None
        if cache_path and cache_path.exists() and not profile.get("refresh_source_index"):
            cached = read_json(cache_path)
            pack = cached.get("pack", {})
            cache_hits += 1
            metadata.append(cached.get("metadata", {}))
        else:
            try:
                raw = client.complete_json(system, user)
                pack = _normalize_source_index_pack(raw.get("pack", raw), valid_chunk_ids, fallback_pack)
                cache_misses += 1
                metadata.append(getattr(client, "last_metadata", {}))
                if cache_path:
                    write_json(
                        cache_path,
                        {
                            "pack": pack,
                            "provider": getattr(client, "cache_identity", {}),
                            "metadata": getattr(client, "last_metadata", {}),
                        },
                    )
            except Exception as exc:  # pragma: no cover - depends on external model behavior
                state["errors"].append({"node": "build_source_index", "message": f"LLM source index failed for pack-{batch_index:03d}; using fallback: {exc}"})
                pack = fallback_pack
        packs.append(pack)

    source_index = {
        "pack_count": len(packs),
        "packs": packs,
        "chunk_count": len(state["parsed_chunks"]),
        "context_strategy": "batched_source_index",
    }
    state["source_index"] = source_index
    write_json(course_path / "source_index.json", source_index)
    (course_path / "source_index.md").write_text(_render_source_index_markdown(source_index), encoding="utf-8")
    write_json(
        course_path / "source_index_meta.json",
        {
            "node": "build_source_index",
            "batch_count": len(chunk_batches),
            "local_cache_hits": cache_hits,
            "local_cache_misses": cache_misses,
            "metadata": metadata,
        },
    )
    state["next_action"] = "synthesize_source_brief"
    return state


def synthesize_source_brief(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Create a learning brief that fuses MinerU text and LVM page understanding."""

    profile = state.get("compile_profile", {})
    learn_by_doing = _is_learn_by_doing(profile)
    if not profile.get("use_source_brief"):
        state["source_brief"] = {}
        state["next_action"] = "plan_course"
        return state

    course_path = course_dir(Path(vault_root), state["course_id"])
    valid_chunk_ids = {chunk["id"] for chunk in state["parsed_chunks"]}
    source_context = _source_index_for_prompt(
        state.get("source_index", {}),
        max_chars=int(profile.get("source_brief_index_chars", 18000)),
    )
    if not source_context:
        source_context = "Source chunks:\n" + _chunks_for_brief_digest(state["parsed_chunks"], max_chunks=int(profile.get("max_brief_chunks", 140)))
    system = (
        "You are a study-guide compiler. Fuse OCR/layout text and visual slide descriptions into a concise, "
        "source-grounded teaching brief. Preserve source chunk ids. Return strict JSON only."
    )
    if learn_by_doing:
        system += " For software manuals, build a task-first learn-by-doing brief that connects reference material with examples."
    user = (
        "Create a source teaching brief JSON with this schema:\n"
        "{\"course_title\":\"...\",\"overview\":\"...\",\"key_concepts\":[\"...\"],"
        "\"methods\":[{\"name\":\"...\",\"purpose\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
        "\"examples\":[{\"title\":\"...\",\"lesson\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
        "\"tasks\":[{\"title\":\"...\",\"outcome\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
        "\"workflows\":[{\"title\":\"...\",\"steps\":[\"...\"],\"source_chunk_ids\":[\"...\"]}],"
        "\"failure_modes\":[{\"symptom\":\"...\",\"fix\":\"...\",\"source_chunk_ids\":[\"...\"]}],"
        "\"lesson_notes\":[{\"title\":\"...\",\"source_chunk_ids\":[\"...\"],\"learning_goal\":\"...\","
        "\"explanation\":\"...\",\"example\":\"...\",\"bridge\":\"...\"}]}\n\n"
        "Requirements:\n"
        "- Work like a student asking: 根据课件总结关键概念、方法和例题，形成大纲和讲义.\n"
        "- Combine exact formulas/text from MinerU chunks with visual context from LVM chunks when both exist.\n"
        "- Keep notes easier to understand than the slides: add motivation, transitions, and concrete examples.\n"
        "- Do not invent source ids; each method/example/note should cite existing chunk ids.\n"
        "- When a source index is provided, use it as the compact map of a larger document instead of asking for all raw text.\n\n"
        f"{_learn_by_doing_brief_requirements() if learn_by_doing else ''}"
        f"{source_context}"
    )

    client = LLMClient.from_env() if profile.get("use_llm_brief", profile.get("use_llm")) else None
    cache_path = _source_brief_cache_path(course_path, client, system, user) if client else None
    if cache_path and cache_path.exists() and not profile.get("refresh_source_brief"):
        cached = read_json(cache_path)
        brief = cached.get("source_brief", {})
        cache_status = "hit"
        metadata = cached.get("metadata", {})
    else:
        brief = {}
        metadata = {}
        cache_status = "local_fallback"
        if client is not None:
            try:
                raw = client.complete_json(system, user)
                brief = _normalize_source_brief(raw, valid_chunk_ids)
                metadata = getattr(client, "last_metadata", {})
                cache_status = "miss"
                if cache_path:
                    write_json(
                        cache_path,
                        {
                            "source_brief": brief,
                            "provider": getattr(client, "cache_identity", {}),
                            "metadata": metadata,
                        },
                    )
            except Exception as exc:  # pragma: no cover - depends on external model behavior
                state["errors"].append({"node": "synthesize_source_brief", "message": f"LLM brief failed; using fallback: {exc}"})
        if not brief:
            brief = _fallback_source_brief(state["parsed_chunks"])

    state["source_brief"] = brief
    write_json(course_path / "source_brief.json", brief)
    (course_path / "source_brief.md").write_text(_render_source_brief_markdown(brief), encoding="utf-8")
    write_json(
        course_path / "source_brief_meta.json",
        {
            "node": "synthesize_source_brief",
            "local_cache": cache_status,
            "cache_path": str(cache_path) if cache_path else "",
            "metadata": metadata,
        },
    )
    state["next_action"] = "plan_course"
    return state


def plan_course(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Use an LLM to create a hierarchical course plan from parsed chunks."""

    profile = state.get("compile_profile", {})
    learn_by_doing = _is_learn_by_doing(profile)
    if not profile.get("use_llm"):
        state["course_plan"] = {}
        state["next_action"] = "synthesize_lesson_notes"
        return state

    client = LLMClient.from_env()
    if client is None:
        fallback_plan = _fallback_course_plan_from_source_index(
            state.get("source_index", {}),
            {chunk["id"] for chunk in state["parsed_chunks"]},
            *_target_lesson_bounds(state),
            learn_by_doing=learn_by_doing,
        )
        if fallback_plan.get("sections"):
            state["course_plan"] = fallback_plan
        else:
            state["errors"].append({"node": "plan_course", "message": "LLM configuration is missing; using deterministic fallback"})
            state["course_plan"] = {}
        state["next_action"] = "synthesize_lesson_notes"
        return state

    source_context = _source_index_for_prompt(
        state.get("source_index", {}),
        max_chars=int(profile.get("course_plan_index_chars", 20000)),
    )
    if not source_context:
        source_context = "Source chunks:\n" + _chunks_for_llm_digest(state["parsed_chunks"], max_chunks=int(profile.get("max_llm_chunks", 90)))
    target_min, target_max = _target_lesson_bounds(state)
    if profile.get("use_source_index_plan"):
        valid_chunk_ids = {chunk["id"] for chunk in state["parsed_chunks"]}
        state["course_plan"] = _fallback_course_plan_from_source_brief(
            state.get("source_brief", {}),
            valid_chunk_ids,
            target_max,
            learn_by_doing=learn_by_doing,
        ) or _fallback_course_plan_from_source_index(
            state.get("source_index", {}),
            valid_chunk_ids,
            target_min,
            target_max,
            learn_by_doing=learn_by_doing,
        )
        write_json(
            course_dir(Path(vault_root), state["course_id"]) / "llm_call_meta.json",
            {
                "node": "plan_course",
                "local_cache": "source_index_plan",
                "fallback": "source_index_plan",
                "reason": "compile profile requested source-index planning",
            },
        )
        write_json(course_dir(Path(vault_root), state["course_id"]) / "course_plan.json", state["course_plan"])
        state["next_action"] = "synthesize_lesson_notes"
        return state
    system = (
        "You are a course compiler. Build a learning-oriented hierarchical outline from source chunks. "
        "Do not invent facts. Use only source chunk ids. Prefer semantic grouping over slide-by-slide splitting. "
        "Return strict JSON only."
    )
    if learn_by_doing:
        system += " In learn-by-doing mode, turn software/manual reference material into a task-first tutorial path."
    user = (
        f"{_review_feedback_for_prompt(profile)}"
        "Create a course plan JSON with this schema:\n"
        "{\"sections\":[{\"title\":\"...\",\"lessons\":[{\"title\":\"...\",\"chunk_ids\":[\"...\"],"
        "\"why\":\"...\",\"lesson_type\":\"setup|task|example|troubleshooting|reference\"}]}],\"rejected_titles\":[\"...\"]}\n\n"
        "Requirements:\n"
        "- sections are top-level chapter-like groups.\n"
        "- lessons are medium-grain topics, not individual examples or slides.\n"
        "- every lesson must cite existing chunk_ids.\n"
        "- titles must be specific enough to study independently.\n\n"
        f"- Return between {target_min} and {target_max} lessons based on the source volume; fewer lessons is only acceptable when the source itself is shorter.\n"
        "- Each substantial source file or chapter should usually contain multiple medium-grain lessons.\n"
        "- Cover all source packs or chapters in order; do not stop after early packs when a compact index is provided.\n\n"
        "- If a compact source index is provided, plan from that index and only request raw chunks later for lesson notes.\n\n"
        f"{_learn_by_doing_plan_requirements() if learn_by_doing else ''}"
        f"{_source_brief_for_prompt(state.get('source_brief', {}))}"
        f"{source_context}"
    )
    course_path = course_dir(Path(vault_root), state["course_id"])
    cache_path = _course_plan_cache_path(course_path, client, system, user)
    if cache_path and cache_path.exists() and not profile.get("refresh_llm_plan"):
        cached = read_json(cache_path)
        state["course_plan"] = _repair_course_plan_coverage(
            cached.get("course_plan", {}),
            state.get("source_index", {}),
            {chunk["id"] for chunk in state["parsed_chunks"]},
            target_min,
        )
        write_json(
            course_path / "llm_call_meta.json",
            {
                "node": "plan_course",
                "local_cache": "hit",
                "cache_path": str(cache_path),
                "provider": cached.get("provider", {}),
            },
        )
        write_json(course_path / "course_plan.json", state["course_plan"])
        state["next_action"] = "synthesize_lesson_notes"
        return state

    try:
        plan = client.complete_json(system, user)
        state["course_plan"] = _repair_course_plan_coverage(
            _normalize_course_plan(plan, {chunk["id"] for chunk in state["parsed_chunks"]}),
            state.get("source_index", {}),
            {chunk["id"] for chunk in state["parsed_chunks"]},
            target_min,
        )
        metadata = getattr(client, "last_metadata", {})
        if cache_path:
            write_json(
                cache_path,
                {
                    "course_plan": state["course_plan"],
                    "provider": getattr(client, "cache_identity", {}),
                    "metadata": metadata,
                },
            )
        write_json(
            course_path / "llm_call_meta.json",
            {
                "node": "plan_course",
                "local_cache": "miss",
                "cache_path": str(cache_path) if cache_path else "",
                "provider": getattr(client, "cache_identity", {}),
                "metadata": metadata,
            },
        )
    except Exception as exc:  # pragma: no cover - depends on external model behavior
        valid_chunk_ids = {chunk["id"] for chunk in state["parsed_chunks"]}
        fallback_plan = _fallback_course_plan_from_source_brief(
            state.get("source_brief", {}),
            valid_chunk_ids,
            target_max,
            learn_by_doing=learn_by_doing,
        ) or _fallback_course_plan_from_source_index(
            state.get("source_index", {}),
            valid_chunk_ids,
            target_min,
            target_max,
            learn_by_doing=learn_by_doing,
        )
        if fallback_plan.get("sections"):
            state["course_plan"] = fallback_plan
            write_json(
                course_path / "llm_call_meta.json",
                {
                    "node": "plan_course",
                    "local_cache": "fallback_after_llm_error",
                    "cache_path": str(cache_path) if cache_path else "",
                    "provider": getattr(client, "cache_identity", {}),
                    "error": str(exc),
                    "fallback": "source_index_plan",
                },
            )
        else:
            state["errors"].append({"node": "plan_course", "message": f"LLM plan failed; using fallback: {exc}"})
            state["course_plan"] = {}

    write_json(course_path / "course_plan.json", state["course_plan"])
    state["next_action"] = "synthesize_lesson_notes"
    return state


def synthesize_lesson_notes(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Create lesson-specific teaching notes from the source brief and course plan."""

    profile = state.get("compile_profile", {})
    plan = state.get("course_plan", {})
    if not profile.get("use_lesson_notes") or not plan.get("sections"):
        state["lesson_notes"] = {}
        state["next_action"] = "extract_units"
        return state

    course_path = course_dir(Path(vault_root), state["course_id"])
    valid_chunk_ids = {chunk["id"] for chunk in state["parsed_chunks"]}
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    targets = _planned_lessons_for_notes(plan, chunk_by_id)
    if not targets:
        state["lesson_notes"] = {}
        state["next_action"] = "extract_units"
        return state

    client = LLMClient.from_env() if profile.get("use_llm_lesson_notes") else None
    lesson_notes: dict[str, Any] = {}
    metadata: list[dict[str, Any]] = []
    cache_status = "local_fallback"
    if client is not None:
        lesson_notes, metadata, cache_status = _llm_lesson_notes_in_batches(
            client=client,
            course_path=course_path,
            targets=targets,
            chunk_by_id=chunk_by_id,
            valid_chunk_ids=valid_chunk_ids,
            source_brief=state.get("source_brief", {}),
            profile=profile,
            errors=state["errors"],
        )
    if not lesson_notes:
        lesson_notes = _fallback_lesson_notes(targets, chunk_by_id, state.get("source_brief", {}))

    state["lesson_notes"] = lesson_notes
    write_json(course_path / "lesson_notes.json", lesson_notes)
    (course_path / "lesson_notes.md").write_text(_render_lesson_notes_markdown(lesson_notes), encoding="utf-8")
    write_json(
        course_path / "lesson_notes_meta.json",
        {
            "node": "synthesize_lesson_notes",
            "local_cache": cache_status,
            "metadata": metadata,
        },
    )
    state["next_action"] = "extract_units"
    return state


def _course_plan_cache_path(course_path: Path, client: Any, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"course_plan-{cache_key(system, user)}.json"


def _source_brief_cache_path(course_path: Path, client: Any, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"source_brief-{cache_key(system, user)}.json"


def _lesson_notes_cache_path(course_path: Path, client: Any, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"lesson_notes-{cache_key(system, user)}.json"


def _lesson_body_cache_path(course_path: Path, client: Any, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"lesson_body-{cache_key(system, user)}.json"


def _find_cached_lesson_body_by_id(course_path: Path, lesson_id: str, source_chunk_ids: set[str]) -> dict[str, Any] | None:
    cache_dir = course_path / "llm_cache"
    if not cache_dir.exists():
        return None
    for path in sorted(cache_dir.glob("lesson_body-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            cached = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        body = cached.get("lesson_body", {})
        if body.get("lesson_id") != lesson_id or not body.get("body_markdown"):
            continue
        covered = {str(chunk_id) for chunk_id in body.get("covered_source_chunk_ids", [])}
        if covered and not covered.issubset(source_chunk_ids):
            continue
        return cached
    return None


def _source_index_cache_path(course_path: Path, client: Any, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"source_index-{cache_key(system, user)}.json"


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _course_style(profile: dict[str, Any]) -> str:
    style = str(profile.get("course_style", "standard")).strip().lower().replace("-", "_")
    if style in {"learn_by_doing", "learning_by_doing", "task_first"}:
        return "learn_by_doing"
    return "standard"


def _is_learn_by_doing(profile: dict[str, Any]) -> bool:
    return _course_style(profile) == "learn_by_doing"


def _learn_by_doing_source_index_requirements() -> str:
    return (
        "Learn-by-doing requirements:\n"
        "- Treat the source as a software/manual reference that may be written in feature order.\n"
        "- Extract runnable tasks, setup steps, configuration edits, commands, expected outputs, examples/case studies, and troubleshooting hints.\n"
        "- Candidate lessons should be task-first workflow titles, not bare feature names or isolated examples.\n"
        "- If a feature explanation and an example both appear in this pack, link them through the same task/workflow when supported by chunk ids.\n\n"
    )


def _learn_by_doing_brief_requirements() -> str:
    return (
        "Learn-by-doing requirements:\n"
        "- Reorganize the material as a task-first tutorial for using a software system.\n"
        "- Identify later examples or case studies that should teach earlier feature/reference sections.\n"
        "- Capture setup prerequisites, files to edit, commands to run, expected results, and common mistakes when the source supports them.\n"
        "- Keep this generic: do not rely on course-specific names or hard-coded product knowledge beyond the cited source chunks.\n\n"
    )


def _learn_by_doing_plan_requirements() -> str:
    return (
        "Learn-by-doing planning requirements:\n"
        "- Do not preserve the manual's feature-order table of contents when a task workflow is more teachable.\n"
        "- Build a hands-on path: prepare environment, run a smallest useful task, modify inputs/configuration, inspect results, then expand to richer examples.\n"
        "- Merge feature/reference chunks with later example chunks when together they support one practical lesson.\n"
        "- Avoid lessons that are only 'Example', 'Command list', or a single API/reference feature; make each lesson answer what the learner will do.\n"
        "- Use task-oriented lesson titles such as configuring, running, inspecting, extending, debugging, or validating a workflow.\n\n"
    )


def _learn_by_doing_note_requirements() -> str:
    return (
        "Learn-by-doing note requirements:\n"
        "- For each lesson, state the task, prerequisites/files, operation steps, expected result, why the feature matters, and likely failure modes when supported.\n"
        "- Prefer executable workflow guidance over reference prose. Do not invent missing commands, filenames, outputs, or parameters.\n\n"
    )


def _learn_by_doing_body_requirements() -> str:
    return (
        "Learn-by-doing writing requirements:\n"
        "- Write as a hands-on software tutorial, not a feature reference.\n"
        "- Organize with headings such as 学习目标, 本节任务, 准备与文件, 操作步骤, 预期结果, 背后的功能, 常见错误, 下一步练习.\n"
        "- Combine feature explanations with cited examples or case studies when both are present in the lesson chunks.\n"
        "- Do not invent commands, file names, outputs, constants, configuration values, or screenshots that are not supported by the provided chunks.\n"
        "- Checklist items should be observable actions, such as running a command, editing a file, checking an output, or explaining a failure mode.\n\n"
    )


def _lesson_body_enrichment_requirements(profile: dict[str, Any]) -> str:
    mode = str(profile.get("lesson_body_enrichment", "standard")).strip().lower()
    if mode not in {"constrained", "local", "true", "1"}:
        return ""
    return (
        "Constrained local enrichment requirements:\n"
        "- Detect only local study gaps inside the provided chunks: skipped example algebra, omitted proof bridges, theorem derivation hints, in-class questions, and easily confused or error-prone concepts.\n"
        "- Add short sections only when useful, using headings such as 局部补全, 推导桥接, 易混辨析, 常见错误, or 随堂问题回应.\n"
        "- For examples, fill intermediate algebra or reasoning only when the shown assumptions, formulas, and quantities are already present in the chunks. Label it as 补全推导（依据源材料 + 标准代数步骤）.\n"
        "- For theorems or proofs left as 思考题/证明略, provide a proof bridge or guided sketch, not a new theorem. If assumptions or key equations are missing, say 待源材料确认.\n"
        "- For in-class questions, answer from the chunks plus standard course reasoning; otherwise turn the answer into a short hint.\n"
        "- Identify at most 3 confusing/error-prone points and keep each explanation local to this lesson.\n"
        "- Keep enrichment bounded: at most 3 enrichment items, each about 120-300 Chinese characters, and total enrichment should stay under about 35% of the lesson body.\n"
        "- Do not invent new examples, numbers, constants, formulas, screenshots, or source facts. Do not expand into adjacent lessons or replace the main lesson with supplements.\n"
        "- Also return local_enrichments metadata when enrichment is used: [{\"type\":\"example_steps|proof_bridge|thinking_question|pitfall|concept_disambiguation\",\"title\":\"...\",\"source_chunk_ids\":[\"...\"],\"status\":\"source_supported|standard_derivation|needs_confirmation\",\"content\":\"...\"}].\n\n"
    )


def _lesson_chunks_for_prompt(source_chunk_ids: list[str], chunk_by_id: dict[str, dict[str, Any]], max_chars: int = 1200) -> str:
    lines: list[str] = []
    for chunk_id in source_chunk_ids:
        chunk = chunk_by_id.get(chunk_id)
        if not chunk:
            continue
        content = "\n".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:18])
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "..."
        lines.append(
            f"- id: {chunk['id']}\n"
            f"  source: {chunk.get('source')}\n"
            f"  title: {chunk.get('title')}\n"
            f"  content: {content}"
        )
    return "\n".join(lines)


def _normalize_lesson_body(raw: dict[str, Any], lesson: dict[str, Any], valid_chunk_ids: set[str], max_chars: int) -> dict[str, Any]:
    lesson_id = str(raw.get("lesson_id") or lesson.get("id", "")).strip()
    body = str(raw.get("body_markdown", "")).strip()
    if body:
        body = _trim_lesson_body(body, max_chars)
    checklist = [str(item).strip() for item in raw.get("checklist", []) if str(item).strip()]
    covered = [str(item) for item in raw.get("covered_source_chunk_ids", []) if str(item) in valid_chunk_ids]
    local_enrichments: list[dict[str, Any]] = []
    for item in raw.get("local_enrichments", []):
        if not isinstance(item, dict):
            continue
        source_ids = [str(chunk_id) for chunk_id in item.get("source_chunk_ids", []) if str(chunk_id) in valid_chunk_ids]
        content = str(item.get("content", "")).strip()
        title = str(item.get("title", "")).strip()
        if not content and not title:
            continue
        local_enrichments.append(
            {
                "type": str(item.get("type", "")).strip(),
                "title": title,
                "source_chunk_ids": source_ids,
                "status": str(item.get("status", "")).strip(),
                "content": _short_text(content, 420),
            }
        )
    return {
        "lesson_id": lesson_id,
        "title": str(raw.get("title") or lesson.get("title", "")).strip(),
        "body_markdown": body,
        "checklist": checklist[:8],
        "covered_source_chunk_ids": covered,
        "local_enrichments": local_enrichments[:3],
    }


def _render_lesson_bodies_markdown(lesson_bodies: dict[str, Any]) -> str:
    lines = ["# Lesson Bodies", ""]
    for item in lesson_bodies.get("lesson_bodies", []):
        refs = ", ".join(item.get("covered_source_chunk_ids", []))
        lines.extend(
            [
                f"## {item.get('title')}",
                "",
                f"- Lesson id: {item.get('lesson_id', '')}",
                f"- Sources: {refs}",
                "",
                str(item.get("body_markdown", "")),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _markdown_line_snippet(markdown: str, line: int | None, radius: int = 1) -> str:
    lines = markdown.splitlines()
    if not lines:
        return ""
    if not line or line < 1:
        return _short_text(lines[0], 220)
    start = max(0, line - 1 - radius)
    end = min(len(lines), line + radius)
    return "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))


def _markdown_diagnostic(
    error_type: str,
    reason: str,
    suggestion: str,
    markdown: str,
    *,
    line: int | None = None,
    column: int | None = None,
    end_line: int | None = None,
    end_column: int | None = None,
    source: str = "local_rules",
    severity: str = "error",
) -> dict[str, Any]:
    return {
        "type": error_type,
        "position": {"line": line, "column": column, "end_line": end_line, "end_column": end_column},
        "line": line,
        "column": column,
        "end_line": end_line,
        "end_column": end_column,
        "snippet": _markdown_line_snippet(markdown, line),
        "reason": _short_text(reason, 420),
        "suggestion": _short_text(suggestion, 420),
        "source": source,
        "severity": severity,
    }


def _run_remark_lint(markdown: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run remark-lint when it is locally installed; never install packages here."""

    command: list[str] | None = None
    local_remark = Path("node_modules") / ".bin" / "remark"
    remark_path = str(local_remark) if local_remark.exists() else shutil.which("remark")
    if remark_path:
        command = [remark_path, "--frail", "--use", "remark-preset-lint-recommended"]
    if not command:
        return [], {"available": False, "command": "", "reason": "remark CLI is not installed"}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lesson.md"
        path.write_text(markdown, encoding="utf-8")
        try:
            result = subprocess.run(command + [str(path)], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return [], {"available": False, "command": " ".join(command), "reason": _short_text(str(exc), 420)}

    combined = "\n".join(part for part in [result.stdout, result.stderr] if part)
    metadata = {
        "available": result.returncode in {0, 1},
        "command": " ".join(command),
        "returncode": result.returncode,
        "raw_output": combined[-4000:],
    }
    diagnostics: list[dict[str, Any]] = []
    pattern = re.compile(r"lesson\.md:(\d+):(\d+):\s*(.+?)(?:\s{2,}(.+))?$")
    for raw_line in combined.splitlines():
        match = pattern.search(raw_line.strip())
        if not match:
            continue
        line = int(match.group(1))
        column = int(match.group(2))
        message = match.group(3).strip()
        rule = (match.group(4) or "remark-lint").strip()
        diagnostics.append(
            _markdown_diagnostic(
                f"remark_lint:{rule}",
                message,
                "Revise the Markdown so remark-lint accepts the structure.",
                markdown,
                line=line,
                column=column,
                source="remark-lint",
            )
        )
    if result.returncode not in {0, 1} and not diagnostics:
        metadata["available"] = False
        metadata["reason"] = _short_text(combined or "remark-lint command failed", 420)
    return diagnostics, metadata


def _local_markdown_diagnostics(markdown: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    lines = markdown.splitlines()

    previous_heading_level = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        heading_prefix = re.match(r"^(#{1,6})(.*)$", stripped)
        if heading_prefix and not heading_prefix.group(2).startswith(" "):
            diagnostics.append(
                _markdown_diagnostic(
                    "heading_format",
                    "Markdown heading marker must be followed by one space.",
                    "Use headings like `## Title`, not `##Title`.",
                    markdown,
                    line=index,
                    column=line.find("#") + 1,
                )
            )
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            if not heading.group(2).strip():
                diagnostics.append(
                    _markdown_diagnostic("empty_heading", "Heading text is empty.", "Add descriptive heading text or remove the heading.", markdown, line=index)
                )
            if previous_heading_level and level > previous_heading_level + 1:
                diagnostics.append(
                    _markdown_diagnostic(
                        "heading_level_jump",
                        f"Heading jumps from H{previous_heading_level} to H{level}.",
                        "Use the next heading level or add the missing parent section.",
                        markdown,
                        line=index,
                    )
                )
            previous_heading_level = level

        list_match = re.match(r"^(\s{1,})([-+*]|\d+[.)])\s+", line)
        if list_match and len(list_match.group(1)) % 2 != 0:
            diagnostics.append(
                _markdown_diagnostic(
                    "list_indentation",
                    "Indented list item uses an odd number of leading spaces.",
                    "Use consistent 2-space or 4-space nested list indentation.",
                    markdown,
                    line=index,
                    column=1,
                )
            )

        if re.search(r"\[[^\]]*\]\(\s*\)", line):
            diagnostics.append(
                _markdown_diagnostic("empty_link", "Markdown link URL is empty.", "Remove the link syntax or provide a valid URL/path.", markdown, line=index)
            )
        if re.search(r"\[\s*\]\([^)]+\)", line):
            diagnostics.append(
                _markdown_diagnostic("empty_link_text", "Markdown link text is empty.", "Add visible link text.", markdown, line=index)
            )

    diagnostics.extend(_table_markdown_diagnostics(markdown))
    diagnostics.extend(_code_fence_markdown_diagnostics(markdown))
    diagnostics.extend(_math_markdown_diagnostics(markdown))
    return diagnostics


def _table_markdown_diagnostics(markdown: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    separator_re = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
    for index, line in enumerate(lines[:-1], start=1):
        if "|" not in line or separator_re.match(line):
            continue
        next_line = lines[index] if index < len(lines) else ""
        if not separator_re.match(next_line):
            continue
        header_cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        separator_cells = [cell.strip() for cell in next_line.strip().strip("|").split("|")]
        if len(header_cells) != len(separator_cells):
            diagnostics.append(
                _markdown_diagnostic(
                    "table_column_mismatch",
                    "Table header and separator row have different column counts.",
                    "Make every table row use the same number of pipe-delimited cells.",
                    markdown,
                    line=index,
                )
            )
    return diagnostics


def _code_fence_markdown_diagnostics(markdown: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    open_fence: tuple[str, int] | None = None
    for index, line in enumerate(markdown.splitlines(), start=1):
        match = re.match(r"^\s*(```+|~~~+)(.*)$", line)
        if not match:
            continue
        marker = match.group(1)[:3]
        info = match.group(2).strip()
        if open_fence:
            if marker == open_fence[0]:
                open_fence = None
            continue
        open_fence = (marker, index)
        if not info:
            diagnostics.append(
                _markdown_diagnostic(
                    "code_fence_missing_language",
                    "Code fence has no language/info string.",
                    "Add a language such as `text`, `bash`, `python`, or the relevant format.",
                    markdown,
                    line=index,
                )
            )
    if open_fence:
        diagnostics.append(
            _markdown_diagnostic(
                "unclosed_code_fence",
                "Code fence is opened but not closed.",
                f"Close the fence with `{open_fence[0]}`.",
                markdown,
                line=open_fence[1],
            )
        )
    return diagnostics


def _math_markdown_diagnostics(markdown: str) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    display_count = sum(line.count("$$") for line in lines)
    if display_count % 2:
        first_line = next((index for index, line in enumerate(lines, start=1) if "$$" in line), 1)
        diagnostics.append(
            _markdown_diagnostic(
                "unbalanced_display_math",
                "Display math delimiter `$$` is not balanced.",
                "Use paired `$$` delimiters on their own lines for display formulas.",
                markdown,
                line=first_line,
            )
        )
    bracket_opens = sum(line.count(r"\[") for line in lines)
    bracket_closes = sum(line.count(r"\]") for line in lines)
    if bracket_opens != bracket_closes:
        diagnostics.append(
            _markdown_diagnostic(
                "unbalanced_latex_display_delimiter",
                "LaTeX display delimiters `\\[` and `\\]` are not balanced.",
                "Use matching `\\[` and `\\]` delimiters.",
                markdown,
                line=1,
            )
        )
    in_display = False
    for index, line in enumerate(lines, start=1):
        if line.count("$$") % 2:
            in_display = not in_display
            continue
        if in_display and re.match(r"^\s*[-+*]\s+", line):
            diagnostics.append(
                _markdown_diagnostic(
                    "formula_markdown_mix",
                    "Markdown list marker appears inside display math.",
                    "Keep LaTeX rows inside math syntax, not Markdown list syntax.",
                    markdown,
                    line=index,
                )
            )
        inline_dollars = len(re.findall(r"(?<!\\)\$", line.replace("$$", "")))
        if inline_dollars % 2:
            diagnostics.append(
                _markdown_diagnostic(
                    "unbalanced_inline_math",
                    "Inline math dollar delimiters are not balanced on this line.",
                    "Use paired `$...$` delimiters or escape literal dollar signs.",
                    markdown,
                    line=index,
                )
            )
    begin_envs = re.findall(r"\\begin\{([^}]+)\}", markdown)
    end_envs = re.findall(r"\\end\{([^}]+)\}", markdown)
    for env in sorted(set(begin_envs + end_envs)):
        if begin_envs.count(env) != end_envs.count(env):
            diagnostics.append(
                _markdown_diagnostic(
                    "unbalanced_latex_environment",
                    f"LaTeX environment `{env}` has unmatched begin/end tags.",
                    f"Ensure every `\\begin{{{env}}}` has a matching `\\end{{{env}}}`.",
                    markdown,
                    line=1,
                )
            )
    return diagnostics


def _markdown_syntax_diagnostics(markdown: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    remark_diagnostics, remark_meta = _run_remark_lint(markdown)
    local_diagnostics = _local_markdown_diagnostics(markdown)
    seen: set[tuple[str, int | None, int | None, str]] = set()
    diagnostics: list[dict[str, Any]] = []
    for item in remark_diagnostics + local_diagnostics:
        key = (str(item.get("type")), item.get("line"), item.get("column"), str(item.get("reason")))
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(item)
    return diagnostics, {"remark_lint": remark_meta, "local_rule_count": len(local_diagnostics)}


def _attach_lesson_to_markdown_diagnostics(diagnostics: list[dict[str, Any]], lesson: dict[str, Any]) -> list[dict[str, Any]]:
    lesson_id = str(lesson.get("id", "unknown"))
    title = str(lesson.get("title", ""))
    enriched: list[dict[str, Any]] = []
    for item in diagnostics:
        copied = dict(item)
        copied["lesson_id"] = lesson_id
        copied["lesson_title"] = title
        enriched.append(copied)
    return enriched


def _chunk_batches_for_index(chunks: list[dict[str, Any]], max_chunks: int, max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for chunk in chunks:
        meaningful = _meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))
        chunk_chars = max(1, sum(len(line) for line in meaningful[:12]))
        if current and (len(current) >= max_chunks or current_chars + chunk_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(chunk)
        current_chars += chunk_chars
    if current:
        batches.append(current)
    return batches


def _chunks_for_context_prompt(chunks: list[dict[str, Any]], max_chars: int = 760) -> str:
    lines: list[str] = []
    for chunk in chunks:
        content = "\n".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:12])
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "..."
        lines.append(
            f"- id: {chunk['id']}\n"
            f"  source: {chunk.get('source')}\n"
            f"  title: {chunk.get('title')}\n"
            f"  content: {content}"
        )
    return "\n".join(lines)


def _normalize_source_index_pack(raw: dict[str, Any], valid_chunk_ids: set[str], fallback: dict[str, Any]) -> dict[str, Any]:
    def refs(value: Any) -> list[str]:
        return [str(item) for item in value or [] if str(item) in valid_chunk_ids]

    def text_list(value: Any, limit: int = 8) -> list[str]:
        return [str(item).strip() for item in value or [] if str(item).strip()][:limit]

    pack_ids = refs(raw.get("source_chunk_ids")) or fallback.get("source_chunk_ids", [])
    methods = []
    for item in raw.get("methods", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("name") and source_chunk_ids:
            methods.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "purpose": str(item.get("purpose", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    examples = []
    for item in raw.get("examples", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("title") and source_chunk_ids:
            examples.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "lesson": str(item.get("lesson", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    candidate_lessons = []
    for item in raw.get("candidate_lessons", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("title") and source_chunk_ids:
            candidate_lessons.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    tasks = []
    for item in raw.get("tasks", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        title = str(item.get("title") or item.get("task") or "").strip()
        if title and source_chunk_ids:
            tasks.append(
                {
                    "title": title,
                    "outcome": str(item.get("outcome") or item.get("result") or "").strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    workflows = []
    for item in raw.get("workflows", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        title = str(item.get("title") or "").strip()
        if title and source_chunk_ids:
            workflows.append(
                {
                    "title": title,
                    "steps": text_list(item.get("steps")),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    failure_modes = []
    for item in raw.get("failure_modes", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        symptom = str(item.get("symptom") or item.get("title") or "").strip()
        if symptom and source_chunk_ids:
            failure_modes.append(
                {
                    "symptom": symptom,
                    "fix": str(item.get("fix") or item.get("cause_or_fix") or "").strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )
    return {
        "pack_id": str(raw.get("pack_id") or fallback.get("pack_id", "")).strip(),
        "title": str(raw.get("title") or fallback.get("title", "")).strip(),
        "summary": str(raw.get("summary") or fallback.get("summary", "")).strip(),
        "key_concepts": [str(item).strip() for item in raw.get("key_concepts", []) if str(item).strip()],
        "methods": methods,
        "examples": examples,
        "tasks": tasks,
        "workflows": workflows,
        "failure_modes": failure_modes,
        "candidate_lessons": candidate_lessons,
        "source_chunk_ids": pack_ids,
    }


def _fallback_source_index_pack(batch_index: int, chunks: list[dict[str, Any]], learn_by_doing: bool = False) -> dict[str, Any]:
    title = _choose_group_title(chunks, f"Source Pack {batch_index}")
    combined = "\n\n".join(str(chunk.get("content", "")) for chunk in chunks)
    concepts: list[str] = []
    candidate_lessons: list[dict[str, Any]] = []
    methods: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    failure_modes: list[dict[str, Any]] = []
    for chunk in chunks:
        chunk_title = _clean_lesson_title(str(chunk.get("title", "")))
        meaningful = _meaningful_lines(str(chunk.get("content", "")), chunk_title)
        if not chunk_title or not meaningful:
            continue
        chunk_id = str(chunk.get("id", ""))
        if len(concepts) < 12:
            concepts.append(chunk_title)
        candidate_title = _task_first_title(chunk_title) if learn_by_doing else chunk_title
        candidate_lessons.append({"title": candidate_title, "reason": meaningful[0][:180], "source_chunk_ids": [chunk_id]})
        normalized = _normalize_title(chunk_title)
        if any(token in normalized for token in ("法", "算法", "method", "插值", "逼近", "拟合", "模型")):
            methods.append({"name": chunk_title, "purpose": meaningful[0][:220], "source_chunk_ids": [chunk_id]})
        example_line = _first_example_line(meaningful)
        if example_line:
            examples.append({"title": chunk_title, "lesson": example_line, "source_chunk_ids": [chunk_id]})
        if learn_by_doing and _looks_like_task_material(chunk_title, meaningful):
            tasks.append({"title": candidate_title, "outcome": meaningful[0][:220], "source_chunk_ids": [chunk_id]})
            workflows.append({"title": candidate_title, "steps": meaningful[:5], "source_chunk_ids": [chunk_id]})
        failure = _first_failure_line(meaningful)
        if learn_by_doing and failure:
            failure_modes.append({"symptom": failure, "fix": "", "source_chunk_ids": [chunk_id]})
    return {
        "pack_id": f"pack-{batch_index:03d}",
        "title": title,
        "summary": _summarize_group(combined, title, limit=900),
        "key_concepts": concepts,
        "methods": methods[:8],
        "examples": examples[:8],
        "tasks": tasks[:12],
        "workflows": workflows[:8],
        "failure_modes": failure_modes[:8],
        "candidate_lessons": candidate_lessons[:32],
        "source_chunk_ids": [str(chunk.get("id", "")) for chunk in chunks],
    }


def _render_source_index_markdown(source_index: dict[str, Any]) -> str:
    lines = ["# Source Index", "", f"Context strategy: {source_index.get('context_strategy', '')}", ""]
    for pack in source_index.get("packs", []):
        lines.extend([f"## {pack.get('pack_id')}: {pack.get('title')}", "", str(pack.get("summary", "")), ""])
        if pack.get("key_concepts"):
            lines.append("Concepts: " + ", ".join(pack["key_concepts"]))
            lines.append("")
        if pack.get("candidate_lessons"):
            lines.append("Candidate lessons:")
            for lesson in pack["candidate_lessons"]:
                refs = ", ".join(lesson.get("source_chunk_ids", []))
                lines.append(f"- {lesson.get('title')}: {lesson.get('reason')} [{refs}]")
            lines.append("")
        if pack.get("tasks"):
            lines.append("Tasks:")
            for task in pack["tasks"]:
                refs = ", ".join(task.get("source_chunk_ids", []))
                lines.append(f"- {task.get('title')}: {task.get('outcome')} [{refs}]")
            lines.append("")
        if pack.get("workflows"):
            lines.append("Workflows:")
            for workflow in pack["workflows"]:
                refs = ", ".join(workflow.get("source_chunk_ids", []))
                steps = "; ".join(workflow.get("steps", [])[:4])
                lines.append(f"- {workflow.get('title')}: {steps} [{refs}]")
            lines.append("")
        if pack.get("failure_modes"):
            lines.append("Failure modes:")
            for failure in pack["failure_modes"]:
                refs = ", ".join(failure.get("source_chunk_ids", []))
                lines.append(f"- {failure.get('symptom')}: {failure.get('fix')} [{refs}]")
            lines.append("")
        refs = ", ".join(pack.get("source_chunk_ids", [])[:20])
        lines.append(f"Sources: {refs}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _source_index_for_prompt(source_index: dict[str, Any], max_chars: int = 14000) -> str:
    if not source_index:
        return ""
    packs = list(source_index.get("packs", []))
    lines = _source_index_prompt_lines(packs, summary_limit=900, concept_limit=12, candidate_limit=8, source_limit=40)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text + "\n"

    per_pack_budget = max(420, min(1100, max_chars // max(len(packs), 1) - 80))
    summary_limit = max(120, min(320, per_pack_budget // 3))
    candidate_limit = 4 if per_pack_budget >= 700 else 2
    source_limit = 20 if per_pack_budget >= 700 else 10
    lines = _source_index_prompt_lines(
        packs,
        summary_limit=summary_limit,
        concept_limit=8,
        candidate_limit=candidate_limit,
        source_limit=source_limit,
        compact=True,
    )
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text + "\n"

    lines = _source_index_prompt_lines_terse_candidates(
        packs,
        summary_limit=70,
        concept_limit=4,
        candidate_limit=12,
        source_limit=6,
    )
    return "\n".join(lines) + "\n"


def _source_index_prompt_lines(
    packs: list[dict[str, Any]],
    *,
    summary_limit: int,
    concept_limit: int,
    candidate_limit: int,
    source_limit: int,
    compact: bool = False,
) -> list[str]:
    lines = [
        "Source index context packs:",
        f"- coverage: {len(packs)} packs included; use every pack when planning the course.",
    ]
    if compact:
        lines.append("- compaction: summaries are shortened to preserve tail-pack coverage.")
    for pack in packs:
        summary = _clip_prompt_text(str(pack.get("summary", "")), summary_limit)
        lines.append(
            f"- pack_id: {pack.get('pack_id')}\n"
            f"  title: {pack.get('title')}\n"
            f"  summary: {summary}\n"
            f"  concepts: {', '.join(pack.get('key_concepts', [])[:concept_limit])}\n"
            f"  source_chunk_ids: {', '.join(pack.get('source_chunk_ids', [])[:source_limit])}"
        )
        for lesson in pack.get("candidate_lessons", [])[:candidate_limit]:
            lines.append(
                f"  candidate_lesson: {lesson.get('title')}\n"
                f"    reason: {lesson.get('reason')}\n"
                f"    chunk_ids: {', '.join(lesson.get('source_chunk_ids', [])[:4])}"
            )
        for task in pack.get("tasks", [])[:3]:
            lines.append(
                f"  task: {task.get('title')}\n"
                f"    outcome: {task.get('outcome')}\n"
                f"    chunk_ids: {', '.join(task.get('source_chunk_ids', [])[:4])}"
            )
        for workflow in pack.get("workflows", [])[:2]:
            steps = "; ".join(str(step) for step in workflow.get("steps", [])[:4])
            lines.append(
                f"  workflow: {workflow.get('title')}\n"
                f"    steps: {steps}\n"
                f"    chunk_ids: {', '.join(workflow.get('source_chunk_ids', [])[:4])}"
            )
        for example in pack.get("examples", [])[:2]:
            lines.append(
                f"  example: {example.get('title')}\n"
                f"    lesson: {example.get('lesson')}\n"
                f"    chunk_ids: {', '.join(example.get('source_chunk_ids', [])[:4])}"
            )
    return lines


def _source_index_prompt_lines_terse_candidates(
    packs: list[dict[str, Any]],
    *,
    summary_limit: int,
    concept_limit: int,
    candidate_limit: int,
    source_limit: int,
) -> list[str]:
    lines = [
        "Source index context packs:",
        f"- coverage: {len(packs)} packs included; use every pack when planning the course.",
        "- compaction: candidate lessons are terse title/chunk-id pairs to preserve topic coverage.",
    ]
    for pack in packs:
        candidate_parts: list[str] = []
        for lesson in pack.get("candidate_lessons", [])[:candidate_limit]:
            title = _clip_prompt_text(str(lesson.get("title", "")), 42)
            chunk_ids = ",".join(str(chunk_id) for chunk_id in lesson.get("source_chunk_ids", [])[:3])
            if title and chunk_ids:
                candidate_parts.append(f"{title} [{chunk_ids}]")
        for task in pack.get("tasks", [])[:4]:
            title = _clip_prompt_text(str(task.get("title", "")), 42)
            chunk_ids = ",".join(str(chunk_id) for chunk_id in task.get("source_chunk_ids", [])[:3])
            if title and chunk_ids:
                candidate_parts.append(f"task:{title} [{chunk_ids}]")
        lines.append(
            f"- pack_id: {pack.get('pack_id')}\n"
            f"  title: {pack.get('title')}\n"
            f"  summary: {_clip_prompt_text(str(pack.get('summary', '')), summary_limit)}\n"
            f"  concepts: {', '.join(pack.get('key_concepts', [])[:concept_limit])}\n"
            f"  source_chunk_ids: {', '.join(pack.get('source_chunk_ids', [])[:source_limit])}\n"
            f"  candidate_lessons: {'; '.join(candidate_parts)}"
        )
    return lines


def _clip_prompt_text(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "..."


def _target_lesson_bounds(state: CourseCompileState) -> tuple[int, int]:
    profile = state.get("compile_profile", {})
    if profile.get("target_lesson_count"):
        target = int(profile["target_lesson_count"])
    else:
        chunk_count = len(state.get("parsed_chunks", []))
        source_count = len(state.get("source_files", []))
        by_chunks = max(8, round(chunk_count / 12))
        by_sources = max(0, source_count * 5)
        target = max(by_chunks, by_sources)
    target = max(6, min(64, target))
    lower = max(4, round(target * 0.75))
    upper = max(lower + 2, round(target * 1.25))
    return lower, min(80, upper)


def _chunks_for_brief_digest(chunks: list[dict[str, Any]], max_chunks: int) -> str:
    lines: list[str] = []
    for chunk in chunks[:max_chunks]:
        meaningful = _meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))
        content = " ".join(meaningful[:8])
        if len(content) > 760:
            content = content[:760] + "..."
        lines.append(
            f"- id: {chunk['id']}\n"
            f"  source: {chunk.get('source')}\n"
            f"  title: {chunk.get('title')}\n"
            f"  content: {content}"
        )
    if len(chunks) > max_chunks:
        lines.append(f"- omitted_chunks: {len(chunks) - max_chunks}")
    return "\n".join(lines)


def _normalize_source_brief(raw: dict[str, Any], valid_chunk_ids: set[str]) -> dict[str, Any]:
    def refs(value: Any) -> list[str]:
        return [str(item) for item in value or [] if str(item) in valid_chunk_ids]

    def text_list(value: Any, limit: int = 8) -> list[str]:
        return [str(item).strip() for item in value or [] if str(item).strip()][:limit]

    methods = []
    for item in raw.get("methods", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("name") and source_chunk_ids:
            methods.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "purpose": str(item.get("purpose", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )

    examples = []
    for item in raw.get("examples", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("title") and source_chunk_ids:
            examples.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "lesson": str(item.get("lesson", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )

    tasks = []
    for item in raw.get("tasks", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        title = str(item.get("title") or item.get("task") or "").strip()
        if title and source_chunk_ids:
            tasks.append(
                {
                    "title": title,
                    "outcome": str(item.get("outcome") or item.get("result") or "").strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )

    workflows = []
    for item in raw.get("workflows", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        title = str(item.get("title") or "").strip()
        if title and source_chunk_ids:
            workflows.append(
                {
                    "title": title,
                    "steps": text_list(item.get("steps")),
                    "source_chunk_ids": source_chunk_ids,
                }
            )

    failure_modes = []
    for item in raw.get("failure_modes", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        symptom = str(item.get("symptom") or item.get("title") or "").strip()
        if symptom and source_chunk_ids:
            failure_modes.append(
                {
                    "symptom": symptom,
                    "fix": str(item.get("fix") or item.get("cause_or_fix") or "").strip(),
                    "source_chunk_ids": source_chunk_ids,
                }
            )

    lesson_notes = []
    for item in raw.get("lesson_notes", []):
        source_chunk_ids = refs(item.get("source_chunk_ids"))
        if item.get("title") and source_chunk_ids:
            lesson_notes.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "source_chunk_ids": source_chunk_ids,
                    "learning_goal": str(item.get("learning_goal", "")).strip(),
                    "explanation": str(item.get("explanation", "")).strip(),
                    "example": str(item.get("example", "")).strip(),
                    "bridge": str(item.get("bridge", "")).strip(),
                }
            )

    return {
        "course_title": str(raw.get("course_title", "")).strip(),
        "overview": str(raw.get("overview", "")).strip(),
        "key_concepts": [str(item).strip() for item in raw.get("key_concepts", []) if str(item).strip()],
        "methods": methods,
        "examples": examples,
        "tasks": tasks,
        "workflows": workflows,
        "failure_modes": failure_modes,
        "lesson_notes": lesson_notes,
    }


def _planned_lessons_for_notes(plan: dict[str, Any], chunk_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for section in plan.get("sections", []):
        section_title = str(section.get("title", "")).strip()
        for lesson in section.get("lessons", []):
            chunk_ids = [str(chunk_id) for chunk_id in lesson.get("chunk_ids", []) if str(chunk_id) in chunk_by_id]
            title = str(lesson.get("title", "")).strip()
            if not title or not chunk_ids:
                continue
            targets.append(
                {
                    "lesson_id": f"planned-{len(targets) + 1:03d}",
                    "section_title": section_title,
                    "title": title,
                    "lesson_type": _normalize_lesson_type(str(lesson.get("lesson_type", "")).strip()),
                    "source_chunk_ids": chunk_ids,
                    "chunks": [chunk_by_id[chunk_id] for chunk_id in chunk_ids],
                }
            )
    return targets


def _planned_lessons_for_prompt(targets: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for target in targets:
        lines.append(
            f"- lesson_id: {target['lesson_id']}\n"
            f"  section: {target.get('section_title', '')}\n"
            f"  title: {target['title']}\n"
            f"  lesson_type: {target.get('lesson_type', '')}\n"
            f"  chunk_ids: {', '.join(target.get('source_chunk_ids', []))}"
        )
        for chunk in target.get("chunks", [])[:8]:
            content = " ".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:5])
            if len(content) > 520:
                content = content[:520] + "..."
            lines.append(
                f"  - id: {chunk['id']}\n"
                f"    source: {chunk.get('source')}\n"
                f"    title: {chunk.get('title')}\n"
                f"    content: {content}"
            )
    return "\n".join(lines)


def _llm_lesson_notes_in_batches(
    client: Any,
    course_path: Path,
    targets: list[dict[str, Any]],
    chunk_by_id: dict[str, dict[str, Any]],
    valid_chunk_ids: set[str],
    source_brief: dict[str, Any],
    profile: dict[str, Any],
    errors: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    notes: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    batches = _lesson_note_batches(
        targets,
        max_lessons=int(profile.get("lesson_note_batch_lessons", 4)),
        max_chars=int(profile.get("lesson_note_batch_chars", 18000)),
    )
    for batch_index, batch in enumerate(batches, start=1):
        system = (
            "You are a course-note compiler. Turn a source-grounded course plan into detailed, lesson-specific study notes. "
            "Use only cited source chunk ids. Return strict JSON only."
        )
        user = (
            "Create lesson notes JSON with this schema:\n"
            "{\"lesson_notes\":[{\"lesson_id\":\"planned-001\",\"title\":\"...\",\"source_chunk_ids\":[\"...\"],"
            "\"learning_goal\":\"...\",\"explanation\":\"...\",\"example\":\"...\",\"bridge\":\"...\","
            "\"task\":\"...\",\"steps\":[\"...\"],\"expected_result\":\"...\",\"failure_modes\":[\"...\"]}]}\n\n"
            "Requirements:\n"
            "- Return one note for each planned lesson id listed in this bounded batch only.\n"
            "- Keep every explanation tightly grounded in its planned source chunks and the source teaching brief.\n"
            "- Make the notes useful for real study, not just outline review: explain motivation, definitions, formulas, method steps, and the role of examples.\n"
            "- Aim for 500-900 Chinese characters in each explanation when the source chunks contain enough substance.\n"
            "- Add concrete examples and transitions only when they clarify the cited material.\n"
            "- Do not invent source ids or course-specific facts not supported by the chunks.\n"
            "- This is a retrieval step: use only the lesson-local chunks below, not the whole document.\n\n"
            f"{_learn_by_doing_note_requirements() if _is_learn_by_doing(profile) else ''}"
            f"{_source_brief_for_prompt(source_brief)}"
            f"Lesson batch {batch_index}/{len(batches)}:\n{_planned_lessons_for_prompt(batch)}"
        )
        cache_path = _lesson_notes_cache_path(course_path, client, system, user)
        if cache_path and cache_path.exists() and not profile.get("refresh_lesson_notes"):
            cached = read_json(cache_path)
            normalized = cached.get("lesson_notes", {})
            cache_hits += 1
            metadata.append(cached.get("metadata", {}))
        else:
            try:
                raw = client.complete_json(system, user)
                normalized = _normalize_lesson_notes(raw, batch, chunk_by_id, valid_chunk_ids, source_brief)
                cache_misses += 1
                metadata.append(getattr(client, "last_metadata", {}))
                if cache_path:
                    write_json(
                        cache_path,
                        {
                            "lesson_notes": normalized,
                            "provider": getattr(client, "cache_identity", {}),
                            "metadata": getattr(client, "last_metadata", {}),
                        },
                    )
            except Exception as exc:  # pragma: no cover - depends on external model behavior
                errors.append({"node": "synthesize_lesson_notes", "message": f"LLM lesson notes failed for batch {batch_index}; using fallback: {exc}"})
                normalized = _fallback_lesson_notes(batch, chunk_by_id, source_brief)
        notes.extend(normalized.get("lesson_notes", []))
    if not notes:
        return {}, metadata, "local_fallback"
    return {"lesson_notes": notes}, [{"cache_hits": cache_hits, "cache_misses": cache_misses, "calls": metadata}], "batched"


def _lesson_note_batches(targets: list[dict[str, Any]], max_lessons: int, max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for target in targets:
        target_chars = 0
        for chunk in target.get("chunks", []):
            target_chars += len(" ".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:12]))
        if current and (len(current) >= max_lessons or current_chars + target_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += target_chars
    if current:
        batches.append(current)
    return batches


def _normalize_lesson_notes(
    raw: dict[str, Any],
    targets: list[dict[str, Any]],
    chunk_by_id: dict[str, dict[str, Any]],
    valid_chunk_ids: set[str],
    source_brief: dict[str, Any],
) -> dict[str, Any]:
    target_by_id = {target["lesson_id"]: target for target in targets}
    target_by_title = {_normalize_title(str(target["title"])): target for target in targets}
    notes: list[dict[str, Any]] = []
    covered: set[str] = set()

    for item in raw.get("lesson_notes", []):
        target = target_by_id.get(str(item.get("lesson_id", "")).strip())
        if target is None:
            target = target_by_title.get(_normalize_title(str(item.get("title", ""))))
        if target is None or target["lesson_id"] in covered:
            continue
        allowed_ids = [chunk_id for chunk_id in target.get("source_chunk_ids", []) if chunk_id in valid_chunk_ids]
        cited_ids = [str(chunk_id) for chunk_id in item.get("source_chunk_ids", []) if str(chunk_id) in allowed_ids]
        if not cited_ids:
            cited_ids = allowed_ids[:8]
        if not cited_ids:
            continue
        notes.append(
            {
                "lesson_id": target["lesson_id"],
                "title": str(item.get("title") or target["title"]).strip(),
                "source_chunk_ids": cited_ids,
                "learning_goal": str(item.get("learning_goal", "")).strip(),
                "explanation": str(item.get("explanation", "")).strip(),
                "example": str(item.get("example", "")).strip(),
                "bridge": str(item.get("bridge", "")).strip(),
                "task": str(item.get("task", "")).strip(),
                "steps": [str(step).strip() for step in item.get("steps", []) if str(step).strip()][:10],
                "expected_result": str(item.get("expected_result", "")).strip(),
                "failure_modes": [str(mode).strip() for mode in item.get("failure_modes", []) if str(mode).strip()][:8],
            }
        )
        covered.add(target["lesson_id"])

    missing = [target for target in targets if target["lesson_id"] not in covered]
    if missing:
        fallback = _fallback_lesson_notes(missing, chunk_by_id, source_brief)
        notes.extend(fallback.get("lesson_notes", []))

    return {"lesson_notes": notes}


def _fallback_lesson_notes(
    targets: list[dict[str, Any]],
    chunk_by_id: dict[str, dict[str, Any]],
    source_brief: dict[str, Any],
) -> dict[str, Any]:
    notes: list[dict[str, Any]] = []
    for target in targets:
        title = str(target.get("title", "")).strip()
        source_chunk_ids = [chunk_id for chunk_id in target.get("source_chunk_ids", []) if chunk_id in chunk_by_id]
        combined = "\n\n".join(str(chunk_by_id[chunk_id].get("content", "")).strip() for chunk_id in source_chunk_ids)
        brief_notes = _teaching_notes_for_unit(title, source_chunk_ids, source_brief)
        meaningful = _meaningful_lines(combined, title)
        summary = _summarize_group(combined, title)
        notes.append(
            {
                "lesson_id": target.get("lesson_id", ""),
                "title": title,
                "source_chunk_ids": source_chunk_ids[:8],
                "learning_goal": brief_notes.get("learning_goal") or f"理解 {title} 的核心问题、方法步骤和适用条件。",
                "explanation": brief_notes.get("explanation") or summary,
                "example": brief_notes.get("example") or _first_example_line(meaningful),
                "bridge": brief_notes.get("bridge", ""),
                "task": brief_notes.get("task", ""),
                "steps": brief_notes.get("steps", []),
                "expected_result": brief_notes.get("expected_result", ""),
                "failure_modes": brief_notes.get("failure_modes", []),
            }
        )
    return {"lesson_notes": notes}


def _fallback_source_brief(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    lesson_notes: list[dict[str, Any]] = []
    concepts: list[str] = []
    examples: list[dict[str, Any]] = []
    methods: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    failure_modes: list[dict[str, Any]] = []
    for chunk in chunks:
        title = _clean_lesson_title(str(chunk.get("title", "")))
        meaningful = _meaningful_lines(str(chunk.get("content", "")), title)
        if not title or not meaningful:
            continue
        chunk_id = str(chunk.get("id", ""))
        note = {
            "title": title,
            "source_chunk_ids": [chunk_id],
            "learning_goal": f"理解 {title} 的问题背景、核心定义和使用条件。",
            "explanation": " ".join(meaningful[:4])[:520],
            "example": _first_example_line(meaningful),
            "bridge": "",
        }
        lesson_notes.append(note)
        if len(concepts) < 20:
            concepts.append(title)
        normalized = _normalize_title(title)
        if any(token in normalized for token in ("法", "算法", "method", "插值", "逼近", "拟合")):
            methods.append({"name": title, "purpose": meaningful[0][:240], "source_chunk_ids": [chunk_id]})
        if note["example"]:
            examples.append({"title": title, "lesson": note["example"], "source_chunk_ids": [chunk_id]})
        if _looks_like_task_material(title, meaningful):
            task_title = _task_first_title(title)
            tasks.append({"title": task_title, "outcome": meaningful[0][:220], "source_chunk_ids": [chunk_id]})
            workflows.append({"title": task_title, "steps": meaningful[:5], "source_chunk_ids": [chunk_id]})
        failure = _first_failure_line(meaningful)
        if failure:
            failure_modes.append({"symptom": failure, "fix": "", "source_chunk_ids": [chunk_id]})
        if len(lesson_notes) >= 40:
            break
    return {
        "course_title": "",
        "overview": "根据源材料自动整理出的学习 brief，用于把原始课件转为更连贯的课程讲义。",
        "key_concepts": concepts,
        "methods": methods[:16],
        "examples": examples[:16],
        "tasks": tasks[:24],
        "workflows": workflows[:16],
        "failure_modes": failure_modes[:16],
        "lesson_notes": lesson_notes,
    }


def _teaching_notes_for_unit(title: str, source_chunk_ids: list[str], brief: dict[str, Any]) -> dict[str, str]:
    if not brief:
        return {}
    source_set = set(source_chunk_ids)
    title_key = _normalize_title(title)
    best: dict[str, Any] | None = None
    best_score = 0
    for note in brief.get("lesson_notes", []):
        note_refs = set(note.get("source_chunk_ids", []))
        note_key = _normalize_title(str(note.get("title", "")))
        score = len(source_set & note_refs) * 4
        if title_key and note_key and (title_key in note_key or note_key in title_key):
            score += 2
        if score > best_score:
            best = note
            best_score = score
    if not best:
        return {}
    return {
        "learning_goal": str(best.get("learning_goal", "")).strip(),
        "explanation": str(best.get("explanation", "")).strip(),
        "example": str(best.get("example", "")).strip(),
        "bridge": str(best.get("bridge", "")).strip(),
        "task": str(best.get("task", "")).strip(),
        "steps": [str(step).strip() for step in best.get("steps", []) if str(step).strip()],
        "expected_result": str(best.get("expected_result", "")).strip(),
        "failure_modes": [str(mode).strip() for mode in best.get("failure_modes", []) if str(mode).strip()],
    }


def _first_example_line(lines: list[str]) -> str:
    for line in lines:
        if any(token in line for token in ("例", "例如", "example", "应用", "题")):
            return line[:260]
    return ""


def _task_first_title(title: str) -> str:
    cleaned = title.strip()
    normalized = _normalize_title(cleaned)
    if normalized.startswith(_normalize_title("动手完成")):
        return cleaned
    task_verbs = (
        "run",
        "running",
        "configure",
        "configuring",
        "setup",
        "build",
        "create",
        "edit",
        "install",
        "debug",
        "validate",
        "inspect",
        "使用",
        "运行",
        "配置",
        "创建",
        "构建",
        "检查",
        "调试",
    )
    if any(normalized.startswith(_normalize_title(verb)) for verb in task_verbs):
        return cleaned
    return f"动手完成：{cleaned}"


def _looks_like_task_material(title: str, lines: list[str]) -> bool:
    haystack = _normalize_title(" ".join([title, *lines[:8]]))
    task_tokens = (
        "run",
        "running",
        "setup",
        "configure",
        "configuration",
        "build",
        "makefile",
        "command",
        "parameter",
        "install",
        "create",
        "edit",
        "output",
        "运行",
        "配置",
        "安装",
        "创建",
        "命令",
        "参数",
        "输出",
        "文件",
    )
    return any(_normalize_title(token) in haystack for token in task_tokens)


def _first_failure_line(lines: list[str]) -> str:
    for line in lines:
        lowered = line.lower()
        if any(token in lowered for token in ("error", "failed", "fails", "warning", "known issue", "troubleshoot")):
            return line[:260]
        if any(token in line for token in ("错误", "失败", "警告", "故障", "问题")):
            return line[:260]
    return ""


def _render_source_brief_markdown(brief: dict[str, Any]) -> str:
    lines = [f"# {brief.get('course_title') or 'Source Teaching Brief'}", ""]
    if brief.get("overview"):
        lines.extend(["## Overview", "", str(brief["overview"]), ""])
    if brief.get("key_concepts"):
        lines.extend(["## Key Concepts", ""])
        lines.extend(f"- {item}" for item in brief["key_concepts"])
        lines.append("")
    if brief.get("methods"):
        lines.extend(["## Methods", ""])
        for method in brief["methods"]:
            refs = ", ".join(method.get("source_chunk_ids", []))
            lines.append(f"- **{method.get('name')}**: {method.get('purpose')} [{refs}]")
        lines.append("")
    if brief.get("examples"):
        lines.extend(["## Examples", ""])
        for example in brief["examples"]:
            refs = ", ".join(example.get("source_chunk_ids", []))
            lines.append(f"- **{example.get('title')}**: {example.get('lesson')} [{refs}]")
        lines.append("")
    if brief.get("tasks"):
        lines.extend(["## Tasks", ""])
        for task in brief["tasks"]:
            refs = ", ".join(task.get("source_chunk_ids", []))
            lines.append(f"- **{task.get('title')}**: {task.get('outcome')} [{refs}]")
        lines.append("")
    if brief.get("workflows"):
        lines.extend(["## Workflows", ""])
        for workflow in brief["workflows"]:
            refs = ", ".join(workflow.get("source_chunk_ids", []))
            steps = "; ".join(workflow.get("steps", [])[:6])
            lines.append(f"- **{workflow.get('title')}**: {steps} [{refs}]")
        lines.append("")
    if brief.get("failure_modes"):
        lines.extend(["## Failure Modes", ""])
        for failure in brief["failure_modes"]:
            refs = ", ".join(failure.get("source_chunk_ids", []))
            lines.append(f"- **{failure.get('symptom')}**: {failure.get('fix')} [{refs}]")
        lines.append("")
    if brief.get("lesson_notes"):
        lines.extend(["## Lesson Notes", ""])
        for note in brief["lesson_notes"]:
            refs = ", ".join(note.get("source_chunk_ids", []))
            lines.extend(
                [
                    f"### {note.get('title')}",
                    "",
                    f"- Goal: {note.get('learning_goal')}",
                    f"- Explanation: {note.get('explanation')}",
                    f"- Example: {note.get('example')}",
                    f"- Bridge: {note.get('bridge')}",
                    f"- Sources: {refs}",
                    "",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def _render_lesson_notes_markdown(lesson_notes: dict[str, Any]) -> str:
    lines = ["# Lesson Teaching Notes", ""]
    for note in lesson_notes.get("lesson_notes", []):
        refs = ", ".join(note.get("source_chunk_ids", []))
        lines.extend(
            [
                f"## {note.get('title')}",
                "",
                f"- Lesson id: {note.get('lesson_id', '')}",
                f"- Goal: {note.get('learning_goal', '')}",
                f"- Explanation: {note.get('explanation', '')}",
                f"- Example: {note.get('example', '')}",
                f"- Bridge: {note.get('bridge', '')}",
                f"- Sources: {refs}",
                "",
            ]
        )
        if note.get("task"):
            lines.extend([f"- Task: {note.get('task')}", ""])
        if note.get("steps"):
            lines.append("Steps:")
            lines.extend(f"- {step}" for step in note.get("steps", []))
            lines.append("")
        if note.get("expected_result"):
            lines.extend([f"- Expected result: {note.get('expected_result')}", ""])
        if note.get("failure_modes"):
            lines.append("Failure modes:")
            lines.extend(f"- {item}" for item in note.get("failure_modes", []))
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _source_brief_for_prompt(brief: dict[str, Any]) -> str:
    if not brief:
        return ""
    return "Source teaching brief:\n" + _render_source_brief_markdown(brief)[:8000] + "\n"


def _resolve_markdown_source(source_path: Path) -> Path:
    if source_path.is_dir():
        full_md = source_path / "full.md"
        if full_md.exists():
            return full_md
        markdown_files = sorted(source_path.glob("*.md"))
        if markdown_files:
            return markdown_files[0]
        raise FileNotFoundError(f"No Markdown file found in parsed source directory: {source_path}")
    return source_path


def _source_id_for_chunks(source_path: Path, markdown_path: Path) -> str:
    if source_path.is_dir():
        return f"{source_path.name}/full.md"
    return source_path.name


def _split_markdown(text: str, source_name: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current_title = Path(source_name).stem
    current_lines: list[str] = []
    start_line = 1
    source_slug = slugify(Path(source_name).with_suffix("").as_posix())

    def flush(end_line: int) -> None:
        content = "\n".join(current_lines).strip()
        if content:
            index = len(chunks) + 1
            chunks.append(
                {
                    "id": f"{source_slug}-chunk-{index}",
                    "source": source_name,
                    "source_file": source_name,
                    "title": current_title,
                    "content": content,
                    "start_line": start_line,
                    "end_line": end_line,
                    "page": None,
                    "block_id": f"{source_slug}-chunk-{index}",
                    "bbox": [],
                    "source_order": index,
                }
            )

    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.startswith("#"):
            flush(line_no - 1)
            current_title = line.lstrip("#").strip() or current_title
            current_lines = [line]
            start_line = line_no
        else:
            current_lines.append(line)

    flush(len(text.splitlines()))
    return chunks


def _attach_image_refs_to_chunks(source_path: Path, markdown_path: Path, chunks: list[dict[str, Any]]) -> None:
    if not chunks:
        return
    text = markdown_path.read_text(encoding="utf-8")
    metadata_by_path = _mineru_image_metadata(source_path if source_path.is_dir() else markdown_path.parent)
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", line):
            raw_path = match.group(2).strip()
            if not raw_path or raw_path.startswith(("http://", "https://", "data:")):
                continue
            if _should_skip_image_ref(markdown_path, raw_path):
                continue
            image_path = _resolve_image_ref_path(markdown_path, raw_path)
            key = _image_path_key(raw_path)
            metadata = metadata_by_path.get(key, {})
            ref = {
                "id": f"{slugify(Path(raw_path).stem)}-{line_no}",
                "alt": match.group(1).strip(),
                "markdown_path": raw_path,
                "path": str(image_path),
                "asset_url": _asset_url_for_path(image_path),
                "line": line_no,
                "page_idx": metadata.get("page_idx"),
                "bbox": metadata.get("bbox", []),
                "mineru_type": metadata.get("type", ""),
                "mineru_sub_type": metadata.get("sub_type", ""),
                "mineru_content": metadata.get("content", ""),
                "caption": metadata.get("caption", []),
                "footnote": metadata.get("footnote", []),
                "page_screenshot_path": _page_screenshot_for_image(markdown_path.parent, metadata.get("page_idx")),
            }
            target = _chunk_for_line(chunks, line_no)
            target.setdefault("image_refs", []).append(ref)


def _chunk_for_line(chunks: list[dict[str, Any]], line_no: int) -> dict[str, Any]:
    for chunk in chunks:
        if int(chunk.get("start_line", 0)) <= line_no <= int(chunk.get("end_line", 0)):
            return chunk
    return chunks[-1]


def _should_skip_image_ref(markdown_path: Path, raw_path: str) -> bool:
    lowered = raw_path.lower()
    parent_parts = {part.lower() for part in markdown_path.parent.parts}
    if "lvm" in parent_parts and re.search(r"(^|/)pages/page-\d+\.(png|jpg|jpeg)$", lowered):
        return True
    return False


def _resolve_image_ref_path(markdown_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate.resolve()
    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (markdown_path.parent / candidate).resolve()


def _page_screenshot_for_image(source_dir: Path, page_idx: Any) -> str:
    try:
        page_number = int(page_idx) + 1
    except (TypeError, ValueError):
        return ""
    candidates = [
        source_dir / "pages" / f"page-{page_number:03d}.png",
        source_dir / "pages" / f"page-{page_number:03d}.jpg",
        source_dir / "pages" / f"page-{page_number:03d}.jpeg",
        source_dir / f"page-{page_number:03d}.png",
        source_dir / f"page-{page_number:03d}.jpg",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return ""


def _mineru_image_metadata(source_dir: Path) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    if not source_dir.exists():
        return metadata
    for path in sorted(source_dir.glob("*_content_list*.json")):
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for item in _flatten_mineru_items(data):
            img_path = _mineru_image_path(item)
            if not img_path:
                continue
            previous = metadata.get(_image_path_key(img_path), {})
            metadata[_image_path_key(img_path)] = {
                "type": str(item.get("type", "")) or previous.get("type", ""),
                "sub_type": str(item.get("sub_type", "")) or previous.get("sub_type", ""),
                "content": _mineru_item_text(item) or previous.get("content", ""),
                "caption": _mineru_caption(item) or previous.get("caption", []),
                "footnote": _mineru_footnote(item) or previous.get("footnote", []),
                "bbox": item.get("bbox", []) or previous.get("bbox", []),
                "page_idx": item.get("page_idx") if item.get("page_idx") is not None else previous.get("page_idx"),
            }
    return metadata


def _flatten_mineru_items(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("type") or value.get("img_path") or value.get("image_path"):
            items.append(value)
        for child in value.values():
            items.extend(_flatten_mineru_items(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_flatten_mineru_items(child))
    return items


def _mineru_image_path(item: dict[str, Any]) -> str:
    if item.get("img_path"):
        return str(item["img_path"])
    if item.get("image_path"):
        return str(item["image_path"])
    content = item.get("content", {})
    if isinstance(content, dict):
        source = content.get("image_source", {})
        if isinstance(source, dict) and source.get("path"):
            return str(source["path"])
    return ""


def _mineru_item_text(item: dict[str, Any]) -> str:
    content = item.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if isinstance(content.get("content"), str):
            return str(content["content"]).strip()
        return _text_from_nested_content(content)
    return ""


def _text_from_nested_content(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"image_source"}:
                continue
            parts.extend(_text_from_nested_content(child).splitlines())
    elif isinstance(value, list):
        for child in value:
            parts.extend(_text_from_nested_content(child).splitlines())
    elif isinstance(value, str):
        parts.append(value)
    return "\n".join(part for part in parts if part).strip()


def _mineru_caption(item: dict[str, Any]) -> list[str]:
    for key in ("image_caption", "chart_caption", "table_caption"):
        if key in item:
            return _caption_values(item.get(key, []))
    content = item.get("content", {})
    if isinstance(content, dict):
        for key in ("image_caption", "chart_caption", "table_caption"):
            if key in content:
                return _caption_values(content.get(key, []))
    return []


def _mineru_footnote(item: dict[str, Any]) -> list[str]:
    for key in ("image_footnote", "chart_footnote", "table_footnote"):
        if key in item:
            return _caption_values(item.get(key, []))
    content = item.get("content", {})
    if isinstance(content, dict):
        for key in ("image_footnote", "chart_footnote", "table_footnote"):
            if key in content:
                return _caption_values(content.get(key, []))
    return []


def _caption_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, list):
        iterable = value
    else:
        iterable = [value]
    for item in iterable:
        text = _text_from_nested_content(item) if isinstance(item, (dict, list)) else str(item).strip()
        if text:
            values.append(text)
    return values


def _image_path_key(value: str) -> str:
    return Path(value).name.lower()


def _asset_url_for_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(Path.cwd().resolve())
    except ValueError:
        return resolved.as_uri()
    return "/api/assets/" + quote(rel.as_posix(), safe="/")


def _understand_image_ref(ref: dict[str, Any], chunk: dict[str, Any], chunk_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    context = _image_context_text(ref, chunk)
    image_type = _classify_image_type(ref, context)
    confidence = _image_confidence(ref, context, image_type)
    needs_confirmation = confidence < 0.45 or image_type == "unknown"
    summary = _image_summary(ref, context, image_type, needs_confirmation)
    caption = _image_caption(ref, summary, needs_confirmation)
    page_context = _image_page_context(ref, chunk, chunk_by_id)
    content_summary = _short_text(str(ref.get("mineru_content") or summary or page_context).strip(), 420)
    preserve_original_image = image_type != "formula_image" or needs_confirmation
    return {
        "id": str(ref.get("id", "")),
        "image_type": image_type,
        "content_summary": content_summary,
        "source_chunk_id": chunk.get("id", ""),
        "source": chunk.get("source", ""),
        "path": ref.get("path", ""),
        "asset_url": ref.get("asset_url", ""),
        "markdown_path": ref.get("markdown_path", ""),
        "page_idx": ref.get("page_idx"),
        "bbox": ref.get("bbox", []),
        "mineru_type": ref.get("mineru_type", ""),
        "mineru_sub_type": ref.get("mineru_sub_type", ""),
        "mineru_content": ref.get("mineru_content", ""),
        "page_screenshot_path": ref.get("page_screenshot_path", ""),
        "neighbor_text": _short_text(context, 1600),
        "page_context": _short_text(page_context, 2400),
        "associated_knowledge_points": _image_knowledge_points(chunk, context),
        "summary": summary,
        "suggested_lesson_title": chunk.get("title", ""),
        "suggested_insert_after": _suggest_image_insert_anchor(chunk, context),
        "suggested_insert_position": "pending_confirmation" if needs_confirmation else "after_source_block",
        "preserve_original_image": preserve_original_image,
        "needs_caption": True,
        "caption": caption,
        "needs_confirmation": needs_confirmation,
        "confidence": confidence,
        "reason": _image_reason(ref, image_type, confidence),
        "understanding_source": "mineru_heuristic",
    }


def _refine_images_with_vision(
    images: list[dict[str, Any]],
    course_path: Path,
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = VisionImageClient.from_env()
    mode = str(profile.get("image_vision_mode", "uncertain")).strip().lower()
    max_images = max(0, int(profile.get("image_vision_max_images", 12)))
    refresh = bool(profile.get("refresh_image_vision"))
    if max_images <= 0:
        return images, {"enabled": True, "status": "disabled_by_limit", "analyzed": 0, "cache_hits": 0, "cache_misses": 0}
    if client is None:
        return images, {"enabled": True, "status": "missing_vision_key", "analyzed": 0, "cache_hits": 0, "cache_misses": 0}

    candidates = _image_vision_candidates(images, mode, max_images)
    if not candidates:
        return images, {"enabled": True, "status": "no_candidates", "analyzed": 0, "cache_hits": 0, "cache_misses": 0}

    cache_dir = ensure_dir(course_path / "image_vision_cache")
    cache_hits = 0
    cache_misses = 0
    to_call: list[dict[str, Any]] = []
    refined_by_id: dict[str, dict[str, Any]] = {}
    for image in candidates:
        cache_path = _image_vision_cache_path(cache_dir, client.cache_identity, image)
        if cache_path.exists() and not refresh:
            refined_by_id[str(image.get("id", ""))] = _strip_internal_image_fields(read_json(cache_path).get("image", {}))
            cache_hits += 1
        else:
            call_record = dict(image)
            call_record["_cache_path"] = str(cache_path)
            to_call.append(call_record)

    if to_call:
        try:
            raw_results = asyncio.run(client.analyze_many(to_call))
            for image, raw in zip(to_call, raw_results):
                normalized = _normalize_vision_image_result(raw, image)
                normalized = _strip_internal_image_fields(normalized)
                refined_by_id[str(image.get("id", ""))] = normalized
                if not str(normalized.get("reason", "")).startswith("vision_mcp_failed:"):
                    write_json(
                        Path(str(image["_cache_path"])),
                        {
                            "image": normalized,
                            "provider": client.cache_identity,
                        },
                    )
                cache_misses += 1
        except Exception as exc:  # pragma: no cover - depends on external MCP/model behavior
            return images, {
                "enabled": True,
                "status": "vision_failed_fallback",
                "error": str(exc),
                "analyzed": cache_hits,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
            }

    refined: list[dict[str, Any]] = []
    for image in images:
        updated = refined_by_id.get(str(image.get("id", "")))
        refined.append(updated if updated else image)
    return refined, {
        "enabled": True,
        "status": "ok",
        "mode": mode,
        "candidate_count": len(candidates),
        "analyzed": len(refined_by_id),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "max_images": max_images,
        "provider": client.cache_identity,
    }


def _image_vision_candidates(images: list[dict[str, Any]], mode: str, max_images: int) -> list[dict[str, Any]]:
    if mode == "all":
        candidates = list(images)
    else:
        candidates = [
            image
            for image in images
            if image.get("needs_confirmation")
            or float(image.get("confidence", 0)) < 0.72
            or image.get("image_type") in {"unknown", "mixed_text_image", "formula_image"}
        ]
    return candidates[:max_images]


def _image_vision_cache_path(cache_dir: Path, provider: dict[str, Any], image: dict[str, Any]) -> Path:
    digest = hashlib.sha256()
    digest.update(json.dumps(provider, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(str(image.get("id", "")).encode("utf-8"))
    digest.update(_image_file_digest(Path(str(image.get("path", "")))).encode("utf-8"))
    return cache_dir / f"{slugify(str(image.get('id', 'image')))}-{digest.hexdigest()[:20]}.json"


def _image_file_digest(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_vision_image_result(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    allowed_types = {
        "function_graph",
        "formula_image",
        "geometry_diagram",
        "structure_diagram",
        "flowchart",
        "experimental_setup",
        "table_screenshot",
        "mixed_text_image",
        "unknown",
    }
    image_type = str(raw.get("image_type") or fallback.get("image_type") or "unknown").strip()
    if image_type not in allowed_types:
        image_type = "unknown"
    confidence = _bounded_float(raw.get("confidence", fallback.get("confidence", 0.0)), 0.0, 0.98)
    needs_confirmation = bool(raw.get("needs_confirmation", image_type == "unknown" or confidence < 0.55))
    if image_type == "unknown" or confidence < 0.55:
        needs_confirmation = True
    summary = _short_text(str(raw.get("summary") or fallback.get("summary") or "").strip(), 360)
    content_summary = _short_text(str(raw.get("content_summary") or raw.get("summary") or fallback.get("content_summary") or fallback.get("summary") or "").strip(), 420)
    caption = _short_text(str(raw.get("caption") or fallback.get("caption") or "").strip(), 160)
    if needs_confirmation and not caption:
        caption = "待确认图片"
    points = [
        str(item).strip()
        for item in raw.get("associated_knowledge_points", fallback.get("associated_knowledge_points", []))
        if str(item).strip()
    ][:6]
    updated = _strip_internal_image_fields(fallback)
    updated.update(
        {
            "image_type": image_type,
            "content_summary": content_summary,
            "associated_knowledge_points": points,
            "summary": summary or str(fallback.get("summary", "")),
            "suggested_insert_after": _short_text(str(raw.get("suggested_insert_after") or fallback.get("suggested_insert_after") or ""), 160),
            "suggested_insert_position": str(raw.get("suggested_insert_position") or fallback.get("suggested_insert_position") or "after_source_block"),
            "preserve_original_image": bool(raw.get("preserve_original_image", fallback.get("preserve_original_image", image_type != "formula_image" or needs_confirmation))),
            "needs_caption": bool(raw.get("needs_caption", fallback.get("needs_caption", True))),
            "caption": caption or str(fallback.get("caption", "")),
            "needs_confirmation": needs_confirmation,
            "confidence": confidence,
            "reason": _short_text(str(raw.get("reason") or fallback.get("reason") or ""), 260),
            "understanding_source": "vision_mcp",
        }
    )
    return updated


def _recognize_formula_images(
    images: list[dict[str, Any]],
    course_path: Path,
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = [image for image in images if _is_formula_image_candidate(image)]
    if not candidates:
        return images, {"enabled": True, "status": "no_formula_candidates", "candidate_count": 0, "recognized": 0, "needs_human_review": 0}

    fallback_by_id = {str(image.get("id", "")): _fallback_formula_image_recognition(image) for image in candidates}
    refined_by_id: dict[str, dict[str, Any]] = {}
    cache_hits = 0
    cache_misses = 0
    status = "heuristic_only"
    use_formula_vision = bool(profile.get("use_formula_image_recognition", profile.get("use_vision_image_understanding", False)))
    client = VisionImageClient.from_env() if use_formula_vision else None
    max_images = max(0, int(profile.get("formula_image_max_images", profile.get("image_vision_max_images", 12))))
    refresh = bool(profile.get("refresh_formula_image_recognition", profile.get("refresh_image_vision", False)))

    if client is not None and max_images > 0:
        cache_dir = ensure_dir(course_path / "formula_image_cache")
        to_call: list[dict[str, Any]] = []
        for image in candidates[:max_images]:
            cache_path = _formula_image_cache_path(cache_dir, client.cache_identity, image)
            if cache_path.exists() and not refresh:
                refined_by_id[str(image.get("id", ""))] = read_json(cache_path).get("formula", {})
                cache_hits += 1
            else:
                call_record = dict(image)
                call_record["_cache_path"] = str(cache_path)
                to_call.append(call_record)
        if to_call:
            analyzer = getattr(client, "analyze_formula_many", None)
            if callable(analyzer):
                try:
                    raw_results = asyncio.run(analyzer(to_call))
                    for image, raw in zip(to_call, raw_results):
                        normalized = _normalize_formula_image_result(raw, image, fallback_by_id.get(str(image.get("id", "")), {}), source="formula_vision_agent")
                        refined_by_id[str(image.get("id", ""))] = normalized
                        if not str(normalized.get("reason", "")).startswith("formula_vision_mcp_failed:"):
                            write_json(Path(str(image["_cache_path"])), {"formula": normalized, "provider": client.cache_identity})
                        cache_misses += 1
                    status = "ok"
                except Exception as exc:  # pragma: no cover - depends on external MCP/model behavior
                    status = f"formula_vision_failed_fallback: {exc}"
            else:
                status = "formula_vision_method_missing"

    updated_images: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []
    for image in images:
        image_id = str(image.get("id", ""))
        if image_id in fallback_by_id:
            formula = refined_by_id.get(image_id) or fallback_by_id[image_id]
            updated = dict(image)
            updated["image_type"] = "formula_image"
            updated["formula_recognition"] = formula
            updated["preserve_original_image"] = bool(formula.get("preserve_original_image", True))
            updated["needs_confirmation"] = bool(updated.get("needs_confirmation") or formula.get("needs_human_review"))
            updated["suggested_insert_position"] = "pending_confirmation" if formula.get("needs_human_review") else "after_source_block"
            if formula.get("caption"):
                updated["caption"] = formula["caption"]
            formulas.append(formula)
            updated_images.append(updated)
        else:
            updated_images.append(image)

    return updated_images, {
        "enabled": True,
        "status": status,
        "candidate_count": len(candidates),
        "recognized": sum(1 for formula in formulas if formula.get("is_formula")),
        "needs_human_review": sum(1 for formula in formulas if formula.get("needs_human_review")),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
    }


def _is_formula_image_candidate(image: dict[str, Any]) -> bool:
    if str(image.get("image_type", "")) == "formula_image":
        return True
    context = "\n".join(
        str(image.get(key, ""))
        for key in ("content_summary", "summary", "caption", "neighbor_text", "page_context", "mineru_content")
    )
    return _looks_like_formula_image(image, context)


def _fallback_formula_image_recognition(image: dict[str, Any]) -> dict[str, Any]:
    context = "\n".join(
        str(image.get(key, ""))
        for key in ("mineru_content", "caption", "content_summary", "neighbor_text", "page_context", "summary")
    )
    candidate = _extract_formula_candidate_text(context)
    latex = _clean_formula_latex(candidate)
    confidence = 0.62 if latex else 0.28
    role = _formula_role_from_context(context)
    needs_review = confidence < 0.75
    markdown = _formula_markdown_from_latex(latex) if latex else ""
    return {
        "image_id": str(image.get("id", "")),
        "source_chunk_id": str(image.get("source_chunk_id", "")),
        "image_path": str(image.get("path", "")),
        "asset_url": str(image.get("asset_url", "")),
        "page_idx": image.get("page_idx"),
        "bbox": image.get("bbox", []),
        "page_screenshot_path": str(image.get("page_screenshot_path", "")),
        "is_formula": bool(candidate or str(image.get("image_type", "")) == "formula_image"),
        "formula_role": role,
        "recognized_text": _short_text(candidate, 500),
        "latex": latex,
        "markdown": markdown,
        "context_check": "uncertain",
        "confidence": confidence,
        "needs_human_review": needs_review,
        "preserve_original_image": True,
        "caption": str(image.get("caption") or "待确认公式图片"),
        "suggested_insert_after": str(image.get("suggested_insert_after", "")),
        "suggested_insert_position": "pending_confirmation" if needs_review else "after_source_block",
        "associated_knowledge_points": list(image.get("associated_knowledge_points", [])),
        "reason": "Heuristic formula-image recognition; requires review before editable formula is trusted.",
        "recognition_source": "mineru_heuristic",
    }


def _normalize_formula_image_result(raw: dict[str, Any], image: dict[str, Any], fallback: dict[str, Any], source: str) -> dict[str, Any]:
    allowed_roles = {"definition", "theorem", "derivation", "example_formula", "annotation_formula", "unknown"}
    role = str(raw.get("formula_role") or fallback.get("formula_role") or "unknown").strip()
    if role not in allowed_roles:
        role = "unknown"
    latex = _clean_formula_latex(str(raw.get("latex") or raw.get("recognized_text") or fallback.get("latex") or ""))
    markdown = str(raw.get("markdown") or "").strip() or _formula_markdown_from_latex(latex)
    confidence = _bounded_float(raw.get("confidence", fallback.get("confidence", 0.0)), 0.0, 0.99)
    context_check = str(raw.get("context_check") or fallback.get("context_check") or "uncertain")
    needs_review = bool(raw.get("needs_human_review", confidence < 0.78 or context_check not in {"consistent", "partially_supported"}))
    if confidence < 0.78 or not latex:
        needs_review = True
    preserve = bool(raw.get("preserve_original_image", needs_review or fallback.get("preserve_original_image", True)))
    return {
        "image_id": str(image.get("id", fallback.get("image_id", ""))),
        "source_chunk_id": str(image.get("source_chunk_id", fallback.get("source_chunk_id", ""))),
        "image_path": str(image.get("path", fallback.get("image_path", ""))),
        "asset_url": str(image.get("asset_url", fallback.get("asset_url", ""))),
        "page_idx": image.get("page_idx", fallback.get("page_idx")),
        "bbox": image.get("bbox", fallback.get("bbox", [])),
        "page_screenshot_path": str(image.get("page_screenshot_path", fallback.get("page_screenshot_path", ""))),
        "is_formula": bool(raw.get("is_formula", True)),
        "formula_role": role,
        "recognized_text": _short_text(str(raw.get("recognized_text") or fallback.get("recognized_text") or ""), 500),
        "latex": latex,
        "markdown": markdown,
        "context_check": context_check,
        "confidence": confidence,
        "needs_human_review": needs_review,
        "preserve_original_image": preserve,
        "caption": _short_text(str(raw.get("caption") or fallback.get("caption") or "公式图片"), 160),
        "suggested_insert_after": _short_text(str(raw.get("suggested_insert_after") or fallback.get("suggested_insert_after") or image.get("suggested_insert_after", "")), 160),
        "suggested_insert_position": "pending_confirmation" if needs_review else str(raw.get("suggested_insert_position") or fallback.get("suggested_insert_position") or "after_source_block"),
        "associated_knowledge_points": list(raw.get("associated_knowledge_points", fallback.get("associated_knowledge_points", image.get("associated_knowledge_points", []))))[:6],
        "reason": _short_text(str(raw.get("reason") or fallback.get("reason") or ""), 360),
        "recognition_source": source,
    }


def _formula_image_cache_path(cache_dir: Path, provider: dict[str, Any], image: dict[str, Any]) -> Path:
    digest = hashlib.sha256()
    digest.update(json.dumps(provider, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    digest.update(str(image.get("id", "")).encode("utf-8"))
    digest.update(_image_file_digest(Path(str(image.get("path", "")))).encode("utf-8"))
    digest.update(str(image.get("page_context", "")).encode("utf-8"))
    return cache_dir / f"{slugify(str(image.get('id', 'formula')))}-{digest.hexdigest()[:20]}.json"


def _extract_formula_candidate_text(context: str) -> str:
    text = str(context or "")
    patterns = [
        r"\$\$([\s\S]+?)\$\$",
        r"\\\[([\s\S]+?)\\\]",
        r"(\\begin\{(?:array|[bpvVB]?matrix|cases|aligned|align|split|gathered)\}[\s\S]+?\\end\{[^}]+\})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    formula_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if _looks_like_standalone_formula_line(stripped) or (("=" in stripped or "≤" in stripped or "≥" in stripped) and re.search(r"[A-Za-z0-9_\\^{}+\-*/=<>≤≥]", stripped)):
            formula_lines.append(stripped)
        if len(formula_lines) >= 6:
            break
    return "\n".join(formula_lines).strip()


def _clean_formula_latex(value: str) -> str:
    cleaned = str(value or "").strip()
    cleaned = re.sub(r"^\$\$|\$\$$", "", cleaned).strip()
    cleaned = re.sub(r"^\\\[|\\\]$", "", cleaned).strip()
    return cleaned


def _formula_markdown_from_latex(latex: str) -> str:
    latex = _clean_formula_latex(latex)
    if not latex:
        return ""
    return "$$\n" + latex + "\n$$"


def _formula_role_from_context(context: str) -> str:
    normalized = _normalize_title(context)
    if any(token in normalized for token in ("定义", "definition")):
        return "definition"
    if any(token in normalized for token in ("定理", "theorem", "命题", "proposition")):
        return "theorem"
    if any(token in normalized for token in ("推导", "证明", "derive", "proof")):
        return "derivation"
    if any(token in normalized for token in ("例题", "example", "例", "习题")):
        return "example_formula"
    if any(token in normalized for token in ("注释", "备注", "remark", "note")):
        return "annotation_formula"
    return "unknown"


def _strip_internal_image_fields(image: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(image).items() if not str(key).startswith("_")}


def _bounded_float(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return min(high, max(low, number))


def _image_context_text(ref: dict[str, Any], chunk: dict[str, Any]) -> str:
    parts = [
        str(chunk.get("title", "")),
        str(ref.get("mineru_type", "")),
        str(ref.get("mineru_sub_type", "")),
        str(ref.get("mineru_content", "")),
        " ".join(str(item) for item in ref.get("caption", [])),
        " ".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:8]),
    ]
    return "\n".join(part for part in parts if part).strip()


def _classify_image_type(ref: dict[str, Any], context: str) -> str:
    mineru_type = str(ref.get("mineru_type", "")).lower()
    sub_type = str(ref.get("mineru_sub_type", "")).lower()
    text = _normalize_title(context)
    if _looks_like_formula_image(ref, context):
        return "formula_image"
    if mineru_type in {"table"} or "table" in sub_type:
        return "table_screenshot"
    if mineru_type in {"chart"} or sub_type in {"line", "bar", "scatter"}:
        return "function_graph"
    if any(token in text for token in ("flowchart", "流程", "步骤", "pipeline", "workflow", "arrow", "箭头")):
        return "flowchart"
    if any(token in text for token in ("结构", "架构", "structure", "architecture", "module", "框图")):
        return "structure_diagram"
    if any(token in text for token in ("几何", "geometry", "triangle", "circle", "坐标", "曲线", "函数", "graph", "plot")):
        return "function_graph"
    if any(token in text for token in ("实验", "装置", "apparatus", "setup", "instrument")):
        return "experimental_setup"
    if sub_type in {"text_image", "natural_image"} or mineru_type == "image":
        return "mixed_text_image" if str(ref.get("mineru_content", "")).strip() else "unknown"
    return "unknown"


def _image_confidence(ref: dict[str, Any], context: str, image_type: str) -> float:
    score = 0.25
    if ref.get("mineru_type"):
        score += 0.2
    if ref.get("bbox"):
        score += 0.1
    if str(ref.get("mineru_content", "")).strip():
        score += 0.2
    if str(ref.get("mineru_sub_type", "")).strip():
        score += 0.1
    if image_type != "unknown":
        score += 0.2
    if image_type == "formula_image" and _extract_formula_candidate_text(context):
        score += 0.1
    if len(_meaningful_lines(context, "")) >= 2:
        score += 0.1
    return min(0.95, score)


def _image_summary(ref: dict[str, Any], context: str, image_type: str, needs_confirmation: bool) -> str:
    content = str(ref.get("mineru_content", "")).strip()
    if content:
        base = _short_text(" ".join(content.splitlines()), 220)
    else:
        lines = _meaningful_lines(context, "")
        base = _short_text(" ".join(lines[:3]), 220)
    if needs_confirmation:
        return base or "图片内容无法由当前解析结果可靠识别，需人工确认后再插入正文。"
    labels = {
        "function_graph": "函数图像或数据曲线",
        "formula_image": "公式、推导或数学表达式图片",
        "geometry_diagram": "几何示意图",
        "structure_diagram": "结构示意图",
        "flowchart": "流程图",
        "experimental_setup": "实验装置图",
        "table_screenshot": "表格截图",
        "mixed_text_image": "图文混排图片",
    }
    return f"{labels.get(image_type, '图片')}：{base}" if base else labels.get(image_type, "图片")


def _image_caption(ref: dict[str, Any], summary: str, needs_confirmation: bool) -> str:
    caption = _short_text("；".join(" ".join(str(item).split()) for item in ref.get("caption", []) if str(item).strip()), 120)
    if caption:
        return caption
    if needs_confirmation:
        return "待确认图片"
    return _short_text(summary, 120)


def _image_knowledge_points(chunk: dict[str, Any], context: str) -> list[str]:
    points: list[str] = []
    title = _clean_lesson_title(str(chunk.get("title", ""))) or str(chunk.get("title", "")).strip()
    if title:
        points.append(title)
    for line in _meaningful_lines(context, title):
        cleaned = _short_text(line, 60)
        if cleaned and _normalize_title(cleaned) not in {_normalize_title(item) for item in points}:
            points.append(cleaned)
        if len(points) >= 4:
            break
    return points


def _looks_like_formula_image(ref: dict[str, Any], context: str) -> bool:
    mineru_type = str(ref.get("mineru_type", "")).lower()
    sub_type = str(ref.get("mineru_sub_type", "")).lower()
    text = str(context or "")
    normalized = _normalize_title(text)
    if any(token in mineru_type for token in ("formula", "equation")) or any(token in sub_type for token in ("formula", "equation")):
        return True
    formula_tokens = (
        "$$",
        "\\[",
        "\\begin",
        "\\frac",
        "\\sum",
        "\\int",
        "\\sqrt",
        "\\lambda",
        "\\theta",
        "\\mu",
        "=",
        "≤",
        "≥",
        "∑",
        "∫",
    )
    if any(token in text for token in formula_tokens) and not re.search(r"\|.+\|", text):
        return True
    return any(token in normalized for token in ("公式", "方程", "推导", "数学表达式", "物理方程", "化学方程", "定理证明"))


def _image_page_context(ref: dict[str, Any], chunk: dict[str, Any], chunk_by_id: dict[str, dict[str, Any]]) -> str:
    page_idx = ref.get("page_idx")
    source = str(chunk.get("source", ""))
    chunks = sorted(chunk_by_id.values(), key=lambda item: int(item.get("source_order", 0)))
    selected: list[str] = []
    for candidate in chunks:
        if str(candidate.get("source", "")) != source:
            continue
        candidate_page = candidate.get("page_idx", candidate.get("page"))
        same_page = page_idx is not None and candidate_page is not None and str(candidate_page) == str(page_idx)
        near_order = abs(int(candidate.get("source_order", 0)) - int(chunk.get("source_order", 0))) <= 1
        if same_page or near_order:
            selected.append(str(candidate.get("content", "")))
        if len(selected) >= 4:
            break
    if not selected:
        selected.append(str(chunk.get("content", "")))
    return "\n\n".join(_short_text(item, 900) for item in selected if item).strip()


def _suggest_image_insert_anchor(chunk: dict[str, Any], context: str) -> str:
    for line in _meaningful_lines(context, str(chunk.get("title", ""))):
        if not line.startswith("!["):
            return _short_text(line, 120)
    return str(chunk.get("title", ""))


def _image_reason(ref: dict[str, Any], image_type: str, confidence: float) -> str:
    parts = [f"type={image_type}", f"confidence={confidence:.2f}"]
    if ref.get("mineru_type"):
        parts.append(f"mineru_type={ref.get('mineru_type')}")
    if ref.get("mineru_sub_type"):
        parts.append(f"sub_type={ref.get('mineru_sub_type')}")
    if ref.get("page_idx") is not None:
        parts.append(f"page={int(ref.get('page_idx')) + 1}")
    return "; ".join(parts)


def _render_image_understanding_markdown(image_understanding: dict[str, Any]) -> str:
    lines = ["# Image Understanding", "", f"Summary: {image_understanding.get('summary', {})}", ""]
    for image in image_understanding.get("images", []):
        lines.extend(
            [
                f"## {image.get('id')}",
                "",
                f"- Type: {image.get('image_type')}",
                f"- Needs confirmation: {image.get('needs_confirmation')}",
                f"- Source chunk: {image.get('source_chunk_id')}",
                f"- Page/BBox: page `{image.get('page_idx')}`, bbox `{image.get('bbox', [])}`",
                f"- Suggested lesson: {image.get('suggested_lesson_title')}",
                f"- Suggested insert: {image.get('suggested_insert_position')} after `{image.get('suggested_insert_after')}`",
                f"- Preserve original image: {image.get('preserve_original_image')}",
                f"- Content summary: {image.get('content_summary')}",
                f"- Summary: {image.get('summary')}",
                f"- Asset: {image.get('asset_url')}",
                "",
            ]
        )
        formula = image.get("formula_recognition", {})
        if formula:
            lines.extend(
                [
                    "### Formula Recognition",
                    "",
                    f"- Role: {formula.get('formula_role')}",
                    f"- Confidence: {formula.get('confidence')}",
                    f"- Needs human review: {formula.get('needs_human_review')}",
                    f"- Context check: {formula.get('context_check')}",
                    f"- Preserve original image: {formula.get('preserve_original_image')}",
                    "",
                    "```latex",
                    str(formula.get("latex", "")),
                    "```",
                    "",
                ]
            )
    if image_understanding.get("pending_confirmation"):
        lines.extend(["## Pending Confirmation", ""])
        for image in image_understanding["pending_confirmation"]:
            lines.append(f"- {image.get('id')}: {image.get('summary')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_formula_image_recognition_markdown(report: dict[str, Any]) -> str:
    lines = ["# Formula Image Recognition", "", f"Summary: {report.get('summary', {})}", ""]
    for formula in report.get("formulas", []):
        lines.extend(
            [
                f"## {formula.get('image_id')}",
                "",
                f"- Source chunk: `{formula.get('source_chunk_id')}`",
                f"- Role: {formula.get('formula_role')}",
                f"- Confidence: {formula.get('confidence')}",
                f"- Context check: {formula.get('context_check')}",
                f"- Needs human review: {formula.get('needs_human_review')}",
                f"- Preserve original image: {formula.get('preserve_original_image')}",
                f"- Suggested insert: {formula.get('suggested_insert_position')} after `{formula.get('suggested_insert_after')}`",
                f"- Page screenshot: `{formula.get('page_screenshot_path')}`",
                f"- Reason: {formula.get('reason')}",
                "",
                "### Markdown",
                "",
                str(formula.get("markdown", "") or "[no reliable editable formula]"),
                "",
                "### LaTeX",
                "",
                "```latex",
                str(formula.get("latex", "")),
                "```",
                "",
            ]
        )
    if not report.get("formulas"):
        lines.append("- No formula image candidates detected.")
    return "\n".join(lines).strip() + "\n"


def _use_llm_structure(state: CourseCompileState) -> bool:
    profile = state.get("compile_profile", {})
    return bool(profile.get("use_llm_structure", profile.get("use_llm", False)))


DEFAULT_LLM_PROMPT_CHAR_LIMIT = 15000


def _llm_prompt_char_limit(state: CourseCompileState | None = None) -> int:
    profile = state.get("compile_profile", {}) if state else {}
    try:
        return max(1000, int(profile.get("llm_prompt_char_limit", DEFAULT_LLM_PROMPT_CHAR_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_LLM_PROMPT_CHAR_LIMIT


def _prompt_char_count(system: str, user: str) -> int:
    return len(system) + len(user)


def _structure_cache_path(course_path: Path, client: Any, name: str, system: str, user: str) -> Path | None:
    cache_key = getattr(client, "cache_key", None)
    if not callable(cache_key):
        return None
    return course_path / "llm_cache" / f"{name}-{cache_key(system, user)}.json"


def _complete_structure_json(
    client: Any,
    course_path: Path,
    cache_name: str,
    system: str,
    user: str,
    payload_key: str,
    refresh: bool = False,
    prompt_char_limit: int = DEFAULT_LLM_PROMPT_CHAR_LIMIT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cache_path = _structure_cache_path(course_path, client, cache_name, system, user)
    prompt_chars = _prompt_char_count(system, user)
    if cache_path and cache_path.exists() and not refresh:
        cached = read_json(cache_path)
        return cached.get(payload_key, cached), {
            "local_cache": "hit",
            "cache_path": str(cache_path),
            "prompt_chars": prompt_chars,
            "prompt_char_limit": prompt_char_limit,
            "metadata": cached.get("metadata", {}),
        }
    if prompt_chars > prompt_char_limit:
        raise ValueError(f"LLM prompt too long before submit: {prompt_chars} chars > limit {prompt_char_limit}; split required")
    raw = client.complete_json(system, user)
    metadata = getattr(client, "last_metadata", {})
    if cache_path:
        write_json(
            cache_path,
            {
                payload_key: raw,
                "provider": getattr(client, "cache_identity", {}),
                "metadata": metadata,
            },
        )
    return raw, {
        "local_cache": "miss",
        "cache_path": str(cache_path) if cache_path else "",
        "prompt_chars": prompt_chars,
        "prompt_char_limit": prompt_char_limit,
        "metadata": metadata,
    }


def _emergency_structure_fallback(
    state: CourseCompileState,
    vault_root: Path | str,
    node: str,
    message: str,
    fallback_payload: dict[str, Any],
) -> CourseCompileState:
    course_path = course_dir(Path(vault_root), state["course_id"])
    write_json(course_path / f"{node}_emergency_fallback.json", fallback_payload)
    state["errors"].append(
        {
            "node": node,
            "message": message,
            "requires_human_review": True,
            "fallback_artifact": f"{node}_emergency_fallback.json",
        }
    )
    state["validation_report"] = {
        "ok": False,
        "checks": ["llm_first_structure"],
        "failures": [
            {
                "node": node,
                "type": "emergency_fallback_requires_review",
                "message": message,
            }
        ],
    }
    state["next_action"] = "human_review"
    return state


def _source_provenance_for_chunk(chunk: dict[str, Any], order_fallback: int = 0) -> dict[str, Any]:
    image_refs = chunk.get("image_refs", []) if isinstance(chunk.get("image_refs", []), list) else []
    first_image = image_refs[0] if image_refs else {}
    page = chunk.get("page", chunk.get("page_idx", first_image.get("page_idx")))
    if page is None and first_image.get("page_idx") is not None:
        page = int(first_image["page_idx"]) + 1
    return {
        "source_file": str(chunk.get("source_file") or chunk.get("source", "")),
        "page": page,
        "block_id": str(chunk.get("block_id") or chunk.get("id", "")),
        "bbox": chunk.get("bbox") or first_image.get("bbox", []) or [],
        "source_order": int(chunk.get("source_order") or order_fallback or 0),
        "chunk_id": str(chunk.get("id", "")),
        "start_line": chunk.get("start_line"),
        "end_line": chunk.get("end_line"),
    }


def _source_provenance_for_ids(source_chunk_ids: list[str], chunk_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _source_provenance_for_chunk(chunk_by_id[chunk_id], index)
        for index, chunk_id in enumerate(source_chunk_ids, start=1)
        if chunk_id in chunk_by_id
    ]


def _chunks_for_structure_prompt(chunks: list[dict[str, Any]], max_chunks: int, max_chars: int = 720) -> str:
    selected = chunks[:max_chunks]
    lines: list[str] = []
    for index, chunk in enumerate(selected, start=1):
        meaningful = " ".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:8])
        if len(meaningful) > max_chars:
            meaningful = meaningful[:max_chars].rstrip() + "..."
        provenance = _source_provenance_for_chunk(chunk, index)
        lines.append(
            f"- id: {chunk.get('id')}\n"
            f"  title: {chunk.get('title')}\n"
            f"  source_file: {provenance['source_file']}\n"
            f"  page: {provenance['page']}\n"
            f"  block_id: {provenance['block_id']}\n"
            f"  bbox: {provenance['bbox']}\n"
            f"  source_order: {provenance['source_order']}\n"
            f"  content: {meaningful}"
        )
    if len(chunks) > len(selected):
        lines.append(f"- omitted_chunks: {len(chunks) - len(selected)}")
    return "\n".join(lines)


def _compact_unit_for_prompt(unit: dict[str, Any], summary_chars: int = 320) -> dict[str, Any]:
    return {
        "id": unit.get("id", ""),
        "title": unit.get("title", ""),
        "section_title": unit.get("section_title", ""),
        "lesson_type": unit.get("lesson_type", ""),
        "content_type": unit.get("content_type", ""),
        "summary": _short_text(str(unit.get("summary", "")), summary_chars),
        "source_chunk_ids": list(unit.get("source_chunk_ids", [])),
    }


def _compact_units_for_prompt(units: list[dict[str, Any]], max_chars: int = 10000, summary_chars: int = 320) -> str:
    compact: list[dict[str, Any]] = []
    for unit in units:
        compact.append(_compact_unit_for_prompt(unit, summary_chars=summary_chars))
        serialized = json.dumps(compact, ensure_ascii=False)
        if len(serialized) > max_chars:
            compact.pop()
            break
    omitted = max(0, len(units) - len(compact))
    payload: dict[str, Any] = {"units": compact}
    if omitted:
        payload["omitted_units"] = omitted
    return json.dumps(payload, ensure_ascii=False)


def _compact_logic_graph_for_prompt(logic_graph: dict[str, Any], max_chars: int = 3600) -> str:
    payload = {
        "nodes": [
            {
                "id": node.get("id", ""),
                "title": node.get("title", ""),
                "role": node.get("role", ""),
            }
            for node in logic_graph.get("nodes", [])
        ],
        "edges": [
            {
                "from": edge.get("from", ""),
                "to": edge.get("to", ""),
                "relation": edge.get("relation", ""),
                "reason": _short_text(str(edge.get("reason", "")), 160),
            }
            for edge in logic_graph.get("edges", [])
        ],
    }
    return _short_text(json.dumps(payload, ensure_ascii=False), max_chars)


def _compact_gap_report_for_prompt(gap_report: dict[str, Any], max_chars: int = 4200) -> str:
    payload = {
        "ok": gap_report.get("ok", False),
        "summary": gap_report.get("summary", {}),
        "items": [
            {
                "unit_id": item.get("unit_id", ""),
                "type": item.get("type", ""),
                "severity": item.get("severity", ""),
                "message": _short_text(str(item.get("message", "")), 180),
                "source_chunk_ids": list(item.get("source_chunk_ids", []))[:8],
            }
            for item in gap_report.get("items", [])
        ],
    }
    return _short_text(json.dumps(payload, ensure_ascii=False), max_chars)


def _fallback_unit_candidate_for_prompt(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": unit.get("title", ""),
        "section_title": unit.get("section_title", ""),
        "lesson_type": unit.get("lesson_type", ""),
        "source_chunk_ids": list(unit.get("source_chunk_ids", [])),
    }


def _bad_independent_lesson_title(title: str) -> bool:
    normalized = _normalize_title(title)
    if _is_contextless_lesson_title(title) or _is_weak_title(title):
        return True
    bad_exact = {
        "图片说明",
        "图表解释",
        "教师提示",
        "页码注释",
        "作者信息",
        "ppt作者",
        "排版说明",
        "视觉说明",
        "source note",
        "image caption",
        "page note",
        "teacher hint",
        "teacher note",
        "slide note",
        "ppt author",
        "author info",
        "author",
    }
    if normalized in {_normalize_title(item) for item in bad_exact}:
        return True
    bad_tokens = ("页码", "作者", "教师提示", "图片说明", "图注", "视觉关系", "排版", "teacherhint", "teachernote", "imagecaption", "pagenote", "authorinfo")
    if any(token in normalized for token in bad_tokens):
        return True
    return len(normalized) < 4


def _merge_duplicate_or_bad_units(units: list[dict[str, Any]], chunk_by_id: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    by_title: dict[str, dict[str, Any]] = {}
    for unit in units:
        title = str(unit.get("title", "")).strip()
        key = _normalize_title(title)
        chunk_ids = [str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []) if str(chunk_id) in chunk_by_id]
        if not title or not chunk_ids:
            rejected.append({"title": title, "reason": "missing_title_or_sources", "source_chunk_ids": chunk_ids})
            continue
        if _bad_independent_lesson_title(title):
            if merged:
                merged[-1]["source_chunk_ids"] = _dedupe_keep_order(merged[-1]["source_chunk_ids"] + chunk_ids)
                merged[-1]["source_provenance"] = _source_provenance_for_ids(merged[-1]["source_chunk_ids"], chunk_by_id)
                rejected.append({"title": title, "reason": "attached_bad_fragment_to_previous", "source_chunk_ids": chunk_ids})
            else:
                rejected.append({"title": title, "reason": "bad_fragment_without_anchor", "source_chunk_ids": chunk_ids})
            continue
        existing = by_title.get(key)
        if existing:
            existing["source_chunk_ids"] = _dedupe_keep_order(existing["source_chunk_ids"] + chunk_ids)
            existing["summary"] = _short_text("\n".join([str(existing.get("summary", "")), str(unit.get("summary", ""))]).strip(), 2400)
            existing["source_provenance"] = _source_provenance_for_ids(existing["source_chunk_ids"], chunk_by_id)
            rejected.append({"title": title, "reason": "merged_duplicate_title", "source_chunk_ids": chunk_ids})
            continue
        unit["source_chunk_ids"] = _dedupe_keep_order(chunk_ids)
        unit["source_provenance"] = _source_provenance_for_ids(unit["source_chunk_ids"], chunk_by_id)
        by_title[key] = unit
        merged.append(unit)
    for index, unit in enumerate(merged, start=1):
        unit["id"] = f"unit-{index:03d}"
        unit["order"] = index
    return merged, rejected


def _normalize_llm_units(raw: dict[str, Any], state: CourseCompileState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    detailed = bool(state.get("compile_profile", {}).get("detailed_lessons"))
    max_body_chars = int(state.get("compile_profile", {}).get("lesson_body_max_chars", 5200 if detailed else 1200))
    units: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("units", []), start=1):
        chunk_ids = [str(chunk_id) for chunk_id in item.get("source_chunk_ids", item.get("chunk_ids", [])) if str(chunk_id) in chunk_by_id]
        if not chunk_ids:
            continue
        title = _clean_lesson_title(str(item.get("title", "")).strip()) or str(item.get("title", "")).strip()
        combined = "\n\n".join(str(chunk_by_id[chunk_id].get("content", "")) for chunk_id in chunk_ids)
        source_refs = _source_refs_for_group([chunk_by_id[chunk_id] for chunk_id in chunk_ids])
        units.append(
            {
                "id": f"unit-{index:03d}",
                "title": title,
                "section_title": str(item.get("section_title", "")).strip(),
                "lesson_type": _normalize_lesson_type(str(item.get("lesson_type", "")).strip()),
                "course_style": _course_style(state.get("compile_profile", {})),
                "summary": _short_text(str(item.get("summary", "")).strip() or _summarize_group(combined, title), 3200 if detailed else 1200),
                "teaching_notes": _normalize_teaching_notes(item.get("teaching_notes", "")),
                "source_highlights": [str(value).strip() for value in item.get("source_highlights", []) if str(value).strip()][:12]
                or _source_highlights_for_lesson(combined, title, max_lines=18 if detailed else 8),
                "detailed_lesson": detailed,
                "lesson_body_max_chars": max_body_chars,
                "source_chunk_id": chunk_ids[0],
                "source_chunk_ids": chunk_ids,
                "source": chunk_by_id[chunk_ids[0]].get("source", ""),
                "source_refs": source_refs,
                "image_refs": _image_refs_for_group([chunk_by_id[chunk_id] for chunk_id in chunk_ids], state.get("image_understanding", {})),
                "content_type": str(item.get("content_type", "source_supported")).strip() or _classify_content_type(combined),
                "source_quote": source_refs[0]["quote"] if source_refs else _first_meaningful_line(combined, title),
                "source_provenance": _source_provenance_for_ids(chunk_ids, chunk_by_id),
                "order": index,
            }
        )
    return _merge_duplicate_or_bad_units(units, chunk_by_id)


def _normalize_teaching_notes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    return {"explanation": text}


def _fallback_extract_units(state: CourseCompileState) -> list[dict[str, Any]]:
    """Emergency local grouping used only when LLM structure extraction is unavailable."""

    units: list[dict[str, Any]] = []
    if state.get("course_plan", {}).get("sections"):
        groups = _groups_from_course_plan(
            state["parsed_chunks"],
            state["course_plan"],
            attach_remaining=bool(state.get("source_brief")),
        )
    else:
        groups = _group_chunks_for_lessons(state["parsed_chunks"])
    repeated_headings = _repeated_titles(state["parsed_chunks"])
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    for index, group in enumerate(groups, start=1):
        first = group[0]
        planned_title = _clean_lesson_title(str(group[0].get("_planned_title", "")).strip())
        section_title = str(group[0].get("_section_title", "")).strip()
        lesson_type = _normalize_lesson_type(str(group[0].get("_planned_lesson_type", "")).strip())
        title = planned_title or _choose_group_title(group, f"Unit {index}", repeated_headings)
        combined = "\n\n".join(str(chunk["content"]).strip() for chunk in group)
        detailed = bool(state.get("compile_profile", {}).get("detailed_lessons"))
        summary_limit = int(state.get("compile_profile", {}).get("unit_summary_chars", 3200 if detailed else 900))
        summary = _summarize_group(combined, title, limit=summary_limit)
        content_type = _classify_content_type(combined)
        source_chunk_ids = [chunk["id"] for chunk in group]
        teaching_notes = _teaching_notes_for_unit(title, source_chunk_ids, state.get("lesson_notes", {}))
        if not teaching_notes:
            teaching_notes = _teaching_notes_for_unit(title, source_chunk_ids, state.get("source_brief", {}))
        source_refs = _source_refs_for_group(group)
        image_refs = _image_refs_for_group(group, state.get("image_understanding", {}))
        max_body_chars = int(state.get("compile_profile", {}).get("lesson_body_max_chars", 5200 if detailed else 1200))
        units.append(
            {
                "id": f"unit-{index:03d}",
                "title": title,
                "section_title": section_title,
                "lesson_type": lesson_type,
                "course_style": _course_style(state.get("compile_profile", {})),
                "summary": summary,
                "teaching_notes": teaching_notes,
                "source_highlights": _source_highlights_for_lesson(combined, title, max_lines=22 if detailed else 10),
                "detailed_lesson": detailed,
                "lesson_body_max_chars": max_body_chars,
                "source_chunk_id": first["id"],
                "source_chunk_ids": source_chunk_ids,
                "source": first["source"],
                "source_refs": source_refs,
                "image_refs": image_refs,
                "content_type": content_type,
                "source_quote": source_refs[0]["quote"],
                "source_provenance": _source_provenance_for_ids(source_chunk_ids, chunk_by_id),
                "order": index,
            }
        )
    return units


def _extract_units_system_prompt() -> str:
    return (
        "You are the structure-extraction agent in a course compiler. Extract medium-grain learning units from source chunks. "
        "Return strict JSON only. Preserve source chunk ids and provenance; do not invent facts."
    )


def _extract_units_user_prompt(
    state: CourseCompileState,
    chunks: list[dict[str, Any]],
    candidate_units: list[dict[str, Any]],
    max_chunk_chars: int = 360,
) -> str:
    return (
        "Return JSON with schema:\n"
        "{\"units\":[{\"title\":\"...\",\"section_title\":\"...\",\"lesson_type\":\"concept|task|example|troubleshooting|reference\","
        "\"summary\":\"...\",\"teaching_notes\":\"...\",\"source_highlights\":[\"...\"],\"source_chunk_ids\":[\"...\"],"
        "\"content_type\":\"source_supported|inferred_from_source|bridge|needs_confirmation\"}],"
        "\"rejected_fragments\":[{\"title\":\"...\",\"reason\":\"...\",\"source_chunk_ids\":[\"...\"]}]}\n\n"
        "Requirements:\n"
        "- Prefer semantic, medium-grain course units over slide-by-slide chunks.\n"
        "- Merge short concepts, teacher hints, image captions, page notes, author/PPT metadata, and layout-only visual descriptions into nearby real units; never make them standalone lessons.\n"
        "- Merge duplicate or near-duplicate titles into one unit.\n"
        "- Every unit must cite existing source_chunk_ids only.\n"
        "- Keep source order and course plan order unless a task-first organization is clearly requested.\n"
        "- Candidate units are planning hints, not mandatory one-to-one outputs.\n\n"
        f"Course style: {_course_style(state.get('compile_profile', {}))}\n"
        f"Candidate units:\n{json.dumps([_fallback_unit_candidate_for_prompt(unit) for unit in candidate_units], ensure_ascii=False)}\n\n"
        f"Source chunks:\n{_chunks_for_structure_prompt(chunks, len(chunks), max_chars=max_chunk_chars)}"
    )


def _candidate_unit_chunks(candidate_units: list[dict[str, Any]], chunk_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    chunk_ids: list[str] = []
    for unit in candidate_units:
        chunk_ids.extend(str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []))
    return [chunk_by_id[chunk_id] for chunk_id in _dedupe_keep_order(chunk_ids) if chunk_id in chunk_by_id]


def _candidate_unit_batches(
    state: CourseCompileState,
    fallback_units: list[dict[str, Any]],
    chunk_by_id: dict[str, dict[str, Any]],
    system: str,
    prompt_limit: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for unit in fallback_units:
        candidate = current + [unit]
        chunks = _candidate_unit_chunks(candidate, chunk_by_id)
        prompt = _extract_units_user_prompt(state, chunks, candidate)
        if _prompt_char_count(system, prompt) <= prompt_limit:
            current = candidate
            continue
        if current:
            batches.append(current)
            current = [unit]
            chunks = _candidate_unit_chunks(current, chunk_by_id)
            prompt = _extract_units_user_prompt(state, chunks, current)
        if _prompt_char_count(system, prompt) <= prompt_limit:
            continue
        split_units = _split_large_candidate_unit(state, unit, chunk_by_id, system, prompt_limit)
        batches.extend(split_units)
        current = []
    if current:
        batches.append(current)
    return batches


def _split_large_candidate_unit(
    state: CourseCompileState,
    unit: dict[str, Any],
    chunk_by_id: dict[str, dict[str, Any]],
    system: str,
    prompt_limit: int,
) -> list[list[dict[str, Any]]]:
    chunk_ids = [str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []) if str(chunk_id) in chunk_by_id]
    if not chunk_ids:
        return [[unit]]
    batches: list[list[dict[str, Any]]] = []
    current_ids: list[str] = []
    for chunk_id in chunk_ids:
        candidate_ids = current_ids + [chunk_id]
        candidate_unit = {**unit, "source_chunk_ids": candidate_ids}
        chunks = [chunk_by_id[item] for item in candidate_ids]
        prompt = _extract_units_user_prompt(state, chunks, [candidate_unit], max_chunk_chars=260)
        if _prompt_char_count(system, prompt) <= prompt_limit:
            current_ids = candidate_ids
            continue
        if current_ids:
            batches.append([{**unit, "source_chunk_ids": current_ids}])
            current_ids = [chunk_id]
            chunks = [chunk_by_id[chunk_id]]
            prompt = _extract_units_user_prompt(state, chunks, [{**unit, "source_chunk_ids": current_ids}], max_chunk_chars=220)
        if _prompt_char_count(system, prompt) > prompt_limit:
            batches.append([{**unit, "source_chunk_ids": [chunk_id]}])
            current_ids = []
    if current_ids:
        batches.append([{**unit, "source_chunk_ids": current_ids}])
    return batches


def _complete_extract_units_json(
    client: Any,
    course_path: Path,
    state: CourseCompileState,
    fallback_units: list[dict[str, Any]],
    refresh: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = _extract_units_system_prompt()
    prompt_limit = _llm_prompt_char_limit(state)
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    direct_user = _extract_units_user_prompt(
        state,
        state["parsed_chunks"][: int(state.get("compile_profile", {}).get("max_structure_chunks", 180))],
        fallback_units,
    )
    if _prompt_char_count(system, direct_user) <= prompt_limit:
        return _complete_structure_json(
            client,
            course_path,
            "structure-units",
            system,
            direct_user,
            "units_result",
            refresh=refresh,
            prompt_char_limit=prompt_limit,
        )

    batches = _candidate_unit_batches(state, fallback_units, chunk_by_id, system, prompt_limit)
    all_units: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    for index, batch_units in enumerate(batches, start=1):
        chunks = _candidate_unit_chunks(batch_units, chunk_by_id)
        user = _extract_units_user_prompt_for_limit(state, chunks, batch_units, system, prompt_limit)
        raw, meta = _complete_structure_json(
            client,
            course_path,
            f"structure-units-batch-{index:03d}",
            system,
            user,
            "units_result",
            refresh=refresh,
            prompt_char_limit=prompt_limit,
        )
        all_units.extend(raw.get("units", []))
        all_rejected.extend(raw.get("rejected_fragments", []))
        batch_meta.append(
            {
                **meta,
                "batch_index": index,
                "batch_count": len(batches),
                "candidate_unit_titles": [str(unit.get("title", "")) for unit in batch_units],
            }
        )
    return {
        "units": all_units,
        "rejected_fragments": all_rejected,
    }, {
        "local_cache": "split_batches",
        "batch_count": len(batches),
        "prompt_char_limit": prompt_limit,
        "batches": batch_meta,
    }


def _extract_units_user_prompt_for_limit(
    state: CourseCompileState,
    chunks: list[dict[str, Any]],
    candidate_units: list[dict[str, Any]],
    system: str,
    prompt_limit: int,
) -> str:
    for max_chunk_chars in (360, 300, 260, 220, 180, 140, 100):
        user = _extract_units_user_prompt(state, chunks, candidate_units, max_chunk_chars=max_chunk_chars)
        if _prompt_char_count(system, user) <= prompt_limit:
            return user
    return _extract_units_user_prompt(state, chunks, candidate_units, max_chunk_chars=80)


def extract_units(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Extract course units with an LLM-first path and gated local fallback."""

    course_path = course_dir(Path(vault_root), state["course_id"])
    if not _use_llm_structure(state):
        units = _fallback_extract_units(state)
        state["units"] = units
        write_json(course_path / "units.json", units)
        state["next_action"] = "organize_logic"
        return state

    client = LLMClient.from_env()
    fallback_units = _fallback_extract_units(state)
    if client is None:
        state["units"] = fallback_units
        return _emergency_structure_fallback(
            state,
            vault_root,
            "extract_units",
            "LLM structure extraction is enabled but no LLM client is configured.",
            {"units": fallback_units, "fallback_kind": "local_grouping_bad_sample"},
        )

    try:
        raw, meta = _complete_extract_units_json(
            client,
            course_path,
            state,
            fallback_units,
            refresh=bool(state.get("compile_profile", {}).get("refresh_llm_structure")),
        )
        units, rejected = _normalize_llm_units(raw, state)
        if not units:
            raise ValueError("LLM returned no valid course units after validation")
        state["units"] = units
        write_json(course_path / "units.json", units)
        write_json(course_path / "units_meta.json", {"node": "extract_units", "llm": meta, "rejected_fragments": rejected})
        state["next_action"] = "organize_logic"
        return state
    except Exception as exc:  # pragma: no cover - external LLM behavior
        state["units"] = fallback_units
        return _emergency_structure_fallback(
            state,
            vault_root,
            "extract_units",
            f"LLM structure extraction failed; emergency fallback requires human review: {exc}",
            {"units": fallback_units, "fallback_kind": "local_grouping_bad_sample"},
        )


def _source_refs_for_group(group: list[dict[str, Any]], max_refs: int = 6) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()

    def add(chunk: dict[str, Any], order_fallback: int) -> None:
        chunk_id = str(chunk["id"])
        if chunk_id in seen_ids or len(selected) >= max_refs:
            return
        provenance = _source_provenance_for_chunk(chunk, order_fallback)
        selected.append(
            {
                "source": chunk["source"],
                "source_file": provenance["source_file"],
                "page": provenance["page"],
                "block_id": provenance["block_id"],
                "bbox": provenance["bbox"],
                "source_order": provenance["source_order"],
                "source_id": provenance["chunk_id"],
                "chunk_id": chunk["id"],
                "quote": _first_meaningful_line(str(chunk["content"]), fallback=str(chunk["title"])),
            }
        )
        seen_ids.add(chunk_id)
        seen_sources.add(str(chunk.get("source", "")))

    for index, chunk in enumerate(group, start=1):
        source = str(chunk.get("source", ""))
        if source not in seen_sources:
            add(chunk, index)
    for index, chunk in enumerate(group, start=1):
        add(chunk, index)
    return selected


def _image_refs_for_group(group: list[dict[str, Any]], image_understanding: dict[str, Any], max_images: int = 4) -> list[dict[str, Any]]:
    chunk_ids = {str(chunk.get("id", "")) for chunk in group}
    images: list[dict[str, Any]] = []
    for image in image_understanding.get("images", []):
        if image.get("needs_confirmation"):
            continue
        if str(image.get("source_chunk_id", "")) not in chunk_ids:
            continue
        images.append(_lesson_image_record(image))
        if len(images) >= max_images:
            break
    return images


def _lesson_image_record(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": image.get("id", ""),
        "image_type": image.get("image_type", ""),
        "content_summary": image.get("content_summary", ""),
        "asset_url": image.get("asset_url", ""),
        "caption": image.get("caption", ""),
        "summary": image.get("summary", ""),
        "source_chunk_id": image.get("source_chunk_id", ""),
        "page_idx": image.get("page_idx"),
        "bbox": image.get("bbox", []),
        "needs_confirmation": bool(image.get("needs_confirmation")),
        "preserve_original_image": bool(image.get("preserve_original_image", True)),
        "suggested_insert_position": image.get("suggested_insert_position", ""),
        "suggested_insert_after": image.get("suggested_insert_after", ""),
        "formula_recognition": image.get("formula_recognition", {}),
    }


def _attach_pending_images_to_last_lesson(lessons: list[dict[str, Any]], image_understanding: dict[str, Any]) -> None:
    pending = [_lesson_image_record(image) for image in image_understanding.get("pending_confirmation", [])]
    if lessons and pending:
        lessons[-1]["pending_image_confirmations"] = pending


def _group_chunks_for_lessons(chunks: list[dict[str, Any]], target_chars: int = 2600) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    current_source = ""
    current_topic = ""
    outline_by_source = _extract_source_outlines(chunks)
    active_outline_by_source: dict[str, str] = {}

    for chunk in chunks:
        content = str(chunk.get("content", "")).strip()
        meaningful = _meaningful_lines(content, str(chunk.get("title", "")))
        if not meaningful:
            continue
        source = str(chunk.get("source", ""))
        chunk_size = sum(len(line) for line in meaningful)
        title = str(chunk.get("title", ""))
        outline_topic = _match_outline_key(title, meaningful, outline_by_source.get(source, []))
        if outline_topic:
            active_outline_by_source[source] = outline_topic
        elif _is_attachment_title(title) or _is_weak_title(title):
            outline_topic = active_outline_by_source.get(source, "")
        else:
            active_outline_by_source[source] = ""
        topic = outline_topic or _topic_key(title, meaningful)
        starts_new_source = bool(current and source != current_source)
        is_major = _is_major_section(title)
        attaches_to_previous = _is_attachment_title(title)
        same_topic = bool(current and topic and topic == current_topic)
        too_large = bool(current and current_chars + chunk_size > target_chars and not same_topic and not attaches_to_previous)
        should_split = starts_new_source or too_large or (is_major and current_chars > 900 and not same_topic)
        if should_split:
            groups.append(current)
            current = []
            current_chars = 0
            current_topic = ""
        current.append(chunk)
        current_chars += chunk_size
        current_source = source
        if topic and not attaches_to_previous:
            current_topic = topic

    if current:
        groups.append(current)
    return _merge_neighbor_groups(groups)


def _groups_from_course_plan(chunks: list[dict[str, Any]], plan: dict[str, Any], attach_remaining: bool = False) -> list[list[dict[str, Any]]]:
    chunk_by_id = {chunk["id"]: chunk for chunk in chunks}
    used: set[str] = set()
    groups: list[list[dict[str, Any]]] = []
    last_section = ""
    repeated_headings = _repeated_titles(chunks)
    for section in plan.get("sections", []):
        section_title = str(section.get("title", "")).strip()
        for lesson in section.get("lessons", []):
            chunk_ids = [chunk_id for chunk_id in lesson.get("chunk_ids", []) if chunk_id in chunk_by_id and chunk_id not in used]
            if not chunk_ids:
                continue
            planned_title = str(lesson.get("title", "")).strip()
            lesson_type = _normalize_lesson_type(str(lesson.get("lesson_type", "")).strip())
            if groups and last_section == section_title and _is_contextless_lesson_title(planned_title):
                for chunk_id in chunk_ids:
                    groups[-1].append(dict(chunk_by_id[chunk_id]))
                    used.add(chunk_id)
                continue
            group: list[dict[str, Any]] = []
            for chunk_id in chunk_ids:
                enriched = dict(chunk_by_id[chunk_id])
                enriched["_planned_title"] = planned_title
                enriched["_section_title"] = section_title
                enriched["_planned_lesson_type"] = lesson_type
                group.append(enriched)
                used.add(chunk_id)
            groups.append(group)
            last_section = section_title

    remaining = [chunk for chunk in chunks if chunk["id"] not in used]
    if remaining:
        remaining_groups = _group_chunks_for_lessons(remaining)
        if attach_remaining and groups:
            _attach_remaining_groups_by_order(groups, remaining_groups, chunks)
        else:
            for group in remaining_groups:
                if groups and _should_attach_remaining_group(group, repeated_headings):
                    groups[-1].extend(group)
                else:
                    groups.append(group)
    return groups


def _attach_remaining_groups_by_order(
    groups: list[list[dict[str, Any]]],
    remaining_groups: list[list[dict[str, Any]]],
    chunks: list[dict[str, Any]],
) -> None:
    order = {str(chunk["id"]): index for index, chunk in enumerate(chunks)}
    anchors = [min(order.get(str(chunk["id"]), 10**9) for chunk in group) for group in groups]
    for group in remaining_groups:
        first_pos = min(order.get(str(chunk["id"]), 10**9) for chunk in group)
        target_index = 0
        for index, anchor in enumerate(anchors):
            if anchor <= first_pos:
                target_index = index
            else:
                break
        groups[target_index].extend(group)
        anchors[target_index] = min(anchors[target_index], first_pos)


def _is_contextless_lesson_title(title: str) -> bool:
    normalized = _normalize_title(title)
    if _is_attachment_title(normalized) or _is_weak_title(normalized):
        return True
    contextless_tokens = (
        "比较",
        "方法比较",
        "小结",
        "总结",
        "结论",
        "重要结论",
        "几个重要",
        "注意事项",
        "补充说明",
        "more",
        "comparison",
        "summary",
        "conclusion",
        "remarks",
    )
    return any(token in normalized for token in contextless_tokens)


def _should_attach_remaining_group(group: list[dict[str, Any]], repeated_headings: set[str]) -> bool:
    title = _choose_group_title(group, "", repeated_headings)
    if not title:
        return True
    if group and _looks_like_cover_or_header_chunk(group[0]):
        return True
    if _looks_like_cover_or_header_group(group):
        return True
    return _is_contextless_lesson_title(title)


def _looks_like_cover_or_header_group(group: list[dict[str, Any]]) -> bool:
    if len(group) > 1:
        return False
    return _looks_like_cover_or_header_chunk(group[0])


def _looks_like_cover_or_header_chunk(chunk: dict[str, Any]) -> bool:
    content = str(chunk.get("content", ""))
    lines = _meaningful_lines(content, str(chunk.get("title", "")))
    if len(lines) <= 2 and sum(len(line) for line in lines) < 80:
        return True
    return False


def _chunks_for_llm_digest(chunks: list[dict[str, Any]], max_chunks: int) -> str:
    lines: list[str] = []
    selected = chunks[:max_chunks]
    for chunk in selected:
        content = " ".join(_meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))[:5])
        if len(content) > 420:
            content = content[:420] + "..."
        lines.append(
            f"- id: {chunk['id']}\n"
            f"  source: {chunk.get('source')}\n"
            f"  title: {chunk.get('title')}\n"
            f"  content: {content}"
        )
    if len(chunks) > len(selected):
        lines.append(f"- omitted_chunks: {len(chunks) - len(selected)}")
    return "\n".join(lines)


def _normalize_course_plan(plan: dict[str, Any], valid_chunk_ids: set[str]) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []
    for section in plan.get("sections", []):
        section_title = str(section.get("title", "")).strip()
        lessons: list[dict[str, Any]] = []
        for lesson in section.get("lessons", []):
            title = str(lesson.get("title", "")).strip()
            chunk_ids = [str(chunk_id) for chunk_id in lesson.get("chunk_ids", []) if str(chunk_id) in valid_chunk_ids]
            if title and chunk_ids:
                normalized_lesson = {"title": title, "chunk_ids": chunk_ids, "why": str(lesson.get("why", "")).strip()}
                lesson_type = _normalize_lesson_type(str(lesson.get("lesson_type", "")).strip())
                if lesson_type:
                    normalized_lesson["lesson_type"] = lesson_type
                lessons.append(normalized_lesson)
        if section_title and lessons:
            sections.append({"title": section_title, "lessons": lessons})
    return {"sections": sections, "rejected_titles": list(plan.get("rejected_titles", []))}


def _normalize_lesson_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"setup", "task", "example", "troubleshooting", "reference", "concept"}:
        return normalized
    return ""


def _fallback_course_plan_from_source_brief(
    brief: dict[str, Any],
    valid_chunk_ids: set[str],
    target_max: int,
    learn_by_doing: bool = False,
) -> dict[str, Any]:
    if not brief:
        return {}
    sections: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    def add(section_title: str, title: str, chunk_ids: list[str], why: str, lesson_type: str) -> None:
        clean_title = _clean_lesson_title(title) or title.strip()
        if not clean_title:
            return
        if learn_by_doing and lesson_type in {"task", "example", "troubleshooting"}:
            clean_title = _task_first_title(clean_title)
        title_key = _normalize_title(clean_title)
        if not title_key or title_key in seen_titles or _title_overlaps_existing(title_key, seen_titles):
            return
        refs = _dedupe_keep_order([chunk_id for chunk_id in chunk_ids if chunk_id in valid_chunk_ids])[:12]
        if not refs:
            return
        section = next((item for item in sections if item["title"] == section_title), None)
        if section is None:
            section = {"title": section_title, "lessons": []}
            sections.append(section)
        section["lessons"].append(
            {
                "title": clean_title,
                "chunk_ids": refs,
                "why": _short_text(why or "Planned from the LLM source teaching brief.", 420),
                "lesson_type": lesson_type,
            }
        )
        seen_titles.add(title_key)

    for workflow in brief.get("workflows", []):
        add(
            "Hands-on Workflow",
            str(workflow.get("title", "")),
            [str(chunk_id) for chunk_id in workflow.get("source_chunk_ids", [])],
            "; ".join(str(step) for step in workflow.get("steps", [])[:6]),
            "task",
        )
    for task in brief.get("tasks", []):
        add(
            "Hands-on Workflow",
            str(task.get("title", "")),
            [str(chunk_id) for chunk_id in task.get("source_chunk_ids", [])],
            str(task.get("outcome", "")),
            "task",
        )
    for note in brief.get("lesson_notes", []):
        add(
            "Core Concepts Through Practice" if learn_by_doing else "Core Concepts",
            str(note.get("title", "")),
            [str(chunk_id) for chunk_id in note.get("source_chunk_ids", [])],
            " ".join(str(note.get(key, "")) for key in ("learning_goal", "explanation", "example")),
            "task" if learn_by_doing else "concept",
        )
    for example in brief.get("examples", []):
        add(
            "Worked Examples",
            str(example.get("title", "")),
            [str(chunk_id) for chunk_id in example.get("source_chunk_ids", [])],
            str(example.get("lesson", "")),
            "example",
        )
    for failure in brief.get("failure_modes", []):
        add(
            "Troubleshooting",
            str(failure.get("symptom", "")),
            [str(chunk_id) for chunk_id in failure.get("source_chunk_ids", [])],
            str(failure.get("fix", "")),
            "troubleshooting",
        )
    lesson_count = sum(len(section["lessons"]) for section in sections)
    if not lesson_count:
        return {}
    if lesson_count > target_max:
        remaining = target_max
        trimmed: list[dict[str, Any]] = []
        for section in sections:
            lessons = section["lessons"][:remaining]
            if lessons:
                trimmed.append({"title": section["title"], "lessons": lessons})
                remaining -= len(lessons)
            if remaining <= 0:
                break
        sections = trimmed
    return {"sections": sections, "rejected_titles": ["Source feature order; planned from task-first source brief."]}


def _fallback_course_plan_from_source_index(
    source_index: dict[str, Any],
    valid_chunk_ids: set[str],
    target_min: int,
    target_max: int,
    learn_by_doing: bool = False,
) -> dict[str, Any]:
    packs = [pack for pack in source_index.get("packs", []) if pack.get("source_chunk_ids")]
    if not packs:
        return {}
    target = max(target_min, min(target_max, len(packs)))
    window_size = max(1, (len(packs) + target - 1) // target)
    sections_by_title: dict[str, dict[str, Any]] = {}
    sections: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for window_start in range(0, len(packs), window_size):
        window = packs[window_start : window_start + window_size]
        lesson = _fallback_plan_lesson_from_pack_window(window, valid_chunk_ids, learn_by_doing)
        if not lesson:
            continue
        title_key = _normalize_title(str(lesson["title"]))
        if _title_overlaps_existing(title_key, seen_titles):
            lesson["title"] = f"{lesson['title']} ({window_start // window_size + 1})"
            title_key = _normalize_title(str(lesson["title"]))
        seen_titles.add(title_key)
        section_title = _fallback_plan_section_title(window, window_start // window_size + 1)
        section = sections_by_title.get(section_title)
        if section is None:
            section = {"title": section_title, "lessons": []}
            sections_by_title[section_title] = section
            sections.append(section)
        section["lessons"].append(lesson)
        if sum(len(section["lessons"]) for section in sections) >= target_max:
            break

    return {"sections": sections, "rejected_titles": ["LLM plan unavailable; used source-index fallback."]}


def _fallback_plan_lesson_from_pack_window(
    packs: list[dict[str, Any]],
    valid_chunk_ids: set[str],
    learn_by_doing: bool,
) -> dict[str, Any] | None:
    titles: list[str] = []
    reasons: list[str] = []
    chunk_ids: list[str] = []
    lesson_type = "task" if learn_by_doing else "concept"
    for pack in packs:
        candidates = _fallback_plan_candidates_for_pack(pack, learn_by_doing)
        for candidate in candidates[:3]:
            title = _clean_lesson_title(str(candidate.get("title", ""))) or str(candidate.get("title", "")).strip()
            if title and not _is_low_value_plan_title(_normalize_title(title)):
                titles.append(title)
            reason = str(candidate.get("reason") or candidate.get("outcome") or candidate.get("lesson") or candidate.get("purpose") or "").strip()
            if reason:
                reasons.append(reason)
            for chunk_id in candidate.get("source_chunk_ids", []):
                if str(chunk_id) in valid_chunk_ids:
                    chunk_ids.append(str(chunk_id))
        for chunk_id in pack.get("source_chunk_ids", [])[:4]:
            if str(chunk_id) in valid_chunk_ids:
                chunk_ids.append(str(chunk_id))
    chunk_ids = _dedupe_keep_order(chunk_ids)[:14]
    if not chunk_ids:
        return None
    title = titles[0] if titles else str(packs[0].get("title") or "Source workflow").strip()
    if learn_by_doing:
        title = _task_first_title(title)
    return {
        "title": title,
        "chunk_ids": chunk_ids,
        "why": _short_text(" ".join(reasons[:4]) or "Built from source-index context packs to preserve long-document coverage.", 420),
        "lesson_type": lesson_type,
    }


def _fallback_plan_candidates_for_pack(pack: dict[str, Any], learn_by_doing: bool) -> list[dict[str, Any]]:
    if learn_by_doing:
        ordered = [
            *pack.get("workflows", []),
            *pack.get("tasks", []),
            *pack.get("candidate_lessons", []),
            *pack.get("examples", []),
            *pack.get("methods", []),
        ]
    else:
        ordered = [
            *pack.get("candidate_lessons", []),
            *pack.get("methods", []),
            *pack.get("examples", []),
        ]
    normalized: list[dict[str, Any]] = []
    for item in ordered:
        title = str(item.get("title") or item.get("name") or item.get("symptom") or "").strip()
        source_chunk_ids = [str(chunk_id) for chunk_id in item.get("source_chunk_ids", [])]
        if title and source_chunk_ids:
            normalized.append({**item, "title": title, "source_chunk_ids": source_chunk_ids})
    if normalized:
        return normalized
    return [
        {
            "title": str(pack.get("title") or "Source pack").strip(),
            "reason": str(pack.get("summary", "")).strip(),
            "source_chunk_ids": list(pack.get("source_chunk_ids", [])),
        }
    ]


def _fallback_plan_section_title(packs: list[dict[str, Any]], index: int) -> str:
    for pack in packs:
        title = str(pack.get("title", "")).strip()
        if title and not _is_weak_title(title):
            return title
    return f"Source Workflow {index}"


def _repair_course_plan_coverage(
    plan: dict[str, Any],
    source_index: dict[str, Any],
    valid_chunk_ids: set[str],
    target_min: int,
) -> dict[str, Any]:
    sections = [
        {"title": str(section.get("title", "")).strip(), "lessons": list(section.get("lessons", []))}
        for section in plan.get("sections", [])
        if str(section.get("title", "")).strip() and section.get("lessons")
    ]
    lesson_count = sum(len(section["lessons"]) for section in sections)
    if lesson_count >= target_min or not source_index.get("packs"):
        return {"sections": sections, "rejected_titles": list(plan.get("rejected_titles", []))}

    seen_titles = {
        _normalize_title(str(lesson.get("title", "")))
        for section in sections
        for lesson in section.get("lessons", [])
    }
    section_by_title = {str(section["title"]): section for section in sections}
    additions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    packs = list(source_index.get("packs", []))
    max_candidates = max((len(pack.get("candidate_lessons", [])) for pack in packs), default=0)
    for candidate_index in range(max_candidates):
        for pack in packs:
            candidates = pack.get("candidate_lessons", [])
            if candidate_index >= len(candidates):
                continue
            candidate = candidates[candidate_index]
            title = _clean_lesson_title(str(candidate.get("title", "")))
            title_key = _normalize_title(title)
            chunk_ids = [
                str(chunk_id)
                for chunk_id in candidate.get("source_chunk_ids", [])
                if str(chunk_id) in valid_chunk_ids
            ]
            if (
                not title
                or not chunk_ids
                or _title_overlaps_existing(title_key, seen_titles)
                or _is_low_value_plan_title(title_key)
            ):
                continue
            additions.append(
                (
                    pack,
                    {
                        "title": title,
                        "chunk_ids": chunk_ids,
                        "why": "Added from source index to improve long-material coverage.",
                    },
                )
            )
            seen_titles.add(title_key)
            if lesson_count + len(additions) >= target_min:
                break
        if lesson_count + len(additions) >= target_min:
            break

    added_chunk_ids = {chunk_id for _, lesson in additions for chunk_id in lesson.get("chunk_ids", [])}
    if added_chunk_ids:
        _prune_existing_plan_chunks(sections, added_chunk_ids)

    for pack, lesson in additions:
        section_title = _source_section_title(pack) or str(pack.get("title") or "Supplemental Topics")
        section = section_by_title.get(section_title)
        if section is None:
            section = {"title": section_title, "lessons": []}
            sections.append(section)
            section_by_title[section_title] = section
        section["lessons"].append(lesson)

    return {"sections": sections, "rejected_titles": list(plan.get("rejected_titles", []))}


def _prune_existing_plan_chunks(sections: list[dict[str, Any]], reserved_chunk_ids: set[str]) -> None:
    for section in sections:
        pruned_lessons: list[dict[str, Any]] = []
        for lesson in section.get("lessons", []):
            chunk_ids = list(lesson.get("chunk_ids", []))
            if len(chunk_ids) > 1:
                kept = [chunk_id for chunk_id in chunk_ids if chunk_id not in reserved_chunk_ids]
                if kept:
                    lesson = dict(lesson)
                    lesson["chunk_ids"] = kept
                    pruned_lessons.append(lesson)
            else:
                pruned_lessons.append(lesson)
        section["lessons"] = pruned_lessons


def _source_section_title(pack: dict[str, Any]) -> str:
    for chunk_id in pack.get("source_chunk_ids", []):
        prefix = str(chunk_id).split("-full-chunk-", 1)[0].strip()
        if prefix:
            return prefix
    return ""


def _is_low_value_plan_title(title_key: str) -> bool:
    if len(title_key) < 4:
        return True
    low_value = {
        "目标",
        "目的与用途",
        "问题的类型",
        "两方面问题",
        "算例",
        "例题",
        "例",
        "numericalanalysis",
        "数值分析",
    }
    low_value_prefixes = ("例", "可证明", "常规命令")
    low_value_tokens = ("常规命令",)
    return (
        title_key in low_value
        or any(title_key.startswith(prefix) for prefix in low_value_prefixes)
        or any(token in title_key for token in low_value_tokens)
    )


def _title_overlaps_existing(title_key: str, seen_titles: set[str]) -> bool:
    if title_key in seen_titles:
        return True
    if len(title_key) < 5:
        return False
    for seen in seen_titles:
        if len(seen) < 5:
            continue
        if title_key in seen or seen in title_key:
            return True
    return False


def _merge_neighbor_groups(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    merged: list[list[dict[str, Any]]] = []
    for group in groups:
        title = str(group[0].get("title", ""))
        group_key = _group_title_key(group)
        if merged and (
            group_key == _group_title_key(merged[-1])
            or _is_attachment_title(title)
            or _is_weak_title(title)
        ):
            merged[-1].extend(group)
        else:
            merged.append(group)
    return merged


def _choose_group_title(group: list[dict[str, Any]], fallback: str, repeated_headings: set[str] | None = None) -> str:
    repeated_headings = repeated_headings or set()
    for chunk in group:
        title = _clean_lesson_title(str(chunk.get("title", "")).strip())
        if _normalize_title(title) not in repeated_headings and _is_good_lesson_title(title):
            return title
    return fallback


def _repeated_titles(chunks: list[dict[str, Any]]) -> set[str]:
    sources_by_title: dict[str, set[str]] = {}
    for chunk in chunks:
        source = str(chunk.get("source", ""))
        title = _normalize_title(str(chunk.get("title", "")))
        if title:
            sources_by_title.setdefault(title, set()).add(source)
    repeated: set[str] = set()
    for title, sources in sources_by_title.items():
        if (len(sources) >= 3 or sum(1 for chunk in chunks if _normalize_title(str(chunk.get("title", ""))) == title) >= 3) and len(title) <= 28:
            repeated.add(title)
    return repeated


def _is_major_section(title: str) -> bool:
    stripped = title.strip()
    return stripped.startswith(("一.", "二.", "三.", "四.", "五.", "六.", "七.", "1.", "2.", "3.", "4.", "5.", "6.", "7."))


def _extract_source_outlines(chunks: list[dict[str, Any]]) -> dict[str, list[str]]:
    outline_by_source: dict[str, list[str]] = {}
    for chunk in chunks:
        source = str(chunk.get("source", ""))
        lines = _meaningful_lines(str(chunk.get("content", "")), str(chunk.get("title", "")))
        for index, line in enumerate(lines):
            if _is_outline_marker(line):
                candidates = [_clean_outline_item(item) for item in lines[index + 1 : index + 16]]
                candidates = [item for item in candidates if _is_outline_item(item)]
                if candidates:
                    existing = outline_by_source.setdefault(source, [])
                    for item in candidates:
                        if item not in existing:
                            existing.append(item)
                break
    return outline_by_source


def _is_outline_marker(value: str) -> bool:
    normalized = _normalize_title(value)
    return normalized in {"目录", "本章内容", "主要内容", "主要教学内容", "内容提要", "outline", "contents"}


def _clean_outline_item(value: str) -> str:
    cleaned = value.strip().lstrip("•-0123456789.、 ")
    cleaned = re.sub(r"^\(?[一二三四五六七八九十]+[.、]\)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*\(\d+(\.\d+)?\s*课时\)\s*$", "", cleaned)
    return cleaned.strip()


def _is_outline_item(value: str) -> bool:
    normalized = _normalize_title(value)
    if len(normalized) < 4 or len(normalized) > 34:
        return False
    if _is_weak_title(normalized) or _is_attachment_title(normalized):
        return False
    return True


def _match_outline_key(title: str, lines: list[str], outline: list[str]) -> str:
    if not outline:
        return ""
    haystack = _normalize_title(" ".join([title, *lines[:4]]))
    matches = []
    for item in outline:
        key = _normalize_title(item)
        if key and (key in haystack or haystack in key):
            matches.append(key)
    return max(matches, key=len) if matches else ""


def _topic_key(title: str, lines: list[str]) -> str:
    normalized = _normalize_title(title)
    if _is_attachment_title(normalized) and lines:
        normalized = _normalize_title(lines[0])
    return normalized


def _group_title_key(group: list[dict[str, Any]]) -> str:
    for chunk in group:
        title = str(chunk.get("title", ""))
        if _is_good_lesson_title(title):
            return _normalize_title(title)
    return _normalize_title(str(group[0].get("title", ""))) if group else ""


def _normalize_title(title: str) -> str:
    value = title.strip().lower()
    value = re.sub(r"^[\-\s]+", "", value)
    value = re.sub(r"^[一二三四五六七八九十]+[.、]\s*", "", value)
    value = re.sub(r"^\d+(\.\d+)*[.、]?\s*", "", value)
    value = re.sub(r"\s+", "", value)
    return value


def _clean_lesson_title(title: str) -> str:
    cleaned = re.sub(r"^page\s+\d+\s*[:：]\s*", "", title.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+(\.\d+)*[.、]?\s*", "", cleaned).strip()
    if _is_weak_title(cleaned) or _is_attachment_title(cleaned):
        return ""
    return cleaned


def _is_attachment_title(title: str) -> bool:
    normalized = _normalize_title(title)
    return normalized.startswith(
        (
            "例",
            "example",
            "实例",
            "应用实例",
            "算法",
            "algorithm",
            "说明",
            "note",
            "证明",
            "proof",
            "小结",
            "summary",
            "定理",
            "theorem",
            "引理",
            "lemma",
            "推论",
            "corollary",
        )
    )


def _is_good_lesson_title(title: str) -> bool:
    normalized = _normalize_title(title)
    if not normalized or _is_attachment_title(normalized) or _is_weak_title(normalized):
        return False
    return len(normalized) >= 3


def _is_weak_title(title: str) -> bool:
    normalized = _normalize_title(title)
    if not normalized:
        return True
    weak_exact = {
        "课程信息",
        "教学团队",
        "方式",
        "考评方法",
        "本章内容",
        "主要内容",
        "目录",
        "问题描述",
        "问题的求解",
        "基本思想",
        "更多形式",
        "matlab命令",
        "参考资料",
        "references",
        "license",
        "acknowledgments",
        "acknowledgments in publication",
        "full license agreement",
        "publication",
        "overview",
        "introduction",
        "视觉关系与逻辑推导",
        "排版与视觉关系说明",
        "教师提示与推导",
        "关键条件说明",
        "视觉与排版说明",
        "教学素材属性",
    }
    if normalized in {_normalize_title(item) for item in weak_exact}:
        return True
    if re.match(r"chapter\d+$", normalized):
        return True
    if re.match(r"第[一二三四五六七八九十\d]+章", normalized):
        return True
    return False


def _first_meaningful_line(content: str, fallback: str) -> str:
    lines = _meaningful_lines(content, fallback)
    if lines:
        return lines[0][:240]
    return fallback[:240]


def _summarize_group(content: str, fallback: str, limit: int = 900) -> str:
    lines = _meaningful_lines(content, fallback)
    selected: list[str] = []
    total = 0
    for line in lines:
        if line in selected:
            continue
        selected.append(line)
        total += len(line)
        if total >= limit:
            break
    return "\n".join(selected) if selected else fallback


def _source_highlights_for_lesson(content: str, fallback: str, max_lines: int = 18) -> list[str]:
    lines = _meaningful_lines(content, fallback)
    highlights: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = _normalize_title(line)
        if not key or key in seen:
            continue
        if _is_low_value_source_line(line):
            continue
        highlights.append(_short_text(line, 360))
        seen.add(key)
        if len(highlights) >= max_lines:
            break
    return highlights


def _is_low_value_source_line(line: str) -> bool:
    stripped = line.strip()
    normalized = _normalize_title(stripped)
    if not normalized:
        return True
    if stripped in {"$$", "\\[", "\\]", "\\(", "\\)"}:
        return True
    if "??" in stripped and len(stripped) < 60:
        return True
    if stripped.startswith("!["):
        return True
    if re.match(r"^page\s+\d+\s*[:：]?", stripped, flags=re.IGNORECASE):
        return True
    if stripped.startswith(("**视觉设计说明**", "**内容类型**", "*注*：底部标注", "*注*: 底部标注")):
        return True
    if normalized in {"问题描述", "问题的求解", "图表解释", "教师提示", "续", "continued"}:
        return True
    if len(normalized) <= 2 and not re.search(r"[=<>≈∫Σ\\]", stripped):
        return True
    return False


def _meaningful_lines(content: str, fallback: str) -> list[str]:
    title = fallback.strip().lstrip("#").strip()
    lines: list[str] = []
    skip_block = False
    for raw in content.splitlines():
        clean = raw.strip().lstrip("#").strip()
        if clean.startswith("<details>"):
            skip_block = True
            continue
        if clean.startswith("</details>"):
            skip_block = False
            continue
        if skip_block:
            continue
        if not clean or clean == title:
            continue
        if _is_low_value_source_line(clean):
            continue
        if clean.startswith(("<summary>", "natural_image", "text_image")):
            continue
        if clean.startswith(("http://", "https://")) and len(clean) < 90:
            continue
        lines.append(clean)
    return lines


def _classify_content_type(content: str) -> str:
    lowered = content.lower()
    if "needs_confirmation" in lowered or "todo" in lowered or "待确认" in content:
        return "needs_confirmation"
    if "bridge" in lowered or "桥接" in content:
        return "bridge"
    if "inferred_from_source" in lowered or "推断" in content:
        return "inferred_from_source"
    return "source_supported"


def _fallback_logic_graph(state: CourseCompileState) -> dict[str, Any]:
    nodes = [
        {
            "id": unit["id"],
            "title": unit["title"],
            "content_type": unit["content_type"],
            "source_provenance": unit.get("source_provenance", []),
        }
        for unit in state["units"]
    ]
    edges = [
        {
            "from": state["units"][index - 1]["id"],
            "to": state["units"][index]["id"],
            "relation": "precedes",
        }
        for index in range(1, len(state["units"]))
    ]
    return {"nodes": nodes, "edges": edges, "strategy": "local_order_fallback"}


def _normalize_logic_graph(raw: dict[str, Any], state: CourseCompileState) -> dict[str, Any]:
    unit_by_id = {unit["id"]: unit for unit in state["units"]}
    nodes: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for item in raw.get("nodes", []):
        unit_id = str(item.get("id") or item.get("unit_id", "")).strip()
        if unit_id not in unit_by_id or unit_id in seen_nodes:
            continue
        unit = unit_by_id[unit_id]
        nodes.append(
            {
                "id": unit_id,
                "title": str(item.get("title") or unit.get("title", "")).strip(),
                "content_type": unit.get("content_type", "source_supported"),
                "role": str(item.get("role", "")).strip(),
                "source_provenance": unit.get("source_provenance", []),
            }
        )
        seen_nodes.add(unit_id)
    for unit in state["units"]:
        if unit["id"] not in seen_nodes:
            nodes.append(
                {
                    "id": unit["id"],
                    "title": unit["title"],
                    "content_type": unit.get("content_type", "source_supported"),
                    "role": "",
                    "source_provenance": unit.get("source_provenance", []),
                }
            )
    valid_relations = {"precedes", "requires", "supports", "contrasts", "expands", "example_of"}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for item in raw.get("edges", []):
        source = str(item.get("from") or item.get("source", "")).strip()
        target = str(item.get("to") or item.get("target", "")).strip()
        relation = str(item.get("relation", "supports")).strip() or "supports"
        if relation not in valid_relations:
            relation = "supports"
        key = (source, target, relation)
        if source not in unit_by_id or target not in unit_by_id or source == target or key in seen_edges:
            continue
        edges.append(
            {
                "from": source,
                "to": target,
                "relation": relation,
                "reason": _short_text(str(item.get("reason", "")).strip(), 240),
            }
        )
        seen_edges.add(key)
    return {"nodes": nodes, "edges": edges, "strategy": "llm_structure_logic"}


def organize_logic(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Organize learning-unit relations with an LLM-first path."""

    course_path = course_dir(Path(vault_root), state["course_id"])
    if not _use_llm_structure(state):
        logic_graph = _fallback_logic_graph(state)
        state["logic_graph"] = logic_graph
        write_json(course_path / "logic_graph.json", logic_graph)
        state["next_action"] = "detect_gaps"
        return state

    client = LLMClient.from_env()
    fallback_graph = _fallback_logic_graph(state)
    if client is None:
        state["logic_graph"] = fallback_graph
        return _emergency_structure_fallback(
            state,
            vault_root,
            "organize_logic",
            "LLM logic organization is enabled but no LLM client is configured.",
            {"logic_graph": fallback_graph, "fallback_kind": "local_order_bad_sample"},
        )

    system = (
        "You are the logic-organization agent in a course compiler. Build a compact relation graph between learning units. "
        "Return strict JSON only and preserve unit ids."
    )
    user = (
        "Return JSON with schema:\n"
        "{\"nodes\":[{\"id\":\"unit-001\",\"title\":\"...\",\"role\":\"foundation|method|example|application|pitfall\"}],"
        "\"edges\":[{\"from\":\"unit-001\",\"to\":\"unit-002\",\"relation\":\"precedes|requires|supports|contrasts|expands|example_of\",\"reason\":\"...\"}]}\n\n"
        "Requirements:\n"
        "- Prefer learning prerequisites and semantic relations over simple adjacency.\n"
        "- Do not create new units or edges to missing ids.\n"
        "- Do not promote examples, image captions, page metadata, author notes, or teacher hints into graph nodes; use existing unit ids only.\n\n"
        f"Units:\n{_compact_units_for_prompt(state['units'], max_chars=10500, summary_chars=240)}"
    )
    try:
        raw, meta = _complete_structure_json(
            client,
            course_path,
            "structure-logic",
            system,
            user,
            "logic_graph",
            refresh=bool(state.get("compile_profile", {}).get("refresh_llm_structure")),
            prompt_char_limit=_llm_prompt_char_limit(state),
        )
        logic_graph = _normalize_logic_graph(raw, state)
        state["logic_graph"] = logic_graph
        write_json(course_path / "logic_graph.json", logic_graph)
        write_json(course_path / "logic_graph_meta.json", {"node": "organize_logic", "llm": meta})
        state["next_action"] = "detect_gaps"
        return state
    except Exception as exc:  # pragma: no cover - external LLM behavior
        state["logic_graph"] = fallback_graph
        return _emergency_structure_fallback(
            state,
            vault_root,
            "organize_logic",
            f"LLM logic organization failed; emergency fallback requires human review: {exc}",
            {"logic_graph": fallback_graph, "fallback_kind": "local_order_bad_sample"},
        )



def _fallback_gap_report(state: CourseCompileState) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for unit in state["units"]:
        summary = str(unit.get("summary", "")).strip()
        if not summary:
            items.append({"unit_id": unit["id"], "type": "empty_summary", "severity": "high"})
        elif len(summary) < 12:
            items.append({"unit_id": unit["id"], "type": "thin_explanation", "severity": "medium"})

        if unit.get("content_type") == "needs_confirmation":
            items.append({"unit_id": unit["id"], "type": "needs_confirmation", "severity": "high"})
        elif unit.get("content_type") == "bridge":
            items.append({"unit_id": unit["id"], "type": "bridge_content", "severity": "low"})

        if _bad_independent_lesson_title(str(unit.get("title", ""))):
            items.append({"unit_id": unit["id"], "type": "bad_independent_fragment", "severity": "high"})

    titles: dict[str, str] = {}
    for unit in state["units"]:
        key = _normalize_title(str(unit.get("title", "")))
        if key in titles:
            items.append({"unit_id": unit["id"], "type": "duplicate_title", "severity": "high", "duplicates": titles[key]})
        elif key:
            titles[key] = unit["id"]

    return {
        "ok": not any(item["severity"] == "high" for item in items),
        "items": items,
        "summary": {
            "total": len(items),
            "high": sum(1 for item in items if item["severity"] == "high"),
            "medium": sum(1 for item in items if item["severity"] == "medium"),
            "low": sum(1 for item in items if item["severity"] == "low"),
        },
        "strategy": "local_gap_fallback",
    }


def _normalize_gap_report(raw: dict[str, Any], state: CourseCompileState) -> dict[str, Any]:
    valid_units = {unit["id"] for unit in state["units"]}
    unit_title_by_id = {unit["id"]: _normalize_title(str(unit.get("title", ""))) for unit in state["units"]}
    unit_ids_by_title: dict[str, list[str]] = {}
    for unit_id, title_key in unit_title_by_id.items():
        if title_key:
            unit_ids_by_title.setdefault(title_key, []).append(unit_id)
    allowed_types = {
        "empty_summary",
        "thin_explanation",
        "needs_confirmation",
        "bridge_content",
        "duplicate_title",
        "bad_independent_fragment",
        "missing_source_provenance",
        "coverage_gap",
        "ordering_gap",
        "over_split_fragment",
    }
    items: list[dict[str, Any]] = []
    for item in raw.get("items", []):
        unit_id = str(item.get("unit_id", "")).strip()
        gap_type = str(item.get("type", "")).strip()
        if unit_id and unit_id not in valid_units:
            continue
        if gap_type not in allowed_types:
            gap_type = "coverage_gap"
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        if gap_type == "duplicate_title":
            exact_duplicates = unit_ids_by_title.get(unit_title_by_id.get(unit_id, ""), [])
            if len(exact_duplicates) < 2:
                gap_type = "over_split_fragment"
                if severity == "high":
                    severity = "medium"
        items.append(
            {
                "unit_id": unit_id,
                "type": gap_type,
                "severity": severity,
                "message": _short_text(str(item.get("message", "")).strip(), 360),
                "source_chunk_ids": [str(chunk_id) for chunk_id in item.get("source_chunk_ids", [])],
            }
        )
    local = _fallback_gap_report(state)
    for item in local["items"]:
        key = (item.get("unit_id", ""), item.get("type", ""))
        if key not in {(existing.get("unit_id", ""), existing.get("type", "")) for existing in items}:
            items.append(item)
    return {
        "ok": not any(item["severity"] == "high" for item in items),
        "items": items,
        "summary": {
            "total": len(items),
            "high": sum(1 for item in items if item["severity"] == "high"),
            "medium": sum(1 for item in items if item["severity"] == "medium"),
            "low": sum(1 for item in items if item["severity"] == "low"),
        },
        "strategy": "llm_gap_detection_with_rule_checks",
    }


def detect_gaps(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Detect learning-structure gaps with an LLM-first path plus rule checks."""

    course_path = course_dir(Path(vault_root), state["course_id"])
    if not _use_llm_structure(state):
        gap_report = _fallback_gap_report(state)
        state["gap_report"] = gap_report
        write_json(course_path / "gap_report.json", gap_report)
        state["next_action"] = "generate_lessons"
        return state

    client = LLMClient.from_env()
    fallback_report = _fallback_gap_report(state)
    if client is None:
        state["gap_report"] = fallback_report
        return _emergency_structure_fallback(
            state,
            vault_root,
            "detect_gaps",
            "LLM gap detection is enabled but no LLM client is configured.",
            {"gap_report": fallback_report, "fallback_kind": "local_gap_bad_sample"},
        )

    system = (
        "You are the gap-detection agent in a course compiler. Review units and the logic graph for learning-quality failures. "
        "Return strict JSON only."
    )
    user = (
        "Return JSON with schema:\n"
        "{\"items\":[{\"unit_id\":\"unit-001\",\"type\":\"empty_summary|thin_explanation|needs_confirmation|bridge_content|"
        "duplicate_title|bad_independent_fragment|missing_source_provenance|coverage_gap|ordering_gap|over_split_fragment\","
        "\"severity\":\"high|medium|low\",\"message\":\"...\",\"source_chunk_ids\":[\"...\"]}]}\n\n"
        "Must flag as high severity when a standalone unit is only a short concept, teacher hint, image caption, page note, PPT author metadata, layout note, or duplicate title.\n\n"
        f"Units:\n{_compact_units_for_prompt(state['units'], max_chars=9000, summary_chars=220)}\n\n"
        f"Logic graph:\n{_compact_logic_graph_for_prompt(state.get('logic_graph', {}), max_chars=3000)}"
    )
    try:
        raw, meta = _complete_structure_json(
            client,
            course_path,
            "structure-gaps",
            system,
            user,
            "gap_report",
            refresh=bool(state.get("compile_profile", {}).get("refresh_llm_structure")),
            prompt_char_limit=_llm_prompt_char_limit(state),
        )
        gap_report = _normalize_gap_report(raw, state)
        state["gap_report"] = gap_report
        write_json(course_path / "gap_report.json", gap_report)
        write_json(course_path / "gap_report_meta.json", {"node": "detect_gaps", "llm": meta})
        state["next_action"] = "generate_lessons"
        return state
    except Exception as exc:  # pragma: no cover - external LLM behavior
        state["gap_report"] = fallback_report
        return _emergency_structure_fallback(
            state,
            vault_root,
            "detect_gaps",
            f"LLM gap detection failed; emergency fallback requires human review: {exc}",
            {"gap_report": fallback_report, "fallback_kind": "local_gap_bad_sample"},
        )


def _fallback_lessons_from_units(state: CourseCompileState) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    for index, unit in enumerate(state["units"], start=1):
        lesson_id = f"lesson-{index:03d}"
        lessons.append(
            {
                "id": lesson_id,
                "title": unit["title"],
                "section_title": unit.get("section_title", ""),
                "lesson_type": unit.get("lesson_type", ""),
                "unit_ids": [unit["id"]],
                "content_type": unit["content_type"],
                "body": _lesson_body(unit),
                "body_max_chars": unit.get("lesson_body_max_chars", 1200),
                "checklist": [f"理解：{unit['title']}"],
                "sources": [
                    {
                        "source": ref["source"],
                        "source_file": ref.get("source_file", ref["source"]),
                        "page": ref.get("page"),
                        "block_id": ref.get("block_id", ref.get("chunk_id", "")),
                        "bbox": ref.get("bbox", []),
                        "source_order": ref.get("source_order", index),
                        "source_id": ref.get("source_id", ref["chunk_id"]),
                        "chunk_id": ref["chunk_id"],
                        "quote": ref["quote"],
                    }
                    for ref in unit.get("source_refs", [])
                ]
                or [
                    {
                        "source": unit["source"],
                        "source_file": unit.get("source", ""),
                        "page": None,
                        "block_id": unit["source_chunk_id"],
                        "bbox": [],
                        "source_order": index,
                        "source_id": unit["source_chunk_id"],
                        "chunk_id": unit["source_chunk_id"],
                        "quote": unit["source_quote"],
                    }
                ],
                "images": list(unit.get("image_refs", [])),
                "order": index,
            }
        )

    concepts = [
        {
            "id": f"concept-{index:03d}",
            "title": unit["title"],
            "unit_id": unit["id"],
            "source_chunk_id": unit["source_chunk_id"],
            "status": "introduced",
        }
        for index, unit in enumerate(state["units"], start=1)
    ]
    outline = {
        "course_id": state["course_id"],
        "lesson_count": len(lessons),
        "sections": _outline_sections(lessons),
        "lessons": [
            {
                "id": lesson["id"],
                "title": lesson["title"],
                "section_title": lesson.get("section_title", ""),
                "lesson_type": lesson.get("lesson_type", ""),
                "unit_ids": lesson["unit_ids"],
                "source_count": len(lesson["sources"]),
            }
            for lesson in lessons
        ],
    }
    return lessons, concepts, outline


def _normalize_llm_lessons(raw: dict[str, Any], state: CourseCompileState) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    unit_by_id = {unit["id"]: unit for unit in state["units"]}
    lessons: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_units: set[str] = set()
    for index, item in enumerate(raw.get("lessons", []), start=1):
        unit_ids = [str(unit_id) for unit_id in item.get("unit_ids", []) if str(unit_id) in unit_by_id]
        if not unit_ids:
            rejected.append({"title": item.get("title", ""), "reason": "missing_valid_unit_ids"})
            continue
        title = _clean_lesson_title(str(item.get("title", "")).strip()) or str(item.get("title", "")).strip()
        if _bad_independent_lesson_title(title):
            rejected.append({"title": title, "reason": "bad_independent_lesson_title", "unit_ids": unit_ids})
            if lessons:
                lessons[-1]["unit_ids"] = _dedupe_keep_order(lessons[-1]["unit_ids"] + unit_ids)
            continue
        units = [unit_by_id[unit_id] for unit_id in unit_ids]
        sources: list[dict[str, Any]] = []
        for unit in units:
            sources.extend(unit.get("source_refs", []))
        normalized_sources = [
            {
                "source": ref.get("source", ref.get("source_file", "")),
                "source_file": ref.get("source_file", ref.get("source", "")),
                "page": ref.get("page"),
                "block_id": ref.get("block_id", ref.get("chunk_id", "")),
                "bbox": ref.get("bbox", []),
                "source_order": ref.get("source_order", source_index),
                "source_id": ref.get("source_id", ref.get("chunk_id", "")),
                "chunk_id": ref.get("chunk_id", ""),
                "quote": ref.get("quote", ""),
            }
            for source_index, ref in enumerate(sources, start=1)
            if ref.get("chunk_id")
        ]
        body = str(item.get("body", "")).strip() or "\n\n".join(_lesson_body(unit) for unit in units)
        checklist = [str(value).strip() for value in item.get("checklist", []) if str(value).strip()]
        if not checklist:
            checklist = [f"理解：{title}"]
        lesson = {
            "id": f"lesson-{len(lessons) + 1:03d}",
            "title": title,
            "section_title": str(item.get("section_title") or units[0].get("section_title", "")).strip(),
            "lesson_type": _normalize_lesson_type(str(item.get("lesson_type") or units[0].get("lesson_type", "")).strip()),
            "unit_ids": unit_ids,
            "content_type": units[0].get("content_type", "source_supported"),
            "body": body,
            "body_max_chars": max(int(unit.get("lesson_body_max_chars", 1200)) for unit in units),
            "checklist": checklist[:8],
            "sources": normalized_sources,
            "images": [image for unit in units for image in unit.get("image_refs", [])][:6],
            "order": len(lessons) + 1,
        }
        lessons.append(lesson)
        used_units.update(unit_ids)

    for unit in state["units"]:
        if unit["id"] not in used_units and not _bad_independent_lesson_title(str(unit.get("title", ""))):
            fallback_lesson, _concepts, _outline = _fallback_lessons_from_units({**state, "units": [unit]})  # type: ignore[arg-type]
            if fallback_lesson:
                lesson = fallback_lesson[0]
                lesson["id"] = f"lesson-{len(lessons) + 1:03d}"
                lesson["order"] = len(lessons) + 1
                lessons.append(lesson)
                rejected.append({"title": unit.get("title", ""), "reason": "llm_omitted_unit_added_by_rule", "unit_ids": [unit["id"]]})

    concepts = [
        {
            "id": f"concept-{index:03d}",
            "title": unit["title"],
            "unit_id": unit["id"],
            "source_chunk_id": unit["source_chunk_id"],
            "source_provenance": unit.get("source_provenance", []),
            "status": "introduced",
        }
        for index, unit in enumerate(state["units"], start=1)
    ]
    outline = {
        "course_id": state["course_id"],
        "lesson_count": len(lessons),
        "sections": _outline_sections(lessons),
        "lessons": [
            {
                "id": lesson["id"],
                "title": lesson["title"],
                "section_title": lesson.get("section_title", ""),
                "lesson_type": lesson.get("lesson_type", ""),
                "unit_ids": lesson["unit_ids"],
                "source_count": len(lesson["sources"]),
            }
            for lesson in lessons
        ],
    }
    return lessons, concepts, outline, rejected


def _generate_lessons_system_prompt() -> str:
    return (
        "You are the lesson-drafting agent in a course compiler. Draft readable, source-grounded lesson records from validated units. "
        "Return strict JSON only."
    )


def _generate_lessons_user_prompt(
    state: CourseCompileState,
    generation_evidence: dict[str, Any],
    units: list[dict[str, Any]],
    units_max_chars: int = 7600,
    evidence_max_chars: int = 1600,
    gap_max_chars: int = 1600,
    graph_max_chars: int = 1600,
) -> str:
    return (
        "Return JSON with schema:\n"
        "{\"lessons\":[{\"title\":\"...\",\"section_title\":\"...\",\"lesson_type\":\"concept|task|example|troubleshooting|reference\","
        "\"unit_ids\":[\"unit-001\"],\"body\":\"...\",\"checklist\":[\"...\"]}],"
        "\"rejected_fragments\":[{\"title\":\"...\",\"reason\":\"...\",\"unit_ids\":[\"...\"]}]}\n\n"
        "Requirements:\n"
        "- Generate medium-grain lessons only. Do not create standalone lessons for short concepts, teacher hints, image captions, page notes, PPT author metadata, or layout-only comments.\n"
        "- Merge duplicate titles and attach examples or notes to their parent conceptual lesson.\n"
        "- Every lesson must cite existing unit_ids only; provenance will be copied from those units.\n"
        "- Body should be a concise draft; detailed body writing can happen later.\n\n"
        f"Units:\n{_compact_units_for_prompt(units, max_chars=units_max_chars, summary_chars=180)}\n\n"
        f"Pre-generation evidence:\n{_lesson_generation_evidence_for_prompt(generation_evidence, max_chars=evidence_max_chars)}\n\n"
        f"Gap report:\n{_compact_gap_report_for_prompt(state.get('gap_report', {}), max_chars=gap_max_chars)}\n\n"
        f"Logic graph:\n{_compact_logic_graph_for_prompt(state.get('logic_graph', {}), max_chars=graph_max_chars)}"
    )


def _unit_batches_for_lesson_generation(
    state: CourseCompileState,
    generation_evidence: dict[str, Any],
    system: str,
    prompt_limit: int,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for unit in state["units"]:
        candidate = current + [unit]
        user = _generate_lessons_user_prompt(state, generation_evidence, candidate)
        if _prompt_char_count(system, user) <= prompt_limit:
            current = candidate
            continue
        if current:
            batches.append(current)
            current = [unit]
            user = _generate_lessons_user_prompt(state, generation_evidence, current, units_max_chars=5200, evidence_max_chars=900, gap_max_chars=900, graph_max_chars=900)
        if _prompt_char_count(system, user) > prompt_limit:
            batches.append([unit])
            current = []
    if current:
        batches.append(current)
    return batches


def _complete_generate_lessons_json(
    client: Any,
    target_dir: Path,
    state: CourseCompileState,
    generation_evidence: dict[str, Any],
    refresh: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = _generate_lessons_system_prompt()
    prompt_limit = _llm_prompt_char_limit(state)
    direct_user = _generate_lessons_user_prompt(state, generation_evidence, state["units"])
    if _prompt_char_count(system, direct_user) <= prompt_limit:
        return _complete_structure_json(
            client,
            target_dir,
            "structure-lessons",
            system,
            direct_user,
            "lessons_result",
            refresh=refresh,
            prompt_char_limit=prompt_limit,
        )

    batches = _unit_batches_for_lesson_generation(state, generation_evidence, system, prompt_limit)
    all_lessons: list[dict[str, Any]] = []
    all_rejected: list[dict[str, Any]] = []
    batch_meta: list[dict[str, Any]] = []
    for index, units in enumerate(batches, start=1):
        user = _generate_lessons_user_prompt(state, generation_evidence, units, units_max_chars=7000, evidence_max_chars=1200, gap_max_chars=1200, graph_max_chars=1200)
        if _prompt_char_count(system, user) > prompt_limit:
            user = _generate_lessons_user_prompt(state, generation_evidence, units, units_max_chars=5200, evidence_max_chars=800, gap_max_chars=800, graph_max_chars=800)
        raw, meta = _complete_structure_json(
            client,
            target_dir,
            f"structure-lessons-batch-{index:03d}",
            system,
            user,
            "lessons_result",
            refresh=refresh,
            prompt_char_limit=prompt_limit,
        )
        all_lessons.extend(raw.get("lessons", []))
        all_rejected.extend(raw.get("rejected_fragments", []))
        batch_meta.append(
            {
                **meta,
                "batch_index": index,
                "batch_count": len(batches),
                "unit_ids": [str(unit.get("id", "")) for unit in units],
            }
        )
    return {"lessons": all_lessons, "rejected_fragments": all_rejected}, {
        "local_cache": "split_batches",
        "batch_count": len(batches),
        "prompt_char_limit": prompt_limit,
        "batches": batch_meta,
    }


def generate_lessons(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Generate lesson drafts with an LLM-first path and provenance enforcement."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    generation_evidence = _persist_lesson_generation_evidence(state, vault_root)
    if not _use_llm_structure(state):
        lessons, concepts, outline = _fallback_lessons_from_units(state)
        state["lessons"] = lessons
        _attach_pending_images_to_last_lesson(state["lessons"], state.get("image_understanding", {}))
        _persist_lesson_evidence(state, vault_root)
        state["concepts"] = concepts
        state["outline"] = outline
        write_json(target_dir / "outline.json", outline)
        write_json(target_dir / "concepts.json", concepts)
        write_json(target_dir / "lessons.json", lessons)
        state["next_action"] = "synthesize_compile_plan"
        return state

    client = LLMClient.from_env()
    fallback_lessons, fallback_concepts, fallback_outline = _fallback_lessons_from_units(state)
    if client is None:
        state["lessons"] = fallback_lessons
        state["concepts"] = fallback_concepts
        state["outline"] = fallback_outline
        return _emergency_structure_fallback(
            state,
            vault_root,
            "generate_lessons",
            "LLM lesson draft generation is enabled but no LLM client is configured.",
            {"lessons": fallback_lessons, "concepts": fallback_concepts, "outline": fallback_outline, "fallback_kind": "local_lesson_bad_sample"},
        )

    try:
        raw, meta = _complete_generate_lessons_json(
            client,
            target_dir,
            state,
            generation_evidence,
            refresh=bool(state.get("compile_profile", {}).get("refresh_llm_structure")),
        )
        lessons, concepts, outline, rejected = _normalize_llm_lessons(raw, state)
        if not lessons:
            raise ValueError("LLM returned no valid lessons after validation")
        state["lessons"] = lessons
        _attach_pending_images_to_last_lesson(state["lessons"], state.get("image_understanding", {}))
        _persist_lesson_evidence(state, vault_root)
        state["concepts"] = concepts
        state["outline"] = outline
        write_json(target_dir / "outline.json", outline)
        write_json(target_dir / "concepts.json", concepts)
        write_json(target_dir / "lessons.json", lessons)
        write_json(target_dir / "lessons_meta.json", {"node": "generate_lessons", "llm": meta, "rejected_fragments": rejected})
        state["next_action"] = "synthesize_compile_plan"
        return state
    except Exception as exc:  # pragma: no cover - external LLM behavior
        state["lessons"] = fallback_lessons
        _persist_lesson_evidence(state, vault_root)
        state["concepts"] = fallback_concepts
        state["outline"] = fallback_outline
        return _emergency_structure_fallback(
            state,
            vault_root,
            "generate_lessons",
            f"LLM lesson draft generation failed; emergency fallback requires human review: {exc}",
            {"lessons": fallback_lessons, "concepts": fallback_concepts, "outline": fallback_outline, "fallback_kind": "local_lesson_bad_sample"},
        )


def _persist_lesson_generation_evidence(state: CourseCompileState, vault_root: Path | str = "course-vault") -> dict[str, Any]:
    locator = SourceLocator.from_state(state)
    unit_records: dict[str, Any] = {}
    for unit in state.get("units", []):
        source_ids = _unit_source_ids(unit)
        unit_records[str(unit.get("id", ""))] = {
            "unit_id": str(unit.get("id", "")),
            "title": str(unit.get("title", "")),
            **locator.get_context(source_ids, before=1, after=1),
        }
    evidence = {
        "units": unit_records,
        "summary": {
            "unit_count": len(unit_records),
            "source_id_count": sum(len(record.get("source_ids", [])) for record in unit_records.values()),
        },
    }
    write_json(course_dir(Path(vault_root), state["course_id"]) / "lesson_generation_evidence.json", evidence)
    return evidence


def _unit_source_ids(unit: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    source_ids.extend(str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []))
    if unit.get("source_chunk_id"):
        source_ids.append(str(unit.get("source_chunk_id")))
    for source in unit.get("source_refs", []):
        source_ids.append(stable_source_id(source))
    return _dedupe_keep_order(source_ids)


def _lesson_generation_evidence_for_prompt(evidence: dict[str, Any], max_chars: int = 12000) -> str:
    lines: list[str] = []
    for unit_id, record in evidence.get("units", {}).items():
        lines.append(f"- unit_id: {unit_id}")
        lines.append(f"  source_ids: {', '.join(record.get('source_ids', []))}")
        for item in record.get("evidence", [])[:4]:
            lines.append(
                f"  - source_id: {item.get('source_id')} page: {item.get('source_page')} block: {item.get('block_id')} excerpt: {_short_text(str(item.get('excerpt', '')), 220)}"
            )
        if sum(len(line) for line in lines) > max_chars:
            return "\n".join(lines)[:max_chars].rstrip() + "\n..."
    return "\n".join(lines)


def _persist_lesson_evidence(state: CourseCompileState, vault_root: Path | str = "course-vault") -> dict[str, Any]:
    locator = SourceLocator.from_state(state)
    lesson_evidence = locator.lesson_evidence(state.get("lessons", []))
    state["lesson_evidence"] = lesson_evidence
    write_json(course_dir(Path(vault_root), state["course_id"]) / "lesson_evidence.json", lesson_evidence)
    return lesson_evidence


def synthesize_compile_plan(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Write a machine-readable and review-readable synthesis plan before body generation."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    compile_plan = _build_compile_plan(state)
    state["compile_plan"] = compile_plan
    state["compile_plan_revisions"] = []
    write_json(target_dir / "compile_plan.json", compile_plan)
    (target_dir / "compile_plan.md").write_text(_render_compile_plan_markdown(compile_plan), encoding="utf-8")
    state["next_action"] = "review_compile_plan_llm"
    return state


def review_compile_plan_llm(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Review the synthesis plan; failed reviews must enter revise before body generation."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    plan = state.get("compile_plan") or _build_compile_plan(state)
    plan_prompt = _compile_plan_review_prompt_payload(plan)
    local_review = _local_compile_plan_review(plan)
    llm_required = _requires_compile_plan_llm_review(state)
    client = LLMClient.from_env() if llm_required else None
    llm_review: dict[str, Any] = {"passed": True, "issues": [], "revise_prompt": {}, "metadata": {"mode": "local_rule_review"}}

    if llm_required and client is None:
        llm_review = {
            "passed": False,
            "issues": [
                {
                    "type": "missing_llm_reviewer",
                    "severity": "high",
                    "message": "Compile-plan LLM review is required but no LLM client is configured.",
                    "lesson_ids": [],
                }
            ],
            "revise_prompt": {
                "objective": "Pause body generation until an LLM reviewer is configured or the user explicitly reviews the compile plan.",
                "actions": ["human_review_required"],
            },
            "metadata": {"mode": "missing_llm"},
        }
    elif client is not None:
        system = (
            "You are a compile-plan reviewer for a course compiler. Review the plan before lesson body generation. "
            "Return strict JSON only."
        )
        user = (
            f"{_review_feedback_for_prompt(state.get('compile_profile', {}))}"
            "Review this Markdown synthesis plan and return JSON with schema:\n"
            "{\"passed\":false,\"issues\":[{\"type\":\"granularity|short_fragment_chapter|duplicate_title|image_random_insert|"
            "visual_note_pollution|formula_markdown_risk|source_gap|other\",\"severity\":\"high|medium|low\","
            "\"message\":\"...\",\"lesson_ids\":[\"lesson-001\"],\"unit_ids\":[\"unit-001\"]}],"
            "\"revise_prompt\":{\"objective\":\"...\",\"actions\":[\"merge_lessons|rename_lesson|move_image|remove_visual_note|flag_manual_confirmation\"],"
            "\"details\":[{\"target\":\"lesson-001\",\"instruction\":\"...\"}]}}\n\n"
            "Must check: uneven lesson granularity, short concepts as standalone chapters, duplicate titles, random image insertion, visual/layout notes polluting body, and formulas mixed with Markdown lists/tables.\n\n"
            "Important: this review happens before final lesson body generation. Concise draft bodies and low estimated token counts are acceptable when unit_ids and source_blocks are present. "
            "Do not fail a lesson only because source_pages is empty; many parsed chunks have no page number, and source_blocks/source chunk ids are authoritative. "
            "Use high severity only for missing unit/source-block coverage, monolithic or duplicate lesson structure, standalone metadata fragments, or image ownership that contradicts source_chunk_ids.\n\n"
            f"Compile plan summary:\n{plan_prompt}"
        )
        try:
            raw, meta = _complete_structure_json(
                client,
                target_dir,
                "compile-plan-review",
                system,
                user,
                "compile_plan_review",
                refresh=bool(state.get("compile_profile", {}).get("refresh_compile_plan_review")),
                prompt_char_limit=_llm_prompt_char_limit(state),
            )
            llm_review = _normalize_compile_plan_review(raw)
            llm_review["metadata"] = meta
        except Exception as exc:  # pragma: no cover - external LLM behavior
            llm_review = {
                "passed": False,
                "issues": [
                    {
                        "type": "review_llm_error",
                        "severity": "high",
                        "message": f"Compile-plan LLM review failed: {exc}",
                        "lesson_ids": [],
                    }
                ],
                "revise_prompt": {
                    "objective": "Do not generate bodies until compile-plan review succeeds.",
                    "actions": ["human_review_required"],
                },
                "metadata": {"mode": "llm_error"},
            }

    review = _merge_compile_plan_reviews(llm_review, local_review, len(state.get("compile_plan_revisions", [])))
    state["compile_plan_review"] = review
    write_json(target_dir / "compile_plan_review.json", review)
    (target_dir / "compile_plan_review.md").write_text(_render_compile_plan_review_markdown(review), encoding="utf-8")
    state["next_action"] = "synthesize_lesson_bodies" if review.get("passed") else "revise_compile_plan"
    return state


def _compile_plan_review_prompt_payload(plan: dict[str, Any], max_chars: int = 11500) -> str:
    payload = {
        "course_id": plan.get("course_id", ""),
        "material_scope": plan.get("material_scope", {}),
        "image_insert_strategy": plan.get("image_insert_strategy", {}),
        "revision_count": plan.get("revision_count", 0),
        "lessons": [
            {
                "lesson_id": lesson.get("lesson_id", ""),
                "title": lesson.get("title", ""),
                "section_title": lesson.get("section_title", ""),
                "unit_ids": lesson.get("unit_ids", []),
                "source_pages": lesson.get("source_pages", [])[:12],
                "source_blocks": lesson.get("source_blocks", [])[:12],
                "source_count": len(lesson.get("sources", [])),
                "image_count": lesson.get("image_insert_strategy", {}).get("image_count", 0),
                "pending_images": lesson.get("image_insert_strategy", {}).get("pending_count", 0),
                "random_image_count": len(lesson.get("image_insert_strategy", {}).get("random_insertion_risk_ids", [])),
                "risks": lesson.get("risks", []),
            }
            for lesson in plan.get("hierarchy", {}).get("lessons", [])
        ],
        "risk_warnings": [risk for risk in plan.get("risk_warnings", []) if risk.get("severity") == "high"],
        "manual_confirmation_items": plan.get("manual_confirmation_items", [])[:40],
    }
    return _short_text(json.dumps(payload, ensure_ascii=False), max_chars)


def revise_compile_plan(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Apply bounded structural revisions from the review prompt and re-enter review."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    max_revisions = int(state.get("compile_profile", {}).get("compile_plan_max_revisions", 4))
    revisions = list(state.get("compile_plan_revisions", []))
    review = state.get("compile_plan_review", {})
    if len(revisions) >= max_revisions:
        if _needs_finer_split(review):
            before_titles = [lesson.get("title", "") for lesson in state.get("lessons", [])]
            revised_lessons, actions = _apply_compile_plan_revisions(state.get("lessons", []), review, state.get("units", []))
            revision_record = {
                "attempt": len(revisions) + 1,
                "review_passed": bool(review.get("passed")),
                "issues": review.get("issues", []),
                "revise_prompt": review.get("revise_prompt", {}),
                "actions": actions,
                "before_titles": before_titles,
                "after_titles": [lesson.get("title", "") for lesson in revised_lessons],
                "revision_stage": "lesson_body_finer_split_after_review_limit",
            }
            revisions.append(revision_record)
            state["compile_plan_revisions"] = revisions
            if any(action.get("action") == "split_lesson" for action in actions):
                state["lessons"] = revised_lessons
                state["outline"] = {
                    "course_id": state["course_id"],
                    "lesson_count": len(revised_lessons),
                    "sections": _outline_sections(revised_lessons),
                    "lessons": [
                        {
                            "id": lesson["id"],
                            "title": lesson["title"],
                            "section_title": lesson.get("section_title", ""),
                            "lesson_type": lesson.get("lesson_type", ""),
                            "unit_ids": lesson.get("unit_ids", []),
                            "source_count": len(lesson.get("sources", [])),
                        }
                        for lesson in revised_lessons
                    ],
                }
                state["compile_plan"] = _build_compile_plan(state)
                write_json(target_dir / "lessons.json", state["lessons"])
                write_json(target_dir / "outline.json", state["outline"])
                write_json(target_dir / "compile_plan.json", state["compile_plan"])
                (target_dir / "compile_plan.md").write_text(_render_compile_plan_markdown(state["compile_plan"]), encoding="utf-8")
                write_json(
                    target_dir / "compile_plan_revision_log.json",
                    {"revisions": revisions, "max_revisions": max_revisions, "status": "lesson_body_finer_split_after_review_limit"},
                )
                state["next_action"] = "synthesize_lesson_bodies"
                return state
            write_json(target_dir / "compile_plan_revision_log.json", {"revisions": revisions, "max_revisions": max_revisions, "status": "fallback_generate_lessons"})
            state["next_action"] = "generate_lessons"
            return state
        if max_revisions >= 4 and _can_continue_after_compile_plan_revision_exhaustion(review):
            write_json(
                target_dir / "compile_plan_revision_log.json",
                {
                    "revisions": revisions,
                    "max_revisions": max_revisions,
                    "status": "advisory_exhausted_continue",
                    "continued_after_advisory_llm_review": True,
                },
            )
            state["next_action"] = "synthesize_lesson_bodies"
            return state
        state["errors"].append(
            {
                "node": "revise_compile_plan",
                "message": f"Compile plan failed review after {len(revisions)} revision attempts.",
                "requires_human_review": True,
            }
        )
        state["validation_report"] = {
            "ok": False,
            "checks": ["compile_plan_review"],
            "failures": review.get("issues", []),
        }
        write_json(target_dir / "compile_plan_revision_log.json", {"revisions": revisions, "max_revisions": max_revisions, "status": "exhausted"})
        state["next_action"] = "human_review"
        return state

    before_titles = [lesson.get("title", "") for lesson in state.get("lessons", [])]
    revised_lessons, actions = _apply_compile_plan_revisions(state.get("lessons", []), review, state.get("units", []))
    state["lessons"] = revised_lessons
    state["outline"] = {
        "course_id": state["course_id"],
        "lesson_count": len(revised_lessons),
        "sections": _outline_sections(revised_lessons),
        "lessons": [
            {
                "id": lesson["id"],
                "title": lesson["title"],
                "section_title": lesson.get("section_title", ""),
                "lesson_type": lesson.get("lesson_type", ""),
                "unit_ids": lesson.get("unit_ids", []),
                "source_count": len(lesson.get("sources", [])),
            }
            for lesson in revised_lessons
        ],
    }
    revision_record = {
        "attempt": len(revisions) + 1,
        "review_passed": bool(review.get("passed")),
        "issues": review.get("issues", []),
        "revise_prompt": review.get("revise_prompt", {}),
        "actions": actions,
        "before_titles": before_titles,
        "after_titles": [lesson.get("title", "") for lesson in revised_lessons],
    }
    revisions.append(revision_record)
    state["compile_plan_revisions"] = revisions
    state["compile_plan"] = _build_compile_plan(state)
    write_json(target_dir / "lessons.json", state["lessons"])
    write_json(target_dir / "outline.json", state["outline"])
    write_json(target_dir / "compile_plan.json", state["compile_plan"])
    (target_dir / "compile_plan.md").write_text(_render_compile_plan_markdown(state["compile_plan"]), encoding="utf-8")
    if _needs_finer_split(review) and not any(action.get("action") == "split_lesson" for action in actions):
        write_json(target_dir / "compile_plan_revision_log.json", {"revisions": revisions, "max_revisions": max_revisions, "status": "fallback_generate_lessons"})
        state["next_action"] = "generate_lessons"
        return state
    write_json(target_dir / "compile_plan_revision_log.json", {"revisions": revisions, "max_revisions": max_revisions, "status": "revised"})
    state["next_action"] = "review_compile_plan_llm"
    return state


def _can_continue_after_compile_plan_revision_exhaustion(review: dict[str, Any]) -> bool:
    issues = list(review.get("issues", []))
    if any(str(issue.get("type", "")) == "needs_finer_split" or issue.get("needs_finer_split") for issue in issues):
        return False
    local_meta = review.get("local_metadata", {})
    if local_meta.get("mode") != "local_rule_review":
        return False
    local_high = [
        issue
        for issue in issues
        if issue.get("severity") == "high" and str(issue.get("message", "")) in {"image_random_insert", "thin_lesson_draft", "short_fragment_chapter"}
    ]
    return not local_high


def _requires_compile_plan_llm_review(state: CourseCompileState) -> bool:
    profile = state.get("compile_profile", {})
    return bool(
        profile.get(
            "use_llm_compile_plan_review",
            profile.get("use_llm", False) or profile.get("use_llm_structure", False) or profile.get("use_llm_lesson_bodies", False),
        )
    )


def _build_compile_plan(state: CourseCompileState) -> dict[str, Any]:
    lessons = list(state.get("lessons", []))
    lesson_plans: list[dict[str, Any]] = []
    total_estimated_tokens = 0
    for lesson in lessons:
        sources = list(lesson.get("sources", []))
        source_pages = _dedupe_keep_order([str(source.get("page")) for source in sources if source.get("page") is not None])
        source_blocks = _dedupe_keep_order([str(source.get("block_id") or source.get("chunk_id", "")) for source in sources if source.get("block_id") or source.get("chunk_id")])
        image_strategy = _lesson_image_strategy(lesson)
        estimated_tokens = _estimate_lesson_tokens(lesson)
        density_estimate = _compile_plan_lesson_density_estimate(lesson, state)
        total_estimated_tokens += estimated_tokens
        lesson_plans.append(
            {
                "lesson_id": lesson.get("id", ""),
                "title": lesson.get("title", ""),
                "section_title": lesson.get("section_title", ""),
                "lesson_type": lesson.get("lesson_type", ""),
                "unit_ids": lesson.get("unit_ids", []),
                "source_pages": source_pages,
                "source_blocks": source_blocks,
                "sources": [
                    {
                        "source_file": source.get("source_file", source.get("source", "")),
                        "page": source.get("page"),
                        "block_id": source.get("block_id", source.get("chunk_id", "")),
                        "bbox": source.get("bbox", []),
                        "source_order": source.get("source_order"),
                        "chunk_id": source.get("chunk_id", ""),
                        "quote": _short_text(str(source.get("quote", "")), 180),
                    }
                    for source in sources
                ],
                "image_insert_strategy": image_strategy,
                "estimated_tokens": estimated_tokens,
                "body_density_estimate": density_estimate,
                "risks": _lesson_plan_risks(lesson),
            }
        )
    material_scope = {
        "source_files": list(state.get("source_files", [])),
        "parsed_chunk_count": len(state.get("parsed_chunks", [])),
        "unit_count": len(state.get("units", [])),
        "lesson_count": len(lessons),
        "source_pages": _dedupe_keep_order(
            [
                str(source.get("page"))
                for lesson in lessons
                for source in lesson.get("sources", [])
                if source.get("page") is not None
            ]
        ),
        "source_blocks": _dedupe_keep_order(
            [
                str(source.get("block_id") or source.get("chunk_id", ""))
                for lesson in lessons
                for source in lesson.get("sources", [])
                if source.get("block_id") or source.get("chunk_id")
            ]
        ),
    }
    return {
        "course_id": state["course_id"],
        "material_scope": material_scope,
        "hierarchy": {
            "sections": _outline_sections(lessons),
            "lessons": lesson_plans,
        },
        "image_insert_strategy": {
            "recognized_images": sum(len(lesson.get("images", [])) for lesson in lessons),
            "pending_confirmation": sum(len(lesson.get("pending_image_confirmations", [])) for lesson in lessons),
            "policy": "Insert only recognized images in lessons whose source chunks support the image; keep uncertain images in pending confirmation.",
        },
        "estimated_tokens": {
            "lesson_body_total": total_estimated_tokens,
            "per_lesson": {lesson["lesson_id"]: lesson["estimated_tokens"] for lesson in lesson_plans},
        },
        "risk_warnings": _compile_plan_risks(lessons),
        "manual_confirmation_items": _compile_plan_manual_items(lessons),
        "revision_count": len(state.get("compile_plan_revisions", [])),
    }


def _render_compile_plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        f"# Compile Plan: {plan.get('course_id', '')}",
        "",
        "## Material Scope",
        "",
    ]
    scope = plan.get("material_scope", {})
    lines.extend(
        [
            f"- Source files: {', '.join(scope.get('source_files', []))}",
            f"- Parsed chunks: {scope.get('parsed_chunk_count', 0)}",
            f"- Units: {scope.get('unit_count', 0)}",
            f"- Lessons: {scope.get('lesson_count', 0)}",
            f"- Source pages: {', '.join(scope.get('source_pages', [])) or 'n/a'}",
            "",
            "## Course Directory",
            "",
        ]
    )
    for section in plan.get("hierarchy", {}).get("sections", []):
        lines.append(f"### {section.get('title', 'Untitled Section')}")
        for lesson_id in section.get("lesson_ids", []):
            lesson = _compile_plan_lesson_by_id(plan, lesson_id)
            if not lesson:
                continue
            pages = ", ".join(lesson.get("source_pages", [])) or "n/a"
            blocks = ", ".join(lesson.get("source_blocks", [])[:8]) or "n/a"
            images = lesson.get("image_insert_strategy", {})
            density = lesson.get("body_density_estimate", {})
            risks = "; ".join(lesson.get("risks", [])) or "none"
            lines.extend(
                [
                    f"- **{lesson.get('lesson_id')} {lesson.get('title')}**",
                    f"  - Sources: pages `{pages}`, blocks `{blocks}`",
                    f"  - Images: {images.get('policy', 'none')} ({images.get('image_count', 0)} images, {images.get('pending_count', 0)} pending)",
                    f"  - Estimated tokens: {lesson.get('estimated_tokens', 0)}",
                    f"  - Body density: estimated plain chars `{density.get('estimated_plain_chars', 0)}/{density.get('plain_limit', 5000)}`, source count `{density.get('source_count', 0)}`, units `{density.get('unit_count', 0)}`, page span `{density.get('page_span', 0)}`",
                    f"  - Risks: {risks}",
                ]
            )
        lines.append("")
    lines.extend(["## Image Ownership", ""])
    for lesson in plan.get("hierarchy", {}).get("lessons", []):
        strategy = lesson.get("image_insert_strategy", {})
        if strategy.get("image_count") or strategy.get("pending_count"):
            lines.append(f"- `{lesson.get('lesson_id')}` {lesson.get('title')}: {strategy.get('policy')}")
    if lines[-1] == "":
        lines.append("- No lesson images planned.")
    lines.extend(["", "## Risk Warnings", ""])
    for risk in plan.get("risk_warnings", []):
        lines.append(f"- `{risk.get('type')}` {risk.get('message')} ({', '.join(risk.get('lesson_ids', []))})")
    if not plan.get("risk_warnings"):
        lines.append("- none")
    lines.extend(["", "## Manual Confirmation Items", ""])
    for item in plan.get("manual_confirmation_items", []):
        lines.append(f"- `{item.get('type')}` {item.get('message')} ({', '.join(item.get('lesson_ids', []))})")
    if not plan.get("manual_confirmation_items"):
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _compile_plan_lesson_by_id(plan: dict[str, Any], lesson_id: str) -> dict[str, Any] | None:
    for lesson in plan.get("hierarchy", {}).get("lessons", []):
        if lesson.get("lesson_id") == lesson_id:
            return lesson
    return None


def _lesson_image_strategy(lesson: dict[str, Any]) -> dict[str, Any]:
    images = list(lesson.get("images", []))
    pending = list(lesson.get("pending_image_confirmations", []))
    lesson_source_ids = {str(source.get("chunk_id", "")) for source in lesson.get("sources", [])}
    random = [
        image.get("id", "")
        for image in images
        if image.get("source_chunk_id") and str(image.get("source_chunk_id")) not in lesson_source_ids
    ]
    policy = "no images"
    if images:
        policy = "insert recognized images after related source-supported explanation"
    if pending:
        policy += "; keep uncertain images in pending confirmation"
    return {
        "policy": policy,
        "image_count": len(images),
        "pending_count": len(pending),
        "image_ids": [image.get("id", "") for image in images],
        "pending_ids": [image.get("id", "") for image in pending],
        "random_insertion_risk_ids": random,
    }


def _estimate_lesson_tokens(lesson: dict[str, Any]) -> int:
    body = str(lesson.get("body", ""))
    source_text = " ".join(str(source.get("quote", "")) for source in lesson.get("sources", []))
    return max(128, (len(body) + len(source_text)) // 2)


def _compile_plan_lesson_density_estimate(lesson: dict[str, Any], state: CourseCompileState) -> dict[str, Any]:
    profile = state.get("compile_profile", {})
    plain_limit = int(profile.get("lesson_body_plain_char_limit", 5000))
    sources = list(lesson.get("sources", []))
    source_text = " ".join(str(source.get("quote", "")) for source in sources)
    plain_chars = len(_plain_markdown_text(str(lesson.get("body", ""))))
    source_quote_chars = len(_plain_markdown_text(source_text))
    estimated_plain_chars = max(plain_chars, int(source_quote_chars * 0.8), 1)
    pages = _numeric_lesson_pages(lesson)
    reasons: list[str] = []
    if estimated_plain_chars > plain_limit:
        reasons.append("estimated_plain_text_exceeds_limit")
    if len(lesson.get("unit_ids", [])) > int(profile.get("lesson_body_max_units", 2)):
        reasons.append("multiple_concepts_mixed")
    if len(sources) > int(profile.get("lesson_body_max_source_chunks", 6)):
        reasons.append("knowledge_points_too_many")
    if pages and (len(pages) > int(profile.get("lesson_body_max_pages", 4)) or max(pages) - min(pages) + 1 > int(profile.get("lesson_body_max_page_span", 3))):
        reasons.append("page_span_too_large")
    return {
        "estimated_plain_chars": estimated_plain_chars,
        "plain_limit": plain_limit,
        "source_count": len(sources),
        "unit_count": len(lesson.get("unit_ids", [])),
        "page_count": len(pages),
        "page_span": (max(pages) - min(pages) + 1) if pages else 0,
        "needs_finer_split": bool(reasons),
        "reasons": reasons,
    }


def _lesson_plan_risks(lesson: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    title = str(lesson.get("title", ""))
    body = str(lesson.get("body", ""))
    if _bad_independent_lesson_title(title):
        risks.append("short_fragment_chapter")
    if len(body.strip()) < 80:
        risks.append("thin_lesson_draft")
    if any(token in _normalize_title(body) for token in ("视觉关系", "排版说明", "图片说明", "teachernote", "imagecaption")):
        risks.append("visual_note_pollution")
    if _formula_markdown_mix_risk(body):
        risks.append("formula_markdown_risk")
    if _lesson_image_strategy(lesson).get("random_insertion_risk_ids"):
        risks.append("image_random_insert")
    return risks


def _compile_plan_risks(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    title_to_lessons: dict[str, list[str]] = {}
    body_lengths = [len(str(lesson.get("body", "")).strip()) for lesson in lessons]
    for lesson in lessons:
        title_key = _normalize_title(str(lesson.get("title", "")))
        title_to_lessons.setdefault(title_key, []).append(str(lesson.get("id", "")))
        for risk in _lesson_plan_risks(lesson):
            risks.append({"type": risk, "severity": "high" if risk != "thin_lesson_draft" else "medium", "message": risk, "lesson_ids": [lesson.get("id", "")]})
    for title_key, lesson_ids in title_to_lessons.items():
        if title_key and len(lesson_ids) > 1:
            risks.append({"type": "duplicate_title", "severity": "high", "message": f"Duplicate lesson title key: {title_key}", "lesson_ids": lesson_ids})
    if body_lengths and max(body_lengths) > max(400, min(body_lengths) * 5):
        risks.append({"type": "granularity", "severity": "medium", "message": "Lesson draft body lengths are uneven.", "lesson_ids": [lesson.get("id", "") for lesson in lessons]})
    return risks


def _compile_plan_manual_items(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for lesson in lessons:
        if lesson.get("pending_image_confirmations"):
            items.append(
                {
                    "type": "pending_image_confirmation",
                    "message": "Lesson has uncertain images that require manual confirmation.",
                    "lesson_ids": [lesson.get("id", "")],
                }
            )
        if lesson.get("content_type") == "needs_confirmation":
            items.append(
                {
                    "type": "source_needs_confirmation",
                    "message": "Lesson content is marked as needs_confirmation.",
                    "lesson_ids": [lesson.get("id", "")],
                }
            )
    return items


def _formula_markdown_mix_risk(value: str) -> bool:
    lines = value.splitlines()
    in_math = False
    math_fence = ""
    for line in lines:
        stripped = line.strip()
        if stripped in {"$$", "\\["} and not in_math:
            in_math = True
            math_fence = stripped
            continue
        if in_math and ((math_fence == "$$" and stripped == "$$") or (math_fence == "\\[" and stripped == "\\]")):
            in_math = False
            math_fence = ""
            continue
        if in_math and re.match(r"^[-*+]\s+", stripped):
            return True
    return bool(re.search(r"\\begin\{(cases|[bpvVB]?matrix|aligned)\}[\s\S]*\n\s*[-*+]\s+", value))


def _normalize_compile_plan_review(raw: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    allowed_types = {
        "granularity",
        "short_fragment_chapter",
        "duplicate_title",
        "image_random_insert",
        "visual_note_pollution",
        "formula_markdown_risk",
        "needs_finer_split",
        "source_gap",
        "missing_llm_reviewer",
        "review_llm_error",
        "other",
    }
    for item in raw.get("issues", []):
        issue_type = str(item.get("type", "other")).strip()
        if issue_type not in allowed_types:
            issue_type = "other"
        severity = str(item.get("severity", "medium")).strip().lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        issues.append(
            {
                "type": issue_type,
                "severity": severity,
                "message": _short_text(str(item.get("message", "")).strip(), 420),
                "lesson_ids": [str(value) for value in item.get("lesson_ids", [])],
                "unit_ids": [str(value) for value in item.get("unit_ids", [])],
            }
        )
    passed = bool(raw.get("passed", not any(item["severity"] == "high" for item in issues)))
    if issues and any(item["severity"] == "high" for item in issues):
        passed = False
    revise_prompt = raw.get("revise_prompt", {}) if isinstance(raw.get("revise_prompt", {}), dict) else {}
    if not passed and not revise_prompt:
        revise_prompt = _revise_prompt_from_issues(issues)
    return {"passed": passed, "issues": issues, "revise_prompt": revise_prompt}


def _local_compile_plan_review(plan: dict[str, Any]) -> dict[str, Any]:
    issues = [
        {
            "type": risk.get("type", "other"),
            "severity": risk.get("severity", "medium"),
            "message": risk.get("message", ""),
            "lesson_ids": risk.get("lesson_ids", []),
            "unit_ids": [],
        }
        for risk in plan.get("risk_warnings", [])
    ]
    passed = not any(issue["severity"] == "high" for issue in issues)
    return {
        "passed": passed,
        "issues": issues,
        "revise_prompt": {} if passed else _revise_prompt_from_issues(issues),
        "metadata": {"mode": "local_rule_review"},
    }


def _merge_compile_plan_reviews(llm_review: dict[str, Any], local_review: dict[str, Any], revision_count: int) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for source in (llm_review, local_review):
        for issue in source.get("issues", []):
            key = (str(issue.get("type", "")), str(issue.get("message", "")), tuple(issue.get("lesson_ids", [])))
            if key in seen:
                continue
            seen.add(key)
            issues.append(issue)
    passed = bool(llm_review.get("passed", True)) and bool(local_review.get("passed", True)) and not any(issue.get("severity") == "high" for issue in issues)
    revise_prompt = llm_review.get("revise_prompt") or local_review.get("revise_prompt") or ({} if passed else _revise_prompt_from_issues(issues))
    return {
        "passed": passed,
        "issues": issues,
        "revise_prompt": revise_prompt,
        "revision_count": revision_count,
        "llm_metadata": llm_review.get("metadata", {}),
        "local_metadata": local_review.get("metadata", {}),
    }


def _revise_prompt_from_issues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    details = []
    actions: list[str] = []
    for issue in issues:
        issue_type = issue.get("type", "other")
        lesson_ids = issue.get("lesson_ids", [])
        if issue_type == "needs_finer_split":
            action = "split_lesson"
        elif issue_type == "granularity":
            action = "split_lesson" if issue.get("severity") == "high" and lesson_ids else "flag_manual_confirmation"
        elif issue_type in {"duplicate_title", "short_fragment_chapter", "thin_lesson_draft"}:
            action = "merge_lessons"
        elif issue_type == "image_random_insert":
            action = "move_image"
        elif issue_type == "visual_note_pollution":
            action = "remove_visual_note"
        elif issue_type == "formula_markdown_risk":
            action = "flag_manual_confirmation"
        else:
            action = "flag_manual_confirmation"
        actions.append(action)
        details.append({"target": ",".join(lesson_ids), "instruction": issue.get("message", issue_type), "issue_type": issue_type})
    return {
        "objective": "Revise the compile plan before generating lesson bodies.",
        "actions": _dedupe_keep_order(actions),
        "details": details,
    }


def _render_compile_plan_review_markdown(review: dict[str, Any]) -> str:
    lines = [
        "# Compile Plan Review",
        "",
        f"- Passed: {bool(review.get('passed'))}",
        f"- Revision count: {review.get('revision_count', 0)}",
        "",
        "## Issues",
        "",
    ]
    for issue in review.get("issues", []):
        lines.append(f"- `{issue.get('severity')}` `{issue.get('type')}` {issue.get('message')} ({', '.join(issue.get('lesson_ids', []))})")
    if not review.get("issues"):
        lines.append("- none")
    lines.extend(["", "## Revise Prompt", "", "```json", json.dumps(review.get("revise_prompt", {}), ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def _apply_compile_plan_revisions(
    lessons: list[dict[str, Any]],
    review: dict[str, Any],
    units: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    revised = [dict(lesson) for lesson in lessons]
    actions: list[dict[str, Any]] = []
    issues = list(review.get("issues", []))
    unit_by_id = {str(unit.get("id", "")): unit for unit in (units or [])}
    for issue in issues:
        issue_type = issue.get("type")
        lesson_ids = [str(value) for value in issue.get("lesson_ids", [])]
        if issue_type == "needs_finer_split" and lesson_ids:
            for lesson_id in lesson_ids:
                changed = _split_lesson_by_id(revised, lesson_id, unit_by_id)
                if changed:
                    actions.append({"action": "split_lesson", "lesson_ids": [lesson_id], "issue_type": issue_type})
        elif issue_type == "granularity" and lesson_ids and issue.get("severity") == "high":
            split_ids: list[str] = []
            for lesson_id in lesson_ids:
                if _split_lesson_by_id(revised, lesson_id, unit_by_id):
                    split_ids.append(lesson_id)
            if split_ids:
                actions.append({"action": "split_lesson", "lesson_ids": split_ids, "issue_type": issue_type})
        elif issue_type in {"duplicate_title", "short_fragment_chapter", "thin_lesson_draft"} and lesson_ids:
            changed = _merge_lessons_by_ids(revised, lesson_ids)
            if not changed and len(lesson_ids) == 1:
                changed = _merge_lesson_with_neighbor(revised, lesson_ids[0])
            if changed:
                actions.append({"action": "merge_lessons", "lesson_ids": lesson_ids, "issue_type": issue_type})
        elif issue_type == "granularity":
            actions.append({"action": "flag_manual_confirmation", "lesson_ids": lesson_ids, "issue_type": issue_type})
        elif issue_type == "visual_note_pollution":
            changed = _remove_visual_note_lines(revised, lesson_ids)
            if changed:
                actions.append({"action": "remove_visual_note", "lesson_ids": lesson_ids, "issue_type": issue_type})
        elif issue_type == "image_random_insert":
            changed = _remove_random_images(revised, lesson_ids)
            if changed:
                actions.append({"action": "move_image", "lesson_ids": lesson_ids, "issue_type": issue_type})
        elif issue_type == "formula_markdown_risk":
            for lesson in revised:
                if not lesson_ids or lesson.get("id") in lesson_ids:
                    lesson["content_type"] = "needs_confirmation"
            actions.append({"action": "flag_manual_confirmation", "lesson_ids": lesson_ids, "issue_type": issue_type})
        else:
            for lesson in revised:
                if not lesson_ids or lesson.get("id") in lesson_ids:
                    lesson["content_type"] = "needs_confirmation"
            actions.append({"action": "flag_manual_confirmation", "lesson_ids": lesson_ids, "issue_type": issue_type})
    for index, lesson in enumerate(revised, start=1):
        lesson["id"] = f"lesson-{index:03d}"
        lesson["order"] = index
    return revised, actions


def _needs_finer_split(review: dict[str, Any]) -> bool:
    return any(str(issue.get("type", "")) == "needs_finer_split" or issue.get("needs_finer_split") for issue in review.get("issues", []))


def _split_lesson_by_id(lessons: list[dict[str, Any]], lesson_id: str, unit_by_id: dict[str, dict[str, Any]]) -> bool:
    index = next((idx for idx, lesson in enumerate(lessons) if str(lesson.get("id", "")) == lesson_id), None)
    if index is None:
        return False
    lesson = lessons[index]
    parts = _split_lesson_by_units(lesson, unit_by_id)
    if not parts:
        parts = _split_lesson_by_sources(lesson)
    if not parts:
        parts = _split_lesson_by_body(lesson)
    if not parts:
        return False
    lessons[index : index + 1] = parts
    return True


def _split_lesson_by_units(lesson: dict[str, Any], unit_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    unit_ids = [str(unit_id) for unit_id in lesson.get("unit_ids", []) if str(unit_id) in unit_by_id]
    if len(unit_ids) <= 1:
        return []
    parts: list[dict[str, Any]] = []
    for part_index, unit_id in enumerate(unit_ids, start=1):
        unit = dict(unit_by_id[unit_id])
        unit.setdefault("summary", unit.get("source_quote", ""))
        unit.setdefault("content_type", "source_supported")
        unit.setdefault("source", "")
        unit.setdefault("source_chunk_id", "")
        unit.setdefault("source_quote", unit.get("summary", ""))
        source_chunk_ids = {str(ref.get("chunk_id") or unit.get("source_chunk_id", "")) for ref in unit.get("source_refs", [])}
        if unit.get("source_chunk_id"):
            source_chunk_ids.add(str(unit.get("source_chunk_id")))
        part = dict(lesson)
        title = _clean_lesson_title(str(unit.get("title", "")).strip()) or _lesson_part_title(str(lesson.get("title", "")), part_index, len(unit_ids))
        part.update(
            {
                "title": title,
                "section_title": str(unit.get("section_title") or lesson.get("section_title", "")),
                "lesson_type": unit.get("lesson_type", lesson.get("lesson_type", "")),
                "unit_ids": [unit_id],
                "body": _lesson_body(unit),
                "body_max_chars": min(int(unit.get("lesson_body_max_chars", lesson.get("body_max_chars", 1200))), 5000),
                "checklist": [f"理解：{title}"],
                "sources": _filter_lesson_sources_by_chunk_ids(lesson, source_chunk_ids),
                "images": [image for image in lesson.get("images", []) if not image.get("source_chunk_id") or str(image.get("source_chunk_id")) in source_chunk_ids],
                "pending_image_confirmations": [
                    image
                    for image in lesson.get("pending_image_confirmations", [])
                    if not image.get("source_chunk_id") or str(image.get("source_chunk_id")) in source_chunk_ids
                ],
            }
        )
        parts.append(part)
    return parts


def _split_lesson_by_sources(lesson: dict[str, Any]) -> list[dict[str, Any]]:
    sources = list(lesson.get("sources", []))
    if len(sources) <= 1:
        return []
    midpoint = max(1, len(sources) // 2)
    groups = [sources[:midpoint], sources[midpoint:]]
    if not groups[1]:
        return []
    body_parts = _split_text_into_parts(str(lesson.get("body", "")), 2)
    parts: list[dict[str, Any]] = []
    for part_index, group in enumerate(groups, start=1):
        chunk_ids = {str(source.get("chunk_id", "")) for source in group if source.get("chunk_id")}
        part = dict(lesson)
        part.update(
            {
                "title": _lesson_part_title(str(lesson.get("title", "")), part_index, len(groups)),
                "body": body_parts[part_index - 1] if part_index - 1 < len(body_parts) else str(lesson.get("body", "")),
                "unit_ids": [],
                "source_chunk_ids": _dedupe_keep_order([str(source.get("chunk_id", "")) for source in group if source.get("chunk_id")]),
                "sources": group,
                "images": [image for image in lesson.get("images", []) if not image.get("source_chunk_id") or str(image.get("source_chunk_id")) in chunk_ids],
                "pending_image_confirmations": [
                    image
                    for image in lesson.get("pending_image_confirmations", [])
                    if not image.get("source_chunk_id") or str(image.get("source_chunk_id")) in chunk_ids
                ],
                "body_max_chars": min(int(lesson.get("body_max_chars", 1200)), 5000),
            }
        )
        parts.append(part)
    return parts


def _split_lesson_by_body(lesson: dict[str, Any]) -> list[dict[str, Any]]:
    body_parts = _split_text_into_parts(str(lesson.get("body", "")), 2)
    if len(body_parts) < 2:
        return []
    parts: list[dict[str, Any]] = []
    for part_index, body in enumerate(body_parts, start=1):
        part = dict(lesson)
        part.update({"title": _lesson_part_title(str(lesson.get("title", "")), part_index, len(body_parts)), "body": body, "body_max_chars": min(int(lesson.get("body_max_chars", 1200)), 5000)})
        parts.append(part)
    return parts


def _lesson_part_title(title: str, part_index: int, total: int) -> str:
    base = str(title or "Untitled").strip()
    if total <= 1:
        return base
    return f"{base}（{part_index}）"


def _split_text_into_parts(text: str, count: int) -> list[str]:
    lines = [line for line in str(text).splitlines()]
    if len(lines) >= count * 2:
        midpoint = max(1, len(lines) // 2)
        return ["\n".join(lines[:midpoint]).strip(), "\n".join(lines[midpoint:]).strip()]
    plain = str(text).strip()
    if len(plain) < 240:
        return []
    midpoint = len(plain) // 2
    split_at = plain.rfind("。", 0, midpoint)
    if split_at < 80:
        split_at = midpoint
    return [plain[: split_at + 1].strip(), plain[split_at + 1 :].strip()]


def _filter_lesson_sources_by_chunk_ids(lesson: dict[str, Any], chunk_ids: set[str]) -> list[dict[str, Any]]:
    filtered = [source for source in lesson.get("sources", []) if str(source.get("chunk_id", "")) in chunk_ids or str(source.get("block_id", "")) in chunk_ids]
    return filtered or list(lesson.get("sources", []))


def _merge_lessons_by_ids(lessons: list[dict[str, Any]], lesson_ids: list[str]) -> bool:
    indexes = [index for index, lesson in enumerate(lessons) if lesson.get("id") in lesson_ids]
    if len(indexes) < 2:
        return False
    first_index = indexes[0]
    target = lessons[first_index]
    for index in sorted(indexes[1:], reverse=True):
        lesson = lessons[index]
        target["unit_ids"] = _dedupe_keep_order(list(target.get("unit_ids", [])) + list(lesson.get("unit_ids", [])))
        target["body"] = "\n\n".join(part for part in (str(target.get("body", "")).strip(), str(lesson.get("body", "")).strip()) if part)
        target["checklist"] = _dedupe_keep_order(list(target.get("checklist", [])) + list(lesson.get("checklist", [])))[:8]
        target["sources"] = _dedupe_sources(list(target.get("sources", [])) + list(lesson.get("sources", [])))
        target["images"] = list(target.get("images", [])) + list(lesson.get("images", []))
        target["pending_image_confirmations"] = list(target.get("pending_image_confirmations", [])) + list(lesson.get("pending_image_confirmations", []))
        del lessons[index]
    return True


def _merge_lesson_with_neighbor(lessons: list[dict[str, Any]], lesson_id: str) -> bool:
    index = next((item for item, lesson in enumerate(lessons) if lesson.get("id") == lesson_id), -1)
    if index < 0 or len(lessons) < 2:
        return False
    if index > 0:
        return _merge_lessons_by_ids(lessons, [str(lessons[index - 1].get("id", "")), lesson_id])
    return _merge_lessons_by_ids(lessons, [lesson_id, str(lessons[index + 1].get("id", ""))])


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for source in sources:
        key = str(source.get("chunk_id") or source.get("block_id") or source)
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def _remove_visual_note_lines(lessons: list[dict[str, Any]], lesson_ids: list[str]) -> bool:
    changed = False
    bad_tokens = ("视觉关系", "排版说明", "图片说明", "视觉说明", "teacher note", "image caption")
    for lesson in lessons:
        if lesson_ids and lesson.get("id") not in lesson_ids:
            continue
        lines = str(lesson.get("body", "")).splitlines()
        kept = [line for line in lines if not any(token in line.lower() for token in bad_tokens)]
        if kept != lines:
            lesson["body"] = "\n".join(kept).strip()
            changed = True
    return changed


def _remove_random_images(lessons: list[dict[str, Any]], lesson_ids: list[str]) -> bool:
    changed = False
    for lesson in lessons:
        if lesson_ids and lesson.get("id") not in lesson_ids:
            continue
        lesson_source_ids = {str(source.get("chunk_id", "")) for source in lesson.get("sources", [])}
        images = list(lesson.get("images", []))
        kept = [image for image in images if not image.get("source_chunk_id") or str(image.get("source_chunk_id")) in lesson_source_ids]
        if len(kept) != len(images):
            lesson["images"] = kept
            changed = True
    return changed


def _lesson_body_source_chunk_ids(
    lesson: dict[str, Any],
    unit_by_id: dict[str, dict[str, Any]],
    chunk_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    explicit_ids = [str(chunk_id) for chunk_id in lesson.get("source_chunk_ids", []) if str(chunk_id) in chunk_by_id]
    if explicit_ids:
        return _dedupe_keep_order(explicit_ids)
    units = [unit_by_id[unit_id] for unit_id in lesson.get("unit_ids", []) if unit_id in unit_by_id]
    source_chunk_ids: list[str] = []
    for unit in units:
        source_chunk_ids.extend(str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []) if str(chunk_id) in chunk_by_id)
    for source in lesson.get("sources", []):
        chunk_id = stable_source_id(source)
        if chunk_id in chunk_by_id:
            source_chunk_ids.append(chunk_id)
    return _dedupe_keep_order(source_chunk_ids)


def _lesson_body_generation_prompt(
    lesson: dict[str, Any],
    profile: dict[str, Any],
    learn_by_doing: bool,
    source_chunk_ids: list[str],
    chunk_by_id: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    system = (
        "You are a course writer. Convert one source-grounded lesson plan into a detailed, readable study lesson. "
        "Use only the provided local source chunks. Return strict JSON only."
    )
    if learn_by_doing:
        system += " In learn-by-doing mode, write task-first software tutorial lessons."
    plain_limit = int(profile.get("lesson_body_plain_char_limit", 5000))
    target_chars = min(int(profile.get("lesson_body_target_chars", 3500)), plain_limit)
    max_chars = min(int(profile.get("lesson_body_max_chars", 7200)), plain_limit)
    user = (
        f"{_review_feedback_for_prompt(profile)}"
        "Write one detailed lesson JSON with this schema:\n"
        "{\"lesson_id\":\"lesson-001\",\"title\":\"...\",\"body_markdown\":\"...\","
        "\"checklist\":[\"...\"],\"covered_source_chunk_ids\":[\"...\"],"
        "\"local_enrichments\":[{\"type\":\"example_steps|proof_bridge|thinking_question|pitfall|concept_disambiguation\","
        "\"title\":\"...\",\"source_chunk_ids\":[\"...\"],\"status\":\"source_supported|standard_derivation|needs_confirmation\",\"content\":\"...\"}]}\n\n"
        "Writing requirements:\n"
        "- Write in Chinese for fragmented study reading.\n"
        "- Cover the lesson's main ideas, key concepts, methods, formulas, and examples from the provided chunks.\n"
        "- Organize the body with Markdown headings such as 学习目标, 概念与直觉, 方法步骤, 例题讲解, 易错点, 小结, 下一节.\n"
        "- Prefer explanation over raw transcription; keep important formulas exactly enough to study.\n"
        "- Do not include layout-only visual descriptions, page headers, image links, or source metadata as teaching content.\n"
        "- Do not invent facts beyond the source chunks. If evidence is missing, say 待源材料确认.\n"
        "- If an example is incomplete because of OCR/source loss, do not reconstruct missing equations, numbers, or results from general knowledge; explain the available part and show the reusable method with symbolic placeholders.\n"
        "- Never infer, reverse-engineer, complete, or choose missing constants/equations for a damaged example. Do not use phrases like 根据上下文补全, 反推, 合理补充, or 供参考 for missing source facts.\n"
        f"- Target length: {target_chars}-{max_chars} Chinese characters when source content is sufficient; never exceed {plain_limit} plain-text characters after Markdown markers are removed.\n\n"
        f"{_learn_by_doing_body_requirements() if learn_by_doing else ''}"
        f"{_lesson_body_enrichment_requirements(profile)}"
        f"Lesson id: {lesson['id']}\n"
        f"Lesson title: {lesson['title']}\n"
        f"Lesson type: {lesson.get('lesson_type', '')}\n"
        f"Section: {lesson.get('section_title', '')}\n\n"
        f"Draft lesson body:\n{str(lesson.get('body', ''))[:4000]}\n\n"
        f"Source chunks:\n{_lesson_chunks_for_prompt(source_chunk_ids, chunk_by_id, max_chars=int(profile.get('lesson_body_chunk_chars', 1200)))}"
    )
    return system, user


def synthesize_lesson_bodies(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Optionally replace draft lesson bodies with LLM-written study notes using local lesson chunks."""

    profile = state.get("compile_profile", {})
    learn_by_doing = _is_learn_by_doing(profile)
    state["lesson_body_revision_request"] = {}
    if not profile.get("use_llm_lesson_bodies"):
        state["lesson_bodies"] = {}
        state["lesson_body_inputs"] = {"lessons": []}
        state["next_action"] = "check_markdown_syntax"
        return state

    client = LLMClient.from_env()
    if client is None:
        state["errors"].append({"node": "synthesize_lesson_bodies", "message": "LLM configuration is missing; keeping draft lesson bodies"})
        state["lesson_bodies"] = {}
        state["lesson_body_inputs"] = {"lessons": []}
        state["next_action"] = "check_markdown_syntax"
        return state

    course_path = course_dir(Path(vault_root), state["course_id"])
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    unit_by_id = {unit["id"]: unit for unit in state["units"]}
    locator = SourceLocator.from_state(state)
    lesson_bodies: list[dict[str, Any]] = []
    lesson_body_inputs: list[dict[str, Any]] = []
    density_checks: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    skipped = 0
    start_order = int(profile.get("lesson_body_start", 1))
    end_order = int(profile.get("lesson_body_end", len(state["lessons"])))
    plain_limit = int(profile.get("lesson_body_plain_char_limit", 5000))
    batch_plain_limit = int(profile.get("lesson_body_batch_plain_char_limit", 5000))
    generated_plain_total = 0

    for lesson_index, lesson in enumerate(state["lessons"], start=1):
        source_chunk_ids = _lesson_body_source_chunk_ids(lesson, unit_by_id, chunk_by_id)
        if not source_chunk_ids:
            continue
        evidence_pack = locator.get_context(source_chunk_ids, before=1, after=1)

        system, user = _lesson_body_generation_prompt(lesson, profile, learn_by_doing, source_chunk_ids, chunk_by_id)
        prompt_text = system + "\n" + user
        density_check = _lesson_body_density_check(
            lesson,
            source_chunk_ids,
            chunk_by_id,
            profile,
            prompt_text,
            _compile_plan_lesson_by_id(state.get("compile_plan", {}), str(lesson.get("id", ""))),
        )
        density_checks.append(density_check)
        if density_check.get("needs_finer_split"):
            return _request_lesson_body_finer_split(
                state,
                vault_root,
                [density_check],
                stage="pre_generation",
                message="Lesson body generation was skipped because the compile plan is too dense for one complete LLM call or fragmented reading.",
                lesson_body_inputs=lesson_body_inputs,
                lesson_bodies=lesson_bodies,
                density_checks=density_checks,
                metadata=metadata,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                skipped=skipped,
            )
        lesson_body_inputs.append(
            {
                "lesson_id": str(lesson["id"]),
                "title": str(lesson.get("title", "")),
                "system_prompt": system,
                "user_prompt": user,
                "source_chunk_ids": source_chunk_ids,
                "evidence": evidence_pack,
                "draft_body": str(lesson.get("body", "")),
                "max_chars": int(lesson.get("body_max_chars", profile.get("lesson_body_max_chars", 7200))),
            }
        )
        cache_path = _lesson_body_cache_path(course_path, client, system, user)
        if cache_path and cache_path.exists() and not profile.get("refresh_lesson_bodies"):
            cached = read_json(cache_path)
            body_record = cached.get("lesson_body", {})
            cache_hits += 1
            metadata.append(cached.get("metadata", {}))
        elif not profile.get("refresh_lesson_bodies"):
            cached = _find_cached_lesson_body_by_id(course_path, lesson["id"], set(source_chunk_ids))
            if cached:
                body_record = cached.get("lesson_body", {})
                cache_hits += 1
                metadata.append(cached.get("metadata", {}))
            elif lesson_index < start_order or lesson_index > end_order:
                skipped += 1
                continue
            else:
                try:
                    raw = client.complete_json(system, user)
                    body_record = _normalize_lesson_body(raw, lesson, set(source_chunk_ids), int(lesson.get("body_max_chars", profile.get("lesson_body_max_chars", 7200))))
                    body_record["_raw_plain_chars"] = len(_plain_markdown_text(str(raw.get("body_markdown", ""))))
                    cache_misses += 1
                    metadata.append(getattr(client, "last_metadata", {}))
                    if cache_path:
                        write_json(
                            cache_path,
                            {
                                "lesson_body": body_record,
                                "provider": getattr(client, "cache_identity", {}),
                                "metadata": getattr(client, "last_metadata", {}),
                            },
                        )
                except Exception as exc:  # pragma: no cover - depends on external model behavior
                    state["errors"].append({"node": "synthesize_lesson_bodies", "message": f"LLM lesson body failed for {lesson['id']}; keeping draft: {exc}"})
                    continue
        elif lesson_index < start_order or lesson_index > end_order:
            skipped += 1
            continue
        else:
            try:
                raw = client.complete_json(system, user)
                body_record = _normalize_lesson_body(raw, lesson, set(source_chunk_ids), int(lesson.get("body_max_chars", profile.get("lesson_body_max_chars", 7200))))
                body_record["_raw_plain_chars"] = len(_plain_markdown_text(str(raw.get("body_markdown", ""))))
                cache_misses += 1
                metadata.append(getattr(client, "last_metadata", {}))
                if cache_path:
                    write_json(
                        cache_path,
                        {
                            "lesson_body": body_record,
                            "provider": getattr(client, "cache_identity", {}),
                            "metadata": getattr(client, "last_metadata", {}),
                        },
                    )
            except Exception as exc:  # pragma: no cover - depends on external model behavior
                state["errors"].append({"node": "synthesize_lesson_bodies", "message": f"LLM lesson body failed for {lesson['id']}; keeping draft: {exc}"})
                continue

        if body_record.get("body_markdown"):
            body_record["body_markdown"] = _normalize_compiled_markdown(str(body_record["body_markdown"]))
            plain_chars = max(len(_plain_markdown_text(body_record["body_markdown"])), int(body_record.get("_raw_plain_chars", 0) or 0))
            generated_plain_total += plain_chars
            if plain_chars > plain_limit:
                post_issue = _lesson_body_post_generation_limit_issue(
                    lesson,
                    body_record,
                    plain_chars,
                    generated_plain_total,
                    plain_limit,
                    batch_plain_limit,
                )
                return _request_lesson_body_finer_split(
                    state,
                    vault_root,
                    [post_issue],
                    stage="post_generation",
                    message="Generated lesson body text exceeds the fragmented-reading length limit and must be split before validation/export.",
                    lesson_body_inputs=lesson_body_inputs,
                    lesson_bodies=lesson_bodies + [body_record],
                    density_checks=density_checks,
                    metadata=metadata,
                    cache_hits=cache_hits,
                    cache_misses=cache_misses,
                    skipped=skipped,
                )
            lesson["body"] = body_record["body_markdown"]
            if body_record.get("checklist"):
                lesson["checklist"] = body_record["checklist"]
            lesson_bodies.append(body_record)

    state["lesson_bodies"] = {"lesson_bodies": lesson_bodies}
    state["lesson_body_inputs"] = {"lessons": lesson_body_inputs}
    _persist_lesson_evidence(state, vault_root)
    target_dir = course_dir(Path(vault_root), state["course_id"])
    write_json(target_dir / "lesson_bodies.json", state["lesson_bodies"])
    (target_dir / "lesson_bodies.md").write_text(_render_lesson_bodies_markdown(state["lesson_bodies"]), encoding="utf-8")
    write_json(
        target_dir / "lesson_bodies_meta.json",
        {
            "node": "synthesize_lesson_bodies",
            "local_cache_hits": cache_hits,
            "local_cache_misses": cache_misses,
            "skipped_outside_range": skipped,
            "lesson_body_start": start_order,
            "lesson_body_end": end_order,
            "lesson_body_plain_char_limit": plain_limit,
            "lesson_body_batch_plain_char_limit": batch_plain_limit,
            "generated_plain_total": generated_plain_total,
            "density_checks": density_checks,
            "lesson_body_inputs": lesson_body_inputs,
            "metadata": metadata,
        },
    )
    write_json(target_dir / "lessons.json", state["lessons"])
    state["next_action"] = "check_markdown_syntax"
    return state


def _lesson_body_density_check(
    lesson: dict[str, Any],
    source_chunk_ids: list[str],
    chunk_by_id: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    prompt: str,
    compile_plan_lesson: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plain_limit = int(profile.get("lesson_body_plain_char_limit", 5000))
    context_limit = min(int(profile.get("lesson_body_context_limit_chars", 24000)), _llm_prompt_char_limit({"compile_profile": profile}))
    max_source_chunks = int(profile.get("lesson_body_max_source_chunks", 6))
    max_units = int(profile.get("lesson_body_max_units", 2))
    max_page_span = int(profile.get("lesson_body_max_page_span", 3))
    max_pages = int(profile.get("lesson_body_max_pages", 4))
    max_formula_count = int(profile.get("lesson_body_max_formula_count", 10))
    max_images = int(profile.get("lesson_body_max_images", 3))
    source_chunks = [chunk_by_id[chunk_id] for chunk_id in source_chunk_ids if chunk_id in chunk_by_id]
    source_text = "\n".join(str(chunk.get("content", "")) for chunk in source_chunks)
    draft_text = str(lesson.get("body", ""))
    source_chars = len(_plain_markdown_text(source_text))
    draft_plain_chars = len(_plain_markdown_text(draft_text))
    plan_density = (compile_plan_lesson or {}).get("body_density_estimate", {}) if isinstance(compile_plan_lesson, dict) else {}
    plan_estimated_plain = int(plan_density.get("estimated_plain_chars", 0) or 0)
    estimated_plain_chars = max(draft_plain_chars, int(source_chars * 0.55), plan_estimated_plain, 1)
    unit_count = len(lesson.get("unit_ids", []))
    pages = _numeric_lesson_pages(lesson)
    page_span = (max(pages) - min(pages) + 1) if pages else 0
    formula_count = _formula_density_count(source_text + "\n" + draft_text)
    image_count = len(lesson.get("images", [])) + len(lesson.get("pending_image_confirmations", []))
    reasons: list[str] = []
    if estimated_plain_chars > plain_limit:
        reasons.append("estimated_plain_text_exceeds_limit")
    if len(prompt) > context_limit:
        reasons.append("single_call_context_too_large")
    if len(source_chunk_ids) > max_source_chunks:
        reasons.append("knowledge_points_too_many")
    if unit_count > max_units:
        reasons.append("multiple_concepts_mixed")
    if len(pages) > max_pages or page_span > max_page_span:
        reasons.append("page_span_too_large")
    formula_context_overloaded = (
        len(prompt) > int(context_limit * 0.75)
        or source_chars > plain_limit
        or len(source_chunk_ids) > max_source_chunks
        or unit_count > max_units
        or len(pages) > max_pages
        or page_span > max_page_span
    )
    if formula_count > max_formula_count and formula_context_overloaded:
        reasons.append("derivation_examples_or_formulas_too_dense")
    if image_count > max_images:
        reasons.append("formula_image_notes_concentrated")
    reasons = _dedupe_keep_order(reasons)
    return {
        "lesson_id": str(lesson.get("id", "")),
        "title": str(lesson.get("title", "")),
        "needs_finer_split": bool(reasons),
        "reasons": reasons,
        "reason": _lesson_body_split_reason_message(reasons),
        "metrics": {
            "estimated_plain_chars": estimated_plain_chars,
            "compile_plan_estimated_plain_chars": plan_estimated_plain,
            "plain_limit": plain_limit,
            "prompt_chars": len(prompt),
            "context_limit": context_limit,
            "source_chars": source_chars,
            "source_chunk_count": len(source_chunk_ids),
            "unit_count": unit_count,
            "page_count": len(pages),
            "page_span": page_span,
            "formula_count": formula_count,
            "image_count": image_count,
        },
        "source_chunk_ids": source_chunk_ids,
        "source_pages": [str(page) for page in pages],
        "suggestion": "Split this lesson into narrower source-backed lessons before generating a full body.",
    }


def _lesson_body_post_generation_limit_issue(
    lesson: dict[str, Any],
    body_record: dict[str, Any],
    plain_chars: int,
    generated_plain_total: int,
    plain_limit: int,
    batch_plain_limit: int,
) -> dict[str, Any]:
    reasons = []
    if plain_chars > plain_limit:
        reasons.append("generated_lesson_plain_text_exceeds_limit")
    return {
        "lesson_id": str(lesson.get("id", body_record.get("lesson_id", ""))),
        "title": str(lesson.get("title", body_record.get("title", ""))),
        "needs_finer_split": True,
        "reasons": reasons,
        "reason": _lesson_body_split_reason_message(reasons),
        "metrics": {
            "generated_plain_chars": plain_chars,
            "generated_plain_total": generated_plain_total,
            "plain_limit": plain_limit,
            "batch_plain_limit": batch_plain_limit,
        },
        "suggestion": "Split this lesson or generation batch before continuing validation/export.",
    }


def _request_lesson_body_finer_split(
    state: CourseCompileState,
    vault_root: Path | str,
    issues: list[dict[str, Any]],
    *,
    stage: str,
    message: str,
    lesson_body_inputs: list[dict[str, Any]],
    lesson_bodies: list[dict[str, Any]],
    density_checks: list[dict[str, Any]],
    metadata: list[dict[str, Any]],
    cache_hits: int,
    cache_misses: int,
    skipped: int,
) -> CourseCompileState:
    target_dir = course_dir(Path(vault_root), state["course_id"])
    lesson_ids = _dedupe_keep_order([str(issue.get("lesson_id", "")) for issue in issues if issue.get("lesson_id")])
    revision_request = {
        "needs_finer_split": True,
        "stage": stage,
        "message": message,
        "preferred_next_action": "revise_compile_plan",
        "fallback_next_action": "generate_lessons",
        "issues": issues,
        "limits": {
            "lesson_body_plain_char_limit": int(state.get("compile_profile", {}).get("lesson_body_plain_char_limit", 5000)),
            "lesson_body_batch_plain_char_limit": int(state.get("compile_profile", {}).get("lesson_body_batch_plain_char_limit", 5000)),
            "lesson_body_context_limit_chars": min(
                int(state.get("compile_profile", {}).get("lesson_body_context_limit_chars", 24000)),
                _llm_prompt_char_limit(state),
            ),
        },
        "revision_prompt": {
            "objective": "Split dense lessons so each lesson is single-topic, fragmented-reading friendly, and generatable in one LLM call.",
            "actions": ["split_lesson"],
            "details": [
                {
                    "target": str(issue.get("lesson_id", "")),
                    "instruction": str(issue.get("reason") or message),
                    "issue_type": "needs_finer_split",
                    "metrics": issue.get("metrics", {}),
                }
                for issue in issues
            ],
        },
    }
    review_issue = {
        "type": "needs_finer_split",
        "severity": "high",
        "message": message,
        "lesson_ids": lesson_ids,
        "unit_ids": [],
        "needs_finer_split": True,
        "stage": stage,
    }
    state["lesson_body_revision_request"] = revision_request
    state["compile_plan_review"] = {
        "passed": False,
        "issues": [review_issue],
        "revise_prompt": revision_request["revision_prompt"],
        "revision_request": revision_request,
        "revision_count": len(state.get("compile_plan_revisions", [])),
    }
    state["lesson_bodies"] = {"lesson_bodies": lesson_bodies}
    state["lesson_body_inputs"] = {"lessons": lesson_body_inputs}
    write_json(target_dir / "lesson_body_revision_request.json", revision_request)
    write_json(target_dir / "compile_plan_review.json", state["compile_plan_review"])
    (target_dir / "compile_plan_review.md").write_text(_render_compile_plan_review_markdown(state["compile_plan_review"]), encoding="utf-8")
    write_json(target_dir / "lesson_bodies.json", state["lesson_bodies"])
    (target_dir / "lesson_bodies.md").write_text(_render_lesson_bodies_markdown(state["lesson_bodies"]), encoding="utf-8")
    write_json(
        target_dir / "lesson_bodies_meta.json",
        {
            "node": "synthesize_lesson_bodies",
            "revision_request": revision_request,
            "local_cache_hits": cache_hits,
            "local_cache_misses": cache_misses,
            "skipped_outside_range": skipped,
            "density_checks": density_checks,
            "lesson_body_inputs": lesson_body_inputs,
            "metadata": metadata,
        },
    )
    state["next_action"] = "revise_compile_plan"
    return state


def _numeric_lesson_pages(lesson: dict[str, Any]) -> list[int]:
    pages: list[int] = []
    for source in lesson.get("sources", []):
        value = source.get("source_page", source.get("page"))
        try:
            pages.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(pages))


def _formula_density_count(value: str) -> int:
    return (
        len(re.findall(r"(?<!\\)\$(?!\$)", value)) // 2
        + len(re.findall(r"\$\$", value)) // 2
        + len(re.findall(r"\\begin\{(?:array|[bpvVB]?matrix|cases|aligned|align|split|gathered)\}", value))
        + len(re.findall(r"\\(?:frac|sum|int|prod|sqrt|left|right|theta|lambda|mu|Phi|Psi)", value))
    )


def _lesson_body_split_reason_message(reasons: list[str]) -> str:
    labels = {
        "estimated_plain_text_exceeds_limit": "预计正文超过 5000 字，无法保证一次完整生成并适合碎片化阅读",
        "single_call_context_too_large": "源材料和提示超过单次 LLM 上下文预算",
        "knowledge_points_too_many": "知识点或来源块过多",
        "multiple_concepts_mixed": "多个概念混杂在同一个 lesson 中",
        "page_span_too_large": "页码跨度过大",
        "derivation_examples_or_formulas_too_dense": "推导、例题或公式过密",
        "formula_image_notes_concentrated": "公式、图片说明或待确认图片集中",
        "generated_lesson_plain_text_exceeds_limit": "生成后的单个 lesson 正文超过 5000 字",
        "generated_batch_plain_text_exceeds_limit": "本次正文生成总量超过 5000 字",
    }
    if not reasons:
        return "Lesson density is acceptable."
    return "；".join(labels.get(reason, reason) for reason in reasons)


def check_markdown_syntax(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Check lesson Markdown and repair full lesson bodies through bounded LLM loops."""

    course_path = course_dir(Path(vault_root), state["course_id"])
    profile = state.get("compile_profile", {})
    max_rounds = int(profile.get("markdown_repair_max_rounds", 3))
    repair_enabled = bool(profile.get("use_llm_lesson_bodies") or profile.get("use_markdown_auto_repair")) and not bool(profile.get("disable_markdown_auto_repair"))
    client = LLMClient.from_env() if repair_enabled else None
    lesson_body_records = {
        str(item.get("lesson_id", "")): item
        for item in state.get("lesson_bodies", {}).get("lesson_bodies", [])
        if isinstance(item, dict)
    }
    input_by_lesson = {
        str(item.get("lesson_id", "")): item
        for item in state.get("lesson_body_inputs", {}).get("lessons", [])
        if isinstance(item, dict)
    }

    lesson_reports: list[dict[str, Any]] = []
    audit_entries: list[dict[str, Any]] = []
    tool_metadata: dict[str, Any] = {}
    all_errors: list[dict[str, Any]] = []

    for lesson in state.get("lessons", []):
        lesson_id = str(lesson.get("id", ""))
        body_record = lesson_body_records.get(lesson_id, {})
        current_markdown = str(body_record.get("body_markdown") or lesson.get("body") or "")
        diagnostics, metadata = _markdown_syntax_diagnostics(current_markdown)
        diagnostics = _attach_lesson_to_markdown_diagnostics(diagnostics, lesson)
        tool_metadata[lesson_id] = metadata
        status = "passed" if not diagnostics else "failed"
        strategy = "none"

        if diagnostics:
            locally_repaired = _repair_markdown_syntax_locally(current_markdown, diagnostics)
            if locally_repaired != current_markdown:
                local_diagnostics, local_metadata = _markdown_syntax_diagnostics(locally_repaired)
                local_diagnostics = _attach_lesson_to_markdown_diagnostics(local_diagnostics, lesson)
                audit_entries.append(
                    {
                        "lesson_id": lesson_id,
                        "round": "local",
                        "agent": "markdown_local_repair",
                        "errors_before": diagnostics,
                        "markdown_before": current_markdown,
                        "markdown_after": locally_repaired,
                        "errors_after": local_diagnostics,
                        "failure_reason": "" if not local_diagnostics else "syntax_errors_remain",
                        "metadata": {"lint": local_metadata},
                    }
                )
                current_markdown = locally_repaired
                diagnostics = local_diagnostics
                tool_metadata[lesson_id] = local_metadata
                if not diagnostics:
                    _apply_repaired_lesson_markdown(lesson, body_record, current_markdown)
                    status = "repaired"
                    strategy = "local_repair"

        if diagnostics and not repair_enabled:
            strategy = "unrepaired_disabled"
        elif diagnostics and client is None:
            strategy = "unrepaired_missing_llm"
            state["errors"].append({"node": "check_markdown_syntax", "lesson_id": lesson_id, "message": "Markdown syntax errors remain and no LLM client is configured."})
        elif diagnostics:
            current_markdown, diagnostics, repair_log, strategy = _repair_lesson_markdown_with_llm(
                client,
                course_path,
                lesson,
                body_record,
                input_by_lesson.get(lesson_id, {}),
                current_markdown,
                diagnostics,
                max_rounds=max_rounds,
                refresh=bool(profile.get("refresh_markdown_repair")),
                prompt_char_limit=_llm_prompt_char_limit(state),
            )
            audit_entries.extend(repair_log)
            if not diagnostics:
                _apply_repaired_lesson_markdown(lesson, body_record, current_markdown)
                status = "repaired"
            else:
                status = "failed"
                state["errors"].append(
                    {
                        "node": "check_markdown_syntax",
                        "lesson_id": lesson_id,
                        "message": f"Markdown syntax errors remain after repair strategy `{strategy}`.",
                    }
                )

        lesson_reports.append(
            {
                "lesson_id": lesson_id,
                "title": str(lesson.get("title", "")),
                "status": status,
                "strategy": strategy,
                "error_count": len(diagnostics),
                "errors": diagnostics,
            }
        )
        all_errors.extend(diagnostics)

    state["lesson_bodies"] = {"lesson_bodies": list(lesson_body_records.values())}
    report = {
        "ok": all(item["error_count"] == 0 for item in lesson_reports),
        "check": "remark-lint plus local Markdown syntax rules",
        "lessons": lesson_reports,
        "errors": all_errors,
        "error_count": len(all_errors),
        "tool_metadata": tool_metadata,
    }
    audit = {
        "max_rounds": max_rounds,
        "entries": audit_entries,
        "lesson_strategies": {str(item.get("lesson_id", "")): str(item.get("strategy", "")) for item in lesson_reports},
        "final_strategy_counts": _count_values([str(item.get("strategy", "")) for item in lesson_reports]),
        "summary": _markdown_repair_audit_summary(audit_entries),
    }
    state["markdown_syntax_report"] = report
    state["markdown_repair_audit"] = audit
    write_json(course_path / "markdown_syntax_report.json", report)
    (course_path / "markdown_syntax_report.md").write_text(_render_markdown_syntax_report(report), encoding="utf-8")
    write_json(course_path / "markdown_repair_audit.json", audit)
    write_json(course_path / "lesson_bodies.json", state["lesson_bodies"])
    (course_path / "lesson_bodies.md").write_text(_render_lesson_bodies_markdown(state["lesson_bodies"]), encoding="utf-8")
    write_json(course_path / "lessons.json", state["lessons"])
    state["next_action"] = "check_grounding_llm"
    return state


def _repair_markdown_syntax_locally(markdown: str, diagnostics: list[dict[str, Any]]) -> str:
    error_types = {str(item.get("type", "")) for item in diagnostics}
    repaired = markdown
    if "formula_markdown_mix" in error_types:
        repaired = _escape_display_math_list_markers(repaired)
    if "list_indentation" in error_types:
        repaired = _normalize_odd_list_indentation(repaired)
    if "code_fence_missing_language" in error_types and "unclosed_code_fence" not in error_types:
        repaired = _add_missing_code_fence_languages(repaired)
    return repaired


def _escape_display_math_list_markers(markdown: str) -> str:
    lines = markdown.splitlines()
    repaired: list[str] = []
    in_math = False
    math_fence = ""
    for line in lines:
        stripped = line.strip()
        if stripped in {"$$", "\\["} and not in_math:
            in_math = True
            math_fence = stripped
            repaired.append(line)
            continue
        if in_math and ((math_fence == "$$" and stripped == "$$") or (math_fence == "\\[" and stripped == "\\]")):
            in_math = False
            math_fence = ""
            repaired.append(line)
            continue
        if in_math:
            line = re.sub(r"^(\s*)([-*+])\s+", r"\1{}\2 ", line)
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if markdown.endswith("\n") else "")


def _normalize_odd_list_indentation(markdown: str) -> str:
    lines = markdown.splitlines()
    repaired: list[str] = []
    for line in lines:
        match = re.match(r"^(\s{1,})([-+*]|\d+[.)])(\s+.*)$", line)
        if match and len(match.group(1)) % 2 != 0:
            spaces = len(match.group(1))
            normalized_spaces = spaces - 1 if spaces > 1 else 2
            line = " " * normalized_spaces + match.group(2) + match.group(3)
        repaired.append(line)
    return "\n".join(repaired) + ("\n" if markdown.endswith("\n") else "")


def _add_missing_code_fence_languages(markdown: str) -> str:
    lines = markdown.splitlines()
    open_fence = ""
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(```+|~~~+)(.*)$", line)
        if not match:
            continue
        indent, marker, info = match.groups()
        marker3 = marker[:3]
        if open_fence:
            if marker3 == open_fence:
                open_fence = ""
            continue
        open_fence = marker3
        if not info.strip():
            lines[index] = f"{indent}{marker}text"
    return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")


def _apply_repaired_lesson_markdown(lesson: dict[str, Any], body_record: dict[str, Any], markdown: str) -> None:
    cleaned = _trim_lesson_body(markdown.strip(), int(lesson.get("body_max_chars", 7200)))
    lesson["body"] = cleaned
    if body_record:
        body_record["body_markdown"] = cleaned


def _repair_lesson_markdown_with_llm(
    client: Any,
    course_path: Path,
    lesson: dict[str, Any],
    body_record: dict[str, Any],
    original_input: dict[str, Any],
    initial_markdown: str,
    initial_errors: list[dict[str, Any]],
    *,
    max_rounds: int,
    refresh: bool,
    prompt_char_limit: int = DEFAULT_LLM_PROMPT_CHAR_LIMIT,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str]:
    lesson_id = str(lesson.get("id", ""))
    current_markdown = initial_markdown
    diagnostics = initial_errors
    repair_log: list[dict[str, Any]] = []
    for round_index in range(1, max_rounds + 1):
        before = current_markdown
        system, user = _markdown_repair_prompt(lesson, body_record, original_input, current_markdown, diagnostics, round_index, prompt_char_limit=prompt_char_limit)
        raw, metadata = _complete_structure_json(
            client,
            course_path,
            f"markdown-repair-{lesson_id}-round-{round_index}",
            system,
            user,
            "markdown_repair",
            refresh=refresh,
        )
        candidate = _extract_repaired_markdown(raw)
        if candidate:
            current_markdown = candidate
        new_diagnostics, lint_metadata = _markdown_syntax_diagnostics(current_markdown)
        new_diagnostics = _attach_lesson_to_markdown_diagnostics(new_diagnostics, lesson)
        repair_log.append(
            {
                "lesson_id": lesson_id,
                "round": round_index,
                "agent": "synthesize_lesson_bodies",
                "errors_before": diagnostics,
                "markdown_before": before,
                "markdown_after": current_markdown,
                "errors_after": new_diagnostics,
                "failure_reason": "" if not new_diagnostics else "syntax_errors_remain",
                "metadata": {"llm": metadata, "lint": lint_metadata},
            }
        )
        diagnostics = new_diagnostics
        if not diagnostics:
            return current_markdown, diagnostics, repair_log, f"repaired_in_{round_index}_rounds"

    summary, regenerated, summary_log = _regenerate_lesson_markdown_after_repair_failures(
        client,
        course_path,
        lesson,
        body_record,
        original_input,
        current_markdown,
        repair_log,
        diagnostics,
        refresh=refresh,
        prompt_char_limit=prompt_char_limit,
    )
    repair_log.extend(summary_log)
    if regenerated:
        current_markdown = regenerated
        diagnostics, _ = _markdown_syntax_diagnostics(current_markdown)
        diagnostics = _attach_lesson_to_markdown_diagnostics(diagnostics, lesson)
    strategy = "summary_regeneration_passed" if not diagnostics else "summary_regeneration_failed"
    if summary:
        repair_log.append(
            {
                "lesson_id": lesson_id,
                "round": "summary",
                "agent": "markdown_repair_summary_agent",
                "final_strategy": strategy,
                "summary": summary,
                "errors_after": diagnostics,
            }
        )
    return current_markdown, diagnostics, repair_log, strategy


def _markdown_repair_prompt(
    lesson: dict[str, Any],
    body_record: dict[str, Any],
    original_input: dict[str, Any],
    current_markdown: str,
    diagnostics: list[dict[str, Any]],
    round_index: int,
    *,
    prompt_char_limit: int = DEFAULT_LLM_PROMPT_CHAR_LIMIT,
) -> tuple[str, str]:
    system = (
        "You are continuing the Synthesize Lesson Bodies conversation for one course lesson. "
        "Repair Markdown syntax only while preserving the source-grounded lesson meaning. Return strict JSON only."
    )
    diagnostics_json = json.dumps(diagnostics, ensure_ascii=False, indent=2)
    checklist_json = json.dumps(body_record.get("checklist", lesson.get("checklist", [])), ensure_ascii=False)
    rules = (
        "Return JSON with schema:\n"
        "{\"lesson_id\":\"lesson-001\",\"title\":\"...\",\"body_markdown\":\"完整修复版 Markdown\","
        "\"checklist\":[\"...\"],\"covered_source_chunk_ids\":[\"...\"],\"repair_notes\":\"...\"}\n\n"
        "Rules:\n"
        "- Output the complete repaired Markdown body, not a patch or diff.\n"
        "- Preserve source-grounded content, citations, formulas, examples, and checklist intent.\n"
        "- Fix only Markdown/LaTeX compatibility issues reported below unless a minimal surrounding rewrite is required.\n"
        "- Do not add course facts that are not present in the original lesson input.\n\n"
        f"Repair round: {round_index}\n"
        f"Lesson id: {lesson.get('id')}\n"
        f"Lesson title: {lesson.get('title')}\n\n"
        f"Current checklist:\n{checklist_json}\n\n"
        "Structured Markdown errors:\n"
        f"{diagnostics_json}\n\n"
    )
    user = _fit_markdown_repair_user_prompt(
        system,
        rules,
        original_input,
        current_markdown,
        prompt_char_limit=prompt_char_limit,
        full_body_required=True,
    )
    return system, user


def _fit_markdown_repair_user_prompt(
    system: str,
    prefix: str,
    original_input: dict[str, Any],
    current_markdown: str,
    *,
    prompt_char_limit: int,
    full_body_required: bool,
) -> str:
    source_system = str(original_input.get("system_prompt", ""))
    source_user = str(original_input.get("user_prompt", ""))
    reserve = max(800, prompt_char_limit - len(system) - len(prefix) - len(current_markdown) - 200)
    system_budget = min(1800, max(200, reserve // 4))
    user_budget = min(5000, max(500, reserve - system_budget))
    body = current_markdown
    body_label = "Current Markdown body"
    note = ""
    user = _render_markdown_repair_user_prompt(prefix, source_system, source_user, body, system_budget, user_budget, body_label, note)
    if _prompt_char_count(system, user) <= prompt_char_limit:
        return user

    system_budget = 500
    user_budget = 1200
    user = _render_markdown_repair_user_prompt(prefix, source_system, source_user, body, system_budget, user_budget, body_label, note)
    if _prompt_char_count(system, user) <= prompt_char_limit:
        return user

    body_budget = max(1200, prompt_char_limit - len(system) - len(prefix) - 2500)
    body = _short_text(current_markdown, body_budget)
    body_label = "Current Markdown body excerpt"
    note = (
        "The full lesson body was too large for one repair prompt. Repair the visible excerpt and preserve structure; "
        "do not invent source facts outside the supplied text.\n\n"
    )
    if full_body_required:
        note += "If the complete body cannot fit, return the best complete body that preserves the supplied excerpt and checklist.\n\n"
    return _render_markdown_repair_user_prompt(prefix, source_system, source_user, body, 400, 800, body_label, note)


def _render_markdown_repair_user_prompt(
    prefix: str,
    source_system: str,
    source_user: str,
    current_markdown: str,
    system_budget: int,
    user_budget: int,
    body_label: str,
    note: str,
) -> str:
    return (
        prefix
        + note
        + f"Original Synthesize Lesson Bodies system prompt:\n{_short_text(source_system, system_budget)}\n\n"
        + f"Original Synthesize Lesson Bodies user prompt:\n{_short_text(source_user, user_budget)}\n\n"
        + f"{body_label}:\n{current_markdown}"
    )


def _extract_repaired_markdown(raw: dict[str, Any]) -> str:
    for key in ("body_markdown", "markdown", "lesson_markdown"):
        value = str(raw.get(key, "")).strip()
        if value:
            return value
    return ""


def _regenerate_lesson_markdown_after_repair_failures(
    client: Any,
    course_path: Path,
    lesson: dict[str, Any],
    body_record: dict[str, Any],
    original_input: dict[str, Any],
    current_markdown: str,
    repair_log: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    *,
    refresh: bool,
    prompt_char_limit: int = DEFAULT_LLM_PROMPT_CHAR_LIMIT,
) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    lesson_id = str(lesson.get("id", ""))
    summary_system = (
        "You are markdown_repair_summary_agent. Summarize repeated Markdown syntax failures and produce reusable repair rules. "
        "Return strict JSON only."
    )
    compact_diagnostics = _compact_markdown_diagnostics(diagnostics, max_items=20)
    compact_log = _compact_markdown_repair_log(repair_log, max_chars=max(1200, prompt_char_limit - len(summary_system) - len(json.dumps(compact_diagnostics, ensure_ascii=False)) - 1800))
    summary_user = (
        "Return JSON with schema:\n"
        "{\"repeated_error_types\":[\"...\"],\"likely_causes\":[\"...\"],\"repair_rules\":[\"...\"],\"regeneration_instruction\":\"...\"}\n\n"
        f"Lesson id: {lesson_id}\n"
        f"Repair log:\n{json.dumps(compact_log, ensure_ascii=False, indent=2)}\n\n"
        f"Remaining errors:\n{json.dumps(compact_diagnostics, ensure_ascii=False, indent=2)}"
    )
    if _prompt_char_count(summary_system, summary_user) > prompt_char_limit:
        compact_log = _compact_markdown_repair_log(repair_log, max_chars=800, include_markdown=False)
        compact_diagnostics = _compact_markdown_diagnostics(diagnostics, max_items=10)
        summary_user = (
            "Return JSON with schema:\n"
            "{\"repeated_error_types\":[\"...\"],\"likely_causes\":[\"...\"],\"repair_rules\":[\"...\"],\"regeneration_instruction\":\"...\"}\n\n"
            f"Lesson id: {lesson_id}\n"
            f"Repair log:\n{json.dumps(compact_log, ensure_ascii=False, indent=2)}\n\n"
            f"Remaining errors:\n{json.dumps(compact_diagnostics, ensure_ascii=False, indent=2)}"
        )
    summary, summary_meta = _complete_structure_json(
        client,
        course_path,
        f"markdown-repair-summary-{lesson_id}",
        summary_system,
        summary_user,
        "markdown_repair_summary",
        refresh=refresh,
    )
    regen_system = (
        "You are a fresh Synthesize Lesson Bodies agent. Regenerate one complete source-grounded lesson body using the original input "
        "and the Markdown repair summary. Return strict JSON only."
    )
    regen_prefix = (
        "Return JSON with schema:\n"
        "{\"lesson_id\":\"lesson-001\",\"title\":\"...\",\"body_markdown\":\"完整 Markdown\","
        "\"checklist\":[\"...\"],\"covered_source_chunk_ids\":[\"...\"]}\n\n"
        "Use the original input as the source of truth. Apply the one-time Markdown repair guidance. Output a complete Markdown body.\n\n"
        f"Repair summary:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
    )
    regen_user = _fit_markdown_repair_user_prompt(
        regen_system,
        regen_prefix,
        original_input,
        current_markdown,
        prompt_char_limit=prompt_char_limit,
        full_body_required=True,
    )
    regenerated_raw, regen_meta = _complete_structure_json(
        client,
        course_path,
        f"markdown-regenerate-{lesson_id}",
        regen_system,
        regen_user,
        "markdown_regeneration",
        refresh=refresh,
    )
    regenerated = _extract_repaired_markdown(regenerated_raw)
    log = [
        {
            "lesson_id": lesson_id,
            "round": "summary",
            "agent": "markdown_repair_summary_agent",
            "errors_before": diagnostics,
            "summary": summary,
            "metadata": summary_meta,
        },
        {
            "lesson_id": lesson_id,
            "round": "regenerate",
            "agent": "synthesize_lesson_bodies",
            "markdown_before": current_markdown,
            "markdown_after": regenerated,
            "metadata": regen_meta,
        },
    ]
    if regenerated and body_record:
        body_record["checklist"] = list(regenerated_raw.get("checklist", body_record.get("checklist", [])))[:8]
        body_record["covered_source_chunk_ids"] = list(regenerated_raw.get("covered_source_chunk_ids", body_record.get("covered_source_chunk_ids", [])))
    return summary, regenerated, log


def _compact_markdown_repair_log(repair_log: list[dict[str, Any]], max_chars: int, include_markdown: bool = True) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    per_markdown = max(160, max_chars // max(1, len(repair_log) * 2))
    for entry in repair_log:
        compact_entry = {
            "lesson_id": entry.get("lesson_id"),
            "round": entry.get("round"),
            "agent": entry.get("agent"),
            "errors_before": _compact_markdown_diagnostics(list(entry.get("errors_before", [])), max_items=8),
            "errors_after": _compact_markdown_diagnostics(list(entry.get("errors_after", [])), max_items=8),
            "failure_reason": entry.get("failure_reason", ""),
        }
        if include_markdown and entry.get("markdown_before"):
            compact_entry["markdown_before_excerpt"] = _short_text(str(entry.get("markdown_before", "")), per_markdown)
        if include_markdown and entry.get("markdown_after"):
            compact_entry["markdown_after_excerpt"] = _short_text(str(entry.get("markdown_after", "")), per_markdown)
        compact.append(compact_entry)
    return compact


def _compact_markdown_diagnostics(diagnostics: list[dict[str, Any]], max_items: int = 20) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in diagnostics[:max_items]:
        compact.append(
            {
                "type": item.get("type"),
                "line": item.get("line"),
                "column": item.get("column"),
                "lesson_id": item.get("lesson_id"),
                "reason": _short_text(str(item.get("reason", "")), 180),
                "suggestion": _short_text(str(item.get("suggestion", "")), 180),
            }
        )
    if len(diagnostics) > max_items:
        type_counts: dict[str, int] = {}
        for item in diagnostics[max_items:]:
            key = str(item.get("type", "unknown"))
            type_counts[key] = type_counts.get(key, 0) + 1
        compact.append({"type": "truncated_diagnostics", "remaining_count": len(diagnostics) - max_items, "remaining_type_counts": type_counts})
    return compact


def _markdown_repair_audit_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    repeated: dict[str, int] = {}
    for entry in entries:
        for error in entry.get("errors_before", []):
            error_type = str(error.get("type", "unknown"))
            repeated[error_type] = repeated.get(error_type, 0) + 1
    return {
        "repair_attempts": sum(1 for entry in entries if isinstance(entry.get("round"), int)),
        "summary_agent_calls": sum(1 for entry in entries if entry.get("agent") == "markdown_repair_summary_agent"),
        "repeated_error_types": repeated,
    }


def _count_values(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _render_markdown_syntax_report(report: dict[str, Any]) -> str:
    lines = ["# Markdown Syntax Report", "", f"- OK: {bool(report.get('ok'))}", ""]
    for lesson in report.get("lessons", []):
        lines.extend(
            [
                f"## {lesson.get('lesson_id')}: {lesson.get('title')}",
                "",
                f"- Status: {lesson.get('status')}",
                f"- Strategy: {lesson.get('strategy')}",
                f"- Error count: {lesson.get('error_count')}",
                "",
            ]
        )
        for error in lesson.get("errors", []):
            lines.append(f"- `{error.get('type')}` line {error.get('line')}: {error.get('reason')} Suggestion: {error.get('suggestion')}")
        if not lesson.get("errors"):
            lines.append("- no syntax errors")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _lesson_body(unit: dict[str, Any]) -> str:
    section = f"Section: {unit['section_title']}\n\n" if unit.get("section_title") else ""
    notes = _normalize_teaching_notes(unit.get("teaching_notes", {}))
    if unit.get("detailed_lesson"):
        return _detailed_lesson_body(unit, notes, section)
    if notes:
        default_goal = f"理解 {unit['title']} 的核心问题和使用场景。"
        parts = [
            f"## {unit['title']}",
            "",
            section.rstrip(),
            "",
            f"Learning goal: {_short_text(notes.get('learning_goal') or default_goal, 180)}",
            "",
            _short_text(notes.get("explanation") or unit["summary"], 520),
        ]
        if notes.get("example"):
            parts.extend(["", f"Example: {_short_text(notes['example'], 260)}"])
        if notes.get("bridge"):
            parts.extend(["", f"Bridge: {_short_text(notes['bridge'], 220)}"])
        parts.extend(["", f"Source note: this lesson is derived from `{unit['source']}`."])
        return "\n".join(part for part in parts if part != "")
    return (
        f"## {unit['title']}\n\n"
        f"{section}"
        f"{unit['summary']}\n\n"
        f"Source note: this lesson is derived from `{unit['source']}`."
    )


def _detailed_lesson_body(unit: dict[str, Any], notes: dict[str, Any], section: str) -> str:
    if unit.get("course_style") == "learn_by_doing":
        return _learn_by_doing_detailed_lesson_body(unit, notes, section)
    default_goal = f"理解 {unit['title']} 的核心问题、关键概念、方法步骤和典型例子。"
    explanation = str(notes.get("explanation") or unit.get("summary", "")).strip()
    highlights = [item for item in unit.get("source_highlights", []) if item]
    summary = str(unit.get("summary", "")).strip()
    parts = [
        f"## {unit['title']}",
        "",
        section.rstrip(),
        "",
        "### 学习目标",
        "",
        str(notes.get("learning_goal") or default_goal).strip(),
        "",
        "### 核心讲解",
        "",
        explanation or summary,
    ]
    if highlights:
        parts.extend(["", "### 课件要点", ""])
        parts.extend(f"- {line}" for line in highlights[:18])
    if notes.get("example"):
        parts.extend(["", "### 例题与直觉", "", str(notes["example"]).strip()])
    if notes.get("bridge"):
        parts.extend(["", "### 前后衔接", "", str(notes["bridge"]).strip()])
    extra = _supplement_lines(summary, highlights)
    if extra:
        parts.extend(["", "### 补充摘录", ""])
        parts.extend(f"- {line}" for line in extra)
    parts.extend(["", f"Source note: this lesson is derived from `{unit['source']}`."])
    body = "\n".join(part for part in parts if part != "")
    return _trim_lesson_body(body, int(unit.get("lesson_body_max_chars", 5200)))


def _learn_by_doing_detailed_lesson_body(unit: dict[str, Any], notes: dict[str, Any], section: str) -> str:
    default_goal = f"完成一个围绕 {unit['title']} 的可验证操作，并理解背后的功能用途。"
    highlights = [item for item in unit.get("source_highlights", []) if item]
    summary = str(unit.get("summary", "")).strip()
    steps = [str(step).strip() for step in notes.get("steps", []) if str(step).strip()]
    failure_modes = [str(item).strip() for item in notes.get("failure_modes", []) if str(item).strip()]
    parts = [
        f"## {unit['title']}",
        "",
        section.rstrip(),
        "",
        "### 学习目标",
        "",
        str(notes.get("learning_goal") or default_goal).strip(),
        "",
        "### 本节任务",
        "",
        str(notes.get("task") or notes.get("example") or summary or default_goal).strip(),
    ]
    if steps:
        parts.extend(["", "### 操作步骤", ""])
        parts.extend(f"{index}. {step}" for index, step in enumerate(steps, start=1))
    elif highlights:
        parts.extend(["", "### 操作步骤", ""])
        parts.extend(f"{index}. {line}" for index, line in enumerate(highlights[:6], start=1))
    if notes.get("expected_result"):
        parts.extend(["", "### 预期结果", "", str(notes["expected_result"]).strip()])
    parts.extend(["", "### 背后的功能", "", str(notes.get("explanation") or summary).strip()])
    if highlights:
        parts.extend(["", "### 源材料要点", ""])
        parts.extend(f"- {line}" for line in highlights[:14])
    if failure_modes:
        parts.extend(["", "### 常见错误", ""])
        parts.extend(f"- {item}" for item in failure_modes)
    if notes.get("bridge"):
        parts.extend(["", "### 下一步练习", "", str(notes["bridge"]).strip()])
    parts.extend(["", f"Source note: this lesson is derived from `{unit['source']}`."])
    body = "\n".join(part for part in parts if part != "")
    return _trim_lesson_body(body, int(unit.get("lesson_body_max_chars", 5200)))


def _trim_lesson_body(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    marker = "\n\n### 源材料补充\n\n"
    if marker in body:
        before, after = body.split(marker, 1)
        reserved = "\n\n[needs_confirmation] 源材料补充较长，已按当前阅读长度限制截断。"
        keep = max(0, limit - len(before) - len(marker) - len(reserved))
        return before + marker + after[:keep].rstrip() + reserved
    return body[: limit - 48].rstrip() + "\n\n[needs_confirmation] 内容较长，已截断。"


def _supplement_lines(summary: str, existing_highlights: list[str], max_lines: int = 8) -> list[str]:
    existing = {_normalize_title(item) for item in existing_highlights}
    selected: list[str] = []
    for line in summary.splitlines():
        cleaned = _short_text(line, 260)
        key = _normalize_title(cleaned)
        if not key or key in existing or _is_low_value_source_line(cleaned):
            continue
        if key in {_normalize_title(item) for item in selected}:
            continue
        selected.append(cleaned)
        if len(selected) >= max_lines:
            break
    return selected


def _short_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value).strip())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _outline_sections(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    by_title: dict[str, dict[str, Any]] = {}
    for lesson in lessons:
        section_title = lesson.get("section_title") or "Course"
        if section_title not in by_title:
            by_title[section_title] = {"title": section_title, "lesson_ids": []}
            sections.append(by_title[section_title])
        by_title[section_title]["lesson_ids"].append(lesson["id"])
    return sections


def check_grounding_llm(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Use an optional LLM validator to detect unsupported claims, captions, and formula explanations."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    failures: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"mode": "disabled"}
    if _requires_validation_llm(state):
        client = LLMClient.from_env()
        if client is None:
            failures.append(
                _validation_failure(
                    "grounding_llm",
                    "missing_llm_validator",
                    "Grounding LLM validation is required but no LLM client is configured.",
                )
            )
            metadata = {"mode": "missing_llm"}
        else:
            system = (
                "You are the grounding validation agent for a source-grounded course compiler. "
                "Check whether lesson body claims, image captions, and formula explanations stay faithful to cited source chunks. "
                "Return strict JSON only."
            )
            user = (
                "Return JSON with schema:\n"
                "{\"ok\":true,\"failures\":[{\"lesson_id\":\"lesson-001\",\"type\":\"unsupported_inference|wrong_caption|wrong_formula_explanation|source_gap|other\","
                "\"message\":\"...\",\"block_id\":\"source-chunk-1\",\"line\":12,\"source_page\":\"3\",\"image_id\":\"image-001\"}]}\n\n"
                "Flag: ungrounded inference, unsupported bridge content not marked as bridge/inferred, wrong image captions, wrong formula explanations, and claims not traceable to cited blocks.\n"
                "Each failure must identify lesson_id, the best source block_id if known, line in the lesson body when possible, and the reason.\n\n"
                f"{_validation_context_for_prompt(state)}"
            )
            try:
                raw, meta = _complete_structure_json(
                    client,
                    target_dir,
                    "grounding-validation",
                    system,
                    user,
                    "grounding_validation",
                    refresh=bool(state.get("compile_profile", {}).get("refresh_validation_llm")),
                )
                failures = _normalize_validation_failures(raw, "grounding_llm", state)
                if bool(raw.get("ok", not failures)) is False and not failures:
                    failures.append(_validation_failure("grounding_llm", "llm_rejected", "Grounding LLM validation failed without structured failures."))
                metadata = meta
            except Exception as exc:  # pragma: no cover - external LLM behavior
                failures.append(_validation_failure("grounding_llm", "llm_validation_error", f"Grounding LLM validation failed: {exc}"))
                metadata = {"mode": "llm_error"}

    _update_validation_report(state, vault_root, "grounding_llm", "LLM grounding fidelity", failures, metadata)
    state["next_action"] = "check_grounding_rules"
    return state


def check_grounding_rules(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Deterministically verify source/page/block/image/bbox traceability."""

    locator = SourceLocator.from_state(state)
    failures: list[dict[str, Any]] = []

    for lesson in state["lessons"]:
        sources = list(lesson.get("sources", []))
        if not sources:
            failures.append(_validation_failure("grounding_rules", "sources_missing", "Lesson has no sources.", lesson))
            continue
        for item in locator.verify_citations(sources).get("failures", []):
            source = item.get("source", {})
            chunk = item.get("chunk", {})
            failure_type = str(item.get("type", "source_chunk_missing"))
            messages = {
                "citation_source_id_missing": "Source reference is missing stable source_id.",
                "source_chunk_missing": "Source chunk does not exist.",
                "block_id_missing": "Source reference is missing block_id.",
                "block_id_untraceable": "Source block_id does not match the parsed chunk.",
                "source_page_missing": "Source reference is missing source_page/page.",
                "source_page_untraceable": "Source page does not match the parsed chunk.",
                "bbox_missing": "Source reference is missing bbox from parser metadata.",
                "source_quote_missing": "Source reference is missing a quote.",
                "source_quote_untraceable": "Source quote not found in parsed chunk.",
            }
            failures.append(_validation_failure("grounding_rules", failure_type, messages.get(failure_type, "Source citation is not traceable."), lesson, source, chunk))

        for image in list(lesson.get("images", [])) + list(lesson.get("pending_image_confirmations", [])):
            for item in locator.verify_images([image]).get("failures", []):
                failure_type = str(item.get("type", "image_id_untraceable"))
                messages = {
                    "image_id_missing": "Lesson image is missing image_id.",
                    "image_id_untraceable": "Lesson image_id is not present in image_understanding.json.",
                    "image_source_chunk_missing": "Image source_chunk_id does not exist.",
                    "image_bbox_missing": "Lesson image is missing bbox metadata.",
                }
                failures.append(_validation_failure("grounding_rules", failure_type, messages.get(failure_type, "Image evidence is not traceable."), lesson, image=image))

    _update_validation_report(state, vault_root, "grounding_rules", "Source/page/block/image/bbox traceability", failures, {"mode": "rules"})
    state["next_action"] = "check_quality_llm"
    return state


def check_quality_llm(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Use an optional LLM validator to inspect coherence, structure, and visual/page-note pollution."""

    target_dir = course_dir(Path(vault_root), state["course_id"])
    failures: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {"mode": "disabled"}
    if _requires_validation_llm(state):
        client = LLMClient.from_env()
        if client is None:
            failures.append(_validation_failure("quality_llm", "missing_llm_validator", "Quality LLM validation is required but no LLM client is configured."))
            metadata = {"mode": "missing_llm"}
        else:
            system = (
                "You are the quality validation agent for a course compiler. Check lesson coherence, chapter structure, and body cleanliness. "
                "Return strict JSON only."
            )
            user = (
                "Return JSON with schema:\n"
                "{\"ok\":true,\"failures\":[{\"lesson_id\":\"lesson-001\",\"type\":\"incoherent_explanation|bad_structure|visual_tool_pollution|page_note_pollution|other\","
                "\"message\":\"...\",\"block_id\":\"source-chunk-1\",\"line\":12}]}\n\n"
                "Flag incoherent explanations, unreasonable chapter structure, examples isolated as chapters, visual tool instructions, layout notes, page numbers, headers, footers, and OCR/layout comments polluting the teaching body.\n"
                "Each failure must identify lesson_id, line in the lesson body when possible, block_id if related, and a concrete reason.\n\n"
                f"{_validation_context_for_prompt(state)}"
            )
            try:
                raw, meta = _complete_structure_json(
                    client,
                    target_dir,
                    "quality-validation",
                    system,
                    user,
                    "quality_validation",
                    refresh=bool(state.get("compile_profile", {}).get("refresh_validation_llm")),
                )
                failures = _normalize_validation_failures(raw, "quality_llm", state)
                if bool(raw.get("ok", not failures)) is False and not failures:
                    failures.append(_validation_failure("quality_llm", "llm_rejected", "Quality LLM validation failed without structured failures."))
                metadata = meta
            except Exception as exc:  # pragma: no cover - external LLM behavior
                failures.append(_validation_failure("quality_llm", "llm_validation_error", f"Quality LLM validation failed: {exc}"))
                metadata = {"mode": "llm_error"}

    _update_validation_report(state, vault_root, "quality_llm", "LLM coherence and pollution review", failures, metadata)
    state["next_action"] = "check_quality_rules"
    return state


def check_quality_rules(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Deterministically validate schema, titles, Markdown, formulas, and minimum lesson substance."""

    failures: list[dict[str, Any]] = []
    required_fields = {"id", "title", "unit_ids", "body", "checklist", "sources", "order"}
    title_to_lessons: dict[str, list[dict[str, Any]]] = {}
    plain_char_by_lesson: dict[str, int] = {}
    if _normalize_compiled_lessons(state):
        write_json(course_dir(Path(vault_root), state["course_id"]) / "lessons.json", state["lessons"])
    for lesson in state["lessons"]:
        lesson_id = str(lesson.get("id", "unknown"))
        title = str(lesson.get("title", "")).strip()
        body = str(lesson.get("body", ""))
        plain_chars = len(_plain_markdown_text(body))
        plain_char_by_lesson[lesson_id] = plain_chars
        lesson["plain_char_count"] = plain_chars
        missing = sorted(required_fields - set(lesson))
        if missing:
            failures.append(_validation_failure("quality_rules", "schema_missing_fields", f"Missing fields: {missing}", lesson))
        if not body.strip():
            failures.append(_validation_failure("quality_rules", "empty_lesson", "Lesson body is empty.", lesson))
        elif plain_chars < int(state.get("compile_profile", {}).get("quality_min_lesson_chars", 24)):
            failures.append(_validation_failure("quality_rules", "short_lesson", "Lesson body is too short for standalone reading.", lesson, line=1))
        body_limit = min(
            int(lesson.get("body_max_chars", state.get("compile_profile", {}).get("lesson_body_max_chars", 7200))),
            int(state.get("compile_profile", {}).get("lesson_body_plain_char_limit", 5000)),
        )
        if plain_chars > body_limit:
            failures.append(_validation_failure("quality_rules", "body_too_long", f"Lesson body exceeds {body_limit} plain-text chars after Markdown markers are removed.", lesson, line=1))
        if not lesson.get("checklist"):
            failures.append(_validation_failure("quality_rules", "checklist_missing", "Lesson has no checklist.", lesson))
        if not title or _bad_independent_lesson_title(title):
            failures.append(_validation_failure("quality_rules", "abnormal_title", "Lesson title is empty, generic, or a fragment that should not stand alone.", lesson))
        if re.search(r"^(page|第?\s*\d+\s*页|幻灯片\s*\d+|slide\s*\d+)$", title, re.IGNORECASE):
            failures.append(_validation_failure("quality_rules", "page_title", "Lesson title appears to be a page or slide marker.", lesson))
        title_to_lessons.setdefault(_normalize_title(title), []).append(lesson)

        code_line = _unclosed_code_fence_line(body)
        if code_line:
            failures.append(_validation_failure("quality_rules", "unclosed_code_block", "Markdown code fence is not closed.", lesson, line=code_line))
        formula_line = _broken_formula_line(body)
        if formula_line:
            failures.append(_validation_failure("quality_rules", "broken_formula", "Displayed formula delimiter or LaTeX environment appears broken.", lesson, line=formula_line))
        mixed_line = _formula_markdown_mix_line(body)
        if mixed_line:
            failures.append(_validation_failure("quality_rules", "formula_markdown_mix", "Markdown list marker appears inside a matrix/cases/aligned formula block.", lesson, line=mixed_line))
        pollution_line = _visual_or_page_pollution_line(body)
        if pollution_line:
            failures.append(_validation_failure("quality_rules", "visual_or_page_note_pollution", "Body contains visual/layout/page-note text instead of teaching content.", lesson, line=pollution_line))
        ocr_line = _ocr_marker_pollution_line(body)
        if ocr_line:
            failures.append(_validation_failure("quality_rules", "ocr_marker_pollution", "Body contains OCR paragraph marker glyphs instead of Markdown list syntax.", lesson, line=ocr_line))

    for normalized_title, lessons in title_to_lessons.items():
        if normalized_title and len(lessons) > 1:
            for lesson in lessons:
                failures.append(_validation_failure("quality_rules", "duplicate_title", f"Duplicate lesson title: {lesson.get('title')}", lesson))

    _update_validation_report(
        state,
        vault_root,
        "quality_rules",
        "Deterministic lesson quality checks",
        failures,
        {
            "mode": "rules",
            "plain_char_by_lesson": plain_char_by_lesson,
            "plain_char_total": sum(plain_char_by_lesson.values()),
            "plain_char_limit": int(state.get("compile_profile", {}).get("lesson_body_plain_char_limit", 5000)),
        },
    )
    state["next_action"] = "export_version" if state.get("validation_report", {}).get("ok") else "repair_course"
    return state


def check_grounding(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Backward-compatible rules-only grounding wrapper."""

    return check_grounding_rules(state, vault_root)


def check_quality(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Backward-compatible rules-only quality wrapper."""

    return check_quality_rules(state, vault_root)


def _requires_validation_llm(state: CourseCompileState) -> bool:
    profile = state.get("compile_profile", {})
    return bool(profile.get("use_llm_validation", profile.get("use_llm", False)))


def _update_validation_report(
    state: CourseCompileState,
    vault_root: Path | str,
    stage: str,
    check_name: str,
    failures: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> None:
    previous = state.get("validation_report", {}) if isinstance(state.get("validation_report", {}), dict) else {}
    layers = dict(previous.get("layers", {}))
    checks = _dedupe_keep_order([str(item) for item in previous.get("checks", [])] + [stage])
    layer = {
        "ok": not failures,
        "check": check_name,
        "failure_count": len(failures),
        "failures": failures,
        "metadata": metadata or {},
    }
    layers[stage] = layer
    all_failures: list[dict[str, Any]] = []
    for key in ("grounding_llm", "grounding_rules", "quality_llm", "quality_rules"):
        all_failures.extend(layers.get(key, {}).get("failures", []))
    validation_report = {
        "ok": all(layer.get("ok", True) for layer in layers.values()),
        "checks": checks,
        "layers": layers,
        "failures": all_failures,
    }
    state["validation_report"] = validation_report
    write_json(course_dir(Path(vault_root), state["course_id"]) / "validation_report.json", validation_report)


def _validation_failure(
    stage: str,
    failure_type: str,
    message: str,
    lesson: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    chunk: dict[str, Any] | None = None,
    *,
    line: int | None = None,
    image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lesson = lesson or {}
    source = source or {}
    chunk = chunk or {}
    image = image or {}
    block_id = str(source.get("block_id") or source.get("chunk_id") or chunk.get("block_id") or chunk.get("id") or image.get("source_chunk_id") or "")
    source_page = source.get("source_page", source.get("page", chunk.get("page", chunk.get("page_idx", image.get("page_idx")))))
    bbox = source.get("bbox") or chunk.get("bbox") or image.get("bbox") or []
    failure_line = line
    if failure_line is None:
        failure_line = source.get("line") or source.get("start_line") or chunk.get("start_line") or 1
    return {
        "stage": stage,
        "type": failure_type,
        "lesson_id": str(lesson.get("id") or source.get("lesson_id") or "unknown"),
        "lesson_title": str(lesson.get("title") or ""),
        "block_id": block_id,
        "source_page": source_page,
        "image_id": str(image.get("id") or source.get("image_id") or ""),
        "bbox": bbox,
        "line": int(failure_line) if str(failure_line).isdigit() else failure_line,
        "message": _short_text(message, 520),
        "reason": _short_text(message, 520),
    }


def _normalize_validation_failures(raw: dict[str, Any], stage: str, state: CourseCompileState) -> list[dict[str, Any]]:
    lesson_by_id = {str(lesson.get("id", "")): lesson for lesson in state.get("lessons", [])}
    failures: list[dict[str, Any]] = []
    for item in raw.get("failures", []):
        if not isinstance(item, dict):
            continue
        lesson = lesson_by_id.get(str(item.get("lesson_id", "")), {})
        source = _source_for_validation_item(lesson, item)
        failures.append(
            _validation_failure(
                stage,
                str(item.get("type") or "other"),
                str(item.get("message") or item.get("reason") or "Validation failure."),
                lesson,
                source,
                line=_coerce_positive_int(item.get("line")),
                image={"id": item.get("image_id", ""), "bbox": item.get("bbox", [])},
            )
        )
    return failures


def _source_for_validation_item(lesson: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    block_id = str(item.get("block_id") or "")
    for source in lesson.get("sources", []):
        if block_id and block_id in {str(source.get("block_id") or ""), str(source.get("chunk_id") or "")}:
            return source
    sources = lesson.get("sources", [])
    return sources[0] if sources else {}


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _validation_context_for_prompt(state: CourseCompileState, max_chars: int | None = None) -> str:
    profile = state.get("compile_profile", {})
    max_chars = int(max_chars or profile.get("validation_prompt_max_chars", 24000))
    chunk_by_id = {str(chunk.get("id", "")): chunk for chunk in state.get("parsed_chunks", [])}
    lines: list[str] = ["Course validation context:"]
    for lesson in state.get("lessons", []):
        body = str(lesson.get("body", ""))
        numbered_body = "\n".join(f"{index}: {line}" for index, line in enumerate(body.splitlines(), start=1))
        if len(numbered_body) > 2200:
            numbered_body = numbered_body[:2200].rstrip() + "\n..."
        lines.extend(
            [
                "",
                f"Lesson {lesson.get('id')}: {lesson.get('title')}",
                f"Section: {lesson.get('section_title', '')}",
                "Body with line numbers:",
                numbered_body,
                "Sources:",
            ]
        )
        for source in lesson.get("sources", [])[:8]:
            chunk = chunk_by_id.get(str(source.get("chunk_id", "")), {})
            content = _short_text(str(chunk.get("content", "")), 700)
            lines.extend(
                [
                    f"- block_id: {source.get('block_id') or source.get('chunk_id')}",
                    f"  source_page: {source.get('source_page', source.get('page'))}",
                    f"  bbox: {source.get('bbox', [])}",
                    f"  quote: {_short_text(str(source.get('quote', '')), 260)}",
                    f"  chunk_content: {content}",
                ]
            )
        if lesson.get("images"):
            lines.append("Images:")
            for image in lesson.get("images", [])[:6]:
                lines.append(
                    f"- image_id: {image.get('id')} source_chunk_id: {image.get('source_chunk_id')} bbox: {image.get('bbox', [])} caption: {image.get('caption', '')} summary: {_short_text(str(image.get('summary', '')), 260)}"
                )
        text = "\n".join(lines)
        if len(text) >= max_chars:
            return text[:max_chars].rstrip() + "\n..."
    return "\n".join(lines)


def _plain_markdown_text(value: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", value)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"[#>*_`|$\\\-\[\]{}()]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_compiled_lessons(state: CourseCompileState) -> bool:
    changed = False
    for lesson in state.get("lessons", []):
        if "body" in lesson:
            body = str(lesson.get("body", ""))
            normalized = _normalize_compiled_markdown(body)
            if normalized != body:
                lesson["body"] = normalized
                changed = True
        if "checklist" in lesson:
            checklist = [_strip_ocr_paragraph_marker(str(item)).strip() for item in lesson.get("checklist", [])]
            if checklist != lesson.get("checklist", []):
                lesson["checklist"] = checklist
                changed = True
    return changed


def _normalize_compiled_markdown(value: str) -> str:
    """Clean compiler-produced lesson Markdown without changing raw or parsed files."""

    lines = str(value or "").splitlines()
    normalized: list[str] = []
    formula_lines: list[str] = []
    in_code = False
    in_math = False
    math_fence = ""

    def flush_formula() -> None:
        if not formula_lines:
            return
        normalized.append("$$")
        normalized.extend(formula_lines)
        normalized.append("$$")
        formula_lines.clear()

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            flush_formula()
            normalized.append(raw_line)
            in_code = not in_code
            continue
        if in_code:
            normalized.append(raw_line)
            continue

        if stripped.startswith("$$") and stripped.endswith("$$") and stripped.count("$$") >= 2:
            flush_formula()
            normalized.append(line if (line := _strip_ocr_paragraph_marker(raw_line)) else raw_line)
            continue
        if stripped in {"$$", "\\["} and not in_math:
            flush_formula()
            normalized.append(raw_line)
            in_math = True
            math_fence = stripped
            continue
        if in_math:
            normalized.append(raw_line)
            if (math_fence == "$$" and stripped == "$$") or (math_fence == "\\[" and stripped == "\\]"):
                in_math = False
                math_fence = ""
            continue

        line = _strip_ocr_paragraph_marker(raw_line)
        if _looks_like_standalone_formula_line(line):
            formula_lines.append(line.strip())
            continue
        flush_formula()
        normalized.append(line)

    flush_formula()
    return "\n".join(normalized).strip()


def _strip_ocr_paragraph_marker(line: str) -> str:
    marker_class = re.escape(OCR_PARAGRAPH_MARKERS)
    cleaned = re.sub(rf"^(\s*(?:[-*+]\s+|\d+[.)]\s+)?)\s*[{marker_class}]\s*", r"\1", str(line))
    cleaned = re.sub(rf"(?<=[\s，。；：、,.;:])\s*[{marker_class}]\s+", " ", cleaned)
    return cleaned


def _looks_like_standalone_formula_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _is_markdown_block_line(stripped):
        return False
    if re.search(r"[\u4e00-\u9fff]", stripped) and not stripped.startswith("\\"):
        return False
    latex_command = r"\\(?:left|right|begin|end|frac|mu|lambda|vdots|ddots|cdots|boxed|sqrt|sum|int|prod|times|in|dots|prime)"
    if re.search(r"\\begin\{(?:array|[bpvVB]?matrix|cases|aligned|align|split|gathered)\}", stripped):
        return True
    if re.search(latex_command, stripped) and re.search(r"(=|\\begin|\\left|\\right|&|\\\\|\+|-)", stripped):
        return True
    if re.match(r"^[A-Za-z]\s*(?:\([^)]*\)|\^\s*\{?\\?prime|\_\s*\{?[^=]+)?\s*=", stripped) and "\\" in stripped:
        return True
    return False


def _is_markdown_block_line(stripped: str) -> bool:
    return bool(
        re.match(r"^(#{1,6})\s+", stripped)
        or re.match(r"^[-*+]\s+", stripped)
        or re.match(r"^\d+[.)]\s+", stripped)
        or re.match(r"^- \[[ xX]\]\s+", stripped)
        or stripped.startswith((">", "!", "|", "Source note:"))
    )


def _unclosed_code_fence_line(value: str) -> int | None:
    open_line: int | None = None
    for index, line in enumerate(value.splitlines(), start=1):
        if line.strip().startswith("```"):
            open_line = index if open_line is None else None
    return open_line


def _broken_formula_line(value: str) -> int | None:
    dollar_line: int | None = None
    bracket_line: int | None = None
    env_stack: list[tuple[str, int]] = []
    for index, line in enumerate(value.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "$$":
            dollar_line = index if dollar_line is None else None
        if stripped == "\\[":
            bracket_line = index
        elif stripped == "\\]":
            bracket_line = None
        for env in re.findall(r"\\begin\{([^}]+)\}", line):
            env_stack.append((env, index))
        for env in re.findall(r"\\end\{([^}]+)\}", line):
            if env_stack and env_stack[-1][0] == env:
                env_stack.pop()
            else:
                return index
    if dollar_line is not None:
        return dollar_line
    if bracket_line is not None:
        return bracket_line
    if env_stack:
        return env_stack[-1][1]
    return None


def _formula_markdown_mix_line(value: str) -> int | None:
    in_math = False
    math_fence = ""
    formula_env_depth = 0
    for index, line in enumerate(value.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("$$") and stripped.endswith("$$") and stripped.count("$$") >= 2:
            continue
        if stripped in {"$$", "\\["} and not in_math:
            in_math = True
            math_fence = stripped
            continue
        if in_math and ((math_fence == "$$" and stripped == "$$") or (math_fence == "\\[" and stripped == "\\]")):
            in_math = False
            math_fence = ""
            continue
        begin_count = len(re.findall(r"\\begin\{(cases|[bpvVB]?matrix|aligned)\}", line))
        end_count = len(re.findall(r"\\end\{(cases|[bpvVB]?matrix|aligned)\}", line))
        line_env_depth = formula_env_depth + begin_count
        if (in_math or formula_env_depth > 0) and re.match(r"^[-*+]\s+", stripped):
            return index
        if line_env_depth > 0 and end_count < begin_count and re.match(r"^[-*+]\s+", stripped):
            return index
        formula_env_depth = max(0, formula_env_depth + begin_count - end_count)
    return None


def _visual_or_page_pollution_line(value: str) -> int | None:
    tokens = (
        "视觉关系",
        "视觉说明",
        "排版说明",
        "图片说明",
        "page header",
        "page footer",
        "teacher note",
        "image caption",
        "layout note",
        "页码",
        "第 页",
    )
    for index, line in enumerate(value.splitlines(), start=1):
        normalized = line.lower()
        if any(token in normalized for token in tokens):
            return index
        if re.match(r"^\s*(page|slide)\s+\d+\s*$", line, re.IGNORECASE):
            return index
        if re.match(r"^\s*第\s*\d+\s*页\s*$", line):
            return index
    return None


def _ocr_marker_pollution_line(value: str) -> int | None:
    marker_class = re.escape(OCR_PARAGRAPH_MARKERS)
    for index, line in enumerate(value.splitlines(), start=1):
        if re.match(rf"^\s*(?:[-*+]\s+|\d+[.)]\s+)?[{marker_class}]", line):
            return index
    return None


def repair_course(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Apply safe local repairs before human review is needed."""

    repairs: list[dict[str, Any]] = []
    state, source_repairs = _repair_grounding_with_source_tool(state)
    repairs.extend(source_repairs)
    state, semantic_repairs = _repair_semantic_failures_with_patches(state, vault_root)
    repairs.extend(semantic_repairs)
    state, length_repairs = _repair_long_lessons_with_split_patches(state, vault_root)
    repairs.extend(length_repairs)
    schema_repairs = _repair_lesson_schema_mechanics(state)
    repairs.extend(schema_repairs)
    for lesson in state["lessons"]:
        if "checklist" not in lesson or not lesson["checklist"]:
            lesson["checklist"] = [f"复习：{lesson.get('title', 'Untitled')}"]
            repairs.append({"lesson_id": lesson.get("id"), "repair": "added_default_checklist", "repair_class": "mechanical"})

    repair_report = {
        "repairs": repairs,
        "repair_classes": _count_values([str(item.get("repair_class", "mechanical")) for item in repairs]),
        "requires_human_review": not repairs,
    }
    target_dir = course_dir(Path(vault_root), state["course_id"])
    write_json(target_dir / "lessons.json", state["lessons"])
    write_json(target_dir / "compile_patches.json", state.get("compile_patches", []))
    write_json(course_dir(Path(vault_root), state["course_id"]) / "repair_report.json", repair_report)
    state["repair_report"] = repair_report
    state["next_action"] = "human_review" if repair_report["requires_human_review"] else "check_grounding_llm"
    return state


def _repair_lesson_schema_mechanics(state: CourseCompileState) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    unit_by_id = {str(unit.get("id", "")): unit for unit in state.get("units", [])}
    for index, lesson in enumerate(state.get("lessons", []), start=1):
        lesson_id = str(lesson.get("id") or f"lesson-{index:03d}")
        if not lesson.get("id"):
            lesson["id"] = lesson_id
            repairs.append({"lesson_id": lesson_id, "repair": "filled_missing_lesson_id", "repair_class": "mechanical"})
        if not lesson.get("order"):
            lesson["order"] = index
            repairs.append({"lesson_id": lesson_id, "repair": "filled_missing_order", "repair_class": "mechanical"})
        if not str(lesson.get("title", "")).strip():
            lesson["title"] = f"Untitled Lesson {index}"
            repairs.append({"lesson_id": lesson_id, "repair": "filled_missing_title", "repair_class": "mechanical"})
        if "unit_ids" not in lesson or not isinstance(lesson.get("unit_ids"), list):
            lesson["unit_ids"] = []
            repairs.append({"lesson_id": lesson_id, "repair": "filled_missing_unit_ids", "repair_class": "mechanical"})
        if not str(lesson.get("body", "")).strip():
            lesson["body"] = f"[needs_confirmation] {lesson.get('title', lesson_id)} 暂无可验证正文，需要回到源材料补全。"
            repairs.append({"lesson_id": lesson_id, "repair": "filled_empty_body", "repair_class": "mechanical"})
        if "sources" not in lesson or not isinstance(lesson.get("sources"), list):
            lesson["sources"] = []
            repairs.append({"lesson_id": lesson_id, "repair": "filled_missing_sources", "repair_class": "mechanical"})
        if not lesson.get("sources"):
            derived_sources = _sources_from_lesson_units(lesson, unit_by_id)
            if derived_sources:
                lesson["sources"] = derived_sources
                repairs.append({"lesson_id": lesson_id, "repair": "filled_sources_from_units", "repair_class": "mechanical"})
    return repairs


def _sources_from_lesson_units(lesson: dict[str, Any], unit_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for unit_id in lesson.get("unit_ids", []):
        unit = unit_by_id.get(str(unit_id), {})
        for ref in unit.get("source_refs", []):
            if isinstance(ref, dict):
                sources.append(dict(ref))
        chunk_id = str(unit.get("source_chunk_id", ""))
        if chunk_id and not any(str(source.get("chunk_id", "")) == chunk_id for source in sources):
            sources.append(
                {
                    "source": unit.get("source", ""),
                    "chunk_id": chunk_id,
                    "block_id": chunk_id,
                    "quote": unit.get("source_quote", ""),
                }
            )
    return _dedupe_sources(sources)


def _repair_long_lessons_with_split_patches(state: CourseCompileState, vault_root: Path | str) -> tuple[CourseCompileState, list[dict[str, Any]]]:
    profile = state.get("compile_profile", {})
    plain_limit = int(profile.get("lesson_body_plain_char_limit", 5000))
    revision_tool = SourceRevisionTool(vault_root, state["course_id"])
    repaired_state: CourseCompileState = state
    repairs: list[dict[str, Any]] = []
    for lesson in list(repaired_state.get("lessons", [])):
        lesson_id = str(lesson.get("id", ""))
        body = str(lesson.get("body", ""))
        lesson_limit = min(int(lesson.get("body_max_chars", plain_limit)), plain_limit)
        plain_chars = len(_plain_markdown_text(body))
        if plain_chars <= lesson_limit:
            continue
        replacement_lessons = _split_lesson_to_plain_limit(lesson, lesson_limit)
        if len(replacement_lessons) <= 1:
            new_body = _trim_lesson_body_to_plain_limit(body, lesson_limit)
            patch = revision_tool.propose_lesson_body_patch(
                repaired_state,
                lesson_id,
                new_body,
                reason="Mechanical repair: trim lesson body to the configured plain-text length limit.",
            )
        else:
            patch = revision_tool.propose_split_lesson_patch(
                repaired_state,
                lesson_id,
                replacement_lessons,
                reason="Mechanical repair: split an overlong lesson into local numbered lesson parts.",
            )
        repaired_state = revision_tool.apply_patch(repaired_state, patch)  # type: ignore[assignment]
        repairs.append(
            {
                "lesson_id": lesson_id,
                "repair": "split_long_lesson" if len(replacement_lessons) > 1 else "trimmed_long_body",
                "repair_class": "mechanical",
                "plain_chars": plain_chars,
                "plain_limit": lesson_limit,
                "patch_id": patch["id"],
            }
        )
    return repaired_state, repairs


def _split_lesson_to_plain_limit(lesson: dict[str, Any], plain_limit: int) -> list[dict[str, Any]]:
    body = str(lesson.get("body", "")).strip()
    if len(_plain_markdown_text(body)) <= plain_limit:
        return [lesson]
    chunks = _body_chunks_for_plain_limit(body, plain_limit)
    if len(chunks) <= 1:
        return []
    parts: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        part = dict(lesson)
        part.update(
            {
                "title": _lesson_part_title(str(lesson.get("title", "")), index, len(chunks)),
                "body": chunk,
                "body_max_chars": plain_limit,
                "plain_char_count": len(_plain_markdown_text(chunk)),
            }
        )
        parts.append(part)
    return parts


def _body_chunks_for_plain_limit(body: str, plain_limit: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current: list[str] = []
    for paragraph in paragraphs:
        candidates = [paragraph]
        if len(_plain_markdown_text(paragraph)) > plain_limit:
            candidates = _split_oversized_paragraph(paragraph, plain_limit)
        for candidate in candidates:
            proposed = "\n\n".join(current + [candidate]).strip()
            if current and len(_plain_markdown_text(proposed)) > plain_limit:
                chunks.append("\n\n".join(current).strip())
                current = [candidate]
            else:
                current.append(candidate)
    if current:
        chunks.append("\n\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _split_oversized_paragraph(paragraph: str, plain_limit: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？.!?])", paragraph)
    parts: list[str] = []
    current = ""
    for sentence in [item.strip() for item in sentences if item.strip()]:
        proposed = (current + sentence).strip()
        if current and len(_plain_markdown_text(proposed)) > plain_limit:
            parts.append(current.strip())
            current = sentence
        else:
            current = proposed
    if current:
        parts.append(current.strip())
    if len(parts) == 1 and len(_plain_markdown_text(parts[0])) > plain_limit:
        text = parts[0]
        return [text[index : index + plain_limit] for index in range(0, len(text), plain_limit)]
    return parts


def _trim_lesson_body_to_plain_limit(body: str, plain_limit: int) -> str:
    if len(_plain_markdown_text(body)) <= plain_limit:
        return body
    kept: list[str] = []
    for line in body.splitlines():
        proposed = "\n".join(kept + [line]).strip()
        if len(_plain_markdown_text(proposed)) > max(0, plain_limit - 40):
            break
        kept.append(line)
    return "\n".join(kept).rstrip() + "\n\n[needs_confirmation] 内容超过长度限制，已截断并等待人工复核。"


def _repair_semantic_failures_with_patches(state: CourseCompileState, vault_root: Path | str) -> tuple[CourseCompileState, list[dict[str, Any]]]:
    failures = list(state.get("validation_report", {}).get("failures", []))
    if not failures:
        return state, []
    semantic_types = {
        "unsupported_inference",
        "wrong_caption",
        "wrong_formula_explanation",
        "source_gap",
        "incoherent_explanation",
        "bad_structure",
        "visual_tool_pollution",
        "page_note_pollution",
        "other",
    }
    revision_tool = SourceRevisionTool(vault_root, state["course_id"])
    locator = SourceLocator.from_state(state)
    repaired_state: CourseCompileState = state
    repairs: list[dict[str, Any]] = []
    repaired_keys: set[tuple[str, str, int]] = set()
    for failure in failures:
        failure_type = str(failure.get("type", ""))
        lesson_id = str(failure.get("lesson_id", ""))
        line = int(failure.get("line", 0) or 0)
        key = (lesson_id, failure_type, line)
        if failure_type not in semantic_types or not lesson_id or key in repaired_keys:
            continue
        lesson = next((item for item in repaired_state.get("lessons", []) if str(item.get("id", "")) == lesson_id), None)
        if lesson is None:
            continue
        if failure_type == "wrong_caption":
            patched, patch = _semantic_patch_wrong_caption(revision_tool, repaired_state, lesson, failure, locator)
        else:
            patched, patch = _semantic_patch_lesson_body(revision_tool, repaired_state, lesson, failure, locator)
        if not patched or not patch:
            continue
        repaired_state = patched  # type: ignore[assignment]
        repaired_keys.add(key)
        repairs.append({"lesson_id": lesson_id, "repair": f"semantic_{failure_type}", "repair_class": "semantic", "patch_id": patch["id"]})
    return repaired_state, repairs


def _semantic_patch_lesson_body(
    revision_tool: SourceRevisionTool,
    state: CourseCompileState,
    lesson: dict[str, Any],
    failure: dict[str, Any],
    locator: SourceLocator,
) -> tuple[CourseCompileState | None, dict[str, Any] | None]:
    body = str(lesson.get("body", ""))
    line = int(failure.get("line", 0) or 0)
    lines = body.splitlines()
    if line > 0 and line <= len(lines):
        lines[line - 1] = f"[needs_confirmation] {str(failure.get('message') or failure.get('type') or '该处需要源材料确认。')}"
        new_body = "\n".join(lines)
    else:
        new_body = body.rstrip() + f"\n\n[needs_confirmation] {str(failure.get('message') or failure.get('type') or '该 lesson 存在语义或 grounding 问题，需要源材料确认。')}"
    chunk_id = str(failure.get("block_id") or "")
    evidence = locator.get_context([chunk_id], before=1, after=1).get("evidence", []) if chunk_id else []
    patch = revision_tool.propose_lesson_body_patch(
        state,
        str(lesson.get("id", "")),
        new_body,
        reason=f"Semantic repair: local patch for `{failure.get('type')}` without rewriting unrelated lessons.",
        evidence=evidence,
    )
    return revision_tool.apply_patch(state, patch), patch  # type: ignore[return-value]


def _semantic_patch_wrong_caption(
    revision_tool: SourceRevisionTool,
    state: CourseCompileState,
    lesson: dict[str, Any],
    failure: dict[str, Any],
    locator: SourceLocator,
) -> tuple[CourseCompileState | None, dict[str, Any] | None]:
    lesson_id = str(lesson.get("id", ""))
    image_id = str(failure.get("image_id") or "")
    if not image_id:
        return _semantic_patch_lesson_body(revision_tool, state, lesson, failure, locator)
    images = list(lesson.get("images", []))
    pending = list(lesson.get("pending_image_confirmations", []))
    moved = None
    kept = []
    for image in images:
        if str(image.get("id") or image.get("image_id") or "") == image_id:
            moved = dict(image)
            moved["needs_confirmation"] = True
            moved["caption"] = str(failure.get("message") or moved.get("caption") or "待确认图片")
        else:
            kept.append(image)
    if moved is None:
        return _semantic_patch_lesson_body(revision_tool, state, lesson, failure, locator)
    patch = revision_tool.propose_patch(
        target={"type": "image", "lesson_id": lesson_id, "action": "move_wrong_caption_to_pending"},
        reason="Semantic repair: move image with disputed caption to pending confirmation for this lesson only.",
        evidence=locator.find_images(source_ids=[str(moved.get("source_chunk_id", ""))], limit=1),
        operations=[
            {"op": "replace_lesson_field", "lesson_id": lesson_id, "field": "images", "before": images, "after": kept},
            {"op": "replace_lesson_field", "lesson_id": lesson_id, "field": "pending_image_confirmations", "before": pending, "after": pending + [moved]},
        ],
    )
    return revision_tool.apply_patch(state, patch), patch  # type: ignore[return-value]


def _repair_grounding_with_source_tool(state: CourseCompileState) -> tuple[CourseCompileState, list[dict[str, Any]]]:
    locator = SourceLocator.from_state(state)
    revision_tool = SourceRevisionTool()
    repairs: list[dict[str, Any]] = []
    repaired_state: CourseCompileState = state

    for lesson in list(repaired_state.get("lessons", [])):
        lesson_id = str(lesson.get("id", ""))
        for source_index, source in enumerate(list(lesson.get("sources", []))):
            source_id = stable_source_id(source)
            chunk = locator.chunk_by_id.get(source_id)
            if not chunk:
                continue
            replacement = _source_ref_from_evidence(source, chunk, source_index + 1)
            if replacement == source:
                continue
            evidence_pack = locator.get_context([source_id], before=1, after=1)
            patch = revision_tool.propose_replace_citation_patch(
                repaired_state,
                lesson_id,
                source_index,
                replacement,
                reason="Repair citation provenance from SourceLocator evidence.",
                evidence=evidence_pack.get("evidence", []),
            )
            repaired_state = revision_tool.apply_patch(repaired_state, patch)  # type: ignore[assignment]
            repairs.append({"lesson_id": lesson_id, "repair": "source_tool_citation_repair", "repair_class": "mechanical", "source_id": source_id, "patch_id": patch["id"]})

        for list_name in ("images", "pending_image_confirmations"):
            current_lesson = next((item for item in repaired_state.get("lessons", []) if str(item.get("id", "")) == lesson_id), {})
            images = list(current_lesson.get(list_name, []))
            repaired_images = [_repair_image_ref_from_evidence(image, locator) for image in images]
            if repaired_images == images:
                continue
            patch = revision_tool.propose_patch(
                target={"type": "image", "lesson_id": lesson_id, "field": list_name, "action": "repair_image_evidence"},
                reason="Repair image provenance from SourceLocator image evidence.",
                evidence=[
                    evidence
                    for image in repaired_images
                    for evidence in locator.find_images(source_ids=[str(image.get("source_chunk_id", ""))], limit=1)
                ],
                operations=[
                    {
                        "op": "replace_lesson_field",
                        "lesson_id": lesson_id,
                        "field": list_name,
                        "before": images,
                        "after": repaired_images,
                    }
                ],
            )
            repaired_state = revision_tool.apply_patch(repaired_state, patch)  # type: ignore[assignment]
            repairs.append({"lesson_id": lesson_id, "repair": "source_tool_image_repair", "repair_class": "mechanical", "field": list_name, "patch_id": patch["id"]})

    return repaired_state, repairs


def _source_ref_from_evidence(source: dict[str, Any], chunk: dict[str, Any], order_fallback: int) -> dict[str, Any]:
    repaired = dict(source)
    chunk_id = str(chunk.get("id", ""))
    repaired["source_id"] = chunk_id
    repaired["chunk_id"] = chunk_id
    repaired["source"] = repaired.get("source") or chunk.get("source", "")
    repaired["source_file"] = repaired.get("source_file") or chunk.get("source_file") or chunk.get("source", "")
    repaired["block_id"] = chunk.get("block_id") or chunk_id
    chunk_page = chunk.get("page", chunk.get("page_idx"))
    if chunk_page is not None:
        repaired["page"] = chunk_page
    if chunk.get("bbox"):
        repaired["bbox"] = chunk.get("bbox", [])
    repaired["source_order"] = repaired.get("source_order") or chunk.get("source_order") or order_fallback
    quote = str(repaired.get("quote", "")).strip()
    content = str(chunk.get("content", ""))
    if not quote or quote not in content:
        repaired["quote"] = _first_meaningful_line(content, fallback=str(chunk.get("title", "")))
    return repaired


def _repair_image_ref_from_evidence(image: dict[str, Any], locator: SourceLocator) -> dict[str, Any]:
    repaired = dict(image)
    image_id = str(image.get("id") or image.get("image_id") or "")
    known_image = locator.image_by_id.get(image_id, {})
    if not known_image:
        return repaired
    repaired.setdefault("id", image_id)
    if not repaired.get("source_chunk_id") and known_image.get("source_chunk_id"):
        repaired["source_chunk_id"] = known_image.get("source_chunk_id")
    if repaired.get("bbox") in (None, [], "") and known_image.get("bbox"):
        repaired["bbox"] = known_image.get("bbox", [])
    if not repaired.get("caption") and known_image.get("caption"):
        repaired["caption"] = known_image.get("caption", "")
    if not repaired.get("asset_url") and known_image.get("asset_url"):
        repaired["asset_url"] = known_image.get("asset_url", "")
    return repaired


def human_review(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Persist a review request when automated repair cannot safely proceed."""

    review = {
        "course_id": state["course_id"],
        "reason": "Automated repair could not resolve validation failures.",
        "validation_report": state.get("validation_report", {}),
        "errors": state.get("errors", []),
    }
    write_json(course_dir(Path(vault_root), state["course_id"]) / "human_review.json", review)
    state["next_action"] = "blocked_for_human_review"
    return state


def export_version(
    state: CourseCompileState,
    vault_root: Path | str = "course-vault",
    version: str = "v1",
) -> CourseCompileState:
    """Export validated lessons as versioned Markdown files."""

    if not state.get("validation_report", {}).get("ok"):
        state["errors"].append({"node": "export_version", "message": "Cannot export invalid course"})
        state["next_action"] = "repair_course"
        return state

    target_dir = course_dir(Path(vault_root), state["course_id"])
    lessons_dir = ensure_dir(target_dir / "versions" / version / "lessons")
    for stale_lesson in lessons_dir.glob("*.md"):
        stale_lesson.unlink()
    for lesson in state["lessons"]:
        filename = f"{lesson['order']:03d}-{slugify(lesson['title'])}.md"
        (lessons_dir / filename).write_text(_render_lesson_markdown(lesson), encoding="utf-8")

    image_summary = state.get("image_understanding", {}).get("summary", {})
    course_meta = {
        "course_id": state["course_id"],
        "version": version,
        "source_files": state["source_files"],
        "lesson_count": len(state["lessons"]),
        "image_understanding": image_summary,
    }
    write_json(target_dir / "course_meta.json", course_meta)
    write_json(
        target_dir / "versions" / version / "version_record.json",
        {
            "course_id": state["course_id"],
            "version": version,
            "lesson_count": len(state["lessons"]),
            "image_understanding": image_summary,
            "image_artifact": "image_understanding.json",
        },
    )
    write_json(target_dir / "compile_profile.json", state["compile_profile"])
    write_json(target_dir / "feedback_log.json", [])
    write_json(target_dir / "compile_patches.json", state["compile_patches"])
    for stale_name in ("human_review.json", "repair_report.json"):
        stale_path = target_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()
    state["next_action"] = "done"
    return state


def _render_lesson_markdown(lesson: dict[str, Any]) -> str:
    source_lines = [
        f"- `{source['source']}` / `{source['chunk_id']}`: "
        f"[source_file={source.get('source_file', source.get('source', ''))}; "
        f"source_id={source.get('source_id', source.get('chunk_id', ''))}; "
        f"page={source.get('page')}; block_id={source.get('block_id', source.get('chunk_id', ''))}; "
        f"bbox={source.get('bbox', [])}; source_order={source.get('source_order')}] "
        f"{source['quote']}"
        for source in lesson["sources"]
    ]
    checklist_lines = [f"- [ ] {item}" for item in lesson["checklist"]]
    figure_section = _render_lesson_figures(lesson.get("images", []))
    pending_section = _render_pending_image_confirmations(lesson.get("pending_image_confirmations", []))
    return (
        f"# {lesson['title']}\n\n"
        f"{lesson['body']}\n\n"
        f"{figure_section}"
        "## Checklist\n\n"
        f"{chr(10).join(checklist_lines)}\n\n"
        "## Sources\n\n"
        f"{chr(10).join(source_lines)}\n"
        f"{pending_section}"
    )


def _render_lesson_figures(images: list[dict[str, Any]]) -> str:
    if not images:
        return ""
    lines = ["## Figures", ""]
    for image in images:
        alt = _markdown_image_alt(str(image.get("caption") or image.get("image_type") or "Source image").strip())
        url = str(image.get("asset_url") or "").strip()
        formula = image.get("formula_recognition", {}) if isinstance(image.get("formula_recognition", {}), dict) else {}
        formula_markdown = str(formula.get("markdown", "")).strip()
        formula_ready = formula_markdown and not formula.get("needs_human_review")
        preserve_image = bool(image.get("preserve_original_image", True))
        if formula_ready:
            lines.extend(
                [
                    f"### {alt}",
                    "",
                    formula_markdown,
                    "",
                    f"*Formula role: {formula.get('formula_role', 'unknown')}; confidence: {formula.get('confidence', '')}*",
                    "",
                ]
            )
        if not url or (formula_ready and not preserve_image):
            continue
        lines.extend(
            [
                f"![{alt}]({url})",
                "",
                f"*{alt}*",
                "",
                str(image.get("summary", "")).strip(),
                "",
            ]
        )
    return "\n".join(line for line in lines if line is not None).strip() + "\n\n" if len(lines) > 2 else ""


def _render_pending_image_confirmations(images: list[dict[str, Any]]) -> str:
    if not images:
        return ""
    lines = ["\n\n## 待确认图片", ""]
    for image in images:
        url = str(image.get("asset_url") or "").strip()
        caption = _markdown_image_alt(str(image.get("caption") or image.get("id") or "待确认图片").strip())
        formula = image.get("formula_recognition", {}) if isinstance(image.get("formula_recognition", {}), dict) else {}
        if url:
            lines.extend([f"![{caption}]({url})", ""])
        lines.extend(
            [
                f"- 图片 ID: `{image.get('id', '')}`",
                f"- 原因: {image.get('summary', '当前解析结果不足，需人工确认。')}",
                f"- Source chunk: `{image.get('source_chunk_id', '')}`",
                f"- 图片类型: `{image.get('image_type', '')}`",
                "",
            ]
        )
        if formula:
            lines.extend(
                [
                    "- 公式识别状态: 待人工审核" if formula.get("needs_human_review") else "- 公式识别状态: 已识别",
                    f"- 公式角色: `{formula.get('formula_role', 'unknown')}`",
                    f"- 识别置信度: `{formula.get('confidence', '')}`",
                    f"- 保留原图: `{formula.get('preserve_original_image', True)}`",
                    "",
                    "```latex",
                    str(formula.get("latex", "")),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _markdown_image_alt(value: str) -> str:
    text = _short_text(" ".join(value.split()), 140)
    return text.replace("[", "(").replace("]", ")").replace("(", "（").replace(")", "）")
