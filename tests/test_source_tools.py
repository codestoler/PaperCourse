from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_graph.source_tools import SourceLocator, SourceRevisionTool
from agent_graph.state import CompileConfig, initial_state


class SourceToolTests(unittest.TestCase):
    def test_source_locator_searches_context_images_formulas_and_tables(self) -> None:
        chunks = [
            {
                "id": "chunk-1",
                "source": "source.md",
                "source_file": "source.md",
                "title": "Newton Method",
                "content": "Newton iteration uses\n$$x_{k+1}=x_k-f(x_k)/f'(x_k)$$\nfor root finding.",
                "page": 2,
                "block_id": "block-a",
                "bbox": [1, 2, 3, 4],
                "start_line": 5,
                "end_line": 8,
            },
            {
                "id": "chunk-2",
                "source": "source.md",
                "source_file": "source.md",
                "title": "Error Table",
                "content": "| k | error |\n| - | - |\n| 1 | 0.1 |",
                "page": 3,
                "block_id": "block-b",
                "bbox": [5, 6, 7, 8],
            },
        ]
        images = {
            "images": [
                {
                    "id": "image-001",
                    "source_chunk_id": "chunk-1",
                    "caption": "Newton tangent diagram",
                    "summary": "A tangent line visualizes one Newton update.",
                    "asset_url": "/assets/newton.png",
                    "page_idx": 2,
                    "bbox": [9, 10, 11, 12],
                }
            ]
        }

        locator = SourceLocator(chunks, images)

        text = locator.search("Newton root", kinds=["text"])
        formula = locator.search("x_{k+1}", kinds=["formula"])
        table = locator.search("error", kinds=["table"])
        image = locator.find_images("tangent")
        context = locator.get_context(["chunk-1"])
        located = locator.locate("Newton iteration finds a root by tangent updates.")

        self.assertEqual(text[0]["source_id"], "chunk-1")
        self.assertEqual(formula[0]["type"], "formula")
        self.assertEqual(table[0]["type"], "table")
        self.assertEqual(image[0]["image_id"], "image-001")
        self.assertEqual(context["images"][0]["source_id"], "chunk-1")
        self.assertEqual(located[0]["source_id"], "chunk-1")

    def test_source_locator_verifies_stable_citation_ids_and_quotes(self) -> None:
        locator = SourceLocator(
            [
                {
                    "id": "chunk-1",
                    "source": "source.md",
                    "title": "Grounding",
                    "content": "Grounded text.",
                    "page": 4,
                    "block_id": "chunk-1",
                    "bbox": [0, 0, 1, 1],
                }
            ]
        )

        ok = locator.verify_citations(
            [
                {
                    "source_id": "chunk-1",
                    "chunk_id": "chunk-1",
                    "block_id": "chunk-1",
                    "page": 4,
                    "bbox": [0, 0, 1, 1],
                    "quote": "Grounded text.",
                }
            ]
        )
        bad = locator.verify_citations([{"chunk_id": "chunk-1", "block_id": "other", "page": 9, "quote": "Different."}])

        self.assertTrue(ok["ok"])
        self.assertFalse(bad["ok"])
        self.assertEqual(
            {failure["type"] for failure in bad["failures"]},
            {"citation_source_id_missing", "block_id_untraceable", "source_page_untraceable", "bbox_missing", "source_quote_untraceable"},
        )

    def test_source_revision_tool_applies_and_rolls_back_lesson_body_patch(self) -> None:
        state = initial_state(CompileConfig(course_id="revision-course", source_files=[]))
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Topic",
                "body": "before",
                "sources": [],
                "checklist": ["Read"],
                "unit_ids": [],
                "order": 1,
            }
        ]
        tool = SourceRevisionTool()

        patch = tool.propose_lesson_body_patch(
            state,
            "lesson-001",
            "after",
            reason="Fix unsupported paragraph using verified evidence.",
            evidence=[{"source_id": "chunk-1"}],
        )
        applied = tool.apply_patch(state, patch)
        rolled_back = tool.rollback_patch(applied, applied["compile_patches"][-1])

        self.assertEqual(applied["lessons"][0]["body"], "after")
        self.assertIn("-before", applied["compile_patches"][-1]["operations"][0]["diff"])
        self.assertEqual(applied["compile_patches"][-1]["reason"], "Fix unsupported paragraph using verified evidence.")
        self.assertEqual(applied["compile_patches"][-1]["evidence"][0]["source_id"], "chunk-1")
        self.assertEqual(rolled_back["lessons"][0]["body"], "before")

    def test_source_revision_tool_compares_version_lesson_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "course-vault"
            v1 = vault / "courses" / "sample" / "versions" / "v1" / "lessons"
            v2 = vault / "courses" / "sample" / "versions" / "v2" / "lessons"
            v1.mkdir(parents=True)
            v2.mkdir(parents=True)
            (v1 / "001-topic.md").write_text("before", encoding="utf-8")
            (v2 / "001-topic.md").write_text("after", encoding="utf-8")
            (v2 / "002-extra.md").write_text("extra", encoding="utf-8")

            diff = SourceRevisionTool(vault, "sample").compare_versions("v1", "v2")

        self.assertEqual(diff["lesson_files_changed"], ["001-topic.md"])
        self.assertEqual(diff["lesson_files_added"], ["002-extra.md"])
        self.assertEqual(diff["lesson_files_removed"], [])

    def test_source_revision_tool_supports_split_merge_image_and_citation_patches(self) -> None:
        state = initial_state(CompileConfig(course_id="revision-course", source_files=[]))
        state["lessons"] = [
            {
                "id": "lesson-001",
                "title": "Combined",
                "body": "alpha beta",
                "sources": [{"source_id": "chunk-1", "chunk_id": "chunk-1", "quote": "alpha"}],
                "images": [{"id": "image-a"}, {"id": "image-b"}],
                "checklist": ["Read"],
                "unit_ids": ["unit-1", "unit-2"],
                "order": 1,
            },
            {
                "id": "lesson-002",
                "title": "Other",
                "body": "other",
                "sources": [{"source_id": "chunk-2", "chunk_id": "chunk-2", "quote": "other"}],
                "images": [],
                "checklist": ["Read"],
                "unit_ids": ["unit-3"],
                "order": 2,
            },
        ]
        tool = SourceRevisionTool()

        split_patch = tool.propose_split_lesson_patch(
            state,
            "lesson-001",
            [
                {**state["lessons"][0], "id": "lesson-001a", "title": "Alpha", "body": "alpha"},
                {**state["lessons"][0], "id": "lesson-001b", "title": "Beta", "body": "beta"},
            ],
            reason="Split dense lesson by evidence scope.",
        )
        split_state = tool.apply_patch(state, split_patch)
        self.assertEqual([lesson["id"] for lesson in split_state["lessons"]], ["lesson-001a", "lesson-001b", "lesson-002"])

        merge_patch = tool.propose_merge_lessons_patch(
            split_state,
            ["lesson-001a", "lesson-001b"],
            {**split_state["lessons"][0], "id": "lesson-001", "title": "Combined Again", "body": "alpha\n\nbeta"},
            reason="Merge over-split lessons with shared evidence.",
        )
        merged_state = tool.apply_patch(split_state, merge_patch)
        self.assertEqual([lesson["id"] for lesson in merged_state["lessons"]], ["lesson-001", "lesson-002"])
        self.assertEqual(merged_state["lessons"][0]["title"], "Combined Again")

        image_patch = tool.propose_move_image_patch(
            state,
            "lesson-001",
            "image-b",
            0,
            reason="Move image next to the source-supported explanation.",
        )
        image_state = tool.apply_patch(state, image_patch)
        self.assertEqual([image["id"] for image in image_state["lessons"][0]["images"]], ["image-b", "image-a"])

        citation_patch = tool.propose_replace_citation_patch(
            state,
            "lesson-001",
            0,
            {"source_id": "chunk-3", "chunk_id": "chunk-3", "quote": "replacement"},
            reason="Replace wrong citation with verified evidence.",
        )
        citation_state = tool.apply_patch(state, citation_patch)
        self.assertEqual(citation_state["lessons"][0]["sources"][0]["source_id"], "chunk-3")

        evidence_patch = tool.propose_add_evidence_patch(
            "lesson-001",
            {"source_id": "chunk-4", "chunk_id": "chunk-4", "quote": "supporting"},
            reason="Supplement missing evidence for a paragraph.",
        )
        evidence_state = tool.apply_patch(state, evidence_patch)
        self.assertEqual(evidence_state["lessons"][0]["sources"][-1]["source_id"], "chunk-4")

        state["compile_plan"] = {"hierarchy": {"lessons": [{"lesson_id": "lesson-001", "title": "Old"}]}}
        plan_patch = tool.propose_state_path_patch(
            state,
            ["compile_plan", "hierarchy", "lessons", 0, "title"],
            "New",
            reason="Retitle compile plan lesson after evidence-backed split.",
            evidence=[{"source_id": "chunk-1"}],
        )
        plan_state = tool.apply_patch(state, plan_patch)
        rolled_back = tool.rollback_patch(plan_state, plan_state["compile_patches"][-1])

        self.assertEqual(plan_state["compile_plan"]["hierarchy"]["lessons"][0]["title"], "New")
        self.assertEqual(rolled_back["compile_plan"]["hierarchy"]["lessons"][0]["title"], "Old")


if __name__ == "__main__":
    unittest.main()
