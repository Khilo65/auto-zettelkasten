# Auto-Zettelkasten v0.29 Index-Driven Graph Completion, Lean State, and Cost-Aware Operation

## Summary

Release v0.29 as a focused correction to the full-library architecture exposed by the v0.28 evaluation.

The v0.28 source corpus is good enough to preserve. Its audited atomic notes captured 434/435 critical facts and supported 796/806 substantive claims with zero wrong-source notes. The next release must therefore avoid another 5,324-source regeneration and concentrate paid work on the graph and cluster layer.

The central correction is to make the already-successful collection, literature, and virtual-topic indexes drive bounded literature-family planning. v0.28 created 493 collection cards, 126 literature shards, 1,431 virtual topics, and 1,467 virtual-topic shards, but its shared family planner ignored those shards, independently chunked a 4,901-row global index of roughly 1.89 million tokens, and reconciled the result down to 13 families covering only 286 sources. That incomplete plan then bypassed shard-based planning and produced only 12 published clusters covering 215 unique analytical sources.

v0.29 will:

- Preserve successful v0.28 atomic notes and compact profiles.
- Use existing topic, collection, and literature indexes as the routing substrate for family planning, relationship discovery, and cluster synthesis.
- Preserve bounded local family proposals during reconciliation and account for every analytical source before declaring the global map complete.
- Persist valid candidate rows independently from optional packet-level accounting so malformed metadata cannot erase useful model output.
- Remove the unused speculative topic-to-topic reconciliation subsystem and its millions of `related_to` rows.
- Replace multi-gigabyte manifests, duplicate registries, full-profile packets, and verbose CLI output with compact receipts, paths, hashes, IDs, and counts.
- Add exact checkpoint and replay fast paths before parsing thousands of notes.
- Repair strong duplicate identity reconciliation, false source parks, transport recovery, and long-document routing without regenerating successful notes.
- Add a local pre-run cost estimator and incremental resume estimate.
- Add durable, intelligible source and graph progress reporting.
- Treat relationship grounding between 85% and 94% as a pass with advisories when endpoint fidelity, direction, and overall usefulness remain strong.

Release identities:

- Engine: `0.29.0`
- Artifact schema: `1.20`
- Provider-event ledger schema: unchanged at `4`
- Source-bundle prompt: `6`, used only for new or explicitly refreshed sources
- Profile prompt: unchanged at `6`
- Relationship discovery prompt: unchanged at `16`
- Relationship adjudication prompt: `16`
- Literature-family planning prompt: `9`
- Cluster planning and proposal prompts: unchanged at `5` and `17`
- Cluster synthesis prompt: `34`
- Relationship registry and decision contract: unchanged
- Literature algorithm: `37`

No new dependency, database, service, async framework, verifier stage, web-search workflow, Zotero mutation, or mandatory provider-call ceiling will be added.

## Design Principles

### 1. One index system, one graph, one cluster system

Do not add another routing or semantic-index subsystem.

Reuse:

- The canonical source catalogue.
- Zotero collection and subcollection cards.
- Existing `by_literature` shards.
- Existing `by_topic` virtual indexes and bounded shards.
- Compact source profiles.
- Existing full-note relationship adjudication.
- Existing full-note cluster writers.

Indexes remain overlapping navigation structures. They help the system decide what to inspect. They do not automatically establish intellectual relationships or become clusters.

Clusters remain probabilistic scholarly syntheses. A cluster writer must read the complete atomic notes of its retained members and decide the central findings, disagreements, qualifications, boundaries, and relevant cited literature.

### 2. Deterministic code routes and preserves; models judge scholarship

Local code may:

- Reconcile exact identities.
- Build and pack indexes.
- Track source and family coverage.
- Canonicalize IDs.
- Deduplicate exact source pairs and family proposals.
- Enforce context and provider contracts.
- Persist valid response rows.
- Project reciprocal Markdown links.
- Calculate progress, costs, hashes, and replay receipts.

Local code must not decide that two works support, undermine, qualify, extend, contrast with, or belong in the same scholarly debate merely because they share words or tags.

DeepSeek produces the compact profile's intellectual facets from the source. Local code then deterministically normalizes those committed facets, assigns primary and secondary topic memberships, splits oversized shards, and renders indexes. Relationship and cluster outputs may link back to topic indexes but must never mutate topic membership. This prevents downstream model output from feeding back into and destabilizing upstream navigation.

### 3. Completion means accounting, not forced clustering

Every eligible analytical source must end the family-planning stage as one or more of:

- Represented in a provisional literature family.
- Examined and currently unclustered.
- Pending because a specifically identified packet has not completed.
- Ineligible for substantive synthesis because of source scope.

There is no required percentage of sources that must join clusters. Legitimately unclustered sources are expected. A global map is nevertheless incomplete if most sources were never examined because routing, context packing, or reconciliation failed.

### 4. No unnecessary work ceilings

Provider concurrency is an in-flight resource bound, not a total-work limit. Context windows and output capacity remain physical packet constraints, but they must cause packet splitting or continuation rather than whole-map failure.

Default work completion remains unlimited. Optional dollar budgets and explicit emergency controls remain available to users. The system must not silently stop after an arbitrary number of families, candidates, relationships, or clusters.

### 5. Separate bootstrap mapping from incremental additive updates

The first full-library build and later Zotero additions are different operating modes.

#### Bootstrap build

For a previously unmapped library:

1. Freeze the Zotero inventory and reconcile canonical identities.
2. Extract sources and generate every atomic note and compact profile.
3. Reach a source-accounting barrier: every frozen inventory row is terminal as analytical, limited, aliased, or parked, with no active or pending source work.
4. Build the canonical catalogue and deterministic collection, literature, and topic indexes once from the committed profiles.
5. Begin family planning only after the source/profile barrier.
6. Complete relationship discovery and adjudication after family planning inputs are stable.
7. Synthesize clusters from complete atomic notes.
8. Project reciprocal Obsidian links and final indexes once.

Do not generate substantive relationships or clusters while thousands of bootstrap atomic notes are still arriving. Source checkpoints remain incremental for crash recovery, but global semantic mapping begins from a stable source snapshot.

#### Incremental update batch

For an already mapped library:

1. Detect newly added or genuinely changed Zotero records through a cheap identity and metadata delta.
2. Reconcile duplicates against the canonical source registry.
3. Generate all new or changed atomic notes and profiles in one update batch.
4. Reach a batch source/profile barrier with no active or pending batch source work.
5. Deterministically add or update only affected catalogue, collection, literature, and topic-index entries.
6. Plan new-to-new and new-to-existing family and relationship candidates through primary routes, secondary bridge routes, citations, and existing family cards.
7. Reconsider only existing family and cluster artifacts plausibly connected to the batch.
8. Commit new relationship decisions, refreshed clusters, and additive Markdown projections together at semantic stage barriers.

Adding a new collection follows the same batch path: map all its new sources first, then examine connections within the collection and between it and the previously mapped library.

Previous valid relationships and clusters remain visible until a successful additive update or replacement commits. Use `affected_update_set`, `refresh_eligible`, `receipt_mismatch`, or `needs_reconsideration` in user-facing and state terminology. Do not describe an existing scholarly artifact as invalid merely because new material was added.

