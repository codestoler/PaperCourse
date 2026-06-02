"""Graph node implementations for the local-first compiler."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .io import course_dir, ensure_dir, read_json, slugify, write_json
from .llm import LLMClient
from .state import CourseCompileState


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
        chunks.extend(source_chunks)
        write_json(compile_dir / f"{slugify(source_path.stem)}.json", source_chunks)

    state["parsed_chunks"] = chunks
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
                    "title": current_title,
                    "content": content,
                    "start_line": start_line,
                    "end_line": end_line,
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


def extract_units(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Turn parsed chunks into coarse knowledge units."""

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
                "content_type": content_type,
                "source_quote": source_refs[0]["quote"],
                "order": index,
            }
        )

    state["units"] = units
    write_json(course_dir(Path(vault_root), state["course_id"]) / "units.json", units)
    state["next_action"] = "organize_logic"
    return state


def _source_refs_for_group(group: list[dict[str, Any]], max_refs: int = 6) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()

    def add(chunk: dict[str, Any]) -> None:
        chunk_id = str(chunk["id"])
        if chunk_id in seen_ids or len(selected) >= max_refs:
            return
        selected.append(
            {
                "source": chunk["source"],
                "chunk_id": chunk["id"],
                "quote": _first_meaningful_line(str(chunk["content"]), fallback=str(chunk["title"])),
            }
        )
        seen_ids.add(chunk_id)
        seen_sources.add(str(chunk.get("source", "")))

    for chunk in group:
        source = str(chunk.get("source", ""))
        if source not in seen_sources:
            add(chunk)
    for chunk in group:
        add(chunk)
    return selected


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


