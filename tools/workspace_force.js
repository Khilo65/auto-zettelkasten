import { forceSimulation, forceLink, forceManyBody, forceCollide, forceCenter, forceX, forceY } from "d3-force";

export const FORCE_LIMITS = Object.freeze({ nodes: 50000, edges: 1000000, ticks: 120, milliseconds: 8000, publishMilliseconds: 250 });

// The worker receives integer topology and positions, without note text or paths.
export async function runForceLayout(input, { onPositions, onControls, cancelled = () => false, yieldControl = () => new Promise((resolve) => setTimeout(resolve, 0)) } = {}) {
  const positions = input?.positions, links = input?.links;
  if (!(positions instanceof Float32Array) || positions.length % 2 || positions.length / 2 > FORCE_LIMITS.nodes
      || !(links instanceof Uint32Array) || links.length % 2 || links.length / 2 > FORCE_LIMITS.edges) throw new Error("Invalid or oversized graph layout input.");
  const count = positions.length / 2;
  if (positions.some((position) => !Number.isFinite(position)) || links.some((index) => index >= count)) throw new Error("Invalid graph layout coordinates or endpoints.");
  const nodes = Array.from({ length: count }, (_, index) => input.randomize === false ? { index, x: positions[index * 2], y: positions[index * 2 + 1] } : { index });
  const edges = Array.from({ length: links.length / 2 }, (_, index) => ({ source: links[index * 2], target: links[index * 2 + 1] }));
  const iterations = Number.isInteger(input.iterations) && input.iterations > 0 ? Math.min(input.iterations, FORCE_LIMITS.ticks) : FORCE_LIMITS.ticks;
  const simulation = forceSimulation(nodes).stop()
    .alphaDecay(1 - Math.pow(.001, 1 / iterations))
    .force("link", forceLink(edges).distance(55))
    .force("charge", forceManyBody().strength(-70).theta(.9).distanceMin(12))
    .force("collision", forceCollide(8).iterations(1))
    .force("center", forceCenter())
    .force("x", forceX().strength(.002)).force("y", forceY().strength(.002));
  const started = performance.now();
  let iteration = 0, published = started, settled = false;
  const controllable = (index) => !settled && !cancelled() && Number.isInteger(index) && index >= 0 && index < count
    && iteration < iterations && performance.now() - started < FORCE_LIMITS.milliseconds;
  const controls = {
    pin(index, point) {
      if (!controllable(index) || !Number.isFinite(point?.x) || !Number.isFinite(point?.y)
          || !Number.isFinite(Math.fround(point.x)) || !Number.isFinite(Math.fround(point.y))) return false;
      const node = nodes[index];
      node.x = node.fx = point.x; node.y = node.fy = point.y; node.vx = node.vy = 0;
      simulation.alpha(Math.max(.3, simulation.alpha())).alphaTarget(.15);
      return true;
    },
    release(index) {
      if (!controllable(index)) return false;
      nodes[index].fx = nodes[index].fy = null;
      simulation.alpha(Math.max(.2, simulation.alpha())).alphaTarget(0);
      return true;
    },
  };
  const publish = (done) => {
    const result = new Float32Array(count * 2);
    for (let index = 0; index < count; index += 1) {
      result[index * 2] = nodes[index].x; result[index * 2 + 1] = nodes[index].y;
      if (!Number.isFinite(result[index * 2]) || !Number.isFinite(result[index * 2 + 1])) throw new Error("Graph layout produced invalid coordinates.");
    }
    onPositions?.({ positions: result, iteration, done, elapsedMs: performance.now() - started });
    published = performance.now();
  };
  try {
    onControls?.(controls);
    while (iteration < iterations && performance.now() - started < FORCE_LIMITS.milliseconds) {
      if (cancelled()) return { status: "cancelled", iteration, elapsedMs: performance.now() - started };
      const batch = performance.now();
      do { simulation.tick(); iteration += 1; }
      while (iteration < iterations && performance.now() - batch < 8 && performance.now() - started < FORCE_LIMITS.milliseconds);
      if (performance.now() - published >= FORCE_LIMITS.publishMilliseconds && iteration < iterations) publish(false);
      await yieldControl();
    }
    if (cancelled()) return { status: "cancelled", iteration, elapsedMs: performance.now() - started };
    publish(true);
    return { status: "complete", iteration, elapsedMs: performance.now() - started, budgetLimited: iteration < iterations };
  } finally { settled = true; simulation.stop(); }
}
