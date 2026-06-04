# AI Course Compiler Todolist

## 工作原则

- [ ] 每次只推进一个明确任务。
- [ ] 每个任务完成后运行本地验证，验证通过再继续下一个。
- [ ] 优先完成 local-first 最小闭环，暂不依赖真实 GLM、MinerU 或 SiliconFlow API。

## 阶段一：LangGraph 最小闭环

- [x] 初始化项目结构：`agent_graph/`、`backend/`、`course-vault/`、`scripts/`、`tests/`。
- [x] 定义 `CourseCompileState` 和核心数据结构。
- [x] 实现本地 Markdown 解析节点 `parse_sources`，写入 `course-vault/raw/` 与 `course-vault/parsed/`。
- [x] 实现知识单元抽取节点 `extract_units`，生成 `units.json`。
- [x] 实现逻辑组织节点 `organize_logic`，生成 `logic_graph.json`。
- [x] 实现课程章节生成节点 `generate_lessons`，生成移动端友好的 lesson 数据。
- [x] 实现来源约束检查节点 `check_grounding`。
- [x] 实现版本导出节点 `export_version`，生成 `versions/v1/lessons/*.md`。
- [x] 提供命令行脚本，可从一个 Markdown 文件编译课程。
- [x] 为阶段一闭环补充自动化测试并通过。

## 阶段二：逻辑重建

- [x] 生成 `outline.json` 与 `concepts.json`。
- [x] 检测未定义术语、推导跳步和章节顺序问题，生成 `gap_report.json`。
- [x] 支持 `source_supported`、`inferred_from_source`、`bridge`、`needs_confirmation` 内容类型。

## 阶段三：验证与修复 Loop

- [x] 增加 schema checker。
- [x] 增加 lesson length / mobile readability checker。
- [x] 失败时进入 `repair_course`，多次失败后进入 `human_review`。
- [x] 保存 graph run log，支持中断后检查。

## 阶段四：反馈与重编译

- [x] 记录阅读问题到 `feedback_log.json`。
- [x] 将反馈整理为 `compile_patches.json`。
- [x] 应用已批准 patch 并导出 `versions/v2/`。

## 阶段五：轻量前端

- [x] 展示课程列表、章节阅读页和 checklist。
- [x] 支持来源片段查看、阅读进度和版本切换。

## 阶段六：数值分析课件实战编译

- [x] 检查 `raw/numerical_analysis/` PDF 输入、页数和 MinerU 配置。
- [x] 创建 `.venv` 并安装 LangGraph、MinerU API 调用和 PDF 检查依赖。
- [x] 将编译 runner 改为真实 LangGraph 状态图，不再手写顺序框架。
- [x] 实现 MinerU PDF 转 Markdown 脚本，支持任务提交、上传、轮询、下载和断点续跑。
- [x] 调用 MinerU 将数值分析 PDF 转为 Markdown。
- [x] 使用 LangGraph agent loop 将 Markdown 材料编译为 `numerical-analysis` 课程。
- [x] 运行测试和产物验证，持续修复直到通过。

## 阶段七：前端课程渲染修复

- [x] 修复 Markdown 直接写入 `innerHTML` 导致 `<` 等课件内容破坏 DOM 的问题。
- [x] 改为安全的逐行 Markdown 渲染，支持标题、段落、列表、checklist、引用和来源块。
- [x] 默认打开 `numerical-analysis` 和最新版本，减少手动切换。
- [x] 验证本地服务已返回新版前端资源。

## 阶段八：前端公式渲染

- [x] 接入 MathJax 3，支持 `$...$`、`$$...$$`、`\\(...\\)`、`\\[...\\]`。
- [x] 在 lesson 加载后调用 MathJax typeset。
- [x] 对未闭合 `$$` 块做前端容错，避免后续课程内容被吞掉。
- [x] 验证本地服务已返回 MathJax 配置和新版渲染脚本。

## 阶段九：LLM 智能课程 Compile

