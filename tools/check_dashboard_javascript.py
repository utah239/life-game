#!/usr/bin/env python3
"""Extract inline dashboard JavaScript and parse it with Node.js."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_PATTERN = re.compile(
    r"<script(?:\s[^>]*)?>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    template = repo_root / "phase1_bank_exp" / "dashboard" / "template.html"
    scripts = SCRIPT_PATTERN.findall(template.read_text(encoding="utf-8"))
    if not scripts:
        print("No inline dashboard JavaScript found", file=sys.stderr)
        return 1
    node = shutil.which("node")
    if node is None:
        print("Node.js is required for dashboard syntax review", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "dashboard-inline.js"
        source.write_text("\n".join(scripts), encoding="utf-8")
        result = subprocess.run([node, "--check", str(source)])
    if result.returncode:
        return result.returncode
    print(f"Dashboard JavaScript passed: {len(scripts)} script block(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
