#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["sounddevice", "numpy", "mlx-whisper"]
# ///
"""Push-to-talk meeting companion.

Keeps the last few minutes of mic audio in memory. Press Enter to transcribe the
recent window locally and get a suggested answer from Claude, grounded in your
git history, Claude Code transcripts, claude-mem (if installed) and any brief in
~/.claude/briefs.

  Enter        answer using the last WINDOW seconds
  20 Enter     answer using the last 20 seconds
  ? text       ask a typed question instead
  q            quit

Scope args set where it looks (default: current directory), including every repo
nested inside. Each is a path, or a folder name under $COMPANION_ROOT (default ~/dev):
  companion .
  companion my-app
  companion ~/work
  companion api web
"""

import base64
import collections
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
import mlx_whisper
import numpy as np
import sounddevice as sd

RATE = 16_000
BLOCK = RATE // 10
BUFFER_SECONDS = 120
WINDOW = int(os.environ.get("COMPANION_WINDOW", 20))
MODEL = os.environ.get("COMPANION_MODEL", "sonnet")
WHISPER = os.environ.get("COMPANION_WHISPER", "mlx-community/whisper-large-v3-turbo")
HOME = Path.home()
ROOT = Path(os.environ.get("COMPANION_ROOT", HOME / "dev")).expanduser()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
MEM_DB = Path(os.environ.get("CLAUDE_MEM_DB", HOME / ".claude-mem/claude-mem.db")).expanduser()
HERE = Path(__file__).resolve().parent
JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_JQL = os.environ.get("COMPANION_JIRA_JQL", "sprint in openSprints() AND statusCategory != Done ORDER BY updated DESC")
JIRA_AUTH = base64.b64encode(f"{os.environ.get('JIRA_EMAIL')}:{os.environ.get('JIRA_API_TOKEN')}".encode()).decode()

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def git_identity():
    get = lambda key: subprocess.run(["git", "config", key], capture_output=True, text=True).stdout.strip()
    return f"{get('user.name')} <{get('user.email')}>"


SYSTEM = f"""You are my live meeting companion. I'm a software developer (git identity: {git_identity()}). Most of my work is done with Claude Code, so I don't always remember the details of what "I" built.

You get a raw local transcript of the last part of a face-to-face meeting. It mixes my voice and the other person's, with no speaker labels and possible transcription errors. Find the most recent question or topic directed at me and tell me what to say.

Where to look (read-only), fastest first:
1. The brief below, if any.
2. Git: `git -C <repo> log --all -i --author=<my email> ...`, `git -C <repo> show --stat <sha>`. Squash-merged PRs may show another author, so also use `gh pr list/view -R <owner/repo>` (owner/repo from `git -C <repo> remote get-url origin`).
3. Claude Code transcripts: {CLAUDE_DIR}/projects/*/*.jsonl. Grep, never read whole files."""

if MEM_DB.exists():
    SYSTEM += f"""
4. claude-mem: `sqlite3 -readonly {MEM_DB}` with tables session_summaries(project, request, completed, learned, next_steps, created_at, created_at_epoch ms) and observations(project, type, title, narrative, created_at_epoch). project is the session folder name, often a ticket or worktree name. Skip transcripts under *observer-sessions*, which are claude-mem's own."""

SYSTEM += """

Jira tickets mentioned in the transcript are attached below it when found. If a ticket has no local work (no commits, branches or sessions for it), say plainly that I haven't started it, then give what it asks, the open questions, and a likely approach based on how similar work was done in my repos.

I'm reading this mid-conversation, so be fast: at most ~4 tool calls, and answer from the brief when it suffices.

Format, in the language of the question:
- First line: the answer as I'd say it out loud, max 25 words.
- Then up to 4 short bullets with supporting detail (why, tradeoffs, status).
- Last line: refs (ticket, PR, commit sha) in brackets.
If you can't find it, say so in one line and suggest what I can say instead. Never invent details."""


def resolve(name):
    path = Path(name).expanduser()
    if path.is_dir():
        return path.resolve()
    matches = [p for p in [ROOT / name, *ROOT.glob(f"*/{name}")] if p.is_dir()]
    if not matches:
        sys.exit(f"scope not found: {name} (not a path, nor a folder under {ROOT})")
    return matches[0]


def worktrees(repo):
    out = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    paths = [Path(line[9:]) for line in out.splitlines() if line.startswith("worktree ")]
    return [p for p in paths if p.is_dir() and not p.is_relative_to(repo)]


def find_repos(folder):
    repos = []
    for root, dirs, files in os.walk(folder):
        if ".git" in dirs or ".git" in files:
            repos.append(Path(root))
            dirs.clear()  # don't descend into a repo; its worktrees come from `git worktree list`
        else:
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
    return repos


