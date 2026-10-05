"""Context Guard companion: PreToolUse hook that rewrites noisy Bash / PowerShell
commands so only the tail (builds, tests, logs) or head (whole-file reads) reaches
the conversation.  Untrimmed output is re-sent on every later turn, so this is
the cheapest token saving there is.  Skipped when the command already limits its
own output (tail/head/grep/Select-Object...), uses a heredoc, or runs in background.
"""
import json, re, sys, os, datetime

TAIL_N = 80      # builds / tests / logs: last N lines
HEAD_N = 400     # whole-file reads: first N lines
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "trim.log")

LIMITED = re.compile(
    r"\|\s*(tail|head|grep|rg|wc|awk|sed\s+-n|cut|sort|uniq|jq|python|py|Select-Object|Select-String|"
    r"Measure-Object|Out-Null|Out-File|findstr|more|less)\b|-TotalCount|-Tail\b|--max-count|-n\s*\d+|"
    r"\bsed\s+-n\b|\bhead\b|\btail\b|>\s*[\w./\:\"']+\s*$|>\s*/dev/null", re.I)
READ = re.compile(r"^\s*(cat|type|Get-Content|gc)\s+\S", re.I)
NOISY = re.compile(
    r"\b(dotnet|msbuild|unity(\.exe)?|npm\s+(run|test|ci|install|start|build)|pnpm|yarn|"
    r"npx\s+(vitest|vite|tsc|jest|playwright|eslint|prettier)|pytest|python3?\s+-m\s+(pytest|unittest)|"
    r"cargo\s+(build|test|run)|go\s+(build|test)|gradle|gradlew|mvn|make|cmake|"
    r"flutter\s+(build|test|run|doctor|pub)|adb\s+logcat|git\s+(log|diff|show|status)|"
    r"tree|ls\s+-R|find\s|Get-ChildItem\s+-Recurse|dir\s+/s|curl|Invoke-WebRequest|wget|"
    r"wsl\b|bash\s+\S+\.sh|schtasks|Get-Process|tasklist|systeminfo|Get-EventLog)\b", re.I)

OFF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "guard.off")


def guard_off():
    """Same switch as context_guard.py: marker present (and not expired) => no trimming."""
    try:
        with open(OFF_PATH, "r", encoding="utf-8") as fh:
            until = float(fh.read().strip() or 0)
    except Exception:
        return False
    import time
    return not (until and until < time.time())


def main():
    if guard_off():
        return
    try:
        # Claude Code sends UTF-8; read bytes so Windows' default code page cannot mangle non-ASCII commands
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        return
    if data.get("hook_event_name") != "PreToolUse":
        return
    tool = data.get("tool_name")
    inp = data.get("tool_input") or {}
    cmd = inp.get("command")
    if tool not in ("Bash", "PowerShell") or not isinstance(cmd, str):
        return
    if inp.get("run_in_background") or "<<" in cmd or "output-trim" in cmd:
        return
    is_read = bool(READ.match(cmd))
    if not (is_read or NOISY.search(cmd)):
        return
    if LIMITED.search(cmd):
        return
    body = cmd.rstrip()
    if tool == "Bash":
        if is_read:
            new = ("( " + body + "\n) 2>&1 | awk -v n=%d 'NR<=n{print} NR==n+1{print \"[output-trim] capped at \" n "
                   "\" lines; read the rest by range (sed -n A,Bp)\"}'" % HEAD_N)
        else:
            new = ("( " + body + "\n) 2>&1 | awk -v n=%d '{b[NR%%n]=$0} END{if(NR>n)print \"[output-trim] \" NR-n "
                   "\" earlier lines omitted of \" NR \"; grep for them, do not rerun untrimmed\"; s=(NR>n)?NR-n+1:1; "
                   "for(i=s;i<=NR;i++)print b[i%%n]}'" % TAIL_N)
    else:
        if is_read:
            new = ("$__o = @(& {\n" + body + "\n} | Out-String -Stream); $__o | Select-Object -First %d; "
                   "if ($__o.Count -gt %d) { \"[output-trim] capped at %d of $($__o.Count) lines; read the rest by range\" }"
                   % (HEAD_N, HEAD_N, HEAD_N))
        else:
            new = ("$__o = @(& {\n" + body + "\n} | Out-String -Stream); if ($__o.Count -gt %d) { "
                   "\"[output-trim] $($__o.Count - %d) earlier lines omitted of $($__o.Count); Select-String for them, do not rerun untrimmed\" }; "
                   "$__o | Select-Object -Last %d" % (TAIL_N, TAIL_N, TAIL_N))
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s %s | %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), tool,
                                         "head" if is_read else "tail", cmd[:120].replace("\n", " ")))
    except Exception:
        pass
    out = dict(inp); out["command"] = new
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": out}}))

if __name__ == "__main__":
    main()
