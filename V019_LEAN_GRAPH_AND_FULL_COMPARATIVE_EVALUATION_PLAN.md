# Auto-Zettelkasten v0.19 Lean Graph and Full Comparative Evaluation Plan

**Status:** Implementation and evaluation complete

**Date:** 2026-07-30

**Evaluated implementation:** `ec95320cf2341461ca7290b35c3be8dd5bb1e781`

**Evaluation:** `/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v019-evaluation-20260730/evaluation/v019-full-comparison.md`

**Foundation:** Engine `0.18.0`, artifact schema `1.14`, relationship registry
schema `6`, relationship decision contract `relationship-decision-v5`, source
prompt `5`, relationship prompt `8`, and cluster-synthesis prompt `27`

## 1. Objective

v0.19 will make the smallest upstream changes needed to address the remaining
v0.18 failures, then run a fresh end-to-end evaluation on the same Mediation
and Conflict Relapse collections.

The release has five objectives:

1. Increase cross-literature bridge recall without weakening final
   relationship judgment.
2. Make relationship responses easier for DeepSeek to complete and more
   precise about proposition, type, direction, and evidence.
3. Preserve the strong v0.18 cluster architecture while tightening only
   cluster-wide and quantitative claims.
4. Fix the two confirmed local defects: the false Obsidian apostrophe warning
   and the overbroad `Introduction` scope heuristic.
5. Prove the complete source-to-Obsidian workflow in one fresh 195-source run
   and compare it with every evaluation from v0.10 through v0.18.

Atomic-note generation is treated as mature. v0.19 will not redesign the
atomic-note prompt, add sentence-level verification, or introduce semantic
retry loops.

## 2. Evidence from v0.18

v0.18 established several foundations that must be preserved:

- all 825 local tests passed;
- frozen atomic notes and profiles received zero semantic changes;
- 15/20 non-benchmark bridge candidates were worth full-note examination;
- relationship type and direction reached 53/63, or 84.13%;
- contextual-link usefulness reached 13/15, or 86.67%;
- all 93 relationship jobs were accounted for before clustering;
- all 11 planned clusters were published;
- cluster membership relevance was 65/65;
- cluster claim support reached 109/114, or 95.61%;
- cluster debate and boundary accuracy reached 54/60, or 90%;
- the graph build completed in 18 minutes 55 seconds; and
- identical replay made zero provider calls and changed no bytes or mtimes.

The remaining gaps were:

- eligible bridge-candidate recall was 12/39, or 30.77%;
- eligible useful final-bridge recall was 9/39, or 23.08%;
- the dedicated bridge call returned only 28 of its 48 available candidates;
- 15/93 relationship jobs were parked:
  - 11 invalid decision contracts;
  - three omitted response rows; and
  - one evidence anchor placed under the wrong endpoint;
- all-direct type and direction missed the 85% threshold by one judgment;
- fully grounded direct relationships reached only 49/63, or 77.78%;
- several cluster-wide quantifiers and statistical comparisons remained too
  broad; and
- the Obsidian missing-link audit reported two false failures for apostrophes
  serialized as doubled quotes in YAML frontmatter.

The evidence indicates that the remaining graph problem is not poor atomic
notes or excessive candidate noise. It is insufficient discovery coverage and
an unnecessarily burdensome relationship response contract.

## 3. Settled design decisions

1. The standalone Python pipeline remains the production orchestrator.
2. DeepSeek `deepseek-v4-flash` remains the generation and reasoning model.
3. The source prompt remains version `5`.
4. Atomic notes, profiles, relationships, clusters, and projections remain
   separate one-way layers.
5. Models never edit Markdown files.
6. Complete atomic notes remain the evidence for final relationship
   adjudication and cluster synthesis.
7. Compact collection indexes and profiles remain the discovery layer.
8. Deterministic code may route, validate IDs, normalize structure,
   deduplicate, persist, and project. It may not decide intellectual
   relationships.
9. No verifier call, semantic retry loop, heuristic JSON repair, or new
   cluster stage will be added.
10. Unclustered sources remain neutral and eligible for future clusters.
11. Cluster coverage remains descriptive, not an acceptance threshold.
12. Locator accuracy remains advisory unless locators are broadly fabricated
    or unusable.
