/* Original capability-only adapter. All writes remain on Zotero's native API. */
"use strict";

var annotationCapabilityEndpoint = null;
var annotationCapabilityRoutes = [
  "/api/users/:userID/items/:itemKey/az-annotation-capabilities",
  "/api/groups/:groupID/items/:itemKey/az-annotation-capabilities",
];

function validKey(value) {
  return typeof value === "string" && /^[A-Z0-9]{8}$/.test(value);
}

function safeVersion(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

async function startup() {
  await Zotero.initializationPromise;
  if (!/^10\.0(?:\.|$)/.test(Zotero.version) || !Zotero.Server?.LocalAPI?.Item
      || typeof Zotero.Annotations?.toJSONSync !== "function"
      || typeof Zotero.Items?.getByLibraryAndKeyAsync !== "function") {
    Zotero.debug("Annotation capability companion is incompatible; leaving annotations read-only");
    return;
  }
  if (annotationCapabilityRoutes.some(route => Zotero.Server.Endpoints[route])) {
    throw new Error("Annotation capability route is already registered");
  }
  annotationCapabilityEndpoint = class extends Zotero.Server.LocalAPI.Item {
    supportedMethods = ["GET"];

    async run(request) {
      const { headers, pathParams, libraryID, searchParams } = request;
      const failure = (status, message) => [status, { "Content-Type": "text/plain", "Cache-Control": "no-store" }, message];
      if (request.method !== "GET") return failure(405, "Read-only endpoint");
      if (headers.has("Origin")) return failure(403, "Browser-origin requests are not supported");
      if (headers.get("Zotero-Server-ID") !== Zotero.Server.LocalAPI.getServerID()) {
        return failure(428, "Expected Zotero server identity is required");
      }
      if (pathParams.userID !== undefined && String(pathParams.userID) !== "0") {
        return failure(400, "Use the local personal-library identity");
      }
      if (!validKey(pathParams.itemKey) || searchParams.getAll("annotationKeys").length !== 1) {
        return failure(400, "Select one PDF and a bounded annotation list");
      }
      const raw = searchParams.get("annotationKeys");
      if (typeof raw !== "string" || raw.length > 575) return failure(400, "Annotation list exceeds its limit");
      const keys = raw === "" ? [] : raw.split(",");
      if (keys.length > 64 || keys.some(key => !validKey(key)) || new Set(keys).size !== keys.length) {
        return failure(400, "Select at most 64 distinct annotation keys");
      }
      const pdf = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, pathParams.itemKey);
      if (!pdf || pdf.libraryID !== libraryID || !pdf.isPDFAttachment?.()) return failure(404, "PDF attachment not found");
      if (!safeVersion(pdf.clientVersion)) return failure(501, "Unsupported local version contract");
      const attachmentReadOnly = !pdf.isEditable() || !!pdf.deleted || !!pdf.parentItem?.deleted;
      const annotations = [], missing = [];
      for (const key of keys) {
        const annotation = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
        if (!annotation || annotation.deleted) { missing.push(key); continue; }
        if (annotation.libraryID !== libraryID || !annotation.isAnnotation?.() || annotation.parentID !== pdf.id) {
          return failure(409, "Annotation does not belong to this PDF");
        }
        if (!safeVersion(annotation.clientVersion)) return failure(501, "Unsupported local version contract");
        const flags = Zotero.Annotations.toJSONSync(annotation);
        if (typeof flags.isExternal !== "boolean" || typeof flags.readOnly !== "boolean") {
          return failure(501, "Unsupported annotation permission contract");
        }
        annotations.push({ key, version: annotation.clientVersion, isExternal: flags.isExternal,
          readOnly: attachmentReadOnly || flags.readOnly || !annotation.isEditable("edit") });
      }
      const scope = pathParams.groupID ? `groups/${pathParams.groupID}` : "users/0";
      return [200, { "Content-Type": "application/json", "Cache-Control": "no-store" }, JSON.stringify({
        bridge_version: 1, zotero_version: Zotero.version, server_id: Zotero.Server.LocalAPI.getServerID(),
        scope, attachment_key: pdf.key, attachment_version: pdf.clientVersion,
        attachment_read_only: attachmentReadOnly, annotations, missing,
      })];
    }
  };
  for (const route of annotationCapabilityRoutes) Zotero.Server.Endpoints[route] = annotationCapabilityEndpoint;
}

function shutdown() {
  for (const route of annotationCapabilityRoutes) {
    if (Zotero.Server.Endpoints[route] === annotationCapabilityEndpoint) delete Zotero.Server.Endpoints[route];
  }
  annotationCapabilityEndpoint = null;
}

function install() {}
function uninstall() {}
