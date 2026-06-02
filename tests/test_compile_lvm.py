from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.compile_lvm import page_heading, render_lvm_markdown, source_cache_id


class CompileLVMTests(unittest.TestCase):
    def test_source_cache_id_depends_on_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "slides.pdf"
            second = Path(tmp) / "slides-copy.pdf"
            first.write_bytes(b"pdf-a")
            second.write_bytes(b"pdf-b")

            self.assertNotEqual(source_cache_id(first), source_cache_id(second))
            self.assertTrue(source_cache_id(first).startswith("slides-"))

    def test_render_lvm_markdown_preserves_page_image_and_analysis(self) -> None:
        markdown = render_lvm_markdown(
            Path("chapter.pdf"),
            [{"page": 1, "image": "pages/page-001.png", "analysis": "## 主题\n\n公式 $x^2$"}],
        )

        self.assertIn("# chapter", markdown)
        self.assertIn("## Page 1: 主题", markdown)
        self.assertIn("![Page 1](pages/page-001.png)", markdown)
        self.assertIn("公式 $x^2$", markdown)

    def test_page_heading_skips_generic_visual_sections(self) -> None:
        heading = page_heading(3, "### 视觉与排版说明\n\n### Topic Alpha")

        self.assertEqual(heading, "Page 3: Topic Alpha")


if __name__ == "__main__":
    unittest.main()
