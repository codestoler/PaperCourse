#!/usr/bin/env python3
"""Playwright smoke checks for the PaperCourse frontend."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--chrome-path", default="", help="Optional explicit Chromium executable path")
    parser.add_argument("--exercise-library-project-flow", action="store_true")
    args = parser.parse_args()

    chrome_path = Path(args.chrome_path).resolve() if args.chrome_path else find_chromium()
    temp_upload: Path | None = None
    cleanup: dict[str, str] = {}

    with sync_playwright() as p:
        launch_args = {"headless": True}
        if chrome_path:
            launch_args["executable_path"] = str(chrome_path)
        browser = p.chromium.launch(**launch_args)
        page = browser.new_page(viewport={"width": 1280, "height": 950})
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(args.base_url, wait_until="networkidle")
        assert page.locator("#libraryFileInput").count() == 1, "missing library file input"
        assert page.locator("#libraryFileList").count() == 1, "missing library file list"
        assert page.locator("#projectForm").count() == 1, "missing project form"
        requirements = page.locator("#projectRequirements").input_value()
        assert "course_structure" in requirements, "default course_structure requirement missing"
        assert "formula_handling" in requirements, "default formula_handling requirement missing"

        if args.exercise_library_project_flow:
            temp_upload = make_upload_file()
            cleanup = exercise_library_project_flow(page, temp_upload, requirements)

        browser.close()

    if temp_upload:
        temp_upload.unlink(missing_ok=True)
    cleanup_smoke_records(cleanup)

    fatal_errors = [item for item in console_errors if "favicon" not in item.lower()]
    assert not fatal_errors, "browser console errors: " + "; ".join(fatal_errors[:5])
    print("browser_smoke=ok")
    if cleanup:
        print(f"library_file_id={cleanup.get('file_id')}")
        print(f"project_id={cleanup.get('project_id')}")
    return 0


def find_chromium() -> Path | None:
    candidates = sorted((ROOT / ".playwright-browsers").glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    candidates += sorted((ROOT / ".playwright-browsers").glob("chromium-*/chrome-linux64/chrome"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def make_upload_file() -> Path:
    path = Path(tempfile.gettempdir()) / f"browser-smoke-{uuid.uuid4().hex[:8]}.md"
    dollars = "$$"
    fence = "```"
    path.write_text(
        "# Browser Smoke Chapter\n\n"
        "Definition 1: This browser smoke validates upload, automatic analysis, visible report, "
        "and project configuration. The text is intentionally long enough for normal analysis "
        "status and includes structured content.\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        f"{dollars}a+b=c{dollars}\n\n"
        f"{fence}python\nprint(1)\n{fence}\n\n"
        "![fig](fig.png)\n",
        encoding="utf-8",
    )
    return path


def exercise_library_project_flow(page, upload_path: Path, default_requirements: str) -> dict[str, str]:
    name = upload_path.name
    page.set_input_files("#libraryFileInput", str(upload_path))
    page.wait_for_function(
        '(filename) => Array.from(document.querySelectorAll(".resource-item strong")).some(el => el.textContent.includes(filename))',
        arg=name,
        timeout=15_000,
    )
    files = page.evaluate('() => fetch("/api/library/files").then(r => r.json())')
    record = next(item for item in files["files"] if item["filename"] == name)
    assert record["upload_status"] == "success", record
    assert record["analysis_status"] in {"success", "warning"}, record

    page.locator(f'[data-library-report="{record["id"]}"]').click()
    page.wait_for_selector(".analysis-report")
    report_text = page.locator(".analysis-report").inner_text()
    for label in ("资料分析报告", "章节结构", "主要知识点", "潜在问题"):
        assert label in report_text, f"analysis report missing {label}"

    page.fill("#projectTitle", "Browser Smoke Project")
    page.fill("#projectSubject", "Integration")
    page.fill("#projectDescription", "Created by browser smoke")
    page.select_option("#projectFiles", [record["id"]])
    page.fill("#projectRequirements", default_requirements + "\nexercise_ratio: 每节至少 2 个检查项。")
    page.locator('#projectForm button[type="submit"]').click()
    page.wait_for_function(
        '() => Array.from(document.querySelectorAll("#projectList strong")).some(el => el.textContent.includes("Browser Smoke Project"))',
        timeout=10_000,
    )
    projects = page.evaluate('() => fetch("/api/projects").then(r => r.json())')
    project = next(item for item in projects["projects"] if item["title"] == "Browser Smoke Project")
    context = page.evaluate(
        '(projectId) => fetch("/api/projects/" + projectId + "/compile-context").then(r => r.json())',
        project["id"],
    )
    assert context["source_files"][0]["id"] == record["id"], context
    assert context["compile_requirements"]["exercise_ratio"] == "每节至少 2 个检查项。", context
    return {"file_id": record["id"], "project_id": project["id"]}


def cleanup_smoke_records(cleanup: dict[str, str]) -> None:
    file_id = cleanup.get("file_id")
    project_id = cleanup.get("project_id")
    if file_id:
        shutil.rmtree(ROOT / "course-vault" / "library" / "files" / file_id, ignore_errors=True)
        (ROOT / "course-vault" / "library" / "analysis" / f"{file_id}.json").unlink(missing_ok=True)
        index_path = ROOT / "course-vault" / "library" / "library_index.json"
        if index_path.exists():
            data = json.loads(index_path.read_text(encoding="utf-8"))
            data["files"] = [item for item in data.get("files", []) if item.get("id") != file_id]
            index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if project_id:
        shutil.rmtree(ROOT / "course-vault" / "projects" / project_id, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
