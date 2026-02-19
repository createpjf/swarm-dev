#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Swarm Agent Stack — One-click local dev setup
#  Usage:  bash setup.sh
# ══════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV_DIR=".venv"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; exit 1; }
info() { echo -e "  ${DIM}$1${RESET}"; }
warn() { echo -e "  ${YELLOW}!${RESET} $1"; }

echo ""
echo "  ███████╗██╗    ██╗ █████╗ ██████╗ ███╗   ███╗"
echo "  ██╔════╝██║    ██║██╔══██╗██╔══██╗████╗ ████║"
echo "  ███████╗██║ █╗ ██║███████║██████╔╝██╔████╔██║"
echo "  ╚════██║██║███╗██║██╔══██║██╔══██╗██║╚██╔╝██║"
echo "  ███████║╚███╔███╔╝██║  ██║██║  ██║██║ ╚═╝ ██║"
echo "  ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝"
echo ""

# ── 1. Find Python 3 ──
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.10+ required. Install: brew install python3"
fi
ok "Python: $($PYTHON --version)"

# ── 2. Create virtual environment ──
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

# Activate
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 3. Install package (editable mode) ──
info "Installing swarm + dependencies..."
pip install --upgrade pip -q 2>/dev/null
pip install -e ".[dev]" -q 2>&1 | tail -3 || true
ok "Dependencies installed"

# ── 4. Create .env if missing ──
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        ok "Created .env from .env.example"
        info "Edit .env to add your API keys"
    fi
else
    ok ".env exists"
fi

# ── 5. Create directories ──
mkdir -p config .logs memory workflows

# ── 6. Install 'swarm' command globally ──
# After `pip install -e .`, swarm is at .venv/bin/swarm
# Symlink to /usr/local/bin so it works from anywhere
SWARM_BIN="$ROOT/$VENV_DIR/bin/swarm"
TARGET="/usr/local/bin/swarm"

if [ -f "$SWARM_BIN" ]; then
    if [ -L "$TARGET" ] && [ "$(readlink "$TARGET")" = "$SWARM_BIN" ]; then
        ok "CLI: swarm (already linked)"
    elif [ -e "$TARGET" ]; then
        warn "Cannot link: $TARGET already exists (different program)"
        info "Use: $SWARM_BIN  or  source .venv/bin/activate && swarm"
    else
        info "Linking swarm → /usr/local/bin/  (may ask for password)"
        if ln -sf "$SWARM_BIN" "$TARGET" 2>/dev/null; then
            ok "CLI: swarm  (linked to /usr/local/bin/)"
        elif sudo ln -sf "$SWARM_BIN" "$TARGET" 2>/dev/null; then
            ok "CLI: swarm  (linked to /usr/local/bin/)"
        else
            warn "Could not link to /usr/local/bin/"
            info "Use: source .venv/bin/activate && swarm"
        fi
    fi
else
    warn "swarm binary not found — try: pip install -e ."
fi

# ── 7. Health check ──
info "Running health check..."
if python3 -c "import yaml, httpx, filelock, rich, questionary" 2>/dev/null; then
    ok "All core packages importable"
else
    fail "Some packages failed to import — check pip install output above"
fi

echo ""
echo -e "  ${GREEN}${BOLD}Setup complete!${RESET}"
echo ""

# ── 8. Launch onboarding wizard ──
echo -e "  ${BOLD}Launching onboarding wizard...${RESET}"
echo ""
exec "$ROOT/$VENV_DIR/bin/swarm" onboard
