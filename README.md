# ai-skills

A personal collection of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills I use day to day. Sharing them here so others can install them with a single command.

## Skills included

- **branch-test-review** — generate a styled HTML report of pytest tests added/modified on the current branch.
- **finalize-branch** — review, simplify, squash, and open an MR for the current branch.
- **handoff** — `/handoff` slash command: compact the current conversation into a handoff document (written to the OS temp dir) so a fresh agent can pick the work up, including a "suggested skills" section. Manual invocation only.
- **mr-review** — `/mr-review` slash command: full review pass on the GitLab MR for the currently-checked-out branch, with per-finding verification and curated diff-note posting.
- **parallel-code-review** — fan out 5 specialist reviewers (correctness, conventions, tests, security, architecture/performance), dedup findings, and return a structured list; invoked by finalize-branch and mr-review.
- **process-mr-feedback** — work through the open review discussion threads on the GitLab MR for the current branch: fetch them, verify each finding against the code, curate a disposition (Fix / Push back / Dismiss / Defer), then implement, push, reply, and resolve.
- **pytest-docker** — run pytest inside the project's docker-compose backend container.
- **rebase-on-main** — rebase the current feature branch onto `main` with guided conflict resolution.
- **squash** — reorganize messy or fixup commits into clean logical commits.
- **worklog** — reconstruct what you worked on in a time window from Claude Code session transcripts + git history; renders a `Subject | Summary | Wallclock | Active estimate` table to help log hours. Reports only; no config required (`AI_SKILLS_TICKET_PREFIX` optionally improves ticket labeling).
- **write-mr-description** — draft a short GitLab MR description (title + body, budgeted to 200 words) for the current branch and create or update the MR with `glab`; runs forked so it can't narrate the implementation session. `finalize-branch` delegates its Step 4 here on GitLab.

Each skill is a directory under [`skills/`](./skills) containing a `SKILL.md` (and optional helper scripts).

## Configuration

Six skills (`finalize-branch`, `mr-review`, `process-mr-feedback`, `pytest-docker`, `rebase-on-main`, `write-mr-description`) accept per-project values through environment variables. The rest work out of the box.

```sh
mkdir -p ~/.config/ai-skills
cp config.example.env ~/.config/ai-skills/config.env
$EDITOR ~/.config/ai-skills/config.env
```

Then expose the variables to Claude Code's shells by adding one line to `~/.zshenv` (or your shell's equivalent — `.bash_profile` for bash, etc.):

```sh
[ -f ~/.config/ai-skills/config.env ] && source ~/.config/ai-skills/config.env
```

Why `.zshenv` and not `.zshrc`? Claude Code's `Bash` tool launches **non-interactive** zsh shells, which only source `.zshenv`. Putting the source line in `.zshrc` won't expose the variables to skills.

Every variable is optional. When a variable is empty, the skill either uses a sensible default (e.g. `gh` for `AI_SKILLS_MR_TOOL`) or skips the step that would have used it (e.g. migration verification is skipped when `AI_SKILLS_ALEMBIC_CMD` is empty). See `config.example.env` for the full list with inline comments.

Common values to set:

| Variable | What it controls |
|---|---|
| `AI_SKILLS_MR_TOOL` | `gh` (GitHub) or `glab` (GitLab) |
| `AI_SKILLS_REVIEWERS` | Comma-separated default reviewers |
| `AI_SKILLS_TICKET_PREFIX` | e.g. `PROJ` — gates the `Closes <TICKET>` line |
| `AI_SKILLS_BACKEND_SERVICE` | docker-compose service that runs your backend / tests |
| `AI_SKILLS_LINT_CMD` / `AI_SKILLS_FORMAT_CMD` | Project lint / format commands |
| `AI_SKILLS_MIGRATIONS_PATH` / `AI_SKILLS_ALEMBIC_CMD` | Alembic paths and invocation |

## Portability

| Skill | Needs config? | What it needs |
|---|---|---|
| `branch-test-review` | No | `git` + Python stdlib only |
| `squash` | No | Generic git operations |
| `finalize-branch` | Optional | `AI_SKILLS_MR_TOOL`, `AI_SKILLS_REVIEWERS`, `AI_SKILLS_TICKET_PREFIX` |
| `handoff` | No | `git` + a writable OS temp dir (`$TMPDIR`, falling back to `/tmp`) |
| `mr-review` | Required | `AI_SKILLS_MR_TOOL=glab` (GitLab-only); `AI_SKILLS_TICKET_PREFIX` is optional. Skill also leverages a tracker MCP (ClickUp/Jira/Linear) if one is installed, otherwise skips the ticket step. |
| `parallel-code-review` | No | Generic git operations |
| `process-mr-feedback` | Required | `AI_SKILLS_MR_TOOL=glab` (GitLab-only). `AI_SKILLS_LINT_CMD`, `AI_SKILLS_FORMAT_CMD`, `AI_SKILLS_TEST_CMD`, `AI_SKILLS_COMMIT_TRAILER` are optional (each step is skipped when its variable is empty; a project test-runner skill is preferred over `AI_SKILLS_TEST_CMD` when present). |
| `pytest-docker` | Optional | `AI_SKILLS_BACKEND_SERVICE` (default `backend`); only useful if you run pytest in docker-compose |
| `rebase-on-main` | Optional | `AI_SKILLS_LINT_CMD`, `AI_SKILLS_FORMAT_CMD`, `AI_SKILLS_MIGRATIONS_PATH`, `AI_SKILLS_ALEMBIC_CMD` (each step is skipped when its variable is empty) |
| `worklog` | Optional | `AI_SKILLS_TICKET_PREFIX` (labeling only); otherwise `git` + Python stdlib. Scans `~/.claude/projects`. |
| `write-mr-description` | Required | `glab` (GitLab-only); tracker MCP optional |

All skills run with no config — the project-specific steps just turn into no-ops.

## Install

```sh
git clone https://github.com/Minkey27/ai-skills.git
cd ai-skills
./install.sh
```

This symlinks every skill in `skills/` into `~/.claude/skills/`. Existing entries are left alone. Pass `--force` if you want to overwrite them:

```sh
./install.sh
```

### Manual install (single skill)

```sh
ln -s "$PWD/skills/<name>" ~/.claude/skills/<name>
```

## Uninstall

Symlinks only — safe to delete directly:

```sh
rm ~/.claude/skills/<name>
```

## Updating

```sh
cd ai-skills
git pull
```

Because the installed paths are symlinks into this clone, `git pull` is the entire update step. No reinstall needed.
