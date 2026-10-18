import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

const bundle = await readFile(new URL("../../src/auto_zettelkasten/web/workspace-vendor.js", import.meta.url), "utf8");
function render(text) {
  const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only" });
  dom.window.eval(bundle);
  dom.window.document.body.innerHTML = dom.window.WorkspaceKit.renderMarkdown(text);
  return dom;
}

test("Markdown cannot execute HTML or fetch images/media", () => {
  const dom = render('# Notes\n<script>alert(1)</script>\n\n![Tracker](https://example.invalid/pixel)\n\n[Bad](javascript:alert(1))');
  try {
    const doc = dom.window.document;
    assert.equal(doc.querySelector("h1").textContent, "Notes");
    assert.equal(doc.querySelectorAll("script,img,iframe,object,audio,video,[onerror]").length, 0);
    assert.equal(doc.querySelectorAll('[href^="javascript:"]').length, 0);
    assert.match(doc.body.textContent, /Tracker/);
  } finally { dom.window.close(); }
});

test("Only explicit HTTP links can navigate outside the workspace", () => {
  const dom = render('[Web](https://example.com/read) [Local](other.md) [File](file:///etc/passwd) [Host](//example.com)');
  try {
    const anchors = [...dom.window.document.querySelectorAll("a")];
    assert.equal(anchors.length, 1);
    assert.equal(anchors[0].href, "https://example.com/read");
    assert.equal(anchors[0].target, "_blank");
    assert.match(anchors[0].rel, /noopener/);
    assert.match(anchors[0].rel, /noreferrer/);
    assert.equal(dom.window.document.querySelectorAll(".markdown-link").length, 2);
  } finally { dom.window.close(); }
});

test("Properties are escaped display content, independent of the authored heading", () => {
  const text = '---\ntitle: Other name\naliases: ["<img src=x onerror=alert(1)>"]\n---\n# Authored heading\n\n**Body**\n';
  const dom = render(text);
  try {
    const doc = dom.window.document;
    assert.equal(doc.querySelector("details summary").textContent, "Properties");
    assert.match(doc.querySelector("details pre").textContent, /<img src=x/);
    assert.equal(doc.querySelectorAll("img").length, 0);
    assert.equal(doc.querySelector("h1").textContent, "Authored heading");
    assert.equal(doc.querySelector("strong").textContent, "Body");
  } finally { dom.window.close(); }
});
