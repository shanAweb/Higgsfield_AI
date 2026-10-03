# CAPTURE-TEST

## 1. Tool and model

- **Tool:** Claude Code CLI, v2.1.288 (macOS, terminal)
- **Model:** `claude-opus-5-5` (Opus 5.5) does both the planning and the executing. There is no separate planner model and no subagents (one agent per task, by choice).
- **Automatic mechanism:** Yes. Claude Code lifecycle hooks (`UserPromptSubmit`, `Stop`, `SessionStart`) run a command on every prompt and at the end of every turn, with no manual step.

## 2. Mechanism and config changed

- **`.claude/settings.json`** (project-level, committed) wires these hooks:
  - `SessionStart` runs `python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/capture.py" SessionStart`
  - `UserPromptSubmit` runs the same script with `UserPromptSubmit` and appends a PROMPT entry. The prompt comes verbatim from the hook's stdin JSON (`prompt`).
  - `Stop` runs the same script with `Stop` and appends a RESPONSE entry. The script reads the session transcript at `transcript_path` (from stdin) and takes all assistant **text** blocks of the turn, skipping thinking blocks and tool calls/results.
- **`.claude/hooks/capture.py`** is the capture script. It writes one file per session, `.agent-logs/YYYY-MM-DD_HH-MM-SS_<session-id>.md`, in the 8x format. Entries are only ever appended; the frontmatter counters/times are refreshed.
- **`.gitignore`** ignores only the hook's private state (`.claude/agent-capture-state/`) and its error log. `.agent-logs/` is **not** ignored.

## 3. Where the canaries landed

Each was a separate, freshly started Claude Code session (`claude -p ...` run in the repo root), so each loaded `.claude/settings.json` from scratch:

- `.agent-logs/2026-10-03_10-19-30_cf3f6d8a-6386-4132-b199-b09134a91035.md`
- `.agent-logs/2026-10-03_10-19-36_79cb622c-013e-4a79-9cb5-d878816e9f6b.md`
- `.agent-logs/2026-10-03_10-20-38_26adf6c9-5cdf-4b64-a6ba-f676cf5edfbe.md`

The setup session itself (which installed the hooks partway through) is in:

- `.agent-logs/2026-10-03_10-13-47_6b4c790c-5d02-4782-97f2-a83df6410647.md`

## 4. Canary entries (raw, copied from the log files by script)

### Canary 1: session `cf3f6d8a`

```
[LOG_ENTRY type=PROMPT num=1 session=cf3f6d8a]
timestamp: 2026-10-03T10:19:30.396Z
model: unknown

CAPTURE TEST — 8x assignment, shanAweb (automated headless canary 1). Reply with exactly: canary 1 received


[LOG_ENTRY type=RESPONSE num=1 session=cf3f6d8a]
timestamp: 2026-10-03T10:19:32.210Z
model: claude-opus-5-5

canary 1 received

```

### Canary 2: separate session `79cb622c`

```
[LOG_ENTRY type=PROMPT num=1 session=79cb622c]
timestamp: 2026-10-03T10:19:36.858Z
model: unknown

CAPTURE TEST — 8x assignment, shanAweb (automated headless canary 2). Reply with exactly: canary 2 received


[LOG_ENTRY type=RESPONSE num=1 session=79cb622c]
timestamp: 2026-10-03T10:19:38.654Z
model: claude-opus-5-5

canary 2 received

```

### Canary 3: separate session `26adf6c9`, multi-part response with a tool call (after the fixes below)

```
[LOG_ENTRY type=PROMPT num=1 session=26adf6c9]
timestamp: 2026-10-03T10:20:38.494Z
model: unknown-until-first-response

CAPTURE TEST — 8x assignment, shanAweb (headless canary after transcript-lag fix). Say 'canary received', then run the shell command: echo tool-ran, then say 'done'.


[LOG_ENTRY type=RESPONSE num=1 session=26adf6c9]
timestamp: 2026-10-03T10:20:45.988Z
model: claude-opus-5-5

canary received

The shell command ran and printed `tool-ran`.

Two connectors, Fireflies and Lusha, need authorization before they can be used. This session is non-interactive, so you'll need to authorize them in your claude.ai connector settings. This canary test didn't need them.

done

```

## 5. What did not work first (all left in the logs, unedited)

1. **The setup prompt was not captured.** The first prompt (the 8x setup instructions) was sent before `.claude/settings.json` existed, so no hook fired for it. The hooks went live partway through that same session. As a result, that session's log *opens with an orphan `RESPONSE num=1`* (only `Stop` fired for that turn) and has no matching PROMPT.
2. **Responses were truncated (bug).** Version 1 kept only the assistant text *after the last tool call* in a turn. The log for the second prompt in the setup session shows the result: the actual answer ("Yes, with one gap…") was written before a verification command and was dropped. Only the post-command paragraph survived. Fix: capture every user-visible text block of the turn (still excluding thinking and tool calls).
3. **The Stop hook raced the transcript (bug).** In a fresh test session (`a7ab07a1`), the hook ran before Claude Code had flushed the final message. The log has "canary received" but is missing the final "done". Fix: the script waits until the transcript ends with the `last_assistant_message` from the Stop payload, and appends that message if it never appears. Canary 3 confirms the fix.
4. **The model is unknown on a session's first prompt.** I dumped the raw hook payloads (debug session `d9059822`). No hook event (`SessionStart`, `UserPromptSubmit`, `Stop`) includes the model. The first PROMPT entry of a session is therefore labelled `unknown-until-first-response`, and every RESPONSE entry, plus the frontmatter `model:`, records the real model from the transcript. A mid-session model switch is added to the frontmatter `model:` field (comma-separated) and shows on each entry.
5. **Log location could follow `cd` (fixed before any real logging).** The first version picked the repo from the session's current directory, so a session that `cd`'d into another git repo could have written logs there. It now always uses `$CLAUDE_PROJECT_DIR`.

Canaries 1 and 2 were sent by Claude Code (on my request) through headless `claude -p` sessions, and they record the hook behaviour at that point. The log files for `a7ab07a1` and `d9059822` are kept as test sessions too.
