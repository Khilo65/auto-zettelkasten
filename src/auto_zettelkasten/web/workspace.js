"use strict";

(() => {
  const kit = window.WorkspaceKit;
  const el = (id) => document.getElementById(id);
  const screen = el("workspace-screen");
  const state = {
    workspace: null, epoch: 0, openRequest: 0, opening: false, creating: false,
    documents: new Map(), active: null, expanded: new Set(), treeFocus: "",
    descending: false, history: [], historyIndex: -1, view: null,
    quickResults: [], quickIndex: 0, quickFocus: null, quickOutside: false, createKind: "file", createParent: "",
    polling: false, confirmation: null, folderDialogGeneration: 0, sidebarHidden: window.innerWidth < 700, recoveryError: "",
    snapshot: null, linkIndex: null, indexing: false, indexRequest: 0, indexTimer: null, indexError: "", indexProgress: "", indexParser: null,
    previewDocuments: new Map(), previewRequests: new Map(),
    contextOpen: false, contextMode: "backlinks", graphTab: false, graphActive: false, localPinned: null,
    graphs: new Map(), graphViews: new WeakMap(), graphLayouts: new Map(), graphSizes: new WeakMap(), graphLabels: new WeakMap(), graphSignatures: new Map(), graphLists: new Set(),
    organization: null, organizationMenu: null, organizationRequest: 0, requiresReopen: false,
  };

  function icon(name) {
    const svg = kit?.icons?.[name]?.();
    if (svg) { svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false"); }
    return svg || document.createTextNode("");
  }

  function fillIcons(root = document) {
    for (const node of root.querySelectorAll("[data-icon]")) node.replaceChildren(icon(node.dataset.icon));
  }

  function message(text = "", error = false) {
    el("workspace-message").textContent = text;
    el("workspace-message").hidden = !text;
    el("workspace-message").classList.toggle("is-error", error);
  }

  const active = () => state.documents.get(state.active);
  const textOf = (doc) => doc.editorState.doc.toString();
  const canEdit = (doc) => Boolean(doc && (doc.editable || doc.override_available));
  const dirty = (doc) => Boolean(doc && (doc.external || textOf(doc) !== doc.savedText));
  const parentPath = (path) => path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const basename = (path) => path.slice(path.lastIndexOf("/") + 1);
  const withoutExtension = (name) => name.replace(/\.md$/i, "");
  const normalizeText = (text) => text.replace(/\r\n/g, "\n");
  const recoveryKey = (doc) => `az.workspace.draft.${doc.recovery_key}`;
  const workspaceVisible = () => !screen.hidden;
  const hasBusyWrite = () => state.opening || state.creating || Boolean(state.organization) || [...state.documents.values()].some((doc) => doc.saving);

  window.workspaceHasUnsavedChanges = () => hasBusyWrite() || [...state.documents.values()].some((doc) => dirty(doc) || doc.recovery);

  function storageFailure(error) {
    state.recoveryError = "Local recovery is unavailable. Save your notes before closing this app.";
    message(state.recoveryError, true);
  }

  function persistDraft(doc) {
    try {
      if (!dirty(doc)) localStorage.removeItem(recoveryKey(doc));
      else localStorage.setItem(recoveryKey(doc), JSON.stringify({ text: textOf(doc), revision: doc.revision, updatedAt: Date.now() }));
    } catch (error) { storageFailure(error); }
  }

  function forgetDraft(doc) {
    try { localStorage.removeItem(recoveryKey(doc)); }
    catch (error) { storageFailure(error); }
  }

  function confirmAction(title, text, action) {
    if (state.confirmation) return Promise.resolve(false);
    const dialog = el("workspace-confirm-dialog"), previousFocus = document.activeElement;
    el("workspace-confirm-title").textContent = title;
    el("workspace-confirm-message").textContent = text;
    el("workspace-confirm-accept").textContent = action;
    return new Promise((resolve) => {
      state.confirmation = { resolve, previousFocus };
      dialog.showModal();
      el("workspace-confirm-cancel").focus();
    });
  }

  function finishConfirmation(accepted) {
    const confirmation = state.confirmation;
    if (!confirmation) return;
    state.confirmation = null;
    el("workspace-confirm-dialog").close();
    if (confirmation.previousFocus?.isConnected) confirmation.previousFocus.focus();
    confirmation.resolve(accepted);
  }

  async function confirmLeaving() {
    if (hasBusyWrite()) { message("Wait for the current file operation to finish."); return false; }
    if (!window.workspaceHasUnsavedChanges()) return true;
    const epoch = state.epoch;
    const accepted = await confirmAction("Open another folder?", "This workspace has unsaved notes or recovery drafts. Recovery is available on reload in this browser session. Save first to keep changes across app restarts.", "Open another folder");
    return accepted && epoch === state.epoch && !hasBusyWrite();
  }

  function setSidebar(hidden) {
    state.sidebarHidden = hidden;
    screen.classList.toggle("sidebar-hidden", hidden);
    el("workspace-sidebar-open").hidden = !hidden;
    if (hidden && el("workspace-sidebar").contains(document.activeElement)) el("workspace-sidebar-open").focus();
    resizeGraphs();
  }

  function showWorkspace() {
    if (state.organization) return;
    el("mapping-screen").hidden = true;
    screen.hidden = false;
    document.body.classList.add("workspace-active");
    el("show-workspace").classList.add("selected");
    el("show-workspace").setAttribute("aria-pressed", "true");
    el("show-mapping").classList.remove("selected");
    el("show-mapping").setAttribute("aria-pressed", "false");
    state.view?.requestMeasure();
    document.querySelector(".skip-link").href = "#workspace-content";
    document.querySelector(".skip-link").textContent = "Skip to document";
  }

  function showMapping() {
    if (state.opening || state.organization) return;
    screen.hidden = true;
    el("mapping-screen").hidden = false;
    document.body.classList.remove("workspace-active");
    el("show-workspace").classList.remove("selected");
    el("show-workspace").setAttribute("aria-pressed", "false");
    el("show-mapping").classList.add("selected");
    el("show-mapping").setAttribute("aria-pressed", "true");
    el("workspace-graph-open").classList.remove("selected");
    el("workspace-graph-open").setAttribute("aria-pressed", "false");
    window.initializeMapping?.();
    document.querySelector(".skip-link").href = "#main";
    document.querySelector(".skip-link").textContent = "Skip to mapper";
    el("mapper-title").focus();
  }

  function showFolderDialog() {
    if (hasBusyWrite()) return message("Wait for the current file operation to finish.");
    state.folderDialogGeneration += 1;
    el("workspace-folder-error").textContent = "";
    el("workspace-folder-path").value = state.workspace?.path || "";
    el("workspace-folder-dialog").showModal();
    el("workspace-folder-path").focus();
  }

  async function openWorkspace(event, requestedPath) {
    event?.preventDefault();
    if (!(await confirmLeaving())) return false;
    const path = requestedPath ?? el("workspace-folder-path").value.trim();
    if (!path) return false;
    if (requestedPath !== undefined) {
      el("workspace-folder-path").value = path;
      if (!el("workspace-folder-dialog").open) el("workspace-folder-dialog").showModal();
    }
    state.opening = true;
    el("workspace-folder-submit").disabled = true;
    el("workspace-folder-browse").disabled = true;
    el("workspace-folder-error").textContent = "Opening folder…";
    screen.inert = true;
    try {
      const result = await api("workspace/open", { path });
      for (const doc of state.documents.values()) if (!doc.recovery) persistDraft(doc);
      state.epoch += 1; state.requiresReopen = false;
      state.openRequest += 1;
      state.workspace = result.workspace;
      state.documents.clear(); state.active = null;
      state.snapshot = null; state.linkIndex = null; state.indexError = ""; state.indexRequest += 1; state.indexing = false;
      state.indexParser?.close(); state.indexParser = null; state.previewDocuments.clear(); state.previewRequests.clear();
      state.graphTab = false; state.graphActive = false; state.localPinned = null;
      for (const graph of state.graphs.values()) graph.destroy();
      state.graphs.clear(); state.graphLayouts.clear(); state.graphSignatures.clear();
      state.history = []; state.historyIndex = -1;
      state.expanded.clear(); state.treeFocus = "";
      state.view?.destroy(); state.view = null;
      el("workspace-editor").replaceChildren();
      el("workspace-folder-dialog").close();
      message(state.workspace.reason || "");
      applyWorkspace(state.workspace);
      renderActive(); showWorkspace();
      if (window.innerWidth >= 700) setSidebar(false);
      return true;
    } catch (error) { el("workspace-folder-error").textContent = error.message; return false; }
    finally {
      state.opening = false; screen.inert = false;
      el("workspace-folder-submit").disabled = false;
      el("workspace-folder-browse").disabled = false;
      if (state.workspace) refreshIndex();
    }
  }

  window.openMemoryWorkspace = async (path, preferredPath) => {
    if (typeof path !== "string" || !path.trim()) return false;
    if (!(await openWorkspace(null, path))) return false;
    if (preferredPath && state.workspace.entries.some((entry) => entry.path === preferredPath && entry.kind === "file")) {
      await openDocument(preferredPath);
      if (active()?.path === preferredPath) setMode("reading");
    }
    return true;
  };

  function applyWorkspace(workspace) {
    state.workspace = workspace;
    el("workspace-root-name").textContent = workspace.name;
    el("workspace-root").title = `${workspace.path}\nOpen another folder`;
    for (const id of ["workspace-sort", "workspace-collapse"]) el(id).disabled = false;
    const writable = workspace.editable !== false;
    el("workspace-new-note").disabled = !writable;
    el("workspace-new-folder").disabled = !writable;
    el("workspace-empty-create").hidden = !writable;
    el("workspace-tree-note").hidden = !workspace.truncated;
    el("workspace-tree-note").textContent = "This folder has more entries than can be shown. Open a smaller folder.";
    renderTree(); renderOrganizationControl();
  }

  function visibleEntries() {
    const byParent = new Map();
    for (const entry of state.workspace?.entries || []) {
      const parent = parentPath(entry.path);
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent).push(entry);
    }
    const result = [];
    const visit = (parent, depth) => {
      const siblings = (byParent.get(parent) || []).sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
        return a.name.localeCompare(b.name, undefined, { numeric: true }) * (state.descending ? -1 : 1);
      });
      siblings.forEach((entry, index) => {
        result.push({ ...entry, depth, position: index + 1, siblings: siblings.length });
        if (entry.kind === "directory" && state.expanded.has(entry.path)) visit(entry.path, depth + 1);
      });
    };
    visit("", 1);
    return result;
  }

  function focusTree(path) {
    state.treeFocus = path;
    renderTree(true);
  }

  function renderTree(restoreFocus = false) {
    const tree = el("workspace-tree");
    if (!state.workspace) return;
    const rows = visibleEntries();
    if (!rows.length) {
      const p = document.createElement("p"); p.className = "sidebar-empty";
      p.textContent = "No Markdown notes yet."; tree.replaceChildren(p); return;
    }
    if (!rows.some((row) => row.path === state.treeFocus)) state.treeFocus = rows.find((row) => row.path === state.active)?.path || rows[0].path;
    const fragment = document.createDocumentFragment();
    for (const entry of rows) {
      const row = document.createElement("button"); row.type = "button"; row.className = "tree-row";
      row.setAttribute("role", "treeitem"); row.setAttribute("aria-level", entry.depth);
      row.setAttribute("aria-setsize", entry.siblings); row.setAttribute("aria-posinset", entry.position);
      row.setAttribute("aria-selected", String(entry.path === state.active));
      row.tabIndex = entry.path === state.treeFocus ? 0 : -1;
      row.dataset.path = entry.path; row.style.setProperty("--depth", entry.depth - 1);
      row.title = entry.path + (entry.editable === false && entry.reason ? `\n${entry.reason}` : "");
      const disclosure = document.createElement("span"); disclosure.className = "tree-disclosure";
      if (entry.kind === "directory") {
        const expanded = state.expanded.has(entry.path);
        row.setAttribute("aria-expanded", String(expanded));
        disclosure.append(icon(expanded ? "ChevronDown" : "ChevronRight"));
      }
      const fileIcon = icon(entry.kind === "directory" ? (state.expanded.has(entry.path) ? "FolderOpen" : "Folder") : "FileText");
      const name = document.createElement("span"); name.className = "tree-label";
      name.textContent = entry.kind === "file" ? withoutExtension(entry.name) : entry.name;
      row.append(disclosure, fileIcon, name);
      row.addEventListener("focus", () => { state.treeFocus = entry.path; renderOrganizationControl(); });
      row.addEventListener("click", () => {
        state.treeFocus = entry.path;
        if (entry.kind === "directory") {
          if (state.expanded.has(entry.path)) state.expanded.delete(entry.path); else state.expanded.add(entry.path);
          renderTree(true);
        } else openDocument(entry.path);
      });
      row.addEventListener("contextmenu", (event) => { event.preventDefault(); state.treeFocus = entry.path; row.focus(); showFileActions(entry); });
      row.addEventListener("keydown", (event) => {
        if (event.key === "F2") { event.preventDefault(); beginOrganization("rename", entry); }
        else if (event.key === "F10" && event.shiftKey) { event.preventDefault(); showFileActions(entry); }
        else treeKey(event, entry, rows);
      });
      fragment.append(row);
    }
    tree.replaceChildren(fragment);
    renderOrganizationControl();
    if (restoreFocus) [...tree.querySelectorAll("[role=treeitem]")].find((row) => row.dataset.path === state.treeFocus)?.focus();
  }

  function treeKey(event, entry, rows) {
    const index = rows.findIndex((row) => row.path === entry.path);
    let next = null;
    if (event.key === "ArrowDown") next = rows[Math.min(index + 1, rows.length - 1)]?.path;
    else if (event.key === "ArrowUp") next = rows[Math.max(index - 1, 0)]?.path;
    else if (event.key === "Home") next = rows[0]?.path;
    else if (event.key === "End") next = rows[rows.length - 1]?.path;
    else if (event.key === "ArrowRight" && entry.kind === "directory") {
      if (!state.expanded.has(entry.path)) { state.expanded.add(entry.path); renderTree(true); }
      else if (rows[index + 1]?.depth > entry.depth) next = rows[index + 1].path;
    } else if (event.key === "ArrowLeft") {
      if (entry.kind === "directory" && state.expanded.has(entry.path)) { state.expanded.delete(entry.path); renderTree(true); }
      else next = parentPath(entry.path);
    } else return;
    event.preventDefault();
    if (next) focusTree(next);
  }

  function reveal(path, takeFocus = false) {
    let parent = parentPath(path);
    while (parent) { state.expanded.add(parent); parent = parentPath(parent); }
    state.treeFocus = path; renderTree(takeFocus);
    [...el("workspace-tree").querySelectorAll("[role=treeitem]")].find((row) => row.dataset.path === path)?.scrollIntoView({ block: "nearest" });
  }

  class BulletWidget extends (kit?.WidgetType || class {}) {
    eq() { return true; }
    toDOM() {
      const span = document.createElement("span");
      span.className = "live-list-marker"; span.textContent = "•";
      span.setAttribute("aria-hidden", "true");
      return span;
    }
  }

  function rememberDocument(value) {
    if (!state.snapshot) state.snapshot = { documents: [], relations: [], warnings: [] };
    const previous = state.snapshot.documents.find((doc) => doc.path === value.path);
    state.snapshot.documents = state.snapshot.documents.filter((doc) => doc.path !== value.path);
    const record = { ...previous, ...value, text: normalizeText(value.text) };
    if (previous && previous.revision !== value.revision) state.snapshot.document_identities = (state.snapshot.document_identities || []).filter((identity) => ![identity.canonical_path, ...identity.path_aliases].includes(value.path));
    state.snapshot.documents.push(state.snapshot.library ? kit.parseLibraryDocument(record) : record);
    rebuildLinks();
  }

  function scheduleLinks() {
    window.clearTimeout(state.indexTimer);
    state.indexTimer = window.setTimeout(rebuildLinks, 180);
  }

  function rebuildLinks(force = false) {
    if ((state.organization && !force) || !state.workspace || !kit.buildLinkIndex) return;
    const documents = new Map((state.snapshot?.documents || []).map((doc) => [doc.path, doc]));
    const verifiedIdentities = (state.snapshot?.document_identities || []).filter((identity) => [identity.canonical_path, ...identity.path_aliases].every((path) => {
      const doc = state.documents.get(path);
      return !doc || !dirty(doc) && doc.revision === documents.get(path)?.revision;
    }));
    for (const [path, value] of state.previewDocuments) if (documents.has(path)) documents.set(path, { ...documents.get(path), ...value });
    for (const doc of state.documents.values()) documents.set(doc.path, { ...documents.get(doc.path), path: doc.path, text: textOf(doc), revision: doc.revision, editable: doc.editable });
    const ordered = [...documents.values()].sort((a, b) => Number(b.path === state.active) - Number(a.path === state.active) || a.path.localeCompare(b.path));
    const semantic = state.snapshot?.semantic_map;
    const semanticDirty = state.previewDocuments.size > 0 || [...state.documents.values()].some((doc) => dirty(doc) || doc.external || doc.recovery);
    const derivedRelations = semanticDirty ? (state.snapshot?.relations || []).filter((relation) => !relation.derived) : state.snapshot?.relations || [];
    state.linkIndex = (state.snapshot?.library ? kit.buildLibraryIndex : kit.buildLinkIndex)(ordered, derivedRelations, state.linkIndex, { document_identities: verifiedIdentities, semantic_map: semanticDirty ? undefined : semantic });
    state.indexVersion = (state.indexVersion || 0) + 1;
    state.view?.dispatch({});
    if (active()?.mode === "reading") renderReading(active());
    hydrateEmbeds();
    if (el("workspace-quick-dialog").open) renderQuickResults();
    renderContext();
    if (state.graphActive) renderGraph("global");
  }

  async function refreshIndex() {
    if (!state.workspace || state.opening || state.indexing || state.organization || state.organizationMenu || state.requiresReopen) return;
    const epoch = state.epoch, request = ++state.indexRequest, workspaceId = state.workspace.id;
    const current = () => epoch === state.epoch && request === state.indexRequest && !state.organization;
    const before = new Map([...state.documents.values()].map((doc) => [doc.path, { doc, revision: doc.revision }]));
    state.indexing = true; state.indexProgress = "Checking note index…"; renderIndexStatus();
    let parser;
    try {
      let snapshot;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const documents = new Map(), entries = new Map(), relations = new Map(), warnings = new Set();
        let cursor, generation, truncated = false, hasEntries = false, metadata = {};
        try {
          for (let pageNumber = 0; ; pageNumber += 1) {
            if (pageNumber > 100000) throw new Error("The index returned too many pages.");
            const page = await api("workspace/index", { workspace_id: workspaceId, paged: true, ...(cursor ? { cursor } : {}), ...(!cursor && !attempt && state.snapshot?.generation ? { known_generation: state.snapshot.generation } : {}) });
            if (!current()) return;
            if (generation && page.generation !== generation) throw new Error("The index generation changed between pages.");
            generation = page.generation;
            if (page.not_modified && page.complete) { state.indexError = ""; return; }
            if (!Array.isArray(page.documents) || !Array.isArray(page.relations)) throw new Error("Invalid note index page.");
            if (page.documents.length) {
              parser ||= new kit.LibraryParser(); state.indexParser = parser;
              // Legacy one-page fixtures use the same bounded parser batches.
              for (let offset = 0; offset < page.documents.length; offset += 32) {
                const parsed = await parser.parse(page.documents.slice(offset, offset + 32));
                if (!current()) return;
                for (const value of parsed) documents.set(value.path, value);
              }
            }
            if (Array.isArray(page.entries)) { hasEntries = true; for (const entry of page.entries) entries.set(entry.path, entry); }
            for (const relation of page.relations) relations.set(relation.id, relation);
            for (const warning of page.warnings || []) warnings.add(warning);
            truncated ||= Boolean(page.truncated);
            if (page.complete !== false) metadata = { document_identities: (Array.isArray(page.document_identities) ? page.document_identities : []).filter((identity) => identity?.verified === true && typeof identity.canonical_path === "string" && Array.isArray(identity.path_aliases) && identity.path_aliases.every((path) => typeof path === "string")), index_coverage: page.index_coverage, relation_resolution: page.relation_resolution, semantic_map: page.semantic_map };
            if (documents.size > 100000 || relations.size > 1000000 || entries.size > 200000) throw new Error("The library exceeds the supported index size.");
            state.indexProgress = `${page.phase === "validate" ? "Verifying" : "Indexing"} ${page.progress?.documents ?? documents.size} notes…`;
            renderIndexStatus();
            if (page.complete !== false) break;
            if (typeof page.next_cursor !== "string" || !page.next_cursor || page.next_cursor === cursor) throw new Error("Invalid note index continuation.");
            cursor = page.next_cursor;
          }
          snapshot = { documents: [...documents.values()], relations: [...relations.values()], warnings: [...warnings], truncated, generation, library: true, complete: true, ...metadata, ...(hasEntries ? { entries: [...entries.values()] } : {}) };
          break;
        } catch (error) {
          if (error.code !== "index_changed" || attempt || !current()) throw error;
          parser?.close(); parser = null; state.indexParser = null;
          state.indexProgress = "Notes changed during indexing. Checking again…"; renderIndexStatus();
        }
      }
      if (!current()) return;
      for (const value of snapshot.documents) {
        const saved = before.get(value.path), doc = state.documents.get(value.path);
        if (!doc || saved?.doc !== doc || saved.revision !== doc.revision || doc.saving || doc.recovery) continue;
        applyDocumentPolicy(doc, { ...doc, editable: value.editable });
        if (value.revision !== doc.revision) {
          const { document: latest } = await api("workspace/read", { workspace_id: workspaceId, path: doc.path });
          if (!current()) return;
          if (state.documents.get(value.path) !== doc || saved.revision !== doc.revision || doc.saving || doc.recovery) continue;
          applyDocumentPolicy(doc, latest);
          if (dirty(doc)) doc.external = latest;
          else { doc.savedText = normalizeText(latest.text); doc.revision = latest.revision; replaceDocumentText(doc, latest.text); }
        }
      }
      // Advance the generation only after open-note reconciliation also succeeds.
      state.snapshot = snapshot; state.indexError = "";
      if (snapshot.entries) applyWorkspace({ ...state.workspace, entries: snapshot.entries, truncated: snapshot.truncated });
      const revisions = new Map(snapshot.documents.map((value) => [value.path, value.revision]));
      for (const [path, value] of state.previewDocuments) if (revisions.get(path) !== value.revision) state.previewDocuments.delete(path);
      state.previewRequests.clear();
      rebuildLinks();
      if (active() && !state.graphActive) renderDocumentStatus();
    } catch (error) {
      if (epoch === state.epoch && request === state.indexRequest) state.indexError = `Connections could not refresh. ${error.message}`;
    } finally {
      parser?.close();
      if (state.indexParser === parser) state.indexParser = null;
      if (request === state.indexRequest) { state.indexing = false; state.indexProgress = ""; renderIndexStatus(); }
    }
  }

  function renderIndexStatus() {
    const warnings = [...(state.snapshot?.warnings || []), ...(state.linkIndex?.warnings || []).map((warning) => typeof warning === "string" ? warning : warning.reason)];
    if (state.snapshot?.index_coverage?.label) warnings.push(state.snapshot.index_coverage.label);
    if (state.linkIndex?.partial || state.snapshot?.truncated) warnings.unshift("Connections are partial. Counts and graphs include indexed content only.");
    const showProgress = state.indexing && !state.snapshot?.complete;
    const text = state.indexError || (showProgress ? state.indexProgress || "Indexing notes…" : warnings.length ? [...new Set(warnings)].join(" ") : "");
    for (const node of document.querySelectorAll(".index-status")) {
      node.hidden = !text;
      if (node.dataset.message === text) continue;
      node.dataset.message = text; node.replaceChildren();
      if (warnings.length && !state.indexError && !showProgress) {
        const details = document.createElement("details"), summary = document.createElement("summary"), description = document.createElement("p");
        summary.textContent = state.linkIndex?.partial || state.snapshot?.truncated ? "Connections are partial" : "Connection details";
        description.textContent = text; details.append(summary, description); node.append(details);
      } else node.textContent = text;
    }
  }

  async function navigateLink(destination, kind = "wiki", sourcePath = active()?.path) {
    if (state.organization) return;
    rebuildLinks();
    const result = kit.resolveLink(state.linkIndex, sourcePath, destination, kind);
    if (result.status !== "resolved") {
      const reasons = { missing: "This note was not found in the available workspace index. Try the file panel or quick-open.", ambiguous: "This link matches more than one note. Use a folder path.", invalid: "This destination is outside the allowed Markdown workspace.", "missing-fragment": "The note exists, but that heading or block was not found." };
      return message(reasons[result.status] || "This link cannot be opened.", true);
    }
    await openDocument(result.path, { refresh: true });
    if (state.active !== result.path) return;
    const current = kit.resolveLink(state.linkIndex, sourcePath, destination, kind);
    if (current.status !== "resolved") return message("The destination changed. Review this link before following it.", true);
    jumpTo(current.from || 0);
  }

  function jumpTo(from, to = from) {
    const doc = active(); if (!doc) return;
    from = Math.max(0, Math.min(textOf(doc).length, from)); to = Math.max(from, Math.min(textOf(doc).length, to));
    if (doc.mode === "reading") {
      const blocks = [...el("workspace-reader").querySelectorAll("[data-source-from]")].filter((node) => node.dataset.sourcePath === doc.path && !node.closest(".note-embed"));
      const target = blocks.filter((node) => Number(node.dataset.sourceFrom) <= from).at(-1) || blocks[0];
      if (target) { target.scrollIntoView({ block: "start" }); target.tabIndex = -1; target.focus({ preventScroll: true }); }
      else el("workspace-content").scrollTop = 0;
    } else if (state.view) {
      state.view.dispatch({ selection: { anchor: from, head: to }, effects: kit.EditorView.scrollIntoView(from, { y: "start" }) });
      state.view.focus();
    }
  }

  function followEditorLink(view, doc) {
    rebuildLinks();
    const position = view.state.selection.main.head;
    const reference = state.linkIndex?.documents.get(doc.path)?.references.find((ref) => position >= ref.from && position <= ref.to);
    if (!reference) return false;
    navigateLink(reference.destination, reference.kind, doc.path); return true;
  }

  function wikiCompletion(context, doc) {
    if (!canEdit(doc) || doc.recovery) return null;
    const match = context.matchBefore(/\[\[[^\]\n]*$/);
    if (!match) return null;
    const prefix = context.state.doc.sliceString(0, match.from), escapes = /\\+$/.exec(prefix)?.[0].length || 0;
    if (escapes % 2) return null;
    for (let node = kit.syntaxTree(context.state).resolveInner(match.from, 1); node; node = node.parent) {
      if (/Code|Comment|HTMLBlock/.test(node.name)) return null;
    }
    const text = context.state.doc.toString(), yaml = /^---\n[\s\S]*?\n(?:---|\.\.\.)(?:\n|$)/.exec(text);
    if (yaml && context.pos < yaml[0].length) return null;
    const candidates = state.linkIndex ? kit.completionCandidates(state.linkIndex, doc.path, match.text.slice(2)) : [];
    return {
      from: match.from + 2,
      options: candidates.map((candidate) => ({
        label: candidate.label, detail: candidate.detail, type: "text",
        apply(view, completion, from, to) {
          let end = to;
          while (end < view.state.doc.length && end < to + 2 && view.state.doc.sliceString(end, end + 1) === "]") end += 1;
          const insert = `${candidate.insert}]]`;
          view.dispatch({ changes: { from, to: end, insert }, selection: { anchor: from + insert.length }, userEvent: "input.complete" });
        },
      })),
      filter: false,
    };
  }

  class NoteLinkWidget extends (kit?.WidgetType || class {}) {
    constructor(reference, embed) { super(); this.reference = reference; this.embed = embed; this.key = `${state.indexVersion}:${reference.sourcePath}:${reference.from}:${reference.destination}`; }
    eq(other) { return other.key === this.key; }
    toDOM() {
      if (this.embed) {
        const node = document.createElement("div"); node.className = "live-note-embed";
        node.innerHTML = kit.renderEmbed(state.linkIndex, this.reference);
        return node;
      }
      const node = document.createElement("button"); node.type = "button";
      node.className = "note-link"; node.textContent = this.reference.label || this.reference.destination;
      node.title = this.reference.destination;
      node.addEventListener("click", (event) => { event.preventDefault(); navigateLink(this.reference.destination, this.reference.kind, this.reference.sourcePath); });
      return node;
    }
    ignoreEvent() { return true; }
  }

  function linkDecorations(editorState, doc) {
    const parsed = state.linkIndex?.documents.get(doc.path);
    if (!parsed || parsed.text !== editorState.doc.toString()) return kit.Decoration.none;
    const ranges = [], selection = editorState.selection.main;
    const start = editorState.doc.lineAt(selection.from).from, end = editorState.doc.lineAt(selection.to).to;
    for (const reference of parsed.references) {
      if (reference.from < 0 || reference.to > editorState.doc.length || reference.from >= reference.to) continue;
      const resolved = reference.resolution?.status === "resolved", outside = reference.to < start || reference.from > end;
      if (doc.mode === "live" && outside && resolved && !editorState.doc.sliceString(reference.from, reference.to).includes("\n")) {
        const line = editorState.doc.lineAt(reference.from);
        const block = reference.embed && line.from === reference.from && line.to === reference.to;
        ranges.push(kit.Decoration.replace({ widget: new NoteLinkWidget(reference, Boolean(block)), block: Boolean(block) }).range(reference.from, reference.to));
      } else {
        ranges.push(kit.Decoration.mark({ class: `note-link-source ${resolved ? "resolved" : "unresolved"}`, attributes: { title: `${reference.destination} · ${reference.resolution?.status || "unavailable"} · ⌘/Ctrl+Enter to follow` } }).range(reference.from, reference.to));
      }
    }
    return kit.Decoration.set(ranges, true);
  }

  function toggleContext(mode) {
    state.contextOpen = !(state.contextOpen && state.contextMode === mode);
    state.contextMode = mode; if (window.innerWidth < 760) setSidebar(true);
    renderContext(); state.view?.requestMeasure();
  }

  function renderContext() {
    const doc = active(), visible = Boolean(doc && state.contextOpen && !state.graphActive);
    el("workspace-context").hidden = !visible;
    el("workspace-context").classList.toggle("local-context", state.contextMode === "local");
    for (const [mode, id] of [["backlinks", "workspace-backlinks-toggle"], ["local", "workspace-local-toggle"], ["outline", "workspace-outline-toggle"]]) {
      const selected = visible && state.contextMode === mode;
      el(id).setAttribute("aria-expanded", String(selected));
      el(id).setAttribute("aria-label", `${selected ? "Hide" : "Show"} ${mode === "local" ? "local graph" : mode}`);
    }
    for (const button of document.querySelectorAll("[data-context]")) button.setAttribute("aria-pressed", String(button.dataset.context === state.contextMode));
    for (const [mode, id] of [["backlinks", "workspace-backlinks"], ["outgoing", "workspace-outgoing"], ["outline", "workspace-outline"], ["local", "workspace-local-graph"]]) el(id).hidden = state.contextMode !== mode;
    renderIndexStatus();
    if (!visible) return;
    if (state.contextMode === "local") return renderGraph("local");
    if (state.contextMode === "outline") return renderOutline();
    const container = el(state.contextMode === "backlinks" ? "workspace-backlink-items" : "workspace-outgoing-items"); container.replaceChildren();
    if (!state.linkIndex) return;
    if (state.contextMode === "backlinks") {
      const groups = kit.backlinks(state.linkIndex, doc.path), total = groups.reduce((sum, group) => sum + group.count, 0);
      el("workspace-backlinks-title").textContent = `Linked mentions · ${total}${state.linkIndex.partial ? " (partial)" : ""}`;
      if (!groups.length) { const p = document.createElement("p"); p.className = "context-empty"; p.textContent = "No linked mentions yet."; container.append(p); }
      for (const group of groups) {
        const details = document.createElement("details"); details.open = true; details.className = "backlink-group";
        const summary = document.createElement("summary"); summary.textContent = `${withoutExtension(basename(group.path))} · ${group.count}`; summary.title = group.path;
        details.append(summary);
        for (const occurrence of group.occurrences) {
          const button = document.createElement("button"); button.type = "button"; button.className = "backlink-occurrence";
          button.textContent = occurrence.snippet; button.title = `${group.path} · line ${occurrence.line}`;
          button.addEventListener("click", async () => { await openDocument(group.path, { refresh: true }); if (state.active === group.path) jumpTo(occurrence.from, occurrence.to); });
          details.append(button);
        }
        container.append(details);
      }
    } else {
      const references = state.linkIndex.documents.get(doc.path)?.references || [];
      if (!references.length) { const p = document.createElement("p"); p.className = "context-empty"; p.textContent = "No outgoing links yet."; container.append(p); }
      for (const reference of references) {
        const button = document.createElement("button"); button.type = "button"; button.className = "outgoing-link";
        button.textContent = `${reference.label || reference.destination} · ${reference.resolution.status}`;
        button.addEventListener("click", () => navigateLink(reference.destination, reference.kind, doc.path)); container.append(button);
      }
    }
  }

  function showGlobalGraph() {
    if (!state.workspace) return showFolderDialog();
    showWorkspace(); state.openRequest += 1; state.graphTab = true; state.graphActive = true; renderActive();
    if (!state.snapshot) refreshIndex();
  }

  function graphControl(mode, name) { return el(`${mode}-graph-${name}`); }

  function ensureGraphControls(mode) {
    const container = el(`workspace-${mode}-graph`);
    if (graphControl(mode, "canvas")) return;
    const local = mode === "local";
    container.innerHTML = `
      <div class="graph-heading"><div><h2 id="${mode}-graph-title">${local ? "Local graph" : "Graph view"}</h2>${local ? `<span id="local-graph-context"></span>` : ""}</div><div class="graph-actions">${local ? '<button id="local-graph-pin" class="icon-button" type="button" aria-label="Pin local graph" title="Pin local graph" aria-pressed="false" data-icon="Pin"></button>' : ""}<button id="${mode}-graph-fit" class="icon-button" type="button" aria-label="Fit ${mode} graph" title="Fit graph" data-icon="Maximize2"></button><button id="${mode}-graph-zoom-in" class="icon-button" type="button" aria-label="Zoom in ${mode} graph" title="Zoom in" data-icon="ZoomIn"></button><button id="${mode}-graph-zoom-out" class="icon-button" type="button" aria-label="Zoom out ${mode} graph" title="Zoom out" data-icon="ZoomOut"></button><button id="${mode}-graph-show-list" class="icon-button" type="button" aria-label="Show ${mode} graph note list" title="Note list" data-icon="List" aria-expanded="false"></button></div></div>
      ${local ? "" : '<div class="index-status" role="status" hidden></div>'}
      <div class="graph-viewport"><div id="${mode}-graph-canvas" class="graph-canvas" role="img" aria-label="${local ? "Local note connections" : "All note connections"}. Use the note list for keyboard navigation."></div><p id="${mode}-graph-empty" class="graph-empty" hidden></p>
        <details class="graph-settings"><summary aria-label="${local ? "Local" : "Global"} graph settings" title="Graph settings"><span data-icon="Settings"></span><span>Settings</span></summary><div>
          ${local ? `<label for="local-graph-depth">Depth</label><select id="local-graph-depth"><option value="1">1</option><option value="2">2</option><option value="3">3</option></select><label class="graph-check"><input id="local-graph-incoming" type="checkbox" checked> Incoming links</label><label class="graph-check"><input id="local-graph-outgoing" type="checkbox" checked> Outgoing links</label><label class="graph-check"><input id="local-graph-neighbors" type="checkbox" checked> Links between neighbors</label>` : ""}
          <label for="${mode}-graph-search">Filter notes</label><input id="${mode}-graph-search" type="text" placeholder="Name or path…" autocomplete="off">
          <label for="${mode}-graph-folder">Folder</label><select id="${mode}-graph-folder"><option value="">All folders</option></select>
          <label for="${mode}-graph-tag">Tag</label><select id="${mode}-graph-tag"><option value="">All tags</option></select>
          <label class="graph-check"><input id="${mode}-graph-existing" type="checkbox"> Existing files only</label>
          <label class="graph-check"><input id="${mode}-graph-indexes" type="checkbox"> Navigation documents and links</label>
          <label class="graph-check"><input id="${mode}-graph-orphans" type="checkbox" checked> Show orphans</label>
          <label class="graph-check"><input id="${mode}-graph-references" type="checkbox"> Markdown links</label>
          <label class="graph-check"><input id="${mode}-graph-citations" type="checkbox"> Citations</label>
          <label class="graph-check"><input id="${mode}-graph-discovery" type="checkbox"> Discovery links</label>
          <label class="graph-check"><input id="${mode}-graph-relations" type="checkbox" checked> Accepted research relations</label>
          <label for="${mode}-graph-type">Relation type</label><select id="${mode}-graph-type"><option value="">All types</option></select>
          <label class="graph-check"><input id="${mode}-graph-labels" type="checkbox" checked> Show labels</label>
        </div></details>
      </div><div class="graph-footer"><span id="${mode}-graph-counts" role="status"></span><span id="${mode}-graph-progress" role="status" hidden>Arranging graph…</span><span class="graph-legend"><span>— Other links</span><span class="relation-legend">╴ Research relations</span></span><span>Discovery suggestions and communities are derived, not accepted research links. Unconnected does not mean unrelated.</span></div><div id="${mode}-graph-list" class="graph-note-list" aria-label="${local ? "Local" : "Global"} graph notes" hidden></div>`;
    fillIcons(container);
    container.querySelectorAll("input,select").forEach((control) => control.addEventListener(control.type === "text" ? "input" : "change", () => renderGraph(mode, true)));
    graphControl(mode, "fit").addEventListener("click", () => fitGraph(state.graphs.get(mode)));
    for (const [name, factor] of [["zoom-in", 1.25], ["zoom-out", .8]]) graphControl(mode, name).addEventListener("click", () => {
      const graph = state.graphs.get(mode); if (!graph) return;
      const point = { x: graph.width() / 2, y: graph.height() / 2 }, view = state.graphViews.get(graph);
      if (view) view.zoomBy(factor, point);
      else graph.zoom({ level: graph.zoom() * factor, renderedPosition: point });
    });
    graphControl(mode, "show-list").addEventListener("click", () => {
      if (state.graphLists.has(mode)) state.graphLists.delete(mode); else state.graphLists.add(mode);
      graphControl(mode, "list").hidden = !state.graphLists.has(mode);
      graphControl(mode, "show-list").setAttribute("aria-expanded", String(state.graphLists.has(mode)));
      if (state.graphLists.has(mode)) renderGraph(mode);
      const graph = state.graphs.get(mode); if (graph) resizeGraph(graph);
    });
    if (local) graphControl(mode, "pin").addEventListener("click", () => { state.localPinned = state.localPinned ? null : active()?.path; renderGraph("local", true); });
  }

  function populateGraphChoices(mode) {
    if (!state.linkIndex) return;
    const documents = [...state.linkIndex.documents.values()];
    const folders = new Set();
    for (const doc of documents) for (let folder = parentPath(doc.path); folder; folder = parentPath(folder)) folders.add(folder);
    for (const [name, values, title] of [
      ["folder", [...folders], "All folders"],
      ["tag", documents.flatMap((doc) => doc.tags || []), "All tags"],
      ["type", (state.snapshot?.relations || []).map((relation) => relation.type), "All types"],
    ]) {
      const select = graphControl(mode, name), value = select.value, choices = [...new Set(values)].sort();
      const signature = JSON.stringify(choices);
      if (select.dataset.choices === signature) continue;
      select.dataset.choices = signature; select.replaceChildren();
      for (const choice of ["", ...choices]) { const option = document.createElement("option"); option.value = choice; option.textContent = choice || title; select.append(option); }
      if (choices.includes(value)) select.value = value;
    }
  }

  function fitGraph(graph) {
    kit.fitGraph(graph);
  }

  function resizeGraphs() {
    window.requestAnimationFrame(() => {
      el("workspace-tabs").querySelector(".active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
      for (const [mode, graph] of state.graphs) {
        const canvas = graphControl(mode, "canvas");
        if (canvas?.offsetWidth && canvas.offsetHeight) resizeGraph(graph);
      }
    });
  }

  function resizeGraph(graph) {
    const view = state.graphViews.get(graph);
    if (view) { view.resize(); return; }
    const before = state.graphSizes.get(graph);
    graph.resize();
    const size = { width: graph.width(), height: graph.height() };
    if (before?.width > 1 && before.height > 1 && size.width > 1 && size.height > 1 && (before.width !== size.width || before.height !== size.height)) {
      graph.panBy({ x: (size.width - before.width) / 2, y: (size.height - before.height) / 2 });
    }
    state.graphSizes.set(graph, size);
  }

  function renderGraph(mode) {
    ensureGraphControls(mode); populateGraphChoices(mode); renderIndexStatus();
    const local = mode === "local", path = local ? state.localPinned || active()?.path : active()?.path;
    if (local) {
      graphControl(mode, "title").textContent = path ? `Graph of ${withoutExtension(basename(path))}` : "Local graph";
      graphControl(mode, "context").textContent = state.localPinned ? "Pinned to this note" : "Linked to active note";
      graphControl(mode, "pin").setAttribute("aria-pressed", String(Boolean(state.localPinned)));
      graphControl(mode, "pin").setAttribute("aria-label", state.localPinned ? "Unpin local graph" : "Pin local graph");
    }
    const options = {
      search: graphControl(mode, "search").value, folder: graphControl(mode, "folder").value, tag: graphControl(mode, "tag").value,
      existingOnly: graphControl(mode, "existing").checked, showReferences: graphControl(mode, "references").checked,
      showNavigation: graphControl(mode, "indexes").checked,
      showCitations: graphControl(mode, "citations").checked, showDiscovery: graphControl(mode, "discovery").checked,
      showRelations: graphControl(mode, "relations").checked, relationType: graphControl(mode, "type").value,
    };
    if (local) options.local = { path, depth: Number(graphControl(mode, "depth").value), incoming: graphControl(mode, "incoming").checked, outgoing: graphControl(mode, "outgoing").checked, neighbors: graphControl(mode, "neighbors").checked };
    let data = state.linkIndex ? kit.graphData(state.linkIndex, options) : { nodes: [], edges: [] };
    if (!graphControl(mode, "orphans").checked) {
      const connected = new Set(data.edges.flatMap((edge) => [edge.source, edge.target]));
      data = { ...data, nodes: data.nodes.filter((node) => connected.has(node.id) || local && node.pathAliases?.includes(path)) };
    }
    const empty = graphControl(mode, "empty"); empty.hidden = Boolean(data.nodes.length);
    empty.textContent = !state.linkIndex && state.indexing ? "Indexing notes…" : local && !path ? "Open a note to see its connections." : "No notes match these filters.";
    const unresolved = data.unresolved || {}, hidden = unresolved.hidden || 0;
    const resolutionCounts = [["missing", "missing"], ["ambiguous", "ambiguous"], ["fragments", "missing heading/block"]].filter(([key]) => unresolved[key]).map(([key, label]) => `${unresolved[key]} ${label}`);
    graphControl(mode, "counts").textContent = `${data.nodes.length} ${data.nodes.length === 1 ? "note" : "notes"} · ${data.edges.length} ${data.edges.length === 1 ? "connection" : "connections"}${options.showNavigation ? "" : " · navigation hidden"}${resolutionCounts.length ? ` · ${resolutionCounts.join(", ")}${hidden ? ` (${hidden} hidden)` : ""}` : ""}${data.partial || state.snapshot?.truncated ? " · partial" : ""}`;
    if (data.semantic_map?.communities?.length) {
      graphControl(mode, "counts").textContent += ` · ${data.semantic_map.communities.length} derived communities`;
      const noiseCount = data.semantic_map.communities.filter((row) => row.noise === true).length;
      if (noiseCount) graphControl(mode, "counts").textContent += ` · ${noiseCount} density-noise sources`;
    }
    if (!local) { el("workspace-save-state").textContent = "Graph view"; el("workspace-counts").textContent = graphControl(mode, "counts").textContent; }
    const signature = JSON.stringify(data);
    let graph = state.graphs.get(mode);
    const list = graphControl(mode, "list");
    if (list.dataset.notes !== signature && (data.nodes.length <= 1000 || state.graphLists.has(mode))) {
      list.dataset.notes = signature; list.replaceChildren();
      let shown = 0;
      const appendPage = () => {
        list.querySelector(".graph-list-more")?.remove();
        for (const node of data.nodes.slice(shown, shown + 200)) {
          const button = document.createElement("button"); button.type = "button"; button.textContent = node.label;
          button.title = [node.path || node.label, ...(node.pathAliases?.filter((alias) => alias !== node.path) || []), node.source_coverage?.label, ...(node.candidates || [])].filter(Boolean).join(" · "); button.disabled = !node.path || node.missing;
          button.addEventListener("click", () => openDocument(node.path, { refresh: true })); list.append(button);
        }
        shown += 200;
        if (shown < data.nodes.length) {
          const more = document.createElement("button"); more.type = "button"; more.className = "graph-list-more";
          more.textContent = `Show more notes (${data.nodes.length - shown} remaining)`; more.addEventListener("click", appendPage); list.append(more);
        }
      };
      appendPage();
    }
    const elements = [
      ...data.nodes.map((node) => ({ data: { ...node, labelOpacity: 1, current: node.pathAliases?.includes(path) ? "yes" : "no" }, classes: node.missing ? "missing" : "" })),
      ...data.edges.map((edge) => ({ data: edge, classes: edge.layer === "relation" ? "relation" : "reference" })),
    ];
    const hadNodes = Boolean(graph?.nodes().length);
    if (!graph) {
      graph = kit.cytoscape({
        container: graphControl(mode, "canvas"), elements: [], layout: { name: "preset" }, minZoom: .005, maxZoom: 8,
        headless: true, styleEnabled: false, textureOnViewport: false,
        style: [
          { selector: "node", style: { "background-color": "#626167", width: 12, height: 12, label: "data(label)", "text-opacity": "data(labelOpacity)", "min-zoomed-font-size": 8, "font-size": 11, "font-family": "-apple-system, BlinkMacSystemFont, sans-serif", color: "#47454b", "text-valign": "bottom", "text-margin-y": 5, "text-max-width": 160, "text-wrap": "ellipsis" } },
          { selector: "node.labels-hidden", style: { label: "" } },
          { selector: 'node[current="yes"]', style: { "background-color": "#7851b6", width: 14, height: 14 } },
          { selector: "node.missing", style: { "background-color": "#d8cbea", "border-width": 1, "border-color": "#9b80bf", "border-style": "dashed", color: "#81669f" } },
          { selector: "edge", style: { width: 1, "line-color": "#bcb9c3", "curve-style": "straight", "target-arrow-shape": "none", "target-arrow-color": "#bcb9c3", "arrow-scale": .45, opacity: .5 } },
          { selector: "edge.relation", style: { "line-color": "#aa8ccf", "line-style": "dashed", "curve-style": "bezier", "target-arrow-color": "#aa8ccf" } },
          { selector: ":selected", style: { "background-color": "#7851b6", "line-color": "#7851b6", "target-arrow-color": "#7851b6" } },
          { selector: ".dimmed", style: { opacity: .15, "text-opacity": 0 } },
          { selector: "node.neighbor", style: { "background-color": "#7851b6" } },
          { selector: "edge.neighbor", style: { "line-color": "#9270bd", "target-arrow-color": "#9270bd", "target-arrow-shape": "triangle", opacity: .9, width: 1.5 } },
          { selector: "node.hovered", style: { label: "data(label)", "min-zoomed-font-size": 0, "text-opacity": 1, "text-wrap": "none", "text-background-color": "#ffffff", "text-background-opacity": .9, "text-background-padding": 4, "background-color": "#7851b6" } },
        ],
      });
      graph.on("tap", "node", (event) => {
        const node = event.target.data();
        if (node.path && !node.missing) openDocument(node.path, { refresh: true });
        else message("This note does not exist in the selected workspace.");
      });
      graph.on("mouseover", "node", (event) => {
        if (state.graphViews.get(graph)) return;
        const node = event.target, labels = state.graphLabels.get(graph);
        labels.hovered = node; labels.neighborhood = node.closedNeighborhood();
        graph.batch(() => {
          if (labels.nodes.length <= 1000) { labels.dimmed = graph.elements(); labels.dimmed.addClass("dimmed"); }
          labels.neighborhood.removeClass("dimmed").addClass("neighbor"); node.addClass("hovered");
          node.style("font-size", Math.max(11, 12 / graph.zoom()));
        });
      });
      graph.on("mouseout", "node", () => {
        if (state.graphViews.get(graph)) return;
        const labels = state.graphLabels.get(graph);
        graph.batch(() => {
          labels.hovered?.removeStyle("font-size").removeClass("hovered");
          labels.neighborhood?.removeClass("neighbor"); labels.dimmed?.removeClass("dimmed");
        });
        labels.hovered = labels.neighborhood = labels.dimmed = null;
      });
      graph.on("zoom", () => {
        if (state.graphViews.get(graph)) return;
        const labels = state.graphLabels.get(graph);
        if (labels.frame) return;
        labels.frame = window.requestAnimationFrame(() => {
          labels.frame = null;
          if (!graph.destroyed()) updateGraphLabels(mode, graph);
        });
      });
      graph.on("grab drag", "node", (event) => {
        if (!state.graphViews.get(graph)) return;
        let layout = state.graphLayouts.get(mode);
        if (!layout?.pinNode?.(event.target.id(), event.target.position())) {
          layout = startGraphLayout(mode, graph, { force: true, fit: false });
          layout.pinNode?.(event.target.id(), event.target.position());
        }
      });
      graph.on("free", "node", () => {
        updateGraphLabels(mode, graph, true);
        if (state.graphViews.get(graph)) startGraphLayout(mode, graph, { force: true, fit: false });
      });
      graph.on("destroy", () => window.cancelAnimationFrame(state.graphLabels.get(graph)?.frame));
      state.graphLabels.set(graph, { frame: null, opacity: null, hidden: null });
      state.graphs.set(mode, graph);
      state.graphViews.set(graph, kit.createGraphView(graph, { container: graphControl(mode, "canvas") }));
    }
    if (signature !== state.graphSignatures.get(mode)) {
      state.graphSignatures.set(mode, signature);
      state.graphLabels.get(graph).opacity = state.graphLabels.get(graph).hidden = null;
      const wanted = new Set(elements.map((element) => element.data.id));
      const retained = new Set(graph.nodes().map((node) => node.id())), added = [];
      graph.batch(() => {
        graph.elements().filter((element) => !wanted.has(element.id())).remove();
        for (const element of elements) {
          const existing = graph.getElementById(element.data.id);
          if (existing.length) { existing.data(element.data); existing.toggleClass("missing", Boolean(element.data.missing)); }
          else { const next = graph.add(element); if (next.isNode()) added.push(next); }
        }
        if (hadNodes) added.forEach((node, index) => {
          const neighbor = node.neighborhood().nodes().filter((entry) => retained.has(entry.id())).first();
          const center = neighbor.length ? neighbor.position() : { x: 0, y: 0 }, angle = index * 2.4, radius = 30 + 8 * Math.sqrt(index);
          node.position({ x: center.x + Math.cos(angle) * radius, y: center.y + Math.sin(angle) * radius });
        });
      });
    }
    graph.nodes().forEach((node) => node.data("current", node.data("pathAliases")?.includes(path) ? "yes" : "no"));
    updateGraphLabels(mode, graph, true);
    resizeGraph(graph);
    startGraphLayout(mode, graph, { randomize: !hadNodes });
  }

  function startGraphLayout(mode, graph, options = {}) {
    const layout = kit.layoutGraph(graph, { interactive: Boolean(state.graphViews.get(graph)), randomize: false, ...options, onError: () => message("Graph layout could not finish. Try filtering to fewer notes.", true) });
    if (state.graphLayouts.get(mode) !== layout) {
      state.graphLayouts.set(mode, layout);
      graphControl(mode, "progress").hidden = false; graphControl(mode, "canvas").setAttribute("aria-busy", "true");
      layout.finished.then((result) => {
        if (graph.destroyed() || state.graphs.get(mode) !== graph || state.graphLayouts.get(mode) !== layout) return;
        graphControl(mode, "progress").hidden = true; graphControl(mode, "canvas").setAttribute("aria-busy", "false");
        if (result.status === "complete" && el("workspace-message").textContent === "Graph layout could not finish. Try filtering to fewer notes.") message();
        updateGraphLabels(mode, graph, true);
      });
    }
    return layout;
  }

  function updateGraphLabels(mode, graph, geometryChanged = false) {
    const view = state.graphViews.get(graph);
    if (view) { view.setOptions({ labels: graphControl(mode, "labels").checked }); return; }
    const labels = state.graphLabels.get(graph);
    if (geometryChanged || !labels.nodes) {
      labels.nodes = graph.nodes();
      const bounds = labels.nodes.boundingBox({ includeLabels: false });
      labels.spacing = Math.sqrt(Math.max(1, bounds.w) * Math.max(1, bounds.h) / Math.max(1, labels.nodes.length));
    }
    const zoom = graph.zoom(), hidden = !graphControl(mode, "labels").checked;
    const opacity = Math.round(Math.max(0, Math.min(1, labels.nodes.length <= 50 ? (zoom - .3) / .5 : (labels.spacing * zoom - 40) / 24)) * 20) / 20;
    graph.batch(() => {
      if (labels.hidden !== hidden) { labels.nodes.toggleClass("labels-hidden", hidden); labels.hidden = hidden; }
      if (labels.opacity !== opacity) { labels.nodes.data("labelOpacity", opacity); labels.opacity = opacity; }
      labels.hovered?.style("font-size", Math.max(11, 12 / zoom));
    });
  }

  function liveDecorations(view, doc) {
    if (doc.mode !== "live") return kit.Decoration.none;
    const decorations = [];
    const selection = view.state.selection.main;
    const activeFrom = view.state.doc.lineAt(selection.from).from;
    const activeTo = view.state.doc.lineAt(selection.to).to;
    const outsideSelection = (from, to) => to < activeFrom || from > activeTo;
    kit.syntaxTree(view.state).iterate({ enter(node) {
      const name = node.name;
      if (/^ATXHeading[1-6]$/.test(name)) decorations.push(kit.Decoration.line({ class: `live-heading live-h${name.slice(-1)}` }).range(view.state.doc.lineAt(node.from).from));
      if (name === "HeaderMark") decorations.push(kit.Decoration.mark({ class: "live-source-mark" }).range(node.from, node.to));
      if (name === "FencedCode") {
        const first = view.state.doc.lineAt(node.from), last = view.state.doc.lineAt(node.to);
        for (let number = first.number; number <= last.number; number += 1) {
          const line = view.state.doc.line(number);
          const edge = number === first.number ? " live-code-first" : number === last.number ? " live-code-last" : "";
          decorations.push(kit.Decoration.line({ class: `live-code-line${edge}` }).range(line.from));
        }
      }
      if (name === "CodeInfo" && outsideSelection(node.from, node.to)) decorations.push(kit.Decoration.mark({ class: "live-code-language" }).range(node.from, node.to));
      if (name === "ListMark" && outsideSelection(node.from, node.to)) {
        if (/^[-+*]$/.test(view.state.doc.sliceString(node.from, node.to))) decorations.push(kit.Decoration.replace({ widget: new BulletWidget() }).range(node.from, node.to));
        else decorations.push(kit.Decoration.mark({ class: "live-ordered-marker" }).range(node.from, node.to));
      }
      if (["StrongEmphasis", "Emphasis", "InlineCode"].includes(name)) decorations.push(kit.Decoration.mark({ class: `live-${name.toLowerCase()}` }).range(node.from, node.to));
      if (["HeaderMark", "EmphasisMark", "CodeMark"].includes(name) && outsideSelection(node.from, node.to)) {
        const value = view.state.doc.sliceString(node.from, node.to);
        if (!value.includes("\n") && value.length < 8) decorations.push(kit.Decoration.replace({}).range(node.from, node.to));
      }
    } });
    return kit.Decoration.set(decorations, true);
  }

  function makeEditorState(doc, text, selection = 0) {
    text = normalizeText(text);
    const preview = kit.ViewPlugin.fromClass(class {
      constructor(view) { this.decorations = liveDecorations(view, doc); }
      update(update) { this.decorations = liveDecorations(update.view, doc); }
    }, { decorations: (plugin) => plugin.decorations });
    return kit.EditorState.create({
      doc: text, selection: { anchor: Math.min(selection, text.length) },
      extensions: [
        kit.EditorState.transactionFilter.of((transaction) => (state.organization || state.requiresReopen) && transaction.docChanged ? [] : transaction),
        kit.basicSetup, kit.markdown(), kit.EditorView.lineWrapping, kit.EditorState.lineSeparator.of("\n"),
        kit.autocompletion({ override: [(context) => wikiCompletion(context, doc)], interactionDelay: 0 }),
        kit.EditorView.cspNonce.of(token),
        doc.editability.of([
          kit.EditorState.readOnly.of(!canEdit(doc) || Boolean(doc.recovery)),
          kit.EditorView.editable.of(canEdit(doc) && !doc.recovery),
        ]),
        doc.identityAttributes.of(kit.EditorView.contentAttributes.of({ "aria-label": `Edit ${doc.name}`, spellcheck: "true" })),
        kit.Prec.highest(kit.keymap.of([
          { key: "Mod-s", run: () => { saveDocument(doc); return true; } },
          { key: "Mod-b", run: (view) => formatSelection(view, "**") },
          { key: "Mod-i", run: (view) => formatSelection(view, "*") },
          { key: "Mod-Enter", run: (view) => followEditorLink(view, doc) },
          ...(kit.indentWithTab ? [kit.indentWithTab] : []),
        ])),
        preview,
        kit.StateField.define({
          create: (editorState) => linkDecorations(editorState, doc),
          update: (value, transaction) => linkDecorations(transaction.state, doc),
          provide: (field) => kit.EditorView.decorations.from(field),
        }),
        kit.EditorView.updateListener.of((update) => {
          doc.editorState = update.state;
          if (update.docChanged && !doc.ignoreChanges) {
            persistDraft(doc);
            scheduleLinks();
            if (state.active === doc.path) { renderTabs(); renderDocumentStatus(); renderOutline(); }
          }
        }),
      ],
    });
  }

  function formatSelection(view, marker) {
    const doc = active();
    if (state.organization || state.requiresReopen || !canEdit(doc) || doc.recovery || doc.mode === "reading") return false;
    const selection = view.state.selection.main;
    const selected = view.state.doc.sliceString(selection.from, selection.to);
    view.dispatch({ changes: { from: selection.from, to: selection.to, insert: `${marker}${selected}${marker}` }, selection: { anchor: selection.from + marker.length, head: selection.to + marker.length }, userEvent: "input" });
    return true;
  }

  function newDocument(value) {
    const doc = { ...value, mode: canEdit(value) ? "live" : "reading", savedText: normalizeText(value.text), identityAttributes: new kit.Compartment(), editability: new kit.Compartment(), recovery: null, external: null, saving: false, ignoreChanges: false };
    try {
      const raw = localStorage.getItem(recoveryKey(doc));
      if (raw) {
        const saved = JSON.parse(raw);
        if (typeof saved.text !== "string" || typeof saved.revision !== "string") throw new Error("Invalid recovery data");
        if (normalizeText(saved.text) !== doc.savedText) doc.recovery = { ...saved, text: normalizeText(saved.text) };
        else localStorage.removeItem(recoveryKey(doc));
      }
    } catch (error) { storageFailure(error); }
    doc.editorState = makeEditorState(doc, value.text);
    return doc;
  }

  async function openDocument(path, options = {}) {
    if (!state.workspace || state.opening || state.organization) return;
    const epoch = state.epoch, request = ++state.openRequest;
    try {
      let doc = state.documents.get(path);
      if (!doc) {
        message(`Opening ${basename(path)}…`);
        const result = await api("workspace/read", { workspace_id: state.workspace.id, path });
        if (epoch !== state.epoch || request !== state.openRequest) return;
        doc = newDocument(result.document); state.documents.set(doc.path, doc);
        rememberDocument(result.document);
      } else if (options.refresh !== false && !dirty(doc) && !doc.recovery && !doc.saving) {
        const revision = doc.revision;
        const result = await api("workspace/read", { workspace_id: state.workspace.id, path });
        if (epoch !== state.epoch || request !== state.openRequest) return;
        if (state.documents.get(path) !== doc) return;
        if (doc.revision === revision && !doc.saving && !doc.recovery) {
          if (doc.revision !== result.document.revision) {
            if (dirty(doc)) doc.external = result.document;
            else {
              doc.savedText = normalizeText(result.document.text); doc.revision = result.document.revision;
              replaceDocumentText(doc, result.document.text);
            }
          }
          applyDocumentPolicy(doc, result.document); rememberDocument(result.document);
        }
      }
      if (epoch !== state.epoch || request !== state.openRequest) return;
      state.active = doc.path;
      state.graphActive = false;
      if (options.historyIndex !== undefined) state.historyIndex = options.historyIndex;
      else if (state.history[state.historyIndex] !== doc.path) {
        state.history = state.history.slice(0, state.historyIndex + 1);
        state.history.push(doc.path); state.historyIndex = state.history.length - 1;
      }
      message(state.recoveryError || "");
      reveal(doc.path); renderActive();
      if (window.innerWidth < 700) setSidebar(true);
      if (doc.mode !== "reading" && !doc.recovery) state.view?.focus();
    } catch (error) { if (epoch === state.epoch && request === state.openRequest) message(error.message, true); }
  }

  function renderTabs() {
    const tabs = el("workspace-tabs"), rows = [...state.documents.values()].map((doc) => ({ key: `note:${doc.path}`, doc }));
    if (state.graphTab) rows.push({ key: "graph", doc: null });
    const keys = new Set(rows.map((row) => row.key));
    for (const wrapper of [...tabs.children]) if (!keys.has(wrapper.dataset.tabKey)) wrapper.remove();
    rows.forEach(({ key, doc }, index) => {
      let wrapper = [...tabs.children].find((node) => node.dataset.tabKey === key);
      if (!wrapper) {
        wrapper = document.createElement("div"); wrapper.className = "document-tab"; wrapper.dataset.tabKey = key;
        const button = document.createElement("button"); button.type = "button"; button.className = "document-tab-name"; button.setAttribute("role", "tab");
        const select = () => wrapper.dataset.tabKey === "graph" ? showGlobalGraph() : openDocument(wrapper.dataset.path, { refresh: false });
        button.addEventListener("click", select);
        button.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const buttons = [...tabs.querySelectorAll('[role="tab"]')], at = buttons.indexOf(button);
          const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (at + (event.key === "ArrowLeft" ? -1 : 1) + buttons.length) % buttons.length;
          buttons[next].click(); buttons[next].focus();
        });
        const close = document.createElement("button"); close.type = "button"; close.className = "icon-button tab-close"; close.append(icon("X"));
        close.addEventListener("click", () => {
          if (wrapper.dataset.tabKey === "graph") {
            state.graphTab = false; state.graphActive = false; renderActive();
            tabs.querySelector('[aria-selected="true"]')?.focus();
          } else {
            const current = state.documents.get(wrapper.dataset.path); if (current) closeDocument(current);
          }
        });
        wrapper.append(button, close);
      }
      const selected = doc ? doc.path === state.active && !state.graphActive : state.graphActive;
      const button = wrapper.firstElementChild, close = wrapper.lastElementChild, name = doc?.name || "Graph view";
      wrapper.dataset.path = doc?.path || ""; wrapper.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
      button.title = doc?.path || name;
      const title = doc ? withoutExtension(name) + (dirty(doc) ? " •" : "") : name;
      if (button.textContent !== title) button.textContent = title;
      button.setAttribute("aria-label", `${name}${dirty(doc) ? ", unsaved" : ""}`);
      close.setAttribute("aria-label", `Close ${name}`); close.title = `Close ${name}`;
      if (tabs.children[index] !== wrapper) tabs.insertBefore(wrapper, tabs.children[index] || null);
    });
    const activeKey = state.graphActive ? "graph" : state.active ? `note:${state.active}` : "";
    if (tabs.dataset.activeKey !== activeKey) tabs.querySelector(".active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    tabs.dataset.activeKey = activeKey;
  }

  async function closeDocument(doc) {
    if (state.organization) return;
    const focused = el("workspace-tabs").contains(document.activeElement);
    if (doc.saving) return message("Wait for this note to finish saving.");
    const epoch = state.epoch;
    if (dirty(doc) || doc.recovery) {
      const accepted = await confirmAction(`Close ${doc.name}?`, "This note has unsaved changes. Recovery is available on reload in this browser session. Save first to keep changes across app restarts.", "Close without saving");
      if (!accepted || epoch !== state.epoch || state.documents.get(doc.path) !== doc || doc.saving) return;
    }
    if (!doc.recovery) persistDraft(doc);
    const paths = [...state.documents.keys()], index = paths.indexOf(doc.path);
    state.documents.delete(doc.path);
    if (state.active === doc.path) {
      state.active = paths[index - 1] || paths[index + 1] || null;
      if (!state.active && state.graphTab) state.graphActive = true;
      state.openRequest += 1;
    }
    renderActive();
    if (focused) el("workspace-tabs").querySelector('[aria-selected="true"]')?.focus();
  }

  function renderActive() {
    const doc = active();
    renderTabs();
    el("workspace-welcome").hidden = Boolean(state.workspace) || state.graphActive;
    el("workspace-empty").hidden = !state.workspace || Boolean(doc) || state.graphActive;
    el("workspace-document-area").hidden = state.graphActive;
    el("workspace-global-graph").hidden = !state.graphActive;
    el("workspace-graph-open").setAttribute("aria-pressed", String(state.graphActive));
    el("workspace-graph-open").classList.toggle("selected", state.graphActive);
    el("show-workspace").classList.toggle("selected", !state.graphActive);
    el("show-workspace").setAttribute("aria-pressed", String(!state.graphActive));
    el("workspace-empty-title").textContent = state.workspace?.entries?.some((entry) => entry.kind === "file") ? "Choose a note" : "A place for your next idea";
    el("workspace-document-header").hidden = !doc || state.graphActive;
    el("workspace-editor").hidden = !doc || doc.mode === "reading";
    el("workspace-reader").hidden = !doc || doc.mode !== "reading";
    el("workspace-disk-panel").hidden = true;
    el("workspace-reveal").disabled = !doc;
    if (state.graphActive) {
      el("show-workspace").classList.remove("selected"); el("show-workspace").setAttribute("aria-pressed", "false");
      for (const id of ["workspace-policy", "workspace-recovery", "workspace-conflict"]) el(id).hidden = true;
      renderGraph("global"); return;
    }
    if (!doc) {
      el("workspace-context").hidden = true;
      el("workspace-policy").hidden = true; el("workspace-recovery").hidden = true; el("workspace-conflict").hidden = true;
      el("workspace-file-title").hidden = true; el("workspace-outline-items").replaceChildren();
      el("workspace-save-state").textContent = state.workspace ? "No note selected" : "Markdown workspace";
      el("workspace-counts").textContent = ""; renderTree(); return;
    }
    el("workspace-file-title").hidden = false;
    el("workspace-file-title").textContent = withoutExtension(doc.name);
    el("workspace-breadcrumbs").textContent = doc.path.split("/").map(withoutExtension).join(" / ");
    el("workspace-breadcrumbs").title = doc.path;
    renderDocumentPolicy(doc);
    for (const button of document.querySelectorAll("[data-mode]")) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === doc.mode));
    }
    if (!state.view) state.view = new kit.EditorView({ state: doc.editorState, parent: el("workspace-editor") });
    else if (state.view.state !== doc.editorState) state.view.setState(doc.editorState);
    state.view.requestMeasure();
    state.view.dispatch({});
    if (doc.mode === "reading") renderReading(doc);
    hydrateEmbeds();
    renderDocumentStatus(); renderOutline(); renderTree(); renderContext();
  }

  function renderDocumentPolicy(doc) {
    el("workspace-policy").hidden = state.graphActive || canEdit(doc) && !doc.override_available && !state.requiresReopen;
    el("workspace-policy").textContent = state.requiresReopen ? "Reopen this folder before editing. The last file operation could not be verified." : doc.override_available
      ? `${doc.source_changed ? "The generated note changed. Your personal edits are preserved." : "Personal edits are saved separately from the generated note."} Only the note body can be saved.${doc.mode === "reading" ? " Choose Source or Live Preview to edit." : ""}`
      : doc.reason || "This generated note is read-only. Its metadata and generated content are managed by the research engine.";
  }

  function renderDocumentStatus() {
    const doc = active(); if (!doc) return;
    renderDocumentPolicy(doc);
    el("workspace-recovery").hidden = !doc.recovery;
    el("workspace-restore").disabled = !canEdit(doc);
    el("workspace-conflict").hidden = !doc.external;
    if (!doc.external) el("workspace-disk-panel").hidden = true;
    if (!el("workspace-disk-panel").hidden && doc.external) renderDiskReview(doc);
    el("workspace-save-draft").disabled = !canEdit(doc) || !canEdit(doc.external) || doc.saving;
    el("workspace-save").disabled = state.requiresReopen || !canEdit(doc) || Boolean(doc.recovery) || doc.saving || !dirty(doc);
    el("workspace-save-state").textContent = state.requiresReopen ? "Reopen required" : doc.saving ? "Saving…" : doc.recovery ? "Recovery available" : doc.external ? "Disk changed · draft preserved" : !canEdit(doc) ? "Read-only" : dirty(doc) ? "Unsaved changes" : doc.override_saved ? "Personal edit saved" : "Saved";
    const text = textOf(doc), words = text.trim() ? text.trim().split(/\s+/u).length : 0;
    const coverage = state.linkIndex?.documents.get(doc.path)?.source_coverage || doc.source_coverage;
    el("workspace-counts").textContent = `${words.toLocaleString()} words · ${text.length.toLocaleString()} characters${coverage?.label ? ` · Source: ${coverage.label}` : ""}`;
    el("workspace-back").disabled = state.historyIndex <= 0;
    el("workspace-forward").disabled = state.historyIndex >= state.history.length - 1;
  }

  function setMode(mode) {
    if (state.organization) return;
    const doc = active(); if (!doc || !["source", "live", "reading"].includes(mode)) return;
    doc.mode = mode;
    rebuildLinks();
    if (state.view) { state.view.dispatch({}); doc.editorState = state.view.state; }
    renderActive();
  }

  function renderReading(doc) {
    const indexed = state.linkIndex?.documents.get(doc.path)?.text === textOf(doc);
    el("workspace-reader").innerHTML = kit.renderMarkdown(textOf(doc), indexed ? { index: state.linkIndex, path: doc.path } : undefined);
    for (const anchor of el("workspace-reader").querySelectorAll('a:not([data-note-destination])')) anchor.setAttribute("rel", "noopener noreferrer");
    hydrateEmbeds();
  }

  async function hydrateEmbeds() {
    const doc = active();
    if (!doc || !state.snapshot?.library || state.organization || !state.linkIndex) return;
    const epoch = state.epoch, workspaceId = state.workspace.id, source = textOf(doc), generation = state.snapshot.generation;
    const paths = [], seen = new Set([doc.path]), queue = [[doc.path, 0]];
    let characters = 0;
    while (queue.length && paths.length < 16) {
      const [parent, depth] = queue.shift();
      for (const ref of state.linkIndex.documents.get(parent)?.references || []) {
        const path = ref.resolution?.path;
        if (!ref.embed || ref.resolution?.status !== "resolved" || seen.has(path)) continue;
        seen.add(path);
        const target = state.linkIndex.documents.get(path);
        if (characters + target.textLength > 4 * 1024 * 1024 || paths.length >= 16) continue;
        characters += target.textLength; paths.push(path);
        if (depth < 1) queue.push([path, depth + 1]);
      }
    }
    const missing = paths.filter((path) => typeof state.linkIndex.documents.get(path)?.text !== "string" && !state.previewRequests.has(path));
    if (!missing.length) return;
    let changed = false;
    for (const path of missing) {
      if (epoch !== state.epoch || generation !== state.snapshot?.generation || active() !== doc || textOf(doc) !== source || state.organization) return;
      if (state.previewRequests.has(path)) continue;
      const token = { revision: state.linkIndex.documents.get(path)?.revision };
      state.previewRequests.set(path, token);
      try {
        const { document: value } = await api("workspace/read", { workspace_id: workspaceId, path });
        if (epoch !== state.epoch || generation !== state.snapshot?.generation || active() !== doc || textOf(doc) !== source || state.organization || token.revision !== value.revision || state.linkIndex.documents.get(path)?.revision !== token.revision) return;
        // Keep the preview cache bounded independently of the complete catalogue.
        const total = () => [...state.previewDocuments.values()].reduce((sum, entry) => sum + entry.text.length, 0);
        while (state.previewDocuments.size >= 16 || total() + value.text.length > 4 * 1024 * 1024) state.previewDocuments.delete(state.previewDocuments.keys().next().value);
        state.previewDocuments.set(path, { ...value, text: normalizeText(value.text) }); changed = true;
      } catch { /* The embedded note retains its explicit open link if unavailable. */ }
      finally {
        if (state.previewRequests.get(path) === token) state.previewRequests.delete(path);
        if (epoch === state.epoch && (generation !== state.snapshot?.generation || active() !== doc || textOf(doc) !== source)) window.setTimeout(hydrateEmbeds, 0);
      }
    }
    if (changed && epoch === state.epoch && generation === state.snapshot?.generation && active() === doc && textOf(doc) === source) rebuildLinks();
  }

  function headings(doc) {
    const result = [];
    kit.syntaxTree(doc.editorState).iterate({ enter(node) {
      if (/^ATXHeading[1-6]$/.test(node.name)) result.push({ from: node.from, level: Number(node.name.slice(-1)), title: doc.editorState.doc.sliceString(node.from, node.to).replace(/^#+\s*/, "").replace(/\s+#+\s*$/, "") });
    } });
    return result;
  }

  function renderOutline() {
    const doc = active(), container = el("workspace-outline-items"); container.replaceChildren();
    if (!doc) return;
    const items = headings(doc);
    if (!items.length) { const p = document.createElement("p"); p.textContent = "No headings in this note."; container.append(p); }
    items.forEach((heading, index) => {
      const button = document.createElement("button"); button.type = "button";
      button.textContent = heading.title || "Untitled heading"; button.style.setProperty("--depth", heading.level - 1);
      button.addEventListener("click", () => {
        if (doc.mode === "reading") el("workspace-reader").querySelectorAll("h1,h2,h3,h4,h5,h6")[index]?.scrollIntoView({ block: "start" });
        else { state.view.dispatch({ selection: { anchor: heading.from }, effects: kit.EditorView.scrollIntoView(heading.from, { y: "start" }) }); state.view.focus(); }
      }); container.append(button);
    });
  }

  function applyDocumentPolicy(doc, value) {
    const changed = doc.editable !== value.editable || doc.reason !== value.reason
      || ["override_available", "override_saved", "source_changed"].some((key) => Boolean(doc[key]) !== Boolean(value[key]));
    doc.editable = value.editable; doc.reason = value.reason;
    for (const key of ["override_available", "override_saved", "source_changed", "source_revision"]) doc[key] = value[key];
    doc.bom = value.bom; doc.newline = value.newline;
    if (changed) {
      const effects = doc.editability.reconfigure([
        kit.EditorState.readOnly.of(!canEdit(doc) || Boolean(doc.recovery)),
        kit.EditorView.editable.of(canEdit(doc) && !doc.recovery),
      ]);
      if (active() === doc && state.view) state.view.dispatch({ effects });
      else doc.editorState = doc.editorState.update({ effects }).state;
    }
    return changed;
  }

  async function saveDocument(doc = active(), options = {}) {
    if (state.organization || state.requiresReopen || !doc || !canEdit(doc) || doc.recovery || doc.saving || state.opening || (!options.force && !dirty(doc))) return;
    const epoch = state.epoch, workspaceId = state.workspace.id;
    const text = textOf(doc), revision = options.revision ?? doc.revision;
    persistDraft(doc); doc.saving = true;
    if (active() === doc) renderDocumentStatus();
    try {
      const { document: saved } = await api(doc.override_available ? "workspace/save-override" : "workspace/save", { workspace_id: workspaceId, document_id: doc.id, revision, text });
      if (epoch !== state.epoch || state.documents.get(doc.path) !== doc) return;
      const policyChanged = applyDocumentPolicy(doc, saved);
      doc.savedText = normalizeText(saved.text); doc.revision = saved.revision; doc.external = null;
      rememberDocument(saved);
      if (textOf(doc) === text && text !== doc.savedText) replaceDocumentText(doc, doc.savedText);
      persistDraft(doc);
      if (policyChanged && active() === doc) renderActive();
      message(state.recoveryError || "");
    } catch (error) {
      if (epoch !== state.epoch || state.documents.get(doc.path) !== doc) return;
      if (error.status === 409 && error.current) {
        doc.external = error.current;
        const policyChanged = applyDocumentPolicy(doc, error.current);
        if (policyChanged && active() === doc) renderActive();
        message("The file changed on disk. Review the disk version before choosing which text to keep.", true);
      } else message(`Could not save ${doc.name}. ${error.message} Your draft remains open.`, true);
    } finally {
      doc.saving = false;
      if (state.documents.get(doc.path) === doc) { renderTabs(); if (active() === doc) renderDocumentStatus(); }
    }
  }

  function replaceDocumentText(doc, text) {
    const selection = doc.editorState.selection.main.head;
    doc.editorState = makeEditorState(doc, text, selection);
    if (active() === doc && state.view) state.view.setState(doc.editorState);
    scheduleLinks();
  }

  function restoreRecovery() {
    const doc = active(); if (!doc?.recovery || !canEdit(doc)) return;
    const saved = doc.recovery; doc.recovery = null;
    if (saved.revision !== doc.revision) doc.external = { ...doc, text: doc.savedText };
    doc.revision = saved.revision;
    replaceDocumentText(doc, saved.text); persistDraft(doc); renderActive();
  }

  async function discardRecovery() {
    const doc = active(); if (!doc?.recovery) return;
    const epoch = state.epoch, recovery = doc.recovery;
    const accepted = await confirmAction("Discard recovery draft?", "Keep the current disk version and remove this note's recovered draft.", "Discard recovery draft");
    if (!accepted || epoch !== state.epoch || active() !== doc || doc.recovery !== recovery || doc.saving) return;
    doc.recovery = null; forgetDraft(doc); replaceDocumentText(doc, doc.savedText); renderActive();
  }

  async function useDiskVersion() {
    const doc = active(); if (!doc?.external || doc.saving) return;
    const epoch = state.epoch, current = doc.external;
    if (dirty(doc)) {
      const accepted = await confirmAction("Use the disk version?", "Discard the draft in this tab and keep the disk version. This removes its recovery draft.", "Use disk version");
      if (!accepted || epoch !== state.epoch || active() !== doc || doc.saving) return;
      if (doc.external?.revision !== current.revision) return message("The disk version changed again. Review it before replacing your draft.", true);
    }
    doc.external = null; doc.savedText = normalizeText(current.text); doc.revision = current.revision;
    applyDocumentPolicy(doc, current);
    replaceDocumentText(doc, current.text); forgetDraft(doc); renderActive();
  }

  function renderDiskReview(doc) {
    if (!doc?.external) return;
    el("workspace-disk-text").textContent = doc.external.text;
    el("workspace-draft-text").textContent = textOf(doc);
    el("workspace-disk-panel").dataset.revision = doc.external.revision;
    el("workspace-disk-panel").dataset.path = doc.path;
  }

  async function saveEditedDraft() {
    const doc = active(), panel = el("workspace-disk-panel");
    if (!doc?.external || !canEdit(doc) || !canEdit(doc.external) || doc.saving || panel.hidden) return;
    if (panel.dataset.revision !== doc.external.revision || panel.dataset.path !== doc.path) {
      renderDiskReview(doc);
      return message("The disk version changed again. Review the updated comparison before saving.", true);
    }
    const epoch = state.epoch, revision = panel.dataset.revision, draft = textOf(doc);
    const accepted = await confirmAction("Save edited draft?", "Replace the disk version shown in this comparison with your edited draft. Further disk changes will still stop the save.", "Save edited draft");
    if (!accepted || epoch !== state.epoch || active() !== doc || doc.saving || !canEdit(doc) || !canEdit(doc.external)) return;
    if (doc.external.revision !== revision || textOf(doc) !== draft) {
      renderDiskReview(doc);
      return message("A version changed while you were reviewing. Review the updated comparison before saving.", true);
    }
    if (doc.override_available) {
      const prefix = (text) => /^---\n[\s\S]*?\n(?:---|\.\.\.)[ \t]*(?:\n|$)/.exec(normalizeText(text))?.[0] || "";
      const previous = prefix(doc.savedText), latest = prefix(doc.external.text);
      if (prefix(draft) === previous && previous !== latest) replaceDocumentText(doc, latest + draft.slice(previous.length));
    }
    await saveDocument(doc, { force: true, revision });
  }

  function showCreate(kind) {
    if (state.requiresReopen) return message("Reopen this workspace before creating files.", true);
    if (!state.workspace || state.workspace.editable === false || hasBusyWrite()) return;
    state.createKind = kind;
    const focused = state.workspace.entries.find((entry) => entry.path === state.treeFocus);
    state.createParent = focused?.kind === "directory" ? focused.path : parentPath(focused?.path || active()?.path || "");
    el("workspace-create-title").textContent = kind === "file" ? "New note" : "New folder";
    el("workspace-create-location").textContent = `In ${state.workspace.name}${state.createParent ? ` / ${state.createParent}` : ""}`;
    el("workspace-create-name").value = kind === "file" ? "Untitled.md" : "Untitled folder";
    el("workspace-create-error").textContent = "";
    el("workspace-create-dialog").showModal();
    el("workspace-create-name").focus();
    el("workspace-create-name").setSelectionRange(0, kind === "file" ? 8 : 15);
  }

  async function createEntry(event) {
    event.preventDefault();
    if (state.creating || !state.workspace) return;
    const epoch = state.epoch, workspaceId = state.workspace.id;
    state.creating = true; el("workspace-create-submit").disabled = true;
    try {
      const result = await api("workspace/create", { workspace_id: workspaceId, parent: state.createParent, name: el("workspace-create-name").value.trim(), kind: state.createKind });
      if (epoch !== state.epoch) return;
      if (state.createParent) state.expanded.add(state.createParent);
      applyWorkspace(result.workspace);
      el("workspace-create-dialog").close();
      if (result.document) {
        const doc = newDocument(result.document); state.documents.set(doc.path, doc);
        await openDocument(doc.path);
      } else {
        message("Folder created."); renderActive();
      }
    } catch (error) { if (epoch === state.epoch) el("workspace-create-error").textContent = error.message; }
    finally { state.creating = false; el("workspace-create-submit").disabled = false; refreshIndex(); }
  }

  function showQuickOpen() {
    if (!state.workspace) return showFolderDialog();
    if (state.opening) return;
    if (!el("workspace-quick-dialog").open) state.quickFocus = document.activeElement;
    state.quickOutside = false;
    el("workspace-quick-query").value = ""; state.quickIndex = 0;
    renderQuickResults(); el("workspace-quick-dialog").showModal(); el("workspace-quick-query").focus();
  }

  function closeQuickOpen() {
    const focus = state.quickFocus; state.quickFocus = null; state.quickOutside = false;
    if (el("workspace-quick-dialog").open) el("workspace-quick-dialog").close();
    if (focus?.isConnected) focus.focus();
  }

  function renderQuickResults() {
    const query = el("workspace-quick-query").value.toLocaleLowerCase().trim();
    const indexed = state.linkIndex ? kit.searchNotes(state.linkIndex, query) : [], seen = new Set(indexed.map((entry) => entry.path));
    state.quickResults = [...indexed, ...(state.workspace?.entries || []).filter((entry) => entry.kind === "file" && entry.path.toLocaleLowerCase().includes(query) && !seen.has(entry.path))].slice(0, 80);
    state.quickIndex = Math.min(state.quickIndex, Math.max(0, state.quickResults.length - 1));
    const list = el("workspace-quick-results");
    const signature = JSON.stringify([query, state.quickIndex, state.quickResults.map((entry) => [entry.path, entry.name, entry.alias])]);
    if (list.dataset.results === signature) return;
    list.dataset.results = signature; list.replaceChildren();
    if (!state.quickResults.length) { const p = document.createElement("p"); p.textContent = "No matching Markdown notes."; list.append(p); }
    state.quickResults.forEach((entry, index) => {
      const button = document.createElement("button"); button.type = "button"; button.setAttribute("role", "option");
      button.id = `workspace-quick-result-${index}`; button.setAttribute("aria-selected", String(index === state.quickIndex));
      const name = document.createElement("span"), path = document.createElement("small");
      name.textContent = entry.alias || withoutExtension(entry.name || basename(entry.path)); path.textContent = entry.path;
      button.append(name, path); button.addEventListener("click", () => { closeQuickOpen(); openDocument(entry.path); }); list.append(button);
    });
    if (state.quickResults.length) el("workspace-quick-query").setAttribute("aria-activedescendant", `workspace-quick-result-${state.quickIndex}`);
    else el("workspace-quick-query").removeAttribute("aria-activedescendant");
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }

  async function pollWorkspace() {
    if (!state.workspace || state.polling || state.opening || state.creating || state.organization || state.organizationMenu || state.requiresReopen || !workspaceVisible() || document.hidden) return;
    state.polling = true;
    const epoch = state.epoch, workspaceId = state.workspace.id;
    const doc = active(), beforeRevision = doc?.revision;
    try {
      if (!state.snapshot?.generation) {
        const { workspace } = await api("workspace/tree", { workspace_id: workspaceId });
        if (epoch !== state.epoch || state.opening || state.organization) return;
        const tree = el("workspace-tree"), wasFocused = tree.contains(document.activeElement);
        applyWorkspace(workspace); if (wasFocused) renderTree(true);
      }
      if (doc && !doc.saving && !doc.recovery) {
        const { document: current } = await api("workspace/read", { workspace_id: workspaceId, path: doc.path });
        if (epoch !== state.epoch || state.opening || state.organization || state.documents.get(doc.path) !== doc || doc.revision !== beforeRevision || doc.saving || doc.recovery) return;
        const policyChanged = applyDocumentPolicy(doc, current);
        if (policyChanged && active() === doc) renderActive();
        if (current.revision !== doc.revision) {
          if (dirty(doc)) { doc.external = current; if (active() === doc) renderDocumentStatus(); }
          else {
            doc.savedText = normalizeText(current.text); doc.revision = current.revision;
            replaceDocumentText(doc, current.text);
            if (active() === doc) { renderActive(); message("Updated from disk."); }
          }
        }
      }
    } catch (error) { if (epoch === state.epoch && !state.opening) message(`Could not refresh files. ${error.message}`, true); }
    finally { state.polling = false; if (epoch === state.epoch && !state.opening) await refreshIndex(); }
  }

  function focusedEntry() {
    return state.workspace?.entries.find((entry) => entry.path === state.treeFocus)
      || state.workspace?.entries.find((entry) => entry.path === state.active);
  }

  function organizationReason(entry) {
    if (!entry) return "Select a file or folder first.";
    if (state.requiresReopen) return "Reopen this workspace before organizing files.";
    if (state.workspace?.editable === false || entry.editable === false) return entry.reason || state.workspace.reason || "Generated content cannot be renamed or moved.";
    if (hasBusyWrite() || state.polling || state.indexing || state.confirmation) return "Wait for the current file operation to finish.";
    if ([...state.documents.values()].some((doc) => dirty(doc) || doc.recovery)) return "Save or review all drafts before organizing files.";
    if (state.workspace?.truncated) return "The file list is partial. Open a smaller workspace before organizing files.";
    if (!kit.planRelocation) return "File organization could not load. Restart the local app.";
    return "";
  }

  function renderOrganizationControl() {
    const entry = focusedEntry(), button = el("workspace-file-actions");
    button.disabled = !entry || Boolean(state.organization);
    button.title = entry ? `File actions: ${entry.path}` : "Select a file or folder first";
  }

  function closeFileActions(restoreFocus = true) {
    const menu = state.organizationMenu;
    state.organizationMenu = null;
    el("workspace-file-menu").close();
    if (restoreFocus && menu?.previousFocus?.isConnected) menu.previousFocus.focus();
  }

  function showFileActions(entry = focusedEntry()) {
    if (!entry || state.organization || state.organizationMenu || document.querySelector("dialog[open]")) return;
    state.treeFocus = entry.path;
    state.organizationMenu = { path: entry.path, previousFocus: document.activeElement };
    el("workspace-file-menu-path").textContent = entry.path;
    const reason = organizationReason(entry);
    el("workspace-file-menu-reason").textContent = reason;
    el("workspace-file-menu-reason").hidden = !reason;
    el("workspace-file-rename").disabled = Boolean(reason);
    el("workspace-file-move").disabled = Boolean(reason);
    el("workspace-file-menu").showModal();
    el(reason ? "workspace-file-menu-close" : "workspace-file-rename").focus();
  }

  function beginOrganization(action, entry = focusedEntry()) {
    const reason = organizationReason(entry);
    if (reason) {
      message(reason, true);
      if (state.organizationMenu) {
        el("workspace-file-menu-reason").textContent = reason; el("workspace-file-menu-reason").hidden = false;
        el("workspace-file-rename").disabled = true; el("workspace-file-move").disabled = true;
      }
      return;
    }
    if (!entry || (document.querySelector("dialog[open]") && !state.organizationMenu)) return;
    const previousFocus = state.organizationMenu?.previousFocus || document.activeElement;
    if (state.organizationMenu) closeFileActions(false);
    state.organization = {
      action, entry: { ...entry }, epoch: state.epoch, workspaceId: state.workspace.id,
      stage: "choose", previousFocus, parent: null, folderIndex: 0,
      active: state.active,
      documents: [...state.documents.values()].map((doc) => ({ doc, path: doc.path, revision: doc.revision, text: textOf(doc), savedText: doc.savedText })),
    };
    state.openRequest += 1; state.indexRequest += 1; state.indexing = false;
    state.indexParser?.close(); state.indexParser = null;
    window.clearTimeout(state.indexTimer);
    screen.inert = true; document.querySelector(".workspace-rail").inert = true;
    renderOrganizationControl();
    const rename = action === "rename";
    el("workspace-organization-title").textContent = rename ? "Rename" : "Move to folder";
    el("workspace-organization-source").textContent = entry.path;
    el("workspace-rename-field").hidden = !rename;
    el("workspace-rename-label").textContent = entry.kind === "file" ? "File name (.md)" : "Folder name";
    el("workspace-rename-name").required = rename;
    el("workspace-rename-name").value = entry.name;
    el("workspace-move-field").hidden = rename;
    el("workspace-move-search").value = "";
    el("workspace-organization-error").textContent = "";
    el("workspace-organization-preview").textContent = rename ? "Review rename" : "Review move";
    setOrganizationChoosing(true);
    if (!rename) renderMoveFolders();
    el("workspace-organization-dialog").showModal();
    const input = el(rename ? "workspace-rename-name" : "workspace-move-search");
    input.focus();
    if (rename) input.setSelectionRange(0, entry.kind === "file" ? entry.name.replace(/\.md$/i, "").length : entry.name.length);
  }

  function setOrganizationChoosing(enabled) {
    for (const id of ["workspace-rename-name", "workspace-move-search"]) el(id).disabled = !enabled;
    for (const button of el("workspace-move-folders").querySelectorAll("button")) button.disabled = !enabled;
    el("workspace-organization-preview").disabled = !enabled;
  }

  function renderMoveFolders() {
    const operation = state.organization;
    if (!operation || operation.action !== "move") return;
    const query = el("workspace-move-search").value.toLocaleLowerCase().trim();
    const source = operation.entry.path, currentParent = parentPath(source);
    const folders = [{ path: "", name: state.workspace.name, kind: "directory", editable: state.workspace.editable }, ...state.workspace.entries.filter((entry) => entry.kind === "directory")]
      .filter((entry) => entry.editable !== false && entry.path !== currentParent && entry.path !== source && !entry.path.startsWith(`${source}/`))
      .filter((entry) => (entry.path || state.workspace.name).toLocaleLowerCase().includes(query))
      .sort((a, b) => a.path.localeCompare(b.path));
    operation.folders = folders;
    operation.folderIndex = Math.max(0, Math.min(operation.folderIndex, folders.length - 1));
    const list = el("workspace-move-folders"); list.replaceChildren();
    if (!folders.length) { const p = document.createElement("p"); p.textContent = "No eligible destination folders."; list.append(p); }
    folders.forEach((entry, index) => {
      const button = document.createElement("button"); button.type = "button"; button.setAttribute("role", "option");
      button.id = `workspace-move-option-${index}`;
      button.setAttribute("aria-selected", String(operation.parent === entry.path));
      button.classList.toggle("keyboard-active", index === operation.folderIndex);
      button.append(icon("Folder"));
      const label = document.createElement("span"); label.textContent = entry.path || `${state.workspace.name} /`;
      button.append(label);
      button.addEventListener("click", () => { operation.parent = entry.path; operation.folderIndex = index; renderMoveFolders(); });
      list.append(button);
    });
    if (folders.length) el("workspace-move-search").setAttribute("aria-activedescendant", `workspace-move-option-${operation.folderIndex}`);
    else el("workspace-move-search").removeAttribute("aria-activedescendant");
    el("workspace-move-selected").textContent = operation.parent === null ? "Choose a destination folder." : `Move into ${operation.parent || `${state.workspace.name} /`}`;
    el("workspace-organization-preview").disabled = operation.parent === null;
  }

  function organizationUnchanged(operation) {
    return state.organization === operation && operation.epoch === state.epoch && operation.workspaceId === state.workspace?.id
      && operation.active === state.active && operation.documents.length === state.documents.size
      && operation.documents.every(({ doc, path, revision, text, savedText }) => state.documents.get(path) === doc && doc.path === path && doc.revision === revision && textOf(doc) === text && doc.savedText === savedText && !dirty(doc) && !doc.recovery && !doc.saving);
  }

  function recoveryBlockers(preview) {
    const found = new Set();
    try {
      for (const doc of preview.documents) {
        if (!doc.recovery_key) throw new Error("Recovery information is missing. Cancel and refresh the workspace before organizing files.");
        if (localStorage.getItem(recoveryKey(doc)) !== null) found.add(doc.path);
      }
      for (const move of preview.moves) {
        if (!move.target_recovery_key) throw new Error("Destination recovery information is missing. Cancel and refresh the workspace before organizing files.");
        if (localStorage.getItem(recoveryKey({ recovery_key: move.target_recovery_key })) !== null) found.add(move.to);
      }
    } catch (error) {
      throw new Error(error.message || "Local recovery could not be checked. Save and review recovery drafts before organizing files.");
    }
    return [...found];
  }

  async function previewOrganization(event) {
    event.preventDefault();
    const operation = state.organization;
    if (!operation || operation.stage !== "choose" || !organizationUnchanged(operation)) return;
    let destination;
    if (operation.action === "rename") {
      const name = el("workspace-rename-name").value.trim();
      if (!name || name.includes("/") || name.includes("\\")) { el("workspace-organization-error").textContent = "Enter one file or folder name."; return; }
      destination = [parentPath(operation.entry.path), name].filter(Boolean).join("/");
    } else {
      if (operation.parent === null) return;
      destination = [operation.parent, operation.entry.name].filter(Boolean).join("/");
    }
    if (destination === operation.entry.path) { el("workspace-organization-error").textContent = "Choose a different name or folder."; return; }
    operation.stage = "preview"; operation.destination = destination;
    const request = ++state.organizationRequest;
    setOrganizationChoosing(false); el("workspace-organization-error").textContent = "Reviewing paths and references…";
    try {
      const preview = await api("workspace/relocate-preview", { workspace_id: operation.workspaceId, source: operation.entry.path, destination });
      if (request !== state.organizationRequest || !organizationUnchanged(operation)) return;
      if (!preview.token || preview.source !== operation.entry.path || preview.destination !== destination || !Array.isArray(preview.documents) || !Array.isArray(preview.moves)) throw new Error("The preview could not be verified. Cancel and try again.");
      if (preview.truncated) throw new Error("The preview is partial. Open a smaller workspace before organizing files.");
      const draftPaths = recoveryBlockers(preview);
      if (draftPaths.length) throw new Error(`Review recovery drafts before organizing files: ${draftPaths.join(", ")}. Cancel, open those notes, and restore or discard each draft.`);
      for (const saved of operation.documents) {
        const current = preview.documents.find((doc) => doc.path === saved.path);
        if (!current || current.revision !== saved.revision) throw new Error("An open note changed on disk. Cancel and refresh before organizing files.");
      }
      const index = kit.buildLinkIndex(preview.documents.map((doc) => ({ ...doc, text: normalizeText(doc.text) })));
      if (index.partial) throw new Error("The link index is partial. Open a smaller workspace before organizing files.");
      operation.preview = preview; operation.plan = kit.planRelocation(index, preview.moves);
      operation.stage = "review";
      renderOrganizationReview(operation);
      el("workspace-organization-dialog").close(); el("workspace-organization-review").showModal();
      el("workspace-organization-review-cancel").focus();
    } catch (error) {
      if (state.organization !== operation || request !== state.organizationRequest) return;
      operation.stage = "choose"; setOrganizationChoosing(true);
      el("workspace-organization-error").textContent = error.message;
    }
  }

  function renderOrganizationReview(operation) {
    const { preview, plan } = operation, rename = operation.action === "rename";
    el("workspace-organization-review-title").textContent = rename ? "Review rename" : "Review move";
    el("workspace-organization-from").textContent = preview.source;
    el("workspace-organization-to").textContent = preview.destination;
    el("workspace-organization-counts").textContent = plan.blocked?.length
      ? `${plan.references} references need attention. ${plan.files} writable ${plan.files === 1 ? "file" : "files"} with changes; protected references are listed below.`
      : `${plan.references} ${plan.references === 1 ? "reference" : "references"} in ${plan.files} ${plan.files === 1 ? "file" : "files"} to update.`;
    const warnings = el("workspace-organization-warnings"); warnings.replaceChildren();
    for (const text of [...(preview.warnings || []), ...(plan.warnings || [])]) {
      const p = document.createElement("p"); p.textContent = typeof text === "string" ? text : text.reason || "A link needs review."; warnings.append(p);
    }
    for (const blocked of plan.blocked || []) {
      const p = document.createElement("p"); p.textContent = `${blocked.path}: ${blocked.count} protected ${blocked.count === 1 ? "reference" : "references"}. ${blocked.reason}`; warnings.append(p);
    }
    const edits = el("workspace-organization-edits"); edits.replaceChildren();
    if (!plan.patches.length) { const p = document.createElement("p"); p.textContent = "No writable link changes are available."; edits.append(p); }
    for (const patch of plan.patches) {
      const details = document.createElement("details"); details.open = true;
      const summary = document.createElement("summary"); summary.textContent = `${patch.path} (${patch.edits.length})`;
      details.append(summary);
      for (const edit of patch.edits) {
        const row = document.createElement("div"); row.className = "organization-edit";
        const before = document.createElement("code"), after = document.createElement("code"), arrow = document.createElement("span");
        before.textContent = edit.before; after.textContent = edit.after; arrow.textContent = "→"; arrow.setAttribute("aria-label", "changes to");
        row.append(before, arrow, after); details.append(row);
      }
      edits.append(details);
    }
    el("workspace-organization-review-error").textContent = "";
    el("workspace-organization-decline").textContent = rename ? "Rename without updating links" : "Move without updating links";
    el("workspace-organization-repair").textContent = rename ? "Update links and rename" : "Update links and move";
    el("workspace-organization-repair").disabled = !plan.safe || !plan.patches.length || Boolean(plan.blocked?.length);
    el("workspace-organization-decline").disabled = false;
  }

  function mappedOrganizationPath(path, preview) {
    if (!path) return path;
    if (path === preview.source) return preview.destination;
    if (preview.kind === "directory" && path.startsWith(`${preview.source}/`)) return preview.destination + path.slice(preview.source.length);
    return preview.moves.find((move) => move.from === path)?.to || path;
  }

  function relocateSelection(selection, edits, length) {
    const map = (position) => {
      let delta = 0;
      for (const edit of [...edits].sort((a, b) => a.from - b.from)) {
        if (position < edit.from) break;
        if (position <= edit.to) return Math.min(length, edit.from + delta + Math.min(position - edit.from, edit.after.length));
        delta += edit.after.length - (edit.to - edit.from);
      }
      return Math.max(0, Math.min(length, position + delta));
    };
    return kit.EditorSelection.create(selection.ranges.map((range) => kit.EditorSelection.range(map(range.anchor), map(range.head))), selection.mainIndex);
  }

  function adoptOrganization(operation, result) {
    const preview = operation.preview, values = new Map(result.documents.map((doc) => [doc.path, doc]));
    const repaired = new Map(operation.appliedRepair ? operation.plan.patches.map((patch) => [patch.path, patch.edits]) : []);
    for (const [path] of state.documents) {
      const nextPath = mappedOrganizationPath(path, preview);
      if ((nextPath !== path || repaired.has(path)) && !values.has(nextPath)) throw new Error("Updated document information is missing.");
    }
    const nextDocuments = new Map();
    for (const [path, doc] of state.documents) {
      const nextPath = mappedOrganizationPath(path, preview), value = values.get(nextPath);
      const selection = doc.editorState.selection, originalText = textOf(doc);
      doc.path = nextPath;
      if (value) for (const key of ["id", "name", "revision", "editable", "reason", "bom", "newline", "recovery_key"]) doc[key] = value[key];
      else if (nextPath !== path) doc.name = basename(nextPath);
      const text = value ? normalizeText(value.text) : originalText;
      doc.savedText = text; doc.external = null; doc.recovery = null;
      if (text !== originalText) {
        doc.editorState = makeEditorState(doc, text);
        doc.editorState = doc.editorState.update({ selection: relocateSelection(selection, repaired.get(path) || [], text.length) }).state;
      } else {
        doc.editorState = doc.editorState.update({ effects: [
          doc.editability.reconfigure([kit.EditorState.readOnly.of(!canEdit(doc)), kit.EditorView.editable.of(canEdit(doc))]),
          doc.identityAttributes.reconfigure(kit.EditorView.contentAttributes.of({ "aria-label": `Edit ${doc.name}`, spellcheck: "true" })),
        ] }).state;
      }
      nextDocuments.set(nextPath, doc);
    }
    state.documents = nextDocuments;
    state.active = mappedOrganizationPath(state.active, preview);
    state.history = state.history.map((path) => mappedOrganizationPath(path, preview));
    state.treeFocus = mappedOrganizationPath(state.treeFocus, preview);
    state.expanded = new Set([...state.expanded].map((path) => mappedOrganizationPath(path, preview)));
    state.localPinned = mappedOrganizationPath(state.localPinned, preview);
    state.snapshot = { ...state.snapshot, documents: preview.documents.map((doc) => {
      const path = mappedOrganizationPath(doc.path, preview), value = values.get(path);
      return value ? { ...value, text: normalizeText(value.text) } : { ...doc, path, text: normalizeText(doc.text) };
    }) };
    for (const mode of ["local", "global"]) {
      for (const name of ["folder", "search"]) {
        const control = graphControl(mode, name); if (!control || !control.value) continue;
        const path = mappedOrganizationPath(control.value, preview);
        if (path !== control.value) {
          if (name === "folder" && ![...control.options].some((option) => option.value === path)) control.add(new Option(path, path));
          control.value = path;
        }
      }
    }
    state.graphSignatures.clear();
    if (active() && state.view) state.view.setState(active().editorState);
    applyWorkspace(result.workspace); rebuildLinks(true); renderActive();
    const focus = state.treeFocus;
    if (state.active) reveal(state.active);
    if (focus) reveal(focus);
  }

  async function applyOrganization(repair) {
    const operation = state.organization;
    if (!operation || operation.stage !== "review" || !organizationUnchanged(operation)) return;
    const { plan, preview } = operation;
    if (repair && (!plan.safe || !plan.patches.length || plan.blocked?.length)) return;
    try {
      const drafts = recoveryBlockers(preview);
      if (drafts.length) throw new Error(`Review recovery drafts before organizing files: ${drafts.join(", ")}.`);
    } catch (error) { el("workspace-organization-review-error").textContent = error.message; return; }
    operation.stage = "apply"; operation.appliedRepair = repair;
    for (const id of ["workspace-organization-repair", "workspace-organization-decline", "workspace-organization-review-cancel"]) el(id).disabled = true;
    el("workspace-organization-review-error").textContent = "Applying the reviewed changes…";
    let applied = false;
    try {
      const result = await api("workspace/relocate", { workspace_id: operation.workspaceId, token: preview.token, repair, patches: repair ? plan.patches : [] });
      applied = true;
      if (!organizationUnchanged(operation)) throw new Error("The workspace changed while the operation was running.");
      if (!result.workspace || result.workspace.id !== operation.workspaceId || !Array.isArray(result.documents)) throw new Error("The operation receipt could not be verified.");
      adoptOrganization(operation, result);
      operation.stage = "complete"; finishOrganization();
      message(repair ? "Files organized and links updated. History restarted only in notes whose link text changed." : "Files organized. Existing link text was left unchanged.");
      refreshIndex();
    } catch (error) {
      if (state.organization !== operation) return;
      operation.stage = "failed";
      state.requiresReopen = true;
      el("workspace-organization-review-error").textContent = applied
        ? "Files changed, but the view could not refresh safely. Cancel and reopen this folder before editing."
        : `${error.message} The operation result could not be verified. Cancel and reopen this folder before editing.`;
      el("workspace-organization-review-cancel").disabled = false;
    }
  }

  function finishOrganization() {
    const operation = state.organization;
    if (!operation || operation.stage === "apply") return;
    state.organization = null; state.organizationRequest += 1;
    el("workspace-organization-dialog").close(); el("workspace-organization-review").close();
    screen.inert = false; document.querySelector(".workspace-rail").inert = false;
    if (state.requiresReopen) {
      for (const doc of state.documents.values()) doc.editorState = doc.editorState.update({ effects: doc.editability.reconfigure([kit.EditorState.readOnly.of(true), kit.EditorView.editable.of(false)]) }).state;
      if (active() && state.view) state.view.setState(active().editorState);
      renderActive();
    }
    for (const id of ["workspace-organization-repair", "workspace-organization-decline", "workspace-organization-review-cancel"]) el(id).disabled = false;
    renderOrganizationControl();
    if (operation.previousFocus?.isConnected) operation.previousFocus.focus();
    else if (state.treeFocus) renderTree(true);
  }

  function navigateHistory(delta) {
    const index = state.historyIndex + delta;
    if (index >= 0 && index < state.history.length) openDocument(state.history[index], { historyIndex: index });
  }

  function setupResize() {
    const resizer = el("workspace-resizer");
    const resize = (width) => {
      const value = Math.max(200, Math.min(420, width));
      screen.style.setProperty("--sidebar-width", `${value}px`); resizer.setAttribute("aria-valuenow", String(value));
      resizeGraphs();
    };
    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || window.innerWidth < 700) return;
      event.preventDefault(); resizer.setPointerCapture(event.pointerId);
      resizer.classList.add("resizing");
      const move = (moveEvent) => resize(moveEvent.clientX - 44);
      const finish = () => { resizer.classList.remove("resizing"); resizer.removeEventListener("pointermove", move); resizer.removeEventListener("pointerup", finish); resizer.removeEventListener("pointercancel", finish); state.view?.requestMeasure(); };
      resizer.addEventListener("pointermove", move); resizer.addEventListener("pointerup", finish); resizer.addEventListener("pointercancel", finish);
    });
    resizer.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      resize(event.key === "Home" ? 200 : event.key === "End" ? 420 : Number(resizer.getAttribute("aria-valuenow")) + (event.key === "ArrowLeft" ? -10 : 10));
      state.view?.requestMeasure();
    });
  }


  fillIcons(); setSidebar(state.sidebarHidden);
  el("show-workspace").addEventListener("click", () => { state.graphActive = false; showWorkspace(); renderActive(); });
  el("workspace-graph-open").addEventListener("click", showGlobalGraph);
  el("show-mapping").addEventListener("click", showMapping);
  if (!kit?.EditorView || !kit?.renderMarkdown) { message("The editor could not load. Restart the local app to try again.", true); return; }
  for (const id of ["rail-open-folder", "workspace-open-folder", "workspace-root", "workspace-welcome-open"]) el(id).addEventListener("click", showFolderDialog);
  el("workspace-sidebar-close").addEventListener("click", () => setSidebar(true));
  el("workspace-sidebar-open").addEventListener("click", () => setSidebar(false));
  el("workspace-confirm-cancel").addEventListener("click", () => finishConfirmation(false));
  el("workspace-confirm-accept").addEventListener("click", () => finishConfirmation(true));
  el("workspace-confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); finishConfirmation(false); });
  el("workspace-file-actions").addEventListener("click", () => showFileActions());
  el("workspace-file-menu-close").addEventListener("click", () => closeFileActions());
  el("workspace-file-menu").addEventListener("cancel", (event) => { event.preventDefault(); closeFileActions(); });
  for (const [id, action] of [["workspace-file-rename", "rename"], ["workspace-file-move", "move"]]) el(id).addEventListener("click", () => {
    const entry = state.workspace?.entries.find((row) => row.path === state.organizationMenu?.path);
    beginOrganization(action, entry);
  });
  el("workspace-file-menu").addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const buttons = [...el("workspace-file-menu").querySelectorAll('[role="menuitem"]')].filter((button) => !button.disabled);
    if (!buttons.length) return;
    event.preventDefault(); const index = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
  });
  el("workspace-organization-form").addEventListener("submit", previewOrganization);
  for (const id of ["workspace-organization-cancel", "workspace-organization-review-cancel"]) el(id).addEventListener("click", finishOrganization);
  for (const id of ["workspace-organization-dialog", "workspace-organization-review"]) el(id).addEventListener("cancel", (event) => { event.preventDefault(); finishOrganization(); });
  el("workspace-organization-repair").addEventListener("click", () => applyOrganization(true));
  el("workspace-organization-decline").addEventListener("click", () => applyOrganization(false));
  el("workspace-move-search").addEventListener("input", () => { if (state.organization) { state.organization.folderIndex = 0; state.organization.parent = null; } renderMoveFolders(); });
  el("workspace-move-search").addEventListener("keydown", (event) => {
    const operation = state.organization;
    if (!operation || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Enter") {
      const folder = operation.folders?.[operation.folderIndex]; if (folder) operation.parent = folder.path;
    } else operation.folderIndex = Math.max(0, Math.min((operation.folders?.length || 1) - 1, operation.folderIndex + (event.key === "ArrowDown" ? 1 : -1)));
    renderMoveFolders();
  });
  for (const surface of [screen, document.querySelector(".workspace-rail")]) surface.addEventListener("click", (event) => {
    if (state.organization) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  el("workspace-folder-form").addEventListener("submit", openWorkspace);
  el("workspace-folder-browse").addEventListener("click", async () => {
    const button = el("workspace-folder-browse"), generation = state.folderDialogGeneration; button.disabled = true;
    el("workspace-folder-error").textContent = "Choose a folder in the system dialog.";
    try {
      const result = await api("pick-folder", { kind: "folder" });
      if (!el("workspace-folder-dialog").open || generation !== state.folderDialogGeneration) return;
      if (result.path) el("workspace-folder-path").value = result.path;
      el("workspace-folder-error").textContent = result.path ? "" : "Selection canceled. You can enter a full path.";
    } catch (error) { if (generation === state.folderDialogGeneration) el("workspace-folder-error").textContent = `${error.message} You can enter a full path.`; }
    finally { button.disabled = false; }
  });
  for (const button of document.querySelectorAll("[data-close-dialog]")) button.addEventListener("click", () => {
    if (state.opening || state.creating) return;
    el(button.dataset.closeDialog).close();
  });
  for (const dialog of document.querySelectorAll(".workspace-dialog")) dialog.addEventListener("cancel", (event) => { if (state.opening || state.creating) event.preventDefault(); });
  el("workspace-new-note").addEventListener("click", () => showCreate("file"));
  el("workspace-empty-create").addEventListener("click", () => showCreate("file"));
  el("workspace-new-folder").addEventListener("click", () => showCreate("directory"));
  el("workspace-create-form").addEventListener("submit", createEntry);
  el("workspace-sort").addEventListener("click", () => {
    state.descending = !state.descending;
    el("workspace-sort").title = el("workspace-sort").ariaLabel = state.descending ? "Sort ascending" : "Sort descending";
    renderTree();
  });
  el("workspace-collapse").addEventListener("click", () => { state.expanded.clear(); renderTree(); });
  el("workspace-reveal").addEventListener("click", () => { if (active()) { setSidebar(false); reveal(active().path, true); } });
  el("workspace-back").addEventListener("click", () => navigateHistory(-1));
  el("workspace-forward").addEventListener("click", () => navigateHistory(1));
  el("workspace-save").addEventListener("click", () => saveDocument());
  for (const button of document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => setMode(button.dataset.mode));
  el("workspace-restore").addEventListener("click", restoreRecovery);
  el("workspace-discard-recovery").addEventListener("click", discardRecovery);
  el("workspace-use-disk").addEventListener("click", useDiskVersion);
  el("workspace-save-draft").addEventListener("click", saveEditedDraft);
  el("workspace-review-disk").addEventListener("click", () => {
    if (!active()?.external) return;
    renderDiskReview(active());
    el("workspace-disk-panel").hidden = false;
  });
  el("workspace-hide-disk").addEventListener("click", () => { el("workspace-disk-panel").hidden = true; });
  el("workspace-outline-toggle").addEventListener("click", () => toggleContext("outline"));
  el("workspace-backlinks-toggle").addEventListener("click", () => toggleContext("backlinks"));
  el("workspace-local-toggle").addEventListener("click", () => toggleContext("local"));
  for (const button of document.querySelectorAll("[data-context]")) button.addEventListener("click", () => { state.contextMode = button.dataset.context; state.contextOpen = true; renderContext(); state.view?.requestMeasure(); });
  el("workspace-context-close").addEventListener("click", () => { state.contextOpen = false; renderContext(); state.view?.requestMeasure(); });
  screen.addEventListener("click", (event) => {
    const anchor = event.target.closest("[data-note-destination]");
    if (!anchor) return;
    event.preventDefault(); navigateLink(anchor.dataset.noteDestination, anchor.dataset.noteKind || "wiki", anchor.dataset.sourcePath || active()?.path);
  });
  el("workspace-quick-open").addEventListener("click", () => { showWorkspace(); showQuickOpen(); });
  const quickDialog = el("workspace-quick-dialog");
  const outsideQuickDialog = (event) => {
    const rect = quickDialog.getBoundingClientRect();
    return event.target === quickDialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
  };
  quickDialog.addEventListener("pointerdown", (event) => { state.quickOutside = event.button === 0 && outsideQuickDialog(event); });
  quickDialog.addEventListener("pointercancel", () => { state.quickOutside = false; });
  quickDialog.addEventListener("click", (event) => {
    const dismiss = state.quickOutside && outsideQuickDialog(event); state.quickOutside = false;
    if (dismiss) closeQuickOpen();
  });
  quickDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeQuickOpen(); });
  el("workspace-quick-query").addEventListener("input", () => { state.quickIndex = 0; renderQuickResults(); });
  el("workspace-quick-query").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); state.quickIndex = Math.max(0, Math.min(state.quickResults.length - 1, state.quickIndex + (event.key === "ArrowDown" ? 1 : -1))); renderQuickResults();
    } else if (event.key === "Enter") {
      event.preventDefault(); const entry = state.quickResults[state.quickIndex];
      if (entry) { closeQuickOpen(); openDocument(entry.path); }
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!workspaceVisible() || !(event.metaKey || event.ctrlKey) || event.altKey || document.querySelector("dialog[open]")) return;
    const key = event.key.toLocaleLowerCase();
    if (key === "s") { event.preventDefault(); saveDocument(); }
    else if (key === "o") { event.preventDefault(); showQuickOpen(); }
    else if (key === "n") { event.preventDefault(); showCreate(event.shiftKey ? "directory" : "file"); }
  });
  window.addEventListener("beforeunload", (event) => {
    for (const doc of state.documents.values()) if (!doc.recovery) persistDraft(doc);
    if (window.workspaceHasUnsavedChanges()) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("resize", () => { state.view?.requestMeasure(); resizeGraphs(); });
  setupResize();
  window.setInterval(pollWorkspace, 5000);
})();
