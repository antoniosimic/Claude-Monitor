#!/usr/bin/env python3
"""
Claude Code StatusLine hook - forwards model/cost/context data
to the animator via UDP, and outputs a simple status line.
"""

import json
import sys
import socket


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("Claude Monitor")
        sys.exit(0)

    workspace = data.get("workspace", {}) if isinstance(data.get("workspace"), dict) else {}
    cwd = data.get("cwd", "") or workspace.get("current_dir", "") or workspace.get("project_dir", "")

    # Forward full stats to animator
    msg = json.dumps({
        "hook_event_name": "StatusUpdate",
        "session_id": data.get("session_id", ""),
        "cwd": cwd,
        "model": data.get("model", {}),
        "cost": data.get("cost", {}),
        "context_window": data.get("context_window", {}),
    })
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(msg.encode("utf-8"), ("127.0.0.1", 9876))
        sock.close()
    except Exception:
        pass

    # Output simple status line for Claude Code's own bar
    model = data.get("model", {}).get("display_name", "?")
    cost = data.get("cost", {}).get("total_cost_usd", 0)
    ctx = data.get("context_window", {}).get("used_percentage", 0)
    print(f"{model} | ${cost:.2f} | ctx: {ctx:.0f}%")


if __name__ == "__main__":
    main()