- [x] 增加 OpenAI-compatible LLM client，读取 `.env` 中的 LLM/GLM 配置。
- [x] 在 LangGraph 中接入 `plan_course` 节点，让模型生成层级 course plan。
- [x] CLI 增加 `--use-llm` 和 `--max-llm-chunks`。
- [x] 根据 LLM plan 生成 section/lesson 层级，并保留 deterministic fallback。
- [x] 将“比较/小结/结论”等上下文依附型标题并入相邻大主题。
- [x] 使用 `course-vault/parsed/numerical_analysis/6函数逼近与插值` 反复迭代，生成 `numerical-analysis-ch6-llm` v7。
- [x] 验证 v7 不再出现 `Numerical Analysis`、`方法比较`、`小结` 等独立坏标题。
- [x] 增加 fake LLM 单元测试覆盖智能编译路径。

## 阶段十：Vault 目录边界整理

- [x] 固定 `course-vault/raw/` 只存放用户原始输入文件。
- [x] 固定 `course-vault/parsed/` 存放 MinerU 完整解析结果目录，包括 `full.md`、`layout.json`、`*_model.json`、`*_content_list*.json`、图片和源 PDF。
- [x] 固定课程编译产物写入 `course-vault/courses/<course_id>/`，包括 `parsed_chunks/`、`course_plan.json`、`outline.json`、`lessons.json` 和版本目录。
- [x] 清理旧版 `course-vault/parsed/*.json` 和 `course-vault/parsed/numerical_analysis/*.md` 残留产物。
- [x] 修改 MinerU 脚本，只将 `parsed/<course>/<source>/full.md` 作为有效解析入口。
- [x] 运行单元测试和课程验证，确认目录整理后编译链路仍通过。

## 阶段十一：GLM Coding Plan LLM 优化

- [x] 审计当前 LLM client，确认旧实现优先使用 `LLM_BASE_URL` 的 OpenAI-compatible 调用。
- [x] 将 provider 选择改为优先 `GLM_ANTHROPIC_URL` / `ANTHROPIC_BASE_URL`，其次 `GLM_BASE_URL`。
- [x] 禁止 SiliconFlow 作为隐式 fallback，仅允许显式 `LLM_ALLOW_SILICONFLOW_FALLBACK=1` 时使用。
- [x] 为 Anthropic messages 请求加入 `cache_control`，让稳定 system prompt 和 source chunks 可走官方 prompt cache。
- [x] 为 `plan_course` 加入本地 course plan cache，重复编译相同材料时复用已有 LLM 结果。
- [x] CLI 增加 `--refresh-llm-plan`，需要强制重新调用模型时可绕过本地缓存。
- [x] 运行测试和一次真实/缓存课程编译验证。
- [x] 使用同一小规模编译重复请求验证 GLM 官方 prompt cache，第二次强制刷新返回 `cache_read_input_tokens=704`。

## 阶段十二：compile-LVM 视觉课程编译

- [x] 阅读 BigModel vision MCP server 文档并验证 `@z_ai/mcp-server` 可通过 MCP stdio 列出视觉工具。
- [x] 增加 `scripts/compile_lvm.py`，把 PDF 每页渲染为 PNG，再调用 vision MCP 的 `analyze_image` 生成页级 Markdown。
- [x] 将 LVM 页图、`page-*.json`、`page_analysis.json`、`full.md` 和 `lvm_manifest.json` 存入 `course-vault/parsed/lvm/<source-hash>/`。
- [x] 使用源文件内容 hash 做视觉缓存，重复编译同一 PDF 不重复调用视觉 MCP。
- [x] 将 LVM 版 `full.md` 接入现有 LangGraph compiler，并支持后续 GLM LLM course plan。
- [x] 改进 LVM 页 heading 与 compiler 标题清洗，避免 `Page 18`、编号标题和视觉排版说明成为独立 lesson 标题。
- [x] 完整处理 `6函数逼近与插值.pdf` 59 页，导出 `numerical-analysis-ch6-lvm` v3。
- [x] 验证 `numerical-analysis-ch6-lvm` v3：18 lessons，`gap_high=0`，无页码/视觉说明类坏标题。

## 阶段十三：MinerU + LVM Hybrid Course Brief

