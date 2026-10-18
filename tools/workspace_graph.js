import { FORCE_LIMITS, runForceLayout } from "./workspace_force.js";

const layouts = new WeakMap();
let requestId = 0;

export function coseLayoutOptions(nodeCount) {
  return {
    name: "cose", animate: true, refresh: 1, animationThreshold: 0,
    fit: false, randomize: true, nodeDimensionsIncludeLabels: false,
    nodeRepulsion: nodeCount <= 50 ? 12000 : 4000, idealEdgeLength: 55, nodeOverlap: 12,
    componentSpacing: 20, gravity: 1, edgeElasticity: nodeCount <= 50 ? 128 : 32,
    numIter: nodeCount > 700 ? 100 : 150,
    initialTemp: 120, coolingFactor: .96, minTemp: 1,
  };
}

export function fitGraph(graph, { padding = 45, maxZoom = 1.1 } = {}) {
  if (!graph || graph.destroyed() || !graph.nodes().length) return false;
  const width = graph.width(), height = graph.height();
  const bounds = graph.elements().boundingBox({ includeLabels: false });
  if (!(width > 1 && height > 1) || ![width, height, bounds.x1, bounds.y1, bounds.x2, bounds.y2, bounds.w, bounds.h].every(Number.isFinite)) return false;
  padding = Math.max(0, Math.min(Number(padding) || 0, (Math.min(width, height) - 1) / 2));
  maxZoom = Number.isFinite(maxZoom) && maxZoom > 0 ? maxZoom : 1.1;
  const zoom = Math.min((width - 2 * padding) / Math.max(1, bounds.w), (height - 2 * padding) / Math.max(1, bounds.h), maxZoom, graph.maxZoom());
  if (!(zoom > 0 && Number.isFinite(zoom))) return false;
  graph.minZoom(Math.min(.005, zoom));
  graph.viewport({
    zoom,
    pan: { x: (width - zoom * (bounds.x1 + bounds.x2)) / 2, y: (height - zoom * (bounds.y1 + bounds.y2)) / 2 },
  });
  return true;
}

const nextFrame = (callback) => typeof globalThis.requestAnimationFrame === "function"
  ? { animation: true, id: globalThis.requestAnimationFrame(callback) }
  : { animation: false, id: setTimeout(callback, 1000 / 60) };

function cancelFrame(frame) {
  if (!frame) return;
  if (frame.animation) globalThis.cancelAnimationFrame(frame.id);
  else clearTimeout(frame.id);
}

