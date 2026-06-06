const hasDocument = typeof document !== "undefined";
const courseList = hasDocument ? document.querySelector("#courseList") : null;
const courseListCompact = hasDocument ? document.querySelector("#courseListCompact") : null;
const courseDashboard = hasDocument ? document.querySelector("#courseDashboard") : null;
const readerPanel = hasDocument ? document.querySelector("#readerPanel") : null;
const managePanel = hasDocument ? document.querySelector("#managePanel") : null;
const emptyState = hasDocument ? document.querySelector("#emptyState") : null;
const refreshCoursesButton = hasDocument ? document.querySelector("#refreshCourses") : null;
const libraryFileInput = hasDocument ? document.querySelector("#libraryFileInput") : null;
const uploadQueue = hasDocument ? document.querySelector("#uploadQueue") : null;
const libraryFileList = hasDocument ? document.querySelector("#libraryFileList") : null;
const projectForm = hasDocument ? document.querySelector("#projectForm") : null;
const projectTitle = hasDocument ? document.querySelector("#projectTitle") : null;
const projectSubject = hasDocument ? document.querySelector("#projectSubject") : null;
const projectDescription = hasDocument ? document.querySelector("#projectDescription") : null;
const projectFiles = hasDocument ? document.querySelector("#projectFiles") : null;
const projectSelectedFiles = hasDocument ? document.querySelector("#projectSelectedFiles") : null;
const projectUploadInput = hasDocument ? document.querySelector("#projectUploadInput") : null;
const projectRequirements = hasDocument ? document.querySelector("#projectRequirements") : null;
const projectList = hasDocument ? document.querySelector("#projectList") : null;
const backToCoursesButton = hasDocument ? document.querySelector("#backToCourses") : null;
const backFromManageButton = hasDocument ? document.querySelector("#backFromManage") : null;
const openManagedCourseButton = hasDocument ? document.querySelector("#openManagedCourse") : null;
const readerTitle = hasDocument ? document.querySelector("#readerTitle") : null;
const manageTitle = hasDocument ? document.querySelector("#manageTitle") : null;
const manageContent = hasDocument ? document.querySelector("#manageContent") : null;
const versionSelect = hasDocument ? document.querySelector("#versionSelect") : null;
const lessonList = hasDocument ? document.querySelector("#lessonList") : null;
const lessonView = hasDocument ? document.querySelector("#lessonView") : null;
const progress = hasDocument ? document.querySelector("#progress") : null;

let courses = [];
let libraryFiles = [];
let openLibraryReports = new Set();
let libraryAnalysisReports = {};
let courseProjects = [];
let projectJobs = {};
let projectPreflightPlans = {};
let projectJobNodes = {};
let projectJobNodeDetails = {};
let editingProjectId = "";
let currentCourse = null;
let currentCourseSummary = null;
let managedCourse = null;
let versions = [];
let currentVersionId = null;
let currentLessonFile = null;
let readLessons = readJsonStorage("readLessons", []);
let readingPositions = readJsonStorage("readingPositions", {});
let lastReadingKey = readTextStorage("lastReadingKey");
let progressFrame = 0;
let projectJobPollTimer = 0;
let libraryPollTimer = 0;

async function loadCourses() {
  const [data] = await Promise.all([fetchCourseList(), loadLibraryFiles(), loadProjects()]);
  courses = data.courses.sort((a, b) => {
    if (a.id === "numerical-analysis") return -1;
    if (b.id === "numerical-analysis") return 1;
    return String(a.title || a.id).localeCompare(String(b.title || b.id));
  });
  renderCourseDashboard();
  renderCompactCourseList();
  if (!courses.length) {
    showDashboard();
    return;
  }
  const last = parseReadingKey(lastReadingKey);
  if (last && courses.some((course) => course.id === last.course)) {
    currentCourse = last.course;
  }
  showDashboard();
}

async function fetchCourseList() {
  const response = await fetch("/api/courses");
  if (response.status === 404) {
    return { courses: [] };
  }
  if (!response.ok) {
    throw new Error(`课程列表读取失败: ${response.status}`);
  }
  return response.json();
}

async function loadLibraryFiles() {
  if (!libraryFileList) {
    return;
  }
  const data = await fetch("/api/library/files").then((response) => response.json());
  libraryFiles = data.files || [];
  renderLibraryFiles();
  renderProjectFileOptions();
  scheduleLibraryPolling();
  await refreshOpenLibraryReports();
}

async function loadProjects() {
  if (!projectList) {
    return;
  }
  const data = await fetch("/api/projects").then((response) => response.json());
  courseProjects = data.projects || [];
  await loadProjectJobs();
  renderProjects();
  if (!projectRequirements.value) {
    projectRequirements.value = formatRequirements(defaultCompileRequirements());
  }
}

async function loadProjectJobs() {
  projectJobs = {};
  await Promise.all(courseProjects.map(async (project) => {
    try {
      const data = await fetch(`/api/projects/${encodeURIComponent(project.id)}/jobs`).then((response) => response.json());
      projectJobs[project.id] = data.jobs || [];
    } catch (error) {
      projectJobs[project.id] = [];
    }
  }));
}

function renderLibraryFiles() {
  if (!libraryFileList) {
    return;
  }
  if (!libraryFiles.length) {
    libraryFileList.innerHTML = `<p class="muted">暂无资料。上传后会自动进入资料分析。</p>`;
    return;
  }
  libraryFileList.innerHTML = libraryFiles.map((file) => renderLibraryFileItem(file, libraryAnalysisReports[file.id])).join("");
  libraryFileList.querySelectorAll("[data-library-report]").forEach((button) => {
    button.addEventListener("click", () => showAnalysisReport(button.dataset.libraryReport));
  });
  libraryFileList.querySelectorAll("[data-library-reparse]").forEach((button) => {
    button.addEventListener("click", () => reparseLibraryFile(button.dataset.libraryReparse));
  });
  libraryFileList.querySelectorAll("[data-library-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteLibraryFile(button.dataset.libraryDelete));
  });
}

function renderLibraryFileItem(file, report = null, reportOpenOverride = null) {
  const status = file.parse_status || file.analysis_status || "unknown";
  const progressValue = parseProgressValue(file);
  const isParsing = isActiveParse(file);
  const canCompile = Boolean(file.can_compile || (status === "parsed" && file.parsed_source_path));
  const usageText = canCompile ? "可用于课程生成" : status === "parse_failed" ? "解析失败，需重新解析后使用" : "解析完成后可用于课程生成";
  const deleteReason = file.delete_block_reason || libraryDeleteBlockReason(file);
  const reportOpen = reportOpenOverride === null ? openLibraryReports.has(file.id) : Boolean(reportOpenOverride);
  return `
    <article class="resource-item">
      <div>
        <strong>${escapeHtml(file.filename || file.id)}</strong>
        <span>${escapeHtml(file.id || "")}</span>
        <span>${escapeHtml(file.parsed_source_path || "尚无 parsed 输入")}</span>
        <div class="parse-progress-row">
          <progress value="${progressValue}" max="100"></progress>
          <span>${progressValue}% · ${escapeHtml(parseStageLabel(file.parse_current_stage || status))}</span>
        </div>
        <small class="parse-usage ${canCompile ? "ready" : "pending"}">${escapeHtml(usageText)}</small>
        ${deleteReason ? `<small class="parse-usage pending">删除受限：${escapeHtml(deleteReason)}</small>` : ""}
        ${file.parse_error ? `<small class="parse-error">${escapeHtml(file.parse_error)}</small>` : ""}
      </div>
      <div class="resource-meta">
        <span class="status-pill status-${escapeAttr(status)}">${escapeHtml(parseStatusLabel(status))}</span>
        <span>${formatBytes(file.size || 0)}</span>
      </div>
      <div class="resource-actions">
        <button type="button" data-library-report="${escapeAttr(file.id)}">${reportOpen ? "收起解析结果" : "查看解析结果"}</button>
        <button type="button" data-library-reparse="${escapeAttr(file.id)}"${isParsing ? " disabled" : ""}>重新解析</button>
        <button type="button" class="danger" data-library-delete="${escapeAttr(file.id)}">删除</button>
      </div>
      ${reportOpen ? `<div class="analysis-report">${report ? renderAnalysisReport(report) : `<p class="muted">正在读取解析报告...</p>`}</div>` : ""}
    </article>
  `;
}

function isActiveParse(file) {
  const status = file?.parse_status || file?.analysis_status || "unknown";
  return ["waiting_parse", "parsing"].includes(status) && !file?.parse_is_stale;
}

function libraryDeleteBlockReason(file) {
  if (isActiveParse(file)) {
    return "资料仍在解析中，请等待解析结束后再删除";
  }
  const projects = file?.usage?.projects || [];
  const courses = file?.usage?.courses || [];
  if (projects.length || courses.length) {
    return `资料仍被使用：${[...projects, ...courses].map((item) => item.title || item.id).join(", ")}`;
  }
  return "";
}

