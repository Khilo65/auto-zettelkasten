import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../../zotero-companion/bootstrap.js", import.meta.url), "utf8");
const route = "/api/users/:userID/items/:itemKey/az-annotation-capabilities";

async function fixture(options = {}) {
  const pdf = { id: 1, key: "PDFATT23", libraryID: 1, clientVersion: 12, deleted: false,
    parentItem: { deleted: false }, isPDFAttachment: () => true, isEditable: () => true };
  const annotation = { id: 2, key: "ANNTA234", libraryID: 1, parentID: 1, clientVersion: 15,
    deleted: false, isAnnotation: () => true, isEditable: () => true };
  const rows = new Map([[pdf.key, pdf], [annotation.key, annotation]]);
  const state = { pdf, annotation, flags: { isExternal: false, readOnly: false }, calls: [] };
  class Item { async init(request) { return this.run(request); } }
  const Zotero = { version: options.version || "10.0.6", initializationPromise: Promise.resolve(), debug() {},
    Server: { Endpoints: {}, LocalAPI: { Item, getServerID: () => "SERVER000001" } },
    Annotations: { toJSONSync: (item) => { state.calls.push(item.key); return state.flags; } },
    Items: { getByLibraryAndKeyAsync: async (libraryID, key) => rows.get(key) || null } };
  const context = vm.createContext({ Zotero, Headers, URLSearchParams });
  vm.runInContext(source, context);
  await context.startup();
  async function request(overrides = {}) {
    const value = { method: "GET", headers: new Headers({ "Zotero-Server-ID": "SERVER000001" }),
      pathParams: { userID: "0", itemKey: pdf.key }, libraryID: 1,
      searchParams: new URLSearchParams({ annotationKeys: annotation.key }), ...overrides };
    return new Zotero.Server.Endpoints[route]().init(value);
  }
  return { state, Zotero, context, request };
}

test("companion exposes only identity-bound flags and native local versions", async () => {
  const { request, state, Zotero } = await fixture();
  const [status, headers, body] = await request();
  assert.equal(status, 200);
  assert.equal(headers["Cache-Control"], "no-store");
  const value = JSON.parse(body);
  assert.deepEqual(value.annotations, [{ key: "ANNTA234", version: 15, isExternal: false, readOnly: false }]);
  assert.equal(value.attachment_version, 12);
  assert.equal(value.scope, "users/0");
  assert.deepEqual(Object.keys(value.annotations[0]).sort(), ["isExternal", "key", "readOnly", "version"]);
  assert.deepEqual(Array.from(new Zotero.Server.Endpoints[route]().supportedMethods), ["GET"]);
  assert.equal(state.calls.length, 1);
});

test("external marks, another author and restricted/deleted PDFs remain read-only", async () => {
  for (const restrict of [
    state => { state.flags = { isExternal: true, readOnly: true }; },
    state => { state.flags.readOnly = true; },
    state => { state.annotation.isEditable = () => false; },
    state => { state.pdf.isEditable = () => false; },
    state => { state.pdf.parentItem.deleted = true; },
    state => { state.pdf.deleted = true; },
  ]) {
    const { state, request } = await fixture(); restrict(state);
    const [status, , body] = await request();
    assert.equal(status, 200);
    assert.equal(JSON.parse(body).annotations[0].readOnly, true);
  }
});

test("capability reads reject browser origins and unbound server identities", async () => {
  const { request, state } = await fixture();
  assert.equal((await request({ headers: new Headers() }))[0], 428);
  assert.equal((await request({ headers: new Headers({ "Zotero-Server-ID": "SERVER000002" }) }))[0], 428);
  assert.equal((await request({ headers: new Headers({ "Zotero-Server-ID": "SERVER000001", Origin: "null" }) }))[0], 403);
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) assert.equal((await request({ method }))[0], 405);
  assert.equal(state.calls.length, 0);
});

test("capabilities cannot cross a PDF, library or deleted annotation", async () => {
  const { state, request } = await fixture();
  state.annotation.parentID = 3;
  assert.equal((await request())[0], 409);
  state.annotation.parentID = 1; state.annotation.libraryID = 2;
  assert.equal((await request())[0], 409);
  state.annotation.libraryID = 1; state.annotation.deleted = true;
  assert.deepEqual(JSON.parse((await request())[2]).missing, [state.annotation.key]);
});

test("malformed, repeated or excessive identities fail before capability lookup", async () => {
  const { request, state } = await fixture();
  for (const annotationKeys of ["bad", "ANNTA234,ANNTA234", Array.from({ length: 65 }, (_, i) => `A${String(i).padStart(7, "2")}`).join(",")]) {
    assert.equal((await request({ searchParams: new URLSearchParams({ annotationKeys }) }))[0], 400);
  }
  assert.equal((await request({ searchParams: new URLSearchParams("annotationKeys=ANNTA234&annotationKeys=THER2345") }))[0], 400);
  assert.equal((await request({ pathParams: { userID: "12", itemKey: "PDFATT23" } }))[0], 400);
  assert.equal(state.calls.length, 0);
});

test("empty annotation selection exposes only fresh attachment permissions", async () => {
  const { request, state } = await fixture();
  const options = { searchParams: new URLSearchParams({ annotationKeys: "" }) };
  let response = await request(options);
  assert.equal(response[0], 200);
  let value = JSON.parse(response[2]);
  assert.deepEqual(value.annotations, []);
  assert.deepEqual(value.missing, []);
  assert.equal(value.attachment_read_only, false);
  state.pdf.isEditable = () => false;
  response = await request(options); value = JSON.parse(response[2]);
  assert.equal(value.attachment_read_only, true);
  assert.equal(state.calls.length, 0);
  assert.equal((await request({ searchParams: new URLSearchParams() }))[0], 400);
});

test("shutdown removes owned routes only and unsupported runtime installs none", async () => {
  const { Zotero, context } = await fixture();
  const replacement = class {};
  Zotero.Server.Endpoints[route] = replacement;
  context.shutdown();
  assert.equal(Zotero.Server.Endpoints[route], replacement);
  assert.equal(Zotero.Server.Endpoints["/api/groups/:groupID/items/:itemKey/az-annotation-capabilities"], undefined);
  const unsupported = await fixture({ version: "10.1.0" });
  assert.equal(Object.keys(unsupported.Zotero.Server.Endpoints).length, 0);
});
