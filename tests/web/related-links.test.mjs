import assert from "node:assert/strict";
import test from "node:test";
import { buildLinkIndex, buildLibraryIndex, parseLibraryDocument, graphData, renderIndexedMarkdown } from "../../tools/workspace_links.js";

const note = (path, text) => ({ path, text, revision: "v1", editable: false });
const block = (text) => `## Related notes — automatic\n\n<!-- auto-zettelkasten:related:start -->\n${text}\n<!-- auto-zettelkasten:related:end -->\n`;

test("Portable automatic links remain a separate discovery layer from authored links", () => {
  const docs = [note("A.md", `My comparison: [[B]].\n\n${block("- [[B]]\n- [[C]]")}`),
    note("B.md", "A substantive note."), note("C.md", block("- [[A]]"))];
  for (const index of [buildLinkIndex(docs), buildLibraryIndex(docs.map(parseLibraryDocument))]) {
    const graph = graphData(index);
    assert.equal(graph.edges.length, 3);
    const manual = graph.edges.filter((edge) => edge.layer === "reference");
    const automatic = graph.edges.filter((edge) => edge.layer === "discovery");
    assert.equal(manual.length, 1);
    assert.equal(automatic.length, 2);
    assert.ok(automatic.every((edge) => edge.type === "semantic_similarity" && edge.accepted === false && edge.evidence_backed === false));
    assert.equal(graphData(index, { showDiscovery: false }).edges.length, 1);
    assert.equal(graphData(index, { showReferences: false }).edges.length, 2);
    if (!index.library) {
      const html = renderIndexedMarkdown(index, "A.md");
      assert.ok(html.includes("Related notes"));
      assert.ok(!html.includes("auto-zettelkasten:related"));
    }
  }
});

test("Authored same-title sections and fenced marker examples are not automatic links", () => {
  const source = `## Related notes — automatic\n\nMy own [[B]].\n\n\`\`\`md\n${block("[[C]]")}\`\`\`\n`;
  const index = buildLinkIndex([note("A.md", source), note("B.md", ""), note("C.md", "")]);
  assert.equal(index.references.length, 1);
  assert.equal(graphData(index).edges[0].layer, "reference");
});

test("Duplicate or reversed ownership markers cannot classify ordinary links as generated", () => {
  for (const text of [block("[[B]]") + block("[[B]]"), "<!-- auto-zettelkasten:related:end -->\n[[B]]\n<!-- auto-zettelkasten:related:start -->\n"]) {
    const index = buildLinkIndex([note("A.md", text), note("B.md", "")]);
    assert.ok(graphData(index).edges.every((edge) => edge.layer === "reference"));
  }
});

test("Encoded relative Markdown fallbacks preserve special-character note identities", () => {
  const target = "Other/Book [edition] #1 50%.md";
  const url = "../Other/Book%20%5Bedition%5D%20%231%2050%25.md";
  const index = buildLinkIndex([note("Folder/Start.md", block(`- [Book edition](${url})`)), note(target, "Evidence.")]);
  assert.equal(index.references.length, 1);
  assert.equal(index.references[0].resolution.status, "resolved");
  assert.equal(index.references[0].resolution.path, target);
  assert.equal(graphData(index).edges[0].layer, "discovery");
});

test("An exported suggestion and a saved reverse-orientation embedding edge display once", () => {
  const revision = "a".repeat(64), digest = "b".repeat(64);
  const docs = [note("A.md", block("[[B]]")), note("B.md", "Substantive finding.")].map((row) => ({ ...row, revision }));
  const saved = { id: "saved", source: "B.md", target: "A.md", type: "semantic_similarity", derived: true,
    decision_status: "suggested", accepted: false, evidence_backed: false, score: .87,
    provenance: { embedding_identity: digest, source_chunk_ids: ["b-evidence"], target_chunk_ids: ["a-evidence"] } };
  const index = buildLinkIndex(docs, [saved], undefined, { semantic_map: { status: "current", identity_digest: digest,
    note_revisions: { "A.md": revision, "B.md": revision }, communities: [] } });
  const graph = graphData(index);
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].score, .87);
  assert.equal(graph.edges[0].source, "B.md");
  assert.deepEqual(graph.edges[0].provenance.source_chunk_ids, ["b-evidence"]);
  assert.equal(graph.edges[0].provenance.exported_markdown, true);
});
