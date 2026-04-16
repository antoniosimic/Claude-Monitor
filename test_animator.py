#!/usr/bin/env python3
"""
Simulates a realistic Claude Code session for testing the animator.
Run while claude_animator.py is open in another terminal.
"""

import json
import socket
import time


def send(event_type, tool_name="", tool_input=None):
    msg = json.dumps({
        "hook_event_name": event_type,
        "tool_name": tool_name,
        "tool_input": tool_input or {},
    })
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(msg.encode("utf-8"), ("127.0.0.1", 9876))
    sock.close()


def main():
    print("=== Claude Code Session Simulator ===\n")

    # --- Turn 1 ---
    print("[User] How do I fix the login bug?")
    send("UserPromptSubmit")
    time.sleep(4)  # Claude typing...

    print("  [Claude] Reading the auth file...")
    send("PreToolUse", "Read", {"file_path": "src/auth/login.ts"})
    time.sleep(4)
    send("PostToolUse", "Read")
    time.sleep(3)  # Claude typing again...

    print("  [Claude] Searching for error handler...")
    send("PreToolUse", "Grep", {"pattern": "handleAuthError"})
    time.sleep(3)
    send("PostToolUse", "Grep")
    time.sleep(2)

    print("  [Claude] Fixing the bug...")
    send("PreToolUse", "Edit", {"file_path": "src/auth/login.ts"})
    time.sleep(3)
    send("PostToolUse", "Edit")
    time.sleep(3)  # Claude typing conclusion...

    print("  [Claude] Running tests...")
    send("PreToolUse", "Bash", {"command": "npm test"})
    time.sleep(5)
    send("PostToolUse", "Bash")
    time.sleep(2)

    print("[Claude] Done!\n")
    send("Stop")
    time.sleep(5)  # Idle...

    # --- Turn 2 ---
    print("[User] Can you also check the API docs?")
    send("UserPromptSubmit")
    time.sleep(3)

    print("  [Claude] Searching the web...")
    send("PreToolUse", "WebSearch", {"query": "REST API auth best practices"})
    time.sleep(4)
    send("PostToolUse", "WebSearch")
    time.sleep(3)

    print("[Claude] Done!\n")
    send("Stop")

    print("=== Simulation complete ===")


if __name__ == "__main__":
    main()
