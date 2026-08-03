# Auto-Zettelkasten v0.28 Independent Concurrency, Unbounded Completion, and Full-Library Recovery

## Summary

Release v0.28 as an operational reliability correction, then resume the frozen 5,324-record full-library run without regenerating successful source bundles.

The v0.27 run established that high DeepSeek concurrency is feasible, but it did not establish that 256 is the optimal provider limit. Its single source-worker pool coupled local extraction to remote generation: workers waiting for one of four local slots occupied capacity that should have been available to provider-ready sources. In addition, an absolute 600-second request deadline interrupted actively streaming responses and produced unrecoverable partial JSON.

v0.28 will:

- Separate local preparation and provider generation into independent stdlib executors, queues, and dispatch loops.
- Let frozen-text sources enter the DeepSeek queue immediately.
- Refill provider slots independently of coordinator commits and Markdown projection.
- Benchmark 256 and 512 provider workers before selecting the DeepSeek `auto` default.
- Replace whole-request deadlines with connection and stream-inactivity timeouts.
- Classify and recover transport failures without retrying semantic failures.
- Make interruption prompt, durable, and resumable.
- Remove default call, chunk, candidate, membership, cluster-writer, and global wall-clock ceilings.
- Add one optional cumulative dollar budget with drain-at-limit behavior.
- Replace the fixed 120-pair graph ceiling with adaptive completion.
- Resume and evaluate the original frozen Zotero snapshot.

Provider concurrency is an in-flight resource bound, not a total-work limit. DeepSeek performs inference remotely, but request bodies, sockets, streamed responses, parsing, and checkpoints consume local memory, bandwidth, and file descriptors. The default should therefore be high and independently scalable, but selected from measured 256-versus-512 performance rather than automatically set to DeepSeek's full documented account allowance.

Release identities:

- Engine: `0.28.0`
- Artifact schema: `1.19`
- Provider-event ledger schema: `4`
- Relationship discovery prompt: `16`
- Source prompt: unchanged at `5`
- Relationship adjudication prompt: unchanged at `15`
- Literature-family, cluster-planning, and cluster-synthesis prompts: unchanged
- Relationship registry, atomic-note contract, and cluster contract: unchanged

## Implementation Changes

### 1. Replace the coupled source pool with two independent executors

Use two `concurrent.futures.ThreadPoolExecutor` instances running long-lived stdlib queue worker loops. Do not create a future for every source, chain callbacks, or add an async framework, process pool, database, or service.

#### Local executor

The local executor owns only:

- Zotero reads.
- Attachment selection and copying.
- PDF or HTML extraction.
- OCR and vision preparation.
- Frozen-content checkpoint writing.

Its worker count is exactly `--parallel`, normally four. Local activity must never exceed that value.

Each local worker enqueues a compact content reference containing the source identity, frozen-content path, content hash, scope, extraction metadata, and item metadata. It must not retain the complete extracted document in a long-lived queue.

Once frozen content is written, the local slot is released before the source is offered to provider dispatch. No DeepSeek request may run while holding a local extraction slot.

Sources that produce a terminal limited result without model generation bypass the provider executor and enter the commit-result path directly.

#### Provider executor

The provider executor owns only:

- Loading one source's frozen text when that provider task starts.
- Reusing or locally recovering a source checkpoint when possible.
- Direct or hierarchical DeepSeek source generation.
- Response parsing, source ownership validation, and provider checkpoint writing.
- Writing an atomic per-item prepared-result checkpoint before returning.

It must never wait for:

- Zotero access, attachment processing, extraction, OCR, or vision work.
- Markdown projection.
- Tag aggregation.
- Coordinator semantic commits.
- Another source's local preparation.

Sources with valid frozen content, reusable source checkpoints, or locally recoverable provider output enter provider-ready dispatch immediately during inventory classification. Newly prepared sources enter it as soon as their local future completes.

Provider workers load frozen text from its path only when starting. Provider-ready queues carry paths and compact metadata, not complete documents.

#### Independent queue lifecycle and refill

Start both executors before scanning the inventory so the first frozen source can reach DeepSeek immediately. Submit only the long-lived worker loops to each executor:

1. The inventory feeder canonicalizes each source exactly once and routes a compact descriptor to the local queue, provider-ready queue, or terminal-result queue.
2. Each local worker repeatedly takes one local descriptor, prepares and checkpoints its content, releases all local resources, and puts a compact content reference on the provider-ready queue.
3. Each provider worker repeatedly takes one provider-ready reference, loads that source's frozen text, completes its sequential direct or hierarchical provider workflow, writes an atomic prepared-result checkpoint, places a compact result reference on the completion queue, and immediately takes the next provider-ready job.
4. The coordinator continuously consumes completion references and serially performs `_finalize_prepared_row`, Markdown writes, frontmatter projection, and aggregate updates.

Provider refilling must not depend on coordinator consumption, Markdown writes, tag commits, or progress serialization. The provider worker returns to the provider-ready queue immediately after persisting and enqueueing its compact result reference.

All three queues are bounded by the finite canonical pending inventory count. This is a natural memory bound rather than a work cap: every pending source descriptor can fit, entries contain only metadata or paths, and the completion queue cannot fill beyond the number of routed sources. It prevents a slow coordinator from blocking provider workers while avoiding an arbitrary queue-size limit.

The append-only provider ledger and per-item prepared-result checkpoint are authoritative. Provider and local workers do not rewrite `progress.yml`; the coordinator projects queue and completion counts into throttled progress state.

After inventory feeding completes, local stop markers follow the final local jobs. The last exiting local worker marks provider input closed. Provider workers exit only when provider input is closed and the provider-ready queue is empty. The coordinator drains results until every routed source is terminal or controlled cancellation leaves it pending.

Monitor the long-lived worker futures. An unexpected local or provider worker death triggers a controlled integrity stop instead of leaving other threads waiting indefinitely. No shared lock may surround extraction, OCR, a provider request, or coordinator commit work.

Record independently:

- Local active, peak, ready-queue, and pending counts.
- Provider active, peak, ready-queue, open-response, and pending counts.
- Completed-result backlog and commit latency.
- Time a provider slot remained idle while provider-ready work existed.
- Extraction, provider, and coordinator throughput.
- Resident full-text count; outside short handoff moments it must not exceed active local preparation plus active provider jobs.

### 2. Benchmark and select DeepSeek automatic concurrency

Do not infer the automatic value from v0.27's observed peak of 178. Benchmark the separated pipeline at:

- 256 provider workers.
- 512 provider workers.

First run deterministic delayed-provider tests with identical job fixtures. Then run two matched, non-overlapping 512-source paid tranches from the frozen full-library workload: one at 256 workers and one at 512. Stratify them by frozen-text size, extraction route, source scope, and direct versus hierarchical processing. Do not regenerate already successful notes merely to benchmark, and retain both tranches as completed evaluation work.

Select `auto=512` only if the 512-worker tranche:

- Improves sustained provider completions per minute by at least 20%.
- Has no sustained 429 burst.
- Does not increase retryable transport failures by more than two percentage points.
- Does not increase p95 provider latency above 1.5 times the 256-worker result.
- Keeps peak RSS below 24 GiB.
- Produces no duplicate reservations, lost events, crossed source ownership, or checkpoint corruption.
- Keeps open responses and queued references within their configured bounds.

Otherwise retain `auto=256` and record the limiting measurement. The result becomes a tested provider capability default, not a semantic fingerprint input.

Explicit numeric `--provider-concurrency` is honored up to DeepSeek's current documented provider/account limit. Do not silently clamp explicit values to 256 or 512. If the requested value exceeds the provider-declared limit or the process cannot support the required file descriptors, fail preflight clearly rather than lowering it silently.

Other cloud providers retain their existing conservative automatic value unless their adapter declares and tests a higher supported value. Local providers continue using `--parallel`.

### 3. Fix streaming transport semantics

Replace the absolute response deadline with:

- `connect_timeout_seconds`: connection and response-header timeout; default 60 seconds.
- `request_idle_timeout_seconds`: maximum time without a meaningful stream event; default 600 seconds for DeepSeek.

Meaningful activity includes visible content, reasoning fragments, usage data, or a terminal event. Mere keepalive bytes do not reset the timer. A call may run longer than ten minutes while it continues making progress.

