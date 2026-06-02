#!/usr/bin/env python3
"""Expand stored MinerU ZIP outputs into per-source parsed directories."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from mineru_pdf_to_md import extract_mineru_zip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parsed-dir", default="course-vault/parsed/numerical_analysis")
    args = parser.parse_args()

    parsed_dir = Path(args.parsed_dir)
    zip_dir = parsed_dir / "_zip"
    if not zip_dir.exists():
        print(f"PROBLEM: no _zip directory found under {parsed_dir}")
        return 2

    count = 0
    for zip_path in sorted(zip_dir.glob("*.zip")):
        result_dir = parsed_dir / zip_path.stem
        extract_mineru_zip(zip_path.read_bytes(), result_dir)
        count += 1
        print(f"parsed_dir={result_dir}")

    print(f"status=done expanded={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
