# Optional Jev relationship review

Jev is an optional decision stage for bounded pairs of existing research-memory
records. It does not discover candidates, generate notes, or change Zotero.
Local MiniLM retrieval and Leiden clustering remain separate. Deterministic
code can save inferred relationships to the registry only on an explicitly
marked private workspace copy; source notes remain unchanged.

`jev-1.13.0` is pinned. The feature defaults to dry run and zero spend. The
client requires fresh approval of exact request identities, the pinned billing
policy, and an aggregate spending limit before live dispatch. Admission reserves
the documented standard-context model-usage ceiling. The byte-based display
estimate is not a token bound or an invoice guarantee.

## Prepare a local review packet

```sh
auto-zettelkasten jev-review --workspace /path/to/workspace --input pair.json
```

The command only validates and renders a request. It makes no provider call,
then writes a `review_required` artifact under
`.auto-zettelkasten/jev/reviews/` with owner-only permissions. Existing note,
override, relationship-registry, and accepted-link files are untouched.

`pair.json` has this shape. Use stable scoped identities and exact source text;
the content hash must be the SHA-256 of the supplied text.

```json
{
  "request_id": "optional-safe-id",
  "workspace_id": "workspace-scope",
  "library_id": "library-scope",
  "left": {
    "source_id": "source-a",
    "note_id": "note-a",
    "coverage": {"status": "partial", "scope": "chapter_extract"},
    "ownership": {"kind": "managed_source"},
    "note_text": "Existing concise note text.",
    "evidence": [{
      "passage_id": "a-passage-1",
      "text": "Exact supplied passage.",
      "locator": {"page": "12"},
      "attribution": "author's own claim",
      "content_sha256": "lowercase-64-character-sha256"
    }]
  },
  "right": {
    "source_id": "source-b",
    "note_id": "note-b",
    "coverage": {"status": "abstract_only"},
    "ownership": {"kind": "managed_source"},
    "note_text": "Existing concise note text.",
    "evidence": [{
      "passage_id": "b-passage-1",
      "text": "Exact supplied passage.",
      "locator": {"section": "Findings"},
      "attribution": "author's own claim",
      "content_sha256": "lowercase-64-character-sha256"
    }]
  }
}
```

At least one and at most four passages are allowed for each endpoint. The
endpoint order is deliberate: directional cache identities never deduplicate
`A → B` with `B → A`. The cache also binds workspace/library/source/note
identities, exact note and evidence text hashes, locators, coverage, ownership,
model, template, question criteria, their order, and relevant request options.
The passage ID `none` is reserved and rejected at input validation.

## Typed decision template v4

The application supplies `relationship-decision-v4`. Each choice includes a
relation, its direction, and one exact passage from each endpoint. This keeps
all three selections together in a single self-contained question. Source text
is labeled as untrusted data, including any instructions embedded in passages.

> Compare only the supplied endpoints and evidence. Do not use outside
> knowledge. Identify a concrete research-memory comparison, including distinct
> predictors, mechanisms, methods or analytical levels that complement one
> another on the same specific problem. Contrasts can compare substantive
> differences without claiming direct refutation. Supports and undermines still
> require comparable propositions and scope. Shared vocabulary, citations or
> cluster membership alone are insufficient. Attribute quoted claims to their
> speaker; do not assume endorsement. Respect scope and coverage. Select only
> supplied passage IDs and never generate explanations or quotations.

The single `relationship` Choice offers support, undermining, qualification,
and extension in both directions, plus symmetric complements and contrasts.
Three additional choices cover broader research recall: symmetric
`contextual_connection` for a bounded conceptual or background connection,
symmetric `methodological_comparison` for concrete measurement/design comparisons,
and directional `potential_research_resource` from a dataset or tool to the work
whose question it might help investigate. A resource connection is a lead, not
confirmed applicability or a research finding. These choices do not require
corroboration, but both supplied passages must establish a concrete retrieval
purpose. Genuine abstracts can establish their stated questions or findings.

Every relationship option names one offered A/B passage pair. For example,
`supports_B_to_A__A0_B0` means B's first passage supports the proposition in A's
first passage. `supports_A_to_B__A0_B0` means the reverse. Indices refer to the
ordered supplied passages; code retains their original stable IDs and text.
With four passages on each side, at most 224 relationship options are offered.

Two further choices, `topical_only_or_none` and `insufficient_evidence`, retain
no evidence pair or active relation. Missing required evidence or unresolved attribution
calls for insufficient evidence; the application never combines separate
answers that might have evaluated different comparisons.

The template considers the best primary relationship even when several labels
are plausible. A partial source can establish a bounded comparison, but cannot
establish omitted outcomes. Extension requires substantive development or
application; citation, an example or document-input provenance alone does not
establish it. Citation and semantic-discovery layers remain distinct.

The resulting artifact contains the validated typed answer, derived relation
and direction, and exact supplied supporting text/locators. Provider-generated
explanations, quotations, or other unrequested fields are rejected. It remains an
inferred machine proposal, not verified research evidence. Historical cache
entries remain under their original identities and are never read as version 4
decisions. Shared-note request packaging uses `relationship-batch-v3`, including
the version of each source question in its cache identity.

## Winner-only inferred links

The `jev-private-winner-only-v1` policy computes the unique highest-probability
option locally and checks it against the provider's choice. There is no numeric
confidence floor or winning-margin cutoff. A topical-only/none winner creates
no substantive edge; insufficient evidence and tied winners are deferred.
None of these outcomes removes independent embedding-discovery suggestions.
Endpoint, model, template, evidence, attribution, coverage and freshness checks
still apply. Scores, exact selected passages and policy/template versions are
retained for graph and recall inspection; confidence is not independently
calibrated link accuracy.

Historical threshold-policy records keep their original policy identities and
remain readable when their evidence is fresh. They cannot be reused as current
winner-only decisions. Changing the rubric requires a new request rather than
relabeling a cached answer. Paid dispatch and private-copy registry application
remain separate, explicit operations.

The client captures an isolated copy of all request fields before dispatch,
including nested coverage, ownership, locators, question criteria, and options.
Request bytes, cache identity, response checks, and saved evidence all use this
same copy. Cache replay validates the request's workspace and library, the
provider and model, selected evidence, and the exact cost derived from usage.

This is an initial contract, not evidence that this prompt is optimal or that
Jev is accurate or calibrated for research relationships. Validate any future
live configuration against a held-out, independently reviewed evaluation before
using the result beyond review.
