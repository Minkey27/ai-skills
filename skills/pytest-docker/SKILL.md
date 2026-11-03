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

Every rule here also applies to a subagent dispatched to run tests. Never run a raw
`docker compose exec <service> pytest …` without loading this skill first (a
`PreToolUse` hook may block the command until it is loaded). Report the
`exit: <N>` line and, on non-zero, the failing test names — the dispatcher never
sees `.test-output.txt`.

**Implementation subagents run Tier 1 only.** Tier 2 belongs to the controller,
at most once per branch, usually at `finalize-branch`. When the dispatch prompt says "run the full suite
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
2. the existing files named after the modules you touched, found by searching
   the host checkout for `test_<module>*.py` or `<ChangedSymbol>` under `tests/`
   (never a container path);
3. for a cross-cutting change, one `-k` expression naming the area, over the
   narrowest directory that holds the matching files — that directory is the
   collection root, not the target.

1 and 2 are both required: a run of only the tests you wrote proves nothing
about the existing behaviour you changed. A glob like `test_<module>*.py` is a
file list, not a directory. Paths in the pytest command are relative to the
container's working directory — strip any host prefix (such as `backend/`) the
tool results carry. One command, about a minute:

```bash
docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/integration/path/test_a.py tests/unit/path/test_b.py -x -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
```

Flags: `-x` stop at the first failure; `--tb=short`; no `-v`/`-s` — debug flags for
a single failing re-run, 10–100× more output on a passing run. Pytest's default
`-rfE` already prints the `FAILED`/`ERROR` lines the pattern below matches.

**A bare directory is not a Tier 1 target.** The directory "covering" a route module
is often a flat few-thousand-test bucket — a full-suite run under a targeted label.
Cannot name the files → `-k <area>` over the narrowest directory holding them;
cannot do that either → say so in the report and let the controller decide.

- Changed `domain/<module>/services.py` → `tests/integration/domain/<module>/test_services*.py` plus `tests/unit/domain/<module>/test_services*.py`
- Changed `presentation/routes/<area>/<page>.py` → `tests/integration/presentation/<area>/test_<page>*.py` plus the test files you wrote
- Changed a template → the route tests that render it (grep the template name under `tests/`)
- In doubt → the integration files over the unit files; more ground per second

## Tier 2 — full suite (at most once per branch)

```bash
T=$(git status --porcelain | grep -q . || git rev-parse 'HEAD^{tree}')
docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/ -q -n 0 --tb=short > .test-output.txt 2>&1; rc=$?
[ "$rc" = 0 ] && [ -n "$T" ] && echo "$T" > "$(git rev-parse --git-dir)/full-suite-green"; echo "exit: $rc"
```

No `-x` — collect every failure. Runs **at most once per branch, by the
controller**, and only when **Size it first** below calls for it. The close of
`finalize-branch` Step 1 is the default moment for that check. A green run from
a clean tree records the tree it tested in the worktree's git dir, so a later
session sees that the suite already passed on this code. What is forbidden is
starting a run on your own initiative: CI runs the suite on every push, and the
local run exists to catch a cross-cutting break before the MR round-trip.
Failures → fix, then size the fix (Tier 2 failures, step 4).

Pass the Bash tool `timeout: 600000` — the suite takes 5–8 minutes and the
default 2-minute timeout backgrounds it. **One run at a time**: the test DB is
shared, so a second concurrent run corrupts both. "Is it still running?" is
answered by `.test-output.txt` — a growing file or a progress line at the tail
means yes, a tally line means no. Never by `ps`/`pgrep` in the container.

### Size it first

A second green run on the same code buys nothing, so list what changed since the
last green full run, or the whole branch when none is on record:

```bash
M="$(git rev-parse --git-dir)/full-suite-green"
if [ -f "$M" ]; then echo "since the last green full run:"; BASE=$(cat "$M")
else echo "no green full run on record, whole branch:"; BASE=$(git merge-base "origin/${AI_SKILLS_TARGET_BRANCH:-main}" HEAD); fi
git diff --name-only "$BASE" HEAD
```

| The list holds | Run |
|---|---|
| nothing | no tests; report the earlier green run |
| test infrastructure (`conftest.py`, fixtures, factories, pytest config), a migration, a dependency or lockfile change, or a shared module most of the app imports; a lint-only edit to `pyproject.toml` is none of these | Tier 2 |
| anything else | collect-only, then one Tier 1 run over the listed files, plus the project's route-wide guard tests when route code is listed |

Collect-only imports every test module, so a test anywhere that still imports a
symbol the change removed or renamed fails here, about a minute:

```bash
docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/ --collect-only -n 0 > .test-output.txt 2>&1; echo "exit: $?"
```

Route-wide guards are tests that check every registered route against lists kept
by hand (CSRF, auth redirects, GET smoke tests). A route added, removed or
renamed breaks them far from the changed module, and they are named after no
module, so the Tier 1 search never finds them. The project's CLAUDE.md or memory
names them.

## Output handling

Redirect to `.test-output.txt` (relative path — each worktree gets its own) and
end the command with `; echo "exit: $?"`. **The exit code is the verdict:** 0
passed; 1 test failures; 2 interrupted (Ctrl-C, killed); 3 internal error; 4 usage
error (bad path, unknown flag); 5 no tests collected — your selector matched
nothing, not a pass; 137 SIGKILL/OOM — stop, report, ask the user to free host
resources, never fall back to directory chunks. Exit 0 never hides a failure.

The `exit: <N>` line is printed to the **Bash result**, never into
`.test-output.txt`. A run that outlives the Bash timeout is backgrounded by the
harness ("Command running in background…"); its `exit:` line is then the last
line of the task file the harness names, delivered with the completion
notification — wait for that notification; do not poll, do not start another run.

- `exit: 0` → done. No `grep`, `tail`, `wc` or `Read` to "really confirm" — a
  second verification command after a green exit is the spiral this rule exists
  to stop.
- non-zero → search `.test-output.txt` for `^FAILED |^ERROR ` to get the
  names, then `Read` for the tracebacks.
- **No `exit:` line anywhere** (session restart, killed wrapper shell) → the
  verdict is in the artifact, not in a re-run. The last line of
  `.test-output.txt` is pytest's tally, `N passed[, M skipped] in Ns`, wrapped in
  `===` only without `-q`: no `failed`/`error` in it → green. Tally missing too
  (client cut off mid-summary) but a `short test summary info` header present →
  every test ran; zero `^FAILED |^ERROR ` lines → green, reported as "tally lost,
  verdict from the summary section". Re-running to recover a number you can
  already read is the same spiral.

## Failures

Every failure on a feature branch is yours. Tier 1 and Tier 2 both end at
`exit: 0`.

### Tier 1 failure (`-x` stopped)

1. Search the output for the name, `Read` the traceback, fix.
2. Re-run that one test with `-vs`:
   ```bash
   docker compose exec "${AI_SKILLS_BACKEND_SERVICE:-backend}" pytest tests/path/to/test.py::test_name -x -vs -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
   ```
3. On `exit: 0`, re-run the Tier 1 file list for neighbours that now fail.

### Tier 2 failures

1. Search the output for every name, `Read` the tracebacks.
2. Report all of them **before** fixing anything.
3. Fix; re-run each fixed test alone with `-vs`.
4. Size the fix, not the branch: every test the fix left alone already
   passed on this code. A fix that touches only test files, and none of the
   test infrastructure listed under **Size it first**, is done once step 3
   passes; commit it and record the tree, so a later session skips the run too:
   ```bash
   git status --porcelain | grep -q . || git rev-parse 'HEAD^{tree}' > "$(git rev-parse --git-dir)/full-suite-green"
   ```
   Any other fix goes through **Size it first** with the fix's own diff as the
   list: a contained runtime change gets collect-only plus Tier 1, and only the
   Tier 2 rows earn a second full run.

## Hard rules

- `-n 0` on every command — xdist hoards CPU/memory on the dev machine and masks ordering bugs.
- Never pipe pytest through `tail`, `grep` or `head`; redirect to a file, then search the file.
- Never `--ignore` to skip a failing test.
- Never `-v`/`-s` by default — only on a single failing test's re-run.
- Never a bare directory as a Tier 1 target; never the full suite during implementation; never Tier 2 as a subagent.
- With `-k`, the collection root is the narrowest directory holding the matching files.
- Read the traceback you already have — no re-running with different flags to "investigate".
- Every failure is yours — never label one pre-existing.
- Never start a test run while another may still be running; never re-run to recover a verdict the output already holds.
