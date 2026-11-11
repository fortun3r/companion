#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["sounddevice", "numpy", "mlx-whisper"]
# ///
"""Push-to-talk meeting companion.

Transcribes the conversation locally in the background, from the mic and from whatever
the Mac plays (so calls work on headphones). Press Enter to get a
suggested answer from Claude for the latest question, with the earlier
conversation as context, grounded in your code, git history, Claude Code
transcripts, claude-mem (if installed), Jira and any brief in ~/.claude/briefs.

  Enter        answer what was just asked (the question is likely in the last WINDOW seconds)
  60 Enter     treat the last 60 seconds as the latest part
  text Enter   same as Enter, but focus the answer on "text"
  ? text       ask a typed question, no audio
  ! ...        select a screen area first and attach the screenshot, e.g. "!" or "! ? what's this error"
  q            quit (and write the meeting summary)

Scope args set where it looks (default: current directory), including every repo
nested inside. Each is a path, or a folder name under $COMPANION_ROOT (default ~/dev):
  companion .
  companion my-app
  companion ~/work
  companion api web
  companion . --about "sprint planning with Ana, SA-3424"
                           what the meeting is about: sharper answers, better spelled names
  companion . --no-record  don't save the full transcript next to the meeting notes
  companion . --mic-only   don't capture what the Mac plays (calls, videos, music)
  companion . --json       for the app: same commands on stdin, one JSON event per line on stdout
"""

import base64
import collections
import json
import queue
import threading
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

WHISPER = os.environ.get("COMPANION_WHISPER", "mlx-community/whisper-large-v3-turbo")

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
# Once the model is cached, skip the Hub's update check (and its unauthenticated-request warning).
# Must be set before huggingface_hub is imported.
_hf_cache = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub")
_whisper_dir = _hf_cache / f"models--{WHISPER.replace('/', '--')}"
_whisper_cached = (_whisper_dir / "snapshots").is_dir()
if _whisper_cached:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
import mlx_whisper
import numpy as np
import sounddevice as sd

RATE = 16_000
BLOCK = RATE // 10
WINDOW = int(os.environ.get("COMPANION_WINDOW", 20))
CONTEXT_SECONDS = int(os.environ.get("COMPANION_CONTEXT", 600))
MIN_CHUNK, MAX_CHUNK = 8, 30  # seconds of audio per background transcription
PAUSE_BLOCKS = 5  # 0.5s of quiet ends a chunk once it's past MIN_CHUNK
# A chunk is only transcribed if it has 0.5s+ this many times louder than the room's noise floor.
# Whisper turns pure room noise into "Thank you." and its no_speech_prob doesn't flag it.
SPEECH_RATIO = float(os.environ.get("COMPANION_SPEECH_RATIO", 3))
MODEL = os.environ.get("COMPANION_MODEL", "sonnet")
HOME = Path.home()
ROOT = Path(os.environ.get("COMPANION_ROOT", HOME / "dev")).expanduser()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
# Briefs and meeting notes stay in one place even when CLAUDE_CONFIG_DIR switches per account
NOTES_HOME = Path(os.environ.get("COMPANION_NOTES_HOME", HOME / ".claude")).expanduser()
MEM_DB = Path(os.environ.get("CLAUDE_MEM_DB", HOME / ".claude-mem/claude-mem.db")).expanduser()
HERE = Path(__file__).resolve().parent
JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_JQL = os.environ.get("COMPANION_JIRA_JQL", "sprint in openSprints() AND statusCategory != Done ORDER BY updated DESC")
JIRA_AUTH = base64.b64encode(f"{os.environ.get('JIRA_EMAIL')}:{os.environ.get('JIRA_API_TOKEN')}".encode()).decode()

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
JSON = "--json" in sys.argv
_emit_lock = threading.Lock()


def emit(kind, **fields):
    """--json mode: one event per line on the real stdout (captions run on their own thread)."""
    with _emit_lock:
        sys.__stdout__.write(json.dumps({"type": kind, **fields}) + "\n")
        sys.__stdout__.flush()


