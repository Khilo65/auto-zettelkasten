import { isAlias, isMap, isScalar, isSeq, parseDocument } from "yaml";
import { sourceLinkMarkup } from "./workspace_sources.js";

const MAX_HEADER = 65536;
const MAX_NODES = 2048;
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const simpleValue = (value) => value === null || typeof value === "string" || typeof value === "boolean" || typeof value === "number" && Number.isFinite(value)
  || Array.isArray(value) && value.length <= 200 && value.every((item) => typeof item === "string");

export function propertyType(value, key = "") {
  if (/^tags?$/.test(key)) return "tags";
  if (Array.isArray(value)) return "list";
  if (typeof value === "boolean") return "checkbox";
  if (typeof value === "number") return "number";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return "date";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)) return "datetime";
  return "text";
}

function parseProperties(text) {
  text = String(text ?? "");
  const open = /^(?:\uFEFF)?---[ \t]*\r?\n/.exec(text);
  if (!open) return { rows: [], error: null, frontmatter: null, document: null };
  const close = /^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/m.exec(text.slice(open[0].length, open[0].length + MAX_HEADER + 16));
  const headerTo = close ? open[0].length + close.index : text.length;
  const to = close ? headerTo + close[0].length : text.length;
  const frontmatter = { from: 0, to, text: text.slice(open[0].length, headerTo), headerFrom: open[0].length, headerTo };
  if (!close) return { rows: [], error: "Unclosed properties block; edit its YAML in Source mode.", frontmatter, document: null };
  if (headerTo - open[0].length > MAX_HEADER) return { rows: [], error: "Properties exceed the metadata limit.", frontmatter, document: null };
  try {
    const document = parseDocument(frontmatter.text, { keepSourceTokens: true, uniqueKeys: true, stringKeys: true, prettyErrors: false });
    if (document.errors.length || document.warnings.length) throw new Error("Properties contain invalid or unsupported YAML; edit them in Source mode.");
    if (document.contents === null) return { rows: [], error: null, frontmatter, document };
    if (!isMap(document.contents)) throw new Error("Properties must be a YAML mapping; edit them in Source mode.");
    if (document.contents.flow) throw new Error("Flow-style property mappings require Source mode.");
    let count = 0;
    const check = (node, depth = 0) => {
      if (++count > MAX_NODES || depth > 32) throw new Error("Properties are too complex for the property editor.");
      if (isAlias(node) || node?.anchor || node?.tag) throw new Error("YAML anchors, aliases and explicit tags require Source mode.");
      if (isMap(node)) for (const pair of node.items) { check(pair.key, depth + 1); check(pair.value, depth + 1); }
      else if (isSeq(node)) for (const child of node.items) check(child, depth + 1);
    };
    check(document.contents);
    if (document.contents.items.length > 200) throw new Error("Properties exceed the 200-field limit.");
    const rows = document.contents.items.map((pair) => {
      if (!isScalar(pair.key) || typeof pair.key.value !== "string") throw new Error("Property names must be text.");
      const value = pair.value?.toJSON() ?? null;
      const namedList = /^(?:tags?|aliases?)$/.test(pair.key.value);
      const editable = simpleValue(value) && (!namedList || value === null || typeof value === "string" || Array.isArray(value));
      return { key: pair.key.value, value, type: editable ? propertyType(value, pair.key.value) : "unsupported", editable };
    });
    return { rows, error: null, frontmatter, document };
  } catch (error) { return { rows: [], error: error.message || "Properties could not be parsed; use Source mode.", frontmatter, document: null }; }
}

export function inspectProperties(text) {
  const { document, ...result } = parseProperties(text);
  return result;
}

