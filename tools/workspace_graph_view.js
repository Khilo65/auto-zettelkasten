const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const ease = (value) => 1 - (1 - clamp(value, 0, 1)) ** 3;

export function projectPoint(point, viewport) {
  return { x: point.x * viewport.zoom + viewport.pan.x, y: point.y * viewport.zoom + viewport.pan.y };
}

export function unprojectPoint(point, viewport) {
  return { x: (point.x - viewport.pan.x) / viewport.zoom, y: (point.y - viewport.pan.y) / viewport.zoom };
}

export function graphNodeRadius(degree, zoom) {
  const base = clamp(4 + Math.log2(Math.max(0, degree) + 1) * 1.25, 4, 15);
  return Math.max(1, base * Math.sqrt(Math.max(.000001, zoom)));
}

export function zoomViewport(viewport, zoom, anchor) {
  const world = unprojectPoint(anchor, viewport);
  return { zoom, pan: { x: anchor.x - world.x * zoom, y: anchor.y - world.y * zoom } };
}

export function segmentVisible(from, to, width, height, margin = 0) {
  return visibleSegment(from.x, from.y, to.x, to.y, width, height, margin);
}

function visibleSegment(x1, y1, x2, y2, width, height, margin) {
  let from = viewportCode(x1, y1, width, height, margin), to = viewportCode(x2, y2, width, height, margin);
  for (let attempt = 0; attempt < 8; attempt++) {
    if (!(from | to)) return true;
    if (from & to) return false;
    const outside = from || to;
    let x, y;
    if (outside & 8) { y = height + margin; x = x1 + (x2 - x1) * (y - y1) / (y2 - y1); }
    else if (outside & 4) { y = -margin; x = x1 + (x2 - x1) * (y - y1) / (y2 - y1); }
    else if (outside & 2) { x = width + margin; y = y1 + (y2 - y1) * (x - x1) / (x2 - x1); }
    else { x = -margin; y = y1 + (y2 - y1) * (x - x1) / (x2 - x1); }
    if (outside === from) { x1 = x; y1 = y; from = viewportCode(x, y, width, height, margin); }
    else { x2 = x; y2 = y; to = viewportCode(x, y, width, height, margin); }
  }
  return false;
}

function viewportCode(x, y, width, height, margin) {
  return (x < -margin ? 1 : x > width + margin ? 2 : 0) | (y < -margin ? 4 : y > height + margin ? 8 : 0);
}

