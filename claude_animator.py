#!/usr/bin/env python3
"""
Claude Code Activity Animator v2
Real-time ASCII animation of Claude Code's activity.

States:
  waiting   → Claude idle, dimmed sprite centered, breathing zzz
  typing    → Claude generating text, bounces left-right smoothly
  walking   → Claude approaching a tool target (ease-in-out)
  action    → Claude working at the tool, pulsing
  returning → Claude walking back, then resumes typing
"""

import json
import time
import sys
import math
import threading
import socket
import signal
import subprocess
import os

from rich.console import Console
from rich.live import Live
from rich.panel import Panel


# ═══════════════════════════════════════════════════════════════
#  SCENE CONSTANTS
# ═══════════════════════════════════════════════════════════════

SCENE_W = 58            # visible scene width
PANEL_W = SCENE_W + 8   # panel total width (border + padding)
SPRITE_W = 11           # visual width of claude sprite
TARGET_W = 10           # visual width of tool target box
IDLE_X = (SCENE_W - SPRITE_W) // 2   # centered idle position
TARGET_X = SCENE_W - TARGET_W - 2    # tool target position
BOUNCE_RANGE = 10       # how far typing bounce goes from center
FPS = 12                # frames per second


# ═══════════════════════════════════════════════════════════════
#  CLAUDE SPRITES  (each line = exactly SPRITE_W visible chars)
#
#  Head:  ▐▛███▜▌   = 7 chars  → pad to 11: 2 left, 2 right
#  Body:  ▝▜█████▛▘ = 9 chars  → pad to 11: 1 left, 1 right
#  Feet:  ▘▘ ▝▝     = 5 chars  → pad to 11: 3 left, 3 right
# ═══════════════════════════════════════════════════════════════

# Resting / idle (dimmed)
S_REST = [
    "  [dim red]▐▛███▜▌[/]  ",
    " [dim red]▝▜█████▛▘[/] ",
    "   [dim red]▘▘ ▝▝[/]   ",
]

# Standing still (bright)
S_STAND = [
    "  [bright_red]▐▛███▜▌[/]  ",
    " [bright_red]▝▜█████▛▘[/] ",
    "   [bright_red]▘▘ ▝▝[/]   ",
]

# Walking right - 4 frames for smooth leg cycle
S_WALK_R = [
    # Frame 0: right foot forward
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "  [bright_red]▘▘[/]   [bright_red]▝▝[/]  ",
    ],
    # Frame 1: feet passing
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "   [bright_red]▘▝▝▘[/]   ",
    ],
    # Frame 2: left foot forward
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "  [bright_red]▝▝[/]   [bright_red]▘▘[/]  ",
    ],
    # Frame 3: feet passing
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "   [bright_red]▝▘▘▝[/]   ",
    ],
]

# Walking left - 4 frames
S_WALK_L = [
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "  [bright_red]▝▝[/]   [bright_red]▘▘[/]  ",
    ],
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "   [bright_red]▝▘▘▝[/]   ",
    ],
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "  [bright_red]▘▘[/]   [bright_red]▝▝[/]  ",
    ],
    [
        "  [bright_red]▐▛███▜▌[/]  ",
        " [bright_red]▝▜█████▛▘[/] ",
        "   [bright_red]▘▝▝▘[/]   ",
    ],
]

# Thinking (at tool target)
S_THINK = [
    "  [bright_red]▐▛███▜▌[/]  ",
    " [bright_red]▝▜█████▛▘[/] ",
    "   [bright_red]▘▘ ▝▝[/]   ",
]

# Thought bubble frames (cycle above head)
THOUGHT_BUBBLES = ["[yellow]°[/] ", "[yellow]•[/] ", "[yellow]°[/]•", " [yellow]•[/]°"]


# ═══════════════════════════════════════════════════════════════
#  TOOL TARGET BOXES  (each line = exactly TARGET_W visible chars)
# ═══════════════════════════════════════════════════════════════

FILE_ART = [
    "[cyan]┌────────┐[/]",
    "[cyan]│ [bold]FILE[/]   [cyan]│[/]",
    "[cyan]└────────┘[/]",
]

