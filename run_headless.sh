#!/usr/bin/env bash
# Generic headless runner launcher.
# Expects to live inside a submodule directory (e.g. <project>/headless/).
# Sources <project>/headless.conf for project-specific settings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
: "${OPENGAUSS_DIR:=$HOME/OpenGauss}"

# Source project-specific config (SESSION_NAME, AUTOFORMALIZE_ARGS, etc.)
CONF="$PROJECT_ROOT/headless.conf"
if [[ -f "$CONF" ]]; then
    # shellcheck disable=SC1090
    source "$CONF"
else
    echo "ERROR: $CONF not found. Create it from headless/headless.conf.example" >&2
    exit 1
fi

# Default PTY output log if not set by headless.conf
: "${PTY_OUTPUT_LOG:=$HOME/${SESSION_NAME:-headless}_output.log}"

cd "$OPENGAUSS_DIR"

exec env GAUSS_PROJECT_ROOT="$PROJECT_ROOT" \
	PYTHONPATH="$OPENGAUSS_DIR${PYTHONPATH:+:$PYTHONPATH}" \
	PTY_OUTPUT_LOG="$PTY_OUTPUT_LOG" \
	AUTOFORMALIZE_ARGS="${AUTOFORMALIZE_ARGS:-}" \
	HEADLESS_MODE="${HEADLESS_MODE:-full}" \
    "$OPENGAUSS_DIR/venv/bin/python" \
    "$SCRIPT_DIR/headless_runner.py" \
    "$@"
