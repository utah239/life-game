#!/usr/bin/env bash
set -eu

project_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
workers="${1:-20}"
cd "$project_dir"
exec python3 -u dashboard/production_disruption_validate.py --workers "$workers" \
  >> dashboard/sweeps/production_disruption_oat.log 2>&1