A transport or budget pause keeps the source or incremental batch stage incomplete and does not silently begin semantic graph work over a partially generated batch. An explicit diagnostic partial-map operation may be supported for recovery, but it remains visibly partial and is not the normal bootstrap or incremental path.

## Implementation Changes

### 1. Delete speculative topic-to-topic reconciliation

Remove the unused lexical topic-pair subsystem from normal and migrated workspaces:

- Delete `_semantic_reconciliation_proposals` and its production callers.
- Stop creating `semantic_review_required` proposal rows.
- Stop embedding reciprocal speculative `related_to` rows inside tag concepts.
- Stop writing `tag_concept_registry.yml` and its canonical map duplicate.
- Remove these artifacts from manifests, reports, compatibility projections, and replay fingerprints.
- Remove tests whose only purpose is to preserve speculative lexical topic relations.
- Retain tests proving that shared lexical wording does not merge concepts automatically.
- Retain public compatibility models such as `TagConcept` only where exported callers still require them; they no longer imply that a production tag-concept artifact exists.

Preserve the independently useful navigation data:

- Source-level typed facets.
- Canonical subject tags.
- Safe aliases created by punctuation, capitalization, whitespace, and other lossless normalization.
- Promoted topic neighborhoods.
- `by_topic`, `by_literature`, and collection indexes.
- Source-to-topic assignments.

Do not replace the deleted subsystem with an on-demand semantic-neighbor framework in v0.29. If later agent-navigation evaluation demonstrates a real need, neighboring topic cards can be compared probabilistically as a separate future feature.

#### Migration and cleanup

Migration from artifact schema `1.19` is local and provider-free:

- Read source profiles, assignments, promoted neighborhoods, topic indexes, and existing semantic graph registries without reading, parsing, or hashing the legacy giant tag-concept registry.
- Build and validate the compact v0.29 navigation state first.
- Record legacy registry paths, sizes, mtimes, and known schema identities in the migration ledger. Reuse a previously stored hash when one exists; do not compute a new hash over the obsolete multi-gigabyte file.
- Retire legacy v0.28 multi-gigabyte `build_map_manifest.yml` files by recognized path and schema without parsing or hashing their embedded payloads.
- Remove only recognized machine-generated speculative registry copies after the compact replacement and migration receipt are durably committed.
- Never remove human-authored Markdown, Zotero data, atomic notes, cluster prose, relationship decisions, or source custody files.
- An interrupted cleanup must be idempotent and resume safely.

### 2. Make existing indexes drive literature-family planning

Replace flat global family planning with bounded shard-led planning.

#### Routing inventory

Create one deterministic planning inventory from the existing virtual catalogue projection and context packers rather than adding a new index implementation. Reuse the current `_virtual_catalogue_projection`, virtual shards, collection shards, literature shards, and the existing `_cluster_plan_packets` / `_pack_source_ids_by_size` measured-size packing paths.

The inventory contains:

- Canonical analytical source IDs.
- Collection and subcollection membership.
- Virtual-topic memberships.
- Literature-shard membership.
- Unfiled and catch-all membership.
- Compact profile hash and scope.

Assign every analytical source one primary family-planning route:

1. Its highest-priority eligible virtual topic.
2. Otherwise its most specific eligible collection or subcollection.
3. Otherwise its literature shard.
4. Otherwise the analytical catch-all.

Secondary topic, collection, and literature memberships remain available for bridge discovery and cross-boundary routing. They do not cause the source's complete compact profile to be repeatedly processed as primary family-planning work.

Persist the selected primary route in the source's planning receipt. During ordinary incremental updates, an existing source keeps its primary route unless its own compact profile changed. If a new source causes a previously singleton facet to become a promoted topic, the new source may use that route and the existing members may appear as secondary or bridge context; do not reassign every old member's primary obligation. Broader primary-route rebalancing belongs to explicit periodic maintenance.

Each source may therefore appear in several navigation indexes without duplicating its atomic note or primary planning obligation. "Each compact source profile exactly once" means once within a packet. Existing virtual-topic assignment limits and context-size splitting remain in force.

#### Packet construction

Treat a shard as a routing job, not an API call. Pack many small labeled shard jobs into each provider request by measured input and expected output size rather than a fixed source count. Deduplicate shared profiles within the packet while preserving each job's source IDs.

Each family-planning packet contains:

- Compact routing cards for its participating shards.
- Each compact source profile exactly once within the packet.
- Collection context only when relevant.
- Existing family cards that overlap the packet, when resuming or incrementally updating.
- Explicit source IDs for which the packet must return an accounting disposition.
- Local complete-note size estimates for downstream family context feasibility.

Do not resend a 4,901-source global spine with every packet. Do not load complete atomic notes during family planning.

Reserve response capacity for every required per-source disposition and family record. A packet that fits only its input but cannot return complete accounting is too large and must be divided before provider invocation.

Run independent packets concurrently. Provider concurrency applies to independent planning packets after their inputs are ready.

#### Family-plan contract

For every supplied source, the built-in reasoner returns one or more of:

- A provisional family assignment with role and compact rationale.
- `currently_unclustered` with a concise explanation.
- A declared cross-packet overlap requiring compact reconciliation.

Persist exact per-source and per-packet accounting:

- Source disposition: `assigned`, `currently_unclustered`, `ineligible`, or `pending`.
- Plan status: `complete` or `partial`.
- Completed and failed packet IDs.
- Unaccounted source IDs.
- Family assignment and supersession lineage.

A source becomes `assigned` when any valid planning packet assigns it. It becomes `currently_unclustered` only after its primary route completes without an assignment. Pending secondary or bridge jobs remain visible separately and do not erase a valid primary disposition. Local source scope determines `ineligible`; DeepSeek does not reclassify source eligibility.

Custom reasoners remain compatible through capability detection. Missing enhanced accounting makes the packet partial, not silently complete.

#### Reconciliation

Reconcile compact family cards, not complete profiles or atomic notes. Reconciliation itself is context-bounded; do not replace one oversized global reconciliation call with another.

Reconciliation may:

- Merge duplicate or near-identical family proposals.
- Preserve overlapping but distinct families.
- Link neighboring family cards.
- Resolve source-role conflicts.
- Recommend a bounded subfamily split when one family would exceed complete-note context capacity.

First reconcile cards connected by shared source IDs in bounded components. Preserve non-overlapping families unchanged. Use the existing probabilistic bridge router to examine intellectually related but source-disjoint family cards; local lexical or collection rules do not merge them.

The union produced by the existing `_merge_literature_family_plans` path is authoritative. Reconciliation must never replace the union of valid local family proposals with a much smaller global list. It may return explicit merge or supersession lineage, but omission from a reconciliation response cannot delete a local family or member. Every local family and source disposition survives until explicitly merged, superseded, or rejected with recorded lineage.

#### Coverage completion

After reconciliation, calculate analytical-source accounting locally.

Route uncovered sources through their existing topic, collection, literature, or catch-all shards. Continue bounded completion until every eligible source is accounted or a specific packet remains pending.

A context-preflight, transport, missing built-in disposition, or provider-contract failure affecting uncovered sources makes the global family plan partial. It must not be converted to an advisory-complete result. A custom reasoner that cannot return complete source dispositions remains usable, but its global plan is partial until local accounting is completed.