- [x] 新增 LangGraph 节点 `synthesize_source_brief`，在 `parse_sources` 后融合 MinerU 文本与 LVM 视觉理解。
- [x] 让 source brief 采用“总结关键概念、方法和例题，形成大纲和讲义”的学习型 JSON/Markdown 结构。
- [x] `plan_course` 引入 source brief 作为高层学习地图，同时仍要求 lesson 引用原始 chunk ids。
- [x] lesson body 使用 brief 中的 `learning_goal`、`explanation`、`example` 和 `bridge`，让讲义比原始课件更易读。
- [x] 未规划的碎片 chunk 按原始顺序附着到最近的已规划 lesson，不再生成散乱小节。
- [x] Source refs 优先保留不同来源证据，让 MinerU 与 LVM 证据同时出现在 lesson 来源中。
- [x] 新增 `scripts/compile_hybrid_course.py`，可直接用 MinerU parsed 目录和 LVM parsed 目录编译 hybrid 课程。
- [x] 修复版本导出时旧 lesson 文件残留导致验证误报的问题。
- [x] 使用第 6 讲缓存产物和 GLM 生成 `numerical-analysis-ch6-hybrid-llm` v3：11 lessons，`gap_high=0`，无页码/封面/视觉说明类坏标题。

## 阶段十四：逐课讲义 Note 优化

- [x] 新增 LangGraph 节点 `synthesize_lesson_notes`，在 `plan_course` 后按 planned lesson 生成逐课学习目标、解释、例子和承接。
- [x] 逐课 note 只允许引用 planned lesson 中已有的 source chunk ids，并在缺失或 LLM 失败时回退到本地 source-brief/chunk 摘要。
- [x] `extract_units` 优先使用 `lesson_notes`，再回退到 `source_brief`，避免多个 lesson 复用同一条宽泛讲义。
- [x] `scripts/compile_hybrid_course.py` 增加 `--use-llm-lesson-notes` 和 `--refresh-lesson-notes`。
- [x] 增加 fake LLM 单元测试，验证 planned lessons 能获得不同的逐课 explanation/example。
- [x] 使用第 6 讲缓存产物本地生成 `numerical-analysis-ch6-hybrid-llm` v4：11 lessons，11 lesson notes，验证通过。
- [x] 使用 GLM 生成 `numerical-analysis-ch6-hybrid-llm` v5：11 lessons，11 lesson notes，`validate_course.py` 报 `status=ok`、`gap_high=0`。

## 阶段十五：详细课程与上下文管理

- [x] 新增 `build_source_index` LangGraph 节点，将长材料分批整理为 source context packs，后续 brief/plan 可从 compact index 工作，避免一次性塞入整本资料。
- [x] `synthesize_lesson_notes` 改为按 lesson batch 调用 LLM，每批只给 planned lessons 的局部 chunks。
- [x] Anthropic/GLM prompt cache 识别 `Source index context packs` 和 `Lesson batch`，减少重复上下文成本。
- [x] `detailed_lessons` 模式放开旧的 1200 字 lesson 上限，导出学习目标、核心讲解、课件要点、例题直觉、前后衔接和补充摘录。
- [x] 过滤 LVM 的视觉排版元数据、页图 Markdown、Page 标题等非学习内容，保留公式、图表关系和讲解信息。
- [x] `scripts/compile_course.py` 和 `scripts/compile_hybrid_course.py` 增加 source-index、lesson-note batch、detailed lesson 相关开关。
- [x] 增加单元测试覆盖 source-index 分批、lesson-note 分批、LLM lesson body 局部上下文、LaTeX JSON 容错、lesson-body 缓存兜底和后端版本自然排序，全部 23 个测试通过。
- [x] 新增 `synthesize_lesson_bodies` 节点，用每节对应的局部 source chunks 生成更详细的碎片化学习正文，避免把整章/整本材料一次性塞进上下文。
- [x] 修复 LLM JSON 返回中裸 LaTeX 反斜杠导致的解析失败，同时保留正常 Markdown 换行，避免公式正文被破坏。
- [x] 导出 `numerical-analysis-ch6-hybrid-llm` v14：9 lessons，总计约 52.4k 字符，`validate_course.py` 报 `status=ok`、`gap_high=0`，9 节均有 LLM lesson body 缓存。
- [x] 修复长 source-index prompt 的尾部截断问题，用紧凑候选标题保留所有 packs 和 pack 内后排主题，避免 Lagrange/Newton/样条等被计划阶段漏掉。
- [x] 增加 plan repair 和 lesson-body 按 `lesson_id` 的本地缓存兜底，防止 LLM 计划过粗或 prompt 小改动导致长正文缓存失效。
- [x] 导出全课程 `numerical-analysis` v18-full-bodies：36 lessons，总计约 188k 字符，36 节均有 LLM lesson body，`validate_course.py` 报 `status=ok`、`gap_high=0`。
- [x] 修复后端版本排序，前端 API 现在默认把 `v18-full-bodies` 作为最新版本；本地服务抽查可读取第 28 节正文、公式、学习目标和 Sources。
- [ ] PROBLEM: 当前真实 GLM 调用在 `use_llm_source_index` 的部分 batch 上响应不稳定，长时间无返回；已保留本地 source-index fallback 和 `--no-source-index` 逃生阀，但还需要异步进度、超时重试和更稳定的 map prompt 才能把 200 页书籍可靠跑完。

