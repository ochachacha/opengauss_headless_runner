#!/usr/bin/env python3
"""
headless_runner.py

Continuous OpenGauss autoformalize runner for headless EC2.

Run via run_headless.sh which sets PYTHONPATH, GAUSS_PROJECT_ROOT, and
PTY_OUTPUT_LOG, then execs this script under the OpenGauss virtualenv.

Behaviour:
  - Resolves the /autoformalize session via resolve_autoformalize_request, which
    handles all Gauss staging (MCP config, lean4-skills plugin, startup context).
  - Spawns the session via SwarmManager.spawn_interactive (PTY-backed, same
    path as the interactive CLI).
  - Skips spawn if a session is already running  (Claude /loop "don't fire if
    busy" semantics).
  - Cancels sessions that have been PTY-silent for longer than IDLE_TIMEOUT
    (stuck / handed-off-to-human detection).
  - Tees PTY output to PTY_OUTPUT_LOG if set, so you can tail -f it from
    another SSH session.
  - Loops forever until SIGTERM/SIGINT or MAX_CYCLES is reached.

Required environment:
    GAUSS_PROJECT_ROOT   Path to the project root (where .gauss/project.yaml lives).
    AUTOFORMALIZE_ARGS   Arguments appended to /autoformalize.

Optional environment (see HEADLESS_RUNNER.md for full reference):
    IDLE_TIMEOUT_SECONDS, NUDGE_GRACE_SECONDS, NUDGE_MESSAGE,
    POLL_INTERVAL_SECONDS, FAILURE_BACKOFF_SECONDS, MAX_CYCLES,
    PTY_OUTPUT_LOG, STUCK_DETECT_DELAY_SECONDS
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Monkey-patch swarm_manager BEFORE it is imported anywhere else.
# Adds:
#   - last-output timestamps for idle/stuck detection
#   - optional PTY output tee to a log file (PTY_OUTPUT_LOG env var)
# ---------------------------------------------------------------------------
import swarm_manager as _sm_module

_last_output_at: dict[str, float] = {}   # task_id -> epoch seconds
_nudge_sent_at: dict[str, float] = {}   # task_id -> when we sent "continue"
_stuck_detected_at: dict[str, float] = {}  # task_id -> when stuck pattern first seen
_orig_remember = _sm_module._remember_recent_output

# Patterns in PTY output that indicate the session is stuck waiting for input
# (e.g. Claude Code hit the context/blocking limit).  The TUI may keep
# redrawing the screen, so silence-based idle detection never fires.
_STUCK_PATTERNS: list[bytes] = [
    b"Context limit reached",
    b"context limit reached",
    b"limit reached",
]

# How long a stuck pattern must persist before we nudge (seconds).
# Avoids reacting to transient messages that scroll past.
STUCK_DETECT_DELAY: int = int(os.environ.get("STUCK_DETECT_DELAY_SECONDS", "30"))

# Opened after the configuration block; None means no tee.
_pty_output_log = None


def _patched_remember(task, chunk: bytes) -> None:  # noqa: ANN001
    _orig_remember(task, chunk)
    if chunk:
        _last_output_at[task.task_id] = time.time()
        # Clear nudge state only if enough time has passed since the nudge
        # (ignore the PTY echo of our own "continue\r" which arrives within ~1s)
        nudge_time = _nudge_sent_at.get(task.task_id)
        if nudge_time is not None and (time.time() - nudge_time) > 5:
            _nudge_sent_at.pop(task.task_id, None)
        if _pty_output_log is not None:
            try:
                _pty_output_log.write(chunk)
                _pty_output_log.flush()
            except Exception:
                pass



_sm_module._remember_recent_output = _patched_remember

# ---------------------------------------------------------------------------

from swarm_manager import SwarmManager  # noqa: E402  (must come after patch)

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

# Path to the project root (where .gauss/project.yaml lives).
PROJECT_ROOT = Path(
    os.environ.get("GAUSS_PROJECT_ROOT", Path(__file__).resolve().parent.parent)
).expanduser().resolve()

# Arguments appended to /autoformalize.  MUST be set via environment or
# headless.conf — there is no project-specific default.
AUTOFORMALIZE_ARGS: str = os.environ.get("AUTOFORMALIZE_ARGS", "")
if not AUTOFORMALIZE_ARGS:
    print(
        "ERROR: AUTOFORMALIZE_ARGS is not set.  Set it in headless.conf or environment.",
        file=sys.stderr,
    )
    sys.exit(1)

# Seconds of PTY silence before a session is considered stuck and cancelled.
IDLE_TIMEOUT: int = int(
    float(os.environ.get("IDLE_TIMEOUT_SECONDS", "60"))
)

# Seconds between poll ticks while a session is running.
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))

# Seconds to wait before retrying after a failed session.
FAILURE_BACKOFF: int = int(os.environ.get("FAILURE_BACKOFF_SECONDS", "300"))

# Maximum number of sessions to run before exiting.  0 = run forever.
MAX_CYCLES: int = int(os.environ.get("MAX_CYCLES", "0"))

# Path to tee Claude's raw PTY output to.  Empty = disabled.
_pty_log_path: str = os.environ.get("PTY_OUTPUT_LOG", "")

# Message sent to the PTY when a session is idle (the nudge).
NUDGE_MESSAGE: str = os.environ.get(
    "NUDGE_MESSAGE", "continue with the most recommended action"
) + "\r"

# ---------------------------------------------------------------------------
# Load ~/.env file
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = "~/.env") -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file.  Skips comments and blank lines."""
    env_file = Path(path).expanduser()
    if not env_file.exists():
        return {}
    result: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")  # strip optional quotes
        if key:
            result[key] = value
    return result

