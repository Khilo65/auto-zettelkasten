import { indexWorkerMessage } from "./workspace_index_worker.js";

// Raw note bodies live only for a page. The persistent catalogue keeps projections.
export class LibraryParser {
  constructor() {
    this.pending = new Map();
    this.sequence = 0;
    this.closed = false;
    if (typeof Worker === "function") {
      this.worker = new Worker("/workspace-index-worker.js");
      this.worker.onmessage = ({ data }) => {
        const pending = this.pending.get(data?.id);
        if (!pending) return;
        this.pending.delete(data.id); clearTimeout(pending.timer);
        if (data.error || !Array.isArray(data.documents)) pending.reject(new Error(data.error || "Invalid library indexing response."));
        else pending.resolve(data.documents);
      };
      this.worker.onerror = () => this.close("Library parsing could not finish. Refresh the workspace to try again.");
    }
  }

  async parse(documents) {
    if (this.closed) throw new Error("Library indexing was cancelled.");
    const id = ++this.sequence;
    if (!this.worker) {
      // Layout-free browsers and tests use the same parser with a yield per note.
      const result = [];
      for (const document of documents) {
        await new Promise((resolve) => setTimeout(resolve, 0));
        if (this.closed) throw new Error("Library indexing was cancelled.");
        const page = indexWorkerMessage({ id, documents: [document] });
        if (page.error) throw new Error(page.error);
        result.push(...page.documents);
      }
      return result;
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.close("Library parsing timed out. Refresh the workspace to try again."), 30000);
      this.pending.set(id, { resolve, reject, timer });
      try { this.worker.postMessage({ id, documents }); }
      catch (error) { this.close(error.message); }
    });
  }

  close(reason = "Library indexing was cancelled.") {
    this.closed = true; this.worker?.terminate();
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error(reason)); }
    this.pending.clear();
  }
}
