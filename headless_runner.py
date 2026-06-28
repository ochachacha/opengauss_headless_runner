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
_done_verdict: dict[str, str] = {}   # task_id -> "pass" | "fail" | "" (from handoff phrase)
_goal_condition: dict[str, str] = {}  # task_id -> /goal condition to inject (autoformalize)
_goal_injected: dict[str, float] = {}  # task_id -> when /goal was injected (inject-once guard)
_disconnect_latched: dict[str, float] = {}  # task_id -> when we last resumed after a disconnect
_disconnect_resumed_count: dict[str, int] = {}  # task_id -> consecutive disconnect resumes
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

# Transient API/stream failures the *child* Claude Code CLI prints inline when
# the streaming HTTP connection drops mid-response. The turn ends and the agent
# idles, but the session process is alive and the conversation is intact, so the
# right recovery is to resume in place (send "continue") — NOT to wait out the
# full idle timer or cancel + re-run the whole phase. Kept deliberately narrow:
# match only the recoverable mid-stream drop, not persistent errors (401, 529
# overloaded, etc.) where an instant retry would just hammer a failing API.
_DISCONNECT_PATTERNS: list[bytes] = [
    b"Connection closed mid-response",
]

# Pattern the autoformalize agent emits when it wants to hand off.
# The agent should print this exact phrase when it is done.
DONE_HANDOFF_PHRASE: str = os.environ.get(
    "DONE_HANDOFF_PHRASE", "HEADLESS_RUNNER_HANDOFF_DONE"
)
# Audit-specific handoff phrases that encode the verdict directly,
# so the runner doesn't have to race against file I/O to parse the report.
DONE_HANDOFF_PASS: str = "HEADLESS_RUNNER_HANDOFF_AUDIT_PASS"
DONE_HANDOFF_FAIL: str = "HEADLESS_RUNNER_HANDOFF_AUDIT_FAIL"
DONE_HANDOFF_QUIT: str = "HEADLESS_RUNNER_HANDOFF_QUIT"
_DONE_PATTERNS: list[bytes] = [
    DONE_HANDOFF_PASS.encode(),
    DONE_HANDOFF_FAIL.encode(),
    DONE_HANDOFF_QUIT.encode(),
    DONE_HANDOFF_PHRASE.encode(),  # generic fallback (autoformalize, fix)
]

# How long a stuck pattern must persist before we nudge (seconds).
# Avoids reacting to transient messages that scroll past.
STUCK_DETECT_DELAY: int = int(os.environ.get("STUCK_DETECT_DELAY_SECONDS", "30"))

# --- Transient-disconnect recovery (see _DISCONNECT_PATTERNS) ---
# Seconds of PTY silence after the disconnect message before we declare the turn
# truly dead and resume (short — the stream has already ended, we just confirm
# it isn't a momentary pause).
DISCONNECT_RESUME_DELAY: int = int(os.environ.get("DISCONNECT_RESUME_DELAY_SECONDS", "5"))
# Minimum gap between successive resume attempts on the same task, so we don't
# re-fire while our own "continue" echo / the model's reply is still streaming
# and the error text lingers in the PTY tail buffer.
DISCONNECT_RESUME_COOLDOWN: int = int(
    os.environ.get("DISCONNECT_RESUME_COOLDOWN_SECONDS", "30")
)
# Max consecutive resume attempts before giving up and falling back to cancel.
# Bounds the loop so a genuine API outage can't become an infinite resume spin.
# The budget resets once a clean resume flushes the error from the PTY tail.
MAX_DISCONNECT_RESUMES: int = int(os.environ.get("MAX_DISCONNECT_RESUMES", "3"))

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

# Optional extra instruction for the agent prompt.  Applied to the FIRST
# session only (whichever phase runs first per HEADLESS_MODE) and then
# consumed — subsequent autoformalize/audit/fix sessions do not receive it.
EXTRA_INSTRUCTION: str = os.environ.get("HEADLESS_EXTRA_INSTRUCTION", "").strip()
_extra_instruction_consumed: bool = False


