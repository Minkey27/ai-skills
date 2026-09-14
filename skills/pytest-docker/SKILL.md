---
name: pytest-docker
description: >
  Use when running pytest, executing tests, writing tests, verifying implementation,
  or when a plan includes test or verification steps. You MUST use this skill whenever
  you are about to run pytest, docker compose exec <service> pytest, or any test command.
  Also use when reviewing a plan that contains testing steps, when checking if code works,
  when doing TDD, or when a task says "verify", "validate", or "test". If you are even
  thinking about running a test — use this skill first.
---

# Pytest in Docker — Test Execution Skill

## Config

Optional `AI_SKILLS_*` env vars, sourced once from `~/.zshenv`
(`[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env`);
commands below use `${VAR:-default}` so the skill works with no config.

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_BACKEND_SERVICE` | `backend` | docker-compose service that runs pytest |

Pytest outside Docker → install a plain-pytest skill instead.

## Subagents

Every rule here applies to a subagent dispatched to run tests. Never run a raw
`docker compose exec <service> pytest …` without loading this skill first (a
`PreToolUse` hook may block the command until it is loaded). Report the
`exit: <N>` line and, on non-zero, the failing test names — the dispatcher never
sees `.test-output.txt`.

**Implementation subagents run Tier 1 only.** Tier 2 belongs to the controller,
once, at `finalize-branch`. When the dispatch prompt says "run the full suite
once before committing", that means the Tier 1 run, once — never `tests/`,
`tests/unit`, `tests/integration`, or a whole context directory. If the blast
radius truly needs the full suite, say so in your report; do not run it. The
observed failure mode is not the literal full suite but the flat directory
"covering" your module, run two or three times per task at minutes each — it
tells the controller nothing it will not learn at `finalize-branch`.

## Before running

```bash
docker compose logs --tail=20 "${AI_SKILLS_BACKEND_SERVICE:-backend}"
```

An `ImportError`, syntax error or startup traceback shows here in seconds, before
a test run would.

## Tier 1 — targeted (after each task)

A Tier 1 run is a **list of test files**, never a directory:

1. every test file you added or changed;
2. the existing files named after the modules you touched —
   `ls tests/**/test_<module>*.py`, or `grep -rl <ChangedSymbol> tests/`;
3. for a cross-cutting change, one `-k` expression naming the area.

1 and 2 are both required: a run of only the tests you wrote proves nothing
about the existing behaviour you changed. A glob like `test_<module>*.py` is a
file list, not a directory. One command, about a minute:

```bash
docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/integration/path/test_a.py tests/unit/path/test_b.py -x -n 0 -ra --tb=short > .test-output.txt 2>&1; echo "exit: $?"
```

Flags: `-x` stop at the first failure; `-ra` prints the `FAILED`/`ERROR` lines the
Grep pattern below matches; `--tb=short`; no `-v`/`-s` — debug flags for a single
failing re-run, 10–100× more output on a passing run.

**A directory is not a Tier 1 target.** The directory "covering" a route module is
often a flat few-thousand-test bucket — a full-suite run under a targeted label.
Cannot name the files → name the area with `-k`; cannot do that either → say so
in the report and let the controller decide.

- Changed `domain/<module>/services.py` → `tests/integration/domain/<module>/test_services*.py` plus `tests/unit/domain/<module>/test_services*.py`
- Changed `presentation/routes/<area>/<page>.py` → `tests/integration/presentation/<area>/test_<page>*.py` plus the test files you wrote
- Changed a template → the route tests that render it (grep the template name under `tests/`)
- In doubt → the integration files over the unit files; more ground per second

## Tier 2 — full suite (once per branch)

```bash
docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/ -q -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
```

No `-x` — collect every failure. Runs **once per branch, by the controller, as
the close of `finalize-branch` Step 2** (after the simplify commit, before
squash). Not between tasks, not per commit: CI runs the suite on every push; the
local run exists to catch a cross-cutting break before the MR round-trip, once.
Failures → classify (below), fix yours, re-run once.

Pass the Bash tool `timeout: 600000` — the suite takes 5–8 minutes and the
default 2-minute timeout backgrounds it. **One run at a time**: the test DB is
shared, so a second concurrent run corrupts both. "Is it still running?" is
answered by `.test-output.txt` — a growing file or a progress line at the tail
means yes, a tally line means no. Never by `ps`/`pgrep` in the container.

## Output handling

Redirect to `.test-output.txt` (relative path — each worktree gets its own) and
end the command with `; echo "exit: $?"`. **The exit code is the verdict:** 0
passed; 1 test failures; 2 usage; 3–5 collection/internal. Exit 0 never hides a
failure.

The `exit: <N>` line is printed to the **Bash result**, never into
`.test-output.txt`. A run that outlives the Bash timeout is backgrounded by the
harness ("Command running in background…"); its `exit:` line is then the last
line of the task file the harness names, delivered with the completion
notification — wait for that notification; do not poll, do not start another run.

- `exit: 0` → done. No `grep`, `tail`, `wc` or `Read` to "really confirm" — a
  second verification command after a green exit is the spiral this rule exists
  to stop.
- non-zero → `Grep` tool (never `Bash(grep …)`) on `.test-output.txt` with
  `^FAILED |^ERROR |^=+ .*(failed|error)` for the names, then `Read` for the
  tracebacks.
- **No `exit:` line anywhere** (session restart, killed wrapper shell) → the
  verdict is in the artifact, not in a re-run. The last line of
  `.test-output.txt` is pytest's tally, `=== N passed[, M skipped] in Ns ===`:
  no `failed`/`error` in it → green. Tally missing too (client cut off
  mid-summary) but a `short test summary info` header present → every test
  ran; zero `^FAILED |^ERROR ` lines → green, reported as "tally lost, verdict
  from the summary section". Re-running to recover a number you can already
  read is the same spiral.

## Failures

### Classify before debugging — the base may be red

Two buckets: **pre-existing** (fails on the base too) and **new** (yours).
Debugging a pre-existing failure as if you caused it is the most expensive way to
waste a task. Cheapest check first; stop when one answers:

1. A recorded known-failure baseline in the testing docs or `CLAUDE.md`/`AGENTS.md`
   → pre-existing.
2. Failing test and whole traceback in code you did not touch, no plausible link →
   probably pre-existing; confirm with 3.
3. That one test on a clean base:
   ```bash
   git stash   # or a scratch worktree
   docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/path/to/test.py::test_name -x -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
   git stash pop
   ```
   Non-zero on the base → pre-existing. Zero on the base → yours.

**Yours** → fix. **Pre-existing** → do not fix, do not fold into your task; report
the count and names and that you confirmed them on the base. Never a bare count:
"17 failing" is unusable, "0 new, 17 pre-existing (confirmed on base)" is green.

Tier 1 must reach `exit: 0`. Tier 2 on a red base never can — its bar is *no new
failures*; do not chase the zero. Re-deriving the same pre-existing list every
task → tell the dispatcher; it belongs in the testing docs as a baseline.

### Tier 1 failure (`-x` stopped)

1. `Grep` the name, `Read` the traceback, fix.
2. Re-run that one test with `-vs`:
   ```bash
   docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/path/to/test.py::test_name -x -vs -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
   ```
3. On `exit: 0`, re-run the Tier 1 file list for neighbours that now fail.

### Tier 2 failures

1. `Grep` every name, `Read` the tracebacks.
2. Classify each — the full suite is where pre-existing failures arrive in bulk.
3. Report all of them, split new / pre-existing, **before** fixing anything.
4. Fix yours; re-run each fixed test alone with `-vs`.
5. Re-run the suite once; green = only the classified pre-existing list remains.

## Hard rules

- `-n 0` on every command — xdist hoards CPU/memory on the dev machine and masks ordering bugs.
- Never pipe pytest through `tail`, `grep` or `head`; redirect, then the `Grep` tool.
- Never `--ignore` to skip a failing test.
- Never `-v`/`-s` by default — only on a single failing test's re-run.
- Never a directory as a Tier 1 target; never the full suite during implementation; never Tier 2 as a subagent.
- Read the traceback you already have — no re-running with different flags to "investigate".
- Never assume the base is green; never report a bare failure count.
- Never start a test run while another may still be running; never re-run to recover a verdict the output already holds.
