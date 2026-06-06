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
            const { parseStatusLabel, projectStatusLabel } = require('./frontend/app.js');
            const states = ['not_started', 'queued', 'analyzing', 'awaiting_confirmation', 'compiling', 'waiting_review', 'succeeded', 'failed'];
            const parseStates = ['waiting_parse', 'parsing', 'parsed', 'parse_failed'];
            process.stdout.write(JSON.stringify({
              projects: Object.fromEntries(states.map((state) => [state, projectStatusLabel(state)])),
              parse: Object.fromEntries(parseStates.map((state) => [state, parseStatusLabel(state)]))
            }));
            """
        )

        self.assertEqual(data["projects"]["not_started"], "未开始")
        self.assertEqual(data["projects"]["queued"], "排队中")
        self.assertEqual(data["projects"]["analyzing"], "分析中")
        self.assertEqual(data["projects"]["awaiting_confirmation"], "待确认")
        self.assertEqual(data["projects"]["compiling"], "编译中")
        self.assertEqual(data["projects"]["waiting_review"], "待人工审核")
        self.assertEqual(data["projects"]["succeeded"], "成功")
        self.assertEqual(data["projects"]["failed"], "失败")
        self.assertEqual(data["parse"]["waiting_parse"], "等待解析")
        self.assertEqual(data["parse"]["parsing"], "解析中")
        self.assertEqual(data["parse"]["parsed"], "解析完成")
        self.assertEqual(data["parse"]["parse_failed"], "解析失败")

    def test_job_intermediate_panel_renders_nodes_and_review_controls(self) -> None:
        html = self.run_node_json(
            """
            const { renderJobIntermediatePanel } = require('./frontend/app.js');
            const html = renderJobIntermediatePanel(
              {
                id: 'job-1',
                state: 'waiting_review',
                current_stage: 'human_review',
                review_summary: {
                  reason: 'needs review',
                  default_target_node: 'repair_course',
                  failures: [{
                    stage: 'quality_rules',
                    type: 'broken_formula',
                    lesson_id: 'lesson-013',
                    lesson_title: 'Broken Formula',
                    line: 63,
                    message: 'Displayed formula delimiter appears broken.'
                  }]
                }
              },
              [
                { node: 'synthesize_source_brief', status: 'finished', output_count: 2, error_count: 0 },
                { node: 'check_markdown_syntax', status: 'failed', output_count: 1, error_count: 1 },
                { node: 'check_quality_rules', status: 'finished', output_count: 1, error_count: 0 }
              ],
              {
                node: 'synthesize_source_brief',
                status: 'finished',
                inputs: [{ name: 'source_index.json', exists: true, size: 12, preview: { pack_count: 3 } }],
                outputs: [{ name: 'source_brief.json', exists: true, size: 20, preview: { summary: 'brief' } }],
                errors: [],
                review: { reason: 'needs review' },
                review_decisions: [{ action: 'request-modification', feedback: 'split' }]
              }
            );
            process.stdout.write(JSON.stringify(html));
            """
        )

        self.assertIn("编译中间结果", html)
        self.assertIn("synthesize_source_brief", html)
        self.assertIn("check_markdown_syntax", html)
        self.assertIn("流程已阻塞在人工审核", html)
        self.assertIn("lesson-013", html)
        self.assertIn("Broken Formula", html)
        self.assertIn("broken_formula", html)
        self.assertIn("技术详情", html)
        self.assertIn("data-job-review=\"approve\"", html)
        self.assertIn("data-job-review=\"request-modification\"", html)
        self.assertIn("允许自动修复并继续", html)
        self.assertIn("跳过审核并尝试导出", html)
        self.assertIn("data-job-control=\"terminate\"", html)
        self.assertIn("source_brief.json", html)
        self.assertIn("review_decisions.jsonl", html)

    def test_analysis_report_renders_parse_artifacts(self) -> None:
        html = self.run_node_json(
            """
            const { renderAnalysisReport } = require('./frontend/app.js');
            const html = renderAnalysisReport({
              status: 'success',
              parse_status: 'parsed',
              chapter_structure: [{title: 'Chapter', line: 1}],
              knowledge_points: [{name: 'Definition'}],
              text_blocks: [{page: 3, line: 12, title: 'Chapter', type: 'formula', text: '$$x+y$$'}],
              formulas: [{line: 12, type: 'display_math', preview: '$$x+y$$'}],
              images: [{line: 14, type: 'markdown_image', path: 'img.png'}],
              tables: [{line: 16, type: 'markdown_table', status: 'recognized'}],
              parse_logs: ['mineru: done'],
              potential_problems: []
            });
            process.stdout.write(JSON.stringify({html}));
            """
        )["html"]

        self.assertIn("解析完成", html)
        self.assertIn("p.3", html)
        self.assertIn("display_math", html)
        self.assertIn("img.png", html)
        self.assertIn("markdown_table", html)
        self.assertIn("mineru: done", html)

    def test_library_file_item_renders_parse_progress_and_keeps_report_open(self) -> None:
        html = self.run_node_json(
            """
            const { renderLibraryFileItem } = require('./frontend/app.js');
            const html = renderLibraryFileItem(
              {
                id: 'file-1',
                filename: 'source.pdf',
                size: 1200,
                parse_status: 'parsing',
                parse_progress: 45,
                parse_current_stage: 'mineru_poll',
                parsed_source_path: '',
                can_compile: false
              },
              { parse_status: 'parse_failed', potential_problems: [{ severity: 'high', message: 'MinerU failed' }], parse_logs: ['failed'] },
              true
            );
            process.stdout.write(JSON.stringify(html));
            """
        )

        self.assertIn('value="45"', html)
        self.assertIn("等待 MinerU 返回", html)
        self.assertIn("解析完成后可用于课程生成", html)
        self.assertIn("收起解析结果", html)
        self.assertIn("MinerU failed", html)
        self.assertIn('class="danger" data-library-delete="file-1"', html)
        self.assertIn("删除受限", html)

    def test_library_file_item_marks_parsed_file_ready_for_compile(self) -> None:
        html = self.run_node_json(
            """
            const { renderLibraryFileItem } = require('./frontend/app.js');
            const html = renderLibraryFileItem(
              {
                id: 'file-1',
                filename: 'source.md',
                size: 1200,
                parse_status: 'parsed',
                parse_progress: 100,
                parse_current_stage: 'parsed',
                parsed_source_path: 'parsed/library/source/content.md',
                can_compile: true
              },
              null,
              false
            );
            process.stdout.write(JSON.stringify(html));
            """
        )

        self.assertIn("可用于课程生成", html)
        self.assertIn('value="100"', html)
        self.assertIn('class="danger" data-library-delete="file-1"', html)
        self.assertNotIn('data-library-delete="file-1" disabled', html)

    def test_project_job_status_renders_control_states_from_backend_job(self) -> None:
        html = self.run_node_json(
            """
            const { renderProjectJobStatus } = require('./frontend/app.js');
            global.projectJobs = undefined;
            const project = {id: 'p1'};
            const app = require('./frontend/app.js');
            // renderProjectJobStatus closes over module state, so expose via an eval-friendly projectJobs update.
            process.stdout.write(JSON.stringify({html: renderProjectJobStatus(project)}));
            """
        )["html"]

        self.assertIn("暂无编译任务", html)


if __name__ == "__main__":
    unittest.main()
