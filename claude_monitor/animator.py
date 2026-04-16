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
import platform
import shutil

from queue import Queue, Empty
import http.server

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

IS_WINDOWS = platform.system() == "Windows"

# Windows sound
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# Windows keyboard
try:
    import msvcrt
except ImportError:
    msvcrt = None

# Unix keyboard
try:
    import select
    import tty
    import termios
    HAS_UNIX_KB = True
except ImportError:
    HAS_UNIX_KB = False

# Windows notifications
try:
    from winotify import Notification
    HAS_WINOTIFY = True
except ImportError:
    HAS_WINOTIFY = False


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
    "name": "Claude",
    "theme": "Red",
    "light_mode": False,
}

APP_NAME = "Claude Monitor"

THEME_COLORS = {
    "Red":     {"bright": "bright_red",     "dim": "dim red",     "lbright": "red",          "ldim": "dark_red"},
    "Blue":    {"bright": "bright_blue",    "dim": "dim blue",    "lbright": "blue",         "ldim": "dark_blue"},
    "Green":   {"bright": "bright_green",   "dim": "dim green",   "lbright": "green",        "ldim": "dark_green"},
    "Yellow":  {"bright": "bright_yellow",  "dim": "dim yellow",  "lbright": "yellow",       "ldim": "dark_orange"},
    "Magenta": {"bright": "bright_magenta", "dim": "dim magenta", "lbright": "magenta",      "ldim": "dark_magenta"},
    "Cyan":    {"bright": "bright_cyan",    "dim": "dim cyan",    "lbright": "dark_cyan",    "ldim": "dark_cyan"},
    "White":   {"bright": "bright_white",   "dim": "dim white",   "lbright": "black",        "ldim": "bright_black"},
}
THEME_NAMES = list(THEME_COLORS.keys())


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


def _play_bell():
    """Terminal bell - bypasses Rich to write directly to terminal."""
    try:
        if not IS_WINDOWS and os.path.exists("/dev/tty"):
            # Write directly to terminal, bypassing Rich's stdout capture
            with open("/dev/tty", "w") as tty_fd:
                tty_fd.write("\a")
                tty_fd.flush()
        else:
            sys.stderr.write("\a")
            sys.stderr.flush()
    except Exception:
        pass


def _play_tone(freq, dur_ms, volume_pct):
    """Play a tone at given volume (0-100). Cross-platform."""
    if volume_pct <= 0:
        return
    amp = max(0.0, min(1.0, volume_pct / 100.0))
    data = _make_tone(freq, dur_ms, amp)

    if IS_WINDOWS and HAS_WINSOUND:
        winsound.PlaySound(data, winsound.SND_MEMORY)
        return

    # Linux/Mac: try aplay (ALSA) or paplay (PulseAudio)
    for cmd in ["aplay", "paplay"]:
        if shutil.which(cmd):
            try:
                args = [cmd, "-q", "-"] if cmd == "aplay" else [cmd, "--raw", "--format=s16le", "--rate=22050", "--channels=1"]
                proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc.stdin.write(data)
                proc.stdin.close()
                proc.wait(timeout=3)
                return
            except Exception:
                continue

    # Fallback: terminal bell
    _play_bell()


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
    """Send a desktop notification. Cross-platform with fallbacks."""
    def _send():
        try:
            # Windows: winotify toast
            if IS_WINDOWS and HAS_WINOTIFY:
                toast = Notification(app_id=APP_NAME, title=title, msg=message)
                toast.show()
                return

            # Linux/Mac: notify-send (desktop) or osascript (Mac)
            if shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message],
                               capture_output=True, timeout=5)
                return

            if platform.system() == "Darwin" and shutil.which("osascript"):
                subprocess.run(["osascript", "-e",
                               f'display notification "{message}" with title "{title}"'],
                               capture_output=True, timeout=5)
                return

            # Fallback: terminal bell + title change via /dev/tty (bypasses Rich)
            try:
                if not IS_WINDOWS and os.path.exists("/dev/tty"):
                    with open("/dev/tty", "w") as tty_fd:
                        tty_fd.write("\a")
                        tty_fd.write(f"\033]2;{title}: {message}\007")
                        tty_fd.write(f"\033]777;notify;{title};{message}\033\\")
                        tty_fd.flush()
                else:
                    _play_bell()
            except Exception:
                _play_bell()
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  SPRITES (generated from theme color)
# ═══════════════════════════════════════════════════════════════

