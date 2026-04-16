#!/usr/bin/env python3
"""
Simulates a full Claude Code session v4 - includes StatusUpdate events.
"""

import json
import socket
import time


def send(event_type, tool_name="", tool_input=None, extra=None):
    msg = {
        "hook_event_name": event_type,
        "tool_name": tool_name,
        "tool_input": tool_input or {},
    }
    if extra:
        msg.update(extra)
    data = json.dumps(msg)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(data.encode("utf-8"), ("127.0.0.1", 9876))
    sock.close()


def send_status(model="Opus 4.6", cost=0.0, ctx_pct=0.0):
    send("StatusUpdate", extra={
        "model": {"display_name": model, "id": "claude-opus-4-6"},
        "cost": {"total_cost_usd": cost},
        "context_window": {"used_percentage": ctx_pct, "total_input_tokens": 0, "total_output_tokens": 0},
    })


def main():
    print("=== Claude Monitor v5 Test ===\n")

    # Initial status
    send_status("Opus 4.6", 0.0, 2.5)
    time.sleep(1)

    # --- Turn 1 ---
    print("[User] Fix the login bug")
    send("UserPromptSubmit")
    send_status("Opus 4.6", 0.012, 5.2)
    time.sleep(4)

    print("  [Claude] Reading file...")
    send("PreToolUse", "Read", {"file_path": "src/auth/login.ts"})
    send_status("Opus 4.6", 0.025, 8.1)
    time.sleep(4)
    send("PostToolUse", "Read")
    send_status("Opus 4.6", 0.038, 12.3)
    time.sleep(3)

    print("  [Claude] Searching code...")
    send("PreToolUse", "Grep", {"pattern": "handleAuthError"})
    send_status("Opus 4.6", 0.045, 15.7)
    time.sleep(3)
    send("PostToolUse", "Grep")
    send_status("Opus 4.6", 0.052, 18.4)
    time.sleep(2)

    print("  [Claude] Editing file...")
    send("PreToolUse", "Edit", {"file_path": "src/auth/login.ts"})
    send_status("Opus 4.6", 0.068, 22.1)
    time.sleep(3)
    send("PostToolUse", "Edit")
    time.sleep(2)

    print("[Claude] Done! (completion chime)")
    send("Stop")
    send_status("Opus 4.6", 0.085, 25.3)
    time.sleep(5)

    # --- Turn 2: Permission request ---
    print("\n[User] Delete old migration files")
    send("UserPromptSubmit")
    send_status("Opus 4.6", 0.095, 28.0)
    time.sleep(3)

    print("  [Claude] Asking permission... (question chime)")
    send("PermissionRequest", "Bash")
    time.sleep(8)

    print("  [Approved] Running command...")
    send("PreToolUse", "Bash", {"command": "rm -rf migrations/old/"})
    send_status("Opus 4.6", 0.112, 32.5)
    time.sleep(4)
    send("PostToolUse", "Bash")
    time.sleep(2)

    print("[Claude] Done!")
    send("Stop")
    send_status("Opus 4.6", 0.128, 35.8)
    time.sleep(3)

    # --- Turn 3: Heavy context usage ---
    print("\n[User] Refactor the entire auth module")
    send("UserPromptSubmit")
    send_status("Opus 4.6", 0.250, 55.0)
    time.sleep(3)

    print("  [Claude] Spawning agent...")
    send("PreToolUse", "Agent", {"prompt": "Analyze auth module"})
    send_status("Opus 4.6", 0.380, 72.0)
    time.sleep(5)
    send("PostToolUse", "Agent")
    send_status("Opus 4.6", 0.520, 85.0)
    time.sleep(2)

    print("[Claude] Done!")
    send("Stop")
    send_status("Opus 4.6", 0.580, 88.5)

    print("\n=== Test complete! ===")
    print("Try pressing S in the animator to open settings!")


if __name__ == "__main__":
    main()