Completed and reconciled families may proceed independently to relationship and cluster synthesis while other planning packets remain pending. The global map must remain visibly `partial`, preserve the last-good versions of affected families, and list every pending packet. One malformed planning response must not suppress unrelated valid clusters, but partial work must never be presented as a complete global map.

Exclude explicit `--compare-collection` keys from the global family-planning prompt. Validate them independently and create additive targeted packets only after global routing. They must not organize, bias, or replace the full-library family plan.

### 3. Guarantee cross-collection and cross-subcollection fertilization

Treat Zotero collection membership as routing provenance, never as an intellectual boundary.

#### Mixed-collection planning packets

When a selected topic or literature shard contains sources from several collections or subcollections, its family-planning packet must include the relevant compact profiles from all represented collections. Do not partition a coherent topic into separate collection-only calls merely because Zotero stores the sources separately.

Packet context identifies each source's collection memberships so DeepSeek can distinguish:

- A literature genuinely distributed across collections.
- Different collections examining adjacent parts of the same problem.
- Collection-specific context that should remain separate.
- Sources that bridge otherwise distinct families.

The reasoner is explicitly asked to consider within-collection families, mixed-collection families, bridge sources, and neighboring but distinct families. It is not required to manufacture a relationship or mixed family when the sources do not support one.

#### Scalable cross-boundary routing

Do not compare every collection with every other collection.

Use three bounded routes:

1. Shared topic and literature shards naturally bring together relevant sources from different collections.
2. Compact family and topic cards identify promising cross-family, cross-topic, and cross-collection bridge jobs.
3. Exact Zotero relations, matched citations, and resolved literature positions create high-priority cross-boundary jobs regardless of topic or family admission.

Sources marked `currently_unclustered` remain eligible through all three routes. Explicit `--compare-collection` requests add targeted comparisons after global routing and never replace it.

For the frozen v0.28 library, the complete virtual-topic routing-card catalogue is approximately 291,000 estimated tokens and may be inspected in one bounded DeepSeek routing call. If a future routing-card catalogue no longer fits safely, page the compact cards, preserve each page's shortlist, and reconcile only those shortlisted cards. Reuse the existing bridge-shard selector and context packer; do not add another semantic index or precompute all topic pairs.

#### Cross-boundary ledger

Persist one compact accounting ledger containing:

- Routing job and packet ID.
- Participating topic, literature, collection, and subcollection IDs.
- A canonical source-list path, hash, and count rather than embedded profiles or repeated source arrays.
- Candidate and current-decision IDs with counts and dispositions rather than duplicated rationales or pair payloads.
- Accepted direct and contextual relationship IDs.
- `no_relationship`, `no_more_candidates`, pending, and failure dispositions.
- Mixed-family, bridge-source, and neighboring-cluster outcomes.

This ledger demonstrates that the system examined collection boundaries without requiring a predetermined number of cross-collection links. It also makes it possible to identify collections or topic families that were never reached because of a routing failure.

Do not materialize every possible cross-boundary endpoint combination or duplicate source lists already owned by packet manifests. The ledger is an accounting index over canonical artifacts, not another graph registry.

#### Cluster projection

Cluster membership remains collection-agnostic. A source may be core in one cluster, context in another, and a bridge to a third. Cluster notes expose meaningful neighboring clusters and bridge members when supported by accepted relationships and complete-note synthesis.

### 4. Preserve discovery output and scale relationship work by family

Run relationship candidate discovery inside admitted family and cross-family bridge packets.

- Remove the released global 120-pair work ceiling from the normal full-library path.
- Retain context-bounded packet sizing and adaptive continuation.
- Let each completed family report `no_more_candidates` when appropriate.
- Continue until every admitted discovery job is completed, explicitly empty, or pending with a recorded failure.
- Preserve collection-crossing and topic-crossing provenance for later evaluation and navigation.

Relationship discovery must not depend entirely on cluster-family admission. Preserve separate jobs for:

- Exact Zotero and citation relationships.
- Resolved literature-position matches.
- Cross-family, cross-topic, and cross-collection bridge discovery.
- Sources marked `currently_unclustered` whose indexes or citations identify plausible neighbors.

Validate and persist every candidate row independently from optional packet-level fields such as `job_outcomes`.

- A valid source pair and rationale survive even when packet accounting is missing or malformed.
- Optional accounting defects produce warnings and incomplete job state, not deletion of valid candidates.
- Duplicate pairs merge provenance without duplicating adjudication.
- Previously accepted, contextual, rejected, and no-relationship decisions remain reusable when their endpoint notes, model, prompt, and policy are unchanged.

Adjudication continues to load both complete atomic notes. Adaptive packets are sized by measured context so large notes reduce the number of simultaneous pair jobs without imposing a fixed global cap.

Persist the complete relationship stage before cluster synthesis begins. A later cluster failure cannot erase completed relationship decisions.

#### Incremental packet state

Fingerprint each family-planning and discovery packet from only its upstream inputs:

- Participating source-profile hashes.
- Routing-card revisions.
- Explicit citation and Zotero-relation revisions relevant to the packet.
- Prompt, model, provider, and policy identities.

Persist family proposals, source dispositions, candidates, and completion state per packet. A newly added or changed source places only its primary planning packet, selected bridge jobs, incident relationship decisions, and plausibly connected clusters in the affected update set. Unrelated packets remain reusable; a catalogue-wide revision counter must not make them appear stale.

#### Lightweight current-decision reuse

Load one compact in-memory lookup from the existing relationship registry at the start of discovery. Key it by the canonical unordered source pair and store only the current decision's endpoint profile hashes, prompt, model, provider, policy, type, direction, and disposition.

When discovery proposes a pair:

- Reuse the current decision when both endpoint profile hashes and the decision contract are unchanged.
- Reconsider the pair when an endpoint changed, new citation or Zotero evidence specifically affects it, or the user explicitly requests refresh.
- Adjudicate it when no current decision exists.

Treat relationship-prompt identity as decision provenance rather than a global retirement trigger. Advancing the built-in prompt from `15` to `16` applies to new pairs, pairs in the affected update set, and explicitly refreshed pairs. Existing prompt-15 machine decisions remain active and visible when their endpoint profiles and policy remain valid. Do not mark them `reconciliation_pending` merely because the engine now defaults to prompt `16`. A previous decision becomes inactive only after a completed valid replacement for that same pair, an endpoint identity retirement, a human override, or an explicit reconciliation request.

Do not store every mathematically possible source pair, rescan all old pairs, or parse and rewrite complete relationship history for each lookup. Persist changed current decisions once at the relationship-stage barrier; retain history through existing stable event IDs.

Incremental discovery normally focuses on new-to-new and new-to-existing pairs. An old-to-old pair enters the affected update set only when new mapped material supplies a concrete route—for example, a new source explicitly frames both old works as competing or complementary—or during an explicit maintenance run.

### 5. Improve relationship grounding without another pass

Advance the relationship prompt only to make one general distinction clearer:

> A direct relationship requires the two works to address a sufficiently specific shared proposition. When they illuminate adjacent units, populations, settings, stages, mechanisms, outcomes, or evidentiary questions without directly testing the same proposition, prefer a contextual relationship and state the boundary precisely.

The instruction must remain domain-general. Do not hardcode conflict-specific actors, stages, mechanisms, or examples.

In the same call, DeepSeek self-checks:

- Endpoint ownership.
- Proposition specificity.
- Actor/reference direction when asymmetric.
- Relationship type.
- Scope boundary.
- Consistency between rationale and endpoint evidence.

No verifier call, deterministic semantic judgment, or atomic-note rewrite is added. Optional evidence-anchor IDs and exact locators remain advisory when the visible rationale is grounded in both complete notes.

### 6. Restore cluster breadth while preserving full-note quality

Keep the existing full-atomic-note cluster writer. Change its inputs and completion behavior rather than adding another synthesis layer.

#### Recall-oriented affected-cluster selection

During incremental updates, place an existing cluster in the affected update set whenever the new batch has a concrete plausible route to it, including:

- A proposed core, context, or bridge role.
- An accepted relationship with a retained member.
- A shared central proposition approached through a new method, case, period, population, setting, or evidence base.
- A source-grounded qualification, contradiction, boundary, or extension.
- A cited work or acquisition recommendation that has now been mapped.
- A new family that may connect to the cluster through a bridge source.

Candidate selection optimizes recall rather than requiring proof that the new source will materially rewrite the cluster. The cluster writer makes the final membership and synthesis decision after reading the complete current-member notes and new candidate notes. Batch all additions affecting the same cluster into one refresh job. If the writer retains no change, keep the existing cluster byte-identical.

Derive refresh-eligible cluster IDs from outputs already produced by incremental family planning, accepted relationships, citation/acquisition reconciliation, and bridge-family routing. Deduplicate those IDs locally and schedule at most one refresh job per affected cluster per batch. Do not add another provider call or semantic stage solely to decide which clusters might need refresh.

#### Cluster inputs

For every admitted family:

- Load the complete atomic note for every proposed member.
- Include the compact family card and relevant accepted relationships.
- Include every unresolved important literature position from retained members.
- Include identity-reconciled mapped/unmapped status for cited works.
- Include neighboring family cards, not neighboring families' complete notes, unless they are proposed bridge members.

Family planning must use measured complete-note sizes when finalizing proposed families. Revalidate the aggregate note size after reconciliation so a merge cannot create an impossible cluster packet. If a proposed or merged family cannot fit within the safe provider context with all complete notes, keep it pending and return it through the existing bounded family-planning path for context-sized coherent subfamilies with explicit parent and overlap lineage. Do not split it arbitrarily, add a separate post-planning synthesis stage, or silently send only a subset while claiming to synthesize the full family.

#### Member roles

Require meaningful use of:

- `core`: directly addresses the cluster's central proposition.
- `context`: supplies relevant setting, measurement, background, or adjacent evidence.
- `bridge`: connects the cluster to another literature or family.

Do not require every family member to be retained. Do require every retained member to receive one source-specific contribution and the correct role.

#### Cluster completion

- Publish every valid cluster independently.
- Preserve last-good cluster versions when a refresh fails.
- Keep a failed writer pending and diagnosable rather than calling the whole map complete.
- Settle interrupted transport attempts on resume.
- Do not let one failed cluster suppress relationships or acquisition accounting.
- Preserve reciprocal source-to-cluster and cluster-to-source links.

Use the Fortna peacekeeping case as a regression fixture: relevant local Fortna works must be presented to peacekeeping family planning and assessed by the writer, while the model remains free to retain, bridge, or exclude them with a source-grounded reason.

### 7. Reconcile source identity before paid work and acquisition projection

Use one canonical identity registry across source generation, citation matching, relationships, clusters, and acquisition recommendations.

Before provider work:

- Normalize Zotero keys, DOI, ISBN, canonical URLs, and title/author/year fields.
- Distinguish document-level identifiers from shared container or volume identifiers.
- Merge exact duplicate Zotero records only when the identifier and bibliographic role identify the same work.
- Never merge a chapter with its edited volume merely because they share an ISBN or container DOI.
- Convert confirmed duplicates into aliases pointing to one canonical atomic note.

For incomplete identifiers:

- Generate conservative title/author/year candidates locally.
- Require compatible work type, chapter/container role, authorship, year or edition, and normalized title evidence.
- Automatically merge only unambiguous matches.
- Preserve ambiguous candidates for review without making a paid source call solely to decide identity.

Graph-only identity reconciliation never regenerates source prose. When two existing notes are confirmed duplicates, retain one canonical active note and convert the other source identities to aliases. Preserve superseded generated notes as inactive historical artifacts until ordinary managed projection can safely remove duplicate visibility; never discard human-authored additions.

Before rendering acquisition recommendations, reconcile every cited work against the canonical source registry again. A locally mapped work becomes `map_existing` or a direct link, never `acquire`.

### 8. Handle books and edited volumes without fragmenting the pipeline

Advance the source prompt for newly mapped or explicitly refreshed books. Existing v0.28 atomic notes remain accepted and are not regenerated merely because the prompt identity advances.

Treat the source-prompt identity as generation provenance rather than a global refresh trigger. A committed source bundle created under prompt `5` remains reusable when its source content and schema are valid. Prompt `6` applies only to a newly generated source, a changed source, or an explicit source refresh. A v0.29 graph-only build must therefore accept the frozen v0.28 source and profile corpus without source provider calls.

Classify book-like sources as:

- Authored monograph.
- Edited volume.
- Collected work.
- Book chapter or contribution.
- Partial book or excerpt.
- Uncertain composition.

Default projection remains one Zotero item to one canonical Markdown note.

For an authored monograph, the note contains:

- Book-level thesis, method or knowledge basis, evidence, findings, and limitations.
- A bounded chapter-by-chapter breakdown when chapter boundaries are recoverable.
- Clear separation between book-level claims and chapter-specific claims.

For an edited volume, the note contains:

- Editors' framing and organizing question.
- Table of contents when recoverable.
- Chapter sections identifying chapter title, chapter author, thesis, evidence or method, major contribution, and relation to the volume.
- No false attribution of a chapter author's claim to the editors or the entire volume.

Create a separate chapter atomic note only when the chapter exists as its own Zotero record or has sufficiently reliable independent identity metadata. Otherwise retain stable chapter headings inside the parent note. This avoids multiplying notes or provider calls while preserving later splitability and Obsidian heading links.

The provider may return volume-level and chapter-level structured analysis in one direct or hierarchical source workflow. Models never edit Markdown files directly.

### 9. Add exact checkpoint and profile fast paths

On an exact source, prompt, model, policy, and artifact fingerprint hit:

- Read one compact item receipt or sidecar.
- Verify referenced note/profile existence and stored hashes.
- Return the prior terminal result immediately.
- Do not parse, augment, normalize, save, reload, or rewrite the atomic note.
- Do not route unchanged items through the completion queue.
- Do not rebuild global navigation or catalogue state per item.

Normalize newline representation before frozen-content hashing so CRLF and LF do not create false source changes.

Locally recover stale checkpoints when the normalized content and semantic inputs match. Preserve a migration receipt; make no provider call.

Profile packets and manifests store source IDs, profile hashes, and paths rather than complete profile copies.

Build source navigation and source indexes once before planning, then pass the prebuilt navigation object through literature mapping and projection. Post-cluster projection may attach cluster IDs and proposition-backed typed links, but it must not rerun subject-tag derivation, topic promotion, or virtual-shard construction. Build the source catalogue once and perform only a targeted post-cluster projection update. Project atomic managed blocks once after semantic graph completion.

