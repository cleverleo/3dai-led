#!/usr/bin/env python3
"""Small Codex adapter for the tool-independent 3dai-led client.

Codex parses the ``async`` hook option but does not currently run command
hooks asynchronously.  LED updates must not hold up the agent when the device
is offline, so this adapter detaches normal updates after forwarding a compact
copy of the hook JSON.  SessionEnd uses ``--sync`` so its lease release cannot
be lost when Codex exits.
"""

import json
import os
import subprocess
import sys


FORWARDED_FIELDS = (
    "session_id",
    "cwd",
    "hook_event_name",
    "tool_name",
    "reason",
)


def hook_payload():
    try:
        source = json.load(sys.stdin)
    except (OSError, ValueError):
        source = {}
    if not isinstance(source, dict):
        source = {}
    return json.dumps(
        {key: source[key] for key in FORWARDED_FIELDS if key in source}
    ).encode("utf-8")


def main():
    if len(sys.argv) < 2:
        return 2

    state = sys.argv[1]
    synchronous = "--sync" in sys.argv[2:]
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    led = os.path.join(repo, "skills", "3dai-led", "led.sh")
    command = [led, state, "codex"]
    payload = hook_payload()

    if synchronous:
        subprocess.run(
            command,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return 0

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.stdin.write(payload)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
