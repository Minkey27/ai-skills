#!/bin/sh
# Claude Code status line script
input=$(cat)

# Read-only git: `status` and `diff` would otherwise take index.lock to refresh
# the stat cache, which races any git write running in the same worktree.
export GIT_OPTIONAL_LOCKS=0

# --- Directory ---
dir=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // ""')
project=$(basename "$dir")

# --- Git branch ---
branch=$(git -C "$dir" branch --show-current 2>/dev/null)
branch_part=""
if [ -n "$branch" ]; then
  branch_part="🌿 ${branch}"
fi

# --- Model ---
model=$(echo "$input" | jq -r '.model.display_name // ""')

# --- Context remaining (visual bar + %) ---
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')
ctx_part=""
if [ -n "$remaining" ]; then
  rem_int=$(printf '%.0f' "$remaining")
  filled=$(( rem_int / 10 ))
  empty=$(( 10 - filled ))
  bar=$(printf '%0.s#' $(seq 1 $filled 2>/dev/null))$(printf '%0.s-' $(seq 1 $empty 2>/dev/null))
  # Color: green >50%, yellow 25-50%, red <25%
  if [ "$rem_int" -gt 50 ]; then
    color="\033[32m"
  elif [ "$rem_int" -gt 25 ]; then
    color="\033[33m"
  else
    color="\033[31m"
  fi
  reset="\033[0m"
  ctx_part="🧠 ${color}[${bar}] ${rem_int}%${reset}"
fi

# --- 5-hour rate limit (bar shows usage filling up) ---
five_h=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
rate_part=""
if [ -n "$five_h" ]; then
  five_int=$(printf '%.0f' "$five_h")
  filled=$(( five_int / 10 ))
  empty=$(( 10 - filled ))
  bar=$(printf '%0.s=' $(seq 1 $filled 2>/dev/null))$(printf '%0.s-' $(seq 1 $empty 2>/dev/null))
  # Color: green <50%, yellow 50-80%, red >80%
  if [ "$five_int" -le 50 ]; then
    color="\033[32m"
  elif [ "$five_int" -le 80 ]; then
    color="\033[33m"
  else
    color="\033[31m"
  fi
  reset="\033[0m"
  rate_part="⏳ ${color}5h (${bar}) ${five_int}%${reset}"
fi

# --- Cache hit rate ---
cache_read=$(echo "$input" | jq -r '.context_window.current_usage.cache_read_input_tokens // empty')
cache_create=$(echo "$input" | jq -r '.context_window.current_usage.cache_creation_input_tokens // empty')
cache_part=""
if [ -n "$cache_read" ] && [ -n "$cache_create" ]; then
  total_cache=$(( cache_read + cache_create ))
  if [ "$total_cache" -gt 0 ]; then
    cache_pct=$(( cache_read * 100 / total_cache ))
    cache_part="💾 cache:${cache_pct}%"
  fi
fi

# --- Context tokens in use (same number the Claude Code UI shows) ---
# input + cache_creation + cache_read of the latest assistant message.
tokens_part=""
used_tok=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
if [ -n "$used_tok" ] && [ "$used_tok" -gt 0 ] 2>/dev/null; then
  tokens_part="🔢 ${used_tok}"
fi

