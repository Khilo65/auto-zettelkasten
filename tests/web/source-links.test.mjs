import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { parseZoteroURI, sourceLinkMarkup } from "../../tools/workspace_sources.js";
import { buildLinkIndex, graphData, planRelocation, renderIndexedMarkdown } from "../../tools/workspace_links.js";
import { renderProperties } from "../../tools/workspace_properties.js";

const pdf = "zotero://open-pdf/library/items/PDFKEY01";
const note = (path, text) => ({ path, text, revision: "v1", editable: true });

test("source navigation preserves library and attachment identity with physical page coordinates", () => {
  const parsed = parseZoteroURI(`${pdf}?annotation=ANNOT001&page=1`);
  assert.deepEqual(parsed, { action: "open-pdf", scope: "library", library_type: "user", library_id: null,
    key: "PDFKEY01", page: 1, page_index: 0, annotation_key: "ANNOT001", uri: `${pdf}?page=1&annotation=ANNOT001` });
  const group = parseZoteroURI("zotero://select/groups/42/items/PDFKEY01");
  assert.equal(group.library_type, "group");
  assert.equal(group.library_id, "42");
  assert.notEqual(group.scope, parsed.scope);
  assert.equal(parseZoteroURI("ZOTERO://select/library/items/SOURCE01").uri, "zotero://select/library/items/SOURCE01");
});

test("unsupported navigation and ambiguous or unsafe query parameters are rejected", () => {
  for (const value of [null, 1, "javascript:alert(1)", "https://example.invalid", "zotero://debug/library/items/PDFKEY01",
    `${pdf}/`, `${pdf}#page=2`, `${pdf}?`, `${pdf}?page=0`, `${pdf}?page=-1`, `${pdf}?page=1.5`, `${pdf}?page=01`,
    `${pdf}?page=9007199254740992`, `${pdf}?page=2&page=3`, `${pdf}?annotation=short`, `${pdf}?annotation=annot001`,
    `${pdf}?page=%32`, `${pdf}?page=2&unknown=3`, `${pdf}?page=2&`, `${pdf}?page=2\n`, `${pdf}?page=2=3`,
    "zotero://select/library/items/SOURCE01?page=1", "zotero://open-pdf/groups/0/items/PDFKEY01",
    "zotero://open-pdf/groups/01/items/PDFKEY01", "zotero://open-pdf/library/items/pdfkey01",
    " zotero://open-pdf/library/items/PDFKEY01", "zotero://open-pdf@evil/library/items/PDFKEY01"]) {
    assert.equal(parseZoteroURI(value), null, String(value));
  }
});

test("source links render safely and never enter Markdown graph or relocation repair", () => {
  const text = `[PDF](${pdf}?page=2&annotation=ANNOT001) [[${pdf}|Source PDF]] [item](zotero://select/groups/42/items/SOURCE01) [bad](${pdf}?page=0) [[Target]]`;
  const index = buildLinkIndex([note("Start.md", text), note("Target.md", "Body")]);
  assert.equal(index.references.length, 1);
  assert.equal(index.references[0].destination, "Target");
  const graph = graphData(index);
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.unresolved.missing, 0);
  const document = new JSDOM(renderIndexedMarkdown(index, "Start.md")).window.document;
  assert.equal(document.querySelectorAll("[data-source-uri]").length, 3);
  assert.equal(document.querySelectorAll("a[href^='zotero:']").length, 0);
  assert.equal(document.querySelector("[data-source-uri]").getAttribute("data-source-uri"), `${pdf}?page=2&annotation=ANNOT001`);
  assert.equal(document.querySelectorAll("a").length, 4);
  const plan = planRelocation(index, [{ from: "Target.md", to: "Renamed.md" }]);
  assert.equal(plan.references, 1);
  assert.equal(plan.patches[0].edits[0].before, "Target");
  assert.equal(plan.patches[0].edits[0].after, "Renamed");
});

test("properties expose only recognized source URLs without changing stored values", () => {
  const html = renderProperties(`---\nsource_pdf_uri: '${pdf}?page=2'\nitems: ['zotero://select/library/items/SOURCE01', 'javascript:alert(1)']\n---\nBody`);
  const document = new JSDOM(html).window.document;
  assert.equal(document.querySelectorAll("[data-source-uri]").length, 2);
  assert.equal(document.querySelectorAll("[data-property-key]").length, 2);
  assert.equal(document.querySelectorAll("script").length, 0);
  const escaped = new JSDOM(sourceLinkMarkup(pdf, '<img src="x" onerror="alert(1)">')).window.document;
  assert.equal(escaped.querySelectorAll("img").length, 0);
  assert.equal(escaped.querySelector("a").textContent, '<img src="x" onerror="alert(1)">');
});
