---
name: mr-review
description: "MANUAL INVOCATION ONLY. Trigger this skill exclusively when the user types the literal slash command `/mr-review`. Do NOT trigger on natural-language phrases like 'review the MR', 'review this branch', 'let's review', or any other variation — those must be handled without this skill unless the user explicitly types the slash form. When invoked, runs a full GitLab MR review on the currently checked-out branch: fetches the linked ClickUp ticket (BPZ-### format), reads the MR description, runs `superpowers:requesting-code-review`, verifies each finding with parallel sub-agents, surfaces intent discrepancies between ticket/description/diff, and posts the findings the user approves back to the MR as line-anchored diff notes. Works for any MR the user has checked out — their own pre-flight self-review or a teammate's branch. Refuses if the MR's source branch isn't currently checked out, because without a working tree the verification sub-agents can't read files at the MR's tip or grep neighbors."
---

# /mr-review

End-to-end review of the open MR on the current branch. The skill orchestrates four jobs that are easy to do badly when done by hand:

1. Gather intent (ticket + MR description) so review findings can be judged against the *goal*, not just the diff.
2. Run `superpowers:requesting-code-review` to get an initial set of findings.
3. Verify each finding by re-reading the actual code, because reviewers (human or LLM) routinely flag things that aren't really problems or whose recommendations don't actually work.
4. Let the user curate which findings get posted, then post them to GitLab as line-anchored diff notes.

## When to use

**This skill is manual-only.** Trigger exclusively on the literal slash command `/mr-review`. If the user says "review the MR" or any natural-language variation without typing the slash form, do **not** invoke this skill — handle the request without it, or ask whether they want to run `/mr-review`.

Once invoked, the skill applies to **any MR the user currently has checked out**, whether they authored it or pulled down a teammate's branch:

- **Pre-flight self-review** — user finished their own work and wants a critical pass before requesting human review.
- **Teammate review** — user checked out a colleague's branch (`glab mr checkout <iid>` or `git checkout <branch>`) and wants to leave structured feedback on the MR.

Do **not** use this skill for:
- An MR whose source branch is **not currently checked out**. The verification step needs a working tree; without one, sub-agents can only see files via `git show`, which kills their ability to grep neighbors or understand surrounding code. Tell the user to check out the branch first (`glab mr checkout <iid>`).
- Posting ad-hoc comments unrelated to a review pass — just use `glab mr note` directly.

**Authorship doesn't matter** — the skill works the same for the user's own MR and a teammate's. The only difference is tone: when reviewing someone else's work, the discrepancy report and findings will be sent to the author via diff notes, so be precise and neutral. When self-reviewing, the same notes are essentially the user talking to themselves; that's fine too.

## Hard rules

- **Branch must match.** Confirm the current branch is the MR's source branch via `glab mr view --output json`. If not, stop and tell the user — switching branches mid-review is the user's call, not yours.
- **Never post without confirmation.** Even if every finding looks great, present the checklist and wait for the user to pick. Posting to GitLab is irreversible (notifications fire, threads exist forever).
- **`AskUserQuestion` has no default-checked option.** All checkboxes always start empty. Do not write "PRE-CHECKED" in option labels and expect them to be selected — they will not be. The skill works around this by splitting recommendations into a separate question (see Step 7).
- **Content-Type header is mandatory** when calling `glab api ... --input -` to create a discussion. Without it GitLab returns HTTP 415. See [reference_glab_diff_notes.md](../../projects/-Users-yyung-Projects-deurdoor/memory/reference_glab_diff_notes.md) — the full position-payload rules and a worked example live there. Don't re-derive them.
- **Sub-agents that verify findings must read the actual files**, not summaries. The whole point is to catch hallucinated or out-of-date findings — that only works if they look at current code at the MR's tip.
- **Honor `--dry-run`.** If the user invokes `/mr-review --dry-run` (or types "dry run" in the same message), build the payloads and print them as the receipt instead of POSTing. Posting to GitLab is irreversible; dry-run is how the user can sanity-check the anchor lines and body text before committing to the notifications.

## Workflow

### 1. Detect the MR and load the diff

```bash
glab mr view --output json
```

Capture: MR `iid`, `source_branch`, `target_branch`, `title`, `description`, `web_url`, `diff_refs` (`base_sha`, `head_sha`, `start_sha`). The SHAs are needed later when posting diff notes.

**If `glab mr view` errors with "merge request ID number required" + multiple matches**, this means several MRs share the current source branch (typically one open + one or more closed/merged from previous iterations). Disambiguate as follows:

1. Pull the iids from the error message and call `glab mr view <iid> --output json` on each.
2. **Auto-pick the single MR where `state == "opened"`.** That is the only candidate that matters for review.
3. If two or more MRs are open on the same branch (very rare), stop and ask the user which one to target.
4. If zero MRs are open (all candidates are closed/merged), stop — there's nothing to review.

Confirm current branch matches `source_branch`. If not, stop and surface the mismatch.

Get the unified diff so later steps can identify added/removed/context lines for accurate position payloads:

```bash
git fetch origin "$(glab mr view -F json | jq -r .target_branch)" --quiet
BASE_SHA=$(git merge-base "origin/$(glab mr view -F json | jq -r .target_branch)" HEAD)
git diff --unified=0 "$BASE_SHA"..HEAD
```

> **Note:** `BASE_SHA` here is the local merge-base for line-math during posting. The `diff_refs.base_sha` from GitLab is what goes in the position payload — keep them distinct.

### 2. Find the ClickUp ticket

The team's tickets use the **`BPZ-123`** custom-id format (Jira-style prefix + number). Try these sources in order; stop at the first hit:

1. **Branch name** — regex `BPZ-\d+`, anywhere in the branch (e.g. `feat/BPZ-456-add-thing`, `BPZ-123`, `andrew/BPZ-789-fix`).
2. **MR title** — same regex, plus bracketed forms `[BPZ-123]` or `(BPZ-123)`.
3. **MR description** — same regex, *and* any `app.clickup.com/t/<id>` URL (treat the URL's `<id>` segment as a direct ClickUp task id, not a BPZ custom id).
4. **Ask the user** — if nothing matches, ask once: "I couldn't find a BPZ ticket reference. Want to provide one, or proceed without?"

Fetch via the ClickUp MCP. `BPZ-123` is a custom id, not the raw task id, so try a few resolution paths in order:

```
1. mcp__claude_ai_ClickUp__clickup_get_task(taskId="BPZ-123")
   # Many ClickUp setups accept custom ids directly here.

2. If that errors / returns nothing:
   mcp__claude_ai_ClickUp__clickup_search(query="BPZ-123")
   # Then take the first result whose custom_id matches exactly.
```

If the URL form (`app.clickup.com/t/<id>`) was the source, that `<id>` is already the raw task id — skip the custom-id dance and call `clickup_get_task` with it directly.

### 3. Score ticket confidence (decide whether to use it)

Tickets vary wildly in clarity. Before letting the ticket shape the review, judge confidence on three dimensions:

- **Goal clarity** — does the ticket state a concrete outcome ("Add X so users can Y")?
- **Acceptance criteria** — explicit, even informal, list of what "done" looks like?
- **Match to diff** — does the work in the MR plausibly correspond to the ticket?

Confidence levels:

| Level | Heuristic | What to do |
|---|---|---|
| High | Clear goal + criteria + diff matches | Use ticket as primary source of truth for intent. |
| Medium | Goal is clear but criteria are vague | Use the goal; don't lean on missing criteria. |
| Low | Body is empty, title-only, or unrelated to diff | **Ignore the ticket entirely** for this review. Note this in the discrepancy report so the user knows. |

Be honest about low confidence — a misread ticket produces worse findings than no ticket. Don't invent criteria to fill gaps.

### 4. Build an intent summary

In a short scratch note (kept in this conversation, not written to a file), write:

- **Goal (from ticket, if confidence ≥ Medium)**: one sentence.
- **MR description summary**: 2–3 bullets of what the MR claims to do.
- **What the diff actually does**: 2–3 bullets, derived from reading the diff, not the description.

Compare them. Flag any of:

- MR claims a behavior the diff doesn't deliver.
- Diff includes substantial work the MR description doesn't mention.
- Ticket goal and MR description disagree (and ticket confidence is high enough to trust).
- Diff touches a domain the ticket says is out of scope.

Save discrepancies for the final report — *do not* let them become "findings" themselves. They are upstream of code review.

### 5. Run requesting-code-review

Invoke the superpowers skill:

```
Skill: superpowers:requesting-code-review
```

Follow its instructions. When it produces findings, capture them in a structured list:

```
[
  {
    "id": "F1",
    "severity": "high|medium|low|nit",
    "file": "path/to/file.py",
    "line_start": 42,
    "line_end": 42,
    "title": "short headline",
    "issue": "what the reviewer says is wrong",
    "recommendation": "what the reviewer suggests"
  },
  ...
]
```

If `line_start` / `line_end` aren't given, do not invent them — leave null and treat the finding as file-level (not line-anchored). Many reviewer outputs are vague about line numbers; guessing produces wrong anchors and confusing diff notes.

### 6. Fan out to verify findings

For every finding, dispatch a sub-agent **in parallel** (single message, many tool calls). Each sub-agent gets a self-contained brief:

```
Verify this code-review finding against the actual code on the current branch.

Finding:
  File: <file>
  Lines: <line_start>-<line_end>  (or "file-level")
  Issue: <issue text>
  Recommendation: <recommendation text>

Tasks:
  1. Read the cited file and surrounding context. Confirm whether the described issue
     is actually present at the cited location on the current branch. If the lines
     have shifted, find the equivalent location.
  2. Independently judge whether the recommendation, if applied, would actually
     resolve the issue without introducing a new problem.

Report:
  - issue_real: yes / no / partial — with one-sentence reason
  - fix_sound:  yes / no / risky   — with one-sentence reason
  - corrected_lines: <if the line numbers were wrong, give the right ones>
  - notes: anything else worth knowing

Be specific. Do not parrot the finding back — actually look at the code. Under 150 words.
```

Aggregate the results into a single table keyed by finding id.

### 7. Present the curation checklist

Before the checklist, surface the discrepancy report from step 4 in plain text — these are not selectable, they're context the user needs to decide what to post.

**Classify every finding into exactly one bucket:**

| Bucket | Rule | Question it goes into |
|---|---|---|
| **Recommended** | `issue_real ∈ {yes, partial}` AND `fix_sound != no` AND (severity ∈ {`critical`, `high`, `medium`} OR the corrected diagnosis is materially useful even at `low`) | Q1 — "Confirm to post" |
| **Optional** | `issue_real ∈ {yes, partial}` but severity is `low`/`nit`, OR `fix_sound == risky` (real but suggestion has caveats) | Q2 — "Optional additions" |
| **Excluded** | `issue_real == no` (verified false positive), OR sub-agent recommends declining | Not shown as a selectable option. Listed in the discrepancy report instead. |

> **Why `partial` belongs in Recommended for Critical findings.** A `partial` verdict often means the *bug* is real but the reviewer's diagnosis of *how* it triggers was wrong. The sub-agent provides a corrected diagnosis; that corrected version is the one that gets posted. Down-rating it to Optional would defeat the verification step's whole purpose.

**Two-question pattern** (works around `AskUserQuestion`'s no-default-checked limitation):

- **Q1: "These N findings are recommended for posting. Tick all you want to send."** Contains only the Recommended bucket. Make the question text explicit: every option in this list is one the skill recommends posting. The user ticks to confirm, unticks to drop.
- **Q2: "Optional additions — none recommended, but you may still want to post some."** Contains the Optional bucket. Empty selection is the expected default; user ticks to opt in.

If either bucket exceeds 4 options, batch within the bucket (Q1a, Q1b, ... then Q2a, Q2b, ...) rather than mixing buckets. Group by severity inside each batch so heavy hitters come first.

Each option label should be short and scannable: `[F3 medium] auth/repositories.py:128 — every dropdown click rewrites user_roles`. Use the `description` field for the one-line summary plus a verification badge like `✓ verified, fix sound` or `⚠ corrected: triggers on sort/filter, not first load` or `⚠ fix requires repo refactor — bigger than one line`.

For findings in the **Excluded** bucket, list them in the discrepancy report with a one-line "why excluded" so the user knows the skill considered them and what verification found. Don't silently drop findings.

### 8. Post the selected findings

**If `--dry-run` was requested**, skip the POSTs. Instead, print each constructed payload to the terminal as the receipt, formatted so the user can verify the anchor lines and body text. Continue to the receipt section below as if posts had succeeded; the receipt should make it obvious nothing was actually sent (e.g. `[DRY-RUN] Would post 8 notes to MR !<iid>`).

Otherwise: for each selected finding, build the discussion payload and POST it. GitLab's API is one discussion per request — there is no batch endpoint. Prefer a single Python helper script that loops over the payloads and captures `discussion_id` + `note_id` from each response, rather than firing many parallel `Bash` calls; sequential posting from one script is easier to debug if a payload is rejected, and the throughput cost (a few hundred ms per POST) is negligible compared to the time you already spent verifying.

Payload skeleton (full rules in [reference_glab_diff_notes.md](../../projects/-Users-yyung-Projects-deurdoor/memory/reference_glab_diff_notes.md)):

```python
{
  "body": "<markdown finding body>",
  "position": {
    "position_type": "text",
    "base_sha":  "<diff_refs.base_sha>",
    "head_sha":  "<diff_refs.head_sha>",
    "start_sha": "<diff_refs.start_sha>",
    "new_path":  "<file>",
    "old_path":  "<file>",            # same unless rename
    "new_line":  <int or null>,
    "old_line":  <int or null>,
    # multiline only:
    "line_range": {
      "start": {"new_line": <s>, "old_line": <s_old_or_null>, "type": "new|old|expanded"},
      "end":   {"new_line": <e>, "old_line": <e_old_or_null>, "type": "new|old|expanded"}
    }
  }
}
```

Line-number rules (added vs removed vs context) and the `Content-Type` header gotcha are in the referenced memory file. Read it before constructing the payload — getting the position wrong silently anchors notes to the wrong file or rejects them with HTTP 415.

For findings with `line_start == line_end`, omit `line_range` (single-line note). For ranges, include both endpoints.

**For file-level findings without line numbers** (e.g. "add this missing test"), post as a general MR note via `glab mr note create` (not `--message` — that flag is deprecated):

```bash
glab mr note create <iid> --message "<body>"
```

`glab mr note create` writes the URL of the posted note to stdout; capture it for the receipt.

**For diff-note URLs**: the `POST .../discussions` response returns a JSON body with `id` (discussion id) and `notes[0].id` (the actual note id). Build the clickable URL as `{mr_web_url}#note_{notes[0].id}` — that anchors the browser to the specific note, which is much more useful than the bare discussion id.

### 9. Print the receipt

After all posts (or after building all dry-run previews), print a table that the user can scan and click through:

```
Posted N diff notes + M general notes to MR !<iid>. (<mr_web_url>)

| ID | Severity | Anchor | URL |
|----|----------|--------|-----|
| F1 | critical | doors_table.html:43 | <mr_web_url>#note_<note_id> |
...
```

For dry-runs, replace "Posted" with "[DRY-RUN] Would post" and the URL column with `(dry-run — not sent)`.

Also restate any **Excluded** findings from step 7 with one-line "why excluded" reasons so the user knows what didn't get posted and why. This closes the loop: every finding the reviewer produced ends up either Posted, Optional-but-not-picked, or Excluded-with-reason. Nothing is silently dropped.

## Body formatting for diff notes

Each note body should follow this shape so reviewers see consistent, scannable comments:

```markdown
**<short title>** — severity: <high|medium|low|nit>

<issue paragraph: what's wrong and why it matters>

**Suggested fix:** <recommendation in 1–3 sentences>
```

Don't paste the entire finding object. Don't include verification metadata in the body (that's for you, not the MR audience). Keep it under ~120 words per note — long notes get skimmed.

## Failure modes to watch for

- **`glab mr view` returns nothing** — no MR on the branch. Tell the user, suggest `glab mr create --draft --reviewer andrew897,janath2` if they want one, and stop.
- **Stale local branch** — if `git fetch` shows the remote has commits you don't, the review will be against old code. Surface this and ask whether to pull first.
- **`requesting-code-review` returns no findings** — perfectly valid. Still produce the discrepancy report from step 4 (if any) and stop without posting.
- **ClickUp MCP unavailable / 401** — proceed without the ticket. Note "ticket unavailable" in the discrepancy report.
- **Findings with invented line numbers** — when the sub-agent reports `issue_real: no` because the cited line doesn't contain the cited problem, treat it as a hallucination, not a real finding.

## Why this shape

Code review skills tend to over-trigger findings (false positives) because LLMs pattern-match on diff text without considering surrounding context or whether the recommendation actually fits the codebase's conventions. The verification fan-out exists to catch that *before* the user has to filter manually in a checklist of 30 items. The discrepancy report exists because finding-level review misses the larger question: "is this MR doing what it claims?" — which is often where the biggest issues live.

The **two-question curation pattern** in Step 7 is a workaround for `AskUserQuestion`'s lack of a default-checked field — but it has a secondary benefit: separating *recommended* from *optional* makes the user's job a one-handed scan-and-tick on the recommended question rather than a careful read of every item to decide what's worth posting. Don't collapse the two questions back into one "everything goes here" list; it loses the recommendation signal entirely.

The **dry-run** mode exists because the first time you run `/mr-review` on a real MR, you don't yet know whether the line-anchor math is right for this codebase's file layout. Posting eight diff notes to the wrong lines is irreversible and noisy; running the same flow with `--dry-run` first costs one round trip and catches anchor bugs before the team sees them.

The **two-stage open-MR resolution** in Step 1 handles the common case where a branch has accumulated multiple MRs across iterations — typically one open and one or more closed/merged. Auto-picking the single open MR matches user intent virtually every time; only stop if the disambiguation is genuinely ambiguous (two opens, or zero opens).