def scope_context(folders):
    """Map scope folders to their repos, worktrees, transcript dirs and claude-mem projects."""
    repos = [r for f in folders for r in find_repos(f)]
    trees = [w for r in repos for w in worktrees(r)]

    # Claude Code names transcript dirs after the session cwd, with / and . replaced by -
    prefixes = tuple(re.sub(r"[/.]", "-", str(p)) for p in [*folders, *repos, *trees])
    projects_dir = CLAUDE_DIR / "projects"
    transcript_dirs = [d for d in projects_dir.iterdir() if d.name.startswith(prefixes)] if projects_dir.is_dir() else []

    text = f"""

SCOPE: only use context from these folders. Ignore every other repo, project and transcript, even if it looks relevant.
- Repos: {", ".join(map(str, repos or folders))}
- Transcript dirs: {", ".join(map(str, transcript_dirs)) or "none"}"""

    if MEM_DB.exists():
        session_ids = [f.stem for d in transcript_dirs for f in d.glob("*.jsonl")]
        db = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True)
        projects = sorted(row[0] for row in db.execute(
            f"select distinct project from sdk_sessions where content_session_id in ({','.join('?' * len(session_ids))}) and project != ''",
            session_ids))
        db.close()
        text += f"\n- claude-mem projects (filter with `project in (...)`): {', '.join(projects) or 'none'}"

    return text, [*folders, *trees]


def latest_brief():
    briefs = sorted((CLAUDE_DIR / "briefs").glob("*.md"), key=lambda p: p.stat().st_mtime)
    return f"\n\nLatest brief ({briefs[-1].name}):\n{briefs[-1].read_text()}" if briefs else ""


def jira_get(path):
    request = urllib.request.Request(f"{JIRA_URL}/rest/api/2/{path}", headers={
        "Authorization": f"Basic {JIRA_AUTH}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def jira_key_pattern():
    """Match only real project keys, loosely, since whisper writes ABC-123 as "ABC 123" or "abc123"."""
    if not (JIRA_URL and os.environ.get("JIRA_API_TOKEN")):
        return None
    try:
        # Jira Cloud silently treats rejected credentials as anonymous, so check them explicitly
        me = jira_get("myself")
        keys = [p["key"] for p in jira_get("project")]
    except urllib.error.HTTPError as e:
        hint = " (check JIRA_EMAIL and that the token is a classic API token, not a scoped one)" if e.code in (401, 403) else ""
        print(f"{BOLD}jira disabled:{RESET} HTTP {e.code}{hint}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"{BOLD}jira disabled:{RESET} {e}")
        return None
    if not keys:
        print(f"{BOLD}jira disabled:{RESET} {me.get('emailAddress', os.environ.get('JIRA_EMAIL'))} can't see any projects")
        return None
    print(f"{DIM}jira: signed in as {me.get('displayName')}, {len(keys)} projects{RESET}")
    return re.compile(rf"\b({'|'.join(map(re.escape, keys))})[\s-]?(\d+)\b", re.I)


def jira_index():
    """One line per open sprint ticket, so a ticket described without its key can still be identified."""
    query = urllib.parse.urlencode({"jql": JIRA_JQL, "fields": "summary,status,assignee", "maxResults": 100})
    for endpoint in ("search/jql", "search"):  # Jira Cloud, then Server/DC
        try:
            issues = jira_get(f"{endpoint}?{query}")["issues"]
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"jira index skipped: HTTP {e.code}")
                return ""
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"jira index skipped: {e}")
            return ""
    else:
        print("jira index skipped: no search endpoint")
        return ""
    print(f"{DIM}jira: {len(issues)} open sprint tickets indexed{RESET}")
    lines = [
        f"- {i['key']}: {i['fields']['summary']} [{i['fields']['status']['name']}, "
        f"{(i['fields']['assignee'] or {}).get('displayName', 'unassigned')}]"
        for i in issues
    ]
    return "\n\nOpen sprint tickets (key: summary [status, assignee]). If the transcript describes one of these without its key, use this to identify it:\n" + "\n".join(lines) if lines else ""


def jira_ticket(key):
    fields = "summary,status,issuetype,priority,assignee,reporter,description,comment,issuelinks,parent,labels"
    try:
        f = jira_get(f"issue/{key}?fields={fields}")["fields"]
    except urllib.error.HTTPError as e:
        print(f"{DIM}jira: {key} {'not found' if e.code == 404 else f'HTTP {e.code}'}{RESET}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"{DIM}jira: {key} failed: {e}{RESET}")
        return None

    name = lambda person: person["displayName"] if person else "unassigned"
    links = [
        f"{kind} {other['key']} ({other['fields']['summary']})"
        for link in f["issuelinks"]
        for kind, other in [(link["type"]["outward"], link.get("outwardIssue")), (link["type"]["inward"], link.get("inwardIssue"))]
        if other
    ]
    comments = [
        f"- {c['author']['displayName']} ({c['created'][:10]}): {c['body'][:500]}"
        for c in f["comment"]["comments"][-3:]
    ]
    parent = f"{f['parent']['key']} ({f['parent']['fields']['summary']})" if f.get("parent") else "none"
    return f"""{key} [{f['issuetype']['name']}] {f['summary']}
Status: {f['status']['name']} · Assignee: {name(f['assignee'])} · Reporter: {name(f['reporter'])} · Priority: {(f.get('priority') or {}).get('name', 'none')}
Parent: {parent} · Labels: {", ".join(f['labels']) or "none"}
Links: {"; ".join(links) or "none"}
Description:
{(f['description'] or "(empty)")[:4000]}
Latest comments:
{chr(10).join(comments) or "none"}"""


def transcribe(audio):
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER, condition_on_previous_text=False)
    return result["text"].strip()


def ask(prompt, session_id, first, scope_text, dirs):
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--append-system-prompt", SYSTEM + scope_text + latest_brief(),
        # Skip user settings so plugin hooks don't slow down or block each answer
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--add-dir", *(str(d) for d in [*dirs, CLAUDE_DIR / "projects", CLAUDE_DIR / "briefs", MEM_DB.parent] if d.is_dir()),
        "--allowedTools", "Read", "Grep", "Glob",
        "Bash(git -C:*)", "Bash(git log:*)", "Bash(git show:*)",
        "Bash(sqlite3 -readonly:*)", "Bash(gh pr view:*)", "Bash(gh pr list:*)",
        "Bash(grep:*)", "Bash(jq:*)",
    ]
    cmd += ["--session-id", session_id] if first else ["--resume", session_id]

    proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        msg = json.loads(line)
        if msg.get("type") != "stream_event":
            if msg.get("type") == "result" and msg.get("is_error"):
                print(f"\n{BOLD}error:{RESET} {msg.get('result')}")
            continue
        event = msg["event"]
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            print(f"{DIM}[{event['content_block']['name']}…]{RESET}", flush=True)
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta":
            print(event["delta"]["text"], end="", flush=True)
    proc.wait()
    print()


