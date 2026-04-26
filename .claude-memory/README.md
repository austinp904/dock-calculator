# .claude-memory

This folder is Claude Code's per-project memory for **dock-calculator**, relocated
from `~/.claude/projects/<encoded-path>/memory/` to here so memory syncs
across machines via git.

Each machine has `~/.claude/projects/<machine-specific-encoded>/memory/`
linked here (symlink on Mac/Linux, directory junction on Windows). Both
sides write through the link into this folder.

Run `bin/setup-claude-memory-link <project>` from the master repo
(`~/Projects/`) on a new machine to create the link. See
`~/Projects/30-Resources/Claude Memory Sync.md` for the full pattern.

## What lives here

- `MEMORY.md` — the index Claude maintains
- `<topic>.md` — individual memory entries (user/feedback/project/reference)

Safe to read, edit, or delete files manually. Claude rewrites them as it
learns new things; corrupted files just lose that one memory.