# ---------------------------------------------------------------------------
# Open PTY output log if configured
# ---------------------------------------------------------------------------

if _pty_log_path:
    _pty_log_file = Path(_pty_log_path).expanduser().resolve()
    _pty_log_file.parent.mkdir(parents=True, exist_ok=True)
    _pty_output_log = open(_pty_log_file, "ab", buffering=0)  # noqa: SIM115

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("headless_runner")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_stop_event = threading.Event()


def _handle_signal(sig: int, _frame: object) -> None:
    log.info("Signal %s received — stopping after current session finishes", sig)
    _stop_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# SwarmManager helpers
# ---------------------------------------------------------------------------


def _running_tasks() -> list:
    """Return running tasks that belong to *this* project (by project_root)."""
    project = str(PROJECT_ROOT)
    return [
        t for t in SwarmManager().list_tasks(status="running")
        if t.project_root == project
    ]


def is_busy() -> bool:
    return bool(_running_tasks())


# How long to wait after sending "continue" before giving up and cancelling.
NUDGE_GRACE: int = int(os.environ.get("NUDGE_GRACE_SECONDS", "120"))


def _send_continue(task) -> bool:  # noqa: ANN001
    """Send NUDGE_MESSAGE to the task's PTY.  Returns True on success."""
    fd = task.pty_master_fd
    if fd is None:
        return False
    try:
        os.write(fd, NUDGE_MESSAGE.encode())
        return True
    except OSError as exc:
        log.warning("Failed to write to PTY for %s: %s", task.task_id, exc)
        return False


def _recent_output_text(task) -> str:  # noqa: ANN001
    """Return the last ~4KB of PTY output as a string for pattern matching."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return ""
    # Check the tail of the buffer (stuck messages appear at the end)
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return tail.decode("utf-8", errors="replace")


def _check_stuck_pattern(task) -> bool:  # noqa: ANN001
    """Return True if the PTY output contains a stuck-state pattern."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return False
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return any(pat in tail for pat in _STUCK_PATTERNS)


