import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import {
  LIMITS, parseDocument, buildLinkIndex, resolveLink, backlinks, planRelocation,
  completionCandidates, searchNotes, graphData, renderIndexedMarkdown, renderReferenceEmbed,
} from "../../tools/workspace_links.js";

const note = (path, text = "", extra = {}) => ({ path, text, revision: "v1", editable: true, ...extra });
const paths = (graph) => graph.nodes.map((node) => node.path || node.label).sort();
const edgePairs = (graph) => graph.edges.map((edge) => `${edge.source}>${edge.target}:${edge.layer}`).sort();

test("Markdown grammar excludes YAML, escaped examples, code and comments while preserving real occurrences and spans", () => {
  const text = [
    "---", "aliases: ['A #1', Second] # comment", "tags:", "  - metadata", "  - nested/tag", "example: '[[YAML]]'", "---",
    "# **Authored** `heading`", "", "[[Target|Displayed]] and [regular **label**](Folder/Target.md#Section) and [reference][id].",
    "", "[id]: Folder/Target.md#^block", "", "- [[Target]]", "> [relative](Folder/Target.md)", "",
    "`[[Inline]]` \\[[Escaped]] <!-- [[Comment]] -->", "", "<!--", "[[Block comment]]", "", "[[Still comment]]", "-->", "",
    "```md", "[[Fence]] [code](Code.md)", "```", "", "    [[Indented]]", "",
    "![alt [[Not a note]] #not-tag](https://example.invalid/tracker.png)", "",
    "#inline #nested/tag \\#escaped `#code` [#label](Folder/Target.md)", "",
  ].join("\r\n");
  const index = buildLinkIndex([note("Start.md", text), note("Folder/Target.md", "# Title\n\n## Section\n\nBody ^block\n")]);
  const parsed = index.documents.get("Start.md");
  assert.deepEqual(parsed.aliases, ["A #1", "Second"]);
  assert.deepEqual(parsed.tags.sort(), ["inline", "metadata", "nested/tag"]);
  assert.equal(parsed.headings[0].title, "Authored heading");
  assert.equal(parsed.references.length, 6);
  assert.deepEqual(parsed.references.map((ref) => ref.destination), ["Target", "Folder/Target.md#Section", "Folder/Target.md#%5Eblock", "Target", "Folder/Target.md", "Folder/Target.md"]);
  for (const ref of parsed.references) {
    assert.equal(ref.resolution.status, "resolved");
    const raw = text.slice(ref.from, ref.to);
    assert.ok(raw.startsWith("[[") || raw.startsWith("["), raw);
    assert.equal(text.slice(0, ref.from).split("\n").length, ref.line);
  }
  assert.equal(text.slice(parsed.references[1].from, parsed.references[1].to), "[regular **label**](Folder/Target.md#Section)");
  assert.equal(parsed.references[1].label, "regular label");
  assert.equal(text.slice(parsed.references[2].from, parsed.references[2].to), "[reference][id]");
  assert.deepEqual(parsed.warnings, []);
});

test("Wiki and Markdown paths, aliases, Unicode and collisions resolve without guessing", () => {
  const index = buildLinkIndex([
    note("Folder/Start.md"), note("Folder/Target name.md", "---\naliases: [Target alias]\n---\n# Title\n"),
    note("Other/Target name.md"), note("Caf\u00e9.md"), note("A#B.md"), note("100%.md"),
    note("Case.md"), note("case.md"), note("Other/Cafe\u0301.md"),
    note("Alias collision.md", "---\naliases: [Target alias]\n---\n"),
  ]);
  assert.equal(resolveLink(index, "Folder/Start.md", "Target name").status, "ambiguous");
  assert.equal(resolveLink(index, "Folder/Start.md", "Folder/Target name|display").path, "Folder/Target name.md");
  assert.equal(resolveLink(index, "Folder/Start.md", "Target%20name.md", "markdown").path, "Folder/Target name.md");
  assert.equal(resolveLink(index, "Folder/Start.md", "../A%23B.md", "markdown").path, "A#B.md");
  assert.equal(resolveLink(index, "Folder/Start.md", "100%").path, "100%.md");
  assert.equal(resolveLink(index, "Folder/Start.md", "Target alias").status, "ambiguous");
  assert.equal(resolveLink(index, "Folder/Start.md", "Case").status, "ambiguous");
  assert.equal(resolveLink(index, "Folder/Start.md", "Caf\u00e9").status, "ambiguous");
  assert.equal(resolveLink(index, "Folder/Start.md", "Caf\u00e9.md", "markdown").status, "missing");
  assert.equal(resolveLink(index, "Folder/Start.md", "../Caf%C3%A9.md", "markdown").path, "Caf\u00e9.md");
  for (const value of ["../../escape.md", "/etc/passwd", "//host/file", "file:///etc/passwd", "C:\\file.md", ".codex/secret.md", "../private/Plan.md", "../11_state/Map.md", "../password.md", "%2e%2e/%2e%2e/escape.md", "Target%00.md"]) {
    assert.equal(resolveLink(index, "Folder/Start.md", value, "markdown").status, "invalid", value);
  }
  assert.equal(resolveLink(index, "Folder/Start.md", "%zz.md", "markdown").status, "invalid");
});

test("Heading sections and existing blocks jump to exact content ranges", () => {
  const text = "# Intro\n\nOpening.\n\n## Section *one*\n\nFirst paragraph ^existing\n\n### Child\n\nNested.\n\n## Peer\n\nSecond.\n\nA separate block.\n\n^separate\n";
  const index = buildLinkIndex([note("Target.md", text)]);
  const section = resolveLink(index, "Target.md", "#Section one");
  assert.equal(section.status, "resolved");
  assert.equal(text.slice(section.from, section.to), "## Section *one*\n\nFirst paragraph ^existing\n\n### Child\n\nNested.\n\n");
  const block = resolveLink(index, "Target.md", "#^existing");
  assert.equal(block.status, "resolved");
  assert.equal(text.slice(block.from, block.to).trim(), "First paragraph");
  const separate = resolveLink(index, "Target.md", "#^separate");
  assert.equal(text.slice(separate.from, separate.to).trim(), "A separate block.");
  assert.equal(resolveLink(index, "Target.md", "#Does not exist").status, "missing-fragment");
  assert.equal(resolveLink(index, "Target.md", "#^new-id").status, "missing-fragment");
  const repeated = buildLinkIndex([note("Repeat.md", "# Same\n\n# Same\n")]);
  assert.equal(resolveLink(repeated, "Repeat.md", "#Same").status, "ambiguous");
  assert.equal(resolveLink(repeated, "Repeat.md", "#same-1").from, 8);
});