13. Zotero remains read-only.
14. Existing human-authored notes and links remain untouched.
15. Unchanged replay must remain zero-call and zero-write.

## 4. Release identities and compatibility

Release as:

- engine `0.19.0`;
- artifact schema `1.14`;
- relationship registry schema `6`;
- relationship provider-decision contract `relationship-decision-v6`;
- source prompt `5`;
- relationship prompt `9`; and
- cluster-synthesis prompt `28`.

Keep public APIs and CLI options unchanged.

The v6 provider contract will be normalized into the existing registry
representation. Existing v5 custom-reasoner responses remain accepted through
capability and contract detection. Prompt-v8 machine relationships are
reconciled under prompt v9; human and structural relationships remain active.

No workspace-wide migration, Zotero mutation, or third-party dependency is
required.

## 5. Implementation

### 5.1 Keep the source layer stable and fix only confirmed scope logic

Do not change atomic-note generation, source prompt identity, note structure,
statistical-explanation instructions, or fidelity policy.

Make one scope-classification correction:

- weak attachment labels such as `Introduction`, `Foreword`, and `Preface`
  imply an excerpt only when the attachment is plausibly short, with a default
  ceiling of 100 recovered pages;
- explicit chapter, appendix, or excerpt evidence remains authoritative
  regardless of length; and
- a fully recovered 337-page report such as *Pathways for Peace* cannot be
  classified as an introduction merely because that word appears in an
  attachment label.

Do not add a tolerant JSON rewriter for the two v0.17 malformed source
responses. Their raw outputs are genuinely ambiguous or syntactically invalid.
Preserve the raw response, completion metadata, and precise terminal reason.
A fresh v0.19 provider response may succeed, but invalid output must still park
safely rather than be guessed into a note.

Continue writing Zotero metadata and item-type problems to a private
remediation ledger. Do not edit Zotero automatically.

### 5.2 Replace bridge discovery with two lean, complementary views

Keep the existing general discovery call and the existing 72-general,
48-bridge, and 120-total capacities.

Replace the single bridge call with two concurrent, complementary bridge calls:

1. a Mediation-led call that treats Mediation sources as focal works and asks
   which Conflict Relapse works provide the most useful comparisons; and
2. a Relapse-led call that reverses the focal direction.

These are discovery views, not two adjudications of the same relationship.
Their results are merged, canonically deduplicated, ranked, and capped at 48
cross-collection candidates before full-note adjudication.

Each bridge packet must:

- include compact collection-index cards;
- include each compact source profile exactly once;
- include title, author, year, collection, thesis, method or knowledge basis,
  scope, and bounded facets;
- include only resolved cross-collection literature-position matches;
- exclude complete atomic notes and evidence-anchor payloads;
- target 24–32 concrete candidates per orientation;
- limit repeated use of one focal source so that a few prominent articles do
  not consume the pool; and
- search across theoretical, mechanistic, empirical, institutional,
  implementation, outcome, sequence, and boundary connections.

Replace the existing 13-field candidate row with a lean provider shape:

```yaml
left_source_id: source-zotero-...
right_source_id: source-zotero-...
comparison_proposition: ...
why_compare: ...
bridge_family: ...
rank: 1
```

General discovery may use the same lean shape with a discovery-family field.
Local code supplies canonical ordering, route, cross-collection status, and
other bookkeeping already known from the job.

For larger libraries, retain the existing hierarchical router:

1. select relevant top-level collection or subcollection combinations from
   compact routing cards;
2. inspect only their compact profile shards;
3. place multiple selected collection-pair jobs into the two orientation
   packets; and
4. preserve the global 48-bridge and 120-total caps.

The number of bridge calls is bounded by context-sized packets, not by every
possible pair of Zotero folders. No all-pairs folder scan is introduced.

Deterministic code may reject unknown IDs, same-folder rows in the bridge pool,
duplicates, exclusions, and over-cap rows. It must not invent replacement
candidates or judge whether a proposed comparison is intellectually valid.

### 5.3 Use a lean keyed relationship-decision contract

