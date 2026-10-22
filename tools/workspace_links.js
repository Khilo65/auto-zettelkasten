import MarkdownIt from "markdown-it";

export const LIMITS = Object.freeze({
  documents: 5000,
  characters: 8 * 1024 * 1024,
  documentCharacters: 1024 * 1024,
  lines: 50000,
  references: 4000,
  workspaceReferences: 20000,
  relations: 10000,
  syntaxPerDocument: 20000,
  syntaxPerWorkspace: 250000,
  completions: 100,
  embedCharacters: 64000,
  embeds: 64,
  embedDepth: 2,
});

export const LIBRARY_LIMITS = Object.freeze({
  documents: 100000,
  workspaceReferences: 1000000,
  relations: 1000000,
  documentCharacters: LIMITS.documentCharacters,
  lines: LIMITS.lines,
  references: 20000,
  syntaxPerDocument: 200000,
  openCharacters: 8 * 1024 * 1024,
  pageDocuments: 32,
  pageCharacters: 1024 * 1024,
});

const fold = (value) => String(value).normalize("NFC").toLowerCase();
const withoutExtension = (path) => path.replace(/\.md$/i, "");
const basename = (path) => withoutExtension(path.slice(path.lastIndexOf("/") + 1));
const encodePath = (path) => path.split("/").map((part) => encodeURIComponent(part)).join("/");
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const privateParts = new Set(["node_modules", "venv", "__pycache__", "credentials", "credential", "secrets", "secret", "private", "01_custody", "11_state", "custody", "state", "profiles", "profile", "indexes", "index", "bundles", "packets", "fingerprints", "note_metadata", "read_attempts", "export_manifest.md"]);
const privateNames = new Set(["credential", "credentials", "secret", "secrets", "password", "passwords", "api_key", "api_keys", "tokens"]);
const externalLink = (value) => /^(?:https?:|mailto:)/i.test(value);
const syntaxCharacters = new Set("[]()*_~`<>!#|\\=-^\n\r");
const researchRelationTypes = new Set(["supports", "undermines", "qualifies", "extends", "complements", "contrasts", "rival_explanation", "boundary_contrast", "methodological_fault_line", "sequential_relationship", "interpretive_or_normative_disagreement", "contextual_connection"]);
const citationRelationTypes = new Set(["cites", "cited_by"]);
const navigationRelationTypes = new Set(["cluster_member", "has_member", "alias_of"]);

function documentMetadata(input) {
  const coverage = input?.source_coverage;
  return {
    ...(typeof input?.navigation === "boolean" ? { navigation: input.navigation } : {}),
    ...(coverage && typeof coverage.status === "string" && typeof coverage.label === "string" ? { source_coverage: { status: coverage.status, label: coverage.label } } : {}),
  };
}

function syntaxCost(text, limit) {
  let cost = 0;
  for (let at = 0; at < text.length; at += 1) {
    if (!syntaxCharacters.has(text[at])) continue;
    if (text[at] === "\r" && text[at + 1] === "\n") at += 1;
    if (++cost > limit) break;
  }
  return cost;
}

function publishedIndexPath(parts, extensionOptional = false) {
  if (typeof parts === "string") parts = parts.split("/");
  if (parts[0] !== "02_source_memory" || parts[1] !== "indexes") return false;
  const tail = parts.slice(2);
  const extension = extensionOptional ? "(?:\\.md)?" : "\\.md";
  if (tail.length === 1) return new RegExp(`^(?:INDEX|CLUSTERS)${extension}$`).test(tail[0]);
  if (tail.length === 2 && ["by_literature", "by_topic"].includes(tail[0])) return Boolean(tail[1]) && (extensionOptional || tail[1].endsWith(".md"));
  return tail.length === 3 && tail[0] === "collections" && /^[A-Za-z0-9_-]+$/.test(tail[1]) && new RegExp(`^(?:INDEX|(?:sources|relationships)-[0-9]{3,})${extension}$`).test(tail[2]);
}

function safePath(value, base = "") {
  if (typeof value !== "string" || /[\x00-\x1f\x7f\\:]/.test(value) || value.startsWith("/")) return null;
  const parts = base ? base.split("/") : [];
  for (const part of value.split("/")) {
    if (part === "." || part === "") continue;
    if (part === "..") { if (!parts.length) return null; parts.pop(); continue; }
    if (part.startsWith(".") || (privateParts.has(fold(part)) && !["index", "indexes"].includes(fold(part))) || privateNames.has(fold(part.replace(/\.[^.]+$/, "")))) return null;
    parts.push(part);
  }
  const published = publishedIndexPath(parts, true);
  if (parts.some((part, at) => privateParts.has(fold(part)) && !(published && ((at === 1 && part === "indexes") || (at === parts.length - 1 && fold(part) === "index"))))) return null;
  return parts.join("/");
}

function lineStarts(text) {
  const result = [0];
  for (let at = 0; at < text.length; at += 1) {
    if (text[at] === "\r") { if (text[at + 1] === "\n") at += 1; result.push(at + 1); }
    else if (text[at] === "\n") result.push(at + 1);
  }
  return result;
}

function lineAt(starts, offset) {
  let low = 0;
  let high = starts.length;
  while (low + 1 < high) { const middle = (low + high) >>> 1; if (starts[middle] <= offset) low = middle; else high = middle; }
  return low;
}

function stripComment(value) {
  let quote = "";
  for (let at = 0; at < value.length; at += 1) {
    if (quote) {
      if (quote === '"' && value[at] === "\\") { at += 1; continue; }
      if (value[at] === quote) { if (quote === "'" && value[at + 1] === "'") at += 1; else quote = ""; }
    } else if (value[at] === '"' || value[at] === "'") quote = value[at];
    else if (value[at] === "#" && (at === 0 || /\s/.test(value[at - 1]))) return value.slice(0, at).trim();
  }
  return value.trim();
}

function scalar(value) {
  value = stripComment(value);
  value = value.trim();
  if (!value) return "";
  if (value[0] === '"') { const parsed = JSON.parse(value); if (typeof parsed !== "string") throw new Error("Expected string"); return parsed; }
  if (value[0] === "'") {
    if (!/^'(?:[^']|'')*'$/.test(value)) throw new Error("Invalid quoted string");
    return value.slice(1, -1).replace(/''/g, "'");
  }
  if (/^[!&*\[\]{}>|?]/.test(value) || /:\s/.test(value)) throw new Error("Unsupported YAML value");
  return value;
}

function scalarList(value) {
  value = stripComment(value);
  if (!value.startsWith("[")) return [scalar(value)].filter(Boolean);
  if (!value.endsWith("]")) throw new Error("Unclosed YAML array");
  const result = [];
  let start = 1;
  let quote = "";
  for (let at = 1; at < value.length; at += 1) {
    const char = value[at];
    if (quote) {
      if (quote === '"' && char === "\\") { at += 1; continue; }
      if (char === quote) { if (quote === "'" && value[at + 1] === "'") at += 1; else quote = ""; }
    } else if (char === '"' || char === "'") quote = char;
    else if (char === "," || at === value.length - 1) { result.push(scalar(value.slice(start, at))); start = at + 1; }
  }
  if (quote || result.length > 200) throw new Error("Unsupported YAML array");
  return result.filter(Boolean);
}