def download_whisper():
    """First run: fetch the model up front, not inside mlx_whisper, to show how far along it is. Progress is
    what has landed on disk, since the Hub only draws progress bars on a terminal."""
    from huggingface_hub import HfApi, snapshot_download
    total = sum(f.size or 0 for f in HfApi().model_info(WHISPER, files_metadata=True).siblings)
    dirs = [_whisper_dir, _hf_cache.parent / "xet"]  # finished files, and xet's chunks as they arrive

    def size():
        n = 0
        for d in dirs:
            for f in d.rglob("*"):
                try:
                    n += f.stat().st_size if f.is_file() and not f.is_symlink() else 0
                except FileNotFoundError:  # moved into place mid-scan
                    pass
        return n

    start, done = size(), threading.Event()

    def report():
        while not done.wait(1):
            got = min(size() - start, total)
            print(f"{DIM}downloading the speech model, first time only: {got / 1e6:,.0f} of {total / 1e6:,.0f} MB{RESET}",
                  end="\r", flush=True)

    threading.Thread(target=report, daemon=True).start()
    try:
        snapshot_download(WHISPER)
    finally:
        done.set()
    print()


class StatusLines:
    """--json mode stands this in for stdout, so every existing print becomes a status event, unstyled."""

    def __init__(self):
        self.buffer = ""

    def write(self, s):
        *lines, self.buffer = re.split(r"[\r\n]", self.buffer + s)
        for line in lines:
            if text := re.sub(r"\033\[[0-9;]*[A-Za-z]", "", line).strip():
                emit("status", text=text)
        return len(s)

    def flush(self):
        pass


def git_identity():
    get = lambda key: subprocess.run(["git", "config", key], capture_output=True, text=True).stdout.strip()
    return f"{get('user.name')} <{get('user.email')}>"


SYSTEM = f"""You are my live meeting companion. I'm a software developer (git identity: {git_identity()}). Most of my work is done with Claude Code, so I don't always remember the details of what "I" built.

You get a raw local transcript of a meeting, in person or on a call, in two parts: the conversation since my last question (context), then the latest part, where what I need now most likely is. On a call, lines are labeled [me] (my mic) or [them] (everyone else on the call, not told apart). In person there are no labels, so infer who is speaking. Expect transcription errors. Use the earlier part to resolve references like "that ticket" or "the other approach", and to find tickets mentioned before. Decide what I need right now:
- A question was just asked: answer it, whoever asked it and whoever it was aimed at, me included asking someone else. I pressed Enter because I want the answer.
- Several questions in a row: answer the last one, plus one line for any earlier one still unanswered.
- A follow-up to something said earlier: answer it, using the earlier context.
- No question, just discussion: one line on what's being discussed, then the one fact most worth adding.
- I was the one talking, not asking: the details I was reaching for.
If I give a focus, answer about that, using the conversation as context.

Where to look (read-only), fastest first:
1. The brief below, if any.
2. Git: `git -C <repo> log --all -i --author=<my email> ...`, `git -C <repo> show --stat <sha>`. Squash-merged PRs may show another author, so also use `gh pr list/view -R <owner/repo>` (owner/repo from `git -C <repo> remote get-url origin`).
3. Claude Code transcripts: {CLAUDE_DIR}/projects/*/*.jsonl. Grep, never read whole files."""

if MEM_DB.exists():
    SYSTEM += f"""
4. claude-mem: `sqlite3 -readonly {MEM_DB}` with tables session_summaries(project, request, completed, learned, next_steps, created_at, created_at_epoch ms) and observations(project, type, title, narrative, created_at_epoch). project is the session folder name, often a ticket or worktree name. Skip transcripts under *observer-sessions*, which are claude-mem's own."""

SYSTEM += """

The code itself is the ground truth for how things work. For "how does X work", "does it do Y" or "what happens when Z" questions, Grep the repos in scope first and read only the matching lines, then cite `file:line`. History explains why. The code shows what's true now. Each folder is on whatever branch is checked out. For unmerged work, look in the ticket's worktree, or use `git -C <repo> show <branch>:<path>`. Before describing how code works, check whether one of my open PRs or worktrees changes it (`git -C <repo> log --all -i --author=<my email> -- <file>`). If one does, say what the main branch does today and what my pending change does.

Jira tickets mentioned in the transcript are attached below it when found. If a ticket has no local work (no commits, branches or sessions for it), say plainly that I haven't started it, then give what it asks, the open questions, and a likely approach based on how similar work was done in my repos.

I'm reading this mid-conversation, so be fast: at most ~4 tool calls, run independent searches in parallel in one turn, and answer from the brief when it suffices.
Your very first output, before any tool call, is one line: `Q: <the question you're answering, max 15 words>`, or `Q: none, <what's being discussed>`, so I can check you picked the right one. After it, write nothing before or between tool calls, only the final answer.

Format, in the language of the question. I glance at this while someone is waiting, so these are hard limits:
- First line: the answer as I'd say it out loud, max 25 words. Plain text.
- Then at most 3 bullets, each one line, max ~20 words: only the details I'd actually say next. No sub-bullets, headings or caveats nobody asked about.
- It's printed raw in a terminal: no **bold** or other markdown emphasis. Backticks for code names are fine.
- End the first line and each bullet with the source of that line in brackets, so I can tell which claim comes from where: ticket, PR, commit sha, file:line, `session <date>` for a Claude Code session or claude-mem summary, `brief` for the brief. Several sources are comma-separated. A line with no source gets none.
If you can't find it, say so in one line and suggest what I can say instead. Never invent details."""

