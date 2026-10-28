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
    let requestedReadOnly = true;
    let failedWrite = false;
    let counter = 0;
    let queuedWrites = 0;
    let tail = Promise.resolve();
    const pending = new Map();

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
      // Do not accumulate a second, unjournaled edit inside the upstream reader
      // while the first save is unresolved and may require a frame reload.
      reader?.setReadOnly(true);
      const operationId = `${instanceId}-${++counter}`;
      const execute = () => new Promise((resolve, reject) => {
        const timer = win.setTimeout(() => {
          // A missing acknowledgement is not evidence of failure or permission to retry.
          reader?.setReadOnly(true);
          error('write-acknowledgement-pending', 'Annotation save is still pending. Keep this tab open while the application reconciles it.');
        }, 30000);
        pending.set(operationId, { resolve, reject, timer });
        emit(type, { operationId, ...payload });
      });
      const result = tail.then(execute);
      tail = result;
      const finished = () => {
        queuedWrites -= 1;
        if (!queuedWrites && !failedWrite && !closing) reader?.setReadOnly(requestedReadOnly);
      };
      result.then(finished, finished);
      // Keep a rejected save observable without an unhandled promise from upstream deletes.
      result.catch(() => {});
      return result;
    }

    async function close() {
      if (!reader) { closed = true; emit('closed'); return; }
      closing = true;
      reader.setReadOnly(true);
      // Upstream has no public flush API. This is pinned to the declared revision;
      // the compatibility test intentionally fails when this bridge changes.
      const manager = reader._annotationManager;
      if (!manager || typeof manager._triggerSaving !== 'function') {
        throw new Error('Reader cannot safely flush pending edits');
      }
      manager._skipAnnotationSavingDebounce = true;
      await manager._triggerSaving();
      await tail;
      if (failedWrite || pending.size || manager._unsavedAnnotations?.size || manager._savingInProgress) {
        throw new Error('Pending annotation changes must be reconciled before closing');
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

    async function handle(event) {
      const data = event.data;
      if (event.source !== parent || event.origin !== origin || !data ||
          data.channel !== CHANNEL || data.instanceId !== instanceId || closed) return;
      try {
        if (data.type === 'operation-result') {
          const entry = pending.get(data.operationId);
          if (!entry) return;
          pending.delete(data.operationId);
          win.clearTimeout(entry.timer);
          if (data.success === true) entry.resolve();
          else {
            failedWrite = true;
            reader?.setReadOnly(true);
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
          const location = readerLocation(data.location);
          if (typeof win.createReader !== 'function') throw new Error('The pinned Zotero reader build is unavailable');
          initializing = true;
          requestedReadOnly = data.readOnly !== false;
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
          if (pending.size || failedWrite) throw new Error('Reconcile pending annotation writes before refreshing');
          const annotations = annotationList(data.annotations || []);
          const ids = data.deletedIds || [];
          if (!Array.isArray(ids) || ids.some(id => !KEY.test(id))) throw new Error('Invalid deleted annotation identity');
          reader.setAnnotations(annotations);
          reader.unsetAnnotations(ids);
        }
        else if (data.type === 'navigate') await reader.navigate(readerLocation(data.location));
        else if (data.type === 'read-only') {
          requestedReadOnly = data.readOnly !== false;
          reader.setReadOnly(failedWrite || closing || queuedWrites > 0 || requestedReadOnly);
        }
        else if (data.type === 'suspend') reader.setSuspended(data.suspended !== false);
        else if (data.type === 'trim-memory') reader.trimMemory();
        else if (data.type === 'close') await close();
        else throw new Error('Unsupported reader command');
      }
      catch (err) { error('reader-command-failed', err.message || 'Reader command failed'); }
    }

    if (!instanceId || !/^[A-Za-z0-9_-]{8,128}$/.test(instanceId) || parent === win || origin === 'null') {
      if (status) status.textContent = 'Open this reader from the application workspace.';
      return { handle, close };
    }
    win.addEventListener('message', handle);
    emit('ready');
    return { handle, close };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { CHANNEL, attachmentURL, readerLocation, createHost };
  else createHost(window);
}());
