"""State and data contracts for the course compiler graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict


ContentType = Literal[
    "source_supported",
    "inferred_from_source",
    "bridge",
    "needs_confirmation",
]


class CourseCompileState(TypedDict):
    course_id: str
    source_files: list[str]
    parsed_chunks: list[dict[str, Any]]
    units: list[dict[str, Any]]
    logic_graph: dict[str, Any]
    gap_report: dict[str, Any]
    outline: dict[str, Any]
    concepts: list[dict[str, Any]]
    lessons: list[dict[str, Any]]
    lesson_bodies: dict[str, Any]
    lesson_body_inputs: dict[str, Any]
    lesson_body_revision_request: dict[str, Any]
    markdown_syntax_report: dict[str, Any]
    markdown_repair_audit: dict[str, Any]
    compile_plan: dict[str, Any]
    compile_plan_review: dict[str, Any]
    compile_plan_revisions: list[dict[str, Any]]
    image_understanding: dict[str, Any]
    source_index: dict[str, Any]
    lesson_evidence: dict[str, Any]
    source_brief: dict[str, Any]
    lesson_notes: dict[str, Any]
    course_plan: dict[str, Any]
    compile_profile: dict[str, Any]
    compile_patches: list[dict[str, Any]]
    repair_report: dict[str, Any]
    validation_report: dict[str, Any]
    next_action: str
    errors: list[dict[str, Any]]
    graph_run_log: list[dict[str, Any]]
    internal_run_log: list[dict[str, Any]]


@dataclass(frozen=True)
class CompilePaths:
    """Filesystem layout for one compile run."""

    vault_root: str = "course-vault"
    raw_dir: str = "course-vault/raw"
    parsed_dir: str = "course-vault/parsed"
    courses_dir: str = "course-vault/courses"


@dataclass
class CompileConfig:
    """Small, local-first compile configuration."""

    course_id: str
    source_files: list[str]
    version: str = "v1"
    profile: dict[str, Any] = field(default_factory=dict)


def initial_state(config: CompileConfig) -> CourseCompileState:
    """Create the graph state with every expected key initialized."""

    return {
        "course_id": config.course_id,
        "source_files": list(config.source_files),
        "parsed_chunks": [],
        "units": [],
        "logic_graph": {},
        "gap_report": {"items": []},
        "outline": {"lessons": []},
        "concepts": [],
        "lessons": [],
        "lesson_bodies": {},
        "lesson_body_inputs": {},
        "lesson_body_revision_request": {},
        "markdown_syntax_report": {},
        "markdown_repair_audit": {},
        "compile_plan": {},
        "compile_plan_review": {},
        "compile_plan_revisions": [],
        "image_understanding": {},
        "source_index": {},
        "lesson_evidence": {},
        "source_brief": {},
        "lesson_notes": {},
        "course_plan": {},
        "compile_profile": dict(config.profile),
        "compile_patches": [],
        "repair_report": {},
        "validation_report": {},
        "next_action": "parse_sources",
        "errors": [],
        "graph_run_log": [],
        "internal_run_log": [],
    }
