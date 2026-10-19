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

test("Managed citations and reciprocal typed rows display one directed fact with all provenance", () => {
  const documents = [
    note("A.md", "Ordinary [[B]].\n\n<!-- auto-zettelkasten:literature:start -->\n- **[[B|Cited work]]** — cited at p. 12\n<!-- auto-zettelkasten:literature:end -->"),
    note("B.md"),
  ];
  const relations = [
    { id: "forward", source: "A.md", target: "B.md", type: "cites" },
    { id: "inverse", source: "B.md", target: "A.md", type: "cited_by" },
  ];
  for (const index of [buildLinkIndex(documents, relations), buildLibraryIndex(documents.map(parseLibraryDocument), relations)]) {
    const options = { ...research, showRelations: false, showCitations: true };
    const citation = graphData(index, options);
    assert.equal(citation.edges.length, 1);
    assert.deepEqual(citation.nodes.map((node) => node.degree), [1, 1]);
    const [edge] = citation.edges;
    assert.deepEqual([edge.source, edge.target, edge.type, edge.count], ["A.md", "B.md", "cites", 1]);
    assert.deepEqual(edge.provenance, [
      { kind: "managed_markdown", source: "A.md", line: 4, destination: "B", from: index.references[1].from, to: index.references[1].to },
      { kind: "typed_relation", ...relations[0] },
      { kind: "typed_relation", ...relations[1] },
    ]);
    assert.equal(index.references.length, 2, "Raw Markdown occurrences remain indexed");
    assert.deepEqual(index.relations, relations, "Typed registry directions remain unchanged");
    const both = graphData(index, { ...options, showReferences: true });
    assert.deepEqual(both.edges.map((entry) => entry.layer), ["reference", "citation"]);
    assert.deepEqual(both.nodes.map((node) => node.degree), [2, 2]);
    assert.deepEqual(graphData(index, { ...options, showReferences: true, showCitations: false }).edges.map((entry) => entry.layer), ["reference"]);
    assert.equal(graphData(index, { ...options, local: { path: "B.md", incoming: false } }).edges.length, 0, "The inverse registry row does not add an outgoing citation from B");
    assert.equal(graphData(index, { ...options, local: { path: "B.md", outgoing: false } }).edges.length, 1);
  }
});

test("Inverse-only citations normalize direction while genuine reciprocal citations stay distinct", () => {
  const index = buildLinkIndex([note("A.md"), note("B.md")], [
    { id: "inverse-only", source: "B.md", target: "A.md", type: "cited_by" },
    { id: "opposite", source: "B.md", target: "A.md", type: "cites" },
  ]);
  const graph = graphData(index, { ...research, showRelations: false, showCitations: true });
  assert.deepEqual(graph.edges.map((edge) => [edge.source, edge.target, edge.type]), [["A.md", "B.md", "cites"], ["B.md", "A.md", "cites"]]);
  assert.deepEqual(graph.nodes.map((node) => node.degree), [2, 2]);
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

test("Generated alias references deduplicate equivalent targets and retain their citation provenance", () => {
  const canonical = "03_literature_synthesis/maps/map-one/clusters/Cluster.md";
  const compatibility = "03_literature_synthesis/clusters/Cluster.md";
  const text = "[[Shared]]\n<!-- auto-zettelkasten:literature:start -->\n[[Shared]]\n<!-- auto-zettelkasten:literature:end -->";
  const documents = [note(canonical, text), note(compatibility, text), note("Shared.md")];
  const metadata = { document_identities: [{ canonical_path: canonical, path_aliases: [compatibility], verified: true }] };
  for (const index of [buildLinkIndex(documents, [], null, metadata), buildLibraryIndex(documents.map(parseLibraryDocument), [], null, metadata)]) {
    const graph = graphData(index);
    assert.equal(graph.nodes.length, 2);
    assert.equal(graph.edges.length, 2, "One ordinary link and one citation fact survive alias collapse");
    assert.deepEqual(graph.nodes.map((node) => node.degree), [2, 2]);
    assert.equal(graph.edges.find((edge) => edge.layer === "reference").count, 2, "Both raw occurrences remain represented on one edge");
    const citation = graph.edges.find((edge) => edge.layer === "citation");
    assert.equal(citation.count, 1);
    assert.deepEqual(citation.provenance.map((row) => row.source), [canonical, compatibility]);
    assert.equal(graph.partial, false);
  }
});

test("A missing destination from a generated compatibility path remains visible", () => {
  const canonical = "03_literature_synthesis/maps/map-one/clusters/Cluster.md";
  const compatibility = "03_literature_synthesis/clusters/Cluster.md";
  const documents = [note(canonical, "[Context](./Context.md)"), note(compatibility, "[Context](./Context.md)"), note(canonical.replace("Cluster.md", "Context.md"))];
  const metadata = { document_identities: [{ canonical_path: canonical, path_aliases: [compatibility], verified: true }] };
  for (const index of [buildLinkIndex(documents, [], null, metadata), buildLibraryIndex(documents.map(parseLibraryDocument), [], null, metadata)]) {
    const graph = graphData(index);
    assert.equal(graph.edges.length, 2);
    assert.equal(graph.nodes.find((node) => node.path === canonical).degree, 2);
    assert.equal(graph.nodes.filter((node) => node.missing).length, 1);
    assert.equal(graph.unresolved.missing, 1);
    assert.equal(graph.partial, false, "All references were indexed; the missing destination is explicit");
    assert.equal(graphData(index, { existingOnly: true }).unresolved.hidden, 1);
  }
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
