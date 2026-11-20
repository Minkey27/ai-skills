---
name: renovate-mr-check
description: "Use when handed a Renovate merge request (an IID, a GitLab MR URL, \"is this bump safe to merge\", \"check the dependency update\", \"review the Renovate MR\") or an MR on a branch named renovate/*. Also use when a dependency upgrade needs a compatibility judgement before merging, or when asked to check the open Renovate MRs with none named (the skill then offers to find them itself). Judges each bump against the release notes and our usage, then approves the ones it clears. GitLab-only (requires `glab`). Supports `--headless --mr-json <file> --out <dir>` for unattended callers such as reviewstation: no `glab`, no questions, no sandbox, no approval; writes `review.md` + `findings.json`."
---

# Renovate MR Compatibility Check

## Overview

Renovate raises an MR per dependency version. Deciding whether one is safe to merge is
tedious by hand: read the MR, read the upstream changelog, work out whether the changed
behaviour is behaviour *we* rely on, and judge whether a green pipeline is real evidence
or an accident of thin coverage.

**Core principle: a version number is not evidence.** The verdict comes from the
intersection of what the release notes changed and what our code actually uses. A clean
major is safe; a patch that touches an API we call untested is not.

CI already runs the project's suites on every MR. The work it *cannot* do is the analysis
below. Do that first, and escalate to a sandbox run only when the analysis shows a real gap.

**Two modes.** Run from a terminal, the skill ends by approving what it cleared (Phase 5):
an approval often lands the merge, so the ✅ bar is the merge bar. With `--headless` it
writes its verdict for a caller and touches nothing: see [Headless mode](#headless-mode---headless),
which wins where it conflicts with a phase below.

## Non-Goals

The skill produces a report, and from a terminal approves what that report cleared. It does not:

- post comments on the MR (in headless mode the caller may post the findings)
- push anything, or edit the Renovate branch (including ticking the rebase checkbox)
- fix our code to accommodate a bump
- approve, or merge, anything the analysis did not mark ✅

## Project facts

Paths, CI job quirks, sandbox commands and manifest groups differ per project, so this skill
derives them at check time (see *How to find out*). Before Phase 1, read what the project
already wrote down about them: its `CLAUDE.md`, and any project skill or note on Renovate
checks (`.claude/skills/*renovate*/SKILL.md`). Their facts beat the generic rules here. Their
steps that ask, sandbox, post or approve still follow this skill's mode.

## Phase 0 — Identify

0. **Resolve the input set.** If the invocation carried IIDs or MR URLs, use them and skip
   to step 1. If it carried none, **fetch the list first, then let the user pick from it**:
   never guess, and never analyse a whole sweep unasked.

   Fetch the open MRs on Renovate branches. When `AI_SKILLS_RENOVATE_USER` is set, also
   filter on that author (add `&author_username=$AI_SKILLS_RENOVATE_USER` to the URL):

   ```bash
   glab api "projects/:id/merge_requests?state=opened&order_by=updated_at&per_page=100" | \
     python3 -c "
   import json,sys
   for m in json.load(sys.stdin):
       if m['source_branch'].startswith('renovate/'):
           print(f\"!{m['iid']}\t{m['title']}\t[{m['source_branch']}]\t{m.get('detailed_merge_status')}\tdraft={m['draft']}\")"
   ```

   Nothing back means no open Renovate MRs: say so and stop. Otherwise **print a numbered
   table first**, so the user is choosing against something they can see:

   | Column | Source |
   |--------|--------|
   | `#` | Row number: the user can pick by number instead of IID |
   | IID | `iid` |
   | package → target | parsed from `title` (`update dependency (\S+) to v?(\S+)`) |
   | branch | `source_branch` |
   | flag | only when notable: `draft`, or `detailed_merge_status` of `conflict` |

   All of that is on the list response: **no per-MR calls to build the menu**. The
   **from**-version and the **ecosystem** need the diff (step 4), so the menu shows the
   target version only, and groups by package name, never by ecosystem.

   Flag the conflicted and draft rows and default to leaving them out: a conflicted MR needs
   Renovate's rebase before any analysis is worth doing.

   Then ask in **plain text**. Do not use `AskUserQuestion` here: it presents a fixed menu
   with a default, and this pick decides which MRs get analysed and, through Phase 5, which
   ones merge. A menu default must not be able to answer that:

   > Which of these should I check? Reply with row numbers or IIDs (`1 3 4`, `!1130 !1132`),
   > `all`, or `none`.

   One open MR needs no question: analyse it, saying which one you picked up. Their reply
   *is* the confirmation; do not ask a second time. An ambiguous reply gets asked again,
   never resolved by guessing a set. Before a large set, say what it costs: each MR is a full
   Phase 1 (changelog range plus usage mapping).

1. `glab mr view <iid> -F json`: title, `source_branch`, `state`, `merged_at`, draft,
   labels, sha, author.
2. **Renovate gate:** `source_branch` starts with `renovate/`, and when
   `AI_SKILLS_RENOVATE_USER` is set, the author is that user. If either fails, say so and
   stop. This is not a general-purpose MR reviewer.
3. **State gate.** Check it before any analysis, or you spend the whole workflow on a
   decision that has already been taken:

   | State | Action |
   |-------|--------|
   | `opened` | Proceed. If `draft: true`, note it: Renovate marks an MR draft while it is still resolving. |
   | `merged` (`merged_at` set) | Already merged. Say so and stop. Continue only if the user explicitly wants a retrospective. |
   | `closed`, not merged | Renovate closed it, usually superseded by a newer MR for the same package. Say so, offer to find the superseding MR, and stop. |

   Renovate churns these fast: an MR that was open an hour ago may be closed and replaced
   now. Re-read the state; never trust one from earlier in the conversation.
4. **Changed paths** → ecosystem(s). One MR may hit several; Renovate groups updates.

   ```bash
   glab api "projects/:id/merge_requests/<iid>/diffs" | \
     python3 -c "import json,sys; [print(f['new_path']) for f in json.load(sys.stdin)]"
   ```

   Do not grep `glab mr diff` for `diff --git`: its hunk headers are `---`/`+++`, so the grep
   returns nothing and reads as "no files changed".
5. Read every package and version delta **from the diff**. The MR body's version table is a
   cross-check, not the source of truth.
6. Fetch the branch and compare against `origin/<target_branch>` (see *Stale base* below).

## Phase 1 — Analyse

No sandbox yet. Per package in the MR:

1. **Classify the bump**: patch / minor / major, and how many intermediate releases are
   skipped. Treat a `0.x` minor with major-level thoroughness (`0.0.21 → 0.0.31` is ten
   releases). Classification sets *how thorough step 2 must be*; on its own it never decides
   the verdict.
2. **Read the release notes for the whole range.** The MR body's release-notes section first
   (Renovate collapses one per version), then the upstream changelog for **every** version in
   the range it leaves out. Hunt removals, renames, changed defaults, dropped runtime support.
   A version whose notes you could not read is a gap, never "no changes".
3. **Map our usage surface.** Grep for the package's import names (obtained authoritatively:
   see *How to find out*) and for the specific symbols the changelog touches. Produce: *we
   call X and Y; the changelog touches Z*.

   Classify the shape first, because it decides what the rest of the analysis looks at.
   Anchor the grep on import statements, not the bare name: a bare name matches MIME strings,
   comments and prose (`multipart/form-data`) and reads as direct use when there is none.
   Search every source tree the project has (application, tests, scripts, templates), not
   only the main package:

   ```bash
   git grep -nE "^\s*(import|from)\s+(<name1>|<name2>)\b"
   git grep -nE "(require\(|from )['\"]<package>['\"/]"
   ```

   | Shape | Signal | What to analyse |
   |-------|--------|-----------------|
   | **Direct** | Import statements found | The symbols we import, against the changelog. Normal path. |
   | **Mediated** | No imports, but another dependency requires it | We use it *through* the intermediary. Check the intermediary's constraint still holds (below), and treat the intermediary's own tests as the proof. Changelog entries about its public API matter only if the intermediary calls them. |
   | **Unused** | No imports and nothing requires it | Nothing to break. Note it and move on: a genuine ✅, unlike the mediated case a bare grep mistakes it for. |

   A package can be **declared directly and still be mediated**: a pin is not evidence we
   import it. `python-multipart` is often pinned directly, imported nowhere, and reached only
   through Starlette's form parsing.

   For a mediated package, confirm every requirer still accepts the new version (see
   *Requirers*). A requirer whose range excludes the new version is ⛔; one that accepts it
   removes the constraint question and leaves only the behaviour question.
4. **Check what asserts that surface.** For each call site, name a test that would fail on a
   behaviour change, or record `no assertion`. This is the blind-spot list, and it is the
   thing a green pipeline cannot tell you.
5. **Check the CI evidence**: one field, and cheap. Red, missing, or stale-based is a ⚠️
   trigger; green is a gate that never upgrades a verdict. See *How to find out*.

## Phase 2 — Verdict

| Verdict | Trigger |
|---------|---------|
| ✅ **Safe to merge** | Release notes found and read for the *full* range, **and** nothing in them intersects the API or behaviour we use, **and** CI green on a non-stale base. Bump size is irrelevant: a clean major qualifies. |
| ⚠️ **Sandbox warranted** | Any of: a changelog entry intersects our usage **and** no test asserts that behaviour; release notes missing, incomplete, or too wide to review credibly, so "no impact" is *unknown* rather than proven; an import name you could not verify, with no imports found; CI red, absent, or green only on a stale base; the package's effect is not test-observable at all and the bump is more than a patch. |
| ⛔ **Blocked** | A documented breaking change we demonstrably hit, a dropped runtime we still require, or a dependency constraint that cannot resolve. |

The ⚠️ blind-spot rule is an **intersection**: (the changelog touched it) ∩ (we use it) ∩
(no test asserts it). Not "our coverage is thin" in general: otherwise a `pytest` bump would
escalate every time, when CI running pytest *is* the proof.

An MR that bumps several packages gets a verdict per package; the MR's verdict is the worst
of them.

**Stale base:** distance from the target branch is not itself a trigger: a branch can be far
behind and still fully tested. The base is stale only when the target branch moved *since the
pipeline ran* in a way that touches a dependency manifest or one of the usage sites from step
3, because only then can the pipeline's result fail to reproduce on merge:

```bash
git log --oneline --since="<head_pipeline.updated_at>" origin/<target_branch> -- <manifests> <usage files>
```

## Phase 3 — Sandbox (gated)

Only on ⚠️ or ⛔, and **never without asking first**. Print the evidence that warrants it
and the literal commands, then wait for a go-ahead. Headless mode never sandboxes.

Once the user says go (a go-ahead to *run the sandbox*, not an MR approval, which is Phase 5):

1. **Get onto the branch, only if you are not already on it.** `git rev-parse --abbrev-ref
   HEAD` first. Already on the Renovate branch means the user checked it out themselves: use
   that worktree as it stands, and leave it alone in step 4. Otherwise check it out the way
   the project does (its project facts name the command, such as a bootstrap script that
   gives the branch its own worktree and services); without one, `git worktree add` the
   remote branch and start the project's environment there. Remember which of the two
   happened: step 4 turns on it.
2. Run the **targeted** suites, chosen from the Phase 1 usage surface: the test files covering
   those call sites. When that mapping is unclear, fall back to the suite tier CI runs for
   this ecosystem rather than guessing something narrower. Use the project's test-runner
   skill (such as `pytest-docker`) when it has one.
3. Exercise each blind spot by hand: for a rendering or build dependency, produce the artefact
   and inspect it, because no assertion covers it.
4. **Tear down only what step 1 created**: the services and the worktree, **always**,
   including on failure. A worktree that was already the user's stays running.

Then re-verdict on what the run showed. A sandbox that exercised the blind spot cleanly turns
⚠️ into ✅, and it is approved in Phase 5 like any other ✅. A run that reproduced the concern,
or that failed to bootstrap, leaves the MR at ⚠️ or ⛔, unapproved, with the evidence
reported. Never let "the sandbox finished" stand in for "the sandbox passed".

## Phase 4 — Report

Markdown, scaled to the verdict: a clean patch bump is a short block; the full shape below is
what ⚠️ and ⛔ earn. From a terminal it is printed; headless, it is `review.md`.

1. **Header**: `MR !<iid> — <pkg> <from> → <to>`, verdict on its own line.
2. **What changes**: package / from / to / bump class / releases skipped; ecosystem(s); base
   freshness.
3. **CI evidence**: one line: pipeline status, and base freshness if it is in doubt. Name
   individual jobs only when citing one as execution proof for this specific package, and
   then say what it runs. Never present a green wall as evidence of safety.
4. **Release notes reviewed**: one line per version in range, each marked *touches our usage*
   or *does not*, and where it was read (MR body, upstream). Notes that could not be found are
   stated as a gap, never omitted.
5. **Our usage surface**: `file:line` per call site, each with its asserting test or
   `no assertion`.
6. **Findings**: numbered. Empty is a valid and good outcome.
7. **Recommendation**: one paragraph; on ⚠️, the sandbox plan, and from a terminal the
   go-ahead question.

## Phase 5 — Approve

Terminal mode only. Approve every MR the report marked ✅, after the report and without a
further prompt. **Only ✅.** A ⚠️ stays unapproved until its sandbox run clears it; a ⛔ is
never approved.

**Approving may be merging: verify it, don't assume it.** It is when the project requires
one approval *and* Renovate has automerge on for this update type; then `glab mr approve
<iid>` puts the bump on the target branch within seconds. Automerge is often set in a shared
preset that `renovate.json` only extends, so don't conclude from `renovate.json` alone (and
`assignAutomerge` sets the assignee, not automerge). Check the approval half once per run:

```bash
glab api "projects/:id/approval_rules" | python3 -c "
import json,sys
for r in json.load(sys.stdin): print(r['name'], r['approvals_required'])"
```

Then state in one line what approving is about to do: not as a question, as a statement, so
the outcome is not a surprise. If automerge turns out to be off, approval is only approval:
report that, rather than a merge that did not happen.

**Sequence MRs that share a manifest.** Two ✅ MRs editing the same lockfile cannot both
merge: the first wins, the second flips to `conflict` and needs a Renovate rebase, a fresh
pipeline and a fresh approval. Group the ✅ set by the lockfile or manifest each changes
(Phase 0.4), approve **one MR per group**, and hold the rest of that group. Groups don't
collide with each other, so approve across groups freely.

Then confirm what actually happened, per MR:

```bash
glab api "projects/:id/merge_requests/<iid>" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['iid'], d['state'], d.get('detailed_merge_status'), d.get('merged_at'))"
```

Report the end state in a table: merged / approved-but-open / conflicted / held for the next
round / left unapproved and why. A held MR is not finished work: say it needs a re-run once
the merged one lands and Renovate rebases it.

If an approval does not merge, read the status rather than re-approving: `conflict` needs
Renovate's rebase, `not_approved` means the approval did not register, `blocked_status` means
another rule is in the way.

## How to Find Out

Derive these at check time. Do not carry answers from a previous run: CI config, pins and job
lists change, and a stale answer fails silently.

**Ecosystem**: from the diff paths.

**Import names** (Python): never guess from the package name, and expect more than one. Ask
the project's environment when it runs (`docker compose exec -T <service> python -c ...`, or
the local virtualenv):

```bash
python -c "
from importlib.metadata import packages_distributions
rev = {}
for mod, dists in packages_distributions().items():
    for d in dists: rev.setdefault(d, []).append(mod)
print(rev.get('<package-name>'))"
```

`python-multipart` exposes **both** `multipart` and `python_multipart`. Guessing one and
grepping for it returns nothing and reads as "we don't use it": a false ✅. Without an
environment, grep every plausible name (the distribution name with `-` as `_`, without a
`python-` or `py` prefix) and record the import name as unverified.

**Requirers**: the lockfile says who depends on the package without running anything:
`uv.lock` and `poetry.lock` list each package's `dependencies`, `package-lock.json` its
`packages.*.dependencies`, `pnpm-lock.yaml` its `dependencies`. Grep the lockfile for the
package name inside those lists. With a running environment, installed metadata answers the
same:

```bash
python -c "
from importlib.metadata import distributions, requires
for d in distributions():
    for r in requires(d.metadata['Name']) or []:
        if '<package-name>' in r.lower():
            print(d.metadata['Name'], d.version, '->', r)"
```

**CI evidence**: one field, read once: `head_pipeline.status` on the MR
(`glab api "projects/:id/merge_requests/<iid>"`; headless, the `--mr-json` file). It is
GitLab's own aggregate over the whole pipeline tree, child and downstream pipelines included.
Do not enumerate jobs to reach the same answer.

Weigh it asymmetrically:

- **Red, absent, or stale-based**: decisive, and a ⚠️ trigger. This is the whole reason the
  step exists.
- **Green**: a *gate*, not evidence. GitLab already refuses to merge a red blocking pipeline,
  so green mostly restates the merge button. It never upgrades a verdict on its own, and it
  says nothing about behaviour no test exercises.

Only when you want to cite one specific job as execution proof is it worth opening the CI
config, and then check two things, because neither is guessable: whether the job carries
`allow_failure: true` (then its success proves nothing), and what it actually runs, from its
script in the CI config and its includes. A job *name* proves nothing: a seeding job may be
what runs the migrations, a Docker build job what builds the CSS. Also notice a job that is
simply absent rather than green.

**Sibling pins that must move together**: grep the *old* version string across the whole
repo. If it appears in more than one place and only one moved, that is a finding. This
catches the known pairs (a `PNPM_VERSION` CI variable ↔ `packageManager`, `tailwindcss` ↔
`@tailwindcss/cli` ↔ its plugins) *and* ones nobody has thought of yet.

**Verification commands**: the project's task runner (`[tool.poe.tasks]`, `package.json`
scripts, a `Makefile`) and the CI job scripts are the source of truth.

## Ecosystem Coverage

Detected from diff paths, not enumerated in advance.

| Ecosystem | Typical paths | Execution proof |
|-----------|---------------|-----------------|
| Python | `pyproject.toml`, `uv.lock`, `poetry.lock`, `requirements*.txt` | pytest suites, migrations |
| JS / TS | `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` | the project's test and build scripts; for Tailwind, rebuild the generated CSS and diff the output |
| Docker images | `Dockerfile*`, `docker-compose*.yml`, image refs in CI config | health checks (`docker compose up -d --wait`) |
| CI tool pins | `# renovate:`-annotated variables and refs in CI config | upstream release notes + the sibling-pin sweep |
| Renovate config | `renovate.json`, `renovate.json5`, `.gitlab/renovate*` | the Renovate config validator, if CI runs one |

A Postgres **major** image bump is ⛔ by default: it needs a data-directory migration, which a
tag swap does not perform.