def _consume_extra_instruction() -> str:
    """Return EXTRA_INSTRUCTION once, then empty on every subsequent call."""
    global _extra_instruction_consumed
    if _extra_instruction_consumed or not EXTRA_INSTRUCTION:
        return ""
    _extra_instruction_consumed = True
    return EXTRA_INSTRUCTION

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

# ---------------------------------------------------------------------------
# /goal integration (Claude Code 2.1.139+)
# ---------------------------------------------------------------------------
# Inject a session-scoped goal into the autoformalize session so it keeps
# taking turns toward a bounded, self-verifying per-session milestone (a small
# evaluator model judges the condition after every turn and re-directs Claude
# until it is met) instead of relying on idle "continue" nudges. A goal also
# survives /compact. Mechanics:
#   - The FIRST user message stays the Gauss workflow-staging prompt (so the
#     lean4 workflow launches normally). The /goal is sent as a FOLLOW-UP PTY
#     message after a short warmup — exactly the channel the nudge uses.
#   - The condition references "this session's instructions" so it stays short
#     (the /goal condition cap is ~4000 chars), and ends by asking the agent to
#     print the handoff phrase, which is still the clean termination signal.
#   - The existing idle/stuck/nudge backstop is LEFT UNCHANGED: the goal drives
#     proactive per-turn continuation, so a goal-driven session rarely idles long
#     enough to trip the nudge — but if it truly goes silent, the nudge,
#     context-limit /compact, and wall-clock caps remain the wedge safety net (a
#     /goal survives both the nudge and /compact).
# Disable with AUTOFORMALIZE_GOAL_ENABLED=0 to fall back to pure nudging.
AUTOFORMALIZE_GOAL_ENABLED: bool = os.environ.get(
    "AUTOFORMALIZE_GOAL_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Seconds after a session's first PTY output before the /goal is injected.
# Lets the Gauss workflow staging / first turn get underway first; the queued
# /goal message is picked up at the next prompt.
GOAL_INJECT_DELAY_SECONDS: int = int(os.environ.get("GOAL_INJECT_DELAY_SECONDS", "90"))

# Default per-session goal condition (override via AUTOFORMALIZE_GOAL_CONDITION
# or headless.conf). Kept short and self-verifying; ends with the handoff phrase
# so the runner's existing done-pattern detection cleanly ends the session, and
# carries a turn bound so the goal terminates even on a hard leaf.
_DEFAULT_AUTOFORMALIZE_GOAL_CONDITION: str = (
    "Continue the Phase-2 dissolution work described in this session's instructions "
    "(CLAUDE.md, PROOF_STRATEGY.md § 'Current priority', FORMALIZATION_GUIDE.md). You are ONE "
    "shoulder in an open-ended, MULTI-SESSION relay whose finish line is a fully axiom-free, "
    "sorry-free proof; the runner WILL spawn another session after you, so your job is not to "
    "finish the project this session — it is to advance it by at least one unit and hand off. "
    "Pick the cheapest open blueprint leaf and dissolve at least one unit of project debt "
    "(#axiom + #sorry + #placeholder-opaque) via the blueprint pattern. 'This axiom is too big "
    "for one session' is NEVER a reason to stop — it is the entire reason the relay exists: "
    "decompose the big axiom into atomic blueprint leaves and grind ONE of them now. This goal "
    "is MET only when, in the surfaced conversation, you have shown (1) a clean `lake build`, "
    "(2) the old vs new debt metric with a strict decrease, and (3) a git commit of the change "
    f"— then print {DONE_HANDOFF_PHRASE} on its own line to HAND OFF to the auditor. Handing off "
    "does NOT stop the runner; the next session immediately continues the relay. If after a "
    "genuine, documented effort you truly cannot reduce debt this session, hand off the SAME way "
    f"after at most 40 turns — still {DONE_HANDOFF_PHRASE}, NEVER the runner's quit phrase. Never "
    "weaken a statement, axiomatize a conclusion, or launder a sorry to satisfy this goal; a "
    "correctly-stated sorry/blueprint leaf is acceptable and simply does not count as debt "
    "reduction."
)
AUTOFORMALIZE_GOAL_CONDITION: str = os.environ.get(
    "AUTOFORMALIZE_GOAL_CONDITION", _DEFAULT_AUTOFORMALIZE_GOAL_CONDITION
).strip()

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


def _sanitize_auth_env(env: dict) -> dict:
    """Strip dead/misleading Anthropic auth vars from a child's environment.

    This project authenticates Claude Code via OAuth (auth_mode=auto + the
    managed-home ``.credentials.json``), not an API key.  Two credential vars
    leak in from the Gauss/OpenGauss launch environment and only cause trouble:

    - ``ANTHROPIC_TOKEN`` — a Gauss api-key-mode artifact (an ``sk-ant-oat``
      token).  Claude Code does not recognize this var at all; it is pure noise
      that misleads auth debugging.
    - ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` when **empty** — a present
      but blank key can shadow OAuth auth and is a 401 footgun.

    A genuinely non-empty ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` (e.g. a
    custom endpoint configured via ``<model>.env``) is preserved, as is
    ``ANTHROPIC_MODEL`` and every other forwarded var.  Mutates and returns
    ``env`` for convenience.
    """
    env.pop("ANTHROPIC_TOKEN", None)
    for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if not env.get(_k):  # unset or empty string
            env.pop(_k, None)
    return env

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


def _pty_send(fd: int, text: str) -> None:
    """Type ``text`` into a PTY, then submit with a SEPARATE Enter keystroke.

    Claude Code's input layer treats a single large PTY burst as a *paste*
    (collapsed to ``[Pasted text #N]``) and absorbs a trailing ``\\r`` inside
    that burst as pasted text rather than firing it as a submit — leaving the
    message lingering, unsent, in the input box.  Writing the Enter as its own
    ``os.write`` after a brief gap makes it land in a separate PTY read so it
    registers as a real submit regardless of message length.
    """
    os.write(fd, text.encode())
    time.sleep(0.3)
    os.write(fd, b"\r")


def _send_continue(task) -> bool:  # noqa: ANN001
    """Send NUDGE_MESSAGE to the task's PTY.  Returns True on success."""
    fd = task.pty_master_fd
    if fd is None:
        return False
    try:
        _pty_send(fd, NUDGE_MESSAGE)
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


def _check_disconnect_pattern(task) -> bool:  # noqa: ANN001
    """Return True if the PTY tail contains a transient API-disconnect message."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return False
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return any(pat in tail for pat in _DISCONNECT_PATTERNS)


def _is_context_limit(task) -> bool:  # noqa: ANN001
    """Return True if the stuck state is specifically a context limit."""
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return False
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    return any(pat in tail for pat in _CONTEXT_LIMIT_PATTERNS)


def _check_done_pattern(task) -> Optional[bytes]:  # noqa: ANN001
    """Return the matched done/handoff pattern, or None if not found.

    Checks PASS/FAIL-specific patterns first so the verdict is captured
    even when the generic pattern would also match.
    """
    buf = getattr(task, "_recent_output", None)
    if not buf:
        return None
    tail = bytes(buf[-4096:]) if len(buf) > 4096 else bytes(buf)
    for pat in _DONE_PATTERNS:
        if pat in tail:
            return pat
    return None


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

        # --- /goal injection: set a session-scoped goal once, after warmup ---
        # The first user message is the Gauss workflow-staging prompt; the /goal
        # is sent here as a follow-up PTY message and queued until the next
        # prompt. Only for tasks with a configured goal that hasn't been injected.
        cond = _goal_condition.get(task.task_id)
        if cond and task.task_id not in _goal_injected:
            first_out = _last_output_at.get(task.task_id)
            started = task.start_time or first_out or now
            if first_out is not None and (now - started) >= GOAL_INJECT_DELAY_SECONDS:
                fd = task.pty_master_fd
                if fd is not None:
                    try:
                        _pty_send(fd, f"/goal {cond}")
                        _goal_injected[task.task_id] = now
                        log.info(
                            "Task %s: injected /goal (%d-char condition) after %ds warmup "
                            "— continue-nudge now suppressed for this task",
                            task.task_id, len(cond), int(now - started),
                        )
                    except OSError as exc:
                        log.warning("Failed to inject /goal for %s: %s", task.task_id, exc)

        # The existing idle/stuck/nudge backstop is left unchanged: /goal drives
        # proactive per-turn continuation, and a goal-driven session rarely idles
        # long enough to trip the nudge — but if it truly goes silent, the nudge
        # and wall-clock cap remain as the wedge safety net (a /goal survives the
        # nudge and /compact).
        is_nudgeable = session_type == "autoformalize"

        # --- Path 0: done-pattern detection (all session types) ---
        # Latch: once the done phrase appears anywhere in the buffer,
        # remember it permanently for this task (even if later output
        # scrolls it out of the 4KB tail).
        if task.task_id not in _done_latched:
            matched = _check_done_pattern(task)
            if matched is not None:
                _done_latched[task.task_id] = now
                # Decode verdict from the specific pattern that matched.
                if matched == DONE_HANDOFF_PASS.encode():
                    _done_verdict[task.task_id] = "pass"
                elif matched == DONE_HANDOFF_FAIL.encode():
                    _done_verdict[task.task_id] = "fail"
                elif matched == DONE_HANDOFF_QUIT.encode():
                    _done_verdict[task.task_id] = "quit"
                else:
                    _done_verdict[task.task_id] = ""
                log.info(
                    "Task %s (%s): done pattern detected (verdict=%s) — "
                    "waiting for %ds of silence before handoff",
                    task.task_id, session_type,
                    _done_verdict[task.task_id] or "generic",
                    DONE_SILENCE,
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

        # --- Path 1.5: transient API-disconnect recovery (all session types) ---
        # The child printed "Connection closed mid-response": the streaming turn
        # died but the session is alive. Resume in place rather than waiting out
        # the idle timer (autoformalize) or cancelling + re-running the whole
        # phase (audit/fix). This deliberately overrides the audit/fix "never
        # nudge" rule: a mid-turn disconnect is a transient failure, distinct
        # from an idle session that is legitimately done. Bounded by
        # MAX_DISCONNECT_RESUMES so a real outage falls through to cancel.
        if _check_disconnect_pattern(task):
            latched_at = _disconnect_latched.get(task.task_id)
            # Don't re-fire while a prior resume's echo/reply is still streaming
            # (the error text lingers in the tail until it scrolls out).
            if latched_at is not None and (now - latched_at) < DISCONNECT_RESUME_COOLDOWN:
                continue
            last_output = _last_output_at.get(task.task_id, now)
            # Confirm the stream really ended (not a momentary mid-stream pause).
            if (now - last_output) < DISCONNECT_RESUME_DELAY:
                continue
            count = _disconnect_resumed_count.get(task.task_id, 0)
            if count >= MAX_DISCONNECT_RESUMES:
                log.warning(
                    "Task %s (%s): API disconnect persisted after %d resume "
                    "attempts — cancelling (fall back to phase retry)",
                    task.task_id, session_type, count,
                )
                mgr.cancel(task.task_id)
                _disconnect_latched.pop(task.task_id, None)
                _disconnect_resumed_count.pop(task.task_id, None)
                continue
            log.warning(
                "Task %s (%s): API disconnect detected — resuming in place "
                "(attempt %d/%d)",
                task.task_id, session_type, count + 1, MAX_DISCONNECT_RESUMES,
            )
            if _send_continue(task):
                _disconnect_latched[task.task_id] = now
                _disconnect_resumed_count[task.task_id] = count + 1
            else:
                log.warning(
                    "Task %s has no writable PTY for disconnect resume — cancelling",
                    task.task_id,
                )
                mgr.cancel(task.task_id)
                _disconnect_latched.pop(task.task_id, None)
                _disconnect_resumed_count.pop(task.task_id, None)
            continue
        else:
            # Error text has scrolled out of the tail — a clean resume produced
            # fresh output. Clear the latch and refill the retry budget.
            _disconnect_latched.pop(task.task_id, None)
            _disconnect_resumed_count.pop(task.task_id, None)

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
                    # autoformalize: nudge (a /goal, if set, survives /compact)
                    if _is_context_limit(task):
                        msg = "/compact"
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
                            _pty_send(fd, msg)
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

    # Keep the managed-home OAuth credentials in sync with the live ~/.claude on
    # every spawn. auth_mode=auto copies .credentials.json into the managed home
    # once at staging, but OAuth refresh tokens ROTATE on use and the two homes
    # share one token lineage: whichever home last refreshed holds the only valid
    # refresh token; the other's is dead. A stale copy -> the child can't refresh
    # -> 401 "Please run /login".
    #
    # Sync toward whichever side is FRESHER (larger expiresAt), in EITHER
    # direction. A blind live->managed copy would be wrong when only autonomous
    # (managed-home) runners are active: the managed child self-refreshes and
    # rotates the token, and a live->managed copy would then clobber the good
    # rotated token with the live home's now-dead one. Copying the fresher side
    # onto the staler keeps both homes alive regardless of who refreshed last.
    _live_creds = Path.home() / ".claude" / ".credentials.json"
    _managed_creds = settings_dir / ".credentials.json"

    def _cred_expiry(p):
        try:
            return _json.loads(p.read_text())["claudeAiOauth"]["expiresAt"]
        except Exception:
            return None

    try:
        _live_exp = _cred_expiry(_live_creds) if _live_creds.is_file() else None
        _mgd_exp = _cred_expiry(_managed_creds) if _managed_creds.is_file() else None
        if _live_exp is not None and (_mgd_exp is None or _live_exp > _mgd_exp):
            shutil.copy2(_live_creds, _managed_creds)
            log.info("Synced OAuth credentials live -> managed (live is fresher)")
        elif _mgd_exp is not None and (_live_exp is None or _mgd_exp > _live_exp):
            shutil.copy2(_managed_creds, _live_creds)
            log.info("Synced OAuth credentials managed -> live (managed is fresher)")
    except Exception as exc:
        log.warning("Could not sync OAuth credentials between homes: %s", exc)

    settings_path = settings_dir / "settings.json"
    try:
        _sd = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        _sd = {}
    _sd["skipDangerousModePermissionPrompt"] = True
    # /goal is implemented as a session Stop-hook; it is unavailable if hooks are
    # disabled. Pin hooks on so a stale/managed setting can't silently break it.
    _sd["disableAllHooks"] = False
    _perms = _sd.setdefault("permissions", {})
    # Bare "*" is rejected by CC's allow-rule parser ("Wildcard tool name *
    # is not supported in allow rules"); bypassPermissions mode is the
    # documented way to auto-approve every tool without prompts. Deny rules
    # are still enforced in this mode (evaluated before allow).
    _perms["defaultMode"] = "bypassPermissions"
    _perms.pop("allow", None)  # drop any stale invalid "*" allow entry
    _perms["deny"] = ["AskUserQuestion"]
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
        if val:  # forward only non-empty values (an empty key is a 401 footgun)
            spawn_env[key] = val
    _sanitize_auth_env(spawn_env)

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
        "\n\nRead the project's CLAUDE.md, PROOF_STRATEGY.md, and FORMALIZATION_GUIDE.md. "
        "Based on those documents, design the right --claim-select for your session."
        f"\n\nWhen you are done with this session's work and want to hand off to the "
        f"audit agent, print exactly this phrase on its own line: {DONE_HANDOFF_PHRASE}. "
        f"Handing off does NOT stop the runner — it ends YOUR turn, and the runner immediately "
        f"spawns the next session to keep grinding. This is a deliberate, open-ended, "
        f"MULTI-SESSION relay: no single session is expected to finish the project, and the "
        f"correct outcome of almost every session is 'one unit of debt dissolved, then hand "
        f"off.'"
        f"\n\nDo NOT stop the runner just because you ran out of easy work, hit a hard axiom, or "
        f"feel the remaining work is 'too large for one session' or 'a multi-session effort.' "
        f"That feeling is EXPECTED and is NEVER a reason to quit: the whole design is that many "
        f"sessions chain together to finish what no single session could. Refusing to engage a "
        f"deep axiom because it is large is the specific laziness this runner exists to defeat — "
        f"if a target is too big to dissolve whole, decompose it into atomic sub-axioms (the "
        f"mandatory blueprint pattern) and grind ONE of them this session. Per CLAUDE.md, Phase "
        f"2's goal is to drive the project axiom count to ZERO: every remaining `axiom` and "
        f"`sorry` is a dissolution target, and there is essentially ALWAYS a cheapest open "
        f"blueprint leaf to grind."
        f"\n\nThe ONLY stop bar is the auditor's `ACCEPTANCE: ACCEPT` condition: emit the quit "
        f"phrase ONLY when you have verified, in THIS session, that the project is genuinely "
        f"complete — `#print axioms TwoOrInfty.prop_main` reports nothing beyond `[propext, "
        f"Classical.choice, Quot.sound]`, AND no `sorry` and no project `axiom` (laundered or "
        f"honest) remains anywhere in the source tree. A correctly-stated `sorry` or honest "
        f"blueprint `axiom` left in the tree does NOT meet the bar — it must be dissolved, never "
        f"laundered away. In that fully-verified, axiom-free and sorry-free case ONLY, print this "
        f"phrase to stop the runner: {DONE_HANDOFF_QUIT}. In EVERY other case — including when "
        f"you are stuck, out of obvious moves, or convinced the rest is too hard for now — hand "
        f"off with {DONE_HANDOFF_PHRASE} instead and let the relay continue. When in doubt, hand "
        f"off; never quit."
    )
    extra = _consume_extra_instruction()
    if extra:
        handoff_instruction += f"\n\nAdditional instruction: {extra}"
    task = _spawn_gauss_session(
        config,
        command,
        prompt_suffix=handoff_instruction,
        description_label="autoformalize",
    )
    # Schedule a session-scoped /goal injection (see _check_idle_timeout). The
    # goal keeps this session iterating toward a bounded milestone instead of
    # relying on idle nudges; it is injected as a follow-up PTY message after a
    # short warmup so the workflow-staging first message runs first.
    if task is not None and AUTOFORMALIZE_GOAL_ENABLED and AUTOFORMALIZE_GOAL_CONDITION:
        _goal_condition[task.task_id] = AUTOFORMALIZE_GOAL_CONDITION
        log.info(
            "Task %s: /goal scheduled (inject ~%ds after first output)",
            task.task_id, GOAL_INJECT_DELAY_SECONDS,
        )
    return task


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
    _sanitize_auth_env(spawn_env)

    audit_prompt += (
        f"\n\nWhen you are completely done writing the audit report, "
        f"print exactly ONE of these phrases on its own line depending on "
        f"the integrity verdict:\n"
        f"  - If INTEGRITY: PASS → {DONE_HANDOFF_PASS}\n"
        f"  - If INTEGRITY: FAIL → {DONE_HANDOFF_FAIL}\n"
    )
    extra = _consume_extra_instruction()
    if extra:
        audit_prompt += f"\n\nAdditional instruction: {extra}"

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

    # Prefer the verdict encoded in the handoff phrase (avoids file-parse races).
    handoff_verdict = _done_verdict.pop(task.task_id, "")
    if handoff_verdict in ("pass", "fail"):
        log.info("Audit verdict from handoff phrase: %s", handoff_verdict)
        return handoff_verdict

    # Fallback: parse the report file.
    log.info("No verdict from handoff phrase — falling back to report file")
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


def _parse_audit_acceptance() -> str:
    """Read the latest audit report and return 'accept', 'reject', or 'unknown'.

    ACCEPTANCE is the project-DONE bar (see audit_prompt.md): ACCEPT only when
    the tree is simultaneously sorry-free and free of every non-kernel axiom.
    This is the authoritative gate the runner uses to decide whether the
    autoformalize agent's quit phrase may actually stop the multi-session
    relay.  An agent emitting the quit phrase while ACCEPTANCE is REJECT is the
    'lazy quit' failure mode — the runner ignores it and continues.
    """
    report_path = _latest_audit_report()
    if report_path is None:
        log.warning("No audit report found in %s/audit/ — cannot parse ACCEPTANCE", PROJECT_ROOT)
        return "unknown"

    try:
        text = report_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read audit report for ACCEPTANCE: %s", exc)
        return "unknown"

    for line in text.splitlines():
        upper = line.strip().upper()
        # Only the FIRST acceptance line counts — ignore later body mentions.
        if upper == "ACCEPTANCE: ACCEPT":
            return "accept"
        if upper == "ACCEPTANCE: REJECT":
            return "reject"

    log.warning("Could not parse ACCEPTANCE from audit report — treating as unknown")
    return "unknown"


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
    extra = _consume_extra_instruction()
    if extra:
        prompt += f"\n\nAdditional instruction: {extra}"

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
    if EXTRA_INSTRUCTION:
        log.info("Extra instruction: %s", EXTRA_INSTRUCTION)
    if AUTOFORMALIZE_GOAL_ENABLED and AUTOFORMALIZE_GOAL_CONDITION:
        log.info(
            "Autoformalize /goal injection ENABLED (warmup %ds, %d-char condition)",
            GOAL_INJECT_DELAY_SECONDS, len(AUTOFORMALIZE_GOAL_CONDITION),
        )
    else:
        log.info("Autoformalize /goal injection disabled — using idle 'continue' nudges")

    # On the first iteration, HEADLESS_MODE controls which phases to skip:
    #   "full"  → run all phases
    #   "audit" → skip autoformalize, start at audit
    #   "fix"   → skip autoformalize and audit, start at fix
    # After the first iteration, all subsequent cycles run all phases.
    skip_autoformalize = HEADLESS_MODE in ("audit", "fix")
    skip_first_audit = HEADLESS_MODE == "fix"

    quit_after_audit = False

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

            # Check if the agent requested a quit (no more meaningful work).
            # We still run a final audit before exiting — the agent's "quit"
            # skips future autoformalize cycles but not the integrity check.
            quit_verdict = _done_verdict.pop(task.task_id, "")
            quit_after_audit = quit_verdict == "quit"
            if quit_after_audit:
                log.info(
                    "Autoformalize agent requested quit — will run a final audit and exit ONLY "
                    "if that audit reports ACCEPTANCE: ACCEPT; otherwise the quit is overridden "
                    "and the relay continues."
                )

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

        if quit_after_audit:
            # The autoformalize agent asked to stop the relay. Honor it ONLY if
            # the authoritative ACCEPTANCE bar agrees the project is genuinely
            # done (sorry-free AND free of every non-kernel axiom). Any remaining
            # honest axiom/sorry ⇒ ACCEPTANCE: REJECT ⇒ this is a 'lazy quit':
            # override it and continue the multi-session relay. This makes the
            # observed "prover quits with honest axioms still open" stop pattern
            # structurally impossible regardless of agent persuasion.
            acceptance = _parse_audit_acceptance()
            if acceptance == "accept":
                log.info(
                    "Quit-after-audit: final audit ACCEPTANCE=ACCEPT — project complete, exiting."
                )
                break
            log.warning(
                "Autoformalize agent emitted the quit phrase, but the latest audit reports "
                "ACCEPTANCE=%s (project NOT complete — honest axioms/sorries remain). Ignoring "
                "the lazy quit and continuing the multi-session relay.",
                acceptance.upper(),
            )
            quit_after_audit = False

        # ── self-update Gauss between cycles ──────────────────────────────
        _run_gauss_update()

    log.info("Headless runner stopped after %d cycle(s)", cycles)
    if _pty_output_log is not None:
        _pty_output_log.close()


if __name__ == "__main__":
    main()
