#!/usr/bin/env python3
"""Compile a course from fused MinerU and LVM parsed source directories."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_graph.compiler import compile_course


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="Parsed source directories or Markdown files, e.g. MinerU dir plus LVM dir")
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--vault-root", default="course-vault")
    parser.add_argument("--version", default="v1")
    parser.add_argument(
        "--course-style",
        default="standard",
        choices=["standard", "learn-by-doing", "learn_by_doing"],
        help="Course organization style; learn-by-doing turns software manuals into task-first tutorials",
    )
    parser.add_argument("--use-llm", action="store_true", help="Use LLM for course plan")
    parser.add_argument("--use-llm-source-index", action="store_true", help="Use LLM to build batched source context packs")
    parser.add_argument("--use-llm-brief", action="store_true", help="Use LLM to synthesize the source teaching brief")
    parser.add_argument("--use-source-index-plan", action="store_true", help="Plan directly from source-index packs without an LLM plan call")
    parser.add_argument("--use-llm-lesson-notes", action="store_true", help="Use LLM for per-lesson teaching notes")
    parser.add_argument("--use-llm-lesson-bodies", action="store_true", help="Use LLM to write full per-lesson study bodies")
    parser.add_argument("--detailed-lessons", action="store_true", default=True, help="Export detailed study lessons")
    parser.add_argument("--no-source-index", action="store_true", help="Disable source-index packs and plan directly from raw chunk digest")
    parser.add_argument("--max-brief-chunks", type=int, default=140)
    parser.add_argument("--max-llm-chunks", type=int, default=120)
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
    parser.add_argument("--refresh-source-index", action="store_true")
    parser.add_argument("--refresh-source-brief", action="store_true")
    parser.add_argument("--refresh-llm-plan", action="store_true")
    parser.add_argument("--refresh-lesson-notes", action="store_true")
    parser.add_argument("--refresh-lesson-bodies", action="store_true")
    args = parser.parse_args()

    profile = {
        "use_source_index": not args.no_source_index,
        "use_llm_source_index": args.use_llm_source_index,
        "use_source_brief": True,
        "use_lesson_notes": True,
        "use_llm_brief": args.use_llm_brief,
        "use_source_index_plan": args.use_source_index_plan,
        "use_llm_lesson_notes": args.use_llm_lesson_notes,
        "use_llm_lesson_bodies": args.use_llm_lesson_bodies,
        "use_llm": args.use_llm,
        "detailed_lessons": args.detailed_lessons,
        "max_brief_chunks": args.max_brief_chunks,
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
        "course_style": args.course_style,
    }
    state = compile_course(args.sources, args.course_id, args.vault_root, args.version, profile=profile)
    print(f"status={state['next_action']}")
    print(f"lessons={len(state['lessons'])}")
    print(f"validation_ok={state['validation_report'].get('ok')}")
    print(f"source_index_packs={len(state.get('source_index', {}).get('packs', []))}")
    print(f"source_brief_notes={len(state.get('source_brief', {}).get('lesson_notes', []))}")
    print(f"lesson_notes={len(state.get('lesson_notes', {}).get('lesson_notes', []))}")
    print(f"lesson_bodies={len(state.get('lesson_bodies', {}).get('lesson_bodies', []))}")
    if state["errors"]:
        print(f"errors={state['errors']}")
        return 1
    return 0 if state["next_action"] == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
