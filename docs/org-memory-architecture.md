# Organization Memory Architecture

This project is moving toward an org-wide work intelligence layer, not just a
personal note system.

The goal is simple: understand what everyone is working on, what changed, why it
changed, what broke, what was learned, and what is blocked without requiring
constant synchronous status meetings.

Git commits are not enough. Most real context lives before the commit:

- agent chats and debugging sessions;
- local branches and forks;
- failed approaches;
- research notes;
- design tradeoffs;
- customer conversations;
- planning docs;
- non-technical execution work.

## Core Model

Each person runs a local memory system. It captures private raw context on their
machine, compiles it into local current-state pages, and publishes selected
work packets into shared org memory.

```text
person local machine
  raw chats / docs / local diffs / research / notes
      ↓
  personal graph + LLM Wiki
      ↓
  publish selected work packets
      ↓
shared org memory
  project pages / people pages / decisions / blockers / questions / research
      ↓
  agents build context packs, digests, and answers for each user
```

The publish boundary matters. Raw personal capture can be broad. Shared memory
should be intentional, synthesized, attributable, and later governed by access
policy.

## Work Packet

A work packet is the atomic shared unit. It should answer:

- What changed?
- Why did it change?
- What alternatives were considered?
- What problems occurred?
- What is still blocked?
- What decisions were made?
- What sources, branches, chats, docs, or commits support this?
- Who should care?

Example shape:

```yaml
type: work_packet
actor: person-id
project: project-id
scope: founders
created_at: 2026-05-26T00:00:00Z
status: active
visibility: shared
sources:
  - local-chat-summary
  - branch:feature/context-cache
  - commit:abc123
tags:
  - backend
  - product
```

## Scopes

Build with future RBAC in mind even if the first version assumes founder-level
trust.

- `private`: local-only, never published by default.
- `shared`: visible to founders/team members.
- `project`: visible to people on a project.
- `org`: visible across the company.
- `public`: safe for external docs or customer-facing material.

The first implementation can treat everything published as `shared`, but every
record should already carry a `visibility` or `scope` field so RBAC can be added
without rewriting the data model.

## Data Objects

Keep the existing graph primitives, then add org-aware metadata:

- `actor`: person or agent who produced the work.
- `project`: product, repo, customer, research area, or operating function.
- `work_packet`: synthesized shareable update.
- `decision`: what was decided and why.
- `blocker`: open constraint or unresolved problem.
- `question`: unresolved question.
- `source`: raw evidence, private or shared.
- `artifact`: branch, commit, PR, doc, deck, spreadsheet, note, research source.
- `access_policy`: future RBAC attachment.

## Shared Views

The shared org wiki should compile these views:

- `org/current-state.md`: what is moving now.
- `org/projects/<project>.md`: project status, decisions, blockers, work packets.
- `org/people/<person>.md`: what a person is working on, recent decisions,
  blockers, and handoff notes.
- `org/decisions.md`: recent durable decisions with owners and sources.
- `org/blockers.md`: active blockers by project/person.
- `org/research.md`: research conclusions and source trail.
- `org/digest/YYYY-MM-DD.md`: daily or weekly digest.

## Agent Behavior

Agents should not expose raw private transcripts by default. They should:

- summarize local work into work packets;
- preserve links to private raw sources locally;
- publish only the synthesized packet and approved supporting artifacts;
- build context packs for each user based on relevance;
- flag stale or contradictory project state;
- ask before promoting private or sensitive material to shared memory.

## Initial Roadmap

1. Add `actor`, `project`, `visibility`, and `scope` metadata to source and
   concept nodes.
2. Add `publish_packet` command that turns local decisions/sources into a
   shareable work packet.
3. Add `org_compile` command that builds shared project/person/digest pages.
4. Add history importers for Claude, Codex, ChatGPT exports, and local git
   branches/forks.
5. Add manifest-based delta ingest so local histories are not reprocessed from
   scratch.
6. Add provenance markers: extracted, inferred, ambiguous.
7. Add RBAC later using the existing `visibility` and `scope` fields.

## Non-Goals For V1

- Full permission system.
- Raw transcript sharing by default.
- Central hosted service.
- Replacing GitHub, Linear, Slack, or docs.

This layer should sit above existing tools and capture the reasoning that those
tools lose.