#### Recover v0.28 operational false parks

Reconcile the known failure classes without broad semantic retry:

- Repair `frozen_content_invalid` rows locally when normalized current content matches the committed semantic source.
- Reclassify provider queue timeout, DNS, 503, premature EOF, and interrupted-stream rows as resumable transport.
- Recursively subdivide oversized coarse chunks instead of treating a 500- to 3,000-page source as terminally unreadable.
- Recover exactly one unambiguous preserved source envelope locally and reapply the complete source-ownership and schema contract.
- Leave genuine wrong-source, ambiguous, content-policy, and nonrecoverable semantic failures parked and inspectable.

The source-recovery operation must not call the provider for previously successful items. Report usable publication yield separately from atomic-note quality.

### 10. Make transport recovery persistent and adaptive

Retain v0.28's independent local and provider executors. Improve provider scheduling rather than introducing another concurrency framework.

Classify transient transport failures explicitly:

- DNS and connection failure.
- HTTP 429, 502, 503, and 504.
- Idle stream.
- Premature EOF or interrupted response.
- Laptop/network interruption.

Transient jobs return to a delayed queue; they do not become semantic parks.

- Use exponential backoff with jitter.
- Do not occupy a provider worker while waiting.
- When failures become provider-wide, pause most new launches and use a small number of health probes.
- Ramp concurrency back up after sustained healthy completions.
- Preserve the immutable request packet and attempt lineage.
- Continue delayed transport recovery until success, user cancellation, or an optional dollar budget pause.
- Never use a hot infinite retry loop.

Use an explicit provider circuit breaker. When a provider-wide failure threshold is reached, stop ordinary launches, mark the run `paused_transport`, and schedule bounded health probes with increasing delay. While the invocation remains alive, successful probes resume the queue automatically. After process or laptop interruption, the next ordinary `resume` continues the same delayed jobs. Status output must show the pause reason and next probe rather than appearing stuck.

Requests rejected before inference and reporting zero usage may retry persistently. Interrupted streams with positive or unknown billable usage remain resumable but are included in the cost estimate and optional spend budget.

Benchmark ready-text provider scheduling at 128, 256, and 512 workers with a deterministic delayed fake provider. Use a small matched paid sample only if transport or source-prompt behavior changes require it. Select the highest automatic concurrency that materially improves sustained throughput without unacceptable 503, premature-EOF, latency, memory, or accounting degradation. Explicit numeric concurrency remains user-controlled up to the provider-declared limit.

Keep OCR local and independently bounded. Add a long-document lane so 500- to 3,000-page documents cannot monopolize ordinary extraction or provider routing. Recursively split oversized chunks rather than parking a whole document after a coarse chunk fails.

#### Finish interruption and accounting guarantees

- Register active HTTP responses and close them during controlled cancellation.
- Cancel or terminate active extraction and OCR subprocesses where the existing extractor exposes a safe handle.
- Stop queue feeding, drain completed prepared results, force ledger barriers, and leave not-started work pending.
- Require Ctrl-C to return within 60 seconds in the real integration path and promptly in local tests.
- Make prepared-result checkpoint creation and coordinator note/tag commit recoverable as one logical transaction: after a crash, resume either finalizes the prepared result exactly once or reuses the completed commit.
- Report cumulative provider attempts and spend for the logical run across every resume, not merely the latest invocation.
- Treat missing usage or pricing as `unknown` or conservatively estimated, never exact `$0`.
- Include `paused_transport` in terminal and progress accounting.

### 11. Replace giant state with compact receipts

Machine state must describe artifacts rather than embed or duplicate them.

#### Canonical ownership

- Store each semantic registry once.
- Map folders reference canonical registries by relative path, revision, and hash.
- Do not duplicate complete navigation, relationship, cluster, or acquisition registries for compatibility.
- Compatibility files contain only the minimal fields required by legacy readers.

#### Build receipts and manifests

The semantic build receipt contains only upstream semantic identities:

- Canonical source/profile-set revision.
- Collection/topic/literature index revisions.
- Human and exact citation-relation revision.
- Provider/model/prompt/policy identities.
- Family, relationship, cluster, and projection revisions.
- Artifact paths, hashes, counts, and byte sizes.
- Completion or partial state with pending job IDs.

For safe replay reuse, the compact receipt also stores an inventory of relevant input paths with size and `mtime_ns`, plus their last semantic hash. Check directory membership to detect added or removed files. If a stat changes, hash and parse only the changed input before deciding whether the semantic build remains reusable.

Never embed the full navigation graph, cluster map, profile catalogue, or provider result in a manifest.

#### Projection

- Compare precomputed content hashes before parsing existing large machine files.
- Write only changed semantic files and managed Markdown blocks.
- Project atomic relationships and cluster memberships once after semantic completion.
- Keep human-authored Markdown byte-identical outside managed blocks.
- Print a concise CLI receipt; write detailed diagnostics to bounded files.

#### Replay

Check the compact semantic receipt before calling `all_workspace_note_rows`, parsing profile YAML, opening every Markdown note, or hashing every generated artifact. Do not enumerate and hash every generated projection file during ordinary replay.

An exact replay returns the prior compact report immediately with zero provider calls, semantic parsing, writes, events, additions, removals, or mtime changes.

Deep integrity verification remains available as an explicit audit operation; it is not performed on every unchanged replay.

### 12. Add cost estimation before paid processing

Add a local command:

```bash
auto-zettelkasten estimate --workspace WORKSPACE --scope library
```

The estimate makes no provider call and reports low, expected, and high ranges.

It identifies whether the proposed operation is a bootstrap build, graph-only rebuild, resume, or incremental update batch. Incremental estimates include only new/changed source work and the predicted affected graph jobs; they must not price the entire mapped library again.

#### Source estimate

Use:

- Canonical source count after exact deduplication.
- Existing reusable notes and checkpoints.
- Metadata-only and limited routes.
- Extracted character or token counts when available.
- Attachment size and page-count ranges before extraction.
- OCR candidate pages.
- Direct versus hierarchical route estimates.
- Historical input, output, reasoning, latency, and retry distributions by route.
- Provider pricing with source and effective date.

Report estimated calls, tokens, cost, local OCR burden, runtime, and uncertainty. Separate provider cost from local OCR time.

#### Graph estimate

After compact profiles and indexes exist, estimate:

- Shard-led family-planning packets.
- Compact reconciliation packets.
- Relationship discovery and adjudication packets.
- Expected cluster writers.
- Incremental calls and cost for a resume.

The estimate is informational. Existing `--max-provider-spend-usd` remains an optional user-controlled pause mechanism, not a mandatory default cap. `--allow-cloud` remains explicit authorization.

Persist the estimate beside the run and compare it with actual usage after completion so future estimates calibrate from real project history.

### 13. Add truthful progress and automatic stage continuation

Normal `map` execution without `--no-synthesis` continues automatically:

```text
source preparation
→ atomic-note generation
→ source health/accounting barrier
→ family and relationship graph
→ clusters and acquisition accounting
→ Obsidian projection
```

If `--no-synthesis` is supplied, completion output must explicitly say that graph generation was intentionally skipped and provide the exact continuation command.

