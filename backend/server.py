"""Small stdlib HTTP server for browsing compiled courses locally."""

from __future__ import annotations

import argparse
import cgi
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
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
            elif method == "GET" and len(parts) == 5 and parts[:3] == ["api", "library", "files"] and parts[4] == "parse":
                self._send_json(read_library_parse_status(VAULT_ROOT, parts[3]))
            elif method == "POST" and len(parts) == 5 and parts[:3] == ["api", "library", "files"] and parts[4] == "parse":
                self._send_json(start_library_parse_task(VAULT_ROOT, parts[3], run_async=True))
            elif method == "GET" and len(parts) == 3 and parts[:2] == ["api", "parse-jobs"]:
                self._send_json(read_parse_job_status(VAULT_ROOT, parts[2]))
            elif method == "GET" and parts == ["api", "projects"]:
                self._send_json({"projects": list_course_projects(VAULT_ROOT)})
            elif method == "POST" and parts == ["api", "projects"]:
                payload = self._read_json_body()
                self._send_json(create_course_project(VAULT_ROOT, payload))
            elif method == "GET" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
                self._send_json(read_course_project(VAULT_ROOT, parts[2]))
            elif method == "GET" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "compile-context":
                self._send_json(project_compile_context(VAULT_ROOT, parts[2]))
            elif method == "POST" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "preflight-plan":
                payload = self._read_json_body()
                self._send_json(generate_project_preflight_plan(VAULT_ROOT, parts[2], payload))
            elif method == "POST" and len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "confirm-plan":
                payload = self._read_json_body()
                self._send_json(confirm_project_preflight_plan(VAULT_ROOT, parts[2], payload))
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
            elif method == "POST" and len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                payload = self._read_json_body()
                self._send_json(control_compile_job(VAULT_ROOT, parts[2], parts[3], payload))
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
            uploaded.append(store_library_upload(VAULT_ROOT, field.filename, field.file, run_async=True))
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


PARSER_REQUIRED_SUFFIXES = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def store_library_upload(vault_root: Path, filename: str, stream, *, run_async: bool = False) -> dict[str, object]:
    """Store one uploaded source file and create a persistent parse task."""

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
        "analysis_status": "waiting_parse",
        "parse_status": "waiting_parse",
        "parse_task_id": "",
        "parsed_source_path": "",
        "uploaded_at": _mtime_iso(target),
    }
    _upsert_library_record(vault_root, record)
    record = start_library_parse_task(vault_root, file_id, run_async=run_async)["file"]
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


def read_library_parse_status(vault_root: Path, file_id: str) -> dict[str, object]:
    file_record = _library_record_by_id(vault_root, file_id)
    task_id = str(file_record.get("parse_task_id") or "")
    job = read_parse_job_status(vault_root, task_id) if task_id else {}
    return {"file": file_record, "parse_job": job}


def start_library_parse_task(vault_root: Path, file_id: str, *, run_async: bool = False) -> dict[str, object]:
    record = _library_record_by_id(vault_root, file_id)
    task_id = f"parse-{_safe_filename(file_id)}-{uuid.uuid4().hex[:10]}"
    now = _mtime_iso_from(time.time())
    job = {
        "id": task_id,
        "file_id": file_id,
        "filename": record.get("filename", ""),
        "state": "waiting_parse",
        "current_stage": "waiting_parse",
        "progress": 0,
        "error": "",
        "parsed_source_path": "",
        "analysis_report_path": "",
        "created_at": now,
        "updated_at": now,
        "started_at": "",
        "finished_at": "",
    }
    job_dir = _parse_job_dir(vault_root, task_id)
    _write_json(job_dir / "job.json", job)
    _append_job_event(job_dir, {"stage": "parse", "status": "waiting_parse", "message": "Parse task created"})
    record.update({"parse_task_id": task_id, "parse_status": "waiting_parse", "analysis_status": "waiting_parse", "updated_at": now})
    _upsert_library_record(vault_root, record)
    if run_async:
        threading.Thread(target=_run_library_parse_task, args=(vault_root, task_id), daemon=True).start()
    else:
        _run_library_parse_task(vault_root, task_id)
    return {"file": _library_record_by_id(vault_root, file_id), "parse_job": read_parse_job_status(vault_root, task_id)}


def read_parse_job_status(vault_root: Path, task_id: str) -> dict[str, object]:
    job_dir = _parse_job_dir(vault_root, task_id)
    path = job_dir / "job.json"
    if not path.exists():
        raise FileNotFoundError(task_id)
    job = _read_json(path)
    return {**job, "events": _read_job_events(job_dir)[-80:]}