SUMMARY_PROMPT = """The meeting is over. Write my notes for it as markdown, with no preamble and no other text:

### Topics
One line per topic discussed, with ticket keys.

### I committed to
Follow-ups I said I'd do, from my own words in the transcripts (e.g. "I'll check after this"; on a call, only [me] lines). Write "none" if there were none. Don't count what you suggested I say unless the transcript shows I said it.

### Open questions
Things raised that nobody could answer yet.

Use only this session's transcripts and answers."""


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
    briefs = sorted((NOTES_HOME / "briefs").glob("*.md"), key=lambda p: p.stat().st_mtime)
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


def transcribe(audio, hint=None):
    """Returns the kept segments as (start second, end second, text).

    The hint (what the meeting is about) helps whisper spell names and jargon; chunks are under 30s, so it
    applies to the whole chunk. On near-silence whisper can repeat the hint back, so those segments are dropped.
    """
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER, condition_on_previous_text=False,
                                    initial_prompt=hint or None)
    norm = lambda text: re.sub(r"\W+", " ", text.lower()).strip()
    # Whisper's own hallucination thresholds: looping text compresses too well, guesses score low
    return [(s["start"], s["end"], s["text"].strip()) for s in result["segments"]
            if s["compression_ratio"] <= 2.4 and s["avg_logprob"] >= -1.0 and norm(s["text"])
            and not (hint and norm(s["text"]) in norm(hint))]


def is_echo(line, others):
    """On speakers the mic also hears the call: drop mic lines that system audio heard at the same moment."""
    start, end, text = line
    heard = {w for s, e, t in others if s < end + 1 and e > start - 1 for w in re.findall(r"\w+", t.lower())}
    words = re.findall(r"\w+", text.lower())
    return bool(words) and sum(w in heard for w in words) >= 0.6 * len(words)


