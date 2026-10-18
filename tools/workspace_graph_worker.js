import { runForceLayout } from "./workspace_force.js";

let active = null;

function publish(job, frame) {
  if (active !== job) return;
  if (job.inFlight !== null) { job.pending = frame; return; }
  job.inFlight = frame.iteration;
  self.postMessage({ type: "positions", requestId: job.requestId, ...frame }, [frame.positions.buffer]);
}

self.onmessage = async ({ data }) => {
  if (data?.type === "pin" || data?.type === "release") {
    if (active && !active.finished && data.requestId === active.requestId) {
      if (data.type === "pin") active.controls?.pin(data.index, { x: data.x, y: data.y });
      else active.controls?.release(data.index);
    }
    return;
  }
  if (data?.type === "ack") {
    if (active && data.requestId === active.requestId && data.iteration === active.inFlight) {
      active.inFlight = null;
      const pending = active.pending;
      active.pending = null;
      if (pending) publish(active, pending);
    }
    return;
  }
  if (data?.type !== "layout" || !Number.isSafeInteger(data.requestId)) return;
  const job = { requestId: data.requestId, inFlight: null, pending: null, controls: null, finished: false };
  active = job;
  try {
    await runForceLayout(data, {
      cancelled: () => active !== job,
      onControls: (controls) => { job.controls = controls; },
      onPositions: (frame) => publish(job, frame),
    });
  } catch {
    if (active === job) self.postMessage({ type: "error", requestId: data.requestId, error: "The graph layout could not finish." });
  } finally { job.finished = true; }
};
