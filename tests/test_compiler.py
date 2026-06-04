from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from agent_graph.compiler import compile_course, compile_graph_edge_specs, compile_graph_node_specs
from agent_graph.feedback import apply_approved_patches, approve_patch, mine_feedback, record_feedback
from agent_graph.nodes import (
    _find_cached_lesson_body_by_id,
    _markdown_syntax_diagnostics,
    _plain_markdown_text,
    _repair_course_plan_coverage,
    _source_index_for_prompt,
    check_grounding_rules,
    check_markdown_syntax,
    check_quality_rules,
    repair_course,
    revise_compile_plan,
    synthesize_lesson_bodies,
)
from agent_graph.state import CompileConfig, initial_state


def _is_compile_plan_review_prompt(system: str, user: str) -> bool:
    return "compile-plan reviewer" in system or "Review this Markdown synthesis plan" in user


def _is_validation_prompt(system: str, user: str) -> bool:
    joined = system + "\n" + user
    return "grounding validation agent" in joined or "quality validation agent" in joined


def _passing_compile_plan_review() -> dict:
    return {
        "passed": True,
        "issues": [],
        "revise_prompt": {"objective": "", "actions": [], "details": []},
    }


def _passing_validation_result() -> dict:
    return {"ok": True, "failures": []}


def _graph_nodes(state: dict) -> list[str]:
    return [item["node"] for item in state["graph_run_log"]]


def _internal_nodes(state: dict) -> list[str]:
    return [item["node"] for item in state["internal_run_log"]]


