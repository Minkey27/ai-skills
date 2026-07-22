---
name: worklog
description: Reconstruct what you worked on in a time window from Claude Code session transcripts and git history, and render a timesheet-style table (Subject | Summary | Wallclock | Active estimate) to help log hours. Use when the user says "/worklog", "what did I work on today", "build my timesheet", "how long did I spend on X", "worklog for last week", or wants a per-subject breakdown of time spent. Reports time only — it does not post anywhere.
---

# worklog

Turn Claude Code session timestamps + git history into a per-subject time table.

## When to use
Triggers: `/worklog`, "what did I work on (today|yesterday|this week)", "build my
timesheet", "how much time did I spend on <ticket>".

## How it works
A deterministic script does the time math; you turn its JSON into the table and
write the human summary. Never compute hours yourself — always run the script.

## Steps

1. **Resolve the window** from the user's request:
   - none / "today" → omit the argument
   - "yesterday" / a specific day → `YYYY-MM-DD`
   - "this week" / a range → `YYYY-MM-DD..YYYY-MM-DD` (compute the dates)

2. **Run the script from the current repo root** (it derives the repo, author,
   and known branches from git):
   ```bash
   python3 ~/.claude/skills/worklog/scripts/worklog.py [WINDOW]
   ```
   It reads `AI_SKILLS_TICKET_PREFIX` from the environment for ticket labeling.

3. **Render a markdown table** from the JSON `subjects` (already sorted by active
   time, descending). Columns: **Subject | Summary | Wallclock | Active estimate**.
   - Format minutes as `Xh Ym` (e.g. `85.0` → `1h 25m`; `0` → `0m`).
   - **Summary**: write one short phrase per subject from its `titles` +
     `prompt_samples` (intent) and `commits` (outcome). Prefer the concrete
     outcome when commits exist.
   - Add a **Totals** row from `meta.totals`.

3a. **Picked up / Finished, when the user wants a per-day narrative** (e.g. for a
   worklog doc entry, not just the timesheet table). Derive these directly from
   `subjects` — do not re-derive them from prompts or guess:
   - **Picked up today**: rows where `started_in_window` is `true` — the ticket's
     very first session (across all history, not just this window) falls inside
     the requested window. A ticket worked on again today after earlier days is
     *not* "picked up" — only its first-ever touch counts.
   - **Finished / merged today**: rows where `merged_commits` is non-empty — a
     `Merge branch '...'` commit landed in this window. A ticket can have local
     commits (`commits`) without being merged; only `merged_commits` means done.
   - Render each as a short bullet list: ticket + a few words from `subject`/
     `titles` (Picked up), or ticket + the merge commit's branch/subject
     (Finished/Merged). Omit either list if empty — don't print "none".

4. **Merge** rows that are obviously the same task — e.g. a `main`/title row that
   is plainly the same work as a ticket row worked later. State any merge you make.

5. **Flag anomalies** beneath the table:
   - any row where `active_min > wallclock_min` (a bug — report it, don't hide it)
   - a row with large `wallclock_min` but tiny `active_min` (session left open)
   - `main (untitled …)` rows — ask the user to label them
   - collapse or footnote `0m` single-event rows (often subagent sessions) so they
     don't clutter the table
   - if `meta.unattributed` is non-empty, list those branches/spans and tell the
     user they were **not** counted (unknown branch) so they can add them by hand.

6. If `subjects` is empty, say so and echo `meta.window` and `meta.repo_root`.

## Hard rules
- Do not invent or adjust the numbers; render exactly what the script returns.
- Do not post anywhere (no ClickUp). Output is the chat table only.