def main():
    scope_text, dirs = scope_context([resolve(a) for a in sys.argv[1:]] or [Path.cwd()])
    summary = [line for line in scope_text.splitlines() if line.startswith(("- Repos", "- claude-mem"))]
    print(DIM + "\n".join(summary) + RESET)

    blocks = collections.deque(maxlen=BUFFER_SECONDS * RATE // BLOCK)

    def on_audio(indata, frames, time, status):
        blocks.append(indata[:, 0].copy())

    jira_keys = jira_key_pattern()
    if jira_keys:
        scope_text += jira_index()
    attached = set()  # tickets already in the Claude session; follow-ups resume it

    print(f"{DIM}loading whisper…{RESET}")
    transcribe(np.zeros(RATE, dtype=np.float32))

    session_id = str(uuid.uuid4())
    first = True
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=on_audio):
        print(f"{BOLD}listening.{RESET} Enter = last {WINDOW}s · '60' = last 60s · '? question' = typed · q = quit")
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd == "q":
                break

            if cmd.startswith("?"):
                prompt = f"Typed question: {cmd[1:].strip()}"
            else:
                seconds = int(cmd) if cmd.isdigit() else WINDOW
                audio = np.concatenate(list(blocks)[-seconds * RATE // BLOCK:]) if blocks else np.zeros(0, np.float32)
                if audio.size == 0 or np.sqrt(np.mean(audio**2)) < 1e-4:
                    print("no audio captured. Check the mic permission for this terminal app in System Settings › Privacy › Microphone.")
                    continue
                text = transcribe(audio)
                print(f"{DIM}heard: {text}{RESET}\n")
                prompt = f"Transcript of the last {seconds}s:\n{text}"

            if jira_keys:
                keys = {f"{m[1].upper()}-{m[2]}" for m in jira_keys.finditer(prompt)} - attached
                tickets = [t for key in sorted(keys) if (t := jira_ticket(key))]
                attached |= keys
                if tickets:
                    print(f"{DIM}[jira {', '.join(sorted(keys))}]{RESET}")
                    prompt += "\n\nJira tickets mentioned:\n\n" + "\n\n".join(tickets)

            ask(prompt, session_id, first, scope_text, dirs)
            first = False


if __name__ == "__main__":
    sys.exit(main())
