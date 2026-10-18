#!/usr/bin/env bash
# LEGACY entry point. Prefer `./start.sh` (or `atlas up`) for a first run.
# This script is kept because release automation and the ontology tests rely
# on its explicit migrate-then-export ordering.
#
# Set up Contingency Atlas from a source checkout and start the local
# dashboard. Demo data is NOT seeded into operator-managed stores; for the
# fictional demo corpus run
# `python scripts/seed_demo.py` (writes to generated/demo) explicitly.
#
# Usage:
#   ./setup_and_run.sh                       # OMLX backend (Mac MLX server)
#   ./setup_and_run.sh --provider dgx        # DGX vLLM backend, qwen27 recipe
#   ATLAS_DGX_SERVE_SCRIPT=/path/to/launcher ./setup_and_run.sh --provider dgx demo
#
# The DGX path is a variant of this script rather than a copy of it: the
# previous standalone setup_and_run_dgx.sh was forked before the seed refusal,
# the ontology extra and the stale-dashboard guard existed, and silently
# rotted. One script, one set of fixes.
set -euo pipefail

cd "$(dirname "$0")"

PROVIDER="${ATLAS_LLM_PROVIDER:-auto}"
if [ "${1:-}" = "--provider" ]; then
    PROVIDER="${2:?--provider needs a value (auto|omlx|dgx)}"
    shift 2
fi
RECIPE="${1:-qwen27}"
SERVE="${ATLAS_DGX_SERVE_SCRIPT:-}"

# The venv is chosen by PLATFORM, not by provider: the checkout travels between
# the Mac and the DGX, and a venv built on the other side must be neither
# reused nor clobbered. Which backend to talk to is a separate question,
# answered below by asking the server rather than by inspecting the machine.
if [ "$(uname -s)" = "Darwin" ]; then
    VENV=.venv
    PLATFORM_DEFAULT=omlx
else
    VENV=.venv-dgx
    PLATFORM_DEFAULT=dgx
fi

case "$PROVIDER" in
    auto)     ;;
    omlx|mlx) PROVIDER=omlx ;;
    dgx|vllm) PROVIDER=dgx ;;
    *) echo "ERROR: unknown provider '$PROVIDER' (expected auto, omlx or dgx)" >&2
       exit 1 ;;
esac

PORT="${ATLAS_PORT:-8080}"

# Dashboards from earlier runs survive the agent session or terminal that
# started them. A stale one keeps serving a register loaded at its own start
# time, so decisions made against it write through stale state — and it holds
# the port, which makes a fresh start look like a hang. Clear them first.
# Only processes whose executable is python count: a shell, editor or grep
# whose command line merely mentions the module must never be a kill target.
dashboard_pids() {
    local pid comm
    for pid in $(pgrep -f ' -m contingency_atlas\.web' 2>/dev/null || true); do
        [ "$pid" = "$$" ] && continue
        comm="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
        case "${comm##*/}" in
            python | python[0-9]*) printf '%s\n' "$pid" ;;
        esac
    done
}

stop_stale_dashboards() {
    local pids
    pids="$(dashboard_pids)"
    if [ -n "$pids" ]; then
        echo "==> Stopping stale dashboard process(es): $(echo "$pids" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        sleep 2
        pids="$(dashboard_pids)"
        if [ -n "$pids" ]; then
            # shellcheck disable=SC2086
            kill -9 $pids 2>/dev/null || true
            sleep 1
        fi
    fi
    if command -v lsof >/dev/null 2>&1 &&
       lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ERROR: port $PORT is held by a process that is not an Atlas dashboard:" >&2
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
        echo "Stop it, or set ATLAS_PORT to a free port." >&2
        exit 1
    fi
}

# Pick Python 3.12 or 3.13 (project requires >=3.12,<3.14)
if command -v python3.12 >/dev/null 2>&1; then
    PY=python3.12
elif command -v python3.13 >/dev/null 2>&1; then
    PY=python3.13
else
    PY=python3
