# Agent Instructions For uncypher-context

Use `./context-safe.sh <command>` for all memory operations. It is the stable
entry point for Codex, OpenCode, Cursor, Claude Code, and other shell-capable
agents.

Do not call `python3 context.py ...` directly unless debugging the CLI itself.
Direct Python bypasses the global lock, isolated temp database, integrity
checks, and backup snapshots.

## Read Order

1. `./context-safe.sh recent --sessions 14`
2. `<vault>/11-LLM-Wiki/wiki/current-state.md`
3. Relevant `<vault>/11-LLM-Wiki/wiki/threads/*.md`
4. `./context-safe.sh get_context "<topic>"`
5. Raw files in `<vault>/11-LLM-Wiki/raw/`

Compiled wiki pages represent the current source of truth. Raw files are
evidence and history.

## Save Rules

Log meaningful events only:

```bash
./context-safe.sh start_session --title "<title>"
./context-safe.sh log_decision "<decision>" --threads "<thread1,thread2>"
./context-safe.sh log_shift "<description>" --thread "<thread>" --from "<old>" --to "<new>"
./context-safe.sh log_question "<question>" --threads "<thread1,thread2>"
./context-safe.sh memory_capture "<title>" --url "<url>" --kind "blog|tweet|doc|note|chat" --threads "<thread>"
./context-safe.sh memory_compile
./context-safe.sh finalize_session --key-context "<briefing>"
```

Run `memory_compile` after important writes so the Obsidian wiki stays current.

## Privacy

Never commit local user data:

- `context.db`
- `context_backup.db`
- `context_backups/`
- `.context.env`
- Obsidian vault contents
- raw private sources
