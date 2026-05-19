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

This skill reads optional config from `~/.config/ai-skills/config.env`. Source it at the start of every run.

```bash
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
SERVICE="${AI_SKILLS_BACKEND_SERVICE:-backend}"
```

| Variable | Default | Purpose |
|---|---|---|
| `AI_SKILLS_BACKEND_SERVICE` | `backend` | Name of the docker-compose service that runs pytest |

All commands below assume `docker compose exec "$SERVICE" pytest …`. If your project runs pytest outside Docker, this skill is not the right tool — install a plain-pytest skill instead.

## Subagent Usage

This skill applies to subagents too. If you are a subagent that has been dispatched with a prompt to run tests:

- **You may run pytest.** The previous blanket ban on subagent test execution has been lifted.
- **You must follow every rule in this skill** — Pre-Test Validation, the tier discipline, exit-code-first output handling, all Hard Rules and Anti-Patterns.
- **Never run a raw `docker compose exec <service> pytest …` command without invoking this skill first.** A `PreToolUse` hook can be configured to block any Bash command containing `pytest` until the skill has been loaded; the same expectation applies to subagents even if a particular session lets the command through.
- **Report back concisely.** Surface the `exit: <N>` line and (on non-zero) the failing test names from `.test-output.txt`. The dispatcher will not see your output file — summarise it for them.

## Pre-Test Validation

**IMPORTANT**: Before running tests after making changes, always check the server logs first to verify the server started without errors:

```bash
docker compose logs --tail=20 "$SERVICE"
```

Look for:
- Import errors (e.g., `ImportError`, `ModuleNotFoundError`)
- Syntax errors
- Server startup failures
- Any Python tracebacks

Only proceed with tests once you've confirmed the server is running correctly. This saves time by catching obvious issues (like import errors) before waiting for test execution.

## Test Tiers

### Tier 1 — Targeted tests (after completing a task)
```bash
docker compose exec "$SERVICE" pytest tests/integration/path/to/relevant_test.py -x -n 0 -ra --tb=short > .test-output.txt 2>&1; echo "exit: $?"
```

Flag rationale:
- `-x` stop on first failure
- `-ra` prints a `FAILED test_name` / `ERROR test_name` line for each failure in the short summary — these are the lines our grep pattern matches (see Output Handling)
- `--tb=short` compact tracebacks
- **No `-v` or `-s` by default** — those produce per-test names and uncaptured stdout, which inflates output for passing runs. Add them only when re-running a specific failing test for debugging (see Failure Handling).

Run tests matching the changed modules — this is the primary iteration loop:
- Changed `domain/<module>/services.py` → run `tests/integration/domain/<module>/`
- Changed `presentation/routes/<area>/` → run `tests/integration/presentation/<area>/`
- Changed domain logic with unit tests → run `tests/unit/domain/<module>/` alongside integration tests
- When in doubt, run the integration tests for the affected area — they cover more ground than unit tests alone

### Tier 2 — Full suite (checkpoints + final verification)
```bash
docker compose exec "$SERVICE" pytest tests/ -q -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
```
- No `-x` — collect all failures at once
- `-n 0` — disable pytest-xdist, run single-process to avoid hoarding CPU/memory on the dev machine
- Run at checkpoints (every 3 completed tasks in a multi-task plan) and as final verification
- If failures are found, fix them before continuing

## Output Handling

All test output goes to `.test-output.txt` (project root). The pytest command always ends with `; echo "exit: $?"` so the exit code is printed in the Bash result. The path is relative, so each worktree gets its own isolated output file — no collisions when running tests in parallel.

### Exit-code-first — green path is a single command

**The pytest exit code is the authoritative pass/fail signal.** 0 = all tests passed, non-zero = something failed (1 = test failures, 2 = usage, 3-5 = collection/internal). There is no case where exit 0 hides a real failure, so on green there is nothing further to check.

1. **Run the pytest command. Look at the `exit: <N>` line in the Bash output.**
2. **`exit: 0` → tests passed. Report success and stop.** Do not grep. Do not tail. Do not `wc`. Do not `Read` the file. Do not run a second grep to see the `N passed` summary — the exit code already told you.
3. **`exit: <non-zero>` → tests failed.** Now use the `Grep` tool (never `Bash(grep ...)`) against `.test-output.txt` with pattern `^FAILED |^ERROR |^=+ .*(failed|error)` to list the failing test names, then `Read` the file for tracebacks.

On the happy path this keeps `.test-output.txt` at zero context cost — nothing is loaded until there's actually something to debug. If you find yourself running a second verification command after seeing `exit: 0`, stop — you have already verified.

## Failure Handling

### Tier 1 failures (`-x` stopped on the first failure)

1. Exit is non-zero → `Grep` `.test-output.txt` for the failing test name, then `Read` for its traceback
2. Fix the failure
3. Re-run **that specific test with `-vs` added** for per-test names and uncaptured stdout:
   ```bash
   docker compose exec "$SERVICE" pytest tests/path/to/test.py::test_name -x -vs -n 0 --tb=short > .test-output.txt 2>&1; echo "exit: $?"
   ```
4. Once `exit: 0`, re-run the Tier 1 target directory to catch any neighbours that now fail

### Tier 2 failures (full suite collected multiple failures)

1. Exit is non-zero → `Grep` `.test-output.txt` for all failing test names, then `Read` the file in full for tracebacks
2. **Report every failure to the user before fixing** — the full-suite run is the only place you see the complete picture; don't start patching blindly
3. Fix the failures
4. Re-run each failing test individually with Tier 1 flags + `-vs` to confirm each fix in isolation
5. Re-run the full suite — repeat until `exit: 0`

## Hard Rules

- **Always source the config first** so `$SERVICE` is populated with `$AI_SKILLS_BACKEND_SERVICE` (default `backend`).
- **Always run single-process with `-n 0`** — never let pytest-xdist auto-detect workers. Parallel runs hoard CPU and memory on the dev machine and can mask ordering-dependent bugs. Every pytest command in this skill must pass `-n 0`.
- Never pipe pytest output through `tail`, `grep`, or `head` in the bash command itself — redirect to `.test-output.txt`, then analyse with the `Grep` tool (using the Grep tool on the saved file is fine and expected)
- Never use `--ignore` flags to skip failing tests
- Never run the full suite during implementation — only at checkpoints and final verification (Tier 2)
- Never use `-v` or `-s` as default flags — they are debugging flags, reserved for re-running a specific failing test
- If a test fails, read the traceback — do not re-run with different flags to "investigate" (except to add `-vs` on a single failing test)
- Assume main is always green — any failing test was most likely introduced by our changes

## Anti-Patterns

- **Verification spiral after `exit: 0`** — piling on `tail`, `grep -E "passed|failed"`, `wc -l`, `grep -E "^="`, or `Read .test-output.txt` to "really confirm" the tests passed. The exit code already confirmed it. Stop at the first green signal.
- **`Bash(grep ...)` against `.test-output.txt`** — prefer the `Grep` tool. Bash greps on this file are a double violation: they're a worse search tool *and* run when the exit code already answered the question.
- `pytest tests/ -x -q 2>&1 | tail -30` — truncates output, causes re-run spirals
- `pytest ... -xvs > .test-output.txt` as the default — `-v -s` inflate output 10–100× for passing runs; they belong in the failure re-run step, not the default loop
- Reading `.test-output.txt` before checking the exit code — wastes tokens on the happy path when `exit: 0` would have confirmed success
- `pytest tests/ --ignore=tests/integration/...` — hiding failures instead of fixing them
- Running full suite after every small change — use Tier 1 instead
- Re-running a failed test with different flags — read the traceback you already have
