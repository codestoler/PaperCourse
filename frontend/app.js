const courseList = document.querySelector("#courseList");
const versionSelect = document.querySelector("#versionSelect");
const lessonList = document.querySelector("#lessonList");
const lessonView = document.querySelector("#lessonView");
const progress = document.querySelector("#progress");

let currentCourse = null;
let versions = [];
let readLessons = new Set(JSON.parse(localStorage.getItem("readLessons") || "[]"));

async function loadCourses() {
  const data = await fetch("/api/courses").then((response) => response.json());
  courseList.innerHTML = "";
  const courses = data.courses.sort((a, b) => {
    if (a.id === "numerical-analysis") return -1;
    if (b.id === "numerical-analysis") return 1;
    return a.id.localeCompare(b.id);
  });
  courses.forEach((course, index) => {
    const button = document.createElement("button");
    button.textContent = course.meta.course_id || course.id;
    button.addEventListener("click", () => selectCourse(course.id, button));
    courseList.appendChild(button);
    if (index === 0) {
      button.click();
    }
  });
  if (!data.courses.length) {
    lessonView.textContent = "No compiled courses found.";
  }
}

async function selectCourse(courseId, button) {
  currentCourse = courseId;
  document.querySelectorAll(".course-list button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  const data = await fetch(`/api/courses/${courseId}/versions`).then((response) => response.json());
  versions = data.versions;
  versionSelect.innerHTML = versions.map((version) => `<option value="${version.id}">${version.id}</option>`).join("");
  if (versions.length) {
    versionSelect.value = versions[versions.length - 1].id;
  }
  renderLessons();
}

function renderLessons() {
  const version = versions.find((item) => item.id === versionSelect.value) || versions[0];
  lessonList.innerHTML = "";
  if (!version) {
    lessonView.textContent = "No versions exported.";
    return;
  }
  version.lessons.forEach((lesson, index) => {
    const button = document.createElement("button");
    button.textContent = lesson.title;
    button.addEventListener("click", () => loadLesson(version.id, lesson.file, button));
    lessonList.appendChild(button);
    if (index === 0) {
      button.click();
    }
  });
  updateProgress(version);
}

async function loadLesson(versionId, file, button) {
  document.querySelectorAll(".lesson-list button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  const key = `${currentCourse}/${versionId}/${file}`;
  readLessons.add(key);
  localStorage.setItem("readLessons", JSON.stringify([...readLessons]));
  const lesson = await fetch(`/api/courses/${currentCourse}/versions/${versionId}/${file}`).then((response) => response.json());
  lessonView.innerHTML = renderMarkdown(lesson.markdown);
  await typesetMath();
  updateProgress(versions.find((item) => item.id === versionId));
}

function updateProgress(version) {
  if (!version || !version.lessons.length) {
    progress.value = 0;
    return;
  }
  const readCount = version.lessons.filter((lesson) => readLessons.has(`${currentCourse}/${version.id}/${lesson.file}`)).length;
  progress.value = Math.round((readCount / version.lessons.length) * 100);
}

function renderMarkdown(markdown) {
  const html = [];
  let paragraph = [];
  let listOpen = false;
  let mathOpen = false;
  let mathLines = [];

  function flushParagraph() {
    if (paragraph.length) {
      html.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    }
  }

  function closeList() {
    if (listOpen) {
      html.push("</ul>");
      listOpen = false;
    }
  }

  function closeMathBlock() {
    if (mathOpen) {
      html.push(`<div class="math-block">\\[${mathLines.map(escapeHtml).join("\n")}\\]</div>`);
      mathOpen = false;
      mathLines = [];
    }
  }

  markdown.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();

    if (mathOpen && trimmed !== "$$" && !startsNewMarkdownBlock(trimmed)) {
      mathLines.push(line);
      return;
    }

    if (mathOpen && startsNewMarkdownBlock(trimmed)) {
      closeMathBlock();
    }

    if (!trimmed) {
      flushParagraph();
      closeList();
      return;
    }

    if (trimmed === "$$") {
      flushParagraph();
      closeList();
      if (mathOpen) {
        closeMathBlock();
      } else {
        mathOpen = true;
        mathLines = [];
      }
      return;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = Math.min(heading[1].length, 3);
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      return;
    }

    const checklist = trimmed.match(/^- \[([ xX])\]\s+(.+)$/);
    if (checklist) {
      flushParagraph();
      closeList();
      const checked = checklist[1].toLowerCase() === "x" ? " checked" : "";
      html.push(`<label class="check-item"><input type="checkbox"${checked}> ${inlineMarkdown(checklist[2])}</label>`);
      return;
    }

    const source = trimmed.match(/^- `(.*?)` \/ `(.*?)`:\s*(.*)$/);
    if (source) {
      flushParagraph();
      closeList();
      html.push(
        `<p class="source-line"><strong>Source</strong>: <code>${escapeHtml(source[1])}</code> ` +
        `<code>${escapeHtml(source[2])}</code><br>${inlineMarkdown(source[3])}</p>`
      );
      return;
    }

    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      if (!listOpen) {
        html.push("<ul>");
        listOpen = true;
      }
      html.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      return;
    }

    if (trimmed.startsWith(">")) {
      flushParagraph();
      closeList();
      html.push(`<blockquote>${inlineMarkdown(trimmed.replace(/^>\s?/, ""))}</blockquote>`);
      return;
    }

    closeList();
    paragraph.push(inlineMarkdown(trimmed));
  });

  flushParagraph();
  closeList();
  closeMathBlock();
  return html.join("");
}

function startsNewMarkdownBlock(trimmed) {
  return (
    !trimmed ||
    trimmed === "$$" ||
    /^(#{1,6})\s+/.test(trimmed) ||
    /^- \[([ xX])\]\s+/.test(trimmed) ||
    /^- `(.*?)` \/ `(.*?)`:/.test(trimmed) ||
    /^[-*]\s+/.test(trimmed) ||
    /^>\s?/.test(trimmed) ||
    /^Source note:/.test(trimmed)
  );
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[char]));
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

versionSelect.addEventListener("change", renderLessons);
loadCourses();
