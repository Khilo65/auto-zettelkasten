import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { appendRelationshipDetails, buildLinkIndex, graphData, relationshipsForNote } from "../../tools/workspace_links.js";

const notes = ["A.md", "B.md", "Other.md"].map((path) => ({ path, text: `# ${path}\nSynthetic note.`, revision: "r1" }));
const relationship = (extra = {}) => ({ id: "jev:one", source: "A.md", target: "B.md", type: "qualifies",
  provider: "typesafe", model: "jev-1.13.0", confidence: .91, top_probability: .92, probability_margin: .84,
  template_version: "relationship-decision-v3", policy_version: "jev-private-winner-only-v1", evidence_basis: "note_summary",
  verification_status: "policy_accepted", probabilities: { qualifies_A_to_B: .92, topical_only_or_none: .08 },
  selected_evidence: [{ path: "A.md", text: "A supplied qualification.", attribution: "Existing summary.",
    passage_id: "a-thesis", locator: { kind: "note_lines", heading: "Thesis", line_start: 4 } }], ...extra });
const expand = (window, details) => { details.open = true; details.dispatchEvent(new window.Event("toggle")); };

test("Jev metadata survives indexing and the research graph without entering citation or Discovery layers", () => {
  const input = relationship();
  const index = buildLinkIndex(notes, [input, { id: "citation", source: "A.md", target: "B.md", type: "cites" }]);
  const projected = index.relations.find((row) => row.id === input.id);
  assert.equal(projected.confidence, .91);
  assert.equal(projected.policy_version, input.policy_version);
  assert.deepEqual(projected.probabilities, input.probabilities);
  assert.deepEqual(projected.selected_evidence, input.selected_evidence);
  const research = graphData(index, { showCitations: false, showReferences: false, showDiscovery: false });
  assert.equal(research.edges.length, 1);
  assert.equal(research.edges[0].layer, "relation");
  assert.equal(research.edges[0].confidence, .91);
  assert.equal(graphData(index, { showRelations: false, showReferences: false }).edges[0].layer, "citation");
});

test("Evidence links use only existing endpoint notes and discard private metadata", () => {
  const index = buildLinkIndex(notes, [relationship({ selected_evidence: [
    { path: "../secret.md", text: "private" }, { path: "Other.md", text: "foreign" },
  ] })]);
  assert.equal(index.relations[0].selected_evidence, undefined);
  const bounded = buildLinkIndex(notes, [relationship({ selected_evidence: [{ path: "A.md", text: "x".repeat(3000),
    content_sha256: "a".repeat(64), locator: { heading: "Thesis", note_relative_path: "/private/secret" } }] })]);
  assert.equal(bounded.relations[0].selected_evidence[0].text.length, 1600);
  assert.equal(bounded.relations[0].selected_evidence[0].content_sha256, undefined);
  assert.equal(bounded.relations[0].selected_evidence[0].locator.note_relative_path, undefined);
});

test("Winner-only inferred links retain low scores in graph and relationship details", (t) => {
  const dom = new JSDOM("<main></main>"); t.after(() => dom.window.close());
  const index = buildLinkIndex(notes, [relationship({ confidence: .25, top_probability: .31, probability_margin: .10 })]);
  const graph = graphData(index, { showCitations: false, showReferences: false, showDiscovery: false });
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].confidence, .25);
  assert.equal(graph.edges[0].policy_version, "jev-private-winner-only-v1");
  const container = dom.window.document.querySelector("main");
  const details = appendRelationshipDetails(container, index, "B.md", { direction: "incoming" });
  expand(dom.window, details);
  assert.match(container.textContent, /A qualifies B/);
  assert.match(container.textContent, /Model confidence: 25%/);
});

test("Unknown legacy confidence remains absent and invalid values do not become zero", () => {
  for (const confidence of [undefined, "0.9", true, Infinity, NaN, -1, 2]) {
    const index = buildLinkIndex(notes, [relationship({ confidence })]);
    assert.equal(index.relations[0].confidence, undefined);
  }
  const legacy = buildLinkIndex(notes, [{ id: "old", source: "A.md", target: "B.md", type: "supports" }]);
  assert.deepEqual(legacy.relations[0], { id: "old", source: "A.md", target: "B.md", type: "supports" });
});

test("Relationship details are lazy, escape source text, and navigate evidence without HTML execution", (t) => {
  const dom = new JSDOM("<main></main>"); t.after(() => dom.window.close());
  const index = buildLinkIndex(notes, [relationship({ selected_evidence: [{ path: "A.md", text: '<img src="x" onerror="alert(1)"> & quote',
    locator: { line_start: 4 }, attribution: "Summary, not a publication quote." }] })]);
  const opened = [], container = dom.window.document.querySelector("main");
  const details = appendRelationshipDetails(container, index, "B.md", { direction: "incoming", onOpen: (...args) => opened.push(args) });
  assert.equal(container.querySelector(".relationship-record"), null);
  expand(dom.window, details);
  assert.match(container.textContent, /A qualifies B/);
  assert.match(container.textContent, /Model confidence: 91%/);
  assert.match(container.textContent, /Based on note summaries/);
  assert.equal(container.querySelector("img"), null);
  assert.match(container.textContent, /<img src=/);
  container.querySelector(".relationship-evidence").click();
  assert.deepEqual(opened, [["A.md", { line_start: 4 }]]);
  assert.equal(container.querySelectorAll(".relationship-record").length, 1);
  expand(dom.window, details);
  assert.equal(container.querySelectorAll(".relationship-record").length, 1);
});

test("Directional and symmetric relationships remain inspectable without treating citations as research", () => {
  const index = buildLinkIndex(notes, [relationship(), { id: "symmetric", source: "A.md", target: "B.md", type: "complements" },
    { id: "citation", source: "A.md", target: "B.md", type: "cites" }]);
  assert.equal(relationshipsForNote(index, "A.md", { direction: "incoming" }).length, 1);
  assert.equal(relationshipsForNote(index, "B.md", { direction: "incoming" }).length, 2);
  assert.equal(relationshipsForNote(index, "A.md", { direction: "outgoing" }).length, 2);
  assert.equal(relationshipsForNote(index, "B.md", { direction: "outgoing" }).length, 1);
});

test("Large relationship lists have bounded lazy rendering and do not invent legacy confidence", (t) => {
  const dom = new JSDOM("<main></main>"); t.after(() => dom.window.close());
  const index = buildLinkIndex(notes, Array.from({ length: 75 }, (_, at) => ({ id: `old-${at}`, source: "A.md", target: "B.md", type: "supports" })));
  const container = dom.window.document.querySelector("main");
  const details = appendRelationshipDetails(container, index, "A.md");
  expand(dom.window, details);
  assert.equal(container.querySelectorAll(".relationship-record").length, 50);
  assert.equal(container.querySelector(".relationship-confidence"), null);
  assert.match(container.textContent, /Showing 50 of 75/);
});
