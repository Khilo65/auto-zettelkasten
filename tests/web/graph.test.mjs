import assert from "node:assert/strict";
import test from "node:test";
import { Worker as ThreadWorker } from "node:worker_threads";
import cytoscape from "cytoscape";
import { coseLayoutOptions, fitGraph, layoutGraph } from "../../tools/workspace_graph.js";
import { FORCE_LIMITS, runForceLayout } from "../../tools/workspace_force.js";

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function clusteredElements(count) {
  const groups = count === 450 ? 15 : 20, size = count / groups;
  const elements = Array.from({ length: count }, (_, at) => ({ data: { id: `n${at}`, label: `Concept ${at}` } }));
  let edge = 0;
  for (let group = 0; group < groups; group += 1) {
    for (let slot = 0; slot < size; slot += 1) for (const delta of [1, 3]) elements.push({ data: { id: `e${edge++}`, source: `n${slot * groups + group}`, target: `n${((slot + delta) % size) * groups + group}` } });
    elements.push({ data: { id: `e${edge++}`, source: `n${group}`, target: `n${(group + 1) % groups}` } });
  }
  return elements;
}

function graph(elements = [{ data: { id: "a" } }, { data: { id: "b" } }, { data: { id: "ab", source: "a", target: "b" } }]) {
  const cy = cytoscape({ headless: true, styleEnabled: true, elements, layout: { name: "preset" }, minZoom: .15, maxZoom: 5, style: [{ selector: "node", style: { width: 9, height: 9 } }] });
  // Headless has no DOM container; these public dimension methods stand in for its viewport.
  cy.width = () => 1000;
  cy.height = () => 720;
  return cy;
}

function seeded(seed, callback) {
  const original = Math.random;
  Math.random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
  try { return callback(); } finally { Math.random = original; }
}

function meanEdgeLength(cy) {
  return cy.edges().reduce((sum, edge) => {
    const from = edge.source().position(), to = edge.target().position();
    return sum + Math.hypot(to.x - from.x, to.y - from.y);
  }, 0) / cy.edges().length;
}

function assertFitted(cy, padding = 45) {
  for (const node of cy.nodes()) {
    const position = node.renderedPosition();
    const halfWidth = node.renderedWidth() / 2, halfHeight = node.renderedHeight() / 2;
    assert.ok(position.x - halfWidth >= padding - 2, `${node.id()} left clipped`);
    assert.ok(position.y - halfHeight >= padding - 2, `${node.id()} top clipped`);
    assert.ok(position.x + halfWidth <= cy.width() - padding + 2, `${node.id()} right clipped`);
    assert.ok(position.y + halfHeight <= cy.height() - padding + 2, `${node.id()} bottom clipped`);
  }
}

test("CoSE options yield every iteration and remain bounded for small graphs", () => {
  for (const count of [10, 150, 450, 1000]) {
    const options = coseLayoutOptions(count);
    assert.equal(options.name, "cose");
    assert.equal(options.animate, true);
    assert.equal(options.refresh, 1);
    assert.equal(options.fit, false);
    assert.ok(options.numIter > 0 && options.numIter <= 150);
  }
});

function compactInput(count, deltas = [1, 17]) {
  const positions = new Float32Array(count * 2), links = new Uint32Array(count * deltas.length * 2);
  for (let node = 0; node < count; node += 1) for (let edge = 0; edge < deltas.length; edge += 1) {
    const offset = (node * deltas.length + edge) * 2;
    links[offset] = node; links[offset + 1] = (node + deltas[edge]) % count;
  }
  return { positions, links, randomize: true };
}

async function until(check, milliseconds = 2000) {
  const deadline = performance.now() + milliseconds;
  while (!check()) { assert.ok(performance.now() < deadline, "Graph worker did not start"); await sleep(5); }
}

function controlledWorker(t) {
  const original = { Worker: globalThis.Worker, requestAnimationFrame: globalThis.requestAnimationFrame, cancelAnimationFrame: globalThis.cancelAnimationFrame };
  const workers = [], frames = new Map();
  let sequence = 0;
  globalThis.Worker = class {
    constructor() { this.messages = []; workers.push(this); }
    postMessage(message) { this.messages.push(message); if (message.type === "layout") this.input = message; }
    terminate() { this.terminated = true; }
    emit(message) { this.onmessage?.({ data: { requestId: this.input.requestId, ...message } }); }
  };
  globalThis.requestAnimationFrame = (callback) => { frames.set(++sequence, callback); return sequence; };
  globalThis.cancelAnimationFrame = (id) => frames.delete(id);
  t.after(() => {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  });
  return { workers, flush: () => { const pending = [...frames.values()]; frames.clear(); for (const callback of pending) callback(performance.now()); } };
}

test("Worker geometry is coalesced per paint and final coordinates are never dropped", async (t) => {
  const harness = controlledWorker(t), cy = graph(clusteredElements(1100));
  let positionEvents = 0;
  cy.on("position", "node", () => { positionEvents += 1; });
  try {
    const controller = layoutGraph(cy);
    harness.flush(); await until(() => harness.workers[0]?.input);
    const worker = harness.workers[0];
    const positions = (offset) => Float32Array.from({ length: 2200 }, (_, index) => index + offset);
    worker.emit({ type: "positions", positions: positions(10), iteration: 1, done: false });
    worker.emit({ type: "positions", positions: positions(20), iteration: 2, done: false });
    assert.equal(positionEvents, 0, "Geometry waits for one paint opportunity instead of blocking each message");
    harness.flush();
    assert.equal(positionEvents, 1100, "Only the newest geometry batch is applied");
    assert.equal(cy.$id("n1").position("x"), 22);
    assert.deepEqual(worker.messages.at(-1), { type: "ack", requestId: worker.input.requestId, iteration: 2 });
    worker.emit({ type: "positions", positions: positions(30), iteration: 3, done: false });
    worker.emit({ type: "positions", positions: positions(40), iteration: 4, done: true });
    harness.flush();
    assert.equal((await controller.finished).status, "complete");
    assert.equal(cy.$id("n1").position("x"), 42);
    assert.equal(positionEvents, 2200);
    assert.equal(worker.terminated, true);
    assert.equal(cy.nodes().length, 1100);
    assert.equal(cy.edges().length, 2220);
    assertFitted(cy);
  } finally { cy.destroy(); }
});

