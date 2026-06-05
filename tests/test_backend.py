from __future__ import annotations

import unittest
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from backend.server import (
    course_management_payload,
    create_course_project,
    confirm_project_preflight_plan,
    control_compile_job,
    delete_lesson_entry,
    generate_project_preflight_plan,
    list_job_node_results,
    list_course_projects,
    list_courses,
    list_library_files,
    list_versions,
    list_project_jobs,
    project_compile_context,
    read_course_project,
    read_job_node_result,
    read_library_analysis,
    read_library_parse_status,
    read_parse_job_status,
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

    def test_course_without_versions_is_listed_as_empty_draft(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = root / "courses" / "draft-course"
            course.mkdir(parents=True)
            (course / "course_meta.json").write_text(json.dumps({"course_id": "draft-course"}), encoding="utf-8")

            courses = list_courses(root)
            versions = list_versions(root, "draft-course")

            self.assertEqual(versions, [])
            self.assertEqual(courses[0]["id"], "draft-course")
            self.assertEqual(courses[0]["version_count"], 0)
            self.assertEqual(courses[0]["latest_version"], "")

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
            self.assertEqual(record["parse_status"], "parsed")
            self.assertEqual(record["analysis_status"], "success")
            self.assertTrue(record["parse_task_id"])
            self.assertTrue((root / str(record["parsed_source_path"])).exists())
            self.assertEqual(files[0]["id"], record["id"])
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["parse_status"], "parsed")
            self.assertTrue(report["text_blocks"])
            self.assertEqual(report["chapter_structure"][0]["title"], "Chapter One")
            self.assertEqual(report["pipeline"][0]["step"], "file_transcoding")
            self.assertTrue(report["tables"])
            self.assertTrue(report["formulas"])
            self.assertTrue(report["code_blocks"])
            self.assertTrue(report["images"])
            parse_status = read_library_parse_status(root, str(record["id"]))
            parse_job = read_parse_job_status(root, str(record["parse_task_id"]))
            self.assertEqual(parse_status["parse_job"]["state"], "parsed")
            self.assertEqual(parse_job["parsed_source_path"], record["parsed_source_path"])
            self.assertFalse((root / "courses").exists())

    def test_library_file_list_includes_parse_progress_and_compile_readiness(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            source_dir = library_dir / "files" / "source-1"
            source_dir.mkdir(parents=True)
            (source_dir / "source.pdf").write_bytes(b"%PDF-1.4\n")
            (library_dir / "library_index.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "id": "source-1",
                                "filename": "source.pdf",
                                "path": "library/files/source-1/source.pdf",
                                "size": 9,
                                "analysis_status": "parsing",
                                "parse_status": "parsing",
                                "parse_task_id": "parse-1",
                                "parsed_source_path": "",
                                "uploaded_at": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job_dir = root / "parse-jobs" / "parse-1"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": "parse-1",
                        "file_id": "source-1",
                        "state": "parsing",
                        "current_stage": "mineru_parse",
                        "progress": 10,
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "t1",
                        "stage": "mineru_poll",
                        "status": "running",
                        "states": [{"file_name": "source.pdf", "state": "running"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            file = list_library_files(root)[0]
            parse_job = read_parse_job_status(root, "parse-1")

            self.assertEqual(file["parse_current_stage"], "mineru_poll")
            self.assertGreaterEqual(file["parse_progress"], 20)
            self.assertFalse(file["can_compile"])
            self.assertEqual(parse_job["current_stage"], "mineru_poll")

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
            self.assertEqual(loaded["status"], "not_started")
            self.assertFalse((root / "projects" / project["id"] / "source.md").exists())

    def test_compile_context_treats_legacy_text_analysis_as_parsed(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "library" / "files" / "legacy" / "legacy.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Legacy\n\nGrounded text.", encoding="utf-8")
            (root / "library").mkdir(exist_ok=True)
            (root / "library" / "library_index.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "id": "legacy",
                                "filename": "legacy.md",
                                "path": "library/files/legacy/legacy.md",
                                "analysis_status": "success",
                                "upload_status": "success",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            project = create_course_project(root, {"title": "Legacy Course", "library_file_ids": ["legacy"]})

            context = project_compile_context(root, project["id"])

            self.assertEqual(context["source_files"][0]["parse_status"], "parsed")
            self.assertTrue(context["source_files"][0]["path"].endswith("legacy.md"))

    def test_project_preflight_plan_and_confirmation_snapshot(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = store_library_upload(root, "first.md", BytesIO(b"# First\n\nGrounded text one."))
            second = store_library_upload(root, "second.md", BytesIO(b"# Second\n\nGrounded text two."))
            project = create_course_project(
                root,
                {
                    "title": "Confirmed Course",
                    "library_file_ids": [first["id"], second["id"]],
                    "compile_requirements": {"exercise_ratio": "每节 2 个检查项。"},
                },
            )

            with self.assertRaises(ValueError):
                start_project_compile_job(root, project["id"], {})

            preflight = generate_project_preflight_plan(root, project["id"], {})
            plan = preflight["plan"]
            confirmed = confirm_project_preflight_plan(root, project["id"], {"plan_id": plan["id"], "selected_scheme_id": "exercise"})
            snapshot = confirmed["confirmed_compile_snapshot"]
            loaded = read_course_project(root, project["id"])

            self.assertEqual(preflight["project"]["status"], "awaiting_confirmation")
            self.assertEqual(plan["source_scope"]["library_file_ids"], [first["id"], second["id"]])
            self.assertEqual(plan["compile_requirements"]["exercise_ratio"], "每节 2 个检查项。")
            self.assertEqual(snapshot["selected_scheme_id"], "exercise")
            self.assertEqual(snapshot["plan_signature"], plan["signature"])
            self.assertEqual(loaded["status"], "not_started")
            self.assertIn("confirmed_compile_snapshot", loaded)

            updated = update_course_project(root, project["id"], {"library_file_ids": [first["id"]]})
            self.assertNotIn("confirmed_compile_snapshot", updated)
            with self.assertRaises(ValueError):
                start_project_compile_job(root, project["id"], {})

    def test_project_compile_job_blocks_unparsed_pdf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("backend.server._run_mineru_parse", side_effect=RuntimeError("MinerU unavailable")):
                record = store_library_upload(root, "source.pdf", BytesIO(b"%PDF-1.4\n"))
            project = create_course_project(root, {"title": "PDF Course", "library_file_ids": [record["id"]]})
            plan = generate_project_preflight_plan(root, project["id"], {})["plan"]
            confirm_project_preflight_plan(root, project["id"], {"plan_id": plan["id"], "selected_scheme_id": "systematic"})

            job = start_project_compile_job(root, project["id"], {})
            loaded = read_job_status(root, str(job["id"]))
            project_after = read_course_project(root, project["id"])

            self.assertEqual(job["state"], "blocked")
            self.assertIn("MinerU", job["error"])
            self.assertEqual(loaded["events"][-1]["status"], "blocked")
            self.assertEqual(project_after["status"], "failed")

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
            plan = generate_project_preflight_plan(root, project["id"], {})["plan"]
            confirm_project_preflight_plan(root, project["id"], {"plan_id": plan["id"], "selected_scheme_id": "quick_review"})

            with patch("backend.server.threading.Thread", FakeThread):
                job = start_project_compile_job(root, project["id"], {})

            jobs = list_project_jobs(root, project["id"])
            loaded = read_job_status(root, str(job["id"]))
            compile_context = json.loads((root / "jobs" / job["id"] / "compile_context.json").read_text(encoding="utf-8"))

            self.assertEqual(job["state"], "queued")
            self.assertEqual(job["confirmed_plan_id"], plan["id"])
            self.assertEqual(job["selected_scheme_id"], "quick_review")
            self.assertEqual(jobs[0]["id"], job["id"])
            self.assertEqual(loaded["state"], "queued")
            self.assertIn("course_structure", compile_context["compile_requirements"])
            self.assertEqual(compile_context["source_files"][0]["id"], record["id"])
            self.assertTrue(compile_context["source_files"][0]["parsed_source_path"])
            self.assertTrue(compile_context["source_files"][0]["path"].endswith("content.md"))
            self.assertEqual(compile_context["confirmed_compile_snapshot"]["selected_scheme_id"], "quick_review")
            self.assertIn("--compile-context", json.loads((root / "jobs" / job["id"] / "job.json").read_text(encoding="utf-8"))["command"])

    def test_compile_job_pause_resume_and_terminate_use_persisted_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "jobs" / "job-1"
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": "job-1",
                        "project_id": "project-1",
                        "course_id": "project-1",
                        "version": "v1",
                        "state": "running",
                        "current_stage": "synthesize_lesson_bodies",
                        "progress": 42,
                        "pid": 12345,
                    }
                ),
                encoding="utf-8",
            )

            with patch("backend.server.os.killpg") as killpg:
                paused = control_compile_job(root, "job-1", "pause", {})
                resumed = control_compile_job(root, "job-1", "resume", {})
                terminating = control_compile_job(root, "job-1", "terminate", {})

            self.assertEqual(paused["state"], "paused")
            self.assertEqual(resumed["state"], "running")
            self.assertEqual(terminating["state"], "terminating")
            self.assertEqual(killpg.call_count, 3)
            events = read_job_status(root, "job-1")["events"]
            self.assertEqual(events[-1]["status"], "terminating")

    def test_compile_job_node_results_snapshot_artifacts_and_errors(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "jobs" / "job-1"
            course_dir = root / "courses" / "course-1"
            job_dir.mkdir(parents=True)
            course_dir.mkdir(parents=True)
            (job_dir / "compile_context.json").write_text(json.dumps({"source_files": []}), encoding="utf-8")
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": "job-1",
                        "project_id": "project-1",
                        "course_id": "course-1",
                        "version": "v1",
                        "state": "failed",
                        "current_stage": "check_markdown_syntax",
                        "progress": 70,
                        "error": "Markdown syntax failed",
                    }
                ),
                encoding="utf-8",
            )
            (job_dir / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": "t1", "stage": "synthesize_source_brief", "status": "started"}),
                        json.dumps({"timestamp": "t2", "stage": "synthesize_source_brief", "status": "finished", "next_action": "plan_course"}),
                        json.dumps({"timestamp": "t3", "stage": "check_markdown_syntax", "status": "failed", "error": "bad list"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (course_dir / "source_brief.json").write_text(json.dumps({"summary": "brief"}), encoding="utf-8")
            (course_dir / "lesson_bodies.json").write_text(json.dumps({"lessons": [{"id": "lesson-001"}]}), encoding="utf-8")
            (course_dir / "markdown_syntax_report.json").write_text(json.dumps({"ok": False, "failures": ["bad list"]}), encoding="utf-8")
            (course_dir / "validation_report.json").write_text(json.dumps({"ok": False}), encoding="utf-8")

            status = read_job_status(root, "job-1")
            nodes = list_job_node_results(root, "job-1")
            brief = read_job_node_result(root, "job-1", "synthesize_source_brief")
            markdown = read_job_node_result(root, "job-1", "check_markdown_syntax")

            self.assertTrue(status["nodes"])
            self.assertTrue((job_dir / "nodes" / "synthesize_source_brief.json").exists())
            self.assertEqual(brief["status"], "finished")
            self.assertEqual(brief["outputs"][0]["preview"]["summary"], "brief")
            self.assertEqual(markdown["status"], "failed")
            self.assertEqual(markdown["errors"][0]["error"], "bad list")
            self.assertTrue(any(node["node"] == "check_markdown_syntax" and node["error_count"] >= 1 for node in nodes))

    def test_waiting_review_blocks_status_and_review_feedback_is_persisted_for_rerun(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "jobs" / "job-1"
            course_dir = root / "courses" / "course-1"
            source = root / "source.md"
            job_dir.mkdir(parents=True)
            course_dir.mkdir(parents=True)
            source.write_text("# Source", encoding="utf-8")
            (job_dir / "compile_context.json").write_text(
                json.dumps({"source_files": [{"path": str(source)}], "confirmed_compile_snapshot": {}}),
                encoding="utf-8",
            )
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": "job-1",
                        "project_id": "project-1",
                        "course_id": "course-1",
                        "version": "v1",
                        "state": "failed",
                        "current_stage": "human_review",
                        "progress": 88,
                    }
                ),
                encoding="utf-8",
            )
            (course_dir / "human_review.json").write_text(json.dumps({"reason": "needs review"}), encoding="utf-8")

            status = read_job_status(root, "job-1")
            self.assertEqual(status["state"], "waiting_review")
            self.assertEqual(read_job_node_result(root, "job-1", "human_review")["status"], "waiting_review")

            class FakeThread:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def start(self) -> None:
                    return None

            with patch("backend.server.threading.Thread", FakeThread):
                rerun = control_compile_job(
                    root,
                    "job-1",
                    "request-modification",
                    {"feedback": "请拆分过长课时", "target_node": "plan_course"},
                )

            rerun_context = json.loads((root / "jobs" / rerun["id"] / "compile_context.json").read_text(encoding="utf-8"))
            self.assertEqual(rerun["rerun"]["from_node"], "plan_course")
            self.assertEqual(rerun_context["review_feedback"][0]["feedback"], "请拆分过长课时")
            self.assertTrue((job_dir / "review_decisions.jsonl").exists())

    def test_waiting_review_job_can_be_terminated_without_live_process(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "jobs" / "job-1"
            course_dir = root / "courses" / "course-1"
            job_dir.mkdir(parents=True)
            course_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(
                json.dumps(
                    {
                        "id": "job-1",
                        "project_id": "project-1",
                        "course_id": "course-1",
                        "version": "v1",
                        "state": "failed",
                        "current_stage": "human_review",
                        "pid": 999999,
                    }
                ),
                encoding="utf-8",
            )
            (course_dir / "human_review.json").write_text(json.dumps({"reason": "needs review"}), encoding="utf-8")
            self.assertEqual(read_job_status(root, "job-1")["state"], "waiting_review")

            with patch("backend.server.os.killpg") as killpg:
                terminated = control_compile_job(root, "job-1", "terminate", {})

            self.assertEqual(terminated["state"], "terminated")
            killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
