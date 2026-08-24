#!/usr/bin/env python3
"""Validate the curated Study 01 publication artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "studies" / "study-01"
MODEL_FILES = {
    "ox-alpha": "ox-alpha.json",
    "gemini-37-flash-high": "gemini-37-flash-high.json",
    "qwen-38-max": "qwen-38-max.json",
}
EXPECTED_SCORES = {
    "ox-alpha": 13,
    "gemini-37-flash-high": 13,
    "qwen-38-max": 15,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fail(message: str) -> None:
    raise AssertionError(message)


def main() -> int:
    manifest = json.loads((STUDY / "manifest.json").read_text())
    summary = json.loads((STUDY / "summary.json").read_text())
    task_ids = manifest["task_ids"]

    if len(task_ids) != 20 or len(set(task_ids)) != 20:
        fail("manifest must contain 20 unique task IDs")
    if summary["task_count"] != 20:
        fail("summary task count must be 20")
    if summary["dataset_revision"] != manifest["dataset_revision"]:
        fail("dataset revision mismatch")

    for slug, filename in MODEL_FILES.items():
        predictions = json.loads((STUDY / "predictions" / filename).read_text())
        report = json.loads((STUDY / "evaluator-reports" / filename).read_text())
        model_summary = summary["models"][slug]

        if set(predictions) != set(task_ids):
            fail(f"prediction task IDs mismatch for {slug}")
        if report["total_instances"] != 20:
            fail(f"evaluator denominator mismatch for {slug}")
        if report["resolved_instances"] != EXPECTED_SCORES[slug]:
            fail(f"unexpected score for {slug}")
        if report["resolved_instances"] != model_summary["resolved"]:
            fail(f"summary/report score mismatch for {slug}")
        if set(report["resolved_ids"]) != set(model_summary["resolved_ids"]):
            fail(f"summary/report resolved IDs mismatch for {slug}")
        if report["infra_failure_instances"] != 0 or report["error_instances"] != 0:
            fail(f"definite evaluator failure remains for {slug}")

    with (STUDY / "per-task.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 20 or {row["instance_id"] for row in rows} != set(task_ids):
        fail("per-task CSV does not match the manifest")

    checksum_path = STUDY / "checksums.sha256"
    for line in checksum_path.read_text().splitlines():
        expected, relative = line.split("  ", 1)
        artifact = STUDY / relative
        if artifact == checksum_path:
            continue
        if not artifact.is_file() or sha256(artifact) != expected:
            fail(f"checksum mismatch: {relative}")

    secret_pattern = re.compile(
        r"(?i)(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|"
        r"authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._-]+|100\.91\.134\.56|/home/rarch)"
    )
    allowed_text_suffixes = {".md", ".json", ".csv", ".py", ".sh", ".cff"}
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix not in allowed_text_suffixes and path.name not in {
            "LICENSE-CODE",
            "LICENSE-REPORT",
            ".gitignore",
        }:
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if secret_pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    if hits:
        fail("sensitive values or local paths found: " + ", ".join(hits))

    print("Study 01 validation passed")
    print("Scores: Ox 13/20, Gemini 13/20, Qwen 15/20")
    print("No definite evaluator failures or publication secret patterns found")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
