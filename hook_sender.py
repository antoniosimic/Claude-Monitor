#!/usr/bin/env python3
"""
Claude Code Hook - sends tool events to the animator via UDP.
Called by Claude Code on PreToolUse and PostToolUse.
"""

import json
import sys
import socket
import os
from datetime import datetime


LOG_FILE = os.path.join(os.path.dirname(__file__), "hook_debug.log")


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} | {msg}\n")


def main():
    log("Hook called!")
    try:
        payload = json.load(sys.stdin)
        log(f"Payload: {json.dumps(payload)[:200]}")
    except Exception as e:
        log(f"Failed to read stdin: {e}")
        sys.exit(0)

    # Forward event to animator
    message = json.dumps({
        "hook_event_name": payload.get("hook_event_name", ""),
        "tool_name": payload.get("tool_name", ""),
        "tool_input": payload.get("tool_input", {}),
        "session_id": payload.get("session_id", ""),
    })

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(message.encode("utf-8"), ("127.0.0.1", 9876))
        sock.close()
    except Exception:
        pass  # Animator not running, no problem

    sys.exit(0)


if __name__ == "__main__":
    main()
