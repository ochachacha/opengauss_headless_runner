#!/usr/bin/env python3
"""
headless_runner.py

Continuous OpenGauss autoformalize runner for headless EC2, with an
audit-fix loop between autoformalize cycles.

Run via run_headless.sh which sets PYTHONPATH, GAUSS_PROJECT_ROOT, and
PTY_OUTPUT_LOG, then execs this script under the OpenGauss virtualenv.

Behaviour:
  Phase 1 — Autoformalize:
    Resolves the /autoformalize session via resolve_autoformalize_request,
    spawns it via SwarmManager.spawn_interactive (PTY-backed), and waits
    for it to finish or be cancelled.

  Phase 2 — Audit-fix loop:
    Spawns a plain `claude` audit agent that reads all Lean source and
    writes a structured report to audit/latest.md with STATUS: PASS/FAIL.
    If FAIL, spawns a Gauss-staged fix agent (with lean4-skills) directed
    by the audit report. Repeats up to MAX_AUDIT_FIX_CYCLES times or
    until the audit passes.

  Phase 3 — Back to Phase 1.

Required environment:
    GAUSS_PROJECT_ROOT   Path to the project root (where .gauss/project.yaml lives).
    AUTOFORMALIZE_ARGS   Arguments appended to /autoformalize.

Optional environment (see README.md for full reference):
    IDLE_TIMEOUT_SECONDS, NUDGE_GRACE_SECONDS, NUDGE_MESSAGE,
    POLL_INTERVAL_SECONDS, FAILURE_BACKOFF_SECONDS, MAX_CYCLES,
    PTY_OUTPUT_LOG, STUCK_DETECT_DELAY_SECONDS,
    MAX_AUDIT_FIX_CYCLES, AUDIT_IDLE_TIMEOUT_SECONDS
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
_task_session_type: dict[str, str] = {}  # task_id -> "autoformalize" | "audit" | "fix"
_done_latched: dict[str, float] = {}  # task_id -> when done pattern first seen (sticky)
_orig_remember = _sm_module._remember_recent_output

# Patterns in PTY output that indicate the session is stuck waiting for input
# (e.g. Claude Code hit the context/blocking limit).  The TUI may keep
# redrawing the screen, so silence-based idle detection never fires.
_STUCK_PATTERNS: list[bytes] = [
    b"Context limit reached",
    b"context limit reached",
    b"limit reached",
]

# Subset of stuck patterns that indicate context exhaustion — these need
# /compact, not a regular nudge (which would just add to the full context).
_CONTEXT_LIMIT_PATTERNS: list[bytes] = [
    b"Context limit reached",
    b"context limit reached",
    b"limit reached",
]

# Pattern the autoformalize agent emits when it wants to hand off.
# The agent should print this exact phrase when it is done.
DONE_HANDOFF_PHRASE: str = os.environ.get(
    "DONE_HANDOFF_PHRASE", "HEADLESS_RUNNER_HANDOFF_DONE"
)
_DONE_PATTERNS: list[bytes] = [DONE_HANDOFF_PHRASE.encode()]

# How long a stuck pattern must persist before we nudge (seconds).
# Avoids reacting to transient messages that scroll past.
STUCK_DETECT_DELAY: int = int(os.environ.get("STUCK_DETECT_DELAY_SECONDS", "30"))

# After the done pattern is seen, how long the PTY must be silent before
# we treat the handoff as complete.  This allows subagents to finish.
DONE_SILENCE: int = int(os.environ.get("DONE_SILENCE_SECONDS", "30"))

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

HEADLESS_DIR = Path(__file__).resolve().parent

# Run mode: "full" (default), "audit", or "fix".
# "full" runs the normal autoformalize → audit-fix loop.
# "audit" skips the first autoformalize and starts at the audit phase,
#         then continues the normal loop (audit-fix → autoformalize → ...).
# "fix" skips the first autoformalize and audit, starts at the fix phase
#       (assumes an audit report already exists), then continues the normal
#       loop (fix → audit → fix? → autoformalize → ...).
HEADLESS_MODE: str = os.environ.get("HEADLESS_MODE", "full").strip().lower()
if HEADLESS_MODE not in ("full", "audit", "fix"):
    print(
        f"ERROR: HEADLESS_MODE must be 'full', 'audit', or 'fix' (got '{HEADLESS_MODE}').",
        file=sys.stderr,
    )
    sys.exit(1)

# Arguments appended to /autoformalize.  Required in "full" mode;
# not needed for "audit" or "fix" modes.
AUTOFORMALIZE_ARGS: str = os.environ.get("AUTOFORMALIZE_ARGS", "")
if not AUTOFORMALIZE_ARGS and HEADLESS_MODE == "full":
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

# Maximum number of autoformalize cycles to run before exiting.  0 = run forever.
MAX_CYCLES: int = int(os.environ.get("MAX_CYCLES", "0"))


# Path to tee Claude's raw PTY output to.  Empty = disabled.
_pty_log_path: str = os.environ.get("PTY_OUTPUT_LOG", "")

# Message sent to the PTY when a session is idle (the nudge).
NUDGE_MESSAGE: str = os.environ.get(
    "NUDGE_MESSAGE", "continue with the most recommended action"
) + "\r"

# Prompt file paths (relative to headless/ dir).
AUDIT_PROMPT_PATH: Path = Path(
    os.environ.get("AUDIT_PROMPT_PATH", str(HEADLESS_DIR / "audit_prompt.md"))
)
FIX_PROMPT_PATH: Path = Path(
    os.environ.get("FIX_PROMPT_PATH", str(HEADLESS_DIR / "fix_prompt.md"))
)

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


def _is_context_limit(task) -> bool:  # noqa: ANN001
    """Return True if the stuck state is specifically a context limit."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return False
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return any(pat in tail for pat in _CONTEXT_LIMIT_PATTERNS)


