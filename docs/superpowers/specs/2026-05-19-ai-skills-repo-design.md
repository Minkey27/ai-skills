# ai-skills repo design

Date: 2026-05-19

## Goal

Turn the `ai-skills` GitHub repo into the canonical home for the user's
personal, shareable Claude Code skills. Replace the current "zip and send"
sharing workflow with a clone-and-symlink model where the repo is the source
of truth and `~/.claude/skills/` consumes it via symlinks.

## Scope

Five skills move into the repo:

- `branch-test-review`
- `finalize-branch`
- `pytest-docker`
- `rebase-on-main`
- `squash`

The four `peon-ping-*` skills stay in `~/.claude/skills/` as real directories.
They are personal (exercise/sound notification helpers) and not intended for
sharing.

## Repo layout

```
ai-skills/
├── README.md          # what these skills are + install instructions
├── install.sh         # symlinks selected skills into ~/.claude/skills/
├── docs/
│   └── superpowers/specs/   # design docs (this file lives here)
└── skills/
    ├── branch-test-review/
    ├── finalize-branch/
    ├── pytest-docker/
    ├── rebase-on-main/
    └── squash/
```

Skills sit under `skills/` so repo-level files (README, install script,
future LICENSE) stay separate from skill content. The layout mirrors
`~/.claude/skills/`, making the symlink mapping a trivial 1:1.

## Source-of-truth flip on the user's machine

For each in-scope skill, perform:

1. `mv ~/.claude/skills/<name>  ~/Projects/ai-skills/skills/<name>`
2. `ln -s ~/Projects/ai-skills/skills/<name>  ~/.claude/skills/<name>`

After the flip, the repo holds the only real copy of each skill. Editing
through either path edits the same files. `git status` in the repo surfaces
changes. The four `peon-ping-*` skills remain untouched.

## Sharing model for other users

The README documents two install paths:

**Manual (per skill):**
```sh
git clone https://github.com/<user>/ai-skills.git
ln -s "$PWD/ai-skills/skills/<name>" ~/.claude/skills/<name>
```

**Scripted (all skills):**
```sh
./install.sh           # symlink every skill, skip if target exists
./install.sh --force   # overwrite existing entries
```

`install.sh` is a small bash script (~20 lines). It iterates over
`skills/*/`, resolves each to an absolute path, and creates symlinks in
`~/.claude/skills/`. If a target path already exists (symlink or real
directory), the script skips it and prints a warning; `--force` replaces it.
This guards against silently clobbering a user's own skill of the same name.

## Risks and validation

- **Symlinked skills load correctly in Claude Code.** The runtime reads file
  contents; it does not care whether the parent path is a symlink. This is
  the same mechanism plugin caches rely on.
- **macOS filesystem behavior.** Symlinks under `~/.claude/` and
  `~/Projects/` work normally; no iCloud or Time Machine quirks apply.
- **Accidental deletion.** `rm -rf ~/.claude/skills/<name>` on a symlink
  removes only the link, not the repo content. Safe.
- **Cross-platform install.** `install.sh` targets bash/zsh on macOS and
  Linux. Windows users are out of scope for the script and can follow the
  manual path with their own symlink command.

## Out of scope

- Publishing as a Claude Code plugin or marketplace entry.
- Versioning, changelogs, or release tags.
- CI checks on skill content.
- Migration of the `peon-ping-*` skills.

These can be added later if the repo grows past a handful of skills.