class Listener:
    """Transcribes the mic and system audio in the background, cutting chunks at pauses.

    Once system audio is heard, each is transcribed on its own and labeled: the mic is me, system audio
    is everyone on the call.

    All whisper calls happen on the worker thread: MLX streams are per-thread.
    Times are seconds of audio received, so the main thread can ask for "the last N seconds".
    """

    def __init__(self, record_path=None, started=None):
        self.audio = queue.Queue()
        self.flushes = queue.Queue()
        self.segments = []  # (end second, "me" / "them" / "" when unlabeled, text), the whole meeting
        self.lock = threading.Lock()
        self.received = 0
        # last minute of block loudness per track (mic, system), for the noise floor
        self.levels = (collections.deque(maxlen=600), collections.deque(maxlen=600))
        self.system = collections.deque(maxlen=5)  # bounded, so a lagging mic can't delay system audio
        self.system_heard = False
        self.live, self.about = False, ""
        # Live captions only grow, like a call's captions: [label, words] turns, plus the words still in flux
        self.captions, self.tentative, self.shown, self.last_preview = [], [], {}, {}
        self.ready = threading.Event()
        self.record_path, self.started = record_path, started
        threading.Thread(target=self._run, daemon=True).start()

    def on_audio(self, indata, frames, time, status):
        silence = np.zeros(BLOCK, dtype=np.float32)
        self.audio.put((indata[:, 0].copy(), self.system.popleft() if self.system else silence))

    def add_system_audio(self, stream):
        """Add a raw 16 kHz float32 stream alongside the mic, which stays the clock."""
        def read():
            while data := stream.read(BLOCK * 4):
                if len(data) == BLOCK * 4:
                    block = np.frombuffer(data, dtype=np.float32)
                    self.system_heard = self.system_heard or bool(block.any())
                    self.system.append(block)

        threading.Thread(target=read, daemon=True).start()

    def now(self):
        return self.received * BLOCK / RATE

    def flush(self):
        """Transcribe whatever is pending right now and wait for it."""
        done = threading.Event()
        self.flushes.put(done)
        done.wait()

    def text(self, start, end=float("inf")):
        with self.lock:
            return [seg for seg in self.segments if start < seg[0] <= end]

    def _run(self):
        transcribe(np.zeros(RATE, dtype=np.float32))
        self.ready.set()
        pending, quiet, sumsq, previewed = [], 0, 0.0, 0
        while True:
            try:
                blocks = [self.audio.get(timeout=0.1)]
            except queue.Empty:
                blocks = []
            try:
                done = self.flushes.get_nowait()
            except queue.Empty:
                done = None
            if done:  # take everything already captured, not just this block
                while not self.audio.empty():
                    blocks.append(self.audio.get_nowait())

            for mic, system in blocks:
                pending.append((mic, system))
                self.received += 1
                self.levels[0].append(float(np.sqrt(np.mean(mic**2))))
                self.levels[1].append(float(np.sqrt(np.mean(system**2))))
                block = mic + system  # pauses are judged on what everyone says
                rms = float(np.sqrt(np.mean(block**2)))
                sumsq += float(np.sum(block**2))
                level = np.sqrt(sumsq / (len(pending) * BLOCK))
                quiet = quiet + 1 if rms < 0.3 * level else 0

            seconds = len(pending) * BLOCK / RATE
            if done or seconds >= MAX_CHUNK or (seconds >= MIN_CHUNK and quiet >= PAUSE_BLOCKS):
                self._cut(pending)
                pending, quiet, sumsq, previewed = [], 0, 0.0, 0
            # ponytail: re-transcribes the whole pending chunk every 2s (~1s of work at 30s); skipped when behind
            elif self.live and len(pending) - previewed >= max(10, len(pending) // 10) and self.audio.qsize() < 5:
                previewed = len(pending)
                self._update_captions(self._transcribe(pending), final=False)
            if done:
                done.set()

    def _cut(self, pending):
        merged = self._transcribe(pending) if pending else []
        with self.lock:
            self.segments.extend(merged)
        if self.live:
            self._update_captions(merged, final=True)
        if self.record_path and merged:
            with self.record_path.open("a") as f:
                f.writelines(f"[{self.started + timedelta(seconds=t):%H:%M:%S}] {as_text([(t, label, text)])}\n"
                             for t, label, text in merged)

    def _transcribe(self, pending):
        start = self.now() - len(pending) * BLOCK / RATE
        mic, system = (np.concatenate(track) for track in zip(*pending))
        # Until system audio is heard (not on a call, or no permission yet) the mic hears everyone
        tracks = {"me": (mic, 0), "them": (system, 1)} if self.system_heard else {"": (mic, 0)}
        lines = {label: self._speech(audio, levels) for label, (audio, levels) in tracks.items()}
        if "me" in lines:
            lines["me"] = [line for line in lines["me"] if not is_echo(line, lines["them"])]

        merged = []  # consecutive lines by the same side become one
        for s, e, label, text in sorted((s, e, label, t) for label, found in lines.items() for s, e, t in found):
            if merged and merged[-1][1] == label:
                merged[-1] = (e, label, f"{merged[-1][2]} {text}")
            else:
                merged.append((e, label, text))
        return [(start + e, label, text) for e, label, text in merged]

    def _loud(self, audio, levels):
        """Whether a track has 0.5s+ of speech above its noise floor."""
        floor = np.percentile(self.levels[levels], 10)
        loudness = np.sqrt(np.mean(audio.reshape(-1, BLOCK) ** 2, axis=1))
        return (loudness > max(SPEECH_RATIO * floor, 1e-3)).sum() >= 5

    def _speech(self, audio, levels):
        return transcribe(audio, self.about) if self._loud(audio, levels) else []

    def _update_captions(self, segments, final):
        """A word is shown for good once two previews of the chunk agree on it, or the chunk ends.

        ponytail: assumes the final text starts with the words already shown; if whisper changes its mind
        about them, a word can be doubled or skipped in the captions (never in the transcript sent to Claude).
        """
        heard = {}
        for _, label, text in segments:
            heard.setdefault(label, []).extend(text.split())
        norm = lambda word: re.sub(r"\W", "", word.lower())
        with self.lock:
            for label, words in heard.items():
                previous = self.last_preview.get(label, [])
                agreed = next((i for i, (a, b) in enumerate(zip(words, previous)) if norm(a) != norm(b)),
                              min(len(words), len(previous)))
                stable, done = len(words) if final else agreed, self.shown.get(label, 0)
                if stable > done:
                    if self.captions and self.captions[-1][0] == label:
                        self.captions[-1][1].extend(words[done:stable])
                    else:
                        self.captions.append([label, words[done:stable]])
                    del self.captions[:-20]
                    self.shown[label] = stable
            if final:
                self.shown, self.last_preview, self.tentative = {}, {}, []
            else:
                self.last_preview = heard
                self.tentative = [(label, words[self.shown.get(label, 0):]) for label, words in heard.items()]

    def caption_turns(self):
        """The last caption turns as (label, settled words, words still in flux)."""
        with self.lock:
            out = [(label, list(words), []) for label, words in self.captions[-10:]]
            for label, words in self.tentative:
                if out and out[-1][0] == label:
                    out[-1] = (label, out[-1][1], words)
                elif words:
                    out.append((label, [], words))
        return out


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
        "--add-dir", *(str(d) for d in [*dirs, CLAUDE_DIR / "projects", NOTES_HOME / "briefs", NOTES_HOME / "meetings", MEM_DB.parent] if d.is_dir()),
        "--allowedTools", "Read", "Grep", "Glob",
        "Bash(git -C:*)", "Bash(git log:*)", "Bash(git show:*)",
        "Bash(sqlite3 -readonly:*)", "Bash(gh pr view:*)", "Bash(gh pr list:*)",
        "Bash(grep:*)", "Bash(jq:*)",
    ]
    cmd += ["--session-id", session_id] if first else ["--resume", session_id]

    answer, printer = [], JsonPrinter() if JSON else AnswerPrinter()
    proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    for line in proc.stdout:
        msg = json.loads(line)
        if msg.get("type") == "assistant":  # a finished block, so the tool input is complete
            for block in msg["message"]["content"]:
                if block["type"] == "tool_use":
                    printer.step(tool_summary(block["name"], block["input"]))
        if msg.get("type") != "stream_event":
            if msg.get("type") == "result" and msg.get("is_error"):
                print(f"\n{BOLD}error:{RESET} {msg.get('result')}")
            continue
        event = msg["event"]
        if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
            printer.tool()
            answer = []  # only text after the last tool call is the answer
        elif event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta":
            printer.text(event["delta"]["text"])
            answer.append(event["delta"]["text"])
    proc.wait()
    printer.finish()
    return re.sub(r"\AQ:.*\n*", "", "".join(answer).strip())  # the Q: line is a progress cue, not part of the answer


