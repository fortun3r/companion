# companion

A push-to-talk meeting companion for developers who do most of their work with Claude Code.

When someone asks you about work you shipped with AI help and you don't remember the details, press Enter. The companion transcribes the last few seconds of the conversation locally, then asks Claude to answer it from your own git history, PRs and Claude Code transcripts. You get a one-line answer you can say out loud, a few supporting bullets, and the commits/PRs it came from.

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

## Usage

```bash
git clone https://github.com/isasiluispy/companion.git
cd your-project
/path/to/companion/companion.py
```

The first run downloads the whisper model (~1.6 GB), and macOS asks for microphone access for your terminal app.

| Input | Does |
|---|---|
| `Enter` | answer from the last 45s of audio |
| `20` + `Enter` | answer from the last 20s |
| `? why did we drop redis` | ask a typed question |
| `q` | quit |

Follow-up questions keep the context from earlier ones.

### Scope

By default it only looks at the current directory. Pass folders to narrow or widen that:

```bash
companion.py my-app                 # one repo, by name under ~/dev
companion.py ~/work                 # every repo under a folder (two levels deep)
companion.py api web                # several repos
```

A scope expands to:

- the repos it covers,
- their git worktrees,
- the Claude Code transcripts recorded in any of those folders,
- the matching claude-mem projects, if claude-mem is installed.

Claude can only open files inside the scope.

### Briefs

The newest markdown file in `~/.claude/briefs/` is included in every request. Write a short "what I did this week and why" there before a meeting (or have Claude Code write it). Answers get faster and better, because Claude needs fewer searches.

### Configuration

| Env var | Default | |
|---|---|---|
| `COMPANION_MODEL` | `sonnet` | any `claude --model` value; `opus` is slower but better |
| `COMPANION_WINDOW` | `45` | default seconds of audio per question |
| `COMPANION_ROOT` | `~/dev` | where scope names are looked up |
| `COMPANION_WHISPER` | `mlx-community/whisper-large-v3-turbo` | any mlx-whisper model |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | where Claude Code keeps transcripts |
| `CLAUDE_MEM_DB` | `~/.claude-mem/claude-mem.db` | claude-mem database |

## How it works

1. A rolling 2-minute buffer of mic audio is kept in memory. Nothing is written to disk.
2. On Enter, the chosen window is transcribed locally with whisper (about 3s on an M-series Mac).
3. The transcript goes to `claude -p` with a system prompt describing your scope. Claude gets read-only tools: `git log/show`, `gh pr view/list`, grep, and read-only sqlite. User settings, plugins, hooks and MCP servers are skipped so each answer starts fast.
4. The answer streams back into the terminal. A typical answer takes 10–25s.

## Privacy and etiquette

- Audio never leaves your machine. The **transcript text** is sent to Claude, the same as anything you type into Claude Code.
- It transcribes everyone in the room. Recording or transcribing people without their consent is illegal in some places, and your employer may have its own rules. Check before using it.
- It doesn't tell speakers apart. Claude infers which question was aimed at you, and sometimes gets it wrong.
- Answers come from your history, but they can still be wrong. Check the refs before repeating something important.

## License

MIT
