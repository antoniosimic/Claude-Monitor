#!/usr/bin/env python3
"""
Claude Monitor v5
Real-time ASCII animation + cost monitoring + settings with volume control.

States:
  waiting   → Claude idle, dimmed sprite, breathing zzz
  typing    → Claude generating text, bounces left-right
  walking   → Claude approaching a tool target (ease-in-out)
  action    → Claude working at the tool, pulsing
  returning → Claude walking back, then resumes typing
  asking    → Claude waiting for user confirmation

Controls:
  S         → Open/close settings
  ↑↓        → Navigate settings
  ←→        → Adjust volume slider
  Enter     → Toggle on/off settings
  Ctrl+C    → Quit
"""

import json
import time
import sys
import math
import struct
import io
import wave
import threading
import socket
import signal
import subprocess
import os

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False

try:
    import msvcrt
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

try:
    from winotify import Notification
    HAS_NOTIFY = True
except ImportError:
    HAS_NOTIFY = False


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".claude-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "volume": 75,          # 0-100
    "activity_log": True,
    "stats_panel": True,
    "notifications": True,
}

APP_NAME = "Claude Monitor"


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  SCENE CONSTANTS
# ═══════════════════════════════════════════════════════════════

SCENE_W = 58
PANEL_W = SCENE_W + 8
SPRITE_W = 11
TARGET_W = 10
IDLE_X = (SCENE_W - SPRITE_W) // 2
TARGET_X = SCENE_W - TARGET_W - 2
BOUNCE_RANGE = 10
FPS = 12


# ═══════════════════════════════════════════════════════════════
#  SOUND ENGINE (WAV generation with true volume control)
# ═══════════════════════════════════════════════════════════════

def _make_tone(freq, dur_ms, amplitude=0.5, sample_rate=22050):
    """Generate a sine wave tone as WAV bytes."""
    n = int(sample_rate * dur_ms / 1000)
    frames = b''.join(
        struct.pack('<h', int(32767 * amplitude * math.sin(2 * math.pi * freq * i / sample_rate)))
        for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(frames)
    return buf.getvalue()


def _play_tone(freq, dur_ms, volume_pct):
    """Play a tone at given volume (0-100)."""
    if not HAS_SOUND or volume_pct <= 0:
        return
    amp = max(0.0, min(1.0, volume_pct / 100.0))
    data = _make_tone(freq, dur_ms, amp)
    winsound.PlaySound(data, winsound.SND_MEMORY)


def play_completion_sound(volume_pct):
    """Pleasant ascending chime: C5 → E5 → G5."""
    if volume_pct <= 0:
        return
    def _play():
        _play_tone(523, 120, volume_pct)
        _play_tone(659, 120, volume_pct)
        _play_tone(784, 200, volume_pct)
    threading.Thread(target=_play, daemon=True).start()


def play_question_sound(volume_pct):
    """Attention chime: G5 → B5 → G5 → B5."""
    if volume_pct <= 0:
        return
    def _play():
        _play_tone(784, 180, volume_pct)
        _play_tone(988, 250, volume_pct)
        time.sleep(0.1)
        _play_tone(784, 180, volume_pct)
        _play_tone(988, 350, volume_pct)
    threading.Thread(target=_play, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  DESKTOP NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

def send_notification(title, message):
    """Send a Windows desktop toast notification."""
    if not HAS_NOTIFY:
        return
    def _send():
        try:
            toast = Notification(app_id=APP_NAME, title=title, msg=message)
            toast.show()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  SPRITES
# ═══════════════════════════════════════════════════════════════

S_REST = [
    "  [dim red]▐▛███▜▌[/]  ",
    " [dim red]▝▜█████▛▘[/] ",
    "   [dim red]▘▘ ▝▝[/]   ",
]

S_STAND = [
    "  [bright_red]▐▛███▜▌[/]  ",
    " [bright_red]▝▜█████▛▘[/] ",
    "   [bright_red]▘▘ ▝▝[/]   ",
]

S_ASK_FRAMES = [
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘▘ ▝▝[/]   "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘▘[/]  [bright_red]▝[/]   "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘▘ ▝▝[/]   "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘[/]  [bright_red]▝▝[/]   "],
]

S_WALK_R = [
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "  [bright_red]▘▘[/]   [bright_red]▝▝[/]  "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘▝▝▘[/]   "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "  [bright_red]▝▝[/]   [bright_red]▘▘[/]  "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▝▘▘▝[/]   "],
]

