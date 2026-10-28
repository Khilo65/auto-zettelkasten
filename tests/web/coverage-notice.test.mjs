import assert from "node:assert/strict";
import test from "node:test";
import { compactCoverageNotice } from "../../tools/workspace_coverage.js";
import { buildLinkIndex, renderIndexedMarkdown } from "../../tools/workspace_links.js";

const inventory = "Recovered PDF pages: 1, 2, 4. Missing or unresolved PDF pages: 3. Claims in this note are restricted to the recovered pages and must not be treated as findings from the complete document.";
const warning = `> [!warning] Coverage boundary\n> ${inventory}`;
const summary = "This note is based on an incomplete extraction of the document.";

test("Only the exact generated coverage inventory is condensed", () => {
  assert.equal(compactCoverageNotice(warning), summary);
  assert.equal(compactCoverageNotice(warning.replaceAll("\n", "\r\n")), summary);
  assert.equal(compactCoverageNotice(warning.replace(", 2", "")), summary);
  for (const text of [null, "Recovered PDF pages: 1, 2, 4.", "> [!warning] Coverage boundary\n> The data omit rural households (page 3).", warning + "\n> An additional material qualification."]) {
    assert.equal(compactCoverageNotice(text), null);
  }
});

test("Reading view condenses the notice without changing source, citations, or cached tokens", () => {
  const text = `# A finding\n\n${warning}\n\n## Findings\nA substantive finding (PDF page 4).\n`;
  const index = buildLinkIndex([{ path: "A.md", text, revision: "unchanged", editable: false }]);
  const before = JSON.stringify(index.documents.get("A.md").tokens);
  for (let repeat = 0; repeat < 2; repeat += 1) {
    const html = renderIndexedMarkdown(index, "A.md");
    assert.ok(html.includes(summary));
    assert.ok(!html.includes("Recovered PDF pages"));
    assert.ok(html.includes("PDF page 4"));
  }
  assert.equal(index.documents.get("A.md").text, text);
  assert.equal(JSON.stringify(index.documents.get("A.md").tokens), before);
});

test("Fenced examples and authored warnings are preserved in reading view", () => {
  const text = `\`\`\`md\n${warning}\n\`\`\`\n\n> [!warning] Coverage boundary\n> Material evidence is absent (PDF page 3).\n`;
  const index = buildLinkIndex([{ path: "A.md", text }]);
  const html = renderIndexedMarkdown(index, "A.md");
  assert.ok(html.includes("Recovered PDF pages"));
  assert.ok(html.includes("Material evidence is absent"));
  assert.ok(!html.includes(summary));
});
