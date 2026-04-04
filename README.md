# Headless Autoformalize Runner

Continuous [OpenGauss](https://github.com/opengauss) autoformalize runner for headless EC2 instances.  Spawns PTY-backed Claude Code sessions, monitors for idle/stuck states, nudges unresponsive sessions, and loops until stopped.

## Setup

Add this repo as a git submodule inside your project:

```bash
cd ~/my-lean-project
git submodule add <repo-url> headless
```

Create a `headless.conf` in your project root (copy from `headless/headless.conf.example`):

```bash
cp headless/headless.conf.example headless.conf
# Edit headless.conf with your project-specific settings
```

Optionally add thin wrappers in your project root for convenience:

```bash
# run_tmux.sh
#!/usr/bin/env bash
exec "$(dirname "$0")/headless/run_tmux.sh" "$@"
```

## Scripts

| Script | Purpose |
|---|---|
| `run_tmux.sh` | Main entry point. Manages a tmux session that runs the headless loop. |
| `run_headless.sh` | Thin wrapper that sets `PYTHONPATH`, `GAUSS_PROJECT_ROOT`, and `PTY_OUTPUT_LOG`, then execs `headless_runner.py` under the OpenGauss virtualenv. |
| `headless_runner.py` | Core loop. Resolves `/autoformalize` via OpenGauss, spawns PTY-backed Claude Code sessions, monitors for idle timeouts, nudges stuck sessions, and loops until stopped. |
| `render_pty.py` | Utility to render the last screen state from a raw PTY log file (uses `pyte` virtual terminal emulation). Usage: `render_pty.py <logfile> [rows] [cols]`. |

## `headless.conf`

Project-specific configuration file, sourced by the shell scripts.  Must live in the project root (parent of the `headless/` directory).

```bash
# Required
SESSION_NAME="myproject-autoprove"
AUTOFORMALIZE_ARGS="--source=reference/paper.pdf --max-cycles=200 ..."

# Optional
DEFAULT_MODEL="claude"        # default model; override with --model
PTY_OUTPUT_LOG="$HOME/my_output.log"  # default: ~/${SESSION_NAME}_output.log
```

## `run_tmux.sh` usage

```
./headless/run_tmux.sh [--model <name>] <command>
```

**Options:**
- `--model <name>` — Model/provider to use (default: `DEFAULT_MODEL` from `headless.conf`). Loads `~/<name>.env` for environment variables (API keys, base URLs, model overrides). Examples: `claude`, `qwen`.

**Commands:**

| Command | Description |
|---|---|
| `start` | Create a tmux session and launch the headless autoprove loop. |
| `attach` | Attach to the running session (`Ctrl-b d` to detach). |
| `stop` | Kill the tmux session. |
| `status` | Check if the session is running (prints pane PIDs). |
| `restart` | Stop then start. |
| `log` | Tail the runner log (`headless_runner.py` stdout). |
| `view` | Tail the live Claude PTY output. |

## Environment variable reference

### `headless_runner.py` configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `GAUSS_PROJECT_ROOT` | Parent of `headless/` directory | Path to the Lean project root. |
| `AUTOFORMALIZE_ARGS` | *(required, from `headless.conf`)* | Arguments appended to `/autoformalize`. |
| `IDLE_TIMEOUT_SECONDS` | `60` | Seconds of PTY silence before nudging a session. |
| `STUCK_DETECT_DELAY_SECONDS` | `30` | Seconds a stuck pattern must persist before nudging. |
| `NUDGE_GRACE_SECONDS` | `120` | Seconds after a nudge before cancelling a still-stuck session. |
| `NUDGE_MESSAGE` | `continue with the most recommended action` | Text sent to the PTY when a session is idle. |
| `POLL_INTERVAL_SECONDS` | `15` | Seconds between status polls while a session is running. |
| `FAILURE_BACKOFF_SECONDS` | `300` | Seconds to wait before retrying after a failed session. |
| `MAX_CYCLES` | `0` (infinite) | Maximum number of sessions to run before exiting. |
| `PTY_OUTPUT_LOG` | *(set by `run_headless.sh`)* | Path to tee raw Claude PTY output to. |

### Model/provider env files (`~/<model>.env`)

`run_tmux.sh --model <name>` sources `~/<name>.env` via bash `source`, making all variables available to `run_headless.sh` and `headless_runner.py`. Additionally, `headless_runner.py` re-reads `~/.env` each cycle via its own simple parser (supports `KEY=VALUE` lines, no shell expansion).

### Forwarded keys

`headless_runner.py` explicitly forwards these variables from its own environment into the spawned Claude Code subprocess:

- `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`
- `CLAUDE_CODE_SUBAGENT_MODEL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`

### Non-Claude model handling

When `ANTHROPIC_MODEL` is set (indicating a non-Claude model), `headless_runner.py` automatically:
- Sets `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE=160000` to prevent compaction loops on smaller-context models.
- Rewrites the `--model` flag in the Claude Code argv.

## Idle/stuck detection

The runner has two detection paths:

1. **Silence-based**: PTY goes truly quiet for `IDLE_TIMEOUT_SECONDS` → sends nudge → if still silent after `NUDGE_GRACE_SECONDS` → cancels.
2. **Stuck-pattern**: PTY keeps redrawing (TUI noise) but output contains a known stuck message (e.g. "Context limit reached"). Bypasses the silence timer and nudges after `STUCK_DETECT_DELAY_SECONDS`.

## Self-update

Between cycles, the runner executes `gauss update` to pick up new OpenGauss releases automatically.

## Authentication — long-lived Max token

By default, Claude Code OAuth credentials (access token + refresh token) expire
relatively quickly.  For unattended multi-day sessions you need a **long-lived
refresh token** so Claude Code can silently re-authenticate.

### Generate a long-lived token

```bash
claude setup-token
```

This is interactive — follow the prompts.  It writes a long-lived refresh token
into `~/.claude/.credentials.json` that still bills against your Claude Max
subscription (not API credits).

### Keep Gauss auth mode on `auto`

In `~/.gauss/config.yaml`:

```yaml
gauss:
  autoformalize:
    auth_mode: auto
```

In `auto` mode, Gauss copies `~/.claude/.credentials.json` (including the
long-lived refresh token) into the managed Claude home.  Claude Code then
auto-refreshes the access token whenever it expires — no more 401s.

> **Why not `api-key` mode?**  Despite the name, `api-key` mode passes the
> token via the `ANTHROPIC_TOKEN` env var.  Claude Code does not recognise
> this env var — it reads `ANTHROPIC_API_KEY` (for real API keys) or
> `CLAUDE_CODE_OAUTH_TOKEN` (for OAuth tokens via env).  The env var also
> doesn't carry a refresh token, so when the access token expires, there's
> no way to renew it.  Stick with `auto`.

If after weeks the refresh token itself expires, just re-run
`claude setup-token` to get a new one.

## Troubleshooting

**OAuth token expired (401 error):**
Re-run `claude setup-token` to get a fresh long-lived refresh token.
Make sure `auth_mode: auto` in `~/.gauss/config.yaml`.

**Session keeps getting cancelled as "stuck":**
Increase `IDLE_TIMEOUT_SECONDS`.  Long Lean compilations can be silent for
minutes.

**`run_tmux.sh view` shows old/stale output:**
The PTY log is truncated at the start of each new session.  If the session
just restarted, the log may be very short.  Wait a moment and try again.