S_WALK_L = [
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "  [bright_red]▝▝[/]   [bright_red]▘▘[/]  "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▝▘▘▝[/]   "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "  [bright_red]▘▘[/]   [bright_red]▝▝[/]  "],
    ["  [bright_red]▐▛███▜▌[/]  ", " [bright_red]▝▜█████▛▘[/] ", "   [bright_red]▘▝▝▘[/]   "],
]

S_THINK = [
    "  [bright_red]▐▛███▜▌[/]  ",
    " [bright_red]▝▜█████▛▘[/] ",
    "   [bright_red]▘▘ ▝▝[/]   ",
]

THOUGHT_BUBBLES = ["[yellow]°[/] ", "[yellow]•[/] ", "[yellow]°[/]•", " [yellow]•[/]°"]
QUESTION_BUBBLES = ["[bold bright_yellow]?[/]  ", " [bold bright_yellow]?[/] ", "  [bold bright_yellow]?[/]", " [bold bright_yellow]?[/] "]


# ═══════════════════════════════════════════════════════════════
#  TOOL TARGETS
# ═══════════════════════════════════════════════════════════════

FILE_ART     = ["[cyan]┌────────┐[/]", "[cyan]│ [bold]FILE[/]   [cyan]│[/]", "[cyan]└────────┘[/]"]
INTERNET_ART = ["[blue]┌────────┐[/]", "[blue]│ [bold]WEB[/]    [blue]│[/]", "[blue]└────────┘[/]"]
TERMINAL_ART = ["[green]┌────────┐[/]", "[green]│ [bold]>_ RUN[/] [green]│[/]", "[green]└────────┘[/]"]
EDIT_ART     = ["[magenta]┌────────┐[/]", "[magenta]│ [bold]EDIT[/]   [magenta]│[/]", "[magenta]└────────┘[/]"]
SEARCH_ART   = ["[cyan]┌────────┐[/]", "[cyan]│ [bold]SEARCH[/] [cyan]│[/]", "[cyan]└────────┘[/]"]
DONE_ART     = ["[green]┌────────┐[/]", "[green]│  [bold]DONE[/]  [green]│[/]", "[green]└────────┘[/]"]
AGENT_ART    = ["[yellow]┌────────┐[/]", "[yellow]│ [bold]AGENT[/]  [yellow]│[/]", "[yellow]└────────┘[/]"]
ASK_ART      = ["[bold bright_yellow]┌────────┐[/]", "[bold bright_yellow]│  YES?  │[/]", "[bold bright_yellow]└────────┘[/]"]

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
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def ease_in_out(t):
    t = max(0.0, min(1.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def progress_bar(pct, width=20):
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    empty = width - filled
    color = "bright_green" if pct < 50 else ("bright_yellow" if pct < 80 else "bright_red")
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"


def volume_bar(vol, width=10):
    """Render volume bar 0-100."""
    filled = int(width * vol / 100)
    empty = width - filled
    if vol == 0:
        return "[dim]" + "░" * width + "[/]"
    color = "bright_green" if vol <= 50 else ("bright_yellow" if vol <= 75 else "bright_red")
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"


# ═══════════════════════════════════════════════════════════════
#  SETTINGS ITEMS
# ═══════════════════════════════════════════════════════════════

SETTINGS_SCHEMA = [
    {"key": "volume",        "label": "Volume",        "type": "slider", "min": 0, "max": 100, "step": 10},
    {"key": "notifications", "label": "Notifications", "type": "toggle"},
    {"key": "activity_log",  "label": "Activity Log",  "type": "toggle"},
    {"key": "stats_panel",   "label": "Stats Panel",   "type": "toggle"},
]


# ═══════════════════════════════════════════════════════════════
#  ANIMATOR
# ═══════════════════════════════════════════════════════════════

class ClaudeAnimator:
    def __init__(self, config):
        self.frame = 0
        self.running = True
        self.phase = "waiting"
        self.total_tools = 0
        self.start_time = time.time()
        self.history = []
        self.config = config

        # Walk
        self.walk_progress = 0.0
        self.walk_start = IDLE_X
        self.walk_end = TARGET_X - SPRITE_W - 2

        # Tool
        self.target_art = FILE_ART
        self.action_label = ""
        self.ask_tool = ""

        # Stats
        self.model_name = "..."
        self.model_id = ""
        self.cost_usd = 0.0
        self.context_pct = 0.0

        # Settings UI
        self.show_settings = False
        self.settings_cursor = 0

    # ───────── settings ─────────

    def toggle_settings(self):
        self.show_settings = not self.show_settings
        if not self.show_settings:
            save_config(self.config)

    def settings_up(self):
        self.settings_cursor = max(0, self.settings_cursor - 1)

    def settings_down(self):
        self.settings_cursor = min(len(SETTINGS_SCHEMA) - 1, self.settings_cursor + 1)

    def settings_toggle_or_enter(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "toggle":
            self.config[item["key"]] = not self.config.get(item["key"], True)

    def settings_left(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "slider":
            val = self.config.get(item["key"], item.get("min", 0))
            self.config[item["key"]] = max(item["min"], val - item["step"])

    def settings_right(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "slider":
            val = self.config.get(item["key"], item.get("min", 0))
            self.config[item["key"]] = min(item["max"], val + item["step"])

    # ───────── events ─────────

    def set_event(self, event_type, tool_name="", detail="", extra=None):
        vol = self.config.get("volume", 75)

        if event_type == "StatusUpdate" and extra:
            model = extra.get("model", {})
            cost = extra.get("cost", {})
            ctx = extra.get("context_window", {})
            self.model_name = model.get("display_name", self.model_name)
            self.model_id = model.get("id", self.model_id)
            self.cost_usd = cost.get("total_cost_usd", self.cost_usd)
            self.context_pct = ctx.get("used_percentage", self.context_pct)
            return

        if event_type == "UserPromptSubmit":
            self.phase = "typing"
            return

        if event_type == "Stop":
            play_completion_sound(vol)
            if self.config.get("notifications", True):
                send_notification(APP_NAME, "Response complete")
            self.phase = "waiting"
            return

        if event_type == "PermissionRequest":
            self.phase = "asking"
            self.ask_tool = tool_name
            play_question_sound(vol)
            if self.config.get("notifications", True):
                send_notification(APP_NAME, f"Approval needed: {tool_name}")
            self.history.append(f"[bold bright_yellow]  ? Approve: {tool_name}[/]")
            if len(self.history) > 8:
                self.history.pop(0)
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
            if len(self.history) > 8:
                self.history.pop(0)

        elif event_type == "PostToolUse":
            self.phase = "returning"
            self.walk_progress = 0.0
            self.action_label = f"Done: {tool_name}"

    # ───────── position ─────────

    def _get_pos(self):
        if self.phase == "waiting":
            return IDLE_X
        elif self.phase == "typing":
            return IDLE_X + int(BOUNCE_RANGE * math.sin(self.frame * 0.1))
        elif self.phase == "walking":
            return int(self.walk_start + (self.walk_end - self.walk_start) * ease_in_out(self.walk_progress))
        elif self.phase == "action":
            return self.walk_end
        elif self.phase == "returning":
            return int(self.walk_end + (self.walk_start - self.walk_end) * ease_in_out(self.walk_progress))
        elif self.phase == "asking":
            return IDLE_X + int(3 * math.sin(self.frame * 0.15))
        return IDLE_X

    def _get_sprite(self, pos, prev_pos):
        if self.phase == "waiting":
            return S_REST
        elif self.phase == "typing":
            idx = (self.frame // 3) % 4
            return S_WALK_R[idx] if pos >= prev_pos else S_WALK_L[idx]
        elif self.phase == "walking":
            return S_WALK_R[(self.frame // 3) % 4]
        elif self.phase == "action":
            return S_THINK
        elif self.phase == "returning":
            return S_WALK_L[(self.frame // 3) % 4]
        elif self.phase == "asking":
            return S_ASK_FRAMES[(self.frame // 5) % 4]
        return S_STAND

    # ───────── ground ─────────

    def _render_ground(self, pos):
        ground = list("─" * SCENE_W)

        if self.phase == "waiting":
            return "[dim]" + "".join(ground) + "[/]"
        elif self.phase == "typing":
            foot = min(max(pos + SPRITE_W // 2, 0), SCENE_W - 1)
            ground[foot] = "●"
            return "[dim]" + "".join(ground[:foot]) + "[bright_red]" + ground[foot] + "[/][dim]" + "".join(ground[foot+1:]) + "[/]"
        elif self.phase == "walking":
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
        elif self.phase == "asking":
            foot = min(max(pos + SPRITE_W // 2, 0), SCENE_W - 1)
            blink = "◆" if (self.frame // 6) % 2 == 0 else "◇"
            ground[foot] = blink
            return "[dim]" + "".join(ground[:foot]) + "[bold bright_yellow]" + ground[foot] + "[/][dim]" + "".join(ground[foot+1:]) + "[/]"

        return "[dim]" + "".join(ground) + "[/]"

    # ───────── settings panel ─────────

    def _render_settings(self):
        lines = []
        lines.append("")
        lines.append("  [bold bright_white]SETTINGS[/]")
        lines.append("  [dim]" + "─" * 40 + "[/]")
        lines.append("")

        for idx, item in enumerate(SETTINGS_SCHEMA):
            key = item["key"]
            label = item["label"]
            selected = idx == self.settings_cursor
            arrow = "[bold bright_yellow]▸[/]" if selected else " "

            if item["type"] == "toggle":
                val = self.config.get(key, True)
                tag = "[bold bright_green]ON [/]" if val else "[bold bright_red]OFF[/]"
                line = f"  {arrow}  {label:<18s}  [{tag}]"
                if selected:
                    line += "    [dim]Enter: toggle[/]"

            elif item["type"] == "slider":
                val = self.config.get(key, 0)
                bar = volume_bar(val)
                pct = f"{val:>3d}%"
                line = f"  {arrow}  {label:<18s}  {bar} {pct}"
                if selected:
                    line += "  [dim]←→[/]"

            lines.append(line)

        lines.append("")
        lines.append("  [dim]" + "─" * 40 + "[/]")
        lines.append("  [dim]↑↓  Navigate[/]")
        lines.append("  [dim]←→  Adjust volume[/]")
        lines.append("  [dim]Enter  Toggle on/off[/]")
        lines.append("  [dim]S   Save & close[/]")
        lines.append("")

        return Panel(
            "\n".join(lines),
            title="[bold bright_yellow] ⚙ Settings [/]",
            subtitle="[dim]S to close[/]",
            border_style="bright_yellow",
            width=PANEL_W,
            padding=(0, 1),
        )

    # ───────── main render ─────────

    def render_frame(self):
        self.frame += 1

        if self.show_settings:
            return self._render_settings()

        # Update walk
        if self.phase == "walking":
            self.walk_progress = min(self.walk_progress + 0.06, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "action"
        elif self.phase == "returning":
            self.walk_progress = min(self.walk_progress + 0.08, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "typing"

        pos = self._get_pos()
        prev_pos = IDLE_X + int(BOUNCE_RANGE * math.sin((self.frame - 1) * 0.1)) if self.phase == "typing" else pos - 1
        sprite = self._get_sprite(pos, prev_pos)

        # Header
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)

        state_tags = {
            "waiting":   "[dim]IDLE[/]",
            "typing":    "[bold bright_green]TYPING[/]",
            "walking":   "[bold bright_yellow]TOOL USE[/]",
            "action":    "[bold bright_yellow]WORKING[/]",
            "returning": "[bold bright_green]RETURNING[/]",
            "asking":    "[bold bright_yellow]WAITING[/]",
        }
        state = state_tags.get(self.phase, "")
        header = f" [bold bright_red]◗[/] [bold white]Claude[/]  [dim]│[/]  {state}  [dim]│[/]  Tools: [bold]{self.total_tools}[/]  [dim]│[/]  {mins:02d}:{secs:02d}"

        lines = [header, "[dim]" + "═" * SCENE_W + "[/]"]

        # Stats
        if self.config.get("stats_panel", True):
            cost_str = f"${self.cost_usd:.3f}" if self.cost_usd > 0 else "$0.00"
            model_str = self.model_name if self.model_name != "..." else "[dim]waiting...[/]"
            bar = progress_bar(self.context_pct, 14)
            ctx_str = f"{self.context_pct:.0f}%"
            vol = self.config.get("volume", 75)
            vol_icon = "🔇" if vol == 0 else ("🔈" if vol <= 33 else ("🔉" if vol <= 66 else "🔊"))

            lines.append(f"  [bold]{model_str}[/] [dim]│[/] {cost_str} [dim]│[/] Ctx:{bar}{ctx_str} [dim]│[/] {vol_icon}")
            lines.append("[dim]" + "─" * SCENE_W + "[/]")

        # Bubble
        bubble_line = " " * SCENE_W
        bub_x = max(pos + SPRITE_W // 2 - 1, 0)
        if self.phase == "action":
            bubble_line = " " * bub_x + THOUGHT_BUBBLES[self.frame % len(THOUGHT_BUBBLES)]
        elif self.phase == "asking":
            bubble_line = " " * bub_x + QUESTION_BUBBLES[self.frame % len(QUESTION_BUBBLES)]
        lines.append(bubble_line)

        # Scene
        show_target = self.phase in ("walking", "action", "returning")
        show_ask = self.phase == "asking"

        for i in range(3):
            pad = " " * max(pos, 0)
            if show_target:
                target = self.target_art if self.phase != "returning" else DONE_ART
                gap = " " * max(1, TARGET_X - pos - SPRITE_W)
                lines.append(pad + sprite[i] + gap + target[i])
            elif show_ask:
                ask_x = IDLE_X + SPRITE_W + 6
                gap = " " * max(1, ask_x - pos - SPRITE_W)
                if (self.frame // 8) % 2 == 0:
                    lines.append(pad + sprite[i] + gap + ASK_ART[i])
                else:
                    lines.append(pad + sprite[i])
            else:
                lines.append(pad + sprite[i])

        lines.append(self._render_ground(pos))

        # Status
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
        elif self.phase == "asking":
            dots = "·" * (1 + (self.frame // 4) % 4)
            tool_info = f" ({self.ask_tool})" if self.ask_tool else ""
            lines.append(f"  [bold bright_yellow]? Waiting for confirmation{tool_info}{dots}[/]")

        lines.append("")
        lines.append("[dim]" + "═" * SCENE_W + "[/]")

        # Activity log
        if self.config.get("activity_log", True):
            lines.append(" [bold]Activity:[/]")
            if self.history:
                for h in self.history[-5:]:
                    lines.append(f"  {h}")
            else:
                lines.append("  [dim italic]nothing yet...[/]")
            lines.append("")

        lines.append(f"  [dim]Press [bold]S[/dim][dim] for settings[/]")

        return Panel(
            "\n".join(lines),
            title="[bold bright_red] ▐▛█▜▌ Claude Monitor [/]",
            subtitle="[dim]Ctrl+C to quit[/]",
            border_style="bright_red",
            width=PANEL_W,
            padding=(0, 1),
        )


# ═══════════════════════════════════════════════════════════════
#  KEYBOARD
# ═══════════════════════════════════════════════════════════════

def keyboard_listener(animator):
    if not HAS_KEYBOARD:
        return
    while animator.running:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b's', b'S'):
                    animator.toggle_settings()
                elif ch == b'\x1b' and animator.show_settings:
                    animator.toggle_settings()
                elif animator.show_settings:
                    if ch == b'\xe0' or ch == b'\x00':
                        ch2 = msvcrt.getch()
                        if ch2 == b'H':      # Up
                            animator.settings_up()
                        elif ch2 == b'P':    # Down
                            animator.settings_down()
                        elif ch2 == b'K':    # Left
                            animator.settings_left()
                        elif ch2 == b'M':    # Right
                            animator.settings_right()
                    elif ch == b'\r':        # Enter
                        animator.settings_toggle_or_enter()
        except Exception:
            pass
        time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════
#  UDP SERVER
# ═══════════════════════════════════════════════════════════════

def run_socket_server(animator, port=9876):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.settimeout(0.1)
    while animator.running:
        try:
            data, _ = sock.recvfrom(8192)
            event = json.loads(data.decode("utf-8"))
            event_type = event.get("hook_event_name", "")
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
            extra = event if event_type == "StatusUpdate" else None
            animator.set_event(event_type, tool_name, str(detail), extra)
        except socket.timeout:
            continue
        except Exception:
            continue
    sock.close()


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════

def kill_old_instances(port=9876):
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if f":{port}" in line and "UDP" in line:
                parts = line.split()
                pid = parts[-1]
                if pid != str(os.getpid()) and pid.isdigit():
                    try:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=3)
                    except Exception:
                        pass
    except Exception:
        pass


def main():
    kill_old_instances()
    time.sleep(0.3)

    config = load_config()
    animator = ClaudeAnimator(config)

    def shutdown(sig, frame):
        save_config(animator.config)
        animator.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=run_socket_server, args=(animator,), daemon=True).start()
    threading.Thread(target=keyboard_listener, args=(animator,), daemon=True).start()

    console = Console()
    console.clear()

    try:
        with Live(animator.render_frame(), console=console, refresh_per_second=FPS, screen=True) as live:
            while animator.running:
                live.update(animator.render_frame())
                time.sleep(1.0 / FPS)
    except KeyboardInterrupt:
        save_config(animator.config)
        animator.running = False


if __name__ == "__main__":
    main()
