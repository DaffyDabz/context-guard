# Context Guard

Context Guard is a set of [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) hooks that put a hard
budget on how big a conversation can get, and trim noisy command output before it reaches the conversation. It is for
anyone whose Claude Code bill is mostly cache reads: every turn re-sends the whole conversation, so one long session
at ~550k tokens per turn costs far more than the work inside it. When I looked at my own transcripts, about 99% of one
week's token volume was exactly that. Rules asking the model to stay small did not stick; this enforces them from
outside the model.

**Status:** in daily use on the author's Windows 11 PC since September 2026; the installer was tested against sample
settings files on Windows PowerShell 5.1, not on a fresh PC; macOS and Linux are untested · **Visibility:** public ·
**Last updated:** 2026-10-04

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Run / Use](#run--use)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Development](#development)
- [Coming soon](#coming-soon)
- [Recent changes](#recent-changes)
- [Credits and license](#credits-and-license)

## Features

- **Budget check on every step:** on session start, on every prompt and before every tool call, it works out the
  current context size, the turns in this session, the session's total tokens and today's total across all sessions
- **Three levels:** `ok` does nothing; `warn` shows you a one-line warning and tells the model to wrap up; `block`
  refuses the prompt or tool call with an explanation until you `/compact`, `/clear` or override
- **Status line:** `[guard OK] ctx 84k 8% | turns 41 | session 3.2M | today 81.9M | <model>` (the flag reads `OK`,
  `WARN`, `BLOCKED` or `OFF`)
- **Handoff notes:** a warning asks the model to write a short task note to
  `~/.claude/projects/<project>/memory/handoff.md`; the next session in that project gets it injected at start
- **Output trimming** (`output_trim.py`): noisy Bash and PowerShell commands (builds, tests, installs, `git log`,
  `find`, `curl` and so on) come back as their last 80 lines; whole-file reads (`cat`, `Get-Content`) as their first
  400 lines, with a note saying how much was cut
- **ON/OFF switch for every window at once:** start a prompt with `!!off`, `!!off 3h`, `!!off 45m` or `!!on`, or run
  `context_guard.py off|on|status`
- **Short override:** a prompt that starts with `!!` lifts the limits in that one session for 15 minutes
- **Daily report** of today's total and the biggest sessions
- **Windows installer** that merges into your existing `settings.json`, keeps your other hooks, and uninstalls cleanly
- Never edits the transcript and never calls the network; if the guard itself crashes it lets Claude Code carry on

## Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) with hooks and a command status line. Built in
  September 2026 against Claude Code 2.1. Output trimming needs PreToolUse hooks that can return `updatedInput`.
- [Python 3](https://www.python.org/downloads/), standard library only, 3.8 or newer. Developed with Python 3.10 and
  also tested with 3.14.
- For output trimming of Bash commands: `awk` (Git Bash on Windows, macOS and Linux all have it).
- For the installer: Windows with Windows PowerShell 5.1 (built in to Windows 10 and 11). PowerShell 7 should work but
  is untested. On macOS or Linux, add the settings by hand (see below).

## Install

These steps come from the installer and the scripts. They have not been tested on a fresh PC.

Windows (PowerShell):

1. Install Python 3 from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH") and Git from
   [git-scm.com](https://git-scm.com/). If typing `python` opens the Microsoft Store, Python is not installed yet.
2. Put the folder where it will stay (the hooks point at this path; move it and you must run the installer again):
   ```powershell
   git clone https://github.com/DaffyDabz/context-guard.git <install-folder>
   cd <install-folder>
   ```
3. Optional: change the limits in `context-guard.json` (see [Configuration](#configuration)). The shipped values are
   the built-in defaults.
4. Run the installer:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Install-ContextGuard.ps1
   ```
   It finds `python` (or the `py` launcher) on your PATH by itself. It backs up
   `%USERPROFILE%\.claude\settings.json` as `settings.json.before-context-guard-<timestamp>` (or creates the file if
   there is none), adds the three guard hooks, the output trimming hook and the status line, and keeps every hook that
   is not its own. It replaces any status line you already had (the backup keeps the old one). It can be re-run
   safely: its own entries are replaced, never doubled. It rewrites the file in PowerShell's JSON layout (same
   settings, different spacing).
5. Start a new Claude Code session. The status line should show `[guard OK] ...`. A session that was already open may
   need a restart.

Installer options:

| Option | What it does |
|---|---|
| `-NoOutputTrim` | Installs the guard without the output trimming hook (and removes that hook if an earlier run added it) |
| `-Python <path>` | Uses this `python.exe` instead of the one found on PATH |
| `-SettingsPath <file>` | Writes to another settings file, for example a project's `.claude\settings.json` |
| `-Uninstall` | Removes only the guard's own hooks and its status line; everything else stays. Works even if Python is gone |

To remove it:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-ContextGuard.ps1 -Uninstall
```

### Manual setup (macOS, Linux, or by hand)

This is what the installer writes: one entry per event running `context_guard.py hook` with a 20-second timeout and
no matcher, the `output_trim.py` entry for the Bash and PowerShell tools with a 10-second timeout, and the status
line. Use your own paths (`python3` on macOS and Linux, `python` or the full path to `python.exe` on Windows):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3 \"<install-folder>/context_guard.py\" hook", "timeout": 20 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3 \"<install-folder>/context_guard.py\" hook", "timeout": 20 } ] }
    ],
    "PreToolUse": [
      { "hooks": [ { "type": "command", "command": "python3 \"<install-folder>/context_guard.py\" hook", "timeout": 20 } ] },
      { "matcher": "Bash|PowerShell",
        "hooks": [ { "type": "command", "command": "python3 \"<install-folder>/output_trim.py\"", "timeout": 10 } ] }
    ]
  },
  "statusLine": { "type": "command", "command": "python3 \"<install-folder>/context_guard.py\" statusline" }
}
```

Merge this into `~/.claude/settings.json`; do not replace hooks you already have. Leave out the `output_trim.py`
entry if you do not want output trimming.

## Run / Use

Once installed it runs by itself. What you will see:

| Situation | What happens |
|---|---|
| Under every limit | Nothing, apart from the status line |
| Over a warn limit | A one-line `Context Guard: ...` message, and the model is told to write the handoff note, finish the current step with few tool calls and recommend `/compact` or a fresh session |
| Over a block limit | The prompt or tool call is refused with the reason and your options: `/compact`, `/clear`, or a prompt starting with `!!` |
| New session | The model gets a short budget summary and, if one exists, the project's handoff note |

Commands (run from `<install-folder>`):

```powershell
python context_guard.py report       # today's total and the 15 biggest sessions with their level
python context_guard.py status       # ON or OFF
python context_guard.py off          # off for every window until you turn it on
python context_guard.py off 3        # off for 3 hours, then back on by itself
python context_guard.py on
```

Switching from inside Claude Code (every open window at once):

- `!!off` turns it off until `!!on`; `!!off 3h` or `!!off 45m` turns it off for that long (a bare number means
  hours). While off there are no warnings, blocks or output trimming, and new sessions are told the guard is off.
  Usage is still counted.
- `!!on` turns it back on.
- `!!` followed by anything else (for example `!! finish the handoff note, then continue`) lifts warnings and blocks
  in that one session for `override_minutes` (15). The prompt itself goes through as normal.

Tip: two desktop shortcuts that run `python "<install-folder>\context_guard.py" off` and `... on` flip the switch
without opening Claude Code. They are not included.

`/compact` shrinks the conversation in place; `/clear` starts fresh. A hook cannot do either for you, so refusing to
continue until you act is the strongest control Claude Code allows.

## Configuration

`context-guard.json` sits next to the scripts. Any key you leave out falls back to the same default built into
`context_guard.py`, and the shipped file holds exactly those defaults. The `_comment` key is ignored.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` stops the guard's checks, warnings, blocks and `!!off`/`!!on` prompts without uninstalling. The status line and output trimming still run; use `context_guard.py off` to stop trimming too |
| `context_warn_tokens` | 120,000 | Context per turn: input + cache read + cache write of the last reply, which every next turn re-sends |
| `context_block_tokens` | 180,000 | |
| `session_turns_warn` | 300 | Assistant replies in this session |
| `session_turns_block` | 600 | |
| `session_total_warn_tokens` | 150,000,000 | All tokens this session (input, cache read, cache write, output) |
| `session_total_block_tokens` | 300,000,000 | |
| `daily_total_warn_tokens` | 600,000,000 | All sessions today (local date) that the guard has seen |
| `daily_total_block_tokens` | 1,000,000,000 | |
| `override_minutes` | 15 | How long a `!!` prompt lifts the limits |
| `log_path` | `state/guard.jsonl` | Decision log, one JSON line per check. Relative to the script folder unless absolute; `""` switches it off |

To keep a warning without ever blocking, set that block value very high.

Other settings live in the code:

- `output_trim.py`: `TAIL_N = 80`, `HEAD_N = 400`, the `LOG` file (`state/trim.log` next to the script), and the
  patterns that decide what counts as noisy (`NOISY`), as a whole-file read (`READ`) or as already limited
  (`LIMITED`).
- `context_guard.py`: the text the model sees (`BLOCK_TEXT`, `WARN_TEXT`, `OFF_TEXT`, `ON_TEXT`,
  `SESSION_START_TEXT`). The session-start text lists five general token-saving rules, and the OFF text tells the
  model that any token-economy rules in your `CLAUDE.md` are suspended while the guard is off. Edit them to match how
  you work.

## How it works

- **Hook input:** Claude Code runs `context_guard.py hook` and passes JSON on stdin with `hook_event_name`,
  `session_id`, `transcript_path` and, for prompts, `prompt`. If the transcript path is missing, the script looks for
  `~/.claude/projects/*/<session_id>.jsonl`.
- **Counting:** it reads only the new part of the transcript since last time (it saves a byte offset), takes the
  `usage` block of each new assistant message, skips message ids it has already counted, and keeps per-session
  totals and per-day totals in `state/<session>.json`. Today's total is the sum over every state file.
- **Deciding:** each of the four measures (context per turn, turns, session total, today total) is compared with its
  warn and block limit. Any block wins over any warning.
- **Answering:** a block exits with code 2 and the reason on stderr, which Claude Code shows and treats as a refusal.
  A warning prints JSON with a `systemMessage` for you and `additionalContext` for the model (UserPromptSubmit and
  PreToolUse; PostToolUse too if you wire it). SessionStart always prints the budget summary plus the handoff note
  (first 4,000 characters).
- **Switches:** `state/guard.off` is the global OFF marker (it holds the time to switch back on, or `0` for "until
  `!!on`"); an expired marker is deleted on the next check. `state/<session>.json.override` holds the end time of a
  `!!` override. Both scripts read the same OFF marker.
- **Status line:** `context_guard.py statusline` gets the session JSON from Claude Code, updates the same counters and
  prints one line. It adds a percentage when that JSON carries `current_usage.context_usage_percentage`.
- **Output trimming:** `output_trim.py` gets the PreToolUse JSON for Bash and PowerShell calls. It leaves the command
  alone when the guard is off, the command runs in the background, uses a heredoc, or already limits its own output
  (`| tail`, `| Select-Object`, `-TotalCount`, a redirect to a file and similar). Otherwise it returns an
  `updatedInput` that wraps the command: in Bash, `( command ) 2>&1 | awk ...` keeps the last 80 lines (or the first
  400 for a file read); in PowerShell the output is collected and passed through `Select-Object -Last 80` (or
  `-First 400`). In Bash this also merges stderr into the output, and the exit code you see is the one from `awk`.
- **Safety:** any error inside the guard is written to stderr and the hook exits 0, so a broken guard never stops
  Claude Code. The hook fields it relies on (`transcript_path`, `session_id`, the `usage` block in the transcript)
  are not a formal API and may change between Claude Code versions.

## Project layout

| Path | What lives there |
|---|---|
| `context_guard.py` | The guard: hook handler, status line, report and on/off commands |
| `output_trim.py` | PreToolUse hook that trims noisy Bash and PowerShell output |
| `Install-ContextGuard.ps1` | Windows installer and uninstaller for the user-level `settings.json` |
| `context-guard.json` | Budgets, the override window and the log path |
| `state/` | Created at run time and gitignored: one JSON per session, override files, the OFF marker, `guard.jsonl` and `trim.log`. Safe to delete (today's count starts again from zero) |

## Development

There is no build step and there are no automated tests. Useful checks:

```powershell
python -m py_compile context_guard.py output_trim.py
'{"hook_event_name":"UserPromptSubmit","session_id":"local-test","prompt":"hello"}' | python context_guard.py hook
'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"npm run build"}}' | python output_trim.py
```

The second line prints nothing while today's total is under its limits (with no transcript, the test session counts
as empty). The third prints the rewritten command. Both write to the log files in `state/`. To try the installer
without touching your real settings, point it at a scratch file:
`.\Install-ContextGuard.ps1 -SettingsPath <scratch-folder>\settings.json`. Changes go straight to the `main` branch;
there are no releases.

## Coming soon

> Keep this list current: when an item ships, move it to Recent changes with the date, then add what is next.

- [ ] Test the manual setup on macOS and Linux
- [ ] Test the installer on PowerShell 7 and on a fresh Windows PC
- [ ] `enabled: false` also stops output trimming (today only `context_guard.py off` does)
- [ ] Keep the real exit code of a trimmed Bash command (today it is `awk`'s)
- [ ] Optional Guard ON and Guard OFF desktop shortcuts from the installer
- [ ] A small automated test script for the hooks and the installer

## Recent changes

- 2026-10-04 Installer fixes: an empty or missing `settings.json` (or `.claude` folder) works, `-Uninstall` no longer
  needs Python or creates a file, settings keep their key order, relative `-SettingsPath` works; hooks read their
  input as UTF-8 so commands with non-ASCII characters are no longer garbled on Windows
- 2026-10-04 Installer finds Python on PATH and also adds and removes the output trimming hook (`-NoOutputTrim` skips it)
- 2026-10-04 Public-ready copy: log paths relative to the install folder, `context-guard.json` ships with the built-in
  defaults, general wording in the messages the model sees
- 2026-10-04 README: full guide (what it is, install, run, coming soon)
- 2026-10-04 Initial commit: snapshot of the working install (guard, output trimmer, installer, config)
- 2026-09-04 Global ON/OFF switch for every window: `!!off`, `!!off 3h`, `!!on` and `context_guard.py off|on|status`;
  output trimming follows it
- 2026-09-01 First version: SessionStart, UserPromptSubmit and PreToolUse hooks, the status line and the installer

## Credits and license

Built for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) using its
[hooks](https://docs.claude.com/en/docs/claude-code/hooks) and [status line](https://docs.claude.com/en/docs/claude-code/statusline)
settings. Written in [Python](https://www.python.org/) (standard library only), with a
[PowerShell](https://learn.microsoft.com/powershell/) installer. Not made by or affiliated with Anthropic.

Context Guard is an original project by DaffyDabz, released under the [MIT License](LICENSE).