def tool_summary(name, args):
    """What a tool call is looking at, in a few words."""
    if name == "Bash":
        what = args.get("command", "")
    elif name == "Grep":
        what = f'grep "{args.get("pattern")}"' + (f" in {Path(args['path']).name}" if args.get("path") else "")
    elif name == "Read":
        what = f"read {Path(args.get('file_path', '')).name}"
    elif name == "Glob":
        what = f"find {args.get('pattern')}"
    else:
        what = name
    return " ".join(what.replace(str(HOME), "~").split())


class AnswerPrinter:
    """Formats the streamed answer a line at a time: bold headline, wrapped bullets, colored code, dim refs."""

    CODE, CODE_END = "\033[36m", "\033[39m"  # color only, so it doesn't end the bold of a headline

    def __init__(self):
        self.buffer, self.searching, self.headline_done = "", False, False
        self.width = min(shutil.get_terminal_size().columns, 100) - 2
        self.started = time.monotonic()

    def elapsed(self):
        return f"{time.monotonic() - self.started:3.0f}s"

    def tool(self):
        if self.buffer.startswith("Q:"):
            self.line(self.buffer)
        self.buffer = ""  # any other unfinished line before a tool call is narration ("Let me check…"), not the answer

    def step(self, what):
        line = f"  {self.elapsed()}  {what}"
        print(f"{DIM}{line[:self.width - 1] + '…' if len(line) > self.width else line}{RESET}", flush=True)
        self.searching = True

    def text(self, delta):
        if self.searching:
            print()
            self.searching = False
        self.buffer += delta
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.line(line)

    def finish(self):
        if self.buffer.strip():
            self.line(self.buffer)
        self.buffer = ""
        print(f"{DIM}  {self.elapsed()}{RESET}")

    def line(self, line):
        text = line.strip().replace("**", "")
        if not text:
            print()
            return
        if text.startswith("Q:") and not self.headline_done:
            print(f"\033[3m{text}{RESET}", flush=True)
            return
        if text.startswith("#"):
            print(f"{BOLD}{text.lstrip('# ')}{RESET}")
            return
        if re.fullmatch(r"\[.*\]", text):
            print(f"{DIM}  {text[1:-1]}{RESET}")
            return

        style, first, rest = "", "", ""
        if text[:2] in ("- ", "* ", "• "):
            text, first, rest = text[2:], "  • ", "    "
        elif not self.headline_done:
            style = BOLD
        self.headline_done = True

        # NBSP keeps code spans from being split across lines; textwrap only breaks on ASCII spaces
        text = re.sub(r"`[^`]+`", lambda m: m[0].replace(" ", " "), text)
        wrapped = textwrap.fill(text, self.width, initial_indent=first, subsequent_indent=rest,
                                break_long_words=False, break_on_hyphens=False).replace(" ", " ")
        wrapped = re.sub(r"^(  • )([^:`\n]{1,30}):", rf"\1{BOLD}\2:{RESET}", wrapped)  # "Gate: ..." labels
        wrapped = re.sub(r"`([^`]+)`", rf"{self.CODE}\1{self.CODE_END}", wrapped)
        wrapped = re.sub(r"(\s)(\[[^\[\]]+\])$", rf"\1{RESET}{DIM}\2", wrapped)  # trailing source
        print(f"{style}{wrapped}{RESET}", flush=True)


