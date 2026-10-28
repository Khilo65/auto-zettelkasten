"use strict";

// The library is another view of existing documents. It never owns a note buffer.
(() => {
  const dataOf = (row) => row?.data || row || {};
  const keyOf = (row) => row?.key || dataOf(row).key;
  const versionOf = (row) => row?.version ?? dataOf(row).version;
  const rowsOf = (page, field = "records") => Array.isArray(page) ? page : (page?.[field] || []);
  const itemName = (row) => dataOf(row).title || dataOf(row).name || "Untitled item";
  const randomToken = () => [...crypto.getRandomValues(new Uint8Array(16))].map((value) => value.toString(16).padStart(2, "0")).join("");
  const randomKey = () => {
    const alphabet = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ";
    let key = "";
    while (key.length < 8) {
      for (const value of crypto.getRandomValues(new Uint8Array(16))) {
        if (value < Math.floor(256 / alphabet.length) * alphabet.length) key += alphabet[value % alphabet.length];
        if (key.length === 8) break;
      }
    }
    return key;
  };

  window.createZoteroLibraryPanel = function ({ container, api, onOpenNote, onOpenPDF, onMigrate, onAuthorization, getWorkspace, message = () => {} }) {
    if (!container || typeof api !== "function") throw new Error("The Zotero panel needs a container and local API.");
    const doc = container.ownerDocument;
    const state = { visible: false, destroyed: false, generation: 0, scope: "users/0", collection: "", query: "", status: {}, libraries: [], collections: [], collectionPage: null, items: [], itemPage: null, selected: null, children: [], childrenPage: null, busy: false, refreshing: false, form: null, timer: null };
    container.classList.add("zotero-library-panel");
    container.setAttribute("aria-label", "Zotero library");

    function node(tag, className, text) {
      const value = doc.createElement(tag);
      if (className) value.className = className;
      if (text !== undefined) value.textContent = text;
      return value;
    }
    function button(label, action, className = "") {
      const value = node("button", className, label); value.type = "button";
      value.addEventListener("click", () => {
        try { Promise.resolve(action()).catch(showError); } catch (error) { showError(error); }
      });
      return value;
    }
    function showError(error) {
      if (state.destroyed) return;
      notice.textContent = error?.message || "The Zotero operation failed.";
      notice.classList.add("is-error");
      message(notice.textContent, true);
    }
    function notify(text) { notice.textContent = text; notice.classList.remove("is-error"); }
    function context() { return { scope: state.scope, workspace_id: getWorkspace?.()?.id }; }
    function isCurrent(generation) { return !state.destroyed && generation === state.generation; }
    function writable() { return state.status.writable === true && state.status.writes_available !== false && state.libraries.find((row) => row.scope === state.scope)?.writable !== false; }
    function canAct() { return !state.busy && !state.destroyed; }
    const header = node("div", "zotero-library-toolbar");
    const library = node("select", "zotero-library-select"); library.setAttribute("aria-label", "Zotero library");
    const refreshButton = button("Refresh", () => refresh());
    const authorizeButton = button("Enable editing", authorize);
    header.append(library, refreshButton, authorizeButton);
    const notice = node("p", "zotero-library-notice", "Open Zotero to browse its library."); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite");
    const search = node("form", "zotero-library-search");
    const query = node("input"); query.type = "search"; query.placeholder = "Search Zotero"; query.setAttribute("aria-label", "Search Zotero items"); query.maxLength = 500;
    const searchButton = node("button", "", "Search"); searchButton.type = "submit"; search.append(query, searchButton);
    const collectionHeading = node("div", "zotero-library-section-heading"); collectionHeading.append(node("span", "", "Collections"));
    const newCollection = button("New", () => collectionForm()); collectionHeading.append(newCollection);
    const collections = node("nav", "zotero-library-collections"); collections.setAttribute("aria-label", "Zotero collections");
    const collectionMore = button("Load more collections", loadMoreCollections); collectionMore.hidden = true;
    const itemsHeading = node("div", "zotero-library-section-heading");
    const items = node("div", "zotero-library-items"); items.setAttribute("role", "list"); items.setAttribute("aria-label", "Zotero items");
    const itemMore = button("Load more items", loadMoreItems); itemMore.hidden = true;
    const details = node("section", "zotero-library-details"); details.hidden = true;
    container.append(header, notice, search, collectionHeading, collections, collectionMore, itemsHeading, items, itemMore, details);

    function renderStatus() {
      const connected = state.status.connected ?? state.status.available;
      authorizeButton.hidden = state.status.writes_available === false || state.status.write_supported === false || writable();
      authorizeButton.disabled = !connected || state.busy;
      authorizeButton.title = "Request permission in Zotero. No library changes are made by browsing.";
      newCollection.hidden = !writable();
      refreshButton.disabled = state.refreshing || state.busy;
      library.disabled = state.busy; searchButton.disabled = state.busy;
    }
    function renderLibraries() {
      const previous = library.value;
      library.replaceChildren(...state.libraries.map((row) => { const option = node("option", "", row.name || row.scope); option.value = row.scope; return option; }));
      library.value = state.scope || previous;
    }
    function renderCollections() {
      const byKey = new Map(state.collections.map((row) => [keyOf(row), row]));
      const children = new Map();
      for (const row of state.collections) {
        const parent = dataOf(row).parentCollection;
        const owner = parent && byKey.has(parent) && parent !== keyOf(row) ? parent : "";
        if (!children.has(owner)) children.set(owner, []);
        children.get(owner).push(row);
      }
      const fragment = doc.createDocumentFragment(); const rendered = new Set();
      function add(label, key, depth) {
        const entry = button(label, () => selectCollection(key), "zotero-collection-row");
        entry.dataset.collectionKey = key; entry.dataset.depth = String(Math.min(depth, 8));
        entry.setAttribute("aria-current", state.collection === key ? "true" : "false"); entry.title = label;
        fragment.append(entry);
      }
      add("All items", "", 0);
      function visit(parent, depth) {
        for (const row of children.get(parent) || []) {
          const key = keyOf(row); if (rendered.has(key)) continue;
          rendered.add(key); add(itemName(row), key, depth); if (depth < 128) visit(key, depth + 1);
        }
      }
      visit("", 0);
      // An incomplete page or malformed parent chain never makes a known row vanish.
      for (const row of state.collections) if (!rendered.has(keyOf(row))) add(itemName(row), keyOf(row), 0);
      collections.replaceChildren(fragment);
      collectionMore.hidden = state.collectionPage?.next_start == null;
    }
    function renderItems() {
      const fragment = doc.createDocumentFragment();
      for (const row of state.items) {
        const entry = node("div", "zotero-item-row"); entry.setAttribute("role", "listitem");
        const title = button(itemName(row), () => selectItem(row), "zotero-item-title");
        title.dataset.itemKey = keyOf(row); title.title = itemName(row); title.setAttribute("aria-expanded", String(keyOf(state.selected) === keyOf(row)));
        entry.append(title);
        const kind = dataOf(row).itemType;
        const label = row.note_path ? "Atomic note available" : row.note_candidates?.length ? `${row.note_candidates.length} atomic notes · choose a file` : row.unbound_note_candidates?.length ? "Existing note needs source binding" : kind === "attachment" ? (dataOf(row).contentType || "Attachment") : kind === "note" ? "Zotero note · read-only" : "No atomic note yet";
        entry.append(node("span", "zotero-item-status", label)); fragment.append(entry);
      }
      if (!state.items.length) fragment.append(node("p", "sidebar-empty", state.query ? "No items match this search." : "No items in this view."));
      items.replaceChildren(fragment);
      const selectedCollection = state.collections.find((row) => keyOf(row) === state.collection);
      itemsHeading.replaceChildren(node("span", "", selectedCollection ? itemName(selectedCollection) : "All items"));
      const total = state.itemPage?.total;
      if (Number.isInteger(total)) itemsHeading.append(node("span", "zotero-count", `${state.items.length} of ${total}`));
      if (selectedCollection && writable()) itemsHeading.append(button("Rename", () => collectionForm(selectedCollection)));
      itemMore.hidden = state.itemPage?.next_start == null;
    }
    function mergeRows(current, added) {
      const result = new Map(current.map((row) => [keyOf(row), row]));
      for (const row of added) result.set(keyOf(row), row);
      return [...result.values()];
    }
    async function refresh() {
      if (!canAct() || state.refreshing || state.form) return;
      const generation = ++state.generation; state.refreshing = true; renderStatus();
      try {
        const status = await api("zotero/status", {});
        if (!isCurrent(generation)) return;
        state.status = status;
        if (!(status.connected ?? status.available)) throw new Error(status.error || status.reason || "Zotero is unavailable. Previously loaded items remain visible.");
        const response = await api("zotero/libraries", {});
        if (!isCurrent(generation)) return;
        const libraries = rowsOf(response, "libraries");
        if (!libraries.some((row) => row.scope === state.scope)) state.scope = libraries[0]?.scope || "users/0";
        const request = context();
        const [collectionPage, itemPage] = await Promise.all([
          api("zotero/collections", { scope: state.scope, start: 0, limit: 64 }),
          api("zotero/items", { ...request, collection_key: state.collection || null, query: state.query, start: 0, limit: 32 }),
        ]);
        if (!isCurrent(generation)) return;
        state.libraries = libraries;
        // Commit a complete pair of page responses; failed reads retain the old view.
        state.collections = rowsOf(collectionPage); state.collectionPage = collectionPage;
        state.items = rowsOf(itemPage); state.itemPage = itemPage;
        renderLibraries(); renderCollections(); renderItems();
        notify(writable() ? "Connected to Zotero" : "Connected · read-only");
        if (state.selected) {
          const row = state.items.find((value) => keyOf(value) === keyOf(state.selected));
          if (row) await selectItem(row, generation);
          else notify("The selected item is outside the loaded page. Its open note is unchanged.");
        }
      } catch (error) { if (isCurrent(generation)) showError(error); }
      finally { if (isCurrent(generation)) { state.refreshing = false; renderStatus(); } }
    }
    async function selectCollection(key) {
      if (!canAct() || state.form) return;
      state.collection = key; state.selected = null; state.children = []; details.hidden = true;
      state.refreshing = false; await refresh();
    }
    async function loadMoreCollections() {
      if (!canAct() || state.collectionPage?.next_start == null) return;
      const generation = state.generation; collectionMore.disabled = true;
      try {
        const page = await api("zotero/collections", { scope: state.scope, start: state.collectionPage.next_start, limit: 64 });
        if (!isCurrent(generation)) return;
        if (page.library_version !== state.collectionPage.library_version) throw new Error("Collections changed while loading. Refresh before loading more.");
        state.collections = mergeRows(state.collections, rowsOf(page)); state.collectionPage = page; renderCollections();
      } finally { collectionMore.disabled = false; }
    }
    async function loadMoreItems() {
      if (!canAct() || state.itemPage?.next_start == null) return;
      const generation = state.generation; itemMore.disabled = true;
      try {
        const page = await api("zotero/items", { ...context(), collection_key: state.collection || null, query: state.query, start: state.itemPage.next_start, limit: 32 });
        if (!isCurrent(generation)) return;
        if (page.library_version !== state.itemPage.library_version) throw new Error("Items changed while loading. Refresh before loading more.");
        state.items = mergeRows(state.items, rowsOf(page)); state.itemPage = page; renderItems();
      } finally { itemMore.disabled = false; }
    }
    async function selectItem(row, existingGeneration) {
      if (!canAct() || state.form) return;
      const generation = existingGeneration ?? ++state.generation;
      if (existingGeneration === undefined) state.refreshing = false;
      state.selected = row; state.children = []; state.childrenPage = null;
      renderItems(); renderDetails();
      if (["attachment", "note"].includes(dataOf(row).itemType)) return;
      const page = await api("zotero/children", { ...context(), parent_key: keyOf(row), start: 0, limit: 64 });
      if (!isCurrent(generation) || keyOf(state.selected) !== keyOf(row)) return;
      state.children = rowsOf(page); state.childrenPage = page; renderDetails();
    }
    async function moreChildren() {
      if (!canAct() || state.childrenPage?.next_start == null) return;
      const generation = state.generation; const key = keyOf(state.selected);
      const page = await api("zotero/children", { ...context(), parent_key: key, start: state.childrenPage.next_start, limit: 64 });
      if (!isCurrent(generation) || keyOf(state.selected) !== key) return;
      if (page.library_version !== state.childrenPage.library_version) throw new Error("Attachments changed while loading. Select the item again to refresh.");
      state.children = mergeRows(state.children, rowsOf(page)); state.childrenPage = page; renderDetails();
    }
    function noteButton(path) { return button("Open atomic note", () => onOpenNote?.(path), "zotero-open-note"); }
    function pdfButton(row) {
      const target = { scope: state.scope, attachment_key: keyOf(row), item_key: dataOf(row).parentItem || keyOf(state.selected), note_path: state.selected?.note_path || row.note_path };
      return button(`Open PDF: ${itemName(row)}`, () => onOpenPDF?.(target), "zotero-open-pdf");
    }
    function readOnlyNote(row) {
      const raw = dataOf(row).note || "";
      const parsed = doc.createElement("template");
      // Template contents are inert: external images/frames are never mounted.
      parsed.innerHTML = raw;
      for (const unsafe of parsed.content.querySelectorAll("script,style,iframe,object,embed")) unsafe.remove();
      for (const block of parsed.content.querySelectorAll("p,div,li,h1,h2,h3,h4,br")) block.append(doc.createTextNode("\n"));
      const view = node("details", "zotero-html-note");
      view.append(node("summary", "", itemName(row) === "Untitled item" ? "Zotero note · read-only" : itemName(row)), node("div", "", parsed.content.textContent?.trim() || "Empty Zotero note"));
      return view;
    }
    function renderDetails() {
      if (!state.selected || state.form) return;
      details.hidden = false; details.replaceChildren(node("h3", "", itemName(state.selected)));
      const row = state.selected, data = dataOf(row), scope = state.scope;
      if (row.note_path) details.append(noteButton(row.note_path));
      else if (row.note_candidates?.length) {
        details.append(node("p", "zotero-item-status", "Several atomic notes refer to this item. Choose the file to open."));
        for (const candidate of row.note_candidates) {
          if (typeof candidate.path === "string") details.append(button(`Open ${candidate.title || "atomic note"} (${candidate.path})`, () => onOpenNote?.(candidate.path), "zotero-open-note"));
        }
      } else if (!row.unbound_note_candidates?.length && !["attachment", "note"].includes(data.itemType)) details.append(node("p", "zotero-item-status", "No atomic note yet. This view does not generate notes."));
      if (row.unbound_note_candidates?.length) {
        details.append(node("p", "zotero-item-status", "Existing note needs source binding. Its historical metadata does not identify a library. Open the note, or explicitly check its source in this library."));
        for (const candidate of row.unbound_note_candidates) {
          if (typeof candidate.path !== "string") continue;
          const label = `${candidate.title || "atomic note"} (${candidate.path})`;
          details.append(button(`Open ${label}`, () => onOpenNote?.(candidate.path), "zotero-open-note"));
          if (typeof onOpenPDF === "function" && /^[A-Z0-9]{8}$/.test(candidate.item_key || "")) {
            details.append(button(`Check source in this library: ${label}`, () => onOpenPDF({ scope, item_key: candidate.item_key, note_path: candidate.path, confirm_scope: true }), "zotero-confirm-source"));
          }
        }
      }
      if (data.itemType === "attachment" && data.contentType === "application/pdf") details.append(pdfButton(row));
      if (data.itemType === "note") details.append(readOnlyNote(row));
      const seenNotes = new Set(row.note_path ? [row.note_path] : []);
      for (const child of state.children) {
        if (child.note_path) {
          if (!seenNotes.has(child.note_path)) { seenNotes.add(child.note_path); details.append(noteButton(child.note_path)); }
        }
        if (dataOf(child).itemType === "attachment" && dataOf(child).contentType === "application/pdf") details.append(pdfButton(child));
        else if (dataOf(child).itemType === "note") details.append(readOnlyNote(child));
        else if (dataOf(child).itemType === "attachment") details.append(node("p", "zotero-item-status", `${itemName(child)} · Open other attachments in Zotero`));
      }
      if (state.childrenPage?.next_start != null) details.append(button("Load more attachments", moreChildren));
      if (writable() && !["attachment", "note"].includes(data.itemType)) details.append(button("Edit item and collections", itemForm));
      if (row.note_path && row.note_status !== "linked" && writable() && state.scope === "users/0" && onMigrate) details.append(button("Link this atomic note…", () => onMigrate({ scope, item_key: keyOf(row), note_path: row.note_path })));
    }
    function inputField(form, labelText, name, value, { multiline = false } = {}) {
      const label = node("label", "zotero-field", labelText); const input = node(multiline ? "textarea" : "input");
      input.name = name; input.value = value || ""; input.maxLength = name === "abstractNote" ? 10000 : 1000;
      label.append(input); form.append(label); return input;
    }
    function beginForm(title) {
      if (!writable() || !canAct() || state.form) return null;
      const form = node("form", "zotero-library-form"); state.form = form;
      form.append(node("h3", "", title)); details.hidden = false; details.replaceChildren(form);
      const error = node("p", "zotero-form-error"); error.setAttribute("role", "alert");
      return { form, error, finish(save) {
        form.append(error); const actions = node("div", "zotero-form-actions");
        const submit = node("button", "", "Save"); submit.type = "submit";
        const cancel = button("Cancel", () => { if (state.busy) return; state.form = null; if (state.selected) renderDetails(); else details.hidden = true; }); actions.append(submit, cancel); form.append(actions);
        form.addEventListener("submit", async (event) => {
          event.preventDefault(); if (state.busy || submit.disabled || !writable()) return;
          state.busy = true; error.textContent = ""; submit.disabled = true; cancel.disabled = true; renderStatus();
          try { await save(); if (state.destroyed) return; state.form = null; state.busy = false; await refresh(); if (state.selected) renderDetails(); else details.hidden = true; }
          catch (failure) {
            if (state.destroyed) return;
            error.textContent = failure.message || "The change could not be saved.";
            const uncertain = ["write_uncertain", "version_conflict", "precondition_required"].includes(failure.code);
            if (uncertain) error.textContent += " Close this form and refresh before taking any further action. No automatic retry was made.";
            submit.disabled = uncertain; state.busy = false; cancel.disabled = false; renderStatus();
          }
        });
        form.querySelector("input,textarea")?.focus();
      } };
    }
    function itemForm() {
      const row = state.selected; if (!row) return;
      const values = dataOf(row), view = beginForm("Edit Zotero item"); if (!view) return;
      const { form } = view; const scope = state.scope; const fields = new Map();
      for (const [field, label] of [["title", "Title"], ["date", "Date"], ["publicationTitle", "Publication"], ["DOI", "DOI"], ["abstractNote", "Abstract"]]) {
        if (field === "title" || Object.hasOwn(values, field)) fields.set(field, inputField(form, label, field, values[field], { multiline: field === "abstractNote" }));
      }
      const tags = inputField(form, "Tags (one per line)", "tags", (values.tags || []).map((tag) => tag.tag).join("\n"), { multiline: true });
      const memberships = new Set(values.collections || []); const changes = new Map();
      const group = node("fieldset"); group.append(node("legend", "", "Collection membership"), node("p", "zotero-item-status", "Removing membership keeps the item in your library."));
      for (const collection of state.collections) {
        const label = node("label", "zotero-membership"); const control = node("input"); control.type = "checkbox"; control.checked = memberships.has(keyOf(collection)); control.value = keyOf(collection);
        control.addEventListener("change", () => changes.set(control.value, control.checked)); label.append(control, doc.createTextNode(itemName(collection))); group.append(label);
      }
      if (state.collectionPage?.next_start != null) group.append(node("p", "zotero-item-status", "Only loaded collections are listed. Other memberships are preserved."));
      form.append(group);
      const writeToken = randomToken();
      view.finish(async () => {
        const patch = {}; for (const [field, control] of fields) if (control.value !== (values[field] || "")) patch[field] = control.value;
        const tagNames = [...new Set(tags.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))];
        if (tagNames.join("\n") !== (values.tags || []).map((tag) => tag.tag).join("\n")) patch.tags = tagNames.map((tag) => (values.tags || []).find((old) => old.tag === tag) || { tag });
        if (changes.size) { for (const [key, checked] of changes) { if (checked) memberships.add(key); else memberships.delete(key); } patch.collections = [...memberships]; }
        if (Object.keys(patch).length) await api("zotero/item-update", { scope, key: keyOf(row), changes: patch, expected_version: versionOf(row), write_token: writeToken });
      });
    }
    function collectionForm(row = null) {
      const view = beginForm(row ? "Rename collection" : "New collection"); if (!view) return;
      const name = inputField(view.form, "Name", "name", row ? itemName(row) : ""); name.required = true;
      const scope = state.scope, parentKey = state.collection || null, writeToken = randomToken(), key = row ? keyOf(row) : randomKey();
      if (!row && parentKey) view.form.append(node("p", "zotero-item-status", "The new collection will be inside the selected collection."));
      view.finish(async () => {
        const common = { scope, key, name: name.value.trim(), write_token: writeToken };
        if (!common.name) throw new Error("Enter a collection name.");
        await api(row ? "zotero/collection-rename" : "zotero/collection-create", { ...common, ...(row ? { expected_version: versionOf(row) } : { parent_key: parentKey }) });
      });
    }
    async function authorize() {
      if (!canAct() || state.status.writes_available === false) return;
      state.busy = true; renderStatus(); notify("Review the authorization request in Zotero.");
      try { await api("zotero/authorize", {}); }
      finally { state.busy = false; renderStatus(); }
      await refresh();
      await onAuthorization?.();
    }
    library.addEventListener("change", () => {
      if (!canAct() || state.form) { library.value = state.scope; return; }
      state.scope = library.value; state.collection = ""; state.selected = null; details.hidden = true; state.refreshing = false; refresh();
    });
    search.addEventListener("submit", (event) => {
      event.preventDefault(); if (!canAct() || state.form) return;
      state.query = query.value.trim(); state.selected = null; details.hidden = true; state.refreshing = false; refresh();
    });
    const focus = () => { if (state.visible && !doc.hidden) refresh(); };
    window.addEventListener("focus", focus);
    function setVisible(visible) {
      state.visible = Boolean(visible); container.hidden = !state.visible;
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
      if (state.visible) { refresh(); state.timer = setInterval(focus, 30000); }
    }
    function destroy() {
      state.destroyed = true; state.generation += 1; if (state.timer) clearInterval(state.timer);
      window.removeEventListener("focus", focus); container.replaceChildren();
    }
    renderStatus();
    return { refresh, destroy, setVisible, isBusy: () => state.busy, hasDraft: () => Boolean(state.form) };
  };
})();
