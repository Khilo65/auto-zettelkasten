# Security Policy

## Supported versions

Security fixes are applied to the latest tagged release.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting instead of opening a public
issue. Include reproduction steps, affected versions, and the expected impact.

Auto-Zettelkasten treats Zotero as read-only. It never writes through the
Zotero API, and it never reads `zotero.sqlite`. Source text is sent to a cloud
provider only when a caller explicitly enables `--allow-cloud`. API keys are
read from environment variables and must not be stored in workspace files.
Saved configuration and the presence of a key do not grant per-run cloud
consent. Built-in Zotero and Ollama clients reject non-loopback base URLs.