def organize_logic(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Build a minimal prerequisite graph from unit order."""

    nodes = [
        {
            "id": unit["id"],
            "title": unit["title"],
            "content_type": unit["content_type"],
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
    logic_graph = {"nodes": nodes, "edges": edges}

    state["logic_graph"] = logic_graph
    write_json(course_dir(Path(vault_root), state["course_id"]) / "logic_graph.json", logic_graph)
    state["next_action"] = "detect_gaps"
    return state


def detect_gaps(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Create a deterministic report of structural learning gaps."""

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

    gap_report = {
        "ok": not any(item["severity"] == "high" for item in items),
        "items": items,
        "summary": {
            "total": len(items),
            "high": sum(1 for item in items if item["severity"] == "high"),
            "medium": sum(1 for item in items if item["severity"] == "medium"),
            "low": sum(1 for item in items if item["severity"] == "low"),
        },
    }
    state["gap_report"] = gap_report
    write_json(course_dir(Path(vault_root), state["course_id"]) / "gap_report.json", gap_report)
    state["next_action"] = "generate_lessons"
    return state


def generate_lessons(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Create short lesson records from source-supported units."""

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
                        "chunk_id": ref["chunk_id"],
                        "quote": ref["quote"],
                    }
                    for ref in unit.get("source_refs", [])
                ]
                or [
                    {
                        "source": unit["source"],
                        "chunk_id": unit["source_chunk_id"],
                        "quote": unit["source_quote"],
                    }
                ],
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
    state["lessons"] = lessons
    state["concepts"] = concepts
    state["outline"] = outline
    target_dir = course_dir(Path(vault_root), state["course_id"])
    write_json(target_dir / "outline.json", outline)
    write_json(target_dir / "concepts.json", concepts)
    write_json(target_dir / "lessons.json", lessons)
    state["next_action"] = "synthesize_lesson_bodies"
    return state


def synthesize_lesson_bodies(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Optionally replace draft lesson bodies with LLM-written study notes using local lesson chunks."""

    profile = state.get("compile_profile", {})
    learn_by_doing = _is_learn_by_doing(profile)
    if not profile.get("use_llm_lesson_bodies"):
        state["lesson_bodies"] = {}
        state["next_action"] = "check_grounding"
        return state

    client = LLMClient.from_env()
    if client is None:
        state["errors"].append({"node": "synthesize_lesson_bodies", "message": "LLM configuration is missing; keeping draft lesson bodies"})
        state["lesson_bodies"] = {}
        state["next_action"] = "check_grounding"
        return state

    course_path = course_dir(Path(vault_root), state["course_id"])
    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    unit_by_id = {unit["id"]: unit for unit in state["units"]}
    lesson_bodies: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    skipped = 0
    start_order = int(profile.get("lesson_body_start", 1))
    end_order = int(profile.get("lesson_body_end", len(state["lessons"])))

    for lesson_index, lesson in enumerate(state["lessons"], start=1):
        units = [unit_by_id[unit_id] for unit_id in lesson.get("unit_ids", []) if unit_id in unit_by_id]
        source_chunk_ids: list[str] = []
        for unit in units:
            source_chunk_ids.extend(str(chunk_id) for chunk_id in unit.get("source_chunk_ids", []) if str(chunk_id) in chunk_by_id)
        source_chunk_ids = _dedupe_keep_order(source_chunk_ids)
        if not source_chunk_ids:
            continue

        system = (
            "You are a course writer. Convert one source-grounded lesson plan into a detailed, readable study lesson. "
            "Use only the provided local source chunks. Return strict JSON only."
        )
        if learn_by_doing:
            system += " In learn-by-doing mode, write task-first software tutorial lessons."
        user = (
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
            f"- Target length: {int(profile.get('lesson_body_target_chars', 3500))}-{int(profile.get('lesson_body_max_chars', 7200))} Chinese characters when source content is sufficient.\n\n"
            f"{_learn_by_doing_body_requirements() if learn_by_doing else ''}"
            f"{_lesson_body_enrichment_requirements(profile)}"
            f"Lesson id: {lesson['id']}\n"
            f"Lesson title: {lesson['title']}\n"
            f"Lesson type: {lesson.get('lesson_type', '')}\n"
            f"Section: {lesson.get('section_title', '')}\n\n"
            f"Draft lesson body:\n{str(lesson.get('body', ''))[:4000]}\n\n"
            f"Source chunks:\n{_lesson_chunks_for_prompt(source_chunk_ids, chunk_by_id, max_chars=int(profile.get('lesson_body_chunk_chars', 1200)))}"
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
            lesson["body"] = body_record["body_markdown"]
            if body_record.get("checklist"):
                lesson["checklist"] = body_record["checklist"]
            lesson_bodies.append(body_record)

    state["lesson_bodies"] = {"lesson_bodies": lesson_bodies}
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
            "metadata": metadata,
        },
    )
    write_json(target_dir / "lessons.json", state["lessons"])
    state["next_action"] = "check_grounding"
    return state


def _lesson_body(unit: dict[str, Any]) -> str:
    section = f"Section: {unit['section_title']}\n\n" if unit.get("section_title") else ""
    notes = unit.get("teaching_notes", {})
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


def check_grounding(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Validate that lesson quotes are backed by parsed source chunks."""

    chunk_by_id = {chunk["id"]: chunk for chunk in state["parsed_chunks"]}
    failures: list[dict[str, Any]] = []

    for lesson in state["lessons"]:
        if not lesson.get("sources"):
            failures.append({"lesson_id": lesson["id"], "message": "Lesson has no sources"})
            continue
        for source in lesson["sources"]:
            chunk = chunk_by_id.get(source.get("chunk_id"))
            quote = str(source.get("quote", "")).strip()
            if not chunk:
                failures.append({"lesson_id": lesson["id"], "message": "Missing source chunk"})
            elif not quote or quote not in str(chunk.get("content", "")):
                failures.append({"lesson_id": lesson["id"], "message": "Source quote not found in chunk"})

    validation_report = {
        "ok": not failures,
        "checks": ["sources_present", "quotes_match_parsed_chunks"],
        "failures": failures,
    }
    state["validation_report"] = validation_report
    write_json(course_dir(Path(vault_root), state["course_id"]) / "validation_report.json", validation_report)
    state["next_action"] = "export_version" if validation_report["ok"] else "repair_course"
    return state


def check_quality(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Validate lesson schemas and mobile readability constraints."""

    failures: list[dict[str, Any]] = []
    required_fields = {"id", "title", "unit_ids", "body", "checklist", "sources", "order"}
    for lesson in state["lessons"]:
        missing = sorted(required_fields - set(lesson))
        if missing:
            failures.append({"lesson_id": lesson.get("id", "unknown"), "type": "schema", "message": f"Missing fields: {missing}"})
        body = str(lesson.get("body", ""))
        body_limit = int(lesson.get("body_max_chars", 1200))
        if len(body) > body_limit:
            failures.append({"lesson_id": lesson.get("id", "unknown"), "type": "readability", "message": f"Lesson body exceeds {body_limit} chars"})
        if not lesson.get("checklist"):
            failures.append({"lesson_id": lesson.get("id", "unknown"), "type": "readability", "message": "Lesson has no checklist"})

    previous = state.get("validation_report", {})
    grounding_failures = list(previous.get("failures", []))
    validation_report = {
        "ok": previous.get("ok", True) and not failures,
        "checks": sorted(set(previous.get("checks", [])) | {"lesson_schema", "mobile_readability"}),
        "failures": grounding_failures + failures,
    }
    state["validation_report"] = validation_report
    write_json(course_dir(Path(vault_root), state["course_id"]) / "validation_report.json", validation_report)
    state["next_action"] = "export_version" if validation_report["ok"] else "repair_course"
    return state


def repair_course(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Apply safe local repairs before human review is needed."""

    repairs: list[dict[str, Any]] = []
    for lesson in state["lessons"]:
        if "checklist" not in lesson or not lesson["checklist"]:
            lesson["checklist"] = [f"复习：{lesson.get('title', 'Untitled')}"]
            repairs.append({"lesson_id": lesson.get("id"), "repair": "added_default_checklist"})
        body_limit = int(lesson.get("body_max_chars", 1200))
        if "body" in lesson and len(str(lesson["body"])) > body_limit:
            lesson["body"] = str(lesson["body"])[: max(0, body_limit - 50)].rstrip() + "\n\n[needs_confirmation] 内容已截断，等待人工复核。"
            repairs.append({"lesson_id": lesson.get("id"), "repair": "trimmed_long_body"})

    repair_report = {"repairs": repairs, "requires_human_review": not repairs}
    write_json(course_dir(Path(vault_root), state["course_id"]) / "repair_report.json", repair_report)
    state["next_action"] = "human_review" if repair_report["requires_human_review"] else "check_grounding"
    return state


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

    course_meta = {
        "course_id": state["course_id"],
        "version": version,
        "source_files": state["source_files"],
        "lesson_count": len(state["lessons"]),
    }
    write_json(target_dir / "course_meta.json", course_meta)
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
        f"- `{source['source']}` / `{source['chunk_id']}`: {source['quote']}"
        for source in lesson["sources"]
    ]
    checklist_lines = [f"- [ ] {item}" for item in lesson["checklist"]]
    return (
        f"# {lesson['title']}\n\n"
        f"{lesson['body']}\n\n"
        "## Checklist\n\n"
        f"{chr(10).join(checklist_lines)}\n\n"
        "## Sources\n\n"
        f"{chr(10).join(source_lines)}\n"
    )
