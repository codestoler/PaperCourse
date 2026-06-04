from __future__ import annotations

import unittest
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from backend.server import (
    course_management_payload,
    create_course_project,
    delete_lesson_entry,
    list_course_projects,
    list_courses,
    list_library_files,
    list_project_jobs,
    project_compile_context,
    read_course_project,
    read_library_analysis,
    read_job_status,
    rename_lesson_entry,
    start_project_compile_job,
    store_library_upload,
    update_course_project,
    _version_sort_key,
)


class BackendTests(unittest.TestCase):
    def test_version_sort_key_orders_natural_versions(self) -> None:
        versions = [Path("v9-parsed-layout"), Path("v18-full-bodies"), Path("v10-llm-outline")]

        ordered = sorted(versions, key=_version_sort_key)

        self.assertEqual([item.name for item in ordered], ["v9-parsed-layout", "v10-llm-outline", "v18-full-bodies"])

    def test_course_management_payload_and_lesson_mutations(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = root / "courses" / "sample-course"
            lessons_dir = course / "versions" / "v1" / "lessons"
            lessons_dir.mkdir(parents=True)
            (course / "course_meta.json").write_text(
                json.dumps({"course_id": "sample-course", "source_files": ["raw/source.md"], "lesson_count": 2}),
                encoding="utf-8",
            )
            (course / "validation_report.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
            (course / "outline.json").write_text(
                json.dumps({"sections": [{"title": "Section", "lesson_ids": ["lesson-001", "lesson-002"]}]}),
                encoding="utf-8",
            )
            (course / "lessons.json").write_text(
                json.dumps(
                    [
                        {"id": "lesson-001", "order": 1, "title": "First"},
                        {"id": "lesson-002", "order": 2, "title": "Second"},
                    ]
                ),
                encoding="utf-8",
            )
            (lessons_dir / "001-first.md").write_text("# First\n\nBody", encoding="utf-8")
            (lessons_dir / "002-second.md").write_text("# Second\n\nBody", encoding="utf-8")

            courses = list_courses(root)
            payload = course_management_payload(root, "sample-course")
            renamed = rename_lesson_entry(root, "sample-course", "v1", "001-first.md", "Renamed Lesson")
            deleted = delete_lesson_entry(root, "sample-course", "v1", "002-second.md")
            lessons = json.loads((course / "lessons.json").read_text(encoding="utf-8"))

            self.assertEqual(courses[0]["status"]["state"], "ready")
            self.assertEqual(payload["source_files"], ["raw/source.md"])
            self.assertEqual(payload["chapter_structure"][0]["lessons"][0]["title"], "First")
            self.assertEqual(renamed["file"], "001-renamed-lesson.md")
            self.assertTrue((lessons_dir / "001-renamed-lesson.md").exists())
            self.assertFalse((lessons_dir / "002-second.md").exists())
            self.assertTrue(deleted["deleted"])
            self.assertEqual([lesson["title"] for lesson in lessons], ["Renamed Lesson"])

    def test_library_upload_analyzes_source_without_compiling_course(self) -> None:
        import tempfile

        source = (
            "# Chapter One\n\n"
            "Definition 1: Interpolation method introduces a stable way to reconstruct values from sampled data. "
            "The notes describe assumptions, data layout, and the difference between interpolation and fitting.\n\n"
            "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
            "$$x^2 + y^2 = 1$$\n\n"
            "```python\nprint('ok')\n```\n\n"
            "![diagram](images/a.png)\n"
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = store_library_upload(root, "notes.md", BytesIO(source))
            files = list_library_files(root)
            report = read_library_analysis(root, str(record["id"]))

            self.assertEqual(record["upload_status"], "success")
            self.assertEqual(record["analysis_status"], "success")
            self.assertEqual(files[0]["id"], record["id"])
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["chapter_structure"][0]["title"], "Chapter One")
            self.assertEqual(report["pipeline"][0]["step"], "file_transcoding")
            self.assertTrue(report["tables"])
            self.assertTrue(report["formulas"])
            self.assertTrue(report["code_blocks"])
            self.assertTrue(report["images"])
            self.assertFalse((root / "courses").exists())

    def test_course_project_saves_requirements_and_library_references(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = store_library_upload(root, "source.md", BytesIO(b"# Topic\n\nGrounded text."))
            project = create_course_project(
                root,
                {
                    "title": "Numerical Methods",
                    "description": "Short course",
                    "subject": "Applied Math",
                    "library_file_ids": [record["id"]],
                    "compile_requirements": {"exercise_ratio": "每节至少 2 题。"},
                },
            )
            updated = update_course_project(
                root,
                project["id"],
                {"compile_requirements": {"formula_handling": "所有重要公式使用 display math。"}},
            )
            loaded = read_course_project(root, project["id"])
            projects = list_course_projects(root)
            context = project_compile_context(root, project["id"])

            self.assertEqual(project["library_file_ids"], [record["id"]])
            self.assertIn("course_structure", project["compile_requirements"])
            self.assertEqual(project["compile_requirements"]["exercise_ratio"], "每节至少 2 题。")
            self.assertEqual(updated["compile_requirements"]["formula_handling"], "所有重要公式使用 display math。")
            self.assertIn("exercise_ratio", updated["compile_requirements"])
            self.assertEqual(loaded["id"], project["id"])
            self.assertEqual(projects[0]["id"], project["id"])
            self.assertEqual(context["source_files"][0]["id"], record["id"])
            self.assertTrue(Path(context["source_files"][0]["path"]).exists())
            self.assertIn("formula_handling", context["compile_requirements"])
            self.assertNotIn("path", loaded)
            self.assertFalse((root / "projects" / project["id"] / "source.md").exists())

    def test_project_compile_job_blocks_unparsed_pdf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = store_library_upload(root, "source.pdf", BytesIO(b"%PDF-1.4\n"))
            project = create_course_project(root, {"title": "PDF Course", "library_file_ids": [record["id"]]})

            job = start_project_compile_job(root, project["id"], {})
            loaded = read_job_status(root, str(job["id"]))

            self.assertEqual(job["state"], "blocked")
            self.assertIn("MinerU", job["error"])
            self.assertEqual(loaded["events"][-1]["status"], "blocked")

    def test_project_compile_job_queues_markdown_without_running_in_test(self) -> None:
        import tempfile

        class FakeThread:
            def __init__(self, *args, **kwargs) -> None:
                self.args = args
                self.kwargs = kwargs

            def start(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = store_library_upload(root, "source.md", BytesIO(b"# Topic\n\nGrounded text."))
            project = create_course_project(root, {"title": "Markdown Course", "library_file_ids": [record["id"]]})

            with patch("backend.server.threading.Thread", FakeThread):
                job = start_project_compile_job(root, project["id"], {})

            jobs = list_project_jobs(root, project["id"])
            loaded = read_job_status(root, str(job["id"]))
            compile_context = json.loads((root / "jobs" / job["id"] / "compile_context.json").read_text(encoding="utf-8"))

            self.assertEqual(job["state"], "queued")
            self.assertEqual(jobs[0]["id"], job["id"])
            self.assertEqual(loaded["state"], "queued")
            self.assertIn("course_structure", compile_context["compile_requirements"])
            self.assertEqual(compile_context["source_files"][0]["id"], record["id"])


if __name__ == "__main__":
    unittest.main()
