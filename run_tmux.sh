#!/usr/bin/env bash
# Generic tmux wrapper for the headless autoformalize runner.
# Expects to live inside a submodule directory (e.g. <project>/headless/).
# Sources <project>/headless.conf for project-specific settings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$PROJECT_ROOT/headless.conf"

# Source project config early to get defaults (SESSION_NAME, DEFAULT_MODEL, etc.)
if [[ -f "$CONF" ]]; then
    # shellcheck disable=SC1090
    source "$CONF"
else
    echo "ERROR: $CONF not found. Create it from headless/headless.conf.example" >&2
    exit 1
fi

MODEL="${DEFAULT_MODEL:-claude}"  # override with --model <name>

# Parse --model flag before subcommand
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            MODEL="$2"; shift 2 ;;
        --model=*)
            MODEL="${1#--model=}"; shift ;;
        *)
            break ;;
    esac
done

# Source env file for the chosen model (e.g. ~/claude.env, ~/qwen.env)
ENV_FILE="$HOME/${MODEL}.env"
if [[ -f "$ENV_FILE" ]]; then
    echo "Loading environment from $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    if [[ "$MODEL" != "claude" ]]; then
        echo "Warning: env file $ENV_FILE not found" >&2
    fi
fi

: "${SESSION_NAME:=headless-autoprove}"
: "${PTY_OUTPUT_LOG:=$HOME/${SESSION_NAME}_output.log}"
GAUSS_LOG="$PTY_OUTPUT_LOG"
RUNNER_LOG="$HOME/${SESSION_NAME}_runner.log"
HEADLESS_SCRIPT="$SCRIPT_DIR/run_headless.sh"
RENDER_SCRIPT="$SCRIPT_DIR/render_pty.py"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--model <name>] <command>

Options:
  --model <name>  Model to use (default: $DEFAULT_MODEL). Loads ~/\<name\>.env
                  for environment variables. Examples: claude, qwen

Commands:
  start    Create a tmux session and run the headless autoprove loop
  attach   Attach to the running session (Ctrl-b d to detach)
  stop     Kill the tmux session
  status   Check if the session is running
  restart  Stop then start
  log      Tail the runner log (headless_runner.py output)
  view     Tail the live Claude PTY output

Runner log: $RUNNER_LOG
PTY log:    $GAUSS_LOG
EOF
    exit 1
}

cmd_start() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is already running."
        echo "  attach:  $0 attach"
        echo "  restart: $0 restart"
        exit 1
    fi

    : > "$RUNNER_LOG"  # truncate runner log (PTY log is managed by headless_runner.py)

    # Source the env file inside the tmux session so the spawned shell
    # inherits the correct API keys (tmux starts a fresh shell).
    local tmux_cmd=""
    if [[ -f "$ENV_FILE" ]]; then
        tmux_cmd="set -a; source '$ENV_FILE'; set +a; "
    fi
    tmux_cmd+="$HEADLESS_SCRIPT 2>&1 | tee -a $RUNNER_LOG"

    tmux new-session -d -s "$SESSION_NAME" \
        "$tmux_cmd"

    echo "Started tmux session '$SESSION_NAME'"
    echo "  attach:    $0 attach"
    echo "  PTY output: $GAUSS_LOG"
    echo "  view:      $0 view"
    echo "  runner log: $RUNNER_LOG"
}

cmd_attach() {
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "No session '$SESSION_NAME' found. Run: $0 start"
        exit 1
    fi
    exec tmux attach-session -t "$SESSION_NAME"
}

cmd_stop() {
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "No session '$SESSION_NAME' found."
        exit 0
    fi
    tmux kill-session -t "$SESSION_NAME"
    echo "Stopped session '$SESSION_NAME'"
}

cmd_status() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is running."
        tmux list-panes -t "$SESSION_NAME" -F '  pid=#{pane_pid}  active=#{pane_active}' 2>/dev/null || true
    else
        echo "Session '$SESSION_NAME' is not running."
    fi
}

cmd_log() {
    exec tail -f "$RUNNER_LOG"
}

cmd_view() {
    if [[ ! -f "$GAUSS_LOG" ]]; then
        echo "No PTY log found at $GAUSS_LOG"
        exit 1
    fi
    exec tail -f -n +1 "$GAUSS_LOG"
}

[[ $# -lt 1 ]] && usage

case "$1" in
    start)   cmd_start ;;
    attach)  cmd_attach ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    restart) cmd_stop; sleep 1; cmd_start ;;
    log)     cmd_log ;;
    view)    cmd_view ;;
    *)       usage ;;
esac
