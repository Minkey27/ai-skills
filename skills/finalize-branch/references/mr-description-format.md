# MR description format

The contract Step 4 drafts against. A reviewer opens the MR to answer one question: *should
this be merged?* Anything that does not help them answer it costs them time. The diff already
shows what changed, line by line — the description supplies only what the diff cannot.

## The body

Four parts, in this order. No others — no Summary, no Overview, no Notes, no Changelog, no
Test plan, no Follow-ups.

```markdown
Closes BPZ-1004

## Why

[1–3 sentences of prose. Not bullets.]

## What

- [The change a reviewer must check, and the reason they cannot see.]

## Caveats

[Omitted entirely unless there is something to flag.]
```

**`Closes BPZ-###` is the first line** — keyword, space, ticket id, nothing else on the
line. Downstream automation (Zapier → ClickUp) parses it to transition the ticket, so a
reworded or reformatted line means the ticket silently never advances. If no ticket
reference exists, omit the line completely: never invent one, never leave a placeholder.

**`## Why` is prose on purpose.** A bullet list invites one more bullet; a paragraph forces
you to decide what the reason actually is.

**`## What` carries only what the diff cannot show** — a rationale, a blast radius, a
compatibility answer, a dependency bump. Not a tour of the files.

**`## Caveats` exists only for what would otherwise mislead a reviewer:** an untested path
that matters, a CI job red for a pre-existing reason, a check only doable by hand,
deliberate scope creep. Most MRs have no Caveats section at all. It is never a test report
— "the suite passes" is worth zero and belongs nowhere.

## Budgets

Limits, not targets. Coming in under is good.

| Part | Limit |
|---|---|
| Title | 72 chars |
| Why | 3 sentences, prose |
| What | 5 bullets, 25 words each |
| Caveats | omitted unless it flags something |
| **Whole body** | **200 words — hard, no exceptions** |

The 200-word cap has no escape hatch. A subtle correctness or security argument compresses
to about three sentences; find the load-bearing one rather than asking for more room.

## Title

- Conventional-commit prefix for infra, chores and bugfixes: `fix(projecten):`, `perf(ci):`,
  `chore(docker):`.
- Plain imperative sentence for features: `Filter the project overview by assigned colleague`.
- **Never** a ticket reference — `Closes` already carries it, and the title is where it rots.
- Never prefix `Draft:` or `WIP:`; use the `--draft` flag.

## Write from the diff, not from the session

Step 4 runs in the same session that did the implementation, so the pull toward narrating
that work is real and has to be resisted deliberately. **Derive every sentence from the diff,
the commit messages and the ticket** — never from what you remember doing. A description
written from session memory narrates the journey (the approach tried first, the refactor
along the way, the edge case found at 4pm) and none of that belongs in front of a reviewer.

The test: for each sentence, ask whether someone reading only `git diff $BASE_SHA..HEAD`
plus the ticket could have written it. If not, it is journey, and it goes.

## Never

❌ `## Test plan`, or any step-by-step for the reviewer to run, verify or click through.
   They read the diff and CI; they did not ask for a QA script. If something genuinely
   cannot be trusted without a manual check, that is one line in **Caveats**.
❌ A bullet per changed file — the diff already shows the file list, in a better format.
❌ A bullet per commit, or any reproduction of `git log` — the Commits tab exists.
❌ A bullet per test added, or any list of `test_*` names.
❌ "The suite passes", "all green", "lint + format clean" — CI says this already.
❌ The `🤖 Generated with Claude Code` footer.
❌ Narrating the implementation ("initially used a dict, then switched to a dataclass").
❌ Restating the ticket description instead of summarising the outcome.
❌ Explaining what obvious code does.
❌ Sections for future work, possible improvements or things out of scope.
❌ Emoji headings, bold-word soup, nested bullets.
❌ Filler: comprehensive, robust, seamlessly, significantly, carefully, properly, ensures,
   leverages.
❌ Hedged non-statements ("this should improve performance in most cases").
❌ `--fill` (`glab mr create --fill` / `gh pr create --fill`) — it rebuilds the description
   from commit messages, which is exactly the noise this format removes.

## Example

A real 653-word description and the same change in 164 words:
[mr-description-examples.md](mr-description-examples.md). Read it before drafting.