function parseProgressValue(file) {
  const status = file.parse_status || file.analysis_status || "unknown";
  if (status === "parsed" || status === "parse_failed") {
    return 100;
  }
  const raw = Number(file.parse_progress || 0);
  if (raw > 0) {
    return Math.max(0, Math.min(99, Math.round(raw)));
  }
  return status === "parsing" ? 10 : 0;
}

function parseStageLabel(stage) {
  return {
    waiting_parse: "等待解析",
    local_parse: "本地解析",
    mineru_parse: "MinerU 解析",
    mineru_poll: "等待 MinerU 返回",
    parsing: "解析中",
    parsed: "解析完成",
    parse_failed: "解析失败",
  }[stage] || stage || "未知阶段";
}

async function showAnalysisReport(fileId) {
  if (openLibraryReports.has(fileId)) {
    openLibraryReports.delete(fileId);
    renderLibraryFiles();
    return;
  }
  openLibraryReports.add(fileId);
  renderLibraryFiles();
  await fetchLibraryAnalysisReport(fileId);
  renderLibraryFiles();
}

async function refreshOpenLibraryReports() {
  const pending = [...openLibraryReports].filter((fileId) => {
    if (libraryAnalysisReports[fileId]) {
      return false;
    }
    const file = libraryFiles.find((item) => item.id === fileId);
    const status = file?.parse_status || file?.analysis_status || "";
    return file && !["waiting_parse", "parsing"].includes(status);
  });
  if (!pending.length) {
    return;
  }
  await Promise.all(pending.map((fileId) => fetchLibraryAnalysisReport(fileId)));
  renderLibraryFiles();
}

async function fetchLibraryAnalysisReport(fileId) {
  try {
    const response = await fetch(`/api/library/files/${encodeURIComponent(fileId)}/analysis`);
    if (!response.ok) {
      libraryAnalysisReports[fileId] = {
        parse_status: "parse_failed",
        potential_problems: [{ severity: "high", message: `读取解析报告失败: ${response.status}` }],
        parse_logs: [],
      };
    } else {
      libraryAnalysisReports[fileId] = await response.json();
    }
  } catch (error) {
    libraryAnalysisReports[fileId] = {
      parse_status: "parse_failed",
      potential_problems: [{ severity: "high", message: `读取解析报告失败: ${error}` }],
      parse_logs: [],
    };
  }
}

function renderAnalysisReport(report) {
  const chapters = report.chapter_structure || [];
  const points = report.knowledge_points || [];
  const problems = report.potential_problems || [];
  const pipeline = report.pipeline || [];
  const blocks = report.text_blocks || [];
  const formulas = report.formulas || [];
  const images = report.images || [];
  const tables = report.tables || [];
  const logs = report.parse_logs || [];
  return `
    <h3>资料分析报告</h3>
    <div class="analysis-grid">
      <div><span>解析状态</span><strong>${escapeHtml(parseStatusLabel(report.parse_status || report.status || "unknown"))}</strong></div>
      <div><span>章节</span><strong>${chapters.length}</strong></div>
      <div><span>知识点</span><strong>${points.length}</strong></div>
      <div><span>文本块</span><strong>${blocks.length}</strong></div>
      <div><span>公式</span><strong>${formulas.length}</strong></div>
      <div><span>图片</span><strong>${images.length}</strong></div>
      <div><span>表格</span><strong>${tables.length}</strong></div>
      <div><span>风险</span><strong>${problems.length}</strong></div>
    </div>
    <h4>处理流程</h4>
    <ul>${pipeline.map((item) => `<li>${escapeHtml(item.step)}: ${escapeHtml(item.status)} (${escapeHtml(item.detail || item.count || "")})</li>`).join("")}</ul>
    <h4>文本块、页码与标题</h4>
    ${blocks.length ? `<div class="block-table">${blocks.slice(0, 60).map((item) => `
      <div>
        <span>p.${Number(item.page || 0)} · line ${Number(item.line || 0)} · ${escapeHtml(item.type || "text")}</span>
        <strong>${escapeHtml(item.title || "未命名段落")}</strong>
        <p>${escapeHtml(item.text || "")}</p>
      </div>
    `).join("")}</div>` : `<p class="muted">未识别出文本块。</p>`}
    <h4>章节结构</h4>
    ${chapters.length ? `<ol>${chapters.slice(0, 12).map((item) => `<li>${escapeHtml(item.title)} <span>line ${Number(item.line || 0)}</span></li>`).join("")}</ol>` : `<p class="muted">未识别出章节。</p>`}
    <h4>主要知识点</h4>
    ${points.length ? `<ul>${points.slice(0, 12).map((item) => `<li>${escapeHtml(item.name)}</li>`).join("")}</ul>` : `<p class="muted">未识别出稳定知识点。</p>`}
    <h4>公式</h4>
    ${formulas.length ? `<ul>${formulas.slice(0, 24).map((item) => `<li>line ${Number(item.line || 0)} · ${escapeHtml(item.type || "")}: <code>${escapeHtml(item.preview || "")}</code></li>`).join("")}</ul>` : `<p class="muted">未识别出公式。</p>`}
    <h4>图片</h4>
    ${images.length ? `<ul>${images.slice(0, 24).map((item) => `<li>line ${Number(item.line || 0)} · ${escapeHtml(item.type || "")}: ${escapeHtml(item.path || item.alt || "")}</li>`).join("")}</ul>` : `<p class="muted">未识别出图片。</p>`}
    <h4>表格</h4>
    ${tables.length ? `<ul>${tables.slice(0, 24).map((item) => `<li>line ${Number(item.line || 0)} · ${escapeHtml(item.type || "")} · ${escapeHtml(item.status || "")}</li>`).join("")}</ul>` : `<p class="muted">未识别出表格。</p>`}
    <h4>解析日志</h4>
    ${logs.length ? `<pre class="parse-log">${escapeHtml(logs.join("\n"))}</pre>` : `<p class="muted">暂无解析日志。</p>`}
    <h4>潜在问题</h4>
    ${problems.length ? `<ul>${problems.map((item) => `<li><strong>${escapeHtml(item.severity || "")}</strong> ${escapeHtml(item.message || item.type)}</li>`).join("")}</ul>` : `<p class="muted">未发现明显风险。</p>`}
  `;
}

async function reparseLibraryFile(fileId) {
  const response = await fetch(`/api/library/files/${encodeURIComponent(fileId)}/parse`, { method: "POST" });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "启动解析失败"));
    return;
  }
  delete libraryAnalysisReports[fileId];
  openLibraryReports.add(fileId);
  await loadLibraryFiles();
}

async function deleteLibraryFile(fileId) {
  const file = libraryFiles.find((item) => item.id === fileId) || { id: fileId, filename: fileId };
  const name = file.filename || file.id || fileId;
  const blockReason = file.delete_block_reason || libraryDeleteBlockReason(file);
  if (blockReason) {
    window.alert(`暂不能删除资料“${name}”。\n${blockReason}`);
    return;
  }
  const confirmed = window.confirm(`删除资料“${name}”？\n\n仅当没有课程或课程项目使用这一来源时才会删除。此操作会移除原始文件和解析结果。`);
  if (!confirmed) {
    return;
  }
  const response = await fetch(`/api/library/files/${encodeURIComponent(fileId)}`, { method: "DELETE" });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "删除资料失败"));
    return;
  }
  delete libraryAnalysisReports[fileId];
  openLibraryReports.delete(fileId);
  await loadLibraryFiles();
  await loadProjects();
}

async function responseErrorMessage(response, fallback) {
  const text = await response.text();
  try {
    const payload = JSON.parse(text);
    const error = payload?.error || {};
    const message = error.message || text.trim();
    return `${fallback}: ${response.status}${message ? `\n${message}` : ""}`;
  } catch (_error) {
    // Fall through to stdlib HTML error parsing for older server responses.
  }
  const message = text.match(/<p>Message: ([\s\S]*?)\.<\/p>/)?.[1] || text.match(/<title>Error response<\/title>[\s\S]*?<p>Error code: \d+<\/p>[\s\S]*?<p>Message: ([\s\S]*?)<\/p>/)?.[1] || text.trim();
  return `${fallback}: ${response.status}${message ? `\n${decodeHtmlEntities(message)}` : ""}`;
}

function renderProjectFileOptions() {
  if (!projectFiles) {
    return;
  }
  const selected = new Set([...projectFiles.selectedOptions].map((option) => option.value));
  projectFiles.innerHTML = libraryFiles.map((file) => (
    `<option value="${escapeAttr(file.id)}"${selected.has(file.id) ? " selected" : ""}>${escapeHtml(file.filename || file.id)} · ${escapeHtml(parseStatusLabel(file.parse_status || file.analysis_status || "unknown"))}${file.can_compile ? " · 可用" : ""}</option>`
  )).join("");
  renderProjectSelectedFiles();
}

