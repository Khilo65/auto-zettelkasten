// Community presentation is derived metadata. It never creates graph edges.
const scales = new Set(["broad", "default", "fine"]);
export function graphCommunityRows(map, scale = "default") {
  if (!map || !["current", "partial"].includes(map.status)) return [];
  const supplied = (Array.isArray(map.community_scales) ? map.community_scales : []).find((row) => row?.scale === scale);
  const rows = supplied ? supplied.communities : scale === "default" ? map.communities : [];
  return (Array.isArray(rows) ? rows : []).filter((row) => row && row.algorithm === "leiden" && row.noise !== true
    && row.stale !== true && !["stale", "unavailable"].includes(row.status) && /^[A-Za-z0-9._:-]{1,256}$/.test(row.community_id || "") && Array.isArray(row.paths));
}
export function graphCommunityScales(map) {
  const available = new Set(graphCommunityRows(map).length ? ["default"] : []);
  for (const row of Array.isArray(map?.community_scales) ? map.community_scales : []) if (scales.has(row?.scale) && graphCommunityRows(map, row.scale).length) available.add(row.scale);
  return ["broad", "default", "fine"].filter((scale) => available.has(scale));
}
export function graphCommunities(data, scale = "default") {
  const nodes = new Map((data?.nodes || []).filter((node) => !node.missing).map((node) => [node.path, node]));
  return graphCommunityRows(data?.semantic_map, scale).map((row) => ({ ...row, scale,
    label: typeof row.display_label === "string" && row.display_label.trim() ? row.display_label.trim().slice(0, 160) : `Community ${row.community_id.replace(/^semantic-/, "").slice(0, 8)}`,
    members: [...new Set(row.paths)].map((path) => nodes.get(path)).filter(Boolean),
  })).filter((row) => row.members.length).sort((a, b) => b.members.length - a.members.length || a.community_id.localeCompare(b.community_id));
}
export function focusGraphCommunity(data, communityId, scale = "default") {
  const selected = graphCommunities(data, scale).find((row) => row.community_id === communityId);
  if (!selected) return data;
  const members = new Set(selected.members.map((node) => node.id)), included = new Set(members);
  for (const edge of data.edges || []) {
    if (members.has(edge.source)) included.add(edge.target);
    if (members.has(edge.target)) included.add(edge.source);
  }
  const nodes = data.nodes.filter((node) => included.has(node.id)).map((node) => ({ ...node, community_bridge: !members.has(node.id) }));
  const edges = data.edges.filter((edge) => included.has(edge.source) && included.has(edge.target));
  // Preserve genuine edge degree only. Membership itself adds no connection.
  const degrees = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) { degrees.set(edge.source, degrees.get(edge.source) + 1); degrees.set(edge.target, degrees.get(edge.target) + 1); }
  return { ...data, nodes: nodes.map((node) => ({ ...node, degree: degrees.get(node.id) })), edges };
}
export function communityHull(points) {
  const sorted = points.map((point) => ({ x: point.x, y: point.y })).sort((a, b) => a.x - b.x || a.y - b.y);
  if (sorted.length < 3) return sorted;
  const cross = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
  const half = (rows) => { const hull = []; for (const p of rows) { while (hull.length >= 2 && cross(hull.at(-2), hull.at(-1), p) <= 0) hull.pop(); hull.push(p); } return hull; };
  const lower = half(sorted), upper = half([...sorted].reverse()); lower.pop(); upper.pop(); return [...lower, ...upper];
}

