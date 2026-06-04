"""Local compile runner for the AI Course Compiler graph."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .io import course_dir, write_json
from .runtime import stage_failed, stage_finished, stage_started, write_runtime_status, utc_now_iso
from .nodes import (
    build_source_index,
    check_grounding_llm,
    check_grounding_rules,
    check_markdown_syntax,
    check_quality_llm,
    check_quality_rules,
    detect_gaps,
    export_version,
    extract_units,
    generate_lessons,
    human_review,
    organize_logic,
    plan_course,
    parse_sources,
    repair_course,
    review_compile_plan_llm,
    revise_compile_plan,
    synthesize_compile_plan,
    synthesize_lesson_bodies,
    synthesize_lesson_notes,
    synthesize_source_brief,
    understand_images,
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


@dataclass(frozen=True)
class CompileGraphNodeSpec:
    """Human-readable metadata for one compiler graph node."""

    name: str
    label: str
    node_type: str
    uses_llm: str
    uses_tools: tuple[str, ...]
    decided_by: str
    state_outputs: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class CompileGraphEdgeSpec:
    """Human-readable metadata for one compiler graph transition."""

    source: str
    target: str
    label: str = ""
    edge_type: str = "logic"
    decided_by: str = "logic_pipeline"
    condition: str = ""


COMPILE_GRAPH_NODE_SPECS: tuple[CompileGraphNodeSpec, ...] = (
    CompileGraphNodeSpec(
        "ingest_pipeline",
        "Ingest Pipeline",
        "logic_pipeline",
        "optional_subpipeline",
        (
            "filesystem",
            "markdown_parser",
            "mineru_metadata_reader",
            "source_indexer",
            "vision_subpipeline_optional",
            "formula_vision_optional",
        ),
        "deterministic_pipeline",
        ("parsed_chunks", "image_understanding", "source_index"),
        "Deterministically parse sources, normalize chunks, bind pages/images/context, and prepare bounded indexes; optional image/formula understanding stays inside this stage.",
    ),
    CompileGraphNodeSpec(
        "course_planning_loop",
        "Course Planning Loop",
        "bounded_agent_loop",
        "optional",
        ("llm_optional", "local_fallback", "source_index", "source_brief", "lesson_notes"),
        "llm_when_enabled_max_iterations",
        ("source_brief", "course_plan", "lesson_notes", "units", "logic_graph", "gap_report", "outline", "lessons"),
        "Run source brief synthesis, course planning, unit extraction, logic organization, gap detection, and lesson drafting inside a bounded loop; fallback failures route to human review.",
    ),
    CompileGraphNodeSpec(
        "compile_plan_gate",
        "Compile Plan Gate",
        "gate_loop",
        "conditional",
        ("filesystem", "json_writer", "markdown_writer", "local_validator", "llm_optional", "local_repair"),
        "review_gate_max_revisions",
        ("compile_plan", "compile_plan_review", "compile_plan_revisions", "next_action"),
        "Write the structured compile plan, review it, revise it when possible, and stop at human review when bounded review/revision cannot pass.",
    ),
    CompileGraphNodeSpec(
        "lesson_body_pipeline",
        "Lesson Body Pipeline",
        "llm_pipeline",
        "optional",
        ("filesystem", "llm_optional", "local_cache"),
        "per_lesson_pipeline",
        ("lesson_bodies", "lesson_body_inputs", "lesson_body_revision_request", "lessons", "next_action"),
        "Generate each lesson body with explicit per-lesson inputs, outputs, local checks, cache records, and targeted split requests.",
    ),
    CompileGraphNodeSpec(
        "validation_repair_loop",
        "Validation & Repair Loop",
        "gate_loop",
        "conditional",
        ("markdown_validator", "local_validator", "remark_lint_optional", "llm_optional", "local_repair"),
        "rules_first_max_iterations",
        ("markdown_syntax_report", "markdown_repair_audit", "validation_report", "compile_patches", "lessons", "next_action"),
        "Run Markdown, grounding, quality, and length checks with rules first, call LLM validators only when configured and needed, and retry localized repairs within a bounded loop.",
    ),
    CompileGraphNodeSpec(
        "export_pipeline",
        "Export Pipeline",
        "logic_pipeline",
        "no",
        ("filesystem", "markdown_writer", "json_writer"),
        "deterministic_pipeline",
        ("course_meta", "version_record", "lessons"),
        "Export Markdown, JSON, version records, and final metadata without LLM calls.",
    ),
    CompileGraphNodeSpec(
        "human_review",
        "Human Review",
        "human_gate",
        "no",
        ("manual_review_queue",),
        "human_gate",
        ("next_action",),
        "Persist a manual review request when bounded automated planning, gating, validation, or repair cannot safely continue.",
    ),
)


COMPILE_GRAPH_EDGES: tuple[CompileGraphEdgeSpec, ...] = (
    CompileGraphEdgeSpec("ingest_pipeline", "course_planning_loop", "ingest artifacts ready"),
    CompileGraphEdgeSpec("export_pipeline", "END", "done"),
    CompileGraphEdgeSpec("human_review", "END", "blocked for manual review"),
)


COMPILE_GRAPH_CONDITIONAL_EDGES: tuple[CompileGraphEdgeSpec, ...] = (
    CompileGraphEdgeSpec(
        "course_planning_loop",
        "compile_plan_gate",
        "planning ready",
        edge_type="conditional",
        decided_by="bounded_planning_loop",
        condition='next_action != "human_review"',
    ),
    CompileGraphEdgeSpec(
        "course_planning_loop",
        "human_review",
        "planning failed",
        edge_type="conditional",
        decided_by="bounded_planning_loop",
        condition='next_action == "human_review"',
    ),
    CompileGraphEdgeSpec(
        "compile_plan_gate",
        "lesson_body_pipeline",
        "review passed",
        edge_type="conditional",
        decided_by="compile_plan_gate",
        condition="compile plan review passed",
    ),
    CompileGraphEdgeSpec(
        "compile_plan_gate",
        "course_planning_loop",
        "needs replanning",
        edge_type="conditional",
        decided_by="compile_plan_gate",
        condition="compile plan revision needs lesson replanning",
    ),
    CompileGraphEdgeSpec(
        "compile_plan_gate",
        "human_review",
        "gate exhausted",
        edge_type="conditional",
        decided_by="compile_plan_gate",
        condition='next_action == "human_review"',
    ),
    CompileGraphEdgeSpec(
        "lesson_body_pipeline",
        "validation_repair_loop",
        "lesson bodies ready",
        edge_type="conditional",
        decided_by="lesson_body_pipeline",
        condition="lesson body generation completed",
    ),
    CompileGraphEdgeSpec(
        "lesson_body_pipeline",
        "compile_plan_gate",
        "needs finer split",
        edge_type="conditional",
        decided_by="lesson_body_pipeline",
        condition="lesson_body_revision_request.needs_finer_split",
    ),
    CompileGraphEdgeSpec(
        "lesson_body_pipeline",
        "human_review",
        "body generation blocked",
        edge_type="conditional",
        decided_by="lesson_body_pipeline",
        condition='next_action == "human_review"',
    ),
    CompileGraphEdgeSpec(
        "validation_repair_loop",
        "export_pipeline",
        "validated",
        edge_type="conditional",
        decided_by="validation_repair_loop",
        condition="validation_report.ok == true",
    ),
    CompileGraphEdgeSpec(
        "validation_repair_loop",
        "human_review",
        "repair exhausted",
        edge_type="conditional",
        decided_by="validation_repair_loop",
        condition='next_action == "human_review"',
    ),
)


def compile_graph_node_specs() -> tuple[CompileGraphNodeSpec, ...]:
    """Return the current compiler graph node metadata for docs/tools."""

    return COMPILE_GRAPH_NODE_SPECS


def compile_graph_edge_specs() -> tuple[CompileGraphEdgeSpec, ...]:
    """Return all current compiler graph transitions for docs/tools."""

    return COMPILE_GRAPH_EDGES + COMPILE_GRAPH_CONDITIONAL_EDGES


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
    write_json(course_dir(Path(vault_root), final_state["course_id"]) / "internal_run_log.json", final_state["internal_run_log"])
    write_runtime_status(
        course_dir(Path(vault_root), final_state["course_id"]),
        {
            "state": "done" if final_state.get("next_action") == "done" and not final_state.get("errors") else "blocked",
            "next_action": final_state.get("next_action", ""),
            "error_count": len(final_state.get("errors", [])),
            "updated_at": utc_now_iso(),
        },
    )
    return final_state


def build_compile_graph(vault_root: str | Path = "course-vault", version: str = "v1"):
    """Build the LangGraph compiler topology."""

    if StateGraph is None or END is None:
        raise RuntimeError("LangGraph is required. Install dependencies with `.venv/bin/python -m pip install langgraph`.") from LANGGRAPH_IMPORT_ERROR

    graph = StateGraph(CourseCompileState)
    graph.add_node("ingest_pipeline", _wrap_stage_node("ingest_pipeline", ingest_pipeline, vault_root))
    graph.add_node("course_planning_loop", _wrap_stage_node("course_planning_loop", course_planning_loop, vault_root))
    graph.add_node("compile_plan_gate", _wrap_stage_node("compile_plan_gate", compile_plan_gate, vault_root))
    graph.add_node("lesson_body_pipeline", _wrap_stage_node("lesson_body_pipeline", lesson_body_pipeline, vault_root))
    graph.add_node("validation_repair_loop", _wrap_stage_node("validation_repair_loop", validation_repair_loop, vault_root))
    graph.add_node("export_pipeline", _wrap_export_stage(vault_root, version))
    graph.add_node("human_review", _wrap_stage_node("human_review", human_review_stage, vault_root))

    graph.set_entry_point("ingest_pipeline")
    for edge in COMPILE_GRAPH_EDGES:
        target = END if edge.target == "END" else edge.target
        graph.add_edge(edge.source, target)
    graph.add_conditional_edges(
        "course_planning_loop",
        _route_after_course_planning_loop,
        {"compile_plan_gate": "compile_plan_gate", "human_review": "human_review"},
    )
    graph.add_conditional_edges(
        "compile_plan_gate",
        _route_after_compile_plan_gate,
        {"lesson_body_pipeline": "lesson_body_pipeline", "course_planning_loop": "course_planning_loop", "human_review": "human_review"},
    )
    graph.add_conditional_edges(
        "lesson_body_pipeline",
        _route_after_lesson_body_pipeline,
        {"validation_repair_loop": "validation_repair_loop", "compile_plan_gate": "compile_plan_gate", "human_review": "human_review"},
    )
    graph.add_conditional_edges(
        "validation_repair_loop",
        _route_after_validation_repair_loop,
        {"export_pipeline": "export_pipeline", "human_review": "human_review"},
    )
    return graph.compile()


def ingest_pipeline(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Run deterministic ingest steps and optional multimodal subpipelines inside one graph stage."""

    for name, node in (
        ("parse_sources", parse_sources),
        ("understand_images", understand_images),
        ("build_source_index", build_source_index),
    ):
        state = _run_internal_step(name, node, state, vault_root)
    return state


