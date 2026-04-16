#!/usr/bin/env python3
"""
Claude Monitor Hook - sends tool events to the animator via UDP.
Called by Claude Code on PreToolUse, PostToolUse, Stop, etc.
"""

import json
import sys
import socket


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

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
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