Continue adjudicating candidates with both complete atomic notes. Preserve
adaptive packets of up to 30 pair jobs and the current token-aware packing.
Each atomic note appears once per request; pair jobs refer to source IDs.

Replace the provider response with one exact keyed envelope. `decisions` is an
object whose keys are the supplied job IDs:

```yaml
decisions:
  relationship-job-...:
    decision: relationship
    relation_type: qualifies
    actor_source_id: source-zotero-...
    reference_source_id: source-zotero-...
    comparison_proposition: ...
    reason: ...
    boundary_or_qualification: ...
    left_evidence_anchor_ids: [...]
    right_evidence_anchor_ids: [...]
    confidence: 0.82
```

The model no longer returns fields already known or safely derived locally:

- canonical pair ordering;
- relationship tier;
- forward display label;
- inverse display label;
- discovery route; or
- duplicated endpoint metadata.

For `no_relationship`, require only the decision, concise reason, and
confidence beneath the job-ID key.

Directional relations such as `supports`, `undermines`, `qualifies`, and
`extends` require actor and reference IDs. Types already defined as symmetric
by the relationship vocabulary may omit direction; local code stores them in
canonical endpoint order, writes canonical-left and canonical-right into the
registry's required actor/reference fields, and derives reciprocal labels.

Local normalization may:

- attach the canonical pair from the job ID;
- derive relationship tier and reciprocal display labels;
- assign returned anchor IDs to the endpoint that actually owns them;
- ignore unknown anchors with an advisory warning; and
- accept a row only when each required endpoint retains at least one known,
  endpoint-owned anchor.

Log every anchor that is re-partitioned or dropped.

This is structural normalization, not intellectual inference. Local code must
not infer a missing actor or reference for a directional relation, choose a
relation type, rewrite a rationale, or upgrade a contextual connection.

Continue parking omitted rows, ambiguous job IDs, unknown endpoints, and
semantically incomplete directional decisions. Do not make a paid retry.

### 5.4 Replace overlapping relationship instructions with one decision ladder

Relationship prompt v9 should replace redundant definitions rather than append
more rules.

The same-call reasoning sequence is:

1. Identify the bounded proposition, question, mechanism, outcome, or sequence
   that makes the two works worth comparing.
2. If they share only a broad topic, return `no_relationship`.
3. If joint reading is useful but the works examine different propositions,
   stages, outcomes, methods, or cases, use a contextual relation.
4. Use a direct relation only when the evidence supports a claim about the
   same bounded proposition or an explicit intellectual lineage.
5. Use:
   - `supports` for compatible evidence on the same proposition;
   - `qualifies` for a condition, limit, subgroup, or boundary;
   - `undermines` for evidence that weakens the reference claim;
   - `contrasts` for incompatible findings or claims on a sufficiently
     comparable proposition;
   - `extends` only for explicit building on, testing, refining, applying, or
     generalizing the reference work; and
   - `complements` for distinct contributions to the same bounded question.
6. Citation, chronology, dataset reuse, coding reuse, or thematic proximity
   alone does not establish support or extension.
7. State the relevant claim from each work and explain why the chosen type and
   direction follow.
8. Treat a partial or limited note only as evidence for what its available
   content explicitly establishes.
9. Before returning, confirm that every pair job has exactly one output row.

DeepSeek remains solely responsible for the substantive judgment. There is no
second verifier and no deterministic semantic veto.

### 5.5 Tighten cluster-wide claims without changing cluster architecture

Keep:

- one global cluster plan;
- complete atomic notes for every proposed member;
- one independent writer call per planned cluster;
- concurrent cluster writers;
- writer authority to remove irrelevant members;
- specific contributions for every retained member;
- isolated parking of malformed clusters; and
- reciprocal additive projection.

Cluster prompt v28 replaces its current overlapping self-review language with
three focused requirements:

1. Prefer named-source attribution for findings, disagreement, and boundaries.
2. Use `all`, `most`, `none`, `consensus`, `includes`, or `excludes` only when
   the final retained-member evidence establishes the relevant numerator and
   denominator.
3. Preserve each study's original statistic, scale, comparison, denominator,
   and direction. Do not create cross-study conversions or treat relative
   risk, odds, hazards, probabilities, and percentage points as interchangeable.

