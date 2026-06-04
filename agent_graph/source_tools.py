"""Unified source evidence and revision helpers for course compilation."""

from __future__ import annotations

import copy
import difflib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import course_dir, read_json, write_json


def stable_source_id(ref: dict[str, Any]) -> str:
    """Return the stable source id used for citations and evidence lookup."""

    return str(ref.get("source_id") or ref.get("chunk_id") or ref.get("block_id") or ref.get("id") or "").strip()


class SourceLocator:
    """Search, locate, and verify evidence from parsed course sources."""

    def __init__(self, parsed_chunks: list[dict[str, Any]], image_understanding: dict[str, Any] | None = None) -> None:
        self.chunks = list(parsed_chunks)
        self.chunk_by_id = {str(chunk.get("id", "")): chunk for chunk in self.chunks if chunk.get("id")}
        self.chunk_order = {str(chunk.get("id", "")): index for index, chunk in enumerate(self.chunks)}
        images = (image_understanding or {}).get("images", []) if isinstance(image_understanding, dict) else []
        self.images = [image for image in images if isinstance(image, dict)]
        self.image_by_id = {str(image.get("id", "")): image for image in self.images if image.get("id")}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "SourceLocator":
        return cls(state.get("parsed_chunks", []), state.get("image_understanding", {}))

    def search(self, query: str, *, kinds: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Search text, formula, table, and image evidence with stable source ids."""

        allowed = set(kinds or ["text", "formula", "table", "image"])
        terms = _query_terms(query)
        results: list[dict[str, Any]] = []
        if allowed & {"text", "formula", "table"}:
            for chunk in self.chunks:
                text = f"{chunk.get('title', '')}\n{chunk.get('content', '')}"
                base_score = _score_text(text, terms)
                if "text" in allowed and base_score:
                    results.append(self._text_evidence(chunk, score=base_score, excerpt=_best_excerpt(text, terms)))
                if "formula" in allowed:
                    for formula in _extract_formula_blocks(str(chunk.get("content", ""))):
                        score = _score_text(formula, terms) or (1 if _formula_query(query) else 0)
                        if score:
                            record = self._text_evidence(chunk, evidence_type="formula", score=score, excerpt=formula)
                            results.append(record)
                if "table" in allowed:
                    for table in _extract_table_blocks(str(chunk.get("content", ""))):
                        score = _score_text(table, terms) or (1 if "|" in query else 0)
                        if score:
                            record = self._text_evidence(chunk, evidence_type="table", score=score, excerpt=table)
                            results.append(record)
        if "image" in allowed:
            results.extend(self.find_images(query, limit=limit))
        results.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("source_id", "")), str(item.get("evidence_id", ""))))
        return results[: max(0, limit)]

    def locate(self, generated_content: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Locate likely source evidence for generated text."""

        phrases = _salient_phrases(generated_content)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for phrase in phrases:
            for evidence in self.search(phrase, limit=limit):
                key = str(evidence.get("evidence_id", ""))
                if key in seen:
                    continue
                seen.add(key)
                evidence = dict(evidence)
                evidence["located_from"] = phrase
                results.append(evidence)
                if len(results) >= limit:
                    return results
        return results

    def get_context(self, source_ids: list[str], *, before: int = 1, after: int = 1) -> dict[str, Any]:
        """Return stable evidence records plus neighboring source context."""

        normalized_ids = _dedupe_keep_order([str(source_id) for source_id in source_ids if str(source_id).strip()])
        records: list[dict[str, Any]] = []
        context: list[dict[str, Any]] = []
        context_seen: set[str] = set()
        for source_id in normalized_ids:
            chunk = self.chunk_by_id.get(source_id)
            if not chunk:
                continue
            records.append(self._text_evidence(chunk, score=1))
            index = self.chunk_order.get(source_id, 0)
            start = max(0, index - before)
            end = min(len(self.chunks), index + after + 1)
            for neighbor in self.chunks[start:end]:
                neighbor_id = str(neighbor.get("id", ""))
                if not neighbor_id or neighbor_id in context_seen:
                    continue
                context_seen.add(neighbor_id)
                context.append(self._text_evidence(neighbor, score=1, excerpt=_short_text(str(neighbor.get("content", "")), 900)))
        images = self.find_images(source_ids=normalized_ids, limit=24)
        return {
            "source_ids": normalized_ids,
            "evidence": records,
            "context": context,
            "images": images,
            "summary": {"evidence_count": len(records), "context_count": len(context), "image_count": len(images)},
        }

    def find_images(self, query: str = "", *, source_ids: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Find image/formula/table visual evidence by query or source ids."""

        allowed_sources = set(source_ids or [])
        terms = _query_terms(query)
        results: list[dict[str, Any]] = []
        for image in self.images:
            source_id = str(image.get("source_chunk_id") or "")
            if allowed_sources and source_id not in allowed_sources:
                continue
            text = "\n".join(
                str(image.get(key, ""))
                for key in ("id", "caption", "summary", "content_summary", "image_type", "suggested_insert_position")
            )
            score = _score_text(text, terms) if terms else 1
            if not score:
                continue
            chunk = self.chunk_by_id.get(source_id, {})
            results.append(
                {
                    "evidence_id": f"image:{image.get('id', '')}",
                    "type": "image",
                    "source_id": source_id,
                    "chunk_id": source_id,
                    "image_id": str(image.get("id", "")),
                    "source_file": str(chunk.get("source_file") or chunk.get("source") or image.get("source_file", "")),
                    "source_page": image.get("page_idx", chunk.get("page", chunk.get("page_idx"))),
                    "block_id": str(chunk.get("block_id") or source_id),
                    "bbox": image.get("bbox", chunk.get("bbox", [])) or [],
                    "asset_url": str(image.get("asset_url", "")),
                    "caption": str(image.get("caption", "")),
                    "summary": str(image.get("summary") or image.get("content_summary") or ""),
                    "formula_recognition": image.get("formula_recognition", {}),
                    "score": score,
                }
            )
        results.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("image_id", ""))))
        return results[: max(0, limit)]

    def verify_citations(self, citations: list[dict[str, Any]]) -> dict[str, Any]:
        """Verify lesson citation records against parsed chunks."""

        failures: list[dict[str, Any]] = []
        for citation in citations:
            source_id = str(citation.get("source_id") or "").strip()
            lookup_id = stable_source_id(citation)
            if not source_id:
                failures.append({"type": "citation_source_id_missing", "source": citation, "chunk": self.chunk_by_id.get(lookup_id, {})})
            chunk = self.chunk_by_id.get(lookup_id)
            if not chunk:
                failures.append({"type": "source_chunk_missing", "source": citation, "chunk": {}})
                continue
            block_id = str(citation.get("block_id") or "")
            if not block_id:
                failures.append({"type": "block_id_missing", "source": citation, "chunk": chunk})
            elif block_id not in {str(chunk.get("block_id") or ""), str(chunk.get("id") or "")}:
                failures.append({"type": "block_id_untraceable", "source": citation, "chunk": chunk})
            expected_page = chunk.get("page", chunk.get("page_idx"))
            citation_page = citation.get("source_page", citation.get("page"))
            if expected_page is not None and citation_page is None:
                failures.append({"type": "source_page_missing", "source": citation, "chunk": chunk})
            elif expected_page is not None and citation_page is not None and str(citation_page) != str(expected_page):
                failures.append({"type": "source_page_untraceable", "source": citation, "chunk": chunk})
            if chunk.get("bbox") and not citation.get("bbox"):
                failures.append({"type": "bbox_missing", "source": citation, "chunk": chunk})
            quote = str(citation.get("quote", "")).strip()
            if not quote:
                failures.append({"type": "source_quote_missing", "source": citation, "chunk": chunk})
            elif quote not in str(chunk.get("content", "")):
                failures.append({"type": "source_quote_untraceable", "source": citation, "chunk": chunk})
        return {"ok": not failures, "failures": failures}

    def verify_images(self, images: list[dict[str, Any]]) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        for image in images:
            image_id = str(image.get("id") or image.get("image_id") or "")
            source_id = str(image.get("source_chunk_id") or image.get("source_id") or "")
            known_image = self.image_by_id.get(image_id, {})
            if not image_id:
                failures.append({"type": "image_id_missing", "image": image, "known_image": known_image})
            elif self.image_by_id and image_id not in self.image_by_id:
                failures.append({"type": "image_id_untraceable", "image": image, "known_image": known_image})
            if source_id and source_id not in self.chunk_by_id:
                failures.append({"type": "image_source_chunk_missing", "image": image, "known_image": known_image})
            if image.get("bbox") in (None, [], "") and (not self.image_by_id or known_image.get("bbox")):
                failures.append({"type": "image_bbox_missing", "image": image, "known_image": known_image})
        return {"ok": not failures, "failures": failures}

    def lesson_evidence(self, lessons: list[dict[str, Any]]) -> dict[str, Any]:
        records: dict[str, Any] = {}
        for lesson in lessons:
            lesson_id = str(lesson.get("id", ""))
            source_ids = _dedupe_keep_order([stable_source_id(source) for source in lesson.get("sources", [])])
            records[lesson_id] = {
                "lesson_id": lesson_id,
                "title": str(lesson.get("title", "")),
                **self.get_context(source_ids, before=1, after=1),
            }
        return {
            "lessons": records,
            "summary": {"lesson_count": len(records), "source_id_count": sum(len(item["source_ids"]) for item in records.values())},
        }

    def _text_evidence(
        self,
        chunk: dict[str, Any],
        *,
        evidence_type: str = "text",
        score: int = 0,
        excerpt: str = "",
    ) -> dict[str, Any]:
        source_id = str(chunk.get("id", ""))
        content = str(chunk.get("content", ""))
        return {
            "evidence_id": f"{evidence_type}:{source_id}",
            "type": evidence_type,
            "source_id": source_id,
            "chunk_id": source_id,
            "source_file": str(chunk.get("source_file") or chunk.get("source", "")),
            "source_page": chunk.get("page", chunk.get("page_idx")),
            "block_id": str(chunk.get("block_id") or source_id),
            "bbox": chunk.get("bbox", []) or [],
            "start_line": chunk.get("start_line"),
            "end_line": chunk.get("end_line"),
            "title": str(chunk.get("title", "")),
            "excerpt": excerpt or _short_text(content, 900),
            "content": content,
            "score": score,
        }


class SourceRevisionTool:
    """Patch-based revision helper for lessons, plans, evidence, images, and citations."""

    def __init__(self, vault_root: str | Path | None = None, course_id: str | None = None) -> None:
        self.vault_root = Path(vault_root) if vault_root is not None else None
        self.course_id = course_id

    def propose_patch(
        self,
        *,
        target: dict[str, Any],
        reason: str,
        operations: list[dict[str, Any]],
        evidence: list[dict[str, Any]] | None = None,
        patch_id: str | None = None,
    ) -> dict[str, Any]:
        patch = {
            "id": patch_id or self._next_patch_id(),
            "status": "proposed",
            "target": target,
            "reason": reason,
            "evidence": evidence or [],
            "operations": operations,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_patch_record(patch)
        return patch

    def propose_lesson_body_patch(
        self,
        state: dict[str, Any],
        lesson_id: str,
        new_body: str,
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lesson = _lesson_by_id(state.get("lessons", []), lesson_id)
        before = str((lesson or {}).get("body", ""))
        return self.propose_patch(
            target={"type": "lesson", "lesson_id": lesson_id, "field": "body"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_lesson_field",
                    "lesson_id": lesson_id,
                    "field": "body",
                    "before": before,
                    "after": new_body,
                    "diff": _unified_diff(before, new_body, fromfile=f"{lesson_id}:before", tofile=f"{lesson_id}:after"),
                }
            ],
        )

    def propose_split_lesson_patch(
        self,
        state: dict[str, Any],
        lesson_id: str,
        replacement_lessons: list[dict[str, Any]],
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lesson = _lesson_by_id(state.get("lessons", []), lesson_id)
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {lesson_id}")
        return self.propose_patch(
            target={"type": "lesson", "lesson_id": lesson_id, "action": "split_lesson"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_lessons",
                    "target_lesson_ids": [lesson_id],
                    "before": [copy.deepcopy(lesson)],
                    "after": copy.deepcopy(replacement_lessons),
                    "diff": _unified_diff(
                        _lessons_diff_text([lesson]),
                        _lessons_diff_text(replacement_lessons),
                        fromfile="split:before",
                        tofile="split:after",
                    ),
                }
            ],
        )

    def propose_merge_lessons_patch(
        self,
        state: dict[str, Any],
        lesson_ids: list[str],
        merged_lesson: dict[str, Any],
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        selected = [lesson for lesson in state.get("lessons", []) if str(lesson.get("id", "")) in set(lesson_ids)]
        if len(selected) != len(set(lesson_ids)):
            missing = sorted(set(lesson_ids) - {str(lesson.get("id", "")) for lesson in selected})
            raise ValueError(f"Unknown lesson ids: {missing}")
        return self.propose_patch(
            target={"type": "lesson", "lesson_ids": lesson_ids, "action": "merge_lessons"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_lessons",
                    "target_lesson_ids": lesson_ids,
                    "before": copy.deepcopy(selected),
                    "after": [copy.deepcopy(merged_lesson)],
                    "diff": _unified_diff(
                        _lessons_diff_text(selected),
                        _lessons_diff_text([merged_lesson]),
                        fromfile="merge:before",
                        tofile="merge:after",
                    ),
                }
            ],
        )

    def propose_move_image_patch(
        self,
        state: dict[str, Any],
        lesson_id: str,
        image_id: str,
        new_index: int,
        *,
        list_name: str = "images",
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lesson = _lesson_by_id(state.get("lessons", []), lesson_id)
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {lesson_id}")
        images = list(lesson.get(list_name, []))
        current_index = next((index for index, image in enumerate(images) if str(image.get("id", "")) == image_id), None)
        if current_index is None:
            raise ValueError(f"Unknown image id for {lesson_id}: {image_id}")
        moved = images.pop(current_index)
        target_index = max(0, min(int(new_index), len(images)))
        images.insert(target_index, moved)
        return self.propose_patch(
            target={"type": "image", "lesson_id": lesson_id, "image_id": image_id, "action": "move_image"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_lesson_field",
                    "lesson_id": lesson_id,
                    "field": list_name,
                    "before": lesson.get(list_name, []),
                    "after": images,
                    "diff": _unified_diff(
                        _image_list_diff_text(lesson.get(list_name, [])),
                        _image_list_diff_text(images),
                        fromfile=f"{lesson_id}:{list_name}:before",
                        tofile=f"{lesson_id}:{list_name}:after",
                    ),
                }
            ],
        )

    def propose_replace_citation_patch(
        self,
        state: dict[str, Any],
        lesson_id: str,
        source_index: int,
        new_source: dict[str, Any],
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lesson = _lesson_by_id(state.get("lessons", []), lesson_id)
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {lesson_id}")
        sources = list(lesson.get("sources", []))
        before = sources[source_index] if 0 <= source_index < len(sources) else {}
        return self.propose_patch(
            target={"type": "citation", "lesson_id": lesson_id, "source_index": source_index, "action": "replace_citation"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_citation",
                    "lesson_id": lesson_id,
                    "source_index": source_index,
                    "before": before,
                    "after": new_source,
                    "diff": _unified_diff(
                        str(before),
                        str(new_source),
                        fromfile=f"{lesson_id}:citation:before",
                        tofile=f"{lesson_id}:citation:after",
                    ),
                }
            ],
        )

    def propose_add_evidence_patch(
        self,
        lesson_id: str,
        source: dict[str, Any],
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.propose_patch(
            target={"type": "evidence", "lesson_id": lesson_id, "source_id": stable_source_id(source), "action": "add_evidence"},
            reason=reason,
            evidence=evidence or [source],
            operations=[{"op": "add_lesson_evidence", "lesson_id": lesson_id, "source": source}],
        )

    def propose_state_path_patch(
        self,
        state: dict[str, Any],
        path: list[str | int],
        new_value: Any,
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        before = copy.deepcopy(_get_state_path(state, path))
        return self.propose_patch(
            target={"type": "state_path", "path": path, "action": "replace_state_path"},
            reason=reason,
            evidence=evidence,
            operations=[
                {
                    "op": "replace_state_path",
                    "path": path,
                    "before": before,
                    "after": new_value,
                    "diff": _unified_diff(
                        str(before),
                        str(new_value),
                        fromfile="state_path:before",
                        tofile="state_path:after",
                    ),
                }
            ],
        )

    def apply_patch(self, state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        revised = copy.deepcopy(state)
        inverse_operations: list[dict[str, Any]] = []
        for operation in patch.get("operations", []):
            inverse_operations.append(_apply_operation(revised, operation))
        applied = copy.deepcopy(patch)
        applied["status"] = "applied"
        applied["applied_at"] = datetime.now(timezone.utc).isoformat()
        applied["inverse_operations"] = inverse_operations
        revised.setdefault("compile_patches", []).append(applied)
        self._append_patch_record(applied)
        return revised

    def rollback_patch(self, state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        rolled_back = copy.deepcopy(state)
        for operation in patch.get("inverse_operations", []):
            _apply_operation(rolled_back, operation)
        record = copy.deepcopy(patch)
        record["status"] = "rolled_back"
        record["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        rolled_back.setdefault("compile_patches", []).append(record)
        self._append_patch_record(record)
        return rolled_back

    def compare_versions(self, version_a: str, version_b: str) -> dict[str, Any]:
        if self.vault_root is None or self.course_id is None:
            raise ValueError("compare_versions requires vault_root and course_id")
        target_dir = course_dir(self.vault_root, self.course_id)
        lessons_a = _read_version_lessons(target_dir, version_a)
        lessons_b = _read_version_lessons(target_dir, version_b)
        return {
            "course_id": self.course_id,
            "from_version": version_a,
            "to_version": version_b,
            "lesson_files_added": sorted(set(lessons_b) - set(lessons_a)),
            "lesson_files_removed": sorted(set(lessons_a) - set(lessons_b)),
            "lesson_files_changed": sorted(name for name in set(lessons_a) & set(lessons_b) if lessons_a[name] != lessons_b[name]),
        }

    def _next_patch_id(self) -> str:
        if self.vault_root is None or self.course_id is None:
            return f"patch-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        path = course_dir(self.vault_root, self.course_id) / "compile_patches.json"
        existing = read_json(path) if path.exists() else []
        return f"patch-{len(existing) + 1:03d}"

    def _append_patch_record(self, patch: dict[str, Any]) -> None:
        if self.vault_root is None or self.course_id is None:
            return
        target_dir = course_dir(self.vault_root, self.course_id)
        path = target_dir / "compile_patches.json"
        records = read_json(path) if path.exists() else []
        records.append(patch)
        write_json(path, records)


def _apply_operation(state: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    op = str(operation.get("op", ""))
    if op == "replace_lessons":
        lessons = state.setdefault("lessons", [])
        target_ids = {str(value) for value in operation.get("target_lesson_ids", [])}
        before = [copy.deepcopy(lesson) for lesson in lessons if str(lesson.get("id", "")) in target_ids]
        insert_index = next((index for index, lesson in enumerate(lessons) if str(lesson.get("id", "")) in target_ids), len(lessons))
        remaining = [lesson for lesson in lessons if str(lesson.get("id", "")) not in target_ids]
        replacement = copy.deepcopy(operation.get("after", []))
        state["lessons"] = remaining[:insert_index] + replacement + remaining[insert_index:]
        _renumber_lessons(state["lessons"])
        return {
            **operation,
            "target_lesson_ids": [str(lesson.get("id", "")) for lesson in replacement],
            "before": replacement,
            "after": before,
        }
    if op == "replace_lesson_field":
        lesson = _lesson_by_id(state.get("lessons", []), str(operation.get("lesson_id", "")))
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {operation.get('lesson_id')}")
        field = str(operation.get("field", "body"))
        before = lesson.get(field)
        lesson[field] = operation.get("after", "")
        return {**operation, "before": operation.get("after", ""), "after": before}
    if op == "replace_citation":
        lesson = _lesson_by_id(state.get("lessons", []), str(operation.get("lesson_id", "")))
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {operation.get('lesson_id')}")
        index = int(operation.get("source_index", 0))
        sources = lesson.setdefault("sources", [])
        before = sources[index] if 0 <= index < len(sources) else {}
        if 0 <= index < len(sources):
            sources[index] = operation.get("after", {})
        else:
            sources.append(operation.get("after", {}))
        return {**operation, "before": operation.get("after", {}), "after": before}
    if op == "add_lesson_evidence":
        lesson = _lesson_by_id(state.get("lessons", []), str(operation.get("lesson_id", "")))
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {operation.get('lesson_id')}")
        source = operation.get("source", {})
        lesson.setdefault("sources", []).append(source)
        return {"op": "remove_lesson_evidence", "lesson_id": operation.get("lesson_id"), "source_id": stable_source_id(source)}
    if op == "remove_lesson_evidence":
        lesson = _lesson_by_id(state.get("lessons", []), str(operation.get("lesson_id", "")))
        if lesson is None:
            raise ValueError(f"Unknown lesson id: {operation.get('lesson_id')}")
        source_id = str(operation.get("source_id", ""))
        sources = lesson.setdefault("sources", [])
        removed = [source for source in sources if stable_source_id(source) == source_id]
        lesson["sources"] = [source for source in sources if stable_source_id(source) != source_id]
        return {"op": "add_lesson_evidence", "lesson_id": operation.get("lesson_id"), "source": removed[0] if removed else {}}
    if op == "replace_state_path":
        path = list(operation.get("path", []))
        before = copy.deepcopy(_get_state_path(state, path))
        _set_state_path(state, path, copy.deepcopy(operation.get("after")))
        return {**operation, "before": operation.get("after"), "after": before}
    raise ValueError(f"Unsupported patch operation: {op}")


def _renumber_lessons(lessons: list[dict[str, Any]]) -> None:
    for index, lesson in enumerate(lessons, start=1):
        lesson["order"] = index
        lesson.setdefault("id", f"lesson-{index:03d}")


def _read_version_lessons(target_dir: Path, version: str) -> dict[str, str]:
    lessons_dir = target_dir / "versions" / version / "lessons"
    if not lessons_dir.exists():
        return {}
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(lessons_dir.glob("*.md"))}


def _lesson_by_id(lessons: list[dict[str, Any]], lesson_id: str) -> dict[str, Any] | None:
    for lesson in lessons:
        if str(lesson.get("id", "")) == lesson_id:
            return lesson
    return None


def _get_state_path(state: dict[str, Any], path: list[str | int]) -> Any:
    current: Any = state
    for part in path:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def _set_state_path(state: dict[str, Any], path: list[str | int], value: Any) -> None:
    if not path:
        raise ValueError("Cannot replace the entire state with replace_state_path")
    current: Any = state
    for part in path[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last = path[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def _lessons_diff_text(lessons: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for lesson in lessons:
        lines.extend(
            [
                f"## {lesson.get('id', '')} {lesson.get('title', '')}",
                str(lesson.get("body", "")),
                "sources: " + ", ".join(stable_source_id(source) for source in lesson.get("sources", [])),
                "",
            ]
        )
    return "\n".join(lines)


def _image_list_diff_text(images: list[dict[str, Any]]) -> str:
    return "\n".join(f"{index}: {image.get('id', '')} {image.get('caption', '')}" for index, image in enumerate(images))


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]+", str(query)) if len(term.strip()) >= 2]


def _score_text(text: str, terms: list[str]) -> int:
    if not terms:
        return 0
    lowered = str(text).lower()
    return sum(lowered.count(term) for term in terms)


def _formula_query(query: str) -> bool:
    return bool(re.search(r"(\\[A-Za-z]+|\$|=|≈|≤|≥|∫|Σ|∑|frac|matrix|公式|方程)", str(query)))


def _extract_formula_blocks(content: str) -> list[str]:
    blocks = re.findall(r"\$\$([\s\S]+?)\$\$", content)
    blocks.extend(re.findall(r"\\\[([\s\S]+?)\\\]", content))
    for line in content.splitlines():
        stripped = line.strip()
        if _formula_query(stripped) and len(stripped) <= 500:
            blocks.append(stripped)
    return _dedupe_keep_order([block.strip() for block in blocks if block.strip()])


def _extract_table_blocks(content: str) -> list[str]:
    tables: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if "|" in line and line.count("|") >= 2:
            current.append(line)
            continue
        if current:
            tables.append("\n".join(current))
            current = []
    if current:
        tables.append("\n".join(current))
    return tables


def _best_excerpt(text: str, terms: list[str], limit: int = 360) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for term in terms:
        for line in lines:
            if term in line.lower():
                return _short_text(line, limit)
    return _short_text(text, limit)


def _salient_phrases(content: str) -> list[str]:
    lines = [line.strip("# -*>\t ") for line in str(content).splitlines() if line.strip()]
    candidates = [line for line in lines if len(line) >= 6]
    words = _query_terms(content)
    if words:
        candidates.append(" ".join(words[:10]))
    return _dedupe_keep_order(candidates)[:12]


def _short_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dedupe_keep_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _unified_diff(before: str, after: str, *, fromfile: str, tofile: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            str(before).splitlines(),
            str(after).splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )
