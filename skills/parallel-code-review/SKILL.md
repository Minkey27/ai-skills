---
name: parallel-code-review
description: Use when you need a high-recall code-review finding pass over a git range — dispatches parallel dimension-specialist reviewers (correctness, conventions, tests, security, architecture/performance), dedupes their findings, and produces a structured findings list. Invoked by finalize-branch and mr-review in place of a single-reviewer pass; can also be run standalone for an ad-hoc deep review. Stays project-agnostic — the conventions reviewer auto-discovers in-repo rule files.
---

# parallel-code-review

A high-recall finder. Where a single reviewer surfaces the obvious few issues,
this fans out specialist reviewers — each with its full attention on one
lane — then dedupes. It does **finding only**: verification, curation, and posting
stay with the caller (`finalize-branch`, `mr-review`, or you, ad-hoc).

**Core principle:** recall is set by the finder; precision is set by whoever
verifies afterward. This skill maximizes recall and hands a clean list onward.

**Cost principle:** each subagent pays a fixed context overhead (system prompt +
injected project instructions) before doing any work — agent *count* is the cost
lever. Finder count therefore scales with diff size (see Tiering); lanes are
never dropped, only combined into fewer agents on small diffs.

**Announce at start:** "Using parallel-code-review to fan out specialist reviewers over the diff."

## Inputs

- `BASE_SHA`, `HEAD_SHA` — the git range. The **caller passes these in**; do not
  recompute them (finalize uses merge-base; mr-review also derives a separate
  GitLab position base — recomputing would fight both).
- **Standalone use:** if no caller supplied them, compute:
  ```bash
  git fetch origin main --quiet
  BASE_SHA=$(git merge-base origin/main HEAD)
  HEAD_SHA=$(git rev-parse HEAD)
  ```

## Output

A deduped findings list in this schema, produced **in your working context** (this
skill runs inline — it is not a returning subagent). The calling skill reads the
list for its next step.

```
{ id, severity, file, line_start, line_end, title, issue, recommendation, source }
```

`source` is the lane that produced the finding (e.g. `"security"`). After dedup a
merged finding's `source` may be an array of lanes (e.g. `["security","correctness"]`),
so treat it as `string | string[]`. It is informational metadata — callers may surface
it or ignore it; the existing `finalize-branch` / `mr-review` capture schemas omit it,
which is fine.

Severity scale: `critical` / `high` / `medium` / `low` / `nit`.

## Process

```dot
digraph pcr {
  rankdir=TB; node [shape=box];
  tier     [label="Measure diff → pick tier\n(git diff --stat)"];
  dispatch [label="Dispatch finders\n(single message, parallel)"];
  collect  [label="Barrier: collect all" shape=diamond];
  dedup    [label="Merge + dedup\n(plain reasoning)"];
  emit     [label="Emit unified findings list"];
  tier -> dispatch -> collect -> dedup -> emit;
}
```

### Step 0: Measure the diff and pick a tier

Run once, in your own context (not in a finder):

```bash
git diff --stat {BASE_SHA}..{HEAD_SHA}
```

From the output, note **total changed lines** (insertions + deletions) and the
**file list**. Pick the tier:

| Condition (check top-down, first match wins) | Finders dispatched |
|---|---|
| Diff touches only docs (`*.md`, `docs/`) | 1 finder: conventions |
| Diff touches only test files | 1 finder: correctness + conventions + tests combined |
| Total changed lines < 150 | 2 finders: (correctness + security), (conventions + tests + architecture/performance) |
| Total changed lines ≥ 150 | 5 finders: one per lane |

All five lanes are always *covered* — small tiers combine lanes into one agent's
brief rather than dropping them. A combined finder receives multiple lane briefs
and tags each finding's `source` with the specific lane it belongs to.

State the chosen tier and line count before dispatching (e.g. "312 changed lines
→ full 5-finder tier").

### Step 1: Dispatch the finders in ONE message

Read [references/specialist-briefs.md](references/specialist-briefs.md). Dispatch
the tier's `general-purpose` subagents **in a single message** (parallel tool
calls). Each gets the shared preamble (with `BASE_SHA`/`HEAD_SHA` and the file
list from Step 0 filled in) plus its lane brief(s).

**Model per finder:** if the harness supports a per-agent model override, dispatch
the *conventions* and *tests* finders on a cheaper tier (e.g. `sonnet`) — those
lanes are mechanical rule/coverage checking. Correctness, security, and
architecture finders inherit the session model. A combined finder containing
correctness or security always inherits the session model.

Do not go below the tier's finder count — the lane separation is what produces
the recall gain. Do not add chunking; each finder sees the whole diff once.

### Step 2: Collect (barrier)

Wait for all dispatched finders. If a finder **fails or returns nothing parseable**, do not abort
the review — record `"<lane> finder failed — coverage incomplete"` and continue
with the survivors. Note it in the summary so the caller knows recall was reduced.

### Step 3: Merge + dedup (plain reasoning — no subagent)

Combine all findings, then dedupe:

- **Match key:** same `file` + overlapping line range (within ±3 lines) + the same
  underlying root issue. (Cross-lane dupes are expected — e.g. security and
  correctness flagging one line.)
- **On collision:** keep one finding; take the **highest** severity; **union** the
  `source` values (e.g. `["security","correctness"]`); merge recommendations if
  they differ.
- **Re-id** sequentially: `F1`, `F2`, … after dedup.
- **Never silently drop:** report the counts — e.g. `"14 raw findings → 10 after dedup"`.

### Step 4: Emit

Produce the deduped list (schema above) plus the one-line dedup/coverage summary.
Stop here — verification, curation, and any posting belong to the caller.

## Edge cases

- **Zero findings** (all finders clean): emit `[]` and say so. Valid outcome.
- **No rule files in repo:** the conventions finder returns `[]`. No error.
- **A finder dies:** continue with survivors; flag incomplete coverage.
- **Invented line numbers:** not this skill's problem to catch — the caller's
  per-finding verification step catches them. Recall need not be precise here.

## Red flags

**Never:**
- Dispatch fewer finders than the tier prescribes, or drop a lane from coverage —
  small tiers combine lanes, they do not remove them.
- Skip Step 0 and default to 5 finders "to be safe" — measuring the diff costs one
  `--stat`; five agents on a 40-line diff costs ~100k tokens of fixed overhead.
- Recompute `BASE_SHA`/`HEAD_SHA` when a caller passed them.
- Use a custom agent type — `general-purpose` only.
- Verify, curate, fix, or post findings — that is the caller's job.
- Silently drop a finding during dedup — report the counts.

**Always:**
- Dispatch all finders in a single message (true parallelism).
- State tier + changed-line count before dispatching.
- Auto-discover non-injected rule files for the conventions lane; degrade to `[]` if none.
- Union `source` and keep highest severity when merging dupes.

## Integration

**Invoked by:**
- `finalize-branch` Step 1 — in place of `superpowers:requesting-code-review`.
- `mr-review` Step 5 — in place of `superpowers:requesting-code-review`.

Both then run their existing per-finding verification fan-out and curation on the
list this skill produces.
