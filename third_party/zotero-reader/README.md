# Zotero reader integration

This application embeds the official Zotero Reader web build at the revision in
`pin.json`. It does not distribute code extracted from an installed application.
The integration host is `src/auto_zettelkasten/web/zotero-reader-host.js`.

## Build and source

Run `python3 tools/build_zotero_reader.py --cache-root /absolute/private/cache`.
The cache must be outside this repository. The script checks exact source and
submodule commits, uses their npm lockfiles, and builds only the upstream `web`
target. `--reuse-build` verifies an existing private build before packaging it.
No paid services or Zotero profile access are used.

The generated runtime assets are under `src/auto_zettelkasten/web/zotero-reader/`.
The manifest records revisions, input lockfile hashes, runtime file hashes and
dependency licenses. `THIRD_PARTY_NOTICES.txt` retains upstream and dependency
license text, including the Apache-licensed PDF.js and its bundled font notices.
Build artifacts, dependencies and the source checkout stay in the private cache.

For corresponding source, distribute this repository's integration source and
build script together with the exact upstream repository and submodule source
archives identified by `pin.json`, their lockfiles and the three locale source
files from the pinned Zotero commit. The cache's `source/` checkout contains those
sources. A distributor must make the complete corresponding source available;
links and build instructions alone do not establish that every distribution
obligation has been satisfied.

## Distribution gate

Zotero Reader is AGPL-3.0. The iframe is an isolation and lifecycle mechanism,
not a licensing exemption. Preserve COPYING, all notices and source offers.
The existing project's license does not override the reader's license. Complete
the combined-application licensing review before distributing this reader build.

## Host contract (version 1)

All messages carry `channel: "az-zotero-reader-v1"` and an `instanceId` supplied
in the host URL fragment. The parent must validate message source, origin and
instance, and the host does the same. The host sends `ready`; parent sends `init`
with `document.url`, `annotations`, optional `location`, `viewState`, `authorName`
and explicit `readOnly`. The URL must be a same-origin attachment endpoint.
The host sends `initialized` only after the upstream reader initializes.

Commands: `annotations` (upserts and `deletedIds`), `read-only`, `navigate`,
`suspend`, `trim-memory`, `operation-result`, and `close`.

Events: `save-annotations`, `delete-annotations`, `view-state`, `open-link`,
`error`, `closed`. Save/delete carry `operationId`; parent must durably enqueue
the operation before returning `operation-result` with matching ID and `success`.
An acknowledgement must mean the write was confirmed, or explicitly describe
failure. An unknown outcome stays pending. Failed writes make the reader
read-only; the parent owns recovery and conflict reconciliation.

The pinned reader has `setSuspended` and `trimMemory`, but no public destroy or
flush method. Its root `initializedPromise` is never resolved at this revision;
the host waits on the primary PDF view's actual initialization promise instead.
The host's close bridge flushes the pinned annotation manager,
waits for pending acknowledgements, suspends pages, destroys each PDF view and
its PDF.js loading task/worker, then emits `closed`. Parent
must remove the iframe to release its document, workers, listeners and canvases.
Do not remove an editable frame before `closed`. The host never writes files or
calls Zotero itself.