def course_planning_loop(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Run semantic planning steps inside a bounded loop."""

    profile = state.get("compile_profile", {})
    max_iterations = max(1, int(profile.get("course_planning_max_iterations", 2)))
    next_step = state.get("next_action") or "plan_course"
    for iteration in range(1, max_iterations + 1):
        state.setdefault("internal_run_log", []).append(
            {"node": "course_planning_loop_iteration", "iteration": iteration, "max_iterations": max_iterations}
        )
        if next_step not in {"extract_units", "organize_logic", "detect_gaps", "generate_lessons"}:
            state = _run_internal_step("synthesize_source_brief", synthesize_source_brief, state, vault_root)
            state = _run_internal_step("plan_course", plan_course, state, vault_root)
            state = _run_internal_step("synthesize_lesson_notes", synthesize_lesson_notes, state, vault_root)
            next_step = state.get("next_action", "extract_units")
        if next_step == "extract_units":
            state = _run_internal_step("extract_units", extract_units, state, vault_root)
            next_step = state.get("next_action", "")
        if _needs_human_review(state):
            return state
        if next_step == "organize_logic":
            state = _run_internal_step("organize_logic", organize_logic, state, vault_root)
            next_step = state.get("next_action", "")
        if _needs_human_review(state):
            return state
        if next_step == "detect_gaps":
            state = _run_internal_step("detect_gaps", detect_gaps, state, vault_root)
            next_step = state.get("next_action", "")
        if _needs_human_review(state):
            return state
        if next_step == "generate_lessons":
            state = _run_internal_step("generate_lessons", generate_lessons, state, vault_root)
            next_step = state.get("next_action", "")
        if _needs_human_review(state):
            return state
        if next_step == "synthesize_compile_plan":
            return state

    _mark_human_review_required(
        state,
        "course_planning_loop",
        f"Course planning did not converge within {max_iterations} iteration(s).",
    )
    return state


def compile_plan_gate(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Synthesize, review, and revise the compile plan inside one bounded gate."""

    profile = state.get("compile_profile", {})
    max_revisions = int(profile.get("compile_plan_max_revisions", 2))
    max_iterations = max(1, int(profile.get("compile_plan_gate_max_iterations", max_revisions + 3)))
    next_step = state.get("next_action") or "synthesize_compile_plan"
    if next_step != "revise_compile_plan":
        state = _run_internal_step("synthesize_compile_plan", synthesize_compile_plan, state, vault_root)
        next_step = state.get("next_action", "review_compile_plan_llm")

    for iteration in range(1, max_iterations + 1):
        state.setdefault("internal_run_log", []).append(
            {"node": "compile_plan_gate_iteration", "iteration": iteration, "max_iterations": max_iterations}
        )
        if next_step == "revise_compile_plan":
            state = _run_internal_step("revise_compile_plan", revise_compile_plan, state, vault_root)
            next_step = state.get("next_action", "")
            if _needs_human_review(state) or next_step == "generate_lessons":
                return state
        if next_step in {"review_compile_plan_llm", "synthesize_compile_plan"}:
            if next_step == "synthesize_compile_plan":
                state = _run_internal_step("synthesize_compile_plan", synthesize_compile_plan, state, vault_root)
            state = _run_internal_step("review_compile_plan_llm", review_compile_plan_llm, state, vault_root)
            next_step = state.get("next_action", "")
            if next_step == "synthesize_lesson_bodies" or _needs_human_review(state):
                return state
            if next_step == "revise_compile_plan":
                continue
        if next_step == "generate_lessons":
            return state

    _mark_human_review_required(
        state,
        "compile_plan_gate",
        f"Compile plan gate did not pass within {max_iterations} review iteration(s).",
    )
    return state


def lesson_body_pipeline(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Generate lesson bodies as a per-lesson pipeline."""

    return _run_internal_step("synthesize_lesson_bodies", synthesize_lesson_bodies, state, vault_root)


def validation_repair_loop(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Run rules-first validation and bounded local repairs."""

    profile = state.get("compile_profile", {})
    max_iterations = max(1, int(profile.get("validation_repair_max_iterations", 2)))
    for iteration in range(1, max_iterations + 1):
        state.setdefault("internal_run_log", []).append(
            {"node": "validation_repair_loop_iteration", "iteration": iteration, "max_iterations": max_iterations}
        )
        state = _run_internal_step("check_markdown_syntax", check_markdown_syntax, state, vault_root)
        state = _run_internal_step("check_grounding_rules", check_grounding_rules, state, vault_root)
        state = _run_internal_step("check_quality_rules", check_quality_rules, state, vault_root)
        rules_ok = bool(state.get("markdown_syntax_report", {}).get("ok", True)) and bool(state.get("validation_report", {}).get("ok"))
        if rules_ok and _requires_validation_llm_from_profile(state):
            state = _run_internal_step("check_grounding_llm", check_grounding_llm, state, vault_root)
            state = _run_internal_step("check_quality_llm", check_quality_llm, state, vault_root)
            state = _run_internal_step("check_quality_rules", check_quality_rules, state, vault_root)
        if bool(state.get("markdown_syntax_report", {}).get("ok", True)) and bool(state.get("validation_report", {}).get("ok")):
            state["next_action"] = "export_version"
            return state
        if iteration >= max_iterations:
            break
        state = _run_internal_step("repair_course", repair_course, state, vault_root)
        if _needs_human_review(state):
            return state

    state = _run_internal_step("repair_course", repair_course, state, vault_root)
    if not _needs_human_review(state):
        _mark_human_review_required(
            state,
            "validation_repair_loop",
            f"Validation and repair did not pass within {max_iterations} iteration(s).",
        )
    return state


def human_review_stage(state: CourseCompileState, vault_root: Path | str = "course-vault") -> CourseCompileState:
    """Write the manual-review artifact as the terminal human gate."""

    return _run_internal_step("human_review", human_review, state, vault_root)


def _run_internal_step(
    name: str,
    node: Callable[[CourseCompileState, Path | str], CourseCompileState],
    state: CourseCompileState,
    vault_root: str | Path,
) -> CourseCompileState:
    course_path = course_dir(Path(vault_root), state["course_id"])
    progress_jsonl = state.get("compile_profile", {}).get("progress_jsonl")
    started = stage_started(course_path, name, progress_jsonl)
    try:
        next_state = node(state, vault_root)
    except Exception as exc:
        stage_failed(course_path, name, started, exc, progress_jsonl)
        raise
    event = stage_finished(
        course_path,
        name,
        started,
        next_action=next_state["next_action"],
        error_count=len(next_state["errors"]),
        extra_jsonl=progress_jsonl,
    )
    next_state.setdefault("internal_run_log", []).append(
        {
            "node": name,
            "next_action": next_state["next_action"],
            "error_count": len(next_state["errors"]),
            "duration_seconds": event.get("duration_seconds", 0),
        }
    )
    return next_state


def _wrap_stage_node(
    name: str,
    node: Callable[[CourseCompileState, Path | str], CourseCompileState],
    vault_root: str | Path,
):
    def wrapped(state: CourseCompileState) -> CourseCompileState:
        before = len(state.setdefault("internal_run_log", []))
        next_state = node(state, vault_root)
        next_state["graph_run_log"].append(
            {
                "node": name,
                "next_action": next_state["next_action"],
                "error_count": len(next_state["errors"]),
                "internal_steps": [entry.get("node", "") for entry in next_state.get("internal_run_log", [])[before:]],
            }
        )
        return next_state

    return wrapped


def _wrap_export_stage(vault_root: str | Path, version: str):
    def wrapped(state: CourseCompileState) -> CourseCompileState:
        before = len(state.setdefault("internal_run_log", []))
        course_path = course_dir(Path(vault_root), state["course_id"])
        progress_jsonl = state.get("compile_profile", {}).get("progress_jsonl")
        started = stage_started(course_path, "export_version", progress_jsonl)
        try:
            next_state = export_version(state, vault_root, version)
        except Exception as exc:
            stage_failed(course_path, "export_version", started, exc, progress_jsonl)
            raise
        event = stage_finished(
            course_path,
            "export_version",
            started,
            next_action=next_state["next_action"],
            error_count=len(next_state["errors"]),
            extra_jsonl=progress_jsonl,
        )
        next_state.setdefault("internal_run_log", []).append(
            {
                "node": "export_version",
                "next_action": next_state["next_action"],
                "error_count": len(next_state["errors"]),
                "duration_seconds": event.get("duration_seconds", 0),
            }
        )
        next_state["graph_run_log"].append(
            {
                "node": "export_pipeline",
                "next_action": next_state["next_action"],
                "error_count": len(next_state["errors"]),
                "internal_steps": [entry.get("node", "") for entry in next_state.get("internal_run_log", [])[before:]],
            }
        )
        return next_state

    return wrapped


def _needs_human_review(state: CourseCompileState) -> bool:
    return state.get("next_action") in {"human_review", "blocked_for_human_review"}


def _mark_human_review_required(state: CourseCompileState, node: str, message: str) -> None:
    state["errors"].append({"node": node, "message": message, "requires_human_review": True})
    previous = state.get("validation_report", {}) if isinstance(state.get("validation_report", {}), dict) else {}
    previous_failures = list(previous.get("failures", []))
    loop_failure = {"node": node, "type": "loop_limit_exhausted", "message": message}
    state["validation_report"] = {
        "ok": False,
        "checks": list(previous.get("checks", [])) + [node],
        "layers": previous.get("layers", {}),
        "failures": previous_failures + [loop_failure],
        "previous_validation_report": previous,
    }
    state["next_action"] = "human_review"


def _requires_validation_llm_from_profile(state: CourseCompileState) -> bool:
    profile = state.get("compile_profile", {})
    return bool(profile.get("use_llm_validation", profile.get("use_llm", False)))


def _route_after_course_planning_loop(state: CourseCompileState) -> str:
    return "human_review" if _needs_human_review(state) else "compile_plan_gate"


def _route_after_compile_plan_gate(state: CourseCompileState) -> str:
    if _needs_human_review(state):
        return "human_review"
    if state.get("next_action") == "generate_lessons":
        return "course_planning_loop"
    return "lesson_body_pipeline"


def _route_after_lesson_body_pipeline(state: CourseCompileState) -> str:
    if _needs_human_review(state):
        return "human_review"
    if state.get("next_action") == "revise_compile_plan":
        return "compile_plan_gate"
    return "validation_repair_loop"


def _route_after_validation_repair_loop(state: CourseCompileState) -> str:
    return "export_pipeline" if state.get("next_action") == "export_version" else "human_review"