test("Worker timeout measures inactivity, excludes a pending paint, and still detects a stalled solver", async (t) => {
  const harness = controlledWorker(t), cy = graph(clusteredElements(1100));
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let errors = 0;
  try {
    const controller = layoutGraph(cy, { onError: () => { errors += 1; } });
    harness.flush(); t.mock.timers.tick(0);
    const worker = harness.workers[0];
    t.mock.timers.tick(11000);
    assert.equal(errors, 0, "A healthy worker is not cut off by the old absolute ten-second deadline");
    worker.emit({ type: "positions", positions: new Float32Array(2200), iteration: 1, done: false });
    t.mock.timers.tick(60000);
    assert.equal(errors, 0, "A worker waiting for the consumer's paint is not stalled");
    harness.flush();
    t.mock.timers.tick(14999);
    assert.equal(errors, 0, "Applying geometry gives the solver a new bounded response window");
    t.mock.timers.tick(1);
    const result = await controller.finished;
    assert.equal(result.status, "error");
    assert.match(result.error.message, /timed out/);
    assert.equal(errors, 1);
    assert.equal(worker.terminated, true);
  } finally { cy.destroy(); }
});

test("Errored geometry retries unchanged topology and cancelled paint cannot mutate or fit", async (t) => {
  const harness = controlledWorker(t), cy = graph(clusteredElements(1100));
  try {
    const failed = layoutGraph(cy, { signature: "same" });
    harness.flush(); await until(() => harness.workers[0]?.input);
    harness.workers[0].emit({ type: "error" });
    assert.equal((await failed.finished).status, "error");
    const retry = layoutGraph(cy, { signature: "same" });
    assert.notEqual(retry, failed, "The same topology can retry after an actual error");
    harness.flush(); await until(() => harness.workers[1]?.input);
    harness.workers[1].emit({ type: "positions", positions: new Float32Array(2200).fill(123), iteration: 1, done: true });
    cy.zoom(.6); cy.pan({ x: 81, y: 94 });
    const before = cy.nodes().map((node) => ({ ...node.position() }));
    retry.cancel(); harness.flush();
    assert.equal((await retry.finished).status, "cancelled");
    assert.deepEqual(cy.nodes().map((node) => node.position()), before);
    assert.equal(cy.zoom(), .6); assert.deepEqual(cy.pan(), { x: 81, y: 94 });
    assert.equal(layoutGraph(cy, { signature: "same" }), retry, "A deliberate grab/cancellation is not restarted by a routine render");
  } finally { cy.destroy(); }
});

test("Interactive layouts queue validated pins before worker startup and protect them from late geometry", async (t) => {
  const harness = controlledWorker(t), cy = graph();
  try {
    const controller = layoutGraph(cy, { interactive: true, fit: false });
    assert.equal(typeof controller.pinNode, "function");
    assert.equal(controller.pinNode("missing", { x: 1, y: 2 }), false);
    assert.equal(controller.pinNode("ab", { x: 1, y: 2 }), false);
    assert.equal(controller.pinNode("a", { x: Infinity, y: 2 }), false);
    assert.equal(controller.pinNode("a", { x: 1e300, y: 2 }), false);
    assert.equal(controller.pinNode("a", { x: 400, y: 500 }), true);
    assert.deepEqual(cy.$id("a").position(), { x: 400, y: 500 });
    harness.flush(); await until(() => harness.workers[0]?.input);
    const worker = harness.workers[0];
    assert.equal(controller.layout, null, "Interactive small graphs use the same bounded worker solver");
    assert.deepEqual(worker.messages.at(-1), { type: "pin", requestId: worker.input.requestId, index: 0, x: 400, y: 500 });
    worker.emit({ type: "positions", positions: new Float32Array([1, 2, 3, 4]), iteration: 1, done: false });
    assert.equal(controller.pinNode("a", { x: 600, y: 700 }), true);
    harness.flush();
    assert.deepEqual(cy.$id("a").position(), { x: 600, y: 700 }, "A buffered frame cannot overwrite a newer drag position");
    assert.equal(controller.releaseNode("missing"), false);
    assert.equal(controller.releaseNode("a"), true);
    assert.deepEqual(worker.messages.at(-1), { type: "release", requestId: worker.input.requestId, index: 0 });
    assert.equal(controller.pinNode("b", { x: 700, y: 800 }), true);
    worker.emit({ type: "positions", positions: new Float32Array([8, 9, 10, 11]), iteration: 2, done: true });
    harness.flush();
    assert.equal((await controller.finished).status, "complete");
    assert.deepEqual(cy.$id("a").position(), { x: 8, y: 9 });
    assert.deepEqual(cy.$id("b").position(), { x: 700, y: 800 }, "Final geometry also preserves a held pin");
    assert.equal(controller.pinNode("a", { x: 1, y: 2 }), false);
    assert.equal(controller.releaseNode("a"), false);
    assert.equal(cy.nodes().length, 2); assert.equal(cy.edges().length, 1);
  } finally { cy.destroy(); }
});