function renderProjectSelectedFiles() {
  if (!projectSelectedFiles || !projectFiles) {
    return;
  }
  const selected = [...projectFiles.selectedOptions].map((option) => {
    const file = libraryFiles.find((item) => item.id === option.value) || { id: option.value, filename: option.textContent };
    return file;
  });
  if (!selected.length) {
    projectSelectedFiles.innerHTML = `<span class="muted">尚未关联资料。</span>`;
    return;
  }
  projectSelectedFiles.innerHTML = selected.map((file) => `
    <span class="selected-source">
      ${escapeHtml(file.filename || file.id)}
      <small>${escapeHtml(parseStatusLabel(file.parse_status || file.analysis_status || "unknown"))}${file.can_compile ? " · 可用于课程生成" : ""}</small>
      <button type="button" aria-label="移除资料 ${escapeAttr(file.filename || file.id)}" data-remove-project-file="${escapeAttr(file.id)}">×</button>
    </span>
  `).join("");
  projectSelectedFiles.querySelectorAll("[data-remove-project-file]").forEach((button) => {
    button.addEventListener("click", () => {
      [...projectFiles.options].forEach((option) => {
        if (option.value === button.dataset.removeProjectFile) {
          option.selected = false;
        }
      });
      renderProjectSelectedFiles();
    });
  });
}

function renderProjects() {
  if (!projectList) {
    return;
  }
  if (!courseProjects.length) {
    projectList.innerHTML = `<p class="muted">暂无课程项目。</p>`;
    return;
  }
  projectList.innerHTML = courseProjects.map((project) => `
    <article class="resource-item">
      <div>
        <strong>${escapeHtml(project.title || project.id)}</strong>
        <span>${escapeHtml(project.subject || "未设置学科方向")} · ${Number((project.library_file_ids || []).length)} 个资料引用</span>
        ${renderProjectStatus(project)}
        ${renderProjectNextStep(project)}
        ${renderProjectJobStatus(project)}
      </div>
      <div class="resource-actions">
        <button type="button" data-project-edit="${escapeAttr(project.id)}">编辑配置</button>
        <button type="button" data-project-plan="${escapeAttr(project.id)}">生成编译计划</button>
        <button type="button" data-project-compile="${escapeAttr(project.id)}"${!projectCanCompile(project) ? " disabled" : ""} title="${escapeAttr(projectCompileBlockReason(project))}">按确认方案编译</button>
      </div>
      ${renderProjectJobControlPanel(project)}
      ${renderProjectJobIntermediatePanel(project)}
      ${renderProjectPreflightPanel(project)}
    </article>
  `).join("");
  projectList.querySelectorAll("[data-project-edit]").forEach((button) => {
    button.addEventListener("click", () => editProject(button.dataset.projectEdit));
  });
  projectList.querySelectorAll("[data-project-compile]").forEach((button) => {
    button.addEventListener("click", () => startProjectCompile(button.dataset.projectCompile));
  });
  projectList.querySelectorAll("[data-project-plan]").forEach((button) => {
    button.addEventListener("click", () => generateProjectPreflight(button.dataset.projectPlan));
  });
  projectList.querySelectorAll("[data-project-confirm-plan]").forEach((button) => {
    button.addEventListener("click", () => confirmProjectPlan(button.dataset.projectConfirmPlan));
  });
  projectList.querySelectorAll("[data-job-control]").forEach((button) => {
    button.addEventListener("click", () => controlProjectJob(button));
  });
  projectList.querySelectorAll("[data-job-nodes-load]").forEach((button) => {
    button.addEventListener("click", () => loadProjectJobNodes(button.dataset.jobNodesLoad));
  });
  projectList.querySelectorAll("[data-job-node-select]").forEach((button) => {
    button.addEventListener("click", () => loadProjectJobNodeDetail(button.dataset.jobId, button.dataset.jobNodeSelect));
  });
  projectList.querySelectorAll("[data-job-review]").forEach((button) => {
    button.addEventListener("click", () => submitProjectJobReview(button));
  });
  scheduleProjectJobPolling();
}

function renderProjectStatus(project) {
  const state = project.status || "not_started";
  return `<span class="project-state"><span class="status-pill status-${escapeAttr(state)}">${escapeHtml(projectStatusLabel(state))}</span></span>`;
}

function renderProjectNextStep(project) {
  const reason = projectCompileBlockReason(project);
  const latest = (projectJobs[project.id] || [])[0];
  const text = latest?.state === "waiting_review" ? "下一步：处理人工审核" : reason ? `下一步：${reason}` : "下一步：按确认方案编译";
  return `<span class="project-next-step">${escapeHtml(text)}</span>`;
}

function renderProjectJobStatus(project) {
  const latest = (projectJobs[project.id] || [])[0];
  if (!latest) {
    return `<span class="project-job muted">暂无编译任务</span>`;
  }
  const label = {
    queued: "等待编译",
    running: "编译中",
    paused: "已暂停",
    terminating: "终止中",
    terminated: "已终止",
    done: "编译完成",
    failed: "编译失败",
    blocked: "需要预处理",
    waiting_review: "待人工审核",
  }[latest.state] || latest.state || "unknown";
  const detail = latest.error || latest.current_stage || latest.version || "";
  return `
    <span class="project-job">
      <span class="status-pill status-${escapeAttr(latest.state || "unknown")}">${escapeHtml(label)}</span>
      <progress value="${Number(latest.progress || 0)}" max="100"></progress>
      <span>${Number(latest.progress || 0)}%</span>
      ${detail ? `<small>${escapeHtml(detail)}</small>` : ""}
    </span>
  `;
}

function renderProjectJobControlPanel(project) {
  const latest = (projectJobs[project.id] || [])[0];
  if (!latest) {
    return "";
  }
  const canPause = latest.state === "running";
  const canResume = latest.state === "paused";
  const canTerminate = ["running", "paused", "queued", "waiting_review"].includes(latest.state);
  const canRerun = ["done", "failed", "blocked", "terminated", "waiting_review"].includes(latest.state);
  const node = latest.current_stage || "synthesize_lesson_bodies";
  return `
    <div class="job-control-panel">
      <button type="button" data-job-control="pause" data-job-id="${escapeAttr(latest.id)}"${!canPause ? " disabled" : ""}>暂停</button>
      <button type="button" data-job-control="resume" data-job-id="${escapeAttr(latest.id)}"${!canResume ? " disabled" : ""}>继续</button>
      <button type="button" data-job-control="terminate" data-job-id="${escapeAttr(latest.id)}"${!canTerminate ? " disabled" : ""}>终止</button>
      <button type="button" data-job-control="rerun-current" data-job-id="${escapeAttr(latest.id)}"${!canRerun ? " disabled" : ""}>重跑当前节点</button>
      <label>
        <span>从节点重跑</span>
        <select data-rerun-node="${escapeAttr(latest.id)}">
          ${compileNodeOptions(node)}
        </select>
      </label>
      <button type="button" data-job-control="rerun-from-node" data-job-id="${escapeAttr(latest.id)}"${!canRerun ? " disabled" : ""}>执行</button>
      <button type="button" data-job-control="clear-results-rerun" data-job-id="${escapeAttr(latest.id)}"${!canRerun ? " disabled" : ""}>清空结果重新编译</button>
    </div>
  `;
}

function renderProjectJobIntermediatePanel(project) {
  const latest = (projectJobs[project.id] || [])[0];
  if (!latest) {
    return "";
  }
  const nodes = projectJobNodes[latest.id] || latest.nodes || [];
  const detail = projectJobNodeDetails[latest.id] || null;
  return renderJobIntermediatePanel(latest, nodes, detail);
}

function renderJobIntermediatePanel(job, nodes = [], detail = null) {
  const nodeRows = (nodes || []).map((node) => `
    <button type="button" class="job-node-row" data-job-id="${escapeAttr(job.id)}" data-job-node-select="${escapeAttr(node.node || "")}">
      <span>${escapeHtml(node.node || "")}</span>
      <span class="status-pill status-${escapeAttr(node.status || "pending")}">${escapeHtml(nodeStatusLabel(node.status || "pending"))}</span>
      <small>${Number(node.output_count || 0)} 输出 · ${Number(node.error_count || 0)} 错误</small>
    </button>
  `).join("");
  return `
    <div class="job-results-panel">
      <div class="job-results-header">
        <div>
          <h3>编译中间结果</h3>
          <span>${escapeHtml(job.id || "")}</span>
        </div>
        <button type="button" data-job-nodes-load="${escapeAttr(job.id)}">刷新节点结果</button>
      </div>
      ${job.state === "waiting_review" ? renderJobReviewPanel(job) : ""}
      <details class="technical-details">
        <summary>技术详情</summary>
        ${nodes.length ? `<div class="job-node-list">${nodeRows}</div>` : `<p class="muted">点击刷新节点结果查看 source brief、图片理解、课程计划、正文、Markdown 检查和质量检查。</p>`}
        ${detail ? renderJobNodeDetail(detail) : ""}
      </details>
    </div>
  `;
}

