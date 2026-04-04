#!/usr/bin/env python3
"""Render the last screen state from a raw PTY output log using pyte.

Usage:
    render_pty.py <logfile> [rows] [cols]

Feeds the entire log through a virtual terminal emulator and prints
the final rendered screen — no garbled escape codes.
"""
import sys
from pathlib import Path

import pyte

def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <logfile> [rows] [cols]", file=sys.stderr)
        sys.exit(1)

    log_path = Path(sys.argv[1])
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    cols = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    if not log_path.exists():
        print(f"File not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    data = log_path.read_bytes()
    stream.feed(data)

    # Print non-empty lines from the bottom of the screen
    lines = [screen.display[i].rstrip() for i in range(rows)]

    # Find last non-empty line to trim trailing blanks
    last = rows - 1
    while last >= 0 and not lines[last]:
        last -= 1

    for i in range(last + 1):
        print(lines[i])


if __name__ == "__main__":
    main()