// Same-data calls preserve positions/viewport. Explicit Fit and resize are separate operations.
export function layoutGraph(graph, options = {}) {
  if (!graph || graph.destroyed()) throw new Error("A live graph is required for layout.");
  const signature = options.signature ?? JSON.stringify([
    graph.nodes().map((node) => node.id()),
    graph.edges().map((edge) => [edge.id(), edge.data("source"), edge.data("target")]),
  ]);
  const previous = layouts.get(graph);
  if (previous?.signature === signature && previous.interactive === Boolean(options.interactive) && previous.status !== "error" && !options.force) return previous.controller;
  previous?.controller.cancel();
  let resolveFinished;
  const finished = new Promise((resolve) => { resolveFinished = resolve; });
  const record = { signature, empty: !graph.nodes().length, interactive: Boolean(options.interactive), layout: null, worker: null, controls: null, indexes: null, requestId: null, pins: new Map(), watchdog: null, frame: null, renderFrame: null, pending: null, timer: null, settled: false, status: null, cancelled: false, viewportChanged: false, fitting: false, previewFitted: false, started: 0 };
  const initialViewport = { zoom: graph.zoom(), x: graph.pan("x"), y: graph.pan("y") };
  const current = () => layouts.get(graph) === record && !record.cancelled && !graph.destroyed();
  const viewportChanged = () => {
    if (record.fitting) return;
    if (graph.zoom() !== initialViewport.zoom || graph.pan("x") !== initialViewport.x || graph.pan("y") !== initialViewport.y) record.viewportChanged = true;
  };
  const pinEvent = (event) => {
    const node = event.target;
    if (node?.isNode?.()) record.controller.pinNode(node.id(), node.position());
  };
  const grabbed = (event) => options.interactive ? pinEvent(event) : record.controller.cancel();
  const freed = (event) => record.controller.releaseNode(event.target.id());
  const destroyed = () => record.controller.cancel();
  const cleanup = () => {
    graph.off("pan zoom", viewportChanged);
    graph.off("grab", grabbed);
    graph.off("drag", "node", pinEvent);
    graph.off("free", "node", freed);
    graph.off("destroy", destroyed);
    record.layout?.off("layoutstop", stopped);
    record.worker?.terminate();
    clearTimeout(record.watchdog);
    cancelFrame(record.renderFrame);
    record.pending = null;
  };
  const finish = (status, error = null) => {
    if (record.settled) return;
    record.settled = true;
    record.status = status;
    cleanup();
    const fitted = status === "complete" && current() && options.fit !== false && !record.viewportChanged ? fitGraph(graph, options) : false;
    const result = { status, fitted, elapsedMs: record.started ? performance.now() - record.started : 0, ...(error ? { error } : {}) };
    resolveFinished(result);
    if (status === "complete" && current()) options.onStop?.(result);
    if (status === "error" && current()) options.onError?.(error);
  };
  const stopped = () => {
    if (!current()) return;
    if (record.layout?.stopped) record.controller.cancel();
    else finish("complete");
  };
  const control = (type, id, point) => {
    const index = record.indexes?.get(id);
    if (index === undefined) return;
    if (record.worker) record.worker.postMessage({ type, requestId: record.requestId, index, ...(point || {}) });
    else record.controls?.[type](index, point);
  };
  const controller = {
    finished,
    get layout() { return record.layout; },
    get worker() { return record.worker; },
    pinNode(id, point) {
      if (!options.interactive || !current() || record.settled || typeof id !== "string" || !graph.getElementById(id).isNode()
          || !Number.isFinite(point?.x) || !Number.isFinite(point?.y)
          || !Number.isFinite(Math.fround(point.x)) || !Number.isFinite(Math.fround(point.y))) return false;
      const position = { x: point.x, y: point.y };
      record.viewportChanged = true;
      record.pins.set(id, position);
      graph.getElementById(id).position(position);
      control("pin", id, position);
      return true;
    },
    releaseNode(id) {
      if (!options.interactive || !current() || record.settled || typeof id !== "string" || !graph.getElementById(id).isNode()) return false;
      record.pins.delete(id);
      control("release", id);
      return true;
    },
    cancel() {
      if (record.settled) return false;
      record.cancelled = true;
      cancelFrame(record.frame);
      clearTimeout(record.timer);
      if (record.layout) {
        // CoSE performs a final refresh on its next frame after stop(); detach that stale refresh.
        record.layout.options.eles = graph.collection();
        record.layout.stop();
      }
      finish("cancelled");
      return true;
    },
  };
  record.controller = controller;
  layouts.set(graph, record);
  graph.on("pan zoom", viewportChanged);
  graph.on("grab", grabbed);
  if (options.interactive) { graph.on("drag", "node", pinEvent); graph.on("free", "node", freed); }
  graph.on("destroy", destroyed);
  if (!graph.nodes().length) { finish("complete"); return controller; }
  const forceLayout = () => {
    const nodes = graph.nodes(), indexes = new Map(nodes.map((node, index) => [node.id(), index]));
    record.indexes = indexes;
    const positions = new Float32Array(nodes.length * 2), links = new Uint32Array(graph.edges().length * 2);
    nodes.forEach((node, index) => { positions[index * 2] = node.position("x"); positions[index * 2 + 1] = node.position("y"); });
    graph.edges().forEach((edge, index) => { links[index * 2] = indexes.get(edge.data("source")); links[index * 2 + 1] = indexes.get(edge.data("target")); });
    const id = ++requestId;
    record.requestId = id;
    const watchWorker = () => {
      clearTimeout(record.watchdog);
      record.watchdog = setTimeout(() => { if (current() && !record.settled) finish("error", new Error("Graph layout timed out.")); }, 15000);
    };
    const queue = (frame) => {
      if (!current() || record.settled) return;
      if (!(frame.positions instanceof Float32Array) || frame.positions.length !== nodes.length * 2
          || frame.positions.some((position) => !Number.isFinite(position)) || typeof frame.done !== "boolean"
          || !Number.isInteger(frame.iteration) || frame.iteration < 0 || frame.iteration > FORCE_LIMITS.ticks) throw new Error("Invalid graph layout positions.");
      // The worker waits for our paint acknowledgement, so consumer delay is not solver inactivity.
      clearTimeout(record.watchdog);
      if (record.pending?.done) return;
      record.pending = frame;
      if (record.renderFrame) return;
      record.renderFrame = nextFrame(() => {
        record.renderFrame = null;
        if (!current() || record.settled) return;
        const next = record.pending;
        record.pending = null;
        try {
          graph.batch(() => nodes.positions((node, index) => record.pins.get(node.id()) || ({ x: next.positions[index * 2], y: next.positions[index * 2 + 1] })));
          if (!next.done && (!previous || previous.empty) && options.randomize !== false && options.fit !== false && !record.previewFitted && !record.viewportChanged) {
            record.fitting = true;
            try { record.previewFitted = fitGraph(graph, options); }
            finally { record.fitting = false; }
            if (record.previewFitted) Object.assign(initialViewport, { zoom: graph.zoom(), x: graph.pan("x"), y: graph.pan("y") });
          }
          if (next.done) finish("complete");
          else if (record.worker) {
            record.worker.postMessage({ type: "ack", requestId: id, iteration: next.iteration });
            watchWorker();
          }
        } catch (error) { finish("error", error); }
      });
    };
    const input = { type: "layout", requestId: id, positions, links, randomize: options.randomize !== false, iterations: options.iterations };
    if (typeof globalThis.Worker === "function") {
      record.worker = new Worker("/workspace-graph-worker.js");
      watchWorker();
      record.worker.onmessage = ({ data }) => {
        if (!current() || record.settled || data?.requestId !== id) return;
        try {
          if (data.type === "error") throw new Error("Graph layout could not finish.");
          if (data.type === "positions") queue(data);
        } catch (error) { finish("error", error); }
      };
      record.worker.onerror = () => { if (current() && !record.settled) finish("error", new Error("Graph layout worker could not start.")); };
      record.worker.postMessage(input, [positions.buffer, links.buffer]);
      for (const [nodeId, position] of record.pins) control("pin", nodeId, position);
    } else {
      runForceLayout(input, {
        cancelled: () => !current() || record.settled, onPositions: queue,
        onControls: (controls) => { record.controls = controls; for (const [nodeId, position] of record.pins) control("pin", nodeId, position); },
      })
        .catch((error) => { if (current() && !record.settled) finish("error", error); });
    }
  };
  record.frame = nextFrame(() => {
    // Starting after the frame gives the workspace a paint opportunity before layout initialization.
    record.timer = setTimeout(() => {
      if (!current()) return;
      try {
        record.started = performance.now();
        if (options.interactive || graph.nodes().length > 1000) { forceLayout(); return; }
        const config = coseLayoutOptions(graph.nodes().length);
        config.randomize = options.randomize !== false;
        if (Number.isInteger(options.iterations) && options.iterations > 0) config.numIter = Math.min(config.numIter, options.iterations);
        record.layout = graph.layout(config);
        record.layout.on("layoutstop", stopped);
        record.layout.run();
      } catch (error) { finish("error", error); }
    }, 0);
  });
  return controller;
}
