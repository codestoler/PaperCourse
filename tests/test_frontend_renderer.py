from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendRendererTests(unittest.TestCase):
    def render_markdown(self, markdown: str) -> str:
        script = (
            "const { renderMarkdown } = require('./frontend/app.js');"
            f"process.stdout.write(renderMarkdown({json.dumps(markdown)}));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout

    def run_node_json(self, script: str):
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(result.stdout)

    def test_gfm_table_renders_as_scrollable_html_table(self) -> None:
        html = self.render_markdown(
            textwrap.dedent(
                """
                | Method | Formula | Note |
                | --- | ---: | :--- |
                | Alpha | $x_i^*$ | stable |
                | Beta | `code` | wide value |
                """
            ).strip()
        )

        self.assertIn('<div class="table-scroll">', html)
        self.assertIn("<table>", html)
        self.assertIn("<thead>", html)
        self.assertIn("<th>Method</th>", html)
        self.assertIn('<td style="text-align: right">$x_i^*$</td>', html)
        self.assertIn("<code>code</code>", html)

    def test_fenced_code_block_preserves_symbols_without_markdown_parsing(self) -> None:
        html = self.render_markdown(
            textwrap.dedent(
                """
                ```python
                value = {"a": 1}
                # *not emphasis*
                ```
                """
            ).strip()
        )

        self.assertIn('<pre class="code-block" data-language="python"><code>', html)
        self.assertIn("&quot;a&quot;", html)
        self.assertIn("# *not emphasis*", html)
        self.assertNotIn("<em>not emphasis</em>", html)

    def test_formula_block_keeps_cases_and_matrix_lines_out_of_lists(self) -> None:
        html = self.render_markdown(
            textwrap.dedent(
                r"""
                $$
                f(x)=\begin{cases}
                - x, & x < 0
                - x^2, & x \ge 0
                \end{cases}
                $$

                \begin{pmatrix}
                - a & b
                - c & d
                \end{pmatrix}
                """
            ).strip()
        )

        self.assertEqual(html.count('<div class="math-block">'), 2)
        self.assertNotIn("<ul>", html)
        self.assertIn(r"\begin{cases}", html)
        self.assertIn(r"x, &amp; x &lt; 0 \\", html)
        self.assertIn(r"\begin{pmatrix}", html)
        self.assertIn(r"a &amp; b \\", html)

    def test_renderer_does_not_clean_ocr_square_markers(self) -> None:
        html = self.render_markdown("□定义6.7：这是 compiler 需要清理的源标记。")

        self.assertIn("□定义6.7", html)

    def test_latex_like_lines_with_pipes_are_not_tables(self) -> None:
        html = self.render_markdown(
            textwrap.dedent(
                r"""
                \[
                \left| x \right| = \begin{cases}
                x, & x \ge 0 \\
                -x, & x < 0
                \end{cases}
                \]
                """
            ).strip()
        )

        self.assertNotIn("<table>", html)
        self.assertIn('<div class="math-block">', html)
        self.assertIn(r"\left| x \right|", html)

    def test_reading_position_is_keyed_by_course_version_and_lesson(self) -> None:
        data = self.run_node_json(
            """
            const { parseReadingKey, withSavedReadingPosition } = require('./frontend/app.js');
            let positions = {};
            positions = withSavedReadingPosition(positions, 'course-a/v1/001.md', 120.4, 33.8, 10);
            positions = withSavedReadingPosition(positions, 'course-a/v2/001.md', 10, 5, 11);
            positions = withSavedReadingPosition(positions, 'course-b/v1/001.md', -20, 140, 12);
            process.stdout.write(JSON.stringify({positions, parsed: parseReadingKey('course-a/v1/001.md')}));
            """
        )

        positions = data["positions"]
        self.assertEqual(positions["course-a/v1/001.md"]["scrollY"], 120)
        self.assertEqual(positions["course-a/v1/001.md"]["progress"], 34)
        self.assertEqual(positions["course-a/v2/001.md"]["scrollY"], 10)
        self.assertEqual(positions["course-b/v1/001.md"]["scrollY"], 0)
        self.assertEqual(positions["course-b/v1/001.md"]["progress"], 100)
        self.assertEqual(data["parsed"], {"course": "course-a", "version": "v1", "file": "001.md"})

    def test_compile_requirements_default_format_and_user_overrides(self) -> None:
        data = self.run_node_json(
            """
            const { defaultCompileRequirements, formatRequirements, parseRequirements } = require('./frontend/app.js');
            const defaults = defaultCompileRequirements();
            const formatted = formatRequirements(defaults);
            const parsed = parseRequirements('exercise_ratio: 每节至少 2 题。\\ncustom_rule: 只使用资料库证据。');
            process.stdout.write(JSON.stringify({defaults, formatted, parsed}));
            """
        )

        self.assertIn("course_structure", data["defaults"])
        self.assertIn("formula_handling", data["defaults"])
        self.assertIn("image_handling", data["defaults"])
        self.assertIn("code_block_handling", data["defaults"])
        self.assertIn("course_structure:", data["formatted"])
        self.assertEqual(data["parsed"]["exercise_ratio"], "每节至少 2 题。")
        self.assertEqual(data["parsed"]["custom_rule"], "只使用资料库证据。")
        self.assertIn("course_structure", data["parsed"])

    def test_project_status_labels_cover_compile_flow(self) -> None:
        data = self.run_node_json(
            """
            const { projectStatusLabel } = require('./frontend/app.js');
            const states = ['not_started', 'queued', 'analyzing', 'awaiting_confirmation', 'compiling', 'succeeded', 'failed'];
            process.stdout.write(JSON.stringify(Object.fromEntries(states.map((state) => [state, projectStatusLabel(state)]))));
            """
        )

        self.assertEqual(data["not_started"], "未开始")
        self.assertEqual(data["queued"], "排队中")
        self.assertEqual(data["analyzing"], "分析中")
        self.assertEqual(data["awaiting_confirmation"], "待确认")
        self.assertEqual(data["compiling"], "编译中")
        self.assertEqual(data["succeeded"], "成功")
        self.assertEqual(data["failed"], "失败")


if __name__ == "__main__":
    unittest.main()
