const originLabels = Object.freeze({ managed: "Managed sources", personal: "Verified personal", unknown: "Other or unknown" });
const colorModes = new Set(["none", "origin", "community"]);
const originColors = Object.freeze({
  light: ["oklch(55% 0.12 255)", "oklch(55% 0.12 180)"],
  dark: ["oklch(80% 0.12 255)", "oklch(80% 0.12 180)"],
});
// Paired light/dark OKLCH samples, inside sRGB in both themes. After the first
// categorical hues, maximize the minimum OKLab distance in either theme.
// Precomputed so larger maps do not run a color-space search while painting.
const communityColors = [
  [54,0.12,78,0.12,245], [54,0.12,78,0.12,30], [54,0.16,78,0.16,145], [54,0.16,78,0.16,330],
  [66,0.12,66,0.12,70], [42,0.08,90,0.08,100], [66,0.16,66,0.16,290], [66,0.08,66,0.08,185],
  [66,0.16,66,0.16,0], [42,0.08,90,0.08,325], [66,0.16,66,0.16,125], [66,0.16,66,0.16,245],
  [48,0.08,84,0.08,185], [54,0.08,78,0.08,100], [60,0.08,72,0.08,310], [66,0.16,66,0.16,325],
  [66,0.16,66,0.16,35], [48,0.08,84,0.08,280], [66,0.08,66,0.08,265], [66,0.08,66,0.08,15],
  [48,0.08,84,0.08,55], [48,0.12,84,0.12,125], [66,0.16,66,0.16,155], [66,0.08,66,0.08,115],
  [60,0.08,72,0.08,225], [54,0.08,78,0.08,350], [60,0.08,72,0.08,50], [60,0.08,72,0.08,150],
  [54,0.12,78,0.12,295], [48,0.12,84,0.12,335], [60,0.12,72,0.12,5], [60,0.16,72,0.16,310],
  [60,0.12,72,0.12,90], [42,0.08,90,0.08,155], [60,0.12,72,0.12,270], [66,0.12,66,0.12,220],
  [54,0.12,78,0.12,65], [60,0.16,72,0.16,340], [60,0.16,72,0.16,45], [60,0.16,72,0.16,135],
  [54,0.08,78,0.08,205], [48,0.08,84,0.08,5], [66,0.08,66,0.08,325], [48,0.08,84,0.08,230],
  [54,0.08,78,0.08,160], [48,0.08,84,0.08,100], [54,0.12,78,0.12,125], [54,0.08,78,0.08,270],
  [66,0.16,66,0.16,270], [60,0.16,72,0.16,25], [60,0.12,72,0.12,120], [66,0.16,66,0.16,55],
  [66,0.12,66,0.12,140], [66,0.12,66,0.12,345], [48,0.08,84,0.08,145], [66,0.12,66,0.12,305],
  [66,0.12,66,0.12,95], [60,0.12,72,0.12,245], [60,0.08,72,0.08,345], [48,0.08,84,0.08,315],
  [54,0.08,78,0.08,315], [66,0.08,66,0.08,50], [60,0.08,72,0.08,185], [54,0.12,78,0.12,5],
  [54,0.08,78,0.08,50], [60,0.12,72,0.12,65], [60,0.12,72,0.12,165], [60,0.12,72,0.12,325],
  [60,0.08,72,0.08,105], [66,0.12,66,0.12,170], [60,0.12,72,0.12,295], [60,0.12,72,0.12,35],
  [66,0.12,66,0.12,280], [66,0.08,66,0.08,150], [66,0.12,66,0.12,25], [60,0.08,72,0.08,260],
  [54,0.12,78,0.12,340], [66,0.12,66,0.12,255], [66,0.08,66,0.08,230], [54,0.12,78,0.12,150],
  [60,0.16,72,0.16,0], [54,0.16,78,0.16,345], [66,0.16,66,0.16,340], [60,0.16,72,0.16,150],
  [66,0.16,66,0.16,15], [54,0.16,78,0.16,315], [66,0.16,66,0.16,310], [60,0.12,72,0.12,140],
  [66,0.12,66,0.12,5], [54,0.08,78,0.08,20], [60,0.08,72,0.08,20], [66,0.08,66,0.08,80],
  [54,0.08,78,0.08,235], [66,0.08,66,0.08,295], [66,0.12,66,0.12,45], [48,0.12,84,0.12,150],
  [54,0.12,78,0.12,320], [66,0.12,66,0.12,120], [54,0.08,78,0.08,130], [60,0.16,72,0.16,325],
  [66,0.16,66,0.16,140], [60,0.16,72,0.16,295], [60,0.12,72,0.12,345], [66,0.12,66,0.12,325],
  [42,0.12,90,0.1,135], [60,0.08,72,0.08,80], [66,0.08,66,0.08,350], [42,0.08,90,0.08,75],
  [54,0.08,78,0.08,75], [48,0.08,84,0.08,30], [48,0.08,84,0.08,340], [48,0.08,84,0.08,255],
  [60,0.08,72,0.08,285], [66,0.12,66,0.12,240], [48,0.12,84,0.12,320], [66,0.12,66,0.12,155],
  [54,0.12,78,0.12,50], [60,0.12,72,0.12,50], [60,0.12,72,0.12,20], [60,0.12,72,0.12,105],
  [60,0.12,72,0.12,310], [60,0.16,72,0.16,15], [66,0.16,66,0.16,25], [60,0.16,72,0.16,35],
  [66,0.16,66,0.16,45], [66,0.16,66,0.16,255], [66,0.16,66,0.16,280], [60,0.16,72,0.16,350],
];