class JsonPrinter(AnswerPrinter):
    """--json mode: the question, steps and raw markdown answer lines as events, for the app to render."""

    def step(self, what):
        emit("step", text=what, seconds=round(time.monotonic() - self.started))

    def line(self, line):
        text = line.strip().replace("**", "")
        if text.startswith("Q:") and not self.headline_done:
            emit("question", text=text[2:].strip())
            return
        self.headline_done = self.headline_done or bool(text)
        emit("line", text=line)

    def finish(self):
        if self.buffer.strip():
            self.line(self.buffer)
        self.buffer = ""
        emit("done", seconds=round(time.monotonic() - self.started))


def log_exchange(notes, title, question, answer):
    """Append as we go, so a crash or Ctrl+C loses nothing."""
    notes.parent.mkdir(parents=True, exist_ok=True)
    if not notes.exists():
        notes.write_text(f"# {title}\n")
    with notes.open("a") as f:
        f.write(f"\n## {datetime.now():%H:%M} · {question}\n\n{answer}\n")


def summarize(notes, title, prompt, session_id, first, scope_text, dirs):
    print(f"\n{DIM}writing meeting summary to {notes} (Ctrl+C to skip)…{RESET}\n")
    try:
        summary = ask(prompt, session_id, first, scope_text, dirs)
    except KeyboardInterrupt:
        return
    exchanges = notes.read_text().removeprefix(f"# {title}\n") if notes.exists() else ""
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(f"# {title}\n\n{summary}\n\n---\n{exchanges}")


def turns(segments):
    """Merge consecutive segments by the same side into [label, text] turns."""
    merged = []
    for _, label, text in segments:
        if merged and merged[-1][0] == label:
            merged[-1][1] += " " + text
        else:
            merged.append([label, text])
    return merged


def as_text(segments):
    return "\n".join(f"[{label}] {text}" if label else text for label, text in turns(segments))


def start_system_audio():
    """Build the capture helper if needed and start it. Returns None, after saying why, if it can't run."""
    source, helper = HERE / "system_audio.swift", HERE / ".system_audio"
    if not helper.exists() or helper.stat().st_mtime < source.stat().st_mtime:
        if not shutil.which("swiftc"):
            print(f"{BOLD}system audio off:{RESET} needs swiftc (xcode-select --install). Using the mic only.")
            return None
        print(f"{DIM}building system audio capture…{RESET}")
        build = subprocess.run(["swiftc", "-O", source, "-o", helper], capture_output=True, text=True)
        if build.returncode:
            print(f"{BOLD}system audio off:{RESET} build failed (needs macOS 14.2+). Using the mic only.\n{build.stderr[-500:]}")
            return None
    return subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # it exits when stdin closes


LIVE_LINES = 4
SPEAKER = {"me": "\033[36m", "them": "\033[33m"}