test("Selecting interactive layout replaces an unchanged noninteractive controller", async (t) => {
  const harness = controlledWorker(t), cy = graph();
  try {
    const plain = layoutGraph(cy);
    const interactive = layoutGraph(cy, { interactive: true, fit: false });
    assert.notEqual(interactive, plain);
    assert.equal((await plain.finished).status, "cancelled");
    harness.flush(); await until(() => harness.workers[0]?.input);
    assert.equal(interactive.pinNode("a", { x: 1, y: 2 }), true);
    interactive.cancel();
  } finally { cy.destroy(); }
});

test("Interactive grab/drag/free preserve the live job while cancelled controls cannot reach its replacement", async (t) => {
  const harness = controlledWorker(t), cy = graph();
  try {
    const first = layoutGraph(cy, { interactive: true, fit: false });
    cy.$id("a").emit("grab");
    harness.flush(); await until(() => harness.workers[0]?.input);
    assert.equal(harness.workers[0].terminated, undefined, "An interactive grab pins instead of cancelling");
    cy.$id("a").position({ x: 123, y: 456 }); cy.$id("a").emit("drag");
    assert.deepEqual(harness.workers[0].messages.at(-1), { type: "pin", requestId: harness.workers[0].input.requestId, index: 0, x: 123, y: 456 });
    cy.$id("a").emit("free");
    assert.equal(harness.workers[0].messages.at(-1).type, "release");
    first.cancel();
    const sent = harness.workers[0].messages.length;
    assert.equal(first.pinNode("a", { x: 1, y: 2 }), false);
    assert.equal(first.releaseNode("a"), false);
    assert.equal(harness.workers[0].messages.length, sent);
    const replacement = layoutGraph(cy, { interactive: true, force: true, fit: false });
    harness.flush(); await until(() => harness.workers[1]?.input);
    assert.notEqual(harness.workers[1].input.requestId, harness.workers[0].input.requestId);
    assert.equal(replacement.pinNode("b", { x: 7, y: 8 }), true);
    replacement.cancel();
    assert.equal((await first.finished).status, "cancelled");
    assert.equal((await replacement.finished).status, "cancelled");
  } finally { cy.destroy(); }
});

test("An initial worker preview fits settling geometry without consuming camera or drag intent or fitting restarts", async (t) => {
  const harness = controlledWorker(t);
  for (const action of ["none", "pan", "drag"]) {
    const cy = graph();
    let fits = 0;
    const viewport = cy.viewport.bind(cy);
    cy.viewport = (...args) => { fits += 1; return viewport(...args); };
    try {
      const priorWorkers = harness.workers.length;
      const controller = layoutGraph(cy, { interactive: true });
      harness.flush(); await until(() => harness.workers.length > priorWorkers && harness.workers.at(-1)?.input);
      const worker = harness.workers.at(-1);
      worker.emit({ type: "positions", positions: new Float32Array([-500, -400, 500, 400]), iteration: 1, done: false });
      harness.flush();
      assert.equal(fits, 1, "The full graph becomes visible while it is still settling");
      const previewCamera = { zoom: cy.zoom(), pan: { ...cy.pan() } };
      if (action === "pan") { cy.zoom(.6); cy.pan({ x: 81, y: 94 }); }
      if (action === "drag") assert.equal(controller.pinNode("a", { x: 100, y: 200 }), true);
      worker.emit({ type: "positions", positions: new Float32Array([-600, -500, 600, 500]), iteration: 2, done: true });
      harness.flush();
      const result = await controller.finished;
      assert.equal(result.fitted, action === "none");
      assert.equal(fits, action === "none" ? 2 : 1, "The preview's own viewport events are not mistaken for user intent");
      if (action === "pan") { assert.equal(cy.zoom(), .6); assert.deepEqual(cy.pan(), { x: 81, y: 94 }); }
      if (action === "drag") assert.deepEqual({ zoom: cy.zoom(), pan: cy.pan() }, previewCamera, "Dragging a node never resets the camera");
      const restart = layoutGraph(cy, { interactive: true, force: true, randomize: false, fit: false });
      harness.flush(); await until(() => harness.workers.at(-1) !== worker && harness.workers.at(-1)?.input);
      const next = harness.workers.at(-1), before = fits;
      next.emit({ type: "positions", positions: new Float32Array([-700, -600, 700, 600]), iteration: 1, done: false });
      harness.flush();
      assert.equal(fits, before, "Drag relaxation never changes the camera");
      restart.cancel();
    } finally { cy.destroy(); }
  }
});

test("Shared solver pin/release controls are bounded, retain all output and stop accepting input when settled", async () => {
  let controls, final;
  const result = await runForceLayout({ positions: new Float32Array(4), links: new Uint32Array([0, 1]), iterations: 3 }, {
    onControls: (value) => {
      controls = value;
      assert.equal(controls.pin(2, { x: 1, y: 2 }), false);
      assert.equal(controls.pin(0, { x: NaN, y: 2 }), false);
      assert.equal(controls.pin(0, { x: 1e300, y: 2 }), false);
      assert.equal(controls.release(-1), false);
      assert.equal(controls.pin(0, { x: 400, y: 500 }), true);
    },
    onPositions: (frame) => { if (frame.done) final = frame; },
  });
  assert.ok(controls, "Live controls are available before the first simulation tick");
  assert.equal(result.status, "complete"); assert.equal(result.iteration, 3);
  assert.deepEqual([...final.positions.slice(0, 2)], [400, 500]);
  assert.equal(final.positions.length, 4); assert.equal(final.positions.every(Number.isFinite), true);
  assert.equal(controls.pin(0, { x: 1, y: 2 }), false); assert.equal(controls.release(0), false);
  await runForceLayout({ positions: new Float32Array(4), links: new Uint32Array([0, 1]), iterations: 3 }, {
    onControls: (value) => { assert.equal(value.pin(0, { x: 400, y: 500 }), true); assert.equal(value.release(0), true); },
    onPositions: (frame) => { if (frame.done) final = frame; },
  });
  assert.notDeepEqual([...final.positions.slice(0, 2)], [400, 500], "Released nodes participate in the remaining bounded relaxation");
});