function renderJobReviewPanel(job) {
  const summary = job.review_summary || {};
  const failures = summary.failures || [];
  const target = summary.default_target_node || "repair_course";
  return `
    <div class="job-review-panel">
      <strong>流程已阻塞在人工审核</strong>
      <p>${escapeHtml(summary.reason || job.error || "需要人工审核")}</p>
      ${failures.length ? `<div class="review-failure-list">${failures.map((item) => `
        <article>
          <strong>${escapeHtml(item.lesson_id || "")} ${escapeHtml(item.lesson_title || "")}</strong>
          <span>${escapeHtml(item.stage || "")} · ${escapeHtml(item.type || "")} · line ${Number(item.line || 0)}</span>
          <p>${escapeHtml(item.message || "")}${item.block_id ? ` · ${escapeHtml(item.block_id)}` : ""}</p>
        </article>
      `).join("")}</div>` : `<p class="muted">未读取到结构化失败项，请打开技术详情查看 human_review.json。</p>`}
      <p class="muted">${escapeHtml(summary.help_text || "通过表示允许系统继续自动修复；跳过可能导出仍有问题的课程。")}</p>
      <textarea data-review-feedback="${escapeAttr(job.id)}" rows="3" placeholder="可选：填写修改意见，后续 agent 会读取这段反馈。"></textarea>
      <label>
        <span>目标节点</span>
        <select data-review-target="${escapeAttr(job.id)}">
          ${compileNodeOptions(target)}
        </select>
      </label>
      <div class="job-review-actions">
        <button type="button" data-job-review="approve" data-job-id="${escapeAttr(job.id)}">允许自动修复并继续</button>
        <button type="button" data-job-review="request-modification" data-job-id="${escapeAttr(job.id)}">填写意见并重跑</button>
        <button type="button" data-job-review="rollback" data-job-id="${escapeAttr(job.id)}">回退到规划</button>
        <button type="button" data-job-review="skip" data-job-id="${escapeAttr(job.id)}">跳过审核并尝试导出</button>
        <button type="button" data-job-control="terminate" data-job-id="${escapeAttr(job.id)}">终止</button>
      </div>
    </div>
  `;
}

function renderJobNodeDetail(detail) {
  const artifacts = (label, items) => `
    <section>
      <h4>${escapeHtml(label)}</h4>
      ${(items || []).map((item) => `
        <article class="artifact-preview">
          <div><strong>${escapeHtml(item.name || "")}</strong><span>${item.exists ? `${Number(item.size || 0).toLocaleString("zh-CN")} bytes` : "未生成"}</span></div>
          ${item.exists ? `<pre>${escapeHtml(formatArtifactPreview(item.preview))}</pre>` : ""}
        </article>
      `).join("") || `<p class="muted">无记录</p>`}
    </section>
  `;
  return `
    <div class="job-node-detail">
      <h3>${escapeHtml(detail.node || "")}</h3>
      <p><span class="status-pill status-${escapeAttr(detail.status || "pending")}">${escapeHtml(nodeStatusLabel(detail.status || "pending"))}</span></p>
      ${detail.errors?.length ? `<div class="node-errors"><strong>错误</strong><pre>${escapeHtml(JSON.stringify(detail.errors, null, 2))}</pre></div>` : ""}
      ${artifacts("输入", detail.inputs)}
      ${artifacts("输出", detail.outputs)}
      ${detail.review && Object.keys(detail.review).length ? artifacts("人工审核", [{ name: "human_review.json", exists: true, preview: detail.review }]) : ""}
      ${detail.review_decisions?.length ? artifacts("审核决策", [{ name: "review_decisions.jsonl", exists: true, preview: detail.review_decisions }]) : ""}
    </div>
  `;
}

function formatArtifactPreview(value) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function nodeStatusLabel(state) {
  return {
    pending: "待运行",
    running: "运行中",
    paused: "已暂停",
    terminating: "终止中",
    finished: "已完成",
    failed: "失败",
    waiting_review: "待人工审核",
  }[state] || state || "未知";
}

function compileNodeOptions(selected) {
  const nodes = [
    "parse_sources",
    "understand_images",
    "build_source_index",
    "synthesize_source_brief",
    "plan_course",
    "synthesize_lesson_notes",
    "generate_lessons",
    "synthesize_compile_plan",
    "review_compile_plan_llm",
    "revise_compile_plan",
    "synthesize_lesson_bodies",
    "check_markdown_syntax",
    "check_grounding_rules",
    "check_quality_rules",
    "repair_course",
    "human_review",
    "export_version",
  ];
  return nodes.map((node) => `<option value="${escapeAttr(node)}"${node === selected ? " selected" : ""}>${escapeHtml(node)}</option>`).join("");
}

async function controlProjectJob(button) {
  const action = button.dataset.jobControl;
  const jobId = button.dataset.jobId;
  const payload = {};
  if (action === "rerun-from-node") {
    payload.node = projectList.querySelector(`[data-rerun-node="${cssEscape(jobId)}"]`)?.value || "";
  }
  if (action === "clear-results-rerun") {
    payload.clear_results = true;
  }
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/${encodeURIComponent(action)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    window.alert(`任务控制失败: ${response.status}`);
    return;
  }
  await loadProjects();
}

async function loadProjectJobNodes(jobId) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/nodes`);
  if (!response.ok) {
    window.alert(`读取节点结果失败: ${response.status}`);
    return;
  }
  const data = await response.json();
  projectJobNodes[jobId] = data.nodes || [];
  renderProjects();
}

async function loadProjectJobNodeDetail(jobId, node) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/nodes/${encodeURIComponent(node)}`);
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "读取节点详情失败"));
    return;
  }
  projectJobNodeDetails[jobId] = await response.json();
  if (!projectJobNodes[jobId]) {
    await loadProjectJobNodes(jobId);
    return;
  }
  renderProjects();
}

async function submitProjectJobReview(button) {
  const action = button.dataset.jobReview;
  const jobId = button.dataset.jobId;
  const feedback = projectList.querySelector(`[data-review-feedback="${cssEscape(jobId)}"]`)?.value || "";
  const targetNode = projectList.querySelector(`[data-review-target="${cssEscape(jobId)}"]`)?.value || "";
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/review/${encodeURIComponent(action)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feedback, target_node: targetNode }),
  });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "提交审核操作失败"));
    return;
  }
  projectJobNodes[jobId] = [];
  projectJobNodeDetails[jobId] = null;
  await loadProjects();
}

function projectHasRunningJob(projectId) {
  return (projectJobs[projectId] || []).some((job) => ["queued", "running", "paused", "terminating", "waiting_review"].includes(job.state));
}

function projectSourcesReady(project) {
  const ids = project?.library_file_ids || [];
  if (!ids.length) {
    return false;
  }
  return ids.every((id) => {
    const file = libraryFiles.find((item) => item.id === id);
    return Boolean(file?.can_compile || (file?.parse_status === "parsed" && file?.parsed_source_path));
  });
}

function projectCanCompile(project) {
  return Boolean(project?.confirmed_compile_snapshot?.plan_id) && projectSourcesReady(project) && !projectHasRunningJob(project.id);
}

function projectCompileBlockReason(project) {
  const ids = project?.library_file_ids || [];
  if (!ids.length) {
    return "先选择资料并保存课程项目";
  }
  const blockedSources = ids.map((id) => libraryFiles.find((item) => item.id === id) || { id }).filter((file) => !Boolean(file?.can_compile || (file?.parse_status === "parsed" && file?.parsed_source_path)));
  if (blockedSources.length) {
    return `等待资料解析完成：${blockedSources.map((file) => file.filename || file.id).join(", ")}`;
  }
  if (!project?.confirmed_compile_snapshot?.plan_id) {
    return "生成并确认编译计划";
  }
  const latest = (projectJobs[project.id] || [])[0];
  if (latest && ["queued", "running", "paused", "terminating", "waiting_review"].includes(latest.state)) {
    return latest.state === "waiting_review" ? "处理人工审核" : "等待当前编译任务结束";
  }
  return "";
}

