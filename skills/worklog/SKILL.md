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

Four blocks, in this order: The day, Hours, Time table, Flags.

Rows are `subjects`, already sorted by active time. Minutes print as `Xh Ym`, fraction
dropped (`85.0` → `1h 25m`, `0.2` → `0m`). Each row gets one **summary phrase**: from
`commits` when present (the outcome), else from `titles` and `prompt_samples`
(the intent). Merge two rows only when they are plainly one task, and say so.

Each row also gets a **status**: `merged into <target>` when `merged_commits` is
non-empty, the target read from `Merge branch 'X' into '<target>'`; `committed`
when only `commits` is; `research` otherwise. Local commits alone are not a
merge. Rows of `meta.reviews` have status `review`.

A session that dispatches subagents (run-epic, reviews) has its own time moved
onto the ticket each agent's description names, so an epic orchestrator splits
over its tickets. Time spent in an agent's worktree (`agent-<id>`) goes to that
agent's ticket. The agents' own time is never counted. What still sits on an
`epic/*` row is work for the whole stack: its `split_over` lists the epic's
tickets that have own time, and `suggested_hours` spreads the row over them.

### The day

Three to six bullets on what the day held, one per piece of work, grouped by
theme rather than by row. Each bullet is a single line that stands on its own.
Name tickets where they help; leave minutes to the tables. Under the list, two
bold lines, not bullets:

- **Most impact:** one row and why, in one line. Rank by status: merged into
  `main`, then merged into another branch, then `committed` or `review`, then
  `research`. Within a tier, ticket work (a `ticket` row, or an epic branch of
  tickets) beats tooling and chores; `active_min` breaks ties.
- **Most time:** the row with the highest `active_min`, with its time. Leave the
  line out when it is the Most impact row.

Untitled rows never qualify for either line.

### Hours

One row per `meta.suggested_hours` entry, in the script's order:

```
| Time  | Task                                                       | Hours |
|-------|------------------------------------------------------------|-------|
| 09:00 | BPZ-1405 Callers that omit as_of on temporal repos         | 1.75  |
| 10:45 | BPZ-1304 djlint gates and README hook table                | 1     |
| 11:45 | Generic werkvoorbereiding "temporal delete audit, tooling" | 0.5   |
```

`Time` is `start`; `Task` is the ticket plus that row's summary phrase. A ticket
entry whose `subjects` include an epic row carries its share of that row; the
phrase stays the ticket's own. The
entry with `ticket: null` renders as `Generic werkvoorbereiding "<comment>"`,
comment built from its `subjects`. Entries with `hours` 0 leave the table and
go beneath it as `under 15m: …`. `meta.workdays` > 1 → drop the `Time` column.
`meta.hours_budget` is 0 → say it is a weekend and point at `--hours`. Leave is
never derivable; do not invent it.

### Time table

```
| Subject | Summary | Status | Wallclock | Active estimate |
|---|---|---|---|---|
| **Ticketed** | | | | |
| BPZ-1405 as_of-callers | Callers that omit as_of on temporal repos | committed | 5h 57m | 4h 10m |
| *Subtotal* | | | 5h 57m | 4h 10m |
| **Non-ticketed** | | | | |
| fix temporal hard deletes | Temporal delete audit | merged into main | 1h 8m | 1h 8m |
| Unlabeled sessions (2) | | | 3m | 3m |
| *Subtotal* | | | 1h 11m | 1h 11m |
| **Total** | | | 7h 8m | 5h 21m |
| **Reviews (not in totals)** | | | | |
| BPZ-1412 order-pdf | MR checked against its ticket | review | 20m | 18m |
```

Ticketed = rows with a `ticket`. A row with a non-empty `split_over` stays under
Non-ticketed with its own minutes, its Summary ending in `(split over BPZ-1, BPZ-2)`.
All `untitled: true` rows collapse into the one
`Unlabeled sessions (N)` row, from `meta.subtotals.untitled`. Subtotals come
from `meta.subtotals`, the total from `meta.totals`. The Reviews group holds the
rows of `meta.reviews` and is left out when there are none. Under the table:
`At the keyboard: Xh Ym` from `meta.totals.union_active_min`; when it is below
the total, add "(the total sums parallel worktree sessions)".

### Flags

- A row with `active_min > wallclock_min`: script bug, report it.
- Large `wallclock_min`, tiny `active_min`: session left open.
- Remaining `untitled` rows: ask the user to label them.
- `meta.unattributed` non-empty: list branch and span, say they were **not**
  counted.