function communityColor(index, dark) {
  const [lightL, lightC, darkL, darkC, baseHue] = communityColors[index % communityColors.length];
  // Beyond the base palette, hue rotation extends colors without guaranteeing separation.
  const hue = (baseHue + Math.floor(index / communityColors.length) * 137.508) % 360;
  const lightness = dark ? darkL : lightL, chroma = dark ? darkC : lightC;
  return `oklch(${lightness}% ${chroma} ${Number(hue.toFixed(3))})`;
}

export function graphCommunityPalette(semanticMap, scale = "default") {
  if (!semanticMap || ["stale", "unavailable"].includes(semanticMap.status)) return {};
  const view = (Array.isArray(semanticMap.community_scales) ? semanticMap.community_scales : []).find((row) => row?.scale === scale);
  const rows = view?.communities || (scale === "default" ? semanticMap.communities : []);
  const ids = new Set((Array.isArray(rows) ? rows : []).filter((row) => row?.algorithm === "leiden" && !row.noise
    && typeof row.community_id === "string" && /^[A-Za-z0-9._:-]{1,256}$/.test(row.community_id)).map((row) => row.community_id));
  // Use the complete saved scale, not visible nodes: filtering/focus must not recolor it.
  return Object.fromEntries([...ids].sort().map((id, index) => [id, index]));
}

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

export function graphNodeAppearance(node, { colorBy = "none", dark = false, communityScale = "default", communityPalette = {} } = {}) {
  const neutral = dark ? "oklch(78% 0.012 290)" : "oklch(52% 0.012 290)";
  if (colorBy === "origin") {
    const origin = graphNodeOrigin(node);
    const palette = originColors[dark ? "dark" : "light"];
    const colors = { managed: palette[0], personal: palette[1], unknown: neutral };
    return { key: `origin:${origin}`, label: originLabels[origin], color: colors[origin] };
  }
  if (colorBy === "community") {
    const membership = graphCommunityMembership(node, communityScale), identity = membership?.community_id;
    if (!identity) return { key: "community:unassigned", label: "Unassigned or unavailable", color: neutral };
    const assigned = communityPalette && Object.hasOwn(communityPalette, identity) ? communityPalette[identity] : undefined;
    const index = Number.isSafeInteger(assigned) && assigned >= 0 ? assigned : hashIdentity(identity);
    return { key: `community:${identity}`, label: typeof membership.display_label === "string" && membership.display_label.trim() ? membership.display_label.trim().slice(0, 160) : `Community ${identity.replace(/^semantic-/, "").slice(0, 8)}`, color: communityColor(index, dark) };
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