function metadata(text) {
  const result = { aliases: [], tags: [], frontmatter: null, warnings: [] };
  const match = /^(?:\uFEFF)?---\n([\s\S]*?)\n(?:---|\.\.\.)[ \t]*(?:\n|$)/.exec(text);
  if (!match) {
    if (/^(?:\uFEFF)?---\n/.test(text)) { result.frontmatter = { from: 0, to: text.length, text }; result.warnings.push("Unclosed properties block was excluded from the link index"); }
    return result;
  }
  result.frontmatter = { from: 0, to: match[0].length, text: match[1] };
  if (match[1].length > 65536) { result.warnings.push("Properties exceed the metadata limit"); return result; }
  const lines = match[1].split("\n");
  for (let at = 0; at < lines.length; at += 1) {
    const entry = /^(aliases?|tags?):\s*(.*)$/.exec(lines[at]);
    if (!entry) continue;
    const key = entry[1].startsWith("alias") ? "aliases" : "tags";
    try {
      let values;
      if (entry[2].trim()) values = scalarList(entry[2]);
      else {
        values = [];
        while (at + 1 < lines.length && /^\s+-\s/.test(lines[at + 1])) values.push(scalar(lines[++at].replace(/^\s+-\s*/, "")));
      }
      result[key].push(...values.slice(0, 200));
    } catch { result.warnings.push(`Unsupported ${key} metadata; use strings or a simple string list`); }
  }
  result.aliases = [...new Set(result.aliases)];
  result.tags = [...new Set(result.tags.map((tag) => tag.replace(/^#/, "")))];
  return result;
}

const parser = new MarkdownIt({ html: true, linkify: false, typographer: false, maxNesting: 32 });
let destinationCaptures = null;
const parseDestination = parser.helpers.parseLinkDestination;
parser.helpers.parseLinkDestination = (source, from, max) => {
  const parsed = parseDestination(source, from, max);
  if (parsed.ok && destinationCaptures) destinationCaptures.push({ source, from: from + (source[from] === "<" ? 1 : 0), to: parsed.pos - (source[from] === "<" ? 1 : 0), href: parser.normalizeLink(parsed.str) });
  return parsed;
};

function normalizedPosition(env, position) {
  const line = lineAt(env.normalizedStarts, position);
  return Math.min(env.original.length, env.originalStarts[line] + position - env.normalizedStarts[line]);
}

const referenceRule = parser.block.ruler.__rules__.find((rule) => rule.name === "reference").fn;
parser.block.ruler.at("reference", (state, ...args) => {
  const previous = destinationCaptures;
  const captures = [];
  const before = state.tokens.length;
  let accepted;
  destinationCaptures = captures;
  try { accepted = referenceRule(state, ...args); }
  finally { destinationCaptures = previous; }
  if (accepted && args[2] !== true) {
    const token = state.tokens.slice(before).find((item) => item.type === "reference_definition");
    const label = token?.meta?.label;
    const capture = captures.find((item) => item.href === state.env.references?.[label]?.href);
    if (label && capture && !state.env.destinationDefinitions.has(label)) state.env.destinationDefinitions.set(label, {
      from: normalizedPosition(state.env, capture.from), to: normalizedPosition(state.env, capture.to), definition: label,
    });
  }
  return accepted;
});

function absolutePosition(env, local) {
  const map = env.workspaceMap;
  if (!map?.length) return 0;
  const normalized = map[Math.min(local, map.length - 1)];
  const line = lineAt(env.normalizedStarts, normalized);
  return Math.min(env.original.length, env.originalStarts[line] + normalized - env.normalizedStarts[line]);
}

function reference(state, from, to, token, kind, embed, destination, label, range = null) {
  if (state.env.suppressReferences) return;
  if (state.env.referencesFound.length >= state.env.referenceBudget) { state.env.referenceLimit = true; return; }
  if (externalLink(destination)) return;
  const first = absolutePosition(state.env, from);
  const last = absolutePosition(state.env, to);
  const line = lineAt(state.env.originalStarts, first);
  const result = {
    from: first, to: last, line: line + 1,
    snippet: state.env.original.slice(state.env.originalStarts[line], state.env.originalStarts[line + 1] ?? state.env.original.length).trim().slice(0, 240),
    destination, label, embed, kind,
    ...(range ? { destinationFrom: range.from, destinationTo: range.to, destinationRaw: state.env.original.slice(range.from, range.to), ...(range.definition ? { definition: range.definition } : {}) } : {}),
  };
  token.meta = { ...token.meta, workspaceReference: result };
  state.env.referencesFound.push(result);
}

parser.inline.ruler.before("link", "workspace_wiki", (state, silent) => {
  const from = state.pos;
  const embed = state.src.startsWith("![[", from);
  if ((!embed && !state.src.startsWith("[[", from)) || state.linkLevel > 0) return false;
  const start = from + (embed ? 3 : 2);
  const end = state.src.indexOf("]]", start);
  if (end < 0 || end - start > 2048 || /[\n\r]/.test(state.src.slice(start, end))) return false;
  const raw = state.src.slice(start, end);
  if (!raw.trim() || raw.includes("[[")) return false;
  const split = raw.indexOf("|");
  const destination = (split < 0 ? raw : raw.slice(0, split)).trim();
  const label = (split < 0 ? destination : raw.slice(split + 1)).trim();
  if (!silent) {
    const token = state.push("workspace_wiki", "", 0);
    token.content = label;
    const rawDestination = split < 0 ? raw : raw.slice(0, split);
    const destinationStart = start + rawDestination.length - rawDestination.trimStart().length;
    reference(state, from, end + 2, token, "wiki", embed, destination, label, { from: absolutePosition(state.env, destinationStart), to: absolutePosition(state.env, destinationStart + destination.length) });
  }
  state.pos = end + 2;
  return true;
});

parser.inline.ruler.before("text", "workspace_tag", (state, silent) => {
  if (state.env.suppressReferences || state.linkLevel > 0 || state.src[state.pos] !== "#" || (state.pos && !/[\s(]/.test(state.src[state.pos - 1]))) return false;
  const match = /^#([\p{L}\p{N}_][\p{L}\p{N}_/-]*)/u.exec(state.src.slice(state.pos));
  if (!match) return false;
  if (!silent) { state.pending += match[0]; state.env.tagsFound.add(match[1]); }
  state.pos += match[0].length;
  return true;
});

// Pinned markdown-it rules supply grammar validation; wrappers only retain consumed source spans.
for (const name of ["link", "image", "backticks", "html_inline"]) {
  const original = parser.inline.ruler.__rules__.find((rule) => rule.name === name).fn;
  parser.inline.ruler.at(name, (state, silent) => {
    const from = state.pos;
    const before = state.tokens.length;
    const previousCapture = destinationCaptures;
    const captures = [];
    let result;
    if (name === "link" || name === "image") destinationCaptures = captures;
    if (name === "image") state.env.suppressReferences = (state.env.suppressReferences || 0) + 1;
    try { result = original(state, silent); }
    finally {
      if (name === "image") state.env.suppressReferences -= 1;
      destinationCaptures = previousCapture;
    }
    if (!result || silent || state.env.suppressReferences) return result;
    const tokens = state.tokens.slice(before);
    const token = tokens.find((item) => item.type === (name === "link" ? "link_open" : name === "image" ? "image" : name === "backticks" ? "code_inline" : "html_inline"));
    if (!token) return result;
    const to = state.pos;
    if (name === "link" || name === "image") {
      const label = name === "image" ? token.content : tokens.slice(tokens.indexOf(token) + 1).filter((item) => item.type === "text" || item.type === "code_inline").map((item) => item.content).join("");
      const href = token.attrGet(name === "image" ? "src" : "href") || "";
      const captured = captures.find((item) => item.source === state.src && item.href === href);
      const range = token.meta?.label ? state.env.destinationDefinitions.get(token.meta.label) : captured ? { from: absolutePosition(state.env, captured.from), to: absolutePosition(state.env, captured.to) } : null;
      reference(state, from, to, token, "markdown", name === "image", href, label, range);
    }
    if (name === "html_inline" && token.content.startsWith("<!--")) state.env.htmlComments.push({ from: absolutePosition(state.env, from), to: absolutePosition(state.env, to), text: token.content });
    state.env.excluded.push([absolutePosition(state.env, from), absolutePosition(state.env, to)]);
    return result;
  });
}

parser.core.ruler.at("inline", (state) => {
  let fallback = [0, state.env.normalizedStarts.length];
  const cursors = new Map();
  for (const token of state.tokens) {
    if (token.map) fallback = token.map;
    if (token.type !== "inline") continue;
    const range = token.map || fallback;
    const map = new Uint32Array(token.content.length + 1);
    let local = 0;
    let nextLine = range[0];
    let mapped = true;
    for (const content of token.content.split("\n")) {
      let matched = false;
      for (let line = nextLine; line < range[1]; line += 1) {
        const sourceLine = state.env.normalizedLines[line] || "";
        const column = sourceLine.indexOf(content, token.map ? 0 : cursors.get(line) || 0);
        if (column < 0) continue;
        const start = state.env.normalizedStarts[line] + column;
        for (let at = 0; at <= content.length; at += 1) map[local + at] = start + at;
        if (!token.map) cursors.set(line, column + content.length);
        nextLine = line + 1;
        matched = true;
        break;
      }
      if (!matched) { state.env.mappingUnsupported = true; mapped = false; break; }
      local += content.length + 1;
    }
    state.env.workspaceMap = map;
    if (!mapped) state.env.suppressReferences = (state.env.suppressReferences || 0) + 1;
    try { state.md.inline.parse(token.content, state.md, state.env, token.children); }
    finally { if (!mapped) state.env.suppressReferences -= 1; }
    token.meta = { ...token.meta, sourceFrom: mapped ? absolutePosition(state.env, 0) : state.env.originalStarts[range[0]] || 0, sourceTo: mapped ? absolutePosition(state.env, token.content.length) : state.env.originalStarts[range[1]] ?? state.env.original.length };
  }
});

function plainText(tokens) {
  return (tokens || []).filter((token) => token.type !== "html_inline").map((token) => ["softbreak", "hardbreak"].includes(token.type) ? " " : token.content || "").join("");
}

export function parseDocument(path, text, budgets = {}) {
  return parseNote(path, text, budgets, LIMITS);
}

function parseNote(path, text, budgets, limits) {
  text = String(text ?? "");
  const referenceBudget = Math.max(0, Math.min(limits.references, budgets.referenceBudget ?? limits.references));
  const syntaxBudget = Math.max(0, Math.min(limits.syntaxPerDocument, budgets.syntaxBudget ?? limits.syntaxPerDocument));
  const result = { path, text, aliases: [], tags: [], headings: [], blocks: [], references: [], tokens: [], warnings: [], truncated: false, unsupported: false, referenceBudget, referenceLimited: false, syntaxBudget, syntaxCost: 0, syntaxLimited: false };
  if (text.length > limits.documentCharacters) { result.truncated = true; result.warnings.push("Note exceeds the link-index character limit"); return result; }
  result.syntaxCost = syntaxCost(text, syntaxBudget);
  if (result.syntaxCost > syntaxBudget) {
    result.unsupported = true;
    result.syntaxLimited = true;
    result.warnings.push(syntaxBudget < limits.syntaxPerDocument ? "Workspace syntax budget reached; backlinks and graphs are partial" : "Note syntax is too complex for live analysis; backlinks and graphs are partial");
    return result;
  }
  const normalized = text.replace(/\r\n?/g, "\n");
  const originalStarts = lineStarts(text);
  const normalizedStarts = lineStarts(normalized);
  if (normalizedStarts.length > limits.lines) { result.truncated = true; result.warnings.push("Note exceeds the link-index line limit"); return result; }
  const properties = metadata(normalized);
  result.aliases = properties.aliases;
  result.tags = properties.tags;
  result.frontmatter = properties.frontmatter;
  result.warnings.push(...properties.warnings);
  const source = properties.frontmatter ? normalized.slice(0, properties.frontmatter.to).replace(/[^\n]/g, " ") + normalized.slice(properties.frontmatter.to) : normalized;
  const env = { original: text, originalStarts, normalizedStarts, normalizedLines: source.split("\n"), referencesFound: [], tagsFound: new Set(), excluded: [], htmlComments: [], referenceBudget, destinationDefinitions: new Map() };
  try { result.tokens = parser.parse(source, env); }
  catch { result.unsupported = true; result.warnings.push("Note could not be parsed for links"); return result; }
  for (const token of result.tokens) {
    if (token.type !== "inline") continue;
    const children = token.children || [];
    for (let at = 0; at < children.length; at += 1) {
      const child = children[at];
      if (child.type !== "link_open" || !child.meta?.workspaceReference) continue;
      let end = at + 1;
      while (end < children.length && children[end].type !== "link_close") end += 1;
      child.meta.workspaceReference.label = plainText(children.slice(at + 1, end));
    }
  }
  result.references = env.referencesFound.sort((left, right) => left.from - right.from);
  const literature = result.tokens.filter((token) => token.type === "html_block" && /^<!-- auto-zettelkasten:literature:(?:start|end) -->\s*$/.test(token.content) && text.slice(originalStarts[token.map[0]], originalStarts[token.map[0] + 1] ?? text.length).trimEnd() === token.content.trim());
  if (literature.length === 2 && literature[0].content.trim() === "<!-- auto-zettelkasten:literature:start -->" && literature[1].content.trim() === "<!-- auto-zettelkasten:literature:end -->") {
    const from = originalStarts[literature[0].map[1]], to = originalStarts[literature[1].map[0]];
    for (const ref of result.references) if (ref.from >= from && ref.to <= to) ref.layer = "citation";
  }
  const markers = result.tokens.filter((token) => token.type === "html_block" && /^<!-- auto-zettelkasten:graph:(?:start|end) -->\s*$/.test(token.content) && text.slice(originalStarts[token.map[0]], originalStarts[token.map[0] + 1] ?? text.length).trimEnd() === token.content.trim());
  if (markers.length === 2 && markers[0].content.trim() === "<!-- auto-zettelkasten:graph:start -->" && markers[1].content.trim() === "<!-- auto-zettelkasten:graph:end -->") {
    const from = originalStarts[markers[0].map[1]];
    const to = originalStarts[markers[1].map[0]];
    const identities = new Map();
    for (const comment of env.htmlComments) {
      if (comment.from < from || comment.to > to) continue;
      const match = /^<!-- relation_id: ([A-Za-z0-9][A-Za-z0-9_.:-]{0,199}) -->$/.exec(comment.text);
      if (!match) continue;
      const line = lineAt(originalStarts, comment.from);
      if (text.slice(comment.to, originalStarts[line + 1] ?? text.length).trim()) continue;
      if (identities.has(line)) identities.set(line, null);
      else identities.set(line, { id: match[1], from: comment.from });
    }
    for (const ref of result.references) {
      const identity = identities.get(ref.line - 1);
      if (identity && ref.from >= from && ref.to <= identity.from) ref.relationId = identity.id;
    }
  }
  result.tags = [...new Set([...result.tags, ...env.tagsFound])];
  if (env.referenceLimit) {
    result.referenceLimited = true;
    result.truncated = true;
    result.warnings.push(referenceBudget < limits.references ? "Workspace link occurrence budget reached; backlinks and graphs are partial" : "Note exceeds the link occurrence limit; backlinks and graphs are partial");
  }
  if (env.mappingUnsupported) { result.unsupported = true; result.warnings.push("Some Markdown source positions could not be indexed"); }
  const slugCounts = new Map();
  const headingIds = new Set();
  let previous = null;
  for (let at = 0; at < result.tokens.length; at += 1) {
    const token = result.tokens[at];
    if (token.map) {
      const from = originalStarts[token.map[0]] ?? text.length;
      const to = originalStarts[token.map[1]] ?? text.length;
      token.meta = { ...token.meta, sourceFrom: from, sourceTo: to };
    }
    if (token.type === "heading_open") {
      const title = plainText(result.tokens[at + 1]?.children).trim();
      const slug = fold(title).replace(/[^\p{L}\p{N}\s_-]/gu, "").trim().replace(/\s+/g, "-") || "section";
      let count = slugCounts.get(slug) || 0;
      while (headingIds.has(count ? `${slug}-${count}` : slug)) count += 1;
      slugCounts.set(slug, count + 1);
      const heading = { id: count ? `${slug}-${count}` : slug, title, level: Number(token.tag.slice(1)), from: token.meta.sourceFrom, to: token.meta.sourceTo, end: text.length, line: token.map[0] + 1 };
      headingIds.add(heading.id);
      result.headings.push(heading);
      token.meta.heading = heading;
    }
    if (token.type === "inline" && token.map) {
      const lastLine = token.map[1] - 1;
      const raw = text.slice(originalStarts[lastLine], originalStarts[lastLine + 1] ?? text.length).replace(/[\r\n]+$/, "");
      const match = /(?:^|[ \t])\^([A-Za-z0-9-]+)[ \t]*$/.exec(raw);
      if (match) {
        const markerFrom = originalStarts[lastLine] + match.index + match[0].indexOf("^");
        const excluded = env.excluded.some(([from, to]) => markerFrom >= from && markerFrom < to) || result.references.some((ref) => markerFrom >= ref.from && markerFrom < ref.to);
        if (!excluded) {
          const standalone = token.content.trim() === `^${match[1]}`;
          const range = standalone && previous ? previous : { from: token.meta.sourceFrom, to: markerFrom };
          result.blocks.push({ id: match[1], from: range.from, to: Math.min(range.to, markerFrom), markerFrom, markerTo: markerFrom + match[1].length + 1 });
        }
      }
      previous = { from: token.meta.sourceFrom, to: token.meta.sourceTo };
    } else if (["fence", "code_block"].includes(token.type)) previous = { from: token.meta.sourceFrom, to: token.meta.sourceTo };
  }
  const following = [];
  for (let at = result.headings.length - 1; at >= 0; at -= 1) {
    const heading = result.headings[at];
    while (following.length && following[following.length - 1].level > heading.level) following.pop();
    heading.end = following[following.length - 1]?.from ?? text.length;
    following.push(heading);
  }
  return result;
}

function addLookup(map, key, path) {
  key = fold(key);
  if (!map.has(key)) map.set(key, new Set());
  map.get(key).add(path);
}

export function buildLinkIndex(documents, typedRelations = [], previousIndex = null, metadata = {}) {
  const index = { documents: new Map(), references: [], relations: [], warnings: [], paths: new Map(), basenames: new Map(), aliases: new Map(), partial: false };
  let characters = 0;
  let remainingReferences = LIMITS.workspaceReferences;
  let remainingSyntax = LIMITS.syntaxPerWorkspace;
  for (const input of documents || []) {
    const path = input?.path;
    if (typeof path !== "string" || safePath(path) !== path || !/\.md$/i.test(path)) { index.warnings.push({ path: String(path || ""), reason: "Unsupported note path" }); continue; }
    const text = String(input.text ?? "");
    if (index.documents.size >= LIMITS.documents || characters + text.length > LIMITS.characters) { index.warnings.push({ path, reason: "Workspace exceeds the link-index size limit" }); continue; }
    if (index.documents.has(path)) { index.warnings.push({ path, reason: "Duplicate note path" }); continue; }
    characters += text.length;
    const cached = previousIndex?.documents.get(path);
    const syntaxBudget = Math.min(remainingSyntax, LIMITS.syntaxPerDocument);
    const referenceBudget = Math.min(remainingReferences, LIMITS.references);
    const reusable = cached?.text === text
      && (cached.syntaxLimited ? cached.syntaxBudget === syntaxBudget : cached.syntaxCost <= syntaxBudget)
      && (!cached.referenceLimited || cached.referenceBudget >= referenceBudget);
    const parsed = reusable ? cached : parseDocument(path, text, { syntaxBudget, referenceBudget });
    const document = { ...parsed, ...documentMetadata(input), revision: input.revision, editable: publishedIndexPath(path) ? false : input.editable, warnings: [...parsed.warnings], references: parsed.references.slice(0, referenceBudget).map((ref) => ({ ...ref, sourcePath: path, resolution: undefined })) };
    if (parsed.references.length > referenceBudget) {
      document.referenceLimited = true;
      document.referenceBudget = referenceBudget;
      document.truncated = true;
      document.warnings.push("Workspace link occurrence budget reached; backlinks and graphs are partial");
    }
    remainingReferences -= document.references.length;
    if (!document.syntaxLimited) remainingSyntax -= document.syntaxCost;
    index.documents.set(path, document);
    addLookup(index.paths, withoutExtension(path), path);
    addLookup(index.basenames, basename(path), path);
    for (const alias of document.aliases) addLookup(index.aliases, alias, path);
    for (const reason of document.warnings) index.warnings.push({ path, reason });
  }
  return finishIndex(index, typedRelations, LIMITS.relations, metadata);
}

function finishIndex(index, typedRelations, relationLimit, metadata) {
  index.canonicalPaths = new Map();
  const identities = (Array.isArray(metadata?.document_identities) ? metadata.document_identities : []).filter((identity) => identity?.verified === true
    && index.documents.has(identity.canonical_path) && Array.isArray(identity.path_aliases)
    && identity.path_aliases.every((path) => typeof path === "string" && index.documents.has(path)));
  const owners = new Map();
  for (const identity of identities) for (const path of new Set([identity.canonical_path, ...identity.path_aliases])) owners.set(path, (owners.get(path) || 0) + 1);
  for (const identity of identities) {
    const paths = [identity.canonical_path, ...identity.path_aliases];
    if (paths.some((path) => owners.get(path) !== 1)) continue;
    for (const path of paths) index.canonicalPaths.set(path, identity.canonical_path);
  }
  const semantic = metadata?.semantic_map;
  const semanticPaths = new Set();
  for (const document of index.documents.values()) delete document.semantic_communities;
  if (semantic && ["current", "partial", "unavailable"].includes(semantic.status)) {
    index.semantic_map = { status: semantic.status, communities: [] };
    if (/^[a-f0-9]{64}$/.test(semantic.identity_digest || "") && semantic.note_revisions && typeof semantic.note_revisions === "object") {
      index.semantic_map.identity_digest = semantic.identity_digest;
      for (const [path, revision] of Object.entries(semantic.note_revisions)) {
        if (index.documents.has(path) && /^[a-f0-9]{64}$/.test(revision) && index.documents.get(path).revision === revision) semanticPaths.add(path);
        else if (index.semantic_map.status !== "unavailable") index.semantic_map.status = "partial";
      }
      for (const community of Array.isArray(semantic.communities) ? semantic.communities.slice(0, relationLimit) : []) {
        if (!community || !/^[A-Za-z0-9._:-]{1,256}$/.test(community.community_id || "") || !["leiden", "hdbscan"].includes(community.algorithm) || !Array.isArray(community.paths)
            || community.noise !== undefined && typeof community.noise !== "boolean") continue;
        const paths = [...new Set(community.paths.filter((path) => semanticPaths.has(path)))];
        if (!paths.length) continue;
        const membership = { community_id: community.community_id, algorithm: community.algorithm,
          ...(typeof community.noise === "boolean" ? { noise: community.noise } : {}) };
        index.semantic_map.communities.push({ ...membership, paths });
        for (const path of paths) {
          const document = index.documents.get(path);
          document.semantic_communities = [...(document.semantic_communities || []), membership];
        }
      }
    }
  }
  for (const document of index.documents.values()) {
    for (const ref of document.references) { ref.resolution = resolveLink(index, document.path, ref.destination, ref.kind); index.references.push(ref); }
  }
  let relationCount = 0;
  for (const relation of typedRelations || []) {
    if (++relationCount > relationLimit) { index.warnings.push({ path: "", reason: "Typed relation budget reached; the typed graph layer is partial" }); break; }
    if (!relation || typeof relation.type !== "string" || !relation.type.trim()) continue;
    const source = index.documents.has(relation.source) ? { status: "resolved", path: relation.source } : resolveLink(index, "", String(relation.source || ""), "wiki");
    const target = index.documents.has(relation.target) ? { status: "resolved", path: relation.target } : resolveLink(index, "", String(relation.target || ""), "wiki");
    if (source.status === "resolved" && target.status === "resolved") {
      const value = { id: String(relation.id || `${source.path}:${relation.type}:${target.path}`), source: source.path, target: target.path, type: relation.type };
      if (relation.derived === true) {
        if (relation.type !== "semantic_similarity" || relation.accepted !== false || relation.evidence_backed !== false || relation.decision_status !== "suggested"
            || !semanticPaths.has(source.path) || !semanticPaths.has(target.path) || !Number.isFinite(relation.score) || relation.score < -1 || relation.score > 1
            || relation.provenance?.embedding_identity !== index.semantic_map?.identity_digest) continue;
        const chunks = (key) => Array.isArray(relation.provenance[key]) && relation.provenance[key].length <= 100000
          && relation.provenance[key].every((value) => typeof value === "string" && value.length > 0 && value.length <= 256);
        if (!chunks("source_chunk_ids") || !chunks("target_chunk_ids")) continue;
        Object.assign(value, { derived: true, decision_status: "suggested", accepted: false, evidence_backed: false, score: relation.score,
          provenance: { kind: "local_embedding_similarity", embedding_identity: index.semantic_map.identity_digest, accepted: false, evidence_backed: false,
            source_chunk_ids: [...relation.provenance.source_chunk_ids], target_chunk_ids: [...relation.provenance.target_chunk_ids] } });
      }
      index.relations.push(value);
    }
    else index.warnings.push({ path: String(relation.source || ""), reason: "A typed relation has an unavailable endpoint" });
  }
  index.partial = index.warnings.length > 0;
  return index;
}

export function parseLibraryDocument(input) {
  const path = input?.path;
  const source = typeof input?.text === "string" ? input.text : "";
  const parsed = parseNote(path, source, {}, LIBRARY_LIMITS);
  if (typeof input?.text !== "string") { parsed.unsupported = true; parsed.warnings.push("Note source is unavailable; backlinks and graphs are partial"); }
  if (typeof path !== "string" || safePath(path) !== path || !/\.md$/i.test(path)) { parsed.unsupported = true; parsed.warnings.push("Unsupported note path"); }
  const { text, tokens, frontmatter, ...projection } = parsed;
  // Copy sliced strings too, so a short snippet cannot retain a whole note body.
  return JSON.parse(JSON.stringify({ ...projection, ...documentMetadata(input), compact: true, textLength: source.length, revision: input?.revision, editable: publishedIndexPath(path || "") ? false : input?.editable }));
}

export function buildLibraryIndex(records, typedRelations = [], previousIndex = null, metadata = {}) {
  const index = { documents: new Map(), references: [], relations: [], warnings: [], paths: new Map(), basenames: new Map(), aliases: new Map(), partial: false, library: true };
  let remainingReferences = LIBRARY_LIMITS.workspaceReferences;
  let openCharacters = 0;
  for (const input of records || []) {
    const path = input?.path;
    if (typeof path !== "string" || safePath(path) !== path || !/\.md$/i.test(path)) { index.warnings.push({ path: String(path || ""), reason: "Unsupported note path" }); continue; }
    if (index.documents.size >= LIBRARY_LIMITS.documents) { index.warnings.push({ path, reason: "Library note budget reached; the catalog is partial" }); break; }
    if (index.documents.has(path)) { index.warnings.push({ path, reason: "Duplicate note path" }); continue; }
    let parsed;
    if (typeof input.text === "string") {
      if (openCharacters + input.text.length <= LIBRARY_LIMITS.openCharacters) {
        const cached = previousIndex?.library && previousIndex.documents.get(path);
        parsed = cached?.text === input.text ? cached : parseNote(path, input.text, {}, LIBRARY_LIMITS);
        openCharacters += input.text.length;
        parsed = { ...parsed, compact: false, textLength: input.text.length };
      } else parsed = parseLibraryDocument(input);
    } else if (input.compact === true && Number.isInteger(input.textLength) && input.textLength >= 0 && ["aliases", "tags", "headings", "blocks", "references", "warnings"].every((key) => Array.isArray(input[key]))) parsed = input;
    else parsed = parseLibraryDocument(input);
    const document = { ...parsed, ...documentMetadata(input), revision: input.revision, editable: publishedIndexPath(path) ? false : input.editable, warnings: [...parsed.warnings], references: parsed.references.slice(0, remainingReferences).map((ref) => ({ ...ref, sourcePath: path, resolution: undefined })) };
    if (parsed.references.length > remainingReferences) {
      document.referenceLimited = true;
      document.truncated = true;
      document.warnings.push("Library link occurrence budget reached; backlinks and graphs are partial");
    }
    remainingReferences -= document.references.length;
    index.documents.set(path, document);
    addLookup(index.paths, withoutExtension(path), path);
    addLookup(index.basenames, basename(path), path);
    for (const alias of document.aliases) addLookup(index.aliases, alias, path);
    for (const reason of document.warnings) index.warnings.push({ path, reason });
  }
  return finishIndex(index, typedRelations, LIBRARY_LIMITS.relations, metadata);
}

export function resolveLink(index, sourcePath, destination, kind = "wiki") {
  if (typeof destination !== "string" || destination.length > 2048) return { status: "invalid" };
  if (kind === "wiki") destination = destination.split("|", 1)[0].trim();
  const hash = destination.indexOf("#");
  let name = (hash < 0 ? destination : destination.slice(0, hash)).trim();
  let fragment = hash < 0 ? "" : destination.slice(hash + 1).trim();
  try { name = decodeURIComponent(name); fragment = decodeURIComponent(fragment); }
  catch { if (kind === "markdown") return { status: "invalid" }; }
  if (/[\x00-\x1f\x7f\\]/.test(name + fragment) || /^[a-z][a-z\d+.-]*:/i.test(name) || name.startsWith("/")) return { status: "invalid" };
  if (destination.startsWith("^") && hash < 0) { fragment = name; name = ""; }
  let candidates;
  if (!name) candidates = index.documents.has(sourcePath) ? [sourcePath] : [];
  else {
    const relative = kind === "markdown" || /^\.{1,2}\//.test(name);
    const base = relative ? sourcePath.slice(0, Math.max(0, sourcePath.lastIndexOf("/"))) : "";
    let normalized = safePath(name, base);
    const sourceDirectory = sourcePath.slice(0, Math.max(0, sourcePath.lastIndexOf("/")));
    const local = !relative && publishedIndexPath(sourcePath) ? safePath(name, sourceDirectory) : null;
    if (local && publishedIndexPath(local, true) && index.paths.has(fold(withoutExtension(local)))) normalized = local;
    if (normalized === null || !normalized || (kind === "markdown" && /\.[^/]+$/.test(normalized) && !/\.md$/i.test(normalized))) return { status: "invalid" };
    const key = fold(withoutExtension(normalized));
    if (relative || name.includes("/") || normalized === local) candidates = [...(index.paths.get(key) || [])];
    else candidates = [...new Set([...(index.basenames.get(key) || []), ...(index.aliases.get(fold(name)) || [])])];
    if (!candidates.length) return { status: "missing", target: normalized, fragment: fragment || undefined };
  }
  if (!candidates.length) return { status: "missing", target: name, fragment: fragment || undefined };
  if (candidates.length > 1 && candidates.every((path) => index.canonicalPaths?.has(path))) {
    const canonical = [...new Set(candidates.map((path) => index.canonicalPaths.get(path)))];
    if (canonical.length === 1) candidates = canonical;
  }
  if (candidates.length > 1) return { status: "ambiguous", candidates: candidates.sort(), fragment: fragment || undefined };
  const path = candidates[0];
  const document = index.documents.get(path);
  if (!fragment) return { status: "resolved", path, from: 0, to: document.textLength ?? document.text.length };
  let matches;
  if (fragment.startsWith("^")) matches = document.blocks.filter((block) => block.id === fragment.slice(1));
  else {
    matches = document.headings.filter((heading) => heading.id === fragment);
    if (!matches.length) matches = document.headings.filter((heading) => fold(heading.title) === fold(fragment));
    if (!matches.length) matches = document.headings.filter((heading) => fold(heading.id) === fold(fragment));
  }
  if (!matches.length) return { status: "missing-fragment", path, fragment };
  if (matches.length > 1) return { status: "ambiguous", path, fragment, candidates: matches.map((match) => `${path}#${match.id}`) };
  const match = matches[0];
  return { status: "resolved", path, fragment, from: match.from, to: fragment.startsWith("^") ? match.to : match.end };
}

export function backlinks(index, path) {
  const groups = new Map();
  for (const reference of index.references) {
    if (reference.resolution.path !== path) continue;
    if (!groups.has(reference.sourcePath)) groups.set(reference.sourcePath, { path: reference.sourcePath, count: 0, occurrences: [] });
    const group = groups.get(reference.sourcePath);
    group.count += 1;
    group.occurrences.push(reference);
  }
  return [...groups.values()].map((group) => ({ ...group, partial: index.partial })).sort((left, right) => left.path.localeCompare(right.path));
}

function shortestDestination(index, sourcePath, path) {
  const choices = [basename(path), withoutExtension(path), path.slice(path.lastIndexOf("/") + 1), path];
  const source = sourcePath.split("/").slice(0, -1);
  const target = withoutExtension(path).split("/");
  while (source.length && target.length && source[0] === target[0]) { source.shift(); target.shift(); }
  choices.push((source.length ? "../".repeat(source.length) : "./") + target.join("/"));
  return [...new Set(choices.map(encodePath))].sort((left, right) => left.length - right.length || left.localeCompare(right)).find((candidate) => {
    const resolved = resolveLink(index, sourcePath, candidate);
    return resolved.status === "resolved" && resolved.path === path;
  }) || null;
}

function relativeDestination(source, target) {
  const sourceParts = source.split("/").slice(0, -1);
  const targetParts = target.split("/");
  while (sourceParts.length && targetParts.length && sourceParts[0] === targetParts[0]) { sourceParts.shift(); targetParts.shift(); }
  return "../".repeat(sourceParts.length) + targetParts.join("/");
}

const encodeRepairPath = (path) => encodePath(path).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);

function sameResolvedTarget(resolution, expected, path) {
  return resolution.status === "resolved" && resolution.path === path
    && (resolution.fragment || "") === (expected.fragment || "")
    && (!expected.fragment || (resolution.from === expected.from && resolution.to === expected.to));
}

function relocationDestination(index, source, target, ref, raw, expected) {
  if (/&#(?:\d+|x[\da-f]+);/i.test(raw)) return null;
  const hash = raw.indexOf("#");
  let split = hash;
  if (ref.kind === "markdown" && split > 0) {
    let slashes = 0;
    for (let at = split - 1; at >= 0 && raw[at] === "\\"; at -= 1) slashes += 1;
    if (slashes % 2) split -= 1;
  }
  const fragment = split < 0 ? "" : raw.slice(split);
  let oldPath = ref.destination.split("#", 1)[0];
  try { oldPath = decodeURIComponent(oldPath); } catch { /* Literal percent signs are allowed in wiki names. */ }
  const withExtension = /\.md$/i.test(oldPath);
  const newPath = withExtension ? target : withoutExtension(target);
  const relative = relativeDestination(source, newPath);
  const choices = [];
  if (ref.kind === "markdown") {
    choices.push(encodeRepairPath(oldPath.startsWith("./") && !relative.startsWith("../") ? `./${relative}` : relative));
    choices.push(encodeRepairPath(relativeDestination(source, target)));
  } else {
    if (/^\.{1,2}\//.test(oldPath)) choices.push(encodeRepairPath(oldPath.startsWith("./") && !relative.startsWith("../") ? `./${relative}` : relative));
    else if (oldPath.includes("/")) choices.push(encodeRepairPath(newPath));
    else choices.push(encodeRepairPath(newPath.slice(newPath.lastIndexOf("/") + 1)));
    choices.push(shortestDestination(index, source, target), encodeRepairPath(newPath), encodeRepairPath(target));
  }
  for (const path of new Set(choices.filter((value) => value !== null))) {
    const after = path + fragment;
    let destination = after;
    if (ref.kind === "markdown") {
      const parsed = parser.helpers.parseLinkDestination(after, 0, after.length);
      if (!parsed.ok || parsed.pos !== after.length) continue;
      destination = parser.normalizeLink(parsed.str);
    }
    if (sameResolvedTarget(resolveLink(index, source, destination, ref.kind), expected, target)) return after;
  }
  return null;
}

function relocationMeaning(resolution, mapping = new Map()) {
  return JSON.stringify({
    status: resolution.status,
    path: mapping.get(resolution.path) || resolution.path,
    target: resolution.target,
    fragment: resolution.fragment,
    candidates: resolution.candidates?.map((candidate) => {
      if (mapping.has(candidate)) return mapping.get(candidate);
      return resolution.path && mapping.has(resolution.path) && candidate.startsWith(`${resolution.path}#`) ? mapping.get(resolution.path) + candidate.slice(resolution.path.length) : candidate;
    }).sort(),
  });
}

export function planRelocation(index, moves) {
  const plan = { patches: [], references: 0, files: 0, unchanged: 0, blocked: [], warnings: [], safe: !index.partial };
  const mapping = new Map();
  const warn = (reason) => { plan.safe = false; if (!plan.warnings.includes(reason)) plan.warnings.push(reason); };
  if (index.library) { warn("Relocation repair requires a complete revision-bound source snapshot; the compact library index cannot authorize repairs."); return plan; }
  if (index.partial) warn("The link index is partial; a complete relocation repair cannot be guaranteed.");
  if (!Array.isArray(moves) || moves.length > LIMITS.documents) { warn("Relocation requires a bounded list of note paths."); return plan; }
  for (const move of moves) {
    if (publishedIndexPath(move?.from || "") || publishedIndexPath(move?.to || "")) { warn("Published memory indexes are permanently read-only and cannot be relocated."); return plan; }
    if (!move || !index.documents.has(move.from) || mapping.has(move.from) || safePath(move.to) !== move.to || !/\.md$/i.test(move.to)) {
      warn("A relocation path is missing, duplicated, private, or invalid.");
      return plan;
    }
    mapping.set(move.from, move.to);
  }
  const destinations = new Map();
  for (const document of index.documents.values()) {
    const path = mapping.get(document.path) || document.path;
    const key = fold(path);
    if (destinations.has(key) && (mapping.has(document.path) || mapping.has(destinations.get(key)))) {
      warn(`Relocation would collide with another note after case and Unicode normalization: ${path}`);
      return plan;
    }
    destinations.set(key, document.path);
  }
  const hypothetical = buildLinkIndex([...index.documents.values()].map((document) => ({ ...document, path: mapping.get(document.path) || document.path })), [], index);
  if (hypothetical.partial) warn("The relocated link index is partial; a complete relocation repair cannot be guaranteed.");
  const patchFiles = new Map();
  const blocked = new Map();
  const blockedReferences = new Set();
  const block = (path, reason, unsafe = false) => {
    const key = JSON.stringify([path, reason]);
    if (!blocked.has(key)) blocked.set(key, { path, count: 0, reason });
    blocked.get(key).count += 1;
    if (unsafe) warn(`${path}: ${reason}`);
  };
  for (const ref of index.references) {
    const document = index.documents.get(ref.sourcePath);
    const source = mapping.get(ref.sourcePath) || ref.sourcePath;
    const original = ref.resolution || resolveLink(index, ref.sourcePath, ref.destination, ref.kind);
    const current = resolveLink(hypothetical, source, ref.destination, ref.kind);
    if (original.status !== "resolved") {
      if (relocationMeaning(original, mapping) !== relocationMeaning(current)) warn(`${ref.sourcePath}:${ref.line}: relocation changes the meaning of an ${original.status} link; no destination was guessed.`);
      continue;
    }
    const target = mapping.get(original.path) || original.path;
    if (sameResolvedTarget(current, original, target)) { plan.unchanged += 1; continue; }
    plan.references += 1;
    if (document?.editable !== true) { blockedReferences.add(ref); block(ref.sourcePath, "The source note is read-only or generated; its links cannot be repaired."); continue; }
    const from = ref.destinationFrom;
    const to = ref.destinationTo;
    if (typeof document.revision !== "string" || !document.revision || document.text.includes("\r") || !Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to <= from || to > document.text.length || document.text.slice(from, to) !== ref.destinationRaw) {
      blockedReferences.add(ref);
      block(ref.sourcePath, "The exact destination span or LF-normalized revision is unavailable; review this link manually.", true);
      continue;
    }
    const before = document.text.slice(from, to);
    const after = relocationDestination(hypothetical, source, target, ref, before, original);
    if (after === null || after === before) { blockedReferences.add(ref); block(ref.sourcePath, "A destination preserving the same target and fragment could not be verified.", true); continue; }
    if (!patchFiles.has(ref.sourcePath)) patchFiles.set(ref.sourcePath, { path: ref.sourcePath, revision: document.revision, edits: [] });
    const patch = patchFiles.get(ref.sourcePath);
    const existing = patch.edits.find((edit) => edit.from === from && edit.to === to);
    if (existing) {
      if (existing.after !== after) { blockedReferences.add(ref); block(ref.sourcePath, "A shared destination requires incompatible repairs.", true); }
      continue;
    }
    if (patch.edits.some((edit) => from < edit.to && to > edit.from)) { blockedReferences.add(ref); block(ref.sourcePath, "Destination edits overlap; review the source note manually.", true); continue; }
    patch.edits.push({ from, to, before, after });
  }
  plan.patches = [...patchFiles.values()].map((patch) => ({ ...patch, edits: patch.edits.sort((left, right) => right.from - left.from || right.to - left.to) })).sort((left, right) => left.path.localeCompare(right.path));
  plan.files = plan.patches.length;
  plan.blocked = [...blocked.values()].sort((left, right) => left.path.localeCompare(right.path) || left.reason.localeCompare(right.reason));
  if (plan.patches.length) {
    const finalIndex = buildLinkIndex([...index.documents.values()].map((document) => {
      let text = document.text;
      for (const edit of patchFiles.get(document.path)?.edits || []) text = text.slice(0, edit.from) + edit.after + text.slice(edit.to);
      return { ...document, path: mapping.get(document.path) || document.path, text };
    }), [], hypothetical);
    if (finalIndex.partial) warn("The final repaired link index is partial; the repair cannot be verified completely.");
    const translate = (path, position) => position + (patchFiles.get(path)?.edits || []).reduce((shift, edit) => shift + (edit.to <= position ? edit.after.length - edit.before.length : 0), 0);
    for (const document of index.documents.values()) {
      const final = finalIndex.documents.get(mapping.get(document.path) || document.path);
      if (!final || final.references.length !== document.references.length) { warn(`${document.path}: repair changes the parsed link occurrences; review this note manually.`); continue; }
      for (let at = 0; at < document.references.length; at += 1) {
        const ref = document.references[at];
        if (blockedReferences.has(ref)) continue;
        const expected = ref.resolution;
        const actual = final.references[at].resolution;
        if (expected.status === "resolved") {
          const translated = { ...expected, from: translate(expected.path, expected.from), to: translate(expected.path, expected.to) };
          if (!sameResolvedTarget(actual, translated, mapping.get(expected.path) || expected.path)) warn(`${document.path}:${ref.line}: destination repair would change the meaning of another resolved link or heading; review it manually.`);
        } else if (relocationMeaning(expected, mapping) !== relocationMeaning(actual)) {
          warn(`${document.path}:${ref.line}: destination repair would change an ${expected.status} link; no destination was guessed.`);
        }
      }
    }
  }
  return plan;
}

export function completionCandidates(index, sourcePath, query = "") {
  query = String(query).replace(/^!?(?:\[\[)/, "");
  const hash = query.indexOf("#");
  const blockQuery = hash < 0 && query.startsWith("^");
  if (hash >= 0 || blockQuery) {
    const base = hash >= 0 ? query.slice(0, hash).trim() : "";
    const fragment = hash >= 0 ? query.slice(hash + 1) : query;
    const resolved = base ? resolveLink(index, sourcePath, base) : resolveLink(index, sourcePath, "");
    if (resolved.status !== "resolved") return [];
    const document = index.documents.get(resolved.path);
    const prefix = base ? shortestDestination(index, sourcePath, resolved.path) : "";
    if (prefix === null) return [];
    const isBlock = fragment.startsWith("^");
    const needle = fold(isBlock ? fragment.slice(1) : fragment);
    return (isBlock ? document.blocks : document.headings).filter((entry) => fold(isBlock ? entry.id : entry.title).includes(needle)).slice(0, LIMITS.completions).flatMap((entry) => {
      const candidates = isBlock ? [`^${entry.id}`] : [entry.title, entry.id].filter(Boolean);
      const chosen = candidates.map((value) => ({ fragment: value, insert: `${prefix}#${isBlock ? value : encodeURIComponent(value)}` })).find((candidate) => {
        const target = resolveLink(index, sourcePath, candidate.insert);
        return target.status === "resolved" && target.path === document.path && target.from === entry.from;
      });
      if (!chosen) return [];
      const repeated = !isBlock && document.headings.filter((heading) => fold(heading.title) === fold(entry.title)).length > 1;
      return [{
        label: isBlock ? `^${entry.id}` : entry.title,
        detail: `${document.path} · ${isBlock ? "block" : `heading ${entry.level}`}${repeated ? ` · line ${entry.line ?? lineAt(lineStarts(document.text || ""), entry.from) + 1}` : ""}`,
        ...chosen, path: document.path, from: entry.from, to: isBlock ? entry.to : entry.end,
      }];
    });
  }
  let search = query.trim();
  try { search = decodeURIComponent(search); } catch { /* A literal percent sign can be part of a note name. */ }
  const needle = fold(search);
  const result = [];
  for (const document of index.documents.values()) {
    const canonical = shortestDestination(index, sourcePath, document.path);
    if (canonical === null) continue;
    if (!needle || fold(document.path).includes(needle)) result.push({ label: basename(document.path), detail: document.path, insert: canonical, path: document.path });
    for (const alias of document.aliases) {
      if (needle && fold(alias).includes(needle)) result.push({ label: alias, detail: `${document.path} · alias`, insert: `${canonical}|${alias}`, path: document.path });
    }
  }
  return result.sort((left, right) => left.label.localeCompare(right.label) || left.path.localeCompare(right.path)).slice(0, LIMITS.completions);
}

export function searchNotes(index, query = "") {
  const terms = fold(query.trim()).split(/\s+/).filter(Boolean);
  return [...index.documents.values()].flatMap((document) => {
    const haystack = fold([document.path, ...document.aliases].join(" "));
    if (!terms.every((term) => haystack.includes(term))) return [];
    const alias = document.aliases.find((value) => terms.length && terms.every((term) => fold(value).includes(term)));
    return [{ path: document.path, name: basename(document.path), ...(alias ? { alias } : {}) }];
  }).sort((left, right) => left.path.localeCompare(right.path));
}

export function graphData(index, options = {}) {
  // Callers without layer options retain the complete legacy graph; the UI selects research first.
  const showNavigation = options.showNavigation ?? options.showIndexes !== false;
  const canonical = (path) => index.canonicalPaths?.get(path) || path;
  const navigation = (path) => index.documents.get(path)?.navigation === true || publishedIndexPath(path)
    || path.startsWith("03_literature_synthesis/") && path.endsWith("/INDEX.md");
  const nodes = new Map();
  for (const document of index.documents.values()) {
    if (!showNavigation && navigation(document.path)) continue;
    const path = canonical(document.path), primary = index.documents.get(path);
    if (!nodes.has(path)) nodes.set(path, { id: path, path, label: basename(path), pathAliases: [], aliases: [], tags: [], folder: path.split("/").slice(0, -1).join("/"), navigation: navigation(path), source_coverage: primary.source_coverage,
      ...(primary.semantic_communities ? { semantic_communities: primary.semantic_communities.map((row) => ({ ...row })) } : {}) });
    const node = nodes.get(path);
    node.pathAliases.push(document.path);
    node.aliases.push(...document.aliases);
    node.tags = [...new Set([...node.tags, ...document.tags])];
  }
  for (const node of nodes.values()) node.pathAliases.sort((a, b) => Number(b === node.path) - Number(a === node.path) || a.localeCompare(b));
  const edges = new Map();
  const addCitation = (source, target, provenance) => {
    const id = `citation:${JSON.stringify([source, target])}`;
    if (!edges.has(id)) edges.set(id, { id, source, target, type: "cites", layer: "citation", count: 1, provenance: [] });
    edges.get(id).provenance.push(provenance);
  };
  const projected = new Set(index.relations.flatMap((relation) => [JSON.stringify([relation.id, canonical(relation.source), canonical(relation.target)]), JSON.stringify([relation.id, canonical(relation.target), canonical(relation.source)])]));
  const enabled = (layer) => ({ reference: options.showReferences, relation: options.showRelations, citation: options.showCitations, navigation: showNavigation, discovery: options.showDiscovery })[layer] !== false;
  for (const ref of index.references) {
    const source = canonical(ref.sourcePath);
    if (!nodes.has(source)) continue;
    const resolution = ref.resolution;
    let target = resolution.status === "resolved" ? canonical(resolution.path) : null;
    if (resolution.status === "invalid") continue;
    if (ref.relationId && target && projected.has(JSON.stringify([ref.relationId, source, target]))) continue;
    const layer = navigation(ref.sourcePath) || target && navigation(target) ? "navigation" : ref.layer === "citation" ? "citation" : "reference";
    if (!enabled(layer)) continue;
    if (!target) {
      target = `unresolved:${JSON.stringify([resolution.status, fold(resolution.path || resolution.target || ref.destination), resolution.fragment || "", resolution.candidates || []])}`;
      const statusLabel = { missing: "missing note", ambiguous: "ambiguous target", "missing-fragment": "missing heading or block" }[resolution.status] || resolution.status;
      if (!nodes.has(target)) nodes.set(target, { id: target, label: `${ref.destination} · ${statusLabel}`, missing: true, status: resolution.status, candidates: resolution.candidates || [], tags: [], folder: "", pathAliases: [], aliases: [] });
    }
    if (layer === "citation") {
      addCitation(source, target, { kind: "managed_markdown", source: ref.sourcePath, line: ref.line, destination: ref.destination, from: ref.from, to: ref.to });
      continue;
    }
    const id = `${layer}:${JSON.stringify([source, target])}`;
    if (edges.has(id)) edges.get(id).count += 1;
    else edges.set(id, { id, source, target, layer, count: 1 });
  }
  for (const relation of index.relations) {
    if (options.relationType && relation.type !== options.relationType) continue;
    const source = canonical(relation.source), target = canonical(relation.target);
    const layer = navigation(source) || navigation(target) || navigationRelationTypes.has(relation.type) ? "navigation"
      : citationRelationTypes.has(relation.type) ? "citation" : researchRelationTypes.has(relation.type) ? "relation" : "discovery";
    if (!enabled(layer) || source === target && relation.source !== relation.target) continue;
    if (layer === "citation") {
      const [citing, cited] = relation.type === "cited_by" ? [target, source] : [source, target];
      addCitation(citing, cited, { kind: "typed_relation", ...relation });
      continue;
    }
    const id = `${layer}:${JSON.stringify([source, target, relation.type])}`;
    if (!edges.has(id)) edges.set(id, { ...relation, id, source, target, layer, count: 1 });
  }
  let allowed = new Set(nodes.keys());
  const search = fold(options.search || "").trim();
  const folder = String(options.folder || "").replace(/\/+$/, "");
  const tags = (Array.isArray(options.tag) ? options.tag : options.tag ? [options.tag] : []).map((tag) => fold(String(tag).replace(/^#/, "")));
  allowed = new Set([...nodes.values()].filter((node) => (!options.existingOnly || !node.missing)
    && (!search || fold([node.label, ...node.pathAliases, ...node.aliases].join(" ")).includes(search))
    && (!folder || node.pathAliases.some((path) => path.startsWith(`${folder}/`)))
    && tags.every((tag) => node.tags.some((value) => fold(value) === tag))).map((node) => node.id));
  let filteredEdges = [...edges.values()].filter((edge) => allowed.has(edge.source) && allowed.has(edge.target));
  const local = options.local || (options.path ? options : null);
  if (local) {
    const center = canonical(local.path);
    const included = new Set(allowed.has(center) ? [center] : []);
    let frontier = new Set(included);
    const depth = Math.max(1, Math.min(3, Number(local.depth) || 1));
    for (let step = 0; step < depth; step += 1) {
      const next = new Set();
      for (const edge of filteredEdges) {
        if (local.outgoing !== false && frontier.has(edge.source) && !included.has(edge.target)) next.add(edge.target);
        if (local.incoming !== false && frontier.has(edge.target) && !included.has(edge.source)) next.add(edge.source);
      }
      for (const node of next) included.add(node);
      frontier = next;
    }
    allowed = included;
    filteredEdges = filteredEdges.filter((edge) => {
      if (!allowed.has(edge.source) || !allowed.has(edge.target)) return false;
      if (edge.source === center && edge.target === center) return local.incoming !== false || local.outgoing !== false;
      if (edge.source === center) return local.outgoing !== false;
      if (edge.target === center) return local.incoming !== false;
      return local.neighbors !== false;
    });
  }
  const visibleNodes = [...nodes.values()].filter((node) => allowed.has(node.id));
  for (const node of visibleNodes) node.degree = 0;
  for (const edge of filteredEdges) { nodes.get(edge.source).degree += 1; nodes.get(edge.target).degree += 1; }
  const unresolved = { missing: 0, ambiguous: 0, fragments: 0, hidden: 0 };
  const destinations = new Set([...edges.values()].filter((edge) => allowed.has(edge.source) && (!local || local.outgoing !== false || edge.source !== canonical(local.path))).map((edge) => edge.target));
  for (const target of destinations) {
    const node = nodes.get(target);
    if (!node?.missing) continue;
    unresolved[node.status === "missing-fragment" ? "fragments" : node.status] += 1;
    if (!allowed.has(target)) unresolved.hidden += 1;
  }
  return { nodes: visibleNodes, edges: filteredEdges, unresolved, partial: index.partial,
    ...(index.semantic_map ? { semantic_map: index.semantic_map } : {}) };
}

function linkMarkup(index, sourcePath, ref) {
  const resolution = resolveLink(index, sourcePath, ref.destination, ref.kind);
  const from = ref.from ?? 0;
  if (resolution.status === "resolved") return { open: `<a href="#" class="note-link" data-note-destination="${escapeHtml(ref.destination)}" data-note-kind="${ref.kind}" data-source-path="${escapeHtml(sourcePath)}" data-source-from="${from}">`, close: "</a>" };
  const status = resolution.status.replace(/-/g, " ");
  return { open: `<span class="note-link note-link-${resolution.status}" data-link-status="${resolution.status}" title="${escapeHtml(`${status}: ${ref.destination}`)}" aria-label="${escapeHtml(`${ref.label || ref.destination} (${status})`)}">`, close: "</span>" };
}

function renderEmbed(index, sourcePath, ref, context) {
  const resolution = resolveLink(index, sourcePath, ref.destination, ref.kind);
  if (resolution.status !== "resolved") { const link = linkMarkup(index, sourcePath, ref); return `${link.open}Embed: ${escapeHtml(ref.label || ref.destination)}${link.close}`; }
  const open = linkMarkup(index, sourcePath, ref);
  const header = `<header>${open.open}${escapeHtml(ref.label || ref.destination)}${open.close}</header>`;
  const key = `${resolution.path}#${resolution.fragment || ""}`;
  if (context.depth >= LIMITS.embedDepth || context.seen.has(key) || context.seen.has(`${resolution.path}#`) || context.budget.embeds >= LIMITS.embeds) return `<section class="note-embed">${header}<p class="note-embed-limit">Embedded note limit reached. Open the note to continue.</p></section>`;
  const target = index.documents.get(resolution.path);
  if (typeof target.text !== "string") return `<section class="note-embed">${header}<p class="note-embed-loading">Loading embedded note…</p></section>`;
  const text = target.text.slice(resolution.from || 0, resolution.to ?? target.text.length);
  if (context.budget.characters + text.length > LIMITS.embedCharacters) return `<section class="note-embed">${header}<p class="note-embed-limit">Embedded content is too large. Open the note to read it.</p></section>`;
  context.budget.characters += text.length;
  context.budget.embeds += 1;
  const seen = new Set(context.seen);
  seen.add(key);
  const parsed = resolution.fragment ? parseNote(target.path, text, {}, index.library ? LIBRARY_LIMITS : LIMITS) : target;
  const body = renderParsed(index, parsed, { ...context, depth: context.depth + 1, seen, offset: resolution.from || 0 }, false);
  return `<section class="note-embed" aria-label="Embedded ${escapeHtml(ref.label || ref.destination)}">${header}<div class="note-embed-content">${body}</div></section>`;
}

parser.renderer.rules.workspace_wiki = (tokens, at, _options, env) => {
  const token = tokens[at];
  const raw = token.meta?.workspaceReference;
  const ref = raw ? { ...raw, from: raw.from + env.context.offset } : null;
  if (!ref) return escapeHtml(token.content);
  if (ref.embed) return renderEmbed(env.index, env.path, ref, env.context);
  const markup = linkMarkup(env.index, env.path, ref);
  return markup.open + escapeHtml(ref.label) + markup.close;
};
parser.renderer.rules.link_open = (tokens, at, _options, env) => {
  const token = tokens[at];
  const href = token.attrGet("href") || "";
  if (/^https?:\/\//i.test(href)) return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`;
  const raw = token.meta?.workspaceReference;
  const ref = raw ? { ...raw, from: raw.from + env.context.offset } : null;
  return ref ? linkMarkup(env.index, env.path, ref).open : "<span>";
};
parser.renderer.rules.link_close = (tokens, at, _options, env) => {
  let depth = 0;
  for (let before = at - 1; before >= 0; before -= 1) {
    if (tokens[before].type === "link_close") depth += 1;
    if (tokens[before].type !== "link_open") continue;
    if (depth) { depth -= 1; continue; }
    if (/^https?:\/\//i.test(tokens[before].attrGet("href") || "")) return "</a>";
    const raw = tokens[before].meta?.workspaceReference;
    const ref = raw ? { ...raw, from: raw.from + env.context.offset } : null;
    return ref ? linkMarkup(env.index, env.path, ref).close : "</span>";
  }
  return "</span>";
};
parser.renderer.rules.image = (tokens, at, _options, env) => {
  const raw = tokens[at].meta?.workspaceReference;
  const ref = raw ? { ...raw, from: raw.from + env.context.offset } : null;
  return ref && /\.md(?:#|$)/i.test(ref.destination) ? renderEmbed(env.index, env.path, ref, env.context) : `<span class="markdown-image">[${escapeHtml(tokens[at].content || "Image")}]</span>`;
};
parser.renderer.rules.html_inline = parser.renderer.rules.html_block = (tokens, at) => tokens[at].content.startsWith("<!--") ? "" : escapeHtml(tokens[at].content);

const renderedDocuments = new WeakMap();

function renderParsed(index, document, context, properties = true) {
  if (!document.tokens) {
    if (typeof document.text !== "string") return "<p>Loading note…</p>";
    if (!renderedDocuments.has(document)) renderedDocuments.set(document, parseNote(document.path, document.text, {}, index.library ? LIBRARY_LIMITS : LIMITS));
    document = renderedDocuments.get(document);
  }
  if (document.syntaxLimited) return `<p class="note-analysis-paused">Live link analysis is paused for this note. Backlinks and graphs are partial. Showing the source text safely.</p><pre class="note-source-fallback" data-source-path="${escapeHtml(document.path)}" data-source-from="${context.offset}">${escapeHtml(document.text)}</pre>`;
  const tokens = document.tokens.map((token) => {
    if (token.nesting !== 1 || !token.meta || token.hidden) return token;
    const clone = Object.assign(Object.create(Object.getPrototypeOf(token)), token);
    clone.attrs = token.attrs ? token.attrs.map((attr) => [...attr]) : [];
    clone.attrSet("data-source-from", String((token.meta.sourceFrom ?? 0) + context.offset));
    clone.attrSet("data-source-path", document.path);
    if (token.meta.heading) clone.attrSet("id", token.meta.heading.id);
    return clone;
  });
  const frontmatter = properties && document.frontmatter ? `<details class="markdown-properties"><summary>Properties</summary><pre>${escapeHtml(document.frontmatter.text)}</pre></details>` : "";
  return frontmatter + parser.renderer.render(tokens, parser.options, { index, path: document.path, context });
}

export function renderIndexedMarkdown(index, path) {
  const document = index.documents.get(path);
  if (!document) return "<p>Note is not available in this workspace.</p>";
  if (document.truncated && !document.tokens?.length && !document.syntaxLimited) return "<p>Note is too large to render safely in the workspace.</p>";
  return renderParsed(index, document, { depth: 0, seen: new Set([`${path}#`]), budget: { characters: 0, embeds: 0 }, offset: 0 });
}

export function renderReferenceEmbed(index, ref) {
  return renderEmbed(index, ref.sourcePath, ref, { depth: 0, seen: new Set([`${ref.sourcePath}#`]), budget: { characters: 0, embeds: 0 }, offset: 0 });
}
