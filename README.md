# AI Course Compiler

AI Course Compiler is a local-first toolchain for turning user-provided learning materials into structured, mobile-readable course versions. It ingests raw files, preserves parser-native outputs, compiles source-grounded lessons, validates the result, and serves the compiled course through a lightweight browser UI.

## Architecture

The compiler follows a LangGraph-style agent loop. Source parsing, source indexing, course planning, lesson generation, validation, repair, export, feedback mining, and recompilation are treated as node-level responsibilities. The intended data flow is:

```text
course-vault/raw/       # original files only
course-vault/parsed/    # MinerU/LVM/parser-native output with IDs, assets, positions
course-vault/courses/   # compiled course artifacts and versioned lessons
```

Main code directories:

```text
agent_graph/     # compiler state, nodes, LLM adapters, orchestration
backend/         # local HTTP API and static server
frontend/        # course browser UI
scripts/         # ingestion, compile, migration, validation helpers
tests/           # unittest suite
resources/       # copied provider references
```

## Environment

Create the local Python environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

LLM and provider configuration belongs in `.env`. Common keys:

```bash
GLM_ANTHROPIC_URL=https://open.bigmodel.cn/api/anthropic
GLM_API_KEY=<your-key>
GLM_MODEL=GLM-4.7
LLM_ALLOW_SILICONFLOW_FALLBACK=0
```

`compile_lvm.py` maps `GLM_API_KEY` to the Z.AI vision MCP server as `Z_AI_API_KEY`. Do not commit `.env` or provider credentials.

## Core Commands

Run the test suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Validate a compiled course:

```bash
.venv/bin/python scripts/validate_course.py --course-id <course_id> --version <version> --min-lessons <n>
```

Start the local browser UI:

```bash
.venv/bin/python backend/server.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`.

No separate lint command is currently configured. Use tests plus focused validation as the required quality gate.

## Agent Graph Diagram

Render the current compiler graph whenever `agent_graph/` topology, routing, or node responsibility changes:

```bash
.venv/bin/python scripts/render_agent_graph.py --render-svg
```

The tool reads graph metadata from `agent_graph.compiler` and writes:

```text
docs/agent_graph.md    # readable diagram and node/edge tables
docs/agent_graph.mmd   # Mermaid source
docs/agent_graph.dot   # Graphviz DOT source
docs/agent_graph.svg   # optional, only when Graphviz dot is installed
```

Node colors identify deterministic logic, optional LLM work, external tools, conditional routing, and human review gates. Edge labels show transition conditions such as `next_action == "export_version"`.

## Parsing Sources

Convert raw PDFs with MinerU and keep the complete parser output under `course-vault/parsed/`:

```bash
.venv/bin/python scripts/mineru_pdf_to_md.py --input-dir course-vault/raw/numerical_analysis --output-dir course-vault/parsed/numerical_analysis
```

For long PDFs, split before MinerU:

```bash
.venv/bin/python scripts/split_pdf_for_mineru.py course-vault/raw/FLASH/flash4_ug_4p8.pdf --output-dir course-vault/parsed/FLASH/_split_input --max-pages 180
.venv/bin/python scripts/mineru_pdf_to_md.py --input-dir course-vault/parsed/FLASH/_split_input --output-dir course-vault/parsed/FLASH/mineru_parts --model-version vlm
```

Use `scripts/migrate_mineru_parsed.py` when older parsed outputs need to be moved into the current `course-vault/parsed/` layout.

## Compiling Courses

Compile Markdown or parsed sources:

```bash
.venv/bin/python scripts/compile_course.py course-vault/parsed/<source> --course-id <course_id> --version v1 --use-source-index --use-source-brief --detailed-lessons
```

Use `--use-llm` for the LLM-first compiler path. It enables LLM planning and LLM-first structure nodes for unit extraction, logic organization, gap detection, and lesson drafting. Use `--use-llm-structure` when you only want the structure nodes to call the LLM.

Compile hybrid MinerU/LVM sources:

```bash
.venv/bin/python scripts/compile_hybrid_course.py course-vault/parsed/<mineru_source> course-vault/parsed/lvm/<lvm_source> --course-id <course_id> --version v1 --use-llm --use-llm-brief --use-llm-lesson-bodies
```