test("The first populated preview fits when a graph was opened before its index arrived", async (t) => {
  const harness = controlledWorker(t), cy = graph([]);
  let fits = 0;
  const viewport = cy.viewport.bind(cy);
  cy.viewport = (...args) => { fits += 1; return viewport(...args); };
  try {
    assert.equal((await layoutGraph(cy, { interactive: true }).finished).status, "complete");
    cy.add([{ data: { id: "a" } }, { data: { id: "b" } }, { data: { id: "ab", source: "a", target: "b" } }]);
    const controller = layoutGraph(cy, { interactive: true });
    harness.flush(); await until(() => harness.workers[0]?.input);
    harness.workers[0].emit({ type: "positions", positions: new Float32Array([-500, -400, 500, 400]), iteration: 1, done: false });
    harness.flush();
    assert.equal(fits, 1, "An empty index is not an earlier populated preview");
    controller.cancel();
  } finally { cy.destroy(); }
});

test("Barnes-Hut core retains every vertex/edge and yields without a multi-second event-loop stall at 7500 nodes", async () => {
  const input = compactInput(7500), originalLinks = input.links.slice();
  const frames = [];
  let last = performance.now(), maxLag = 0, heartbeats = 0;
  const heartbeat = setInterval(() => {
    const now = performance.now(); maxLag = Math.max(maxLag, now - last - 16); last = now; heartbeats += 1;
  }, 16);
  try {
    const result = await runForceLayout({ ...input, iterations: 8 }, { onPositions: (frame) => frames.push(frame) });
    assert.equal(result.status, "complete");
    assert.equal(result.iteration, 8);
    assert.ok(heartbeats > 0);
    assert.ok(maxLag < 1000, `Largest event-loop delay was ${maxLag.toFixed(0)}ms`);
    const final = frames.at(-1);
    assert.equal(final.done, true);
    assert.equal(final.positions.length, 15000);
    assert.equal(final.positions.every(Number.isFinite), true);
    assert.equal(new Set(Array.from({ length: 7500 }, (_, index) => `${final.positions[index * 2]},${final.positions[index * 2 + 1]}`)).size, 7500);
    assert.equal(input.positions.every((value) => value === 0), true, "Input positions are not mutated or transferred by the shared solver");
    assert.deepEqual(input.links, originalLinks, "D3 receives private link objects, preserving every original endpoint");
    assert.equal(FORCE_LIMITS.ticks, 120);
  } finally { clearInterval(heartbeat); }
});

test("Large graph fallback uses yielding force geometry, retains all elements, and fits once", async () => {
  const elements = clusteredElements(1100), baseline = graph(elements), force = graph(elements);
  try {
    baseline.layout({ name: "concentric", animate: false, fit: false, minNodeSpacing: 24 }).run();
    const controller = layoutGraph(force);
    const result = await controller.finished;
    assert.equal(controller.layout, null, "Large graphs never run the quadratic CoSE path");
    assert.equal(result.status, "complete");
    assert.equal(result.fitted, true);
    assert.equal(force.nodes().length, 1100);
    assert.equal(force.edges().length, elements.length - 1100);
    assert.deepEqual(force.elements().map((element) => element.id()).sort(), elements.map((element) => element.data.id).sort());
    assert.ok(meanEdgeLength(force) < meanEdgeLength(baseline) * .6, "Linked notes group more closely than in a concentric fallback");
    assertFitted(force);
  } finally { baseline.destroy(); force.destroy(); }
});

test("Shared force input validation, tick limits and cancellation do not publish stale final coordinates", async () => {
  await assert.rejects(runForceLayout({ positions: new Float32Array(4), links: new Uint32Array([0, 2]) }), /endpoints/);
  await assert.rejects(runForceLayout({ positions: new Float32Array([NaN, 0]), links: new Uint32Array() }), /coordinates/);
  await assert.rejects(runForceLayout({ positions: new Float32Array(3), links: new Uint32Array() }), /input/);
  let cancelled = false, frames = 0;
  const result = await runForceLayout({ ...compactInput(100), iterations: 999 }, {
    onPositions: () => { frames += 1; },
    cancelled: () => cancelled,
    yieldControl: async () => { cancelled = true; },
  });
  assert.equal(result.status, "cancelled");
  assert.ok(result.iteration <= FORCE_LIMITS.ticks);
  assert.equal(frames, 0);
});

test("Production worker entry returns transferable finite positions for every input node", async (t) => {
  const entry = new URL("../../tools/workspace_graph_worker.js", import.meta.url).href;
  const adapter = `import { parentPort } from "node:worker_threads";
    globalThis.self = { postMessage: (data, transfers) => parentPort.postMessage(data, transfers) };
    await import(${JSON.stringify(entry)});
    parentPort.on("message", (data) => self.onmessage({ data }));`;
  const worker = new ThreadWorker(new URL(`data:text/javascript,${encodeURIComponent(adapter)}`));
  t.after(() => worker.terminate());
  let timeout;
  const final = new Promise((resolve, reject) => {
    timeout = setTimeout(() => reject(new Error("Graph worker did not finish")), 5000);
    worker.on("error", reject);
    worker.on("message", (frame) => {
      if (frame.type === "error") reject(new Error(frame.error));
      if (frame.done) resolve(frame);
    });
  });
  try {
    const input = { type: "layout", requestId: 42, ...compactInput(1001), iterations: 3 };
    worker.postMessage(input, [input.positions.buffer, input.links.buffer]);
    assert.equal(input.positions.byteLength, 0);
    assert.equal(input.links.byteLength, 0);
    const frame = await final;
    assert.equal(frame.type, "positions");
    assert.equal(frame.requestId, 42);
    assert.equal(frame.iteration, 3);
    assert.ok(frame.positions instanceof Float32Array);
    assert.equal(frame.positions.length, 2002);
    assert.equal(frame.positions.every(Number.isFinite), true);
  } finally { clearTimeout(timeout); }
});

