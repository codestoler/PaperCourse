# Repository Guidelines

## Project Goal

AI Course Compiler turns user-owned PDFs, Markdown notes, slides, and parsed documents into mobile-readable, versioned courses with outlines, lesson bodies, checklists, source citations, and progress state. It is not a generic LMS or chat-only RAG app: generated material must stay source-grounded, and bridge explanations must be marked as inferred or bridge content.

## Core Architecture

Use a local-first LangGraph-style agent loop. Parsing, indexing, planning, lesson generation, validation, repair, export, feedback mining, and recompilation are graph/node responsibilities, not one-off linear stages. Long sources must use source indexes, briefs, lesson notes, and cached artifacts instead of full-book prompts.

## Key Directories

- `PROJECT.md`: product and architecture source of truth.
- `agent_graph/`: compiler state, nodes, LLM adapters, orchestration.
- `backend/`, `frontend/`: local API/server and static course browser.
- `scripts/`: ingestion, compile, migration, and validation helpers.
- `tests/`: standard-library `unittest` suite.
- `resources/`: copied provider notes; verify live behavior before production use.
- `course-vault/raw/`: untouched original files.
- `course-vault/parsed/`: complete MinerU/LVM/parser outputs with IDs, assets, and positions.
- `course-vault/courses/<course_id>/`: compiled artifacts and `versions/<version>/lessons/`.

## Essential Commands

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate_course.py --course-id <course_id> --version <version> --min-lessons <n>
.venv/bin/python scripts/compile_hybrid_course.py <parsed_sources...> --course-id <course_id> --version <version>
.venv/bin/python scripts/render_agent_graph.py --render-svg
.venv/bin/python backend/server.py --host 127.0.0.1 --port 8765
```

The browser runs at `http://127.0.0.1:8765` and reads `course-vault/courses/`.

## Engineering Rules

- Keep course-specific content out of code, tests, prompts, and generic compiler logic. Use neutral fixtures.
- Do not hardcode chapter names, domains, examples, or source-specific rules.
- Preserve the `raw -> parsed -> courses` boundary. Never overwrite raw sources or collapse parser output into compiled course folders.
- Prefer stable JSON/Markdown artifacts such as `course_meta.json`, `compile_profile.json`, `source_index.json`, `image_understanding.json`, and `lesson_bodies.json`.
- Python uses 4-space indentation, `snake_case`, typed state where practical, and standard-library `unittest`.
- Bound LLM calls with chunking, indexes, briefs, and cached refresh flags.
- After modifying `agent_graph/` topology, node routing, or node responsibility, run `scripts/render_agent_graph.py` and commit the regenerated graph docs.
- Store secrets only in `.env`; never commit API keys or generated credentials.

## Completion Standard

Before finishing code or prompt changes, run relevant unit tests and one focused compile or validation command when course output changes. For frontend changes, start the local server and verify rendering in the browser UI. Record durable research progress, blockers, and repeated external failures as `PROBLEM:` entries in `todolist.md`.
