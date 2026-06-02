from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from agent_graph.compiler import compile_course
from agent_graph.feedback import apply_approved_patches, approve_patch, mine_feedback, record_feedback
from agent_graph.nodes import _find_cached_lesson_body_by_id, _repair_course_plan_coverage, _source_index_for_prompt, check_grounding
from agent_graph.state import CompileConfig, initial_state


class CompilerTests(unittest.TestCase):
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
            checked = check_grounding(state, Path(tmp) / "vault")

        self.assertFalse(checked["validation_report"]["ok"])
        self.assertEqual(checked["next_action"], "repair_course")
        self.assertEqual(len(checked["validation_report"]["failures"]), 1)

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
                    profile={"use_llm": True},
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
                compile_course([str(source)], "cached-course", vault, "v1", profile={"use_llm": True})
                state = compile_course([str(source)], "cached-course", vault, "v2", profile={"use_llm": True})

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
                    profile={"use_source_brief": True, "use_llm_brief": True, "use_llm": True},
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(state["lessons"]), 1)
            self.assertIn("synthesize_source_brief", [item["node"] for item in state["graph_run_log"]])
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
                        "use_lesson_notes": True,
                        "use_llm_lesson_notes": True,
                    },
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(state["lessons"]), 2)
            self.assertEqual(len(state["lesson_notes"]["lesson_notes"]), 2)
            self.assertIn("synthesize_lesson_notes", [item["node"] for item in state["graph_run_log"]])
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
            self.assertIn("build_source_index", [item["node"] for item in state["graph_run_log"]])
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
                    profile={"use_llm": True, "use_llm_lesson_bodies": True, "lesson_body_max_chars": 2400},
                )

            self.assertEqual(state["next_action"], "done")
            self.assertEqual(len(client.body_prompts), 2)
            self.assertIn("synthesize_lesson_bodies", [item["node"] for item in state["graph_run_log"]])
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
