# Claude Instructions For uncypher-context

Use this project as a local-first memory system. Always prefer the wrapper:

```bash
./context-safe.sh <command>
```

Do not call `python3 context.py ...` directly unless debugging the CLI itself.
The wrapper serializes access, uses an isolated temp SQLite DB, validates
integrity, and keeps backups.

## Reading Memory

Start with:

```bash
./context-safe.sh recent --sessions 14
```

Then read the compiled wiki:

```text
<vault>/11-LLM-Wiki/wiki/current-state.md
<vault>/11-LLM-Wiki/wiki/threads/*.md
<vault>/11-LLM-Wiki/wiki/discarded-ideas.md
<vault>/11-LLM-Wiki/wiki/open-questions.md
```

Only search raw sources after checking compiled current-state pages. Raw sources
are historical evidence. Current-state pages are the default source of truth.

## Saving Memory

At session start:

```bash
./context-safe.sh start_session --title "<descriptive title>"
```

After a real decision:

```bash
./context-safe.sh log_decision "<decision>" --threads "<thread1,thread2>"
./context-safe.sh memory_compile
```

After a thinking shift:

```bash
./context-safe.sh log_shift "<what changed>" --thread "<thread>" --from "<old>" --to "<new>"
./context-safe.sh memory_compile
```

When an unresolved question appears:

```bash
./context-safe.sh log_question "<question>" --threads "<thread1,thread2>"
```

When the user shares a useful tweet, blog, article, document, meeting note, or
pasted text:

```bash
./context-safe.sh memory_capture "<title>" --url "<url>" --kind "blog|tweet|doc|note|chat" --threads "<thread>" --notes "<why it matters>"
./context-safe.sh memory_compile
```

At session end:

```bash
./context-safe.sh finalize_session --key-context "<2-3 sentence briefing for the next session>"
./context-safe.sh memory_compile
```

## Behavior Rules

- Do not revive superseded ideas as current strategy.
- If old and new positions conflict, prefer the compiled thread page and
  `discarded-ideas.md`.
- If a useful synthesis is created during a conversation, save it back into the
  graph or capture it as a raw note.
- Keep user data local. Do not commit `context.db`, `.context.env`, vault
  contents, or raw private sources.