test("Production worker accepts scoped live pins and ignores stale, out-of-range and non-finite controls", async (t) => {
  const entry = new URL("../../tools/workspace_graph_worker.js", import.meta.url).href;
  const adapter = `import { parentPort } from "node:worker_threads";
    globalThis.self = { postMessage: (data, transfers) => parentPort.postMessage(data, transfers) };
    await import(${JSON.stringify(entry)});
    parentPort.on("message", (data) => self.onmessage({ data }));`;
  const worker = new ThreadWorker(new URL(`data:text/javascript,${encodeURIComponent(adapter)}`));
  t.after(() => worker.terminate());
  const frames = [];
  let acknowledge = false, timeout;
  const final = new Promise((resolve, reject) => {
    timeout = setTimeout(() => reject(new Error("Controlled graph worker did not finish")), 12000);
    worker.on("error", reject);
    worker.on("message", (frame) => {
      if (frame.type === "error") reject(new Error(frame.error));
      if (frame.type !== "positions") return;
      frames.push(frame);
      if (frame.done) resolve(frame);
      else if (acknowledge) worker.postMessage({ type: "ack", requestId: frame.requestId, iteration: frame.iteration });
    });
  });
  try {
    const input = { type: "layout", requestId: 42, ...compactInput(2000) };
    worker.postMessage(input, [input.positions.buffer, input.links.buffer]);
    await until(() => frames.length, 10000);
    assert.equal(frames[0].done, false, "The pin is sent while the bounded solver is running");
    worker.postMessage({ type: "pin", requestId: 42, index: 0, x: 1000, y: 2000 });
    worker.postMessage({ type: "pin", requestId: 41, index: 0, x: -1000, y: -2000 });
    worker.postMessage({ type: "pin", requestId: 42, index: 2000, x: -1000, y: -2000 });
    worker.postMessage({ type: "pin", requestId: 42, index: .5, x: -1000, y: -2000 });
    worker.postMessage({ type: "pin", requestId: 42, index: 0, x: Infinity, y: 2 });
    worker.postMessage({ type: "release", requestId: 41, index: 0 });
    acknowledge = true;
    worker.postMessage({ type: "ack", requestId: 42, iteration: frames[0].iteration });
    const frame = await final;
    assert.deepEqual([...frame.positions.slice(0, 2)], [1000, 2000]);
    assert.equal(frame.positions.length, 4000); assert.equal(frame.positions.every(Number.isFinite), true);
    assert.ok(frame.iteration <= FORCE_LIMITS.ticks);
    assert.ok(frame.elapsedMs < FORCE_LIMITS.milliseconds + 1000, "Reheating never resets the job's elapsed budget");
  } finally { clearTimeout(timeout); }
});

test("Production worker keeps one frame in flight and retains the newest final geometry until its matching acknowledgement", async (t) => {
  const entry = new URL("../../tools/workspace_graph_worker.js", import.meta.url).href;
  const adapter = `import { parentPort } from "node:worker_threads";
    let clock = 0;
    Object.defineProperty(globalThis, "performance", { value: { now: () => clock += 50 }, configurable: true });
    globalThis.self = { postMessage: (data, transfers) => parentPort.postMessage(data, transfers) };
    await import(${JSON.stringify(entry)});
    parentPort.on("message", async (data) => {
      await self.onmessage({ data });
      if (data.type === "layout") parentPort.postMessage({ type: "solver-finished" });
    });`;
  const worker = new ThreadWorker(new URL(`data:text/javascript,${encodeURIComponent(adapter)}`));
  t.after(() => worker.terminate());
  const frames = [];
  let solverFinished = false, workerError = null;
  worker.on("error", (error) => { workerError = error; });
  worker.on("message", (frame) => {
    if (frame.type === "solver-finished") solverFinished = true;
    else if (frame.type === "error") workerError = new Error(frame.error);
    else frames.push(frame);
  });
  const input = { type: "layout", requestId: 42, ...compactInput(1001) };
  worker.postMessage(input, [input.positions.buffer, input.links.buffer]);
  await until(() => solverFinished || workerError);
  assert.equal(workerError, null);
  assert.equal(frames.length, 1, "Progress cannot accumulate while the consumer is busy");
  assert.equal(frames[0].done, false);
  worker.postMessage({ type: "ack", requestId: 41, iteration: frames[0].iteration });
  worker.postMessage({ type: "ack", requestId: 42, iteration: frames[0].iteration + 1 });
  await sleep(20);
  assert.equal(frames.length, 1, "Stale requests and mismatched acknowledgements cannot release pending geometry");
  worker.postMessage({ type: "ack", requestId: 42, iteration: frames[0].iteration });
  await until(() => frames.length === 2);
  assert.equal(frames[1].done, true, "The latest pending frame retains solver completion");
  assert.ok(frames[1].iteration <= FORCE_LIMITS.ticks);
  assert.equal(frames[1].positions.length, 2002);
  assert.equal(frames[1].positions.every(Number.isFinite), true);
  worker.postMessage({ type: "ack", requestId: 42, iteration: frames[0].iteration });
  await sleep(20);
  assert.equal(frames.length, 2, "Duplicate acknowledgements do not replay final geometry");
});

