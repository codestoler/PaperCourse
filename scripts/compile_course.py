#!/usr/bin/env python3
"""Compile Markdown source files into a local course version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_graph.compiler import compile_course


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="Markdown source files to compile")
    parser.add_argument("--course-id", required=True, help="Stable course identifier")
    parser.add_argument("--vault-root", default="course-vault", help="Output vault directory")
    parser.add_argument("--version", default="v1", help="Version directory name")
    parser.add_argument(
        "--course-style",
        default="standard",
        choices=["standard", "learn-by-doing", "learn_by_doing"],
        help="Course organization style; learn-by-doing turns software manuals into task-first tutorials",
    )
    parser.add_argument("--use-llm", action="store_true", help="Use the configured LLM to plan course hierarchy")
    parser.add_argument("--use-llm-structure", action="store_true", help="Use LLM-first unit extraction, logic organization, gap detection, and lesson drafting")
    parser.add_argument("--use-source-index", action="store_true", help="Build batched source context packs before planning")
    parser.add_argument("--use-llm-source-index", action="store_true", help="Use the configured LLM to build source context packs")
    parser.add_argument("--use-source-brief", action="store_true", help="Build a source teaching brief before planning")
    parser.add_argument("--use-llm-brief", action="store_true", help="Use the configured LLM to build the source teaching brief")
    parser.add_argument("--use-source-index-plan", action="store_true", help="Plan directly from source-index packs without an LLM plan call")
    parser.add_argument("--use-lesson-notes", action="store_true", help="Build per-lesson teaching notes after planning")
    parser.add_argument("--use-llm-lesson-notes", action="store_true", help="Use the configured LLM for per-lesson teaching notes")
    parser.add_argument("--use-llm-lesson-bodies", action="store_true", help="Use the configured LLM to write full per-lesson study bodies")
    parser.add_argument("--use-llm-validation", action="store_true", help="Use the configured LLM for grounding and quality validation gates")
    parser.add_argument("--use-vision-image-understanding", action="store_true", help="Use the configured vision MCP server to refine selected image records")
    parser.add_argument("--use-formula-image-recognition", action="store_true", help="Use the configured vision MCP server to convert formula images into editable Markdown/LaTeX")
    parser.add_argument("--image-vision-mode", default="uncertain", choices=["uncertain", "all"], help="Which images should be sent to the vision model")
    parser.add_argument("--image-vision-max-images", type=int, default=12, help="Maximum image crops sent to the vision model in one compile")
    parser.add_argument("--formula-image-max-images", type=int, default=12, help="Maximum formula image crops sent to the formula recognition agent in one compile")
    parser.add_argument("--detailed-lessons", action="store_true", help="Export fuller study notes instead of compact lesson summaries")
    parser.add_argument("--max-llm-chunks", type=int, default=90, help="Maximum parsed chunks sent to the LLM planner")
    parser.add_argument("--source-brief-index-chars", type=int, default=18000)
    parser.add_argument("--course-plan-index-chars", type=int, default=20000)
    parser.add_argument("--target-lesson-count", type=int, default=0)
    parser.add_argument("--source-index-batch-chunks", type=int, default=32)
    parser.add_argument("--lesson-note-batch-lessons", type=int, default=4)
    parser.add_argument("--lesson-body-max-chars", type=int, default=7200)
    parser.add_argument("--lesson-body-target-chars", type=int, default=3500)
    parser.add_argument("--lesson-body-chunk-chars", type=int, default=1200)
    parser.add_argument(
        "--lesson-body-enrichment",
        default="standard",
        choices=["standard", "constrained"],
        help="Enable bounded local LLM enrichment for skipped steps, proof bridges, questions, and pitfalls",
    )
    parser.add_argument("--lesson-body-start", type=int, default=1, help="1-based first lesson allowed to make a new LLM body call")
    parser.add_argument("--lesson-body-end", type=int, default=10**9, help="1-based last lesson allowed to make a new LLM body call")
    parser.add_argument("--refresh-source-index", action="store_true", help="Ignore the local source-index cache")
    parser.add_argument("--refresh-source-brief", action="store_true", help="Ignore the local source-brief cache")
    parser.add_argument("--refresh-llm-plan", action="store_true", help="Ignore the local LLM plan cache and call the provider")
    parser.add_argument("--refresh-lesson-notes", action="store_true", help="Ignore the local lesson-note cache")
    parser.add_argument("--refresh-lesson-bodies", action="store_true", help="Ignore the local lesson-body cache")
    parser.add_argument("--refresh-image-vision", action="store_true", help="Ignore cached per-image vision analysis")
    parser.add_argument("--refresh-formula-image-recognition", action="store_true", help="Ignore cached per-formula image recognition")
    parser.add_argument("--progress-jsonl", default="", help="Optional JSONL file that receives compile progress events")
    parser.add_argument("--compile-context", default="", help="Confirmed project compile context JSON produced by the local backend")
    parser.add_argument("--llm-timeout", type=int, default=0, help="Override LLM request timeout in seconds")
    parser.add_argument("--llm-connect-timeout", type=int, default=0, help="Override LLM TCP connect timeout in seconds")
    parser.add_argument("--llm-retries", type=int, default=-1, help="Override LLM retry count for transient failures")
    parser.add_argument("--llm-retry-backoff", type=float, default=-1.0, help="Override LLM retry backoff base in seconds")
    args = parser.parse_args()

    if args.llm_timeout > 0:
        os.environ["LLM_TIMEOUT"] = str(args.llm_timeout)
    if args.llm_connect_timeout > 0:
        os.environ["LLM_CONNECT_TIMEOUT"] = str(args.llm_connect_timeout)
    if args.llm_retries >= 0:
        os.environ["LLM_RETRIES"] = str(args.llm_retries)
    if args.llm_retry_backoff >= 0:
        os.environ["LLM_RETRY_BACKOFF_SECONDS"] = str(args.llm_retry_backoff)

    profile = {
        "use_source_index": args.use_source_index,
        "use_llm_source_index": args.use_llm_source_index,
        "use_source_brief": args.use_source_brief,
        "use_llm_brief": args.use_llm_brief,
        "use_source_index_plan": args.use_source_index_plan,
        "use_lesson_notes": args.use_lesson_notes,
        "use_llm_lesson_notes": args.use_llm_lesson_notes,
        "use_llm_lesson_bodies": args.use_llm_lesson_bodies,
        "use_llm_validation": args.use_llm_validation,
        "use_vision_image_understanding": args.use_vision_image_understanding,
        "use_formula_image_recognition": args.use_formula_image_recognition,
        "image_vision_mode": args.image_vision_mode,
        "image_vision_max_images": args.image_vision_max_images,
        "formula_image_max_images": args.formula_image_max_images,
        "use_llm": args.use_llm,
        "use_llm_structure": args.use_llm_structure or args.use_llm,
        "detailed_lessons": args.detailed_lessons,
        "max_llm_chunks": args.max_llm_chunks,
        "source_brief_index_chars": args.source_brief_index_chars,
        "course_plan_index_chars": args.course_plan_index_chars,
        "target_lesson_count": args.target_lesson_count,
        "source_index_batch_chunks": args.source_index_batch_chunks,
        "lesson_note_batch_lessons": args.lesson_note_batch_lessons,
        "lesson_body_max_chars": args.lesson_body_max_chars,
        "lesson_body_target_chars": args.lesson_body_target_chars,
        "lesson_body_chunk_chars": args.lesson_body_chunk_chars,
        "lesson_body_enrichment": args.lesson_body_enrichment,
        "lesson_body_start": args.lesson_body_start,
        "lesson_body_end": args.lesson_body_end,
        "refresh_source_index": args.refresh_source_index,
        "refresh_source_brief": args.refresh_source_brief,
        "refresh_llm_plan": args.refresh_llm_plan,
        "refresh_lesson_notes": args.refresh_lesson_notes,
        "refresh_lesson_bodies": args.refresh_lesson_bodies,
        "refresh_image_vision": args.refresh_image_vision,
        "refresh_formula_image_recognition": args.refresh_formula_image_recognition,
        "progress_jsonl": args.progress_jsonl,
        "course_style": args.course_style,
    }
    if args.compile_context:
        context_path = Path(args.compile_context)
        context = json.loads(context_path.read_text(encoding="utf-8"))
        snapshot = context.get("confirmed_compile_snapshot", {}) if isinstance(context, dict) else {}
        scheme = snapshot.get("selected_scheme", {}) if isinstance(snapshot, dict) else {}
        overrides = scheme.get("compile_profile_overrides", {}) if isinstance(scheme, dict) else {}
        for key, value in overrides.items():
            if key in profile and value not in (None, ""):
                profile[key] = value
        profile["project_compile_context_path"] = str(context_path)
        profile["confirmed_library_file_ids"] = list(snapshot.get("library_file_ids", [])) if isinstance(snapshot, dict) else []
        profile["user_compile_requirements"] = dict(snapshot.get("compile_requirements", {})) if isinstance(snapshot, dict) else {}
        profile["review_feedback"] = list(context.get("review_feedback", [])) if isinstance(context, dict) else []
        profile["selected_compile_scheme"] = scheme
        profile["confirmed_preflight_plan_id"] = str(snapshot.get("plan_id", "")) if isinstance(snapshot, dict) else ""
        profile["confirmed_preflight_plan_signature"] = str(snapshot.get("plan_signature", "")) if isinstance(snapshot, dict) else ""
    state = compile_course(args.sources, args.course_id, args.vault_root, args.version, profile=profile)
    course_path = Path(args.vault_root) / "courses" / args.course_id
    print(f"status={state['next_action']}")
    print(f"lessons={len(state['lessons'])}")
    print(f"validation_ok={state['validation_report'].get('ok')}")
    print(f"course_path={course_path}")
    if state["errors"]:
        print(f"errors={state['errors']}")
        return 1
    return 0 if state["next_action"] == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
