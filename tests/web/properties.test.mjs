import test from "node:test";
import assert from "node:assert/strict";
import { inspectProperties, patchProperty, propertyLists, renderProperties } from "../../tools/workspace_properties.js";
import { buildLinkIndex, buildLibraryIndex, parseLibraryDocument, completionCandidates, graphData, resolveLink, searchNotes } from "../../tools/workspace_links.js";

const note = (header, body = "# Body\n") => `---\n${header}\n---\n${body}`;

test("property parser accepts writer's indentless YAML lists and common typed values", () => {
  const source = note("tags:\n- topic\n- '#أفريقيا'\naliases: ['One, Two', Café]\ncount: 3\nactive: true\ndate: 2026-10-28\nwhen: 2026-10-28T12:30:00Z\ntext: 'no'");
  const parsed = inspectProperties(source);
  assert.equal(parsed.error, null);
  assert.deepEqual(parsed.rows.map((row) => row.type), ["tags", "list", "number", "checkbox", "date", "datetime", "text"]);
  assert.deepEqual(propertyLists(source).tags, ["topic", "أفريقيا"]);
  assert.deepEqual(propertyLists(source).aliases, ["One, Two", "Café"]);
});

test("field patching preserves unrelated bytes, comments, BOM and CRLF body", () => {
  const source = "\uFEFF---\r\n# Keep this\r\ntitle: old # Keep title comment\r\ntags: # Keep list comment\r\n- one\r\n- two\r\nnested:\r\n  key: 'untouched' # Also keep\r\n---\r\n\r\nBody [[Target]]\r\n";
  const title = patchProperty(source, "title", "New \"quoted\" title");
  assert.equal(title, source.replace("title: old", 'title: "New \\"quoted\\" title"'));
  const tags = patchProperty(source, "tags", ["three", "four"]);
  assert.match(tags, /tags: # Keep list comment\r\n  \["three","four"\]\r\nnested:/);
  assert.equal(tags.slice(tags.indexOf("nested:")), source.slice(source.indexOf("nested:")));
  assert.equal(inspectProperties(tags).error, null);
});

test("property patching supports add, remove, null and empty frontmatter", () => {
  const bare = "\uFEFF# Note\nBody\n";
  const added = patchProperty(bare, "new", false);
  assert.match(added, /^\uFEFF---\n"new": false\n---\n# Note/);
  assert.equal(inspectProperties(added).rows[0].value, false);
  const source = note("first: yes\nempty:\nlast: keep");
  assert.equal(inspectProperties(patchProperty(source, "empty", "filled")).rows.find((row) => row.key === "empty").value, "filled");
  const removed = patchProperty(source, "first", null, { remove: true });
  assert.equal(removed, note("empty:\nlast: keep"));
  const empty = patchProperty("---\n---\nBody\n", "a", 2);
  assert.equal(inspectProperties(empty).rows[0].value, 2);
});

test("invalid and unsupported YAML never silently rewrites", () => {
  for (const header of ["tags: [broken", "a: 1\na: 2", "a: &base one\nb: *base", "a: !!js/function alert(1)", "- item"]) {
    const source = note(header);
    assert.ok(inspectProperties(source).error, header);
    assert.throws(() => patchProperty(source, "a", "new"));
  }
  const source = note("nested:\n  x: y\nregular: okay");
  assert.equal(inspectProperties(source).rows[0].editable, false);
  assert.ok(inspectProperties("---\nnever closes").error);
  assert.ok(inspectProperties(note(`long: ${"a".repeat(65536)}`)).error);
  assert.match(renderProperties(note("unsafe: '<script>bad</script>'")), /&lt;script&gt;/);
});

test("personal labels and tags survive compaction without becoming graph identities", () => {
  const a = { path: "A.md", text: note("tags:\n- canonical\naliases: [Original]", "[[B]]"), display_title: "My title", personal_properties: { tags: ["personal"], aliases: ["Added"] }, effective_tags: ["canonical", "personal"], effective_aliases: ["Original", "Added"] };
  const b = { path: "B.md", text: "Body", display_title: "My title" };
  for (const build of [() => buildLinkIndex([a, b]), () => buildLibraryIndex([parseLibraryDocument(a), parseLibraryDocument(b)])]) {
    const index = build();
    assert.deepEqual(index.documents.get("A.md").tags, ["canonical", "personal"]);
    assert.deepEqual(index.documents.get("A.md").aliases, ["Original", "Added"]);
    assert.deepEqual(searchNotes(index, "My title").map((row) => row.path), ["A.md", "B.md"]);
    assert.deepEqual(completionCandidates(index, "A.md", "My title").map((row) => row.path), ["A.md", "B.md"]);
    for (const completion of completionCandidates(index, "A.md", "My title")) assert.equal(resolveLink(index, "A.md", completion.insert).path, completion.path);
    assert.deepEqual(graphData(index).nodes.filter((node) => !node.missing).map((node) => [node.id, node.label]), [["A.md", "My title"], ["B.md", "My title"]]);
  }
});

test("metadata refresh removes old personal tags and aliases rather than accumulating cache values", () => {
  const text = note("tags: [canonical]\naliases: [Original]");
  const first = buildLinkIndex([{ path: "A.md", text, personal_properties: { tags: ["old"], aliases: ["old alias"] } }]);
  const current = buildLinkIndex([{ path: "A.md", text, personal_properties: {}, effective_tags: ["canonical"], effective_aliases: ["Original"] }], [], first);
  assert.deepEqual(current.documents.get("A.md").tags, ["canonical"]);
  assert.deepEqual(current.documents.get("A.md").aliases, ["Original"]);
});
