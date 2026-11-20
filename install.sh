#!/usr/bin/env sh
# applybot installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/brunooffs/applybot/main/install.sh | sh
#
# It installs `uv` (a small, widely used Python tool installer) if you don't have it, installs
# applybot with it (uv fetches a suitable Python by itself), creates your private folder
# (~/.applybot) and runs `applybot doctor`. It does NOT install Ollama or download any model:
# doctor tells you what fits your computer and the exact command to run.
# Read it first if you like; it is short. Re-running it updates applybot and keeps your files.
set -eu

SRC="${APPLYBOT_SRC:-git+https://github.com/brunooffs/applybot}"

say() { printf '\n==> %s\n' "$1"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv is not installed. It is the tool that installs applybot and the right Python for it."
  printf 'Install uv now from https://astral.sh/uv ? [Y/n] '
  if [ -t 0 ]; then read -r ans; else ans=y; fi
  case "${ans:-y}" in
    n|N) echo "Stopped. Install uv (https://docs.astral.sh/uv/) and run this again."; exit 1 ;;
  esac
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # the uv installer puts uv in ~/.local/bin (or ~/.cargo/bin on older versions)
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || { echo "uv was installed but is not on PATH yet. Open a new terminal and run this again."; exit 1; }
fi

say "Installing applybot"
uv tool install --force --python ">=3.10" "$SRC"

BIN="$(uv tool dir --bin)"
PATH="$BIN:$PATH"
export PATH

say "Creating your applybot folder"
applybot init

say "Checking this computer"
applybot doctor || true

cat <<EOF

Done. If a new terminal says "applybot: command not found", run:  uv tool update-shell
Then open a new terminal and start with:  applybot serve --open
EOF