# --- Git dirty count ---
dirty_part=""
if [ -n "$dir" ]; then
  dirty_count=$(git -C "$dir" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  if [ "$dirty_count" -gt 0 ]; then
    dirty_part="✏️ ${dirty_count} dirty"
  fi
fi

# --- Git stash count ---
stash_part=""
if [ -n "$dir" ]; then
  stash_count=$(git -C "$dir" stash list 2>/dev/null | wc -l | tr -d ' ')
  if [ "$stash_count" -gt 0 ]; then
    stash_part="📦 stash:${stash_count}"
  fi
fi

# --- ClickUp ticket (BPZ-### parsed from branch name) ---
ticket_part=""
if [ -n "$branch" ]; then
  ticket=$(printf '%s' "$branch" | grep -oE '[A-Z]{2,}-[0-9]+' | head -1)
  if [ -n "$ticket" ]; then
    ticket_url="https://app.clickup.com/t/9012470558/${ticket}"
    # OSC 8 hyperlink: clickable label, URL hidden
    ticket_part="\033]8;;${ticket_url}\033\\\\🎫 ${ticket}\033]8;;\033\\\\"
  fi
fi

# --- Backend URL (from backend/.env BACKEND_PORT) ---
backend_part=""
env_file="${dir}/backend/.env"
if [ -f "$env_file" ]; then
  backend_port=$(grep "^BACKEND_PORT=" "$env_file" | cut -d= -f2 | tail -1)
  if [ -n "$backend_port" ]; then
    backend_part="🌐 http://localhost:${backend_port}"
  fi
fi

# --- Branch diff vs the parent branch ---
# The parent is whichever of origin/main, origin/master and origin/epic/* leaves
# HEAD with the fewest commits of its own. Commits are matched by patch, not by
# SHA, so a branch cut from an epic that was rebased since still counts only its
# own work. An epic's own remote is excluded, so an epic compares against main.
diff_part=""
if [ -n "$dir" ] && [ -n "$branch" ]; then
  best=""
  epics=$(git -C "$dir" for-each-ref --format='%(refname:short)' \
    --exclude="refs/remotes/origin/${branch}" refs/remotes/origin/epic/ 2>/dev/null)
  for ref in origin/main origin/master $epics; do
    own=$(git -C "$dir" rev-list --cherry-pick --right-only --no-merges --topo-order \
      "${ref}...HEAD" 2>/dev/null) || continue
    count=$(printf '%s' "$own" | grep -c .)
    if [ -z "$best" ] || [ "$count" -lt "$best" ]; then
      best=$count
      parent_ref=$ref
      oldest=$(printf '%s\n' "$own" | tail -1)
    fi
  done
  # Diff from the newer of the merge-base and the commit before the oldest own
  # one: the first covers main merged into the branch, the second a rebased epic.
  if [ "${best:-0}" -gt 0 ]; then
    base=$(git -C "$dir" merge-base "$parent_ref" HEAD)
    fork=$(git -C "$dir" rev-parse "${oldest}^")
    git -C "$dir" merge-base --is-ancestor "$base" "$fork" && base=$fork
    set -- $(git -C "$dir" diff --numstat "$base" 2>/dev/null | awk '{a+=$1; d+=$2} END {print a+0, d+0}')
    added=$1
    deleted=$2
    if [ "$added" -gt 0 ] || [ "$deleted" -gt 0 ]; then
      diff_part="\033[32m+${added}\033[0m/\033[31m-${deleted}\033[0m"
    fi
  fi
fi

# --- Running slash command / skill (written by hooks/active-skill.sh) ---
skill_part=""
sid=$(echo "$input" | jq -r '.session_id // empty')
skill_file="$HOME/.claude/state/active-skill/${sid}"
if [ -n "$sid" ] && [ -f "$skill_file" ]; then
  top=$(sed -n 1p "$skill_file")
  sub=$(sed -n 2p "$skill_file")
  label="${top:+/${top}}"
  [ -n "$sub" ] && label="${label:+${label} › }${sub}"
  [ -n "$label" ] && skill_part="\033[1;36m🧭 ${label}\033[0m"
fi

# --- Assemble: three short rows, the terminal truncates instead of wrapping ---
join_parts() {
  out=""
  for part in "$@"; do
    [ -n "$part" ] && out="${out:+${out}  }${part}"
  done
  printf '%s' "$out"
}
row1=$(join_parts "$skill_part" "📂 ${project}" "$branch_part" "$ticket_part")
row2=$(join_parts "$backend_part" "$diff_part" "$dirty_part" "$stash_part")
row3=$(join_parts "🤖 ${model}" "$ctx_part" "$rate_part" "$tokens_part" "$cache_part")

status=""
for row in "$row1" "$row2" "$row3"; do
  [ -n "$row" ] && status="${status:+${status}\n}${row}"
done

printf '%b' "$status"
