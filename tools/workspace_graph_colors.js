const originLabels = Object.freeze({ managed: "Managed sources", personal: "Verified personal", unknown: "Other or unknown" });
const colorModes = new Set(["none", "origin", "community"]);
// Fixed categorical colors are keyed by identity, never by visible order or layout.
const communityHues = [255, 180, 80, 330, 20, 295, 125, 50, 220, 65, 350, 145];
const communityColors = Object.freeze({
  light: communityHues.map((hue) => `oklch(55% 0.12 ${hue})`),
  dark: communityHues.map((hue) => `oklch(80% 0.12 ${hue})`),
});

export function graphNodeOrigin(node) {
  return node && !node.missing && Object.hasOwn(node, "note_origin") && ["managed", "personal"].includes(node.note_origin) ? node.note_origin : "unknown";
}

export function normalizeGraphPreferences(input) {
  const value = input && typeof input === "object" && !Array.isArray(input) && (!Object.hasOwn(input, "version") || input.version === 1) ? input : {};
  const suppliedOrigins = Object.hasOwn(value, "origins") && value.origins && typeof value.origins === "object" && !Array.isArray(value.origins) ? value.origins : {};
  const origins = {};
  for (const origin of Object.keys(originLabels)) origins[origin] = Object.hasOwn(suppliedOrigins, origin) && typeof suppliedOrigins[origin] === "boolean" ? suppliedOrigins[origin] : true;
  return { version: 1, colorBy: Object.hasOwn(value, "colorBy") && colorModes.has(value.colorBy) ? value.colorBy : "none", origins };
}

export function graphCommunityMembership(node, scale = "default") {
  if (node?.missing || node?.semantic_stale === true || ["stale", "unavailable"].includes(node?.semantic_status)) return null;
  return (Array.isArray(node?.semantic_communities) ? node.semantic_communities : [])
    .filter((row) => row?.algorithm === "leiden" && row.noise !== true && row.stale !== true && !["stale", "unavailable"].includes(row.status)
      && typeof row.community_id === "string" && /^[A-Za-z0-9._:-]{1,256}$/.test(row.community_id))
    .filter((row) => (row.scale || "default") === scale).sort((a, b) => a.community_id.localeCompare(b.community_id))[0] || null;
}

function hashIdentity(value) {
  let hash = 2166136261;
  for (let at = 0; at < value.length; at++) hash = Math.imul(hash ^ value.charCodeAt(at), 16777619);
  return hash >>> 0;
}

export function graphNodeAppearance(node, { colorBy = "none", dark = false, communityScale = "default" } = {}) {
  const neutral = dark ? "oklch(78% 0.012 290)" : "oklch(52% 0.012 290)";
  if (colorBy === "origin") {
    const origin = graphNodeOrigin(node);
    const palette = communityColors[dark ? "dark" : "light"];
    const colors = { managed: palette[0], personal: palette[1], unknown: neutral };
    return { key: `origin:${origin}`, label: originLabels[origin], color: colors[origin] };
  }
  if (colorBy === "community") {
    const membership = graphCommunityMembership(node, communityScale), identity = membership?.community_id;
    if (!identity) return { key: "community:unassigned", label: "Unassigned or unavailable", color: neutral };
    const palette = communityColors[dark ? "dark" : "light"];
    return { key: `community:${identity}`, label: typeof membership.display_label === "string" && membership.display_label.trim() ? membership.display_label.trim().slice(0, 160) : `Community ${identity.replace(/^semantic-/, "").slice(0, 8)}`, color: palette[hashIdentity(identity) % palette.length] };
  }
  return { key: "none", label: "Notes", color: neutral };
}

export function graphLegend(nodes, options = {}) {
  const entries = new Map();
  for (const node of Array.isArray(nodes) ? nodes : []) {
    const appearance = graphNodeAppearance(node, options);
    if (!entries.has(appearance.key)) entries.set(appearance.key, { ...appearance, count: 0 });
    entries.get(appearance.key).count += 1;
  }
  const originOrder = ["origin:managed", "origin:personal", "origin:unknown"];
  return [...entries.values()].sort((left, right) => options.colorBy === "origin"
    ? originOrder.indexOf(left.key) - originOrder.indexOf(right.key) : left.key.localeCompare(right.key));
}

export function filterGraphOrigins(data, origins) {
  const selected = normalizeGraphPreferences({ origins }).origins;
  const nodes = (Array.isArray(data?.nodes) ? data.nodes : []).filter((node) => node && selected[graphNodeOrigin(node)]).map((node) => ({ ...node, degree: 0 }));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = (Array.isArray(data?.edges) ? data.edges : []).filter((edge) => edge && byId.has(edge.source) && byId.has(edge.target));
  for (const edge of edges) { byId.get(edge.source).degree += 1; byId.get(edge.target).degree += 1; }
  return { ...data, nodes, edges };
}