test("Seven references form two backlink groups, update after edits and preserve previous snapshots", () => {
  const first = note("One.md", "[[Target]] [[Target alias]]");
  const second = note("Two.md", "[[Target|Display]] [link](Target.md) [[Target#Section]] [[Target#^block]] ![[Target#Section]]");
  const target = note("Target.md", "---\naliases: Target alias\n---\n# Section\n\nParagraph ^block\n");
  const original = buildLinkIndex([first, second, target]);
  const groups = backlinks(original, "Target.md");
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(({ path, count }) => ({ path, count })), [{ path: "One.md", count: 2 }, { path: "Two.md", count: 5 }]);
  assert.equal(groups.reduce((count, group) => count + group.count, 0), 7);
  const changed = buildLinkIndex([note("One.md", "No link"), second, note("Target.md", "# Section\n\nParagraph ^block\n")], [], original);
  assert.equal(backlinks(changed, "Target.md").length, 1);
  assert.equal(backlinks(changed, "Target.md")[0].count, 5);
  assert.equal(original.documents.get("One.md").references.length, 2);
  assert.equal(original.references[1].resolution.status, "resolved");
  const gone = buildLinkIndex([first, second], [], original);
  assert.equal(gone.references[0].resolution.status, "missing");
  assert.equal(original.references[0].resolution.status, "resolved");
  assert.equal(changed.documents.get("Two.md").tokens, original.documents.get("Two.md").tokens, "Unchanged grammar tokens are reused");
});

test("Completion inserts one canonical destination, supports aliases, headings and existing blocks only", () => {
  const index = buildLinkIndex([
    note("Start.md", "# Current\n\nCurrent block ^here\n"),
    note("Notes/Target.md", "---\naliases: [Target alias]\n---\n# Section\n\nParagraph ^existing\n"),
    note("Other/Target.md", "# Other"), note("Unique.md"),
  ]);
  const alias = completionCandidates(index, "Start.md", "Target alias");
  assert.equal(alias.length, 1);
  assert.equal(alias[0].insert, "Notes/Target|Target alias");
  assert.equal(resolveLink(index, "Start.md", alias[0].insert).path, alias[0].path);
  assert.equal(completionCandidates(index, "Start.md", "Unique")[0].insert, "Unique");
  assert.equal(completionCandidates(index, "Start.md", "Target alias#Sec")[0].insert, "Notes/Target#Section");
  assert.equal(completionCandidates(index, "Start.md", "Target alias#^ex")[0].insert, "Notes/Target#^existing");
  assert.equal(completionCandidates(index, "Start.md", "#Cur")[0].insert, "#Current");
  assert.equal(completionCandidates(index, "Start.md", "^he")[0].insert, "#^here");
  assert.deepEqual(completionCandidates(index, "Start.md", "Target#"), [], "Ambiguous destination does not guess a heading source");
  assert.deepEqual(completionCandidates(index, "Start.md", "Target alias#^new"), [], "Completion never invents or writes a block ID");
  for (const row of [...alias, ...completionCandidates(index, "Start.md", "#")]) assert.doesNotMatch(row.insert, /\[\[|\]\]/);
  assert.deepEqual(searchNotes(index, "Target alias"), [{ path: "Notes/Target.md", name: "Target", alias: "Target alias" }]);
});

test("Global graph has independent reference/relation layers, filter sets, orphans and distinct missing IDs", () => {
  const index = buildLinkIndex([
    note("Notes/A.md", "---\ntags: [research]\n---\n[[B]] [[B]] [[Missing]] `[[Ghost]]`\n\n```md\n[[Another ghost]]\n```"),
    note("Notes/B.md", "---\naliases: [Second concept]\ntags: research\n---\n[[C]]"),
    note("C.md"), note("Orphan.md"),
  ], [{ id: "accepted", source: "Notes/A.md", target: "C.md", type: "supports" }, { id: "projection-duplicate", source: "Notes/A.md", target: "C.md", type: "supports" }]);
  const all = graphData(index);
  assert.deepEqual(paths(all), ["C.md", "Missing · missing note", "Notes/A.md", "Notes/B.md", "Orphan.md"]);
  assert.equal(all.edges.find((edge) => edge.source === "Notes/A.md" && edge.target === "Notes/B.md").count, 2);
  assert.equal(all.edges.filter((edge) => edge.layer === "relation").length, 1);
  assert.ok(all.nodes.find((node) => node.missing).id.startsWith("unresolved:"));
  assert.deepEqual(paths(graphData(index, { existingOnly: true })), ["C.md", "Notes/A.md", "Notes/B.md", "Orphan.md"]);
  assert.deepEqual(paths(graphData(index, { folder: "Notes" })), ["Notes/A.md", "Notes/B.md"]);
  assert.deepEqual(paths(graphData(index, { tag: "#research" })), ["Notes/A.md", "Notes/B.md"]);
  assert.deepEqual(paths(graphData(index, { search: "Second concept" })), ["Notes/B.md"]);
  assert.deepEqual(graphData(index, { showReferences: false }).edges.map((edge) => edge.layer), ["relation"]);
  assert.equal(graphData(index, { showRelations: false }).edges.some((edge) => edge.layer === "relation"), false);
  assert.equal(graphData(index, { relationType: "contradicts" }).edges.filter((edge) => edge.layer === "relation").length, 0);
  const second = buildLinkIndex([...index.documents.values(), note("Missing.md"), ...Array.from({ length: 15 }, (_, at) => note(`Added/Concept ${at + 1}.md`, "[[B]]"))], index.relations, index);
  assert.equal(graphData(second).nodes.length, 20);
  assert.equal(graphData(second).nodes.some((node) => node.missing), false);
  assert.equal(index.documents.has("Missing.md"), false);
});