INTERNET_ART = [
    "[blue]┌────────┐[/]",
    "[blue]│ [bold]WEB[/]    [blue]│[/]",
    "[blue]└────────┘[/]",
]

TERMINAL_ART = [
    "[green]┌────────┐[/]",
    "[green]│ [bold]>_ RUN[/] [green]│[/]",
    "[green]└────────┘[/]",
]

EDIT_ART = [
    "[magenta]┌────────┐[/]",
    "[magenta]│ [bold]EDIT[/]   [magenta]│[/]",
    "[magenta]└────────┘[/]",
]

SEARCH_ART = [
    "[cyan]┌────────┐[/]",
    "[cyan]│ [bold]SEARCH[/] [cyan]│[/]",
    "[cyan]└────────┘[/]",
]

DONE_ART = [
    "[green]┌────────┐[/]",
    "[green]│  [bold]DONE[/]  [green]│[/]",
    "[green]└────────┘[/]",
]

AGENT_ART = [
    "[yellow]┌────────┐[/]",
    "[yellow]│ [bold]AGENT[/]  [yellow]│[/]",
    "[yellow]└────────┘[/]",
]

TOOL_ART = {
    "Read": FILE_ART, "Glob": SEARCH_ART, "Grep": SEARCH_ART,
    "Edit": EDIT_ART, "Write": EDIT_ART, "Bash": TERMINAL_ART,
    "WebFetch": INTERNET_ART, "WebSearch": INTERNET_ART, "Agent": AGENT_ART,
}

TOOL_LABELS = {
    "Read": "Reading", "Glob": "Searching files", "Grep": "Searching code",
    "Edit": "Editing", "Write": "Writing", "Bash": "Running command",
    "WebFetch": "Fetching web", "WebSearch": "Searching web", "Agent": "Spawning agent",
}


# ═══════════════════════════════════════════════════════════════
#  EASING
# ═══════════════════════════════════════════════════════════════