Add a compact append-only stage-event JSONL and a read-only status command. Reuse existing run and progress state rather than adding a daemon or service.

```bash
auto-zettelkasten status --workspace WORKSPACE --run-id RUN_ID
```

Status reports:

- Current phase and invocation ID.
- Operating mode: bootstrap, incremental batch, graph-only, resume, or replay.
- Incremental batch size and affected-update-set counts when applicable.
- Active time, wall time, and interrupted time.
- Source terminal, active, queued, OCR, provider-ready, provider-active, paused-transport, and parked counts.
- Local and provider concurrency and throughput.
- Delayed retry counts and next retry time.
- Families planned and analytical sources accounted.
- Discovery jobs, candidate pairs, adjudications, and accepted relationships.
- Cluster writers planned, active, published, pending, and failed.
- Projection file counts and current artifact.
- Last successful semantic checkpoint.
- Budget or transport pause reason.

Progress counters remain cumulative across resumes. Stale `started` provider events are settled as interrupted during resume. A laptop shutdown cannot preserve the process, but completed checkpoints allow the same command to resume without repeating semantic work.

### 14. Preserve a separate periodic-maintenance boundary

Normal incremental updates do not globally rescan old-to-old pairs, every currently unclustered source, or every established cluster.

Preserve compact receipts and current-decision memory so a future explicit maintenance operation can reconsider:

- Long-unclustered sources.
- Old relationships surfaced by later literature.
- Newly resolvable citations and acquisition recommendations.
- Accumulated pending cluster additions.
- Cross-family bridges that emerge after substantial library growth.

Do not add a new maintenance command or scheduler in v0.29. Implement it only when incremental evaluation demonstrates a concrete need. Any future maintenance operation must be separately cost-estimated, graph-only unless source content changed, and explicitly requested or scheduled—not triggered after every addition.

## Interfaces and Compatibility

Externally visible additions:

- `auto-zettelkasten estimate`
- `auto-zettelkasten status`
- Source-bundle support for book composition and chapter breakdowns.

Existing interfaces remain compatible:

- `map`, `resume`, and `build-map` retain their meanings.
- `map` uses bootstrap mode when no completed canonical graph receipt exists and incremental mode when a completed graph exists plus a Zotero/source delta; it records the selected mode explicitly.
- `build-map` remains the graph-only path over already committed notes and profiles.
- Existing numeric and automatic provider concurrency remain operational settings, not semantic fingerprints.
- Existing optional dollar budgeting remains available.
- Existing custom reasoners remain compatible through capability detection.
- Existing v0.28 notes and profiles remain valid for graph-only builds.
- An engine or source-prompt version advance alone does not regenerate a committed source bundle; source content change or explicit refresh remains required.
- Zotero remains read-only.

Public API compatibility does not require old artifact readers to consume v0.29 state blindly. Advance the artifact schema and update internal readers to resolve canonical registry paths from compact pointer files. Do not duplicate full arrays merely to preserve the physical layout of schema `1.19` artifacts.

Migration is local, lazy, idempotent, and provider-free. No existing atomic prose or human-authored Markdown is rewritten merely because the artifact schema advances.

## Tests

### Bootstrap and incremental lifecycle

Add tests proving:

- Bootstrap relationship and cluster calls cannot begin before the frozen source/profile accounting barrier.
- Source checkpoints may complete individually without repeatedly rebuilding global semantic indexes.
- A batch of new sources generates all new notes before incremental family, relationship, or cluster work begins.
- Adding a new collection considers new-to-new and new-to-existing relationships in one incremental batch.
- Deterministic catalogue and index projection rewrites only files affected by committed new profiles.
- Unchanged family packets, current relationship decisions, clusters, and atomic prose remain reusable.
- Old-to-old pairs are not globally rescanned during an ordinary incremental update.
- A new source that explicitly connects two existing works may create a targeted old-to-old candidate.
- Recall-oriented affected-cluster selection considers plausible core, context, bridge, boundary, and acquisition-resolution routes.
- Several additions affecting one cluster produce one batched refresh job.
- A refresh retaining no new member leaves the existing cluster byte-identical.
- The last-good cluster remains visible until a valid replacement commits.
- A single-source delta never causes a full-library semantic rebuild or global projection rewrite.

### Navigation and state deletion

Add tests proving:

- Topic indexes and source assignments remain identical after speculative reconciliation is removed.
- Identical committed compact profiles generate byte-identical topic indexes without a provider call.
- A changed compact profile rewrites only affected topic shards and indexes.
- Safe normalization aliases remain available.
- No production path calls `_semantic_reconciliation_proposals` or emits speculative `related_to` rows.
- No tag-concept registry is written at root or map scope.
- No legacy giant registry or build manifest is parsed or newly hashed during migration.
- Legacy generated registries are ignored during planning and safely cleaned only after compact migration state commits.
- Manifests contain paths, hashes, counts, and revisions rather than embedded semantic payloads.
- CLI completion output remains bounded.

### Index-led family planning

Add fixtures for:

- Organized nested collections.
- A completely flat root library.
- Overlapping virtual-topic memberships.
- Unfiled sources.
- Oversized topic shards split by context size.
- Explicit collection comparisons remaining additive.
- Local family proposals surviving reconciliation.
- Local families and members surviving omission from a reconciliation response.
- Reconciliation merging duplicates without discarding coverage.
- Coverage completion routing only uncovered sources.
- A failed completion packet leaving the map partial.
- A partial family plan preventing cluster calls only for affected families while unrelated completed families proceed and the global map remains partial.
- Every eligible analytical source receiving a disposition.
- Topic indexes informing family planning without automatically becoming clusters.
- Every analytical source receiving exactly one primary planning route.
- Existing primary routes remaining stable when a different source promotes a new shared topic.
- A newly promoted topic exposing old members as secondary context without reassigning their primary obligations.
- Secondary memberships remaining available for bridge discovery without duplicating primary obligations.
- Many tiny shard jobs sharing one context-bounded provider packet.
- Profiles shared by several jobs appearing once in the packet source table.
- Packet bounds reserving both input and required disposition/family output capacity.
- Reconciliation operating on bounded shared-source components and preserving unrelated families without a provider call.
- The compact topic-card router using one call when it fits and paged shortlist reconciliation when it does not.
- Downstream relationship or cluster changes never mutating deterministic topic membership.

### Discovery and relationships

Add tests proving:

- Valid candidate rows survive missing or malformed optional `job_outcomes`.
- Candidate provenance survives deduplication, resume, and replay.
- No global candidate ceiling suppresses completed families.
- Continuation excludes previously returned pairs.
- `currently_unclustered` sources remain eligible for citation, topic-neighbor, and bridge relationship jobs.
- Per-packet fingerprints reuse unchanged family and discovery work across run IDs.
- Adding one source places only its primary packet, selected bridge jobs, incident relationships, and plausibly connected clusters in the affected update set.
- Complete relationship decisions persist before cluster work.
- Direct versus contextual classification respects general analytical-scope differences.
- No conflict-specific vocabulary is required by the prompt.
- Direction, endpoint ownership, reciprocal projection, and unique active pair state remain correct.
- Advancing the default relationship prompt does not hide, retire, or mark unchanged prompt-15 decisions pending.
- A completed prompt-16 replacement supersedes only its own prior pair decision.

### Cross-collection fertilization

