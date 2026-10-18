import assert from "node:assert/strict";
import test from "node:test";
import { Worker } from "node:worker_threads";
import {
  LIBRARY_LIMITS, parseLibraryDocument, buildLibraryIndex, buildLinkIndex, resolveLink,
  backlinks, completionCandidates, graphData, renderIndexedMarkdown, renderReferenceEmbed, planRelocation,
} from "../../tools/workspace_links.js";
import { indexWorkerMessage } from "../../tools/workspace_index_worker.js";

const note = (path, text = "", extra = {}) => ({ path, text, revision: "v1", editable: true, ...extra });
const compact = (...documents) => documents.map(parseLibraryDocument);

test("Paged compact parsing indexes 7500 notes and 51MiB without losing nodes or references", () => {
  const documents = [];
  let page = [];
  let totalCharacters = 0;
  const flush = () => {
    const response = indexWorkerMessage({ id: documents.length, documents: page });
    assert.equal(response.error, undefined);
    documents.push(...response.documents);
    page = [];
  };
  for (let at = 0; at < 7500; at += 1) {
    const prefix = `---\naliases: [Alias ${at}]\ntags: [synthetic]\n---\n# Section ${at}\n\n${Array.from({ length: 12 }, (_, offset) => `[[Note ${(at + offset + 1) % 7500}]]`).join(" ")}\n\n`;
    const text = prefix + "Plain synthetic prose. ".repeat(400).slice(0, 7130 - prefix.length);
    totalCharacters += text.length;
    page.push(note(`Note ${at}.md`, text));
    if (page.length === LIBRARY_LIMITS.pageDocuments) flush();
  }
  if (page.length) flush();
  assert.ok(totalCharacters > 50 * 1024 * 1024);
  assert.equal(documents.length, 7500);
  assert.ok(documents.every((document) => !("text" in document) && !("tokens" in document) && !("frontmatter" in document)));
  const index = buildLibraryIndex(documents);
  assert.equal(index.partial, false);
  assert.equal(index.documents.size, 7500);
  assert.equal(index.references.length, 90000);
  assert.ok(index.references.every((reference) => reference.resolution.status === "resolved"));
  assert.equal(backlinks(index, "Note 0.md").reduce((sum, group) => sum + group.count, 0), 12);
  const graph = graphData(index, { existingOnly: true });
  assert.equal(graph.nodes.length, 7500);
  assert.equal(graph.edges.length, 90000);
  assert.equal(resolveLink(index, "Note 0.md", "Alias 7499#Section 7499").status, "resolved");
});

test("Compact and full snapshots share grammar, aliases, fragments, spans and backlinks", () => {
  const documents = [
    note("Start.md", "---\nexample: '[[Ghost]]'\n---\n[[Named alias#Section]] ![[Target#^block]] [link][id]\n\n[id]: Target.md#Section\n\n`[[Ghost]]` <!-- [[Ghost]] -->\n\n```md\n[[Ghost]]\n```"),
    note("Target.md", "---\naliases: [Named alias]\n---\n# Section\n\nParagraph ^block\n\n# Same\n\n# Same"),
  ];
  const full = buildLinkIndex(documents);
  const projected = buildLibraryIndex(compact(...documents));
  assert.equal(projected.partial, false);
  assert.deepEqual(projected.references, full.references);
  for (const destination of ["Target", "Named alias", "Target#Section", "Target#^block", "Target#same-1"]) assert.deepEqual(resolveLink(projected, "Start.md", destination), resolveLink(full, "Start.md", destination));
  assert.deepEqual(backlinks(projected, "Target.md"), backlinks(full, "Target.md"));
  const repeated = completionCandidates(projected, "Start.md", "Target#Same");
  assert.equal(repeated.length, 2);
  assert.ok(repeated.every((item) => /line \d/.test(item.detail)));
  assert.notEqual(repeated[0].insert, repeated[1].insert);
});