test("Large worker layouts use compact topology, reject stale outputs and respect cancellation and viewport intent", async (t) => {
  const original = globalThis.Worker, workers = [];
  class FakeWorker {
    constructor(url) { this.url = url; this.terminated = false; workers.push(this); }
    postMessage(input, transfers) { if (input.type === "layout") { this.input = input; this.transfers = transfers; } }
    terminate() { this.terminated = true; }
    emit(frame) { this.onmessage?.({ data: frame }); }
  }
  globalThis.Worker = FakeWorker;
  t.after(() => { if (original === undefined) delete globalThis.Worker; else globalThis.Worker = original; });
  const cy = graph(clusteredElements(1100));
  const positions = new Float32Array(2200);
  for (let index = 0; index < 1100; index += 1) { positions[index * 2] = index * 10; positions[index * 2 + 1] = index % 37; }
  let callbacks = 0;
  try {
    const old = layoutGraph(cy, { signature: "old", onStop: () => { callbacks += 1; } });
    await until(() => workers[0]?.input);
    const worker = workers[0];
    assert.equal(worker.url, "/workspace-graph-worker.js");
    assert.equal(worker.input.positions.length, 2200);
    assert.equal(worker.input.links.length, cy.edges().length * 2);
    assert.equal(worker.transfers.length, 2);
    assert.equal(Object.hasOwn(worker.input, "nodes"), false, "No note text, labels, paths or IDs are sent to the worker");
    worker.emit({ type: "positions", requestId: worker.input.requestId + 1, positions, done: true });
    assert.equal(cy.$id("n1").position("x"), 0, "Wrong generation cannot change coordinates");
    assert.equal(old.cancel(), true);
    assert.equal((await old.finished).status, "cancelled");
    assert.equal(worker.terminated, true);
    worker.emit({ type: "positions", requestId: worker.input.requestId, positions, done: true });
    assert.equal(cy.$id("n1").position("x"), 0, "Queued outputs from a cancelled worker are ignored");
    const current = layoutGraph(cy, { signature: "new", onStop: () => { callbacks += 1; } });
    await until(() => workers[1]?.input);
    cy.zoom(.6); cy.pan({ x: 81, y: 94 });
    workers[1].emit({ type: "positions", requestId: workers[1].input.requestId, positions, iteration: 1, done: false });
    await until(() => cy.$id("n1").position("x") === 10);
    assert.equal(cy.$id("n1").position("x"), 10);
    workers[1].emit({ type: "positions", requestId: workers[1].input.requestId, positions, iteration: 2, done: true });
    const result = await current.finished;
    assert.equal(result.status, "complete");
    assert.equal(result.fitted, false);
    assert.equal(workers[1].terminated, true);
    assert.equal(cy.zoom(), .6); assert.deepEqual(cy.pan(), { x: 81, y: 94 });
    assert.equal(callbacks, 1);
    assert.equal(layoutGraph(cy, { signature: "new" }), current, "Same topology retains coordinates and finished controller");
    assert.equal(workers.length, 2);
  } finally { cy.destroy(); }
});

test("Invalid worker positions fail visibly without corrupting current graph geometry", async (t) => {
  const original = globalThis.Worker;
  let worker;
  globalThis.Worker = class {
    constructor() { worker = this; }
    postMessage(input) { this.input = input; }
    terminate() { this.terminated = true; }
  };
  t.after(() => { if (original === undefined) delete globalThis.Worker; else globalThis.Worker = original; });
  const cy = graph(clusteredElements(1100));
  let errors = 0;
  try {
    const controller = layoutGraph(cy, { onError: () => { errors += 1; } });
    await until(() => worker?.input);
    worker.onmessage({ data: { type: "positions", requestId: worker.input.requestId, positions: new Float32Array([NaN, 0]), done: true } });
    assert.equal((await controller.finished).status, "error");
    assert.equal(errors, 1);
    assert.equal(worker.terminated, true);
    assert.equal(cy.nodes().length, 1100);
    assert.equal(cy.nodes().every((node) => Number.isFinite(node.position("x")) && Number.isFinite(node.position("y"))), true);
  } finally { cy.destroy(); }
});

for (const count of [450, 1000]) test(`Force geometry preserves ${count} nodes and all edges, improves linked locality, and fits without clipping`, () => {
  const elements = clusteredElements(count), baseline = graph(elements), force = graph(elements);
  try {
    baseline.layout({ name: "concentric", animate: false, fit: false, minNodeSpacing: 24, padding: 40 }).run();
    seeded(0x421ac, () => force.layout({ ...coseLayoutOptions(count), animate: false }).run());
    assert.equal(force.nodes().length, count);
    assert.equal(force.edges().length, elements.length - count);
    assert.deepEqual(force.elements().map((element) => element.id()).sort(), elements.map((element) => element.data.id).sort());
    assert.equal(force.nodes().every((node) => Number.isFinite(node.position("x")) && Number.isFinite(node.position("y"))), true);
    assert.equal(new Set(force.nodes().map((node) => `${node.position("x").toFixed(3)},${node.position("y").toFixed(3)}`)).size, count);
    assert.ok(meanEdgeLength(force) < meanEdgeLength(baseline) * .5, "Related nodes are substantially closer than in the prior concentric layout");
    assert.equal(fitGraph(force), true);
    assertFitted(force);
    assert.equal(fitGraph(baseline), true);
    assertFitted(baseline);
    if (count === 1000) assert.ok(baseline.zoom() < .15, "The old minimum would have clipped this graph");
  } finally { baseline.destroy(); force.destroy(); }
});

test("Explicit Fit computes lower zoom bounds for very large extents and handles small/empty graphs", () => {
  const cy = graph();
  try {
    cy.$id("a").position({ x: -1000000, y: -300000 });
    cy.$id("b").position({ x: 1000000, y: 300000 });
    assert.equal(fitGraph(cy), true);
    assert.ok(cy.minZoom() <= .005 && cy.zoom() < .005);
    assertFitted(cy);
    cy.remove(cy.$id("b"));
    assert.equal(fitGraph(cy), true);
    assert.ok(cy.zoom() <= 1.1);
    cy.remove(cy.elements());
    assert.equal(fitGraph(cy), false);
  } finally { cy.destroy(); }
  assert.equal(fitGraph(cy), false);
});