def _check_idle_timeout() -> None:
    """Nudge idle tasks with 'continue'; cancel if nudge didn't help.

    Two detection paths:
    1. Silence-based: PTY goes truly quiet for IDLE_TIMEOUT seconds.
    2. Stuck-pattern: PTY keeps redrawing but output contains a known
       stuck message (e.g. "Context limit reached").  The TUI noise
       prevents path 1 from firing, so we detect the pattern directly.
    """
    mgr = SwarmManager()
    now = time.time()
    for task in _running_tasks():
        nudge_time = _nudge_sent_at.get(task.task_id)

        # --- Path 2: stuck-pattern detection (bypasses silence timer) ---
        if _check_stuck_pattern(task):
            if task.task_id not in _stuck_detected_at:
                _stuck_detected_at[task.task_id] = now
                log.info(
                    "Task %s: stuck pattern detected in PTY output, "
                    "will nudge in %ds if it persists",
                    task.task_id, STUCK_DETECT_DELAY,
                )
            elif now - _stuck_detected_at[task.task_id] > STUCK_DETECT_DELAY:
                if nudge_time is None:
                    log.info(
                        "Task %s: stuck pattern persisted for %ds — sending 'continue'",
                        task.task_id,
                        int(now - _stuck_detected_at[task.task_id]),
                    )
                    if _send_continue(task):
                        _nudge_sent_at[task.task_id] = now
                    else:
                        log.warning("Task %s has no writable PTY — cancelling", task.task_id)
                        mgr.cancel(task.task_id)
                elif now - nudge_time > NUDGE_GRACE:
                    log.warning(
                        "Task %s still stuck %ds after nudge — cancelling",
                        task.task_id, int(now - nudge_time),
                    )
                    mgr.cancel(task.task_id)
                    _nudge_sent_at.pop(task.task_id, None)
                    _stuck_detected_at.pop(task.task_id, None)
                # Skip silence-based check — stuck-pattern path is handling it
                continue
        else:
            # Pattern gone — clear stuck state
            _stuck_detected_at.pop(task.task_id, None)

        # --- Path 1: silence-based idle detection ---
        last = _last_output_at.get(
            task.task_id,
            task.start_time or now,
        )
        silent_for = now - last
        if silent_for > IDLE_TIMEOUT:
            if nudge_time is None:
                # First time idle — send "continue" instead of killing
                log.info(
                    "Task %s silent for %.1fmin (> %.1fmin limit) — sending 'continue'",
                    task.task_id,
                    silent_for / 60,
                    IDLE_TIMEOUT / 60,
                )
                if _send_continue(task):
                    _nudge_sent_at[task.task_id] = now
                else:
                    # Can't write to PTY — cancel immediately
                    log.warning("Task %s has no writable PTY — cancelling", task.task_id)
                    mgr.cancel(task.task_id)

            elif now - nudge_time > NUDGE_GRACE:
                # We already nudged, but still no output — cancel
                log.warning(
                    "Task %s still silent %ds after 'continue' nudge — cancelling",
                    task.task_id,
                    int(now - nudge_time),
                )
                mgr.cancel(task.task_id)
                _nudge_sent_at.pop(task.task_id, None)



def _wait_for_task(task) -> str:  # noqa: ANN001
    """Block until task leaves 'running'.  Returns final status string."""
    while True:
        if _stop_event.is_set():
            log.info("Stop requested — cancelling task %s", task.task_id)
            SwarmManager().cancel(task.task_id)
            return "cancelled"

        _check_idle_timeout()

        current = SwarmManager().get_task(task.task_id)
        if current:
            log.info(
                "Task %s  status=%s  progress=%s  lean=%s",
                current.task_id, current.status,
                current.progress, current.lean_status,
            )
        if current and current.status != "running":
            return current.status if current else "unknown"

        _stop_event.wait(timeout=POLL_INTERVAL)

    return "unknown"  # unreachable

