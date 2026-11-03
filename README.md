# companion

Instant recall of your own engineering history, during meetings.

When you ship across many repos and tickets, the specifics blur: which PR, which commit, which tradeoff came up in review. When a question comes up, press Enter. The companion transcribes the last few seconds locally and pulls the exact details from your code, git history, PRs, Claude Code sessions and Jira. You get the answer in one line, a few supporting facts, and the commits, PRs and files behind it, without breaking the conversation to go digging.

```
> 
heard: What is the ticket ABC-123 about? Why did you go with the queue instead of a cron?

[Bash…]
[Bash…]
It moves invoice sync to a queue so retries don't block the nightly run.

- The cron version timed out on large accounts. The queue processes one account per job.
- Retries use exponential backoff, capped at 5 attempts.
- It's merged and has been in production since Oct 20.

[ABC-123, PR #412, 9f3c2e1]
```

## Requirements

- macOS on Apple Silicon (transcription uses [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper))
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.claude.com/en/docs/claude-code), logged in (`claude` on your PATH). Answers use your existing Claude subscription or API key.
- Optional: [`gh`](https://cli.github.com/), logged in, for PR lookups
- Optional: [claude-mem](https://github.com/thedotmack/claude-mem), whose session summaries are the best source for *why* something was done
- Optional: a Jira API token, for tickets you haven't touched locally yet

## Usage

```bash
git clone https://github.com/fortun3r/companion.git
ln -s "$PWD/companion/companion.py" ~/.local/bin/companion   # any directory on your PATH

cd your-project
companion .
```

The first run downloads the whisper model (~1.6 GB), and macOS asks for microphone access for your terminal app.

| Input | Does |
|---|---|
| `Enter` | answer from the last 20s of audio |
| `60` + `Enter` | answer from the last 60s |
| `? why did we drop redis` | ask a typed question |
| `q` | quit |

Follow-up questions keep the context from earlier ones.

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
| `COMPANION_WINDOW` | `20` | default seconds of audio per question |
| `COMPANION_ROOT` | `~/dev` | where scope names are looked up |
| `COMPANION_WHISPER` | `mlx-community/whisper-large-v3-turbo` | any mlx-whisper model |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where Claude Code keeps transcripts |
| `CLAUDE_MEM_DB` | `~/.claude-mem/claude-mem.db` | claude-mem database |
| `COMPANION_NOTES_HOME` | `~/.claude` | where `briefs/` and `meetings/` live |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | unset | enable Jira lookups |
| `COMPANION_JIRA_JQL` | `sprint in openSprints() AND statusCategory != Done ORDER BY updated DESC` | tickets indexed at startup (max 100) |

## How it works

1. A rolling 2-minute buffer of mic audio is kept in memory. Audio is never written to disk.
2. On Enter, the chosen window is transcribed locally with whisper (about 3s on an M-series Mac).
3. The transcript goes to `claude -p` with a system prompt describing your scope. Claude gets read-only tools: file read/grep/glob over the repos in scope, `git log/show`, `gh pr view/list`, and read-only sqlite. User settings, plugins, hooks and MCP servers are skipped so each answer starts fast.
4. The answer streams back into the terminal. A typical answer takes 10–25s.

## Privacy and etiquette

- Audio never leaves your machine. The **transcript text** is sent to Claude, the same as anything you type into Claude Code, along with any Jira tickets it fetched.
- Meeting notes, which include what other people said in the snippets you asked about, are stored as plain markdown in `~/.claude/meetings/`. Treat them like any meeting notes you'd keep.
- It transcribes everyone in the room. Recording or transcribing people without their consent is illegal in some places, and your employer may have its own rules. Check before using it.
- It doesn't tell speakers apart. Claude infers which question was aimed at you, and sometimes gets it wrong.
- Answers come from your history, but they can still be wrong. Check the refs before repeating something important.

## License

MIT