Do not add a cluster verifier, deterministic statistical adjudicator, family
reconciliation pass, or smaller evidence packet. The v0.18 full-note cluster
writer already passes the main quality thresholds.

### 5.6 Correct the Obsidian missing-link audit

The two v0.18 apostrophe failures are false positives from scanning YAML
frontmatter as Markdown. PyYAML correctly represents an apostrophe as `''`
inside a single-quoted YAML scalar, while the actual Markdown body links
contain normal apostrophes and resolve.

Fix the shared missing-link scanner to:

- identify and skip or parse the leading YAML frontmatter block; and
- inspect only Markdown body wikilinks.

Do not globally replace doubled apostrophes and do not change valid note titles
or body links.

### 5.7 Preserve replay and acyclic fingerprints

Retain the v0.18 semantic dependency chain:

```text
source content
    → source bundle
    → compact profile and indexes
    → relationship candidates
    → relationship decisions
    → cluster plan
    → cluster syntheses
    → registry commit
    → Markdown and Obsidian projection
```

Update candidate and relationship fingerprints only for the new prompt,
contract, and the two bridge orientations. Downstream graph, cluster,
projection, timestamp, and history changes must never invalidate upstream
semantic work.

Persist all completed and parked relationship decisions before cluster
planning. Keep stable event IDs and byte-change-aware writes. An identical
global replay must short-circuit before any provider call or generated-file
write.

## 6. Regression tests

Add the smallest focused tests needed to prove the changed behavior.

### 6.1 Source scope

- A fully recovered 337-page attachment labeled `Introduction` remains a full
  document.
- A genuinely short book introduction remains partial.
- Explicit chapter, appendix, and excerpt signals still take precedence.
- Invalid or ambiguous source JSON remains parked with raw response metadata.

### 6.2 Bridge discovery

- Both bridge orientations receive collection-index cards.
- Each source appears once per packet.
- Bridge packets contain no full notes or evidence anchors.
- Only resolved cross-collection literature positions are included.
- Lean candidate rows normalize into current internal candidates.
- Same-folder rows are rejected from the bridge pool.
- The two pools merge, deduplicate, rank, and cap at 48.
- General plus bridge candidates never exceed 120.
- Multi-collection routing packs selected collection-pair jobs without an
  all-pairs scan.
- Existing custom reasoners remain compatible.

### 6.3 Relationship adjudication

- Every provider row is matched by `pair_job_id`.
- Canonical pair, tier, and reciprocal labels are derived locally.
- Symmetric relationships may omit actor/reference and remain valid.
- Directional relationships still require actor/reference.
- Returned anchors are partitioned by actual endpoint ownership.
- Unknown or one-sided evidence cannot publish a direct relationship.
- Missing response rows park only their jobs.
- Legacy v5 responses still normalize.
- Citation and dataset reuse do not automatically become `supports`.
- Unsupported lineage does not become `extends`.
- Different stages or outcomes do not become false `contrasts`.
- Complete current decisions are committed before cluster work.

### 6.4 Clusters and projection

- Cluster-wide universal and consensus wording remains bounded to retained
  members.
- Statistical scale and denominator remain source-specific.
- One malformed cluster cannot make the whole map partial.
- YAML frontmatter containing `What's` does not create a false missing link.
- The corresponding body wikilink resolves exactly.
- Atomic relationships and atomic-cluster memberships remain reciprocal.

### 6.5 Replay and compatibility

- An identical build makes zero provider calls and writes no files.
- Changing projections or frontmatter does not invalidate semantic work.
- Relationship prompt or contract changes invalidate only relationship and
  downstream work.
- Source prompt and unchanged successful notes remain reusable.
- Run the complete existing suite and require no regressions.

### 6.6 Bounded provider-contract smoke test

Before starting the fresh corpus run, make one non-persistent adjudication call
over 12 deterministic, non-benchmark v0.18 pair jobs. Require:

- exactly 12 keyed decision entries;
- zero omitted or duplicate job IDs;
- zero invalid v6 envelopes; and
- successful normalization of every structurally complete row.

