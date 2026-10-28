import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
const hostModule = { exports: {} };
vm.runInNewContext(fs.readFileSync(new URL('../../src/auto_zettelkasten/web/zotero-reader-host.js', import.meta.url), 'utf8'),
  { module: hostModule, URL, URLSearchParams });
const { createHost, attachmentURL, readerLocation, CHANNEL } = hostModule.exports;
const origin = 'http://127.0.0.1:57891';
const instanceId = 'fixture-instance';
const url = `${origin}/api/zotero/attachments/opaque-capability-123456/file`;
const annotation = { id: 'ABCD1234', type: 'highlight', text: 'Synthetic quotation', sortIndex: '00000|000001', position: { pageIndex: 0, rects: [[10, 10, 50, 25]] } };

function setup() {
  const events = [], calls = [], timers = new Map();
  let options;
  const reader = {
    initializedPromise: new Promise(() => {}),
    _primaryView: { initializedPromise: Promise.resolve(true), destroy: () => calls.push(['destroy-view']),
      _iframeWindow: { PDFViewerApplication: { pdfLoadingTask: { destroy: async () => calls.push(['destroy-worker']) } } } },
    _annotationManager: { _unsavedAnnotations: new Map(), _savingInProgress: false, _triggerSaving: async () => {} },
    setReadOnly: value => calls.push(['readOnly', value]),
    setAnnotations: value => calls.push(['annotations', value]),
    unsetAnnotations: value => calls.push(['deleted', value]),
    navigate: value => calls.push(['navigate', value]),
    setSuspended: value => calls.push(['suspended', value]),
    trimMemory: () => calls.push(['trim']),
    _getString: key => key,
    openContextMenu: menu => calls.push(['menu', menu]),
  };
  const parent = { postMessage: (message, target) => events.push({ ...message, target }) };
  const win = {
    location: { origin, hash: `#instance=${instanceId}` }, parent,
    document: { getElementById: () => ({ hidden: false, textContent: '' }) },
    addEventListener: () => {},
    setTimeout: fn => { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    createReader: input => { options = input; return reader; },
  };
  const host = createHost(win);
  const send = (data, overrides = {}) => host.handle({ source: parent, origin, data: { channel: CHANNEL, instanceId, ...data }, ...overrides });
  return { events, calls, timers, host, reader, send, options: () => options };
}

test('PDF URL accepts only a same-origin capability endpoint', () => {
  assert.equal(attachmentURL(url, origin), url);
  for (const invalid of ['https://evil.test/book.pdf', '/api/workspace/document?path=secret',
    'file:///private/book.pdf', 'javascript:alert(1)', `${url}?token=global`, `${url}#page=2`,
    `${origin}/api/zotero/attachments/short/file`, 'http://user:pass@127.0.0.1:57891/api/zotero/attachments/opaque-capability-123456/file']) {
    assert.throws(() => attachmentURL(invalid, origin));
  }
});

test('physical pages remain separate from display labels and invalid targets fail', () => {
  assert.equal(JSON.stringify(readerLocation({ pageIndex: 0, annotationID: 'ABCD1234', pageLabel: '458' })), JSON.stringify({ annotationID: 'ABCD1234', pageIndex: 0 }));
  assert.throws(() => readerLocation({ pageIndex: -1 }));
  assert.throws(() => readerLocation({ annotationID: '../other' }));
});

test('init binds source, origin and instance; read-only is the safe default', async () => {
  const f = setup();
  for (const override of [{ source: {} }, { origin: 'https://evil.test' }, { data: { channel: CHANNEL, instanceId: 'other', type: 'init', document: { url } } }]) {
    await f.send({ type: 'init', document: { url } }, override);
  }
  assert.equal(f.options(), undefined);
  await f.send({ type: 'init', document: { url }, annotations: [annotation] });
  assert.equal(f.options().readOnly, true);
  assert.equal(f.options().enableReadAloud, false);
  assert.equal(f.options().data.url, url);
  assert.equal(f.options().primaryViewState, undefined);
  assert.ok(f.events.some(x => x.type === 'initialized'));
  await f.send({ type: 'init', document: { url } });
  assert.equal(f.events.at(-1).type, 'error');
});

test('save/delete operations serialize, require exact acknowledgements and do not retry', async () => {
  const f = setup();
  await f.send({ type: 'init', document: { url }, readOnly: false });
  const first = f.options().onSaveAnnotations([annotation]);
  const second = f.options().onSaveAnnotations([{ ...annotation, comment: 'Changed' }]);
  assert.equal(f.calls.some(row => row[0] === 'readOnly' && row[1] === true), false, 'typing remains available during normal saves');
  await Promise.resolve();
  const request = f.events.find(x => x.type === 'save-annotations');
  assert.ok(request);
  assert.equal(f.events.filter(x => x.type === 'save-annotations').length, 1);
  await f.send({ type: 'operation-result', operationId: 'wrong', success: true });
  assert.equal(f.events.filter(x => x.type === 'save-annotations').length, 1);
  await f.send({ type: 'operation-result', operationId: request.operationId, success: true });
  await first;
  await Promise.resolve();
  const requests = f.events.filter(x => x.type === 'save-annotations');
  assert.equal(requests.length, 2);
  await f.send({ type: 'operation-result', operationId: requests[1].operationId, success: true });
  await second;
  assert.equal(f.calls.some(row => row[0] === 'readOnly'), false, 'normal acknowledgements must not reset the active reader tool');
  f.options().onDeleteAnnotations(['ABCD1234']);
  await Promise.resolve();
  const deletion = f.events.find(x => x.type === 'delete-annotations');
  await f.send({ type: 'operation-result', operationId: deletion.operationId, success: true });
  await f.send({ type: 'close' });
  assert.equal(f.events.at(-1).type, 'closed');
  assert.ok(f.calls.some(x => x[0] === 'suspended' && x[1]));
  assert.ok(f.calls.some(x => x[0] === 'destroy-worker'));
});

test('failed writes prevent close and unknown outcomes remain pending', async () => {
  const f = setup();
  await f.send({ type: 'init', document: { url }, readOnly: false });
  const save = f.options().onSaveAnnotations([annotation]);
  await Promise.resolve();
  for (const timeout of f.timers.values()) timeout();
  assert.ok(f.events.some(x => x.code === 'write-acknowledgement-pending'));
  const request = f.events.find(x => x.type === 'save-annotations');
  assert.equal(f.events.filter(x => x.type === 'save-annotations').length, 1);
  await f.send({ type: 'operation-result', operationId: request.operationId, success: false, error: 'Version conflict' });
  await assert.rejects(save, /Version conflict/);
  await f.send({ type: 'close' });
  assert.equal(f.events.at(-1).type, 'error');
  assert.equal(f.events.some(x => x.type === 'closed'), false);
});

test('links are delegated, unsafe schemes ignored, annotations refreshed by identity', async () => {
  const f = setup();
  await f.send({ type: 'init', document: { url } });
  f.options().onOpenLink('javascript:alert(1)');
  f.options().onOpenLink('file:///secret');
  f.options().onOpenLink('zotero://open-pdf/library/items/ABCD1234?page=1');
  assert.equal(f.events.filter(x => x.type === 'open-link').length, 1);
  await f.send({ type: 'annotations', annotations: [annotation], deletedIds: ['WXYZ5678'] });
  assert.deepEqual(f.calls.filter(x => x[0] === 'deleted'), [['deleted', ['WXYZ5678']]]);
});

test('unsupported PDF mutation commands are removed from the reader menu', async () => {
  const f = setup();
  await f.send({ type: 'init', document: { url }, readOnly: false });
  f.options().onOpenContextMenu({ itemGroups: [[{ label: 'reader-rotate-left' }, { label: 'reader-rotate-right' }], [{ label: 'Copy' }]] });
  const menu = f.calls.find(row => row[0] === 'menu')[1];
  assert.equal(menu.itemGroups.length, 1);
  assert.equal(menu.itemGroups[0][0].label, 'Copy');
});

test('packaged runtime matches the declared pin and contains notices and range-capable PDF.js', () => {
  const base = new URL('../../src/auto_zettelkasten/web/zotero-reader/', import.meta.url);
  const manifest = JSON.parse(fs.readFileSync(new URL('manifest.json', base), 'utf8'));
  const pin = JSON.parse(fs.readFileSync(new URL('../../third_party/zotero-reader/pin.json', import.meta.url), 'utf8'));
  assert.equal(manifest.revision, pin.revision);
  assert.deepEqual(manifest.submodules, pin.submodules);
  for (const name of ['reader.js', 'reader.css', 'COPYING', 'THIRD_PARTY_NOTICES.txt', 'pdf/web/viewer.html', 'pdf/build/pdf.worker.mjs']) {
    assert.ok(fs.statSync(new URL(name, base)).size > 0);
  }
  for (const [name, digest] of Object.entries(manifest.assets_sha256)) {
    assert.equal(createHash('sha256').update(fs.readFileSync(new URL(name, base))).digest('hex'), digest, name);
  }
  assert.equal(manifest.distribution_review_required, true);
});

test('closing retained failed writes requires every emitted operation identity', async () => {
  const f = setup(); await f.send({ type: 'init', document: { url }, readOnly: false });
  const save = f.options().onSaveAnnotations([annotation]); await Promise.resolve();
  const operation = f.events.find(row => row.type === 'save-annotations');
  await f.send({ type: 'operation-result', operationId: operation.operationId, success: false, error: 'Retained locally' });
  await assert.rejects(save); await Promise.resolve();
  await f.send({ type: 'close-retained', operationIds: ['another-operation'] });
  assert.equal(f.events.at(-1).type, 'error');
  await f.send({ type: 'close-retained', operationIds: [operation.operationId] });
  assert.equal(f.events.at(-1).type, 'closed');
  assert.equal(f.events.some(row => row.type === 'operation-result' && row.success), false);
});

test('newer comment text is not overwritten by acknowledgements and is retained on save failure', async () => {
  const f = setup(); await f.send({ type: 'init', document: { url }, readOnly: false });
  const save = f.options().onSaveAnnotations([{ ...annotation, comment: 'E' }]); await Promise.resolve();
  const operation = f.events.find(row => row.type === 'save-annotations');
  f.reader._annotationManager._unsavedAnnotations.set(annotation.id, { ...annotation, comment: 'Entire comment typed during the request' });
  await f.send({ type: 'annotations', annotations: [{ ...annotation, comment: 'E' }], deletedIds: [] });
  assert.equal(f.calls.some(row => row[0] === 'annotations'), false, 'a remote acknowledgement cannot overwrite newer typing');
  await f.send({ type: 'operation-result', operationId: operation.operationId, success: false, error: 'Native version changed' });
  await assert.rejects(save); await Promise.resolve();
  const retained = f.events.find(row => row.type === 'retain-annotations');
  assert.equal(retained.annotations[0].comment, 'Entire comment typed during the request');
  await f.send({ type: 'retention-result', operationId: retained.operationId, retained: true });
  assert.equal(f.reader._annotationManager._unsavedAnnotations.size, 0);
  await f.send({ type: 'close-retained', operationIds: [operation.operationId, retained.operationId] });
  assert.equal(f.events.at(-1).type, 'closed');
});

test('own confirmations advance versions without echoing comments or resetting the active tool', async () => {
  const f = setup(); await f.send({ type: 'init', document: { url }, readOnly: false });
  const shown = { ...annotation, version: 1, comment: 'Current text being typed' };
  const draft = { ...shown, comment: 'Even newer text' };
  f.reader._annotationManager._annotations = [shown];
  f.reader._annotationManager._unsavedAnnotations.set(annotation.id, draft);
  await f.send({ type: 'annotations', annotations: [{ ...annotation, version: 2, comment: 'Earlier acknowledged text' }], confirmedIds: [annotation.id], deletedIds: [] });
  await f.send({ type: 'read-only', readOnly: false });
  assert.equal(shown.version, 2); assert.equal(draft.version, 2);
  assert.equal(shown.comment, 'Current text being typed'); assert.equal(draft.comment, 'Even newer text');
  assert.equal(f.calls.some(row => row[0] === 'annotations' || row[0] === 'readOnly' || row[0] === 'deleted'), false);
  f.reader._annotationManager._unsavedAnnotations.clear();
  await f.send({ type: 'annotations', annotations: [{ ...annotation, version: 3, comment: 'Last saved text' }], confirmedIds: [annotation.id], deletedIds: [] });
  assert.equal(shown.version, 3); assert.equal(shown.comment, 'Current text being typed');
  assert.equal(f.calls.some(row => row[0] === 'annotations'), false);
  await f.send({ type: 'annotations', annotations: [{ ...annotation, version: 3, comment: 'Last saved text', readOnly: true }], confirmedIds: [annotation.id] });
  assert.equal(shown.readOnly, true); assert.equal(shown.comment, 'Current text being typed');
  assert.equal(f.calls.filter(row => row[0] === 'annotations').at(-1)[1][0].comment, 'Current text being typed');
  await f.send({ type: 'read-only', readOnly: true });
  assert.deepEqual(f.calls.at(-1), ['readOnly', true], 'actual permission loss still locks editing');
});

test('an acknowledgement timeout retains newer buffered edits without resending the unknown save', async () => {
  const f = setup(); await f.send({ type: 'init', document: { url }, readOnly: false });
  f.options().onSaveAnnotations([{ ...annotation, comment: 'First text' }]); await Promise.resolve();
  f.reader._annotationManager._unsavedAnnotations.set(annotation.id, { ...annotation, comment: 'Latest text retained on timeout' });
  for (const timer of [...f.timers.values()]) timer();
  const retained = f.events.find(row => row.type === 'retain-annotations');
  assert.equal(retained.annotations[0].comment, 'Latest text retained on timeout');
  assert.equal(f.events.filter(row => row.type === 'save-annotations').length, 1);
  assert.deepEqual(f.calls.filter(row => row[0] === 'readOnly').at(-1), ['readOnly', true]);
  await f.send({ type: 'retention-result', operationId: retained.operationId, retained: true });
  assert.equal(f.reader._annotationManager._unsavedAnnotations.size, 0);
});

test('application close flushes upstream debounced edits before reporting ready', async () => {
  const f = setup(); await f.send({ type: 'init', document: { url }, readOnly: false });
  f.reader._annotationManager._unsavedAnnotations.set(annotation.id, annotation);
  f.reader._annotationManager._triggerSaving = async () => {
    if (!f.reader._annotationManager._unsavedAnnotations.size) return;
    f.reader._annotationManager._unsavedAnnotations.clear();
    await f.options().onSaveAnnotations([annotation]);
  };
  const flushed = f.send({ type: 'flush' }); await Promise.resolve(); await Promise.resolve();
  const operation = f.events.find(row => row.type === 'save-annotations'); assert.ok(operation);
  assert.equal(f.events.some(row => row.type === 'flush-result'), false);
  await f.send({ type: 'operation-result', operationId: operation.operationId, success: true }); await flushed;
  assert.equal(f.events.at(-1).type, 'flush-result'); assert.equal(f.events.at(-1).success, true);
  assert.equal(f.calls.some(row => row[0] === 'destroy-view'), false);
});
