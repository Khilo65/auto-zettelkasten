const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

export const isZoteroScheme = (value) => typeof value === "string" && /^zotero:/i.test(value);

// Deliberately accept only source navigation, never arbitrary protocol actions.
// A personal-library URI is relative to the connected Zotero user, not a global ID.
export function parseZoteroURI(value) {
  if (typeof value !== "string" || value.length > 2048 || /[\s\x00-\x1f\x7f]/.test(value)) return null;
  const match = /^zotero:\/\/(select|open-pdf)\/(library|groups\/([1-9][0-9]*))\/items\/([A-Z0-9]{8})(?:\?([^#]+))?$/.exec(value.replace(/^zotero:\/\//i, "zotero://"));
  if (!match) return null;
  const [, action, scope, groupId, key, query] = match;
  const params = new Map();
  if (query) {
    if (action !== "open-pdf") return null;
    for (const part of query.split("&")) {
      const entry = /^(page|annotation)=([^=]+)$/.exec(part);
      if (!entry || params.has(entry[1])) return null;
      if (entry[1] === "page" && (!/^[1-9][0-9]*$/.test(entry[2]) || !Number.isSafeInteger(Number(entry[2])))) return null;
      if (entry[1] === "annotation" && !/^[A-Z0-9]{8}$/.test(entry[2])) return null;
      params.set(entry[1], entry[2]);
    }
  }
  const page = params.has("page") ? Number(params.get("page")) : null;
  const annotation = params.get("annotation") || null;
  const normalizedQuery = [page === null ? "" : `page=${page}`, annotation ? `annotation=${annotation}` : ""].filter(Boolean).join("&");
  return { action, scope, library_type: groupId ? "group" : "user", library_id: groupId || null, key,
    page, page_index: page === null ? null : page - 1, annotation_key: annotation,
    uri: `zotero://${action}/${scope}/items/${key}${normalizedQuery ? `?${normalizedQuery}` : ""}` };
}

export function sourceLinkMarkup(uri, label = uri) {
  const source = parseZoteroURI(uri);
  return source ? `<a href="#" class="source-link" data-source-uri="${escapeHtml(source.uri)}">${escapeHtml(label)}</a>` : escapeHtml(label);
}
