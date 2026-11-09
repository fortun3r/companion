# companion

Instant recall of your own engineering history, during meetings.

When you ship across many repos and tickets, the specifics blur: which PR, which commit, which tradeoff came up in review. When a question comes up, press Enter. The companion, which has been transcribing the conversation locally in the background, works out what was just asked and pulls the exact details from your code, git history, PRs, Claude Code sessions and Jira. You get the answer in one line, a few supporting facts, and the commits, PRs and files behind it, without breaking the conversation to go digging.

![demo](https://github.com/user-attachments/assets/3d442ee9-3863-40fe-bd56-90cbc724ec12)

<sub>Asking the companion about its own repo. The other person's voice is macOS `say` fed in as the mic. Everything after that is the real pipeline: local whisper, Claude reading the code, and the meeting summary on quit. Waits are trimmed.</sub>

## Requirements

- macOS on Apple Silicon (transcription uses [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)). macOS 14.2+ and Xcode Command Line Tools to hear calls on headphones; without them it uses the mic only.
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.claude.com/en/docs/claude-code), logged in (`claude` on your PATH). Answers use your existing Claude subscription or API key.
- Optional: [`gh`](https://cli.github.com/), logged in, for PR lookups
- Optional: [claude-mem](https://github.com/thedotmack/claude-mem), whose session summaries are the best source for *why* something was done
- Optional: a Jira API token, for tickets you haven't touched locally yet

## Usage

```bash
git clone https://github.com/fortun3r/companion.git
mkdir -p ~/.local/bin   # uv's installer already puts this on your PATH
ln -s "$PWD/companion/companion.py" ~/.local/bin/companion

cd your-project
companion .
```

The first run takes a few minutes: uv sets up Python and the dependencies (~150 MB), then the whisper model downloads (~1.6 GB). macOS will ask for microphone access for your terminal app. If you deny it, the companion says so on the first Enter. `companion --help` lists the keys and options.

| Input | Does |
|---|---|
| `Enter` | answer what you need right now |
| `the pagination part` + `Enter` | same, but focused on that |
| `60` + `Enter` | treat the last 60s, not 20s, as "the latest part" |
| `? why did we drop redis` | ask a typed question, no audio |
| `q` | quit, and write the meeting summary |

### What it answers

It transcribes the conversation in the background, cutting at pauses. When you press Enter, Claude gets two parts: the conversation since your last question, as context, and the latest ~20s, where the question most likely is. The earlier part resolves "that ticket" or "so why not the other approach?", and ticket keys mentioned minutes ago still get looked up.

| What just happened | What you get |
|---|---|
| Someone asked you something | The answer |
| Several questions in a row | The last one answered, plus a line for any still open |
| A follow-up to something from earlier | The answer, using the earlier context |
| No question, just discussion | What's being discussed, and the one fact worth adding |
| You were the one talking | The details you were reaching for |

Follow-up questions keep the context from earlier ones. Most audio is already transcribed when you press Enter, so the answer starts right away.

The transcript stays in memory. Pass `--record` to also save it next to the meeting notes, as `<date>-<time>-<scope>-transcript.md`. Then the summary covers the whole meeting, not just the parts around your questions.

### Calls and headphones

It hears the mic and everything the Mac plays, so the other person on a call is captured even on headphones, with no virtual audio driver and no output settings to change. The first run compiles a small helper (`system_audio.swift`, a Core Audio process tap), and startup shows `hearing: mic + system audio`.

macOS needs your terminal app allowed under System Settings › Privacy & Security › Screen & System Audio Recording. Until then, capture is silent, and the companion tells you on Enter.

On a call, the transcript is labeled: the mic is `[me]`, system audio is `[them]`. Claude knows when a question came from someone else and when you were the one talking. Other people on the call aren't told apart from each other. On speakers, the mic also hears the call, so mic lines that match what system audio heard at the same moment are dropped; headphones are still more reliable.

System audio means all of it, including videos and music. In a face-to-face meeting, use `companion . --mic-only`. Everyone is on the same mic there, so nothing is labeled.

### Scope

It looks at the folders you pass, and everything inside them. With no arguments it uses the current directory.

```bash
companion .                         # this folder and every repo nested inside it
companion my-app                    # one repo, by name under ~/dev
companion ~/work                    # every repo under a folder
companion api web                   # several repos
```

A scope expands to:

- every repo inside it, at any depth (hidden folders and `node_modules` are skipped),
- their git worktrees,
- the Claude Code transcripts recorded in any of those folders,
- the matching claude-mem projects, if claude-mem is installed.

Claude can only open files inside the scope.

### Briefs

The newest markdown file in `~/.claude/briefs/` is included in every request. Write a short "what I did this week and why" there before a meeting (or have Claude Code write it). Answers get faster and better, because Claude needs fewer searches.

### Meeting notes

Every question and answer is saved as it happens to `~/.claude/meetings/<date>-<time>-<scope>.md`, so a crash or Ctrl+C loses nothing. When you quit with `q`, Claude writes a summary at the top of the file:

```markdown
# Meeting 2026-11-03 18:21 · my-app

### Topics
- Whether the pixel tracks invites: only the landing page loads it (ABC-123)

### I committed to
- Check the invite page events tomorrow and get back to them

### Open questions
- Do we want a pixel event on invites at all?

---

## 18:22 · heard: does the pixel track invites?
…
```

"I committed to" lists only follow-ups you actually said out loud in the transcripts, not things the companion suggested you say. Press Ctrl+C while the summary is being written to skip it.

New Claude Code sessions don't know about these notes on their own. To make every session aware of them, add this to your global `~/.claude/CLAUDE.md`:

```markdown
# Meeting notes
Meeting notes from the companion live in `~/.claude/meetings/` (one file per meeting, named `<date>-<time>-<scope>.md`, with a summary of topics, commitments and open questions above the first `---`). Check them when I ask about a meeting, what I promised, or what someone asked about a ticket.
```

Then questions like "what did I promise in Monday's meeting?" work in any session. Claude only opens the notes when the question calls for them, so it costs nothing otherwise.

### Jira

Your local history can't answer questions about tickets you haven't started. Connect Jira to fill that gap.

**1. Create a token.** Go to [id.atlassian.com → API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) and choose **Create API token**, *not* "with scopes": scoped tokens don't work with basic auth and always return 401. Copy it with the dialog's Copy button. It's only shown once.

**2. Check the clipboard actually has it.** A token is about 190 characters:

```bash
pbpaste | wc -c
```

**3. Store it in the keychain from the clipboard.** Use this instead of the interactive `-w` prompt, which can silently save an empty value if the paste doesn't take. `-U` overwrites an existing item:

```bash
security add-generic-password -U -a "$USER" -s jira-api-token -w "$(pbpaste)"
security find-generic-password -s jira-api-token -w | wc -c   # ~190 again, not 1
```

**4. Check Jira accepts it.** This should print `200`:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u "you@company.com:$(security find-generic-password -s jira-api-token -w)" https://your-team.atlassian.net/rest/api/2/myself
```

A `401` means the email or token is wrong, and an empty token is the usual cause. The email must be exactly the one at [id.atlassian.com → Email](https://id.atlassian.com/manage-profile/email). If a correct classic token still gets 401, your org admin may have disabled API tokens.

**5. Add a wrapper to `~/.zshrc`.** It hands the token to `companion` only, instead of exporting it to every process:

```bash
companion() {
  JIRA_URL=https://your-team.atlassian.net \
  JIRA_EMAIL=you@company.com \
  JIRA_API_TOKEN=$(security find-generic-password -s jira-api-token -w) \
  command companion "$@"
}
```

Open a new terminal and run `companion .`. Startup should show `jira: signed in as <you>, N projects`. If it shows `jira disabled: …`, the message says what's wrong.

With Jira connected:

- **Spoken keys:** when a ticket key is said ("SA 3424" and "sa3424" both count), the ticket is fetched directly from Jira and attached to the question. That covers summary, status, assignee, description, links and the latest comments, and takes about 0.5s with no extra tool calls. Only keys of your real Jira projects are matched.
- **Described tickets:** at startup it indexes every open ticket in the current sprint, one line each. So "the invoice export thing" still resolves to the right ticket, even when nobody says the key and it isn't yours. Change what gets indexed with `COMPANION_JIRA_JQL`.
- **Not started yet:** if a ticket has no local work, the answer says so plainly, then covers what the ticket asks, the open questions, and a likely approach based on similar work in your repos.

### Configuration

| Env var | Default | |
|---|---|---|
| `COMPANION_MODEL` | `sonnet` | any `claude --model` value; `opus` is slower but better |
| `COMPANION_WINDOW` | `20` | seconds treated as "the latest part" |
| `COMPANION_CONTEXT` | `600` | max seconds of earlier conversation sent as context |
| `COMPANION_SPEECH_RATIO` | `3` | how much louder than the room's noise floor counts as speech; raise it if background noise turns into text |
| `COMPANION_ROOT` | `~/dev` | where scope names are looked up |
| `COMPANION_WHISPER` | `mlx-community/whisper-large-v3-turbo` | any mlx-whisper model |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where Claude Code keeps transcripts |
| `CLAUDE_MEM_DB` | `~/.claude-mem/claude-mem.db` | claude-mem database |
| `COMPANION_NOTES_HOME` | `~/.claude` | where `briefs/` and `meetings/` live |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | unset | enable Jira lookups |
| `COMPANION_JIRA_JQL` | `sprint in openSprints() AND statusCategory != Done ORDER BY updated DESC` | tickets indexed at startup (max 100) |

## How it works

1. Mic and system audio are transcribed locally with whisper in the background, in 8–30s chunks cut at pauses. Chunks without speech, judged against the room's noise floor, are skipped, since whisper otherwise invents text like "Thank you.". Audio is never written to disk.
2. On Enter, the untranscribed tail is transcribed (about 1s on an M-series Mac). The text is split into context (since your last question, max 10 min) and the latest ~20s.
3. The transcript goes to `claude -p` with a system prompt describing your scope. Claude gets read-only tools: file read/grep/glob over the repos in scope, `git log/show`, `gh pr view/list`, and read-only sqlite. User settings, plugins, hooks and MCP servers are skipped so each answer starts fast.
4. The answer streams back into the terminal. A typical answer takes 10–25s.

## Privacy and etiquette

- Audio never leaves your machine, whether from the mic or from system audio. **Transcript text** is sent to Claude on each Enter: the conversation since your last question, max 10 min, plus any Jira tickets fetched. When you quit, whatever was said since your last question is sent for the summary.
- The full transcript is only kept in memory unless you pass `--record`. Meeting notes, which include what other people said around your questions, are stored as plain markdown in `~/.claude/meetings/`. Treat them like any meeting notes you'd keep.
- It transcribes everyone in the room and everyone on the call. Recording or transcribing people without their consent is illegal in some places, and your employer may have its own rules. Check before using it.
- On a call it only knows `[me]` from `[them]`, not one person on the call from another. In person it doesn't tell anyone apart, so Claude infers who asked what, and sometimes gets it wrong.
- Answers come from your history, but they can still be wrong. Check the refs before repeating something important.

## License

MIT