Add tests proving:

- A topic shard containing several collections produces a mixed planning packet rather than collection-isolated packets.
- Collection membership is preserved as provenance but never blocks a valid candidate pair or family assignment.
- Shared-topic routing, compact bridge-card routing, and exact citation/Zotero routing all operate independently.
- The system does not enumerate every possible collection pair.
- Explicit collection comparisons remain additive to the global plan.
- `currently_unclustered` sources remain eligible for cross-collection bridge discovery.
- Every cross-boundary job receives a completed, empty, pending, or failed ledger disposition.
- Cross-boundary ledgers reference canonical source lists and decision IDs without embedded profiles, rationales, or all-pairs matrices.
- A valid response containing no meaningful cross-collection relationship is accepted without manufacturing one.
- Mixed families retain sources from all supported collections through reconciliation.
- Bridge sources and neighboring clusters project only from accepted full-note judgments.

### Clusters and acquisition

Add tests proving:

- Every cluster writer receives all retained complete atomic notes.
- Oversized families split before writing rather than silently dropping members.
- Reconciliation-created oversized families remain pending for the existing bounded planning path rather than arbitrary local splitting.
- Core, context, and bridge roles remain distinct.
- Every retained member has a specific contribution.
- One failed writer does not erase other clusters or relationship decisions.
- Refresh-eligible cluster IDs derive from existing incremental outputs without another provider call and deduplicate to one writer job per cluster per batch.
- Fortna's mapped peacekeeping sources reach the relevant family packet and writer.
- Existing local works cannot project as `acquire` after identity reconciliation.
- Acquisition accounting remains complete when a writer fails.

### Identity and books

Add fixtures covering:

- Exact duplicate Zotero records.
- Shared book ISBNs without chapter-volume false merges.
- DOI-bearing chapters and edited volumes.
- Title/author/year variants.
- Editions and translations.
- Authored monographs.
- Edited volumes with recoverable chapter authors and titles.
- Partial books and excerpts.
- Separate chapter Zotero records linking to their parent volume.
- No attribution of chapter findings to editors or the entire volume.

### Checkpoints, transport, cost, and progress

Add tests proving:

- Exact checkpoint replay reads only a compact receipt.
- CRLF/LF normalization repairs false content changes.
- Exact profile hits perform no note rewrite or checkpoint rewrite.
- DNS, 503, 429, idle, and premature-EOF failures enter delayed transport recovery.
- Waiting retries occupy no provider worker.
- Provider-wide failures reduce launch permits and later recover.
- User cancellation persists completed work and leaves delayed jobs resumable.
- Active HTTP responses and cancellable OCR/extraction subprocesses close on Ctrl-C.
- Prepared-result and coordinator commits finalize exactly once after an interruption at either boundary.
- 128-, 256-, and 512-worker fake-provider runs produce identical semantic results.
- Cost estimation uses canonical jobs, route-specific history, current pricing metadata, and incremental resume state.
- Cumulative attempts and spend survive resume; unknown cost is never reported as exact zero.
- Actual sample cost is reconciled back into estimator history.
- Progress remains cumulative and truthful across interruption and resume.
- `paused_transport` is included in progress and terminal accounting.
- `--no-synthesis` reports graph generation as intentionally skipped.

### Performance and replay

Add local full-corpus benchmarks proving:

- Navigation is built once.
- Subject-tag derivation and topic promotion are invoked exactly once per semantic build.
- Catalogue is built once.
- Atomic managed blocks are projected once.
- Profile packets contain IDs and hashes rather than complete profiles.
- No manifest, registry duplicate, or CLI log grows in proportion to a recursively embedded full graph.
- Exact graph replay performs zero provider calls, semantic parsing, writes, events, file additions/removals, byte changes, and mtime changes.
- An added, removed, or changed semantic input makes the prior compact receipt non-reusable and reparses only the affected inputs before fallback.
- Exact replay can be tested with note, YAML, and provider loaders configured to fail if invoked.

Run the complete pytest suite, Ruff, package build, migration tests, and deterministic performance fixtures with no regressions.

