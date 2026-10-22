import assert from "node:assert/strict";
import test from "node:test";
import { buildLinkIndex, buildLibraryIndex, graphData, parseLibraryDocument } from "../../tools/workspace_links.js";

const digest = "1".repeat(64), revision = "2".repeat(64);
const documents = ["A.md", "B.md"].map((path) => ({ path, text: "# Synthetic source\n", revision, editable: false }));
const semantic = { status: "current", identity_digest: digest, note_revisions: { "A.md": revision, "B.md": revision },
  communities: [{ community_id: "semantic-pair", algorithm: "leiden", paths: ["A.md", "B.md"] }] };
const suggestion = { id: "semantic:pair", source: "A.md", target: "B.md", type: "semantic_similarity", derived: true,
  decision_status: "suggested", accepted: false, evidence_backed: false, score: 0.75,
  provenance: { kind: "local_embedding_similarity", embedding_identity: digest, source_chunk_ids: ["chunk-a"], target_chunk_ids: ["chunk-b"], private_path: "/do/not/expose" } };
const research = { showReferences: false, showCitations: false, showDiscovery: false, showNavigation: false };

test("Local semantic neighbors stay separate from accepted relations and citations, with communities on source nodes", () => {
  const relations = [{ id: "accepted", source: "A.md", target: "B.md", type: "supports" },
    { id: "cites", source: "A.md", target: "B.md", type: "cites" }, suggestion];
  for (const index of [buildLinkIndex(documents, relations, null, { semantic_map: semantic }),
    buildLibraryIndex(documents.map(parseLibraryDocument), relations, null, { semantic_map: semantic })]) {
    const accepted = graphData(index, research);
    assert.equal(accepted.nodes.length, 2, "Community membership must not add an artificial hub node");
    assert.deepEqual(accepted.edges.map((row) => row.type), ["supports"]);
    for (const node of accepted.nodes) assert.deepEqual(node.semantic_communities, [{ community_id: "semantic-pair", algorithm: "leiden" }]);
    const discovery = graphData(index, { ...research, showRelations: false, showDiscovery: true });
    assert.equal(discovery.edges.length, 1);
    const [edge] = discovery.edges;
    assert.equal(edge.layer, "discovery");
    assert.equal(edge.score, 0.75);
    assert.equal(edge.accepted, false);
    assert.equal(edge.evidence_backed, false);
    assert.deepEqual(edge.provenance.source_chunk_ids, ["chunk-a"]);
    assert.equal("private_path" in edge.provenance, false);
    assert.deepEqual(graphData(index, { ...research, showRelations: false, showCitations: true }).edges.map((row) => row.type), ["cites"]);
  }
});

test("Stale note revisions and removed semantic metadata cannot retain cached communities or suggestions", () => {
  const initial = buildLinkIndex(documents, [suggestion], null, { semantic_map: semantic });
  const changed = documents.map((row) => row.path === "A.md" ? { ...row, revision: "3".repeat(64) } : row);
  const stale = buildLinkIndex(changed, [suggestion], initial, { semantic_map: semantic });
  assert.equal(stale.relations.length, 0);
  assert.equal(stale.semantic_map.status, "partial");
  assert.equal(stale.documents.get("A.md").semantic_communities, undefined);
  assert.ok(stale.documents.get("B.md").semantic_communities);
  const noCache = buildLinkIndex(documents, [suggestion], initial);
  assert.equal(noCache.relations.length, 0);
  assert.ok([...noCache.documents.values()].every((row) => row.semantic_communities === undefined));
  const compact = buildLibraryIndex(documents.map(parseLibraryDocument), [suggestion], null, { semantic_map: semantic });
  const compactNoCache = buildLibraryIndex([...compact.documents.values()], [suggestion], compact);
  assert.equal(compactNoCache.relations.length, 0);
  assert.ok([...compactNoCache.documents.values()].every((row) => row.semantic_communities === undefined));
});

test("Malformed or promoted derived records cannot enter research or citation layers", () => {
  for (const altered of [{ accepted: true }, { evidence_backed: true }, { type: "supports" }, { type: "cites" },
    { score: NaN }, { provenance: { ...suggestion.provenance, embedding_identity: "4".repeat(64) } }]) {
    const index = buildLinkIndex(documents, [{ ...suggestion, ...altered }], null, { semantic_map: semantic });
    assert.equal(index.relations.length, 0);
  }
});

test("Optional density noise stays explicit on source memberships and is never a shared hub", () => {
  const metadata = { ...semantic, communities: documents.map((row, index) => ({ community_id: `semantic-noise-${index}`,
    algorithm: "hdbscan", paths: [row.path], noise: true })) };
  for (const index of [buildLinkIndex(documents, [], null, { semantic_map: metadata }),
    buildLibraryIndex(documents.map(parseLibraryDocument), [], null, { semantic_map: metadata })]) {
    const graph = graphData(index, research);
    assert.equal(graph.nodes.length, 2);
    assert.equal(graph.edges.length, 0);
    assert.equal(graph.semantic_map.communities.filter((row) => row.noise === true).length, 2);
    for (const node of graph.nodes) {
      assert.equal(node.semantic_communities.length, 1);
      assert.equal(node.semantic_communities[0].noise, true);
      assert.equal(node.semantic_communities[0].algorithm, "hdbscan");
    }
  }
});