Require an OpenAI-compatible stream to end with `[DONE]` or a terminal `finish_reason`. EOF before either is a transport interruption, not malformed semantic output.

Add `ProviderTransportError` carrying:

- Transport category: connection, DNS, socket, idle timeout, interrupted stream, premature EOF, retryable HTTP status, or network unavailable.
- Underlying exception class, errno, and bounded message.
- Partial visible output and stable stream hashes.
- Provider response ID, model, usage, finish reason, and event counts when present.
- `retryable` and `retry_on_resume` state.

Retry behavior per invocation:

- Connection, DNS, network, idle timeout, interrupted stream, premature EOF, HTTP 429, and HTTP 5xx: one immediate retry.
- True visible-empty or reasoning-only completion: one exact mechanical retry.
- Non-empty malformed JSON, source-ownership failure, wrong schema, content filtering, or semantic contract failure: no paid retry.
- Two failed transport attempts leave the item `paused_transport`, not terminally parked. A later resume may try again after conditions improve.

Retain `--request-deadline-seconds` as a deprecated alias for the inactivity timeout. It must no longer terminate an actively streaming response.

### 4. Recover v0.27 state without regenerating successes

Persist explicit failure classes instead of inferring failure type from whether raw output is non-empty.

Migrate legacy v0.27 checkpoints locally:

- Stream-read, DNS, network, timeout, premature-EOF, and interrupted-response failures become retryable transport.
- Empty visible completions become provider-empty.
- Completed non-empty malformed or invalid responses remain semantic/contract failures.
- Content-filtered responses remain terminal provider-policy failures.
- Reservations lacking completion events become interrupted transport attempts.

Reconcile the 53 unmatched v0.27 reservations by appending stable `interrupted` completion events. They remain counted as paid attempts, while their source jobs remain resumable.

Reuse without provider calls:

- All 844 completed source bundles.
- Existing valid atomic notes and profiles.
- Frozen content and extraction checkpoints.
- Locally recoverable provider responses.

Recover the observed contract wrapper only when the decoded mapping has exactly one top-level `source-bundle-envelope-v2` key whose value is a mapping. Reject sibling fields, recursion, lists, and ambiguity, then apply the complete existing schema, source identity, and ownership validation. This should recover `9SNN45AG` with zero provider calls.

### 5. Make interruption safe across both executors

Use one shared cancellation event and an active-response registry.

On Ctrl-C, budget pause, or fatal integrity failure:

1. Stop the inventory feeder.
2. Stop adding local and provider-ready jobs.
3. Cancel not-started futures in both executors.
4. Wake queue waiters and local acquisition waiters with cancellation-aware sentinels.
5. Close registered active HTTP responses so streamed reads unwind.
6. Terminate or cancel active OCR/extraction subprocesses where the existing extractor supports it.
7. Persist completed provider prepared-result checkpoints.
8. Drain and commit already completed result references.
9. Force progress and provider-ledger barriers.
10. Shut down both executors with `cancel_futures=True`.

Cancelled jobs remain pending. They must not receive semantic failure checkpoints. Provider attempts already reserved are completed as `cancelled` or `interrupted` and remain counted.

Require controlled interruption to return within 60 seconds with the real provider and promptly in local tests. No manual SIGTERM should be necessary.

### 6. Remove premature source-processing ceilings

Make operational work limits optional and unlimited by default:

- Global source/profile attempts: unlimited.
- Per-document calls: unlimited.
- Total chunks: unlimited.
- Whole-document wall-clock deadline: disabled.
- Provider work queue: progressively fed until every finite inventory item is accounted.

Compatibility behavior:

- `--max-profile-calls 0`: unlimited and the default.
- `--max-document-calls 0`: unlimited.
- `--max-total-chunks 0`: unlimited.
- `--document-deadline-seconds 0`: no whole-document deadline.
- Positive values remain explicit emergency controls.

Explicit zero must override a sticky v0.27 ceiling. Retain the old configured value only as audit history.

Capacity must route rather than fail where possible:

- Direct prompts that do not fit route immediately to hierarchical reading.
- Direct generation ending in `finish_reason=length` routes to hierarchical reading.
- Finite chunk plans process every chunk.
- Context packers derive usable input from provider context capacity, stage output allowance, and framing reserve rather than a fixed percentage alone.
- Context-bounded multi-job responses preserve complete returned rows and continue only missing job IDs.

Provider context and output limits remain physical constraints. They are not removed, but packet splitting and continuation must prevent them from becoming avoidable whole-stage failures.

### 7. Add one optional dollar budget

Add:

- API: `max_provider_spend_usd: Decimal | None = None`
- CLI: `--max-provider-spend-usd AMOUNT`

Behavior:

- Omitted means unlimited.
- Spend is cumulative for the logical run ID across resumes.
- Successful, failed, retried, cancelled-after-reservation, and interrupted paid attempts are accounted.
- Stop submitting new local work that would require a provider call and new provider jobs when completed-attempt spend reaches the amount.
- Let in-flight calls finish.
- Commit every completed result.
- Leave unscheduled work pending.
- Record `paused_budget`, never failed or exhausted.
- A later higher or absent budget resumes from checkpoints.
- Report any overshoot caused by calls already in flight.

Use provider-reported usage when available. Otherwise store a conservative estimated cost based on known input and emitted output, clearly labeled `estimated`. Store the pricing source and effective date with the run. Reject dollar budgeting for providers without registered pricing rather than inventing a cost.

Legacy call ceilings remain optional emergency controls but default to unlimited and are not the recommended budget interface.

### 8. Remove graph and cluster work caps

Relationship discovery:

- Remove the 120-unique-pair global ceiling.
- Retain context-bounded collection, virtual-index, family, and broad discovery jobs.
- Continue each job until DeepSeek reports `completed` or `no_more_candidates`, or a continuation produces zero new valid unique pairs.
- Exclude prior returned pairs in continuations.
- Preserve every valid candidate and all contributing family provenance.
- Do not manufacture candidates locally or discard them because another family returned many.

Relationship adjudication:

- Adjudicate every selected candidate.
- Use adaptive context packing, normally about 12–15 comparisons per request.
- Run independent packets concurrently.
- Preserve complete decisions from truncated packets and continue only missing pair IDs.
- Persist all completed decisions before cluster planning.
- A malformed packet affects only unresolved pairs.

Cluster generation:

- Remove the fifteen-writer cap and fixed retry reserve.
- Synthesize every valid planned cluster through provider concurrency.
- Make maximum memberships optional and unlimited by default.
- Do not mark work `deferred_budget` unless the user explicitly supplied a dollar or emergency limit.
- Isolate malformed or empty responses to their cluster.
- Preserve valid existing clusters and recommendations when refreshes fail.

Defaults:

- `--max-synthesis-calls 0`: unlimited.
- `--literature-deadline-seconds 0`: no global literature wall deadline.

Relationship discovery prompt 16 changes only the completion contract: discovery continues to useful exhaustion instead of targeting a fixed quota.

### 9. Preserve replay stability

Operational settings must not enter semantic fingerprints:

- Local or provider concurrency.
- Queue-window sizes.
- Connection and inactivity timeouts.
- Dollar or emergency budgets.
- Retry availability.
- Progress-write frequency.
- Resume mode.

Prompt, model, source, index, and semantic policy changes retain their existing invalidation behavior.

An unchanged completed replay must make zero provider calls, append no events, write no files, and preserve hashes and mtimes. A paused run is resumable rather than a completed replay.

## Tests

### Independent concurrency

Add tests proving:

- Frozen-text sources reach DeepSeek while every local extraction slot is occupied.
- Four deliberately slow OCR jobs do not prevent hundreds of provider-ready sources from filling the provider executor.
- Local activity never exceeds `--parallel`.
- Provider activity independently reaches its configured concurrency.
- A completed provider request is replaced before a deliberately slow coordinator commit finishes.
- Provider workers never execute Zotero, extraction, OCR, projection, or commit code.
- Queue entries contain references rather than complete document bodies.
- Resident complete texts never exceed active local preparations plus active provider jobs.
- Local, provider-ready, completed-result, open-response, event, and attempt counts remain bounded and exact.
- The 256- and 512-worker runs produce byte-identical semantic artifacts.
- No source is lost, duplicated, or cross-owned under concurrent completion.
- One ordinary source failure does not cancel siblings; an unexpected long-lived worker-loop death produces a controlled integrity stop rather than a hang.

