/* Isolated adapter for the pinned official Zotero Reader web build. */
(function () {
  'use strict';
  const CHANNEL = 'az-zotero-reader-v1';
  const KEY = /^[A-Z0-9]{8}$/;

  function attachmentURL(value, origin) {
    const url = new URL(String(value), origin);
    if (url.origin !== origin || url.username || url.password || url.search || url.hash ||
        !/^\/api\/zotero\/attachments\/[A-Za-z0-9_-]{16,256}\/file$/.test(url.pathname)) {
      throw new Error('Only the attachment-bound local PDF endpoint is allowed');
    }
    return url.href;
  }

  function readerLocation(value) {
    if (!value || typeof value !== 'object') return undefined;
    const result = {};
    if (value.annotationID !== undefined) {
      if (!KEY.test(value.annotationID)) throw new Error('Invalid annotation target');
      result.annotationID = value.annotationID;
    }
    if (value.pageIndex !== undefined) {
      if (!Number.isInteger(value.pageIndex) || value.pageIndex < 0) throw new Error('Invalid physical page index');
      result.pageIndex = value.pageIndex;
    }
    return result;
  }

  function annotationList(value) {
    if (!Array.isArray(value) || value.length > 10000) throw new Error('Invalid annotation list');
    if (value.some(item => !item || !KEY.test(item.id))) throw new Error('Invalid annotation identity');
    const encoded = JSON.stringify(value);
    if (encoded.length > 16 * 1024 * 1024) throw new Error('Annotation payload is too large');
    return JSON.parse(encoded);
  }

  function createHost(win) {
    const origin = win.location.origin;
    const instanceId = new URLSearchParams(win.location.hash.slice(1)).get('instance');
    const status = win.document.getElementById('reader-host-status');
    const parent = win.parent;
    let reader = null;
    let initializing = false;
    let closed = false;
    let closing = false;
    let flushing = false;
    let requestedReadOnly = true;
    let appliedReadOnly = true;
    let failedWrite = false;
    let counter = 0;
    let queuedWrites = 0;
    let tail = Promise.resolve();
    const pending = new Map();
    const retainedWrites = new Map();
    const retaining = new Map();
    const confirmedAnnotations = new Map();
    const previewPending = new Map();
    let previewPreferences = {};

    function previewSettings(value) {
      if (value === undefined) return {};
      if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > 10000
          || Object.entries(value).some(([id, enabled]) => !KEY.test(id) || typeof enabled !== 'boolean')) throw new Error('Invalid annotation preview preferences');
      return { ...value };
    }

    function previewable(id) {
      const annotation = confirmedAnnotations.get(id);
      if (closed || closing || queuedWrites || failedWrite || previewPending.has(id)
          || !annotation || !['highlight', 'underline'].includes(annotation.type)
          || !Number.isSafeInteger(annotation.version) || annotation.version < 1
          || reader?._annotationManager?._unsavedAnnotations?.has(id)) return null;
      return annotation;
    }

    function previewMenu(event) {
      // This pinned upstream extension hook retains annotation IDs; the
      // rendered onOpenContextMenu callback deliberately does not expose them.
      const detail = event.detail;
      if (!reader || detail?.reader !== reader || detail.type !== 'createAnnotationContextMenu'
          || !Array.isArray(detail.params?.ids) || detail.params.ids.length !== 1 || typeof detail.append !== 'function') return;
      const id = detail.params.ids[0], annotation = previewable(id);
      if (!annotation) return;
      const enabled = previewPreferences[id] !== true;
      detail.append({
        label: enabled ? 'Show image in note' : 'Hide image in note',
        onCommand: () => {
          const current = previewable(id);
          if (!current || current.version !== annotation.version) return;
          previewPending.set(id, { version: current.version, enabled });
          emit('preview-request', { annotationId: id, annotationVersion: current.version, enabled });
        },
      });
    }

    function setReadOnly(value) {
      // Upstream also resets the active tool when this is called. A routine
      // save acknowledgement must not interrupt typing or the selected tool.
      if (reader && value !== appliedReadOnly) { reader.setReadOnly(value); appliedReadOnly = value; }
    }

    function retainUnsent() {
      const manager = reader?._annotationManager;
      const unsaved = [...(manager?._unsavedAnnotations || [])];
      for (const deleting of [false, true]) {
        const values = unsaved.filter(([, value]) => deleting ? value === null : value !== null);
        if (!values.length) continue;
        const operationId = `${instanceId}-retained-${++counter}`;
        retainedWrites.set(operationId, { sent: true }); retaining.set(operationId, values.map(([id]) => id));
        emit('retain-annotations', { operationId, deleting, ...(deleting ? { ids: values.map(([id]) => id) } : { annotations: values.map(([, value]) => value) }) });
      }
    }

    function emit(type, data = {}) {
      parent.postMessage({ channel: CHANNEL, instanceId, type, ...data }, origin);
    }

    function error(code, message) {
      if (status) { status.textContent = message; status.hidden = false; }
      emit('error', { code, message });
    }

    function operation(type, payload) {
      if (closed || failedWrite) return Promise.reject(new Error('Annotation writes require reconciliation'));
      queuedWrites += 1;
      // Keep typing available while the native save is in flight. On failure,
      // freeze editing and durably retain any newer upstream-buffered changes.
      const operationId = `${instanceId}-${++counter}`;
      retainedWrites.set(operationId, { sent: false });
      const execute = () => new Promise((resolve, reject) => {
        const timer = win.setTimeout(() => {
          // A missing acknowledgement is not evidence of failure or permission to retry.
          failedWrite = true;
          setReadOnly(true);
          retainUnsent();
          error('write-acknowledgement-pending', 'Annotation save is still pending. Keep this tab open while the application reconciles it.');
        }, 30000);
        pending.set(operationId, { resolve, reject, timer });
        retainedWrites.get(operationId).sent = true;
        emit(type, { operationId, ...payload });
      });
      const result = tail.then(execute);
      tail = result;
      const finished = () => {
        queuedWrites -= 1;
        if (!queuedWrites && !failedWrite && !closing && !flushing) setReadOnly(requestedReadOnly);
      };
      result.then(finished, finished);
      // Keep a rejected save observable without an unhandled promise from upstream deletes.
      result.catch(() => {});
      return result;
    }

    async function close(retainedIds = null) {
      if (!reader) { closed = true; emit('closed'); return; }
      closing = true;
      setReadOnly(true);
      // Upstream has no public flush API. This is pinned to the declared revision;
      // the compatibility test intentionally fails when this bridge changes.
      const manager = reader._annotationManager;
      if (!manager || typeof manager._triggerSaving !== 'function') {
        throw new Error('Reader cannot safely flush pending edits');
      }
      if (retainedIds !== null) {
        const ids = new Set(retainedIds);
        if (!failedWrite || pending.size || queuedWrites || manager._unsavedAnnotations?.size || !retainedWrites.size
            || retaining.size || [...retainedWrites].some(([id, value]) => !value.sent || !ids.has(id))) {
          throw new Error('Every reader change must be durably retained before closing');
        }
      } else {
        manager._skipAnnotationSavingDebounce = true;
        await manager._triggerSaving();
        await tail;
        if (failedWrite || pending.size || manager._unsavedAnnotations?.size || manager._savingInProgress) {
          throw new Error('Pending annotation changes must be reconciled before closing');
        }
      }
      reader.setSuspended(true);
      reader.trimMemory();
      for (const view of [reader._primaryView, reader._secondaryView]) {
        const loadingTask = view?._iframeWindow?.PDFViewerApplication?.pdfLoadingTask;
        view?.destroy?.();
        // Explicitly terminate PDF.js workers before iframe removal. Calling the
        // viewer's close() instead may invoke its unsupported form-download flow.
        if (loadingTask) await loadingTask.destroy();
      }
      closed = true;
      emit('closed'); // Parent must now remove the iframe: the complete teardown boundary.
    }

    async function flush() {
      if (!reader) return;
      const manager = reader._annotationManager;
      if (!manager || typeof manager._triggerSaving !== 'function' || failedWrite || retaining.size) throw new Error('Resolve or retain the pending annotation changes before closing the application');
      const previous = manager._skipAnnotationSavingDebounce;
      flushing = true; setReadOnly(true); manager._skipAnnotationSavingDebounce = true;
      try {
        for (let attempt = 0; attempt < 8; attempt++) {
          await manager._triggerSaving(); await tail; await Promise.resolve();
          if (!pending.size && !manager._unsavedAnnotations?.size && !manager._savingInProgress && !queuedWrites) return;
        }
        throw new Error('The PDF reader has not confirmed all annotation changes');
      } finally { flushing = false; manager._skipAnnotationSavingDebounce = previous; setReadOnly(failedWrite || closing || requestedReadOnly); }
    }

    async function handle(event) {
      const data = event.data;
      if (event.source !== parent || event.origin !== origin || !data ||
          data.channel !== CHANNEL || data.instanceId !== instanceId || closed) return;
      try {
        if (data.type === 'preview-result') {
          const request = previewPending.get(data.annotationId);
          if (!request || request.version !== data.annotationVersion) return;
          previewPending.delete(data.annotationId);
          if (data.success === true && data.enabled === request.enabled) previewPreferences[data.annotationId] = request.enabled;
          else if (typeof data.error === 'string' && status) { status.textContent = data.error; status.hidden = false; }
          return;
        }
        if (data.type === 'retention-result') {
          const ids = retaining.get(data.operationId);
          if (!ids) return;
          if (data.retained === true) {
            for (const id of ids) reader?._annotationManager?._unsavedAnnotations?.delete(id);
            retaining.delete(data.operationId);
          } else error('annotation-retention-failed', data.error || 'Newer edits are not yet retained. Keep this PDF open.');
          return;
        }
        if (data.type === 'operation-result') {
          const entry = pending.get(data.operationId);
          if (!entry) return;
          pending.delete(data.operationId);
          win.clearTimeout(entry.timer);
          if (data.success === true) { retainedWrites.delete(data.operationId); entry.resolve(); }
          else {
            failedWrite = true;
            setReadOnly(true);
            retainUnsent();
            const message = typeof data.error === 'string' ? data.error : 'Annotation write needs reconciliation';
            error('annotation-write-failed', message);
            entry.reject(new Error(message));
          }
          return;
        }
        if (data.type === 'init') {
          if (reader || initializing) throw new Error('Reader already initialized');
          const url = attachmentURL(data.document?.url, origin);
          const annotations = annotationList(data.annotations || []);
          previewPreferences = previewSettings(data.previewPreferences);
          for (const row of annotations) confirmedAnnotations.set(row.id, row);
          const location = readerLocation(data.location);
          if (typeof win.createReader !== 'function') throw new Error('The pinned Zotero reader build is unavailable');
          initializing = true;
          requestedReadOnly = data.readOnly !== false;
          appliedReadOnly = requestedReadOnly;
          reader = win.createReader({
            type: 'pdf', data: { url }, annotations, location,
            readOnly: requestedReadOnly,
            authorName: typeof data.authorName === 'string' ? data.authorName.slice(0, 200) : '',
            // An empty invented state makes upstream wait for every page (or a
            // timeout) before declaring a new document ready. Omit absent state.
            primaryViewState: data.viewState || undefined,
            enableReadAloud: false,
            sidebarOpen: true,
            sidebarView: 'annotations',
            onOpenContextMenu: params => {
              const unsupported = new Set(['reader-rotate-left', 'reader-rotate-right'].map(key => reader._getString(key)));
              const itemGroups = (params.itemGroups || []).map(group => group.filter(item => !unsupported.has(item.label))).filter(group => group.length);
              if (itemGroups.length) reader.openContextMenu({ ...params, itemGroups });
            },
            onSaveAnnotations: items => items.length ? operation('save-annotations', { annotations: annotationList(items) }) : tail,
            onDeleteAnnotations: ids => {
              if (ids.length) operation('delete-annotations', { ids: ids.slice() }).catch(() => {});
            },
            onChangeViewState: (state, primary) => emit('view-state', { viewState: state, primary }),
            onChangeSidebarWidth: width => emit('sidebar-state', { width }),
            onToggleSidebar: open => emit('sidebar-state', { open }),
            onChangeSidebarView: view => emit('sidebar-state', { view }),
            onOpenLink: url => {
              if (typeof url === 'string' && /^(https?:|zotero:)/i.test(url)) emit('open-link', { url });
            },
            onConfirm: () => false,
            onSaveCustomThemes: () => {},
            onSetLightTheme: () => {},
            onSetDarkTheme: () => {},
          });
          // At the pinned revision Reader.initializedPromise is never resolved;
          // PDFView's promise is the actual document-ready signal.
          if (!reader._primaryView?.initializedPromise) throw new Error('Reader readiness contract changed');
          if (await reader._primaryView.initializedPromise === false) throw new Error('Reader was closed before the PDF loaded');
          if (closed) return;
          if (status) status.hidden = true;
          emit('initialized');
          return;
        }
        if (!reader) throw new Error('Reader is not initialized');
        if (data.type === 'annotations') {
          if (failedWrite) throw new Error('Reconcile pending annotation writes before refreshing');
          const annotations = annotationList(data.annotations || []);
          if (data.previewPreferences !== undefined) previewPreferences = previewSettings(data.previewPreferences);
          const ids = data.deletedIds || [];
          if (!Array.isArray(ids) || ids.some(id => !KEY.test(id))) throw new Error('Invalid deleted annotation identity');
          const local = new Set(reader._annotationManager?._unsavedAnnotations?.keys() || []);
          const ownConfirmed = new Set(Array.isArray(data.confirmedIds) ? data.confirmedIds : []);
          const permissionUpdates = [];
          for (const value of annotations) if (ownConfirmed.has(value.id) && Number.isInteger(value.version)) {
            const draft = reader._annotationManager._unsavedAnnotations.get(value.id);
            if (draft) draft.version = value.version;
            const shown = reader._annotationManager._annotations?.find(row => row.id === value.id);
            if (shown) shown.version = value.version;
            if (shown && typeof value.readOnly === 'boolean' && shown.readOnly !== value.readOnly) {
              shown.readOnly = value.readOnly;
              if (draft) draft.readOnly = value.readOnly;
              permissionUpdates.push(shown);
            }
          }
          const shown = new Map((reader._annotationManager?._annotations || []).map(value => [value.id, value]));
          const updates = annotations.filter(value => !local.has(value.id) && !(ownConfirmed.has(value.id) && shown.has(value.id))
            && JSON.stringify(shown.get(value.id)) !== JSON.stringify(value));
          if (updates.length || permissionUpdates.length) reader.setAnnotations([...updates, ...permissionUpdates]);
          const removed = ids.filter(id => !local.has(id));
          if (removed.length) reader.unsetAnnotations(removed);
          for (const value of annotations) confirmedAnnotations.set(value.id, value);
          for (const id of ids) { confirmedAnnotations.delete(id); previewPending.delete(id); delete previewPreferences[id]; }
        }
        else if (data.type === 'preview-preferences') previewPreferences = previewSettings(data.previewPreferences);
        else if (data.type === 'navigate') await reader.navigate(readerLocation(data.location));
        else if (data.type === 'read-only') {
          requestedReadOnly = data.readOnly !== false;
          setReadOnly(failedWrite || closing || flushing || requestedReadOnly);
        }
        else if (data.type === 'suspend') reader.setSuspended(data.suspended !== false);
        else if (data.type === 'trim-memory') reader.trimMemory();
        else if (data.type === 'close') await close();
        else if (data.type === 'flush') {
          try { await flush(); emit('flush-result', { success: true }); }
          catch (err) { emit('flush-result', { success: false, error: err.message }); }
        }
        else if (data.type === 'close-retained') {
          if (!Array.isArray(data.operationIds) || !data.operationIds.length || data.operationIds.some(id => typeof id !== 'string')) throw new Error('Invalid retained-operation identities');
          await close(data.operationIds);
        }
        else throw new Error('Unsupported reader command');
      }
      catch (err) { error('reader-command-failed', err.message || 'Reader command failed'); }
    }

    if (!instanceId || !/^[A-Za-z0-9_-]{8,128}$/.test(instanceId) || parent === win || origin === 'null') {
      if (status) status.textContent = 'Open this reader from the application workspace.';
      return { handle, close };
    }
    win.addEventListener('message', handle);
    win.addEventListener('customEvent', previewMenu);
    emit('ready');
    return { handle, close };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { CHANNEL, attachmentURL, readerLocation, createHost };
  else createHost(window);
}());
