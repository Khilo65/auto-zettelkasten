import assert from "node:assert/strict";
import test from "node:test";
import { LibraryParser } from "../../tools/workspace_index_client.js";

test("library parser worker RPC ignores stale replies and cancels outstanding pages", async (t) => {
  const original = globalThis.Worker;
  const workers = [];
  globalThis.Worker = class {
    constructor(url) { this.url = url; workers.push(this); }
    postMessage(value) { this.message = value; }
    terminate() { this.terminated = true; }
  };
  t.after(() => { if (original === undefined) delete globalThis.Worker; else globalThis.Worker = original; });
  const parser = new LibraryParser(), worker = workers[0];
  assert.equal(worker.url, "/workspace-index-worker.js");
  const parsing = parser.parse([{ path: "A.md", text: "# A" }]);
  worker.onmessage({ data: { id: 999, documents: [] } });
  worker.onmessage({ data: { id: worker.message.id, documents: [{ path: "A.md", compact: true }] } });
  assert.equal((await parsing)[0].path, "A.md");
  const pending = parser.parse([{ path: "B.md", text: "# B" }]);
  const rejected = assert.rejects(pending, /cancelled/);
  parser.close(); await rejected;
  assert.equal(worker.terminated, true);
  await assert.rejects(parser.parse([]), /cancelled/);
});

test("library parser fallback yields and returns compact projections without note bodies", async () => {
  const parser = new LibraryParser();
  let yielded = false;
  setTimeout(() => { yielded = true; }, 0);
  const [record] = await parser.parse([{ path: "A.md", text: "# A\n[[B]]", revision: "one" }]);
  assert.equal(yielded, true);
  assert.equal(record.compact, true);
  assert.equal(record.text, undefined);
  assert.equal(record.references.length, 1);
  parser.close();
});
