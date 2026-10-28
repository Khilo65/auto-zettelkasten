"use strict";

// Each PDF lives in its own upstream reader window. The note editor stays owned
// by workspace.js so source and note tabs never create competing text buffers.
window.createSourceReader = function ({ container, api, message, onAnnotations, onState }) {
  const channel = "az-zotero-reader-v1", entries = new Map();
  let active = null;
  function permissions(entry, result = {}) {
    if (typeof result.readOnly === "boolean") entry.config.readOnly = result.readOnly;
    if (typeof (result.readOnlyReason ?? result.read_only_reason) === "string") entry.config.read_only_reason = result.readOnlyReason ?? result.read_only_reason;
    if (entry.initialized) send(entry, "read-only", { readOnly: entry.failed || entry.config.readOnly !== false });
    onState?.(entry.config.id, entry.viewState);
  }
  function boundDraft(entry, row) {
    if (!row || typeof row !== "object") throw new Error("Invalid annotation draft.");
    for (const [key, expected] of [["scope", entry.config.scope], ["attachment_key", entry.config.attachment_key], ["parentItem", entry.config.attachment_key]]) {
      if (row[key] !== undefined && row[key] !== expected) throw new Error("The annotation draft belongs to another source.");
    }
    return { ...entry.config.annotations.find((prior) => prior.id === row.id), ...row, scope: entry.config.scope, attachment_key: entry.config.attachment_key, parentItem: entry.config.attachment_key, version: row.version ?? entry.config.annotations.find((prior) => prior.id === row.id)?.version ?? 0 };
  }
  const send = (entry, type, fields = {}) => entry.frame?.contentWindow?.postMessage({ channel, instanceId: entry.instanceId, type, ...fields }, location.origin);
  const stateKey = (entry) => `az.reader.${entry.config.id}.${entry.config.pdf_identity || ""}`;
  function savedState(entry) {
    try { return JSON.parse(localStorage.getItem(stateKey(entry)) || "null"); } catch { return null; }
  }
  function persist(entry) {
    try { localStorage.setItem(stateKey(entry), JSON.stringify(entry.viewState || null)); }
    catch { message("Reader position could not be saved on this computer.", true); }
  }
  function createFrame(entry) {
    if (entry.frame || entries.get(entry.config.id) !== entry || active !== entry.config.id) return;
    entry.hasMounted = true;
    entry.instanceId = crypto.randomUUID();
    const frame = document.createElement("iframe");
    frame.title = `PDF reader: ${entry.config.title}`;
    frame.className = "source-reader-frame";
    frame.src = `/zotero-reader-host.html#instance=${entry.instanceId}`;
    frame.referrerPolicy = "no-referrer";
    entry.frame = frame; entry.ready = false; entry.initialized = false; frame.inert = true; container.append(frame);
  }
  function mount(entry) {
    if (entry.frame || entry.renewing || entries.get(entry.config.id) !== entry || active !== entry.config.id) return;
    if (!entry.hasMounted) { createFrame(entry); return; }
    const epoch = entry.mountEpoch, config = entry.config;
    entry.renewing = true;
    // A suspended reader's attachment capability may have expired. Renew only
    // its attachment access; the existing paired note remains the same buffer.
    Promise.resolve().then(() => api("zotero/open-pdf", { scope: config.scope, attachment_key: config.attachment_key })).then((result) => {
      if (entries.get(config.id) !== entry || epoch !== entry.mountEpoch || active !== config.id || entry.frame) return;
      const renewed = result?.reader;
      if (!renewed || renewed.id !== config.id || renewed.scope !== config.scope || renewed.attachment_key !== config.attachment_key
          || renewed.item_key !== config.item_key || typeof renewed.pdf_identity !== "string" || !renewed.pdf_identity
          || !/^\/api\/zotero\/attachments\/[A-Za-z0-9_-]+\/file$/.test(renewed.url || "")) {
        throw new Error("The renewed PDF did not match this source. Reopen it from its Zotero item.");
      }
      const changed = renewed.pdf_identity !== config.pdf_identity;
      const location = changed || entry.viewState || savedState(entry) ? {} : config.location;
      if (changed) { entry.viewState = null; entry.resetViewState = true; }
      Object.assign(entry.config, renewed, { note_path: config.note_path, expanded: config.expanded, location });
      if (changed) {
        entry.config.warning = "The PDF file changed. Its previous reading position and highlight target were reset; verify annotations before editing.";
        message(entry.config.warning, true);
      }
      createFrame(entry);
    }).catch((error) => {
      if (entries.get(config.id) === entry && epoch === entry.mountEpoch && active === config.id) {
        message(`The PDF could not reopen. ${error.message} Select this tab again to retry.`, true);
      }
    }).finally(() => {
      entry.renewing = false;
      // An explicit source-open may replace the config during the renewal.
      if (epoch !== entry.mountEpoch && entries.get(entry.config.id) === entry && active === entry.config.id && !entry.frame) mount(entry);
    });
  }
  async function handleMessage(event) {
    if (event.origin !== location.origin || !event.data || event.data.channel !== channel) return;
    const entry = [...entries.values()].find((row) => row.frame?.contentWindow === event.source && row.instanceId === event.data.instanceId);
    if (!entry) return;
    const data = event.data;
    if (data.type === "ready") {
      entry.ready = true;
      send(entry, "init", { document: { url: entry.config.url, attachmentKey: entry.config.attachment_key }, annotations: entry.config.annotations,
        location: entry.config.location, viewState: entry.resetViewState ? null : entry.viewState || savedState(entry), readOnly: entry.config.readOnly !== false });
    } else if (data.type === "initialized") {
      entry.initialized = true; entry.frame.inert = false;
      permissions(entry);
      send(entry, "suspend", { suspended: active !== entry.config.id });
    } else if (data.type === "view-state" && data.primary !== false && data.viewState) {
      entry.viewState = data.viewState; entry.resetViewState = false; persist(entry); onState?.(entry.config.id, data.viewState);
    } else if (data.type === "retain-annotations") {
      entry.pending += 1; entry.failed = true; entry.failedOperations.add(data.operationId); permissions(entry);
      try {
        const annotations = data.deleting ? data.ids.map((id) => ({ id, version: entry.config.annotations.find((row) => row.id === id)?.version || 0 }))
          : data.annotations.map((row) => boundDraft(entry, row));
        const result = await api("zotero/annotation-retain", { scope: entry.config.scope, attachment_key: entry.config.attachment_key, operation_id: data.operationId, annotations, delete: data.deleting === true });
        if (result.retained !== true) throw new Error("The newer annotation draft was not confirmed as retained locally.");
        send(entry, "retention-result", { operationId: data.operationId, retained: true });
        message("Annotation save needs review. Your latest changes are retained locally in Annotation changes.");
      } catch (error) { send(entry, "retention-result", { operationId: data.operationId, retained: false, error: error.message }); message(error.message, true); }
      finally { entry.pending -= 1; onState?.(entry.config.id, entry.viewState); }
    } else if (data.type === "save-annotations" || data.type === "delete-annotations") {
      entry.pending += 1;
      try {
        const deleting = data.type === "delete-annotations";
        const annotations = deleting ? data.ids.map((id) => {
          const prior = entry.config.annotations.find((row) => row.id === id);
          if (!prior) throw new Error("An annotation changed. Refresh before deleting it.");
          return { id, version: prior.version };
        }) : data.annotations.map((row) => boundDraft(entry, row));
        const result = await api(`zotero/annotation-${deleting ? "delete" : "save"}`, {
          scope: entry.config.scope, attachment_key: entry.config.attachment_key, operation_id: data.operationId, annotations,
        });
        if (result.resolved === false || result.pending_receipt_ids?.length || (Array.isArray(result.pending) ? result.pending.length > 0 : result.pending)) throw new Error(result.warning || "The annotation save is pending reconciliation. It has been retained locally.");
        permissions(entry, result);
        if (Array.isArray(result.annotations) || Array.isArray(result.deleted)) {
          // Write responses contain confirmed deltas, not an attachment snapshot.
          // A replay may return no changes; absence never means deletion here.
          const merged = new Map(entry.config.annotations.map((row) => [row.id, row]));
          for (const row of result.annotations || []) merged.set(row.id, { ...merged.get(row.id), ...row });
          const deletedIds = [...new Set(result.deleted || [])];
          for (const id of deletedIds) merged.delete(id);
          entry.config.annotations = [...merged.values()];
          send(entry, "annotations", { annotations: entry.config.annotations, deletedIds, confirmedIds: (result.annotations || []).map((row) => row.id) });
        }
        send(entry, "operation-result", { operationId: data.operationId, success: true });
        Promise.resolve(onAnnotations?.(entry.config)).catch((error) => message(`Annotations saved; note projection: ${error.message}`, true));
      } catch (error) {
        entry.failed = true; entry.failedOperations.add(data.operationId); onState?.(entry.config.id, entry.viewState);
        send(entry, "operation-result", { operationId: data.operationId, success: false, error: error.message });
        message(error.message, true);
      } finally { entry.pending -= 1; }
    } else if (data.type === "closed") {
      entry.frame?.remove(); entry.frame = null; entry.ready = false; entry.initialized = false; entry.closing = false;
      const manual = entry.manualClosing; entry.closed?.(true); entry.closed = null;
      if (!manual && active === entry.config.id) mount(entry);
    } else if (data.type === "flush-result") {
      entry.flushed?.(data.success === true); entry.flushed = null;
    } else if (data.type === "open-link") {
      if (/^https?:\/\//i.test(data.url || "")) window.open(data.url, "_blank", "noopener,noreferrer");
    } else if (data.type === "error") {
      message(data.message || data.error || "The PDF reader could not complete this operation.", true);
      entry.closed?.(false); entry.closed = null;
    }
  }
  window.addEventListener("message", handleMessage);
  return {
    register(config) {
      config = { ...config, read_only_reason: config.read_only_reason ?? config.readOnlyReason ?? "" };
      let entry = entries.get(config.id);
      if (!entry) { entry = { config, frame: null, pending: 0, failed: false, failedOperations: new Set(), viewState: null, closing: false, hasMounted: false, renewing: false, mountEpoch: 0, resetViewState: false }; entries.set(config.id, entry); }
      else { entry.mountEpoch += 1; entry.config = config; permissions(entry, config); if (entry.initialized && !entry.pending && !entry.failed) { send(entry, "annotations", { annotations: config.annotations }); if (config.location) send(entry, "navigate", { location: config.location }); } }
      return entry.config;
    },
    activate(id) {
      active = id;
      for (const [key, entry] of entries) {
        if (key === id) { mount(entry); if (entry.frame) entry.frame.hidden = false; if (entry.initialized) send(entry, "suspend", { suspended: false }); }
        else if (entry.frame) { entry.frame.hidden = true; if (entry.initialized) send(entry, "suspend", { suspended: true }); }
      }
      // Inactive windows flush durable edits before releasing their PDF canvases.
      for (const [key, entry] of entries) if (key !== active && entry.frame && entry.initialized && !entry.closing && !entry.closed && !entry.pending && !entry.failed) { entry.closing = true; send(entry, "close"); }
    },
    async close(id, { keepPending = false } = {}) {
      const entry = entries.get(id); if (!entry) return true;
      let retained = [];
      if (entry.pending) { message("Wait until the annotation changes are retained locally before closing this PDF.", true); return false; }
      if (entry.failed) {
        if (!keepPending) { message("Resolve the pending annotation save before closing this PDF.", true); return false; }
        const result = await api("zotero/annotation-recovery", { scope: entry.config.scope, attachment_key: entry.config.attachment_key, start: 0, limit: 64 });
        const durable = new Set((result.records || []).filter((row) => row.durable === true).map((row) => row.request_id));
        retained = [...entry.failedOperations];
        if (retained.some((id) => !durable.has(id)) || !retained.length && !(result.records || []).every((row) => row.durable === true)) { message("Some changes are not confirmed as retained locally. Keep this PDF open.", true); return false; }
      }
      if (entry.frame && entry.initialized) {
        entry.manualClosing = true;
        const okay = await new Promise((resolve) => { entry.closed = resolve; if (!entry.closing) { entry.closing = true; send(entry, retained.length ? "close-retained" : "close", { operationIds: retained }); } setTimeout(() => { if (entry.closed === resolve) { entry.closed = null; resolve(false); } }, 10000); });
        if (!okay) { entry.manualClosing = false; entry.closing = false; message("The reader has not confirmed its edits are saved. The PDF remains open.", true); return false; }
      } else entry.frame?.remove();
      entry.mountEpoch += 1; persist(entry); entries.delete(id); if (active === id) active = null;
      api("zotero/reader-close", { reader_id: id }).catch(() => {});
      return true;
    },
    hasPending: () => [...entries.values()].some((row) => row.pending || row.failed),
    async refresh(id) {
      const entry = entries.get(id); if (!entry || entry.pending) return;
      let result;
      try { result = await api(entry.failed && entry.failedOperations.size ? "zotero/annotation-reconcile" : "zotero/annotation-refresh", { scope: entry.config.scope, attachment_key: entry.config.attachment_key, ...(entry.failedOperations.size ? { request_ids: [...entry.failedOperations] } : {}) }); }
      catch (error) { permissions(entry, { readOnly: true, read_only_reason: error.message }); throw error; }
      permissions(entry, result);
      if (result.pending_receipt_ids?.length || Array.isArray(result.pending) && result.pending.length) { entry.failed = true; permissions(entry); throw new Error("Saved annotation changes need recovery. Open Annotation changes to review them."); }
      if (entry.failed && entry.failedOperations.size && !result.resolved) throw new Error("The pending annotation changes need reconciliation. Your edits remain retained; no automatic retry was made.");
      if (result.annotations) {
        const deletedIds = entry.config.annotations.filter((old) => !result.annotations.some((row) => row.id === old.id)).map((row) => row.id);
        entry.config.annotations = result.annotations;
        if (entry.failed) { entry.frame?.remove(); entry.frame = null; entry.failed = false; entry.failedOperations.clear(); onState?.(entry.config.id, entry.viewState); if (active === id) mount(entry); }
        else if (entry.initialized) send(entry, "annotations", { annotations: result.annotations, deletedIds });
        await onAnnotations?.(entry.config);
      }
    },
    hasFailed: (id) => Boolean(entries.get(id)?.failed),
    async refreshAll() { for (const id of entries.keys()) { try { await this.refresh(id); } catch (error) { message(error.message, true); } } },
    async flushAll() {
      for (const entry of entries.values()) {
        if (entry.failed) return false;
        if (!entry.frame || !entry.initialized) continue;
        const okay = await new Promise((resolve) => { entry.flushed = resolve; send(entry, "flush"); setTimeout(() => { if (entry.flushed === resolve) { entry.flushed = null; resolve(false); } }, 10000); });
        if (!okay) return false;
      }
      return ![...entries.values()].some((row) => row.pending || row.failed);
    },
    async retry(id) {
      const entry = entries.get(id); if (!entry?.failed || entry.pending) return;
      const result = await api("zotero/annotation-retry", { scope: entry.config.scope, attachment_key: entry.config.attachment_key, request_ids: [...entry.failedOperations] });
      if (!result.resolved) throw new Error("Some annotation changes still require reconciliation. No unknown write was retried.");
      permissions(entry, result);
      entry.config.annotations = result.annotations;
      entry.frame?.remove(); entry.frame = null; entry.failed = false; entry.failedOperations.clear(); onState?.(entry.config.id, entry.viewState);
      if (active === id) mount(entry);
      await onAnnotations?.(entry.config);
    },
    async clear() { for (const key of [...entries.keys()]) if (!(await this.close(key))) return false; return true; },
    async destroy() { if (!(await this.clear())) return false; window.removeEventListener("message", handleMessage); return true; },
  };
};
