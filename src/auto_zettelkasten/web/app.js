"use strict";

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let auth = { ready: false };
let ticket = null;
let busy = false;
let running = false;
let loginPending = false;
let savedVault = "";
let pollTimer = null;
let collectionsAvailable = false;
let mappedWorkspace = "";
let openingMemory = false;

function choice(name) {
  return document.querySelector(`input[name="${name}"]:checked`).value;
}

async function api(path, body) {
  const response = await fetch(`/api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Session-Token": token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || result.message || "The local app could not complete this request.");
    error.status = response.status;
    error.code = result.code;
    error.current = result.current;
    throw error;
  }
  return result;
}

if (document.querySelector('meta[name="desktop-shell"]')?.content === "true") {
  let desiredZoom = 1, appliedZoom = 1, zoomRevision = 0;
  let changingZoom = false, resetZoom = false, zoomError = null;

  async function applyViewZoom() {
    if (changingZoom) return;
    changingZoom = true;
    try {
      while (desiredZoom !== appliedZoom || resetZoom) {
        const level = desiredZoom, revision = zoomRevision;
        resetZoom = false;
        try {
          const result = await api("view-zoom", { level });
          if (!Number.isFinite(result?.level) || result.level !== level) throw new Error("The desktop view size could not be confirmed. Try again.");
          appliedZoom = level;
          if (zoomError && zoomError.target.textContent === zoomError.message) {
            zoomError.target.textContent = "";
            zoomError.target.hidden = true;
            zoomError.target.classList.remove("is-error");
          }
          zoomError = null;
        } catch (error) {
          // Only a failure for the latest key intent may roll that intent back.
          if (revision === zoomRevision) { desiredZoom = appliedZoom; resetZoom = false; }
          const target = $("workspace-screen").hidden ? $("notice") : $("workspace-message");
          const message = `Could not change view size. ${error.message}`;
          target.textContent = message;
          target.hidden = false;
          if (target.id === "workspace-message") target.classList.add("is-error");
          zoomError = { target, message };
        }
      }
    } finally {
      changingZoom = false;
    }
  }

  document.addEventListener("keydown", (event) => {
    if (!(event.metaKey || event.ctrlKey) || event.altKey || event.isComposing) return;
    const add = event.key === "+" || event.key === "=" || event.code === "NumpadAdd";
    const subtract = event.key === "-" || event.code === "NumpadSubtract";
    const reset = event.key === "0";
    if (!add && !subtract && !reset) return;
    event.preventDefault();
    event.stopPropagation();
    const level = reset ? 1 : Math.max(0.5, Math.min(2, Math.round((desiredZoom + (add ? 0.1 : -0.1)) * 10) / 10));
    if (level === desiredZoom && !reset) return;
    desiredZoom = level;
    zoomRevision += 1;
    if (reset) resetZoom = true;
    applyViewZoom();
  }, true);
}

function status(id, message, error = false) {
  $(id).textContent = message;
  $(id).classList.toggle("error", error);
}

function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function controls() {
  $("setup").disabled = busy || running || loginPending;
  const destinationReady = choice("destination") === "obsidian" ? $("vault").value.trim() : $("output-folder").value.trim();
  const connected = auth.ready && auth.method === choice("method");
  const sourceReady = choice("source-scope") === "library" || $("collection").value;
  $("estimate").disabled = busy || running || loginPending || !connected || !sourceReady || !destinationReady;
  $("approve").disabled = busy || running || !ticket || !$("cloud-consent").checked;
  $("cancel-estimate").disabled = busy || running;
  $("connect-codex").disabled = busy || running || loginPending;
  $("login-codex").disabled = busy || running || loginPending;
  $("auth-badge").textContent = connected ? (auth.method === "deepseek" ? "Key ready" : "Connected") : "Not connected";
  $("auth-badge").classList.toggle("ready", connected);
  $("open-mapped-workspace").disabled = openingMemory || running;
}

function updateChoices() {
  $("codex-fields").hidden = choice("method") !== "codex";
  $("key-fields").hidden = choice("method") !== "deepseek";
  $("folder-fields").hidden = choice("destination") !== "folder";
  $("vault-fields").hidden = choice("destination") !== "obsidian";
  const library = choice("source-scope") === "library";
  $("collection-fields").hidden = library;
  $("collection").required = !library;
  $("collection").disabled = library || !collectionsAvailable;
  $("include-subcollections").disabled = library;
  controls();
}

function invalidate() {
  if (ticket) api("cancel", { estimate_id: ticket.estimate_id }).catch(() => {});
  ticket = null;
  $("review").hidden = true;
  $("cloud-consent").checked = false;
  if (!running) $("review-empty").hidden = false;
  controls();
}

function applyAuth(value) {
  auth = value.auth || value;
  loginPending = ["pending", "login_pending", "logging_in"].includes(auth.status);
  status("auth-message", auth.ready && auth.method === "deepseek" ? "" : auth.message || "", ["failed", "blocked", "unavailable"].includes(auth.status));
  controls();
}

async function connect(method) {
  invalidate();
  notice();
  busy = true;
  controls();
  status("auth-message", method === "codex" ? "Checking your local ChatGPT login…" : "Preparing the API key…");
  try {
    if (method === "deepseek" && !$("api-key").value.trim()) throw new Error("Paste a DeepSeek API key first.");
    applyAuth(await api("connect", { method, ...(method === "deepseek" ? { api_key: $("api-key").value.trim() } : { codex_executable: $("codex-executable").value.trim() }) }));
    if (!auth.ready) status("auth-message", auth.message || "Connection is not ready. Check your login and try again.", true);
  } catch (error) {
    status("auth-message", error.message, true);
  } finally {
    $("api-key").value = "";
    busy = false;
    controls();
  }
}

async function findZotero() {
  $("find-zotero").disabled = true;
  status("zotero-message", "Looking for your Zotero collections…");
  try {
    const { collections } = await api("collections");
    const previous = $("collection").value;
    $("collection").replaceChildren(new Option(collections.length ? "Choose a collection" : "No collections found", ""));
    for (const row of collections) {
      const data = row.data || row;
      const key = row.key || data.key;
      if (key) $("collection").add(new Option(row.path || data.name || row.name || key, key));
    }
    $("collection").value = previous;
    collectionsAvailable = collections.length > 0;
    status("zotero-message", collections.length ? `${collections.length} ${collections.length === 1 ? "collection" : "collections"} found. Nested collections show their full path. Zotero is read-only.` : "Zotero is connected, with no collections. Choose Entire Zotero library, or create a collection in Zotero and try again.");
  } catch (error) {
    collectionsAvailable = false;
    $("collection").replaceChildren(new Option("Zotero is unavailable", ""));
    $("collection").disabled = true;
    status("zotero-message", `${error.message} Open Zotero Desktop and enable its local API, then try Find Zotero.`, true);
  } finally {
    $("find-zotero").disabled = false;
    updateChoices();
  }
}

async function saveVault() {
  if (choice("destination") !== "obsidian") return;
  const remember = $("remember-vault").checked;
  const vault = $("vault").value.trim();
  if (remember && !vault) return;
  try {
    const result = await api("preferences", { vault, remember_vault: remember });
    const preferences = result.preferences || result;
    savedVault = preferences.vault || "";
    $("forget-vault").hidden = !savedVault;
    status("destination-message", savedVault ? "Vault remembered on this computer." : "Vault will be used only for this mapping.");
  } catch (error) {
    status("destination-message", error.message, true);
  }
}

async function pickFolder(kind) {
  busy = true;
  controls();
  status("destination-message", "Choose a folder in the system dialog.");
  try {
    const result = await api("pick-folder", { kind });
    if (result.path) {
      $(kind === "codex" ? "codex-executable" : kind === "vault" ? "vault" : "output-folder").value = result.path;
      invalidate();
      status("destination-message", "");
      if (kind === "vault") await saveVault();
    } else status("destination-message", "Folder selection canceled. You can also enter a path above.");
  } catch (error) {
    status("destination-message", `${error.message} You can enter the full folder path above.`, true);
  } finally {
    busy = false;
    controls();
  }
}

const usd = (amount) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount);

function renderEstimate(result) {
  ticket = result;
  const estimate = result.estimate;
  const source = result.source || result.collection;
  const selection = source.path || source.name;
  $("review-collection").textContent = selection + (source.include_subcollections ? " (including subcollections)" : "");
  $("cloud-consent-text").textContent = `I approve sending the sources from ${selection}${source.include_subcollections ? " and its subcollections" : ""} to the selected provider for mapping.`;
  $("review-sources").textContent = `${estimate.inventory_count} total · ${estimate.new_source_jobs} to read · ${estimate.reusable_or_existing_records} reusable`;
  $("review-provider").textContent = estimate.provider === "codex" ? "ChatGPT subscription" : "DeepSeek API key";
  const costs = estimate.total_cost_usd || {};
  const priced = costs.expected_usd !== null && costs.expected_usd !== undefined;
  $("cost-label").textContent = estimate.provider === "codex" ? "Subscription usage" : "Estimated cost";
  $("review-cost").textContent = priced ? `${usd(costs.expected_usd)} expected · ${usd(costs.low_usd)}–${usd(costs.high_usd)} range (USD)` : estimate.provider === "codex" ? "Uses your ChatGPT plan" : "Dollar estimate unavailable";
  const sourceCalls = estimate.source_calls?.expected;
  const graphCalls = estimate.graph_calls?.expected;
  $("calls-row").hidden = sourceCalls === undefined || graphCalls === undefined;
  $("review-calls").textContent = `${sourceCalls ?? 0} source + ${graphCalls ?? 0} relationship calls`;
  $("review-workspace").textContent = result.workspace;
  $("obsidian-path-row").hidden = !result.obsidian_path;
  $("review-obsidian").textContent = result.obsidian_path || "";
  $("cost-note").textContent = priced ? "This is an estimate, not a spending cap. Document length, extraction, and relationship work can change the final cost." : "Subscription usage and remaining quota cannot be priced here. Quota interruptions leave completed work checkpointed.";
  $("review-empty").hidden = true;
  $("review").hidden = false;
  $("cloud-consent").checked = false;
}

async function estimate(event) {
  event.preventDefault();
  if (busy || running) return;
  invalidate();
  notice();
  busy = true;
  controls();
  $("review-empty").hidden = true;
  $("estimate-loading").hidden = false;
  try {
    renderEstimate(await api("estimate", {
      scope: choice("source-scope"),
      collection_key: choice("source-scope") === "collection" ? $("collection").value : "",
      include_subcollections: choice("source-scope") === "collection" && $("include-subcollections").checked,
      output_folder: $("output-folder").value.trim(),
      destination: choice("destination"),
      vault: $("vault").value.trim(),
      remember_vault: $("remember-vault").checked,
    }));
  } catch (error) {
    notice(error.message);
    $("review-empty").hidden = false;
  } finally {
    busy = false;
    $("estimate-loading").hidden = true;
    controls();
    if (ticket) $("cloud-consent").focus();
  }
}

function renderRun(value) {
  const run = value.run || value;
  if (!run || run.status === "idle") return;
  running = ["running", "queued", "exporting"].includes(run.status);
  $("review-empty").hidden = true;
  $("review").hidden = true;
  $("run-panel").hidden = false;
  const titles = { completed: "Your research memory is ready.", partial: "Mapping needs review.", failed: "Mapping could not finish.", exporting: "Adding your notes to Obsidian…" };
  $("run-title").textContent = titles[run.status] || "Building your research memory…";
  const report = run.report || {};
  const counts = run.counts || report;
  const total = counts.inventory_count || 0;
  const done = counts.terminal_count ?? ((counts.validated_note_count || 0) + (counts.limited_note_count || 0) + (counts.parked_for_review_count || 0) + (counts.duplicate_alias_count || 0));
  if (total > 0) {
    $("run-progress").max = total;
    $("run-progress").value = Math.min(total, done);
  } else $("run-progress").removeAttribute("value");
  $("run-progress").hidden = !running;
  $("run-detail").textContent = total ? `${done} of ${total} sources processed · ${counts.validated_note_count || 0} atomic notes · ${counts.typed_relation_count || 0} relationships` : "Preparing your sources and checking saved work.";
  status("run-message", run.error || run.export_error || run.message || (running ? "Keep this local app running. Completed work is checkpointed." : ""), run.status === "failed" || Boolean(run.export_error));
  mappedWorkspace = run.memory_workspace || run.workspace || "";
  $("open-mapped-workspace").hidden = running || !run.workspace_available || !mappedWorkspace;
  $("run-paths").replaceChildren();
  const exportRoot = run.obsidian?.metadata?.export_root || run.obsidian?.export_root || run.obsidian_path;
  for (const [label, path] of [["Workspace", run.workspace], ["Obsidian folder", exportRoot]]) {
    if (!path) continue;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const definition = document.createElement("dd");
    term.textContent = label;
    definition.textContent = path;
    definition.className = "path";
    row.append(term, definition);
    $("run-paths").append(row);
  }
  $("new-map").hidden = running;
  controls();
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = window.setInterval(async () => {
    try {
      if (loginPending) applyAuth((await api("state")).auth);
      if (running) renderRun(await api("run"));
      if (!running && !loginPending) { window.clearInterval(pollTimer); pollTimer = null; }
    } catch (error) { notice(`${error.message} Keep the local app running and refresh to reconnect.`); }
  }, 2000);
}

$("connect-codex").addEventListener("click", () => connect("codex"));
$("connect-key").addEventListener("click", () => connect("deepseek"));
$("login-codex").addEventListener("click", async () => {
  invalidate();
  busy = true;
  controls();
  status("auth-message", "Opening ChatGPT sign-in in your browser…");
  try { applyAuth(await api("login", { codex_executable: $("codex-executable").value.trim() })); startPolling(); }
  catch (error) { status("auth-message", error.message, true); }
  finally { busy = false; controls(); }
});
$("find-zotero").addEventListener("click", () => { invalidate(); findZotero(); });
$("pick-output").addEventListener("click", () => pickFolder("folder"));
$("pick-vault").addEventListener("click", () => pickFolder("vault"));
$("pick-codex").addEventListener("click", () => pickFolder("codex"));
$("vault").addEventListener("change", saveVault);
$("remember-vault").addEventListener("change", saveVault);
$("forget-vault").addEventListener("click", async () => {
  try {
    await api("preferences", { vault: "", remember_vault: false });
    savedVault = "";
    $("remember-vault").checked = false;
    $("forget-vault").hidden = true;
    status("destination-message", "Saved vault forgotten. The current path can still be used for this mapping.");
    invalidate();
  } catch (error) { status("destination-message", error.message, true); }
});
for (const input of $("setup").querySelectorAll("input, select")) {
  input.addEventListener("input", () => { invalidate(); updateChoices(); });
  input.addEventListener("change", () => { invalidate(); updateChoices(); });
}
$("mapping-form").addEventListener("submit", estimate);
$("cloud-consent").addEventListener("change", controls);
$("cancel-estimate").addEventListener("click", invalidate);
$("approve").addEventListener("click", async () => {
  if (!ticket || !$("cloud-consent").checked || busy || running) return;
  busy = true;
  controls();
  notice();
  try {
    renderRun(await api("approve", { estimate_id: ticket.estimate_id, allow_cloud: true }));
    ticket = null;
    startPolling();
  } catch (error) { notice(error.message); invalidate(); }
  finally { busy = false; controls(); }
});
$("new-map").addEventListener("click", () => {
  running = false;
  mappedWorkspace = "";
  $("open-mapped-workspace").hidden = true;
  $("run-panel").hidden = true;
  invalidate();
  document.querySelector('input[name="source-scope"]:checked').focus();
});
$("open-mapped-workspace").addEventListener("click", async () => {
  if (!mappedWorkspace || openingMemory || running) return;
  const path = mappedWorkspace;
  openingMemory = true; controls();
  try {
    if (!window.openMemoryWorkspace) throw new Error("The note workspace could not load. Restart the app and try again.");
    await window.openMemoryWorkspace(path, "02_source_memory/indexes/INDEX.md");
  } catch (error) { status("run-message", error.message, true); }
  finally { openingMemory = false; controls(); }
});

async function initialize() {
  try {
    const state = await api("state");
    $("output-folder").value = state.default_output_folder || "";
    $("codex-executable").value = state.codex_executable || "";
    savedVault = state.preferences?.vault || "";
    if (savedVault) {
      $("vault").value = savedVault;
      document.querySelector('input[name="destination"][value="obsidian"]').checked = true;
      $("forget-vault").hidden = false;
    }
    if (state.auth?.ready && ["deepseek", "codex"].includes(state.auth.method)) document.querySelector(`input[name="method"][value="${state.auth.method}"]`).checked = true;
    applyAuth(state.auth || { ready: false });
    updateChoices();
    renderRun(state.run);
    if (running || loginPending) startPolling();
  } catch (error) { notice(error.message); }
}
let mappingInitialized = false;
window.initializeMapping = async () => {
  if (mappingInitialized) return;
  mappingInitialized = true;
  await initialize();
  await findZotero();
};
