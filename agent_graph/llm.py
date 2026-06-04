"""LLM clients for course compilation."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_env(path: str | Path = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    keys = (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_ALLOW_GENERIC_FALLBACK",
        "LLM_ALLOW_SILICONFLOW_FALLBACK",
        "LLM_TIMEOUT",
        "LLM_RETRIES",
        "LLM_RETRY_BACKOFF_SECONDS",
        "GLM_ANTHROPIC_URL",
        "GLM_BASE_URL",
        "GLM_API_KEY",
        "GLM_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    )
    for key in keys:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class LLMClient:
    """Small JSON-completion client with GLM-first provider selection."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        provider: str = "openai",
        retries: int = 2,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.provider = provider
        self.retries = max(0, retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.last_metadata: dict[str, Any] = {}

    @classmethod
    def from_env(cls) -> "LLMClient | None":
        values = load_env()
        timeout = int(values.get("LLM_TIMEOUT", "300"))
        retries = int(values.get("LLM_RETRIES", "2"))
        retry_backoff_seconds = float(values.get("LLM_RETRY_BACKOFF_SECONDS", "2"))
        glm_key = values.get("GLM_API_KEY") or values.get("ANTHROPIC_AUTH_TOKEN")
        glm_model = values.get("GLM_MODEL") or values.get("LLM_MODEL") or "GLM-4.7"
        anthropic_url = values.get("GLM_ANTHROPIC_URL") or values.get("ANTHROPIC_BASE_URL")
        if anthropic_url and glm_key:
            return cls(anthropic_url, glm_key, glm_model, timeout=timeout, provider="anthropic", retries=retries, retry_backoff_seconds=retry_backoff_seconds)

        glm_base_url = values.get("GLM_BASE_URL")
        if glm_base_url and glm_key and glm_model:
            return cls(glm_base_url, glm_key, glm_model, timeout=timeout, provider="openai", retries=retries, retry_backoff_seconds=retry_backoff_seconds)

        llm_base_url = values.get("LLM_BASE_URL", "")
        llm_key = values.get("LLM_API_KEY")
        llm_model = values.get("LLM_MODEL")
        allow_generic = values.get("LLM_ALLOW_GENERIC_FALLBACK") == "1"
        allow_siliconflow = values.get("LLM_ALLOW_SILICONFLOW_FALLBACK") == "1"
        if llm_base_url and llm_key and llm_model and allow_generic:
            if "siliconflow.cn" not in llm_base_url or allow_siliconflow:
                return cls(llm_base_url, llm_key, llm_model, timeout=timeout, provider="openai", retries=retries, retry_backoff_seconds=retry_backoff_seconds)
        return None

    @property
    def cache_identity(self) -> dict[str, str]:
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model}

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        errors: list[str] = []
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                result = self._complete_json_once(system, user)
                self.last_metadata = {
                    **self.last_metadata,
                    "attempt": attempt,
                    "attempts": attempt,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "timeout_seconds": self.timeout,
                }
                return result
            except Exception as exc:
                errors.append(str(exc))
                self.last_metadata = {
                    "provider": self.provider,
                    "model": self.model,
                    "attempt": attempt,
                    "attempts": attempt,
                    "timeout_seconds": self.timeout,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc),
                }
                if attempt >= attempts:
                    break
                time.sleep(self.retry_backoff_seconds * attempt)
        raise RuntimeError(f"LLM request failed after {attempts} attempt(s): {errors[-1] if errors else 'unknown error'}")

    def _complete_json_once(self, system: str, user: str) -> dict[str, Any]:
        if self.provider == "anthropic":
            return self._complete_json_anthropic(system, user)
        try:
            return self._complete_json(system, user, use_response_format=True)
        except RuntimeError as exc:
            if "response_format" not in str(exc).lower() and "400" not in str(exc):
                raise
            return self._complete_json(system, user, use_response_format=False)

    def cache_key(self, system: str, user: str) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps(self.cache_identity, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
        digest.update(system.encode("utf-8"))
        digest.update(b"\n")
        digest.update(user.encode("utf-8"))
        return digest.hexdigest()[:24]

    def _complete_json_anthropic(self, system: str, user: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": 0.1,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": _anthropic_user_blocks(user),
                }
            ],
        }
        request = Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        self.last_metadata = {
            "provider": self.provider,
            "model": self.model,
            "usage": data.get("usage", {}),
            "stop_reason": data.get("stop_reason"),
        }
        content = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        return parse_json_object(content)

    def _complete_json(self, system: str, user: str, use_response_format: bool) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if use_response_format:
            payload["response_format"] = {"type": "json_object"}
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        self.last_metadata = {
            "provider": self.provider,
            "model": self.model,
            "usage": data.get("usage", {}),
        }
        content = data["choices"][0]["message"]["content"]
        return parse_json_object(content)