fi

echo "==> Using $PY ($($PY --version 2>&1))"

# Create the virtual environment if needed, then activate it
if [ ! -d "$VENV" ]; then
    echo "==> Creating virtual environment in $VENV"
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# The ontology extra is not optional: the guard fails closed, so without
# rdflib/pyshacl/owlrl every machine-producer write seam raises GuardError
# and the pipeline cannot ingest, save the register, or file a proposal.
echo "==> Installing package with dev and ontology dependencies"
python -m pip install --upgrade pip
python -m pip install -e '.[dev,ontology]'

bring_up_vllm() {
    echo "==> Bringing up vLLM (recipe: $RECIPE) on 127.0.0.1:8000"
    if curl -sS --max-time 3 -H "Authorization: Bearer test" \
            http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
        echo "    already serving — leaving the current model in place"
    elif [ -n "$SERVE" ] && [ -x "$SERVE" ]; then
        "$SERVE" up "$RECIPE"
    else
        echo "ERROR: no vLLM server on 127.0.0.1:8000 and no bring-up script" >&2
        echo "       Set ATLAS_DGX_SERVE_SCRIPT to an executable launcher." >&2
        exit 1
    fi
}

# Which backend? Ask the server what it serves rather than inspecting the
# machine: both providers default to the same endpoint and differ only in
# model id, so reachability proves nothing — an OMLX-configured run on a DGX
# connects happily and then fails every call on a model vLLM does not serve.
if [ "$PROVIDER" = "auto" ]; then
    echo "==> Detecting LLM backend"
    # stdout carries the answer and is captured; -v explains on stderr, which
    # flows to the terminal so a wrong-model endpoint names what it does serve.
    if DETECTED="$(python -m contingency_atlas.provider_detect -v)" \
       && [ -n "$DETECTED" ]; then
        PROVIDER="$DETECTED"
        echo "    detected: $PROVIDER (its model is the one being served)"
    elif [ "$PLATFORM_DEFAULT" = "dgx" ] && [ -n "$SERVE" ] && [ -x "$SERVE" ]; then
        echo "    nothing served yet — starting the local backend, then retrying"
        bring_up_vllm
        PROVIDER="$(python -m contingency_atlas.provider_detect || true)"
        PROVIDER="${PROVIDER:-$PLATFORM_DEFAULT}"
        echo "    using: $PROVIDER"
    else
        PROVIDER="$PLATFORM_DEFAULT"
        echo "    undecided — falling back to the $(uname -s) default: $PROVIDER"
    fi
fi

if [ "$PROVIDER" = "dgx" ]; then
    bring_up_vllm
    # Pin the model from the server itself: dgx.model must match GET /v1/models,
    # and deriving it here keeps recipe switches from drifting the config.
    MODEL="$(curl -sS --max-time 5 -H "Authorization: Bearer test" \
        http://127.0.0.1:8000/v1/models \
        | python -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')"
    echo "==> Served model: $MODEL"
fi

echo "==> Running test suite"
python -m pytest

echo "==> Upgrading existing runtime store schemas"
stop_stale_dashboards
python scripts/migrate_runtime_stores.py

echo "==> Refreshing ontology guard snapshot (existing.ttl)"
python ontology/export_existing.py

# Exported after the test suite so tests stay deterministic regardless of the
# ambient backend. Detection resolved to a concrete provider above, so
# everything downstream is explicit — nothing else re-probes or re-guesses.
export ATLAS_LLM_PROVIDER="$PROVIDER"
if [ "$PROVIDER" = "dgx" ]; then
    export VLLM_MODEL="$MODEL"
    export VLLM_API_KEY="${VLLM_API_KEY:-test}"
    echo "    headless? tunnel with: ssh -L $PORT:127.0.0.1:$PORT <this-host>"
fi

echo "==> Starting local dashboard at http://127.0.0.1:$PORT (provider=$PROVIDER, Ctrl+C to stop)"
python -m contingency_atlas.web --port "$PORT"
