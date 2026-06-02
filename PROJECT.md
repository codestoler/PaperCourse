下面是修改后的版本，重点已改为：**架构目标一开始就是 LangGraph-based agent loop，而不是固定 stage-based pipeline**。stage 仍可存在，但只作为 LangGraph node 的实现单元。

---

# AI Course Compiler 项目文档

## 1. 目标

本项目目标是实现一个轻量级 **AI Course Compiler**：将用户提供的学习资料，如 PDF 教材、Markdown 笔记、PPT 转写文本、手动解析后的文档等，自动整理为适合手机端学习的课程结构，包括学习路径、知识点 checklist、公众号式微章节、来源引用、阅读进度和可重编译版本。

本项目不是传统 LMS，也不是普通 RAG 问答系统。核心目标是：使用 **LangGraph 驱动的 agent loop**，把凝练、跳步、逻辑混乱的资料，重建为有先修关系、有桥接说明、有来源约束的个人学习课程。LangGraph 官方文档强调其适合构建具备 persistence、human-in-the-loop、状态记忆和可恢复执行的 agent，这正好适合本项目的“编译—验证—修复—重编译”循环。([LangChain 文档][1])

Codex 在做技术决策和修改方案时，应使用以下原则进行 validation：

1. **Agent loop 优先**：项目主架构应基于 LangGraph 的状态图，而不是简单线性 pipeline。解析、抽取、重排、验证、修复、重编译都应是 graph node。
2. **轻量优先**：优先使用本地文件、Markdown、JSON、SQLite、单体后端，不引入完整 LMS、LRS、多服务微服务或过早云端架构。
3. **材料优先**：所有课程内容必须基于用户资料。允许生成 bridge，但必须明确标记为桥接说明，不得伪装成原文内容。
4. **元 prompt 稳定**：不要让 agent 自由重写 core prompt。用户提问只能转化为 compile patch 或 compile profile 配置。
5. **可中断、可检查、可恢复**：长任务应通过 LangGraph checkpoint 或等价机制保存状态，支持人类审阅后继续执行。
6. **版本化**：重新编译课程时不覆盖旧版本，应生成 `versions/v1/`, `versions/v2/` 等目录。
7. **先局部可用，再扩展格式**：MVP 优先支持 PDF/Markdown，暂不做完整视频、多模态图表和复杂交互题系统。

## 2. 架构

推荐采用 local-first 单体架构，但核心编排层必须是 **LangGraph agent loop**：

```text
AI Course Compiler
├── frontend/          # 手机端友好的 PWA / Web UI
├── backend/           # FastAPI 单体服务
├── agent_graph/       # LangGraph 编排核心
├── course-vault/      # 本地课程资料库
└── scripts/           # 编译、测试、维护脚本
```

`llm-wiki` 的遗留资料可作为重要参考：它已经验证了 `raw/ parsed/ wiki` 三层沉淀、Markdown vault、patch-based 更新、后台任务队列、只读浏览、query promotion 和 revision queue 等机制。新项目应继承这些工程思想，但目标从“持续维护 Wiki”改为“编译可学习课程”。

推荐目录：

```text
course-vault/
├── raw/
├── parsed/
└── courses/
    └── course_id/
        ├── course_meta.json
        ├── compile_profile.json
        ├── feedback_log.json
        ├── compile_patches.json
        ├── units.json
        ├── logic_graph.json
        ├── gap_report.json
        ├── outline.json
        ├── concepts.json
        └── versions/
            ├── v1/lessons/
            └── v2/lessons/
```

LangGraph 状态建议定义为：

```python
class CourseCompileState(TypedDict):
    course_id: str
    source_files: list[str]
    parsed_chunks: list[dict]
    units: list[dict]
    logic_graph: dict
    gap_report: dict
    outline: dict
    concepts: list[dict]
    lessons: list[dict]
    compile_profile: dict
    compile_patches: list[dict]
    validation_report: dict
    next_action: str
    errors: list[dict]
```

核心 graph node：

```text
parse_sources
extract_units
organize_logic
detect_gaps
plan_outline
generate_concepts
generate_lessons
check_grounding
repair_course
export_version
answer_question
mine_feedback
apply_compile_patch
recompile
human_review
```

