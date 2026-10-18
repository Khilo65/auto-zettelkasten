import { LIBRARY_LIMITS, parseLibraryDocument } from "./workspace_links.js";

export function indexWorkerMessage(message) {
  const id = message?.id;
  if ((typeof id !== "string" && typeof id !== "number") || !Array.isArray(message?.documents) || message.documents.length > LIBRARY_LIMITS.pageDocuments) return { id, error: "Invalid library indexing page." };
  let characters = 0;
  for (const document of message.documents) {
    if (!document || typeof document.path !== "string" || typeof document.text !== "string") return { id, error: "Invalid library note source." };
    characters += document.text.length;
    if (characters > LIBRARY_LIMITS.pageCharacters) return { id, error: "Library indexing page exceeds the source limit." };
  }
  try { return { id, documents: message.documents.map(parseLibraryDocument) }; }
  catch { return { id, error: "Library indexing page could not be parsed safely." }; }
}

if (typeof self !== "undefined" && typeof self.postMessage === "function" && typeof document === "undefined") {
  self.addEventListener("message", (event) => self.postMessage(indexWorkerMessage(event.data)));
}
