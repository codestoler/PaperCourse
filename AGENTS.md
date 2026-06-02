# Repository Guidelines

## Project Structure & Module Organization

This repository is currently a planning and reference workspace for the AI Course Compiler. `PROJECT.md` is the primary product and architecture brief; read it before changing direction or adding implementation details. `resources/` stores external service notes and copied documentation, including GLM, SiliconFlow, and MinerU references.

The intended implementation structure described in `PROJECT.md` is:

```text
frontend/        # Mobile-friendly PWA or web UI
backend/         # FastAPI service
agent_graph/     # LangGraph orchestration and state graph
course-vault/    # Local raw, parsed, and compiled course data
scripts/         # Maintenance, compile, and test helpers
```

Keep generated course content under `course-vault/` once that directory exists. Do not mix source references, compiled lessons, and application code in the same folder.

## Build, Test, and Development Commands

For current documentation work, use:

```bash
rg "LangGraph|course-vault|compile" .
```

to inspect project decisions quickly.

The local compiler and course browser can be run with:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/mineru_pdf_to_md.py --input-dir course-vault/raw/numerical_analysis --output-dir course-vault/parsed/numerical_analysis
.venv/bin/python scripts/compile_course.py course-vault/parsed/numerical_analysis/* --course-id numerical-analysis --version v2
.venv/bin/python scripts/compile_course.py course-vault/parsed/numerical_analysis/6函数逼近与插值 --course-id numerical-analysis-ch6-llm --version v7 --use-llm --max-llm-chunks 130
.venv/bin/python scripts/compile_course.py course-vault/parsed/numerical_analysis/6函数逼近与插值 --course-id numerical-analysis-ch6-llm --version v8 --use-llm --max-llm-chunks 130 --refresh-llm-plan
.venv/bin/python scripts/compile_lvm.py course-vault/raw/numerical_analysis/6函数逼近与插值.pdf --course-id numerical-analysis-ch6-lvm --version v3 --dpi 120 --use-llm --max-llm-chunks 80
.venv/bin/python scripts/compile_hybrid_course.py course-vault/parsed/numerical_analysis/6函数逼近与插值 course-vault/parsed/lvm/6函数逼近与插值-187571e5cbd6 --course-id numerical-analysis-ch6-hybrid-llm --version v14 --use-llm --use-llm-brief --use-llm-lesson-bodies --no-source-index --max-brief-chunks 160 --max-llm-chunks 120 --lesson-body-target-chars 3000 --lesson-body-max-chars 9000
.venv/bin/python scripts/compile_hybrid_course.py course-vault/parsed/numerical_analysis/6函数逼近与插值 course-vault/parsed/lvm/6函数逼近与插值-187571e5cbd6 --course-id numerical-analysis-ch6-hybrid-llm --version v15-enriched-smoke --use-llm --use-llm-brief --use-llm-lesson-bodies --no-source-index --max-brief-chunks 160 --max-llm-chunks 120 --lesson-body-target-chars 3200 --lesson-body-max-chars 9500 --lesson-body-chunk-chars 1400 --lesson-body-enrichment constrained --refresh-lesson-bodies --lesson-body-start 6 --lesson-body-end 6
.venv/bin/python scripts/compile_hybrid_course.py course-vault/parsed/numerical_analysis/1数值计算导论 course-vault/parsed/numerical_analysis/2非线性方程 course-vault/parsed/numerical_analysis/3线性方程组的直接解法 course-vault/parsed/numerical_analysis/4线性方程组的迭代解法 course-vault/parsed/numerical_analysis/5矩阵特征值计算 course-vault/parsed/numerical_analysis/6函数逼近与插值 course-vault/parsed/numerical_analysis/7数值积分微分 --course-id numerical-analysis --version v18-full-bodies --use-llm --use-llm-brief --use-llm-lesson-bodies --max-brief-chunks 260 --max-llm-chunks 180 --lesson-note-batch-lessons 2 --lesson-body-target-chars 3000 --lesson-body-max-chars 9000
.venv/bin/python scripts/split_pdf_for_mineru.py course-vault/raw/FLASH/flash4_ug_4p8.pdf --output-dir course-vault/parsed/FLASH/_split_input --max-pages 180
.venv/bin/python scripts/mineru_pdf_to_md.py --input-dir course-vault/parsed/FLASH/_split_input --output-dir course-vault/parsed/FLASH/mineru_parts --model-version vlm
.venv/bin/python scripts/compile_hybrid_course.py course-vault/parsed/FLASH/mineru_parts/flash4_ug_4p8_part001_pages001-180 course-vault/parsed/FLASH/mineru_parts/flash4_ug_4p8_part002_pages181-360 course-vault/parsed/FLASH/mineru_parts/flash4_ug_4p8_part003_pages361-540 course-vault/parsed/FLASH/mineru_parts/flash4_ug_4p8_part004_pages541-661 --course-id flash-user-guide --version v1-learn-by-doing --course-style learn-by-doing --use-llm --use-llm-brief --use-source-index-plan --use-llm-lesson-bodies --source-index-batch-chunks 24 --target-lesson-count 18 --lesson-body-target-chars 2000 --lesson-body-max-chars 4200 --lesson-body-chunk-chars 400
.venv/bin/python scripts/validate_course.py --course-id flash-user-guide --version v1-learn-by-doing --min-lessons 15
.venv/bin/python scripts/validate_course.py --course-id numerical-analysis --version v18-full-bodies --min-lessons 30
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python backend/server.py --host 127.0.0.1 --port 8765
```

The browser UI is served from `frontend/` and reads compiled courses from `course-vault/`. Hybrid compiles write `source_index.*`, `source_brief.*`, `lesson_notes.*`, and `lesson_bodies.*` under `course-vault/courses/<course_id>/`; keep parser-native MinerU/LVM outputs under `course-vault/parsed/`. Use `--use-llm-source-index` only when provider latency is acceptable. For long manuals, prefer local source-index plus `--use-source-index-plan`, with `--course-style learn-by-doing` when reorganizing feature-order software documentation into task-first tutorials. Use `--lesson-body-enrichment constrained` when lesson bodies should add bounded local bridge work for skipped example steps, theorem/proof hints, classroom questions, and confusing concepts; do not use it to invent missing OCR facts or expand adjacent topics.

## Coding Style & Naming Conventions

Use Markdown for project notes and keep headings concise. Prefer English file and directory names for code, JSON keys, and generated artifacts, matching existing names like `course_meta.json`, `compile_profile.json`, and `logic_graph.json`. Python code should use 4-space indentation, `snake_case` functions, and typed data structures for LangGraph state. Keep architecture changes aligned with the agent-loop principles in `PROJECT.md`.

## Testing Guidelines

Tests use the standard library `unittest` framework under `tests/`. Add files named `test_*.py` and run `python3 -m unittest discover -s tests -v`. Focus first on graph node contracts, source-grounding checks, JSON schema validation, and versioned export behavior. Tests should avoid real API calls unless explicitly marked as integration tests.

## Commit & Pull Request Guidelines

This directory has no Git history available, so use a simple conventional style: `feat:`, `fix:`, `docs:`, `test:`, or `chore:` followed by a short imperative summary. Pull requests should include the purpose, files or modules changed, validation performed, and any new configuration or API-key requirements. Include screenshots only for frontend UI changes.

## Security & Configuration Tips

Keep API keys in `.env` and never commit secrets. Prefer the GLM coding-plan Anthropic endpoint with `GLM_ANTHROPIC_URL=https://open.bigmodel.cn/api/anthropic`, `GLM_API_KEY`, and `GLM_MODEL=GLM-4.7` for LLM planning. `compile_lvm.py` maps `GLM_API_KEY` to the Z.AI vision MCP server as `Z_AI_API_KEY` and stores page images plus visual analysis under `course-vault/parsed/lvm/`. The compiler skips SiliconFlow as an implicit fallback; set `LLM_ALLOW_SILICONFLOW_FALLBACK=1` only for an intentional manual fallback. Treat copied provider documentation in `resources/` as reference material; verify live API behavior before relying on limits, endpoints, or model names in production code.
