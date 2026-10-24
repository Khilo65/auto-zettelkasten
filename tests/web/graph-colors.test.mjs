import assert from "node:assert/strict";
import test from "node:test";
import cytoscape from "cytoscape";
import { JSDOM } from "jsdom";
import { filterGraphOrigins, graphCommunityPalette, graphLegend, graphNodeAppearance, graphNodeOrigin, normalizeGraphPreferences } from "../../tools/workspace_graph_colors.js";
import { buildLibraryIndex, buildLinkIndex, graphData, parseLibraryDocument } from "../../tools/workspace_links.js";
import { createGraphView } from "../../tools/workspace_graph_view.js";

const revision = "1".repeat(64);
const membership = { community_id: "semantic-0123456789abcdefghij", algorithm: "leiden" };

test("Versioned preferences safely default all origins on and preserve independent toggles", () => {
  const defaults = { version: 1, colorBy: "none", origins: { managed: true, personal: true, unknown: true } };
  for (const value of [null, undefined, false, "community", [], Object.assign([], { colorBy: "origin" }), Object.create({ colorBy: "origin", origins: { managed: false } }),
    { version: 2, colorBy: "origin", origins: { unknown: false } }, { origins: Object.create({ managed: false }) }]) assert.deepEqual(normalizeGraphPreferences(value), defaults);
  assert.deepEqual(normalizeGraphPreferences({ version: 1, colorBy: "community", origins: { managed: false, personal: "false" }, ignored: true }),
    { ...defaults, colorBy: "community", origins: { managed: false, personal: true, unknown: true } });
  assert.deepEqual(normalizeGraphPreferences(JSON.parse(JSON.stringify(defaults))), defaults);
});

test("Origin projection trusts explicit backend metadata, never folders, editability or frontmatter", () => {
  const docs = [
    { path: "Managed.md", text: "# Edited source\n", revision, editable: true, note_origin: "managed" },
    { path: "Personal.md", text: "# Personal\n", revision, editable: true, note_origin: "personal" },
    { path: "02_source_memory/notes/Old.md", text: "---\nnote_origin: personal\n---\n# Imported\n", revision, editable: false },
  ];
  for (const index of [buildLinkIndex(docs), buildLibraryIndex(docs.map(parseLibraryDocument))]) {
    assert.deepEqual(graphData(index).nodes.map((node) => node.note_origin), ["managed", "personal", "unknown"]);
    assert.equal(graphNodeOrigin({ note_origin: "personal", missing: true }), "unknown");
    assert.equal(graphNodeOrigin({ note_origin: "manual", editable: true }), "unknown");
    const refreshed = buildLibraryIndex(docs.map((row) => parseLibraryDocument({ ...row, note_origin: undefined })), [], index);
    assert.ok(graphData(refreshed).nodes.every((node) => node.note_origin === "unknown"), "Reusing parsed bodies must not retain obsolete origin claims");
  }
});

test("Every origin filter combination retains only incident enabled edges without mutating registry data", () => {
  const data = {
    nodes: ["managed", "personal", "unknown"].map((origin) => Object.freeze({ id: origin, note_origin: origin, degree: 99 })),
    edges: [
      { id: "accepted", source: "managed", target: "personal", layer: "relation", accepted: true },
      { id: "discovery", source: "managed", target: "unknown", layer: "discovery", accepted: false },
      { id: "citation", source: "personal", target: "unknown", layer: "citation" },
      { id: "reference", source: "unknown", target: "unknown", layer: "reference" },
    ].map(Object.freeze), partial: false,
  };
  Object.freeze(data.nodes); Object.freeze(data.edges); Object.freeze(data);
  const before = JSON.stringify(data);
  for (let bits = 0; bits < 8; bits++) {
    const origins = Object.fromEntries(["managed", "personal", "unknown"].map((origin, index) => [origin, Boolean(bits & 1 << index)]));
    const result = filterGraphOrigins(data, origins), visible = new Set(result.nodes.map((node) => node.id));
    assert.deepEqual(result.nodes.map((node) => node.id), Object.keys(origins).filter((origin) => origins[origin]));
    assert.deepEqual(result.edges, data.edges.filter((edge) => visible.has(edge.source) && visible.has(edge.target)));
    for (const node of result.nodes) assert.equal(node.degree, result.edges.reduce((count, edge) => count + Number(edge.source === node.id) + Number(edge.target === node.id), 0));
  }
  assert.equal(JSON.stringify(data), before);
  assert.equal(filterGraphOrigins(data, { managed: false }).edges.some((edge) => edge.id === "accepted"), false);
  assert.equal(data.edges[0].accepted, true);
  assert.deepEqual(filterGraphOrigins(null).nodes, []);
});

