"""Local compile runner for the AI Course Compiler graph."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .io import course_dir, write_json
from .nodes import (
    build_source_index,
    check_grounding,
    check_quality,
    detect_gaps,
    export_version,
    extract_units,
    generate_lessons,
    human_review,
    organize_logic,
    plan_course,
    parse_sources,
    repair_course,
    synthesize_lesson_bodies,
    synthesize_lesson_notes,
    synthesize_source_brief,
)
from .state import CompileConfig, CourseCompileState, initial_state

try:
    from langgraph.graph import END, StateGraph
except ImportError as exc:  # pragma: no cover - exercised only outside the project venv
    END = None
    StateGraph = None
    LANGGRAPH_IMPORT_ERROR = exc
else:
    LANGGRAPH_IMPORT_ERROR = None


def compile_course(
    source_files: list[str],
    course_id: str,
    vault_root: str | Path = "course-vault",
    version: str = "v1",
    profile: dict | None = None,
) -> CourseCompileState:
    """Run the local compiler through a real LangGraph state graph."""

    if StateGraph is None or END is None:
        raise RuntimeError("LangGraph is required. Install dependencies with `.venv/bin/python -m pip install langgraph`.") from LANGGRAPH_IMPORT_ERROR

    state = initial_state(CompileConfig(course_id=course_id, source_files=source_files, version=version, profile=profile or {}))
    graph = build_compile_graph(vault_root, version)
    final_state = graph.invoke(state)
    write_json(course_dir(Path(vault_root), final_state["course_id"]) / "graph_run_log.json", final_state["graph_run_log"])
    return final_state


def build_compile_graph(vault_root: str | Path = "course-vault", version: str = "v1"):
    """Build the LangGraph compiler topology."""

    if StateGraph is None or END is None:
        raise RuntimeError("LangGraph is required. Install dependencies with `.venv/bin/python -m pip install langgraph`.") from LANGGRAPH_IMPORT_ERROR

    graph = StateGraph(CourseCompileState)
    graph.add_node("parse_sources", _wrap_node("parse_sources", parse_sources, vault_root))
    graph.add_node("build_source_index", _wrap_node("build_source_index", build_source_index, vault_root))
    graph.add_node("synthesize_source_brief", _wrap_node("synthesize_source_brief", synthesize_source_brief, vault_root))
    graph.add_node("plan_course", _wrap_node("plan_course", plan_course, vault_root))
    graph.add_node("synthesize_lesson_notes", _wrap_node("synthesize_lesson_notes", synthesize_lesson_notes, vault_root))
    graph.add_node("extract_units", _wrap_node("extract_units", extract_units, vault_root))
    graph.add_node("organize_logic", _wrap_node("organize_logic", organize_logic, vault_root))
    graph.add_node("detect_gaps", _wrap_node("detect_gaps", detect_gaps, vault_root))
    graph.add_node("generate_lessons", _wrap_node("generate_lessons", generate_lessons, vault_root))
    graph.add_node("synthesize_lesson_bodies", _wrap_node("synthesize_lesson_bodies", synthesize_lesson_bodies, vault_root))
    graph.add_node("check_grounding", _wrap_node("check_grounding", check_grounding, vault_root))
    graph.add_node("check_quality", _wrap_node("check_quality", check_quality, vault_root))
    graph.add_node("repair_course", _wrap_node("repair_course", repair_course, vault_root))
    graph.add_node("human_review", _wrap_node("human_review", human_review, vault_root))
    graph.add_node("export_version", _wrap_export(vault_root, version))

    graph.set_entry_point("parse_sources")
    graph.add_edge("parse_sources", "build_source_index")
    graph.add_edge("build_source_index", "synthesize_source_brief")
    graph.add_edge("synthesize_source_brief", "plan_course")
    graph.add_edge("plan_course", "synthesize_lesson_notes")
    graph.add_edge("synthesize_lesson_notes", "extract_units")
    graph.add_edge("extract_units", "organize_logic")
    graph.add_edge("organize_logic", "detect_gaps")
    graph.add_edge("detect_gaps", "generate_lessons")
    graph.add_edge("generate_lessons", "synthesize_lesson_bodies")
    graph.add_edge("synthesize_lesson_bodies", "check_grounding")
    graph.add_edge("check_grounding", "check_quality")
    graph.add_conditional_edges(
        "check_quality",
        _route_after_validation,
        {"export_version": "export_version", "repair_course": "repair_course"},
    )
    graph.add_conditional_edges(
        "repair_course",
        _route_after_repair,
        {"check_grounding": "check_grounding", "human_review": "human_review"},
    )
    graph.add_edge("export_version", END)
    graph.add_edge("human_review", END)
    return graph.compile()


def _run_node(
    name: str,
    node: Callable[[CourseCompileState, Path | str], CourseCompileState],
    state: CourseCompileState,
    vault_root: str | Path,
    run_log: list[dict[str, object]],
) -> CourseCompileState:
    state = node(state, vault_root)
    run_log.append({"node": name, "next_action": state["next_action"], "error_count": len(state["errors"])})
    return state


def _wrap_node(
    name: str,
    node: Callable[[CourseCompileState, Path | str], CourseCompileState],
    vault_root: str | Path,
):
    def wrapped(state: CourseCompileState) -> CourseCompileState:
        next_state = node(state, vault_root)
        next_state["graph_run_log"].append(
            {"node": name, "next_action": next_state["next_action"], "error_count": len(next_state["errors"])}
        )
        return next_state

    return wrapped


def _wrap_export(vault_root: str | Path, version: str):
    def wrapped(state: CourseCompileState) -> CourseCompileState:
        next_state = export_version(state, vault_root, version)
        next_state["graph_run_log"].append(
            {"node": "export_version", "next_action": next_state["next_action"], "error_count": len(next_state["errors"])}
        )
        return next_state

    return wrapped


def _route_after_validation(state: CourseCompileState) -> str:
    return "export_version" if state["next_action"] == "export_version" else "repair_course"


def _route_after_repair(state: CourseCompileState) -> str:
    return "check_grounding" if state["next_action"] == "check_grounding" else "human_review"