def ease_in_out(t):
    """Cubic ease-in-out: smooth acceleration and deceleration."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4 * t * t * t
    else:
        return 1 - pow(-2 * t + 2, 3) / 2


# ═══════════════════════════════════════════════════════════════
#  ANIMATOR
# ═══════════════════════════════════════════════════════════════

class ClaudeAnimator:
    def __init__(self):
        self.frame = 0
        self.running = True
        self.phase = "waiting"
        self.total_tools = 0
        self.start_time = time.time()
        self.history = []

        # Walk animation state
        self.walk_progress = 0.0   # 0.0 → 1.0
        self.walk_start = IDLE_X
        self.walk_end = TARGET_X - SPRITE_W - 2

        # Tool state
        self.target_art = FILE_ART
        self.action_label = ""

    def set_event(self, event_type, tool_name="", detail=""):
        if event_type == "UserPromptSubmit":
            self.phase = "typing"
            return

        if event_type == "Stop":
            self.phase = "waiting"
            return

        if event_type == "PreToolUse":
            self.total_tools += 1
            self.target_art = TOOL_ART.get(tool_name, FILE_ART)
            label = TOOL_LABELS.get(tool_name, tool_name)
            short = detail[:22].replace("\n", " ") if detail else ""
            self.action_label = f"{label}: {short}" if short else label

            self.phase = "walking"
            self.walk_progress = 0.0
            self.walk_start = IDLE_X
            self.walk_end = TARGET_X - SPRITE_W - 2

            self.history.append(f"[dim]  {tool_name}: {detail[:42]}[/]")
            if len(self.history) > 6:
                self.history.pop(0)

        elif event_type == "PostToolUse":
            self.phase = "returning"
            self.walk_progress = 0.0
            self.action_label = f"Done: {tool_name}"

    # ───────── position helpers ─────────

    def _get_pos(self):
        """Get current x position of Claude sprite."""
        if self.phase == "waiting":
            return IDLE_X
        elif self.phase == "typing":
            # Smooth sine bounce around center
            t = self.frame * 0.1
            return IDLE_X + int(BOUNCE_RANGE * math.sin(t))
        elif self.phase == "walking":
            t = ease_in_out(self.walk_progress)
            return int(self.walk_start + (self.walk_end - self.walk_start) * t)
        elif self.phase == "action":
            return self.walk_end
        elif self.phase == "returning":
            t = ease_in_out(self.walk_progress)
            return int(self.walk_end + (self.walk_start - self.walk_end) * t)
        return IDLE_X

    def _get_sprite(self, pos, prev_pos):
        """Get the right sprite for current state and direction."""
        if self.phase == "waiting":
            # Alternate rest frames for breathing
            if (self.frame // 15) % 2 == 0:
                return S_REST
            else:
                return S_REST

        elif self.phase == "typing":
            moving_right = pos >= prev_pos
            idx = (self.frame // 3) % 4
            return S_WALK_R[idx] if moving_right else S_WALK_L[idx]

        elif self.phase == "walking":
            idx = (self.frame // 3) % 4
            return S_WALK_R[idx]

        elif self.phase == "action":
            return S_THINK

        elif self.phase == "returning":
            idx = (self.frame // 3) % 4
            return S_WALK_L[idx]

        return S_STAND

    # ───────── ground / trail ─────────

    def _render_ground(self, pos):
        """Render the ground line under the scene."""
        ground = list("─" * SCENE_W)

        if self.phase == "waiting":
            # Simple ground
            return "[dim]" + "".join(ground) + "[/]"

        elif self.phase == "typing":
            # Dot under Claude's feet
            foot = min(max(pos + SPRITE_W // 2, 0), SCENE_W - 1)
            ground[foot] = "●"
            return "[dim]" + "".join(ground[:foot]) + "[bright_red]" + ground[foot] + "[/][dim]" + "".join(ground[foot+1:]) + "[/]"

        elif self.phase == "walking":
            # Trail behind Claude
            cx = min(pos + SPRITE_W // 2, SCENE_W - 1)
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(cx):
                ground[i] = "·"
            ground[min(cx, SCENE_W - 1)] = "►"
            for i in range(cx + 1, tx):
                ground[i] = "·"
            s = "".join(ground)
            return f"[dim]{s[:cx]}[bright_red]{s[cx]}[/][dim]{s[cx+1:]}[/]"

        elif self.phase == "action":
            pulse = "⣾⣽⣻⢿⡿⣟⣯⣷"
            p = pulse[self.frame % len(pulse)]
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(tx):
                ground[i] = "·"
            ground[tx] = p
            s = "".join(ground)
            return f"[dim]{s[:tx]}[bright_red]{s[tx]}[/][dim]{s[tx+1:]}[/]"

        elif self.phase == "returning":
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(tx + 1):
                ground[i] = "·"
            ground[tx] = "✓"
            s = "".join(ground)
            return f"[dim]{s[:tx]}[green]{s[tx]}[/][dim]{s[tx+1:]}[/]"

        return "[dim]" + "".join(ground) + "[/]"

    # ───────── main render ─────────

    def render_frame(self):
        self.frame += 1

        # Update walk progress
        if self.phase == "walking":
            self.walk_progress = min(self.walk_progress + 0.06, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "action"
        elif self.phase == "returning":
            self.walk_progress = min(self.walk_progress + 0.08, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "typing"

        # Positions
        pos = self._get_pos()
        prev_t = (self.frame - 1) * 0.1
        prev_pos = IDLE_X + int(BOUNCE_RANGE * math.sin(prev_t)) if self.phase == "typing" else pos - 1

        sprite = self._get_sprite(pos, prev_pos)

        # ── Header ──
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)

        state_tags = {
            "waiting":   "[dim]IDLE[/]",
            "typing":    "[bold bright_green]TYPING[/]",
            "walking":   "[bold bright_yellow]TOOL USE[/]",
            "action":    "[bold bright_yellow]WORKING[/]",
            "returning": "[bold bright_green]RETURNING[/]",
        }
        state = state_tags.get(self.phase, "")

        header = f" [bold bright_red]◗[/] [bold white]Claude[/]  [dim]│[/]  {state}  [dim]│[/]  Tools: [bold]{self.total_tools}[/]  [dim]│[/]  {mins:02d}:{secs:02d}"

        lines = []
        lines.append(header)
        lines.append("[dim]" + "═" * SCENE_W + "[/]")

        # ── Thought bubble (above Claude when thinking/action) ──
        bubble_line = " " * SCENE_W
        if self.phase == "action":
            bub = THOUGHT_BUBBLES[self.frame % len(THOUGHT_BUBBLES)]
            bub_x = pos + SPRITE_W // 2 - 1
            bubble_line = " " * bub_x + bub + " " * max(0, SCENE_W - bub_x - 3)
        lines.append(bubble_line)

        # ── Scene: Claude sprite + optional tool target ──
        show_target = self.phase in ("walking", "action", "returning")
        for i in range(3):
            pad_l = " " * max(pos, 0)

            if show_target:
                target = self.target_art if self.phase != "returning" else DONE_ART
                gap_size = max(1, TARGET_X - pos - SPRITE_W)
                gap = " " * gap_size
                scene_line = pad_l + sprite[i] + gap + target[i]
            else:
                scene_line = pad_l + sprite[i]

            lines.append(scene_line)

        # ── Ground ──
        lines.append(self._render_ground(pos))

        # ── Status label ──
        lines.append("")
        if self.phase == "waiting":
            zzz = "z" * (1 + (self.frame // 8) % 4)
            lines.append(f"  [dim italic]{zzz}... waiting for input[/]")
        elif self.phase == "typing":
            dots = "·" * (1 + self.frame % 4)
            lines.append(f"  [bold bright_green]✎ generating response{dots}[/]")
        elif self.phase in ("walking", "action"):
            lines.append(f"  [bold bright_yellow]⚡ {self.action_label}[/]")
        elif self.phase == "returning":
            lines.append(f"  [bold bright_green]✓ {self.action_label}[/]")

        lines.append("")
        lines.append("[dim]" + "═" * SCENE_W + "[/]")

        # ── Activity log ──
        lines.append(" [bold]Activity:[/]")
        if self.history:
            for h in self.history[-5:]:
                lines.append(f"  {h}")
        else:
            lines.append("  [dim italic]nothing yet...[/]")

        content = "\n".join(lines)
        return Panel(
            content,
            title="[bold bright_red] ▐▛█▜▌ Claude Animator [/]",
            subtitle="[dim]Ctrl+C to quit[/]",
            border_style="bright_red",
            width=PANEL_W,
            padding=(0, 1),
        )


# ═══════════════════════════════════════════════════════════════
#  UDP SERVER  (receives hook events)
# ═══════════════════════════════════════════════════════════════

def run_socket_server(animator, port=9876):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.settimeout(0.1)

    while animator.running:
        try:
            data, _ = sock.recvfrom(4096)
            event = json.loads(data.decode("utf-8"))
            tool_name = event.get("tool_name", "")
            tool_input = event.get("tool_input", {})
            detail = ""
            if isinstance(tool_input, dict):
                detail = (
                    tool_input.get("file_path", "")
                    or tool_input.get("command", "")
                    or tool_input.get("pattern", "")
                    or tool_input.get("query", "")
                    or ""
                )
            animator.set_event(
                event.get("hook_event_name", ""),
                tool_name,
                str(detail),
            )
        except socket.timeout:
            continue
        except Exception:
            continue
    sock.close()


# ═══════════════════════════════════════════════════════════════
#  STARTUP  (kills old instances, launches fresh)
# ═══════════════════════════════════════════════════════════════

def kill_old_instances(port=9876):
    """Kill any existing processes listening on our port."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "UDP" in line:
                parts = line.split()
                pid = parts[-1]
                my_pid = str(os.getpid())
                if pid != my_pid and pid.isdigit():
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=3
                        )
                    except Exception:
                        pass
    except Exception:
        pass


def main():
    # Clean up old instances first
    kill_old_instances()
    time.sleep(0.3)

    animator = ClaudeAnimator()

    def shutdown(sig, frame):
        animator.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start UDP listener
    server_thread = threading.Thread(target=run_socket_server, args=(animator,), daemon=True)
    server_thread.start()

    console = Console()
    console.clear()

    try:
        with Live(
            animator.render_frame(),
            console=console,
            refresh_per_second=FPS,
            screen=True,
        ) as live:
            while animator.running:
                live.update(animator.render_frame())
                time.sleep(1.0 / FPS)
    except KeyboardInterrupt:
        animator.running = False


if __name__ == "__main__":
    main()