## 阶段十六：FLASH Learn-by-Doing 软件手册编译

- [x] 将 `course-vault/raw/FLASH/flash4_ug_4p8.pdf` 作为新的真实课程编译测试源，保持原始 PDF 只存放在 `raw/FLASH/`。
- [x] 新增 `scripts/split_pdf_for_mineru.py`，把 661 页 FLASH 手册拆成 4 个小于 MinerU 200 页限制的 PDF 分片，并写入 `course-vault/parsed/FLASH/_split_input/split_manifest.json`。
- [x] 调用 MinerU 解析 4 个 FLASH 分片，完整结果保存在 `course-vault/parsed/FLASH/mineru_parts/`，包含 `full.md`、`layout.json`、`*_model.json`、`*_content_list*.json`、源 PDF 和 `_zip`。
- [x] 新增通用 `course_style=learn-by-doing` profile，不硬编码 FLASH 细节；source index、source brief、plan、lesson notes 和 lesson bodies prompt 都改为抽取任务、示例、工作流、功能解释与常见错误。
- [x] 新增 `--use-source-index-plan`，当 LLM 课程计划返回坏 JSON 或长文档 plan 不稳定时，可直接从 LLM source brief + source index 生成中等粒度任务式大纲。
- [x] 编译 `flash-user-guide` `v1-learn-by-doing`：19 lessons，类型覆盖 `task`、`example`、`troubleshooting`，19 节均有 LLM lesson body，`validate_course.py` 报 `status=ok`、`gap_high=0`。
- [x] 本地 API 抽查通过：`/api/courses` 能发现 `flash-user-guide`，`/versions` 返回 `v1-learn-by-doing`，URL 编码中文 lesson 文件名后可读取 Markdown，正文包含“操作步骤”和 Sources。
- [x] 单元测试增加 learn-by-doing prompt 与 source-index-plan 路径覆盖，`python -m unittest discover -s tests -v` 共 25 个测试通过。
- [ ] PROBLEM: MinerU 单文件页数上限为 200 页，661 页 FLASH 手册必须先分片再解析；当前已用 `split_pdf_for_mineru.py --max-pages 180` 解决，但后续应把分片/合并 manifest 集成进更自动化的 ingest 流程。
- [ ] PROBLEM: 真实 GLM 在全量 `--use-llm-source-index` 和直接 LLM course plan 上仍可能出现长等待或坏 JSON；当前稳定路径是 `--use-llm-brief --use-source-index-plan`，后续需要更强的 JSON repair、请求超时重试和分阶段进度输出。

## 阶段十七：局部补全与易错点辨析