A generated file (compiled CSS, a lockfile's hashes) is verified by regenerating and diffing,
never by reading the diff.

## Headless mode (--headless)

```
/renovate-mr-check --headless --mr-json <path/to/mr.json> --out <dir> [--since <sha> --prior <file>]
```

For an unattended caller, such as reviewstation: a daemon running the agent in a detached
worktree checked out at the MR head. It wants the verdict as findings; it gates, posts and
approves itself. There is no `glab` and nobody to answer. Its contract is reviewstation's
`internal/app/prompts/findings-format.md`.

**Never ask anything.** No `AskUserQuestion`, no plain-text question that waits for a reply.
When blocked, write nothing and end the turn with one line `QUESTION: …`.

| Phase | Headless |
|---|---|
| 0 | The MR is `--mr-json`: the raw `glab api projects/<id>/merge_requests/<iid>` object. No list, no pick, no `glab`, no `git fetch` (the caller fetched). Guard: `git rev-parse HEAD` == `diff_refs.head_sha`, else `QUESTION: HEAD <x> != head_sha <y>`. The caller chose this skill for the Renovate bot's MR: skip the Renovate and state gates. Changed paths: `git diff --name-only <diff_refs.base_sha> <diff_refs.head_sha>`. |
| 1 | Unchanged, with the tools the caller allows. Release notes: the MR description first; for versions it leaves out, `WebSearch` when the caller allows it. What neither covers is a gap, so ⚠️. No running environment: the lockfile for requirers, grep for import names (recorded as unverified). Read JSON with the Read tool; `python3` may not be allowed. |
| 2 | Unchanged. |
| 3 | Never runs. A ⚠️ finding says what a sandbox run would have to show, and the commands for it, so the reviewer can run them. |
| 4 | The report, written to `<out>/review.md`. |
| 5 | Never runs. The caller approves. |

**Output**: exactly two files in `--out`, nothing written anywhere else:

1. **`review.md`**: the Phase 4 report.
2. **`findings.json`**: written **last**, after `review.md`, and only once complete:

```json
{
  "spec": 2,
  "summary": "⚠️ Sandbox warranted. urllib3 2.7.1 → 2.8.0 changes the default retry backoff, and nothing asserts the retries in our payment client.",
  "findings": [{
    "severity": "medium",
    "title": "urllib3 2.8 changes the default retry backoff our payment client relies on, and no test asserts it",
    "note_body": "**issue (non-blocking):** urllib3 2.8.0 changes the default `backoff_max` …\n\n**Fix:** run `tests/payments/test_client.py` against this branch, or …",
    "file": "app/payments/client.py",
    "side": "new",
    "line_start": 41,
    "line_end": 44,
    "ai": {"real": "partial", "sound": "yes", "suggest": null, "why": "The changelog touches a default we use; whether it changes our behaviour is unproven.", "evidence": "2.8.0 release notes (MR body); client.py:41-44 builds Retry() without backoff_max; no test mocks the retry timing."}
  }]
}
```

How the verdict becomes findings:

| Verdict | `summary` starts with | Findings |
|---|---|---|
| ✅ | `✅ Safe to merge.` | `[]` |
| ⚠️ | `⚠️ Sandbox warranted.` | One per gap: an intersection with no assertion, a version whose notes you could not read, an unverified import name, red or missing or stale CI. `severity` `medium`; note label `**issue (non-blocking):**`; `ai.suggest` `null`, so the reviewer decides. |
| ⛔ | `⛔ Blocked.` | One per blocker, plus any ⚠️ gaps. `severity` `high` (`critical` for data loss, such as a Postgres major); note label `**issue (blocking):**`; `ai.suggest` `"blocking"`. |

- `summary`: the verdict, then one or two sentences: per package `<pkg> <from> → <to>` and
  what decided it. Shown to the reviewer, never posted.
- `file` and lines: the call site when the gap is in our code (lines outside the MR's diff
  post as a general comment that names them); otherwise the manifest line of the bump in the
  diff. A gap about the whole MR, such as red CI: no `file`.
- `note_body`: posted to GitLab as written, for the team: what changed upstream, what we use,
  and under `**Fix:**` the check that would settle it (the targeted tests, the artefact to
  rebuild and diff). No verdict emoji, no finding ids.
- `ai`: `real` and `sound` as far as the analysis got (`partial` / `risky` for an unproven
  gap); `why` is the verdict reasoning; `evidence` lists what you read: which release notes
  and where, the grep, the lockfile lines, the pipeline status.

Write both files with the Write tool, not a shell heredoc. In `findings.json`, JSON-escape the
markdown strings (`\n`, `\"`, `\\`), then read the file back and check it by eye.

**End the turn** with one line: `MR !<iid>: <verdict>, <n> findings → <out>`.

### Only the new commits (`--since <sha> --prior <file>`)

Renovate rewrites its branch on every rebase and update, so the commits after `<sha>` are
rarely a meaningful slice. Check the whole MR again as above, and leave out a finding that
raises the same gap as one in `--prior` (same package and call site; its lines may have
shifted). `summary` judges the MR as it now stands.

- **Both flags or neither.** One without the other: write nothing, end with
  `QUESTION: --since and --prior go together`.

## Error Handling

| Situation | Action |
|-----------|--------|
| MR not found, `glab` unauthenticated | Report and stop. Do not guess from the URL. |
| Not a Renovate MR | Stop at the Phase 0 gate. |
| No pipeline on the MR | Not an error: a ⚠️ trigger, reported as missing execution evidence. |
| Changelog unreachable | Not an error: a ⚠️ trigger, because "no impact" becomes unproven rather than false. |
| Sandbox bootstrap fails | Report what failed, tear down what got created anyway, fall back to the analysis-only verdict. Never claim a result the run did not produce. |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Escalating on "it's a major" | Severity sets review depth, never the verdict. A clean major is ✅. |
| Concluding ✅ from a thin changelog | No readable notes means *unknown*, not *no impact*. That is ⚠️. |
| Presenting a green pipeline as evidence of safety | Green is a gate GitLab already enforces. Only red, missing, or stale moves a verdict. |
| Enumerating the job tree to answer "is CI green" | `head_pipeline.status` already aggregates the children. Open jobs only to cite one as proof. |
| Trusting a green job that cannot fail | Read `allow_failure` in the CI config. |
| Citing a job by name without reading its script | The name is not the command. |
| Guessing an import name from the package name | Query `packages_distributions()`. Expect more than one name. |
| Grepping only the main package | Usage also lives in tests, scripts and templates. |
| Grepping the bare package name | Matches MIME strings, comments and prose. Anchor on `import`/`from`/`require`, then classify direct vs mediated vs unused. |
| "Grep found no imports, so we don't use it" | Mediated use is still use. Check what requires it before concluding unused. |
| Treating a direct pin as proof we import it | A direct pin can still be mediated: `python-multipart` is often pinned and imported nowhere. |
| Sandboxing without asking | Phase 3 is gated. A bootstrap plus seed is minutes of the user's time. |
| Approving a ⚠️ because CI is green | ⚠️ means the analysis found a gap CI cannot see. Only a sandbox run clears it. |
| Approving every ✅ bump of one lockfile at once | They share it. One merges, the rest conflict. One per manifest group. |
| Sweeping for MRs when none were named | Ask first: a sweep can return a dozen MRs, each a full analysis. |
| Putting the MR pick in an `AskUserQuestion` menu | Its default could answer a question that ends in merges. Ask in plain text against the numbered table. |
| Reporting the MR body's version table as fact | Read the diff. The body is a cross-check. |
| Analysing a closed or merged MR | Check `state` first. Renovate closes and recreates MRs constantly. |
| Grepping `glab mr diff` for `diff --git` | It emits `---`/`+++`. Use the `diffs` API endpoint for changed paths. |
| Leaving a sandbox worktree behind | Tear down after a bootstrap, including on failure, but only what the run created. |
| Asking, sandboxing or approving in headless mode | Headless never does. Gaps become ⚠️ findings; the caller approves. |
| Hardcoding the project | Use `:id`; `glab` resolves it from the git remote. |
| Using `gh` | Renovate MRs here are on GitLab. |
