---
name: finalize-branch
description: Use when implementation is complete and you want to review, simplify, squash, and create an MR — the full post-implementation finalization workflow before sending work for review. Pass `yolo` as an argument to skip the simplify/squash confirmation gates while still letting the user curate which code-review findings to fix.
---

# Finalize Branch

## Overview

Workflow that chains four post-implementation steps: code review, simplify, squash, and MR creation.

**Core principle:** Review it, clean it, squash it, ship it.

**Announce at start:** "I'm using the finalize-branch skill to review, simplify, squash, and create an MR for this branch." If yolo mode is active, add: "Running in **yolo** mode — auto-applying simplify and squash without confirmation gates. Code-review findings will still be curated by you."

## Arguments

The skill accepts one optional argument that comes through as the literal text after `/finalize-branch`.

| Arg | Effect |
|---|---|
| _(none)_ | **Gated mode** (default). User confirms between Steps 1→2, 2→3, 3→4. |
| `yolo` (also accepts `--yolo`, `auto`, `-y`) | **Yolo mode.** Gates between Steps 2→3→4 are removed. Step 1 keeps the curation checklist (that's selection, not gating). |

Detect by matching the args case-insensitively against `^(yolo|--yolo|auto|-y)$`. If anything else is passed, ask the user what they meant rather than guessing.

**What yolo does NOT change:**
- Pre-flight checks still run and still stop the workflow on failure.
- Step 1's curation checklist (verification fan-out + pre-selected fixes) still runs — the user picks which findings to address.
- The `squash` sub-skill has its own internal confirmation; we don't override that, surface it to the user as-is.
- The squash skill's diff-verification (no changes lost) still runs.

## Config

This skill reads optional config via the `AI_SKILLS_*` env vars. Recommended setup is one line in `~/.zshenv`:

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

That makes the variables available to every shell Claude spawns. Commands below use `${VAR:-default}` syntax inline.

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_MR_TOOL` | `gh` | `gh` (GitHub) or `glab` (GitLab) |
| `AI_SKILLS_REVIEWERS` | _(empty)_ | Comma-separated reviewer handles. Empty → no `--reviewer` flag |
| `AI_SKILLS_TARGET_BRANCH` | `main` | Target branch for the MR/PR |
| `AI_SKILLS_TICKET_PREFIX` | _(empty)_ | Ticket prefix (e.g. `PROJ`). Empty → match any uppercase slug |

## Pre-flight Checks

Before starting, verify all three conditions. Stop if any fails.

```bash
# 1. Not on main
[ "$(git branch --show-current)" = "main" ] && echo "STOP: on main" && exit 1

# 2. Clean working tree
git status --porcelain | grep -q . && echo "STOP: uncommitted changes" && exit 1

# 3. Has commits ahead of main
git fetch origin main --quiet
MERGE_BASE=$(git merge-base origin/main HEAD)
[ "$(git rev-parse HEAD)" = "$MERGE_BASE" ] && echo "STOP: no commits ahead of main" && exit 1
```

## The Process

```dot
digraph finalize {
    rankdir=TB;
    node [shape=box];

    preflight [label="Pre-flight checks"];
    review [label="Step 1: requesting-code-review"];
    verify [label="Fan out sub-agents\nto verify each finding"];
    curate [label="Curation checklist\n(pre-selected by recommendation)" shape=diamond];
    fix [label="Fix selected findings\n+ commit"];
    gate1 [label="User confirms\n(skipped in yolo)" shape=diamond];
    simplify [label="Step 2: Simplify\n(auto-applies in yolo)"];
    gate2 [label="User confirms\n(skipped in yolo)" shape=diamond];
    squash [label="Step 3: Squash\n(squash skill has its own gates)"];
    gate3 [label="User confirms\n(skipped in yolo)" shape=diamond];
    mr [label="Step 4: Create MR"];
    done [label="Done" shape=doublecircle];

    preflight -> review -> verify -> curate -> fix -> gate1;
    gate1 -> simplify [label="proceed"];
    simplify -> gate2 -> squash [label="proceed"];
    squash -> gate3 -> mr [label="proceed"];
    mr -> done;
}
```

### Step 1: Code Review (with verification + curation)

This step is identical in both gated and yolo modes — the curation checklist *is* the gate.

**REQUIRED SUB-SKILL:** Use `superpowers:requesting-code-review` if available; otherwise dispatch a code-reviewer subagent directly.

Compute SHAs using the merge-base (never `origin/main` directly):

```bash
git fetch origin main --quiet
BASE_SHA=$(git merge-base origin/main HEAD)
HEAD_SHA=$(git rev-parse HEAD)
```

Dispatch the code-reviewer subagent with these SHAs.

**1a. Structure the findings.** Capture each finding as:

```
{
  "id": "F1",
  "severity": "high|medium|low|nit",
  "file": "path/to/file.py",
  "line_start": 42,
  "line_end": 42,
  "title": "short headline",
  "issue": "what the reviewer says is wrong",
  "recommendation": "what the reviewer suggests"
}
```

If line numbers aren't provided, leave them null and treat the finding as file-level. Do not invent line numbers — wrong anchors mislead the user later.

**1b. Fan out to verify each finding in parallel.** Single message, one sub-agent per finding (use the `general-purpose` Agent type). Each sub-agent gets:

```
Verify this code-review finding against the actual code on the current branch.

Finding:
  File: <file>
  Lines: <line_start>-<line_end>  (or "file-level")
  Issue: <issue text>
  Recommendation: <recommendation text>

Tasks:
  1. Read the cited file and surrounding context. Confirm whether the described
     issue is actually present at the cited location on the current branch. If
     the lines have shifted, find the equivalent location.
  2. Independently judge whether the recommendation, if applied, would actually
     resolve the issue without introducing a new problem.

Report:
  - issue_real: yes / no / partial — with one-sentence reason
  - fix_sound:  yes / no / risky   — with one-sentence reason
  - corrected_lines: <if the line numbers were wrong, give the right ones>
  - notes: anything else worth knowing

Be specific. Do not parrot the finding back — actually look at the code. Under 150 words.
```

Aggregate the results into a table keyed by finding id.

**1c. Present the curation checklist** via `AskUserQuestion` with `multiSelect: true`.

- **Pre-checked** (place first in the option list, mark "(Recommended)" in label): `issue_real == yes` AND severity ∈ {`high`, `medium`} AND `fix_sound != no`.
- **Unchecked but shown**: nits, partial-real issues, risky fixes — the user may still want to fix them.
- **Excluded entirely**: `issue_real == no` AND `fix_sound == no` — these are hallucinations. List them briefly in plain text above the checklist so the user knows they were dropped.

Option label format: `[F3 medium] services.py:120 — duplicate enum 'Afdeling'`. Use the `description` field for the one-line issue summary plus a verification badge like `✓ verified, fix sound` or `⚠ lines shifted to 125-128`.

If there are more than 4 options total, batch into successive `AskUserQuestion` calls (4 per question), grouped by severity so heavy hitters come first.

**1d. Fix the selected findings, commit.** Skip any the user did not select. Commit with a message like `fix: address code-review findings (F1, F3, F5)`.

**1e. Gate transition:**
- **Gated mode:** Ask "Proceed to Step 2 (simplify)?" before continuing.
- **Yolo mode:** Skip the confirmation — proceed directly to Step 2 in the same turn.

### Step 2: Simplify

**REQUIRED SUB-SKILL:** Use `simplify` (code-simplifier agent) if available.

The code-simplifier analyzes recently modified code on the branch and proposes clarity/consistency improvements.

**Gated mode** (default):
1. Present the proposed changes to the user
2. User approves or rejects individual changes
3. Commit approved changes
4. **GATE:** Ask the user to confirm before proceeding to Step 3

**Yolo mode:**
1. Apply all proposed changes the code-simplifier returns. Trust the sub-skill's judgment.
2. Print a short summary of what was applied (file + one-line description per change) so the user can see what happened.
3. Commit with `refactor: simplify per code-simplifier`.
4. Proceed directly to Step 3 in the same turn — no confirmation.

### Step 3: Squash

**REQUIRED SUB-SKILL:** Use `squash`

The squash skill has its own internal gates that this skill does NOT override (its grouping confirmation and diff-verification are safety, not approval). Yolo mode does not silence them — if the user wants them silenced, that's a change to the `squash` skill itself.

**Gated mode** (default): after squash completes, **GATE:** Ask the user to confirm before proceeding to Step 4.

**Yolo mode:** after squash completes (including its own internal confirmation), proceed directly to Step 4 in the same turn — no extra confirmation.

### Step 4: Create MR

Push the branch and create the MR.

```bash
git push -u origin "$(git branch --show-current)"
```

**Generate MR content — no approval gate. The user has opted into auto-accept, so thoroughness replaces the gate.**

Before drafting, read the full scope of the branch so the title/description reflect *all* the changes, not just the last commit subject:

```bash
BASE_SHA=$(git merge-base "origin/${AI_SKILLS_TARGET_BRANCH:-main}" HEAD)

# All commits on the branch with bodies
git log --reverse --format='%s%n%n%b' $BASE_SHA..HEAD

# File-level scope check — confirm the diff matches what the commits claim
git diff --stat $BASE_SHA..HEAD
```

**Extract ticket reference.** Check the branch name first, then commit subjects/bodies. Use the first match. Pattern: `${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+`.

```bash
PATTERN="${AI_SKILLS_TICKET_PREFIX:-[A-Z]+}-[0-9]+"
BRANCH=$(git branch --show-current)
TICKET=$(printf '%s\n' "$BRANCH" | grep -oE "$PATTERN" | head -1)
if [ -z "$TICKET" ]; then
  TICKET=$(git log --format='%s%n%b' $BASE_SHA..HEAD | grep -oE "$PATTERN" | head -1)
fi
echo "Ticket: ${TICKET:-<none>}"
```

If a ticket was found, include `Closes <TICKET>` as the first line of the description (above `## Summary`). If no ticket was found, omit the line entirely — do not invent one and do not leave a placeholder.

Draft:
- **Title** (under 70 chars): describe the most significant outcome of the branch, not just the first commit subject. If the branch does one thing, name that thing. If it does several, name the theme.
- **Description:**
  - `Closes <TICKET>` — only when a ticket reference was found.
  - `## Summary` — bullets explaining WHAT changed and WHY. Use as many bullets as needed; every meaningful change on the branch should be represented.
  - `## Test plan` — bullets covering how to verify the change.

Announce the chosen title and description in your response (so the user sees what was decided), then create the MR in the same turn without waiting.

**Required flags on every MR created by this skill:**
- Draft marker — via the tool's draft flag (the boolean), **not** by prefixing `Draft:`/`WIP:` to the title.
- Reviewers — from `$AI_SKILLS_REVIEWERS` if set; omit `--reviewer` entirely if empty.
- Assignee — `@me`. Both `gh` and `glab` resolve this to the authenticated user.

**Tool-specific invocation** — `gh` and `glab` differ on flag names. Pick the block matching `$AI_SKILLS_MR_TOOL` (default `gh`):

```bash
# Build the reviewer flag only when AI_SKILLS_REVIEWERS is non-empty
REVIEWER_FLAG=""
[ -n "${AI_SKILLS_REVIEWERS:-}" ] && REVIEWER_FLAG="--reviewer $AI_SKILLS_REVIEWERS"

if [ "${AI_SKILLS_MR_TOOL:-gh}" = "glab" ]; then
  glab mr create \
    --title "$TITLE" \
    --description "$DESCRIPTION" \
    --target-branch "${AI_SKILLS_TARGET_BRANCH:-main}" \
    --draft \
    --assignee @me \
    $REVIEWER_FLAG
else
  gh pr create \
    --title "$TITLE" \
    --body "$DESCRIPTION" \
    --base "${AI_SKILLS_TARGET_BRANCH:-main}" \
    --draft \
    --assignee @me \
    $REVIEWER_FLAG
fi
```

Where `$DESCRIPTION` is the heredoc-built body:

```bash
DESCRIPTION="$(cat <<EOF
${TICKET:+Closes $TICKET

}## Summary
<bullet points>

## Test plan
<verification steps>
EOF
)"
```

Return the MR/PR URL when done.

## Red Flags

**Never:**
- Skip the Step 1 curation checklist — even in yolo mode, the user selects which findings to fix. Yolo only skips gates *between* steps, not within Step 1.
- Skip a gate between Steps 1–3 **in gated mode** — every one needs user confirmation in default mode
- Treat anything other than the documented yolo aliases (`yolo`, `--yolo`, `auto`, `-y`) as yolo — ask the user instead of guessing
- Skip pre-selecting findings by recommendation — the curation checklist must come pre-checked based on severity + verification, not blank
- Skip the verification fan-out — pre-selected findings are only trustworthy if sub-agents have confirmed each one
- Apply unverified code-simplifier suggestions in yolo without printing a summary the user can scan
- Force-push without the squash skill's verification passing
- Prefix the MR/PR title with `Draft:` or `WIP:` — use the tool's draft flag instead
- Draft MR title/description from the last commit subject alone — read the full branch scope first
- Invent or hallucinate a ticket number — only include `Closes <TICKET>` if the reference actually appears in the branch name or commits
- Leave a literal `<TICKET>` placeholder in the description — strip the line entirely when no ticket is found

**Always:**
- Detect the yolo argument before starting — announce it explicitly so the user can interrupt if they didn't mean it
- Compute BASE_SHA as merge-base, never use the remote target branch directly
- Dispatch finding-verification sub-agents in parallel (single message, many tool calls)
- Pre-check curation options for `issue_real == yes` AND severity ∈ {high, medium} AND `fix_sound != no`; leave the rest unchecked
- Drop hallucinated findings (`issue_real == no` AND `fix_sound == no`) from the checklist and list them briefly above it
- Commit fixes from each step before proceeding to the next
- Use the tool from `$AI_SKILLS_MR_TOOL` (default `gh`) for MR/PR creation
- Run lint and format before any commits (project-specific; if your project has them, run them)
- Read `git log` and `git diff --stat` over `BASE_SHA..HEAD` before drafting MR title/description — thoroughness replaces the removed approval gate
- Pass `--draft` and `--assignee @me` on every invocation
- Only add `--reviewer` when `$AI_SKILLS_REVIEWERS` is non-empty
- Extract a ticket reference before drafting; if one exists, prepend `Closes <TICKET>` as the first line of the description

## Integration

**Pairs with:**
- **superpowers:executing-plans** — invoke this skill after plan execution completes
- **superpowers:requesting-code-review** — Step 1 sub-skill
- **simplify** (code-simplifier) — Step 2 sub-skill
- **squash** — Step 3 sub-skill
