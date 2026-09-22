---
name: worklog
description: Use when the user types /worklog, asks what they worked on today, yesterday or this week, wants a timesheet or an hour split per ticket, or asks how long they spent on a ticket. Reconstructs the answer from Claude Code session transcripts and the current repo's git history. Reports only; never posts anywhere.
---

# worklog

A script does every time calculation and prints JSON. You render that JSON as
the four blocks below and write the summary phrases. Never compute or adjust a
number yourself. Never post the result anywhere; the chat output is the
deliverable.

## Run

```bash
python3 ~/.claude/skills/worklog/scripts/worklog.py [WINDOW] [--hours N]
```

Run it from the repo root. `WINDOW`: omit for today, `YYYY-MM-DD` for one day,
`YYYY-MM-DD..YYYY-MM-DD` for a range. `--hours N` replaces the default budget of
8h per weekday (half day, weekend). Ticket labels use `AI_SKILLS_TICKET_PREFIX`.

- Window is today and `meta.totals.active_min` < 15 → rerun for yesterday and
  say so in one line.
- `subjects` is empty → say so, echo `meta.window` and `meta.repo_root`, stop.

## Render

Rows are `subjects`, already sorted by active time. Minutes print as `Xh Ym`, fraction
dropped (`85.0` → `1h 25m`, `0.2` → `0m`). Each row gets one **summary phrase**: from
`commits` when present (the outcome), else from `titles` and `prompt_samples`
(the intent). Merge two rows only when they are plainly one task, and say so.

### Time table

```
| Subject | Summary | Wallclock | Active estimate |
|---|---|---|---|
| **Ticketed** | | | |
| BPZ-1405 as_of-callers | Callers that omit as_of on temporal repos | 5h 57m | 4h 10m |
| *Subtotal* | | 5h 57m | 4h 10m |
| **Non-ticketed** | | | |
| fix temporal hard deletes | Temporal delete audit | 1h 8m | 1h 8m |
| Unlabeled sessions (2) | | 3m | 3m |
| *Subtotal* | | 1h 11m | 1h 11m |
| **Total** | | 7h 8m | 5h 21m |
```

Ticketed = rows with a `ticket`. All `untitled: true` rows collapse into the one
`Unlabeled sessions (N)` row, from `meta.subtotals.untitled`. Subtotals come
from `meta.subtotals`, the total from `meta.totals`. Under the table:
`At the keyboard: Xh Ym` from `meta.totals.union_active_min`; when it is below
the total, add "(the total sums parallel worktree sessions)".

### Four lists

Four headings, `## Picked up`, `## Worked on`, `## Merged`, `## Reviews`, always
all four, in this order. One bullet shape:
`- **BPZ-1234** slug: summary (Xh Ym)`; a non-ticket row puts its subject where
the ticket goes. Skip `untitled` and `0m` rows. Empty list → `- none`.

- Picked up: `started_in_window` true.
- Worked on: `started_in_window` false and `merged_commits` empty.
- Merged: `merged_commits` non-empty; name the merged branch. Local `commits`
  alone are not a merge.
- Reviews: rows of `meta.reviews`. Never in the other lists or totals.

### Suggested hours

One row per `meta.suggested_hours` entry, in the script's order:

```
| Time  | Task                                                       | Hours |
|-------|------------------------------------------------------------|-------|
| 09:00 | BPZ-1405 Callers that omit as_of on temporal repos         | 1.75  |
| 10:45 | BPZ-1304 djlint gates and README hook table                | 1     |
| 11:45 | Generic werkvoorbereiding "temporal delete audit, tooling" | 0.5   |
```

`Time` is `start`; `Task` is the ticket plus that row's summary phrase. The
entry with `ticket: null` renders as `Generic werkvoorbereiding "<comment>"`,
comment built from its `subjects`. Entries with `hours` 0 leave the table and
go beneath it as `under 15m: …`. `meta.workdays` > 1 → drop the `Time` column.
`meta.hours_budget` is 0 → say it is a weekend and point at `--hours`. Leave is
never derivable; do not invent it.

### Flags

- A row with `active_min > wallclock_min`: script bug, report it.
- Large `wallclock_min`, tiny `active_min`: session left open.
- Remaining `untitled` rows: ask the user to label them.
- `meta.unattributed` non-empty: list branch and span, say they were **not**
  counted.