def _run_library_parse_task(vault_root: Path, task_id: str) -> None:
    job_dir = _parse_job_dir(vault_root, task_id)
    job = _read_json(job_dir / "job.json")
    file_id = str(job.get("file_id", ""))
    record = _library_record_by_id(vault_root, file_id)
    source_path = (vault_root / str(record["path"])).resolve()
    now = _mtime_iso_from(time.time())
    job.update({"state": "parsing", "current_stage": "mineru_parse" if _requires_mineru(source_path) else "local_parse", "progress": 10, "started_at": now, "updated_at": now})
    _write_json(job_dir / "job.json", job)
    _append_job_event(job_dir, {"stage": job["current_stage"], "status": "started", "message": "Parsing started"})
    record.update({"parse_status": "parsing", "analysis_status": "parsing", "updated_at": now})
    _upsert_library_record(vault_root, record)
    try:
        if _requires_mineru(source_path):
            parsed_markdown = _run_mineru_parse(vault_root, record, job_dir)
        else:
            parsed_markdown = _run_local_parse(vault_root, record)
        parsed_relative = str(parsed_markdown.relative_to(vault_root))
        parsed_text = parsed_markdown.read_text(encoding="utf-8")
        report = analyze_library_file(vault_root, {**record, "parsed_source_path": parsed_relative}, text_override=parsed_text, parsed_path=parsed_markdown)
        now = _mtime_iso_from(time.time())
        job.update(
            {
                "state": "parsed",
                "current_stage": "parsed",
                "progress": 100,
                "parsed_source_path": parsed_relative,
                "analysis_report_path": str(Path("library") / "analysis" / f"{file_id}.json"),
                "finished_at": now,
                "updated_at": now,
            }
        )
        _write_json(job_dir / "job.json", job)
        _append_job_event(job_dir, {"stage": "parse", "status": "parsed", "message": "Parse completed"})
        record.update(
            {
                "parse_status": "parsed",
                "analysis_status": report["status"],
                "parsed_source_path": parsed_relative,
                "analysis_report_path": str(Path("library") / "analysis" / f"{file_id}.json"),
                "updated_at": now,
            }
        )
        _upsert_library_record(vault_root, record)
    except Exception as exc:  # pragma: no cover - external parser behavior
        message = str(exc)
        report = _failed_analysis_report(record, source_path, message)
        _write_json(_library_analysis_path(vault_root, file_id), report)
        now = _mtime_iso_from(time.time())
        job.update({"state": "parse_failed", "current_stage": "parse_failed", "progress": 100, "error": message, "finished_at": now, "updated_at": now})
        _write_json(job_dir / "job.json", job)
        _append_job_event(job_dir, {"stage": "parse", "status": "parse_failed", "error": message})
        record.update({"parse_status": "parse_failed", "analysis_status": "failed", "analysis_report_path": str(Path("library") / "analysis" / f"{file_id}.json"), "updated_at": now})
        _upsert_library_record(vault_root, record)


def _run_local_parse(vault_root: Path, record: dict[str, object]) -> Path:
    source_path = (vault_root / str(record["path"])).resolve()
    text, _ = _extract_text_for_analysis(source_path)
    if not text.strip():
        raise RuntimeError("Local parser could not extract text from uploaded file")
    parsed_dir = _parsed_library_dir(vault_root, str(record["id"]))
    parsed_dir.mkdir(parents=True, exist_ok=True)
    parsed_markdown = parsed_dir / "content.md"
    parsed_markdown.write_text(text, encoding="utf-8")
    _write_json(parsed_dir / "blocks.json", _text_blocks_from_markdown(text, str(record["id"])))
    (parsed_dir / "parse.log").write_text("local_parse=success\n", encoding="utf-8")
    return parsed_markdown


