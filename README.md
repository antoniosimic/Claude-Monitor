# Claude Monitor

Real-time ASCII activity monitor for [Claude Code](https://claude.com/claude-code).

Watch a tiny ASCII Claude walk over to tool icons as Claude Code reads files, runs Bash, fetches the web, and so on — plus live cost, context window usage, and a per-session activity log. Supports multiple concurrent Claude Code sessions from a single monitor process.

## Features

- Animated ASCII sprite that reacts to `PreToolUse` / `PostToolUse` / `Stop` / `PermissionRequest` hook events
- Per-session stats: model, total cost (USD), context window %, tool counts, activity log
- Multi-session layouts: `single`, `grid` (up to 4), `compact` (5+), or `auto`
- Sound + desktop notifications (Windows) on completion and permission requests
- Web dashboard at `http://localhost:7777`
- Status line for Claude Code's own bar: `model | $cost | ctx: NN%`

## Requirements

- Python >= 3.8
- Claude Code installed and configured
- Windows for native notifications (`winotify`); sound + UDP work cross-platform

## Install

```bash
git clone https://github.com/antoniosimic/Claude-Monitor.git
cd Claude-Monitor
pip install .
```

This installs four console scripts:

| Command | Purpose |
| --- | --- |
| `claude-monitor` | Runs the animated monitor UI |
| `claude-monitor-hook` | Hook script invoked by Claude Code on tool events |
| `claude-monitor-status` | Status line script for Claude Code |
| `claude-monitor-setup` | Auto-configures `~/.claude/settings.json` |

## Setup

Run the setup helper once — it backs up your existing settings and wires up the hooks and status line:

```bash
claude-monitor-setup
```

Then restart Claude Code so the new hooks take effect.

## Usage

1. Start the monitor in its own terminal:
   ```bash
   claude-monitor
   ```
2. Start Claude Code in another terminal and use it normally.
3. The monitor reacts in real time. Open `http://localhost:7777` for the web view.

### Controls

| Key | Action |
| --- | --- |
| `S` | Open / close settings |
| `M` | Cycle layout (auto / single / grid / compact) |
| `Tab` | Cycle focus to next session |
| `1`–`9` | Jump focus to session N |
| `↑` `↓` | Navigate settings |
| `←` `→` | Adjust slider / cycle option |
| `Enter` | Toggle / save edit |
| `Ctrl+C` | Quit |

## Configuration

Config lives at `~/.claude-monitor/config.json` and is edited live from the settings panel (`S`). Defaults:

```json
{
  "volume": 75,
  "activity_log": true,
  "stats_panel": true,
  "notifications": true,
  "name": "Claude",
  "theme": "Red",
  "light_mode": false,
  "layout": "auto",
  "session_timeout_min": 30
}
```

## How it works

Claude Code hooks pipe a JSON payload into `claude-monitor-hook` and `claude-monitor-status`. Each script forwards the event over UDP to `127.0.0.1:9876`, where the running `claude-monitor` process routes it to the matching session by `session_id` and updates the animation, stats, and web dashboard.

## Verify hooks work

```bash
echo '{"hook_event_name":"test"}' | claude-monitor-hook
```

If `claude-monitor` is running, you'll see the event arrive.

## License

MIT — Antonio Simic
