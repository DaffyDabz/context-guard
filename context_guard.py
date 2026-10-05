"""Context Guard -- external token/context budget enforcement for Claude Code.

Why: on the author's machine 99% of one week's token volume was cache reads, i.e. the same huge conversation
(~550k tokens) re-sent on every one of ~5,000 daily turns. Claude Code has no built-in cap, but its hooks
receive the session transcript path and can BLOCK a prompt or a tool call with a reason. This script is that
enforcement. It never edits the transcript and never calls the network.

Subcommands (all read the hook JSON on stdin):
  hook        SessionStart / UserPromptSubmit / PreToolUse handler (dispatches on hook_event_name)
  on|off [h]  global switch (all windows): 'off' suspends guard + output trimming until 'on' (or for h hours);
              also as a prompt prefix: '!!off', '!!off 3h', '!!on'.  '!!' alone lifts limits for override_minutes.
  status      print ON/OFF
  statusline  prints one status line for Claude Code's statusLine setting
  report      prints today's totals and the biggest sessions (no stdin needed)

Budget semantics (see context-guard.json):
  context tokens  = input + cache_read + cache_creation of the LAST assistant message = what every further
                    turn will re-send.  warn -> reminder injected; block -> prompt/tool refused until /compact
                    or /clear brings it down.
  session total   = cumulative tokens of this session (all four counters).
  daily total     = cumulative tokens of all sessions today (local date).
  turns           = assistant messages in this session.
Exit code 2 + stderr text = block (Claude Code shows the text). Exit 0 + JSON on stdout = allow/warn.
"""
import datetime as _dt
import glob
import json
import re
import time
import os
import sys

GUARD_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(GUARD_DIR, "context-guard.json")
STATE_DIR = os.path.join(GUARD_DIR, "state")
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

DEFAULTS = {
    "context_warn_tokens": 120000,
    "context_block_tokens": 180000,
    "session_total_warn_tokens": 150000000,
    "session_total_block_tokens": 300000000,
    "daily_total_warn_tokens": 600000000,
    "daily_total_block_tokens": 1000000000,
    "session_turns_warn": 300,
    "session_turns_block": 600,
    "log_path": "state/guard.jsonl",   # relative paths resolve against this folder; "" disables the log
    "enabled": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg


def log_event(cfg, obj):
    try:
        path = cfg.get("log_path")
        if not path:
            return
        path = os.path.join(GUARD_DIR, os.path.expandvars(os.path.expanduser(path)))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        obj = dict(obj)
        obj["ts"] = _dt.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- transcript accounting

def _usage_of(line):
    """Return (message_id, usage dict, timestamp) for an assistant line, else None."""
    if '"usage"' not in line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return (msg.get("id"), usage, obj.get("timestamp"))


def _state_path(session_id):
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:80] or "unknown"
    return os.path.join(STATE_DIR, safe + ".json")


def load_state(session_id):
    try:
        with open(_state_path(session_id), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"offset": 0, "seen": [], "turns": 0, "total": 0, "by_day": {}, "last_context": 0, "last_ts": None}


