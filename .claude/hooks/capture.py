#!/usr/bin/env python3
"""8x agent-capture hook for Claude Code.

Wired in .claude/settings.json to three events:
  SessionStart      -> remember the session's model (if the payload has it)
  UserPromptSubmit  -> append a PROMPT entry (verbatim prompt from stdin)
  Stop              -> append a RESPONSE entry (final assistant text of the
                       turn, read from the transcript JSONL path on stdin)

Writes one file per session: .agent-logs/YYYY-MM-DD_HH-MM-SS_<session-id>.md
Entries are only ever appended; the frontmatter counters are refreshed.
Never raises: a logging failure must not block the user's turn.
"""
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
import traceback

TOOL = "claude-code"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def repo_root(cwd):
    # Always log into the project that owns these hooks, even if the session cd'd elsewhere.
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.environ["CLAUDE_PROJECT_DIR"]
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.environ.get("CLAUDE_PROJECT_DIR") or cwd


def git_author(root):
    try:
        out = subprocess.run(["git", "-C", root, "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        if out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USER", "unknown")


# ---------- state (model per session) ----------

def state_path(root, sid):
    d = os.path.join(root, ".claude", "agent-capture-state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{sid}.json")


def load_state(root, sid):
    try:
        with open(state_path(root, sid)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(root, sid, st):
    with open(state_path(root, sid), "w") as f:
        json.dump(st, f)


# ---------- transcript parsing ----------

def read_transcript(path):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def is_real_prompt(r):
    if r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain"):
        return False
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c) \
            and any(isinstance(b, dict) and b.get("type") == "text" for b in c)
    return False


def last_model(rows):
    for r in reversed(rows):
        if r.get("type") == "assistant" and not r.get("isSidechain"):
            m = (r.get("message") or {}).get("model")
            if m and m != "<synthetic>":
                return m
    return None


def final_response(rows):
    """All user-visible assistant text of the last turn (no thinking, no tool calls).

    An earlier version kept only text after the last tool result; that dropped
    the actual answer whenever it was written before a verifying tool call.
    """
    start = 0
    for i in range(len(rows) - 1, -1, -1):
        if is_real_prompt(rows[i]):
            start = i + 1
            break
    texts, model, ts = [], None, None
    for r in rows[start:]:
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        msg = r.get("message") or {}
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                texts.append(b["text"])
                ts = r.get("timestamp") or ts
        if msg.get("model") and msg.get("model") != "<synthetic>":
            model = msg["model"]
    return "\n\n".join(texts), model, ts


# ---------- log file ----------

def log_file(root, sid):
    d = os.path.join(root, ".agent-logs")
    os.makedirs(d, exist_ok=True)
    hits = sorted(glob.glob(os.path.join(d, f"*_{sid}.md")))
    if hits:
        return hits[0]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(d, f"{stamp}_{sid}.md")


def render_header(meta):
    sid = meta["session_id"]
    fm = "\n".join(f"{k}: {meta[k]}" for k in
                   ["session_id", "date", "author", "model", "tool", "project",
                    "total_exchanges", "first_prompt_time", "last_prompt_time"])
    return (f"---\n{fm}\n---\n\n# Session Log - {meta['date']}\n\n"
            f"Session: `{sid[:8]}` | Project: `{meta['project']}` | Author: `{meta['author']}`\n\n---\n")


def parse_header(text):
    meta = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        for line in text[4:end].splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                meta[k] = v
    return meta


def append_entry(root, sid, kind, ts, model, body):
    path = log_file(root, sid)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    meta = parse_header(existing)
    # body = everything after the header block
    marker = "\n---\n"
    if existing:
        hdr_end = existing.find("\n---\n", 4) + len(marker)       # end of frontmatter
        hdr_end = existing.find(marker, hdr_end) + len(marker)     # end of title block
        body_text = existing[hdr_end:]
    else:
        body_text = ""
        meta = {
            "session_id": sid,
            "date": ts[:10],
            "author": git_author(root),
            "model": model,
            "tool": TOOL,
            "project": os.path.basename(root),
            "total_exchanges": "0",
            "first_prompt_time": ts if kind == "PROMPT" else "",
            "last_prompt_time": ts if kind == "PROMPT" else "",
        }
    n = int(meta.get("total_exchanges") or 0)
    if kind == "PROMPT":
        n += 1
        meta["total_exchanges"] = str(n)
        meta["last_prompt_time"] = ts
        if not meta.get("first_prompt_time"):
            meta["first_prompt_time"] = ts
    num = max(n, 1)
    if model and not model.startswith("unknown"):
        if (meta.get("model") or "unknown").startswith("unknown"):
            meta["model"] = model
        elif model not in meta["model"].split(", "):
            meta["model"] += ", " + model   # model switch mid-session stays visible
    entry = (f"\n[LOG_ENTRY type={kind} num={num} session={sid[:8]}]\n"
             f"timestamp: {ts}\nmodel: {model or 'unknown'}\n\n{body.rstrip()}\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_header(meta) + body_text + entry)


# ---------- main ----------

def main():
    data = json.loads(sys.stdin.read() or "{}")
    event = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    sid = data.get("session_id") or "unknown-session"
    root = repo_root(data.get("cwd") or os.getcwd())
    st = load_state(root, sid)
    if os.environ.get("CAPTURE_DEBUG"):
        with open(state_path(root, f"debug-{event}"), "w") as f:
            json.dump({k: v for k, v in data.items() if k != "prompt"}, f, indent=1)

    if event == "SessionStart":
        m = data.get("model")
        if isinstance(m, dict):
            m = m.get("id") or m.get("display_name")
        if m:
            st["model"] = m
            save_state(root, sid, st)
        return

    rows = read_transcript(data.get("transcript_path"))

    if event == "UserPromptSubmit":
        # No hook payload carries the model; on a session's first prompt it is only
        # known once the response arrives (recorded on the RESPONSE entry + header).
        model = last_model(rows) or st.get("model") or os.environ.get("ANTHROPIC_MODEL") \
            or "unknown-until-first-response"
        append_entry(root, sid, "PROMPT", now_iso(), model, data.get("prompt", ""))
        return

    if event == "Stop":
        last = (data.get("last_assistant_message") or "").strip()
        text, model, ts = "", None, None
        # The transcript can lag the Stop event (seen in testing: the final message
        # was missing). Wait until it contains the payload's last message.
        for _ in range(20):
            rows = read_transcript(data.get("transcript_path"))
            text, model, ts = final_response(rows)
            if text and (not last or text.rstrip().endswith(last)):
                break
            time.sleep(0.25)
        if last and not text.rstrip().endswith(last):
            text = (text + "\n\n" + last) if text else last
            ts = None   # transcript never caught up; stamp with hook time
        model = model or last_model(rows) or st.get("model") or "unknown"
        if model != "unknown":
            st["model"] = model
            save_state(root, sid, st)
        append_entry(root, sid, "RESPONSE", ts or now_iso(), model,
                     text or "(no final text response captured)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            with open(os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."),
                                   ".claude", "agent-capture-errors.log"), "a") as f:
                f.write(now_iso() + "\n" + traceback.format_exc() + "\n")
        except Exception:
            pass
    sys.exit(0)