test("Community colors remain deterministic across ordering, filtering, serialization and themes", () => {
  const first = { id: "A", semantic_communities: [membership] }, second = { id: "B", semantic_communities: [{ ...membership, community_id: "semantic-another" }] };
  for (const dark of [false, true]) {
    const options = { colorBy: "community", dark }, original = graphNodeAppearance(first, options);
    assert.deepEqual(graphNodeAppearance(JSON.parse(JSON.stringify(first)), options), original);
    for (const nodes of [[first], [second, first], [first, second]]) {
      const legend = graphLegend(nodes, options);
      assert.deepEqual(legend.find((entry) => entry.key === original.key), { ...original, count: 1 });
    }
    assert.deepEqual(graphLegend([first, first], options), [{ ...original, count: 2 }]);
  }
  assert.notEqual(graphNodeAppearance(first, { colorBy: "community", dark: false }).color, graphNodeAppearance(first, { colorBy: "community", dark: true }).color);
});

test("Saved community palettes avoid color collisions and survive filtered views at every scale", () => {
  const communities = Array.from({ length: 96 }, (_, index) => ({ community_id: `semantic-${String(index).padStart(3, "0")}`, algorithm: "leiden" }));
  const semantic = { status: "current", communities, community_scales: [
    { scale: "default", communities }, { scale: "fine", communities: communities.slice(24) },
  ] };
  const before = JSON.stringify(semantic), palette = graphCommunityPalette(semantic);
  assert.deepEqual(graphCommunityPalette({ ...semantic, community_scales: [{ scale: "default", communities: [...communities].reverse() }] }), palette);
  const nodes = communities.map((row) => ({ semantic_communities: [row] }));
  for (const dark of [false, true]) {
    const options = { colorBy: "community", dark, communityPalette: palette };
    const colors = nodes.map((node) => graphNodeAppearance(node, options).color);
    assert.equal(new Set(colors).size, 96, "Different communities must not hash into the same twelve colors");
    assert.ok(new Set(colors.slice(0, 24).map((color) => /oklch\((\d+)%/.exec(color)[1])).size > 1, "Larger maps also vary lightness");
    const focused = graphLegend([nodes[51], nodes[3]], options);
    assert.equal(focused.find((entry) => entry.key.endsWith(communities[51].community_id)).color, colors[51]);
    assert.equal(graphNodeAppearance(nodes[3], { ...options, communityPalette: JSON.parse(JSON.stringify(palette)) }).color, colors[3]);
  }
  assert.equal(Object.keys(graphCommunityPalette(semantic, "fine")).length, 72);
  assert.deepEqual(graphCommunityPalette({ ...semantic, status: "unavailable" }), {});
  assert.deepEqual(graphCommunityPalette({ communities: [{ ...membership, noise: true }, { ...membership, community_id: "<script>" }] }), {});
  assert.equal(JSON.stringify(semantic), before);
});

test("Larger community palettes maintain perceptual separation in both themes without gamut clipping", () => {
  const communities = Array.from({ length: 128 }, (_, index) => ({ community_id: `semantic-${String(index).padStart(3, "0")}`, algorithm: "leiden" }));
  const communityPalette = graphCommunityPalette({ communities });
  for (const dark of [false, true]) {
    const colors = communities.map((row) => {
      const color = graphNodeAppearance({ semantic_communities: [row] }, { colorBy: "community", dark, communityPalette }).color;
      const [, l, c, h] = /^oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)$/.exec(color);
      const angle = Number(h) * Math.PI / 180, L = Number(l) / 100, a = Number(c) * Math.cos(angle), b = Number(c) * Math.sin(angle);
      const x = (L + .3963377774 * a + .2158037573 * b) ** 3;
      const y = (L - .1055613458 * a - .0638541728 * b) ** 3;
      const z = (L - .0894841775 * a - 1.291485548 * b) ** 3;
      const rgb = [4.0767416621 * x - 3.3077115913 * y + .2309699292 * z,
        -1.2684380046 * x + 2.6097574011 * y - .3413193965 * z, -.0041960863 * x - .7034186147 * y + 1.707614701 * z];
      assert.ok(rgb.every((value) => value >= -.00001 && value <= 1.00001), `${color} stays within sRGB`);
      return [L, a, b];
    });
    for (let left = 0; left < colors.length; left++) for (let right = 0; right < left; right++) {
      const distance = Math.hypot(...colors[left].map((value, index) => value - colors[right][index]));
      assert.ok(distance > (left < 24 ? .08 : left < 96 ? .04 : .027), `Communities ${left} and ${right} should remain separated in ${dark ? "dark" : "light"} mode`);
    }
  }
});

