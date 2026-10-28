import assert from "node:assert/strict";
import test from "node:test";
import { buildLibraryIndex, buildLinkIndex, graphData, parseLibraryDocument } from "../../tools/workspace_links.js";
import { graphCommunityMembership, graphNodeAppearance } from "../../tools/workspace_graph_colors.js";
import { communityHull, filterGraphDiscoveryScopes, focusGraphCommunity, graphCommunities, graphCommunityScales } from "../../tools/workspace_graph_communities.js";

const revision = "1".repeat(64), identity_digest = "2".repeat(64);
const docs = ["A.md", "B.md", "C.md", "D.md"].map((path) => ({ path, revision, text: `# ${path}` }));
const row = (id, paths, extra = {}) => ({ community_id: id, algorithm: "leiden", paths, ...extra });
const semantic = { status: "current", identity_digest, note_revisions: Object.fromEntries(docs.map((doc) => [doc.path, revision])),
  communities: [row("ab", ["A.md", "B.md"], { display_label: "Institutions and contestation" }), row("cd", ["C.md", "D.md"])],
  community_scales: [{ scale: "broad", resolution: .5, communities: [row("all", docs.map((doc) => doc.path))] }, { scale: "fine", resolution: 2, communities: docs.map((doc) => row(`fine-${doc.path}`, [doc.path])) }] };
const edges = [{ source: "A.md", target: "C.md", type: "supports" }, { source: "C.md", target: "D.md", type: "qualifies" }];
const graph = (documents = docs) => graphData(buildLinkIndex(documents, edges, null, { semantic_map: semantic }), { showNavigation: false });

test("Focus retains members and directly connected external bridges without inventing edges or degrees", () => {
  const data = graph(), original = JSON.stringify(data), focused = focusGraphCommunity(data, "ab");
  assert.deepEqual(focused.nodes.map((node) => node.id), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(focused.nodes.map((node) => node.degree), [1, 0, 1]);
  assert.equal(focused.nodes.find((node) => node.id === "C.md").community_bridge, true);
  assert.deepEqual(focused.edges.map((edge) => edge.type), ["supports"]);
  assert.equal(JSON.stringify(data), original);
  assert.equal(focusGraphCommunity(data, "unavailable"), data);
});

test("Scale memberships are independent, labels are readable, and legacy maps remain readable", () => {
  const data = graph();
  assert.deepEqual(graphCommunityScales(data.semantic_map), ["broad", "default", "fine"]);
  assert.equal(graphCommunities(data)[0].label, "Institutions and contestation");
  assert.equal(graphCommunities(data, "fine").length, 4);
  assert.equal(graphCommunities(data, "broad")[0].members.length, 4);
  const node = data.nodes[0];
  assert.equal(graphCommunityMembership(node, "fine").community_id, "fine-A.md");
  assert.equal(graphNodeAppearance(node, { colorBy: "community" }).label, "Institutions and contestation");
  assert.notEqual(graphNodeAppearance(node, { colorBy: "community", communityScale: "fine" }).key, graphNodeAppearance(node, { colorBy: "community" }).key);
  const legacy = graphData(buildLinkIndex(docs, [], null, { semantic_map: { ...semantic, community_scales: undefined } }));
  assert.deepEqual(graphCommunityScales(legacy.semantic_map), ["default"]);
});

test("Changed note revisions remove stale memberships at every scale and unavailable maps expose no labels", () => {
  const data = graph(docs.map((doc) => doc.path === "A.md" ? { ...doc, revision: "3".repeat(64) } : doc));
  assert.equal(data.semantic_map.status, "partial");
  assert.equal(graphCommunityMembership(data.nodes[0]), null);
  for (const scale of graphCommunityScales(data.semantic_map)) assert.ok(graphCommunities(data, scale).every((community) => community.members.every((node) => node.path !== "A.md")));
  assert.deepEqual(graphCommunities({ ...data, semantic_map: { ...data.semantic_map, status: "unavailable" } }), []);
});

test("Navigation documents remain absent from global and local source graphs when navigation is disabled", () => {
  const index = buildLinkIndex([...docs, { path: "04_indexes/INDEX.md", text: "# Index\n[[A]]\n[[B]]\n", navigation: true }], edges);
  for (const local of [undefined, { path: "A.md", depth: 3 }]) {
    const data = graphData(index, { showNavigation: false, local });
    assert.ok(data.nodes.every((node) => !node.path.startsWith("04_indexes/")));
    assert.ok(data.edges.every((edge) => edge.layer !== "navigation"));
  }
});

test("Region hulls omit interior points without changing supplied coordinates", () => {
  const points = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 5, y: 5 }, { x: 10, y: 10 }, { x: 0, y: 10 }], before = JSON.stringify(points);
  assert.equal(communityHull(points).length, 4); assert.equal(JSON.stringify(points), before);
  assert.deepEqual(communityHull([]), []);
});