def live_lines(listener):
    """Captions: one block per speaker turn, settled words bright, words still in flux dim, oldest lines off the top."""
    width = min(shutil.get_terminal_size().columns, 100) - 2
    lines = []
    for label, settled, tentative in listener.caption_turns():
        prefix = f"{label:>4}  " if label else ""
        rows, row, used = [], [], 0
        for i, word in enumerate(settled + tentative):
            if row and used + 1 + len(word) > width - len(prefix):
                rows.append(row)
                row, used = [], 0
            used += len(word) + (1 if row else 0)
            row.append(word if i < len(settled) else f"{DIM}{word}{RESET}")
        rows.append(row)
        lines += [(SPEAKER[label] + prefix + RESET if i == 0 and label else " " * len(prefix)) + " ".join(row)
                  for i, row in enumerate(rows) if row]
    return [""] * (LIVE_LINES - len(lines[-LIVE_LINES:])) + lines[-LIVE_LINES:]


def show_live(listener, idle, lock):
    """Keep the lines above the prompt showing the last words heard, while waiting for input."""
    shown = None
    while True:
        idle.wait()
        lines = live_lines(listener)
        with lock:
            if idle.is_set() and lines != shown:
                # save cursor, go up to the live lines, rewrite them, back to where the user is typing
                out = "".join(f"\r\033[2K{line}\033[1B" for line in lines)
                sys.stdout.write(f"\0337\033[{LIVE_LINES}A{out}\0338")
                sys.stdout.flush()
                shown = lines
        threading.Event().wait(0.5)


def emit_captions(listener):
    shown = None
    while True:
        turns = [{"speaker": label, "settled": " ".join(settled), "tentative": " ".join(tentative)}
                 for label, settled, tentative in listener.caption_turns()]
        if turns != shown:
            emit("captions", turns=turns)
            shown = turns
        threading.Event().wait(0.5)


def conversation_prompt(listener, sent_upto, seconds, focus):
    """Split the transcript into what's new since the last question and the latest `seconds`."""
    now = listener.now()
    recent = listener.text(max(sent_upto, now - CONTEXT_SECONDS))
    earlier = [seg for seg in recent if seg[0] <= now - seconds]
    latest = [seg for seg in recent if seg[0] > now - seconds]
    if not latest and earlier:  # a chunk can end before the window, e.g. on the pause right after a question
        latest = [earlier.pop()]
    if not latest:
        return None, None
    prompt = f"Conversation since my last question (context):\n{as_text(earlier)}\n\n" if earlier else ""
    prompt += f"Latest part (most likely what I need now):\n{as_text(latest)}"
    if focus:
        prompt += f"\n\nFocus: {focus}"
    return prompt, as_text(latest)


