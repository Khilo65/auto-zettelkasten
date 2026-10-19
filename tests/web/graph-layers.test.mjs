import assert from "node:assert/strict";
import test from "node:test";
import { buildLinkIndex, buildLibraryIndex, graphData, parseLibraryDocument, resolveLink, searchNotes } from "../../tools/workspace_links.js";

const note = (path, text = "", extra = {}) => ({ path, text, revision: "v1", editable: false, ...extra });
const research = { showReferences: false, showCitations: false, showDiscovery: false, showNavigation: false };

test("Research, citation, Markdown, discovery and navigation layers remain independently selectable", () => {
  const index = buildLinkIndex([
    note("A.md", "[[B]]\n\n<!-- auto-zettelkasten:literature:start -->\n- [[C]]\n<!-- auto-zettelkasten:literature:end -->"),
    note("B.md"), note("C.md"), note("Literature map.md", "[[A]] [[B]]", { navigation: true }),
    note("Coverage.md", "[[A]]", { navigation: true }),
  ], [
    { id: "accepted", source: "A.md", target: "B.md", type: "supports" },
    { id: "cited", source: "B.md", target: "C.md", type: "cites" },
    { id: "candidate", source: "A.md", target: "C.md", type: "semantic_similarity" },
    { id: "member", source: "Literature map.md", target: "C.md", type: "has_member" },
  ]);
  const selected = graphData(index, research);
  assert.deepEqual(selected.nodes.map((node) => node.path), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(selected.edges.map((edge) => edge.layer), ["relation"]);
  assert.deepEqual(selected.nodes.map((node) => node.degree), [1, 1, 0]);
  const citation = graphData(index, { ...research, showRelations: false, showCitations: true });
  assert.equal(citation.edges.length, 2);
  assert.ok(citation.edges.every((edge) => edge.layer === "citation"));
  assert.deepEqual(graphData(index, { ...research, showRelations: false, showReferences: true }).edges.map((edge) => edge.layer), ["reference"]);
  assert.deepEqual(graphData(index, { ...research, showRelations: false, showDiscovery: true }).edges.map((edge) => edge.layer), ["discovery"]);
  const navigation = graphData(index, { ...research, showRelations: false, showNavigation: true });
  assert.equal(navigation.nodes.length, 5);
  assert.equal(navigation.edges.length, 4);
  assert.ok(navigation.edges.every((edge) => edge.layer === "navigation"));
  assert.equal(searchNotes(index, "Literature map")[0].path, "Literature map.md");
});

test("Only verified generated path aliases collapse and ordinary note identities never do", () => {
  const documents = [
    note("02_source_memory/notes/source-a.md", "# Source"),
    note("Source copy.md", "# Source"),
    note("One/Same.md", "---\nnote_id: shared\n---\n# Same"),
    note("Two/Same.md", "---\nnote_id: shared\n---\n# Same"),
    note("Start.md", "[[Source copy]] [[02_source_memory/notes/source-a]] [[Same]]"),
  ];
  const identity = { canonical_path: documents[0].path, path_aliases: [documents[1].path], verified: true };
  const index = buildLinkIndex(documents, [], null, { document_identities: [identity] });
  const graph = graphData(index);
  assert.equal(graph.nodes.filter((node) => node.path).length, 4);
  const source = graph.nodes.find((node) => node.path === identity.canonical_path);
  assert.deepEqual(source.pathAliases, [identity.canonical_path, "Source copy.md"]);
  assert.equal(graph.edges.filter((edge) => edge.target === identity.canonical_path).length, 1);
  assert.equal(graph.nodes.filter((node) => node.path?.endsWith("Same.md")).length, 2);
  assert.equal(resolveLink(index, "Start.md", "Source copy").path, "Source copy.md", "Explicit links still open their actual files");
  assert.equal(resolveLink(index, "Start.md", "Same").status, "ambiguous");
  assert.equal(graphData(index, { folder: "Two" }).nodes[0].path, "Two/Same.md");
  assert.equal(graphData(index, { search: "Source copy" }).nodes[0].path, identity.canonical_path);
  assert.equal(graphData(buildLinkIndex(documents, [], null, { document_identities: [{ ...identity, verified: false }] })).nodes.filter((node) => node.path).length, 5);
  assert.equal(searchNotes(index, "Source copy")[0].path, "Source copy.md");
});

test("Unresolved graph destinations name missing notes, ambiguous matches and missing fragments explicitly", () => {
  const index = buildLinkIndex([note("A.md", "[[Missing]] [[Same]] [[B#Gone]]"), note("B.md"), note("One/Same.md"), note("Two/Same.md")]);
  const unresolved = graphData(index).nodes.filter((node) => node.missing);
  assert.deepEqual(unresolved.map((node) => node.status).sort(), ["ambiguous", "missing", "missing-fragment"]);
  assert.ok(unresolved.find((node) => node.status === "missing").label.endsWith(" · missing note"));
  assert.ok(unresolved.find((node) => node.status === "ambiguous").label.endsWith(" · ambiguous target"));
  assert.ok(unresolved.find((node) => node.status === "missing-fragment").label.endsWith(" · missing heading or block"));
  assert.equal(graphData(index, { existingOnly: true }).unresolved.hidden, 3);
  assert.equal(graphData(index, { existingOnly: true }).nodes.some((node) => node.missing), false);
});

test("Compact worker projections and open overlays retain backend navigation and source coverage facts", () => {
  const record = note("Map.md", "[[A]]", { navigation: true, source_coverage: { status: "partial", label: "Partial document" } });
  const compact = parseLibraryDocument(record);
  assert.equal(compact.navigation, true);
  assert.deepEqual(compact.source_coverage, record.source_coverage);
  const index = buildLibraryIndex([compact, parseLibraryDocument(note("A.md"))]);
  assert.equal(graphData(index, { showNavigation: false }).nodes.length, 1);
  assert.deepEqual(index.documents.get("Map.md").source_coverage, record.source_coverage);
});
