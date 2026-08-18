#!/usr/bin/env bash
set -eu

project_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
workers="${1:-20}"
cd "$project_dir"
exec python3 -u dashboard/robustness_validate.py --workers "$workers" \
  >> dashboard/sweeps/robustness_run.log 2>&1
