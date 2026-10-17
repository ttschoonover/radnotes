#!/bin/sh
# Install or update RadNotes on macOS or Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/ttschoonover/radnotes/main/install.sh | sh
#
# Options (environment variables):
#   RADNOTES_DIR=...          where to install (default ~/.local/share/radnotes)
#   RADNOTES_SKIP_RADICLE=1   don't install Radicle
#   RADNOTES_SOURCE=...       install from this file or URL instead of the latest release
set -eu

REPO="ttschoonover/radnotes"
DIR="${RADNOTES_DIR:-$HOME/.local/share/radnotes}"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
fail() { printf 'radnotes install: %s\n' "$*" >&2; exit 1; }

# 1. Python 3.10 or newer --------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  case "$(uname -s)" in
    Darwin) fail "RadNotes needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ (or: brew install python), then run this again." ;;
    *)      fail "RadNotes needs Python 3.10 or newer. On Ubuntu or Debian: sudo apt install python3 python3-venv" ;;
  esac
fi
command -v git >/dev/null 2>&1 || fail "RadNotes needs git. On a Mac: xcode-select --install. On Ubuntu: sudo apt install git"
"$PY" -m venv --help >/dev/null 2>&1 || fail "Python's venv module is missing. On Ubuntu or Debian: sudo apt install python3-venv"

# 2. RadNotes in its own environment -------------------------------------------------
say "Installing RadNotes into $DIR"
mkdir -p "$DIR" "$BIN"
if ! "$DIR/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
  rm -rf "$DIR/venv"
  "$PY" -m venv "$DIR/venv" || fail "Couldn't create a Python environment in $DIR/venv"
fi
"$DIR/venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true

installed=""
if [ -n "${RADNOTES_SOURCE:-}" ]; then
  "$DIR/venv/bin/python" -m pip install -q --upgrade "$RADNOTES_SOURCE" && installed=1
else
  for src in "https://github.com/$REPO/releases/latest/download/radnotes.tar.gz" \
             "https://github.com/$REPO/archive/refs/heads/main.zip"; do
    if "$DIR/venv/bin/python" -m pip install -q --upgrade "$src" 2>/dev/null; then installed=1; break; fi
  done
fi
[ -n "$installed" ] || fail "Couldn't download RadNotes. Check your internet connection and try again."
ln -sf "$DIR/venv/bin/radnotes" "$BIN/radnotes"

# 3. Radicle (does the syncing) ---------------------------------------------------------
if [ -z "${RADNOTES_SKIP_RADICLE:-}" ] && ! command -v rad >/dev/null 2>&1 && [ ! -x "$HOME/.radicle/bin/rad" ]; then
  say "Installing Radicle, which RadNotes uses to sync"
  curl -sSf https://radicle.xyz/install | sh || say "Couldn't install Radicle. Notes still work on this device; install it later from https://radicle.xyz"
fi

# 4. Make the commands available in new terminals ------------------------------------------
case "${SHELL:-}" in
  */zsh) RC="$HOME/.zshrc" ;;
  */bash) if [ "$(uname -s)" = Darwin ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
  *) RC="$HOME/.profile" ;;
esac
if ! grep -qs 'radnotes-path' "$RC"; then
  printf '\nexport PATH="$HOME/.local/bin:$HOME/.radicle/bin:$PATH"  # radnotes-path\n' >> "$RC"
fi

VERSION="$("$DIR/venv/bin/python" -c 'import radnotes; print(radnotes.__version__)')"
say ""
say "RadNotes $VERSION is installed."
say ""
say "Open a new terminal window, then:"
if ! "$HOME/.radicle/bin/rad" self --did >/dev/null 2>&1 && ! rad self --did >/dev/null 2>&1; then
  say "  rad auth          create your Radicle identity (once per device)"
fi
say "  rad node start    start syncing (after each restart)"
say "  radnotes          open your notes"
say ""
say "Later, update with: radnotes update"