def _check_done_pattern(task) -> bool:  # noqa: ANN001
    """Return True if the PTY output contains the done/handoff pattern."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return False
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return any(pat in tail for pat in _DONE_PATTERNS)


def _check_idle_timeout() -> None:
    """Monitor running tasks for idle, stuck, and done states.

    Behaviour depends on session type:
    - autoformalize: nudge on idle/stuck, cancel on done signal.
    - audit/fix: never nudge (they must exit on their own). Cancel only
      on hard idle timeout as a safety net.

    Detection paths:
    0. Done-pattern: agent emitted DONE_HANDOFF_PHRASE → cancel gracefully.
    1. Silence-based: PTY goes truly quiet for IDLE_TIMEOUT seconds.
    2. Stuck-pattern: PTY keeps redrawing but output contains a known
       stuck message (e.g. "Context limit reached").
    """
    mgr = SwarmManager()
    now = time.time()
    for task in _running_tasks():
        session_type = _task_session_type.get(task.task_id, "autoformalize")
        nudge_time = _nudge_sent_at.get(task.task_id)
        is_nudgeable = session_type == "autoformalize"

        # --- Path 0: done-pattern detection (all session types) ---
        # Latch: once the done phrase appears anywhere in the buffer,
        # remember it permanently for this task (even if later output
        # scrolls it out of the 4KB tail).
        if task.task_id not in _done_latched and _check_done_pattern(task):
            _done_latched[task.task_id] = now
            log.info(
                "Task %s (%s): done pattern detected — waiting for %ds of "
                "silence before handoff",
                task.task_id, session_type, DONE_SILENCE,
            )

        if task.task_id in _done_latched:
            # Check silence: time since last PTY output.
            last_output = _last_output_at.get(task.task_id, now)
            silent_since_done = now - last_output
            if silent_since_done >= DONE_SILENCE:
                log.info(
                    "Task %s (%s): silent for %ds after done signal — "
                    "cancelling (clean handoff)",
                    task.task_id, session_type, int(silent_since_done),
                )
                mgr.cancel(task.task_id)
                _done_latched.pop(task.task_id, None)
            # While latched but not yet silent, skip nudge/stuck checks —
            # the agent is wrapping up.
            continue

        # --- Path 2: stuck-pattern detection (bypasses silence timer) ---
        if _check_stuck_pattern(task):
            if task.task_id not in _stuck_detected_at:
                _stuck_detected_at[task.task_id] = now
                log.info(
                    "Task %s (%s): stuck pattern detected in PTY output, "
                    "will %s in %ds if it persists",
                    task.task_id, session_type,
                    "nudge" if is_nudgeable else "cancel",
                    STUCK_DETECT_DELAY,
                )
            elif now - _stuck_detected_at[task.task_id] > STUCK_DETECT_DELAY:
                if not is_nudgeable:
                    # audit/fix: no nudging, just cancel on stuck
                    log.warning(
                        "Task %s (%s): stuck for %ds — cancelling (no nudge for %s)",
                        task.task_id, session_type,
                        int(now - _stuck_detected_at[task.task_id]), session_type,
                    )
                    mgr.cancel(task.task_id)
                    _stuck_detected_at.pop(task.task_id, None)
                elif nudge_time is None:
                    # autoformalize: nudge
                    if _is_context_limit(task):
                        msg = "/compact\r"
                        label = "/compact"
                    else:
                        msg = NUDGE_MESSAGE
                        label = "continue"
                    log.info(
                        "Task %s: stuck pattern persisted for %ds — sending '%s'",
                        task.task_id,
                        int(now - _stuck_detected_at[task.task_id]),
                        label,
                    )
                    fd = task.pty_master_fd
                    if fd is not None:
                        try:
                            os.write(fd, msg.encode())
                            _nudge_sent_at[task.task_id] = now
                        except OSError as exc:
                            log.warning("Failed to write to PTY for %s: %s", task.task_id, exc)
                            mgr.cancel(task.task_id)
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
            if not is_nudgeable:
                # audit/fix: cancel directly on hard timeout
                log.warning(
                    "Task %s (%s) silent for %.1fmin — cancelling (no nudge for %s)",
                    task.task_id, session_type,
                    silent_for / 60, session_type,
                )
                mgr.cancel(task.task_id)
            elif nudge_time is None:
                # autoformalize: nudge first
                log.info(
                    "Task %s silent for %.1fmin (> %.1fmin limit) — sending 'continue'",
                    task.task_id,
                    silent_for / 60,
                    IDLE_TIMEOUT / 60,
                )
                if _send_continue(task):
                    _nudge_sent_at[task.task_id] = now
                else:
                    log.warning("Task %s has no writable PTY — cancelling", task.task_id)
                    mgr.cancel(task.task_id)
            elif now - nudge_time > NUDGE_GRACE:
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
# Session launcher (Gauss-staged)
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    """Load Gauss CLI config the same way cli.py does."""
    try:
        from cli import load_cli_config  # type: ignore[import]
        return load_cli_config()
    except Exception as exc:
        log.warning("Could not import load_cli_config from cli.py (%s) — using {}", exc)
        return {}


def _spawn_gauss_session(
    config: dict,
    command: str,
    *,
    prompt_override: str | None = None,
    prompt_suffix: str | None = None,
    description_label: str = "",
) -> Optional[object]:
    """
    Resolve and spawn a Gauss-managed session.

    Uses resolve_autoformalize_request to stage all managed assets (MCP
    config, lean4-skills plugin, startup context) before handing off to
    SwarmManager.spawn_interactive.

    Args:
        config: Gauss CLI config dict.
        command: The workflow command string (e.g. "/autoformalize ...",
                 "/prove fix issues").
        prompt_override: If set, completely replaces the startup prompt
                         (the last element of argv).
        prompt_suffix: If set (and prompt_override is None), appended to
                       the default startup prompt.
        description_label: Human-readable label for logs.

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
    import json as _json
    backend_home = plan.managed_context.backend_home
    backend_home.mkdir(parents=True, exist_ok=True)

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

    settings_dir = backend_home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    try:
        _sd = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        _sd = {}
    _sd["skipDangerousModePermissionPrompt"] = True
    _sd.setdefault("permissions", {})["allow"] = ["*"]
    _sd["permissions"]["deny"] = ["AskUserQuestion"]
    settings_path.write_text(_json.dumps(_sd, indent=2) + "\n")
    log.info("Wrote permissions + skipDangerousModePermissionPrompt to %s", settings_path)

    hr = plan.handoff_request
    argv = list(hr.argv)

    mgr = SwarmManager()

    # Truncate PTY output log at the start of each session.
    if _pty_output_log is not None:
        try:
            _pty_output_log.seek(0)
            _pty_output_log.truncate()
        except Exception:
            pass

    label = description_label or plan.workflow_kind
    log.info(
        "Spawning %s session  cwd=%s  argv=%s",
        label,
        hr.cwd,
        " ".join(str(a) for a in argv[:7]),
    )

    # Merge ~/.env vars into the subprocess environment.
    spawn_env = dict(hr.env)
    spawn_env.update(_dotenv)

    # Forward model/provider env vars into the child.
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
        spawn_env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] = "160000"
        for i, arg in enumerate(argv):
            if arg == "--model" and i + 1 < len(argv):
                log.info("Overriding --model %s → %s", argv[i + 1], override_model)
                argv[i + 1] = override_model
                break

    # Set the startup prompt.
    if prompt_override is not None and argv:
        argv[-1] = prompt_override
    elif prompt_suffix is not None and argv:
        argv[-1] = argv[-1] + prompt_suffix
    elif argv:
        argv[-1] = (
            argv[-1]
            + "\n\nRead the project's CLAUDE.md for detailed instructions before starting work."
        )

    try:
        task = mgr.spawn_interactive(
            theorem="(headless continuous run)",
            description=f"headless {label}",
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

    _task_session_type[task.task_id] = description_label or "autoformalize"
    log.info(
        "Spawned task %s (%s)%s",
        task.task_id,
        _task_session_type[task.task_id],
        f"  (PTY output → {_pty_log_path})" if _pty_log_path else "",
    )
    return task


# ---------------------------------------------------------------------------
# Phase-specific session spawners
# ---------------------------------------------------------------------------


def _spawn_autoformalize_session(config: dict) -> Optional[object]:
    """Phase 1: spawn the autoformalize session."""
    command = f"/autoformalize {AUTOFORMALIZE_ARGS}".strip()
    handoff_instruction = (
        "\n\nRead the project's CLAUDE.md for detailed instructions before starting work."
        f"\n\nWhen you are done and want to hand off to the audit agent, "
        f"print exactly this phrase on its own line: {DONE_HANDOFF_PHRASE}"
    )
    return _spawn_gauss_session(
        config,
        command,
        prompt_suffix=handoff_instruction,
        description_label="autoformalize",
    )


def _spawn_audit_session() -> Optional[str]:
    """Phase 2a: spawn a plain `claude` audit agent (no Gauss staging).

    Spawns `claude` via SwarmManager.spawn_interactive (same PTY
    infrastructure as other sessions, so output is teed to PTY_OUTPUT_LOG).
    The audit agent reads all Lean source, writes a timestamped report
    to audit/, and exits.

    Returns the final status: "pass", "fail", or "error".
    """
    claude_exe = shutil.which("claude")
    if not claude_exe:
        log.error("claude executable not found — cannot run audit")
        return "error"

    if not AUDIT_PROMPT_PATH.exists():
        log.error("Audit prompt not found at %s", AUDIT_PROMPT_PATH)
        return "error"
    audit_prompt = AUDIT_PROMPT_PATH.read_text(encoding="utf-8")

    # Ensure audit/ directory exists.
    audit_dir = PROJECT_ROOT / "audit"
    audit_dir.mkdir(exist_ok=True)

    log.info("Spawning audit agent  cwd=%s", PROJECT_ROOT)

    # Build the env: inherit current env, forward relevant keys.
    spawn_env = dict(os.environ)
    _dotenv = _load_dotenv()
    spawn_env.update(_dotenv)

    audit_prompt += (
        f"\n\nWhen you are completely done writing the audit report, "
        f"print exactly this phrase on its own line: {DONE_HANDOFF_PHRASE}"
    )

    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
    argv = [
        claude_exe,
        "--dangerously-skip-permissions",
        "--model", model,
        audit_prompt,
    ]

    # Truncate PTY output log at the start of each session.
    if _pty_output_log is not None:
        try:
            _pty_output_log.seek(0)
            _pty_output_log.truncate()
        except Exception:
            pass

    mgr = SwarmManager()
    try:
        task = mgr.spawn_interactive(
            theorem="(audit)",
            description="headless audit",
            argv=argv,
            cwd=str(PROJECT_ROOT),
            env=spawn_env,
            workflow_kind="audit",
            workflow_command="audit",
            project_name=PROJECT_ROOT.name,
            project_root=str(PROJECT_ROOT),
        )
    except Exception as exc:
        log.error("Audit spawn_interactive failed: %s", exc, exc_info=True)
        return "error"

    _task_session_type[task.task_id] = "audit"
    log.info(
        "Spawned audit task %s%s",
        task.task_id,
        f"  (PTY output → {_pty_log_path})" if _pty_log_path else "",
    )

    status = _wait_for_task(task)
    log.info("Audit agent finished  status=%s", status)

    if status == "failed":
        log.warning("Audit agent failed — still checking for report")

    return _parse_audit_verdict()


def _latest_audit_report() -> Optional[Path]:
    """Return the path to the most recent audit report in audit/, or None."""
    audit_dir = PROJECT_ROOT / "audit"
    if not audit_dir.is_dir():
        return None
    reports = sorted(
        (p for p in audit_dir.glob("*.md") if p.name != ".gitkeep"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _parse_audit_verdict() -> str:
    """Read the latest audit report and return 'pass', 'fail', or 'error'.

    Reads the INTEGRITY line (not STATUS).  The COMPLETENESS line is
    logged but does not affect the verdict.
    """
    report_path = _latest_audit_report()
    if report_path is None:
        log.warning("No audit report found in %s/audit/", PROJECT_ROOT)
        return "error"

    log.info("Reading audit report: %s", report_path)

    try:
        text = report_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read audit report: %s", exc)
        return "error"

    integrity = None
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        # Only read the FIRST integrity/status line — ignore any later
        # mentions in the report body (examples, quotes, findings).
        if integrity is None:
            if upper == "INTEGRITY: PASS":
                integrity = "pass"
            elif upper == "INTEGRITY: FAIL":
                integrity = "fail"
            elif upper == "STATUS: PASS":
                integrity = "pass"
            elif upper == "STATUS: FAIL":
                integrity = "fail"
        # Always log completeness (informational).
        if upper.startswith("COMPLETENESS:"):
            log.info("Audit completeness: %s", stripped)

    if integrity is None:
        log.warning("Could not parse INTEGRITY from audit report — treating as fail")
        return "fail"

    return integrity


def _spawn_fix_session(config: dict) -> Optional[object]:
    """Phase 2b: spawn a Gauss-staged fix agent with lean4-skills.

    Uses /prove as the routing command (any valid command works — we only
    need Gauss staging).  The startup prompt is completely replaced with
    the fix prompt + audit report path.
    """
    if not FIX_PROMPT_PATH.exists():
        log.error("Fix prompt not found at %s", FIX_PROMPT_PATH)
        return None
    fix_prompt = FIX_PROMPT_PATH.read_text(encoding="utf-8")

    audit_report = _latest_audit_report()
    if audit_report is None:
        log.error("No audit report found — cannot spawn fix agent")
        return None

    prompt = (
        f"{fix_prompt}\n\n"
        f"The audit report is at: {audit_report}\n"
        f"Read it now and fix every issue marked FAIL.\n\n"
        f"Read the project's CLAUDE.md for project rules before starting.\n\n"
        f"When you are completely done with all fixes and have committed, "
        f"print exactly this phrase on its own line: {DONE_HANDOFF_PHRASE}"
    )

    # Use /prove as the routing command — we only need Gauss staging.
    # The prompt_override replaces the "run /lean4:prove" instruction.
    return _spawn_gauss_session(
        config,
        "/prove fix audit issues",
        prompt_override=prompt,
        description_label="fix",
    )


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
        "Headless runner started  mode=%s  project=%s  args='%s'  "
        "idle_timeout=%.2fh  poll=%ds  max_cycles=%s%s",
        HEADLESS_MODE,
        PROJECT_ROOT,
        AUTOFORMALIZE_ARGS,
        IDLE_TIMEOUT / 3600,
        POLL_INTERVAL,
        MAX_CYCLES if MAX_CYCLES else "∞",
        f"  pty_log={_pty_log_path}" if _pty_log_path else "",
    )

    # On the first iteration, HEADLESS_MODE controls which phases to skip:
    #   "full"  → run all phases
    #   "audit" → skip autoformalize, start at audit
    #   "fix"   → skip autoformalize and audit, start at fix
    # After the first iteration, all subsequent cycles run all phases.
    skip_autoformalize = HEADLESS_MODE in ("audit", "fix")
    skip_first_audit = HEADLESS_MODE == "fix"

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

        # ════════════════════════════════════════════════════════════════════
        # Phase 1: Autoformalize
        # ════════════════════════════════════════════════════════════════════
        if skip_autoformalize:
            log.info("═══ Skipping autoformalize (--audit/--fix first cycle) ═══")
            skip_autoformalize = False  # only skip once
        else:
            log.info("═══ Phase 1: Autoformalize (cycle %d) ═══", cycles + 1)

            task = _spawn_autoformalize_session(config)
            if task is None:
                log.warning(
                    "Autoformalize session could not be spawned — retrying in %ds",
                    FAILURE_BACKOFF,
                )
                _stop_event.wait(timeout=FAILURE_BACKOFF)
                continue

            status = _wait_for_task(task)
            log.info("Autoformalize finished  status=%s", status)

            if _stop_event.is_set():
                break

            if status == "failed":
                log.warning(
                    "Autoformalize failed — waiting %ds before retry",
                    FAILURE_BACKOFF,
                )
                _stop_event.wait(timeout=FAILURE_BACKOFF)
                continue

        # ════════════════════════════════════════════════════════════════════
        # Phase 2: Audit-fix loop (runs until integrity passes)
        #
        # No round limit — integrity failures (wrong blackbox statements,
        # vacuous proofs, contradictory constants) are too dangerous to
        # skip. The autoformalizer must not run on a broken foundation.
        # The loop always terminates because:
        #   - The auditor passes honest sorry's (completeness ≠ integrity)
        #   - The fixer converts bad proofs to sorry's → integrity passes
        #   - Spawn/audit errors break out as a safety valve
        # ════════════════════════════════════════════════════════════════════
        audit_round = 0
        while not _stop_event.is_set():
            audit_round += 1

            # ── Phase 2a: Audit ──────────────────────────────────────────
            if skip_first_audit:
                log.info("═══ Skipping audit (--fix first cycle) ═══")
                skip_first_audit = False  # only skip once
                # Go straight to fix — we need an existing report.
                report = _latest_audit_report()
                if report is None:
                    log.error("No audit report found in audit/ — cannot fix without one")
                    break
                verdict = _parse_audit_verdict()
                if verdict == "pass":
                    log.info("Existing audit already passes — skipping fix")
                    break
            else:
                log.info("═══ Phase 2a: Audit (round %d) ═══", audit_round)

                verdict = _spawn_audit_session()
                log.info("Audit verdict: %s", verdict)

                if verdict == "pass":
                    log.info("Audit passed — proceeding to next autoformalize cycle")
                    break

                if verdict == "error":
                    log.warning("Audit errored — retrying after backoff")
                    _stop_event.wait(timeout=FAILURE_BACKOFF)
                    continue

            if _stop_event.is_set():
                break

            # ── Phase 2b: Fix ────────────────────────────────────────────
            log.info("═══ Phase 2b: Fix (round %d) ═══", audit_round)

            fix_task = _spawn_fix_session(config)
            if fix_task is None:
                log.warning("Fix session could not be spawned — retrying after backoff")
                _stop_event.wait(timeout=FAILURE_BACKOFF)
                continue

            fix_status = _wait_for_task(fix_task)
            log.info("Fix finished  status=%s", fix_status)

            if fix_status == "failed":
                log.warning("Fix session failed — retrying after backoff")
                _stop_event.wait(timeout=FAILURE_BACKOFF)

        cycles += 1
        log.info("Completed full cycle %d", cycles)

        if _stop_event.is_set():
            break

        # ── self-update Gauss between cycles ──────────────────────────────
        _run_gauss_update()

    log.info("Headless runner stopped after %d cycle(s)", cycles)
    if _pty_output_log is not None:
        _pty_output_log.close()


if __name__ == "__main__":
    main()