test("Open dirty overlays update aliases, headings and backlinks without mutating prior compact snapshots", () => {
  const source = parseLibraryDocument(note("Start.md", "[[Target#Old]] [[Alias]]"));
  const target = parseLibraryDocument(note("Target.md", "---\naliases: [Alias]\n---\n# Old"));
  const original = buildLibraryIndex([source, target]);
  const changed = buildLibraryIndex([source, note("Target.md", "# New", { revision: "v2" })], [], original);
  assert.equal(changed.references[0].resolution.status, "missing-fragment");
  assert.equal(changed.references[1].resolution.status, "missing");
  assert.equal(original.references[0].resolution.status, "resolved");
  assert.equal(original.references[1].resolution.status, "resolved");
  assert.equal(source.references[0].resolution, undefined);
  assert.equal(changed.documents.get("Target.md").text, "# New");
  assert.match(renderIndexedMarkdown(changed, "Target.md"), /<h1[^>]*>New<\/h1>/);
  const reused = buildLibraryIndex([source, note("Target.md", "# New", { revision: "v2" })], [], changed);
  assert.equal(reused.documents.get("Target.md").tokens, changed.documents.get("Target.md").tokens);
});

test("Compact embeds display a safe loading link then render only hydrated target bodies", () => {
  const records = compact(note("Start.md", "![[Target#Section]]"), note("Target.md", "# Section\n\nContent."));
  const index = buildLibraryIndex(records);
  const placeholder = renderReferenceEmbed(index, index.references[0]);
  assert.match(placeholder, /Loading embedded note…/);
  assert.match(placeholder, /data-note-destination="Target#Section"/);
  assert.doesNotMatch(placeholder, /Content\./);
  const hydrated = buildLibraryIndex([note("Start.md", "![[Target#Section]]"), note("Target.md", "# Section\n\nContent.")], [], index);
  const html = renderIndexedMarkdown(hydrated, "Start.md");
  assert.match(html, /Content\./);
  assert.match(html, /data-source-path="Target.md"/);
  assert.match(html, /data-source-path="Start.md"/);
  assert.doesNotMatch(html, /Loading embedded/);
});

test("Open source retention is bounded independently of complete catalog and link coverage", () => {
  const text = "p".repeat(LIBRARY_LIMITS.documentCharacters);
  const index = buildLibraryIndex(Array.from({ length: 10 }, (_, at) => note(`Open ${at}.md`, text)));
  assert.equal(index.documents.size, 10);
  assert.equal(index.partial, false);
  assert.equal([...index.documents.values()].reduce((sum, document) => sum + (document.text?.length || 0), 0), LIBRARY_LIMITS.openCharacters);
  assert.equal(index.documents.get("Open 9.md").compact, true);
  assert.equal(resolveLink(index, "Open 0.md", "Open 9").to, text.length);
  const compactDocument = parseLibraryDocument(note("Lazy.md", "# Hydrated title"));
  index.documents.set("Lazy.md", { ...compactDocument, text: "# Hydrated title" });
  assert.match(renderIndexedMarkdown(index, "Lazy.md"), /Hydrated title<\/h1>/);
});

test("Library typed relation coverage exceeds the legacy typed-edge budget", () => {
  const relations = Array.from({ length: 10001 }, (_, at) => ({ id: `r${at}`, source: "A.md", target: "B.md", type: "supports" }));
  const index = buildLibraryIndex(compact(note("A.md"), note("B.md")), relations);
  assert.equal(index.relations.length, 10001);
  assert.equal(index.partial, false);
});

test("Library per-note guards preserve unsupported nodes and make partial backlinks explicit", () => {
  const complex = note("Complex.md", "[]".repeat(LIBRARY_LIMITS.syntaxPerDocument / 2 + 1));
  const tooLarge = note("Large.md", "x".repeat(LIBRARY_LIMITS.documentCharacters + 1));
  const records = compact(complex, tooLarge, note("Lines.md", "a\n".repeat(LIBRARY_LIMITS.lines)), note("Target.md"), note("Source.md", "[[Target]]"));
  const index = buildLibraryIndex(records);
  assert.equal(index.documents.size, 5);
  assert.equal(graphData(index).nodes.length, 5);
  assert.equal(index.partial, true);
  assert.equal(index.documents.get("Complex.md").syntaxLimited, true);
  assert.equal(index.documents.get("Large.md").truncated, true);
  assert.equal(index.documents.get("Lines.md").truncated, true);
  assert.equal(backlinks(index, "Target.md")[0].partial, true);
  const full = buildLibraryIndex([complex]);
  assert.match(renderIndexedMarkdown(full, complex.path), /note-source-fallback/);
  assert.match(renderIndexedMarkdown(full, complex.path), /Live link analysis is paused/);
});

