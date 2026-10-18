import assert from "node:assert/strict";
import test from "node:test";
import cytoscape from "cytoscape";
import { JSDOM, VirtualConsole } from "jsdom";
import { createGraphView, graphNodeRadius, projectPoint, segmentVisible, unprojectPoint, zoomViewport } from "../../tools/workspace_graph_view.js";

function harness(t, elements, { reducedMotion = false, width = 400, height = 300 } = {}) {
  const errors = [], virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (error) => errors.push(error));
  const dom = new JSDOM("<div id='graph'></div>", { pretendToBeVisual: true, virtualConsole });
  const w = dom.window, container = w.document.getElementById("graph"), callbacks = new Map();
  let time = 0, sequence = 0, bounds = { left: 0, top: 0, width, height, right: width, bottom: height };
  Object.defineProperty(w.performance, "now", { value: () => time });
  w.requestAnimationFrame = (callback) => { callbacks.set(++sequence, callback); return sequence; };
  w.cancelAnimationFrame = (id) => callbacks.delete(id);
  const motionListeners = new Set(), media = { matches: reducedMotion, addEventListener: (_, fn) => motionListeners.add(fn), removeEventListener: (_, fn) => motionListeners.delete(fn) };
  w.matchMedia = () => media;
  container.getBoundingClientRect = () => bounds;
  w.CanvasRenderingContext2D = class {};
  const paintings = [], context = {
    globalAlpha: 1, path: [],
    setTransform() {},
    clearRect() { paintings.push({ circles: [], strokes: [], texts: [] }); },
    beginPath() { this.path = []; },
    moveTo(x, y) { this.previous = { x, y }; },
    lineTo(x, y) { this.path.push({ from: this.previous, to: { x, y } }); this.previous = { x, y }; },
    quadraticCurveTo(cx, cy, x, y) { this.path.push({ from: this.previous, control: { x: cx, y: cy }, to: { x, y } }); this.previous = { x, y }; },
    arc(x, y, radius) { this.path.push({ x, y, radius }); },
    fill() { for (const circle of this.path.filter((item) => "radius" in item)) paintings.at(-1).circles.push({ ...circle, color: this.fillStyle, alpha: this.globalAlpha }); },
    stroke() { paintings.at(-1).strokes.push({ path: [...this.path], dash: [...this.dash], color: this.strokeStyle, alpha: this.globalAlpha, width: this.lineWidth }); },
    setLineDash(dash) { this.dash = dash; },
    fillText(text, x, y) { paintings.at(-1).texts.push({ text, x, y, font: this.font, alpha: this.globalAlpha }); },
    strokeText() {},
  };
  w.HTMLCanvasElement.prototype.getContext = () => context;
  const captures = new Set();
  w.HTMLCanvasElement.prototype.setPointerCapture = (id) => captures.add(id);
  w.HTMLCanvasElement.prototype.hasPointerCapture = (id) => captures.has(id);
  w.HTMLCanvasElement.prototype.releasePointerCapture = (id) => captures.delete(id);
  w.HTMLCanvasElement.prototype.getBoundingClientRect = () => bounds;
  const graph = cytoscape({ headless: true, styleEnabled: false, container, elements, layout: { name: "preset" }, minZoom: .0001, maxZoom: 8 });
  const view = createGraphView(graph, { container });
  const canvas = container.querySelector("canvas");
  const frame = (milliseconds = 16) => {
    time += milliseconds;
    const pending = [...callbacks.values()]; callbacks.clear();
    for (const callback of pending) callback(time);
  };
  const settle = () => { for (let at = 0; callbacks.size && at < 200; at++) frame(); assert.equal(callbacks.size, 0, "An idle graph stops requesting animation frames"); };
  const pointer = (type, x, y, pointerId = 1, pointerType = "mouse") => {
    const event = new w.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y });
    Object.defineProperties(event, { pointerId: { value: pointerId }, pointerType: { value: pointerType } });
    canvas.dispatchEvent(event); return event;
  };
  const wheel = (deltaY, x, y, deltaMode = 0) => {
    const event = new w.WheelEvent("wheel", { deltaY, clientX: x, clientY: y, deltaMode, bubbles: true, cancelable: true });
    canvas.dispatchEvent(event); return event;
  };
  t.after(() => { view?.destroy(); graph.destroy(); w.close(); assert.deepEqual(errors, []); });
  return { w, graph, view, canvas, frame, settle, pointer, wheel, paintings, captures, callbacks,
    resize(nextWidth, nextHeight) { bounds = { ...bounds, width: nextWidth, height: nextHeight, right: nextWidth, bottom: nextHeight }; view.resize(); },
    motion(value) { media.matches = value; motionListeners.forEach((listener) => listener()); },
  };
}