### Transport, recovery, and shutdown

Prove:

- Active streams may exceed 600 seconds while events continue.
- Silent streams raise typed idle transport errors.
- Premature EOF is retryable transport.
- Partial output and underlying failure evidence survive persistence.
- Transport and true-empty failures receive exactly one immediate retry.
- Semantic and malformed non-empty responses receive no paid retry.
- Two transport failures become `paused_transport`.
- Resume retries transport-paused jobs without retrying unchanged semantic failures.
- All 53 interrupted v0.27 reservations reconcile once.
- The exact contract wrapper recovers under full ownership validation.
- Ctrl-C cancels queued work in both executors, closes active responses, preserves prepared results, and resumes without duplicate attempts.

### Limits, budgets, graph, and replay

Prove:

- Default source and literature runs have no total call, chunk, candidate, membership, cluster-writer, or wall-clock ceiling.
- Positive emergency limits remain enforceable.
- Explicit zero overrides persisted legacy ceilings.
- Dollar drain-at-limit stops new provider scheduling, preserves in-flight results, records overshoot, and leaves remaining work pending.
- Candidate discovery continues while it returns new unique pairs and terminates on explicit completion or no progress.
- Every selected pair and planned cluster is completed or has a precise non-cap failure.
- Identical completed replay makes zero calls and zero writes.

Run the complete pytest suite, Ruff without rewriting, package build, migration tests, 500-item concurrency stress test, 256-versus-512 delayed-provider benchmark, and interruption/resume stress test. Commit the implementation candidate before the paid concurrency benchmark. After the benchmark, change only the tested DeepSeek `auto` constant to 512 or 256, rerun the focused concurrency/replay tests, and commit the final evaluated v0.28 release before continuing the full run.

## Full-Library Recovery and Evaluation

### 1. Preserve and resume the frozen snapshot

Create an APFS copy-on-write clone of:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/full-zotero-v027-evaluation-20260802`

at:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/full-zotero-v028-evaluation-20260802`

Leave v0.27 evidence unchanged. Resume the frozen source run ID `eval-library-v027-full-20260802` in the clone, recording that it started under v0.27 and completed under v0.28. Do not refresh Zotero inventory or include later additions.

Resume order:

1. Reconcile interrupted provider events.
2. Apply zero-call local response recovery.
3. Immediately queue every frozen-text or reusable-checkpoint source for provider-stage handling.
4. Start local preparation independently for remaining sources.
5. Reuse every successful v0.27 source bundle.
6. Retry transport-paused and provider-empty sources under the corrected policy.
7. Process unattempted inventory rows.
8. Continue until every frozen inventory row is terminal or paused by a current external outage.

Use four local workers, no dollar or call budget, no chunk or document deadline, a 60-second connection timeout, a 600-second inactivity timeout, and no semantic retry.

### 2. Benchmark provider concurrency during recovery

Run two matched, non-overlapping 512-source paid tranches through the separated pipeline at 256 and 512 workers. Use only sources that genuinely require processing; do not regenerate successes. Match tranches by source length, scope, route, and expected direct/hierarchical work. Their successful results remain part of the resumed run rather than being discarded.

Freeze the benchmark metrics and apply the selection criteria above. Set the DeepSeek `auto` constant to the selected value, run the focused tests, and create the final release commit. Use that value for the remainder of the run. Record both results and the decision. A benchmark pause is controlled and resumable; it must not create terminal failures.

The initial recovery health window is observational. Pause only for wrong-source output, corrupt accounting, sustained external unavailability, unsafe memory growth, or inability to preserve state. Do not stop because a fixed number of calls, documents, candidates, clusters, or elapsed hours has been reached.

### 3. Source acceptance and replay

Require:

- Zero active streams cut merely because total wall time reaches 600 seconds.
- Local peak at or below four.
- Provider peak at or below the selected value and independently above local concurrency.
- No avoidable provider idle time while ready work exists.
- Exact accounting for every provider attempt and interrupted v0.27 reservation.
- Zero duplicate jobs, crossed ownership, or wrong-source notes.
- At least 95% valid-note, limited-note, or canonical-alias yield.
- Semantic parks below 5%.
- No terminal result caused solely by a call, chunk, queue, or deadline ceiling.
- Every successful v0.27 note reused without semantic change.
- Identical source replay with zero calls and zero writes.

Report actual calls, tokens, spend, throughput, latency, queue depths, commit backlog, open responses, memory, disk growth, and runtime. Do not judge success against a call count.

### 4. Build and evaluate the global graph

After source replay passes, run global graph ID `eval-global-v028-full-20260802` with:

- No synthesis-call ceiling.
- No global literature wall deadline.
- No fixed candidate, membership, or cluster-writer ceiling.
- The selected DeepSeek automatic concurrency.
- Existing hierarchical collection and virtual-index routing.
- Adaptive discovery completion.
- Both complete atomic notes for relationship adjudication.
- Complete retained member notes for cluster synthesis.
- No semantic retries or live web search.

Retain the v0.27 full-library evaluation design:

- Mechanical audit of all 5,324 frozen records.
- Canonical identity and duplicate audit.
- 100 analytical notes, 25 limited notes, up to 20 parked sources, and 40 statistical notes.
- 100 index-navigation tasks.
- Every inferred relationship when feasible, otherwise a deterministic stratified sample.
- Every cluster when there are at most 40, otherwise a stratified 40-cluster sample.
- 50 acquisition recommendations.
- Historical 40 Mediation-Relapse bridge pairs descriptively.
- Historical 14 bridge families and frozen 30 full-library families.
- Frozen 60 plausible full-library pairs.
- Berg citation linking and Fortna peacekeeping discovery cases.
- Complete concurrency, transport, cost, runtime, and replay audit.

Acceptance:

- Atomic critical-fact recall at least 85%.
- Claim, headline-statistic, and causal calibration at least 95%.
- Limited-note classification at least 90%.
- Strong duplicate recall at least 95% with zero false merges.
- Index navigability at least 90% and useful routing at least 80%.
- Relationship endpoint fidelity at least 95%, direct precision at least 90%, and contextual usefulness at least 85%.
- Full-library family coverage at least 24/30 and blind candidate usefulness at least 80%.
- Cluster membership relevance at least 90%, claim support at least 95%, and debate/boundary accuracy at least 90%.
- Acquisition precision at least 90%.
- Relationship and cluster reciprocity 100%, with zero duplicate-active pairs or unresolved generated links.
- Completed replay with zero calls, events, file changes, or mtime changes.

Unclustered sources, optional anchors, approximate locators, label nuance, and descriptive exact-pair misses remain advisory. Use only `Pass`, `Pass with advisories`, or `Fail`. Fixed-cap exhaustion is not an acceptable terminal outcome.

## Deliverables and Assumptions

Write:

- `evaluation/v028-full-library-recovery-evaluation.md`
- Machine-readable concurrency, queue, transport, recovery, budget, identity, source, index, discovery, relationship, cluster, acquisition, runtime, and replay metrics.
- The 256-versus-512 benchmark and automatic-concurrency decision.
- A migration manifest for every v0.27 failure and interrupted reservation.
- Comparisons with v0.19, v0.25, v0.26, and partial v0.27 results.
- Compatible v0.10-v0.28 trend tables and representative outputs.

Export the stable result to:

`/Users/khalilalwazir/Documents/Auto-Zettelkasten-test/full-zotero-v028-obsidian-vault-20260802`

Assumptions:

- The frozen Zotero snapshot remains read-only.
- Successful v0.27 notes remain authoritative reusable checkpoints.
- Provider concurrency is operational and never enters semantic fingerprints.
- The current recovery has no dollar or call budget.
- Explicit concurrency is honored up to the provider-declared limit after preflight.
- Queue bounds protect local resources but never reduce total work.
- Provider context/output capacities are physical constraints handled through routing and continuation.
- No new dependency, async framework, service, database, Zotero mutation, web-search stage, or verifier workflow is introduced.
- All generated and evaluation artifacts remain private.