test("Layout starts after yielding, fits once on completion, and preserves same-data viewport and positions", async () => {
  const cy = graph();
  let stops = 0, fits = 0;
  const originalViewport = cy.viewport.bind(cy);
  cy.viewport = (...args) => { fits += 1; return originalViewport(...args); };
  try {
    const controller = layoutGraph(cy, { signature: "same", iterations: 3, onStop: () => { stops += 1; } });
    assert.equal(controller.layout, null);
    await sleep(0);
    assert.equal(controller.layout, null, "Initialization does not block the first event-loop turn");
    assert.equal((await controller.finished).status, "complete");
    assert.equal(stops, 1);
    assert.equal(fits, 1);
    cy.zoom(.7); cy.pan({ x: 123, y: 234 });
    const before = { zoom: cy.zoom(), pan: { ...cy.pan() }, positions: cy.nodes().map((node) => ({ ...node.position() })) };
    assert.equal(layoutGraph(cy, { signature: "same", onStop: () => { stops += 1; } }), controller);
    cy.resize();
    assert.deepEqual({ zoom: cy.zoom(), pan: cy.pan(), positions: cy.nodes().map((node) => node.position()) }, before);
    assert.equal(stops, 1);
    assert.equal(fits, 1);
  } finally { cy.destroy(); }
});

test("User viewport changes during layout suppress automatic fitting", async () => {
  const cy = graph();
  try {
    const controller = layoutGraph(cy, { iterations: 4 });
    while (!controller.layout) await sleep(5);
    cy.zoom(.6); cy.pan({ x: 81, y: 94 });
    const result = await controller.finished;
    assert.equal(result.status, "complete");
    assert.equal(result.fitted, false);
    assert.equal(cy.zoom(), .6);
    assert.deepEqual(cy.pan(), { x: 81, y: 94 });
  } finally { cy.destroy(); }
});

test("Cancellation stops pending/active layouts and prevents late refreshes, callbacks and viewport resets", async () => {
  const cy = graph();
  let stops = 0;
  try {
    const pending = layoutGraph(cy, { signature: "pending", onStop: () => { stops += 1; } });
    assert.equal(pending.cancel(), true);
    assert.equal((await pending.finished).status, "cancelled");
    await sleep(25);
    assert.equal(pending.layout, null);
    const active = layoutGraph(cy, { signature: "active", onStop: () => { stops += 1; } });
    while (!active.layout) await sleep(5);
    active.cancel();
    cy.$id("a").position({ x: 999, y: 777 });
    cy.pan({ x: 21, y: 34 });
    const positions = cy.nodes().map((node) => ({ ...node.position() }));
    active.layout.emit("layoutstop");
    await sleep(50);
    assert.deepEqual(cy.nodes().map((node) => node.position()), positions, "A cancelled CoSE frame cannot overwrite current positions");
    assert.deepEqual(cy.pan(), { x: 21, y: 34 });
    assert.equal(stops, 0);
    assert.equal((await active.finished).status, "cancelled");
    assert.equal(active.cancel(), false);
  } finally { cy.destroy(); }
});

test("Replacement and destruction settle outstanding work without stale completion callbacks", async () => {
  const cy = graph();
  const callbacks = [];
  const old = layoutGraph(cy, { signature: "old", onStop: () => callbacks.push("old") });
  const current = layoutGraph(cy, { signature: "current", iterations: 2, onStop: () => callbacks.push("current") });
  assert.equal((await old.finished).status, "cancelled");
  assert.equal((await current.finished).status, "complete");
  assert.deepEqual(callbacks, ["current"]);
  const doomed = layoutGraph(cy, { signature: "destroyed", onStop: () => callbacks.push("destroyed") });
  cy.destroy();
  assert.equal((await doomed.finished).status, "cancelled");
  assert.deepEqual(callbacks, ["current"]);
});

test("Stopping the underlying CoSE layout is treated as cancellation without a late fit", async () => {
  const cy = graph();
  let stops = 0;
  try {
    const controller = layoutGraph(cy, { onStop: () => { stops += 1; } });
    while (!controller.layout) await sleep(5);
    controller.layout.stop();
    const positions = cy.nodes().map((node) => ({ ...node.position() }));
    assert.equal((await controller.finished).status, "cancelled");
    await sleep(30);
    assert.deepEqual(cy.nodes().map((node) => node.position()), positions);
    assert.equal(stops, 0);
  } finally { cy.destroy(); }
});

test("Incremental layout can preserve initial node placement instead of randomizing it", async () => {
  const cy = graph();
  try {
    cy.$id("a").position({ x: 0, y: 0 });
    cy.$id("b").position({ x: 10000, y: 0 });
    const controller = layoutGraph(cy, { randomize: false, iterations: 1, fit: false });
    assert.equal((await controller.finished).status, "complete");
    assert.equal(controller.layout.options.randomize, false);
    assert.ok(Math.abs(cy.$id("a").position("x") - cy.$id("b").position("x")) > 9000, "One bounded simulation step starts from the supplied positions");
  } finally { cy.destroy(); }
});