const node = (id, x, y, data = {}) => ({ data: { id, label: id, ...data }, position: { x, y } });
const edge = (id, source, target, layer = "reference") => ({ data: { id, source, target, layer } });
const close = (left, right, epsilon = 1e-8) => assert.ok(Math.abs(left - right) < epsilon, `${left} differs from ${right}`);

test("Projection inverts at negative coordinates and wide zoom ranges, and zoom retains its anchor", () => {
  for (const zoom of [.0001, .2, 1, 8]) {
    const viewport = { zoom, pan: { x: -701, y: 303 } }, point = { x: -300.5, y: 702.75 };
    const restored = unprojectPoint(projectPoint(point, viewport), viewport);
    close(restored.x, point.x, .000001); close(restored.y, point.y, .000001);
    const anchor = { x: 177, y: 93 }, before = unprojectPoint(anchor, viewport), next = zoomViewport(viewport, zoom * 1.25, anchor);
    const after = unprojectPoint(anchor, next); close(before.x, after.x, .000001); close(before.y, after.y, .000001);
  }
});

test("Radii grow with degree and square-root zoom while spacing opens linearly through zoom 8", () => {
  assert.ok(graphNodeRadius(20, 1) > graphNodeRadius(0, 1));
  assert.ok(graphNodeRadius(1000000, 1) <= 15);
  close(graphNodeRadius(20, 4) / graphNodeRadius(20, 1), 2);
  close(graphNodeRadius(20, 8) / graphNodeRadius(20, 4), Math.sqrt(2));
  const separation = (zoom) => projectPoint({ x: 100, y: 0 }, { zoom, pan: { x: 0, y: 0 } }).x / graphNodeRadius(20, zoom);
  assert.ok(separation(8) > separation(4) && separation(4) > separation(1));
});

test("A segment crossing the viewport remains visible with both endpoints outside", () => {
  assert.equal(segmentVisible({ x: -200, y: 150 }, { x: 900, y: 150 }, 400, 300), true);
  assert.equal(segmentVisible({ x: -200, y: -5 }, { x: 900, y: -5 }, 400, 300), false);
  assert.equal(segmentVisible({ x: -200, y: -5 }, { x: 900, y: -5 }, 400, 300, 6), true);
  assert.equal(segmentVisible({ x: -10, y: -10 }, { x: -10, y: -10 }, 400, 300), false);
});

test("Missing canvas capability returns null before requesting a context", () => {
  const dom = new JSDOM("<div></div>");
  dom.window.HTMLCanvasElement.prototype.getContext = () => assert.fail("Canvas context must not be requested without capability");
  assert.equal(createGraphView({}, { container: dom.window.document.querySelector("div") }), null);
  dom.window.close();
});

test("Panning paints newly exposed nodes before pointer release and keeps crossing edges live", (t) => {
  const app = harness(t, [node("A", -100, 150), node("B", 1000, 150), edge("ab", "A", "B")]); app.settle();
  assert.equal(app.paintings.at(-1).circles.length, 0);
  assert.equal(app.paintings.at(-1).strokes.flatMap((stroke) => stroke.path).filter((path) => path.from).length, 1);
  app.pointer("pointerdown", 200, 250); app.pointer("pointermove", -600, 250); app.frame();
  close(app.graph.pan("x"), -800);
  assert.ok(app.paintings.at(-1).circles.some((circle) => Math.abs(circle.x - 200) < .01), "The initially offscreen node enters during the held pan");
  assert.equal(app.captures.has(1), true);
  app.pointer("pointerup", -600, 250); app.settle();
  assert.equal(app.captures.size, 0);
});

