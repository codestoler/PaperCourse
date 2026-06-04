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
let courseProjects = [];
let projectJobs = {};
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

async function loadCourses() {
  const [data] = await Promise.all([fetch("/api/courses").then((response) => response.json()), loadLibraryFiles(), loadProjects()]);
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

async function loadLibraryFiles() {
  if (!libraryFileList) {
    return;
  }
  const data = await fetch("/api/library/files").then((response) => response.json());
  libraryFiles = data.files || [];
  renderLibraryFiles();
  renderProjectFileOptions();
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
  libraryFileList.innerHTML = libraryFiles.map((file) => `
    <article class="resource-item">
      <div>
        <strong>${escapeHtml(file.filename || file.id)}</strong>
        <span>${escapeHtml(file.id || "")}</span>
      </div>
      <div class="resource-meta">
        <span class="status-pill status-${escapeAttr(file.analysis_status || "unknown")}">${escapeHtml(file.analysis_status || "unknown")}</span>
        <span>${formatBytes(file.size || 0)}</span>
      </div>
      <div class="resource-actions">
        <button type="button" data-library-report="${escapeAttr(file.id)}">查看分析报告</button>
      </div>
    </article>
  `).join("");
  libraryFileList.querySelectorAll("[data-library-report]").forEach((button) => {
    button.addEventListener("click", () => showAnalysisReport(button.dataset.libraryReport));
  });
}

async function showAnalysisReport(fileId) {
  const report = await fetch(`/api/library/files/${encodeURIComponent(fileId)}/analysis`).then((response) => response.json());
  const target = libraryFileList.querySelector(`[data-library-report="${cssEscape(fileId)}"]`)?.closest(".resource-item");
  if (!target) {
    return;
  }
  const existing = target.querySelector(".analysis-report");
  if (existing) {
    existing.remove();
    return;
  }
  const panel = document.createElement("div");
  panel.className = "analysis-report";
  panel.innerHTML = renderAnalysisReport(report);
  target.appendChild(panel);
}

function renderAnalysisReport(report) {
  const chapters = report.chapter_structure || [];
  const points = report.knowledge_points || [];
  const problems = report.potential_problems || [];
  const pipeline = report.pipeline || [];
  return `
    <h3>资料分析报告</h3>
    <div class="analysis-grid">
      <div><span>解析状态</span><strong>${escapeHtml(report.status || "unknown")}</strong></div>
      <div><span>章节</span><strong>${chapters.length}</strong></div>
      <div><span>知识点</span><strong>${points.length}</strong></div>
      <div><span>风险</span><strong>${problems.length}</strong></div>
    </div>
    <h4>处理流程</h4>
    <ul>${pipeline.map((item) => `<li>${escapeHtml(item.step)}: ${escapeHtml(item.status)} (${escapeHtml(item.detail || item.count || "")})</li>`).join("")}</ul>
    <h4>章节结构</h4>
    ${chapters.length ? `<ol>${chapters.slice(0, 12).map((item) => `<li>${escapeHtml(item.title)} <span>line ${Number(item.line || 0)}</span></li>`).join("")}</ol>` : `<p class="muted">未识别出章节。</p>`}
    <h4>主要知识点</h4>
    ${points.length ? `<ul>${points.slice(0, 12).map((item) => `<li>${escapeHtml(item.name)}</li>`).join("")}</ul>` : `<p class="muted">未识别出稳定知识点。</p>`}
    <h4>潜在问题</h4>
    ${problems.length ? `<ul>${problems.map((item) => `<li><strong>${escapeHtml(item.severity || "")}</strong> ${escapeHtml(item.message || item.type)}</li>`).join("")}</ul>` : `<p class="muted">未发现明显风险。</p>`}
  `;
}

function renderProjectFileOptions() {
  if (!projectFiles) {
    return;
  }
  const selected = new Set([...projectFiles.selectedOptions].map((option) => option.value));
  projectFiles.innerHTML = libraryFiles.map((file) => (
    `<option value="${escapeAttr(file.id)}"${selected.has(file.id) ? " selected" : ""}>${escapeHtml(file.filename || file.id)}</option>`
  )).join("");
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
        ${renderProjectJobStatus(project)}
      </div>
      <div class="resource-actions">
        <button type="button" data-project-edit="${escapeAttr(project.id)}">编辑配置</button>
        <button type="button" data-project-compile="${escapeAttr(project.id)}"${projectHasRunningJob(project.id) ? " disabled" : ""}>开始编译</button>
      </div>
    </article>
  `).join("");
  projectList.querySelectorAll("[data-project-edit]").forEach((button) => {
    button.addEventListener("click", () => editProject(button.dataset.projectEdit));
  });
  projectList.querySelectorAll("[data-project-compile]").forEach((button) => {
    button.addEventListener("click", () => startProjectCompile(button.dataset.projectCompile));
  });
  scheduleProjectJobPolling();
}

function renderProjectJobStatus(project) {
  const latest = (projectJobs[project.id] || [])[0];
  if (!latest) {
    return `<span class="project-job muted">未编译</span>`;
  }
  const label = {
    queued: "等待编译",
    running: "编译中",
    done: "编译完成",
    failed: "编译失败",
    blocked: "需要预处理",
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

function projectHasRunningJob(projectId) {
  return (projectJobs[projectId] || []).some((job) => ["queued", "running"].includes(job.state));
}

async function startProjectCompile(projectId) {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/compile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!response.ok) {
    window.alert(`启动编译失败: ${response.status}`);
    return;
  }
  await loadProjects();
}

function scheduleProjectJobPolling() {
  if (projectJobPollTimer) {
    window.clearTimeout(projectJobPollTimer);
    projectJobPollTimer = 0;
  }
  const hasRunning = Object.values(projectJobs).some((jobs) => jobs.some((job) => ["queued", "running"].includes(job.state)));
  if (!hasRunning) {
    return;
  }
  projectJobPollTimer = window.setTimeout(async () => {
    projectJobPollTimer = 0;
    await loadProjects();
    await loadCourses();
  }, 3000);
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
      row.querySelector("strong").textContent = "分析完成";
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
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    window.alert(`保存失败: ${response.status}`);
    return;
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
    preprocessMarkdown,
    parseReadingKey,
    readingKey,
    renderMarkdown,
    stripMarkdownListMarker,
    withSavedReadingPosition,
  };
}
