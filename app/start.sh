# Sourced by the app inside the login shell, so a `companion` wrapper in ~/.zshrc still applies.
# Gets what's missing first, saying so in the panel, so the app works on a Mac with nothing installed.
status() { printf '{"type": "status", "text": "%s"}\n' "$1"; }

# The app's own companion and uv first; ~/.local/bin is where Claude Code's installer puts claude
export PATH="$COMPANION_APP_DIR:$HOME/.local/bin:$PATH"

if ! command -v claude >/dev/null; then
  status "Installing Claude, which Companion uses to answer…"
  curl -fsSL https://claude.ai/install.sh | bash >&2 ||
    { echo "Couldn't install Claude. Check the internet connection and press Start listening again." >&2; exit 1; }
fi

if ! claude auth status >/dev/null 2>&1; then
  status "Companion needs a Claude account (Pro or Max plan) to answer. Sign in in the browser window that just opened."
  claude auth login </dev/null >&2 ||
    { echo "Not signed in to Claude. Press Start listening to try again." >&2; exit 1; }
fi

status "Starting… the first time takes a few minutes."
companion "$@"
