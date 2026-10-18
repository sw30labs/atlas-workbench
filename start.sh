#!/usr/bin/env bash
#
# One command to a running Contingency Atlas dashboard.
#
#   ./start.sh              # set up, load the demo corpus, open the dashboard
#   ./start.sh --port 9000  # same, on another port
#
# Idempotent: re-running reuses the virtualenv and existing data. Set
# ATLAS_SKIP_INSTALL=1 to skip the (fast, cached) dependency refresh.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_VERSION="${ATLAS_PYTHON:-3.13}"
VENV=".venv"

die() { printf '\nstart.sh: %s\n' "$*" >&2; exit 1; }

if [ ! -x "$VENV/bin/python" ]; then
  printf 'Creating %s with Python %s...\n' "$VENV" "$PYTHON_VERSION"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$PYTHON_VERSION" "$VENV" || die "could not create the virtualenv"
  else
    for candidate in "python$PYTHON_VERSION" python3.13 python3.12 python3; do
      if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if (3,12) <= sys.version_info[:2] < (3,14) else 1)'; then
          "$candidate" -m venv "$VENV" || die "could not create the virtualenv"
          break
        fi
      fi
    done
  fi
fi
[ -x "$VENV/bin/python" ] || die "Python 3.12 or 3.13 is required. Install one, or install uv: https://docs.astral.sh/uv/"

if [ "${ATLAS_SKIP_INSTALL:-0}" != "1" ]; then
  printf 'Installing dependencies...\n'
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python" -q -e . || die "dependency install failed"
  else
    # A venv created by `uv venv` (without --seed) ships no pip; bootstrap it.
    "$VENV/bin/python" -m pip --version >/dev/null 2>&1 \
      || "$VENV/bin/python" -m ensurepip --upgrade >/dev/null \
      || die "no pip in $VENV and ensurepip failed"
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q -e . || die "dependency install failed"
  fi
fi

if [ "${ATLAS_INSTALL_ONLY:-0}" = "1" ]; then
  printf 'Install complete. Run ./start.sh to open the dashboard.\n'
  exit 0
fi

exec "$VENV/bin/atlas" up "$@"
