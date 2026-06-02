"""Feedback-to-patch helpers for local recompilation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import course_dir, read_json, write_json
from .nodes import export_version
from .state import CourseCompileState


def record_feedback(
    vault_root: str | Path,
    course_id: str,
    lesson_id: str,
    question: str,
) -> dict[str, Any]:
    """Append a reader question to feedback_log.json."""

    target_dir = course_dir(Path(vault_root), course_id)
    log_path = target_dir / "feedback_log.json"
    feedback_log = read_json(log_path) if log_path.exists() else []
    item = {
        "id": f"feedback-{len(feedback_log) + 1:03d}",
        "lesson_id": lesson_id,
        "question": question,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    feedback_log.append(item)
    write_json(log_path, feedback_log)
    return item


def mine_feedback(vault_root: str | Path, course_id: str) -> list[dict[str, Any]]:
    """Convert open feedback into proposed compile patches."""

    target_dir = course_dir(Path(vault_root), course_id)
    feedback_log = read_json(target_dir / "feedback_log.json")
    existing = read_json(target_dir / "compile_patches.json") if (target_dir / "compile_patches.json").exists() else []
    existing_feedback_ids = {patch.get("feedback_id") for patch in existing}
    patches = list(existing)

    for item in feedback_log:
        if item["status"] == "open" and item["id"] not in existing_feedback_ids:
            patches.append(
                {
                    "id": f"patch-{len(patches) + 1:03d}",
                    "feedback_id": item["id"],
                    "lesson_id": item["lesson_id"],
                    "action": "add_bridge_note",
                    "status": "proposed",
                    "note": f"Bridge note requested by reader question: {item['question']}",
                }
            )

    write_json(target_dir / "compile_patches.json", patches)
    return patches


def approve_patch(vault_root: str | Path, course_id: str, patch_id: str) -> dict[str, Any]:
    """Mark one proposed compile patch as approved."""

    target_dir = course_dir(Path(vault_root), course_id)
    patches = read_json(target_dir / "compile_patches.json")
    selected: dict[str, Any] | None = None
    for patch in patches:
        if patch["id"] == patch_id:
            patch["status"] = "approved"
            selected = patch
            break
    if selected is None:
        raise ValueError(f"Unknown patch id: {patch_id}")
    write_json(target_dir / "compile_patches.json", patches)
    return selected


def apply_approved_patches(
    vault_root: str | Path,
    course_id: str,
    version: str = "v2",
) -> CourseCompileState:
    """Apply approved bridge-note patches and export a new course version."""

    target_dir = course_dir(Path(vault_root), course_id)
    lessons = read_json(target_dir / "lessons.json")
    patches = read_json(target_dir / "compile_patches.json")
    approved = [patch for patch in patches if patch["status"] == "approved"]

    for patch in approved:
        for lesson in lessons:
            if lesson["id"] == patch["lesson_id"]:
                lesson["body"] = f"{lesson['body']}\n\n> Bridge: {patch['note']}"
                patch["status"] = "applied"

    state: CourseCompileState = {
        "course_id": course_id,
        "source_files": read_json(target_dir / "course_meta.json").get("source_files", []),
        "parsed_chunks": [],
        "units": read_json(target_dir / "units.json"),
        "logic_graph": read_json(target_dir / "logic_graph.json"),
        "gap_report": read_json(target_dir / "gap_report.json"),
        "outline": read_json(target_dir / "outline.json"),
        "concepts": read_json(target_dir / "concepts.json"),
        "lessons": lessons,
        "compile_profile": read_json(target_dir / "compile_profile.json"),
        "compile_patches": patches,
        "validation_report": {"ok": True, "checks": ["approved_patch_application"], "failures": []},
        "next_action": "export_version",
        "errors": [],
    }
    write_json(target_dir / "lessons.json", lessons)
    write_json(target_dir / "compile_patches.json", patches)
    write_json(target_dir / "changelog.json", [{"version": version, "applied_patches": [patch["id"] for patch in approved]}])
    return export_version(state, vault_root, version)
