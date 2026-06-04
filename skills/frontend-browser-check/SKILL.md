---
name: frontend-browser-check
description: Browser-driven frontend recognition and validation for this repository. Use when Codex needs to inspect the local UI, verify rendered frontend behavior, run Playwright smoke checks, validate upload/progress/report/project flows, or confirm that a frontend change works in a real browser rather than only by unit tests.
---

# Frontend Browser Check

## Purpose

Use a real Chromium browser to verify frontend behavior for this repository. Prefer this skill after changing `frontend/`, `backend/server.py` API routes consumed by the UI, or any course/library/project workflow that must be visually or interactively confirmed.

## Workflow

1. Start the local server from the repository root:

   ```bash
   .venv/bin/python backend/server.py --host 127.0.0.1 --port 8766
   ```

   If `8766` is unavailable, choose another free port and pass that URL to the script.

2. Run the smoke script:

   ```bash
   .venv/bin/python skills/frontend-browser-check/scripts/browser_smoke.py --base-url http://127.0.0.1:8766
   ```

3. For the full资料库上传、资料分析、课程项目配置 flow, run:

   ```bash
   .venv/bin/python skills/frontend-browser-check/scripts/browser_smoke.py \
     --base-url http://127.0.0.1:8766 \
     --exercise-library-project-flow
   ```

4. Stop the local server after the check. Do not leave browser or server sessions running.

## What To Verify

- Page loads without fatal console errors.
- `#libraryFileInput`, `#libraryFileList`, `#projectForm`, and `#projectRequirements` exist.
- Default compile requirements are visible and include course structure and formula handling.
- Upload flow shows a file in the library list and exposes success/failure status.
- Analysis report opens and includes parsing status, chapter structure, knowledge points, and risk sections.
- Project creation persists title, description, subject, selected library references, and edited compile requirements.
- `/api/projects/<id>/compile-context` returns saved requirements and resolved library file references.

## Browser Location

The project may use Playwright browsers installed under `.playwright-browsers/`. The script searches these locations before falling back to Playwright defaults:

- `.playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell`
- `.playwright-browsers/chromium-*/chrome-linux64/chrome`

If no browser exists, ask the user to run:

```bash
.venv/bin/playwright install chromium
```

If launch fails because system libraries are missing, ask for:

```bash
sudo .venv/bin/playwright install-deps chromium
```

## Cleanup

The full-flow smoke creates temporary library and project records, then deletes them. If a run is interrupted, remove records whose filename or project title starts with `browser-smoke-` from `course-vault/library/` and `course-vault/projects/`.