function renderProjectPreflightPanel(project) {
  const plan = projectPreflightPlans[project.id] || project.confirmed_compile_snapshot?.preflight_plan;
  if (!plan) {
    if (project.confirmed_compile_snapshot?.plan_id) {
      return `<div class="preflight-panel"><p class="muted">已确认计划 ${escapeHtml(project.confirmed_compile_snapshot.plan_id)}，可按确认方案编译。</p></div>`;
    }
    return "";
  }
  const confirmed = project.confirmed_compile_snapshot?.plan_id === plan.id;
  const selectedScheme = project.confirmed_compile_snapshot?.selected_scheme_id || plan.default_scheme_id || "systematic";
  const sources = plan.source_scope?.sources || [];
  const outline = plan.preliminary_outline || [];
  const risks = plan.risks || [];
  return `
    <div class="preflight-panel">
      <div class="preflight-header">
        <div>
          <h3>编译前计划</h3>
          <span>${escapeHtml(plan.id || "")}</span>
        </div>
        <span class="status-pill status-${confirmed ? "not_started" : "awaiting_confirmation"}">${confirmed ? "已确认" : "待确认"}</span>
      </div>
      <div class="preflight-metrics">
        <div><span>资料</span><strong>${Number(plan.source_scope?.source_count || sources.length)}</strong></div>
        <div><span>预计章节</span><strong>${Number(plan.estimated_lesson_count || 0)}</strong></div>
        <div><span>学习时间</span><strong>${Number(plan.estimated_study_minutes || 0)} 分钟</strong></div>
        <div><span>Token</span><strong>${Number(plan.estimated_token_cost?.total_tokens || 0).toLocaleString("zh-CN")}</strong></div>
      </div>
      <h4>资料范围与解析结果</h4>
      <ul class="preflight-source-list">
        ${sources.map((source) => `
          <li>
            <strong>${escapeHtml(source.filename || source.id)}</strong>
            <span>${escapeHtml(source.analysis_status || "unknown")} · ${Number(source.chapter_count || 0)} 章 · ${Number(source.formula_count || 0)} 公式 · ${Number(source.image_count || 0)} 图</span>
          </li>
        `).join("")}
      </ul>
      <h4>初步大纲</h4>
      <ol class="preflight-outline">
        ${outline.slice(0, 6).map((group) => `<li><strong>${escapeHtml(group.title || "")}</strong><span>${(group.chapters || []).slice(0, 5).map((chapter) => escapeHtml(chapter.title || "")).join(" / ")}</span></li>`).join("")}
      </ol>
      <h4>编译要求</h4>
      <dl class="requirements-list">
        ${Object.entries(plan.compile_requirements || {}).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}
      </dl>
      <h4>方案</h4>
      <div class="scheme-grid">
        ${(plan.schemes || []).map((scheme) => `
          <label class="scheme-option">
            <input type="radio" name="scheme-${escapeAttr(project.id)}" value="${escapeAttr(scheme.id)}"${scheme.id === selectedScheme ? " checked" : ""}${confirmed ? " disabled" : ""}>
            <strong>${escapeHtml(scheme.title || scheme.id)}</strong>
            <span>${escapeHtml(scheme.summary || "")}</span>
            <small>${Number(scheme.target_lesson_count || 0)} 节 · ${Number(scheme.estimated_study_minutes || 0)} 分钟</small>
          </label>
        `).join("")}
      </div>
      <h4>风险提示</h4>
      <ul class="risk-list">${risks.map((risk) => `<li><strong>${escapeHtml(risk.severity || "")}</strong> ${escapeHtml(risk.message || risk.type || "")}</li>`).join("")}</ul>
      <div class="preflight-actions">
        <button class="primary-action" type="button" data-project-confirm-plan="${escapeAttr(project.id)}"${confirmed ? " disabled" : ""}>确认资料范围、要求和方案</button>
      </div>
    </div>
  `;
}

async function generateProjectPreflight(projectId) {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/preflight-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "生成计划失败"));
    return;
  }
  const data = await response.json();
  projectPreflightPlans[projectId] = data.plan;
  await loadProjects();
  projectPreflightPlans[projectId] = data.plan;
  renderProjects();
}

async function confirmProjectPlan(projectId) {
  const plan = projectPreflightPlans[projectId] || courseProjects.find((project) => project.id === projectId)?.confirmed_compile_snapshot?.preflight_plan;
  if (!plan) {
    return;
  }
  const selected = projectList.querySelector(`input[name="scheme-${cssEscape(projectId)}"]:checked`)?.value || plan.default_scheme_id || "systematic";
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/confirm-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: plan.id, selected_scheme_id: selected }),
  });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "确认计划失败"));
    return;
  }
  await loadLibraryFiles();
  await loadProjects();
}

async function startProjectCompile(projectId) {
  const project = courseProjects.find((item) => item.id === projectId);
  const planId = project?.confirmed_compile_snapshot?.plan_id || "";
  if (!planId) {
    window.alert("请先生成并确认本次编译的资料范围、编译要求和方案。");
    return;
  }
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/compile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan_id: planId }),
  });
  if (!response.ok) {
    window.alert(await responseErrorMessage(response, "启动编译失败"));
    return;
  }
  await loadProjects();
}

function scheduleProjectJobPolling() {
  if (projectJobPollTimer) {
    window.clearTimeout(projectJobPollTimer);
    projectJobPollTimer = 0;
  }
  const hasRunning = Object.values(projectJobs).some((jobs) => jobs.some((job) => ["queued", "running", "paused", "terminating"].includes(job.state)));
  if (!hasRunning) {
    return;
  }
  projectJobPollTimer = window.setTimeout(async () => {
    projectJobPollTimer = 0;
    await loadProjects();
    await loadCourses();
  }, 3000);
}

function scheduleLibraryPolling() {
  if (libraryPollTimer) {
    window.clearTimeout(libraryPollTimer);
    libraryPollTimer = 0;
  }
  const hasParsing = libraryFiles.some((file) => isActiveParse(file));
  if (!hasParsing) {
    return;
  }
  libraryPollTimer = window.setTimeout(async () => {
    libraryPollTimer = 0;
    await loadLibraryFiles();
  }, 2500);
}

function editProject(projectId) {
  const project = courseProjects.find((item) => item.id === projectId);
  if (!project) {
    return;
  }
  editingProjectId = project.id;
  projectTitle.value = project.title || "";
  projectSubject.value = project.subject || "";
  projectDescription.value = project.description || "";
  renderProjectFileOptions();
  const selected = new Set(project.library_file_ids || []);
  [...projectFiles.options].forEach((option) => {
    option.selected = selected.has(option.value);
  });
  projectRequirements.value = formatRequirements(project.compile_requirements || defaultCompileRequirements());
  renderProjectSelectedFiles();
}

function renderCourseDashboard() {
  if (!courseList) {
    return;
  }
  courseList.innerHTML = "";
  emptyState.hidden = courses.length > 0;
  courses.forEach((course) => {
    const card = document.createElement("article");
    card.className = "course-card";
    card.tabIndex = 0;
    const progressValue = courseProgress(course);
    card.innerHTML = `
      <div class="course-card-header">
        <div>
          <h2>${escapeHtml(course.title || course.id)}</h2>
          <p>${escapeHtml(course.description || "")}</p>
        </div>
        <span class="status-pill status-${escapeAttr(course.status?.state || "unknown")}">${escapeHtml(course.status?.label || "状态未知")}</span>
      </div>
      <div class="course-card-meta">
        <span>${escapeHtml(course.latest_version || "no version")}</span>
        <span>${Number(course.lesson_count || 0)} 节</span>
        <span>${formatDate(course.updated_at)}</span>
      </div>
      <div class="course-progress-row">
        <progress value="${progressValue}" max="100"></progress>
        <span>${progressValue}%</span>
      </div>
      <div class="course-card-actions">
        <button class="primary-action" type="button" data-action="open">开始学习</button>
        <button class="secondary-action" type="button" data-action="manage">管理</button>
      </div>
    `;
    card.addEventListener("click", (event) => {
      const action = event.target?.dataset?.action;
      if (action === "manage") {
        showManage(course.id);
      } else {
        selectCourse(course.id);
      }
    });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        selectCourse(course.id);
      }
    });
    courseList.appendChild(card);
  });
}

function renderCompactCourseList() {
  if (!courseListCompact) {
    return;
  }
  courseListCompact.innerHTML = "";
  courses.forEach((course) => {
    const button = document.createElement("button");
    button.textContent = course.title || course.id;
    button.dataset.courseId = course.id;
    button.addEventListener("click", () => selectCourse(course.id));
    courseListCompact.appendChild(button);
  });
}

async function selectCourse(courseId) {
  saveReadingPosition();
  currentCourse = courseId;
  currentCourseSummary = courses.find((course) => course.id === courseId) || null;
  showReader();
  document.querySelectorAll(".course-list button").forEach((item) => {
    item.classList.toggle("active", item.dataset.courseId === courseId);
  });
  if (readerTitle) {
    readerTitle.textContent = currentCourseSummary?.title || courseId;
  }
  const data = await fetch(`/api/courses/${courseId}/versions`).then((response) => response.json());
  versions = data.versions;
  versionSelect.innerHTML = versions.map((version) => `<option value="${version.id}">${version.id}</option>`).join("");
  const last = parseReadingKey(lastReadingKey);
  if (versions.length) {
    const restored = last && last.course === courseId && versions.some((version) => version.id === last.version);
    versionSelect.value = restored ? last.version : versions[versions.length - 1].id;
  }
  renderLessons();
}

function showDashboard() {
  saveReadingPosition();
  courseDashboard.hidden = false;
  readerPanel.hidden = true;
  managePanel.hidden = true;
  renderCourseDashboard();
}

function showReader() {
  courseDashboard.hidden = true;
  readerPanel.hidden = false;
  managePanel.hidden = true;
}

async function showManage(courseId) {
  saveReadingPosition();
  managedCourse = courseId;
  courseDashboard.hidden = true;
  readerPanel.hidden = true;
  managePanel.hidden = false;
  manageTitle.textContent = "加载中";
  manageContent.innerHTML = "";
  const data = await fetch(`/api/courses/${courseId}/manage`).then((response) => response.json());
  renderManagePage(data);
}