test("Discovery display density never changes saved candidates, accepted relations or citations", async () => {
  const { limitGraphDiscovery } = await import("../../tools/workspace_graph_communities.js");
  const nodes = Array.from({ length: 20 }, (_, at) => ({ id: String(at), degree: 0 }));
  const discovery = nodes.slice(1).map((node, at) => ({ id: `d${at}`, source: "0", target: node.id, layer: "discovery", type: "semantic_similarity", score: 1 - at / 100 }));
  const accepted = { id: "accepted", source: "0", target: "19", layer: "relation", type: "supports" }, citation = { id: "citation", source: "0", target: "18", layer: "citation", type: "cites" };
  const data = { nodes, edges: [...discovery, accepted, citation] }, before = JSON.stringify(data);
  const sparse = limitGraphDiscovery(data, 8);
  assert.equal(sparse.hidden_discovery_edges, 11); assert.equal(sparse.edges.length, 10);
  assert.ok(sparse.edges.includes(accepted)); assert.ok(sparse.edges.includes(citation));
  assert.equal(limitGraphDiscovery(data, "all").edges.length, 21);
  assert.equal(JSON.stringify(data), before);
});

test("Sparse discovery caps apply to both endpoints with deterministic score ties", async () => {
  const { limitGraphDiscovery } = await import("../../tools/workspace_graph_communities.js");
  const nodes = Array.from({ length: 7 }, (_, index) => ({ id: String(index), degree: 99 }));
  const discovery = [];
  for (let source = 0; source < nodes.length; source += 1) {
    for (let target = source + 1; target < nodes.length; target += 1) {
      discovery.push({ id: `${source}-${target}`, source: String(source), target: String(target),
        layer: "discovery", type: "semantic_similarity", score: .8 });
    }
  }
  const reference = { id: "manual", source: "0", target: "6", layer: "reference", type: "link" };
  const citation = { id: "citation", source: "0", target: "5", layer: "citation", type: "cites" };
  const data = { nodes, edges: [...discovery, reference, citation], marker: "retained" }, before = JSON.stringify(data);
  for (const cap of [2, 4, 8]) {
    const selected = limitGraphDiscovery(data, cap), reordered = limitGraphDiscovery({ ...data, edges: [...data.edges].reverse() }, cap);
    const ids = (value) => value.edges.filter((edge) => edge.layer === "discovery").map((edge) => edge.id).sort();
    assert.deepEqual(ids(selected), ids(reordered));
    assert.equal(selected.nodes.length, nodes.length); assert.equal(selected.marker, "retained");
    assert.ok(selected.edges.includes(reference)); assert.ok(selected.edges.includes(citation));
    assert.equal(selected.hidden_discovery_edges, discovery.length - ids(selected).length);
    for (const node of selected.nodes) {
      assert.ok(selected.edges.filter((edge) => edge.layer === "discovery" && [edge.source, edge.target].includes(node.id)).length <= cap);
      assert.equal(node.degree, selected.edges.filter((edge) => [edge.source, edge.target].includes(node.id)).length);
    }
  }
  assert.deepEqual(limitGraphDiscovery(data).edges, limitGraphDiscovery(data, 8).edges);
  assert.deepEqual(limitGraphDiscovery(data, 3).edges, limitGraphDiscovery(data, 8).edges);
  assert.equal(JSON.stringify(data), before);
});

test("Experimental graph-value priority changes actual capped selection only when explicitly enabled", async () => {
  const { limitGraphDiscovery } = await import("../../tools/workspace_graph_communities.js");
  const nodes = ["A", "B", "C", "D", "E", "F"].map((id) => ({ id }));
  const make = (target, score, graph_value_priority) => ({ id: `A-${target}`, source: "A", target,
    layer: "discovery", type: "semantic_similarity", score, graph_value_priority });
  const edges = [make("B", .9), make("C", .8), make("D", .2, 2), make("E", .1, 1), make("F", .01, "2")];
  const manual = { id: "manual", source: "A", target: "F", layer: "reference", graph_value_priority: 2 };
  const data = { nodes, edges: [...edges, manual] }, original = JSON.stringify(data);
  const ids = (value) => value.edges.filter((edge) => edge.layer === "discovery").map((edge) => edge.id);
  assert.deepEqual(ids(limitGraphDiscovery(data, 2)), ["A-B", "A-C"]);
  assert.deepEqual(ids(limitGraphDiscovery(data, 2, { graphValuePriority: true })), ["A-D", "A-E"]);
  assert.deepEqual(ids(limitGraphDiscovery(data, 2, { graphValuePriority: "true" })), ["A-B", "A-C"]);
  assert.deepEqual(ids(limitGraphDiscovery({ nodes, edges: [make("B", .9, -1), make("C", .8, Infinity), make("D", .7, 99)] }, 2,
    { graphValuePriority: true })), ["A-B", "A-C"]);
  assert.ok(limitGraphDiscovery(data, 2, { graphValuePriority: true }).edges.includes(manual));
  assert.equal(JSON.stringify(data), original);
});

