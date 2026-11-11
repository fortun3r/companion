# Sourced by the app inside the login shell, so a `companion` wrapper in ~/.zshrc still applies.
# Says in the panel when Claude Code is missing, and signs in to Claude when needed.
status() { printf '{"type": "status", "text": "%s"}\n' "$1"; }

# The app's own companion and uv first; ~/.local/bin is where Claude Code's installer puts claude
export PATH="$COMPANION_APP_DIR:$HOME/.local/bin:$PATH"

if ! command -v claude >/dev/null; then
  echo "Companion needs Claude Code to answer, and it isn't installed. Install it from claude.com/product/claude-code, then press Start listening again." >&2
  exit 1
fi

if ! claude auth status >/dev/null 2>&1; then
  status "Companion needs a Claude account (Pro or Max plan) to answer. Sign in in the browser window that just opened."
  claude auth login </dev/null >&2 ||
    { echo "Not signed in to Claude. Press Start listening to try again." >&2; exit 1; }
fi

status "Starting… the first time takes a few minutes."
companion "$@"