export function limitGraphDiscovery(data, cap = 8) {
  if (cap === "all") return { ...data, hidden_discovery_edges: 0 };
  if (![8, 16, 32].includes(cap)) cap = 8;
  const degree = new Map(), retained = new Set();
  const candidates = data.edges.filter((edge) => edge.layer === "discovery" && edge.type === "semantic_similarity")
    .sort((a, b) => (Number.isFinite(b.score) ? b.score : -1) - (Number.isFinite(a.score) ? a.score : -1) || a.id.localeCompare(b.id));
  for (const edge of candidates) {
    if ((degree.get(edge.source) || 0) >= cap || (degree.get(edge.target) || 0) >= cap) continue;
    retained.add(edge.id); degree.set(edge.source, (degree.get(edge.source) || 0) + 1); degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  const edges = data.edges.filter((edge) => edge.layer !== "discovery" || edge.type !== "semantic_similarity" || retained.has(edge.id));
  const allDegrees = new Map(data.nodes.map((node) => [node.id, 0]));
  for (const edge of edges) { allDegrees.set(edge.source, (allDegrees.get(edge.source) || 0) + 1); allDegrees.set(edge.target, (allDegrees.get(edge.target) || 0) + 1); }
  return { ...data, nodes: data.nodes.map((node) => ({ ...node, degree: allDegrees.get(node.id) })), edges, hidden_discovery_edges: candidates.length - retained.size };
}

export function filterGraphDiscoveryScopes(data, scope = "") {
  if (!["within_collection", "whole_library"].includes(scope)) return { ...data, hidden_discovery_scope_edges: 0 };
  const edges = data.edges.filter((edge) => edge.layer !== "discovery" || edge.type !== "semantic_similarity"
    || Array.isArray(edge.provenance?.discovery_scopes) && edge.provenance.discovery_scopes.includes(scope));
  const degrees = new Map(data.nodes.map((node) => [node.id, 0]));
  for (const edge of edges) { degrees.set(edge.source, (degrees.get(edge.source) || 0) + 1); degrees.set(edge.target, (degrees.get(edge.target) || 0) + 1); }
  return { ...data, nodes: data.nodes.map((node) => ({ ...node, degree: degrees.get(node.id) })), edges,
    hidden_discovery_scope_edges: data.edges.length - edges.length };
}

export function appendCommunityConnections(container, community, data, { onOpen = () => {} } = {}) {
  const doc = container.ownerDocument, ids = new Set(community.members.map((node) => node.id));
  const rows = data.edges.filter((edge) => ids.has(edge.source) || ids.has(edge.target));
  const byId = new Map(data.nodes.map((node) => [node.id, node]));
  const details = doc.createElement("details"), summary = doc.createElement("summary");
  details.className = "relationship-details community-connections"; summary.textContent = `Connections · ${rows.length}`; details.append(summary); container.append(details);
  let shown = 0;
  const page = () => {
    details.querySelector(".community-connections-more")?.remove();
    for (const edge of rows.slice(shown, shown + 50)) {
      const record = doc.createElement("details"), heading = doc.createElement("summary"); record.className = "relationship-record";
      const label = (id) => byId.get(id)?.label || id;
      heading.textContent = `${label(edge.source)} ${String(edge.type || "links to").replace(/_/g, " ")} ${label(edge.target)}`; record.append(heading);
      const description = doc.createElement("p");
      description.textContent = edge.layer === "discovery" ? `Discovery suggestion${Number.isFinite(edge.score) ? ` · similarity ${edge.score.toFixed(3)}` : ""}. Not an accepted claim relationship.${edge.derived === true ? " Current mapped note revisions." : ""}`
        : edge.layer === "citation" ? "Citation record. A reference connection does not establish agreement."
          : edge.layer === "relation" ? `Saved research relationship${Number.isFinite(edge.confidence) ? ` · model confidence ${Math.round(edge.confidence * 100)}%` : ""}.${edge.evidence_basis === "note_summary" ? " Based on note summaries." : ""}` : "Saved Markdown link.";
      record.append(description);
      if (edge.layer === "discovery" && Array.isArray(edge.provenance?.discovery_scopes)) {
        const origins = edge.provenance.discovery_scopes.filter((scope) => ["within_collection", "whole_library"].includes(scope))
          .map((scope) => scope === "within_collection" ? "Within-collection search" : "Whole-library search");
        if (origins.length) {
          const origin = doc.createElement("p"), shared = edge.provenance.shared_collection_keys;
          origin.textContent = `Discovered by: ${origins.join(" and ")}.${Array.isArray(shared) && shared.length ? ` ${shared.length} shared ${shared.length === 1 ? "collection" : "collections"}.` : ""}`;
          record.append(origin);
        }
      }
      if (Number.isFinite(edge.confidence)) description.title = "Model confidence is not a verified probability of correctness.";
      for (const id of new Set([edge.source, edge.target])) {
        const node = byId.get(id); if (!node?.path || node.missing) continue;
        const open = doc.createElement("button"); open.type = "button"; open.textContent = `Open ${node.label}`; open.addEventListener("click", () => onOpen(node.path)); record.append(open);
      }
      for (const evidence of (edge.selected_evidence || []).slice(0, 2)) {
        if (![byId.get(edge.source)?.path, byId.get(edge.target)?.path].includes(evidence.path) || typeof evidence.text !== "string") continue;
        const button = doc.createElement("button"); button.type = "button"; button.className = "relationship-evidence backlink-occurrence";
        button.textContent = `${evidence.locator?.heading || "Evidence"}: ${evidence.text.slice(0, 1600)}`;
        button.addEventListener("click", () => onOpen(evidence.path, evidence.locator)); record.append(button);
      }
      details.append(record);
    }
    shown += 50;
    if (shown < rows.length) { const more = doc.createElement("button"); more.type = "button"; more.className = "community-connections-more"; more.textContent = `Show ${Math.min(50, rows.length - shown)} more connections`; more.addEventListener("click", page); details.append(more); }
  };
  details.addEventListener("toggle", () => { if (details.open && !shown) page(); });
  return details;
}
