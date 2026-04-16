#!/usr/bin/env python3
"""
Claude Monitor Setup - configures Claude Code hooks automatically.
Run: claude-monitor-setup
"""

import json
import os
import shutil
import sys


CLAUDE_SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".claude")
CLAUDE_SETTINGS_FILE = os.path.join(CLAUDE_SETTINGS_DIR, "settings.json")


def find_command(name):
    """Find the full path to an installed console script."""
    path = shutil.which(name)
    if path:
        return path.replace("\\", "/")
    return name


def main():
    print()
    print("  ▐▛█▜▌  Claude Monitor Setup")
    print("  ═══════════════════════════")
    print()

    hook_cmd = find_command("claude-monitor-hook")
    status_cmd = find_command("claude-monitor-status")

    print(f"  Hook command:   {hook_cmd}")
    print(f"  Status command: {status_cmd}")
    print()

    # Load existing settings
    settings = {}
    if os.path.exists(CLAUDE_SETTINGS_FILE):
        try:
            with open(CLAUDE_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
        except Exception:
            pass
        # Backup
        backup = CLAUDE_SETTINGS_FILE + ".backup"
        try:
            with open(backup, "w") as f:
                json.dump(settings, f, indent=2)
            print(f"  Backed up existing settings to {backup}")
        except Exception:
            pass

    # Configure hooks
    hook_entry = [{"matcher": "", "hooks": [{"type": "command", "command": hook_cmd}]}]
    hook_entry_match = [{"matcher": ".*", "hooks": [{"type": "command", "command": hook_cmd}]}]

    settings["hooks"] = {
        "UserPromptSubmit": hook_entry,
        "PreToolUse": hook_entry_match,
        "PostToolUse": hook_entry_match,
        "Stop": hook_entry,
        "PermissionRequest": hook_entry_match,
    }

    # Configure status line
    settings["statusLine"] = {
        "type": "command",
        "command": status_cmd,
    }

    # Save
    os.makedirs(CLAUDE_SETTINGS_DIR, exist_ok=True)
    with open(CLAUDE_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

    print()
    print("  Done! Claude Code hooks configured.")
    print()
    print("  Usage:")
    print("    1. Open a NEW terminal and run:  claude-monitor")
    print("    2. Use Claude Code in another terminal as normal")
    print("    3. Watch the monitor react to Claude's activity!")
    print()
    print("  Controls (in the monitor window):")
    print("    S      Open/close settings")
    print("    ↑↓     Navigate settings")
    print("    ←→     Adjust volume")
    print("    Enter  Toggle on/off")
    print("    Ctrl+C Quit")
    print()


if __name__ == "__main__":
    main()