This call tests provider compliance, not semantic quality, and must not expose
the 40-pair benchmark or write into the v0.19 evaluation workspace. If it
fails, stop before the full paid run. Fix the prompt or parser, rerun the local
suite, and commit a new evaluated implementation; do not retry the unchanged
smoke request.

## 7. Fresh full two-folder evaluation

### 7.1 Workspace and corpus

Create:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/mediation-relapse-v019-evaluation-20260730`

Use the unchanged Zotero collections:

- Mediation: `B887A4Q8`, expected 75 sources;
- Conflict Relapse: `D2XT9ZU9`, expected 120 sources; and
- Combined: expected 195 unique sources.

This is a fresh end-to-end run. Do not copy the v0.18 graph or frozen v0.17
profiles into the evaluated workspace.

Before paid processing:

1. inspect the implementation diff;
2. run the full local test suite and package build;
3. commit the v0.19 implementation with `.DS_Store` excluded;
4. require a clean tracked tree;
5. initialize the workspace and run `doctor`;
6. record the commit, package versions, engine, schemas, prompts, model,
   settings, timestamps, and timezone;
7. freeze both Zotero inventories, attachment identities, and source hashes;
8. verify collection counts, duplicate keys, titles, and overlap; and
9. compare the inventory with v0.16 and v0.17 in `source-drift.yml`.

Keep all generated material, source text, evaluations, and the bridge benchmark
private. Keep the 40-pair benchmark outside every model-visible workspace input
until all generation calls finish; only then copy it into `evaluation/`.

### 7.2 Source mapping

Map the collections sequentially without collection-level synthesis:

- `eval-mediation-v019-20260730`;
- `eval-relapse-v019-20260730`.

Use:

- DeepSeek `deepseek-v4-flash`;
- explicit cloud permission;
- `provider_concurrency=auto`;
- four configured workers;
- OCR auto, English;
- 120,000-character direct reads;
- 60,000-character chunks;
- at most 64 chunks and 24 document calls;
- 120-second request deadline;
- 900-second document deadline;
- 900-token chunk outputs;
- 3,000-token document syntheses;
- 50% source-context target;
- 80% literature-context target; and
- `--max-profile-calls 250` per collection.

The existing profile ceiling does not cap source-generation calls. Source calls
remain bounded by the 24-call per-document limit and must be reported
separately in the provider ledger; do not claim an aggregate source ceiling
that the current interface cannot enforce.

Allow the normal single transport retry. Do not retry semantic or contract
failures and do not raise a ceiling during the frozen run.

### 7.3 One global map

Build one global map without `--source-set`:

- run ID `eval-global-v019-20260730`;
- no source regeneration;
- `provider_concurrency=auto`;
- 7,200-second literature deadline; and
- 30-call cumulative literature ceiling.

Do not generate separate Mediation and Relapse graphs. Collection membership
supplies filtered views over one global registry and cluster map.

Expected allocation:

| Stage | Expected calls |
|---|---:|
| General discovery | 1 |
| Two bridge orientations | 2 |
| Full-note relationship adjudication | 4–6 |
| Global cluster planning | 1 |
| Concurrent cluster writers | 11–15 |
| Transport/overflow reserve | 5 |
| **Expected semantic total before reserve** | **19–25** |
| **Maximum including the five-call reserve** | **24–30** |
| **Hard ceiling** | **30** |

Freeze completed artifacts before evaluation. Do not change code, prompts,
configuration, or thresholds during the evaluated run.

If a threshold is missed, complete the planned audits and report the exact
failure stage. Do not patch, retry semantically, expose the benchmark, or
increase a ceiling inside the frozen evaluation.

## 8. Complete evaluation

### 8.1 Mechanical audit of all 195 records

Audit:

- terminal accounting;
- source and attachment identity;
- extraction route and recovered coverage;
- scope classification;
- analytical, partial, metadata-only, and parked status;
- atomic-note and compact-profile structure;
- evidence-anchor ownership and resolution;
- deterministic catalogue and index inclusion;
- relationship job accounting;
- reciprocal atomic relationships;
- reciprocal atomic-cluster membership;
- stale prompt/version projections;
- Obsidian link resolution;
- provider calls, retries, and checkpoint hits; and
- generated-file integrity.

Report:

- all-source terminal-note yield;
- readable-source success using the historically readable denominator;
- every parked item and its precise failure stage;
- all Zotero metadata/type issues; and
- source drift relative to earlier evaluations.

Acceptance:

- zero pending or processing-partial items;
- at least 175/177 historically readable sources successfully represented;
- no wrong-source note;
- no invented complete-document finding;
- no unexplained regression in a previously successful source;
- *Pathways for Peace* classified according to its recovered content; and
- complete call and state accounting.

### 8.2 Atomic-note audit

Reuse the existing deterministic 30-source sample:

- 15 sources per collection;
- the same frozen Zotero keys used in v0.15 and v0.16; and
- the same source-first scoring rules.

The historical sample contained 24 analytical and six limited notes, but v0.19
must score each key under its current valid status rather than force the old
quota. If source drift makes a key unavailable, select and record a
deterministic same-collection, same-route replacement before reading any v0.19
note.

Before reading each note, independently record:

- thesis;
- method or knowledge basis;
- two major findings;
- limitation;
- important data, examples, cases, or historical analogies; and
- important cited-literature positions.

Score critical-fact recall, substantive-claim support, headline numbers and
scales, causal calibration, important evidence capture, literature-position
accuracy, and limited-note scope.

Acceptance:

- critical-fact recall at least 95%;
- substantive-claim support at least 95%;
- headline numeric accuracy at least 95%;
- no material invented statistic or unsupported causal upgrade;
- every limited note accurately states its available-content boundary; and
- zero invented complete-document findings.

Locator accuracy is advisory. Minor page drift does not fail the release.

### 8.3 Statistical audit

Reuse the frozen deterministic 20-source statistical sample covering:

- percentages and percentage-point changes;
- probabilities and marginal effects;
- coefficients;
- odds and hazard ratios;
- interaction terms;
- confidence intervals and p-values;
- null findings; and
- observational causal-language risks.

Score:

- raw statistic preservation;
- estimand, scale, baseline, reference, denominator, and direction;
- percentage versus percentage-point wording;
- invented conversions;
- useful plain-English interpretation; and
- causal calibration.

Acceptance:

- 100% preservation of audited headline values;
- at least 90% correct scale, reference, baseline, denominator, and direction;
- zero material invented conversion; and
- improved plus equivalent paired explanations outnumber worsened ones.

Simple wording such as `11% versus 50%` is already clear. Recasting it as “11
versus 50 per 100” is equivalent, not automatically better.

### 8.4 Bridge discovery

Reuse the frozen 40-pair benchmark without exposing it to any model input.

Report:

- raw recall against all 40 pairs;
- eligible recall for pairs whose endpoints produced usable analytical notes;
- candidate recall;
- useful final-link recall, including valid direct and genuinely useful
  contextual relationships;
- direct-only recall descriptively;
- candidate capacity use;
- coverage by collection orientation and bridge family; and
- losses at routing, discovery, filtering, contract parsing, persistence, and
  projection.

Create a deterministic 20-pair non-benchmark sample from generated
cross-folder candidates and audit whether each is non-trivial and worth
full-note comparison.

Acceptance:

- eligible candidate recall at least 70%;
- eligible useful final-link recall at least 70%;
- non-benchmark candidate plausibility at least 70%;
- zero same-folder admission through the bridge-only pools;
- zero valid candidate lost through deterministic filtering or persistence;
  and
- 100% explicit cross-folder recall.

The exact 40 pairs are a stable comparative benchmark, not the only possible
good graph. Report useful alternative pairs and bridge-family coverage so that
recall is not mistaken for a complete theory of graph quality.

### 8.5 Relationship quality

Audit:

- every direct relationship when there are at most 120, otherwise a
  deterministic 100-link sample;
- every cross-folder direct relationship;
- every cross-folder contextual relationship;
- every explicit cross-folder Zotero or citation relation used by the recall
  metric;
- every final benchmark bridge; and
- every structurally parked relationship row.

Read both complete atomic notes before scoring:

- exact relation type and intellectual direction;
- proposition-level grounding;
- evidence ownership and adequacy;
- rationale consistency;
- contextual usefulness;
- structural contract completion; and
- reciprocal projection.

Acceptance:

- all-direct type and direction at least 85%;
- cross-folder direct type and direction at least 85%;
- fully grounded direct relationships at least 85%;
- contextual usefulness at least 80%;
- structurally valid rows at least
  `ceil(0.98 × all expected adjudication jobs)`, with omitted rows included as
  invalid in that denominator;
- 100% completed-or-parked job accounting;
- 100% explicit cross-folder recall;
- 100% reciprocal accepted-link projection; and
- zero stale prompt-v8 machine links.

A useful contextual connection is not a failed direct relationship merely
because it is not `supports`, `qualifies`, or `extends`.

### 8.6 Cluster quality

If v0.19 produces at most 20 clusters, audit every cluster. Otherwise audit:

- every mixed-literature cluster;
- the eight largest other clusters;
- the best civil-war duration or termination cluster; and
- every cluster containing a statistical-sample source.

Score:

- membership relevance;
- one specific cluster-relevant contribution for every retained member;
- source-specific claim support;
- statistical interpretation;
- debate, consensus, disagreement, inclusion, exclusion, and boundary claims;
- reciprocal membership and neighboring-cluster links; and
- exact Obsidian target resolution.

Acceptance:

- membership relevance at least 90%;
- a specific contribution for every retained member;
- source-specific claim support at least 95%;
- debate and boundary accuracy at least 90%;
- 100% reciprocal registry and visible Obsidian membership links;
- every planned cluster published or individually parked;
- no whole-map partial state caused by one cluster; and
- zero fabricated consensus, disagreement, or cluster-wide quantitative
  comparison.

Report unclustered sources descriptively. Do not fail the release because a
source does not yet fit a useful cluster.

### 8.7 Replay, runtime, and efficiency

Before replay, snapshot:

- every generated path;
- file hash, size, and nanosecond mtime;
- provider-ledger counts;
- relationship and cluster semantic digests; and
- registry/history event counts.

Replay the identical global build and require:

- zero provider calls;
- zero file additions or removals;
- zero byte changes;
- zero mtime changes;
- zero semantic graph or cluster changes; and
- zero new ledger, history, revision, or timestamp event.

Record generation and evaluation time separately. Break generation time into:

- extraction and local preparation;
- source calls;
- profile and index construction;
- candidate routing and discovery;
- relationship adjudication;
- cluster planning;
- cluster writing;
- projection and export; and
- replay.

Also record peak provider concurrency, stage-level calls, retries, failures,
cache hits, and repeated semantic job keys.

Acceptance:

- normal global literature usage at or below 25 calls and never above 30;
- no repeated completed semantic work;
- zero-call, zero-write replay;
- production generation below four hours, with a target below 90 minutes; and
- evaluation time reported separately rather than attributed to production.

## 9. Comparison with every prior evaluation

Use matched Zotero keys, source pairs, and cluster topics rather than unstable
generated IDs. Exclude changed source content from paired claims and report it
separately.

Do not collapse incompatible metrics into one trend. Produce five tables.

### 9.1 Atomic and source quality

Compare v0.10 through v0.19. Include v0.11 explicitly as unscored where no
comparable atomic audit exists, mark v0.14 as unscored where no fresh atomic
audit exists, and mark v0.18 as frozen.

Include:

- readable-source success and parked count;
- critical-fact recall;
- claim and numeric support;
- material causal upgrades;
- limited-note scope; and
- statistical-explanation results.

### 9.2 Frozen 40-pair bridge benchmark

Preserve the historical series:

| Version | Candidate recall | Useful/final recall |
|---|---:|---:|
| v0.10 | N/A | 0/40 |
| v0.11 | 4/40 | 2/40 |
| v0.12 | 12/40 | 2/40 |
| v0.13 | N/A | 10/40 |
| v0.14 | N/A | 2/40 |
| v0.15 | N/A | 6/40 |
| v0.16 | 5/40 | 5/40 |
| v0.17 | 7/40 | 4/40 |
| v0.18 | 12/40 | 9/40 |
| v0.19 | Measure fresh | Measure fresh |

Report raw and eligible denominators and note corpus differences.

### 9.3 Earlier broad inferred-link precision

Keep the earlier metric separate:

| Version | Strict inferred cross-folder precision |
|---|---:|
| v0.11 | 8/27 (29.63%) |
| v0.12 | 6/9 (66.67%) |
| v0.13 | 23/32 (71.88%) |
| v0.14 | 9/17 (52.94%) |
| v0.15 | 13/35 (37.14%) |

Do not append v0.16–v0.19 direct-link scores to this table.

### 9.4 Modern relationship audit

Compare modern metrics separately:

| Version | Cross-folder type/direction | All-direct type/direction | Fully grounded direct | Contextual usefulness |
|---|---:|---:|---:|---:|
| v0.16 | 4/6 (66.67%) | N/A | N/A | 5/5 (100%; small sample) |
| v0.17 | 7/8 (87.5%) | 59/71 (83.10%) | 54/71 (76.06%) | 3/4 (75%) |
| v0.18 | 16/20 (80%) | 53/63 (84.13%) | 49/63 (77.78%) | 13/15 (86.67%) |
| v0.19 | Measure fresh | Measure fresh | Measure fresh | Measure fresh |

Where possible, add a matched-pair comparison for relationships present in
both v0.18 and v0.19.

### 9.5 Clusters, replay, calls, and runtime

Compare v0.13 through v0.19 while marking corpus and definition changes.

Include:

- planned, published, parked, and mixed clusters;
- audited membership, specific contributions, claim support, and
  debate/boundary accuracy;
- unclustered counts descriptively;
- graph and source provider calls;
- repeated semantic calls;
- zero-call and zero-write replay;
- generation wall time; and
- evaluation wall time.

The narrative comparison must explain whether each v0.19 change is:

- a real quality improvement;
- an efficiency improvement;
- equivalent under a changed sample;
- a regression;
- or not comparable.

## 10. Deliverables

Write inside the new workspace:

- `evaluation/v019-full-comparison.md`;
- `evaluation/metrics.yml`;
- `evaluation/source-metrics.yml`;
- `evaluation/atomic-metrics.yml`;
- `evaluation/statistical-metrics.yml`;
- `evaluation/bridge-metrics.yml`;
- `evaluation/relationship-metrics.yml`;
- `evaluation/cluster-metrics.yml`;
- `evaluation/runtime-metrics.yml`;
- `evaluation/replay-metrics.yml`;
- `evaluation/atomic-sample.yml`;
- `evaluation/statistical-sample.yml`;
- `evaluation/curated-bridge-benchmark.yml`;
- `evaluation/nonbenchmark-candidate-sample.yml`;
- `evaluation/source-drift.yml`;
- `evaluation/zotero-metadata-remediation.yml`;
- pre-replay and post-replay snapshots; and
- a machine-readable replay diff.

Export one separate private Obsidian vault after the replay passes. Include a
home note linking to atomic notes, collection indexes, clusters, relationship
views, unclustered sources, and evaluation results.

The main report must contain:

- an executive verdict by subsystem;
- representative atomic notes and clusters;
- representative bridge and relationship successes and failures;
- exact failure stages;
- source-linked evidence for quality judgments;
- the v0.10–v0.19 trend tables;
- runtime separated from evaluation time; and
- a prioritized list of any defects that remain.

## 11. Verdict rules

Issue separate verdicts for:

- source recovery;
- atomic notes;
- statistical interpretation;
- bridge discovery;
- relationship adjudication;
- clusters;
- Obsidian projection;
- replay and efficiency; and
- overall release readiness.

Do not fail v0.19 merely for:

- minor locator drift;
- stylistic differences;
- retaining an already-clear percentage;
- a useful source remaining unclustered; or
- a safely parked genuinely malformed provider response when readable-source
  success remains above threshold.

Fail or qualify the relevant subsystem for:

- wrong-source or invented complete-document content;
- material unsupported causal or statistical claims;
- missed bridge, relationship, or cluster thresholds;
- structurally lost relationship jobs;
- stale or nonreciprocal projections;
- repeated completed semantic work;
- replay provider calls or file rewrites; or
- production runtime above four hours.

Do not weaken thresholds, expose the benchmark, add retries, change prompts, or
raise call ceilings during the frozen evaluation.