test("Canvas retains all 7566 nodes and all edges without an internal display cap", (t) => {
  const nodes = Array.from({ length: 7566 }, (_, at) => node(`n${at}`, at % 100 * 8 + 5, Math.floor(at / 100) * 8 + 5));
  const edges = Array.from({ length: 10000 }, (_, at) => edge(`e${at}`, `n${at % nodes.length}`, `n${(at + 31) % nodes.length}`, at % 7 === 0 ? "relation" : "reference"));
  const app = harness(t, [...nodes, ...edges], { width: 1000, height: 800 }); app.settle();
  assert.equal(app.paintings.at(-1).circles.length, 7566);
  assert.equal(app.paintings.at(-1).strokes.flatMap((stroke) => stroke.path).filter((path) => path.from).length, 10000);
  assert.ok(app.paintings.at(-1).strokes.some((stroke) => stroke.dash.length));
  assert.equal(app.graph.nodes().length, 7566); assert.equal(app.graph.edges().length, 10000);
});

test("Zoom-in anchors the pointer through easing; zoom-out uses the view center", (t) => {
  const app = harness(t, [node("A", 120, 100)]); app.settle();
  const event = app.wheel(-200, 120, 100); assert.equal(event.defaultPrevented, true);
  assert.ok(app.graph.zoom() > 1);
  app.frame(); app.frame();
  const circle = app.paintings.at(-1).circles[0]; close(circle.x, 120, .001); close(circle.y, 100, .001);
  app.settle();
  const center = { x: 200, y: 150 }, before = unprojectPoint(center, { zoom: app.graph.zoom(), pan: app.graph.pan() });
  app.view.zoomBy(.5, { x: 10, y: 10 });
  const after = unprojectPoint(center, { zoom: app.graph.zoom(), pan: app.graph.pan() });
  close(before.x, after.x); close(before.y, after.y); app.settle();
});

test("Click and drag are distinct, drag updates the model, and capture survives leaving the canvas", (t) => {
  const app = harness(t, [node("A", 100, 100)]); app.settle();
  const counts = { tap: 0, grab: 0, drag: 0, free: 0 };
  for (const type of Object.keys(counts)) app.graph.on(type, "node", () => counts[type]++);
  app.pointer("pointerdown", 100, 100); app.pointer("pointermove", 102, 101); app.pointer("pointerup", 102, 101);
  assert.deepEqual(counts, { tap: 1, grab: 0, drag: 0, free: 0 });
  app.pointer("pointerdown", 100, 100); app.pointer("pointermove", 600, 400); app.frame();
  assert.deepEqual({ ...app.graph.$id("A").position() }, { x: 600, y: 400 });
  assert.equal(app.captures.has(1), true);
  app.pointer("pointerup", 600, 400);
  assert.deepEqual(counts, { tap: 1, grab: 1, drag: 1, free: 1 });
  assert.equal(app.captures.size, 0); app.settle();
});

test("Worker position targets interpolate without writing intermediate model coordinates", (t) => {
  const app = harness(t, [node("A", 100, 100)]); app.settle();
  let changes = 0; app.graph.on("position", "node", () => changes++);
  app.graph.$id("A").position({ x: 300, y: 100 }); app.frame(); app.frame(80);
  const intermediate = app.paintings.at(-1).circles[0].x;
  assert.ok(intermediate > 100 && intermediate < 300); assert.equal(app.graph.$id("A").position("x"), 300);
  assert.equal(changes, 1);
  app.graph.$id("A").position({ x: 200, y: 100 }); app.frame();
  assert.ok(app.paintings.at(-1).circles[0].x > 200, "A replacement target starts from the drawn pose");
  app.settle(); close(app.paintings.at(-1).circles[0].x, 200); assert.equal(changes, 2);
});

