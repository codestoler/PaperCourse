#!/usr/bin/env python3
"""Validate exported course artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--vault-root", default="course-vault")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--min-lessons", type=int, default=1)
    args = parser.parse_args()

    course_dir = Path(args.vault_root) / "courses" / args.course_id
    required = [
        "course_meta.json",
        "units.json",
        "logic_graph.json",
        "gap_report.json",
        "outline.json",
        "concepts.json",
        "lessons.json",
        "validation_report.json",
        "graph_run_log.json",
    ]
    missing = [name for name in required if not (course_dir / name).exists()]
    if missing:
        print(f"PROBLEM: missing course artifacts: {missing}")
        return 2

    lessons = read_json(course_dir / "lessons.json")
    validation = read_json(course_dir / "validation_report.json")
    gap_report = read_json(course_dir / "gap_report.json")
    lesson_files = sorted((course_dir / "versions" / args.version / "lessons").glob("*.md"))

    problems: list[str] = []
    if len(lessons) < args.min_lessons:
        problems.append(f"lesson count {len(lessons)} is below minimum {args.min_lessons}")
    if len(lesson_files) != len(lessons):
        problems.append(f"exported lesson file count {len(lesson_files)} != lessons.json count {len(lessons)}")
    if not validation.get("ok"):
        problems.append(f"validation_report is not ok: {validation.get('failures')}")
    if gap_report.get("summary", {}).get("high", 0):
        problems.append(f"gap_report has high severity items: {gap_report.get('summary')}")
    empty_files = [path.name for path in lesson_files if not path.read_text(encoding="utf-8").strip()]
    if empty_files:
        problems.append(f"empty lesson files: {empty_files[:5]}")

    if problems:
        print("PROBLEM: course validation failed")
        for problem in problems:
            print(f"- {problem}")
        return 3

    print(
        f"status=ok course={args.course_id} version={args.version} "
        f"lessons={len(lessons)} gap_high={gap_report.get('summary', {}).get('high', 0)}"
    )
    return 0


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
