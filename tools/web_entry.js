export { basicSetup } from "codemirror";
export { EditorState, EditorSelection, Compartment, StateEffect, StateField, RangeSetBuilder, Prec } from "@codemirror/state";
export { EditorView, Decoration, ViewPlugin, WidgetType, keymap, lineNumbers, drawSelection } from "@codemirror/view";
export { defaultKeymap, history, historyKeymap, undo, redo, selectAll, indentWithTab } from "@codemirror/commands";
export { searchKeymap, openSearchPanel, highlightSelectionMatches } from "@codemirror/search";
export { closeBrackets, closeBracketsKeymap, autocompletion, startCompletion, acceptCompletion, completionStatus } from "@codemirror/autocomplete";
export { default as cytoscape } from "cytoscape";
export * from "./workspace_links.js";
export { LibraryParser } from "./workspace_index_client.js";
export { fitGraph, layoutGraph } from "./workspace_graph.js";
export { createGraphView } from "./workspace_graph_view.js";
export * from "./workspace_graph_colors.js";
export * from "./workspace_graph_communities.js";
import { renderIndexedMarkdown, renderReferenceEmbed } from "./workspace_links.js";
export { syntaxTree, syntaxHighlighting, defaultHighlightStyle, bracketMatching, indentOnInput, foldGutter, foldKeymap } from "@codemirror/language";
export { markdown, markdownLanguage, markdownKeymap, insertNewlineContinueMarkup, deleteMarkupBackward } from "@codemirror/lang-markdown";

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import {
  createElement, Folder, FolderOpen, FolderPlus, FilePlus, FileText, PanelLeftClose,
  PanelLeftOpen, Search, ListTree, NotebookPen, ArrowLeft, ArrowRight,
  ChevronsUpDown, ChevronRight, ChevronDown, X, Plus, Save, BookOpen, Code,
  PencilLine, RefreshCw, Ellipsis, Settings, Check, Copy, AlertTriangle, Map,
  Menu, List, Bold, Italic, Undo, Redo, Network, Link, Link2, Link2Off, Pin, Maximize2, ZoomIn, ZoomOut, Workflow,
} from "lucide";

const iconNodes = {
  Folder, FolderOpen, FolderPlus, FilePlus, FileText, PanelLeftClose, PanelLeftOpen,
  Search, ListTree, NotebookPen, ArrowLeft, ArrowRight, ChevronsUpDown, ChevronRight,
  ChevronDown, X, Plus, Save, BookOpen, Code, PencilLine, RefreshCw, Ellipsis,
  Settings, Check, Copy, AlertTriangle, Map, Menu, List, Bold, Italic, Undo, Redo,
  Network, Link, Link2, Link2Off, Pin, Maximize2, ZoomIn, ZoomOut, Workflow,
};
export const icons = Object.fromEntries(Object.entries(iconNodes).map(([name, node]) => [
  name, () => createElement(node, { width: "18", height: "18", "stroke-width": "1.7", "aria-hidden": "true", focusable: "false" }),
]));

const markdownRenderer = new MarkdownIt({ html: false, linkify: false, typographer: false });
markdownRenderer.renderer.rules.image = (tokens, index) => {
  const alt = markdownRenderer.utils.escapeHtml(tokens[index].content || "Image");
  return `<span class="markdown-image">[${alt}]</span>`;
};
// Relative destinations need the workspace resolver; never navigate the local server.
markdownRenderer.renderer.rules.link_open = (tokens, index) => {
  const href = tokens[index].attrGet("href") || "";
  return /^https?:\/\//i.test(href)
    ? `<a href="${markdownRenderer.utils.escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`
    : "<span class=\"markdown-link\">";
};
markdownRenderer.renderer.rules.link_close = (tokens, index) => {
  let depth = 0;
  for (let at = index - 1; at >= 0; at -= 1) {
    if (tokens[at].type === "link_close") depth += 1;
    if (tokens[at].type === "link_open") {
      if (depth) { depth -= 1; continue; }
      return /^https?:\/\//i.test(tokens[at].attrGet("href") || "") ? "</a>" : "</span>";
    }
  }
  return "</span>";
};

function sanitizeMarkdown(html) {
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["img", "video", "audio", "source", "iframe", "object", "embed", "form", "input", "style", "script"],
    FORBID_ATTR: ["style", "src", "srcset", "nonce"],
    ADD_ATTR: ["target"],
  });
}

export function renderEmbed(index, reference) {
  return sanitizeMarkdown(renderReferenceEmbed(index, reference));
}

export function renderMarkdown(text, context) {
  if (context?.index && context.path) return sanitizeMarkdown(renderIndexedMarkdown(context.index, context.path));
  let properties = "";
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(text);
  if (frontmatter) {
    properties = `<details class="markdown-properties"><summary>Properties</summary><pre>${markdownRenderer.utils.escapeHtml(frontmatter[1])}</pre></details>`;
    text = text.slice(frontmatter[0].length);
  }
  return sanitizeMarkdown(properties + markdownRenderer.render(text));
}
