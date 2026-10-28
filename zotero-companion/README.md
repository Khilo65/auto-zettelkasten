# Annotation capability companion

This original, read-only companion exposes attachment-bound annotation
permissions through Zotero's native local API. All annotation changes still use
Zotero's own write authorization and version checks.

Build with `python3 tools/build_zotero_companion.py --output /private/path/companion.xpi`.
The builder never installs the extension or accesses a Zotero profile.

This preview is updated by manually installing a new XPI. Zotero requires an
`update_url` and its update-security policy requires HTTPS. The manifest uses an
explicitly reserved `.invalid` HTTPS address because no update service is
published. Update checks fail closed; no compatibility, signing or transport
security setting is disabled. A distributed release needs a maintained HTTPS
update manifest before enabling automatic updates.
