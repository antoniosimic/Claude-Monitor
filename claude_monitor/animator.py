#!/usr/bin/env python3
"""
Claude Monitor v6 — multi-session
Real-time ASCII animation + cost monitoring + settings, supporting many
concurrent Claude Code sessions from a single monitor process.

Per-session state (phase, walking, stats, history) lives in SessionAnimator.
Global state (config, sprites, settings UI, alerts, layout) lives in MonitorApp.

Layouts:
  single   → full sprite for the focused session (identical to v5 for 1 session)
  grid     → up to 4 sessions side-by-side as compact panels
  compact  → vertical list of one-liner status rows (5+ sessions)
  auto     → picks single/grid/compact based on session count

Controls:
  S         → Open/close settings
  M         → Cycle layout (auto/single/grid/compact)
  Tab       → Cycle focus to next session
  1-9       → Jump focus to session N
  ↑↓        → Navigate settings
  ←→        → Adjust slider / cycle option
  Enter     → Toggle / save edit
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

import http.server

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

IS_WINDOWS = platform.system() == "Windows"

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import select
    import tty
    import termios
    HAS_UNIX_KB = True
except ImportError:
    HAS_UNIX_KB = False

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
    "volume": 75,
    "activity_log": True,
    "stats_panel": True,
    "notifications": True,
    "name": "Claude",
    "theme": "Red",
    "light_mode": False,
    "layout": "auto",
    "session_timeout_min": 30,
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

# Per-session accent colors used to differentiate sessions visually.
SESSION_ACCENTS = ["bright_red", "bright_cyan", "bright_green", "bright_magenta",
                   "bright_yellow", "bright_blue", "bright_white"]


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

# Compact (grid) scene dimensions
GRID_SCENE_W = 36
GRID_PANEL_W = GRID_SCENE_W + 6
GRID_IDLE_X = (GRID_SCENE_W - SPRITE_W) // 2
GRID_TARGET_X = GRID_SCENE_W - TARGET_W - 1


# ═══════════════════════════════════════════════════════════════
#  SOUND ENGINE
# ═══════════════════════════════════════════════════════════════

def _make_tone(freq, dur_ms, amplitude=0.5, sample_rate=22050):
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
    try:
        if not IS_WINDOWS and os.path.exists("/dev/tty"):
            with open("/dev/tty", "w") as tty_fd:
                tty_fd.write("\a")
                tty_fd.flush()
        else:
            sys.stderr.write("\a")
            sys.stderr.flush()
    except Exception:
        pass


def _play_tone(freq, dur_ms, volume_pct):
    if volume_pct <= 0:
        return
    amp = max(0.0, min(1.0, volume_pct / 100.0))
    data = _make_tone(freq, dur_ms, amp)

    if IS_WINDOWS and HAS_WINSOUND:
        winsound.PlaySound(data, winsound.SND_MEMORY)
        return

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

    _play_bell()


def play_completion_sound(volume_pct):
    if volume_pct <= 0:
        return
    def _play():
        _play_tone(523, 120, volume_pct)
        _play_tone(659, 120, volume_pct)
        _play_tone(784, 200, volume_pct)
    threading.Thread(target=_play, daemon=True).start()


def play_question_sound(volume_pct):
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
    def _send():
        try:
            if IS_WINDOWS and HAS_WINOTIFY:
                toast = Notification(app_id=APP_NAME, title=title, msg=message)
                toast.show()
                return

            if shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message],
                               capture_output=True, timeout=5)
                return

            if platform.system() == "Darwin" and shutil.which("osascript"):
                subprocess.run(["osascript", "-e",
                               f'display notification "{message}" with title "{title}"'],
                               capture_output=True, timeout=5)
                return

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
#  SPRITES
# ═══════════════════════════════════════════════════════════════

def _build_sprites(theme_name, light_mode=False):
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
    filled = int(width * vol / 100)
    empty = width - filled
    if vol == 0:
        return "[dim]" + "░" * width + "[/]"
    color = "bright_green" if vol <= 50 else ("bright_yellow" if vol <= 75 else "bright_red")
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"


def derive_session_name(cwd, fallback):
    """Pick a friendly short label for a session from its working directory."""
    if not cwd:
        return fallback
    base = os.path.basename(cwd.rstrip("/\\"))
    return base or fallback


# ═══════════════════════════════════════════════════════════════
#  SETTINGS ITEMS
# ═══════════════════════════════════════════════════════════════

LAYOUT_OPTIONS = ["auto", "single", "grid", "compact"]

SETTINGS_SCHEMA = [
    {"key": "name",          "label": "Name",          "type": "text",   "max_len": 16},
    {"key": "theme",         "label": "Theme",         "type": "select", "options": THEME_NAMES},
    {"key": "light_mode",    "label": "Light Mode",    "type": "toggle"},
    {"key": "layout",        "label": "Layout",        "type": "select", "options": LAYOUT_OPTIONS},
    {"key": "volume",        "label": "Volume",        "type": "slider", "min": 0, "max": 100, "step": 10},
    {"key": "notifications", "label": "Notifications", "type": "toggle"},
    {"key": "activity_log",  "label": "Activity Log",  "type": "toggle"},
    {"key": "stats_panel",   "label": "Stats Panel",   "type": "toggle"},
]


# ═══════════════════════════════════════════════════════════════
#  SESSION ANIMATOR (per-session state)
# ═══════════════════════════════════════════════════════════════

class SessionAnimator:
    """Holds the animation + activity state for ONE Claude Code session."""

    def __init__(self, session_id, app, cwd="", index=0):
        self.session_id = session_id
        self.app = app
        self.cwd = cwd
        self.index = index  # 0-based slot index, used for accent color and number key
        self.name = derive_session_name(cwd, f"Session {index + 1}")

        self.frame = 0
        self.phase = "waiting"
        self.total_tools = 0
        self.start_time = time.time()
        self.last_activity = time.time()
        self.history = []

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

    @property
    def config(self):
        return self.app.config

    @property
    def sprites(self):
        return self.app.sprites

    @property
    def accent(self):
        return SESSION_ACCENTS[self.index % len(SESSION_ACCENTS)]

    def short_id(self):
        if not self.session_id or self.session_id == "default":
            return "default"
        return self.session_id[:8]

    def update_cwd(self, cwd):
        if cwd and cwd != self.cwd:
            self.cwd = cwd
            self.name = derive_session_name(cwd, f"Session {self.index + 1}")

    # ───────── events ─────────

    def set_event(self, event_type, tool_name="", detail="", extra=None):
        self.last_activity = time.time()
        vol = self.config.get("volume", 75)

        push_web_event(self.session_id, self.name, event_type, tool_name, detail)

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
            self.app.show_alert(f"✓ {self.name}: COMPLETE", "bold bright_green")
            if self.config.get("notifications", True):
                send_notification(APP_NAME, f"{self.name}: Response complete")
            self.phase = "waiting"
            return

        if event_type == "PermissionRequest":
            self.phase = "asking"
            self.ask_tool = tool_name
            play_question_sound(vol)
            self.app.show_alert(f"? {self.name}: APPROVE {tool_name}", "bold bright_yellow")
            if self.config.get("notifications", True):
                send_notification(APP_NAME, f"{self.name}: Approve {tool_name}")
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

    # ───────── position / sprite ─────────

    def _get_pos(self, idle_x=IDLE_X, walk_end_target=None):
        we = self.walk_end if walk_end_target is None else walk_end_target
        if self.phase == "waiting":
            return idle_x
        elif self.phase == "typing":
            return idle_x + int(BOUNCE_RANGE * math.sin(self.frame * 0.1))
        elif self.phase == "walking":
            return int(self.walk_start + (we - self.walk_start) * ease_in_out(self.walk_progress))
        elif self.phase == "action":
            return we
        elif self.phase == "returning":
            return int(we + (self.walk_start - we) * ease_in_out(self.walk_progress))
        elif self.phase == "asking":
            return idle_x + int(3 * math.sin(self.frame * 0.15))
        return idle_x

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

    def _render_ground(self, pos, scene_w=SCENE_W, target_x=TARGET_X):
        ground = list("─" * scene_w)
        tc = self.app._theme_color()
        d = self.app._dim()

        if self.phase == "waiting":
            return f"[{d}]" + "".join(ground) + "[/]"
        elif self.phase == "typing":
            foot = min(max(pos + SPRITE_W // 2, 0), scene_w - 1)
            ground[foot] = "●"
            return f"[{d}]" + "".join(ground[:foot]) + f"[{tc}]" + ground[foot] + f"[/][{d}]" + "".join(ground[foot+1:]) + "[/]"
        elif self.phase == "walking":
            cx = min(pos + SPRITE_W // 2, scene_w - 1)
            tx = min(target_x + TARGET_W // 2, scene_w - 1)
            for i in range(cx):
                ground[i] = "·"
            ground[min(cx, scene_w - 1)] = "►"
            for i in range(cx + 1, tx):
                ground[i] = "·"
            s = "".join(ground)
            return f"[{d}]{s[:cx]}[{tc}]{s[cx]}[/][{d}]{s[cx+1:]}[/]"
        elif self.phase == "action":
            pulse = "⣾⣽⣻⢿⡿⣟⣯⣷"
            p = pulse[self.frame % len(pulse)]
            tx = min(target_x + TARGET_W // 2, scene_w - 1)
            for i in range(tx):
                ground[i] = "·"
            ground[tx] = p
            s = "".join(ground)
            return f"[{d}]{s[:tx]}[{tc}]{s[tx]}[/][{d}]{s[tx+1:]}[/]"
        elif self.phase == "returning":
            tx = min(target_x + TARGET_W // 2, scene_w - 1)
            for i in range(tx + 1):
                ground[i] = "·"
            ground[tx] = "✓"
            s = "".join(ground)
            return f"[{d}]{s[:tx]}[green]{s[tx]}[/][{d}]{s[tx+1:]}[/]"
        elif self.phase == "asking":
            foot = min(max(pos + SPRITE_W // 2, 0), scene_w - 1)
            blink = "◆" if (self.frame // 6) % 2 == 0 else "◇"
            ground[foot] = blink
            return f"[{d}]" + "".join(ground[:foot]) + "[bold bright_yellow]" + ground[foot] + f"[/][{d}]" + "".join(ground[foot+1:]) + "[/]"

        return f"[{d}]" + "".join(ground) + "[/]"

    # ───────── advance physics for one frame ─────────

    def advance(self):
        self.frame += 1
        if self.phase == "walking":
            self.walk_progress = min(self.walk_progress + 0.06, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "action"
        elif self.phase == "returning":
            self.walk_progress = min(self.walk_progress + 0.08, 1.0)
            if self.walk_progress >= 1.0:
                self.phase = "typing"

    # ───────── full-size scene render (single layout) ─────────

    def render_scene_panel(self, focused=True):
        """Full-size panel — identical layout to v5 single-session view."""
        d = self.app._dim()
        t = self.app._text()
        tc = self.app._theme_color()

        pos = self._get_pos()
        prev_pos = IDLE_X + int(BOUNCE_RANGE * math.sin((self.frame - 1) * 0.1)) if self.phase == "typing" else pos - 1
        sprite = self._get_sprite(pos, prev_pos)

        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)

        state_tags = {
            "waiting":   f"[{d}]IDLE[/]",
            "typing":    "[bold bright_green]TYPING[/]",
            "walking":   "[bold bright_yellow]TOOL USE[/]",
            "action":    "[bold bright_yellow]WORKING[/]",
            "returning": "[bold bright_green]RETURNING[/]",
            "asking":    "[bold bright_yellow]WAITING[/]",
        }
        # Use the configured "name" if there is only ONE session (v5 behavior),
        # otherwise show the per-session name so users can tell sessions apart.
        if len(self.app.sessions) <= 1:
            display_name = self.config.get("name", "Claude")
        else:
            display_name = self.name
        state = state_tags.get(self.phase, "")
        header = f" [bold {tc}]◗[/] [bold {t}]{display_name}[/]  [{d}]│[/]  {state}  [{d}]│[/]  Tools: [bold]{self.total_tools}[/]  [{d}]│[/]  {mins:02d}:{secs:02d}"

        lines = [header, f"[{d}]" + "═" * SCENE_W + "[/]"]

        # Visual alert banner (5s)
        if self.app.alert_message and (time.time() - self.app.alert_time) < 5.0:
            blink = (self.frame // 4) % 2 == 0
            if blink:
                pad_total = max(0, SCENE_W - len(self.app.alert_message) - 4)
                pad_l = pad_total // 2
                pad_r = pad_total - pad_l
                lines.append(f"[{self.app.alert_style}]{'─' * pad_l}  {self.app.alert_message}  {'─' * pad_r}[/]")
            else:
                lines.append("")
        elif self.app.alert_message and (time.time() - self.app.alert_time) >= 5.0:
            self.app.alert_message = ""

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

        # Status text
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

        # Title and subtitle differ in single vs multi-session mode
        if len(self.app.sessions) <= 1:
            title = f"[bold {tc}] ▐▛█▜▌ Claude Monitor [/]"
            subtitle = f"[{d}]Web: http://localhost:{WEB_PORT}  |  S: settings  |  Ctrl+C[/]"
        else:
            ac = self.accent
            focus_marker = "★" if focused else " "
            title = f"[bold {ac}] {focus_marker} {self.name}  [{d}]({self.short_id()})[/] [/]"
            subtitle = f"[{d}]M: layout  Tab: focus  S: settings[/]"

        lines.append(f"  [{d}]Press [bold]S[/{d}][{d}] for settings[/]")

        border = self.accent if (focused and len(self.app.sessions) > 1) else tc
        return Panel(
            "\n".join(lines),
            title=title,
            subtitle=subtitle,
            border_style=border,
            width=PANEL_W,
            padding=(0, 1),
        )

    # ───────── grid (mid-size) panel ─────────

    def render_grid_panel(self, focused=False):
        """Compact panel for grid layout — keeps the walking sprite, smaller scene."""
        d = self.app._dim()
        t = self.app._text()
        tc = self.app._theme_color()
        ac = self.accent

        # Recompute walk endpoints scaled to the grid scene width.
        grid_walk_end = GRID_TARGET_X - SPRITE_W - 1
        pos = self._get_pos(idle_x=GRID_IDLE_X, walk_end_target=grid_walk_end)
        prev_pos = GRID_IDLE_X + int(BOUNCE_RANGE * math.sin((self.frame - 1) * 0.1)) if self.phase == "typing" else pos - 1
        sprite = self._get_sprite(pos, prev_pos)

        # Header line: name + state + tools + cost
        state_tags = {
            "waiting":   f"[{d}]IDLE[/]",
            "typing":    "[bold bright_green]TYPING[/]",
            "walking":   "[bold bright_yellow]TOOL[/]",
            "action":    "[bold bright_yellow]WORK[/]",
            "returning": "[bold bright_green]RTN[/]",
            "asking":    "[bold bright_yellow]ASK[/]",
        }
        state = state_tags.get(self.phase, "")
        cost_str = f"${self.cost_usd:.2f}"
        ctx_str = f"{int(self.context_pct)}%"
        focus_marker = "★" if focused else " "
        header = f" [bold {ac}]{focus_marker}[/] [bold {t}]{self.name[:14]}[/]  [{d}]│[/]  {state}  [{d}]│[/]  T:[bold]{self.total_tools}[/]  [{d}]│[/]  {cost_str}  [{d}]│[/]  {ctx_str}"

        lines = [header, f"[{d}]" + "─" * GRID_SCENE_W + "[/]"]

        # Scene (3 sprite rows)
        show_target = self.phase in ("walking", "action", "returning")
        show_ask = self.phase == "asking"

        for i in range(3):
            pad = " " * max(pos, 0)
            if show_target:
                target = self.target_art if self.phase != "returning" else DONE_ART
                gap = " " * max(1, GRID_TARGET_X - pos - SPRITE_W)
                lines.append(pad + sprite[i] + gap + target[i])
            elif show_ask:
                ask_x = GRID_IDLE_X + SPRITE_W + 4
                gap = " " * max(1, ask_x - pos - SPRITE_W)
                if (self.frame // 8) % 2 == 0:
                    lines.append(pad + sprite[i] + gap + ASK_ART[i])
                else:
                    lines.append(pad + sprite[i])
            else:
                lines.append(pad + sprite[i])

        lines.append(self._render_ground(pos, scene_w=GRID_SCENE_W, target_x=GRID_TARGET_X))

        # Action label
        if self.phase == "waiting":
            label = f"[{d} italic]waiting...[/]"
        elif self.phase == "typing":
            label = "[bold bright_green]✎ generating[/]"
        elif self.phase in ("walking", "action"):
            label = f"[bold bright_yellow]⚡ {self.action_label[:28]}[/]"
        elif self.phase == "returning":
            label = f"[bold bright_green]✓ {self.action_label[:28]}[/]"
        elif self.phase == "asking":
            label = f"[bold bright_yellow]? approve {self.ask_tool[:18]}[/]"
        else:
            label = ""
        lines.append(f"  {label}")

        return Panel(
            "\n".join(lines),
            title=f"[bold {ac}] {self.name}  [{d}]#{self.index + 1}[/] [/]",
            subtitle=f"[{d}]{self.short_id()}[/]",
            border_style=ac if focused else d,
            width=GRID_PANEL_W,
            padding=(0, 1),
        )

    # ───────── compact one-liner ─────────

    def render_compact_row(self, focused=False):
        d = self.app._dim()
        ac = self.accent
        t = self.app._text()

        phase_dot = {
            "waiting":   f"[{d}]●[/]",
            "typing":    "[bold bright_green]●[/]",
            "walking":   "[bold bright_yellow]●[/]",
            "action":    "[bold bright_yellow]◉[/]",
            "returning": "[bold bright_green]◉[/]",
            "asking":    "[bold bright_yellow]◆[/]",
        }.get(self.phase, "●")

        phase_lbl = {
            "waiting": "idle", "typing": "typing", "walking": "tool",
            "action": "working", "returning": "done", "asking": "approve?",
        }.get(self.phase, "")

        focus_marker = f"[bold {ac}]★[/]" if focused else " "
        slot = f"[bold {ac}]#{self.index + 1}[/]"
        name = f"[bold {t}]{self.name[:14]:<14s}[/]"
        cost_str = f"${self.cost_usd:.2f}"
        last = self.action_label[:30] if self.phase != "waiting" else ""
        elapsed = int(time.time() - self.last_activity)
        when = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m"

        return Text.from_markup(
            f"  {focus_marker} {slot}  {phase_dot} {name}  "
            f"[{d}]│[/]  [bold]{phase_lbl:<8s}[/]  "
            f"[{d}]│[/]  T:[bold]{self.total_tools:>3d}[/]  "
            f"[{d}]│[/]  {cost_str:>7s}  "
            f"[{d}]│[/]  {when:>4s}  "
            f"[{d}]│[/]  [{d}]{last}[/]"
        )


# ═══════════════════════════════════════════════════════════════
#  MONITOR APP (orchestrator + global UI state)
# ═══════════════════════════════════════════════════════════════

class MonitorApp:
    """Top-level app: owns config, sprites, sessions dict, settings UI."""

    def __init__(self, config):
        self.config = config
        self.sprites = _build_sprites(config.get("theme", "Red"), config.get("light_mode", False))

        self.sessions = {}            # session_id -> SessionAnimator
        self.session_order = []       # creation order, for index/focus
        self.focused_idx = 0

        # UI overlay state
        self.show_settings = False
        self.settings_cursor = 0
        self.editing_text = False
        self.edit_buffer = ""

        # Visual alert banner
        self.alert_message = ""
        self.alert_style = ""
        self.alert_time = 0.0

        # Layout override (if user pressed M); empty = use config["layout"]
        self.layout_override = ""
        self.frame = 0
        self.running = True

    # ───────── session management ─────────

    def get_or_create_session(self, session_id, cwd=""):
        sid = session_id or "default"
        if sid not in self.sessions:
            session = SessionAnimator(sid, self, cwd=cwd, index=len(self.session_order))
            self.sessions[sid] = session
            self.session_order.append(sid)
        else:
            self.sessions[sid].update_cwd(cwd)
        return self.sessions[sid]

    def handle_event(self, session_id, event_type, tool_name, detail, extra, cwd=""):
        session = self.get_or_create_session(session_id, cwd)
        session.set_event(event_type, tool_name, detail, extra)

    def focused_session(self):
        if not self.session_order:
            return None
        idx = max(0, min(self.focused_idx, len(self.session_order) - 1))
        return self.sessions[self.session_order[idx]]

    def cycle_focus(self):
        if self.session_order:
            self.focused_idx = (self.focused_idx + 1) % len(self.session_order)

    def jump_to_session(self, idx):
        if 0 <= idx < len(self.session_order):
            self.focused_idx = idx

    def cycle_layout(self):
        cur = self.layout_override or self.config.get("layout", "auto")
        i = LAYOUT_OPTIONS.index(cur) if cur in LAYOUT_OPTIONS else 0
        new = LAYOUT_OPTIONS[(i + 1) % len(LAYOUT_OPTIONS)]
        self.layout_override = new
        self.config["layout"] = new

    def effective_layout(self):
        mode = self.layout_override or self.config.get("layout", "auto")
        if mode != "auto":
            return mode
        n = len(self.sessions)
        if n <= 1:
            return "single"
        if n <= 4:
            return "grid"
        return "compact"

    # ───────── alerts ─────────

    def show_alert(self, message, style="bold bright_green"):
        self.alert_message = message
        self.alert_style = style
        self.alert_time = time.time()

    # ───────── theme helpers ─────────

    def _is_light(self):
        return self.config.get("light_mode", False)

    def _theme_color(self):
        theme = self.config.get("theme", "Red")
        colors = THEME_COLORS.get(theme, THEME_COLORS["Red"])
        return colors["lbright"] if self._is_light() else colors["bright"]

    def _dim(self):
        return "bright_black" if self._is_light() else "dim"

    def _text(self):
        return "black" if self._is_light() else "white"

    def _rebuild_sprites(self):
        self.sprites = _build_sprites(self.config.get("theme", "Red"), self._is_light())

    # ───────── settings (global) ─────────

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
            if item["key"] == "light_mode":
                self._rebuild_sprites()
        elif item["type"] == "text":
            if self.editing_text:
                self.config[item["key"]] = self.edit_buffer or self.config.get(item["key"], "")
                self.editing_text = False
            else:
                self.editing_text = True
                self.edit_buffer = self.config.get(item["key"], "")

    def settings_text_input(self, ch):
        if not self.editing_text:
            return
        item = SETTINGS_SCHEMA[self.settings_cursor]
        max_len = item.get("max_len", 16)
        if ch == "\x08" or ch == "\x7f":
            self.edit_buffer = self.edit_buffer[:-1]
        elif ch == "\x1b":
            self.editing_text = False
        elif len(ch) == 1 and ch.isprintable() and len(self.edit_buffer) < max_len:
            self.edit_buffer += ch

    def settings_cancel_edit(self):
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
            if item["key"] == "layout":
                self.layout_override = self.config["layout"]

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
            if item["key"] == "layout":
                self.layout_override = self.config["layout"]

    # ───────── settings panel render ─────────

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
        lines.append(f"  [{d}]Sessions: {len(self.sessions)}  |  Layout: {self.effective_layout()}[/]")
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

    # ───────── empty (no sessions yet) ─────────

    def _render_empty(self):
        d = self._dim()
        t = self._text()
        tc = self._theme_color()
        lines = [
            "",
            f"  [bold {t}]Waiting for Claude Code activity...[/]",
            "",
            f"  [{d}]No sessions connected yet.[/]",
            "",
            f"  [{d}]1. Open a NEW terminal[/]",
            f"  [{d}]2. Start Claude Code (claude){'/'}",
            f"  [{d}]3. Send a prompt — this monitor will activate[/]",
            "",
            f"  [{d}]Web dashboard: http://localhost:{WEB_PORT}[/]",
            "",
            f"  [{d}]S: settings  |  Ctrl+C: quit[/]",
        ]
        return Panel(
            "\n".join(lines),
            title=f"[bold {tc}] ▐▛█▜▌ Claude Monitor [/]",
            subtitle=f"[{d}]waiting...[/]",
            border_style=tc,
            width=PANEL_W,
            padding=(1, 2),
        )

    # ───────── main render ─────────

    def render(self):
        self.frame += 1

        # Advance every session's animation each frame (so all of them animate
        # in parallel — even sessions not currently focused).
        for sess in self.sessions.values():
            sess.advance()

        if self.show_settings:
            return self._render_settings()

        if not self.sessions:
            return self._render_empty()

        layout = self.effective_layout()

        if layout == "single":
            return self.focused_session().render_scene_panel(focused=True)

        if layout == "grid":
            sessions = [self.sessions[sid] for sid in self.session_order]
            focused_id = self.session_order[self.focused_idx] if self.session_order else None
            panels = [s.render_grid_panel(focused=(s.session_id == focused_id)) for s in sessions]
            cols = Columns(panels, equal=True, expand=False, padding=(0, 1))
            return Panel(
                cols,
                title=f"[bold {self._theme_color()}] ▐▛█▜▌ Claude Monitor — {len(sessions)} sessions [/]",
                subtitle=f"[{self._dim()}]M: layout  Tab: focus  1-9: jump  S: settings[/]",
                border_style=self._theme_color(),
                padding=(0, 1),
            )

        # compact
        sessions = [self.sessions[sid] for sid in self.session_order]
        focused_id = self.session_order[self.focused_idx] if self.session_order else None
        rows = []
        d = self._dim()
        tc = self._theme_color()
        rows.append(Text.from_markup(
            f"  [{d}]    #   ● {'Name':<14s}  │  {'state':<8s}  "
            f"│  Tools  │   Cost   │ Last  │  Activity[/]"
        ))
        rows.append(Text.from_markup(f"  [{d}]" + "─" * 90 + "[/]"))
        for s in sessions:
            rows.append(s.render_compact_row(focused=(s.session_id == focused_id)))

        # Aggregate stats line
        total_tools = sum(s.total_tools for s in sessions)
        total_cost = sum(s.cost_usd for s in sessions)
        active = sum(1 for s in sessions if s.phase != "waiting")
        rows.append(Text.from_markup(f"  [{d}]" + "─" * 90 + "[/]"))
        rows.append(Text.from_markup(
            f"  [{d}]Total:[/] [bold]{len(sessions)}[/] sessions  "
            f"[{d}]│[/]  [bold]{active}[/] active  "
            f"[{d}]│[/]  [bold]{total_tools}[/] tools  "
            f"[{d}]│[/]  [bold]${total_cost:.3f}[/]"
        ))
        if self.alert_message and (time.time() - self.alert_time) < 5.0:
            rows.append(Text.from_markup(f"  [{self.alert_style}]► {self.alert_message}[/]"))

        return Panel(
            Group(*rows),
            title=f"[bold {tc}] ▐▛█▜▌ Claude Monitor — {len(sessions)} sessions [/]",
            subtitle=f"[{d}]M: layout  Tab: focus  1-9: jump  S: settings[/]",
            border_style=tc,
            padding=(0, 1),
        )


# ═══════════════════════════════════════════════════════════════
#  KEYBOARD
# ═══════════════════════════════════════════════════════════════

def _keyboard_windows(app):
    while app.running:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getch()

                if app.editing_text:
                    if ch == b'\r':
                        app.settings_toggle_or_enter()
                    elif ch == b'\x1b':
                        app.settings_cancel_edit()
                    elif ch == b'\x08':
                        app.settings_text_input("\x08")
                    elif ch not in (b'\xe0', b'\x00'):
                        try:
                            app.settings_text_input(ch.decode("utf-8"))
                        except Exception:
                            pass
                    continue

                if ch in (b's', b'S'):
                    app.toggle_settings()
                elif ch == b'\x1b' and app.show_settings:
                    app.toggle_settings()
                elif app.show_settings:
                    if ch == b'\xe0' or ch == b'\x00':
                        ch2 = msvcrt.getch()
                        if ch2 == b'H':
                            app.settings_up()
                        elif ch2 == b'P':
                            app.settings_down()
                        elif ch2 == b'K':
                            app.settings_left()
                        elif ch2 == b'M':
                            app.settings_right()
                    elif ch == b'\r':
                        app.settings_toggle_or_enter()
                else:
                    # Multi-session keys (only when settings closed)
                    if ch in (b'm', b'M'):
                        app.cycle_layout()
                    elif ch == b'\t':
                        app.cycle_focus()
                    elif ch in (b'1', b'2', b'3', b'4', b'5', b'6', b'7', b'8', b'9'):
                        app.jump_to_session(int(ch.decode()) - 1)
        except Exception:
            pass
        time.sleep(0.05)


def _keyboard_unix(app):
    if not sys.stdin.isatty():
        return
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while app.running:
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1)

                if app.editing_text:
                    if ch in ('\r', '\n'):
                        app.settings_toggle_or_enter()
                    elif ch == '\x1b':
                        app.settings_cancel_edit()
                    elif ch in ('\x08', '\x7f'):
                        app.settings_text_input("\x08")
                    elif ch.isprintable():
                        app.settings_text_input(ch)
                    continue

                if ch in ('s', 'S'):
                    app.toggle_settings()
                elif app.show_settings and ch == 'k':
                    app.settings_up()
                elif app.show_settings and ch == 'j':
                    app.settings_down()
                elif app.show_settings and ch == 'h':
                    app.settings_left()
                elif app.show_settings and ch == 'l':
                    app.settings_right()
                elif ch == '\x1b':
                    if select.select([sys.stdin], [], [], 0.15)[0]:
                        ch2 = sys.stdin.read(1)
                        if ch2 == '[':
                            if select.select([sys.stdin], [], [], 0.15)[0]:
                                ch3 = sys.stdin.read(1)
                                if ch3 == 'A':
                                    app.settings_up()
                                elif ch3 == 'B':
                                    app.settings_down()
                                elif ch3 == 'D':
                                    app.settings_left()
                                elif ch3 == 'C':
                                    app.settings_right()
                    elif app.show_settings:
                        app.toggle_settings()
                elif ch in ('\r', '\n'):
                    app.settings_toggle_or_enter()
                elif not app.show_settings:
                    if ch in ('m', 'M'):
                        app.cycle_layout()
                    elif ch == '\t':
                        app.cycle_focus()
                    elif ch in ('1', '2', '3', '4', '5', '6', '7', '8', '9'):
                        app.jump_to_session(int(ch) - 1)
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception:
            pass


def keyboard_listener(app):
    if IS_WINDOWS and msvcrt:
        _keyboard_windows(app)
    elif HAS_UNIX_KB:
        _keyboard_unix(app)


# ═══════════════════════════════════════════════════════════════
#  UDP SERVER (routes events by session_id)
# ═══════════════════════════════════════════════════════════════

def run_socket_server(app, port=9876):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.settimeout(0.1)
    while app.running:
        try:
            data, _ = sock.recvfrom(8192)
            event = json.loads(data.decode("utf-8"))
            event_type = event.get("hook_event_name", "")
            tool_name = event.get("tool_name", "")
            tool_input = event.get("tool_input", {})
            session_id = event.get("session_id", "") or "default"
            cwd = event.get("cwd", "") or event.get("workspace", {}).get("current_dir", "") if isinstance(event.get("workspace"), dict) else event.get("cwd", "")
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
            app.handle_event(session_id, event_type, tool_name, str(detail), extra, cwd=cwd)
        except socket.timeout:
            continue
        except Exception:
            continue
    sock.close()


# ═══════════════════════════════════════════════════════════════
#  WEB SERVER
# ═══════════════════════════════════════════════════════════════

WEB_PORT = 7777

# Web event buffer and app reference
_web_events = []
_web_event_counter = 0
_web_app = None


def push_web_event(session_id, session_name, event_type, tool_name="", detail=""):
    """Push event to the web event buffer (session-aware)."""
    global _web_event_counter
    _web_event_counter += 1
    event = {
        "session_id": session_id or "default",
        "session_name": session_name or "default",
        "type": event_type,
        "tool": tool_name,
        "detail": detail,
        "id": _web_event_counter,
        "ts": time.time(),
    }
    _web_events.append(event)
    if len(_web_events) > 200:
        _web_events.pop(0)


def _phase_meta(phase):
    return {
        "waiting":   ("💤", "Idle", "#8b949e"),
        "typing":    ("✍️", "Typing", "#3fb950"),
        "walking":   ("🚶", "Walking to tool", "#d29922"),
        "action":    ("⚙️", "Using tool", "#d29922"),
        "returning": ("🔙", "Returning", "#3fb950"),
        "asking":    ("🤔", "Approval needed", "#f85149"),
    }.get(phase, ("💤", "Idle", "#8b949e"))


def _build_session_card(session):
    """One session card for the web dashboard."""
    phase_icon, phase_label, phase_color = _phase_meta(session.phase)
    cost = f"${session.cost_usd:.4f}"
    ctx_val = int(session.context_pct)
    ctx_color = "#3fb950" if ctx_val < 60 else "#d29922" if ctx_val < 85 else "#f85149"
    elapsed = int(time.time() - session.start_time)
    mins, secs = divmod(elapsed, 60)
    hrs, mins = divmod(mins, 60)
    uptime = f"{hrs}h {mins:02d}m {secs:02d}s" if hrs else f"{mins}m {secs:02d}s"
    last_active = int(time.time() - session.last_activity)
    last_str = f"{last_active}s ago" if last_active < 60 else f"{last_active // 60}m ago"

    return f"""
    <div class="session-card phase-{session.phase}">
      <div class="session-head">
        <div class="session-id">
          <span class="session-dot" style="background:{phase_color}"></span>
          <span class="session-name">{session.name}</span>
          <span class="session-num">#{session.index + 1}</span>
        </div>
        <span class="session-phase">{phase_icon} {phase_label}</span>
      </div>
      <div class="session-body">
        <div class="session-stat"><span class="lbl">Model</span><span class="val">{session.model_name}</span></div>
        <div class="session-stat"><span class="lbl">Cost</span><span class="val">{cost}</span></div>
        <div class="session-stat"><span class="lbl">Tools</span><span class="val">{session.total_tools}</span></div>
        <div class="session-stat"><span class="lbl">Up</span><span class="val">{uptime}</span></div>
        <div class="session-stat"><span class="lbl">Last</span><span class="val">{last_str}</span></div>
        <div class="session-stat ctx"><span class="lbl">Ctx {ctx_val}%</span>
          <div class="ctx-bar"><div class="ctx-fill" style="width:{ctx_val}%;background:{ctx_color}"></div></div>
        </div>
      </div>
      <div class="session-action">{session.action_label or '—'}</div>
      <div class="session-id-full">{session.short_id()}{' · ' + session.cwd if session.cwd else ''}</div>
    </div>
    """


def _build_web_page():
    """Render the multi-session dashboard."""
    app = _web_app
    sessions = list(app.sessions.values()) if app else []

    # Notify events: anything Stop / PermissionRequest from any session
    notify_events = json.dumps([
        {"id": e["id"], "type": e["type"], "tool": e.get("tool", ""), "session_name": e.get("session_name", "")}
        for e in _web_events if e.get("type") in ("Stop", "PermissionRequest")
    ])

    # Determine top-line status
    if not sessions:
        top_status = "Waiting for Claude Code activity..."
        top_icon = "💤"
        top_color = "#8b949e"
    else:
        active = [s for s in sessions if s.phase != "waiting"]
        if not active:
            top_status = f"All {len(sessions)} session(s) idle"
            top_icon = "💤"
            top_color = "#8b949e"
        else:
            asking = [s for s in active if s.phase == "asking"]
            if asking:
                top_status = f"{len(asking)} session(s) awaiting approval"
                top_icon = "❓"
                top_color = "#f85149"
            else:
                top_status = f"{len(active)}/{len(sessions)} session(s) working"
                top_icon = "⚡"
                top_color = "#3fb950"

    # Aggregate stats
    total_cost = sum(s.cost_usd for s in sessions)
    total_tools = sum(s.total_tools for s in sessions)

    # Session cards
    cards_html = "".join(_build_session_card(s) for s in sessions) if sessions else \
        '<div class="empty">No sessions yet. Start Claude Code in another terminal — events will appear here.</div>'

    # Activity log (cross-session)
    log_entries = ""
    type_icons = {"UserPromptSubmit": "📝", "PreToolUse": "⚡", "PostToolUse": "✅",
                  "Stop": "🏁", "PermissionRequest": "❓", "StatusUpdate": "📊"}
    for e in list(_web_events[-20:])[::-1]:
        icon = type_icons.get(e.get("type", ""), "•")
        sname = e.get("session_name", "")
        etype = e.get("type", "")
        tool = e.get("tool", "")
        detail = e.get("detail", "")
        desc = tool if tool else detail if detail else etype
        log_entries += (
            f'<div class="log-entry"><span class="log-icon">{icon}</span>'
            f'<span class="log-session">{sname}</span>'
            f'<span class="log-type">{etype}</span>'
            f'<span class="log-desc">{desc}</span></div>'
        )

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>Claude Monitor — {top_status}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, 'Segoe UI', sans-serif; min-height: 100vh; }}
  .header {{ background: linear-gradient(135deg, #1a1e2e 0%, #0d1117 100%);
             border-bottom: 1px solid #21262d; padding: 1rem 2rem; display: flex;
             align-items: center; justify-content: space-between; }}
  .header-left {{ display: flex; align-items: center; gap: 1rem; }}
  .claude-icon {{ width: 40px; height: 40px; }}
  .header-title {{ font-size: 1.2rem; font-weight: 600; color: #f0f3f6; }}
  .header-sub {{ font-size: 0.75rem; color: #8b949e; }}
  .header-right {{ display: flex; gap: 0.5rem; align-items: center; }}
  .badge {{ display: inline-block; background: #21262d; border: 1px solid #30363d;
            border-radius: 20px; padding: 0.25rem 0.75rem; font-size: 0.8rem; }}
  .main {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem; }}

  .top-status {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px;
                 padding: 1.25rem; margin-bottom: 1rem; text-align: center; }}
  .top-icon {{ font-size: 2rem; margin-bottom: 0.25rem; }}
  .top-text {{ font-size: 1.1rem; font-weight: 600; }}

  .agg-stats {{ display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }}
  .agg-stat {{ flex: 1; min-width: 130px; background: #161b22; border: 1px solid #21262d;
               border-radius: 10px; padding: 0.75rem; text-align: center; }}
  .agg-val {{ font-size: 1.2rem; font-weight: 700; color: #f0f3f6; }}
  .agg-lbl {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; }}

  .sessions-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
                    gap: 0.75rem; margin-bottom: 1rem; }}
  .session-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                   padding: 0.85rem; transition: border-color 0.2s; }}
  .session-card.phase-asking {{ border-color: #f85149; box-shadow: 0 0 0 1px #f8514955; }}
  .session-card.phase-typing, .session-card.phase-returning {{ border-color: #3fb950; }}
  .session-card.phase-walking, .session-card.phase-action {{ border-color: #d29922; }}
  .session-head {{ display: flex; justify-content: space-between; align-items: center;
                   margin-bottom: 0.6rem; padding-bottom: 0.5rem; border-bottom: 1px solid #21262d; }}
  .session-id {{ display: flex; align-items: center; gap: 0.4rem; }}
  .session-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  .session-name {{ font-weight: 600; color: #f0f3f6; }}
  .session-num {{ color: #8b949e; font-size: 0.75rem; font-family: 'Cascadia Code', monospace; }}
  .session-phase {{ font-size: 0.75rem; color: #c9d1d9; }}
  .session-body {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem 1rem;
                   margin-bottom: 0.5rem; }}
  .session-stat {{ display: flex; justify-content: space-between; font-size: 0.8rem; }}
  .session-stat.ctx {{ grid-column: span 2; flex-direction: column; gap: 0.25rem; }}
  .lbl {{ color: #8b949e; }}
  .val {{ color: #f0f3f6; font-weight: 500; font-family: 'Cascadia Code', monospace; }}
  .ctx-bar {{ background: #21262d; border-radius: 4px; height: 5px; overflow: hidden; }}
  .ctx-fill {{ height: 100%; transition: width 0.3s; }}
  .session-action {{ font-size: 0.78rem; color: #d29922; font-family: 'Cascadia Code', monospace;
                     padding-top: 0.4rem; border-top: 1px solid #21262d;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .session-id-full {{ font-size: 0.65rem; color: #484f58; font-family: 'Cascadia Code', monospace;
                      margin-top: 0.4rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  .empty {{ text-align: center; padding: 2rem; color: #8b949e; font-size: 0.95rem;
            background: #161b22; border: 1px dashed #30363d; border-radius: 10px; }}

  .controls {{ background: #161b22; border: 1px solid #21262d; border-radius: 10px;
               padding: 1rem; margin-bottom: 1rem; display: flex;
               align-items: center; justify-content: center; gap: 0.75rem; flex-wrap: wrap; }}
  .btn {{ background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 0.5rem 1rem;
         border-radius: 8px; cursor: pointer; font-family: inherit; font-size: 0.85rem; }}
  .btn:hover {{ background: #30363d; }}
  .vol-wrap {{ display: flex; align-items: center; gap: 0.5rem; color: #8b949e; font-size: 0.8rem; }}
  .vol-wrap input {{ width: 80px; accent-color: #f85149; }}

  .activity {{ background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 1rem; }}
  .activity-title {{ font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em;
                     margin-bottom: 0.75rem; font-weight: 600; }}
  .log-entry {{ display: grid; grid-template-columns: 24px 130px 130px 1fr; gap: 0.5rem;
                padding: 0.35rem 0; border-bottom: 1px solid #0d1117; font-size: 0.78rem;
                align-items: center; }}
  .log-icon {{ text-align: center; }}
  .log-session {{ color: #f0f3f6; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .log-type {{ color: #8b949e; font-family: 'Cascadia Code', monospace; font-size: 0.7rem; }}
  .log-desc {{ color: #c9d1d9; font-family: 'Cascadia Code', monospace; font-size: 0.7rem;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  .footer {{ text-align: center; padding: 1rem; color: #484f58; font-size: 0.7rem; }}
  .pulse {{ animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
</style>
</head><body>

<div class="header">
  <div class="header-left">
    <svg class="claude-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z" fill="#f85149" opacity="0.15"/>
      <path d="M16.8 8.5c-.3-.5-.8-.8-1.3-.8h-1.2l-1.8 3.5L10.7 7.7H9.5c-.5 0-1 .3-1.3.8L5.5 13.8c-.2.4-.2.8 0 1.2.3.5.8.8 1.3.8h1.2l1.8-3.5 1.8 3.5h1.2c.5 0 1-.3 1.3-.8l2.7-5.3c.2-.4.2-.8 0-1.2z" fill="#f85149"/>
    </svg>
    <div>
      <div class="header-title">Claude Monitor</div>
      <div class="header-sub">Multi-session activity dashboard</div>
    </div>
  </div>
  <div class="header-right">
    <span class="badge">{len(sessions)} session(s)</span>
  </div>
</div>

<div class="main">
  <div class="top-status">
    <div class="top-icon">{top_icon}</div>
    <div class="top-text" style="color:{top_color}">{top_status}</div>
  </div>

  <div class="agg-stats">
    <div class="agg-stat"><div class="agg-val">{len(sessions)}</div><div class="agg-lbl">Sessions</div></div>
    <div class="agg-stat"><div class="agg-val">${total_cost:.4f}</div><div class="agg-lbl">Total Cost</div></div>
    <div class="agg-stat"><div class="agg-val">{total_tools}</div><div class="agg-lbl">Total Tools</div></div>
  </div>

  <div class="sessions-grid">
    {cards_html}
  </div>

  <div class="controls">
    <button class="btn" onclick="testSound()">🎵 Test Sound</button>
    <button class="btn" onclick="enableNotif()">🔔 Enable Notifications</button>
    <div class="vol-wrap">🔊 <input type="range" id="vol" min="0" max="100" value="50"></div>
  </div>

  <div class="activity">
    <div class="activity-title">📋 Recent activity (all sessions)</div>
    {log_entries if log_entries else '<div style="color:#484f58;font-size:0.8rem;padding:0.5rem 0">No events yet...</div>'}
  </div>
</div>

<div class="footer">
  <span class="pulse">●</span> Auto-refreshing every 2s &nbsp;|&nbsp; Claude Monitor v6 (multi-session)
</div>

<script>
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function getVol() {{ return (document.getElementById('vol').value || 50) / 100 * 0.4; }}
function playTone(f, d) {{
  audioCtx.resume();
  const o = audioCtx.createOscillator(), g = audioCtx.createGain();
  o.connect(g); g.connect(audioCtx.destination);
  o.type = 'sine'; o.frequency.value = f; g.gain.value = getVol();
  g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + d);
  o.start(); o.stop(audioCtx.currentTime + d);
}}
function playCompletion() {{
  playTone(523,.15); setTimeout(()=>playTone(659,.15),130); setTimeout(()=>playTone(784,.25),260);
}}
function playQuestion() {{
  playTone(784,.2); setTimeout(()=>playTone(988,.3),200);
  setTimeout(()=>playTone(784,.2),550); setTimeout(()=>playTone(988,.4),750);
}}
function testSound() {{ audioCtx.resume().then(()=>playCompletion()); }}
function enableNotif() {{
  if (Notification.permission==='default') Notification.requestPermission();
}}
const events = {notify_events};
const lastPlayed = parseInt(sessionStorage.getItem('lp')||'0');
const nw = events.filter(e=>e.id>lastPlayed);
if (nw.length>0) {{
  const l = nw[nw.length-1];
  audioCtx.resume().then(()=>{{
    if (l.type==='Stop') playCompletion();
    else if (l.type==='PermissionRequest') playQuestion();
  }});
  if (Notification.permission==='granted') {{
    const sn = l.session_name || 'Claude';
    if (l.type==='Stop') new Notification('Claude Monitor',{{body: sn + ': Response complete'}});
    else if (l.type==='PermissionRequest') new Notification('Claude Monitor',{{body: sn + ': Approval needed: ' + (l.tool||'')}});
  }}
  sessionStorage.setItem('lp', String(events[events.length-1].id));
}}
const vol = document.getElementById('vol');
if (sessionStorage.getItem('v')) vol.value = sessionStorage.getItem('v');
vol.addEventListener('input', () => sessionStorage.setItem('v', vol.value));
</script>
</body></html>
"""


class _WebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = _build_web_page()
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def run_web_server(port=WEB_PORT):
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), _WebHandler)
        server.serve_forever()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════

def kill_old_instances(port=9876):
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

    global _web_app
    config = load_config()
    app = MonitorApp(config)
    _web_app = app

    def shutdown(sig, frame):
        save_config(app.config)
        app.running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=run_socket_server, args=(app,), daemon=True).start()
    threading.Thread(target=keyboard_listener, args=(app,), daemon=True).start()
    threading.Thread(target=run_web_server, daemon=True).start()

    console = Console()
    console.clear()

    try:
        with Live(app.render(), console=console, refresh_per_second=FPS, screen=True) as live:
            while app.running:
                live.update(app.render())
                time.sleep(1.0 / FPS)
    except KeyboardInterrupt:
        save_config(app.config)
        app.running = False


if __name__ == "__main__":
    main()
