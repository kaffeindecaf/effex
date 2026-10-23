#!/bin/sh
# effex installer: symlink effex.py onto PATH so `effex` works from anywhere.
# idempotent - run it again after moving the repo and it repoints the link.
set -e

here=$(cd "$(dirname "$0")" && pwd)
bindir="$HOME/.local/bin"

mkdir -p "$bindir"
ln -sf "$here/effex.py" "$bindir/effex"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$bindir"; then
    echo "note: $bindir is not on your PATH - add it, or run $bindir/effex"
fi
echo "effex -> $bindir/effex"