test("Local graph depth and direction produce explicit node and edge sets", () => {
  const index = buildLinkIndex([
    note("A.md", "[[B]]"), note("B.md", "[[C]]"), note("C.md", "[[D]]"),
    note("D.md"), note("Incoming.md", "[[B]]"), note("Far.md", "[[Incoming]]"), note("Orphan.md"),
  ]);
  const one = graphData(index, { local: { path: "B.md", depth: 1 } });
  assert.deepEqual(paths(one), ["A.md", "B.md", "C.md", "Incoming.md"]);
  assert.deepEqual(edgePairs(one), ["A.md>B.md:reference", "B.md>C.md:reference", "Incoming.md>B.md:reference"]);
  const two = graphData(index, { local: { path: "B.md", depth: 2 } });
  assert.deepEqual(paths(two), ["A.md", "B.md", "C.md", "D.md", "Far.md", "Incoming.md"]);
  assert.deepEqual(edgePairs(two), ["A.md>B.md:reference", "B.md>C.md:reference", "C.md>D.md:reference", "Far.md>Incoming.md:reference", "Incoming.md>B.md:reference"]);
  assert.deepEqual(paths(graphData(index, { local: { path: "B.md", depth: 2, incoming: false } })), ["B.md", "C.md", "D.md"]);
  assert.deepEqual(paths(graphData(index, { local: { path: "B.md", depth: 2, outgoing: false } })), ["A.md", "B.md", "Far.md", "Incoming.md"]);
  assert.deepEqual(edgePairs(graphData(index, { local: { path: "B.md", depth: 2, neighbors: false } })), edgePairs(one));
  assert.deepEqual(paths(graphData(index, { local: { path: "B.md", incoming: false, outgoing: false } })), ["B.md"]);
});