## Cost-Conscious Evaluation

Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/full-zotero-v029-index-graph-evaluation-20260805`

Use isolated copies of the frozen v0.28 workspace. Preserve the original evaluation unchanged.

### Stage 1: Local state and migration benchmark

Make zero provider calls.

Measure before and after:

- Workspace bytes excluding source custody.
- Largest generated state files.
- Navigation build time.
- Exact profile preflight time.
- Projection time.
- Replay time.
- Files parsed and written.
- Peak memory.

Acceptance:

- Existing topic indexes and memberships remain complete.
- Speculative topic-pair artifacts are absent from the v0.29 projection.
- Navigation and manifest state shrink by at least 95% from the v0.28 generated-state baseline.
- No manifest or duplicated registry approaches multi-gigabyte size.
- Exact source/profile preflight completes in minutes rather than 38–49 minutes.
- Projection completes in minutes rather than 1 hour 47 minutes.
- Exact replay completes without loading all notes and makes zero calls or writes.
- Strong v0.28 duplicate groups are reconciled or explicitly marked ambiguous with zero false merges.
- Locally repairable newline/hash and envelope parks recover without provider calls.

### Stage 2: Optional paid source sample

Run only if implementation changes source prompting, provider scheduling, or long-document behavior in a way local tests cannot validate.

Use a deterministic 50–100-source sample stratified across:

- Ready extracted text.
- OCR.
- Long reports and books.
- Previously interrupted transport cases.
- Previously locally recoverable cases.
- Authored monographs.
- Edited volumes and chapters.

Do not regenerate successful ordinary articles solely for comparison.

Acceptance:

- At least 95% usable terminal results.
- Zero wrong-source notes.
- Previously successful semantic content remains equivalent.
- Book and chapter authorship and scope are correct.
- No semantic retry.
- Transport failures remain resumable.
- The actual cost falls within the estimator's low-to-high range; target expected-value error within 25% for this first calibrated release.

Stop if source quality regresses. Do not start a full source rerun.

### Stage 3: Topic-shard graph sample

Select 20–30 diverse promoted topic, collection, and literature shards covering approximately 300–500 frozen analytical notes.

Include:

- Large and small topics.
- Nested and flat collections.
- Overlapping topics.
- Unfiled sources.
- Several domains beyond peace and conflict research.
- Mediation and Conflict Relapse as two comparison collections, not the global organizing frame.

Run family planning, discovery, adjudication, cluster synthesis, acquisition reconciliation, projection, and replay using the frozen notes.

Freeze a cross-boundary audit manifest before reading generated results. It records the collections and subcollections represented in each selected shard, a blind sample of plausible cross-collection candidates, exact Zotero/citation crossings, and expected opportunities for mixed families or bridge roles without prescribing final relationship types.

Before the initial sample build, deterministically reserve 20–30 otherwise eligible frozen notes as an incremental delta. Build the sample graph from the remaining notes, then add the reserved notes in one batch using their already generated atomic notes and profiles. This exercises incremental family, relationship, cluster, index, projection, cost-estimate, and replay behavior with zero source-generation calls.

Acceptance:

- Every selected analytical source receives a planning disposition.
- Every selected analytical source enters primary family planning exactly once; secondary appearances are attributable to selected bridge jobs.
- Small shard jobs are co-packed, and provider call count does not scale one-for-one with topic count.
- Every selected analytical source is supplied to an accountable family-planning packet; `currently_unclustered` remains a valid outcome after examination.
- No valid local family or candidate row is lost through reconciliation, optional packet metadata, persistence, or resume.
- Blind candidate usefulness at least 80%.
- Blind cross-collection candidate usefulness at least 80% when the eligible sample contains at least ten pairs.
- Every selected cross-boundary routing job is accounted, including valid `no_relationship` and `no_more_candidates` outcomes.
- No collection boundary prevents an exact Zotero or resolved citation relationship from reaching the graph.
- Relationship endpoint fidelity 100%.
- Direct type and direction at least 90%.
- Fully grounded direct relationships at least 85%; 85–94% is pass with advisories.
- Contextual usefulness at least 85%.
- Cluster membership relevance at least 90%.
- Cluster claim support at least 95%.
- Every retained member has a specific contribution and a meaningful core, context, or bridge role.
- Failed packets remain isolated, pending, and resumable.
- Replay is zero-call and zero-write.
- The incremental delta makes zero source/profile calls and performs semantic work only for its primary packets, selected bridges, new relationships, and affected clusters.
- Unrelated baseline families, relationships, clusters, and projections remain byte-identical.
- New-to-new and new-to-existing candidates are considered; old-to-old adjudication occurs only with a recorded new-evidence route.
- The incremental cost estimate covers the actual graph cost range.

If planning breadth or candidate preservation fails, stop and repair it before the graph-only full-library run.

### Stage 4: Graph-only full-library rebuild

Reuse every valid v0.28 atomic note and compact profile. Source/profile provider calls must remain zero.

Run IDs:

- `eval-v029-global-graph-20260805`
- `eval-v029-global-replay-20260805`

Before paid graph work, write and display the v0.29 graph cost estimate. Do not impose a fixed literature-call ceiling. The optional dollar budget may be used only if the user explicitly chooses it.

Evaluate:

- Complete analytical-source planning accounting.
- Topic, literature, collection, and unfiled-source breadth.
- Mixed-collection families, bridge-role sources, neighboring-cluster links, and collections represented in candidate discovery.
- Cross-boundary job accounting and collections or subcollections never reached.
- Primary-profile submission count, secondary bridge amplification, shard jobs per packet, packet input/output utilization, and reconciliation component sizes.
- Full-library 30-family benchmark coverage.
- Historical Mediation–Relapse family and pair metrics descriptively.
- Blind candidate usefulness and candidate retention.
- Every current direct relationship and a deterministic contextual sample.
- Every mixed cluster and a stratified sample of remaining clusters.
- Fortna peacekeeping inclusion assessment.
- Berg citation resolution.
- Acquisition identity and action accuracy.
- Reciprocal relationships and cluster memberships.
- Runtime, provider calls, tokens, cost, interruptions, delayed retries, state size, projection, and replay.

Acceptance:

- 100% analytical-source family-planning accounting.
- Every analytical source has one completed or explicitly pending primary planning route, with no duplicate primary submission.
- Small topic jobs are co-packed; API call growth follows packed source/family volume rather than raw topic count.
- No systematic collection or topic omission.
- Every eligible cross-boundary job has a terminal or explicitly pending disposition.
- Blind cross-collection candidate usefulness at least 80%.
- Exact resolvable Zotero and citation relationships cross collection boundaries with 100% recall after canonical alias resolution.
- At least 24/30 eligible full-library benchmark families covered.
- No arbitrary clustered-source percentage or cluster-count threshold.
- Every planned valid cluster publishes or has a specific isolated pending failure.
- Zero valid candidates lost through deterministic filtering, contract metadata, or persistence.
- Zero duplicate-active source pairs.
- Relationship endpoint fidelity 100%.
- Direct type/direction at least 90%.
- Direct grounding at least 85%, with 85–94% reported as advisory rather than failure.
- Contextual usefulness at least 85%.
- Cluster membership relevance at least 90%.
- Cluster claim support at least 95%.
- Reciprocal accepted relationships and cluster memberships 100%.
- Acquisition identity and action accuracy at least 95%.
- No generated multi-gigabyte manifest, duplicated registry, or CLI log.
- Projection and local reconciliation complete in minutes, with at least an 80% wall-time reduction from v0.28.
- Exact replay makes zero provider calls, semantic parses, writes, new events, additions, removals, byte changes, or mtime changes.

Export the stable result to:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/full-zotero-v029-obsidian-vault-20260805`

## Evaluation Verdicts

Use only:

- **Pass:** Core integrity, completion, breadth, quality, and replay requirements succeed.
- **Pass with advisories:** The system is operationally complete, with isolated relationship-label nuance, grounding between 85% and 94%, approximate locators, optional evidence-anchor gaps, legitimate unclustered sources, or isolated recoverable transport failures.
- **Fail:** Wrong-source output, false identity merges, systematic relationship distortion, incomplete family/source accounting, discarded valid candidates, broad family-planning collapse, materially unsupported cluster claims, repeated completed work, replay calls or writes, or multi-hour/multi-gigabyte local-state behavior.

Do not fail merely because:

- Some sources remain unclustered after genuine examination.
- Optional evidence-anchor arrays are empty while rationales remain source-grounded.
- Exact locator precision is imperfect.
- An isolated relationship label is debatable.
- An exact historical benchmark pair is missed while family breadth and blind usefulness pass.

## Deliverables

Write:

- `evaluation/v029-index-graph-comparison.md`
- Machine-readable source-repair, identity, bootstrap, incremental-update, index, family, cross-collection, discovery, relationship, cluster, acquisition, cost, runtime, state-size, progress, and replay metrics.
- Before/after artifact-size and stage-runtime tables.
- Frozen sample and benchmark manifests.
- Cost estimate versus actual reconciliation.
- Representative topic-index routing paths, clusters, relationships, identity resolutions, and failures.
- A v0.19–v0.29 comparable trend table, with the v0.28 full-library baseline clearly separated from earlier two-folder evaluations.
- A private Obsidian export with a home index, functional reciprocal graph links, collection navigation, topic navigation, and cluster navigation.

## Assumptions

- The v0.28 atomic-note corpus is accepted as the primary frozen source layer for graph remediation.
- No complete 5,324-source regeneration will be run in v0.29.
- The source prompt change applies only to new or explicitly refreshed book-like sources.
- Book/volume enhancement is secondary and cannot block the graph/state release or force regeneration of the frozen corpus.
- The speculative topic-pair registry has no required downstream consumer and will be removed rather than redesigned.
- Existing topic indexes remain navigation artifacts and become family-planning inputs; they do not automatically become clusters.
- Cross-collection accounting proves that boundaries were examined; it does not impose quotas or require unsupported relationships.
- Bootstrap graph generation begins only after the frozen source/profile barrier; ordinary incremental graph updates begin only after the new-source batch barrier.
- Incremental updates are additive and affected-set based; they do not globally rescan old pairs or rewrite unchanged clusters.
- Periodic global maintenance remains explicitly deferred from v0.29.
- Zotero remains read-only.
- Evaluation benchmarks never enter model prompts or production ranking.
- Provider use remains private and requires explicit cloud authorization.
- Optional dollar budgeting is user-controlled; there is no default work ceiling that can prematurely truncate the global map.
