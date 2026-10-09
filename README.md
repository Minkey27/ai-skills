# ai-skills

A personal collection of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills I use day to day. Sharing them here so others can install them with a single command.

## Skills included

- **branch-test-review** — generate a styled HTML report of pytest tests added/modified on the current branch.
- **finalize-branch** — review, simplify, squash, and open an MR for the current branch (including the MR title and 200-word-budgeted description); findings are curated in plannotator when available, in the terminal otherwise.
- **handoff** — `/handoff` slash command: compact the current conversation into a handoff document (written to the OS temp dir) so a fresh agent can pick the work up, including a "suggested skills" section. Manual invocation only.
- **mr-review** — `/mr-review` slash command: full review pass on the GitLab MR for the currently-checked-out branch, with per-finding verification and curated diff-note posting; findings are curated in plannotator when available, in the terminal otherwise.
- **process-mr-feedback** — work through the open review discussion threads on the GitLab MR for the current branch: fetch them, verify each finding against the code, curate a disposition (Fix / Push back / Dismiss / Defer), then implement, push, reply, and resolve; findings are curated in plannotator when available, in the terminal otherwise.
- **pytest-docker** — run pytest inside the project's docker-compose backend container.
- **rebase-on-epic** — restack a story branch onto its epic's latest tip, replaying only the branch's own commits even after the epic was force-pushed.
- **rebase-on-main** — rebase the current feature branch onto `main` with guided conflict resolution.
- **squash** — reorganize messy or fixup commits into clean logical commits.
- **worklog** — reconstruct what you worked on in a time window from Claude Code session transcripts + git history; renders a `Subject | Summary | Wallclock | Active estimate` table to help log hours. Reports only; no config required (`AI_SKILLS_TICKET_PREFIX` optionally improves ticket labeling).

Each skill is a directory under [`skills/`](./skills) containing a `SKILL.md` (and optional helper scripts).

## Configuration

Six skills (`finalize-branch`, `mr-review`, `process-mr-feedback`, `pytest-docker`, `rebase-on-epic`, `rebase-on-main`) accept per-project values through environment variables. The rest work out of the box.

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
| `squash` | Optional | `AI_SKILLS_TARGET_BRANCH` names the default branch, falling back to `origin/HEAD`, then `main`; otherwise generic git operations |
| `finalize-branch` | Optional | `AI_SKILLS_MR_TOOL`, `AI_SKILLS_REVIEWERS`, `AI_SKILLS_TARGET_BRANCH`, `AI_SKILLS_TICKET_PREFIX`. Uses a tracker MCP (ClickUp/Jira/Linear) for ticket intent if one is installed, otherwise skips that lookup. Uses [plannotator](https://plannotator.ai) for finding curation when the `plannotator` binary is on `PATH`; falls back to numbered terminal prompts when it is not. |
| `handoff` | No | `git` + a writable OS temp dir (`$TMPDIR`, falling back to `/tmp`) |
| `mr-review` | Required | `AI_SKILLS_MR_TOOL=glab` (GitLab-only); `AI_SKILLS_TICKET_PREFIX` is optional. Skill also leverages a tracker MCP (ClickUp/Jira/Linear) if one is installed, otherwise skips the ticket step. Uses [plannotator](https://plannotator.ai) for finding curation when the `plannotator` binary is on `PATH`; falls back to numbered terminal prompts when it is not. |
| `process-mr-feedback` | Required | `AI_SKILLS_MR_TOOL=glab` (GitLab-only). `AI_SKILLS_LINT_CMD`, `AI_SKILLS_FORMAT_CMD`, `AI_SKILLS_TEST_CMD`, `AI_SKILLS_COMMIT_TRAILER` are optional (each step is skipped when its variable is empty; a project test-runner skill is preferred over `AI_SKILLS_TEST_CMD` when present). Uses [plannotator](https://plannotator.ai) for finding curation when the `plannotator` binary is on `PATH`; falls back to numbered terminal prompts when it is not. |
| `pytest-docker` | Optional | `AI_SKILLS_BACKEND_SERVICE` (default `backend`); only useful if you run pytest in docker-compose |
| `rebase-on-epic` | Optional | Same variables as `rebase-on-main`. Finds the epic from the MR target through `glab` + `jq` when you don't name it |
| `rebase-on-main` | Optional | `AI_SKILLS_LINT_CMD`, `AI_SKILLS_FORMAT_CMD`, `AI_SKILLS_MIGRATIONS_PATH`, `AI_SKILLS_ALEMBIC_CMD` (each step is skipped when its variable is empty) |
| `worklog` | Optional | `AI_SKILLS_TICKET_PREFIX` (labeling only); otherwise `git` + Python stdlib. Scans `~/.claude/projects`. |

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

### Status line

`statusline/statusline-command.sh` is my Claude Code status line. It prints three rows, because Claude Code truncates a wide status line instead of wrapping it. Link it in and point `settings.json` at the link:

```sh
ln -s "$PWD/statusline/statusline-command.sh" ~/.claude/statusline-command.sh
```

```json
"statusLine": { "type": "command", "command": "bash ~/.claude/statusline-command.sh" }
```

It needs `jq`. The `🧭` skill label only shows when a hook writes `~/.claude/state/active-skill/<session_id>`; without one that part stays empty.

### iTerm2 session status

iTerm2's Session Status panel shows each Claude Code session as idle, working or waiting. Its own `cc-status` hook fills the detail line with the last reply when a turn ends, but blanks it while Claude works. `iterm2/cc-status-tool` wraps `cc-status` and writes the prompt there, then the latest tool, such as `Bash · List files` or `Edit · app.py`. The tool stays up between tool calls, so a long think still shows what came last. When the turn ends it swaps the reply preview for the session title, so an idle session still says what it was about; with no title yet, the preview stays.

It builds on iTerm2's Claude Code integration, so set that up first: it links `~/.config/iterm2/cc-status` and wires it into the hook events in `~/.claude/settings.json`. Then link the wrapper in:

```sh
mkdir -p ~/.config/iterm2/with-detail
ln -s "$PWD/iterm2/cc-status-tool" ~/.config/iterm2/with-detail/cc-status
```

In `settings.json`, point the `UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `Stop` entries at `/Users/<you>/.config/iterm2/with-detail/cc-status`. The other events stay on `cc-status`. The link name and the absolute path both matter. iTerm2 3.7.4 checks that each of its hook events has a command that ends in `/cc-status` and is an executable path, and it offers to reinstall the hook on every launch when one does not. Don't accept that offer: Reinstall points every command ending in `/cc-status` back at iTerm2's own binary, which drops the wrapper. Give the `Notification` entry `"matcher": "permission_prompt"`: the `idle_prompt` notification arrives a minute after each turn and blanks the reply preview, and `Stop` already covers everything it would show. It needs `jq`.

### Session title

Claude Code only titles a session from its first message, and skips that message when it is a slash command. A session opened with `/mr-review` therefore stays "Claude Code" in the terminal title and in `/resume`. `hooks/session-title.sh` titles those from the command and the git branch, such as `mr-review · feature-x`. It leaves plain-text sessions to Claude Code's own title, and never replaces a `/rename`.

```sh
ln -s "$PWD/hooks/session-title.sh" ~/.claude/hooks/session-title.sh
```

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "bash ~/.claude/hooks/session-title.sh", "timeout": 5 }] }]
```

It needs `jq`.

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