test("Unassigned, stale, missing and density noise stay neutral without erasing another source's current Leiden membership", () => {
  const neutral = graphNodeAppearance({}, { colorBy: "community" });
  for (const node of [{}, { semantic_stale: true, semantic_communities: [membership] }, { semantic_status: "unavailable", semantic_communities: [membership] },
    { missing: true, semantic_communities: [membership] }, { semantic_communities: [{ ...membership, noise: true }] },
    { semantic_communities: [{ ...membership, algorithm: "hdbscan" }] }, { semantic_communities: [{ ...membership, stale: true }] },
    { semantic_communities: [{ ...membership, status: "stale" }] }, { semantic_communities: [{ ...membership, community_id: "<script>" }] }]) assert.deepEqual(graphNodeAppearance(node, { colorBy: "community" }), neutral);
  const docs = ["A.md", "B.md"].map((path) => ({ path, text: "# Source\n", revision, note_origin: "managed" }));
  const semantic = { status: "current", identity_digest: "2".repeat(64), note_revisions: { "A.md": revision, "B.md": revision }, communities: [{ ...membership, paths: ["A.md", "B.md"] }] };
  const initial = buildLinkIndex(docs, [], null, { semantic_map: semantic });
  const changed = buildLinkIndex(docs.map((doc) => doc.path === "A.md" ? { ...doc, revision: "3".repeat(64) } : doc), [], initial, { semantic_map: semantic });
  const projected = graphData(changed).nodes;
  assert.deepEqual(graphNodeAppearance(projected[0], { colorBy: "community" }), neutral);
  assert.notEqual(graphNodeAppearance(projected[1], { colorBy: "community" }).key, neutral.key);
});

function canvasHarness(t) {
  const dom = new JSDOM("<html data-theme='light'><body><div id='graph'></div></body></html>", { pretendToBeVisual: true });
  const win = dom.window, container = win.document.querySelector("div"), callbacks = new Map(), frames = [];
  let sequence = 0, now = 0;
  win.requestAnimationFrame = (callback) => { callbacks.set(++sequence, callback); return sequence; };
  win.cancelAnimationFrame = (id) => callbacks.delete(id);
  win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  container.getBoundingClientRect = () => ({ width: 400, height: 300 });
  win.CanvasRenderingContext2D = class {};
  const context = {
    setTransform() {}, clearRect() { frames.push({ fills: [], outlines: [] }); }, beginPath() { this.circle = null; },
    arc(x, y, radius) { this.circle = { x, y, radius }; }, moveTo() {}, lineTo() {}, quadraticCurveTo() {}, setLineDash() {},
    fill() { frames.at(-1).fills.push({ ...this.circle, color: this.fillStyle }); },
    stroke() { if (this.circle) frames.at(-1).outlines.push({ ...this.circle, color: this.strokeStyle }); },
    fillText() {}, strokeText() {}, measureText(text) { return { width: text.length * 6 }; },
  };
  win.HTMLCanvasElement.prototype.getContext = () => context;
  const graph = cytoscape({ headless: true, styleEnabled: false, container, layout: { name: "preset" }, elements: [
    { data: { id: "A", note_origin: "managed", semantic_communities: [membership] }, position: { x: 100, y: 100 } },
  ] });
  const view = createGraphView(graph, { container, labels: false, colorBy: "origin", dark: false });
  const settle = () => { for (let at = 0; callbacks.size && at < 200; at++) { now += 16; const pending = [...callbacks.values()]; callbacks.clear(); for (const callback of pending) callback(now); } assert.equal(callbacks.size, 0); };
  t.after(() => { view.destroy(); graph.destroy(); win.close(); });
  return { win, graph, view, settle, frames };
}

test("Canvas recoloring and selection use the shared resolver, preserve fills and never change geometry", (t) => {
  const app = canvasHarness(t), node = app.graph.$id("A"); app.settle();
  const position = { ...node.position() }, pan = { ...app.graph.pan() }, zoom = app.graph.zoom();
  let layouts = 0; app.graph.on("layoutstart", () => layouts++);
  for (const colorBy of ["origin", "community", "none"]) for (const dark of [false, true]) {
    const communityPalette = { [membership.community_id]: dark ? 2 : 1 };
    app.view.setAppearance({ colorBy, dark, communityPalette }); app.settle();
    const expected = graphNodeAppearance(node.data(), { colorBy, dark, communityPalette }).color;
    assert.equal(app.frames.at(-1).fills[0].color, expected);
    node.select(); node.data("current", "yes"); node.addClass("hovered neighbor"); app.settle();
    assert.equal(app.frames.at(-1).fills[0].color, expected);
    assert.ok(app.frames.at(-1).outlines.some((outline) => outline.radius > app.frames.at(-1).fills[0].radius));
    node.unselect(); node.data("current", "no"); node.removeClass("hovered neighbor"); app.settle();
  }
  assert.deepEqual({ ...node.position() }, position); assert.deepEqual({ ...app.graph.pan() }, pan); assert.equal(app.graph.zoom(), zoom); assert.equal(layouts, 0);
});

test("Automatic light/dark repaint uses the same resolver and disconnects on destruction", async (t) => {
  const app = canvasHarness(t), node = app.graph.$id("A"); app.view.setAppearance({ colorBy: "origin", dark: null }); app.settle();
  assert.equal(app.frames.at(-1).fills[0].color, graphNodeAppearance(node.data(), { colorBy: "origin", dark: false }).color);
  app.win.document.documentElement.dataset.theme = "dark"; await Promise.resolve(); app.settle();
  assert.equal(app.frames.at(-1).fills[0].color, graphNodeAppearance(node.data(), { colorBy: "origin", dark: true }).color);
  app.view.destroy(); app.win.document.documentElement.dataset.theme = "light"; await Promise.resolve(); app.settle();
});