- [x] 为 `synthesize_lesson_bodies` 增加通用 `lesson_body_enrichment=constrained` profile，不硬编码数值分析或 FLASH 细节。
- [x] 在 lesson-body prompt 中加入受控补全规则：只补当前 lesson chunks 内的例题跳步、证明桥接、思考题/随堂问题和易混点。
- [x] 保留 OCR/源材料缺失保护：不得反推或编造缺失公式、数字、常数、例题和源事实。
- [x] 将 LLM 返回的 `local_enrichments` 写入 `lesson_bodies.json`，并过滤无效 chunk id，最多保留 3 条。
- [x] 增加 `--lesson-body-enrichment constrained` CLI 开关，`compile_course.py` 和 `compile_hybrid_course.py` 均可使用。
- [x] 增加 fake LLM 单元测试，验证 prompt 包含“局部补全/易混辨析/最多 3 条/不得发明事实”等约束，且 enrichment 元数据会落盘。
- [x] 清理测试样例中的硬编码课程具体内容，将 Newton/差商/牛顿插值/数值分析等真实课程词替换为中性 `Topic Alpha` / `Source Title` fixture。
- [x] 使用数值分析 ch6 smoke 验证：`numerical-analysis-ch6-hybrid-llm` `v15-enriched-smoke`，只刷新第 6 节 lesson body，28 lessons，`validate_course.py` 报 `status=ok`、`gap_high=0`。
- [x] 抽查第 6 节“法方程的推导与几何意义”：生成偏导展开补全、充分性证明桥接和 Gram 矩阵下标易混辨析，均绑定到本节 source chunks。
- [ ] PROBLEM: 真实 LLM 编译不能在 network-restricted sandbox 中运行；首次全量刷新因网络限制长时间无输出，已终止并改为外部执行的单节 smoke。后续应在 CLI 加入更明确的超时、进度输出和单节重试策略。

## 阶段十八：MinerU 图片理解与课程落位

- [x] 在 LangGraph compile loop 中新增 `understand_images` 节点，位于 `parse_sources` 后、source-index/brief/plan 前。
- [x] 从 MinerU `full.md` 图片链接和 `*_content_list*.json` 中提取图片路径、bbox、page、type/sub_type、OCR/表格内容、caption/footnote。
- [x] 生成结构化 `image_understanding.json/.md`，字段覆盖图片类型、关联知识点、摘要、建议插入 lesson/anchor、图注需求、置信度和 `needs_confirmation`。
- [x] 采用轻量默认策略：不默认调用外部视觉模型，优先复用 MinerU 切图、位置和邻近文本；LVM 页级截图只作为视觉分析来源，不再作为待插入图片污染待确认区。
- [x] 编译导出时将可识别图片写入对应 lesson 的 `## Figures`，无法可靠识别的图片只放到最后一节的 `## 待确认图片`。
- [x] 前端 Markdown 渲染支持 `![alt](url)`、图注和图片样式；后端增加 `/api/assets/...` 安全读取 `course-vault` 图片资产。
- [x] 版本记录新增 `versions/<version>/version_record.json`，保存图片理解统计和 artifact 位置。
- [x] 新增中性 fixture 单元测试，覆盖可识别图片落位、未知图片待确认、asset URL 和 version record。
- [x] 真实 ch6 smoke：`numerical-analysis-ch6-image-smoke` `v3-image-smoke`，37 lessons，图片统计 total=37、recognized=36、needs_confirmation=1，`validate_course.py` 报 `status=ok`、`gap_high=0`。
- [x] 本地服务验证通过：课程 API 可读取 `v3-image-smoke`，lesson Markdown 包含图片，图片 asset 返回 `200 image/jpeg`。
- [x] 新增可选真实视觉模型补强：`--use-vision-image-understanding`、`--image-vision-mode uncertain|all`、`--image-vision-max-images`、`--refresh-image-vision`。
- [x] 将 per-image vision 结果缓存到 `course-vault/courses/<course_id>/image_vision_cache/`，同一图片重复编译可命中缓存，避免重复消耗 token。
- [x] 使用真实 Z.AI MCP vision 路径验证 1 张待确认切图：`v5-image-vision-one`，vision analyzed=1，cache_misses=1，图片从待确认变为 recognized，`validate_course.py` 通过。
- [x] 使用同配置重复编译验证缓存：`v7-image-vision-cache-clean`，cache_hits=1、cache_misses=0，37 images 全部 recognized，`validate_course.py` 通过。
- [ ] PROBLEM: 真实 vision MCP 仍依赖外部服务和 `npx @z_ai/mcp-server` 启动，单图调用可能需要十几秒；当前已有 `--image-vision-max-images` 和缓存控制成本，后续还应补进程级超时与分批进度输出。

## 阶段十九：Agent Graph 可视化

