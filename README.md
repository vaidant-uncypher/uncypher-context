# uncypher-context

A local-first memory layer for AI-assisted work.

`uncypher-context` combines a small SQLite knowledge graph with an Obsidian
LLM Wiki compiler. It is built for people who work across Claude, ChatGPT,
Codex, Cursor, and other agents and need a durable system for:

- saving decisions, questions, and thinking shifts as they happen;
- capturing tweets, blogs, docs, meeting notes, and chat summaries as raw
  evidence;
- compiling current-state markdown pages that agents read before old files;
- marking superseded ideas so stale thinking does not keep resurfacing;
- keeping the database reliable when multiple agents are running.

It is inspired by Andrej Karpathy's LLM Wiki pattern: raw sources, compiled
wiki, and agent instructions. Compared with broader Obsidian Wiki frameworks,
this project is intentionally small: one Python CLI, one reliability wrapper,
and markdown output that any agent can read.

## What You Get

```text
context.py              SQLite graph + memory CLI
context-safe.sh         reliable wrapper: lock, temp DB, backups, integrity checks
AGENTS.md              generic instructions for Codex/OpenCode-style agents
CLAUDE.md              generic instructions for Claude Code
.context.env.example   vault/config example
setup.sh               creates local config and initializes memory
```

Generated local data is ignored by git:

```text
context.db
context_backup.db
context_backups/
.context.env
<your Obsidian vault>/
```

## Quick Start

```bash
git clone https://github.com/vaidant-uncypher/uncypher-context.git
cd uncypher-context
./setup.sh
```

The setup script asks for an Obsidian vault path, writes `.context.env`, and
initializes the memory wiki.

If you prefer manual setup:

```bash
cp .context.env.example .context.env
# edit CONTEXT_VAULT_PATH in .context.env
./context-safe.sh memory_init
./context-safe.sh memory_compile
```

## Daily Workflow

Start a session:

```bash
./context-safe.sh start_session --title "Research pricing strategy"
```

Log decisions continuously:

```bash
./context-safe.sh log_decision "Use founder-led outbound for the first 20 customers" \
  --threads "go-to-market,pricing"
```

Log a thinking shift:

```bash
./context-safe.sh log_shift "Pricing model changed" \
  --thread "pricing" \
  --from "single flat monthly plan" \
  --to "base retainer plus usage tier"
```

Capture external sources:

```bash
./context-safe.sh memory_capture "Useful post on AI memory" \
  --url "https://example.com/post" \
  --kind blog \
  --threads "ai-memory" \
  --notes "Good framing for raw/wiki/schema distinction"
```

Compile the wiki:

```bash
./context-safe.sh memory_compile
```

Read current state:

```bash
./context-safe.sh recent --sessions 14
./context-safe.sh get_context "go-to-market"
./context-safe.sh get_thread "pricing"
./context-safe.sh memory_search "AI memory"
./context-safe.sh memory_lint
```

End a session:

```bash
./context-safe.sh finalize_session \
  --key-context "Moved pricing from flat fee to base plus usage; capture customer proof next."
```

Finalized sessions are also captured as raw chat memory in the LLM Wiki.

## Architecture

There are two layers.

The SQLite graph stores structured memory:

- `session` nodes: work sessions
- `thread` nodes: ongoing topics
- `concept` nodes: decisions and thinking shifts
- `question` nodes: unresolved questions
- `source` nodes: external/raw material

Edges record relationships:

- `ADVANCED`: session advanced a thread
- `RELATES_TO`: source/concept/question relates to thread
- `EVOLVED_FROM`: new thinking replaced old thinking
- `RESOLVED_BY`: question resolved by a session
- `LOGGED_IN`: concept logged in a session

The LLM Wiki compiles this graph into Obsidian markdown:

```text
11-LLM-Wiki/
  raw/
    chats/
    sources/
    assets/
  wiki/
    current-state.md
    discarded-ideas.md
    open-questions.md
    source-ledger.md
    session-ledger.md
    threads/
  schema.md
```

Agents should read `wiki/current-state.md` and relevant `wiki/threads/*.md`
before searching raw files. Raw files are evidence; compiled wiki pages are the
current source of truth.

## Reliability Model

Always use `./context-safe.sh`, even on your host machine.

The wrapper makes SQLite safe for agent workflows by:

- taking a global lock before touching `context.db`;
- copying the DB to a unique native `/tmp` working directory;
- disabling confidence-cache writes for wrapper-driven reads;
- checking SQLite integrity before and after publish;
- keeping `context_backup.db` and timestamped snapshots in `context_backups/`;
- atomically replacing the canonical DB only after validation passes.

This avoids the common failure mode where multiple agents run memory commands at
the same time and corrupt or overwrite a shared SQLite working copy.

## Commands

```bash
# session lifecycle
./context-safe.sh start_session --title "..."
./context-safe.sh finalize_session --key-context "..."

# graph writes
./context-safe.sh log_decision "..." --threads "thread-a,thread-b"
./context-safe.sh log_shift "..." --thread "thread-a" --from "old" --to "new"
./context-safe.sh log_question "..." --threads "thread-a"
./context-safe.sh create_thread "thread-a" --current-position "..."
./context-safe.sh relate "node-id-1" "node-id-2"
./context-safe.sh resolve_question "question-id" --session "session-id"

# graph reads
./context-safe.sh recent --sessions 14
./context-safe.sh get_context "topic"
./context-safe.sh get_thread "thread-a"
./context-safe.sh open_questions
./context-safe.sh confidence

# wiki/source layer
./context-safe.sh memory_init
./context-safe.sh memory_capture "Title" --kind blog --url "https://..." --threads "thread-a"
./context-safe.sh memory_compile
./context-safe.sh memory_search "term"
./context-safe.sh memory_lint
```

## Relationship To Ar9av/obsidian-wiki

[Ar9av/obsidian-wiki](https://github.com/Ar9av/obsidian-wiki) is a broader
skills framework for many agents, with setup automation, skill packs, history
ingest, manifests, and Obsidian graph enhancements.

This repo focuses on a smaller core:

- a reliable SQLite graph for sessions, decisions, shifts, and questions;
- a compiled Obsidian wiki for current-state reasoning;
- a hardened wrapper designed for concurrent agent sessions;
- no required Python dependencies beyond the standard library.

The projects are complementary: `obsidian-wiki` is a full framework; this is a
portable personal memory kernel.

## License

MIT