注意：这些 node 可以按条件循环，不应写死为一次性顺序。典型 loop：

```text
extract_units
  → organize_logic
  → detect_gaps
  → generate_lessons
  → check_grounding
  → repair_course
  → check_grounding
  → export_version / human_review
```

用户阅读时提问也进入 graph：

```text
answer_question
  → mine_feedback
  → generate_compile_patch
  → human_review
  → apply_compile_patch
  → recompile
```

## 3. 计划

Codex 应将以下大目标拆分为 `todolist.md`，并逐项勾选。

### 阶段一：LangGraph 最小闭环

* 初始化项目结构。
* 引入 LangGraph。
* 定义 `CourseCompileState`。
* 实现最小 graph：

  * `parse_sources`
  * `extract_units`
  * `organize_logic`
  * `generate_lessons`
  * `check_grounding`
  * `export_version`
* 支持导入 PDF/Markdown。
* 将原始资料写入 `raw/`。
* 将解析结果写入 `parsed/`。
* 生成 `versions/v1/lessons/*.md`。

### 阶段二：逻辑重建

* 生成 `units.json`。
* 生成 `logic_graph.json`。
* 识别先修关系。
* 检测未定义术语、推导跳步、顺序混乱。
* 生成 `gap_report.json`。
* 支持 bridge paragraph / bridge lesson。
* 明确标记内容类型：

  * `source_supported`
  * `inferred_from_source`
  * `bridge`
  * `needs_confirmation`

### 阶段三：验证—修复 loop

* 实现 source grounding checker。
* 实现 output schema checker。
* 实现 lesson length / mobile readability checker。
* 失败时进入 `repair_course` node。
* 多次失败后进入 `human_review`。
* 保存 graph run log。

### 阶段四：阅读反馈与重编译

* 用户可在 lesson 中提问。
* 问题写入 `feedback_log.json`。
* Feedback Miner 生成 `compile_patches.json`。
* 用户批准 patch。
* graph 根据 patch 局部重编译 lesson。
* 支持生成 `versions/v2/`。
* 保存 changelog。

### 阶段五：轻量前端

* 课程列表。
* 章节阅读页。
* 知识点 checklist。
* 阅读进度条。
* 来源片段查看。
* 重新编译按钮。
* 版本切换。

暂不实现：完整 LMS、多用户班级、成绩册、xAPI/LRS、视频自动解析、复杂多 agent 辩论、生产级云服务。

## 4. 资源

当前目录下的 `llm-wiki` 遗留资料可作为架构参考。重点参考：

```text
schema/          # 产品理念、计划、测试资料
demo/            # 已可运行本地原型
demo/agent/      # 旧 DemoAgentGraph，可参考但需升级为真正 LangGraph
demo/core/       # ingest、query、wiki、search、task_queue 等核心模块
raw/ parsed/ wiki/ 思想
```

映射关系：

```text
llm-wiki raw/          → AI Course Compiler raw/
llm-wiki parsed/       → AI Course Compiler parsed/
llm-wiki wiki/         → AI Course Compiler courses/
article plan           → lesson plan
wiki patch             → compile patch
query promotion        → feedback-to-patch
revision queue         → recompile queue
MOC / index            → outline / checklist
```

API key 使用 `.env` 管理，不提交 Git

### GLM.md: 
**注意我使用的是coding plan**
智谱清言平台使用：大模型
使用概述和方法以及常见错误码和额度限制都参见文档

### MinerU.md: 
MinerU平台使用：pdf转markdown

### SiliconFlow.md
Qwen/Qwen3-Embedding-8B

推荐依赖：

```text
LangGraph：agent loop / state graph / checkpoint / human review
FastAPI：本地后端
mineru：PDF 解析
SQLite：轻量状态存储
Markdown + JSON：课程产物
Chroma：可选 parsed 检索
Next.js PWA：手机端阅读 UI
```

最小可用闭环：

```text
一本 PDF / Markdown
  → LangGraph compile loop
  → parsed chunks
  → knowledge units
  → logic graph
  → checklist
  → mobile lessons
  → grounding check
  → course version
  → reading feedback
  → compile patch
  → recompile
```

[1]: https://docs.langchain.com/oss/python/langgraph/overview?utm_source=chatgpt.com "LangGraph overview - Docs by LangChain"