- [x] 在 `agent_graph.compiler` 中新增 graph node/edge metadata，记录每个节点的 LLM 使用、工具依赖、决策来源、状态输出和转移条件。
- [x] `build_compile_graph` 复用共享 edge metadata 注册线性边，降低真实拓扑和文档图漂移风险。
- [x] 新增 `scripts/render_agent_graph.py`，生成 `docs/agent_graph.md`、`docs/agent_graph.mmd` 和 `docs/agent_graph.dot`。
- [x] 图中区分 deterministic logic、optional LLM、external tool、conditional routing 和 human review gate，并标注 `next_action` 条件转移。
- [x] `AGENTS.md` 增加约束：修改 `agent_graph/` 拓扑、路由或节点职责后必须运行 graph render 工具并提交更新后的图。
- [x] `README.md` 增加完整工具说明和输出文件说明。
- [x] 新增单元测试校验 graph metadata 覆盖所有 transition endpoint。
- [x] 已运行 `.venv/bin/python scripts/render_agent_graph.py --render-svg`；本机缺少 Graphviz `dot`，因此 SVG 跳过，但 Markdown/Mermaid/DOT 已生成。
- [x] 已运行 `.venv/bin/python -m unittest discover -s tests -v`，29 个测试全部通过。

## 阶段二十：课程结构阶段 LLM-first

- [x] 将 `extract_units` 改为支持 LLM-first 课程单元提取，LLM 输出经规则校验后写入 `units.json`，并保留 `source_file`、`page`、`block_id`、`bbox`、`source_order`。
- [x] 将 `organize_logic` 改为支持 LLM-first 逻辑组织，输出语义/先修关系图；本地顺序边仅作为 emergency fallback 样例。
- [x] 将 `detect_gaps` 改为支持 LLM-first 缺口检测，并用本地规则补充检测碎片章节、重复标题、图片说明/教师提示/页码/作者信息成章等错误。
- [x] 将 `generate_lessons` 改为支持 LLM-first lesson 草案生成，规范化阶段强制从 unit 继承完整来源信息，过滤坏章节标题。
- [x] 修改 LangGraph 路由：四个结构节点的 emergency fallback 都会进入 `human_review`，不会继续静默导出课程。
- [x] CLI 增加 `--use-llm-structure`；脚本中 `--use-llm` 会默认启用结构阶段 LLM-first。
- [x] Markdown Sources 行追加 provenance 摘要，JSON lesson/unit/source refs 也保留完整来源字段。
- [x] 新增测试覆盖四个结构节点 LLM 调用、重复标题合并、图片说明/教师提示不成章、完整 provenance、LLM 不可用时进入 human review。
- [x] 修改 agent graph 后已重新运行 `scripts/render_agent_graph.py --render-svg`，更新 `docs/agent_graph.md/.mmd/.dot`。
- [x] 已运行 `.venv/bin/python -m unittest discover -s tests -v`，35 个测试全部通过。

## 阶段二十一：Compile Plan 审核与修订 Gate

- [x] 在 `generate_lessons` 和 `synthesize_lesson_bodies` 之间新增 `synthesize_compile_plan -> review_compile_plan_llm` gate，审核通过才允许进入正文生成。
- [x] `synthesize_compile_plan` 输出 `compile_plan.json` 和 `compile_plan.md`，覆盖课程 ID、资料范围、章节层级、来源页码、来源 block、图片插入策略、预计 token、风险提示和人工确认项。
- [x] `review_compile_plan_llm` 合并本地规则与可选 LLM 审核，检查粒度不均、短概念成章、重复标题、图片随机插入、视觉说明污染和公式混排风险。
- [x] 审核失败时写入结构化 `revise_prompt`，路由到 `revise_compile_plan`，不得直接进入 `synthesize_lesson_bodies`。
- [x] `revise_compile_plan` 写入 `compile_plan_revision_log.json`，每次修订后重新生成 plan 并回到审核；超过修订次数则进入 `human_review`。
- [x] 新增单元测试覆盖正常放行、审核失败后修订并复审、修订次数耗尽后阻断正文生成。
- [x] 已运行 `.venv/bin/python -m unittest discover -s tests -v`，38 个测试全部通过。

## 阶段二十二：Grounding 与 Quality 双层 Validation