test("The larger per-note reference guard is truthful and retains the target graph node", () => {
  const source = parseLibraryDocument(note("Source.md", "[[Target]]\n".repeat(LIBRARY_LIMITS.references + 1)));
  assert.equal(source.references.length, LIBRARY_LIMITS.references);
  assert.equal(source.referenceLimited, true);
  const index = buildLibraryIndex([source, ...compact(note("Target.md"))]);
  assert.equal(index.documents.size, 2);
  assert.equal(index.references.length, 20000);
  assert.equal(index.partial, true);
  assert.equal(backlinks(index, "Target.md")[0].count, 20000);
  assert.equal(backlinks(index, "Target.md")[0].partial, true);
});

test("Compact libraries cannot authorize organization repair, even when link coverage is complete", () => {
  const index = buildLibraryIndex(compact(note("Source.md", "[[Target]]"), note("Target.md")));
  assert.equal(index.partial, false);
  const plan = planRelocation(index, [{ from: "Target.md", to: "New.md" }]);
  assert.equal(plan.safe, false);
  assert.deepEqual(plan.patches, []);
  assert.match(plan.warnings[0], /revision-bound source snapshot/);
  const invalid = buildLibraryIndex(compact(note("private/Secret.md"), note("Visible.md")));
  assert.equal(invalid.documents.has("private/Secret.md"), false);
  assert.equal(invalid.documents.has("Visible.md"), true);
  assert.equal(invalid.partial, true);
});

test("Worker page validation bounds raw inputs and preserves request IDs without retaining state", () => {
  assert.match(indexWorkerMessage({ id: "bad", documents: [{}] }).error, /Invalid library note source/);
  assert.match(indexWorkerMessage({ id: "bad", documents: Array.from({ length: 33 }, () => note("N.md")) }).error, /Invalid library indexing page/);
  assert.match(indexWorkerMessage({ id: "large", documents: [note("N.md", "x".repeat(LIBRARY_LIMITS.pageCharacters + 1))] }).error, /exceeds the source limit/);
  const first = indexWorkerMessage({ id: "first", documents: [note("N.md", "[[Target]]")] });
  const second = indexWorkerMessage({ id: "second", documents: [note("N.md", "Changed", { revision: "v2" })] });
  assert.equal(first.id, "first");
  assert.equal(first.documents[0].references.length, 1);
  assert.equal(second.id, "second");
  assert.equal(second.documents[0].references.length, 0);
  assert.equal(second.documents[0].revision, "v2");
});

test("Worker entry handles actual off-thread messages using the browser envelope", async () => {
  const entry = new URL("../../tools/workspace_index_worker.js", import.meta.url).href;
  const worker = new Worker(new URL(`data:text/javascript,${encodeURIComponent(`import { parentPort } from "node:worker_threads"; globalThis.self = { postMessage: value => parentPort.postMessage(value), addEventListener: (_name, listener) => parentPort.on("message", data => listener({data})) }; await import(${JSON.stringify(entry)});`)}`));
  try {
    const response = await new Promise((resolve, reject) => {
      worker.once("message", resolve);
      worker.once("error", reject);
      worker.postMessage({ id: "off-thread", documents: [note("N.md", "# Title\n\n[[Target]]")] });
    });
    assert.equal(response.id, "off-thread");
    assert.equal(response.documents[0].headings[0].title, "Title");
    assert.equal(response.documents[0].references.length, 1);
    assert.equal("text" in response.documents[0], false);
    assert.equal("tokens" in response.documents[0], false);
  } finally { await worker.terminate(); }
});