class CompilerTests(unittest.TestCase):
    def test_compile_graph_metadata_covers_transition_endpoints(self) -> None:
        nodes = {node.name for node in compile_graph_node_specs()}
        edges = list(compile_graph_edge_specs())
        targets = {edge.target for edge in edges if edge.target != "END"}
        expected_nodes = {
            "ingest_pipeline",
            "course_planning_loop",
            "compile_plan_gate",
            "lesson_body_pipeline",
            "validation_repair_loop",
            "export_pipeline",
            "human_review",
        }

        self.assertEqual(nodes, expected_nodes)
        self.assertTrue(edges)
        self.assertTrue({edge.source for edge in edges}.issubset(nodes))
        self.assertTrue(targets.issubset(nodes))
        self.assertTrue(nodes - {"ingest_pipeline"} <= targets)
        self.assertIn(("ingest_pipeline", "course_planning_loop"), {(edge.source, edge.target) for edge in edges})
        self.assertIn(("course_planning_loop", "compile_plan_gate"), {(edge.source, edge.target) for edge in edges})
        self.assertIn(("compile_plan_gate", "lesson_body_pipeline"), {(edge.source, edge.target) for edge in edges})
        self.assertIn(("lesson_body_pipeline", "validation_repair_loop"), {(edge.source, edge.target) for edge in edges})
        self.assertIn(("validation_repair_loop", "export_pipeline"), {(edge.source, edge.target) for edge in edges})
        self.assertNotIn("parse_sources", nodes)
        self.assertNotIn("check_markdown_syntax", nodes)

    def test_markdown_syntax_diagnostics_are_structured(self) -> None:
        diagnostics, metadata = _markdown_syntax_diagnostics("##Broken heading\n\n[text]()\n\n```\ncode")
        failure_types = {item["type"] for item in diagnostics}

        self.assertIn("heading_format", failure_types)
        self.assertIn("empty_link", failure_types)
        self.assertIn("code_fence_missing_language", failure_types)
        self.assertIn("unclosed_code_fence", failure_types)
        self.assertIn("position", diagnostics[0])
        self.assertIn("snippet", diagnostics[0])
        self.assertIn("reason", diagnostics[0])
        self.assertIn("suggestion", diagnostics[0])
        self.assertIn("remark_lint", metadata)

    def test_markdown_syntax_check_repairs_full_lesson_body_and_records_audit(self) -> None:
        class RepairClient:
            last_metadata = {"provider": "fake"}

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def complete_json(self, system: str, user: str) -> dict:
                self.calls.append((system, user))
                self_test = CompilerTests()
                self_test.assertIn("Current Markdown body", user)
                self_test.assertIn("Structured Markdown errors", user)
                return {
                    "lesson_id": "lesson-001",
                    "title": "Topic",
                    "body_markdown": "## Topic\n\n### 学习目标\n理解 Topic。\n\n```text\nsource-backed example\n```",
                    "checklist": ["检查 Markdown"],
                    "covered_source_chunk_ids": ["chunk-1"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="md-course", source_files=[], profile={"use_llm_lesson_bodies": True}))
            state["parsed_chunks"] = [{"id": "chunk-1", "title": "Topic", "content": "source-backed example"}]
            state["units"] = [{"id": "unit-1", "source_chunk_ids": ["chunk-1"]}]
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Topic",
                    "unit_ids": ["unit-1"],
                    "body": "##Topic\n\n```\nsource-backed example",
                    "body_max_chars": 1200,
                    "checklist": ["old"],
                    "sources": [{"chunk_id": "chunk-1"}],
                    "order": 1,
                }
            ]
            state["lesson_bodies"] = {
                "lesson_bodies": [
                    {
                        "lesson_id": "lesson-001",
                        "title": "Topic",
                        "body_markdown": "##Topic\n\n```\nsource-backed example",
                        "checklist": ["old"],
                        "covered_source_chunk_ids": ["chunk-1"],
                    }
                ]
            }
            state["lesson_body_inputs"] = {"lessons": [{"lesson_id": "lesson-001", "system_prompt": "system", "user_prompt": "user"}]}
            client = RepairClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                checked = check_markdown_syntax(state, vault)

            self.assertTrue(checked["markdown_syntax_report"]["ok"])
            self.assertEqual(checked["markdown_syntax_report"]["error_count"], 0)
            self.assertIn("```text", checked["lessons"][0]["body"])
            self.assertEqual(checked["lessons"][0]["checklist"], ["old"])
            self.assertEqual(len(client.calls), 1)
            audit = checked["markdown_repair_audit"]
            self.assertEqual(audit["summary"]["repair_attempts"], 1)
            self.assertIn("markdown_before", audit["entries"][0])
            self.assertIn("markdown_after", audit["entries"][0])
            self.assertTrue((vault / "courses" / "md-course" / "markdown_syntax_report.json").exists())

    def test_lesson_body_preflight_requests_finer_split_before_llm_call(self) -> None:
        class NoCallClient:
            def complete_json(self, system: str, user: str) -> dict:
                raise AssertionError("dense lesson should be split before body generation")

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(
                CompileConfig(
                    course_id="dense-preflight",
                    source_files=[],
                    profile={"use_llm_lesson_bodies": True, "lesson_body_max_source_chunks": 2},
                )
            )
            state["parsed_chunks"] = [
                {"id": f"chunk-{index}", "title": f"Topic {index}", "content": "source paragraph " * 20}
                for index in range(1, 5)
            ]
            state["units"] = [
                {"id": f"unit-{index}", "title": f"Topic {index}", "source_chunk_ids": [f"chunk-{index}"], "source_chunk_id": f"chunk-{index}", "content_type": "source_supported", "source": "source.md", "source_quote": "quote"}
                for index in range(1, 5)
            ]
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Dense Topic",
                    "unit_ids": [f"unit-{index}" for index in range(1, 5)],
                    "body": "draft",
                    "body_max_chars": 7200,
                    "checklist": ["draft"],
                    "sources": [{"chunk_id": f"chunk-{index}", "block_id": f"chunk-{index}", "quote": "quote"} for index in range(1, 5)],
                    "order": 1,
                }
            ]
            state["compile_plan"] = {
                "hierarchy": {
                    "lessons": [
                        {
                            "lesson_id": "lesson-001",
                            "body_density_estimate": {
                                "estimated_plain_chars": 1200,
                                "plain_limit": 5000,
                                "needs_finer_split": True,
                                "reasons": ["knowledge_points_too_many"],
                            },
                        }
                    ]
                }
            }

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=NoCallClient()):
                checked = synthesize_lesson_bodies(state, vault)

            request = checked["lesson_body_revision_request"]
            self.assertEqual(checked["next_action"], "revise_compile_plan")
            self.assertTrue(request["needs_finer_split"])
            self.assertEqual(request["stage"], "pre_generation")
            self.assertIn("knowledge_points_too_many", request["issues"][0]["reasons"])
            self.assertEqual(checked["compile_plan_review"]["issues"][0]["type"], "needs_finer_split")
            self.assertTrue((vault / "courses" / "dense-preflight" / "lesson_body_revision_request.json").exists())

    def test_revise_compile_plan_splits_needs_finer_split_lesson_by_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="split-units", source_files=[]))
            state["units"] = [
                {"id": "unit-1", "title": "Alpha", "section_title": "S", "lesson_type": "concept", "content_type": "source_supported", "source": "source.md", "source_chunk_id": "chunk-1", "source_quote": "alpha", "source_refs": [{"chunk_id": "chunk-1", "source": "source.md", "quote": "alpha"}]},
                {"id": "unit-2", "title": "Beta", "section_title": "S", "lesson_type": "concept", "content_type": "source_supported", "source": "source.md", "source_chunk_id": "chunk-2", "source_quote": "beta", "source_refs": [{"chunk_id": "chunk-2", "source": "source.md", "quote": "beta"}]},
            ]
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Alpha Beta",
                    "section_title": "S",
                    "lesson_type": "concept",
                    "unit_ids": ["unit-1", "unit-2"],
                    "body": "combined",
                    "body_max_chars": 7200,
                    "checklist": ["combined"],
                    "sources": [{"chunk_id": "chunk-1", "quote": "alpha"}, {"chunk_id": "chunk-2", "quote": "beta"}],
                    "images": [],
                    "order": 1,
                }
            ]
            state["compile_plan_review"] = {
                "passed": False,
                "issues": [{"type": "needs_finer_split", "severity": "high", "lesson_ids": ["lesson-001"], "message": "split"}],
                "revise_prompt": {"actions": ["split_lesson"]},
            }

            revised = revise_compile_plan(state, vault)

            self.assertEqual(revised["next_action"], "review_compile_plan_llm")
            self.assertEqual([lesson["id"] for lesson in revised["lessons"]], ["lesson-001", "lesson-002"])
            self.assertEqual([lesson["title"] for lesson in revised["lessons"]], ["Alpha", "Beta"])
            self.assertEqual(revised["compile_plan_revisions"][0]["actions"][0]["action"], "split_lesson")

    def test_revise_compile_plan_falls_back_to_generate_lessons_when_split_is_unreliable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="split-fallback", source_files=[]))
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Dense",
                    "unit_ids": ["unit-missing"],
                    "body": "too short",
                    "checklist": ["x"],
                    "sources": [],
                    "order": 1,
                }
            ]
            state["compile_plan_review"] = {
                "passed": False,
                "issues": [{"type": "needs_finer_split", "severity": "high", "lesson_ids": ["lesson-001"], "message": "split"}],
                "revise_prompt": {"actions": ["split_lesson"]},
            }

            revised = revise_compile_plan(state, vault)

            self.assertEqual(revised["next_action"], "generate_lessons")
            revision_log = json.loads((vault / "courses" / "split-fallback" / "compile_plan_revision_log.json").read_text(encoding="utf-8"))
            self.assertEqual(revision_log["status"], "fallback_generate_lessons")

    def test_revise_compile_plan_splits_dense_single_unit_by_sources_as_numbered_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="split-source-parts", source_files=[]))
            state["units"] = [
                {"id": "unit-1", "title": "Long Chapter", "source_chunk_ids": ["chunk-1", "chunk-2"], "source_chunk_id": "chunk-1"}
            ]
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Long Chapter",
                    "section_title": "S",
                    "unit_ids": ["unit-1"],
                    "body": "第一部分。\n\n第二部分。",
                    "body_max_chars": 7200,
                    "checklist": ["draft"],
                    "sources": [{"chunk_id": "chunk-1", "quote": "alpha"}, {"chunk_id": "chunk-2", "quote": "beta"}],
                    "images": [],
                    "order": 1,
                }
            ]
            state["compile_plan_review"] = {
                "passed": False,
                "issues": [{"type": "needs_finer_split", "severity": "high", "lesson_ids": ["lesson-001"], "message": "split"}],
                "revise_prompt": {"actions": ["split_lesson"]},
            }

            revised = revise_compile_plan(state, vault)

            self.assertEqual(revised["next_action"], "review_compile_plan_llm")
            self.assertEqual([lesson["title"] for lesson in revised["lessons"]], ["Long Chapter（1）", "Long Chapter（2）"])
            self.assertEqual([lesson["order"] for lesson in revised["lessons"]], [1, 2])
            self.assertEqual(revised["lessons"][0]["sources"][0]["chunk_id"], "chunk-1")
            self.assertEqual(revised["lessons"][1]["sources"][0]["chunk_id"], "chunk-2")

    def test_lesson_body_post_generation_limit_requests_finer_split(self) -> None:
        class LongBodyClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, system: str, user: str) -> dict:
                self.calls += 1
                return {
                    "lesson_id": "lesson-001",
                    "title": "Long Body",
                    "body_markdown": "## Long Body\n\n" + ("字" * 5001),
                    "checklist": ["检查长度"],
                    "covered_source_chunk_ids": ["chunk-1"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="post-limit", source_files=[], profile={"use_llm_lesson_bodies": True}))
            state["parsed_chunks"] = [{"id": "chunk-1", "title": "Long Body", "content": "short source"}]
            state["units"] = [{"id": "unit-1", "title": "Long Body", "source_chunk_ids": ["chunk-1"]}]
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Long Body",
                    "unit_ids": ["unit-1"],
                    "body": "draft",
                    "body_max_chars": 7200,
                    "checklist": ["draft"],
                    "sources": [{"chunk_id": "chunk-1", "quote": "short source"}],
                    "order": 1,
                }
            ]
            client = LongBodyClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                checked = synthesize_lesson_bodies(state, vault)

            self.assertEqual(client.calls, 1)
            self.assertEqual(checked["next_action"], "revise_compile_plan")
            request = checked["lesson_body_revision_request"]
            self.assertEqual(request["stage"], "post_generation")
            self.assertIn("generated_lesson_plain_text_exceeds_limit", request["issues"][0]["reasons"])

    def test_lesson_body_batch_generation_limit_requests_finer_split(self) -> None:
        class BatchBodyClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, system: str, user: str) -> dict:
                self.calls += 1
                lesson_id = "lesson-001" if self.calls == 1 else "lesson-002"
                return {
                    "lesson_id": lesson_id,
                    "title": f"Body {self.calls}",
                    "body_markdown": f"## Body {self.calls}\n\n" + ("字" * 2600),
                    "checklist": ["检查长度"],
                    "covered_source_chunk_ids": [f"chunk-{self.calls}"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="batch-limit", source_files=[], profile={"use_llm_lesson_bodies": True}))
            state["parsed_chunks"] = [{"id": "chunk-1", "title": "A", "content": "short"}, {"id": "chunk-2", "title": "B", "content": "short"}]
            state["units"] = [{"id": "unit-1", "title": "A", "source_chunk_ids": ["chunk-1"]}, {"id": "unit-2", "title": "B", "source_chunk_ids": ["chunk-2"]}]
            state["lessons"] = [
                {"id": "lesson-001", "title": "A", "unit_ids": ["unit-1"], "body": "draft", "body_max_chars": 7200, "checklist": ["draft"], "sources": [{"chunk_id": "chunk-1", "quote": "short"}], "order": 1},
                {"id": "lesson-002", "title": "B", "unit_ids": ["unit-2"], "body": "draft", "body_max_chars": 7200, "checklist": ["draft"], "sources": [{"chunk_id": "chunk-2", "quote": "short"}], "order": 2},
            ]
            client = BatchBodyClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                checked = synthesize_lesson_bodies(state, vault)

            self.assertEqual(client.calls, 2)
            self.assertEqual(checked["next_action"], "revise_compile_plan")
            self.assertIn("generated_batch_plain_text_exceeds_limit", checked["lesson_body_revision_request"]["issues"][0]["reasons"])

    def test_compile_markdown_exports_versioned_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Intro\n\nThis is the first concept.\n\n## Next\n\nThis is the second concept.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            state = compile_course([str(source)], "sample-course", vault)

            lessons = sorted((vault / "courses" / "sample-course" / "versions" / "v1" / "lessons").glob("*.md"))
            self.assertEqual(state["next_action"], "done")
            self.assertTrue(state["validation_report"]["ok"])
            self.assertGreaterEqual(len(lessons), 1)
            self.assertFalse((vault / "raw" / "source.md").exists())
            self.assertTrue((vault / "courses" / "sample-course" / "parsed_chunks" / "source.json").exists())
            self.assertTrue((vault / "courses" / "sample-course" / "units.json").exists())
            self.assertTrue((vault / "courses" / "sample-course" / "logic_graph.json").exists())
            self.assertTrue((vault / "courses" / "sample-course" / "lesson_generation_evidence.json").exists())
            self.assertTrue((vault / "courses" / "sample-course" / "lesson_evidence.json").exists())
            source_line = lessons[0].read_text(encoding="utf-8")
            self.assertIn("source_id=", source_line)

    def test_compile_plan_gate_writes_review_artifacts_before_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Topic Alpha\n\nA source-backed explanation with enough detail for a lesson draft.\n\n"
                "# Topic Beta\n\nA second source-backed explanation for another lesson draft.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            state = compile_course([str(source)], "plan-course", vault)

            course_path = vault / "courses" / "plan-course"
            graph_nodes = _graph_nodes(state)
            internal_nodes = _internal_nodes(state)
            self.assertLess(graph_nodes.index("course_planning_loop"), graph_nodes.index("compile_plan_gate"))
            self.assertLess(graph_nodes.index("compile_plan_gate"), graph_nodes.index("lesson_body_pipeline"))
            self.assertLess(internal_nodes.index("generate_lessons"), internal_nodes.index("synthesize_compile_plan"))
            self.assertLess(internal_nodes.index("synthesize_compile_plan"), internal_nodes.index("review_compile_plan_llm"))
            self.assertLess(internal_nodes.index("review_compile_plan_llm"), internal_nodes.index("synthesize_lesson_bodies"))
            self.assertTrue((course_path / "compile_plan.json").exists())
            self.assertTrue((course_path / "compile_plan.md").exists())
            self.assertTrue((course_path / "compile_plan_review.json").exists())
            self.assertTrue((course_path / "compile_plan_review.md").exists())

            plan = json.loads((course_path / "compile_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["course_id"], "plan-course")
            self.assertIn("material_scope", plan)
            self.assertIn("hierarchy", plan)
            self.assertIn("image_insert_strategy", plan)
            self.assertIn("estimated_tokens", plan)
            self.assertIn("risk_warnings", plan)
            self.assertTrue(plan["hierarchy"]["lessons"][0]["source_blocks"])

    def test_compile_plan_review_failure_revises_before_lesson_bodies(self) -> None:
        class ReviewClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def __init__(self) -> None:
                self.review_calls = 0

            def cache_key(self, system: str, user: str) -> str:
                return f"review-{self.review_calls}-{len(user)}"

            def complete_json(self, system: str, user: str):
                if not _is_compile_plan_review_prompt(system, user):
                    raise AssertionError("Only compile-plan review should call this fake client")
                self.review_calls += 1
                if self.review_calls == 1:
                    return {
                        "passed": False,
                        "issues": [
                            {
                                "type": "visual_note_pollution",
                                "severity": "high",
                                "message": "Remove visual/layout notes before body generation.",
                                "lesson_ids": ["lesson-001"],
                                "unit_ids": [],
                            }
                        ],
                        "revise_prompt": {
                            "objective": "Remove visual notes from the compile plan.",
                            "actions": ["remove_visual_note"],
                            "details": [{"target": "lesson-001", "instruction": "Remove visual/layout notes."}],
                        },
                    }
                return _passing_compile_plan_review()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Topic Alpha\n\nFirst source-backed explanation for the compiler fixture.\n图片说明: remove this layout note.\n\n"
                "# Topic Beta\n\nSecond source-backed explanation for the compiler fixture.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            client = ReviewClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course(
                    [str(source)],
                    "review-revise-course",
                    vault,
                    "v1",
                    profile={"use_llm_compile_plan_review": True},
                )

            course_path = vault / "courses" / "review-revise-course"
            graph_nodes = _graph_nodes(state)
            internal_nodes = _internal_nodes(state)
            self.assertEqual(state["next_action"], "done")
            self.assertEqual(client.review_calls, 2)
            self.assertLess(graph_nodes.index("compile_plan_gate"), graph_nodes.index("lesson_body_pipeline"))
            self.assertLess(internal_nodes.index("review_compile_plan_llm"), internal_nodes.index("revise_compile_plan"))
            self.assertLess(internal_nodes.index("revise_compile_plan"), internal_nodes.index("synthesize_lesson_bodies"))
            self.assertEqual(len(state["compile_plan_revisions"]), 1)
            self.assertEqual(state["compile_plan_revisions"][0]["actions"][0]["action"], "remove_visual_note")

            revision_log = json.loads((course_path / "compile_plan_revision_log.json").read_text(encoding="utf-8"))
            review = json.loads((course_path / "compile_plan_review.json").read_text(encoding="utf-8"))
            self.assertEqual(revision_log["status"], "revised")
            self.assertEqual(revision_log["revisions"][0]["revise_prompt"]["actions"], ["remove_visual_note"])
            self.assertTrue(review["passed"])

    def test_compile_plan_review_blocks_bodies_after_revision_limit(self) -> None:
        class FailingReviewClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def __init__(self) -> None:
                self.review_calls = 0

            def cache_key(self, system: str, user: str) -> str:
                return f"always-fail-{self.review_calls}-{len(user)}"

            def complete_json(self, system: str, user: str):
                if not _is_compile_plan_review_prompt(system, user):
                    raise AssertionError("Only compile-plan review should call this fake client")
                self.review_calls += 1
                return {
                    "passed": False,
                    "issues": [
                        {
                            "type": "formula_markdown_risk",
                            "severity": "high",
                            "message": "Formula and Markdown layout require manual confirmation.",
                            "lesson_ids": ["lesson-001"],
                            "unit_ids": [],
                        }
                    ],
                    "revise_prompt": {
                        "objective": "Do not continue until formula layout is reviewed.",
                        "actions": ["flag_manual_confirmation"],
                        "details": [{"target": "lesson-001", "instruction": "Confirm formula Markdown."}],
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Topic Alpha\n\nA source-backed explanation with a displayed formula.\n", encoding="utf-8")
            vault = root / "course-vault"
            client = FailingReviewClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course(
                    [str(source)],
                    "review-blocked-course",
                    vault,
                    "v1",
                    profile={"use_llm_compile_plan_review": True, "compile_plan_max_revisions": 1},
                )

            course_path = vault / "courses" / "review-blocked-course"
            graph_nodes = _graph_nodes(state)
            internal_nodes = _internal_nodes(state)
            self.assertEqual(state["next_action"], "blocked_for_human_review")
            self.assertEqual(client.review_calls, 2)
            self.assertNotIn("lesson_body_pipeline", graph_nodes)
            self.assertNotIn("synthesize_lesson_bodies", internal_nodes)
            self.assertIn("human_review", graph_nodes)

            revision_log = json.loads((course_path / "compile_plan_revision_log.json").read_text(encoding="utf-8"))
            review = json.loads((course_path / "compile_plan_review.json").read_text(encoding="utf-8"))
            self.assertEqual(revision_log["status"], "exhausted")
            self.assertFalse(review["passed"])
            self.assertEqual(review["revise_prompt"]["actions"], ["flag_manual_confirmation"])

    def test_llm_structure_nodes_filter_fragments_and_preserve_provenance(self) -> None:
        class StructureClient:
            calls: list[str] = []
            last_metadata = {"usage": {"input_tokens": 1}}
            cache_identity = {"provider": "fake", "model": "structure"}

            def complete_json(self, system: str, user: str):
                joined = system + "\n" + user
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "structure-extraction agent" in joined:
                    self.calls.append("extract_units")
                    return {
                        "units": [
                            {
                                "title": "Topic Alpha Method",
                                "section_title": "Core",
                                "lesson_type": "concept",
                                "summary": "A medium-grain method unit.",
                                "source_chunk_ids": ["source-chunk-1"],
                            },
                            {
                                "title": "Topic Alpha Method",
                                "section_title": "Core",
                                "lesson_type": "example",
                                "summary": "Duplicate title should merge.",
                                "source_chunk_ids": ["source-chunk-2"],
                            },
                            {
                                "title": "Image Caption",
                                "summary": "Caption-only material should attach, not become a lesson.",
                                "source_chunk_ids": ["source-chunk-3"],
                            },
                            {
                                "title": "Teacher Hint",
                                "summary": "Teacher hint should not become a lesson.",
                                "source_chunk_ids": ["source-chunk-4"],
                            },
                        ]
                    }
                if "logic-organization agent" in joined:
                    self.calls.append("organize_logic")
                    return {
                        "nodes": [{"id": "unit-001", "title": "Topic Alpha Method", "role": "method"}],
                        "edges": [],
                    }
                if "gap-detection agent" in joined:
                    self.calls.append("detect_gaps")
                    return {"items": []}
                if "lesson-drafting agent" in joined:
                    self.calls.append("generate_lessons")
                    return {
                        "lessons": [
                            {
                                "title": "Topic Alpha Method",
                                "section_title": "Core",
                                "lesson_type": "concept",
                                "unit_ids": ["unit-001"],
                                "body": "Study the merged source-backed method and its attached context.",
                                "checklist": ["Explain the method", "Locate the source evidence"],
                            },
                            {
                                "title": "图片说明",
                                "unit_ids": ["unit-001"],
                                "body": "This must not survive as a standalone lesson.",
                            },
                        ]
                    }
                raise AssertionError(f"Unexpected LLM prompt: {joined[:200]}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Topic Alpha Method\n\nMain method explanation.\n\n"
                "# Topic Alpha Method\n\nWorked example for the same method.\n\n"
                "# Image Caption\n\n![diagram](/api/assets/example.png)\n\n"
                "# Teacher Hint\n\nRemember to compare assumptions.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            client = StructureClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course([str(source)], "structure-course", vault, "v1", profile={"use_llm_structure": True})

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(client.calls, ["extract_units", "organize_logic", "detect_gaps", "generate_lessons"])
            self.assertEqual(len(state["units"]), 1)
            self.assertEqual(state["units"][0]["source_chunk_ids"], ["source-chunk-1", "source-chunk-2", "source-chunk-3", "source-chunk-4"])
            self.assertEqual(len(state["lessons"]), 1)
            self.assertEqual(state["lessons"][0]["title"], "Topic Alpha Method")
            self.assertNotIn("Image Caption", [lesson["title"] for lesson in state["lessons"]])
            first_source = state["lessons"][0]["sources"][0]
            for field in ("source_file", "page", "block_id", "bbox", "source_order", "chunk_id", "quote"):
                self.assertIn(field, first_source)
            self.assertEqual(first_source["source_file"], "source.md")
            self.assertTrue((vault / "courses" / "structure-course" / "units_meta.json").exists())

    def test_llm_structure_fallback_routes_to_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Topic Alpha\n\nSource-backed explanation.\n", encoding="utf-8")
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=None):
                state = compile_course([str(source)], "blocked-structure-course", vault, "v1", profile={"use_llm": True})

            course_path = vault / "courses" / "blocked-structure-course"
            self.assertEqual(state["next_action"], "blocked_for_human_review")
            self.assertTrue(state["errors"])
            self.assertIn("extract_units", [error["node"] for error in state["errors"]])
            self.assertIn("human_review", _graph_nodes(state))
            self.assertNotIn("organize_logic", _internal_nodes(state))
            self.assertTrue((course_path / "extract_units_emergency_fallback.json").exists())
            self.assertTrue((course_path / "human_review.json").exists())

    def test_image_understanding_places_figures_and_pending_confirmations(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            source_dir = root / "parsed-source"
            images_dir = source_dir / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "chart.jpg").write_bytes(b"chart")
            (images_dir / "unknown.jpg").write_bytes(b"unknown")
            source_dir.joinpath("full.md").write_text(
                "# Topic Alpha\n\n"
                "The local concept is explained with a plotted relation.\n\n"
                "![Relation chart](images/chart.jpg)\n\n"
                "# Topic Beta\n\n"
                "This section contains a visual with insufficient parser evidence.\n\n"
                "![](images/unknown.jpg)\n",
                encoding="utf-8",
            )
            source_dir.joinpath("sample_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "chart",
                            "img_path": "images/chart.jpg",
                            "content": "| x | y |\n| - | - |\n| 0 | 0 |\n| 1 | 1 |",
                            "sub_type": "line",
                            "bbox": [10, 20, 300, 220],
                            "page_idx": 2,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            vault = root / "course-vault"

            state = compile_course([str(source_dir)], "image-course", vault, "v1")

            course_path = vault / "courses" / "image-course"
            image_understanding = json.loads((course_path / "image_understanding.json").read_text(encoding="utf-8"))
            lesson_file = next((course_path / "versions" / "v1" / "lessons").glob("*.md"))
            lesson_markdown = lesson_file.read_text(encoding="utf-8")
            version_record = json.loads((course_path / "versions" / "v1" / "version_record.json").read_text(encoding="utf-8"))

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(image_understanding["summary"]["total"], 2)
            self.assertEqual(image_understanding["summary"]["needs_confirmation"], 1)
            self.assertEqual(image_understanding["images"][0]["image_type"], "function_graph")
            self.assertIn("## Figures", lesson_markdown)
            self.assertIn("![", lesson_markdown)
            self.assertIn("/api/assets/", lesson_markdown)
            self.assertIn("## 待确认图片", lesson_markdown)
            self.assertEqual(version_record["image_understanding"]["total"], 2)

    def test_vision_image_understanding_refines_uncertain_images_with_cache(self) -> None:
        class FakeVisionClient:
            cache_identity = {"provider": "fake-vision", "model": "test"}
            calls = 0

            async def analyze_many(self, records):
                self.__class__.calls += len(records)
                return [
                    {
                        "image_type": "structure_diagram",
                        "associated_knowledge_points": ["Topic Alpha"],
                        "summary": "A compact structure diagram showing the local relationship.",
                        "suggested_insert_after": "The local concept",
                        "needs_caption": True,
                        "caption": "Local structure diagram",
                        "needs_confirmation": False,
                        "confidence": 0.91,
                        "reason": "Recognized by fake vision model.",
                    }
                    for _ in records
                ]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            source_dir = root / "parsed-source"
            images_dir = source_dir / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "unknown.jpg").write_bytes(b"unknown")
            source_dir.joinpath("full.md").write_text(
                "# Topic Alpha\n\n"
                "The local concept uses a visual relation.\n\n"
                "![](images/unknown.jpg)\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            profile = {
                "use_vision_image_understanding": True,
                "image_vision_mode": "uncertain",
                "image_vision_max_images": 4,
            }

            with patch("agent_graph.nodes.VisionImageClient.from_env", return_value=FakeVisionClient()):
                state = compile_course([str(source_dir)], "vision-image-course", vault, "v1", profile=profile)
                state_cached = compile_course([str(source_dir)], "vision-image-course", vault, "v2", profile=profile)

            course_path = vault / "courses" / "vision-image-course"
            image_understanding = json.loads((course_path / "image_understanding.json").read_text(encoding="utf-8"))
            lesson_file = next((course_path / "versions" / "v2" / "lessons").glob("*.md"))
            lesson_markdown = lesson_file.read_text(encoding="utf-8")

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(state_cached["next_action"], "done")
            self.assertEqual(FakeVisionClient.calls, 1)
            self.assertEqual(image_understanding["summary"]["recognized"], 1)
            self.assertEqual(image_understanding["summary"]["needs_confirmation"], 0)
            self.assertEqual(image_understanding["summary"]["vision"]["cache_hits"], 1)
            self.assertIn("Local structure diagram", lesson_markdown)
            self.assertNotIn("## 待确认图片", lesson_markdown)

    def test_formula_image_heuristic_preserves_low_confidence_original(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            source_dir = root / "parsed-source"
            images_dir = source_dir / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "formula.jpg").write_bytes(b"formula")
            source_dir.joinpath("full.md").write_text(
                "# Energy Relation\n\n"
                "The derivation introduces the displayed equation below.\n\n"
                "![Energy formula](images/formula.jpg)\n\n"
                "This relation connects mass and energy in the surrounding explanation.\n",
                encoding="utf-8",
            )
            source_dir.joinpath("sample_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "formula",
                            "img_path": "images/formula.jpg",
                            "content": "$$ E = mc^2 $$",
                            "bbox": [20, 80, 260, 130],
                            "page_idx": 1,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            vault = root / "course-vault"

            state = compile_course([str(source_dir)], "formula-heuristic-course", vault, "v1")

            course_path = vault / "courses" / "formula-heuristic-course"
            image_understanding = json.loads((course_path / "image_understanding.json").read_text(encoding="utf-8"))
            formula_report = json.loads((course_path / "formula_image_recognition.json").read_text(encoding="utf-8"))
            formula_markdown = (course_path / "formula_image_recognition.md").read_text(encoding="utf-8")
            lesson_file = next((course_path / "versions" / "v1" / "lessons").glob("*.md"))
            lesson_markdown = lesson_file.read_text(encoding="utf-8")

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(image_understanding["images"][0]["image_type"], "formula_image")
            self.assertTrue(image_understanding["images"][0]["formula_recognition"]["needs_human_review"])
            self.assertTrue(image_understanding["images"][0]["preserve_original_image"])
            self.assertEqual(image_understanding["summary"]["formula_recognition"]["needs_human_review"], 1)
            self.assertEqual(formula_report["summary"]["recognized"], 1)
            self.assertIn("E = mc^2", formula_report["formulas"][0]["latex"])
            self.assertIn("待人工审核", lesson_markdown)
            self.assertIn("![", lesson_markdown)
            self.assertIn("E = mc^2", formula_markdown)

    def test_formula_image_agent_renders_trusted_editable_markdown(self) -> None:
        class FakeFormulaVisionClient:
            cache_identity = {"provider": "fake-formula-vision", "model": "test"}
            calls = 0

            async def analyze_formula_many(self, records):
                self.__class__.calls += len(records)
                for record in records:
                    for field in ("page_context", "neighbor_text", "page_screenshot_path"):
                        if field not in record:
                            raise AssertionError(f"Missing formula context field: {field}")
                    if "E = mc^2" not in record.get("page_context", "") + record.get("mineru_content", ""):
                        raise AssertionError("Formula recognition did not receive adjacent source context")
                return [
                    {
                        "is_formula": True,
                        "formula_role": "definition",
                        "recognized_text": "E = mc^2",
                        "latex": "E = mc^2",
                        "markdown": "$$\nE = mc^2\n$$",
                        "context_check": "consistent",
                        "confidence": 0.93,
                        "needs_human_review": False,
                        "preserve_original_image": False,
                        "caption": "Mass-energy relation",
                        "reason": "Formula is supported by the page context.",
                    }
                    for _ in records
                ]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            source_dir = root / "parsed-source"
            images_dir = source_dir / "images"
            images_dir.mkdir(parents=True)
            (images_dir / "formula.jpg").write_bytes(b"formula")
            source_dir.joinpath("full.md").write_text(
                "# Energy Relation\n\n"
                "The displayed formula defines the mass-energy relation.\n\n"
                "![Energy formula](images/formula.jpg)\n\n"
                "Use this formula as the definition for later examples.\n",
                encoding="utf-8",
            )
            source_dir.joinpath("sample_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "formula",
                            "img_path": "images/formula.jpg",
                            "content": "$$ E = mc^2 $$",
                            "bbox": [20, 80, 260, 130],
                            "page_idx": 1,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            vault = root / "course-vault"
            profile = {"use_formula_image_recognition": True, "formula_image_max_images": 4}

            with patch("agent_graph.nodes.VisionImageClient.from_env", return_value=FakeFormulaVisionClient()):
                state = compile_course([str(source_dir)], "formula-agent-course", vault, "v1", profile=profile)

            course_path = vault / "courses" / "formula-agent-course"
            image_understanding = json.loads((course_path / "image_understanding.json").read_text(encoding="utf-8"))
            formula_report = json.loads((course_path / "formula_image_recognition.json").read_text(encoding="utf-8"))
            lesson_file = next((course_path / "versions" / "v1" / "lessons").glob("*.md"))
            lesson_markdown = lesson_file.read_text(encoding="utf-8")

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(FakeFormulaVisionClient.calls, 1)
            self.assertFalse(image_understanding["images"][0]["needs_confirmation"])
            self.assertFalse(image_understanding["images"][0]["preserve_original_image"])
            self.assertEqual(formula_report["formulas"][0]["formula_role"], "definition")
            self.assertFalse(formula_report["formulas"][0]["needs_human_review"])
            self.assertIn("## Figures", lesson_markdown)
            self.assertIn("E = mc^2", lesson_markdown)
            self.assertNotIn("![", lesson_markdown)
            self.assertNotIn("## 待确认图片", lesson_markdown)

    def test_source_index_prompt_compacts_without_dropping_tail_packs(self) -> None:
        source_index = {
            "packs": [
                {
                    "pack_id": f"pack-{index:03d}",
                    "title": f"Chapter {index}",
                    "summary": "summary " * 80,
                    "key_concepts": [f"concept-{index}-{item}" for item in range(6)],
                    "candidate_lessons": [
                        {"title": f"Lesson {index}-{item}", "reason": "reason " * 20, "source_chunk_ids": [f"chunk-{index}-{item}"]}
                        for item in range(4)
                    ],
                    "source_chunk_ids": [f"chunk-{index}-{item}" for item in range(20)],
                }
                for index in range(1, 10)
            ]
        }

        prompt = _source_index_for_prompt(source_index, max_chars=1800)

        self.assertIn("pack-001", prompt)
        self.assertIn("pack-009", prompt)
        self.assertIn("Lesson 9-3", prompt)
        self.assertNotIn("omitted_index_tail", prompt)

    def test_course_plan_repair_adds_source_index_candidates_when_too_sparse(self) -> None:
        plan = {
            "sections": [
                {"title": "Section", "lessons": [{"title": "Existing", "chunk_ids": ["chunk-1", "chunk-2"], "why": ""}]}
            ],
            "rejected_titles": [],
        }
        source_index = {
            "packs": [
                {
                    "title": "Pack",
                    "source_chunk_ids": ["chapter-a-full-chunk-1"],
                    "candidate_lessons": [
                        {"title": "Existing", "source_chunk_ids": ["chunk-1"]},
                        {"title": "Useful Topic", "source_chunk_ids": ["chunk-2"]},
                        {"title": "例题", "source_chunk_ids": ["chunk-3"]},
                        {"title": "Another Topic", "source_chunk_ids": ["chunk-4"]},
                    ],
                }
            ]
        }

        repaired = _repair_course_plan_coverage(plan, source_index, {"chunk-1", "chunk-2", "chunk-3", "chunk-4"}, 3)
        titles = [lesson["title"] for section in repaired["sections"] for lesson in section["lessons"]]
        existing = next(lesson for section in repaired["sections"] for lesson in section["lessons"] if lesson["title"] == "Existing")

        self.assertEqual(titles.count("Existing"), 1)
        self.assertIn("Useful Topic", titles)
        self.assertIn("Another Topic", titles)
        self.assertNotIn("例题", titles)
        self.assertEqual(existing["chunk_ids"], ["chunk-1"])

    def test_lesson_body_cache_fallback_matches_lesson_and_source_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            course_path = Path(tmp)
            cache_dir = course_path / "llm_cache"
            cache_dir.mkdir()
            (cache_dir / "lesson_body-old.json").write_text(
                json.dumps(
                    {
                        "lesson_body": {
                            "lesson_id": "lesson-001",
                            "body_markdown": "cached body",
                            "covered_source_chunk_ids": ["chunk-1"],
                        },
                        "metadata": {"provider": "test"},
                    }
                ),
                encoding="utf-8",
            )

            cached = _find_cached_lesson_body_by_id(course_path, "lesson-001", {"chunk-1", "chunk-2"})
            stale = _find_cached_lesson_body_by_id(course_path, "lesson-001", {"chunk-2"})

        self.assertIsNotNone(cached)
        self.assertEqual(cached["lesson_body"]["body_markdown"], "cached body")
        self.assertIsNone(stale)

    def test_export_version_removes_stale_lesson_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Intro\n\nA compact source paragraph.\n", encoding="utf-8")
            vault = root / "course-vault"
            stale = vault / "courses" / "sample-course" / "versions" / "v1" / "lessons" / "999-stale.md"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text("# stale\n", encoding="utf-8")

            compile_course([str(source)], "sample-course", vault, "v1")

            self.assertFalse(stale.exists())

    def test_grounding_fails_when_quote_is_not_in_chunk(self) -> None:
        state = initial_state(CompileConfig(course_id="bad-course", source_files=[]))
        state["parsed_chunks"] = [
            {
                "id": "chunk-1",
                "source": "source.md",
                "title": "Intro",
                "content": "Grounded text.",
                "start_line": 1,
                "end_line": 1,
            }
        ]
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Intro",
                "sources": [{"source": "source.md", "chunk_id": "chunk-1", "quote": "Different text."}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            checked = check_grounding_rules(state, Path(tmp) / "vault")

        self.assertFalse(checked["validation_report"]["ok"])
        self.assertEqual(checked["next_action"], "check_quality_llm")
        self.assertIn("source_quote_untraceable", {failure["type"] for failure in checked["validation_report"]["failures"]})

    def test_grounding_rules_report_provenance_locations(self) -> None:
        state = initial_state(CompileConfig(course_id="bad-provenance", source_files=[]))
        state["parsed_chunks"] = [
            {
                "id": "chunk-1",
                "source": "source.md",
                "title": "Topic Alpha",
                "content": "Grounded text.",
                "page": 3,
                "bbox": [1, 2, 3, 4],
                "start_line": 9,
            }
        ]
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Topic Alpha",
                "sources": [{"source": "source.md", "chunk_id": "chunk-1", "quote": "Grounded text."}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            checked = check_grounding_rules(state, Path(tmp) / "vault")

        failures = checked["validation_report"]["failures"]
        failure_types = {failure["type"] for failure in failures}
        self.assertIn("block_id_missing", failure_types)
        self.assertIn("source_page_missing", failure_types)
        self.assertIn("bbox_missing", failure_types)
        self.assertTrue(all(failure["lesson_id"] == "lesson-001" for failure in failures))
        self.assertTrue(all(failure["block_id"] == "chunk-1" for failure in failures))
        self.assertTrue(all(failure["line"] == 9 for failure in failures))

    def test_repair_course_uses_source_tool_to_patch_citation_provenance(self) -> None:
        state = initial_state(CompileConfig(course_id="repair-grounding", source_files=[]))
        state["parsed_chunks"] = [
            {
                "id": "chunk-1",
                "source": "source.md",
                "source_file": "source.md",
                "title": "Topic",
                "content": "Grounded text.",
                "page": 7,
                "block_id": "block-1",
                "bbox": [1, 2, 3, 4],
                "start_line": 3,
            }
        ]
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Topic",
                "unit_ids": ["unit-001"],
                "body": "Grounded text.",
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-1", "quote": "Different."}],
                "order": 1,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            repaired = repair_course(state, Path(tmp) / "vault")

        source = repaired["lessons"][0]["sources"][0]
        self.assertEqual(repaired["next_action"], "check_grounding_llm")
        self.assertEqual(source["source_id"], "chunk-1")
        self.assertEqual(source["block_id"], "block-1")
        self.assertEqual(source["page"], 7)
        self.assertEqual(source["bbox"], [1, 2, 3, 4])
        self.assertEqual(source["quote"], "Grounded text.")
        self.assertTrue(repaired["compile_patches"])
        self.assertEqual(repaired["compile_patches"][-1]["target"]["action"], "replace_citation")

    def test_quality_rules_report_markdown_and_title_locations(self) -> None:
        state = initial_state(CompileConfig(course_id="bad-quality", source_files=[]))
        source = {
            "source": "source.md",
            "chunk_id": "chunk-1",
            "block_id": "chunk-1",
            "page": 1,
            "bbox": [0, 0, 1, 1],
            "quote": "Grounded text.",
        }
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Topic Alpha",
                "unit_ids": ["unit-001"],
                "body": "```python\nprint('open')\n\n$$\n\\begin{cases}\n- x = 1\n\\end{cases}\n$$",
                "checklist": ["Read"],
                "sources": [source],
                "order": 1,
            },
            {
                "id": "lesson-002",
                "title": "Topic Alpha",
                "unit_ids": ["unit-002"],
                "body": "Grounded text.",
                "checklist": ["Read"],
                "sources": [source],
                "order": 2,
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            checked = check_quality_rules(state, Path(tmp) / "vault")

        failures = checked["validation_report"]["failures"]
        failure_types = {failure["type"] for failure in failures}
        self.assertIn("duplicate_title", failure_types)
        self.assertIn("unclosed_code_block", failure_types)
        self.assertIn("formula_markdown_mix", failure_types)
        self.assertEqual(checked["next_action"], "repair_course")
        self.assertTrue(all("lesson_id" in failure and "line" in failure and "reason" in failure for failure in failures))

    def test_quality_rules_record_plain_text_char_counts_and_limit(self) -> None:
        state = initial_state(CompileConfig(course_id="plain-count", source_files=[], profile={"lesson_body_plain_char_limit": 20}))
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Plain Count",
                "unit_ids": ["unit-001"],
                "body": "## 标题\n\n**粗体内容** `code` " + "字" * 25,
                "body_max_chars": 200,
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-1", "block_id": "chunk-1", "page": 1, "bbox": [0, 0, 1, 1], "quote": "字"}],
                "order": 1,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            checked = check_quality_rules(state, Path(tmp) / "vault")

        layer = checked["validation_report"]["layers"]["quality_rules"]
        self.assertGreater(layer["metadata"]["plain_char_by_lesson"]["lesson-001"], 20)
        self.assertGreater(checked["lessons"][0]["plain_char_count"], 20)
        self.assertIn("body_too_long", {failure["type"] for failure in checked["validation_report"]["failures"]})

    def test_repair_course_splits_overlong_lesson_with_local_patch(self) -> None:
        state = initial_state(CompileConfig(course_id="split-long-repair", source_files=[], profile={"lesson_body_plain_char_limit": 120}))
        long_body = "第一段。" + ("字" * 90) + "\n\n第二段。" + ("词" * 90)
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "长章节",
                "unit_ids": ["unit-001"],
                "body": long_body,
                "body_max_chars": 120,
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-1", "quote": "第一段"}],
                "order": 1,
            },
            {
                "id": "lesson-002",
                "title": "短章节",
                "unit_ids": ["unit-002"],
                "body": "短内容。",
                "body_max_chars": 120,
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-2", "quote": "短内容"}],
                "order": 2,
            },
        ]
        state["validation_report"] = {"ok": False, "failures": [{"lesson_id": "lesson-001", "type": "body_too_long"}]}

        with tempfile.TemporaryDirectory() as tmp:
            repaired = repair_course(state, Path(tmp) / "vault")

        self.assertEqual(repaired["next_action"], "check_grounding_llm")
        self.assertEqual([lesson["title"] for lesson in repaired["lessons"][:2]], ["长章节（1）", "长章节（2）"])
        self.assertEqual(repaired["lessons"][2]["title"], "短章节")
        self.assertTrue(all(len(_plain_markdown_text(lesson["body"])) <= 120 for lesson in repaired["lessons"][:2]))
        self.assertEqual(repaired["compile_patches"][-1]["target"]["action"], "split_lesson")

    def test_repair_course_applies_semantic_lesson_patch_only_to_failed_lesson(self) -> None:
        state = initial_state(CompileConfig(course_id="semantic-local-repair", source_files=[]))
        state["parsed_chunks"] = [{"id": "chunk-1", "title": "Evidence", "content": "Grounded fact.", "source": "source.md"}]
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Needs Patch",
                "unit_ids": ["unit-001"],
                "body": "第一行正确。\n第二行无依据。",
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-1", "quote": "Grounded fact."}],
                "order": 1,
            },
            {
                "id": "lesson-002",
                "title": "Untouched",
                "unit_ids": ["unit-002"],
                "body": "保持不变。",
                "checklist": ["Read"],
                "sources": [{"chunk_id": "chunk-1", "quote": "Grounded fact."}],
                "order": 2,
            },
        ]
        state["validation_report"] = {
            "ok": False,
            "failures": [
                {
                    "stage": "grounding_llm",
                    "lesson_id": "lesson-001",
                    "type": "unsupported_inference",
                    "message": "Unsupported claim.",
                    "line": 2,
                    "block_id": "chunk-1",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            repaired = repair_course(state, Path(tmp) / "vault")

        self.assertEqual(repaired["next_action"], "check_grounding_llm")
        self.assertIn("[needs_confirmation] Unsupported claim.", repaired["lessons"][0]["body"])
        self.assertEqual(repaired["lessons"][1]["body"], "保持不变。")
        self.assertEqual(repaired["compile_patches"][-1]["target"]["lesson_id"], "lesson-001")
        self.assertIn("Semantic repair", repaired["compile_patches"][-1]["reason"])

    def test_repair_course_fills_schema_empty_body_and_sources_mechanically(self) -> None:
        state = initial_state(CompileConfig(course_id="schema-mechanical-repair", source_files=[]))
        state["units"] = [
            {
                "id": "unit-001",
                "source": "source.md",
                "source_chunk_id": "chunk-1",
                "source_quote": "Grounded text.",
                "source_refs": [{"source": "source.md", "chunk_id": "chunk-1", "block_id": "chunk-1", "quote": "Grounded text."}],
            }
        ]
        state["lessons"] = [{"title": "", "unit_ids": ["unit-001"], "body": "", "checklist": []}]
        state["validation_report"] = {"ok": False, "failures": [{"type": "schema_missing_fields", "lesson_id": ""}]}

        with tempfile.TemporaryDirectory() as tmp:
            repaired = repair_course(state, Path(tmp) / "vault")

        lesson = repaired["lessons"][0]
        self.assertEqual(repaired["next_action"], "check_grounding_llm")
        self.assertEqual(lesson["id"], "lesson-001")
        self.assertEqual(lesson["order"], 1)
        self.assertIn("[needs_confirmation]", lesson["body"])
        self.assertEqual(lesson["sources"][0]["chunk_id"], "chunk-1")
        self.assertGreaterEqual(repaired["repair_report"]["repair_classes"]["mechanical"], 1)

    def test_quality_rules_normalize_compiled_markdown_before_export(self) -> None:
        state = initial_state(CompileConfig(course_id="normalized-quality", source_files=[]))
        source = {
            "source": "source.md",
            "chunk_id": "chunk-1",
            "block_id": "chunk-1",
            "page": 1,
            "bbox": [0, 0, 1, 1],
            "quote": "Grounded text.",
        }
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Topic Alpha",
                "unit_ids": ["unit-001"],
                "body": (
                    "### 核心讲解\n"
                    " 第1种边界条件用于补足方程。\n"
                    "\\mu_j M_{j-1} + 2M_j + \\lambda_j M_{j+1} = d_j\n"
                    "\\left[ \\begin{array}{cc} 2 & 1 \\\\ \\mu_1 & 2 \\end{array} \\right]\n"
                    "-  列表项应保留 Markdown bullet。"
                ),
                "checklist": ["□ 理解方程结构"],
                "sources": [source],
                "order": 1,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            checked = check_quality_rules(state, Path(tmp) / "vault")

        body = checked["lessons"][0]["body"]
        self.assertNotIn("", body)
        self.assertNotIn("□", checked["lessons"][0]["checklist"][0])
        self.assertIn("第1种边界条件", body)
        self.assertIn("- 列表项应保留 Markdown bullet。", body)
        self.assertIn("$$\n\\mu_j M_{j-1}", body)
        self.assertIn("\\begin{array}{cc}", body)
        self.assertEqual(checked["next_action"], "export_version")

    def test_validation_llm_failure_blocks_export_and_reports_location(self) -> None:
        class ValidationClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def complete_json(self, system: str, user: str):
                if "grounding validation agent" in system:
                    self.calls.append("grounding")
                    return {
                        "ok": False,
                        "failures": [
                            {
                                "lesson_id": "lesson-001",
                                "type": "unsupported_inference",
                                "message": "The lesson adds a claim not present in the source block.",
                                "block_id": "source-chunk-1",
                                "line": 3,
                            }
                        ],
                    }
                if "quality validation agent" in system:
                    self.calls.append("quality")
                    return _passing_validation_result()
                raise AssertionError("Unexpected prompt")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Topic Alpha\n\nGrounded text for the lesson.\n", encoding="utf-8")
            vault = root / "course-vault"
            client = ValidationClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course([str(source)], "llm-validation-fail", vault, "v1", profile={"use_llm_validation": True})

            graph_nodes = _graph_nodes(state)
            internal_nodes = _internal_nodes(state)
            report = state["validation_report"]
            failure = report["layers"]["grounding_llm"]["failures"][0]

            self.assertEqual(client.calls, ["grounding", "quality"])
            self.assertFalse(report["ok"])
            self.assertNotIn("export_pipeline", graph_nodes)
            self.assertNotIn("export_version", internal_nodes)
            self.assertIn("repair_course", internal_nodes)
            self.assertEqual(failure["lesson_id"], "lesson-001")
            self.assertEqual(failure["block_id"], "source-chunk-1")
            self.assertEqual(failure["line"], 3)
            self.assertIn("unsupported", failure["type"])

    def test_validation_llm_and_rules_must_pass_before_export(self) -> None:
        class PassingValidationClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def complete_json(self, system: str, user: str):
                if "grounding validation agent" in system:
                    self.calls.append("grounding")
                    return _passing_validation_result()
                if "quality validation agent" in system:
                    self.calls.append("quality")
                    return _passing_validation_result()
                raise AssertionError("Unexpected prompt")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Topic Alpha\n\nGrounded text for the lesson.\n", encoding="utf-8")
            vault = root / "course-vault"
            client = PassingValidationClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course([str(source)], "llm-validation-pass", vault, "v1", profile={"use_llm_validation": True})

            graph_nodes = _graph_nodes(state)
            internal_nodes = _internal_nodes(state)
            report = state["validation_report"]
            self.assertEqual(client.calls, ["grounding", "quality"])
            self.assertLess(graph_nodes.index("validation_repair_loop"), graph_nodes.index("export_pipeline"))
            self.assertLess(internal_nodes.index("check_grounding_rules"), internal_nodes.index("check_grounding_llm"))
            self.assertLess(internal_nodes.index("check_quality_rules"), internal_nodes.index("check_quality_llm"))
            self.assertTrue(report["ok"])
            self.assertEqual(set(report["layers"]), {"grounding_llm", "grounding_rules", "quality_llm", "quality_rules"})
            self.assertIn("export_pipeline", graph_nodes)
            self.assertIn("export_version", internal_nodes)

    def test_markdown_syntax_node_repairs_full_lesson_markdown(self) -> None:
        class RepairClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def complete_json(self, system: str, user: str):
                self.calls.append(system)
                self_test.assertIn("complete repaired Markdown body", user)
                self_test.assertIn("Structured Markdown errors", user)
                return {
                    "lesson_id": "lesson-001",
                    "title": "Topic Alpha",
                    "body_markdown": "## Topic Alpha\n\n### 学习目标\n\n修复后的正文。\n\n```text\nsample\n```",
                    "checklist": ["检查 Markdown"],
                    "covered_source_chunk_ids": ["source-chunk-1"],
                    "repair_notes": "Fixed heading and code fence syntax.",
                }

        self_test = self
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            state = initial_state(CompileConfig(course_id="markdown-course", source_files=[], profile={"use_llm_lesson_bodies": True}))
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Topic Alpha",
                    "unit_ids": [],
                    "body": "##Topic Alpha\n\n```\nsample",
                    "checklist": ["检查 Markdown"],
                    "sources": [],
                    "order": 1,
                }
            ]
            state["lesson_bodies"] = {
                "lesson_bodies": [
                    {
                        "lesson_id": "lesson-001",
                        "title": "Topic Alpha",
                        "body_markdown": "##Topic Alpha\n\n```\nsample",
                        "checklist": ["检查 Markdown"],
                        "covered_source_chunk_ids": ["source-chunk-1"],
                    }
                ]
            }
            state["lesson_body_inputs"] = {
                "lessons": [
                    {
                        "lesson_id": "lesson-001",
                        "system_prompt": "original system",
                        "user_prompt": "original user with source chunks",
                        "source_chunk_ids": ["source-chunk-1"],
                    }
                ]
            }

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=RepairClient()):
                state = check_markdown_syntax(state, vault)

            self.assertEqual(state["next_action"], "check_grounding_llm")
            self.assertTrue(state["markdown_syntax_report"]["ok"])
            self.assertEqual(state["markdown_syntax_report"]["lessons"][0]["status"], "repaired")
            self.assertIn("```text", state["lessons"][0]["body"])
            audit = json.loads((vault / "courses" / "markdown-course" / "markdown_repair_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["entries"][0]["round"], 1)
            self.assertIn("markdown_before", audit["entries"][0])
            self.assertIn("markdown_after", audit["entries"][0])

    def test_markdown_syntax_summary_agent_regenerates_after_three_failed_repairs(self) -> None:
        class FallbackRepairClient:
            def __init__(self) -> None:
                self.repair_calls = 0
                self.summary_calls = 0
                self.regenerate_calls = 0

            def complete_json(self, system: str, user: str):
                if "markdown_repair_summary_agent" in system:
                    self.summary_calls += 1
                    return {
                        "repeated_error_types": ["unbalanced_inline_math"],
                        "likely_causes": ["The writer leaves dollar delimiters open."],
                        "repair_rules": ["Use paired inline math delimiters."],
                        "regeneration_instruction": "Regenerate with balanced formulas.",
                    }
                if "fresh Synthesize Lesson Bodies agent" in system:
                    self.regenerate_calls += 1
                    self_test.assertIn("Repair summary", user)
                    self_test.assertIn("Original Synthesize Lesson Bodies user prompt", user)
                    return {
                        "lesson_id": "lesson-001",
                        "title": "Topic Alpha",
                        "body_markdown": "## Topic Alpha\n\n公式 $x$ 已闭合。",
                        "checklist": ["检查公式"],
                        "covered_source_chunk_ids": ["source-chunk-1"],
                    }
                if "continuing the Synthesize Lesson Bodies conversation" in system:
                    self.repair_calls += 1
                    return {
                        "lesson_id": "lesson-001",
                        "title": "Topic Alpha",
                        "body_markdown": "## Topic Alpha\n\n公式 $x 未闭合",
                        "checklist": ["检查公式"],
                        "covered_source_chunk_ids": ["source-chunk-1"],
                    }
                raise AssertionError("Unexpected prompt")

        self_test = self
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            client = FallbackRepairClient()
            state = initial_state(CompileConfig(course_id="markdown-fallback", source_files=[], profile={"use_llm_lesson_bodies": True}))
            state["lessons"] = [
                {
                    "id": "lesson-001",
                    "title": "Topic Alpha",
                    "unit_ids": [],
                    "body": "## Topic Alpha\n\n公式 $x 未闭合",
                    "checklist": ["检查公式"],
                    "sources": [],
                    "order": 1,
                }
            ]
            state["lesson_bodies"] = {
                "lesson_bodies": [
                    {
                        "lesson_id": "lesson-001",
                        "title": "Topic Alpha",
                        "body_markdown": "## Topic Alpha\n\n公式 $x 未闭合",
                        "checklist": ["检查公式"],
                        "covered_source_chunk_ids": ["source-chunk-1"],
                    }
                ]
            }
            state["lesson_body_inputs"] = {
                "lessons": [
                    {
                        "lesson_id": "lesson-001",
                        "system_prompt": "original system",
                        "user_prompt": "original user with source chunks",
                        "source_chunk_ids": ["source-chunk-1"],
                    }
                ]
            }

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = check_markdown_syntax(state, vault)

            self.assertEqual(client.repair_calls, 3)
            self.assertEqual(client.summary_calls, 1)
            self.assertEqual(client.regenerate_calls, 1)
            self.assertTrue(state["markdown_syntax_report"]["ok"])
            self.assertEqual(state["markdown_syntax_report"]["lessons"][0]["strategy"], "summary_regeneration_passed")
            audit = state["markdown_repair_audit"]
            self.assertEqual(audit["summary"]["repair_attempts"], 3)
            self.assertGreaterEqual(audit["summary"]["summary_agent_calls"], 1)

    def test_feedback_patch_exports_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Intro\n\nA compact source paragraph.\n", encoding="utf-8")
            vault = root / "course-vault"
            compile_course([str(source)], "sample-course", vault)

            feedback = record_feedback(vault, "sample-course", "lesson-001", "Please add a bridge.")
            patches = mine_feedback(vault, "sample-course")
            approved = approve_patch(vault, "sample-course", patches[-1]["id"])
            state = apply_approved_patches(vault, "sample-course", "v2")

            lesson_files = list((vault / "courses" / "sample-course" / "versions" / "v2" / "lessons").glob("*.md"))
            self.assertEqual(feedback["status"], "open")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(lesson_files), 1)
            self.assertIn("Bridge:", lesson_files[0].read_text(encoding="utf-8"))

    def test_llm_plan_drives_hierarchical_outline(self) -> None:
        class FakeClient:
            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                return {
                    "sections": [
                        {
                            "title": "Core Topic",
                            "lessons": [
                                {
                                    "title": "Main Method",
                                    "chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                    "why": "Method and comparison belong together.",
                                },
                                {
                                    "title": "几个重要结论",
                                    "chunk_ids": ["source-chunk-3"],
                                    "why": "This should attach to the previous lesson.",
                                },
                            ],
                        }
                    ],
                    "rejected_titles": ["Source Title"],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Main Method\n\nUse this method for the main problem.\n\n"
                "# 方法比较\n\nCompare variants in context.\n\n"
                "# 几个重要结论\n\nConclusion should not become a standalone lesson.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=FakeClient()):
                state = compile_course(
                    [str(source)],
                    "llm-course",
                    vault,
                    "v1",
                    profile={"use_llm": True, "use_llm_structure": False},
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(state["outline"]["sections"][0]["title"], "Core Topic")
            self.assertEqual(len(state["lessons"]), 1)
            self.assertEqual(state["lessons"][0]["title"], "Main Method")
            self.assertEqual(state["lessons"][0]["section_title"], "Core Topic")

    def test_llm_plan_uses_local_cache_on_recompile(self) -> None:
        class CountingClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {"usage": {"input_tokens": 10}}

            def __init__(self) -> None:
                self.calls = 0

            def cache_key(self, system: str, user: str) -> str:
                return "stable-key"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                self.calls += 1
                return {
                    "sections": [
                        {
                            "title": "Core Topic",
                            "lessons": [
                                {
                                    "title": "Main Method",
                                    "chunk_ids": ["source-chunk-1"],
                                    "why": "One source-backed lesson.",
                                }
                            ],
                        }
                    ],
                    "rejected_titles": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("# Main Method\n\nUse this method for the main problem.\n", encoding="utf-8")
            vault = root / "course-vault"
            client = CountingClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                compile_course([str(source)], "cached-course", vault, "v1", profile={"use_llm": True, "use_llm_structure": False})
                state = compile_course([str(source)], "cached-course", vault, "v2", profile={"use_llm": True, "use_llm_structure": False})

            self.assertEqual(client.calls, 1)
            self.assertEqual(state["lessons"][0]["title"], "Main Method")
            self.assertTrue((vault / "courses" / "cached-course" / "llm_cache" / "course_plan-stable-key.json").exists())

    def test_source_brief_enriches_lesson_body(self) -> None:
        class BriefAndPlanClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def cache_key(self, system: str, user: str) -> str:
                return "brief" if "source teaching brief JSON" in user else "plan"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "source teaching brief JSON" in user:
                    return {
                        "course_title": "Hybrid Course",
                        "overview": "Fuse visual and text evidence into a study guide.",
                        "key_concepts": ["Main Method"],
                        "methods": [
                            {
                                "name": "Main Method",
                                "purpose": "Solve the central problem.",
                                "source_chunk_ids": ["source-chunk-1"],
                            }
                        ],
                        "examples": [
                            {
                                "title": "Worked Example",
                                "lesson": "Use one small numerical case to see the method.",
                                "source_chunk_ids": ["source-chunk-1"],
                            }
                        ],
                        "lesson_notes": [
                            {
                                "title": "Main Method",
                                "source_chunk_ids": ["source-chunk-1"],
                                "learning_goal": "Know when to use the method.",
                                "explanation": "Start from the problem, then map each formula to the visual slide.",
                                "example": "Try a two-step example before the formal proof.",
                                "bridge": "This connects the visual intuition to the algebraic rule.",
                            }
                        ],
                    }
                return {
                    "sections": [
                        {
                            "title": "Core Topic",
                            "lessons": [
                                {
                                    "title": "Main Method",
                                    "chunk_ids": ["source-chunk-1"],
                                    "why": "Use the brief to keep the lesson focused.",
                                }
                            ],
                        }
                    ],
                    "rejected_titles": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Main Method\n\nFormula and visual interpretation.\n\n"
                "# Extra Slide\n\nA detail slide that should attach to the planned lesson.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=BriefAndPlanClient()):
                state = compile_course(
                    [str(source)],
                    "hybrid-course",
                    vault,
                    "v1",
                    profile={"use_source_brief": True, "use_llm_brief": True, "use_llm": True, "use_llm_structure": False},
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(state["lessons"]), 1)
            self.assertIn("synthesize_source_brief", _internal_nodes(state))
            self.assertIn("Learning goal: Know when to use the method.", state["lessons"][0]["body"])
            self.assertIn("Example: Try a two-step example", state["lessons"][0]["body"])
            self.assertTrue((vault / "courses" / "hybrid-course" / "source_brief.md").exists())

    def test_lesson_notes_follow_planned_lessons(self) -> None:
        class LessonNotesClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def cache_key(self, system: str, user: str) -> str:
                if "source teaching brief JSON" in user:
                    return "brief"
                if "lesson notes JSON" in user:
                    return "lesson-notes"
                return "plan"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "source teaching brief JSON" in user:
                    return {
                        "course_title": "Hybrid Course",
                        "overview": "Broad source summary.",
                        "key_concepts": ["Method A", "Method B"],
                        "methods": [],
                        "examples": [],
                        "lesson_notes": [
                            {
                                "title": "Broad Topic",
                                "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                "learning_goal": "Understand the broad topic.",
                                "explanation": "Broad explanation.",
                                "example": "Broad example.",
                                "bridge": "Broad bridge.",
                            }
                        ],
                    }
                if "lesson notes JSON" in user:
                    return {
                        "lesson_notes": [
                            {
                                "lesson_id": "planned-001",
                                "title": "Method A",
                                "source_chunk_ids": ["source-chunk-1"],
                                "learning_goal": "Know when Method A applies.",
                                "explanation": "A-specific explanation grounded in chunk one.",
                                "example": "A-specific example.",
                                "bridge": "A leads into Method B.",
                            },
                            {
                                "lesson_id": "planned-002",
                                "title": "Method B",
                                "source_chunk_ids": ["source-chunk-2"],
                                "learning_goal": "Know why Method B is different.",
                                "explanation": "B-specific explanation grounded in chunk two.",
                                "example": "B-specific example.",
                                "bridge": "B closes the sequence.",
                            },
                        ]
                    }
                return {
                    "sections": [
                        {
                            "title": "Core Topic",
                            "lessons": [
                                {
                                    "title": "Method A",
                                    "chunk_ids": ["source-chunk-1"],
                                    "why": "First planned lesson.",
                                },
                                {
                                    "title": "Method B",
                                    "chunk_ids": ["source-chunk-2"],
                                    "why": "Second planned lesson.",
                                },
                            ],
                        }
                    ],
                    "rejected_titles": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Method A\n\nChunk one explains the first method.\n\n"
                "# Method B\n\nChunk two explains the second method.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=LessonNotesClient()):
                state = compile_course(
                    [str(source)],
                    "lesson-note-course",
                    vault,
                    "v1",
                    profile={
                        "use_source_brief": True,
                        "use_llm_brief": True,
                        "use_llm": True,
                        "use_llm_structure": False,
                        "use_lesson_notes": True,
                        "use_llm_lesson_notes": True,
                    },
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(state["lessons"]), 2)
            self.assertEqual(len(state["lesson_notes"]["lesson_notes"]), 2)
            self.assertIn("synthesize_lesson_notes", _internal_nodes(state))
            self.assertIn("A-specific explanation", state["lessons"][0]["body"])
            self.assertIn("B-specific example", state["lessons"][1]["body"])
            self.assertTrue((vault / "courses" / "lesson-note-course" / "lesson_notes.md").exists())

    def test_source_index_and_lesson_notes_are_batched(self) -> None:
        class BatchAwareClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def __init__(self) -> None:
                self.source_index_calls = 0
                self.lesson_note_prompts: list[str] = []

            def cache_key(self, system: str, user: str) -> str:
                return f"{len(user)}-{self.source_index_calls}-{len(self.lesson_note_prompts)}"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "Lesson batch" in user:
                    self.lesson_note_prompts.append(user)
                if "context pack" in user and "candidate_lessons" in user:
                    self.source_index_calls += 1
                    chunk_ids = [line.strip()[4:] for line in user.splitlines() if line.strip().startswith("id: ")]
                    return {
                        "pack": {
                            "pack_id": f"pack-{self.source_index_calls:03d}",
                            "title": f"Pack {self.source_index_calls}",
                            "summary": "Compact pack summary.",
                            "key_concepts": ["Method"],
                            "methods": [],
                            "examples": [],
                            "candidate_lessons": [
                                {"title": f"Pack {self.source_index_calls} Lesson", "reason": "Useful topic.", "source_chunk_ids": chunk_ids[:1]}
                            ],
                            "source_chunk_ids": chunk_ids,
                        }
                    }
                if "source teaching brief JSON" in user:
                    if "Source index context packs:" not in user:
                        raise AssertionError("source brief should use source index")
                    return {
                        "course_title": "Indexed Course",
                        "overview": "Indexed overview.",
                        "key_concepts": ["Method"],
                        "methods": [],
                        "examples": [],
                        "lesson_notes": [],
                    }
                if "course plan JSON" in user:
                    if "Source index context packs:" not in user:
                        raise AssertionError("course plan should use source index")
                    return {
                        "sections": [
                            {
                                "title": "Core",
                                "lessons": [
                                    {"title": "Method One", "chunk_ids": ["source-chunk-1"], "why": "First topic."},
                                    {"title": "Method Two", "chunk_ids": ["source-chunk-3"], "why": "Second topic."},
                                ],
                            }
                        ],
                        "rejected_titles": [],
                    }
                if "lesson notes JSON" in user:
                    lesson_id = "planned-001" if "Method One" in user else "planned-002"
                    title = "Method One" if lesson_id == "planned-001" else "Method Two"
                    chunk_id = "source-chunk-1" if lesson_id == "planned-001" else "source-chunk-3"
                    return {
                        "lesson_notes": [
                            {
                                "lesson_id": lesson_id,
                                "title": title,
                                "source_chunk_ids": [chunk_id],
                                "learning_goal": f"Study {title}.",
                                "explanation": f"{title} detailed explanation.",
                                "example": f"{title} example.",
                                "bridge": "Next topic.",
                            }
                        ]
                    }
                raise AssertionError("Unexpected prompt")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Method One\n\nDefinition and formula for method one.\n\n"
                "# Detail\n\nSupporting detail.\n\n"
                "# Method Two\n\nDefinition and formula for method two.\n\n"
                "# Example\n\nWorked example.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            client = BatchAwareClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course(
                    [str(source)],
                    "batched-course",
                    vault,
                    "v1",
                    profile={
                        "use_source_index": True,
                        "use_llm_source_index": True,
                        "source_index_batch_chunks": 2,
                        "use_source_brief": True,
                        "use_llm_brief": True,
                        "use_llm": True,
                        "use_llm_structure": False,
                        "use_lesson_notes": True,
                        "use_llm_lesson_notes": True,
                        "lesson_note_batch_lessons": 1,
                        "detailed_lessons": True,
                    },
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(client.source_index_calls, 2)
            self.assertEqual(len(state["lesson_notes"]["lesson_notes"]), 2)
            self.assertEqual(len(state["source_index"]["packs"]), 2)
            self.assertIn("build_source_index", _internal_nodes(state))
            self.assertIn("### 课件要点", state["lessons"][0]["body"])
            self.assertTrue((vault / "courses" / "batched-course" / "source_index.md").exists())

    def test_llm_lesson_body_uses_only_lesson_chunks(self) -> None:
        class BodyClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def __init__(self) -> None:
                self.body_prompts: list[str] = []

            def cache_key(self, system: str, user: str) -> str:
                return f"body-{len(self.body_prompts)}-{len(user)}"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "course plan JSON" in user:
                    return {
                        "sections": [
                            {
                                "title": "Core",
                                "lessons": [
                                    {"title": "Method A", "chunk_ids": ["source-chunk-1"], "why": "First method."},
                                    {"title": "Method B", "chunk_ids": ["source-chunk-2"], "why": "Second method."},
                                ],
                            }
                        ],
                        "rejected_titles": [],
                    }
                if "Write one detailed lesson JSON" in user:
                    self.body_prompts.append(user)
                    is_method_a = "Lesson title: Method A" in user
                    if is_method_a and "Unrelated Method B" in user:
                        raise AssertionError("lesson body prompt should not include unrelated chunks")
                    lesson_id = "lesson-001" if is_method_a else "lesson-002"
                    title = "Method A" if is_method_a else "Method B"
                    chunk_id = "source-chunk-1" if is_method_a else "source-chunk-2"
                    return {
                        "lesson_id": lesson_id,
                        "title": title,
                        "body_markdown": (
                            f"## {title}\n\n"
                            f"### 学习目标\n理解 {title}。\n\n"
                            "### 概念与直觉\nMethod A explains a source-backed formula.\n\n"
                            "### 方法步骤\n1. Read the formula. 2. Apply the rule.\n\n"
                            "### 例题讲解\nUse a small source-backed example."
                        ),
                        "checklist": [f"说出 {title} 的用途", f"复述 {title} 的步骤"],
                        "covered_source_chunk_ids": [chunk_id],
                    }
                raise AssertionError("Unexpected prompt")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Method A\n\nMethod A explains a source-backed formula.\n\n"
                "# Method B\n\nUnrelated Method B should not be sent to Method A body writing.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            client = BodyClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course(
                    [str(source)],
                    "body-course",
                    vault,
                    "v1",
                    profile={"use_llm": True, "use_llm_structure": False, "use_llm_lesson_bodies": True, "lesson_body_max_chars": 2400},
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(client.body_prompts), 2)
            self.assertIn("synthesize_lesson_bodies", _internal_nodes(state))
            self.assertIn("### 方法步骤", state["lessons"][0]["body"])
            self.assertEqual(state["lessons"][0]["checklist"], ["说出 Method A 的用途", "复述 Method A 的步骤"])
            self.assertTrue((vault / "courses" / "body-course" / "lesson_bodies.md").exists())

    def test_constrained_lesson_body_enrichment_is_prompted_and_recorded(self) -> None:
        class EnrichmentClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def __init__(self) -> None:
                self.body_prompt = ""

            def cache_key(self, system: str, user: str) -> str:
                return f"enrichment-{len(user)}"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "course plan JSON" in user:
                    return {
                        "sections": [
                            {
                                "title": "Generic Topic Group",
                                "lessons": [
                                    {
                                        "title": "Topic Alpha",
                                        "chunk_ids": ["source-chunk-1"],
                                        "why": "The source has a skipped example and a proof hint.",
                                    }
                                ],
                            }
                        ],
                        "rejected_titles": [],
                    }
                if "Write one detailed lesson JSON" in user:
                    self.body_prompt = user
                    self._assert_enrichment_prompt(user)
                    return {
                        "lesson_id": "lesson-001",
                        "title": "Topic Alpha",
                        "body_markdown": (
                            "## Topic Alpha\n\n"
                            "### 学习目标\n理解源材料中的递推步骤。\n\n"
                            "### 局部补全\n补全推导（依据源材料 + 标准代数步骤）：先固定已给的低阶关系，"
                            "再逐层代入到目标表达式中，得到例题中省略的中间项。\n\n"
                            "### 易混辨析\n递推构造不是定义替换；它是用已有关系逐步形成目标表达式。"
                        ),
                        "checklist": ["解释递推步骤", "指出递推构造与定义替换的区别"],
                        "covered_source_chunk_ids": ["source-chunk-1"],
                        "local_enrichments": [
                            {
                                "type": "example_steps",
                                "title": "例题跳步",
                                "source_chunk_ids": ["source-chunk-1", "source-chunk-99"],
                                "status": "standard_derivation",
                                "content": "由已给关系补出相邻两层之间的代入关系，不补新数值。",
                            },
                            {
                                "type": "concept_disambiguation",
                                "title": "递推构造与定义替换",
                                "source_chunk_ids": ["source-chunk-1"],
                                "status": "source_supported",
                                "content": "递推构造强调逐层使用已知关系，定义替换只是在同一层改写符号。",
                            },
                        ],
                    }
                raise AssertionError("Unexpected prompt")

            def _assert_enrichment_prompt(self, user: str) -> None:
                self_test = CompilerTests()
                self_test.assertIn("Constrained local enrichment requirements", user)
                self_test.assertIn("局部补全", user)
                self_test.assertIn("易混辨析", user)
                self_test.assertIn("at most 3 enrichment items", user)
                self_test.assertIn("Do not invent new examples, numbers, constants, formulas", user)
                self_test.assertIn("local_enrichments", user)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Topic Alpha\n\n"
                "源材料给出递推关系后，例题直接写出目标表达式，中间代入步骤略。\n"
                "定理证明留作思考题：目标性质可由递推关系推出。\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"
            client = EnrichmentClient()

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=client):
                state = compile_course(
                    [str(source)],
                    "enriched-course",
                    vault,
                    "v1",
                    profile={
                        "use_llm": True,
                        "use_llm_structure": False,
                        "use_llm_lesson_bodies": True,
                        "lesson_body_enrichment": "constrained",
                        "lesson_body_max_chars": 2600,
                    },
                )

            body_record = state["lesson_bodies"]["lesson_bodies"][0]
            persisted = json.loads((vault / "courses" / "enriched-course" / "lesson_bodies.json").read_text(encoding="utf-8"))

            self.assertEqual(state["next_action"], "done")
            self.assertIn("### 局部补全", state["lessons"][0]["body"])
            self.assertIn("### 易混辨析", state["lessons"][0]["body"])
            self.assertEqual(len(body_record["local_enrichments"]), 2)
            self.assertEqual(body_record["local_enrichments"][0]["source_chunk_ids"], ["source-chunk-1"])
            self.assertEqual(persisted["lesson_bodies"][0]["local_enrichments"][1]["type"], "concept_disambiguation")

    def test_learn_by_doing_profile_drives_task_first_prompts(self) -> None:
        test_case = self

        class LearnByDoingClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def cache_key(self, system: str, user: str) -> str:
                if "context pack" in user:
                    return "learn-index"
                if "source teaching brief JSON" in user:
                    return "learn-brief"
                if "course plan JSON" in user:
                    return "learn-plan"
                if "lesson notes JSON" in user:
                    return "learn-notes"
                return "learn-body"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                self._assert_learn_prompt(system, user)
                if "source teaching brief JSON" in user:
                    return {
                        "course_title": "Software Tutorial",
                        "overview": "Learn by doing with setup, run, and inspection tasks.",
                        "key_concepts": ["setup"],
                        "methods": [],
                        "examples": [
                            {
                                "title": "Small run",
                                "lesson": "Use the example to practice setup.",
                                "source_chunk_ids": ["source-chunk-2"],
                            }
                        ],
                        "tasks": [
                            {
                                "title": "Configure and run a small example",
                                "outcome": "A runnable result is produced.",
                                "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                            }
                        ],
                        "workflows": [
                            {
                                "title": "Configure and run a small example",
                                "steps": ["Edit config", "Run command"],
                                "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                            }
                        ],
                        "failure_modes": [],
                        "lesson_notes": [],
                    }
                if "course plan JSON" in user:
                    test_case.assertIn("Do not preserve the manual's feature-order table of contents", user)
                    test_case.assertIn("task:", user)
                    return {
                        "sections": [
                            {
                                "title": "First Runnable Workflow",
                                "lessons": [
                                    {
                                        "title": "Configure and run a small example",
                                        "chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                        "why": "This combines reference setup with a later runnable example.",
                                        "lesson_type": "task",
                                    }
                                ],
                            }
                        ],
                        "rejected_titles": ["Feature reference"],
                    }
                if "context pack" in user:
                    return {
                        "pack": {
                            "pack_id": "pack-001",
                            "title": "Manual Tasks",
                            "summary": "Setup and run a small software example.",
                            "key_concepts": ["setup", "example"],
                            "methods": [],
                            "examples": [
                                {
                                    "title": "Small run",
                                    "lesson": "Run the example after setup.",
                                    "source_chunk_ids": ["source-chunk-2"],
                                }
                            ],
                            "tasks": [
                                {
                                    "title": "Configure and run a small example",
                                    "outcome": "A runnable result is produced.",
                                    "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                }
                            ],
                            "workflows": [
                                {
                                    "title": "Configure and run a small example",
                                    "steps": ["Edit the configuration.", "Run the command."],
                                    "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                }
                            ],
                            "failure_modes": [],
                            "candidate_lessons": [
                                {
                                    "title": "Configure and run a small example",
                                    "reason": "Combines feature setup with the later example.",
                                    "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                }
                            ],
                            "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                        }
                    }
                if "lesson notes JSON" in user:
                    test_case.assertIn("operation steps", user)
                    return {
                        "lesson_notes": [
                            {
                                "lesson_id": "planned-001",
                                "title": "Configure and run a small example",
                                "source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                                "learning_goal": "Run the smallest supported workflow.",
                                "explanation": "The setup feature matters because it selects the files used by the later example.",
                                "example": "Use the small example as the first run.",
                                "bridge": "Change one input after the first successful run.",
                                "task": "Configure the problem and run the documented example.",
                                "steps": ["Edit the configuration.", "Run the documented command."],
                                "expected_result": "The example run produces an output to inspect.",
                                "failure_modes": ["Missing configuration causes the run to fail."],
                            }
                        ]
                    }
                if "Write one detailed lesson JSON" in user:
                    test_case.assertIn("hands-on software tutorial", user)
                    test_case.assertIn("操作步骤", user)
                    return {
                        "lesson_id": "lesson-001",
                        "title": "Configure and run a small example",
                        "body_markdown": (
                            "## Configure and run a small example\n\n"
                            "### 学习目标\nRun the smallest supported workflow.\n\n"
                            "### 本节任务\nConfigure the problem and run the documented example.\n\n"
                            "### 操作步骤\n1. Edit the configuration.\n2. Run the documented command.\n\n"
                            "### 预期结果\nThe example run produces an output to inspect.\n\n"
                            "### 背后的功能\nThe setup feature selects files used by the example."
                        ),
                        "checklist": ["Edit the configuration", "Run the documented command", "Check the output"],
                        "covered_source_chunk_ids": ["source-chunk-1", "source-chunk-2"],
                    }
                raise AssertionError("Unexpected prompt")

            def _assert_learn_prompt(self, system: str, user: str) -> None:
                joined = system + "\n" + user
                test_case.assertIn("learn-by-doing", joined.lower())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text(
                "# Setup feature\n\nEdit the configuration before running the program.\n\n"
                "# Later worked example\n\nRun the documented command and inspect the output.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=LearnByDoingClient()):
                state = compile_course(
                    [str(source)],
                    "learn-course",
                    vault,
                    "v1",
                    profile={
                        "course_style": "learn-by-doing",
                        "use_source_index": True,
                        "use_llm_source_index": True,
                        "source_index_batch_chunks": 4,
                        "use_source_brief": True,
                        "use_llm_brief": True,
                        "use_llm": True,
                        "use_llm_structure": False,
                        "use_lesson_notes": True,
                        "use_llm_lesson_notes": True,
                        "use_llm_lesson_bodies": True,
                        "lesson_body_max_chars": 4000,
                        "detailed_lessons": True,
                    },
                )

            self.assertEqual(state["next_action"], "done")
            self.assertFalse(state["errors"])
            self.assertEqual(state["course_plan"]["sections"][0]["lessons"][0]["lesson_type"], "task")
            self.assertEqual(state["lessons"][0]["lesson_type"], "task")
            self.assertIn("### 操作步骤", state["lessons"][0]["body"])
            self.assertEqual(len(state["source_brief"]["tasks"]), 1)
            self.assertTrue((vault / "courses" / "learn-course" / "source_index.md").exists())

    def test_source_index_plan_uses_brief_tasks_without_llm_plan_call(self) -> None:
        class BriefOnlyClient:
            provider = "anthropic"
            base_url = "https://open.bigmodel.cn/api/anthropic"
            model = "GLM-4.7"
            cache_identity = {"provider": provider, "base_url": base_url, "model": model}
            last_metadata = {}

            def cache_key(self, system: str, user: str) -> str:
                return "brief-only"

            def complete_json(self, system: str, user: str):
                if _is_compile_plan_review_prompt(system, user):
                    return _passing_compile_plan_review()
                if _is_validation_prompt(system, user):
                    return _passing_validation_result()
                if "course plan JSON" in user:
                    raise AssertionError("source-index plan mode should not call the LLM planner")
                if "source teaching brief JSON" not in user:
                    raise AssertionError("unexpected LLM call")
                return {
                    "course_title": "Software Tutorial",
                    "overview": "Task-first software tutorial.",
                    "key_concepts": ["setup", "run"],
                    "methods": [],
                    "examples": [
                        {
                            "title": "Run the supplied example",
                            "lesson": "Use the example after configuration.",
                            "source_chunk_ids": ["manual-chunk-2"],
                        }
                    ],
                    "tasks": [
                        {
                            "title": "Configure the first runnable case",
                            "outcome": "Generate a configured application.",
                            "source_chunk_ids": ["manual-chunk-1", "manual-chunk-2"],
                        }
                    ],
                    "workflows": [
                        {
                            "title": "Configure, run, and inspect output",
                            "steps": ["Configure the case.", "Run it.", "Inspect output."],
                            "source_chunk_ids": ["manual-chunk-1", "manual-chunk-2"],
                        }
                    ],
                    "failure_modes": [
                        {
                            "symptom": "Configuration fails with a missing unit.",
                            "fix": "Check the required unit directives.",
                            "source_chunk_ids": ["manual-chunk-1"],
                        }
                    ],
                    "lesson_notes": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.md"
            source.write_text(
                "# Configure\n\nUse setup and unit directives to configure the application.\n\n"
                "# Example\n\nRun the supplied example and inspect output.\n",
                encoding="utf-8",
            )
            vault = root / "course-vault"

            with patch("agent_graph.nodes.LLMClient.from_env", return_value=BriefOnlyClient()):
                state = compile_course(
                    [str(source)],
                    "brief-plan-course",
                    vault,
                    "v1",
                    profile={
                        "course_style": "learn-by-doing",
                        "use_source_index": True,
                        "use_llm_source_index": False,
                        "use_source_brief": True,
                        "use_llm_brief": True,
                        "use_llm": True,
                        "use_llm_structure": False,
                        "use_source_index_plan": True,
                        "detailed_lessons": True,
                    },
                )

            planned_titles = [
                lesson["title"]
                for section in state["course_plan"]["sections"]
                for lesson in section["lessons"]
            ]
            titles = [lesson["title"] for lesson in state["lessons"]]
            lesson_types = [lesson.get("lesson_type") for lesson in state["lessons"]]

            self.assertEqual(state["next_action"], "done")
            self.assertFalse(state["errors"])
            self.assertIn("Configure, run, and inspect output", planned_titles)
            self.assertIn("Run the supplied example", planned_titles)
            self.assertIn("动手完成：Configuration fails with a missing unit.", planned_titles)
            self.assertIn("Configure, run, and inspect output", titles)
            self.assertIn("task", lesson_types)
            self.assertEqual(state["course_plan"]["rejected_titles"], ["Source feature order; planned from task-first source brief."])
            self.assertTrue((vault / "courses" / "brief-plan-course" / "course_plan.json").exists())


if __name__ == "__main__":
    unittest.main()