function renderLessons() {
  saveReadingPosition();
  const version = versions.find((item) => item.id === versionSelect.value) || versions[0];
  lessonList.innerHTML = "";
  if (!version) {
    lessonView.textContent = "No versions exported.";
    currentVersionId = null;
    currentLessonFile = null;
    updateReadingProgress();
    return;
  }
  const last = parseReadingKey(lastReadingKey);
  let selectedButton = null;
  version.lessons.forEach((lesson, index) => {
    const button = document.createElement("button");
    button.textContent = lesson.title;
    button.dataset.file = lesson.file;
    button.addEventListener("click", () => loadLesson(version.id, lesson.file, button));
    lessonList.appendChild(button);
    const restored = last && last.course === currentCourse && last.version === version.id && last.file === lesson.file;
    if (restored || (!last && index === 0) || (last && index === 0 && !selectedButton)) {
      selectedButton = button;
    }
  });
  if (selectedButton) {
    selectedButton.click();
  }
}

async function loadLesson(versionId, file, button) {
  saveReadingPosition();
  document.querySelectorAll(".lesson-list button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  currentVersionId = versionId;
  currentLessonFile = file;
  const key = readingKey();
  if (key) {
    readLessons.add(key);
    lastReadingKey = key;
    localStorage.setItem("readLessons", JSON.stringify([...readLessons]));
    localStorage.setItem("lastReadingKey", key);
  }
  const lesson = await fetch(`/api/courses/${currentCourse}/versions/${versionId}/${file}`).then((response) => response.json());
  lessonView.innerHTML = renderMarkdown(lesson.markdown);
  window.scrollTo(0, 0);
  await typesetMath();
  restoreReadingPosition();
  updateReadingProgress();
  renderCourseDashboard();
}

function renderManagePage(data) {
  const course = data.course || {};
  const status = course.status || data.status || {};
  manageTitle.textContent = course.title || course.id || "Course";
  openManagedCourseButton.onclick = () => selectCourse(course.id);
  const sourceFiles = data.source_files || [];
  const sections = data.chapter_structure || [];
  const entries = data.content_entries || [];
  manageContent.innerHTML = `
    <section class="manage-summary">
      <div><span>课程 ID</span><strong>${escapeHtml(course.id || "")}</strong></div>
      <div><span>当前版本</span><strong>${escapeHtml(data.latest_version || course.latest_version || "")}</strong></div>
      <div><span>最近编译</span><strong>${formatDate(course.updated_at)}</strong></div>
      <div><span>状态</span><strong class="status-text status-${escapeAttr(status.state || "unknown")}">${escapeHtml(status.label || "状态未知")}</strong></div>
    </section>
    <section class="manage-section">
      <h2>资料文件</h2>
      ${sourceFiles.length ? `<ul class="file-list">${sourceFiles.map((file) => `<li>${escapeHtml(file)}</li>`).join("")}</ul>` : `<p class="muted">暂无资料文件记录。</p>`}
    </section>
    <section class="manage-section">
      <h2>章节结构</h2>
      ${sections.length ? sections.map(renderManageSection).join("") : `<p class="muted">暂无章节结构。</p>`}
    </section>
    <section class="manage-section">
      <h2>内容条目</h2>
      <div class="entry-list">
        ${entries.length ? entries.map((entry) => renderContentEntry(entry, data.latest_version || course.latest_version, course.id)).join("") : `<p class="muted">暂无内容条目。</p>`}
      </div>
    </section>
  `;
  manageContent.querySelectorAll("[data-entry-action]").forEach((button) => {
    button.addEventListener("click", () => handleEntryAction(button));
  });
}

function renderManageSection(section) {
  return `
    <details class="chapter-group" open>
      <summary>${escapeHtml(section.title || "Course")}</summary>
      <ol>
        ${(section.lessons || []).map((lesson) => `<li>${escapeHtml(lesson.title || lesson.id || "")}</li>`).join("")}
      </ol>
    </details>
  `;
}

function renderContentEntry(entry, version, courseId) {
  return `
    <div class="content-entry">
      <div>
        <strong>${escapeHtml(entry.title || entry.file)}</strong>
        <span>${escapeHtml(entry.file || "")}</span>
      </div>
      <div class="entry-actions">
        <button type="button" data-entry-action="view" data-course="${escapeAttr(courseId)}" data-version="${escapeAttr(version)}" data-file="${escapeAttr(entry.file)}">查看</button>
        <button type="button" data-entry-action="rename" data-course="${escapeAttr(courseId)}" data-version="${escapeAttr(version)}" data-file="${escapeAttr(entry.file)}">重命名</button>
        <button type="button" data-entry-action="delete" data-course="${escapeAttr(courseId)}" data-version="${escapeAttr(version)}" data-file="${escapeAttr(entry.file)}">删除</button>
      </div>
    </div>
  `;
}

async function handleEntryAction(button) {
  const { entryAction, course, version, file } = button.dataset;
  if (entryAction === "view") {
    await selectCourse(course);
    versionSelect.value = version;
    renderLessons();
    const lessonButton = [...lessonList.querySelectorAll("button")].find((item) => item.dataset.file === file);
    if (lessonButton) {
      await loadLesson(version, file, lessonButton);
    }
    return;
  }
  if (entryAction === "rename") {
    const title = window.prompt("新的章节标题", button.closest(".content-entry").querySelector("strong").textContent);
    if (!title) {
      return;
    }
    await fetch(`/api/courses/${encodeURIComponent(course)}/versions/${encodeURIComponent(version)}/${encodeURIComponent(file)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    await showManage(course);
    return;
  }
  if (entryAction === "delete" && window.confirm("删除这个课程内容条目？")) {
    await fetch(`/api/courses/${encodeURIComponent(course)}/versions/${encodeURIComponent(version)}/${encodeURIComponent(file)}`, { method: "DELETE" });
    await showManage(course);
  }
}

function uploadLibraryFiles(files) {
  if (!files.length) {
    return;
  }
  const form = new FormData();
  [...files].forEach((file) => form.append("files", file));
  const row = document.createElement("div");
  row.className = "upload-row";
  row.innerHTML = `
    <span>${[...files].map((file) => escapeHtml(file.name)).join(", ")}</span>
    <progress value="0" max="100"></progress>
    <strong>等待上传</strong>
  `;
  uploadQueue.prepend(row);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/library/upload");
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) {
      return;
    }
    const percent = Math.round((event.loaded / event.total) * 100);
    row.querySelector("progress").value = percent;
    row.querySelector("strong").textContent = `上传 ${percent}%`;
  });
  xhr.addEventListener("load", async () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      row.querySelector("progress").value = 100;
      row.querySelector("strong").textContent = "已上传，等待后端解析";
      await loadLibraryFiles();
    } else {
      row.querySelector("strong").textContent = `失败 ${xhr.status}`;
    }
  });
  xhr.addEventListener("error", () => {
    row.querySelector("strong").textContent = "上传失败";
  });
  xhr.send(form);
}

async function uploadProjectFiles(files) {
  if (!files.length || !projectFiles) {
    return;
  }
  const form = new FormData();
  [...files].forEach((file) => form.append("files", file));
  const response = await fetch("/api/library/upload", {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    window.alert(`上传失败: ${response.status}`);
    return;
  }
  const data = await response.json();
  const newIds = new Set((data.files || []).map((file) => file.id));
  const previouslySelected = new Set([...projectFiles.selectedOptions].map((option) => option.value));
  await loadLibraryFiles();
  [...projectFiles.options].forEach((option) => {
    option.selected = previouslySelected.has(option.value) || newIds.has(option.value);
  });
  renderProjectSelectedFiles();
}

async function saveProject(event) {
  event.preventDefault();
  const payload = {
    title: projectTitle.value.trim(),
    subject: projectSubject.value.trim(),
    description: projectDescription.value.trim(),
    library_file_ids: [...projectFiles.selectedOptions].map((option) => option.value),
    compile_requirements: parseRequirements(projectRequirements.value),
  };
  const url = editingProjectId ? `/api/projects/${encodeURIComponent(editingProjectId)}` : "/api/projects";
  const method = editingProjectId ? "PATCH" : "POST";
  const previousEditingId = editingProjectId;
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    window.alert(`保存失败: ${response.status}`);
    return;
  }
  if (previousEditingId) {
    delete projectPreflightPlans[previousEditingId];
  }
  editingProjectId = "";
  projectForm.reset();
  projectRequirements.value = formatRequirements(defaultCompileRequirements());
  await loadProjects();
}

function defaultCompileRequirements() {
  return {
    course_structure: "按学习目标组织为中等粒度章节，避免把例子或短概念单独作为章节。",
    explanation_depth: "适合碎片化阅读，补足必要推导但标记 bridge/inferred 内容。",
    exercise_ratio: "每节包含 1-3 个检查项或练习提示。",
    formula_handling: "保留重要公式为 LaTeX display math，避免公式被 Markdown 表格或列表破坏。",
    image_handling: "保留源图片引用，生成可追溯说明；不确定图片进入待确认列表。",
    code_block_handling: "保留代码块语言与命令，不编造缺失参数或输出。",
    source_grounding: "课程内容必须引用资料库文件和解析块，缺失信息标记待源材料确认。",
  };
}

function formatRequirements(requirements) {
  return Object.entries(requirements || defaultCompileRequirements())
    .map(([key, value]) => `${key}: ${value}`)
    .join("\n");
}

function parseRequirements(value) {
  const parsed = {};
  String(value || "").split(/\r?\n/).forEach((line) => {
    const index = line.indexOf(":");
    if (index <= 0) {
      return;
    }
    const key = line.slice(0, index).trim();
    const text = line.slice(index + 1).trim();
    if (key && text) {
      parsed[key] = text;
    }
  });
  return { ...defaultCompileRequirements(), ...parsed };
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function cssEscape(value) {
  if (typeof CSS !== "undefined" && CSS.escape) {
    return CSS.escape(value);
  }
  return String(value).replace(/"/g, '\\"');
}

function readingKey(course = currentCourse, version = currentVersionId, file = currentLessonFile) {
  if (!course || !version || !file) {
    return "";
  }
  return `${course}/${version}/${file}`;
}

function readJsonStorage(key, fallback) {
  if (typeof localStorage === "undefined") {
    return fallback instanceof Array ? new Set(fallback) : fallback;
  }
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback));
    return fallback instanceof Array ? new Set(parsed) : parsed;
  } catch (_error) {
    return fallback instanceof Array ? new Set(fallback) : fallback;
  }
}

function readTextStorage(key) {
  if (typeof localStorage === "undefined") {
    return "";
  }
  return localStorage.getItem(key) || "";
}

function parseReadingKey(key) {
  const match = String(key || "").match(/^([^/]+)\/([^/]+)\/(.+)$/);
  if (!match) {
    return null;
  }
  return { course: match[1], version: match[2], file: match[3] };
}

function saveReadingPosition() {
  if (!hasDocument || typeof localStorage === "undefined") {
    return;
  }
  const key = readingKey();
  if (!key) {
    return;
  }
  const metrics = readingMetrics();
  readingPositions = withSavedReadingPosition(
    readingPositions,
    key,
    Math.round(window.scrollY || document.documentElement.scrollTop || 0),
    metrics.progress,
    Date.now()
  );
  localStorage.setItem("readingPositions", JSON.stringify(readingPositions));
  localStorage.setItem("lastReadingKey", key);
}

function restoreReadingPosition() {
  if (!hasDocument) {
    return;
  }
  const saved = readingPositions[readingKey()];
  if (!saved) {
    return;
  }
  const maxScroll = readingMetrics().maxScroll;
  const target = Math.max(0, Math.min(Number(saved.scrollY) || 0, maxScroll));
  window.scrollTo(0, target);
}

function updateReadingProgress() {
  if (!progress) {
    return;
  }
  progress.value = readingMetrics().progress;
}

function scheduleReadingProgressUpdate() {
  if (progressFrame) {
    return;
  }
  progressFrame = window.requestAnimationFrame(() => {
    progressFrame = 0;
    updateReadingProgress();
    saveReadingPosition();
  });
}

function readingMetrics() {
  if (!hasDocument) {
    return { progress: 0, maxScroll: 0 };
  }
  const root = document.documentElement;
  const maxScroll = Math.max(0, root.scrollHeight - window.innerHeight);
  const current = Math.max(0, window.scrollY || root.scrollTop || 0);
  const percent = maxScroll ? Math.round((Math.min(current, maxScroll) / maxScroll) * 100) : 100;
  return { progress: percent, maxScroll };
}

function courseProgress(course) {
  const version = course.latest_version || "";
  const total = Math.max(0, Number(course.lesson_count || 0));
  if (!total || !version) {
    return 0;
  }
  const prefix = `${course.id}/${version}/`;
  let read = 0;
  readLessons.forEach((key) => {
    if (String(key).startsWith(prefix)) {
      read += 1;
    }
  });
  return Math.max(0, Math.min(100, Math.round((read / total) * 100)));
}

function formatDate(value) {
  if (!value) {
    return "暂无时间";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function projectStatusLabel(state) {
  return {
    not_started: "未开始",
    queued: "排队中",
    analyzing: "分析中",
    awaiting_confirmation: "待确认",
    compiling: "编译中",
    waiting_review: "待人工审核",
    succeeded: "成功",
    failed: "失败",
  }[state] || "状态未知";
}

function parseStatusLabel(state) {
  return {
    waiting_parse: "等待解析",
    parsing: "解析中",
    parsed: "解析完成",
    parse_failed: "解析失败",
    success: "解析完成",
    warning: "解析警告",
    failed: "解析失败",
    unknown: "状态未知",
  }[state] || state || "状态未知";
}

function withSavedReadingPosition(positions, key, scrollY, progressValue, updatedAt = Date.now()) {
  if (!key) {
    return positions;
  }
  return {
    ...positions,
    [key]: {
      scrollY: Math.max(0, Math.round(Number(scrollY) || 0)),
      progress: Math.max(0, Math.min(100, Math.round(Number(progressValue) || 0))),
      updatedAt,
    },
  };
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let listOpen = "";
  let mathOpen = false;
  let mathClose = "$$";
  let mathLines = [];
  let codeOpen = false;
  let codeLanguage = "";
  let codeLines = [];

  function flushParagraph() {
    if (paragraph.length) {
      html.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    }
  }

  function closeList() {
    if (listOpen) {
      html.push(`</${listOpen}>`);
      listOpen = "";
    }
  }

  function openList(tag) {
    if (listOpen === tag) {
      return;
    }
    closeList();
    html.push(`<${tag}>`);
    listOpen = tag;
  }

  function closeMathBlock() {
    if (!mathOpen) {
      return;
    }
    const body = normalizeLatexBlock(mathLines).map(escapeHtml).join("\n");
    html.push(`<div class="math-block">\\[${body}\\]</div>`);
    mathOpen = false;
    mathClose = "$$";
    mathLines = [];
  }

  function closeCodeBlock() {
    if (!codeOpen) {
      return;
    }
    const lang = codeLanguage ? ` data-language="${escapeAttr(codeLanguage)}"` : "";
    html.push(`<pre class="code-block"${lang}><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    codeOpen = false;
    codeLanguage = "";
    codeLines = [];
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();

    if (codeOpen) {
      if (/^```/.test(trimmed)) {
        closeCodeBlock();
      } else {
        codeLines.push(line);
      }
      continue;
    }

    if (mathOpen) {
      if (trimmed === mathClose) {
        closeMathBlock();
      } else {
        mathLines.push(line);
      }
      continue;
    }

    const codeFence = trimmed.match(/^```([A-Za-z0-9_-]*)\s*$/);
    if (codeFence) {
      flushParagraph();
      closeList();
      codeOpen = true;
      codeLanguage = codeFence[1] || "";
      codeLines = [];
      continue;
    }

    if (trimmed === "$$" || trimmed === "\\[") {
      flushParagraph();
      closeList();
      mathOpen = true;
      mathClose = trimmed === "$$" ? "$$" : "\\]";
      mathLines = [];
      continue;
    }

    if (isStandaloneLatexEnvironmentStart(trimmed)) {
      flushParagraph();
      closeList();
      const block = collectLatexEnvironment(lines, index);
      index = block.endIndex;
      html.push(`<div class="math-block">\\[${normalizeLatexBlock(block.lines).map(escapeHtml).join("\n")}\\]</div>`);
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      closeList();
      continue;
    }

    if (isGfmTableStart(lines, index)) {
      flushParagraph();
      closeList();
      const table = collectGfmTable(lines, index);
      html.push(renderTable(table.rows, table.alignments));
      index = table.endIndex;
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = Math.min(heading[1].length, 3);
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const image = trimmed.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (image) {
      flushParagraph();
      closeList();
      const alt = image[1].trim() || "Course image";
      const src = sanitizeImageUrl(image[2].trim());
      if (src) {
        html.push(
          `<figure class="lesson-figure"><img src="${escapeAttr(src)}" alt="${escapeAttr(alt)}" loading="lazy">` +
          `<figcaption>${inlineMarkdown(alt)}</figcaption></figure>`
        );
      }
      continue;
    }

    const checklist = trimmed.match(/^- \[([ xX])\]\s+(.+)$/);
    if (checklist) {
      flushParagraph();
      closeList();
      const checked = checklist[1].toLowerCase() === "x" ? " checked" : "";
      html.push(`<label class="check-item"><input type="checkbox"${checked}> ${inlineMarkdown(checklist[2])}</label>`);
      continue;
    }

    const source = trimmed.match(/^- `(.*?)` \/ `(.*?)`:\s*(.*)$/);
    if (source) {
      flushParagraph();
      closeList();
      html.push(
        `<p class="source-line"><strong>Source</strong>: <code>${escapeHtml(source[1])}</code> ` +
        `<code>${escapeHtml(source[2])}</code><br>${inlineMarkdown(source[3])}</p>`
      );
      continue;
    }

    const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      openList("ol");
      html.push(`<li>${inlineMarkdown(ordered[1])}</li>`);
      continue;
    }

    const bullet = trimmed.match(/^[-*+]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      openList("ul");
      html.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      continue;
    }

    if (trimmed.startsWith(">")) {
      flushParagraph();
      closeList();
      html.push(`<blockquote>${inlineMarkdown(trimmed.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }

    closeList();
    paragraph.push(inlineMarkdown(trimmed));
  }

  flushParagraph();
  closeList();
  closeMathBlock();
  closeCodeBlock();
  return html.join("");
}

function preprocessMarkdown(markdown) {
  return String(markdown || "");
}

function isStandaloneLatexEnvironmentStart(trimmed) {
  return /^\\begin\{(cases|[bpvVB]?matrix|aligned|align|array|split|gathered)\}/.test(stripMarkdownListMarker(trimmed));
}

function collectLatexEnvironment(lines, startIndex) {
  const collected = [];
  let depth = 0;
  let endIndex = startIndex;
  let sawBegin = false;
  for (let index = startIndex; index < lines.length; index += 1) {
    const cleaned = stripMarkdownListMarker(lines[index]);
    collected.push(cleaned);
    const beginCount = (cleaned.match(/\\begin\{/g) || []).length;
    const endCount = (cleaned.match(/\\end\{/g) || []).length;
    sawBegin = sawBegin || beginCount > 0;
    depth += beginCount - endCount;
    endIndex = index;
    if (sawBegin && depth <= 0) {
      break;
    }
  }
  return { lines: collected, endIndex };
}

function normalizeLatexBlock(lines) {
  const cleaned = lines
    .map((line) => stripMarkdownListMarker(line).trimEnd())
    .filter((line, index, all) => line.trim() || index > 0 && index < all.length - 1);
  const envStack = [];
  return cleaned.map((line, index) => {
    const trimmed = line.trim();
    const begin = trimmed.match(/\\begin\{([^}]+)\}/);
    const end = trimmed.match(/\\end\{([^}]+)\}/);
    if (begin && !end) {
      envStack.push(begin[1]);
      return line;
    }
    const inAlignedEnv = envStack.some((env) => /^(cases|[bpvVB]?matrix|aligned|align|array|split|gathered)$/.test(env));
    const next = cleaned[index + 1] ? cleaned[index + 1].trim() : "";
    const shouldAddBreak = (
      inAlignedEnv &&
      trimmed &&
      !trimmed.endsWith("\\\\") &&
      !trimmed.startsWith("\\end") &&
      !next.startsWith("\\end") &&
      (trimmed.includes("&") || /[=<>]/.test(trimmed))
    );
    if (end) {
      const position = envStack.lastIndexOf(end[1]);
      if (position >= 0) {
        envStack.splice(position, 1);
      }
    }
    return shouldAddBreak ? `${line} \\\\` : line;
  });
}

function stripMarkdownListMarker(line) {
  return String(line).replace(/^(\s*)(?:[-*+]|\d+[.)])\s+(?=(?:\\|&|[A-Za-z0-9{}()[\]^_+\-=]))/, "$1");
}

function isGfmTableStart(lines, index) {
  if (index + 1 >= lines.length) {
    return false;
  }
  const header = lines[index].trim();
  const separator = lines[index + 1].trim();
  if (looksLikeLatexLine(header) || looksLikeLatexLine(separator)) {
    return false;
  }
  const headerCells = splitTableRow(header);
  const separatorCells = splitTableRow(separator);
  return (
    isTableRow(header) &&
    isTableSeparator(separator) &&
    headerCells.length >= 2 &&
    separatorCells.length >= 2 &&
    headerCells.some((cell) => cell.trim())
  );
}

function collectGfmTable(lines, startIndex) {
  const header = splitTableRow(lines[startIndex]);
  const alignments = splitTableRow(lines[startIndex + 1]).map(tableAlignment);
  const rows = [header];
  let endIndex = startIndex + 1;
  for (let index = startIndex + 2; index < lines.length; index += 1) {
    if (!isTableRow(lines[index].trim()) || isTableSeparator(lines[index].trim())) {
      break;
    }
    rows.push(splitTableRow(lines[index]));
    endIndex = index;
  }
  return { rows, alignments, endIndex };
}

function isTableRow(line) {
  return line.includes("|") && !/^```/.test(line) && !looksLikeLatexLine(line);
}

function isTableSeparator(line) {
  if (!isTableRow(line)) {
    return false;
  }
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function looksLikeLatexLine(line) {
  return /\\(?:left|right|begin|end|frac|sum|int|prod|sqrt|mu|lambda|vdots|ddots|cdots|boxed|begin\{array\})/.test(String(line));
}

function splitTableRow(line) {
  let value = line.trim();
  if (value.startsWith("|")) {
    value = value.slice(1);
  }
  if (value.endsWith("|")) {
    value = value.slice(0, -1);
  }
  const cells = [];
  let current = "";
  let escaped = false;
  for (const char of value) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      current += char;
      continue;
    }
    if (char === "|") {
      cells.push(current.replace(/\\\|/g, "|").trim());
      current = "";
      continue;
    }
    current += char;
  }
  cells.push(current.replace(/\\\|/g, "|").trim());
  return cells;
}

function tableAlignment(separatorCell) {
  const value = separatorCell.trim();
  if (value.startsWith(":") && value.endsWith(":")) {
    return "center";
  }
  if (value.endsWith(":")) {
    return "right";
  }
  return "left";
}

function renderTable(rows, alignments) {
  const columnCount = Math.max(...rows.map((row) => row.length));
  const normalized = rows.map((row) => Array.from({ length: columnCount }, (_item, index) => row[index] || ""));
  const header = normalized[0] || [];
  const body = normalized.slice(1);
  const headHtml = header.map((cell, index) => renderTableCell("th", cell, alignments[index])).join("");
  const bodyHtml = body
    .map((row) => `<tr>${row.map((cell, index) => renderTableCell("td", cell, alignments[index])).join("")}</tr>`)
    .join("");
  return `<div class="table-scroll"><table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

function renderTableCell(tag, value, alignment) {
  const align = alignment && alignment !== "left" ? ` style="text-align: ${alignment}"` : "";
  return `<${tag}${align}>${inlineMarkdown(value)}</${tag}>`;
}

function inlineMarkdown(value) {
  const parts = String(value).split(/(\$[^$\n]+\$|\\\([^\n]+?\\\))/g);
  return parts.map((part) => {
    if (/^\$[^$\n]+\$$/.test(part) || /^\\\([^\n]+?\\\)$/.test(part)) {
      return escapeHtml(part);
    }
    return escapeHtml(part)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function decodeHtmlEntities(value) {
  return String(value).replace(/&(amp|lt|gt|quot|#39);/g, (entity, name) => ({
    amp: "&",
    lt: "<",
    gt: ">",
    quot: "\"",
    "#39": "'",
  }[name] || entity));
}

function sanitizeImageUrl(value) {
  if (!value) {
    return "";
  }
  if (value.startsWith("/api/assets/")) {
    return value;
  }
  if (/^https?:\/\//.test(value)) {
    return value;
  }
  return "";
}

async function typesetMath() {
  if (!window.MathJax || !window.MathJax.typesetPromise) {
    return;
  }
  try {
    await window.MathJax.typesetPromise([lessonView]);
  } catch (error) {
    console.error("MathJax typeset failed", error);
  }
}

if (hasDocument) {
  versionSelect.addEventListener("change", renderLessons);
  refreshCoursesButton.addEventListener("click", loadCourses);
  libraryFileInput.addEventListener("change", () => {
    uploadLibraryFiles(libraryFileInput.files || []);
    libraryFileInput.value = "";
  });
  projectFiles.addEventListener("change", renderProjectSelectedFiles);
  projectUploadInput.addEventListener("change", () => {
    uploadProjectFiles(projectUploadInput.files || []);
    projectUploadInput.value = "";
  });
  projectForm.addEventListener("submit", saveProject);
  backToCoursesButton.addEventListener("click", showDashboard);
  backFromManageButton.addEventListener("click", showDashboard);
  window.addEventListener("scroll", scheduleReadingProgressUpdate, { passive: true });
  window.addEventListener("beforeunload", saveReadingPosition);
  loadCourses();
}

if (typeof module !== "undefined") {
  module.exports = {
    collectGfmTable,
    defaultCompileRequirements,
    formatRequirements,
    normalizeLatexBlock,
    parseRequirements,
    parseStatusLabel,
    parseProgressValue,
    parseStageLabel,
    projectCompileBlockReason,
    preprocessMarkdown,
    parseReadingKey,
    projectStatusLabel,
    readingKey,
    renderAnalysisReport,
    renderLibraryFileItem,
    renderJobReviewPanel,
    renderJobIntermediatePanel,
    renderJobNodeDetail,
    renderProjectJobStatus,
    renderMarkdown,
    stripMarkdownListMarker,
    withSavedReadingPosition,
  };
}