def main():
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(__doc__.strip())
        return
    about = ""
    if "--about" in sys.argv:
        i = sys.argv.index("--about")
        if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("-"):
            sys.exit('--about needs a description, e.g. --about "sprint planning with Ana"')
        about = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    record = "--no-record" not in sys.argv  # --record is the default, still accepted
    mic_only = "--mic-only" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--record", "--no-record", "--mic-only", "--json")]
    if unknown := [a for a in args if a.startswith("-")]:
        sys.exit(f"unknown option: {' '.join(unknown)} (see companion --help)")
    if JSON:
        sys.stdout = StatusLines()
    scope_text, dirs = scope_context([resolve(a) for a in args] or [Path.cwd()])
    if about:
        scope_text += f"\n\nWhat this meeting is about, in my words: {about}"
    summary = [line for line in scope_text.splitlines() if line.startswith(("- Repos", "- claude-mem"))]
    print(DIM + "\n".join(summary) + RESET)

    jira_keys = jira_key_pattern()
    if jira_keys:
        scope_text += jira_index()
    attached = set()  # tickets already in the Claude session; follow-ups resume it

    scope_name = "-".join(resolve(a).name for a in args) or Path.cwd().name
    started = datetime.now()
    notes = NOTES_HOME / "meetings" / f"{started:%Y-%m-%d-%H%M}-{re.sub(r'[^A-Za-z0-9_-]+', '-', scope_name)}.md"
    title = f"Meeting {started:%Y-%m-%d %H:%M} · {scope_name}"
    transcript = notes.with_name(notes.stem + "-transcript.md") if record else None
    if transcript:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        print(f"{DIM}recording transcript to {transcript}{RESET}")

    if not _whisper_cached:
        download_whisper()
    print(f"{DIM}loading whisper…{RESET}")
    listener = Listener(transcript, started)
    listener.about = about
    listener.ready.wait()
    system_audio = None if mic_only else start_system_audio()
    if system_audio:
        listener.add_system_audio(system_audio.stdout)
    print(f"{DIM}hearing: mic{' + system audio' if system_audio else ''}{RESET}")

    session_id = str(uuid.uuid4())
    first = True
    sent_upto = 0.0  # audio second up to which the conversation has been sent to Claude
    listener.live = JSON or sys.stdout.isatty()
    idle, lock = threading.Event(), threading.Lock()
    if JSON:
        threading.Thread(target=emit_captions, args=(listener,), daemon=True).start()
    elif listener.live:
        threading.Thread(target=show_live, args=(listener, idle, lock), daemon=True).start()
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=listener.on_audio):
        print(f"{BOLD}listening.{RESET} Enter = answer · '60' = last 60s · 'text' = focus · '? question' = typed · '!' = + screenshot · q = quit")
        while True:
            if JSON:  # every time it waits for a command, so the app knows even when a command ends early
                emit("ready")
            if listener.live and not JSON:
                print("\n".join(live_lines(listener)))
                idle.set()
            try:
                cmd = input("" if JSON else "> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                with lock:
                    idle.clear()
            if cmd == "q":
                break

            shot = None
            if cmd.startswith("!"):
                cmd = cmd[1:].strip()
                shot = notes.with_name(f"{notes.stem}-shot-{datetime.now():%H%M%S}.png")
                shot.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["screencapture", "-i", "-x", str(shot)])
                if not shot.exists():  # Esc during the selection
                    print("screenshot cancelled.")
                    continue

            if cmd.startswith("?"):
                question = f"asked: {cmd[1:].strip()}"
                prompt = f"Typed question: {cmd[1:].strip()}"
            else:
                print(f"{DIM}transcribing the last few seconds…{RESET}", end="", flush=True)
                listener.flush()
                print("\r\033[2K", end="")
                if listener.received == 0:
                    print("no audio captured. Check the mic permission for this terminal app in System Settings › Privacy › Microphone.")
                    continue
                if system_audio and not listener.system_heard:
                    print(f"{DIM}system audio has been silent so far. If a call is playing, allow this terminal app in "
                          f"System Settings › Privacy & Security › Screen & System Audio Recording.{RESET}")
                seconds = int(cmd) if cmd.isdigit() else WINDOW
                focus = "" if cmd.isdigit() else cmd
                prompt, latest = conversation_prompt(listener, sent_upto, seconds, focus)
                if prompt:
                    sent_upto = listener.now()
                    print(f"{DIM}heard: {latest}{RESET}\n")
                    question = f"heard: {' '.join(latest.split())}" + (f" · focus: {focus}" if focus else "")
                elif shot:  # the screenshot alone is the question
                    prompt, question = (f"Focus: {focus}" if focus else ""), "screenshot" + (f" · focus: {focus}" if focus else "")
                else:
                    print("nothing heard yet." if first else "nothing new heard since the last question.")
                    continue

            if shot:
                prompt += f"\n\nScreenshot of my screen, read it first: {shot}"
                question += f" · [screenshot]({shot.name})"

            if jira_keys:
                keys = {f"{m[1].upper()}-{m[2]}" for m in jira_keys.finditer(prompt)} - attached
                if keys:
                    print(f"{DIM}[jira {', '.join(sorted(keys))}]{RESET}", flush=True)
                tickets = [t for key in sorted(keys) if (t := jira_ticket(key))]
                attached |= keys
                if tickets:
                    prompt += "\n\nJira tickets mentioned:\n\n" + "\n\n".join(tickets)

            print(f"{DIM}  asking claude…{RESET}", flush=True)
            answer = ask(prompt, session_id, first, scope_text, dirs)
            first = False
            log_exchange(notes, title, question, answer)

    listener.flush()
    unsent = as_text(listener.text(sent_upto))
    if notes.exists() or (record and listener.segments):
        prompt = (f"Conversation since my last question:\n{unsent}\n\n" if unsent else "") + SUMMARY_PROMPT
        summarize(notes, title, prompt, session_id, first, scope_text, dirs)
        print(f"\n{DIM}notes: {notes}{RESET}")


if __name__ == "__main__":
    sys.exit(main())