test("Unchanged viewport events do not suppress Fit, while an actual change stays sticky after restoration", async () => {
  const cy = graph();
  try {
    const unchanged = layoutGraph(cy, { iterations: 3 });
    while (!unchanged.layout) await sleep(5);
    cy.pan({ ...cy.pan() });
    cy.viewport({ zoom: cy.zoom(), pan: { ...cy.pan() } });
    cy.emit("pan zoom");
    assert.equal((await unchanged.finished).fitted, true, "Cytoscape emits pan/zoom even when their numeric values remain unchanged");
    const changed = layoutGraph(cy, { force: true, iterations: 3 });
    const initial = { zoom: cy.zoom(), pan: { ...cy.pan() } };
    while (!changed.layout) await sleep(5);
    cy.pan({ x: initial.pan.x + 10, y: initial.pan.y });
    cy.viewport(initial);
    assert.equal((await changed.finished).fitted, false, "A real movement remains intentional even if the user returns to the starting viewport");
    assert.equal(cy.zoom(), initial.zoom);
    assert.deepEqual(cy.pan(), initial.pan);
  } finally { cy.destroy(); }
});

test("Compact component packing keeps connected notes readable alongside many orphans without hiding data", () => {
  const elements = Array.from({ length: 450 }, (_, at) => ({ data: { id: `n${at}` } }));
  let edge = 0;
  for (let at = 0; at < 180; at += 1) for (const delta of [1, 4]) elements.push({ data: { id: `e${edge++}`, source: `n${at}`, target: `n${(at + delta) % 180}` } });
  const prior = graph(elements), compact = graph(elements);
  try {
    for (const cy of [prior, compact]) {
      cy.width = () => 430;
      cy.height = () => 650;
      cy.style().selector("node").style({ width: 12, height: 12 }).update();
    }
    seeded(0x421ac, () => prior.layout({ ...coseLayoutOptions(450), animate: false, componentSpacing: 80 }).run());
    seeded(0x421ac, () => compact.layout({ ...coseLayoutOptions(450), animate: false }).run());
    assert.equal(fitGraph(prior), true);
    assert.equal(fitGraph(compact), true);
    const connected = (cy) => cy.nodes().filter((node) => node.degree() > 0).boundingBox({ includeLabels: false });
    const before = connected(prior), after = connected(compact);
    assert.ok(after.w * compact.zoom() > before.w * prior.zoom() * 2, "Connected notes use substantially more of the visible width");
    assert.ok(after.h * compact.zoom() > before.h * prior.zoom() * 2, "Orphan spacing does not dominate the fitted height");
    assert.equal(compact.nodes().length, 450);
    assert.equal(compact.edges().length, 360);
    assert.equal(compact.nodes().filter((node) => node.degree() === 0).length, 270);
    assert.deepEqual(compact.elements().map((element) => element.id()).sort(), elements.map((element) => element.data.id).sort());
    assertFitted(compact);
  } finally { prior.destroy(); compact.destroy(); }
});

test("Small neighborhoods use more canvas with fewer estimated title overlaps across five seeds", () => {
  const elements = Array.from({ length: 10 }, (_, at) => ({ data: { id: `n${at}`, label: `Concept ${at}: ${"Long research title ".repeat(4)}`.slice(0, 30 + Math.round(at * 50 / 9)) } }));
  let edge = 0;
  for (let at = 1; at <= 6; at += 1) elements.push({ data: { id: `e${edge++}`, source: "n0", target: `n${at}` } });
  for (let at = 1; at < 9; at += 1) elements.push({ data: { id: `e${edge++}`, source: `n${at}`, target: `n${at + 1}` } });
  const estimatedOverlaps = (cy) => {
    // Headless has no font measurement; use capped 160px title rectangles at font-size 11.
    const rectangles = cy.nodes().map((node) => {
      const point = node.position(), width = Math.min(160, node.data("label").length * 11 * .55);
      return { x1: point.x - width / 2, x2: point.x + width / 2, y1: point.y + 11, y2: point.y + 22 };
    });
    let overlaps = 0;
    for (let from = 0; from < rectangles.length; from += 1) for (let to = from + 1; to < rectangles.length; to += 1) {
      const first = rectangles[from], second = rectangles[to];
      if (Math.min(first.x2, second.x2) > Math.max(first.x1, second.x1) && Math.min(first.y2, second.y2) > Math.max(first.y1, second.y1)) overlaps += 1;
    }
    return overlaps;
  };
  let priorOverlaps = 0, currentOverlaps = 0;
  for (const seed of [1, 2, 3, 4, 5]) {
    const prior = graph(elements), current = graph(elements);
    try {
      for (const cy of [prior, current]) {
        cy.width = () => 420;
        cy.height = () => 600;
        cy.style().selector("node").style({ width: 12, height: 12, label: "data(label)", "font-size": 11, "text-max-width": 160, "text-wrap": "ellipsis", "text-valign": "bottom", "text-margin-y": 5 }).update();
      }
      seeded(seed, () => prior.layout({ ...coseLayoutOptions(10), animate: false, nodeRepulsion: 4000, edgeElasticity: 32 }).run());
      seeded(seed, () => current.layout({ ...coseLayoutOptions(10), animate: false }).run());
      priorOverlaps += estimatedOverlaps(prior);
      currentOverlaps += estimatedOverlaps(current);
      assert.equal(fitGraph(current), true);
      assert.equal(current.nodes().length, 10);
      assert.equal(current.edges().length, 14);
      assert.deepEqual(current.elements().map((element) => element.id()).sort(), elements.map((element) => element.data.id).sort());
      assert.ok(current.nodes().boundingBox({ includeLabels: false }).w * current.zoom() > 300, "The small neighborhood uses most of the available 330px inner width");
      assertFitted(current);
    } finally { prior.destroy(); current.destroy(); }
  }
  assert.ok(currentOverlaps < priorOverlaps / 2, `${currentOverlaps} estimated overlap pairs versus ${priorOverlaps} previously`);
  assert.equal(coseLayoutOptions(50).nodeRepulsion, 12000);
  assert.equal(coseLayoutOptions(51).nodeRepulsion, 4000);
  assert.equal(coseLayoutOptions(51).edgeElasticity, 32);
  assert.equal(coseLayoutOptions(10).idealEdgeLength, 55);
});
