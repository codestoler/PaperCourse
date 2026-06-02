#!/usr/bin/env python3
"""Convert local PDFs to Markdown through MinerU precise batch API."""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests


MINERU_API = "https://mineru.net/api/v4"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="course-vault/raw/numerical_analysis")
    parser.add_argument("--output-dir", default="course-vault/parsed/numerical_analysis")
    parser.add_argument("--model-version", default="vlm", choices=["pipeline", "vlm"])
    parser.add_argument("--poll-interval", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    token = load_env_token("MINERU_TOKEN")
    if not token:
        print("PROBLEM: MINERU_TOKEN is missing or empty in .env")
        return 2

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"PROBLEM: no PDF files found in {input_dir}")
        return 2

    manifest_path = output_dir / "mineru_manifest.json"
    manifest = read_manifest(manifest_path)
    if all(parsed_markdown_path(output_dir, pdf.stem).exists() for pdf in pdfs):
        print(f"status=done files={len(pdfs)} output_dir={output_dir}")
        return 0

    if not manifest.get("batch_id"):
        manifest = submit_batch(token, pdfs, args.model_version)
        write_json(manifest_path, manifest)

    if not manifest.get("uploaded"):
        upload_files(pdfs, manifest["file_urls"])
        manifest["uploaded"] = True
        write_json(manifest_path, manifest)

    results = poll_batch(token, manifest["batch_id"], args.timeout, args.poll_interval)
    manifest["results"] = results
    write_json(manifest_path, manifest)

    failed = [item for item in results if item.get("state") == "failed"]
    if failed:
        print("PROBLEM: MinerU failed for files:")
        for item in failed:
            print(f"- {item.get('file_name')}: {item.get('err_msg')}")
        return 3

    download_results(results, output_dir)
    missing = [pdf.name for pdf in pdfs if not parsed_markdown_path(output_dir, pdf.stem).exists()]
    if missing:
        print(f"PROBLEM: MinerU completed but Markdown extraction is missing for: {missing}")
        return 4

    print(f"status=done files={len(pdfs)} output_dir={output_dir}")
    return 0


def load_env_token(name: str) -> str:
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    return os.environ.get(name, "").strip()


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def submit_batch(token: str, pdfs: list[Path], model_version: str) -> dict[str, Any]:
    files = [{"name": pdf.name, "data_id": safe_data_id(pdf.stem)} for pdf in pdfs]
    response = requests.post(
        f"{MINERU_API}/file-urls/batch",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "files": files,
            "model_version": model_version,
            "language": "ch",
            "enable_formula": True,
            "enable_table": True,
        },
        timeout=60,
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("code") != 0:
        raise RuntimeError(f"MinerU batch submit failed: http={response.status_code} payload={redact(payload)}")
    return {
        "batch_id": payload["data"]["batch_id"],
        "files": files,
        "file_urls": payload["data"]["file_urls"],
        "uploaded": False,
    }


def upload_files(pdfs: list[Path], urls: list[str]) -> None:
    if len(pdfs) != len(urls):
        raise RuntimeError("MinerU returned a different number of upload URLs than input files")
    for pdf, url in zip(pdfs, urls):
        with pdf.open("rb") as handle:
            response = requests.put(url, data=handle, timeout=180)
        if response.status_code not in {200, 204}:
            raise RuntimeError(f"Upload failed for {pdf.name}: http={response.status_code}")
        print(f"uploaded={pdf.name}")


def poll_batch(token: str, batch_id: str, timeout: int, interval: int) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    last_states = ""
    while time.time() < deadline:
        response = requests.get(
            f"{MINERU_API}/extract-results/batch/{batch_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
            timeout=60,
        )
        payload = response.json()
        if response.status_code != 200 or payload.get("code") != 0:
            raise RuntimeError(f"MinerU poll failed: http={response.status_code} payload={redact(payload)}")
        results = payload.get("data", {}).get("extract_result", [])
        states = ", ".join(f"{item.get('file_name')}={item.get('state')}" for item in results)
        if states != last_states:
            print(states)
            last_states = states
        if results and all(item.get("state") in {"done", "failed"} for item in results):
            return results
        time.sleep(interval)
    raise TimeoutError(f"MinerU polling timed out for batch_id={batch_id}")


def download_results(results: list[dict[str, Any]], output_dir: Path) -> None:
    zip_dir = output_dir / "_zip"
    zip_dir.mkdir(parents=True, exist_ok=True)
    for item in results:
        if item.get("state") != "done":
            continue
        file_name = item["file_name"]
        stem = Path(file_name).stem
        result_dir = output_dir / stem
        target = parsed_markdown_path(output_dir, stem)
        if target.exists() and any(result_dir.iterdir() if result_dir.exists() else []):
            continue
        url = item.get("full_zip_url")
        if not url:
            raise RuntimeError(f"Missing full_zip_url for {file_name}")
        zip_path = zip_dir / f"{Path(file_name).stem}.zip"
        if zip_path.exists():
            content = zip_path.read_bytes()
        else:
            response = requests.get(url, timeout=180)
            response.raise_for_status()
            content = response.content
            zip_path.write_bytes(content)
        extract_mineru_zip(content, result_dir)
        if not target.exists():
            raise RuntimeError(f"No full.md found after extracting MinerU zip for {file_name}")
        print(f"parsed_dir={result_dir}")


def extract_mineru_zip(content: bytes, result_dir: Path) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        archive.extractall(result_dir)
        md_name = next((name for name in archive.namelist() if name.endswith("full.md")), None)
        if md_name is None:
            md_name = next((name for name in archive.namelist() if name.endswith(".md")), None)
        if md_name and md_name != "full.md":
            full_md = result_dir / "full.md"
            if not full_md.exists():
                full_md.write_text((result_dir / md_name).read_text(encoding="utf-8"), encoding="utf-8")


def parsed_markdown_path(output_dir: Path, stem: str) -> Path:
    return output_dir / stem / "full.md"


def safe_data_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)[:128]


def redact(payload: Any) -> Any:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 800:
        text = text[:800] + "..."
    return text


if __name__ == "__main__":
    raise SystemExit(main())
