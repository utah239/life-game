#!/usr/bin/env python3
"""Run deterministic repository checks used by the PR review gate.

This is deliberately a review *check*, not a GitHub approval.  It catches
structural regressions that are easy to miss in a large generated diff while
leaving model-behaviour validation to the unit and policy suites.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tokenize
from pathlib import Path


TEXT_SUFFIXES = {
    ".cfg", ".css", ".html", ".ini", ".js", ".json", ".md", ".ps1",
    ".py", ".sh", ".toml", ".txt", ".yaml", ".yml",
}
FORBIDDEN_ARTIFACT_SUFFIXES = {
    ".arrow", ".cubin", ".db", ".feather", ".h5", ".hdf5", ".nbi",
    ".nbc", ".npy", ".npz", ".onnx", ".parquet", ".pkl", ".pickle",
    ".pt", ".pth", ".qdrep", ".safetensors", ".sqlite", ".sqlite3",
}
FORBIDDEN_DIRECTORY_PARTS = {
    "artifacts", "backups", "captures", "checkpoints", "frames",
    "node_modules", "outputs", "recordings", "reports", "runs", "sweeps",
}
UPWARD_IMPORTS = {"event_store", "game", "projection"}
MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024


def tracked_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], cwd=repo_root, check=True,
        stdout=subprocess.PIPE)
    return [repo_root / raw.decode("utf-8")
            for raw in result.stdout.split(b"\0") if raw]


def conflict_marker_issue(text: str) -> bool:
    lines = {line.rstrip("\r\n") for line in text.splitlines()}
    return (any(line.startswith("<<<<<<< ") for line in lines)
            and "=======" in lines
            and any(line.startswith(">>>>>>> ") for line in lines))


def imported_roots(tree: ast.AST) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def has_wildcard_import(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree))


def production_python(relative: Path) -> bool:
    return (relative.suffix == ".py"
            and relative.parts[:1] == ("phase1_bank_exp",)
            and not relative.name.startswith("test_")
            and relative.parent.as_posix() in {
                "phase1_bank_exp", "phase1_bank_exp/institutions"})


def review_file(repo_root: Path, path: Path) -> list[str]:
    relative = path.relative_to(repo_root)
    rel = relative.as_posix()
    issues = []
    if path.stat().st_size > MAX_TRACKED_FILE_BYTES:
        issues.append(
            f"{rel}: tracked file exceeds {MAX_TRACKED_FILE_BYTES} bytes")
    if (relative.suffix.lower() in FORBIDDEN_ARTIFACT_SUFFIXES
            or any(part in FORBIDDEN_DIRECTORY_PARTS
                   for part in relative.parts)):
        issues.append(f"{rel}: generated/runtime artifact is tracked")
    if rel == "phase1_bank_exp/dashboard/life_ledger.html":
        issues.append(f"{rel}: generated dashboard HTML is tracked")

    if relative.suffix.lower() in TEXT_SUFFIXES:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(f"{rel}: text file is not valid UTF-8")
            text = ""
        if conflict_marker_issue(text):
            issues.append(f"{rel}: unresolved merge conflict markers")

    if relative.suffix != ".py":
        return issues
    try:
        with tokenize.open(path) as source:
            tree = ast.parse(source.read(), filename=rel)
    except (SyntaxError, UnicodeError) as exc:
        issues.append(f"{rel}: Python parse failed: {exc}")
        return issues

    if production_python(relative):
        roots = imported_roots(tree)
        if relative.name != "game.py":
            forbidden = sorted(roots & UPWARD_IMPORTS)
            if forbidden:
                issues.append(
                    f"{rel}: production module imports upper layer "
                    + ", ".join(forbidden))
        if relative.parent.as_posix() == "phase1_bank_exp/institutions" \
                and "random" in roots:
            issues.append(
                f"{rel}: institution rule has an implicit random dependency")
        if has_wildcard_import(tree):
            issues.append(f"{rel}: wildcard import is not allowed")
    return issues


def run_review(repo_root: Path) -> tuple[list[str], int]:
    files = tracked_files(repo_root)
    issues = []
    for path in files:
        if path.is_file():
            issues.extend(review_file(repo_root, path))
    return issues, len(files)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    issues, file_count = run_review(repo_root)
    if issues:
        print("Automated review failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"Automated review passed: {file_count} tracked files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
