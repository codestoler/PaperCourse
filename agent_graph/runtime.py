"""Runtime progress and event helpers for long-running local compiles."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import ensure_dir, write_json


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_event(
    *,
    stage: str,
    status: str,
    message: str = "",
    **fields: Any,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_now_iso(),
        "stage": stage,
        "status": status,
    }
    if message:
        event["message"] = message
    event.update({key: value for key, value in fields.items() if value is not None})
    return event


def append_runtime_event(course_path: Path, event: dict[str, Any], extra_jsonl: str | Path | None = None) -> None:
    ensure_dir(course_path)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with (course_path / "runtime_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line)
    if extra_jsonl:
        extra_path = Path(extra_jsonl)
        ensure_dir(extra_path.parent)
        with extra_path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def write_runtime_status(course_path: Path, status: dict[str, Any]) -> None:
    write_json(course_path / "runtime_status.json", status)


def stage_started(course_path: Path, stage: str, extra_jsonl: str | Path | None = None) -> float:
    started = time.monotonic()
    append_runtime_event(course_path, runtime_event(stage=stage, status="started"), extra_jsonl)
    write_runtime_status(
        course_path,
        {
            "state": "running",
            "current_stage": stage,
            "updated_at": utc_now_iso(),
        },
    )
    return started


def stage_finished(
    course_path: Path,
    stage: str,
    started: float,
    *,
    next_action: str,
    error_count: int,
    extra_jsonl: str | Path | None = None,
) -> dict[str, Any]:
    event = runtime_event(
        stage=stage,
        status="finished",
        duration_seconds=round(time.monotonic() - started, 3),
        next_action=next_action,
        error_count=error_count,
    )
    append_runtime_event(course_path, event, extra_jsonl)
    write_runtime_status(
        course_path,
        {
            "state": "running" if next_action != "done" else "done",
            "current_stage": stage,
            "next_action": next_action,
            "error_count": error_count,
            "updated_at": utc_now_iso(),
        },
    )
    return event


def stage_failed(
    course_path: Path,
    stage: str,
    started: float,
    exc: BaseException,
    extra_jsonl: str | Path | None = None,
) -> None:
    event = runtime_event(
        stage=stage,
        status="failed",
        duration_seconds=round(time.monotonic() - started, 3),
        error=repr(exc),
    )
    append_runtime_event(course_path, event, extra_jsonl)
    write_runtime_status(
        course_path,
        {
            "state": "failed",
            "current_stage": stage,
            "error": repr(exc),
            "updated_at": utc_now_iso(),
        },
    )