For long manuals, prefer local source-index planning to keep context bounded:

```bash
.venv/bin/python scripts/compile_hybrid_course.py <parsed_parts...> --course-id flash-user-guide --version v1-learn-by-doing --course-style learn-by-doing --use-llm --use-llm-brief --use-source-index-plan --use-llm-lesson-bodies --source-index-batch-chunks 24 --target-lesson-count 18 --lesson-body-target-chars 2000 --lesson-body-max-chars 4200 --lesson-body-chunk-chars 400
```

Use `--lesson-body-enrichment constrained` when lesson bodies should add bounded local bridge work for skipped example steps, proof hints, in-class questions, and confusing points. Do not use enrichment to invent missing OCR facts or expand unrelated topics.

After lesson drafts are generated, the compiler writes a synthesis plan and reviews it before detailed body generation. `compile_plan.json` is the execution artifact; `compile_plan.md` is the review surface. If the review fails, `revise_compile_plan` writes `compile_plan_revision_log.json` and loops back to review. Body generation is blocked until the plan passes or the revision limit routes the run to `human_review`.

Validation runs after body generation as four gates: `check_grounding_llm`, `check_grounding_rules`, `check_quality_llm`, and `check_quality_rules`. The rules gates always run; `--use-llm` enables LLM validation by default, and `--use-llm-validation` enables it explicitly. Export only happens when all validation layers pass. `validation_report.json` stores per-layer failures with lesson, block, line, page/image, bbox, and reason fields.

Use optional per-image vision refinement only when needed:

```bash
.venv/bin/python scripts/compile_hybrid_course.py <parsed_sources...> --course-id <course_id> --version v1 --use-vision-image-understanding --image-vision-mode uncertain --image-vision-max-images 12
```

The default image agent uses MinerU metadata and neighbor text without extra model calls. `--image-vision-mode uncertain` sends only low-confidence or pending-confirmation images to the vision MCP server; `all` sends more images up to `--image-vision-max-images`. Results are cached under `course-vault/courses/<course_id>/image_vision_cache/`. Use `--refresh-image-vision` to bypass that cache.

Useful refresh flags:

```text
--refresh-source-index
--refresh-source-brief
--refresh-llm-plan
--refresh-lesson-notes
--refresh-lesson-bodies
--refresh-image-vision
```

For expensive LLM runs, narrow the lesson range with `--lesson-body-start` and `--lesson-body-end`.

If an LLM-first structure node cannot call the configured provider or returns unusable JSON, the compiler writes an `*_emergency_fallback.json` artifact and routes to `human_review` instead of silently exporting a local fallback course.

## Output Artifacts

Hybrid compiles write artifacts under `course-vault/courses/<course_id>/`, including:

```text
course_meta.json
compile_profile.json
source_index.json
source_brief.json
image_understanding.json
lesson_notes.json
compile_plan.json
compile_plan.md
compile_plan_review.json
compile_plan_review.md
compile_plan_revision_log.json
lesson_bodies.json
versions/<version>/lessons/*.md
versions/<version>/version_record.json
```

Keep generated courses versioned. Do not overwrite old versions unless intentionally rerunning the same version for local experimentation.

`image_understanding.json` records each extracted image's type, source chunk, page/bbox metadata, associated knowledge points, summary, insertion suggestion, caption need, and confirmation status. Recognized images are exported near their related lesson content; uncertain images are kept at the final lesson's `待确认图片` section.

## Validation Workflow

Use this minimum loop for compiler changes:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_course.py --course-id <course_id> --version <version> --min-lessons <n>
```

For UI changes, start the backend server and verify that the course list, version list, lesson markdown, formulas, and source references render correctly at `http://127.0.0.1:8765`.

## Development Rules

Tests should use neutral fixtures such as `Topic Alpha`, not real course names or examples. The compiler must stay generic: no hardcoded chapter names, domain-specific grouping rules, source-specific prompts, or hidden assumptions tied to one course.

When processing long materials, use indexes, briefs, lesson notes, body caches, and refresh flags. Avoid one-shot prompts containing a full 200-page book or manual.
