#!/bin/bash
# Claude Code never auto-titles a session that opens with a slash command, so the
# terminal title stays "Claude Code". Title those from the command and git branch.
in=$(cat)
jq -e '(.session_title // "") == "" and (.prompt | startswith("/"))' <<<"$in" >/dev/null || exit 0
grep -q '"type":"ai-title"' "$(jq -r .transcript_path <<<"$in")" 2>/dev/null && exit 0
branch=$(git -C "$(jq -r .cwd <<<"$in")" branch --show-current 2>/dev/null)
jq -c --arg b "$branch" '
  (.prompt | split("\n")[0] | ltrimstr("/") | .[0:60]) + (if $b == "" then "" else " · " + $b end)
  | {hookSpecificOutput: {hookEventName: "UserPromptSubmit", sessionTitle: .}}' <<<"$in"
