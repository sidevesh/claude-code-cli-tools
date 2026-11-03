#!/bin/sh
# claude-code-cli-tools installer.
#
#   curl -fsSL https://raw.githubusercontent.com/sidevesh/claude-code-cli-tools/main/install.sh | sh
#
# Installs four payload files plus two Herdr entry-point symlinks. Everything is
# staged in a temp dir that is removed on exit:
#
#   <prefix>/bin/claude-remote-launch
#   <prefix>/bin/claude-profile-move
#   <prefix>/bin/claude-herdr-resume-hook
#   <prefix>/bin/herdr-launch -> <prefix>/bin/claude-remote-launch
#   <prefix>/lib/claude-code-cli-tools/claude_profile_lib.py
#   <prefix>/lib/claude-code-cli-tools/herdr-bin/claude -> <prefix>/bin/claude-remote-launch
#
# Environment:
#   PREFIX                        install root (default ~/.local)
#   BINDIR / LIBDIR               override either directory individually
#   CLAUDE_CLI_TOOLS_ASSUME_YES=1 don't prompt before upgrading an existing install
#   CLAUDE_CLI_TOOLS_REF          install a specific tag/branch instead of latest
#
# When piping, put variables on the `sh` side - on the left they would apply to
# curl, not to this script:
#
#   curl -fsSL .../install.sh | PREFIX=/opt/tools sh
set -eu

REPO="sidevesh/claude-code-cli-tools"
PREFIX="${PREFIX:-$HOME/.local}"
BINDIR="${BINDIR:-$PREFIX/bin}"
LIBDIR="${LIBDIR:-$PREFIX/lib/claude-code-cli-tools}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required but was not found on PATH"

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -qO- "$1"; }
else
    fail "need curl or wget"
fi

# Resolve the newest release tag. Version sorting is done in python3 because
# `sort -V` is GNU-only and this has to work on macOS too.
if [ -n "${CLAUDE_CLI_TOOLS_REF:-}" ]; then
    REF="$CLAUDE_CLI_TOOLS_REF"
else
    REF=$(fetch "https://api.github.com/repos/$REPO/tags?per_page=100" 2>/dev/null \
        | python3 -c '
import json, re, sys
try:
    tags = json.load(sys.stdin)
except Exception:
    print("main"); raise SystemExit
best = None
for t in tags:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", t.get("name", ""))
    if m:
        v = tuple(int(g) for g in m.groups())
        if best is None or v > best[0]:
            best = (v, t["name"])
print(best[1] if best else "main")
' 2>/dev/null) || REF="main"
    [ -n "$REF" ] || REF="main"
fi

say "claude-code-cli-tools"
say "  source : $REPO @ $REF"
say "  bin    : $BINDIR"
say "  lib    : $LIBDIR"

# Detect an existing install and confirm before replacing it.
EXISTING=""
if [ -f "$BINDIR/claude-remote-launch" ] || [ -f "$LIBDIR/claude_profile_lib.py" ]; then
    EXISTING=$("$BINDIR/claude-remote-launch" --version 2>/dev/null || echo "unknown version")
    say ""
    say "Existing install found: $EXISTING"
    if [ -n "${CLAUDE_CLI_TOOLS_ASSUME_YES:-}" ]; then
        say "CLAUDE_CLI_TOOLS_ASSUME_YES set - upgrading without prompting."
    # stdin is the curl pipe, so the answer has to come from the terminal.
    # Test by opening it: /dev/tty can exist yet be unopenable (cron, CI,
    # containers), so [ -e /dev/tty ] is not a sufficient check.
    elif exec 3</dev/tty 2>/dev/null; then
        printf 'Upgrade it? [y/N] '
        read -r reply <&3 || reply=""
        exec 3<&-
        case "$reply" in
            y|Y|yes|YES) ;;
            *) say "aborted."; exit 1 ;;
        esac
    else
        fail "already installed, and no terminal is available to confirm on.
       Set CLAUDE_CLI_TOOLS_ASSUME_YES=1 to upgrade non-interactively:
         curl -fsSL .../install.sh | CLAUDE_CLI_TOOLS_ASSUME_YES=1 sh"
    fi
fi

# Stage in a temp dir so a failed download never leaves a half-written install,
# and nothing is ever written to the current directory.
TMP=$(mktemp -d "${TMPDIR:-/tmp}/cctools.XXXXXX") || fail "could not create temp dir"
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

RAW="https://raw.githubusercontent.com/$REPO/$REF"
say ""
for item in "bin/claude-remote-launch" "bin/claude-profile-move" "bin/claude-herdr-resume-hook" "lib/claude_profile_lib.py"; do
    name=${item##*/}
    say "  downloading $name"
    fetch "$RAW/$item" > "$TMP/$name" || fail "download failed: $item"
    # A 404 page would be small and would not start with a shebang.
    [ -s "$TMP/$name" ] || fail "empty download: $item"
    if [ "${item#bin/}" != "$item" ]; then
        head -n1 "$TMP/$name" | grep -q '^#!' || fail "$name does not look like a script"
    else
        grep -q '__version__' "$TMP/$name" || fail "$name does not look like the shared module"
    fi
done

mkdir -p "$BINDIR" "$LIBDIR"
install_file() {  # src dst mode   (install(1) differs across platforms; do it by hand)
    cp "$1" "$2.tmp.$$" && chmod "$3" "$2.tmp.$$" && mv "$2.tmp.$$" "$2"
}
install_file "$TMP/claude-remote-launch"  "$BINDIR/claude-remote-launch"  755
install_file "$TMP/claude-profile-move"   "$BINDIR/claude-profile-move"   755
install_file "$TMP/claude_profile_lib.py" "$LIBDIR/claude_profile_lib.py" 644

# Herdr's native restore command is `claude --resume <id>`. This private PATH
# entry redirects only Herdr sessions launched through herdr-launch back
# through claude-remote-launch, inferring the original profile from the resume
# id and preserving naming, trust and Remote Control without replacing the
# user's normal `claude` command.
mkdir -p "$LIBDIR/herdr-bin"
ln -sfn "$BINDIR/claude-remote-launch" "$BINDIR/herdr-launch"
ln -sfn "$BINDIR/claude-remote-launch" "$LIBDIR/herdr-bin/claude"

# Register a separate companion hook in every discovered Claude profile. It
# records a custom Herdr resume command containing the absolute router path, so
# restores remain profile-aware even when an SSH peer starts Herdr after reboot
# before herdr-launch has supplied its private PATH.
"$BINDIR/claude-remote-launch" --install-herdr-hooks

say ""
say "Installed $("$BINDIR/claude-remote-launch" --version 2>/dev/null || echo "claude-code-cli-tools")"

case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)
        say ""
        say "warning: $BINDIR is not on your PATH. Add to your shell rc:"
        say "    export PATH=\"$BINDIR:\$PATH\""
        ;;
esac

say ""
say "Optional - per-profile aliases, add to ~/.bashrc or ~/.zshrc:"
say '    eval "$(claude-remote-launch --shell-init)"'
say ""
say "Try: claude-remote-launch --help"
say "     herdr-launch --help"
