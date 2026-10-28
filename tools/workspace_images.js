import { parseZoteroURI } from "./workspace_sources.js";

const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const supported = /\.(?:png|jpe?g|webp)$/i;
const imageExtension = /\.(?:png|jpe?g|webp|gif|apng|svg|bmp|tiff?|avif|heic|ico)(?:[?#]|$)/i;

function decoded(value, kind) {
  try { return decodeURIComponent(value); }
  catch { return kind === "wiki" ? value : null; }
}

export function isImageDestination(destination) {
  if (typeof destination !== "string") return false;
  return imageExtension.test(decoded(destination.split("|", 1)[0].trim(), "wiki") || "");
}

export function parseImageReference(destination, label = "", options = {}) {
  const kind = options.kind === "wiki" ? "wiki" : "markdown";
  const raw = typeof destination === "string" ? destination.trim() : "";
  const path = decoded(raw, kind);
  const dimension = /^(\d{1,5})(?:x(\d{1,5}))?$/.exec(String(label).trim());
  const dimensions = dimension && Number(dimension[1]) > 0 && Number(dimension[1]) <= 10000
    && (!dimension[2] || Number(dimension[2]) > 0 && Number(dimension[2]) <= 10000)
    ? { width: Number(dimension[1]), ...(dimension[2] ? { height: Number(dimension[2]) } : {}) } : {};
  let status = "ready";
  if (!path || raw.length > 2048 || /[\x00-\x1f\x7f\\]/.test(path) || path.startsWith("/") || /^[a-z][a-z\d+.-]*:/i.test(path)) status = "invalid";
  if (/^(?:https?:)?\/\//i.test(raw)) status = "remote";
  if (status === "ready" && (!supported.test(path) || /[?#]/.test(raw))) status = "unsupported";
  const alt = dimension ? path?.slice(path.lastIndexOf("/") + 1) || "Image" : String(label || path?.slice(path.lastIndexOf("/") + 1) || "Image").slice(0, 2048);
  return {
    sourcePath: typeof options.sourcePath === "string" ? options.sourcePath : "",
    destination: raw, label: alt, kind, embed: options.embed !== false,
    ...(Number.isSafeInteger(options.from) ? { from: options.from } : {}),
    ...(Number.isSafeInteger(options.to) ? { to: options.to } : {}),
    ...dimensions, status, supported: status === "ready",
  };
}

function imageMessage(ref) {
  if (ref.status === "remote") return "Remote images are not loaded";
  if (ref.status === "unsupported") return "Unsupported image format";
  if (ref.status !== "ready") return "Invalid image reference";
  return "Loading image…";
}

export function imagePlaceholder(reference, { openOnly = false } = {}) {
  const ref = parseImageReference(reference.destination, reference.width ? `${reference.width}${reference.height ? `x${reference.height}` : ""}` : reference.label, reference);
  // Keep source positions and ownership outside executable URLs. Only the
  // confined backend resolver supplies an image URL after this is sanitized.
  const data = escapeHtml(JSON.stringify({ ...ref, label: reference.label || ref.label }));
  const open = `<span class="markdown-image${ref.embed ? "" : " markdown-image-link"}" data-image-reference="${data}">`;
  return openOnly ? open : `${open}<span class="markdown-image-status">${escapeHtml(`${ref.label}: ${imageMessage(ref)}`)}</span></span>`;
}

function mediaURL(value, document) {
  if (typeof value !== "string" || value.length > 4096) throw new Error("Image response has no valid local URL.");
  const base = document.defaultView?.location?.href;
  const url = new URL(value, base);
  if (!base || !/^https?:$/.test(url.protocol) || url.origin !== new URL(base).origin || url.username || url.password) throw new Error("Only workspace image URLs can be displayed.");
  return url.href;
}

function sourceURI(reference, element) {
  const pdfURI = value => { const parsed = parseZoteroURI(value); return parsed?.action === "open-pdf" ? parsed : null; };
  if (reference.sourceURI) return pdfURI(reference.sourceURI)?.uri || null;
  const entry = element?.closest("li");
  if (!entry) return null;
  const sequence = [...entry.querySelectorAll("[data-image-reference], .markdown-image, [data-source-uri]")].filter(node => node.closest("li") === entry);
  const header = sequence.map(node => pdfURI(node.getAttribute("data-source-uri"))).find(Boolean);
  if (!header) return null;
  const start = sequence.indexOf(element);
  for (let at = start + 1; start >= 0 && at < sequence.length; at += 1) {
    const node = sequence[at];
    if (node.matches("[data-image-reference], .markdown-image")) break;
    if (node.textContent.trim() !== "Open in PDF") continue;
    const page = pdfURI(node.getAttribute("data-source-uri"));
    // A multi-page preview keeps the same annotation identity while changing
    // its physical page. A damaged/foreign page link must not borrow the header.
    if (!page || page.scope !== header.scope || page.key !== header.key || page.annotation_key !== header.annotation_key) return null;
    return page.uri;
  }
  return header.uri;
}

export function createImageElement(reference, options = {}) {
  const document = options.document || globalThis.document;
  const ref = { ...parseImageReference(reference.destination, reference.width ? `${reference.width}${reference.height ? `x${reference.height}` : ""}` : reference.label, reference), label: reference.label || "Image" };
  const node = document.createElement("span"); node.className = `markdown-image${ref.embed ? "" : " markdown-image-link"}`;
  const status = document.createElement("span"); status.className = "markdown-image-status"; status.textContent = `${ref.label}: ${imageMessage(ref)}`;
  node.append(status);
  const locator = sourceURI(reference, options.context || node);
  let alive = true;
  node.cancelImageLoad = () => { alive = false; };
  node.imageReady = (async () => {
    if (!ref.supported) return null;
    try {
      if (typeof options.resolve !== "function") throw new Error("Image resolver is unavailable.");
      const response = await options.resolve(ref);
      const data = { ...response, url: mediaURL(response?.url, document), label: ref.label, reference: ref, sourceURI: locator };
      if (!alive) return null;
      const button = document.createElement("button"); button.type = "button"; button.className = "markdown-image-open";
      button.setAttribute("aria-label", `Open image: ${ref.label}`); button.title = response?.path || ref.destination;
      if (ref.embed) {
        const image = document.createElement("img"); image.alt = ref.label; image.loading = "lazy"; image.decoding = "async";
        if (ref.width) image.width = ref.width;
        if (ref.height) image.height = ref.height;
        image.addEventListener("error", () => { status.textContent = `${ref.label}: Image is unavailable or could not be decoded.`; node.replaceChildren(status); }, { once: true });
        image.src = data.url; button.append(image);
      } else button.textContent = ref.label;
      button.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); options.onOpen?.(data); });
      node.replaceChildren(button);
      if (data.sourceURI && options.onSource) {
        const source = document.createElement("button"); source.type = "button"; source.className = "markdown-image-source"; source.textContent = "Open in PDF";
        source.addEventListener("click", event => { event.preventDefault(); event.stopPropagation(); options.onSource(data.sourceURI); });
        node.append(source);
      }
      if (response?.status && !["current", "ready"].includes(response.status)) {
        status.textContent = response.status === "stale" ? "Preview is out of date" : "Last confirmed preview";
        node.append(status);
      }
      return data;
    } catch (error) {
      if (alive) status.textContent = `${ref.label}: ${String(error?.message || "Image is unavailable.").slice(0, 240)}`;
      return null;
    }
  })();
  return node;
}

export function hydrateImages(container, options = {}) {
  const nodes = [...container.querySelectorAll("[data-image-reference]")].slice(0, 1000);
  return Promise.all(nodes.map(async placeholder => {
    let ref;
    try {
      const value = placeholder.getAttribute("data-image-reference");
      if (!value || value.length > 8192) return;
      ref = JSON.parse(value);
      if (!ref || typeof ref.destination !== "string" || typeof ref.sourcePath !== "string") return;
    } catch { return; }
    const node = createImageElement(ref, { ...options, document: placeholder.ownerDocument, context: placeholder });
    placeholder.replaceWith(node);
    return node.imageReady;
  }));
}

export function createImageViewer(options = {}) {
  const document = options.document || globalThis.document;
  let overlay = null, previousFocus = null;
  const close = () => {
    overlay?.remove(); overlay = null;
    document.removeEventListener("keydown", keyboard, true);
    if (previousFocus?.isConnected) previousFocus.focus();
    previousFocus = null;
  };
  const keyboard = event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    else if (event.key === "Tab" && overlay) {
      const buttons = [...overlay.querySelectorAll("button")];
      const at = buttons.indexOf(document.activeElement);
      if (event.shiftKey && at <= 0) { event.preventDefault(); buttons[buttons.length - 1]?.focus(); }
      else if (!event.shiftKey && at === buttons.length - 1) { event.preventDefault(); buttons[0]?.focus(); }
    }
  };
  const open = data => {
    const url = mediaURL(data?.url, document);
    close(); previousFocus = document.activeElement;
    overlay = document.createElement("div"); overlay.className = "workspace-image-viewer"; overlay.setAttribute("role", "dialog"); overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", data?.label || data?.path || "Image viewer");
    const header = document.createElement("div"); header.className = "workspace-image-viewer-header";
    const name = document.createElement("span"); name.textContent = data?.label || data?.path || "Image"; header.append(name);
    const source = parseZoteroURI(data?.sourceURI);
    if (source?.action === "open-pdf" && options.onSource) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = "Open in PDF";
      button.addEventListener("click", () => { close(); options.onSource(source.uri); }); header.append(button);
    }
    const dismiss = document.createElement("button"); dismiss.type = "button"; dismiss.textContent = "Close"; dismiss.setAttribute("aria-label", "Close image viewer"); dismiss.addEventListener("click", close); header.append(dismiss);
    const image = document.createElement("img"); image.src = url; image.alt = data?.label || "Image";
    overlay.append(header, image); overlay.addEventListener("click", event => { if (event.target === overlay) close(); });
    document.body.append(overlay); document.addEventListener("keydown", keyboard, true); dismiss.focus();
    return overlay;
  };
  return { open, close, get element() { return overlay; } };
}