test("Reduced motion settles immediately and disabling labels retains readable hover text", (t) => {
  const app = harness(t, [node("A", 100, 100)], { reducedMotion: true }); app.settle();
  app.view.setOptions({ labels: false }); app.graph.$id("A").position({ x: 150, y: 100 }); app.frame();
  close(app.paintings.at(-1).circles[0].x, 150); assert.equal(app.paintings.at(-1).texts.length, 0);
  let entered = 0, left = 0;
  app.graph.on("mouseover", "node", () => entered++); app.graph.on("mouseout", "node", () => left++);
  app.pointer("pointermove", 150, 100); app.frame();
  assert.equal(entered, 1); assert.equal(app.paintings.at(-1).texts[0].text, "A");
  assert.ok(Number.parseFloat(app.paintings.at(-1).texts[0].font) >= 12);
  app.pointer("pointerleave", 600, 400); app.frame(); assert.equal(left, 1); app.settle();
});

test("Resize preserves the world center and high-DPI backing pixels", (t) => {
  const app = harness(t, [node("A", 100, 100)], { reducedMotion: true }); app.settle();
  const before = unprojectPoint({ x: 200, y: 150 }, { zoom: app.graph.zoom(), pan: app.graph.pan() });
  Object.defineProperty(app.w, "devicePixelRatio", { value: 2 }); app.resize(600, 500); app.settle();
  const after = unprojectPoint({ x: 300, y: 250 }, { zoom: app.graph.zoom(), pan: app.graph.pan() });
  close(before.x, after.x); close(before.y, after.y);
  assert.equal(app.canvas.width, 1200); assert.equal(app.canvas.height, 1000);
});

test("Release coordinates are applied, cancellation emits free without a tap, and destroy does not restart a drag", (t) => {
  const app = harness(t, [node("A", 100, 100)], { reducedMotion: true }); app.settle();
  let taps = 0, frees = 0;
  app.graph.on("tap", "node", () => taps++); app.graph.on("free", "node", () => frees++);
  app.pointer("pointerdown", 100, 100); app.pointer("pointermove", 120, 100); app.pointer("pointerup", 140, 110);
  assert.deepEqual({ ...app.graph.$id("A").position() }, { x: 140, y: 110 });
  assert.equal(frees, 1); assert.equal(taps, 0); app.settle();
  app.pointer("pointerdown", 140, 110); app.pointer("pointermove", 150, 120); app.pointer("pointercancel", 150, 120);
  assert.equal(frees, 2); assert.equal(taps, 0); assert.equal(app.captures.size, 0); app.settle();
  app.pointer("pointerdown", 150, 120); app.pointer("pointermove", 160, 130); app.view.destroy();
  assert.equal(frees, 2, "Destroy does not emit a free event that can restart physics");
});

test("Pinch follows its center and the remaining pointer continues panning without restoring an old scale", (t) => {
  const app = harness(t, [node("A", 350, 250)], { reducedMotion: true }); app.settle();
  app.pointer("pointerdown", 100, 100, 1, "touch"); app.pointer("pointerdown", 200, 100, 2, "touch");
  app.pointer("pointermove", 300, 100, 2, "touch");
  close(app.graph.zoom(), 2);
  const anchored = projectPoint({ x: 150, y: 100 }, { zoom: app.graph.zoom(), pan: app.graph.pan() });
  close(anchored.x, 200); close(anchored.y, 100);
  app.pointer("pointerup", 300, 100, 2, "touch"); app.pointer("pointermove", 110, 110, 1, "touch");
  close(app.graph.zoom(), 2); close(app.graph.pan("x"), -90); close(app.graph.pan("y"), -90);
  app.pointer("pointerup", 110, 110, 1, "touch"); app.settle(); assert.equal(app.captures.size, 0);
});

test("Wheel units normalize consistently and full detents use predictable zoom increments", (t) => {
  const pixels = harness(t, []), lines = harness(t, []), pages = harness(t, []);
  pixels.settle(); lines.settle(); pages.settle();
  pixels.wheel(-120, 100, 100); lines.wheel(-3, 100, 100, 1); pages.wheel(-.15, 100, 100, 2);
  close(pixels.graph.zoom(), 1.5); close(lines.graph.zoom(), pixels.graph.zoom()); close(pages.graph.zoom(), pixels.graph.zoom());
  pixels.settle(); lines.settle(); pages.settle();
});