- [x] 将旧 `check_grounding` / `check_quality` 拆分为 `check_grounding_llm`、`check_grounding_rules`、`check_quality_llm`、`check_quality_rules` 四个 graph 节点。
- [x] `check_grounding_llm` 支持检查无来源推断、错误图注、错误公式解释；`--use-llm` 默认启用，也可通过 `--use-llm-validation` 显式启用。
- [x] `check_grounding_rules` 检查 source page、block id、quote、image id、bbox 与 parsed/image artifacts 的可追溯性。
- [x] `check_quality_llm` 支持检查课程讲解连贯性、章节结构合理性、视觉工具说明/页码注释污染。
- [x] `check_quality_rules` 检查空章节、超短章节、重复标题、异常标题、公式破损、Markdown 列表与 matrix/cases/aligned 混排、代码块未闭合等确定性问题。
- [x] `validation_report.json` 改为四层 `layers` 报告，失败项包含 lesson、block、line、source_page/image_id、bbox 和 reason。
- [x] graph gate 改为四层检查全部通过才允许 `export_version`；任一失败进入 `repair_course`。
- [x] 新增测试覆盖 provenance 定位、Markdown/公式/title 规则定位、LLM validation 失败阻断 export、LLM+rules 全通过后 export。
- [x] 已运行 `.venv/bin/python -m unittest discover -s tests -v`，42 个测试全部通过。

## 阶段二十三：课程选择与课程管理界面

- [x] 清理 `course-vault/courses/` 中的历史测试课程，仅保留 `numerical-analysis`、`flash-user-guide`、`numerical-analysis-ch6-hybrid-llm`。
- [x] 将课程选择页改为响应式卡片布局，桌面端多列、移动端单列。
- [x] 课程卡片展示课程标题、简介、最近更新时间、学习进度、lesson 数、最新版本和编译状态。
- [x] 增加课程库空状态提示，引导用户从 `course-vault/raw/` 上传/创建课程。
- [x] 每张课程卡片提供“开始学习”和“管理”入口。
- [x] 新增课程管理 API：课程基础信息、资料文件列表、版本列表、章节结构、内容条目和 validation 状态。
- [x] 新增课程管理页，展示基本信息、资料文件、章节结构、最近编译时间和当前状态。
- [x] 支持课程内容条目查看、重命名和删除；重命名会同步 Markdown H1、lesson 文件名和 `lessons.json` 标题。
- [x] 本地服务验证通过：`/api/courses` 只返回 3 门课程，`/api/courses/numerical-analysis/manage` 返回 7 个资料文件、7 个章节分组、36 个内容条目。
- [x] 已运行 `node -c frontend/app.js`、`.venv/bin/python -m py_compile backend/server.py`、`.venv/bin/python -m unittest discover -s tests -v`，43 个测试全部通过。
- [ ] PROBLEM: Playwright/Chromium 截图验证因本机缺少 `libnspr4.so` 无法启动；需要安装浏览器系统依赖后才能做截图级 UI 回归。

## 阶段二十四：统一 Source Evidence 与 Revision Tool

- [x] 新增 `agent_graph/source_tools.py`，提供统一 `SourceLocator` 能力：`search`、`locate`、`get_context`、`find_images`、`verify_citations`、`verify_images`，覆盖文本、公式、表格、图片和邻近上下文。
- [x] lesson provenance 增加稳定 `source_id`，导出 Markdown Sources 同步显示 `source_id`，避免依赖 LLM 自编页码。
- [x] `generate_lessons` 和 `synthesize_lesson_bodies` 写入/携带 `lesson_evidence.json` 与 per-lesson evidence pack，使正文生成前具备显式 evidence。
- [x] `check_grounding_rules` 改为基于 `SourceLocator` 的 citation/image verification，继续输出结构化 validation failure。
- [x] `repair_course` 新增基于 `SourceLocator` evidence 的 citation/image provenance 修复，并通过 `SourceRevisionTool` 记录 patch 操作。
- [x] 新增 `SourceRevisionTool`，支持 patch/diff 风格的 lesson 正文替换、lesson 拆分/合并、图片顺序调整、引用替换、补充 evidence、compile plan/state path patch、rollback 和 version compare。
- [x] 新增 `tests/test_source_tools.py` 覆盖 source locator、citation verification、revision patch/rollback、split/merge/image/citation/evidence patch 和 version compare。
- [x] 已运行 `.venv/bin/python -m unittest discover -s tests -v`，66 个测试全部通过。