// Cytoscape retains topology and target coordinates; this view owns screen-space painting and input.
export function createGraphView(graph, { container, labels = true } = {}) {
  const win = container?.ownerDocument?.defaultView;
  if (!win || typeof win.CanvasRenderingContext2D !== "function") return null;
  const canvas = container.ownerDocument.createElement("canvas");
  let context;
  try { context = canvas.getContext("2d"); } catch { return null; }
  if (!context) return null;
  const previousPosition = container.style.position;
  if (win.getComputedStyle(container).position === "static") container.style.position = "relative";
  Object.assign(canvas.style, { position: "absolute", inset: "0", width: "100%", height: "100%", touchAction: "none", outlineOffset: "-2px" });
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", "Interactive note graph. Use arrow keys to pan, plus or minus to zoom, or the note list to open a note.");
  container.append(canvas);

  const readViewport = () => ({ zoom: graph.zoom(), pan: { ...graph.pan() } });
  let target = readViewport(), camera = readViewport(), frame = null, destroyed = false, internalViewport = false;
  let width = 0, height = 0, lastWidth = 0, lastHeight = 0, ratio = 0, lastTime = win.performance.now();
  let topologyDirty = true, metadataDirty = true, positionsDirty = new Set();
  const nodes = [], edges = [], records = new Map(), edgeRecords = new Map(), pointers = new Map(), grid = new Map();
  let cellSize = 32, gridDirty = true, gesture = null, hover = null, fadingHover = null, hoverMix = 0;
  let latestPointer = null, pointerDirty = false, inertia = null, zoomAnchor = null, hoverLabel = null;
  const media = win.matchMedia?.("(prefers-reduced-motion: reduce)");
  let reducedMotion = Boolean(media?.matches);
  const now = () => win.performance.now();
  const copyCamera = () => ({ zoom: camera.zoom, pan: { ...camera.pan } });
  const requestDraw = () => { if (!destroyed && frame === null) frame = win.requestAnimationFrame(draw); };
  const eventPoint = (event) => {
    const rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const emit = (node, type) => { if (!destroyed && !graph.destroyed() && node && !node.model.removed()) node.model.emit(type); };

  function setViewport(viewport, anchor = null) {
    if (destroyed || graph.destroyed()) return;
    internalViewport = true;
    try { graph.viewport(viewport); } finally { internalViewport = false; }
    target = readViewport(); zoomAnchor = anchor; requestDraw();
  }

  function viewportChanged() {
    if (destroyed) return;
    target = readViewport();
    if (!internalViewport) {
      // External Fit/restore is authoritative even when the next wheel event arrives before paint.
      camera = { zoom: target.zoom, pan: { ...target.pan } };
      zoomAnchor = null; inertia = null; pointerDirty = true;
    }
    requestDraw();
  }

  function zoomBy(factor, point) {
    if (destroyed || !Number.isFinite(factor) || factor <= 0) return;
    inertia = null;
    const anchor = factor > 1 && point ? point : { x: width / 2, y: height / 2 };
    const zoom = clamp(target.zoom * factor, graph.minZoom(), graph.maxZoom());
    const world = unprojectPoint(anchor, camera);
    setViewport(zoomViewport(camera, zoom, anchor), { screen: anchor, world });
  }

  function resize() {
    if (destroyed) return;
    const rect = container.getBoundingClientRect();
    width = rect.width || container.clientWidth; height = rect.height || container.clientHeight;
    if (!(width > 0 && height > 0)) return;
    const dpr = Math.max(1, Number(win.devicePixelRatio) || 1);
    const changed = width !== lastWidth || height !== lastHeight;
    if (changed || dpr !== ratio) {
      canvas.width = Math.max(1, Math.round(width * dpr)); canvas.height = Math.max(1, Math.round(height * dpr)); ratio = dpr;
      graph.resize();
      if (lastWidth && lastHeight && changed) {
        const dx = (width - lastWidth) / 2, dy = (height - lastHeight) / 2;
        camera.pan.x += dx; camera.pan.y += dy;
        setViewport({ zoom: target.zoom, pan: { x: target.pan.x + dx, y: target.pan.y + dy } });
      }
      lastWidth = width; lastHeight = height;
    }
    requestDraw();
  }

  function pose(node, time) {
    const progress = node.duration ? ease((time - node.started) / node.duration) : 1;
    const previousX = node.x, previousY = node.y;
    node.x = node.fromX + (node.targetX - node.fromX) * progress;
    node.y = node.fromY + (node.targetY - node.fromY) * progress;
    if (node.x !== previousX || node.y !== previousY) gridDirty = true;
    return progress < 1;
  }

  function metadata(node) {
    const data = node.model.data();
    const label = String(data.label ?? data.path ?? node.id);
    if (node.label !== label) { node.label = label; node.shortLabel = undefined; }
    node.current = data.current === "yes"; node.missing = Boolean(data.missing) || node.model.hasClass("missing");
    node.selected = node.model.selected(); node.dimmed = node.model.hasClass("dimmed");
    node.neighbor = node.model.hasClass("neighbor"); node.modelHovered = node.model.hasClass("hovered");
  }

  function syncScene(time) {
    if (topologyDirty) {
      const wanted = new Set(); nodes.length = 0; edges.length = 0;
      graph.nodes().forEach((model) => {
        const id = model.id(), position = model.position(); wanted.add(id);
        let node = records.get(id);
        if (!node) {
          node = { id, x: position.x, y: position.y, fromX: position.x, fromY: position.y, targetX: position.x, targetY: position.y, duration: 0, started: time };
          records.set(id, node);
        }
        node.model = model; node.adjacent = new Set(); node.degree = 0; metadata(node); nodes.push(node);
        positionsDirty.add(id);
      });
      for (const id of records.keys()) if (!wanted.has(id)) records.delete(id);
      const wantedEdges = new Set();
      graph.edges().forEach((model) => {
        const data = model.data(), source = records.get(data.source), destination = records.get(data.target);
        if (!source || !destination) return;
        source.degree += 1; destination.degree += 1; source.adjacent.add(destination.id); destination.adjacent.add(source.id);
        const id = model.id(); wantedEdges.add(id);
        let edge = edgeRecords.get(id);
        if (!edge) { edge = {}; edgeRecords.set(id, edge); }
        Object.assign(edge, { model, source, destination, relation: data.layer === "relation" || model.hasClass("relation"), selected: model.selected() });
        edges.push(edge);
      });
      for (const id of edgeRecords.keys()) if (!wantedEdges.has(id)) edgeRecords.delete(id);
      for (const node of nodes) node.baseRadius = graphNodeRadius(node.degree, 1);
      if (hover && !records.has(hover.id)) setHover(null);
      if (fadingHover && !records.has(fadingHover.id)) fadingHover = null;
      topologyDirty = false; metadataDirty = false; gridDirty = true;
    } else if (metadataDirty) {
      for (const node of nodes) metadata(node);
      for (const edge of edges) { edge.relation = edge.model.data("layer") === "relation" || edge.model.hasClass("relation"); edge.selected = edge.model.selected(); }
      metadataDirty = false;
    }
    for (const id of positionsDirty) {
      const node = records.get(id); if (!node) continue;
      const next = node.model.position();
      if (!Number.isFinite(next.x) || !Number.isFinite(next.y) || next.x === node.targetX && next.y === node.targetY) continue;
      pose(node, time);
      node.fromX = node.x; node.fromY = node.y; node.targetX = next.x; node.targetY = next.y;
      node.started = time; node.duration = reducedMotion || gesture?.node === node ? 0 : 250;
      gridDirty = true;
    }
    positionsDirty.clear();
  }

  function rebuildGrid() {
    grid.clear();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of nodes) { minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x); minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y); }
    cellSize = clamp(Math.sqrt(Math.max(1, maxX - minX) * Math.max(1, maxY - minY) / Math.max(1, nodes.length)) * 2, 16, 128);
    for (const node of nodes) {
      const key = `${Math.floor(node.x / cellSize)},${Math.floor(node.y / cellSize)}`;
      let bucket = grid.get(key); if (!bucket) grid.set(key, bucket = []); bucket.push(node);
    }
    gridDirty = false;
  }

  function hit(point) {
    if (point.x < 0 || point.x > width || point.y < 0 || point.y > height) return null;
    if (gridDirty) rebuildGrid();
    const world = unprojectPoint(point, camera), reach = (15 * Math.sqrt(camera.zoom) + 4) / camera.zoom;
    const left = Math.floor((world.x - reach) / cellSize), right = Math.floor((world.x + reach) / cellSize);
    const top = Math.floor((world.y - reach) / cellSize), bottom = Math.floor((world.y + reach) / cellSize);
    let result = null, distance = Infinity;
    const check = (node) => {
      const squared = (node.x * camera.zoom + camera.pan.x - point.x) ** 2 + (node.y * camera.zoom + camera.pan.y - point.y) ** 2;
      if (squared <= (graphNodeRadius(node.degree, camera.zoom) + 4) ** 2 && squared <= distance) { result = node; distance = squared; }
    };
    if ((right - left + 1) * (bottom - top + 1) > 256) nodes.forEach(check);
    else for (let x = left; x <= right; x++) for (let y = top; y <= bottom; y++) grid.get(`${x},${y}`)?.forEach(check);
    return result;
  }

  function setHover(next) {
    if (next === hover) return;
    const previous = hover; hover = next;
    emit(previous, "mouseout"); emit(next, "mouseover");
    if (next) fadingHover = next;
    canvas.style.cursor = gesture?.dragged ? "grabbing" : next ? "pointer" : "grab";
    requestDraw();
  }

  function nodeAlpha(node) {
    const related = fadingHover && (node === fadingHover || fadingHover.adjacent.has(node.id));
    return related ? 1 : (node.dimmed && !fadingHover ? .2 : 1 - .8 * hoverMix);
  }

  function fitLabel(text, maxWidth, limit = 200) {
    const characters = [];
    for (const character of text) { if (characters.length === limit) break; characters.push(character); }
    const candidate = characters.join(""), measured = context.measureText(candidate).width;
    if (candidate.length === text.length && measured <= maxWidth) return { text: candidate, width: measured };
    if (context.measureText("…").width > maxWidth) return { text: "", width: 0 };
    let from = 0, to = characters.length;
    while (from < to) {
      const middle = Math.ceil((from + to) / 2);
      if (context.measureText(characters.slice(0, middle).join("") + "…").width <= maxWidth) from = middle;
      else to = middle - 1;
    }
    const fitted = characters.slice(0, from).join("") + "…";
    return { text: fitted, width: context.measureText(fitted).width };
  }

  function paint() {
    context.setTransform(ratio, 0, 0, ratio, 0, 0); context.clearRect(0, 0, width, height);
    const growth = Math.sqrt(camera.zoom);
    for (const node of nodes) {
      node.screenX = node.x * camera.zoom + camera.pan.x; node.screenY = node.y * camera.zoom + camera.pan.y;
      node.radius = Math.max(1, node.baseRadius * growth);
    }
    for (const relation of [false, true]) for (const highlighted of [false, true]) {
      context.beginPath();
      for (const edge of edges) {
        const incident = Boolean(fadingHover && (edge.source === fadingHover || edge.destination === fadingHover));
        if (edge.relation !== relation || (incident || edge.selected) !== highlighted) continue;
        const source = edge.source, destination = edge.destination;
        if (edge.source === edge.destination) {
          const radius = source.radius * 2;
          if (source.screenX < -radius * 4 || source.screenX > width + radius * 4 || source.screenY < -radius * 4 || source.screenY > height + radius * 4) continue;
          context.moveTo(source.screenX + radius * 2, source.screenY - radius);
          context.arc(source.screenX + radius, source.screenY - radius, radius, 0, Math.PI * 2);
        } else {
          if (!visibleSegment(source.screenX, source.screenY, destination.screenX, destination.screenY, width, height, relation ? 20 : 2)) continue;
          const dx = destination.screenX - source.screenX, dy = destination.screenY - source.screenY, length = Math.hypot(dx, dy);
          if (length <= source.radius + destination.radius) continue;
          const fromX = source.screenX + dx / length * source.radius, fromY = source.screenY + dy / length * source.radius;
          const toX = destination.screenX - dx / length * destination.radius, toY = destination.screenY - dy / length * destination.radius;
          context.moveTo(fromX, fromY);
          if (relation) {
            context.quadraticCurveTo((fromX + toX) / 2 - dy / length * 12, (fromY + toY) / 2 + dx / length * 12, toX, toY);
          } else context.lineTo(toX, toY);
        }
      }
      context.strokeStyle = highlighted ? "#8760b9" : relation ? "#aa8ccf" : "#bcb9c3";
      context.globalAlpha = highlighted ? .8 : .45 * (1 - .8 * hoverMix);
      context.lineWidth = highlighted ? 1.4 : .8; context.setLineDash(relation ? [4, 4] : []); context.stroke();
    }
    context.setLineDash([]);
    const normalFont = 12 * Math.sqrt(camera.zoom);
    const labelOpacity = clamp(Math.log2(camera.zoom) + 1, 0, 1);
    context.textBaseline = "top"; context.textAlign = "center";
    for (const node of nodes) {
      const x = node.screenX, y = node.screenY, radius = node.radius;
      if (x < -radius - 4 || x > width + radius + 4 || y < -radius - 4 || y > height + radius + 4) continue;
      const hovered = node === hover || node.modelHovered;
      context.globalAlpha = nodeAlpha(node); context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2);
      context.fillStyle = hovered || node.selected || node.current || node.neighbor ? "#7851b6" : node.missing ? "#d8cbea" : "#68666d"; context.fill();
      if (node.missing || hovered) {
        context.beginPath(); context.arc(x, y, radius + (hovered ? 2.5 : 0), 0, Math.PI * 2);
        context.strokeStyle = hovered ? "#8b65bd" : "#9b80bf"; context.lineWidth = 1; context.setLineDash(node.missing && !hovered ? [2, 2] : []); context.stroke(); context.setLineDash([]);
      }
      if (labels && labelOpacity > .01 && !hovered) {
        context.globalAlpha = nodeAlpha(node) * labelOpacity; context.fillStyle = "#57535f";
        if (node.shortLabel === undefined) {
          // Measure once at the base size; zoom scales glyphs and label width together.
          context.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
          node.shortLabel = fitLabel(node.label, 220, 50).text;
        }
        context.font = `${normalFont}px -apple-system, BlinkMacSystemFont, sans-serif`;
        context.fillText(node.shortLabel, x, y + radius + 5);
      }
    }
    if (hover && records.has(hover.id)) {
      const point = projectPoint(hover, camera), radius = graphNodeRadius(hover.degree, camera.zoom), font = Math.max(15, 12 * Math.sqrt(camera.zoom));
      context.globalAlpha = 1; context.font = `${font}px -apple-system, BlinkMacSystemFont, sans-serif`;
      const maxWidth = Math.max(1, Math.min(340, width - 8));
      if (!hoverLabel || hoverLabel.source !== hover.label || hoverLabel.font !== font || hoverLabel.maxWidth !== maxWidth) {
        hoverLabel = { source: hover.label, font, maxWidth, ...fitLabel(hover.label, maxWidth) };
      }
      const labelWidth = hoverLabel.width;
      const x = clamp(point.x, labelWidth / 2 + 4, Math.max(labelWidth / 2 + 4, width - labelWidth / 2 - 4));
      const y = clamp(point.y + radius + 15, 2, Math.max(2, height - font - 2));
      context.lineWidth = 4; context.strokeStyle = "#ffffff"; context.strokeText(hoverLabel.text, x, y);
      context.fillStyle = "#684297"; context.fillText(hoverLabel.text, x, y);
    }
    context.globalAlpha = 1;
  }

  function draw(time) {
    frame = null;
    if (destroyed || graph.destroyed() || !(width > 0 && height > 0)) return;
    const elapsed = Math.min(50, Math.max(1, time - lastTime)); lastTime = time;
    syncScene(time);
    let moving = false;
    for (const node of nodes) if (pose(node, time)) { moving = true; gridDirty = true; }
    if (inertia && !reducedMotion) {
      const decay = Math.exp(-elapsed / 180); inertia.x *= decay; inertia.y *= decay;
      if (Math.hypot(inertia.x, inertia.y) < .02) inertia = null;
      else {
        const pan = { x: target.pan.x + inertia.x * elapsed, y: target.pan.y + inertia.y * elapsed };
        setViewport({ zoom: target.zoom, pan }); moving = true;
      }
    }
    const direct = reducedMotion || Boolean(gesture?.dragged) || gesture?.kind === "pinch";
    const amount = direct ? 1 : 1 - Math.exp(-elapsed / 100);
    const previousZoom = camera.zoom, previousX = camera.pan.x, previousY = camera.pan.y;
    camera.zoom = Math.exp(Math.log(camera.zoom) + (Math.log(target.zoom) - Math.log(camera.zoom)) * amount);
    if (zoomAnchor) {
      camera.pan.x = zoomAnchor.screen.x - zoomAnchor.world.x * camera.zoom;
      camera.pan.y = zoomAnchor.screen.y - zoomAnchor.world.y * camera.zoom;
    } else {
      camera.pan.x += (target.pan.x - camera.pan.x) * amount; camera.pan.y += (target.pan.y - camera.pan.y) * amount;
    }
    const unsettled = Math.abs(Math.log(camera.zoom / target.zoom)) > .0001 || Math.hypot(camera.pan.x - target.pan.x, camera.pan.y - target.pan.y) > .05;
    if (!unsettled) { camera = { zoom: target.zoom, pan: { ...target.pan } }; zoomAnchor = null; }
    const geometryMoved = gridDirty || previousZoom !== camera.zoom || previousX !== camera.pan.x || previousY !== camera.pan.y;
    if (gridDirty) rebuildGrid();
    if ((pointerDirty || geometryMoved) && latestPointer && !gesture?.dragged) { pointerDirty = false; setHover(hit(latestPointer)); }
    const desiredMix = hover ? 1 : 0;
    hoverMix += (desiredMix - hoverMix) * (reducedMotion ? 1 : 1 - Math.exp(-elapsed / 150));
    if (Math.abs(desiredMix - hoverMix) < .005) hoverMix = desiredMix;
    if (!hoverMix && !hover) fadingHover = null;
    paint();
    graph.emit("render");
    if (moving || unsettled || hoverMix !== desiredMix) requestDraw();
  }

  function capture(event) { try { canvas.setPointerCapture(event.pointerId); } catch { /* The pointer may already have ended. */ } }
  function release(id) { try { if (canvas.hasPointerCapture?.(id)) canvas.releasePointerCapture(id); } catch { /* The browser may have released it. */ } }
  function beginPinch() {
    const pair = [...pointers.values()].slice(0, 2), center = { x: (pair[0].x + pair[1].x) / 2, y: (pair[0].y + pair[1].y) / 2 };
    if (gesture?.node && gesture.dragged) emit(gesture.node, "free");
    gesture = { kind: "pinch", distance: Math.max(1, Math.hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y)), zoom: camera.zoom, world: unprojectPoint(center, camera), dragged: true };
    inertia = null; setHover(null);
  }
  function pointerDown(event) {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.preventDefault(); canvas.focus({ preventScroll: true }); capture(event);
    const point = eventPoint(event); pointers.set(event.pointerId, point); latestPointer = point; inertia = null;
    if (pointers.size >= 2) { beginPinch(); return; }
    syncScene(now()); if (gridDirty) rebuildGrid();
    const node = hit(point), world = unprojectPoint(point, camera);
    gesture = { kind: node ? "node" : "pan", node, id: event.pointerId, start: point, previous: point,
      camera: copyCamera(), offset: node ? { x: world.x - node.x, y: world.y - node.y } : null,
      velocity: { x: 0, y: 0 }, time: now(), dragged: false };
    setViewport(copyCamera()); setHover(node);
  }
  function pointerMove(event) {
    const point = eventPoint(event); latestPointer = point;
    if (!pointers.has(event.pointerId)) { pointerDirty = true; requestDraw(); return; }
    pointers.set(event.pointerId, point);
    if (gesture?.kind === "pinch") {
      const pair = [...pointers.values()].slice(0, 2); if (pair.length < 2) return;
      const center = { x: (pair[0].x + pair[1].x) / 2, y: (pair[0].y + pair[1].y) / 2 };
      const zoom = clamp(gesture.zoom * Math.hypot(pair[1].x - pair[0].x, pair[1].y - pair[0].y) / gesture.distance, graph.minZoom(), graph.maxZoom());
      setViewport({ zoom, pan: { x: center.x - gesture.world.x * zoom, y: center.y - gesture.world.y * zoom } }); return;
    }
    if (!gesture || gesture.id !== event.pointerId) return;
    if (!gesture.dragged && Math.hypot(point.x - gesture.start.x, point.y - gesture.start.y) >= 4) {
      gesture.dragged = true; if (gesture.node) emit(gesture.node, "grab"); canvas.style.cursor = "grabbing";
    }
    if (!gesture.dragged) return;
    const time = now(), elapsed = Math.max(1, time - gesture.time);
    gesture.velocity.x = clamp(.6 * gesture.velocity.x + .4 * (point.x - gesture.previous.x) / elapsed, -3, 3);
    gesture.velocity.y = clamp(.6 * gesture.velocity.y + .4 * (point.y - gesture.previous.y) / elapsed, -3, 3);
    gesture.previous = point; gesture.time = time;
    if (gesture.node && records.has(gesture.node.id)) {
      const world = unprojectPoint(point, camera), node = gesture.node;
      const position = { x: world.x - gesture.offset.x, y: world.y - gesture.offset.y };
      node.model.position(position); node.x = node.fromX = node.targetX = position.x; node.y = node.fromY = node.targetY = position.y; node.duration = 0;
      gridDirty = true; emit(node, "drag"); requestDraw();
    } else setViewport({ zoom: gesture.camera.zoom, pan: { x: gesture.camera.pan.x + point.x - gesture.start.x, y: gesture.camera.pan.y + point.y - gesture.start.y } });
  }
  function pointerUp(event) {
    if (!pointers.has(event.pointerId)) return;
    const point = eventPoint(event), last = pointers.get(event.pointerId);
    if (point.x !== last.x || point.y !== last.y) pointerMove(event);
    const previous = gesture; pointers.delete(event.pointerId); release(event.pointerId);
    if (previous?.kind === "pinch") {
      camera = { zoom: target.zoom, pan: { ...target.pan } };
      const remaining = [...pointers.entries()][0];
      gesture = remaining ? { kind: "pan", id: remaining[0], start: remaining[1], previous: remaining[1], camera: copyCamera(), velocity: { x: 0, y: 0 }, time: now(), dragged: true } : null;
    } else {
      gesture = null;
      if (previous?.node) emit(previous.node, previous.dragged ? "free" : "tap");
      else if (previous?.dragged && !reducedMotion && now() - previous.time < 80) inertia = { ...previous.velocity };
    }
    canvas.style.cursor = hover ? "pointer" : "grab"; pointerDirty = true; requestDraw();
  }
  function cancelPointers(notify = true) {
    const previous = gesture, ids = [...pointers.keys()];
    pointers.clear(); gesture = null; inertia = null;
    for (const id of ids) release(id);
    if (notify && previous?.node && previous.dragged) emit(previous.node, "free");
    setHover(null);
  }
  function wheel(event) {
    event.preventDefault(); cancelPointers();
    latestPointer = eventPoint(event); pointerDirty = true;
    const unit = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1;
    zoomBy(1.5 ** clamp(-event.deltaY * unit / 120, -4, 4), eventPoint(event));
  }
  function keydown(event) {
    if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
    const shifts = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
    if (shifts[event.key]) {
      event.preventDefault(); inertia = null; const [x, y] = shifts[event.key];
      setViewport({ zoom: target.zoom, pan: { x: target.pan.x + x, y: target.pan.y + y } });
    } else if (["+", "=", "-"].includes(event.key)) { event.preventDefault(); zoomBy(event.key === "-" ? .8 : 1.25); }
  }
  const leave = () => { if (!pointers.size) { latestPointer = null; pointerDirty = false; setHover(null); } };
  const topologyChanged = () => { topologyDirty = true; requestDraw(); };
  const dataChanged = () => { metadataDirty = true; requestDraw(); };
  const positionChanged = (event) => { positionsDirty.add(event.target.id()); requestDraw(); };
  const motionChanged = () => {
    reducedMotion = Boolean(media.matches); if (reducedMotion) inertia = null;
    if (reducedMotion) for (const node of nodes) node.duration = 0;
    requestDraw();
  };
  const handlers = { pointerdown: pointerDown, pointermove: pointerMove, pointerup: pointerUp, pointercancel: () => cancelPointers(),
    lostpointercapture: (event) => { if (pointers.has(event.pointerId)) cancelPointers(); }, pointerleave: leave, wheel, keydown };
  for (const [type, handler] of Object.entries(handlers)) canvas.addEventListener(type, handler, type === "wheel" ? { passive: false } : undefined);
  graph.on("pan zoom", viewportChanged); graph.on("add remove", topologyChanged); graph.on("data style select unselect", dataChanged);
  graph.on("position", "node", positionChanged);
  graph.on("destroy", destroy);
  win.addEventListener("resize", resize);
  media?.addEventListener?.("change", motionChanged);
  const observer = typeof win.ResizeObserver === "function" ? new win.ResizeObserver(resize) : null;
  observer?.observe(container);
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (frame !== null) win.cancelAnimationFrame(frame);
    cancelPointers(false); observer?.disconnect(); win.removeEventListener("resize", resize); media?.removeEventListener?.("change", motionChanged);
    for (const [type, handler] of Object.entries(handlers)) canvas.removeEventListener(type, handler);
    graph.off("pan zoom", viewportChanged); graph.off("add remove", topologyChanged); graph.off("data style select unselect", dataChanged);
    graph.off("position", "node", positionChanged); graph.off("destroy", destroy);
    records.clear(); edgeRecords.clear(); nodes.length = edges.length = 0; positionsDirty.clear(); grid.clear(); canvas.remove();
    if (container.style.position === "relative" && previousPosition !== "relative") container.style.position = previousPosition;
  }
  resize(); requestDraw();
  return { setOptions(options) { if (typeof options?.labels === "boolean") { labels = options.labels; requestDraw(); } }, resize, zoomBy, destroy };
}
