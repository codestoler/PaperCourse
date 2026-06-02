"""Small stdlib HTTP server for browsing compiled courses locally."""

from __future__ import annotations

import argparse
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote


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

    def _handle_api(self) -> None:
        parts = [unquote(part) for part in self.path.split("?")[0].strip("/").split("/")]
        try:
            if parts == ["api", "courses"]:
                self._send_json({"courses": self._list_courses()})
            elif len(parts) == 4 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                self._send_json({"versions": self._list_versions(parts[2])})
            elif len(parts) == 6 and parts[:2] == ["api", "courses"] and parts[3] == "versions":
                self._send_json(self._read_lesson(parts[2], parts[4], parts[5]))
            else:
                self.send_error(404, "Unknown API route")
        except FileNotFoundError:
            self.send_error(404, "Course data not found")

    def _list_courses(self) -> list[dict[str, object]]:
        courses_dir = VAULT_ROOT / "courses"
        if not courses_dir.exists():
            return []
        courses: list[dict[str, object]] = []
        for path in sorted(item for item in courses_dir.iterdir() if item.is_dir()):
            meta_path = path / "course_meta.json"
            meta = _read_json(meta_path) if meta_path.exists() else {"course_id": path.name}
            courses.append({"id": path.name, "meta": meta})
        return courses

    def _list_versions(self, course_id: str) -> list[dict[str, object]]:
        versions_dir = VAULT_ROOT / "courses" / course_id / "versions"
        versions: list[dict[str, object]] = []
        for version_dir in sorted((item for item in versions_dir.iterdir() if item.is_dir()), key=_version_sort_key):
            lesson_dir = version_dir / "lessons"
            lessons = [
                {"file": lesson.name, "title": _title_from_markdown(lesson)}
                for lesson in sorted(lesson_dir.glob("*.md"))
            ]
            versions.append({"id": version_dir.name, "lessons": lessons})
        return versions

    def _read_lesson(self, course_id: str, version: str, lesson_file: str) -> dict[str, object]:
        lesson_path = VAULT_ROOT / "courses" / course_id / "versions" / version / "lessons" / lesson_file
        text = lesson_path.read_text(encoding="utf-8")
        return {"file": lesson_file, "title": _title_from_text(text), "markdown": text}

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


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
