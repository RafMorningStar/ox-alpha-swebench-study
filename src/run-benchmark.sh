#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SRC_DIR/.." && pwd)"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  PYTHON="$VIRTUAL_ENV/bin/python"
else
  PYTHON="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  printf 'Python was not found. Create .venv or activate a compatible environment.\n' >&2
  exit 1
fi

old_profile=""
if command -v powerprofilesctl >/dev/null 2>&1; then
  old_profile="$(powerprofilesctl get 2>/dev/null || true)"
  powerprofilesctl set performance 2>/dev/null || true
fi

restore_profile() {
  if [[ -n "$old_profile" ]]; then
    powerprofilesctl set "$old_profile" 2>/dev/null || true
  fi
}
trap restore_profile EXIT

if [[ $# -eq 0 ]]; then
  command="overnight"
else
  command="$1"
  shift
fi

if [[ "$command" != "status" && "$command" != "report" ]] && ! docker info >/dev/null 2>&1; then
  printf 'Docker is not available. Start Docker, then run this script again.\n' >&2
  exit 1
fi

status=0
systemd-inhibit \
    --what=sleep:idle \
    --who=llm-agent-bench \
    --why="Running offline SWE-bench benchmark" \
    --mode=block \
    "$PYTHON" "$SRC_DIR/benchmark.py" "$command" "$@" || status=$?
exit "$status"
