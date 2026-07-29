# Auto-Zettelkasten Desktop and Open-Source Product Idea

**Status:** Future idea; not part of the current core-workflow release

**Recorded:** 2026-07-29

## Opportunity

Auto-Zettelkasten can become a local-first desktop application without requiring
Codex, Claude Code, or another coding harness. The existing Python engine already
performs the necessary orchestration: it reads Zotero, extracts sources, calls a
configured model, maintains indexes and registries, and projects ordinary
Markdown and Obsidian links.

The product opportunity is to preserve an open, inspectable engine while charging
for convenience, automation, collaboration, and managed services.

## Product principles

- The user's Zotero library and generated Markdown remain locally owned.
- Zotero is read-only unless a future feature receives explicit authorization.
- Existing notes remain usable after cancellation.
- The core schemas, graph registry, and export formats remain documented.
- A coding harness is optional, not required.
- Model providers remain replaceable.
- Users may bring their own provider key.
- Managed cloud processing and synchronization are opt-in.

## Possible editions

### Open-source engine

- Command-line mapping.
- Bring-your-own model key.
- Local Zotero access.
- Atomic notes, clusters, indexes, and Obsidian export.
- Manual and incremental runs.
- Public artifact schemas and migration tools.

### Paid desktop application

- Native installer and graphical collection picker.
- Progress, cost, failure, and provenance views.
- Automatic incremental Zotero synchronization.
- Scheduled background mapping.
- Review queues for missing metadata and parked sources.
- Visual relationship and cluster browsing.
- Managed upgrades, migrations, backups, and exports.
- Optional managed provider billing.

### Team or institutional tier

- Encrypted synchronization.
- Shared research libraries and review workflows.
- Team annotations and cluster curation.
- Hosted scheduled processing.
- Research OS integrations.
- Access controls, audit records, and usage reporting.

## Likely architecture

```text
Desktop interface
    ↓
Bundled local Auto-Zettelkasten engine
    ↓
Read-only Zotero connector
    ↓
Configured model API
    ↓
Local workspace and Obsidian vault
```

The desktop interface may wrap the existing engine rather than replace it.
Product work should begin only after the core workflow reliably provides:

- strong one-shot atomic-note generation;
- selective and useful relationship discovery;
- accurate, source-specific cluster synthesis;
- scalable collection and subcollection indexes;
- incremental updates;
- byte-stable unchanged replay;
- robust credential storage; and
- clear recovery from provider or document failures.

## Commercial models to evaluate later

- Fully open-source application with paid support and hosted synchronization.
- Open-core engine with a paid desktop interface and collaboration features.
- Free bring-your-own-key edition plus a subscription with managed model usage.
- Institutional licensing and deployment support.

Licensing, contributor rights, dependency licenses, provider terms, privacy,
support obligations, and unit economics must be reviewed before selecting a
commercial model.

## Current decision

Preserve this as a future product direction. Do not add desktop, billing,
account, synchronization, or licensing work to the next core-workflow release.
