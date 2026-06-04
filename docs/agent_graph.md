# Agent Graph State Diagram

Generated from `agent_graph.compiler` metadata. Regenerate this file after changing the agent graph:

```bash
.venv/bin/python scripts/render_agent_graph.py --render-svg
```

```mermaid
flowchart TD
  START(["START"]):::entry
  END(["END"]):::terminal
  ingest_pipeline["Ingest Pipeline<br/>LLM: optional_subpipeline<br/>Tools: filesystem, markdown_parser, mineru_metadata_reader, source_indexer, vision_subpipeline_optional, formula_vision_optional<br/>Decision: deterministic_pipeline"]:::logic
  course_planning_loop["Course Planning Loop<br/>LLM: optional<br/>Tools: llm_optional, local_fallback, source_index, source_brief, lesson_notes<br/>Decision: llm_when_enabled_max_iterations"]:::llm
  compile_plan_gate["Compile Plan Gate<br/>LLM: conditional<br/>Tools: filesystem, json_writer, markdown_writer, local_validator, llm_optional, local_repair<br/>Decision: review_gate_max_revisions"]:::decision
  lesson_body_pipeline["Lesson Body Pipeline<br/>LLM: optional<br/>Tools: filesystem, llm_optional, local_cache<br/>Decision: per_lesson_pipeline"]:::llm
  validation_repair_loop["Validation &amp; Repair Loop<br/>LLM: conditional<br/>Tools: markdown_validator, local_validator, remark_lint_optional, llm_optional, local_repair<br/>Decision: rules_first_max_iterations"]:::decision
  export_pipeline["Export Pipeline<br/>LLM: no<br/>Tools: filesystem, markdown_writer, json_writer<br/>Decision: deterministic_pipeline"]:::logic
  human_review["Human Review<br/>LLM: no<br/>Tools: manual_review_queue<br/>Decision: human_gate"]:::human
  START --> ingest_pipeline
  ingest_pipeline -->|"ingest artifacts ready"| course_planning_loop
  export_pipeline -->|"done"| END
  human_review -->|"blocked for manual review"| END
  course_planning_loop -.->|"planning ready; next_action != &quot;human_review&quot;"| compile_plan_gate
  course_planning_loop -.->|"planning failed; next_action == &quot;human_review&quot;"| human_review
  compile_plan_gate -.->|"review passed; compile plan review passed"| lesson_body_pipeline
  compile_plan_gate -.->|"needs replanning; compile plan revision needs lesson replanning"| course_planning_loop
  compile_plan_gate -.->|"gate exhausted; next_action == &quot;human_review&quot;"| human_review
  lesson_body_pipeline -.->|"lesson bodies ready; lesson body generation completed"| validation_repair_loop
  lesson_body_pipeline -.->|"needs finer split; lesson_body_revision_request.needs_finer_split"| compile_plan_gate
  lesson_body_pipeline -.->|"body generation blocked; next_action == &quot;human_review&quot;"| human_review
  validation_repair_loop -.->|"validated; validation_report.ok == true"| export_pipeline
  validation_repair_loop -.->|"repair exhausted; next_action == &quot;human_review&quot;"| human_review
  classDef entry fill:#eef2ff,stroke:#4f46e5,color:#111827
  classDef terminal fill:#f3f4f6,stroke:#374151,color:#111827
  classDef logic fill:#ecfdf5,stroke:#047857,color:#064e3b
  classDef llm fill:#fef3c7,stroke:#b45309,color:#78350f
  classDef tool fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e
  classDef decision fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
  classDef human fill:#f5f3ff,stroke:#7c3aed,color:#4c1d95
```

## Node Semantics

| Node | Type | LLM | Tools | Decision | State Outputs |
| --- | --- | --- | --- | --- | --- |
| `ingest_pipeline` | logic_pipeline | optional_subpipeline | filesystem, markdown_parser, mineru_metadata_reader, source_indexer, vision_subpipeline_optional, formula_vision_optional | deterministic_pipeline | `parsed_chunks`, `image_understanding`, `source_index` |
| `course_planning_loop` | bounded_agent_loop | optional | llm_optional, local_fallback, source_index, source_brief, lesson_notes | llm_when_enabled_max_iterations | `source_brief`, `course_plan`, `lesson_notes`, `units`, `logic_graph`, `gap_report`, `outline`, `lessons` |
| `compile_plan_gate` | gate_loop | conditional | filesystem, json_writer, markdown_writer, local_validator, llm_optional, local_repair | review_gate_max_revisions | `compile_plan`, `compile_plan_review`, `compile_plan_revisions`, `next_action` |
| `lesson_body_pipeline` | llm_pipeline | optional | filesystem, llm_optional, local_cache | per_lesson_pipeline | `lesson_bodies`, `lesson_body_inputs`, `lesson_body_revision_request`, `lessons`, `next_action` |
| `validation_repair_loop` | gate_loop | conditional | markdown_validator, local_validator, remark_lint_optional, llm_optional, local_repair | rules_first_max_iterations | `markdown_syntax_report`, `markdown_repair_audit`, `validation_report`, `compile_patches`, `lessons`, `next_action` |
| `export_pipeline` | logic_pipeline | no | filesystem, markdown_writer, json_writer | deterministic_pipeline | `course_meta`, `version_record`, `lessons` |
| `human_review` | human_gate | no | manual_review_queue | human_gate | `next_action` |

## Transitions

| From | To | Type | Decided By | Condition / Label |
| --- | --- | --- | --- | --- |
| `ingest_pipeline` | `course_planning_loop` | logic | logic_pipeline | `ingest artifacts ready` |
| `export_pipeline` | END | logic | logic_pipeline | `done` |
| `human_review` | END | logic | logic_pipeline | `blocked for manual review` |
| `course_planning_loop` | `compile_plan_gate` | conditional | bounded_planning_loop | `planning ready; next_action != "human_review"` |
| `course_planning_loop` | `human_review` | conditional | bounded_planning_loop | `planning failed; next_action == "human_review"` |
| `compile_plan_gate` | `lesson_body_pipeline` | conditional | compile_plan_gate | `review passed; compile plan review passed` |
| `compile_plan_gate` | `course_planning_loop` | conditional | compile_plan_gate | `needs replanning; compile plan revision needs lesson replanning` |
| `compile_plan_gate` | `human_review` | conditional | compile_plan_gate | `gate exhausted; next_action == "human_review"` |
| `lesson_body_pipeline` | `validation_repair_loop` | conditional | lesson_body_pipeline | `lesson bodies ready; lesson body generation completed` |
| `lesson_body_pipeline` | `compile_plan_gate` | conditional | lesson_body_pipeline | `needs finer split; lesson_body_revision_request.needs_finer_split` |
| `lesson_body_pipeline` | `human_review` | conditional | lesson_body_pipeline | `body generation blocked; next_action == "human_review"` |
| `validation_repair_loop` | `export_pipeline` | conditional | validation_repair_loop | `validated; validation_report.ok == true` |
| `validation_repair_loop` | `human_review` | conditional | validation_repair_loop | `repair exhausted; next_action == "human_review"` |
