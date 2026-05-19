#!/usr/bin/env bash
# Symlink every skill under skills/ into ~/.claude/skills/.
# Usage: ./install.sh [--force]
#   --force  Replace any existing ~/.claude/skills/<name> entry.

set -euo pipefail

force=0
if [ "${1:-}" = "--force" ]; then
  force=1
elif [ -n "${1:-}" ]; then
  echo "Usage: $0 [--force]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src_dir="$repo_root/skills"
dst_dir="$HOME/.claude/skills"

if [ ! -d "$src_dir" ]; then
  echo "No skills/ directory at $src_dir" >&2
  exit 1
fi

mkdir -p "$dst_dir"

installed=0
skipped=0
replaced=0

for skill_path in "$src_dir"/*/; do
  name="$(basename "$skill_path")"
  target="$dst_dir/$name"

  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$force" = "1" ]; then
      rm -rf "$target"
      ln -s "${skill_path%/}" "$target"
      echo "REPLACED $name"
      replaced=$((replaced + 1))
    else
      echo "SKIP     $name (already exists; use --force to replace)"
      skipped=$((skipped + 1))
    fi
  else
    ln -s "${skill_path%/}" "$target"
    echo "INSTALL  $name"
    installed=$((installed + 1))
  fi
done

echo
echo "Done. installed=$installed replaced=$replaced skipped=$skipped"
