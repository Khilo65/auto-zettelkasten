# Contributing

Auto-Zettelkasten is a file-first Python 3.11+ project.

1. Create a virtual environment and install `.[test]`.
2. Keep Zotero access read-only and preserve the explicit cloud-consent gate.
3. Use only synthetic or redistributable fixtures. Never commit PDFs, notes,
   credentials, or metadata from a real library.
4. Run `python -m pytest` before opening a pull request.
5. Keep `auto_zettelkasten` independent: importing `research_os` is an
   architecture violation.

Behavior changes should include focused tests. Artifact-schema changes must
increment `ARTIFACT_SCHEMA_VERSION` and document compatibility implications.
