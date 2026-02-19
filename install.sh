#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  Swarm Agent Stack — Remote one-liner installer
#  Usage:  curl -fsSL https://raw.githubusercontent.com/createpjf/swarm-dev/main/install.sh | bash
# ══════════════════════════════════════════════════════════════
set -euo pipefail

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

REPO="${SWARM_REPO:-https://github.com/createpjf/swarm-dev.git}"
INSTALL_DIR="${SWARM_INSTALL_DIR:-$HOME/swarm-dev}"

echo ""
echo "  ███████╗██╗    ██╗ █████╗ ██████╗ ███╗   ███╗"
echo "  ██╔════╝██║    ██║██╔══██╗██╔══██╗████╗ ████║"
echo "  ███████╗██║ █╗ ██║███████║██████╔╝██╔████╔██║"
echo "  ╚════██║██║███╗██║██╔══██║██╔══██╗██║╚██╔╝██║"
echo "  ███████║╚███╔███╔╝██║  ██║██║  ██║██║ ╚═╝ ██║"
echo "  ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝"
echo ""
echo -e "  ${BOLD}One-liner installer${RESET}"
echo ""

# ── 1. Check prerequisites ──
if ! command -v git &>/dev/null; then
    fail "git is required. Install: brew install git  (macOS) or apt install git  (Linux)"
fi

# ── 2. Find Python 3.10+ ──
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
    fail "Python 3.10+ required. Install: brew install python3  (macOS) or apt install python3  (Linux)"
fi
ok "Python: $($PYTHON --version)"

# ── 3. Check install directory ──
if [ -d "$INSTALL_DIR" ] && [ "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    warn "Directory already exists: $INSTALL_DIR"
    info "To update: cd $INSTALL_DIR && swarm update"
    info "To reinstall: rm -rf $INSTALL_DIR && re-run this script"
    exit 0
fi

# ── 4. Clone repository ──
info "Cloning from $REPO ..."
git clone --depth 1 "$REPO" "$INSTALL_DIR" 2>&1 | tail -2
ok "Cloned to $INSTALL_DIR"

# ── 5. Run setup ──
cd "$INSTALL_DIR"
if [ -f "setup.sh" ]; then
    info "Running setup..."
    exec bash setup.sh
else
    fail "setup.sh not found in $INSTALL_DIR"
fi