test("A static pointer is rechecked when reduced-motion targets move, and an outlier does not change label visibility", (t) => {
  const cohort = Array.from({ length: 51 }, (_, at) => node(`near${at}`, 110 + at % 8 * 6, 120 + Math.floor(at / 8) * 6));
  const app = harness(t, [node("A", 100, 100), ...cohort, node("Outlier", 1000000, 1000000)], { reducedMotion: true }); app.settle();
  assert.ok(app.paintings.at(-1).texts.some((label) => label.text === "A"));
  app.graph.$id("Outlier").remove(); app.settle();
  assert.ok(app.paintings.at(-1).texts.some((label) => label.text === "A"), "Removing a remote outlier does not hide dense-cohort labels");
  let left = 0; app.graph.on("mouseout", "node", () => left++);
  app.pointer("pointermove", 100, 100); app.settle();
  app.graph.$id("A").position({ x: 200, y: 100 }); app.frame();
  assert.equal(left, 1, "A node leaving the pointer clears hover even when interpolation is disabled"); app.settle();
});

test("Topology refresh preserves retained geometry and destroy cancels callbacks and input", (t) => {
  const app = harness(t, [node("A", 100, 100)], { reducedMotion: true }); app.settle();
  app.graph.add(node("B", 250, 150)); app.graph.add(edge("ab", "A", "B")); app.settle();
  assert.equal(app.paintings.at(-1).circles.length, 2);
  app.graph.$id("A").data("current", "yes"); app.settle();
  assert.equal(app.paintings.at(-1).circles.find((circle) => circle.x === 100).color, "#7851b6");
  let renders = 0; app.graph.on("render", () => renders++);
  app.view.zoomBy(2); app.frame(); assert.equal(renders, 1);
  app.pointer("pointerdown", 100, 100); app.pointer("pointermove", 150, 150);
  const position = { ...app.graph.$id("A").position() };
  app.view.destroy(); assert.equal(app.callbacks.size, 0); assert.equal(app.canvas.isConnected, false);
  app.pointer("pointermove", 300, 200); app.graph.pan({ x: 50, y: 50 }); app.frame();
  assert.equal(renders, 1); assert.deepEqual({ ...app.graph.$id("A").position() }, position);
});


test("Edges meet node boundaries, labels sit below nodes, and distant labels fade", (t) => {
  const app = harness(t, [node("A", 100, 100), node("B", 200, 100), edge("ab", "A", "B")], { reducedMotion: true }); app.settle();
  const path = app.paintings.at(-1).strokes.flatMap((stroke) => stroke.path).find((item) => item.from);
  assert.ok(path.from.x > 100 && path.to.x < 200);
  assert.equal(path.from.y, 100); assert.equal(path.to.y, 100);
  assert.ok(app.paintings.at(-1).texts.every((text) => text.y > 100));
  app.view.zoomBy(.5); app.settle(); assert.equal(app.paintings.at(-1).texts.length, 0);
  app.pointer("pointermove", 150, 125); app.settle();
  assert.equal(app.paintings.at(-1).texts[0].text, "A");
  assert.ok(Number.parseFloat(app.paintings.at(-1).texts[0].font) >= 15);
});

test("Zoom immediately after Fit uses the fitted camera instead of an offscreen old camera", (t) => {
  const app = harness(t, [node("A", 10000, 0), node("B", 10200, 0)]); app.settle();
  assert.equal(app.paintings.at(-1).circles.length, 0);
  app.graph.viewport({ zoom: .5, pan: { x: 200 - 10100 * .5, y: 150 } });
  app.view.zoomBy(2, { x: 200, y: 150 }); app.settle();
  assert.equal(app.paintings.at(-1).circles.length, 2);
  close(app.paintings.at(-1).circles[0].x, 100); close(app.paintings.at(-1).circles[1].x, 300);
});