test("Search-scope filtering changes only semantic suggestions and retains palette metadata", () => {
  const data = graph(), make = (id, scopes) => ({ id, source: "A.md", target: "B.md", layer: "discovery", type: "semantic_similarity", provenance: scopes && { discovery_scopes: scopes } });
  const citation = { id: "citation", source: "B.md", target: "C.md", type: "cites", layer: "citation" };
  const markdown = { id: "markdown", source: "B.md", target: "D.md", layer: "reference" };
  data.edges.push(citation, markdown, make("within", ["within_collection"]), make("library", ["whole_library"]), make("both", ["within_collection", "whole_library"]), make("legacy"));
  const before = JSON.stringify(data);
  for (const [scope, retained] of [["within_collection", ["within", "both"]], ["whole_library", ["library", "both"]]]) {
    const selected = filterGraphDiscoveryScopes(data, scope);
    assert.deepEqual(selected.edges.filter((edge) => edge.layer === "discovery").map((edge) => edge.id), retained);
    assert.ok(selected.edges.includes(citation)); assert.ok(selected.edges.includes(markdown));
    assert.ok(selected.edges.some((edge) => edge.type === "supports"));
    assert.equal(selected.hidden_discovery_scope_edges, 2);
    assert.equal(selected.semantic_map, data.semantic_map);
    for (const node of selected.nodes) assert.equal(node.degree, selected.edges.reduce((sum, edge) => sum + Number(edge.source === node.id) + Number(edge.target === node.id), 0));
  }
  assert.equal(filterGraphDiscoveryScopes(data).edges.length, data.edges.length);
  assert.equal(JSON.stringify(data), before);
});

test("Both frontend indexes validate scoped metadata and preserve legacy suggestions", () => {
  const derived = { id: "suggestion", source: "A.md", target: "B.md", type: "semantic_similarity", derived: true, accepted: false, evidence_backed: false, decision_status: "suggested", score: .8,
    provenance: { embedding_identity: identity_digest, source_chunk_ids: ["chunk-a"], target_chunk_ids: ["chunk-b"] } };
  for (const build of [() => (relations) => buildLinkIndex(docs, relations, null, { semantic_map: semantic }), () => (relations) => buildLibraryIndex(docs.map(parseLibraryDocument), relations, null, { semantic_map: semantic })]) {
    const index = build();
    assert.equal(index([derived]).relations[0].provenance.discovery_scopes, undefined);
    const scoped = { ...derived, provenance: { ...derived.provenance, discovery_scopes: ["within_collection", "whole_library"], shared_collection_keys: ["collection-synthetic"] } };
    assert.deepEqual(graphData(index([scoped]), { showDiscovery: true }).edges[0].provenance.discovery_scopes, ["whole_library", "within_collection"]);
    for (const malformed of [{ discovery_scopes: "within_collection" }, { discovery_scopes: ["other"] }, { discovery_scopes: [] }, { discovery_scopes: ["whole_library", "whole_library"] }, { shared_collection_keys: ["bad\nkey"] }, { shared_collection_keys: ["x".repeat(257)] }]) {
      assert.equal(index([{ ...derived, provenance: { ...derived.provenance, ...malformed } }]).relations.length, 0);
    }
  }
});

test("Community connection details distinguish discovery scores, citations and inferred evidence", async () => {
  const { JSDOM } = await import("jsdom"), { appendCommunityConnections } = await import("../../tools/workspace_graph_communities.js");
  const dom = new JSDOM("<aside></aside>"), panel = dom.window.document.querySelector("aside"), data = graph();
  data.edges.push({ id: "similar", source: "A.md", target: "B.md", layer: "discovery", type: "semantic_similarity", score: .73, derived: true,
    provenance: { discovery_scopes: ["within_collection", "whole_library"], shared_collection_keys: ["opaque-key-not-a-label"] } });
  data.edges.push({ id: "citation", source: "B.md", target: "C.md", layer: "citation", type: "cites" });
  data.edges[0] = { ...data.edges[0], confidence: .61, evidence_basis: "note_summary", selected_evidence: [{ path: "A.md", text: "A source claim", locator: { heading: "Thesis" } }, { path: "D.md", text: "Fabricated unrelated endpoint" }] };
  const opened = [], details = appendCommunityConnections(panel, graphCommunities(data)[0], data, { onOpen: (...args) => opened.push(args) });
  assert.equal(details.querySelectorAll(".relationship-record").length, 0, "Connection details render only when requested");
  details.open = true; details.dispatchEvent(new dom.window.Event("toggle"));
  assert.match(details.textContent, /similarity 0.730/); assert.match(details.textContent, /Current mapped note revisions/);
  assert.match(details.textContent, /Citation record/); assert.match(details.textContent, /model confidence 61%/);
  assert.match(details.textContent, /Within-collection search and Whole-library search/); assert.match(details.textContent, /1 shared collection/);
  assert.doesNotMatch(details.textContent, /opaque-key-not-a-label/);
  assert.match(details.textContent, /A source claim/); assert.doesNotMatch(details.textContent, /Fabricated unrelated endpoint/);
  details.querySelector(".relationship-evidence").click(); assert.deepEqual(opened, [["A.md", { heading: "Thesis" }]]);
  dom.window.close();
});