def _run_mineru_parse(vault_root: Path, record: dict[str, object], job_dir: Path) -> Path:
    source_path = (vault_root / str(record["path"])).resolve()
    parsed_dir = _parsed_library_dir(vault_root, str(record["id"]))
    parsed_dir.mkdir(parents=True, exist_ok=True)
    command_template = os.environ.get("PAPERCOURSE_MINERU_COMMAND", "").strip()
    if command_template:
        command = command_template.format(input=str(source_path), output=str(parsed_dir), progress=str(job_dir / "events.jsonl"))
        shell = True
    elif source_path.suffix.lower() == ".pdf":
        command = [
            sys.executable,
            str(ROOT / "scripts" / "mineru_pdf_to_md.py"),
            "--input-dir",
            str(source_path.parent),
            "--output-dir",
            str(parsed_dir),
            "--progress-jsonl",
            str(job_dir / "events.jsonl"),
        ]
        shell = False
    else:
        raise RuntimeError(f"MinerU parser command is not configured for {source_path.suffix.lower() or 'unknown'} files")
    _append_job_event(job_dir, {"stage": "mineru_parse", "status": "running", "message": "MinerU command started"})
    result = subprocess.run(command, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=shell, timeout=7200)
    (parsed_dir / "parse.log").write_text(result.stdout or "", encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"MinerU parse failed with code {result.returncode}: {_short_preview(result.stdout or '', 500)}")
    markdowns = sorted(path for path in parsed_dir.rglob("*.md") if path.is_file())
    if not markdowns:
        raise RuntimeError("MinerU parse completed but no Markdown file was produced")
    parsed_markdown = parsed_dir / "content.md"
    if markdowns[0] != parsed_markdown:
        parsed_markdown.write_text(markdowns[0].read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(parsed_dir / "blocks.json", _text_blocks_from_markdown(parsed_markdown.read_text(encoding="utf-8"), str(record["id"])))
    return parsed_markdown


def analyze_library_file(vault_root: Path, record: dict[str, object], *, text_override: str | None = None, parsed_path: Path | None = None) -> dict[str, object]:
    path = (parsed_path or (vault_root / str(record.get("parsed_source_path") or record["path"]))).resolve()
    if not path.is_file() or not path.is_relative_to(vault_root.resolve()):
        raise FileNotFoundError(str(record.get("id", "")))
    if text_override is None:
        text, extraction = _extract_text_for_analysis(path)
    else:
        text, extraction = text_override, {"transcoding_status": "success", "text_status": "success", "encoding": "parsed_markdown"}
    report = {
        "file_id": record["id"],
        "filename": record["filename"],
        "status": "success" if text.strip() else "warning",
        "parse_status": record.get("parse_status", ""),
        "parsed_source_path": record.get("parsed_source_path", ""),
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
        "text_blocks": _text_blocks_from_markdown(text, str(record["id"]))[:160],
        "tables": _detect_tables(text, path),
        "formulas": _detect_formulas(text),
        "code_blocks": _detect_code_blocks(text, path),
        "images": _detect_images(text, path),
        "parse_logs": _read_parse_logs(vault_root, record),
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


PROJECT_STATUS_LABELS = {
    "not_started": "未开始",
    "queued": "排队中",
    "analyzing": "分析中",
    "awaiting_confirmation": "待确认",
    "compiling": "编译中",
    "succeeded": "成功",
    "failed": "失败",
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
        "status": "not_started",
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
        project.pop("confirmed_compile_snapshot", None)
    if "compile_requirements" in payload:
        project["compile_requirements"] = {**default_compile_requirements(), **dict(payload.get("compile_requirements", {}) or {})}
        project.pop("confirmed_compile_snapshot", None)
    if "library_file_ids" in payload or "compile_requirements" in payload:
        project["status"] = "not_started"
    project["updated_at"] = _mtime_iso_from(__import__("time").time())
    _write_json(path, project)
    return project


def project_compile_context(vault_root: Path, project_id: str, snapshot: dict[str, object] | None = None) -> dict[str, object]:
    """Return the saved project config plus library file paths for downstream compile tasks."""

    project = read_course_project(vault_root, project_id)
    source_ids = [str(item) for item in (snapshot or project).get("library_file_ids", []) if str(item).strip()]
    requirements = dict((snapshot or project).get("compile_requirements", default_compile_requirements()) or {})
    files_by_id = {str(item.get("id")): item for item in list_library_files(vault_root)}
    source_files = []
    missing_files = []
    for file_id in source_ids:
        record = files_by_id.get(str(file_id))
        if not record:
            missing_files.append(str(file_id))
            continue
        compile_path = str(record.get("parsed_source_path") or record["path"])
        source_files.append(
            {
                "id": record["id"],
                "filename": record["filename"],
                "path": str((vault_root / compile_path).resolve()),
                "original_path": str((vault_root / str(record["path"])).resolve()),
                "parsed_source_path": str(record.get("parsed_source_path", "")),
                "parse_status": record.get("parse_status", record.get("analysis_status", "unknown")),
                "parse_task_id": record.get("parse_task_id", ""),
                "analysis_report_path": str((vault_root / str(record.get("analysis_report_path", ""))).resolve()) if record.get("analysis_report_path") else "",
                "analysis_status": record.get("analysis_status", "unknown"),
            }
        )
    context = {
        "project": project,
        "source_files": source_files,
        "missing_library_file_ids": missing_files,
        "compile_requirements": {**default_compile_requirements(), **requirements},
    }
    if snapshot:
        context["confirmed_compile_snapshot"] = snapshot
        if snapshot.get("preflight_plan"):
            context["preflight_plan"] = snapshot["preflight_plan"]
    return context


def generate_project_preflight_plan(vault_root: Path, project_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    """Create a pre-compile plan from the current or supplied project source scope."""

    payload = payload or {}
    path = _project_path(vault_root, project_id)
    project = _read_json(path)
    now = _mtime_iso_from(time.time())
    project["status"] = "analyzing"
    project["updated_at"] = now
    _write_json(path, project)

    source_ids = [str(item) for item in payload.get("library_file_ids", project.get("library_file_ids", [])) if str(item).strip()]
    requirements = {**default_compile_requirements(), **dict(payload.get("compile_requirements", project.get("compile_requirements", {})) or {})}
    snapshot_basis = {"library_file_ids": source_ids, "compile_requirements": requirements}
    context = project_compile_context(vault_root, project_id, snapshot_basis)
    if context["missing_library_file_ids"]:
        raise ValueError("Project references missing library files")
    source_reports = [_preflight_source_report(vault_root, item) for item in context["source_files"]]
    outline = _preflight_outline(source_reports)
    estimated_lessons = _estimate_lesson_count(source_reports)
    schemes = _preflight_schemes(estimated_lessons)
    risks = _preflight_risks(context, source_reports)
    plan_id = f"plan-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    signature = _stable_signature({"source_ids": source_ids, "requirements": requirements, "schemes": schemes})
    plan = {
        "id": plan_id,
        "project_id": project_id,
        "status": "awaiting_confirmation",
        "created_at": now,
        "source_scope": {
            "library_file_ids": source_ids,
            "source_count": len(source_ids),
            "sources": source_reports,
        },
        "compile_requirements": requirements,
        "preliminary_outline": outline,
        "estimated_lesson_count": estimated_lessons,
        "estimated_study_minutes": estimated_lessons * 18,
        "estimated_token_cost": _estimate_token_cost(source_reports, estimated_lessons),
        "risks": risks,
        "schemes": schemes,
        "default_scheme_id": "systematic",
        "signature": signature,
    }
    plan_path = _preflight_plan_path(vault_root, project_id, plan_id)
    _write_json(plan_path, plan)
    project["status"] = "awaiting_confirmation"
    project["latest_preflight_plan_id"] = plan_id
    project["latest_preflight_plan_path"] = str(plan_path.relative_to(vault_root))
    project.pop("confirmed_compile_snapshot", None)
    project["updated_at"] = _mtime_iso_from(time.time())
    _write_json(path, project)
    return {"project": project, "plan": plan}


def confirm_project_preflight_plan(vault_root: Path, project_id: str, payload: dict[str, object]) -> dict[str, object]:
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        raise ValueError("plan_id is required")
    plan = _read_json(_preflight_plan_path(vault_root, project_id, plan_id))
    selected_scheme_id = str(payload.get("selected_scheme_id") or plan.get("default_scheme_id") or "systematic")
    schemes = [item for item in plan.get("schemes", []) if isinstance(item, dict)]
    selected_scheme = next((item for item in schemes if item.get("id") == selected_scheme_id), None)
    if selected_scheme is None:
        raise ValueError("Unknown compile scheme")
    snapshot = {
        "confirmed_at": _mtime_iso_from(time.time()),
        "plan_id": plan["id"],
        "plan_signature": plan["signature"],
        "library_file_ids": list(plan.get("source_scope", {}).get("library_file_ids", [])),
        "compile_requirements": plan.get("compile_requirements", default_compile_requirements()),
        "selected_scheme_id": selected_scheme_id,
        "selected_scheme": selected_scheme,
        "preflight_plan": plan,
    }
    path = _project_path(vault_root, project_id)
    project = _read_json(path)
    project["confirmed_compile_snapshot"] = snapshot
    project["status"] = "not_started"
    project["updated_at"] = _mtime_iso_from(time.time())
    _write_json(path, project)
    return {"project": project, "confirmed_compile_snapshot": snapshot}


def _preflight_source_report(vault_root: Path, source_file: dict[str, object]) -> dict[str, object]:
    analysis_path = str(source_file.get("analysis_report_path") or "")
    report = _read_json(Path(analysis_path)) if analysis_path and Path(analysis_path).exists() else {}
    document = report.get("document", {}) if isinstance(report, dict) else {}
    chapters = report.get("chapter_structure", []) if isinstance(report, dict) else []
    problems = report.get("potential_problems", []) if isinstance(report, dict) else []
    return {
        "id": source_file.get("id", ""),
        "filename": source_file.get("filename", ""),
        "analysis_status": source_file.get("analysis_status", "unknown"),
        "source_type": document.get("source_type", ""),
        "character_count": int(document.get("character_count") or 0),
        "line_count": int(document.get("line_count") or 0),
        "chapter_count": len(chapters),
        "chapters": chapters[:12],
        "knowledge_point_count": len(report.get("knowledge_points", [])) if isinstance(report, dict) else 0,
        "table_count": len(report.get("tables", [])) if isinstance(report, dict) else 0,
        "formula_count": len(report.get("formulas", [])) if isinstance(report, dict) else 0,
        "image_count": len(report.get("images", [])) if isinstance(report, dict) else 0,
        "problems": problems,
    }


def _preflight_outline(source_reports: list[dict[str, object]]) -> list[dict[str, object]]:
    outline = []
    for source in source_reports:
        chapters = [
            {"title": str(item.get("title", "")), "level": int(item.get("level") or 1), "source_id": source.get("id", "")}
            for item in source.get("chapters", [])
            if isinstance(item, dict) and str(item.get("title", "")).strip()
        ]
        outline.append(
            {
                "source_id": source.get("id", ""),
                "title": source.get("filename", ""),
                "chapters": chapters or [{"title": str(source.get("filename", "资料概览")), "level": 1, "source_id": source.get("id", "")}],
            }
        )
    return outline


def _estimate_lesson_count(source_reports: list[dict[str, object]]) -> int:
    total_chars = sum(int(item.get("character_count") or 0) for item in source_reports)
    chapter_count = sum(int(item.get("chapter_count") or 0) for item in source_reports)
    by_chars = max(1, round(total_chars / 2600)) if total_chars else max(1, len(source_reports))
    return max(1, min(80, max(chapter_count, by_chars)))


def _estimate_token_cost(source_reports: list[dict[str, object]], lesson_count: int) -> dict[str, object]:
    input_chars = sum(int(item.get("character_count") or 0) for item in source_reports)
    input_tokens = round(input_chars / 2.6)
    output_tokens = lesson_count * 1100
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "basis": "本地按字符数和预计章节数粗估，实际消耗以运行日志为准。",
    }


def _preflight_risks(context: dict[str, object], source_reports: list[dict[str, object]]) -> list[dict[str, object]]:
    risks: list[dict[str, object]] = []
    for missing in context.get("missing_library_file_ids", []):
        risks.append({"severity": "high", "type": "missing_source", "message": f"资料库缺少引用资料 {missing}。"})
    for source in source_reports:
        filename = str(source.get("filename") or source.get("id") or "source")
        if source.get("analysis_status") not in {"success", "warning"}:
            risks.append({"severity": "high", "type": "analysis_failed", "message": f"{filename} 尚未成功解析。"})
        if source.get("analysis_status") == "warning":
            risks.append({"severity": "medium", "type": "analysis_warning", "message": f"{filename} 的资料分析存在警告。"})
        if int(source.get("character_count") or 0) > 180000:
            risks.append({"severity": "medium", "type": "large_source", "message": f"{filename} 较长，编译会依赖 source index 和分批生成。"})
        for problem in source.get("problems", []):
            if isinstance(problem, dict):
                risks.append(
                    {
                        "severity": str(problem.get("severity") or "medium"),
                        "type": str(problem.get("type") or "analysis_problem"),
                        "message": f"{filename}: {problem.get('message') or problem.get('type')}",
                    }
                )
    if not risks:
        risks.append({"severity": "low", "type": "normal", "message": "未发现阻塞性风险；仍需在编译后查看 validation 报告。"})
    return risks


def _preflight_schemes(estimated_lessons: int) -> list[dict[str, object]]:
    quick = max(3, round(estimated_lessons * 0.45))
    systematic = max(estimated_lessons, quick)
    exercise = max(systematic, round(estimated_lessons * 1.15))
    research = max(systematic, round(estimated_lessons * 0.85))
    return [
        {
            "id": "quick_review",
            "title": "快速复习版",
            "summary": "压缩为高频知识点、关键公式和复习 checklist。",
            "target_lesson_count": quick,
            "estimated_study_minutes": quick * 10,
            "compile_profile_overrides": {"target_lesson_count": quick, "lesson_body_target_chars": 1800, "lesson_body_max_chars": 3600},
        },
        {
            "id": "systematic",
            "title": "系统学习版",
            "summary": "按资料结构和学习目标生成中等粒度完整课程。",
            "target_lesson_count": systematic,
            "estimated_study_minutes": systematic * 18,
            "compile_profile_overrides": {"target_lesson_count": systematic, "lesson_body_target_chars": 3500, "lesson_body_max_chars": 7200},
        },
        {
            "id": "exercise",
            "title": "习题强化版",
            "summary": "增加检查项、例题讲解和易错点辨析。",
            "target_lesson_count": exercise,
            "estimated_study_minutes": exercise * 22,
            "compile_profile_overrides": {"target_lesson_count": exercise, "lesson_body_target_chars": 3900, "lesson_body_max_chars": 7600, "lesson_body_enrichment": "constrained"},
        },
        {
            "id": "research",
            "title": "研究导向版",
            "summary": "突出定义、假设、推导脉络、证据缺口和可追溯引用。",
            "target_lesson_count": research,
            "estimated_study_minutes": research * 24,
            "compile_profile_overrides": {"target_lesson_count": research, "lesson_body_target_chars": 4200, "lesson_body_max_chars": 8000},
        },
    ]


def _compile_args_from_confirmed_snapshot(snapshot: dict[str, object]) -> list[str]:
    scheme = snapshot.get("selected_scheme", {}) if isinstance(snapshot.get("selected_scheme"), dict) else {}
    overrides = scheme.get("compile_profile_overrides", {}) if isinstance(scheme.get("compile_profile_overrides"), dict) else {}
    args: list[str] = []
    option_map = {
        "target_lesson_count": "--target-lesson-count",
        "lesson_body_target_chars": "--lesson-body-target-chars",
        "lesson_body_max_chars": "--lesson-body-max-chars",
        "lesson_body_enrichment": "--lesson-body-enrichment",
    }
    for key, option in option_map.items():
        if overrides.get(key) not in (None, ""):
            args.extend([option, str(overrides[key])])
    return args


def _build_compile_command(
    vault_root: Path,
    sources: list[Path],
    course_id: str,
    version: str,
    job_dir: Path,
    snapshot: dict[str, object],
    *,
    rerun_from_node: str = "",
) -> list[str]:
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
        "--compile-context",
        str(job_dir / "compile_context.json"),
    ]
    command.extend(_compile_args_from_confirmed_snapshot(snapshot))
    command.extend(_refresh_args_from_node(rerun_from_node))
    return command


def _refresh_args_from_node(node: str) -> list[str]:
    order = [
        ("build_source_index", ["--refresh-source-index"]),
        ("synthesize_source_brief", ["--refresh-source-brief"]),
        ("plan_course", ["--refresh-llm-plan"]),
        ("synthesize_lesson_notes", ["--refresh-lesson-notes"]),
        ("synthesize_lesson_bodies", ["--refresh-lesson-bodies"]),
        ("understand_images", ["--refresh-image-vision"]),
        ("formula_image_recognition", ["--refresh-formula-image-recognition"]),
    ]
    if not node:
        return []
    aliases = {
        "parse_sources": "build_source_index",
        "extract_units": "plan_course",
        "organize_logic": "plan_course",
        "detect_gaps": "plan_course",
        "generate_lessons": "plan_course",
        "synthesize_compile_plan": "plan_course",
        "review_compile_plan_llm": "plan_course",
        "check_markdown_syntax": "synthesize_lesson_bodies",
        "check_grounding_rules": "synthesize_lesson_bodies",
        "check_quality_rules": "synthesize_lesson_bodies",
        "export_version": "synthesize_lesson_bodies",
    }
    normalized = aliases.get(node, node)
    try:
        start = [item[0] for item in order].index(normalized)
    except ValueError:
        return []
    args: list[str] = []
    for _, flags in order[start:]:
        args.extend(flags)
    return args


def control_compile_job(vault_root: Path, job_id: str, action: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = payload or {}
    job_dir = _job_dir(vault_root, job_id)
    job_path = job_dir / "job.json"
    if not job_path.exists():
        raise FileNotFoundError(job_id)
    job = _read_json(job_path)
    action = action.replace("_", "-")
    if action == "pause":
        _signal_job_process(job, signal.SIGSTOP)
        job.update({"state": "paused", "updated_at": _mtime_iso_from(time.time())})
        _write_json(job_path, job)
        _append_job_event(job_dir, {"stage": "control", "status": "paused", "message": "Compile process paused"})
        return read_job_status(vault_root, job_id)
    if action in {"resume", "continue"}:
        _signal_job_process(job, signal.SIGCONT)
        job.update({"state": "running", "updated_at": _mtime_iso_from(time.time())})
        _write_json(job_path, job)
        _append_job_event(job_dir, {"stage": "control", "status": "resumed", "message": "Compile process resumed"})
        return read_job_status(vault_root, job_id)
    if action in {"terminate", "stop"}:
        _signal_job_process(job, signal.SIGTERM)
        job.update({"state": "terminating", "current_stage": "terminating", "updated_at": _mtime_iso_from(time.time())})
        _write_json(job_path, job)
        _append_job_event(job_dir, {"stage": "control", "status": "terminating", "message": "Compile process termination requested"})
        return read_job_status(vault_root, job_id)
    if action in {"rerun-current", "rerun-from-node", "clear-results-rerun"}:
        node = str(payload.get("node") or job.get("current_stage") or "")
        clear = action == "clear-results-rerun" or bool(payload.get("clear_results"))
        return _rerun_compile_job(vault_root, job, rerun_from_node=node, clear_results=clear)
    raise ValueError("Unknown job control action")


def _signal_job_process(job: dict[str, object], sig: int) -> None:
    pid = int(job.get("pid") or 0)
    if pid <= 0:
        raise ValueError("Job has no running process id")
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        raise ValueError("Compile process is no longer running") from None


def _rerun_compile_job(vault_root: Path, previous_job: dict[str, object], *, rerun_from_node: str, clear_results: bool) -> dict[str, object]:
    project_id = str(previous_job.get("project_id") or "")
    context_path = _job_dir(vault_root, str(previous_job["id"])) / "compile_context.json"
    if not context_path.exists():
        raise FileNotFoundError("compile_context.json")
    context = _read_json(context_path)
    snapshot = context.get("confirmed_compile_snapshot", {}) if isinstance(context, dict) else {}
    sources = [Path(str(item["path"])) for item in context.get("source_files", []) if isinstance(item, dict)]
    version = str(previous_job.get("version") or f"v{time.strftime('%Y%m%d%H%M%S')}")
    course_id = str(previous_job.get("course_id") or _safe_filename(project_id))
    if clear_results:
        shutil.rmtree(vault_root / "courses" / course_id, ignore_errors=True)
    job_id = f"{_safe_filename(project_id)}-{uuid.uuid4().hex[:12]}"
    job_dir = _job_dir(vault_root, job_id)
    job = {
        "id": job_id,
        "project_id": project_id,
        "course_id": course_id,
        "version": version,
        "state": "queued",
        "current_stage": "queued",
        "progress": 0,
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "error": "",
        "command": [],
        "compile_requirements": previous_job.get("compile_requirements", default_compile_requirements()),
        "confirmed_plan_id": previous_job.get("confirmed_plan_id", ""),
        "selected_scheme_id": previous_job.get("selected_scheme_id", ""),
        "rerun": {"from_node": rerun_from_node, "clear_results": clear_results, "previous_job_id": previous_job.get("id", "")},
        "created_at": _mtime_iso_from(time.time()),
        "updated_at": _mtime_iso_from(time.time()),
    }
    _write_json(job_dir / "compile_context.json", context)
    command = _build_compile_command(vault_root, sources, course_id, version, job_dir, snapshot, rerun_from_node=rerun_from_node)
    job["command"] = command
    _write_json(job_dir / "job.json", job)
    _append_job_event(job_dir, {"stage": "control", "status": "rerun_queued", "message": f"Rerun from {rerun_from_node or 'start'}"})
    _set_project_status(vault_root, project_id, "queued", {"last_job_id": job_id, "last_compile_version": version})
    threading.Thread(target=_run_compile_job, args=(vault_root, job_id, command), daemon=True).start()
    return _job_response(job)


def _preflight_plan_path(vault_root: Path, project_id: str, plan_id: str) -> Path:
    root = (vault_root / "projects" / _safe_filename(project_id) / "preflight_plans").resolve()
    candidate = (root / f"{_safe_filename(plan_id)}.json").resolve()
    if root.exists() and not candidate.is_relative_to(root):
        raise FileNotFoundError(plan_id)
    return candidate


def _stable_signature(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def start_project_compile_job(vault_root: Path, project_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    project = read_course_project(vault_root, project_id)
    snapshot = project.get("confirmed_compile_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Compile requires a confirmed source scope and preflight plan")
    requested_plan_id = str((payload or {}).get("plan_id") or snapshot.get("plan_id") or "")
    if requested_plan_id != str(snapshot.get("plan_id") or ""):
        raise ValueError("Confirmed compile plan does not match the requested plan")
    context = project_compile_context(vault_root, project_id, snapshot)
    project = context["project"]
    sources = [Path(str(item["path"])) for item in context["source_files"]]
    if context["missing_library_file_ids"]:
        raise ValueError("Project references missing library files")
    if not sources:
        raise ValueError("Project has no source files")
    blocked_sources = [
        str(item.get("filename") or Path(str(item.get("original_path") or item.get("path", ""))).name)
        for item in context["source_files"]
        if str(item.get("parse_status") or "") != "parsed"
    ]
    job_id = f"{_safe_filename(project_id)}-{uuid.uuid4().hex[:12]}"
    version = str((payload or {}).get("version") or f"v{time.strftime('%Y%m%d%H%M%S')}")
    course_id = _safe_filename(str(project.get("id") or project_id))
    job_dir = _job_dir(vault_root, job_id)
    job = {
        "id": job_id,
        "project_id": project_id,
        "course_id": course_id,
        "version": version,
        "state": "blocked" if blocked_sources else "queued",
        "current_stage": "queued",
        "progress": 0,
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "error": f"Sources require completed parsing before compile: {', '.join(blocked_sources)}" if blocked_sources else "",
        "command": [],
        "compile_requirements": context.get("compile_requirements", default_compile_requirements()),
        "confirmed_plan_id": snapshot.get("plan_id", ""),
        "selected_scheme_id": snapshot.get("selected_scheme_id", ""),
        "created_at": _mtime_iso_from(time.time()),
        "updated_at": _mtime_iso_from(time.time()),
    }
    _write_json(job_dir / "compile_context.json", context)
    _write_json(job_dir / "job.json", job)
    if blocked_sources:
        _append_job_event(job_dir, {"stage": "prepare", "status": "blocked", "message": job["error"]})
        _set_project_status(vault_root, project_id, "failed", {"last_job_id": job_id, "last_compile_version": version})
        return _job_response(job)

    command = _build_compile_command(vault_root, sources, course_id, version, job_dir, snapshot)
    job["command"] = command
    _write_json(job_dir / "job.json", job)
    _set_project_status(vault_root, project_id, "queued", {"last_job_id": job_id, "last_compile_version": version})
    thread = threading.Thread(target=_run_compile_job, args=(vault_root, job_id, command), daemon=True)
    thread.start()
    return _job_response(job)


def _run_compile_job(vault_root: Path, job_id: str, command: list[str]) -> None:
    job_dir = _job_dir(vault_root, job_id)
    job = _read_json(job_dir / "job.json")
    job.update({"state": "running", "current_stage": "starting", "started_at": _mtime_iso_from(time.time()), "updated_at": _mtime_iso_from(time.time())})
    _write_json(job_dir / "job.json", job)
    _set_project_status(vault_root, str(job.get("project_id", "")), "compiling", {"last_job_id": job_id, "last_compile_version": job.get("version", "")})
    _append_job_event(job_dir, {"stage": "job", "status": "started", "message": "Compile process started"})
    try:
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        job = _read_json(job_dir / "job.json")
        job["pid"] = process.pid
        job["updated_at"] = _mtime_iso_from(time.time())
        _write_json(job_dir / "job.json", job)
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.strip()
            if stripped:
                _append_job_event(job_dir, {"stage": "subprocess", "status": "output", "message": stripped})
        exit_code = process.wait()
        job = _read_json(job_dir / "job.json")
        was_terminated = job.get("state") in {"terminating", "terminated"} or exit_code < 0
        job.update(
            {
                "state": "terminated" if was_terminated else "done" if exit_code == 0 else "failed",
                "current_stage": "terminated" if was_terminated else "done" if exit_code == 0 else "failed",
                "progress": 100 if exit_code == 0 else _job_progress_from_events(job_dir),
                "finished_at": _mtime_iso_from(time.time()),
                "updated_at": _mtime_iso_from(time.time()),
                "exit_code": exit_code,
                "pid": 0,
            }
        )
        if was_terminated:
            job["error"] = "Compile process was terminated"
        elif exit_code != 0:
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
    if job.get("state") in {"running", "paused", "terminating"}:
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
        "confirmed_plan_id": job.get("confirmed_plan_id", ""),
        "selected_scheme_id": job.get("selected_scheme_id", ""),
        "pid": job.get("pid", 0),
        "rerun": job.get("rerun", {}),
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
    state = "succeeded" if job.get("state") == "done" else "failed"
    _set_project_status(
        vault_root,
        project_id,
        state,
        {"last_job_id": job.get("id", ""), "last_compile_version": job.get("version", "")},
    )


def _set_project_status(vault_root: Path, project_id: str, status: str, extra: dict[str, object] | None = None) -> None:
    try:
        path = _project_path(vault_root, project_id)
    except FileNotFoundError:
        return
    project = _read_json(path)
    project["status"] = status if status in PROJECT_STATUS_LABELS else "failed"
    if extra:
        project.update(extra)
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


def _parse_job_dir(vault_root: Path, task_id: str) -> Path:
    return vault_root / "parse-jobs" / _safe_filename(task_id)


def _parsed_library_dir(vault_root: Path, file_id: str) -> Path:
    return vault_root / "parsed" / "library" / _safe_filename(file_id)


def _library_record_by_id(vault_root: Path, file_id: str) -> dict[str, object]:
    for record in list_library_files(vault_root):
        if str(record.get("id")) == str(file_id):
            return dict(record)
    raise FileNotFoundError(file_id)


def _requires_mineru(path: Path) -> bool:
    return path.suffix.lower() in PARSER_REQUIRED_SUFFIXES


def _text_blocks_from_markdown(text: str, file_id: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    title = ""
    page = 1
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        page_match = re.search(r"(?:page|第)\s*(\d{1,4})\s*(?:页)?", stripped, re.IGNORECASE)
        if page_match:
            page = int(page_match.group(1))
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            title = heading.group(2).strip()
        block_type = "heading" if heading else "table" if "|" in stripped else "formula" if "$" in stripped or "\\begin{" in stripped else "text"
        blocks.append(
            {
                "id": f"{file_id}-block-{len(blocks) + 1:04d}",
                "page": page,
                "line": line_no,
                "title": title,
                "type": block_type,
                "text": stripped[:1000],
            }
        )
    return blocks


def _read_parse_logs(vault_root: Path, record: dict[str, object]) -> list[str]:
    task_id = str(record.get("parse_task_id") or "")
    logs: list[str] = []
    if task_id:
        for event in _read_job_events(_parse_job_dir(vault_root, task_id))[-40:]:
            message = event.get("message") or event.get("error") or event.get("status")
            if message:
                logs.append(f"{event.get('timestamp', '')} {event.get('stage', '')}: {message}")
    parsed_path = str(record.get("parsed_source_path") or "")
    if parsed_path:
        parse_log = (vault_root / parsed_path).parent / "parse.log"
        if parse_log.exists():
            logs.extend(parse_log.read_text(encoding="utf-8").splitlines()[-40:])
    return logs[-80:]


def _failed_analysis_report(record: dict[str, object], path: Path, message: str) -> dict[str, object]:
    return {
        "file_id": record.get("id", ""),
        "filename": record.get("filename", path.name),
        "status": "failed",
        "parse_status": "parse_failed",
        "parsed_source_path": record.get("parsed_source_path", ""),
        "pipeline": [
            {"step": "file_upload", "status": record.get("upload_status", "success"), "detail": str(record.get("path", ""))},
            {"step": "mineru_parse", "status": "failed", "detail": message},
        ],
        "document": {"line_count": 0, "character_count": 0, "source_type": path.suffix.lower().lstrip(".") or "unknown"},
        "chapter_structure": [],
        "knowledge_points": [],
        "text_blocks": [],
        "tables": [],
        "formulas": [],
        "code_blocks": [],
        "images": _detect_images("", path),
        "parse_logs": [message],
        "potential_problems": [{"type": "parser_failed", "severity": "high", "message": message}],
    }


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