# ---------------------------------------------------------------------------
# Session launcher
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load Gauss CLI config the same way cli.py does."""
    try:
        from cli import load_cli_config  # type: ignore[import]
        return load_cli_config()
    except Exception as exc:
        log.warning("Could not import load_cli_config from cli.py (%s) — using {}", exc)
        return {}


def _spawn_session(config: dict) -> Optional[object]:
    """
    Resolve and spawn a new /autoformalize session.

    Uses resolve_autoformalize_request to stage all managed assets (MCP
    config, lean4-skills plugin, startup context) before handing off to
    SwarmManager.spawn_interactive — identical to what the interactive CLI
    does when you type /autoprove.

    Returns the SwarmTask on success, None on error.
    """
    from gauss_cli.autoformalize import (  # type: ignore[import]
        resolve_autoformalize_request,
        AutoformalizeError,
    )

    # Reload ~/.env each cycle so credential updates are picked up live.
    _dotenv = _load_dotenv()
    if _dotenv:
        log.info("Loaded %d env var(s) from ~/.env: %s", len(_dotenv), ", ".join(_dotenv))

    command = f"/autoformalize {AUTOFORMALIZE_ARGS}".strip()
    log.info("Resolving: %s  (project: %s)", command, PROJECT_ROOT)

    try:
        plan = resolve_autoformalize_request(
            command,
            config,
            active_cwd=str(PROJECT_ROOT),
        )
    except AutoformalizeError as exc:
        log.error("resolve_autoformalize_request failed: %s", exc)
        return None
    except Exception as exc:
        log.error("Unexpected error during autoformalize resolution: %s", exc, exc_info=True)
        return None

    # Pre-confirm the bypass permissions dialog in the managed HOME.
    # autoformalize.py overrides HOME to backend_home, so the real ~/.claude.json
    # is invisible to the claude subprocess — we must write the confirmation there.
    import json as _json
    backend_home = plan.managed_context.backend_home
    backend_home.mkdir(parents=True, exist_ok=True)

    # 1) Write project trust flags into .claude.json
    claude_json_path = backend_home / ".claude.json"
    try:
        _cj = _json.loads(claude_json_path.read_text()) if claude_json_path.exists() else {}
    except Exception:
        _cj = {}
    _projects = _cj.setdefault("projects", {})
    _pkey = str(plan.project.root.resolve())
    _pentry = _projects.setdefault(_pkey, {})
    _pentry["hasTrustDialogAccepted"] = True
    _pentry["hasTrustDialogHooksAccepted"] = True
    _pentry.setdefault("allowedTools", [])
    claude_json_path.write_text(_json.dumps(_cj, indent=2))
    log.info("Pre-confirmed project trust in %s", claude_json_path)

    # 2) Write skipDangerousModePermissionPrompt into settings.json so the
    #    bypass-permissions TUI confirmation dialog is suppressed.
    settings_dir = backend_home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    try:
        _sd = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        _sd = {}
    _sd["skipDangerousModePermissionPrompt"] = True
    # Mirror the project's permissions.allow so tools are auto-approved even
    # when HOME is overridden to backend_home and the project settings.local.json
    # isn't resolved.
    _sd.setdefault("permissions", {})["allow"] = ["*"]
    _sd["permissions"]["deny"] = ["AskUserQuestion"]
    settings_path.write_text(_json.dumps(_sd, indent=2) + "\n")
    log.info("Wrote permissions + skipDangerousModePermissionPrompt to %s", settings_path)

    hr = plan.handoff_request
    argv = list(hr.argv)

    mgr = SwarmManager()

    # Truncate PTY output log at the start of each session so it doesn't
    # grow unboundedly. tail -f still works since the fd stays open.
    if _pty_output_log is not None:
        try:
            _pty_output_log.seek(0)
            _pty_output_log.truncate()
        except Exception:
            pass

    log.info(
        "Spawning %s session  cwd=%s  argv=%s",
        plan.workflow_kind,
        hr.cwd,
        " ".join(str(a) for a in argv[:7]),
    )

    # Merge ~/.env vars into the subprocess environment.
    spawn_env = dict(hr.env)
    spawn_env.update(_dotenv)

    # Forward model/provider env vars into the child.  Gauss's
    # autoformalize resolver strips some auth keys and hardcodes
    # --model claude-opus-4-6.  When an alternative provider is
    # configured (e.g. via ~/qwen.env or ~/openrouter.env loaded by
    # run_tmux.sh), we must (a) re-inject the env vars so the Claude
    # Code CLI sees the right endpoint/credentials, and (b) rewrite
    # the --model flag in argv to match ANTHROPIC_MODEL.
    _FORWARD_KEYS = (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
    for key in _FORWARD_KEYS:
        val = os.environ.get(key)
        if val is not None:
            spawn_env[key] = val

    # Rewrite --model in argv if ANTHROPIC_MODEL is set.
    override_model = os.environ.get("ANTHROPIC_MODEL")
    if override_model:
        # Prevent "Context limit reached" infinite compaction loops on
        # smaller-context models.  Claude Code hardcodes 200K context for
        # unrecognised models (effective window ~180K after output-token
        # reservation).  Setting the blocking limit to 160K ensures
        # compaction fires early enough that the compacted result fits.
        # Not needed for native Claude models which have 1M context.
        spawn_env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] = "160000"
        for i, arg in enumerate(argv):
            if arg == "--model" and i + 1 < len(argv):
                log.info("Overriding --model %s → %s", argv[i + 1], override_model)
                argv[i + 1] = override_model
                break

    try:
        task = mgr.spawn_interactive(
            theorem="(headless continuous run)",
            description=f"headless {plan.workflow_kind}",
            argv=argv,
            cwd=hr.cwd,
            env=spawn_env,
            workflow_kind=plan.workflow_kind,
            workflow_command=plan.backend_command,
            project_name=plan.project.name,
            project_root=str(plan.project.root),
        )
    except Exception as exc:
        log.error("spawn_interactive failed: %s", exc, exc_info=True)
        return None

    log.info(
        "Spawned task %s%s",
        task.task_id,
        f"  (PTY output → {_pty_log_path})" if _pty_log_path else "",
    )
    return task

# ---------------------------------------------------------------------------
# Gauss self-update
# ---------------------------------------------------------------------------


def _run_gauss_update() -> None:
    """Run `gauss update` between cycles to pick up new Gauss releases."""
    gauss_exe = shutil.which("gauss")
    if not gauss_exe:
        log.warning("gauss executable not found — skipping self-update")
        return
    log.info("Running gauss update …")
    try:
        result = subprocess.run(
            [gauss_exe, "update"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log.info("gauss update succeeded")
        else:
            log.warning("gauss update exited %d: %s", result.returncode,
                        (result.stderr or result.stdout or "").strip()[:500])
    except subprocess.TimeoutExpired:
        log.warning("gauss update timed out after 300s")
    except Exception as exc:
        log.warning("gauss update failed: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    config = _load_config()
    cycles = 0

    log.info(
        "Headless runner started  project=%s  args='%s'  "
        "idle_timeout=%.2fh  poll=%ds  max_cycles=%s%s",
        PROJECT_ROOT,
        AUTOFORMALIZE_ARGS,
        IDLE_TIMEOUT / 3600,
        POLL_INTERVAL,
        MAX_CYCLES if MAX_CYCLES else "∞",
        f"  pty_log={_pty_log_path}" if _pty_log_path else "",
    )

    while not _stop_event.is_set():

        # ── busy-check (Claude /loop "don't fire if busy" semantics) ────────
        if is_busy():
            log.debug("Session already running — skipping spawn")
            _stop_event.wait(timeout=POLL_INTERVAL)
            continue

        # ── cycle limit ──────────────────────────────────────────────────────
        if MAX_CYCLES and cycles >= MAX_CYCLES:
            log.info("Reached max_cycles=%d — exiting", MAX_CYCLES)
            break

        # ── spawn ────────────────────────────────────────────────────────────
        task = _spawn_session(config)
        if task is None:
            log.warning(
                "Session could not be spawned — retrying in %ds", FAILURE_BACKOFF
            )
            _stop_event.wait(timeout=FAILURE_BACKOFF)
            continue

        # ── wait ─────────────────────────────────────────────────────────────
        status = _wait_for_task(task)
        cycles += 1
        log.info(
            "Task %s finished  status=%s  cycle=%d", task.task_id, status, cycles
        )

        if _stop_event.is_set():
            break

        # ── back-off on failure ───────────────────────────────────────────────
        if status == "failed":
            log.warning(
                "Session failed — waiting %ds before retry", FAILURE_BACKOFF
            )
            _stop_event.wait(timeout=FAILURE_BACKOFF)

        # On "complete" or "cancelled" (idle-timeout), loop immediately.

        # ── self-update Gauss between cycles ──────────────────────────────
        _run_gauss_update()

    log.info("Headless runner stopped after %d cycle(s)", cycles)
    if _pty_output_log is not None:
        _pty_output_log.close()


if __name__ == "__main__":
    main()