def save_state(session_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(session_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, _state_path(session_id))


def update_state(session_id, transcript_path):
    """Incrementally fold new transcript lines into the session state. Returns the state."""
    state = load_state(session_id)
    if not transcript_path or not os.path.isfile(transcript_path):
        return state
    size = os.path.getsize(transcript_path)
    offset = int(state.get("offset") or 0)
    if size < offset:  # file rewritten/truncated: start over
        state = {"offset": 0, "seen": [], "turns": 0, "total": 0, "by_day": {}, "last_context": 0, "last_ts": None}
        offset = 0
    seen = list(state.get("seen") or [])
    seen_set = set(seen)
    with open(transcript_path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    # keep an incomplete trailing line for next time
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return state
    body = chunk[: last_nl + 1]
    state["offset"] = offset + len(body)
    for raw in body.split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace")
        rec = _usage_of(line)
        if rec is None:
            continue
        mid, usage, ts = rec
        if mid and mid in seen_set:
            continue
        if mid:
            seen.append(mid)
            seen_set.add(mid)
        inp = int(usage.get("input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        turn_total = inp + cr + cc + out
        state["turns"] = int(state.get("turns") or 0) + 1
        state["total"] = int(state.get("total") or 0) + turn_total
        state["last_context"] = inp + cr + cc
        state["last_ts"] = ts
        day = _local_day(ts)
        by_day = state.setdefault("by_day", {})
        by_day[day] = int(by_day.get(day) or 0) + turn_total
    state["seen"] = seen[-200:]
    state["transcript_path"] = transcript_path
    save_state(session_id, state)
    return state


def _local_day(ts):
    try:
        t = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
        return t.strftime("%Y-%m-%d")
    except Exception:
        return _dt.date.today().isoformat()


def daily_total(today=None):
    """Sum of today's tokens across every session state file (only sessions the guard has seen)."""
    today = today or _dt.date.today().isoformat()
    total = 0
    for path in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            total += int((st.get("by_day") or {}).get(today) or 0)
        except Exception:
            continue
    return total


def handoff_path(transcript):
    """<~/.claude/projects/<slug>/memory/handoff.md> for the project this transcript belongs to."""
    if not transcript:
        return None
    return os.path.join(os.path.dirname(transcript), "memory", "handoff.md")


def _override_path(session_id):
    return _state_path(session_id) + ".override"


def set_override(session_id, minutes):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_override_path(session_id), "w", encoding="utf-8") as fh:
        fh.write(str(time.time() + minutes * 60))


def override_active(session_id):
    try:
        with open(_override_path(session_id), "r", encoding="utf-8") as fh:
            return float(fh.read().strip()) > time.time()
    except Exception:
        return False


# ---- global ON/OFF switch (all sessions, all windows) -------------------------
OFF_PATH = os.path.join(STATE_DIR, "guard.off")   # present => guard + output trimming are OFF
SWITCH_RE = re.compile(r"^!!\s*(off|on)\b(.*)$", re.I | re.S)


def set_off(hours=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OFF_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(time.time() + hours * 3600) if hours else "0")


def set_on():
    try:
        os.remove(OFF_PATH)
    except FileNotFoundError:
        pass


def off_until():
    """None when the guard is ON; 0 when OFF until switched on; else the epoch time it comes back on."""
    try:
        with open(OFF_PATH, "r", encoding="utf-8") as fh:
            until = float(fh.read().strip() or 0)
    except Exception:
        return None
    if until and until < time.time():
        set_on()
        return None
    return until


def guard_off():
    return off_until() is not None


def _until_text(until):
    if not until:
        return " until you turn it back on"
    return " until " + _dt.datetime.fromtimestamp(until).strftime("%H:%M")


def _parse_hours(text):
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minutes)?", text or "")
    if not m:
        return None
    n = float(m.group(1))
    return n / 60.0 if (m.group(2) or "h").startswith("m") else n


def find_transcript(session_id):
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", str(session_id) + ".jsonl"))
    return hits[0] if hits else None


# ---------------------------------------------------------------- decisions

def fmt(n):
    n = int(n or 0)
    if n >= 1_000_000_000:
        return "%.2fB" % (n / 1e9)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 1_000:
        return "%dk" % round(n / 1e3)
    return str(n)


def evaluate(cfg, state, day_total):
    """Return (level, reasons) where level in {'ok','warn','block'}."""
    ctx = int(state.get("last_context") or 0)
    turns = int(state.get("turns") or 0)
    total = int(state.get("total") or 0)
    blocks, warns = [], []

    def check(value, warn_key, block_key, label):
        if value >= int(cfg[block_key]):
            blocks.append("%s %s (hard limit %s)" % (label, fmt(value), fmt(cfg[block_key])))
        elif value >= int(cfg[warn_key]):
            warns.append("%s %s (warn %s, hard limit %s)" % (label, fmt(value), fmt(cfg[warn_key]), fmt(cfg[block_key])))

    check(ctx, "context_warn_tokens", "context_block_tokens", "context per turn")
    check(turns, "session_turns_warn", "session_turns_block", "turns this session")
    check(total, "session_total_warn_tokens", "session_total_block_tokens", "session total")
    check(day_total, "daily_total_warn_tokens", "daily_total_block_tokens", "today total")
    if blocks:
        return "block", blocks
    if warns:
        return "warn", warns
    return "ok", []


BLOCK_TEXT = (
    "CONTEXT GUARD BLOCKED THIS ACTION: {reasons}. Every further turn re-sends the whole context as cache reads. "
    "Options: (a) type /compact to shrink the conversation; (b) /clear for a fresh session -- the handoff note at "
    "{handoff} is injected into the next session automatically; (c) if you must continue here, start your prompt "
    "with !! to lift the block for {mins} minutes (e.g. '!! write the handoff note, then continue'). Raise limits "
    "deliberately in {config}. Claude: do not retry; tell the user this verbatim."
)

WARN_TEXT = (
    "Context Guard warning: {reasons}. FIRST, write or refresh the handoff note at {handoff} (task, current state, "
    "decisions made, files touched, exact next step, <=40 lines) so a /clear loses nothing; it is auto-injected into "
    "the next session. Then finish the current step with the fewest tool calls possible and recommend /compact or a "
    "fresh session. Do not read large files whole; read the smallest range you need."
)

OFF_TEXT = (
    "CONTEXT GUARD IS OFF{until}. Any token-economy rules in CLAUDE.md are SUSPENDED: do NOT "
    "recommend /clear or /compact, do NOT write handoff notes or end reports with a handoff block, do NOT stop at task "
    "boundaries or ask whether to continue -- keep working the task through to the end, reading whatever you need. "
    "Tool output is not trimmed. It comes back on with a prompt starting '!!on' or 'python context_guard.py on'."
)

ON_TEXT = (
    "Context Guard is back ON from this turn: any token-economy rules in CLAUDE.md apply again."
)

SESSION_START_TEXT = (
    "Context Guard is active for this session. Budget: context per turn warn {cw} / block {cb}; turns warn {tw} / block "
    "{tb}; today so far {day}. Token economy rules: (1) read files by line range or by section, never a whole large file; "
    "(2) keep tool output small (head/tail/grep, not full dumps); (3) batch independent tool calls; (4) one task per "
    "session, then tell the user to /clear; (5) when work is long-running, delegate the grind to a subagent or "
    "another tool instead of looping here. When the guard warns, act on it before continuing."
)


def read_payload():
    """Hook JSON from stdin. Claude Code sends UTF-8; read bytes so Windows' default code page cannot mangle it."""
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8-sig")) or {}
    except Exception:
        return {}


def write_stderr(text):
    """UTF-8 to stderr (a non-ASCII path in the message must not crash the block on a cp1252 console)."""
    try:
        sys.stderr.buffer.write(text.encode("utf-8"))
        sys.stderr.flush()
    except Exception:
        sys.stderr.write(text.encode("ascii", "replace").decode("ascii"))


def cmd_hook():
    cfg = load_config()
    payload = read_payload()
    event = payload.get("hook_event_name") or ""
    session_id = payload.get("session_id") or "unknown"
    transcript = payload.get("transcript_path") or find_transcript(session_id)
    if not cfg.get("enabled", True):
        return 0
    hpath = handoff_path(transcript) or "<project>/memory/handoff.md"

    # '!!off [hours]' / '!!on' as a prompt prefix flips the global switch for every window.
    m = SWITCH_RE.match(str(payload.get("prompt") or "").lstrip()) if event == "UserPromptSubmit" else None
    if m:
        if m.group(1).lower() == "off":
            hours = _parse_hours(m.group(2))
            set_off(hours)
            msg = "Context Guard switched OFF" + _until_text(off_until())
            text = OFF_TEXT.format(until=_until_text(off_until()))
        else:
            set_on()
            msg, text = "Context Guard switched ON", ON_TEXT
        log_event(cfg, {"event": event, "session": session_id, "level": "switch", "reasons": [msg]})
        print(json.dumps({"systemMessage": msg, "hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
        return 0

    state = update_state(session_id, transcript)   # keep counting while OFF so totals stay honest
    day_total = daily_total()

    until = off_until()
    if until is not None:
        log_event(cfg, {"event": event, "session": session_id, "level": "off", "context": state.get("last_context"),
                        "turns": state.get("turns"), "session_total": state.get("total"), "day_total": day_total})
        if event == "SessionStart":
            text = OFF_TEXT.format(until=_until_text(until)) + " Today so far: " + fmt(day_total) + "."
            text += _handoff_note(hpath)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
        return 0

    level, reasons = evaluate(cfg, state, day_total)
    log_event(cfg, {"event": event, "session": session_id, "level": level, "context": state.get("last_context"),
                    "turns": state.get("turns"), "session_total": state.get("total"), "day_total": day_total,
                    "tool": payload.get("tool_name"), "reasons": reasons})

    mins = int(cfg.get("override_minutes", 15))
    if event == "UserPromptSubmit" and str(payload.get("prompt") or "").lstrip().startswith("!!"):
        set_override(session_id, mins)
    if level != "ok" and event != "SessionStart" and override_active(session_id):
        # '!!' = lift limits for the window entirely (no block, no warn nag), just a one-line note.
        print(json.dumps({"systemMessage": "Context Guard: !! override active (%d min window) -- limits ignored; "
                                           "'!!off' switches the guard off for good" % mins}))
        return 0

    if event == "SessionStart":
        text = SESSION_START_TEXT.format(cw=fmt(cfg["context_warn_tokens"]), cb=fmt(cfg["context_block_tokens"]),
                                         tw=cfg["session_turns_warn"], tb=cfg["session_turns_block"], day=fmt(day_total))
        if level != "ok":
            text += " CURRENT STATUS: " + "; ".join(reasons)
        text += _handoff_note(hpath)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
        return 0

    if level == "block":
        write_stderr(BLOCK_TEXT.format(reasons="; ".join(reasons), config=CONFIG_PATH, handoff=hpath, mins=mins))
        return 2

    if level == "warn":
        text = WARN_TEXT.format(reasons="; ".join(reasons), handoff=hpath)
        out = {"systemMessage": "Context Guard: " + "; ".join(reasons)}
        if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": text}
        print(json.dumps(out))
        return 0
    return 0


def _handoff_note(hpath):
    try:
        with open(hpath, "r", encoding="utf-8") as fh:
            note = fh.read(4000).strip()
    except Exception:
        return ""
    if not note:
        return ""
    return (chr(10)*2 + "HANDOFF NOTE from the previous session (" + hpath + "). Read it before doing anything; "
            "it is the task state the last session left. Overwrite it when the task moves on, delete it "
            "when the task is done:" + chr(10) + note)


def cmd_switch(sub, args):
    if sub == "off":
        set_off(_parse_hours(" ".join(args)))
    elif sub == "on":
        set_on()
    until = off_until()
    print("Context Guard is %s" % ("ON" if until is None else "OFF" + _until_text(until)))
    return 0


def cmd_statusline():
    cfg = load_config()
    payload = read_payload()
    session_id = payload.get("session_id") or "unknown"
    transcript = payload.get("transcript_path") or find_transcript(session_id)
    state = update_state(session_id, transcript)
    ctx = int(state.get("last_context") or 0)
    cu = payload.get("current_usage") or {}
    pct = cu.get("context_usage_percentage")
    day_total = daily_total()
    level, _ = evaluate(cfg, state, day_total)
    flag = "OFF" if guard_off() else {"ok": "OK", "warn": "WARN", "block": "BLOCKED"}[level]
    model = ((payload.get("model") or {}).get("display_name") if isinstance(payload.get("model"), dict) else None) or (payload.get("cost") or {}).get("model") or ""
    pct_txt = (" %.0f%%" % float(pct)) if isinstance(pct, (int, float)) else ""
    print("[guard %s] ctx %s%s | turns %s | session %s | today %s | %s" % (
        flag, fmt(ctx), pct_txt, state.get("turns", 0), fmt(state.get("total", 0)), fmt(day_total), model))
    return 0


def cmd_report():
    cfg = load_config()
    today = _dt.date.today().isoformat()
    rows = []
    for path in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            rows.append((os.path.basename(path)[:-5], st))
        except Exception:
            continue
    print("Context Guard report  (%s)   config: %s" % (today, CONFIG_PATH))
    print("today total: %s  (warn %s, block %s)" % (fmt(daily_total(today)), fmt(cfg["daily_total_warn_tokens"]), fmt(cfg["daily_total_block_tokens"])))
    rows.sort(key=lambda r: -int(r[1].get("total") or 0))
    for sid, st in rows[:15]:
        level, _ = evaluate(cfg, st, 0)
        print("  %-10s ctx=%-7s turns=%-5s total=%-8s last=%s  [%s]" % (
            sid[:10], fmt(st.get("last_context")), st.get("turns"), fmt(st.get("total")), (st.get("last_ts") or "")[:16], level))
    return 0


def main():
    sub = sys.argv[1] if len(sys.argv) > 1 else "hook"
    if sub == "hook":
        return cmd_hook()
    if sub == "statusline":
        return cmd_statusline()
    if sub == "report":
        return cmd_report()
    if sub in ("on", "off", "status"):
        return cmd_switch(sub, sys.argv[2:])
    sys.stderr.write("usage: context_guard.py hook|statusline|report|on|off [hours]|status\n")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never break Claude Code because the guard itself failed
        sys.stderr.write("context_guard internal error: %r\n" % (exc,))
        sys.exit(0)
