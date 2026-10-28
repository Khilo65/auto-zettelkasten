"use strict";

// Recovery is a view of durable backend receipts, never a second write queue.
window.createAnnotationPanel = function ({ container, api, message = () => {}, getWorkspace, getDocument, onUpdated, onOpenPDF, onOpenNote }) {
  if (!container || typeof api !== "function") throw new Error("Annotation changes need a container and local API.");
  const doc = container.ownerDocument;
  const state = { visible: false, destroyed: false, generation: 0, refreshing: false, busy: false, records: [], next: null, server: null, jobs: [], review: null, focus: null };
  const recoveryLabels = { saved: "Saved in Zotero", discarded: "Kept Zotero version · local draft retained", waiting_to_send: "Waiting to send", checking_outcome: "Checking outcome", conflict: "Conflict", unable_to_verify: "Unable to verify" };
  const syncLabels = { queued: "Note update pending", checking: "Checking note update", waiting_for_draft: "Note update waiting for your draft", ready: "Note update ready", blocked: "Annotation section needs review", waiting_for_zotero: "Waiting for Zotero", failed: "Note update needs attention", current: "Note up to date", paused: "Note updates paused" };
  function node(tag, className, text) { const value = doc.createElement(tag); if (className) value.className = className; if (text !== undefined) value.textContent = text; return value; }
  function button(label, action, className = "") {
    const value = node("button", className, label); value.type = "button";
    value.addEventListener("click", () => { try { Promise.resolve(action()).catch(showError); } catch (error) { showError(error); } });
    return value;
  }
  function notify(text, error = false) { if (state.destroyed) return; notice.textContent = text; notice.classList.toggle("is-error", error); }
  function showError(error) { const text = error?.message || "Annotation changes could not be checked."; notify(text, true); message(text, true); }
  function current(generation) { return !state.destroyed && generation === state.generation; }
  function identity(row) { return { scope: row.scope, attachment_key: row.attachment_key }; }
  function operationIds(row) {
    const values = row.operation_ids;
    return Array.isArray(values) && values.length > 0 && values.length <= 64 && values.every((value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)) && new Set(values).size === values.length ? values : null;
  }
  function recoveryKey(row) {
    return row.receipt_id ? `receipt:${row.receipt_id}` : `operations:${row.scope}:${row.attachment_key}:${[...(operationIds(row) || [])].sort().join(",")}`;
  }
  function workspace() { const value = getWorkspace?.(); if (!value?.id) throw new Error("Open a Markdown workspace to review note updates."); return value; }
  function serializedDraft(row) { return JSON.stringify(row.drafts || [], null, 2); }
  function title(row) { return row.title || row.note_path || row.attachment_title || `PDF ${row.attachment_key || ""}`; }
  container.classList.add("workspace-annotations"); container.setAttribute("aria-label", "Annotation changes"); container.hidden = true;
  const heading = node("div", "annotation-panel-heading"); heading.append(node("h2", "", "Annotation changes"));
  const refreshButton = button("Refresh", () => refresh()); const closeButton = button("Close", hide); heading.append(refreshButton, closeButton);
  const notice = node("p", "annotation-panel-notice", "Checking saved annotation changes does not resend them."); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
  const list = node("section", "annotation-recovery-list"); list.setAttribute("aria-label", "Saved annotation changes");
  const more = button("Load more saved changes", loadMore); more.hidden = true;
  const sync = node("section", "annotation-sync-list"); sync.setAttribute("aria-label", "Automatic note updates");
  const review = node("section", "annotation-review"); review.hidden = true; review.setAttribute("aria-label", "Review annotation changes"); review.setAttribute("tabindex", "-1");
  container.append(heading, notice, list, more, sync, review);

  function renderBusy() { refreshButton.disabled = state.busy || state.refreshing; more.disabled = state.busy || state.refreshing; for (const value of container.querySelectorAll("button[data-action]")) value.disabled = state.busy; }
  async function copyDraft(value, textarea) {
    try {
      if (!doc.defaultView.navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await doc.defaultView.navigator.clipboard.writeText(value); notify("Local annotation draft copied.");
    } catch {
      textarea.focus(); textarea.select(); notify("The draft is selected. Use Copy from the Edit menu or your keyboard.");
    }
  }
  function draftView(row) {
    const details = node("details", "annotation-local-draft"); details.append(node("summary", "", "View retained local draft"));
    const text = node("textarea"); text.readOnly = true; text.value = serializedDraft(row); text.setAttribute("aria-label", "Retained local annotation draft"); text.rows = 7;
    details.append(text, button("Copy local draft", () => copyDraft(text.value, text))); return details;
  }
  function renderRecords() {
    list.replaceChildren(node("h3", "", "Saved annotation changes"));
    if (!state.records.length) list.append(node("p", "annotation-empty", "No retained annotation changes need recovery."));
    for (const row of state.records) {
      const item = node("article", "annotation-change"); item.dataset.receiptId = row.receipt_id || "";
      item.append(node("h4", "", title(row)), node("p", "annotation-change-state", recoveryLabels[row.status] || "Unable to verify"));
      if (row.reason || row.warning) item.append(node("p", "annotation-change-reason", row.reason || row.warning));
      if (row.status === "saved") item.append(node("p", "annotation-change-reason", "Saved in Zotero. The note update is tracked separately below."));
      if (row.durable === true) item.append(node("p", "annotation-change-reason", "Your local draft is retained on this computer."));
      item.append(draftView(row));
      const actions = node("div", "annotation-actions");
      const add = (label, action) => { const value = button(label, action); value.dataset.action = "recovery"; actions.append(value); };
      if (row.actions?.check === true) add("Check saved state", () => recover(row, "reconcile"));
      if (row.receipt_id && row.actions?.retry === true) add("Retry saved changes", () => recover(row, "retry"));
      if (row.receipt_id && row.actions?.discard === true) add("Keep Zotero version", () => reviewDiscard(row));
      if (onOpenPDF && row.scope && row.attachment_key) add("Open PDF", () => onOpenPDF(identity(row)));
      if (onOpenNote && row.note_path) add("Open note", () => onOpenNote(row.note_path));
      item.append(actions); list.append(item);
    }
    more.hidden = state.next == null; renderBusy();
  }
  function renderJobs() {
    sync.replaceChildren(node("h3", "", "Automatic note updates"));
    if (!getWorkspace?.()?.id) { sync.append(node("p", "annotation-empty", "Open a Markdown workspace to see its note updates.")); return; }
    if (!state.jobs.length) sync.append(node("p", "annotation-empty", "No shared notes are enrolled in automatic annotation updates."));
    for (const row of state.jobs) {
      const item = node("article", "annotation-change"); item.dataset.syncId = row.id || "";
      item.append(node("h4", "", title(row)), node("p", "annotation-change-state", syncLabels[row.state] || "Note update pending"));
      if (row.reason) item.append(node("p", "annotation-change-reason", row.reason));
      if (row.preview_status && row.preview_status !== "current") item.append(node("p", "annotation-change-reason", row.preview_reason || (row.preview_status === "last_confirmed" ? "Showing the last confirmed image previews." : row.preview_status === "stale" ? "Image previews need verification against the current PDF." : "Image previews are pending.")));
      const actions = node("div", "annotation-actions");
      if (onOpenNote && row.note_path) actions.append(button("Open note", () => onOpenNote(row.note_path)));
      if (["blocked", "paused"].includes(row.state)) { const value = button("Review annotation section", () => reviewProjection(row)); value.dataset.action = "projection"; actions.append(value); }
      item.append(actions); sync.append(item);
    }
    renderBusy();
  }
  async function refresh() {
    if (state.destroyed || state.busy || state.refreshing) return;
    const generation = ++state.generation, selectedWorkspace = getWorkspace?.()?.id; state.refreshing = true; renderBusy();
    const results = await Promise.allSettled([
      api("zotero/annotation-recovery", { start: 0, limit: 32 }),
      selectedWorkspace ? api("zotero/annotation-sync-status", { workspace_id: selectedWorkspace }) : Promise.resolve({ jobs: [] }),
    ]);
    if (!current(generation)) return;
    state.refreshing = false;
    const [recoveryResult, syncResult] = results;
    if (recoveryResult.status === "fulfilled") {
      const value = recoveryResult.value;
      if (!Array.isArray(value?.records)) showError(new Error("Saved annotation changes returned an incomplete inventory."));
      else { state.records = value.records; state.next = value.next_start ?? null; state.server = value.server_id; renderRecords(); }
    } else showError(recoveryResult.reason);
    if (syncResult.status === "fulfilled" && selectedWorkspace === getWorkspace?.()?.id) { state.jobs = Array.isArray(syncResult.value?.jobs) ? syncResult.value.jobs : []; renderJobs(); }
    else if (syncResult.status === "rejected") showError(syncResult.reason);
    renderBusy();
  }
  async function loadMore() {
    if (state.destroyed || state.busy || state.refreshing || state.next == null) return;
    const generation = state.generation, start = state.next; state.refreshing = true; renderBusy();
    try {
      const result = await api("zotero/annotation-recovery", { start, limit: 32 });
      if (!current(generation)) return;
      if (!Array.isArray(result?.records) || result.server_id !== state.server) throw new Error("The Zotero connection changed. Refresh saved changes before continuing.");
      const rows = new Map(state.records.map((row) => [recoveryKey(row), row])); for (const row of result.records) rows.set(recoveryKey(row), row);
      state.records = [...rows.values()]; state.next = result.next_start ?? null; renderRecords();
    } finally { if (current(generation)) { state.refreshing = false; renderBusy(); } }
  }
  async function recover(row, action) {
    if (state.busy || state.destroyed) return;
    const operations = operationIds(row);
    if ((!row.receipt_id && (action !== "reconcile" || !operations)) || row.actions?.[{ reconcile: "check", retry: "retry", discard: "discard" }[action]] !== true) throw new Error("Refresh this change before choosing an action.");
    state.busy = true; renderBusy();
    try {
      const selected = row.receipt_id ? { receipt_ids: [row.receipt_id] } : { operation_ids: operations };
      const result = await api(`zotero/annotation-${action}`, { ...identity(row), ...selected, ...(action === "discard" ? { discard: true } : {}) });
      if (state.destroyed) return;
      if (action === "discard") { review.hidden = true; state.review = null; }
      await onUpdated?.(result, { ...identity(row), note_path: row.note_path });
      notify(result.resolved === false ? "Some changes still need review. Their local drafts remain retained." : action === "discard" ? "Kept Zotero's version. Your local draft remains in recovery history." : "Saved state checked. Note updates are tracked separately.");
    } finally { state.busy = false; renderBusy(); }
    await refresh();
  }
  function reviewDiscard(row) {
    state.review = { kind: "discard", row }; review.replaceChildren(node("h3", "", "Keep Zotero's version?"), node("p", "", "This stops retrying the selected local change. Its text remains in private recovery history; it does not delete the Zotero annotation."), draftView(row));
    if (row.observed?.length) { const observed = node("pre"); observed.textContent = JSON.stringify(row.observed, null, 2); review.append(node("h4", "", "Observed Zotero state"), observed); }
    review.append(button("Cancel", closeReview), button("Keep Zotero version and retain local draft", () => recover(row, "discard"))); review.hidden = false; review.focus();
  }
  function closeReview() { review.hidden = true; state.review = null; }
  async function projectionContext(row) {
    const selected = workspace();
    const value = getDocument ? await getDocument(row.note_path) : (await api("workspace/read", { workspace_id: selected.id, path: row.note_path })).document;
    if (selected.id !== getWorkspace?.()?.id) throw new Error("The workspace changed. Review this note again.");
    if (!value?.id || !value?.revision) throw new Error("The note could not be read for reconciliation.");
    if (value.dirty || value.recovery || value.saving) throw new Error("Save or discard the note draft before reviewing its annotation section.");
    return { workspace_id: selected.id, note_path: row.note_path, document_id: value.id, revision: value.revision, ...identity(row) };
  }
  async function reviewProjection(row) {
    if (state.busy || state.destroyed) return;
    const context = await projectionContext(row);
    const result = await api("zotero/annotation-projection-preview", context);
    if (state.destroyed || context.workspace_id !== getWorkspace?.()?.id) return;
    state.review = { kind: "projection", row, context, result };
    review.replaceChildren(node("h3", "", "Review the annotation section"), node("p", "", "Edits inside the automatic section are preserved until you choose how to reconcile them. This does not change annotations in Zotero."));
    for (const [label, text] of [["Current annotation section", result.existing_section], ["Proposed Zotero section", result.proposed_section]]) {
      const field = node("label", "annotation-preview-field", label), value = node("textarea"); value.readOnly = true; value.value = typeof text === "string" ? text : ""; value.rows = 7; value.setAttribute("aria-label", label); field.append(value); review.append(field);
    }
    const choices = new Set(result.choices || []), actions = node("div", "annotation-actions");
    if (choices.has("preserve")) actions.append(button("Keep edits as personal prose and rebuild", () => resolveProjection("preserve")));
    if (choices.has("replace")) actions.append(button("Rebuild and keep a recovery copy", () => resolveProjection("replace")));
    if (choices.has("pause")) actions.append(button("Pause note updates", () => resolveProjection("pause")));
    actions.append(button("Cancel", closeReview)); review.append(actions); review.hidden = false; review.focus();
  }
  async function resolveProjection(choice) {
    if (state.busy || state.destroyed || state.review?.kind !== "projection") return;
    const selected = state.review;
    if (selected.context.workspace_id !== getWorkspace?.()?.id) throw new Error("The workspace changed. Review this note again.");
    state.busy = true; renderBusy();
    try {
      const context = await projectionContext(selected.row);
      if (context.revision !== selected.context.revision) throw new Error("The note changed after this preview. Review its current annotation section again.");
      const result = await api("zotero/annotation-projection-resolve", { ...selected.context, preview_token: selected.result.preview_token, choice });
      await onUpdated?.(result, { ...identity(selected.row), note_path: selected.row.note_path });
      closeReview(); notify(choice === "pause" ? "Automatic annotation updates are paused for this note." : "Annotation section updated. Your previous text remains recoverable.");
    } finally { state.busy = false; renderBusy(); }
    await refresh();
  }
  function show() { if (state.destroyed) return; state.focus = doc.activeElement; state.visible = true; container.hidden = false; refresh(); }
  function hide() { state.visible = false; container.hidden = true; if (state.focus?.isConnected) state.focus.focus(); }
  const focus = () => { if (state.visible && !doc.hidden) refresh(); };
  doc.defaultView.addEventListener("focus", focus);
  const escape = (event) => { if (event.key === "Escape" && !state.busy) { event.preventDefault(); if (!review.hidden) closeReview(); else hide(); } };
  container.addEventListener("keydown", escape);
  renderRecords(); renderJobs();
  return {
    refresh, show, hide,
    hasPending: () => state.records.some((row) => !["saved", "discarded"].includes(row.status)) || state.jobs.some((row) => row.state !== "current"),
    hasBusy: () => state.busy,
    isVisible: () => state.visible,
    destroy() { state.destroyed = true; state.generation += 1; doc.defaultView.removeEventListener("focus", focus); container.removeEventListener("keydown", escape); container.replaceChildren(); container.hidden = true; },
  };
};