def _anthropic_user_blocks(user: str) -> list[dict[str, Any]]:
    marker = next(
        (item for item in ("Source chunks:\n", "Source index context packs:\n", "Lesson batch ") if item in user),
        "",
    )
    if marker not in user:
        return [{"type": "text", "text": user, "cache_control": {"type": "ephemeral"}}]
    before, source_chunks = user.split(marker, 1)
    blocks: list[dict[str, Any]] = []
    if before.strip():
        blocks.append({"type": "text", "text": before.strip()})
    blocks.append({"type": "text", "text": marker + source_chunks, "cache_control": {"type": "ephemeral"}})
    blocks.append({"type": "text", "text": "Return strict JSON only."})
    return blocks


def parse_json_object(content: str) -> dict[str, Any]:
    candidate = _extract_json_object(content)
    repaired = _escape_latex_backslashes(candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        if candidate != content:
            return json.loads(_escape_latex_backslashes(candidate))
        raise


def _extract_json_object(content: str) -> str:
    stripped = content.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _escape_latex_backslashes(candidate: str) -> str:
    """Escape raw LaTeX command slashes inside JSON strings without touching JSON syntax."""

    result: list[str] = []
    in_string = False
    i = 0
    while i < len(candidate):
        char = candidate[i]
        if not in_string:
            result.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            result.append(char)
            in_string = False
            i += 1
            continue

        if char != "\\":
            result.append(char)
            i += 1
            continue

        next_char = candidate[i + 1] if i + 1 < len(candidate) else ""
        after_next = candidate[i + 2] if i + 2 < len(candidate) else ""
        if next_char in {'"', "\\", "/"}:
            result.extend((char, next_char))
            i += 2
        elif next_char == "u":
            maybe_hex = candidate[i + 2 : i + 6]
            if len(maybe_hex) == 4 and all(item in "0123456789abcdefABCDEF" for item in maybe_hex):
                result.append(candidate[i : i + 6])
                i += 6
            else:
                result.append("\\\\")
                i += 1
        elif next_char in {"b", "f", "n", "r", "t"}:
            if _is_latex_command_with_json_escape_prefix(candidate, i + 1):
                result.append("\\\\")
                i += 1
            else:
                result.extend((char, next_char))
                i += 2
        else:
            result.append("\\\\")
            i += 1
    return "".join(result)


_LATEX_COMMANDS_WITH_JSON_ESCAPE_PREFIX = {
    "bar",
    "begin",
    "beta",
    "big",
    "Big",
    "binom",
    "boldsymbol",
    "frac",
    "forall",
    "nabla",
    "ne",
    "neq",
    "not",
    "notin",
    "nu",
    "rangle",
    "rightarrow",
    "rho",
    "right",
    "rm",
    "tag",
    "tan",
    "tau",
    "text",
    "tfrac",
    "theta",
    "tilde",
    "times",
    "to",
    "top",
}


def _is_latex_command_with_json_escape_prefix(candidate: str, start: int) -> bool:
    end = start
    while end < len(candidate) and candidate[end].isascii() and candidate[end].isalpha():
        end += 1
    return candidate[start:end] in _LATEX_COMMANDS_WITH_JSON_ESCAPE_PREFIX