export function propertyLists(text) {
  const result = inspectProperties(text);
  const values = (keys) => [...new Set(result.rows.filter((row) => keys.includes(row.key)).flatMap((row) => typeof row.value === "string" ? [row.value] : Array.isArray(row.value) ? row.value.filter((item) => typeof item === "string") : []).filter(Boolean))];
  const warnings = result.error ? [result.error] : result.rows.filter((row) => /^(?:tags?|aliases?)$/.test(row.key) && !row.editable).map((row) => `Unsupported ${row.key} metadata; use strings or a simple string list`);
  return { ...result, warnings, aliases: values(["aliases", "alias"]), tags: values(["tags", "tag"]).map((tag) => tag.replace(/^#/, "")) };
}

export function patchProperty(text, key, value, { remove = false } = {}) {
  text = String(text ?? "");
  if (typeof key !== "string" || !key.trim() || key.length > 256 || /[\x00-\x1f\x7f]/.test(key)) throw new Error("Use a nonempty property name without control characters.");
  if (!remove && (!simpleValue(value) || JSON.stringify(value).length > 32768)) throw new Error("Use text, a string list, a number, a checkbox or a date.");
  const parsed = parseProperties(text);
  if (parsed.error) throw new Error(parsed.error);
  const newline = text.includes("\r\n") ? "\r\n" : "\n";
  const literal = JSON.stringify(value);
  if (!parsed.frontmatter) {
    if (remove) return text;
    const bom = text.startsWith("\uFEFF") ? "\uFEFF" : "";
    return `${bom}---${newline}${JSON.stringify(key)}: ${literal}${newline}---${newline}${text.slice(bom.length)}`;
  }
  const { document, frontmatter } = parsed;
  const pair = document?.contents?.items?.find((entry) => entry.key.value === key);
  if (!pair) {
    if (remove) return text;
    const separator = frontmatter.text && !frontmatter.text.endsWith("\n") ? newline : "";
    return text.slice(0, frontmatter.headerTo) + `${separator}${JSON.stringify(key)}: ${literal}${newline}` + text.slice(frontmatter.headerTo);
  }
  if (parsed.rows.find((row) => row.key === key)?.editable === false) throw new Error("This nested property requires Source mode.");
  if (remove) {
    const start = frontmatter.headerFrom + pair.key.range[0];
    const end = frontmatter.headerFrom + (pair.value?.range?.[2] ?? pair.key.range[2]);
    const lineStart = text.lastIndexOf("\n", start - 1) + 1;
    const after = text.slice(end).match(/^[ \t]*(?:\r?\n|$)/)?.[0].length || 0;
    return text.slice(0, lineStart) + text.slice(end + after);
  }
  const from = frontmatter.headerFrom + (pair.value?.range?.[0] ?? pair.key.range[1] + 1);
  const to = frontmatter.headerFrom + (pair.value?.range?.[1] ?? pair.key.range[1] + 1);
  const old = text.slice(from, to);
  const ending = /\r?\n$/.test(old) ? newline : "";
  const column = from - text.lastIndexOf("\n", from - 1) - 1;
  const leading = column === 0 ? "  " : text[from - 1] === ":" ? " " : "";
  const result = text.slice(0, from) + `${leading}${literal}${ending}` + text.slice(to);
  if (inspectProperties(result).error) throw new Error("This property cannot be changed safely; use Source mode.");
  return result;
}

export function renderProperties(text, { editable = false, generated = false } = {}) {
  const parsed = inspectProperties(text);
  if (!parsed.frontmatter) return "";
  if (parsed.error) return `<details class="markdown-properties"><summary>Properties</summary><p class="property-warning">${escapeHtml(parsed.error)}</p><pre>${escapeHtml(parsed.frontmatter.text)}</pre></details>`;
  const valueMarkup = (value) => Array.isArray(value) ? value.map((item) => sourceLinkMarkup(item)).join(", ")
    : sourceLinkMarkup(value === null ? "" : typeof value === "object" ? JSON.stringify(value) : value);
  const rows = parsed.rows.map((row) => `<div class="property-row" data-property-key="${escapeHtml(row.key)}" data-property-type="${row.type}"><span class="property-key">${escapeHtml(row.key)}</span><span class="property-value">${valueMarkup(row.value)}</span>${generated ? '<span class="property-origin">Generated</span>' : editable && row.editable ? `<button type="button" data-property-edit="${escapeHtml(row.key)}" aria-label="Edit ${escapeHtml(row.key)}">Edit</button>` : ""}</div>`).join("");
  return `<details class="markdown-properties" open><summary>Properties</summary>${rows || '<p class="property-empty">No properties</p>'}</details>`;
}