test("Graphs can hide generated indexes without hiding atomic links or using indexes as local bridges", () => {
  const indexes = [
    "02_source_memory/indexes/INDEX.md", "02_source_memory/indexes/CLUSTERS.md",
    "02_source_memory/indexes/by_topic/topic-one.md", "02_source_memory/indexes/by_literature/literature-one.md",
    "02_source_memory/indexes/collections/ABC123/INDEX.md", "02_source_memory/indexes/collections/ABC123/sources-001.md",
    "02_source_memory/indexes/collections/ABC123/relationships-001.md", "03_literature_synthesis/INDEX.md",
    "03_literature_synthesis/clusters/INDEX.md", "03_literature_synthesis/gaps/INDEX.md",
    "03_literature_synthesis/maps/map-one/INDEX.md", "03_literature_synthesis/maps/map-one/clusters/INDEX.md",
    "03_literature_synthesis/maps/map-one/gaps/INDEX.md",
  ];
  const atomic = ["A.md", "B.md", "C.md", "D.md", "INDEX.md", "Price index.md", "03_literature_synthesis/clusters/Cluster one.md"];
  const index = buildLinkIndex([
    ...indexes.map((path) => note(path, "[[A]] [[D]] [[Index-only missing]]")),
    ...atomic.map((path) => note(path, path === "A.md" ? `[[B]] [[${indexes[0]}]] [[Atomic missing]]` : "")),
  ], [{ id: "accepted", source: "A.md", target: "C.md", type: "supports" }]);
  const all = graphData(index, { existingOnly: true });
  const filtered = graphData(index, { showIndexes: false, existingOnly: true });
  assert.equal(all.nodes.length, indexes.length + atomic.length);
  assert.deepEqual(paths(filtered), [...atomic].sort());
  assert.deepEqual(edgePairs(filtered), ["A.md>B.md:reference", "A.md>C.md:relation"]);
  assert.equal(filtered.partial, false, "An intentional view filter does not make the catalogue partial");
  const local = graphData(index, { showIndexes: false, existingOnly: true, local: { path: "A.md", depth: 3 } });
  assert.deepEqual(paths(local), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(graphData(index, { showIndexes: false, local: { path: indexes[0] } }).nodes, []);
  const missing = graphData(index, { showIndexes: false }).nodes.filter((node) => node.missing);
  assert.deepEqual(missing.map((node) => node.label), ["Atomic missing · missing note"], "Hidden indexes cannot leave phantom unresolved nodes behind");
  assert.deepEqual(paths(graphData(index, { showIndexes: false, existingOnly: true, search: "Price index" })), ["Price index.md"]);
  assert.deepEqual(paths(graphData(index, { showIndexes: false, existingOnly: true, folder: "02_source_memory/indexes" })), []);
  assert.equal(backlinks(index, "A.md").length, indexes.length, "Index backlinks remain available outside the graph");
  assert.equal(resolveLink(index, "A.md", indexes[0]).status, "resolved");
  assert.deepEqual(graphData(index, { showIndexes: true, existingOnly: true }), all, "Showing indexes restores the exact graph");
});

test("Reading and embeds use shared resolution, bounded section/block content and inert missing links", () => {
  const index = buildLinkIndex([
    note("Start.md", "[[Target|Go]] [[Missing]] [[Target#Absent]] [regular](Target.md#Section)\n\n![[Target#Section]]\n\n![[Target#^existing]]\n\n<script>alert(1)</script>\n\n![tracker](https://example.invalid/a.png)"),
    note("Target.md", "# Intro\n\n## Section\n\nEmbedded **body** [[Start]]. ^existing\n\n## Peer\n\nMust not appear in section embed.\n"),
  ]);
  const dom = new JSDOM(renderIndexedMarkdown(index, "Start.md"));
  const doc = dom.window.document;
  assert.equal(doc.querySelectorAll("script,img,iframe,object").length, 0);
  assert.equal(doc.querySelectorAll('[data-link-status="missing"]').length, 1);
  assert.equal(doc.querySelectorAll('[data-link-status="missing-fragment"]').length, 1);
  assert.equal(doc.querySelector('[data-note-destination="Target"]').textContent, "Go");
  assert.equal(doc.querySelector('[data-note-destination="Target.md#Section"]').getAttribute("data-note-kind"), "markdown");
  assert.equal(doc.querySelectorAll(".note-embed").length, 2);
  for (const embed of doc.querySelectorAll(".note-embed")) {
    assert.match(embed.textContent, /Embedded body/);
    assert.doesNotMatch(embed.textContent, /Must not appear/);
    assert.ok(embed.querySelector("header [data-note-destination]"));
  }
  const heading = doc.querySelector(".note-embed h2");
  assert.equal(Number(heading.getAttribute("data-source-from")), index.documents.get("Target.md").headings[1].from);
  const inside = doc.querySelector('.note-embed [data-note-destination="Start"]');
  assert.equal(Number(inside.getAttribute("data-source-from")), index.documents.get("Target.md").references[0].from);
  assert.match(renderReferenceEmbed(index, index.documents.get("Start.md").references.find((ref) => ref.embed)), /note-embed/);
  dom.window.close();
  const cyclic = buildLinkIndex([note("A.md", "![[B]]"), note("B.md", "![[A]]")]);
  assert.match(renderIndexedMarkdown(cyclic, "A.md"), /Embedded note limit reached/);
  const tooLarge = buildLinkIndex([note("A.md", "![[B]]"), note("B.md", "x".repeat(LIMITS.embedCharacters + 1))]);
  assert.match(renderIndexedMarkdown(tooLarge, "A.md"), /Embedded content is too large/);
});

test("Indexing reports bounded or unsupported input without evaluating YAML or leaking private paths", () => {
  const parsed = parseDocument("Start.md", "---\naliases: !!js/function alert(1)\ntags: {object: invalid}\n---\n[[Target]]");
  assert.deepEqual(parsed.aliases, []);
  assert.deepEqual(parsed.tags, []);
  assert.equal(parsed.warnings.length, 2);
  const large = parseDocument("Large.md", "x".repeat(LIMITS.documentCharacters + 1));
  assert.equal(large.truncated, true);
  assert.deepEqual(large.references, []);
  const unclosed = parseDocument("Bad.md", "---\naliases: '[[Not a reference]]'\n");
  assert.equal(unclosed.warnings.length, 1);
  assert.deepEqual(unclosed.references, []);
  const index = buildLinkIndex([note(".codex/Private.md", "Secret"), note("private/Plan.md", "Secret"), note("11_state/Data.md", "Secret"), note("Good.md")]);
  assert.deepEqual([...index.documents.keys()], ["Good.md"]);
  assert.equal(index.warnings.length, 3);
});

test("Source positions handle bare CR, table cells and wrapped lists; unsupported table text stays visible", () => {
  for (const text of ["A\rB\r[[One]]", "- first\n  wrapped [[One]]\n", "| a | [[One]] |\n|---|---|\n| hi | there |\n"]) {
    const parsed = parseDocument("A.md", text);
    assert.equal(parsed.references.length, 1);
    assert.equal(text.slice(parsed.references[0].from, parsed.references[0].to), "[[One]]");
  }
  const index = buildLinkIndex([note("A.md", "| a\\|b | [[One]] |\n|---|---|\n| hi | there |\n")]);
  assert.match(renderIndexedMarkdown(index, "A.md"), /a\|b/);
  assert.equal(index.documents.get("A.md").unsupported, true);
  assert.match(index.warnings[0].reason, /source positions/);
});

test("Completion encodes reserved filename characters and validates the shortest unique destination", () => {
  const index = buildLinkIndex([
    note("Start.md"), note("A#B.md"), note("100%.md"), note("Literal%23.md"), note("Pipe|Name.md"),
    note("Bracket]]Name.md"), note("^Caret.md"), note("A.md.md"), note("Notes/A#B.md"),
    note("Case.md"), note("case.md"),
  ]);
  const all = completionCandidates(index, "Start.md", "");
  assert.equal(all.some((entry) => entry.path === "Case.md" || entry.path === "case.md"), false, "No impossible case-colliding completion is emitted");
  for (const path of ["A#B.md", "100%.md", "Literal%23.md", "Pipe|Name.md", "Bracket]]Name.md", "^Caret.md", "A.md.md", "Notes/A#B.md"]) {
    const entry = all.find((candidate) => candidate.path === path);
    assert.ok(entry, path);
    assert.equal(resolveLink(index, "Start.md", entry.insert).path, path, entry.insert);
    assert.equal(resolveLink(index, "Start.md", entry.insert).status, "resolved", entry.insert);
    assert.doesNotMatch(entry.insert, /\[\[|\]\]|\|/);
  }
  assert.equal(all.find((entry) => entry.path === "A#B.md").insert, "./A%23B");
  assert.equal(all.find((entry) => entry.path === "100%.md").insert, "100%25");
  assert.equal(all.find((entry) => entry.path === "Literal%23.md").insert, "Literal%2523");
  assert.equal(completionCandidates(index, "Start.md", "A%23B").length, 2);
  const unambiguous = buildLinkIndex([note("Start.md"), note("A#B.md")]);
  assert.equal(completionCandidates(unambiguous, "Start.md", "A")[0].insert, "A%23B");
  const related = buildLinkIndex([...unambiguous.documents.values()], [{ id: "typed", source: "Start.md", target: "A#B.md", type: "supports" }]);
  assert.equal(graphData(related).edges.find((edge) => edge.layer === "relation").target, "A#B.md", "Typed relation endpoints are canonical paths, independent of link syntax");
});

test("Repeated heading completions select distinct canonical IDs including authored suffix collisions", () => {
  const text = "# Same\n\nFirst\n\n# Same\n\nSecond\n\n# same-1\n\nThird\n\n# Pipe | heading\n\nFourth\n";
  const index = buildLinkIndex([note("Start.md"), note("Repeated.md", text)]);
  const document = index.documents.get("Repeated.md");
  assert.deepEqual(document.headings.map((heading) => heading.id), ["same", "same-1", "same-1-1", "pipe-heading"]);
  assert.equal(resolveLink(index, "Start.md", "Repeated#Same").status, "ambiguous");
  assert.equal(resolveLink(index, "Start.md", "Repeated#same").from, document.headings[0].from);
  assert.equal(resolveLink(index, "Start.md", "Repeated#same-1").from, document.headings[1].from);
  assert.equal(resolveLink(index, "Start.md", "Repeated#same-1-1").from, document.headings[2].from);
  const candidates = completionCandidates(index, "Start.md", "Repeated#");
  assert.deepEqual(candidates.map((entry) => entry.insert), ["Repeated#same", "Repeated#same-1", "Repeated#same-1-1", "Repeated#Pipe%20%7C%20heading"]);
  for (const entry of candidates) {
    const resolved = resolveLink(index, "Start.md", entry.insert);
    assert.equal(resolved.status, "resolved");
    assert.equal(resolved.from, entry.from);
  }
  assert.notEqual(candidates[0].detail, candidates[1].detail, "Repeated titles expose their source line");
});

test("Rendered jump anchors retain source identity and absolute offsets through nested heading/block embeds", () => {
  const start = "# Intro\n\n## Target\n\nParagraph.\n\n![[Embed]]";
  const embed = "# Embedded\n\nContent.\n\n![[Nested#Section]]";
  const nested = "# Intro\n\n## Section\n\nNested [[Start#Target]]. ^block\n\n## Peer\n\nExcluded.\n";
  const index = buildLinkIndex([note("Start.md", start), note("Embed.md", embed), note("Nested.md", nested)]);
  const dom = new JSDOM(renderIndexedMarkdown(index, "Start.md"));
  const doc = dom.window.document;
  const startHeadings = [...doc.querySelectorAll('h1[data-source-path="Start.md"],h2[data-source-path="Start.md"]')];
  assert.deepEqual(startHeadings.map((heading) => heading.textContent), ["Intro", "Target"]);
  const target = resolveLink(index, "Start.md", "#Target");
  const activeAnchors = [...doc.querySelectorAll('[data-source-from][data-source-path="Start.md"]')];
  const selected = activeAnchors.filter((node) => Number(node.dataset.sourceFrom) <= target.from).sort((a, b) => Number(b.dataset.sourceFrom) - Number(a.dataset.sourceFrom))[0];
  assert.equal(selected.tagName, "H2");
  assert.equal(selected.textContent, "Target");
  const embeddedHeading = doc.querySelector('h1[data-source-path="Embed.md"]');
  assert.equal(embeddedHeading.textContent, "Embedded");
  const nestedHeading = doc.querySelector('h2[data-source-path="Nested.md"]');
  assert.equal(Number(nestedHeading.dataset.sourceFrom), nested.indexOf("## Section"));
  const nestedLink = doc.querySelector('a[data-note-destination="Start#Target"]');
  assert.equal(nestedLink.dataset.sourcePath, "Nested.md");
  assert.equal(Number(nestedLink.dataset.sourceFrom), nested.indexOf("[[Start#Target]]"));
  dom.window.close();
  const blockSource = note("Block source.md", "![[Nested#^block]]");
  const blockIndex = buildLinkIndex([...index.documents.values(), blockSource]);
  const blockDom = new JSDOM(renderIndexedMarkdown(blockIndex, blockSource.path));
  const blockParagraph = blockDom.window.document.querySelector('p[data-source-path="Nested.md"]');
  const block = resolveLink(blockIndex, blockSource.path, "Nested#^block");
  assert.equal(Number(blockParagraph.dataset.sourceFrom), block.from);
  const blockLink = blockParagraph.querySelector('[data-note-destination="Start#Target"]');
  assert.equal(Number(blockLink.dataset.sourceFrom), nested.indexOf("[[Start#Target]]"));
  blockDom.window.close();
});

test("Local graph direction controls hide reciprocal center edges and preserve neighbor edges at depth two", () => {
  const index = buildLinkIndex([
    note("A.md", "[[B]] [[A]]"), note("B.md", "[[A]] [[C]]"), note("C.md", "[[B]]"),
  ]);
  const outgoing = graphData(index, { local: { path: "A.md", depth: 2, incoming: false, outgoing: true } });
  assert.deepEqual(paths(outgoing), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(edgePairs(outgoing), ["A.md>A.md:reference", "A.md>B.md:reference", "B.md>C.md:reference", "C.md>B.md:reference"]);
  const incoming = graphData(index, { local: { path: "A.md", depth: 2, incoming: true, outgoing: false } });
  assert.deepEqual(paths(incoming), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(edgePairs(incoming), ["A.md>A.md:reference", "B.md>A.md:reference", "B.md>C.md:reference", "C.md>B.md:reference"]);
  const noNeighbors = graphData(index, { local: { path: "A.md", depth: 2, incoming: false, outgoing: true, neighbors: false } });
  assert.deepEqual(paths(noNeighbors), ["A.md", "B.md", "C.md"]);
  assert.deepEqual(edgePairs(noNeighbors), ["A.md>A.md:reference", "A.md>B.md:reference"]);
  assert.deepEqual(graphData(index, { local: { path: "A.md", incoming: false, outgoing: false } }).edges, []);
});

test("Aggregate reference and typed-edge budgets identify partial results and recover when excess input is removed", () => {
  const sources = Array.from({ length: 6 }, (_, at) => note(`Source ${at}.md`, "[[Target]] ".repeat(LIMITS.references)));
  const target = note("Target.md");
  const index = buildLinkIndex([...sources, target]);
  assert.equal(index.references.length, LIMITS.workspaceReferences);
  assert.equal(index.partial, true);
  assert.match(index.warnings.at(-1).reason, /Workspace link occurrence budget/);
  assert.equal(backlinks(index, target.path).every((group) => group.partial), true);
  assert.equal(backlinks(index, target.path).reduce((count, group) => count + group.count, 0), LIMITS.workspaceReferences);
  assert.equal(graphData(index).partial, true);
  const recovered = buildLinkIndex([sources.at(-1), target], [], index);
  assert.equal(recovered.references.length, LIMITS.references);
  assert.equal(recovered.partial, false, "A previously omitted cached document is reparsed when the budget becomes available");
  assert.equal(index.references.length, LIMITS.workspaceReferences, "Previous snapshots stay stable");
  const relations = Array.from({ length: LIMITS.relations + 1 }, (_, at) => ({ id: `relation-${at}`, source: "Target.md", target: "Source 5.md", type: `kind-${at}` }));
  const typed = buildLinkIndex([sources.at(-1), target], relations);
  assert.equal(typed.relations.length, LIMITS.relations);
  assert.equal(typed.partial, true);
  assert.match(typed.warnings.at(-1).reason, /Typed relation budget/);
});

test("Syntax storms use a bounded plain-text reader fallback and an aggregate syntax limit", () => {
  for (const text of ["[[Target]] ".repeat(Math.ceil(LIMITS.syntaxPerDocument / 4) + 1), "a\n".repeat(LIMITS.syntaxPerDocument + 1)]) {
    assert.ok(text.length < LIMITS.documentCharacters);
    const index = buildLinkIndex([note("Storm.md", text), note("Target.md")]);
    const parsed = index.documents.get("Storm.md");
    assert.equal(parsed.syntaxLimited, true);
    assert.equal(parsed.tokens.length, 0);
    assert.equal(parsed.references.length, 0);
    assert.equal(index.partial, true);
    const dom = new JSDOM(renderIndexedMarkdown(index, "Storm.md"));
    assert.equal(dom.window.document.querySelector(".note-source-fallback").textContent, text);
    assert.equal(dom.window.document.querySelectorAll("a").length, 0);
    assert.match(dom.window.document.querySelector(".note-analysis-paused").textContent, /Backlinks and graphs are partial/);
    dom.window.close();
  }
  const text = `\`\`\`\n${"-".repeat(14990)}\n\`\`\`\n`;
  const many = Array.from({ length: 18 }, (_, at) => note(`Code ${at}.md`, text));
  const index = buildLinkIndex(many);
  assert.equal(index.documents.size, 18);
  assert.equal(index.partial, true);
  assert.ok([...index.documents.values()].some((document) => document.syntaxLimited));
  assert.match(index.warnings.at(-1).reason, /Workspace syntax budget/);
  const recovered = buildLinkIndex([many.at(-1)], [], index);
  assert.equal(recovered.partial, false);
  assert.equal(recovered.documents.get(many.at(-1).path).syntaxLimited, false);
  const normal = Array.from({ length: 5000 }, (_, at) => note(`Concept ${at}.md`, `---\naliases: [Alias ${at}]\ntags: [research]\n---\n# Concept ${at}\n\n[[Concept ${(at + 1) % 5000}]] [[Concept ${(at + 7) % 5000}]]\n\n## Section\n\nDetails ^block-${at}\n`));
  const complete = buildLinkIndex(normal);
  assert.equal(complete.documents.size, 5000);
  assert.equal(complete.references.length, 10000);
  assert.equal(complete.partial, false, JSON.stringify(complete.warnings.slice(0, 2)));
});

test("Only identified projections in a well-formed owned graph block deduplicate accepted typed edges", () => {
  const start = "<!-- auto-zettelkasten:graph:start -->";
  const end = "<!-- auto-zettelkasten:graph:end -->";
  const owned = `${start}\n- supports: [[Target]] — reason <!-- relation_id: accepted -->\n- supports: [[Other]] <!-- relation_id: accepted -->\n- supports: [[Target]] <!-- relation_id: unknown -->\n${end}`;
  const relations = [{ id: "accepted", source: "Source.md", target: "Target.md", type: "supports" }];
  const index = buildLinkIndex([note("Source.md", `${owned}\n\nHuman [[Target]]. <!-- relation_id: accepted -->\n`), note("Target.md"), note("Other.md")], relations);
  assert.deepEqual(index.references.map((ref) => ref.relationId), ["accepted", "accepted", "unknown", undefined]);
  assert.equal(backlinks(index, "Target.md")[0].count, 3, "Backlinks retain all raw occurrences");
  const graph = graphData(index);
  assert.equal(graph.edges.find((edge) => edge.source === "Source.md" && edge.target === "Target.md" && edge.layer === "reference").count, 2, "Unmatched projection and human link remain ordinary references");
  assert.equal(graph.edges.filter((edge) => edge.layer === "relation").length, 1);
  assert.equal(graph.edges.find((edge) => edge.target === "Other.md").layer, "reference", "Same ID with a different endpoint is not suppressed");
  const projectionOnly = buildLinkIndex([note("Source.md", `${start}\n- supports: [[Target]] <!-- relation_id: accepted -->\n${end}`), note("Target.md")], relations);
  assert.equal(graphData(projectionOnly, { showRelations: false }).edges.length, 0, "Hiding typed relations cannot relabel their projections as human references");
  assert.equal(graphData(projectionOnly, { showReferences: false }).edges.length, 1);
  for (const text of [
    `${start}\n[[Target]] <!-- relation_id: accepted -->`,
    `${end}\n[[Target]] <!-- relation_id: accepted -->\n${start}`,
    `${start}\n${start}\n[[Target]] <!-- relation_id: accepted -->\n${end}`,
    `\`\`\`md\n${start}\n\`\`\`\n[[Target]] <!-- relation_id: accepted -->\n${end}`,
    `${start}\n[[Target]] \\<!-- relation_id: accepted -->\n${end}`,
  ]) {
    const malformed = buildLinkIndex([note("Source.md", text), note("Target.md")], relations);
    assert.equal(malformed.references[0].relationId, undefined, text);
    assert.equal(graphData(malformed).edges.filter((edge) => edge.layer === "reference").length, 1, text);
  }
});

function relocateFixture(index, moves, plan) {
  const mapping = new Map(moves.map(({ from, to }) => [from, to]));
  return buildLinkIndex([...index.documents.values()].map((document) => {
    let text = document.text;
    const patch = plan.patches.find((entry) => entry.path === document.path);
    for (const edit of patch?.edits || []) {
      assert.equal(text.slice(edit.from, edit.to), edit.before);
      text = text.slice(0, edit.from) + edit.after + text.slice(edit.to);
    }
    return { ...document, path: mapping.get(document.path) || document.path, text };
  }));
}

test("Relocation reviews seven references in two files and preserves labels, encoding, fragments, embeds and H1", () => {
  const target = "---\naliases: [Target alias]\n---\n# Authored title\n\n## Section\n\nParagraph ^block\n";
  const first = "# Source one\n\n[[Target Name]] [[Target Name|Alias text]]\n\n`[[Target Name]]`\n\n```md\n[[Target Name]]\n```\n";
  const second = "# Source two\n\n[[Notes/Target Name#Section]]\n\n[Encoded](Notes/Target%20Name.md#Section \"A title\")\n\n[[Target Name#^block]]\n\n![[Target Name#Section]]\n\n[Angle](<Notes/Target Name.md#Section> 'Another title')\n";
  const index = buildLinkIndex([note("One.md", first), note("Two.md", second), note("Notes/Target Name.md", target)]);
  const moves = [{ from: "Notes/Target Name.md", to: "Notes/Renamed Target.md" }];
  const plan = planRelocation(index, moves);
  assert.equal(plan.safe, true);
  assert.equal(plan.references, 7);
  assert.equal(plan.files, 2);
  assert.equal(plan.unchanged, 0);
  assert.deepEqual(plan.blocked, []);
  assert.deepEqual(plan.warnings, []);
  for (const patch of plan.patches) {
    assert.equal(patch.revision, "v1");
    assert.ok(patch.edits.every((edit) => !/[\[\]<>|]/.test(edit.before)), "Only inner destinations are edited");
  }
  const result = relocateFixture(index, moves, plan);
  assert.equal(result.documents.get("Notes/Renamed Target.md").text, target, "The target's authored heading and metadata stay byte-identical");
  const repairedFirst = result.documents.get("One.md").text;
  assert.match(repairedFirst, /\[\[Renamed%20Target\|Alias text\]\]/);
  assert.match(repairedFirst, /`\[\[Target Name\]\]`/);
  assert.match(repairedFirst, /```md\n\[\[Target Name\]\]\n```/);
  const repairedSecond = result.documents.get("Two.md").text;
  assert.match(repairedSecond, /\[Encoded\]\(Notes\/Renamed%20Target\.md#Section "A title"\)/);
  assert.match(repairedSecond, /\[Angle\]\(<Notes\/Renamed%20Target\.md#Section> 'Another title'\)/);
  assert.match(repairedSecond, /!\[\[Renamed%20Target#Section\]\]/);
  assert.match(repairedSecond, /\[\[Renamed%20Target#\^block\]\]/);
  assert.equal(backlinks(result, "Notes/Renamed Target.md").reduce((count, group) => count + group.count, 0), 7);
  assert.equal(result.references.every((ref) => ref.resolution.status === "resolved"), true);
  assert.equal(index.documents.get("One.md").text, first, "Planning never mutates the source snapshot");
});

test("Unique-basename folder moves and alias references remain unchanged while relative outgoing links are repaired", () => {
  const index = buildLinkIndex([
    note("One.md", "[[Target]] [[Target|Alias label]] [[Target alias]]\n"),
    note("Two.md", "[[Target#Section]] [[Target#^block]] ![[Target#Section]] [[Target]]\n"),
    note("Notes/Target.md", "---\naliases: Target alias\n---\n# Section\n\nParagraph ^block\n"),
  ]);
  const plan = planRelocation(index, [{ from: "Notes/Target.md", to: "Drafts/Target.md" }]);
  assert.equal(plan.safe, true);
  assert.equal(plan.references, 0);
  assert.equal(plan.unchanged, 7);
  assert.deepEqual(plan.patches, []);
  const outgoing = buildLinkIndex([
    note("Notes/Source.md", "# Source\n\n[Peer](Peer.md#Section \"Keep title\") [[./Peer#^block|Keep label]] [[#Local]]\n\n## Local\n\nBody.\n"),
    note("Notes/Peer.md", "# Section\n\nBody ^block\n"),
  ]);
  const moves = [{ from: "Notes/Source.md", to: "Drafts/Source.md" }];
  const repaired = planRelocation(outgoing, moves);
  assert.equal(repaired.safe, true);
  assert.equal(repaired.references, 2);
  assert.equal(repaired.unchanged, 1);
  assert.equal(repaired.patches[0].path, "Notes/Source.md");
  const result = relocateFixture(outgoing, moves, repaired);
  assert.match(result.documents.get("Drafts/Source.md").text, /\[Peer\]\(\.\.\/Notes\/Peer\.md#Section "Keep title"\)/);
  assert.match(result.documents.get("Drafts/Source.md").text, /\[\[\.\.\/Notes\/Peer#\^block\|Keep label\]\]/);
  assert.equal(result.references.every((ref) => ref.resolution.status === "resolved"), true);
});

test("Reference-style destinations repair once while counting all uses and preserving duplicate definitions", () => {
  const text = "# Source\n\n[first][ID] [second][id] ![embed][ID]\n\n[ID]: <Notes/Target%20Name.md#Section> \"Keep definition title\"\n[ID]: Other.md \"Ignored duplicate\"\n";
  const index = buildLinkIndex([note("Source.md", text), note("Notes/Target Name.md", "# Section\n\nBody\n"), note("Other.md")]);
  assert.equal(index.references.length, 3);
  const moves = [{ from: "Notes/Target Name.md", to: "Drafts/New Name.md" }];
  const plan = planRelocation(index, moves);
  assert.equal(plan.safe, true);
  assert.equal(plan.references, 3);
  assert.equal(plan.files, 1);
  assert.equal(plan.patches[0].edits.length, 1);
  assert.equal(plan.patches[0].edits[0].before, "Notes/Target%20Name.md#Section");
  const result = relocateFixture(index, moves, plan);
  assert.equal(result.references.every((ref) => ref.resolution.path === "Drafts/New Name.md"), true);
  assert.match(result.documents.get("Source.md").text, /\[first\]\[ID\] \[second\]\[id\] !\[embed\]\[ID\]/);
  assert.match(result.documents.get("Source.md").text, /\[ID\]: <Drafts\/New%20Name\.md#Section> "Keep definition title"/);
  assert.match(result.documents.get("Source.md").text, /\[ID\]: Other\.md "Ignored duplicate"/);
});

test("Relocation refuses guesses, case/Unicode collisions, private paths and changed unresolved meanings", () => {
  const ambiguous = buildLinkIndex([note("Source.md", "[[Target]]"), note("One/Target.md"), note("Two/Target.md")]);
  const changed = planRelocation(ambiguous, [{ from: "One/Target.md", to: "One/Renamed.md" }]);
  assert.equal(changed.safe, false);
  assert.equal(changed.references, 0);
  assert.deepEqual(changed.patches, []);
  assert.match(changed.warnings[0], /ambiguous link/);
  const missing = buildLinkIndex([note("Source.md", "[[Missing]]"), note("Target.md")]);
  const found = planRelocation(missing, [{ from: "Target.md", to: "Missing.md" }]);
  assert.equal(found.safe, false);
  assert.match(found.warnings[0], /missing link/);
  for (const to of ["case.md", "Cafe\u0301.md", ".codex/Target.md", "../Escape.md"]) {
    const index = buildLinkIndex([note("Target.md"), note("Case.md"), note("Caf\u00e9.md")]);
    const result = planRelocation(index, [{ from: "Target.md", to }]);
    assert.equal(result.safe, false, to);
    assert.deepEqual(result.patches, [], to);
  }
  const unicode = buildLinkIndex([note("Source.md", "[[Caf\u00e9]]"), note("Caf\u00e9.md")]);
  const moves = [{ from: "Caf\u00e9.md", to: "R\u00e9sum\u00e9#100%.md" }];
  const planned = planRelocation(unicode, moves);
  assert.equal(planned.safe, true);
  assert.equal(relocateFixture(unicode, moves, planned).references[0].resolution.path, moves[0].to);
});

test("Read-only/generated repairs are counted and blocked without patches; partial or unmappable snapshots are unsafe", () => {
  const index = buildLinkIndex([note("Generated.md", "[[Target]] [[Target|Display]]", { editable: false }), note("User.md", "[[Target]]"), note("Target.md")]);
  const moves = [{ from: "Target.md", to: "Renamed.md" }];
  const plan = planRelocation(index, moves);
  assert.equal(plan.safe, true, "Read-only blockers can be reviewed explicitly without inventing a patch");
  assert.equal(plan.references, 3);
  assert.equal(plan.files, 1);
  assert.equal(plan.patches[0].path, "User.md");
  assert.equal(plan.blocked[0].path, "Generated.md");
  assert.equal(plan.blocked[0].count, 2);
  const partial = planRelocation({ ...index, partial: true }, moves);
  assert.equal(partial.safe, false);
  assert.match(partial.warnings[0], /partial/);
  const broken = buildLinkIndex([note("Source.md", "[[Target]]"), note("Target.md")]);
  delete broken.references[0].destinationFrom;
  const unmappable = planRelocation(broken, moves);
  assert.equal(unmappable.safe, false);
  assert.deepEqual(unmappable.patches, []);
  assert.match(unmappable.blocked[0].reason, /exact destination span/);
  const noRevision = buildLinkIndex([note("Source.md", "[[Target]]", { revision: undefined }), note("Target.md")]);
  assert.equal(planRelocation(noRevision, moves).safe, false);
});

test("The final repaired index detects changed authored heading meanings without rejecting ordinary offset shifts", () => {
  const index = buildLinkIndex([note("Source.md", "# [[Target]]\n\nBody\n"), note("Other.md", "[[Source#Target]]"), note("Target.md")]);
  const plan = planRelocation(index, [{ from: "Target.md", to: "New.md" }]);
  assert.equal(plan.safe, false);
  assert.match(plan.warnings.join("\n"), /resolved link or heading/);
  const shifted = buildLinkIndex([
    note("Notes/Source.md", "[Peer](Peer.md)\n\n## Section\n\nBody ^block\n"),
    note("Notes/Peer.md"), note("Other.md", "[[Source#Section]] [[Source#^block]]"),
  ]);
  const moves = [{ from: "Notes/Source.md", to: "Drafts/Source.md" }];
  const valid = planRelocation(shifted, moves);
  assert.equal(valid.safe, true, valid.warnings.join("\n"));
  assert.equal(valid.references, 1);
  assert.equal(valid.unchanged, 2);
  const result = relocateFixture(shifted, moves, valid);
  assert.equal(result.references.every((ref) => ref.resolution.status === "resolved"), true);
});

test("Canonical published memory indexes resolve their local navigation and remain permanently read-only", () => {
  const master = "02_source_memory/indexes/INDEX.md";
  const target = "02_source_memory/notes/target.md";
  const topic = "02_source_memory/indexes/by_topic/INDEX.md";
  const collection = "02_source_memory/indexes/collections/ABC123/INDEX.md";
  const index = buildLinkIndex([
    note(master, "# Memory\n\n[[../notes/target]] [[by_topic/INDEX]] [[collections/ABC123/INDEX]]\n"),
    note(target, "# Target\n\n[[../indexes/INDEX]] [Memory](../indexes/INDEX.md)\n"),
    note(topic, "# Topics\n\n[[topic-one]]\n"),
    note("02_source_memory/indexes/by_topic/topic-one.md", "# Topic\n"),
    note("02_source_memory/indexes/by_literature/literature-one.md", "# Literature\n\n[[../../notes/target]]\n"),
    note("02_source_memory/indexes/CLUSTERS.md", "# Clusters\n"),
    note(collection, "# Collection\n\n[[sources-001]] [[relationships-001]] [[relationships-1000]]\n"),
    note("02_source_memory/indexes/collections/ABC123/sources-001.md", "# Sources\n"),
    note("02_source_memory/indexes/collections/ABC123/relationships-001.md", "# Accepted relationships\n"),
    note("02_source_memory/indexes/collections/ABC123/relationships-1000.md", "# More accepted relationships\n"),
    note("sources-001.md", "# Unrelated human note\n"),
  ]);
  assert.equal(index.partial, false);
  assert.equal(index.references.every((ref) => ref.resolution.status === "resolved"), true);
  assert.equal(resolveLink(index, master, "../notes/target").path, target);
  assert.equal(resolveLink(index, target, "02_source_memory/indexes/INDEX").path, master);
  assert.equal(resolveLink(index, target, "../indexes/INDEX").path, master);
  assert.equal(resolveLink(index, master, "by_topic/INDEX").path, topic);
  assert.equal(resolveLink(index, master, "collections/ABC123/INDEX").path, collection);
  assert.equal(resolveLink(index, collection, "sources-001").path, "02_source_memory/indexes/collections/ABC123/sources-001.md", "Published chunk navigation is relative to its canonical collection");
  assert.equal([...index.documents.values()].filter((document) => document.path.startsWith("02_source_memory/indexes/")).every((document) => document.editable === false), true);
  assert.equal(graphData(index).nodes.length, index.documents.size);
  assert.equal(backlinks(index, target).length, 2);
  const repair = planRelocation(index, [{ from: target, to: "02_source_memory/notes/renamed.md" }]);
  assert.equal(repair.blocked.some((entry) => entry.path === master), true);
  assert.equal(repair.patches.some((entry) => entry.path.startsWith("02_source_memory/indexes/")), false);
  assert.equal(planRelocation(index, [{ from: master, to: "Moved.md" }]).safe, false);
  assert.equal(planRelocation(index, [{ from: target, to: "02_source_memory/indexes/by_topic/new.md" }]).safe, false);
});

test("Published-index allowance excludes generic indexes, private descendants, state, YAML and unrecognized families", () => {
  const forbidden = [
    "indexes/INDEX.md", "Indexes/INDEX.md", "Other/indexes/INDEX.md",
    "02_source_memory/Indexes/INDEX.md", "02_source_memory/indexes/Private.md", "02_source_memory/indexes/arbitrary.md",
    "02_source_memory/indexes/typed_links.yml", "02_source_memory/indexes/by_topic/topic.yml",
    "02_source_memory/indexes/by_topic/private/Leak.md", "02_source_memory/indexes/by_literature/profiles/Leak.md",
    "02_source_memory/indexes/collections/ABC123/Private.md", "02_source_memory/indexes/collections/ABC123/sources-01.md",
    "02_source_memory/indexes/collections/ABC123/relationships-01.md", "02_source_memory/indexes/collections/ABC123/nested/Leak.md",
    "02_source_memory/indexes/collections/private/INDEX.md", "02_source_memory/indexes/source_sets/INDEX.md",
    "11_state/INDEX.md", ".codex/INDEX.md",
  ];
  const index = buildLinkIndex([note("Start.md"), ...forbidden.map((path) => note(path, "Excluded"))]);
  assert.deepEqual([...index.documents.keys()], ["Start.md"]);
  assert.equal(index.warnings.length, forbidden.length);
  for (const path of forbidden) assert.notEqual(resolveLink(index, "Start.md", path).status, "resolved", path);
  assert.equal(resolveLink(index, "Start.md", "private/../Start").status, "invalid", "Normalization cannot make a private traversal acceptable");
});