def _build_sprites(theme_name, light_mode=False):
    """Generate all sprite variants for a given theme color."""
    colors = THEME_COLORS.get(theme_name, THEME_COLORS["Red"])
    c = colors["lbright"] if light_mode else colors["bright"]
    d = colors["ldim"] if light_mode else colors["dim"]

    body_top = "▐▛███▜▌"
    body_mid = "▝▜█████▛▘"

    rest = [
        f"  [{d}]{body_top}[/]  ",
        f" [{d}]{body_mid}[/] ",
        f"   [{d}]▘▘ ▝▝[/]   ",
    ]
    stand = [
        f"  [{c}]{body_top}[/]  ",
        f" [{c}]{body_mid}[/] ",
        f"   [{c}]▘▘ ▝▝[/]   ",
    ]
    think = list(stand)

    ask_frames = [
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘▘ ▝▝[/]   "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘▘[/]  [{c}]▝[/]   "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘▘ ▝▝[/]   "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘[/]  [{c}]▝▝[/]   "],
    ]
    walk_r = [
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"  [{c}]▘▘[/]   [{c}]▝▝[/]  "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘▝▝▘[/]   "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"  [{c}]▝▝[/]   [{c}]▘▘[/]  "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▝▘▘▝[/]   "],
    ]
    walk_l = [
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"  [{c}]▝▝[/]   [{c}]▘▘[/]  "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▝▘▘▝[/]   "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"  [{c}]▘▘[/]   [{c}]▝▝[/]  "],
        [f"  [{c}]{body_top}[/]  ", f" [{c}]{body_mid}[/] ", f"   [{c}]▘▝▝▘[/]   "],
    ]

    return {
        "rest": rest, "stand": stand, "think": think,
        "ask_frames": ask_frames, "walk_r": walk_r, "walk_l": walk_l,
    }


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
    {"key": "name",          "label": "Name",          "type": "text",   "max_len": 16},
    {"key": "theme",         "label": "Theme",         "type": "select", "options": THEME_NAMES},
    {"key": "light_mode",    "label": "Light Mode",    "type": "toggle"},
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
        self.editing_text = False
        self.edit_buffer = ""

        # Visual alerts (for cloud/web terminals without sound)
        self.alert_message = ""
        self.alert_style = ""
        self.alert_time = 0.0

        # Sprites (built from theme)
        self.sprites = _build_sprites(config.get("theme", "Red"), config.get("light_mode", False))

    # ───────── settings ─────────

    def toggle_settings(self):
        self.show_settings = not self.show_settings
        if not self.show_settings:
            save_config(self.config)

    def show_alert(self, message, style="bold bright_green"):
        """Show a visual alert banner for a few seconds."""
        self.alert_message = message
        self.alert_style = style
        self.alert_time = time.time()

    def settings_up(self):
        self.settings_cursor = max(0, self.settings_cursor - 1)

    def settings_down(self):
        self.settings_cursor = min(len(SETTINGS_SCHEMA) - 1, self.settings_cursor + 1)

    def settings_toggle_or_enter(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "toggle":
            self.config[item["key"]] = not self.config.get(item["key"], True)
            if item["key"] == "light_mode":
                self._rebuild_sprites()
        elif item["type"] == "text":
            if self.editing_text:
                # Confirm edit
                self.config[item["key"]] = self.edit_buffer or self.config.get(item["key"], "")
                self.editing_text = False
            else:
                # Start editing
                self.editing_text = True
                self.edit_buffer = self.config.get(item["key"], "")

    def settings_text_input(self, ch):
        """Handle a character during text editing."""
        if not self.editing_text:
            return
        item = SETTINGS_SCHEMA[self.settings_cursor]
        max_len = item.get("max_len", 16)
        if ch == "\x08" or ch == "\x7f":  # Backspace / Delete
            self.edit_buffer = self.edit_buffer[:-1]
        elif ch == "\x1b":  # Escape - cancel
            self.editing_text = False
        elif len(ch) == 1 and ch.isprintable() and len(self.edit_buffer) < max_len:
            self.edit_buffer += ch

    def settings_cancel_edit(self):
        """Cancel text editing."""
        self.editing_text = False

    def settings_left(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "slider":
            val = self.config.get(item["key"], item.get("min", 0))
            self.config[item["key"]] = max(item["min"], val - item["step"])
        elif item["type"] == "select":
            options = item["options"]
            cur = self.config.get(item["key"], options[0])
            idx = options.index(cur) if cur in options else 0
            self.config[item["key"]] = options[(idx - 1) % len(options)]
            if item["key"] == "theme":
                self._rebuild_sprites()

    def settings_right(self):
        item = SETTINGS_SCHEMA[self.settings_cursor]
        if item["type"] == "slider":
            val = self.config.get(item["key"], item.get("min", 0))
            self.config[item["key"]] = min(item["max"], val + item["step"])
        elif item["type"] == "select":
            options = item["options"]
            cur = self.config.get(item["key"], options[0])
            idx = options.index(cur) if cur in options else 0
            self.config[item["key"]] = options[(idx + 1) % len(options)]
            if item["key"] == "theme":
                self._rebuild_sprites()

    # ───────── events ─────────

    def set_event(self, event_type, tool_name="", detail="", extra=None):
        vol = self.config.get("volume", 75)

        # Push all events to browser clients
        push_web_event(event_type, tool_name, detail)

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
            self.show_alert("✓ RESPONSE COMPLETE", "bold bright_green")
            if self.config.get("notifications", True):
                send_notification(APP_NAME, "Response complete")
            self.phase = "waiting"
            return

        if event_type == "PermissionRequest":
            self.phase = "asking"
            self.ask_tool = tool_name
            play_question_sound(vol)
            self.show_alert(f"? APPROVAL NEEDED: {tool_name}", "bold bright_yellow")
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
        sp = self.sprites
        if self.phase == "waiting":
            return sp["rest"]
        elif self.phase == "typing":
            idx = (self.frame // 3) % 4
            return sp["walk_r"][idx] if pos >= prev_pos else sp["walk_l"][idx]
        elif self.phase == "walking":
            return sp["walk_r"][(self.frame // 3) % 4]
        elif self.phase == "action":
            return sp["think"]
        elif self.phase == "returning":
            return sp["walk_l"][(self.frame // 3) % 4]
        elif self.phase == "asking":
            return sp["ask_frames"][(self.frame // 5) % 4]
        return sp["stand"]

    # ───────── ground ─────────

    def _is_light(self):
        return self.config.get("light_mode", False)

    def _theme_color(self):
        theme = self.config.get("theme", "Red")
        colors = THEME_COLORS.get(theme, THEME_COLORS["Red"])
        return colors["lbright"] if self._is_light() else colors["bright"]

    def _dim(self):
        """Return the dim/muted style for separators and secondary text."""
        return "bright_black" if self._is_light() else "dim"

    def _text(self):
        """Return the normal text style."""
        return "black" if self._is_light() else "white"

    def _rebuild_sprites(self):
        """Rebuild sprites after theme or light_mode change."""
        self.sprites = _build_sprites(self.config.get("theme", "Red"), self._is_light())

    def _render_ground(self, pos):
        ground = list("─" * SCENE_W)
        tc = self._theme_color()
        d = self._dim()

        if self.phase == "waiting":
            return f"[{d}]" + "".join(ground) + "[/]"
        elif self.phase == "typing":
            foot = min(max(pos + SPRITE_W // 2, 0), SCENE_W - 1)
            ground[foot] = "●"
            return f"[{d}]" + "".join(ground[:foot]) + f"[{tc}]" + ground[foot] + f"[/][{d}]" + "".join(ground[foot+1:]) + "[/]"
        elif self.phase == "walking":
            cx = min(pos + SPRITE_W // 2, SCENE_W - 1)
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(cx):
                ground[i] = "·"
            ground[min(cx, SCENE_W - 1)] = "►"
            for i in range(cx + 1, tx):
                ground[i] = "·"
            s = "".join(ground)
            return f"[{d}]{s[:cx]}[{tc}]{s[cx]}[/][{d}]{s[cx+1:]}[/]"
        elif self.phase == "action":
            pulse = "⣾⣽⣻⢿⡿⣟⣯⣷"
            p = pulse[self.frame % len(pulse)]
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(tx):
                ground[i] = "·"
            ground[tx] = p
            s = "".join(ground)
            return f"[{d}]{s[:tx]}[{tc}]{s[tx]}[/][{d}]{s[tx+1:]}[/]"
        elif self.phase == "returning":
            tx = min(TARGET_X + TARGET_W // 2, SCENE_W - 1)
            for i in range(tx + 1):
                ground[i] = "·"
            ground[tx] = "✓"
            s = "".join(ground)
            return f"[{d}]{s[:tx]}[green]{s[tx]}[/][{d}]{s[tx+1:]}[/]"
        elif self.phase == "asking":
            foot = min(max(pos + SPRITE_W // 2, 0), SCENE_W - 1)
            blink = "◆" if (self.frame // 6) % 2 == 0 else "◇"
            ground[foot] = blink
            return f"[{d}]" + "".join(ground[:foot]) + "[bold bright_yellow]" + ground[foot] + f"[/][{d}]" + "".join(ground[foot+1:]) + "[/]"

        return f"[{d}]" + "".join(ground) + "[/]"

    # ───────── settings panel ─────────

    def _render_settings(self):
        d = self._dim()
        t = self._text()
        lines = []
        lines.append("")
        lines.append(f"  [bold {t}]SETTINGS[/]")
        lines.append(f"  [{d}]" + "─" * 40 + "[/]")
        lines.append("")

        for idx, item in enumerate(SETTINGS_SCHEMA):
            key = item["key"]
            label = item["label"]
            selected = idx == self.settings_cursor
            arrow = f"[bold {self._theme_color()}]▸[/]" if selected else " "

            if item["type"] == "toggle":
                val = self.config.get(key, True)
                tag = "[bold bright_green]ON [/]" if val else "[bold bright_red]OFF[/]"
                line = f"  {arrow}  {label:<18s}  [{tag}]"
                if selected:
                    line += f"    [{d}]Enter: toggle[/]"

            elif item["type"] == "slider":
                val = self.config.get(key, 0)
                bar = volume_bar(val)
                pct = f"{val:>3d}%"
                line = f"  {arrow}  {label:<18s}  {bar} {pct}"
                if selected:
                    line += f"  [{d}]←→ / h,l[/]"

            elif item["type"] == "text":
                if selected and self.editing_text:
                    cursor = "▏" if (self.frame // 6) % 2 == 0 else " "
                    line = f"  {arrow}  {label:<18s}  [bold {t}]{self.edit_buffer}{cursor}[/]"
                    line += f"  [{d}]Enter: save  Esc: cancel[/]"
                else:
                    val = self.config.get(key, "")
                    line = f"  {arrow}  {label:<18s}  [bold {t}]{val}[/]"
                    if selected:
                        line += f"    [{d}]Enter: edit[/]"

            elif item["type"] == "select":
                val = self.config.get(key, item["options"][0])
                sc = THEME_COLORS.get(val, {}).get("bright", "white") if key == "theme" else t
                line = f"  {arrow}  {label:<18s}  [{sc}]◀ {val} ▶[/]"
                if selected:
                    line += f"  [{d}]←→ / h,l[/]"

            lines.append(line)

        lines.append("")
        lines.append(f"  [{d}]" + "─" * 40 + "[/]")
        lines.append(f"  [{d}]↑↓ or j/k  Navigate        Enter  Edit/Toggle[/]")
        lines.append(f"  [{d}]←→ or h/l  Adjust/Cycle    S      Save & close[/]")
        lines.append("")

        tc = self._theme_color()
        return Panel(
            "\n".join(lines),
            title=f"[bold {tc}] ⚙ Settings [/]",
            subtitle=f"[{d}]S to close[/]",
            border_style=tc,
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

        d = self._dim()
        t = self._text()
        tc = self._theme_color()

        state_tags = {
            "waiting":   f"[{d}]IDLE[/]",
            "typing":    "[bold bright_green]TYPING[/]",
            "walking":   "[bold bright_yellow]TOOL USE[/]",
            "action":    "[bold bright_yellow]WORKING[/]",
            "returning": "[bold bright_green]RETURNING[/]",
            "asking":    "[bold bright_yellow]WAITING[/]",
        }
        name = self.config.get("name", "Claude")
        state = state_tags.get(self.phase, "")
        header = f" [bold {tc}]◗[/] [bold {t}]{name}[/]  [{d}]│[/]  {state}  [{d}]│[/]  Tools: [bold]{self.total_tools}[/]  [{d}]│[/]  {mins:02d}:{secs:02d}"

        lines = [header, f"[{d}]" + "═" * SCENE_W + "[/]"]

        # Visual alert banner (shown for 5 seconds)
        if self.alert_message and (time.time() - self.alert_time) < 5.0:
            blink = (self.frame // 4) % 2 == 0
            if blink:
                pad_total = max(0, SCENE_W - len(self.alert_message) - 4)
                pad_l = pad_total // 2
                pad_r = pad_total - pad_l
                lines.append(f"[{self.alert_style}]{'─' * pad_l}  {self.alert_message}  {'─' * pad_r}[/]")
            else:
                lines.append("")
        elif self.alert_message:
            self.alert_message = ""

        # Stats
        if self.config.get("stats_panel", True):
            cost_str = f"${self.cost_usd:.3f}" if self.cost_usd > 0 else "$0.00"
            model_str = self.model_name if self.model_name != "..." else f"[{d}]waiting...[/]"
            bar = progress_bar(self.context_pct, 14)
            ctx_str = f"{self.context_pct:.0f}%"
            vol = self.config.get("volume", 75)
            vol_icon = "🔇" if vol == 0 else ("🔈" if vol <= 33 else ("🔉" if vol <= 66 else "🔊"))

            lines.append(f"  [bold]{model_str}[/] [{d}]│[/] {cost_str} [{d}]│[/] Ctx:{bar}{ctx_str} [{d}]│[/] {vol_icon}{vol}%")
            lines.append(f"[{d}]" + "─" * SCENE_W + "[/]")

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
            lines.append(f"  [{d} italic]{zzz}... waiting for input[/]")
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
        lines.append(f"[{d}]" + "═" * SCENE_W + "[/]")

        # Activity log
        if self.config.get("activity_log", True):
            lines.append(f" [bold {t}]Activity:[/]")
            if self.history:
                for h in self.history[-5:]:
                    lines.append(f"  {h}")
            else:
                lines.append(f"  [{d} italic]nothing yet...[/]")
            lines.append("")

        lines.append(f"  [{d}]Press [bold]S[/{d}][{d}] for settings[/]")

        return Panel(
            "\n".join(lines),
            title=f"[bold {tc}] ▐▛█▜▌ Claude Monitor [/]",
            subtitle=f"[{d}]Web: http://localhost:{WEB_PORT}  |  Ctrl+C to quit[/]",
            border_style=tc,
            width=PANEL_W,
            padding=(0, 1),
        )


# ═══════════════════════════════════════════════════════════════
#  KEYBOARD
# ═══════════════════════════════════════════════════════════════

def _keyboard_windows(animator):
    """Windows keyboard input via msvcrt."""
    while animator.running:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getch()

                # Text editing mode - capture all keys
                if animator.editing_text:
                    if ch == b'\r':
                        animator.settings_toggle_or_enter()  # confirm
                    elif ch == b'\x1b':
                        animator.settings_cancel_edit()
                    elif ch == b'\x08':  # Backspace
                        animator.settings_text_input("\x08")
                    elif ch not in (b'\xe0', b'\x00'):
                        try:
                            animator.settings_text_input(ch.decode("utf-8"))
                        except Exception:
                            pass
                    continue

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


def _keyboard_unix(animator):
    """Unix keyboard input via termios + select."""
    if not sys.stdin.isatty():
        return
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while animator.running:
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1)

                # Text editing mode
                if animator.editing_text:
                    if ch in ('\r', '\n'):
                        animator.settings_toggle_or_enter()
                    elif ch == '\x1b':
                        animator.settings_cancel_edit()
                    elif ch in ('\x08', '\x7f'):
                        animator.settings_text_input("\x08")
                    elif ch.isprintable():
                        animator.settings_text_input(ch)
                    continue

                if ch in ('s', 'S'):
                    animator.toggle_settings()
                elif animator.show_settings and ch == 'k':   # vim up
                    animator.settings_up()
                elif animator.show_settings and ch == 'j':   # vim down
                    animator.settings_down()
                elif animator.show_settings and ch == 'h':   # vim left
                    animator.settings_left()
                elif animator.show_settings and ch == 'l':   # vim right
                    animator.settings_right()
                elif ch == '\x1b':
                    # Escape or arrow key sequence (0.15s timeout for web terminals)
                    if select.select([sys.stdin], [], [], 0.15)[0]:
                        ch2 = sys.stdin.read(1)
                        if ch2 == '[':
                            if select.select([sys.stdin], [], [], 0.15)[0]:
                                ch3 = sys.stdin.read(1)
                                if ch3 == 'A':    # Up
                                    animator.settings_up()
                                elif ch3 == 'B':  # Down
                                    animator.settings_down()
                                elif ch3 == 'D':  # Left
                                    animator.settings_left()
                                elif ch3 == 'C':  # Right
                                    animator.settings_right()
                    elif animator.show_settings:
                        animator.toggle_settings()
                elif ch in ('\r', '\n'):
                    animator.settings_toggle_or_enter()
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception:
            pass


def keyboard_listener(animator):
    """Cross-platform keyboard listener."""
    if IS_WINDOWS and msvcrt:
        _keyboard_windows(animator)
    elif HAS_UNIX_KB:
        _keyboard_unix(animator)
    # No keyboard support available - settings won't be interactive


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
#  WEB SERVER (browser sound + notifications)
# ═══════════════════════════════════════════════════════════════

WEB_PORT = 7777

WEB_PAGE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Claude Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'Cascadia Code', 'Fira Code', monospace;
         display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .container { text-align: center; padding: 2rem; }
  .logo { color: #f85149; font-size: 2rem; margin-bottom: 1rem; }
  .status { font-size: 1.5rem; margin: 1rem 0; padding: 1rem 2rem; border-radius: 8px;
            border: 1px solid #30363d; background: #161b22; min-width: 300px; }
  .idle { color: #8b949e; }
  .typing { color: #3fb950; }
  .tool { color: #d29922; }
  .done { color: #3fb950; }
  .asking { color: #d29922; }
  .tool-name { font-size: 0.9rem; color: #8b949e; margin-top: 0.5rem; }
  .connected { color: #3fb950; font-size: 0.8rem; margin-top: 1rem; }
  .disconnected { color: #f85149; font-size: 0.8rem; margin-top: 1rem; }
  .controls { margin-top: 2rem; }
  .btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 0.5rem 1rem;
         border-radius: 6px; cursor: pointer; font-family: inherit; margin: 0.25rem; }
  .btn:hover { background: #30363d; }
  .btn.active { border-color: #3fb950; color: #3fb950; }
  .volume { margin-top: 1rem; color: #8b949e; }
  .flash { animation: flash 0.5s ease-in-out 3; }
  @keyframes flash { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
</head><body>
<div class="container">
  <div class="logo">▐▛█▜▌ Claude Monitor</div>
  <div class="status idle" id="status">Waiting for connection...</div>
  <div class="tool-name" id="tool"></div>
  <div class="controls">
    <button class="btn active" id="soundBtn" onclick="toggleSound()">🔊 Sound</button>
    <button class="btn active" id="notifBtn" onclick="toggleNotif()">🔔 Notifications</button>
    <button class="btn" onclick="testSound()">🎵 Test</button>
  </div>
  <div class="volume">Volume: <input type="range" id="vol" min="0" max="100" value="50"
       oninput="document.getElementById('volLabel').textContent=this.value+'%'">
       <span id="volLabel">50%</span></div>
  <div id="conn" class="connected">● Connected</div>
</div>
<script>
let soundEnabled = true, notifEnabled = true;
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

function getVol() { return document.getElementById('vol').value / 100 * 0.4; }

function playTone(freq, dur) {
  if (!soundEnabled) return;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.connect(gain); gain.connect(audioCtx.destination);
  osc.type = 'sine'; osc.frequency.value = freq;
  gain.gain.value = getVol();
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
  osc.start(); osc.stop(audioCtx.currentTime + dur);
}

function playCompletion() {
  playTone(523, 0.15);
  setTimeout(() => playTone(659, 0.15), 130);
  setTimeout(() => playTone(784, 0.25), 260);
}

function playQuestion() {
  playTone(784, 0.2);
  setTimeout(() => playTone(988, 0.3), 200);
  setTimeout(() => playTone(784, 0.2), 550);
  setTimeout(() => playTone(988, 0.4), 750);
}

function testSound() {
  audioCtx.resume().then(() => playCompletion());
}

function showNotif(title, body) {
  if (!notifEnabled || Notification.permission !== 'granted') return;
  new Notification(title, { body, icon: '🤖' });
}

function toggleSound() {
  soundEnabled = !soundEnabled;
  const btn = document.getElementById('soundBtn');
  btn.textContent = soundEnabled ? '🔊 Sound' : '🔇 Sound';
  btn.classList.toggle('active', soundEnabled);
}

function toggleNotif() {
  notifEnabled = !notifEnabled;
  const btn = document.getElementById('notifBtn');
  btn.textContent = notifEnabled ? '🔔 Notifications' : '🔕 Notifications';
  btn.classList.toggle('active', notifEnabled);
}

// Request notification permission on first interaction
document.addEventListener('click', () => {
  audioCtx.resume();
  if (Notification.permission === 'default') Notification.requestPermission();
}, { once: true });

// SSE connection with auto-reconnect
function connect() {
  const evtSource = new EventSource('events');
  const el = document.getElementById('status');
  const toolEl = document.getElementById('tool');
  const connEl = document.getElementById('conn');

  evtSource.onopen = () => {
    connEl.className = 'connected'; connEl.textContent = '● Connected';
  };

  evtSource.onmessage = (e) => {
    const d = JSON.parse(e.data);
    el.className = 'status';
    toolEl.textContent = '';

    if (d.type === 'UserPromptSubmit') {
      el.className = 'status typing'; el.textContent = '✎ Generating response...';
    } else if (d.type === 'Stop') {
      el.className = 'status done flash'; el.textContent = '✓ Response complete';
      audioCtx.resume().then(() => playCompletion());
      showNotif('Claude Monitor', 'Response complete');
    } else if (d.type === 'PermissionRequest') {
      el.className = 'status asking flash'; el.textContent = '? Approval needed';
      toolEl.textContent = d.tool || '';
      audioCtx.resume().then(() => playQuestion());
      showNotif('Claude Monitor', 'Approval needed: ' + (d.tool || ''));
    } else if (d.type === 'PreToolUse') {
      el.className = 'status tool'; el.textContent = '⚡ ' + (d.detail || d.tool || 'Working...');
      toolEl.textContent = d.tool || '';
    } else if (d.type === 'PostToolUse') {
      el.className = 'status typing'; el.textContent = '✎ Generating response...';
    } else if (d.type === 'StatusUpdate') {
      // Ignore status updates in UI
    }
  };

  evtSource.onerror = () => {
    connEl.className = 'disconnected'; connEl.textContent = '● Disconnected - reconnecting...';
    evtSource.close();
    setTimeout(connect, 3000);
  };
}

connect();
</script>
</body></html>
"""

# SSE client queues
_sse_queues = []


def push_web_event(event_type, tool_name="", detail=""):
    """Push an event to all connected SSE browser clients."""
    event = {"type": event_type, "tool": tool_name, "detail": detail}
    data = json.dumps(event)
    for q in list(_sse_queues):
        try:
            q.put_nowait(data)
        except Exception:
            pass


class _WebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(WEB_PAGE.encode("utf-8"))
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Flush a large padding block to push past proxy buffers
            # Many proxies (nginx, GCP) buffer ~4-8KB before forwarding
            padding = ": " + " " * 4096 + "\n\n"
            self.wfile.write(padding.encode())
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()

            q = Queue()
            _sse_queues.append(q)
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    except Empty:
                        # Send keepalive comment
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                if q in _sse_queues:
                    _sse_queues.remove(q)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


def run_web_server(port=WEB_PORT):
    """Start the web server for browser-based sound and notifications."""
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), _WebHandler)
        server.serve_forever()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════

def kill_old_instances(port=9876):
    """Kill any previous monitor instances holding the UDP port."""
    my_pid = str(os.getpid())

    if IS_WINDOWS:
        try:
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if f":{port}" in line and "UDP" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid != my_pid and pid.isdigit():
                        try:
                            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=3)
                        except Exception:
                            pass
        except Exception:
            pass
    else:
        # Linux/Mac: try lsof, then fuser
        try:
            result = subprocess.run(["lsof", "-i", f"UDP:{port}", "-t"],
                                    capture_output=True, text=True, timeout=5)
            for pid in result.stdout.strip().splitlines():
                pid = pid.strip()
                if pid.isdigit() and pid != my_pid:
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=3)
        except Exception:
            try:
                result = subprocess.run(["fuser", f"{port}/udp"],
                                        capture_output=True, text=True, timeout=5)
                for pid in result.stdout.split():
                    pid = pid.strip()
                    if pid.isdigit() and pid != my_pid:
                        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=3)
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
    threading.Thread(target=run_web_server, daemon=True).start()

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
