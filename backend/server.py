"""Small stdlib HTTP server for browsing compiled courses locally."""

from __future__ import annotations

import argparse
import cgi
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
VAULT_ROOT = ROOT / "course-vault"


class CourseRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api()
            return
        self.send_error(404, "Unknown route")

    def do_PATCH(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api()
            return
        self.send_error(404, "Unknown route")

    def do_DELETE(self) -> None:
        if self.path.startswith("/api/"):
            self._handle_api()
            return
        self.send_error(404, "Unknown route")

    def _handle_api(self) -> None:
        parts = [unquote(part) for part in self.path.split("?")[0].strip("/").split("/")]
        try:
            method = self.command.upper()
            if method == "GET" and parts == ["api", "courses"]:
                self._send_json({"courses": list_courses(VAULT_ROOT)})
            elif method == "GET" and parts == ["api", "library", "files"]:
                self._send_json({"files": list_library_files(VAULT_ROOT)})
            elif method == "POST" and parts == ["api", "library", "upload"]:
                self._send_json({"files": self._handle_library_upload()})
            elif method == "GET" and len(parts) == 5 and parts[:3] == ["api", "library", "files"] and parts[4] == "analysis":
                self._send_json(read_library_analysis(VAULT_ROOT, parts[3]))
            elif method == "GET" and parts == ["api", "projects"]:
                self._send_json({"projects": list_course_projects(VAULT_ROOT)})
            elif method == "POST" and parts == ["api", "projects"]:
                payload = self._read_json_body()
                self._send_json(create_course_project(VAULT_ROOT, payload))
            elif method == "GET" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
                self._send_json(read_course_project(VAULT_ROOT, parts[2]))
            elif method == "GET" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "compile-context":
                self._send_json(project_compile_context(VAULT_ROOT, parts[2]))
            elif method == "POST" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "compile":
                payload = self._read_json_body()
                self._send_json(start_project_compile_job(VAULT_ROOT, parts[2], payload))
            elif method == "GET" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "jobs":
                self._send_json({"jobs": list_project_jobs(VAULT_ROOT, parts[2])})
            elif method == "PATCH" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
                payload = self._read_json_body()
                self._send_json(update_course_project(VAULT_ROOT, parts[2], payload))
            elif method == "GET" and len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self._send_json(read_job_status(VAULT_ROOT, parts[2]))
            elif len(parts) >= 3 and parts[:2] == ["api", "assets"]:
                self._send_asset(parts[2:])
            elif method == "GET" and len(parts) == 4 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                self._send_json({"versions": list_versions(VAULT_ROOT, parts[2])})
            elif method == "GET" and len(parts) == 4 and parts[:2] == ["api", "courses"] and parts[3] == "manage":
                self._send_json(course_management_payload(VAULT_ROOT, parts[2]))
            elif method == "GET" and len(parts) == 6 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                self._send_json(read_lesson(VAULT_ROOT, parts[2], parts[4], parts[5]))
            elif method == "PATCH" and len(parts) == 6 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                payload = self._read_json_body()
                self._send_json(rename_lesson_entry(VAULT_ROOT, parts[2], parts[4], parts[5], str(payload.get("title", "")).strip()))
            elif method == "DELETE" and len(parts) == 6 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                self._send_json(delete_lesson_entry(VAULT_ROOT, parts[2], parts[4], parts[5]))
            else:
                self.send_error(404, "Unknown API route")
        except FileNotFoundError:
            self.send_error(404, "Course data not found")
        except ValueError as exc:
            self.send_error(400, str(exc))

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _handle_library_upload(self) -> list[dict[str, object]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Expected multipart/form-data upload")
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        fields = form["files"] if "files" in form else form["file"] if "file" in form else []
        if not isinstance(fields, list):
            fields = [fields]
        uploaded: list[dict[str, object]] = []
        for field in fields:
            if not getattr(field, "filename", ""):
                continue
            uploaded.append(store_library_upload(VAULT_ROOT, field.filename, field.file))
        if not uploaded:
            raise ValueError("No files uploaded")
        return uploaded

    def _send_asset(self, parts: list[str]) -> None:
        relative = Path(*parts)
        candidate = (ROOT / relative).resolve()
        vault = VAULT_ROOT.resolve()
        if not candidate.is_file() or not candidate.is_relative_to(vault):
            self.send_error(404, "Asset not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def store_library_upload(vault_root: Path, filename: str, stream) -> dict[str, object]:
    """Store one uploaded source file and synchronously create an analysis report."""

    safe_name = _safe_filename(filename)
    content = stream.read()
    if isinstance(content, str):
        content = content.encode("utf-8")
    file_hash = hashlib.sha256(content).hexdigest()
    file_id = f"{Path(safe_name).stem[:48] or 'source'}-{file_hash[:12]}"
    target_dir = vault_root / "library" / "files" / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    target.write_bytes(content)
    record = {
        "id": file_id,
        "filename": safe_name,
        "path": str(target.relative_to(vault_root)),
        "size": len(content),
        "sha256": file_hash,
        "mime_type": mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
        "upload_status": "success",
        "analysis_status": "running",
        "uploaded_at": _mtime_iso(target),
    }
    report = analyze_library_file(vault_root, record)
    record["analysis_status"] = report["status"]
    record["analysis_report_path"] = str(Path("library") / "analysis" / f"{file_id}.json")
    _upsert_library_record(vault_root, record)
    return record


def list_library_files(vault_root: Path = VAULT_ROOT) -> list[dict[str, object]]:
    index = _read_json_if_exists(vault_root / "library" / "library_index.json", {"files": []})
    files = index.get("files", []) if isinstance(index, dict) else []
    return sorted((item for item in files if isinstance(item, dict)), key=lambda item: str(item.get("uploaded_at", "")), reverse=True)


def read_library_analysis(vault_root: Path, file_id: str) -> dict[str, object]:
    path = _library_analysis_path(vault_root, file_id)
    if not path.exists():
        raise FileNotFoundError(file_id)
    return _read_json(path)


def analyze_library_file(vault_root: Path, record: dict[str, object]) -> dict[str, object]:
    path = (vault_root / str(record["path"])).resolve()
    if not path.is_file() or not path.is_relative_to(vault_root.resolve()):
        raise FileNotFoundError(str(record.get("id", "")))
    text, extraction = _extract_text_for_analysis(path)
    report = {
        "file_id": record["id"],
        "filename": record["filename"],
        "status": "success" if text.strip() else "warning",
        "pipeline": [
            {"step": "file_transcoding", "status": extraction["transcoding_status"], "detail": extraction["encoding"]},
            {"step": "text_extraction", "status": extraction["text_status"], "detail": f"{len(text)} characters extracted"},
            {"step": "table_recognition", "status": "success", "count": len(_detect_tables(text, path))},
            {"step": "formula_recognition", "status": "success", "count": len(_detect_formulas(text))},
            {"step": "code_block_recognition", "status": "success", "count": len(_detect_code_blocks(text, path))},
            {"step": "image_recognition", "status": "success", "count": len(_detect_images(text, path))},
        ],
        "document": {
            "line_count": len(text.splitlines()),
            "character_count": len(text),
            "source_type": path.suffix.lower().lstrip(".") or "unknown",
        },
        "chapter_structure": _detect_chapters(text),
        "knowledge_points": _detect_knowledge_points(text),
        "tables": _detect_tables(text, path),
        "formulas": _detect_formulas(text),
        "code_blocks": _detect_code_blocks(text, path),
        "images": _detect_images(text, path),
        "potential_problems": _detect_analysis_problems(text, path, extraction),
    }
    if report["potential_problems"]:
        report["status"] = "warning" if text.strip() else "failed"
    _write_json(_library_analysis_path(vault_root, str(record["id"])), report)
    return report


def default_compile_requirements() -> dict[str, object]:
    return {
        "course_structure": "按学习目标组织为中等粒度章节，避免把例子或短概念单独作为章节。",
        "explanation_depth": "适合碎片化阅读，补足必要推导但标记 bridge/inferred 内容。",
        "exercise_ratio": "每节包含 1-3 个检查项或练习提示。",
        "formula_handling": "保留重要公式为 LaTeX display math，避免公式被 Markdown 表格或列表破坏。",
        "image_handling": "保留源图片引用，生成可追溯说明；不确定图片进入待确认列表。",
        "code_block_handling": "保留代码块语言与命令，不编造缺失参数或输出。",
        "source_grounding": "课程内容必须引用资料库文件和解析块，缺失信息标记待源材料确认。",
    }


def create_course_project(vault_root: Path, payload: dict[str, object]) -> dict[str, object]:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("Project title is required")
    project_id = _slugify_filename(str(payload.get("id") or title))
    projects_dir = vault_root / "projects"
    project_path = projects_dir / project_id / "project.json"
    suffix = 2
    while project_path.exists():
        project_id = f"{_slugify_filename(title)}-{suffix}"
        project_path = projects_dir / project_id / "project.json"
        suffix += 1
    project = {
        "id": project_id,
        "title": title,
        "description": str(payload.get("description", "")).strip(),
        "subject": str(payload.get("subject", "")).strip(),
        "library_file_ids": [str(item) for item in payload.get("library_file_ids", []) if str(item).strip()],
        "compile_requirements": {**default_compile_requirements(), **dict(payload.get("compile_requirements", {}) or {})},
        "created_at": _mtime_iso_from(__import__("time").time()),
        "updated_at": _mtime_iso_from(__import__("time").time()),
        "status": "configured",
    }
    _write_json(project_path, project)
    return project


def list_course_projects(vault_root: Path = VAULT_ROOT) -> list[dict[str, object]]:
    projects_dir = vault_root / "projects"
    if not projects_dir.exists():
        return []
    projects = []
    for path in sorted(projects_dir.glob("*/project.json")):
        try:
            projects.append(_read_json(path))
        except json.JSONDecodeError:
            continue
    return sorted(projects, key=lambda item: str(item.get("updated_at", "")), reverse=True)


def read_course_project(vault_root: Path, project_id: str) -> dict[str, object]:
    path = _project_path(vault_root, project_id)
    return _read_json(path)


def update_course_project(vault_root: Path, project_id: str, payload: dict[str, object]) -> dict[str, object]:
    path = _project_path(vault_root, project_id)
    project = _read_json(path)
    if payload.get("title") is not None:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise ValueError("Project title is required")
        project["title"] = title
    for key in ("description", "subject"):
        if key in payload:
            project[key] = str(payload.get(key, "")).strip()
    if "library_file_ids" in payload:
        project["library_file_ids"] = [str(item) for item in payload.get("library_file_ids", []) if str(item).strip()]
    if "compile_requirements" in payload:
        project["compile_requirements"] = {**default_compile_requirements(), **dict(payload.get("compile_requirements", {}) or {})}
    project["updated_at"] = _mtime_iso_from(__import__("time").time())
    _write_json(path, project)
    return project


def project_compile_context(vault_root: Path, project_id: str) -> dict[str, object]:
    """Return the saved project config plus library file paths for downstream compile tasks."""

    project = read_course_project(vault_root, project_id)
    files_by_id = {str(item.get("id")): item for item in list_library_files(vault_root)}
    source_files = []
    missing_files = []
    for file_id in project.get("library_file_ids", []):
        record = files_by_id.get(str(file_id))
        if not record:
            missing_files.append(str(file_id))
            continue
        source_files.append(
            {
                "id": record["id"],
                "filename": record["filename"],
                "path": str((vault_root / str(record["path"])).resolve()),
                "analysis_report_path": str((vault_root / str(record.get("analysis_report_path", ""))).resolve()) if record.get("analysis_report_path") else "",
                "analysis_status": record.get("analysis_status", "unknown"),
            }
        )
    return {
        "project": project,
        "source_files": source_files,
        "missing_library_file_ids": missing_files,
        "compile_requirements": project.get("compile_requirements", default_compile_requirements()),
    }


def start_project_compile_job(vault_root: Path, project_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    context = project_compile_context(vault_root, project_id)
    project = context["project"]
    sources = [Path(str(item["path"])) for item in context["source_files"]]
    if context["missing_library_file_ids"]:
        raise ValueError("Project references missing library files")
    if not sources:
        raise ValueError("Project has no source files")
    unsupported = [path.name for path in sources if path.suffix.lower() == ".pdf"]
    job_id = f"{_safe_filename(project_id)}-{uuid.uuid4().hex[:12]}"
    version = str((payload or {}).get("version") or f"v{time.strftime('%Y%m%d%H%M%S')}")
    course_id = _safe_filename(str(project.get("id") or project_id))
    job_dir = _job_dir(vault_root, job_id)
    job = {
        "id": job_id,
        "project_id": project_id,
        "course_id": course_id,
        "version": version,
        "state": "blocked" if unsupported else "queued",
        "current_stage": "queued",
        "progress": 0,
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "error": f"PDF requires MinerU parsing before browser compile: {', '.join(unsupported)}" if unsupported else "",
        "command": [],
        "compile_requirements": context.get("compile_requirements", default_compile_requirements()),
        "created_at": _mtime_iso_from(time.time()),
        "updated_at": _mtime_iso_from(time.time()),
    }
    _write_json(job_dir / "compile_context.json", context)
    _write_json(job_dir / "job.json", job)
    if unsupported:
        _append_job_event(job_dir, {"stage": "prepare", "status": "blocked", "message": job["error"]})
        return _job_response(job)

    command = [
        sys.executable,
        str(ROOT / "scripts" / "compile_course.py"),
        *[str(path) for path in sources],
        "--course-id",
        course_id,
        "--vault-root",
        str(vault_root),
        "--version",
        version,
        "--use-source-index",
        "--use-source-brief",
        "--use-lesson-notes",
        "--detailed-lessons",
        "--progress-jsonl",
        str(job_dir / "events.jsonl"),
    ]
    job["command"] = command
    _write_json(job_dir / "job.json", job)
    thread = threading.Thread(target=_run_compile_job, args=(vault_root, job_id, command), daemon=True)
    thread.start()
    return _job_response(job)


def _run_compile_job(vault_root: Path, job_id: str, command: list[str]) -> None:
    job_dir = _job_dir(vault_root, job_id)
    job = _read_json(job_dir / "job.json")
    job.update({"state": "running", "current_stage": "starting", "started_at": _mtime_iso_from(time.time()), "updated_at": _mtime_iso_from(time.time())})
    _write_json(job_dir / "job.json", job)
    _append_job_event(job_dir, {"stage": "job", "status": "started", "message": "Compile process started"})
    try:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.strip()
            if stripped:
                _append_job_event(job_dir, {"stage": "subprocess", "status": "output", "message": stripped})
        exit_code = process.wait()
        job = _read_json(job_dir / "job.json")
        job.update(
            {
                "state": "done" if exit_code == 0 else "failed",
                "current_stage": "done" if exit_code == 0 else "failed",
                "progress": 100 if exit_code == 0 else _job_progress_from_events(job_dir),
                "finished_at": _mtime_iso_from(time.time()),
                "updated_at": _mtime_iso_from(time.time()),
                "exit_code": exit_code,
            }
        )
        if exit_code != 0:
            job["error"] = f"Compile process exited with code {exit_code}"
        _write_json(job_dir / "job.json", job)
        _append_job_event(job_dir, {"stage": "job", "status": job["state"], "exit_code": exit_code})
        _update_project_job_status(vault_root, str(job["project_id"]), job)
    except Exception as exc:  # pragma: no cover - subprocess environment specific
        job = _read_json(job_dir / "job.json")
        job.update(
            {
                "state": "failed",
                "current_stage": "failed",
                "finished_at": _mtime_iso_from(time.time()),
                "updated_at": _mtime_iso_from(time.time()),
                "error": repr(exc),
            }
        )
        _write_json(job_dir / "job.json", job)
        _append_job_event(job_dir, {"stage": "job", "status": "failed", "error": repr(exc)})
        _update_project_job_status(vault_root, str(job["project_id"]), job)


def read_job_status(vault_root: Path, job_id: str) -> dict[str, object]:
    job_dir = _job_dir(vault_root, job_id)
    job_path = job_dir / "job.json"
    if not job_path.exists():
        raise FileNotFoundError(job_id)
    job = _read_json(job_path)
    events = _read_job_events(job_dir)
    if job.get("state") == "running":
        latest = next((event for event in reversed(events) if event.get("stage") and event.get("status") != "output"), {})
        if latest:
            job["current_stage"] = str(latest.get("stage", job.get("current_stage", "")))
            job["progress"] = _job_progress_from_events(job_dir)
    return {**_job_response(job), "events": events[-40:]}


def list_project_jobs(vault_root: Path, project_id: str) -> list[dict[str, object]]:
    jobs_dir = vault_root / "jobs"
    if not jobs_dir.exists():
        return []
    jobs = []
    for path in sorted(jobs_dir.glob("*/job.json")):
        try:
            job = _read_json(path)
        except json.JSONDecodeError:
            continue
        if str(job.get("project_id")) == project_id:
            jobs.append(_job_response(job))
    return sorted(jobs, key=lambda item: str(item.get("created_at", "")), reverse=True)


def _job_response(job: dict[str, object]) -> dict[str, object]:
    return {
        "id": job["id"],
        "project_id": job.get("project_id", ""),
        "course_id": job.get("course_id", ""),
        "version": job.get("version", ""),
        "state": job.get("state", "unknown"),
        "progress": int(job.get("progress") or 0),
        "current_stage": job.get("current_stage", ""),
        "started_at": job.get("started_at", ""),
        "finished_at": job.get("finished_at", ""),
        "exit_code": job.get("exit_code"),
        "error": job.get("error", ""),
        "status_url": f"/api/jobs/{job['id']}",
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
    }


def _job_dir(vault_root: Path, job_id: str) -> Path:
    return vault_root / "jobs" / _safe_filename(job_id)


def _append_job_event(job_dir: Path, event: dict[str, object]) -> None:
    event = {"timestamp": _mtime_iso_from(time.time()), **event}
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_job_events(job_dir: Path) -> list[dict[str, object]]:
    path = job_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _job_progress_from_events(job_dir: Path) -> int:
    order = [
        "parse_sources",
        "understand_images",
        "build_source_index",
        "synthesize_source_brief",
        "plan_course",
        "synthesize_lesson_notes",
        "extract_units",
        "organize_logic",
        "detect_gaps",
        "generate_lessons",
        "synthesize_compile_plan",
        "review_compile_plan_llm",
        "synthesize_lesson_bodies",
        "check_markdown_syntax",
        "check_grounding_rules",
        "check_quality_rules",
        "export_version",
    ]
    completed = {str(event.get("stage")) for event in _read_job_events(job_dir) if event.get("status") == "finished"}
    if not completed:
        return 5
    index = max((order.index(stage) + 1 for stage in completed if stage in order), default=1)
    return min(95, max(5, round(index / len(order) * 100)))


def _update_project_job_status(vault_root: Path, project_id: str, job: dict[str, object]) -> None:
    try:
        path = _project_path(vault_root, project_id)
    except FileNotFoundError:
        return
    project = _read_json(path)
    project["status"] = "compiled" if job.get("state") == "done" else str(job.get("state", "unknown"))
    project["last_job_id"] = job.get("id", "")
    project["last_compile_version"] = job.get("version", "")
    project["updated_at"] = _mtime_iso_from(time.time())
    _write_json(path, project)


def list_courses(vault_root: Path = VAULT_ROOT) -> list[dict[str, object]]:
    courses_dir = vault_root / "courses"
    if not courses_dir.exists():
        return []
    courses = [course_summary(vault_root, path.name) for path in sorted(item for item in courses_dir.iterdir() if item.is_dir())]
    return sorted(courses, key=lambda item: (0 if item["id"] == "numerical-analysis" else 1, str(item.get("title", item["id"]))))


def course_summary(vault_root: Path, course_id: str) -> dict[str, object]:
    course_path = _course_path(vault_root, course_id)
    meta = _read_json_if_exists(course_path / "course_meta.json", {"course_id": course_id})
    validation = _read_json_if_exists(course_path / "validation_report.json", {})
    versions = list_versions(vault_root, course_id)
    latest_version = versions[-1]["id"] if versions else ""
    lesson_count = int(meta.get("lesson_count") or (versions[-1]["lesson_count"] if versions else 0))
    return {
        "id": course_id,
        "title": _course_title(course_id, meta),
        "description": _course_description(course_id, meta),
        "updated_at": _latest_mtime_iso(course_path),
        "status": _compile_status(course_path, validation),
        "lesson_count": lesson_count,
        "version_count": len(versions),
        "latest_version": latest_version,
        "source_files": list(meta.get("source_files", [])),
        "meta": meta,
    }


def list_versions(vault_root: Path, course_id: str) -> list[dict[str, object]]:
    versions_dir = _course_path(vault_root, course_id) / "versions"
    versions: list[dict[str, object]] = []
    for version_dir in sorted((item for item in versions_dir.iterdir() if item.is_dir()), key=_version_sort_key):
        lesson_dir = version_dir / "lessons"
        lessons = [
            {"file": lesson.name, "title": _title_from_markdown(lesson), "updated_at": _mtime_iso(lesson)}
            for lesson in sorted(lesson_dir.glob("*.md"))
        ]
        versions.append({"id": version_dir.name, "lessons": lessons, "lesson_count": len(lessons), "updated_at": _latest_mtime_iso(version_dir)})
    return versions


def course_management_payload(vault_root: Path, course_id: str) -> dict[str, object]:
    summary = course_summary(vault_root, course_id)
    course_path = _course_path(vault_root, course_id)
    lessons = _read_json_if_exists(course_path / "lessons.json", [])
    outline = _read_json_if_exists(course_path / "outline.json", {"lessons": []})
    validation = _read_json_if_exists(course_path / "validation_report.json", {})
    versions = list_versions(vault_root, course_id)
    latest_version = str(summary.get("latest_version") or (versions[-1]["id"] if versions else ""))
    latest_lessons = versions[-1]["lessons"] if versions else []
    return {
        "course": summary,
        "source_files": summary.get("source_files", []),
        "versions": versions,
        "latest_version": latest_version,
        "lessons": lessons,
        "outline": outline,
        "chapter_structure": _chapter_structure(outline, lessons),
        "content_entries": latest_lessons,
        "validation": validation,
        "status": summary["status"],
    }


def read_lesson(vault_root: Path, course_id: str, version: str, lesson_file: str) -> dict[str, object]:
    lesson_path = _lesson_path(vault_root, course_id, version, lesson_file)
    text = lesson_path.read_text(encoding="utf-8")
    return {"file": lesson_file, "title": _title_from_text(text), "markdown": text}


def rename_lesson_entry(vault_root: Path, course_id: str, version: str, lesson_file: str, title: str) -> dict[str, object]:
    if not title:
        raise ValueError("Title is required")
    lesson_path = _lesson_path(vault_root, course_id, version, lesson_file)
    text = lesson_path.read_text(encoding="utf-8")
    new_text = _replace_markdown_title(text, title)
    new_name = f"{_lesson_order_prefix(lesson_path.name)}-{_slugify_filename(title)}.md"
    new_path = lesson_path.with_name(new_name)
    if new_path != lesson_path and new_path.exists():
        raise ValueError("A lesson file with the new title already exists")
    lesson_path.write_text(new_text, encoding="utf-8")
    if new_path != lesson_path:
        lesson_path.rename(new_path)
    _update_lessons_json_title(vault_root, course_id, lesson_file, title)
    return {"file": new_path.name, "title": title, "updated_at": _mtime_iso(new_path)}


def delete_lesson_entry(vault_root: Path, course_id: str, version: str, lesson_file: str) -> dict[str, object]:
    lesson_path = _lesson_path(vault_root, course_id, version, lesson_file)
    lesson_path.unlink()
    _delete_lesson_from_lessons_json(vault_root, course_id, lesson_file)
    return {"deleted": True, "file": lesson_file}


def _extract_text_for_analysis(path: Path) -> tuple[str, dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "", {"transcoding_status": "unsupported", "text_status": "failed", "encoding": "pdf_requires_parser"}
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
            return text.replace("\r\n", "\n"), {"transcoding_status": "success", "text_status": "success", "encoding": encoding}
        except UnicodeDecodeError:
            continue
    return "", {"transcoding_status": "failed", "text_status": "failed", "encoding": "unknown"}


def _detect_chapters(text: str) -> list[dict[str, object]]:
    chapters: list[dict[str, object]] = []
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    numbered_pattern = re.compile(r"^\s*((?:第\s*)?\d+(?:\.\d+)*[章节.]?)\s+(.{2,80})$")
    for line_no, line in enumerate(text.splitlines(), start=1):
        heading = heading_pattern.match(line)
        if heading:
            chapters.append({"title": heading.group(2).strip(), "level": len(heading.group(1)), "line": line_no})
            continue
        numbered = numbered_pattern.match(line)
        if numbered:
            chapters.append({"title": f"{numbered.group(1)} {numbered.group(2).strip()}", "level": 2, "line": line_no})
    if chapters:
        return chapters[:80]
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return [{"title": title[:80] or "未识别章节", "level": 1, "line": 1}] if text.strip() else []


def _detect_knowledge_points(text: str) -> list[dict[str, object]]:
    candidates: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"^[\s\-*+□■▪▫◆◇◻◼\d.)]+", "", line).strip()
        if not 4 <= len(cleaned) <= 80:
            continue
        if re.match(r"^(定义|定理|引理|命题|算法|例|Example|Definition|Theorem)\s*[\d.]*", cleaned, re.IGNORECASE):
            candidates.append(cleaned)
        elif any(token in cleaned for token in ("方法", "公式", "模型", "步骤", "误差", "边界条件", "参数", "配置")):
            candidates.append(cleaned)
    seen: set[str] = set()
    points = []
    for item in candidates:
        key = re.sub(r"\s+", "", item.lower())
        if key in seen:
            continue
        seen.add(key)
        points.append({"name": item, "confidence": "medium"})
        if len(points) >= 24:
            break
    return points


def _detect_tables(text: str, path: Path) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        if "|" in lines[index] and re.match(r"^\s*\|?\s*:?-{3,}:?", lines[index + 1]):
            tables.append({"type": "markdown_table", "line": index + 1, "status": "recognized"})
    if path.suffix.lower() == ".csv" and any("," in line for line in lines[:20]):
        tables.append({"type": "csv_table", "line": 1, "status": "recognized"})
    if re.search(r"<table[\s>]", text, re.IGNORECASE):
        tables.append({"type": "html_table", "line": _line_for_match(text, re.search(r"<table[\s>]", text, re.IGNORECASE)), "status": "recognized"})
    return tables[:40]


def _detect_formulas(text: str) -> list[dict[str, object]]:
    formulas: list[dict[str, object]] = []
    patterns = [
        (r"\$\$[\s\S]+?\$\$", "display_math"),
        (r"\\\[[\s\S]+?\\\]", "display_math"),
        (r"\\begin\{(?:array|[bpvVB]?matrix|cases|aligned|align|split|gathered)\}[\s\S]+?\\end\{[^}]+\}", "latex_environment"),
        (r"\$[^$\n]{2,}\$", "inline_math"),
    ]
    for pattern, formula_type in patterns:
        for match in re.finditer(pattern, text):
            formulas.append({"type": formula_type, "line": _line_for_match(text, match), "preview": _short_preview(match.group(0), 120)})
            if len(formulas) >= 80:
                return formulas
    return formulas


def _detect_code_blocks(text: str, path: Path) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for match in re.finditer(r"```([A-Za-z0-9_-]*)\n[\s\S]*?```", text):
        blocks.append({"type": "fenced_code", "language": match.group(1) or "", "line": _line_for_match(text, match)})
    if path.suffix.lower() in {".py", ".js", ".ts", ".sh", ".cpp", ".c", ".h", ".java", ".f90", ".f"}:
        blocks.insert(0, {"type": "source_file", "language": path.suffix.lower().lstrip("."), "line": 1})
    return blocks[:40]


def _detect_images(text: str, path: Path) -> list[dict[str, object]]:
    images: list[dict[str, object]] = []
    for match in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", text):
        images.append({"type": "markdown_image", "alt": match.group(1), "path": match.group(2), "line": _line_for_match(text, match), "status": "referenced"})
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        images.insert(0, {"type": "image_file", "path": path.name, "line": 1, "status": "requires_vision_analysis"})
    return images[:80]


def _detect_analysis_problems(text: str, path: Path, extraction: dict[str, str]) -> list[dict[str, object]]:
    problems: list[dict[str, object]] = []
    if extraction["text_status"] != "success":
        problems.append({"type": "unable_to_parse", "severity": "high", "message": "当前本地分析器无法提取该文件文本，需要 MinerU/OCR 或专用解析器。"})
    if path.suffix.lower() == ".pdf":
        problems.append({"type": "parser_required", "severity": "high", "message": "PDF 已上传但尚未经过 MinerU 转换，无法确认缺页、公式和图片质量。"})
    if text and len(text.strip()) < 200:
        problems.append({"type": "low_text_volume", "severity": "medium", "message": "提取文本较少，可能是扫描件、图片型文档或解析不完整。"})
    if text.count("�") > 5 or len(re.findall(r"\?{2,}", text)) > 3:
        problems.append({"type": "poor_scan_or_encoding", "severity": "medium", "message": "文本中存在较多乱码或占位符，可能影响章节和公式识别。"})
    page_mentions = [int(item) for item in re.findall(r"(?:page|第)\s*(\d{1,4})\s*(?:页)?", text, re.IGNORECASE)]
    if page_mentions and max(page_mentions) - min(page_mentions) + 1 > len(set(page_mentions)):
        problems.append({"type": "possible_missing_or_duplicate_pages", "severity": "low", "message": "页码序列存在重复或间断迹象，需要人工检查缺页。"})
    return problems


def _line_for_match(text: str, match: re.Match[str] | None) -> int:
    if match is None:
        return 1
    return text.count("\n", 0, match.start()) + 1


def _short_preview(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value).strip())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _library_analysis_path(vault_root: Path, file_id: str) -> Path:
    root = (vault_root / "library" / "analysis").resolve()
    return (root / f"{_safe_filename(file_id)}.json").resolve()


def _project_path(vault_root: Path, project_id: str) -> Path:
    root = (vault_root / "projects").resolve()
    candidate = (root / _safe_filename(project_id) / "project.json").resolve()
    if not candidate.is_file() or not candidate.is_relative_to(root):
        raise FileNotFoundError(project_id)
    return candidate


def _upsert_library_record(vault_root: Path, record: dict[str, object]) -> None:
    index_path = vault_root / "library" / "library_index.json"
    index = _read_json_if_exists(index_path, {"files": []})
    files = [item for item in index.get("files", []) if isinstance(item, dict) and item.get("id") != record.get("id")] if isinstance(index, dict) else []
    files.append(record)
    _write_json(index_path, {"files": files})


def _safe_filename(value: str) -> str:
    name = Path(str(value).replace("\\", "/")).name.strip()
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "-", name, flags=re.UNICODE).strip(".-")
    return cleaned or "file"


def _course_path(vault_root: Path, course_id: str) -> Path:
    root = (vault_root / "courses").resolve()
    candidate = (root / course_id).resolve()
    if not candidate.is_dir() or not candidate.is_relative_to(root):
        raise FileNotFoundError(course_id)
    return candidate


def _lesson_path(vault_root: Path, course_id: str, version: str, lesson_file: str) -> Path:
    lessons_dir = (_course_path(vault_root, course_id) / "versions" / version / "lessons").resolve()
    candidate = (lessons_dir / lesson_file).resolve()
    if not candidate.is_file() or not candidate.is_relative_to(lessons_dir):
        raise FileNotFoundError(lesson_file)
    return candidate


def _read_json_if_exists(path: Path, fallback):
    return _read_json(path) if path.exists() else fallback


def _course_title(course_id: str, meta: dict[str, object]) -> str:
    if meta.get("title"):
        return str(meta["title"])
    titles = {
        "numerical-analysis": "数值分析完整课程",
        "flash-user-guide": "FLASH 模拟软件教程",
        "numerical-analysis-ch6-hybrid-llm": "数值分析第六章",
    }
    return titles.get(course_id, str(meta.get("course_id") or course_id))


def _course_description(course_id: str, meta: dict[str, object]) -> str:
    if meta.get("description"):
        return str(meta["description"])
    descriptions = {
        "numerical-analysis": "覆盖数值计算导论、方程求解、线性代数、特征值、函数逼近、积分微分等完整材料。",
        "flash-user-guide": "按 learn-by-doing 方式组织的 FLASH 模拟软件上手教程。",
        "numerical-analysis-ch6-hybrid-llm": "围绕函数逼近与插值章节的碎片化学习版，包含局部补全与易错点辨析。",
    }
    return descriptions.get(course_id, f"{len(meta.get('source_files', []))} source files compiled into a course.")


def _compile_status(course_path: Path, validation: object) -> dict[str, object]:
    graph_log = _read_json_if_exists(course_path / "graph_run_log.json", [])
    last_action = graph_log[-1].get("next_action", "") if isinstance(graph_log, list) and graph_log else ""
    if (course_path / "human_review.json").exists():
        return {"state": "blocked", "label": "需要人工复核", "detail": "Human review required"}
    if isinstance(validation, dict) and validation.get("ok") is False:
        return {"state": "failed", "label": "编译需修复", "detail": "Validation failed"}
    if last_action == "done" or (isinstance(validation, dict) and validation.get("ok")):
        return {"state": "ready", "label": "可学习", "detail": "Compiled and validated"}
    return {"state": "unknown", "label": "状态未知", "detail": str(last_action or "No compile log")}


def _latest_mtime_iso(path: Path) -> str:
    mtimes = [item.stat().st_mtime for item in path.rglob("*") if item.is_file()]
    return _mtime_iso_from(max(mtimes) if mtimes else path.stat().st_mtime)


def _mtime_iso(path: Path) -> str:
    return _mtime_iso_from(path.stat().st_mtime)


def _mtime_iso_from(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _chapter_structure(outline: object, lessons: object) -> list[dict[str, object]]:
    if not isinstance(outline, dict):
        return []
    lesson_items = lessons if isinstance(lessons, list) else []
    lesson_by_id = {str(lesson.get("id", "")): lesson for lesson in lesson_items if isinstance(lesson, dict)}
    sections = []
    for section in outline.get("sections", []):
        if not isinstance(section, dict):
            continue
        ids = [str(item) for item in section.get("lesson_ids", [])]
        sections.append(
            {
                "title": section.get("title", "Course"),
                "lessons": [
                    {"id": lesson_id, "title": lesson_by_id.get(lesson_id, {}).get("title", lesson_id)}
                    for lesson_id in ids
                ],
            }
        )
    if sections:
        return sections
    return [{"title": "Course", "lessons": [{"id": item.get("id", ""), "title": item.get("title", "")} for item in lesson_items if isinstance(item, dict)]}]


def _replace_markdown_title(text: str, title: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            lines[index] = f"# {title}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return f"# {title}\n\n{text}"


def _lesson_order_prefix(filename: str) -> str:
    match = re.match(r"^(\d+)", filename)
    return match.group(1) if match else "000"


def _slugify_filename(value: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip(), flags=re.UNICODE).strip("-").lower()
    return slug or "lesson"


def _update_lessons_json_title(vault_root: Path, course_id: str, old_file: str, title: str) -> None:
    lessons_path = _course_path(vault_root, course_id) / "lessons.json"
    if not lessons_path.exists():
        return
    lessons = _read_json(lessons_path)
    order = int(_lesson_order_prefix(old_file))
    if isinstance(lessons, list):
        for lesson in lessons:
            if isinstance(lesson, dict) and int(lesson.get("order") or 0) == order:
                lesson["title"] = title
                break
        lessons_path.write_text(json.dumps(lessons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _delete_lesson_from_lessons_json(vault_root: Path, course_id: str, old_file: str) -> None:
    lessons_path = _course_path(vault_root, course_id) / "lessons.json"
    if not lessons_path.exists():
        return
    lessons = _read_json(lessons_path)
    order = int(_lesson_order_prefix(old_file))
    if isinstance(lessons, list):
        lessons = [lesson for lesson in lessons if not (isinstance(lesson, dict) and int(lesson.get("order") or 0) == order)]
        lessons_path.write_text(json.dumps(lessons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _title_from_markdown(path: Path) -> str:
    return _title_from_text(path.read_text(encoding="utf-8"))


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled"


def _version_sort_key(path: Path) -> tuple[object, ...]:
    parts: list[object] = []
    for part in re.split(r"(\d+)", path.name):
        if part.isdigit():
            parts.append(int(part))
        elif part:
            parts.append(part)
    return tuple(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), CourseRequestHandler)
    print(f"Serving http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
