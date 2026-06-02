#!/usr/bin/env python3
"""Split large PDFs into MinerU-sized parts while preserving page ranges."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fitz


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="Original PDF to split")
    parser.add_argument("--output-dir", required=True, help="Directory for split PDF parts")
    parser.add_argument("--max-pages", type=int, default=180, help="Maximum pages per part; keep below MinerU limits")
    args = parser.parse_args()

    source = Path(args.pdf)
    if not source.exists() or source.suffix.lower() != ".pdf":
        print(f"PROBLEM: expected a PDF file: {source}")
        return 2
    if args.max_pages < 1:
        print("PROBLEM: --max-pages must be positive")
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(source)
    parts: list[dict[str, object]] = []
    for start in range(0, doc.page_count, args.max_pages):
        end = min(start + args.max_pages, doc.page_count)
        part = fitz.open()
        part.insert_pdf(doc, from_page=start, to_page=end - 1)
        name = f"{source.stem}_part{len(parts) + 1:03d}_pages{start + 1:03d}-{end:03d}.pdf"
        path = output_dir / name
        part.save(path)
        part.close()
        parts.append(
            {
                "path": str(path),
                "source_path": str(source),
                "part": len(parts) + 1,
                "start_page": start + 1,
                "end_page": end,
                "page_count": end - start,
            }
        )

    manifest = {
        "source_path": str(source),
        "source_page_count": doc.page_count,
        "max_pages": args.max_pages,
        "parts": parts,
    }
    doc.close()
    (output_dir / "split_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"status=done source_pages={manifest['source_page_count']} parts={len(parts)} output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
