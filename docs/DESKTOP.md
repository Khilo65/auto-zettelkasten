# Desktop app

The desktop app uses the same local mapper and frontend inside a native window.
Choose a connection, select a Zotero collection, choose a destination or Obsidian
vault, review the estimate, and approve the mapping. The remembered vault remains
local to the user. API keys are not bundled into the application.

## Run from source

Use Python 3.12 on macOS or Windows in a virtual environment:

```sh
python -m pip install -e ".[desktop,local-mapping]"
auto-zettelkasten-desktop
```

The desktop window starts its own loopback server on an available port. You do
not need to start `serve` separately. Native folder dialogs select the output
folder or an existing Obsidian vault. Mapping still requires explicit approval.
Local MiniLM + Leiden is the default mapping backend. Select an existing cached
`all-MiniLM-L6-v2` model directory before mapping; the app does not download model
weights. HDBSCAN remains optional and requires the separate
`local-mapping-hdbscan` extra when running from source.

## Graph views

The graph opens with accepted research relations selected and navigation
documents hidden. Graph settings separately enable citations, ordinary Markdown
links, discovery links, and navigation documents. Node size follows the visible
connections. A source with no visible connections has not necessarily been
examined for relationships.

Indexes and literature maps remain available in the file panel and quick open.
Verified generated canonical and compatibility copies share a graph node while
retaining their file paths. Ordinary notes, different versions, and copies with
different personal edits remain separate.

Missing and ambiguous Markdown destinations are labeled explicitly. The optional
**Existing files only** filter hides these graph nodes and reports their counts.
Markdown indexing, recorded source coverage, and research comparison coverage
are separate: a complete file index does not establish complete source reading
or relationship discovery.

## Build for macOS or Windows

Build on the target operating system, using a clean Python 3.12 virtual
environment. The bundles include Python and mapper dependencies; end users do
not install Python.

```sh
python -m pip install -e ".[test,desktop,desktop-build,local-mapping]"
python -m pytest tests/test_desktop.py tests/test_desktop_packaging.py tests/test_webapp.py
python tools/build_desktop.py
```

The normal bundle includes the local MiniLM/Leiden runtime and is substantially
larger than the viewer alone. The build fails early if required dependencies or
their package metadata are missing. Use a clean environment: the build collects
installed package code, native libraries, and package data, not Hugging Face
caches, model directories, workspaces, or credentials. Model weights remain
external and must be selected on the user's machine. HDBSCAN is not required;
install `local-mapping-hdbscan` in the build environment only to include it.

On macOS, the result is `dist/Auto-Zettelkasten.app`. Double-click it or copy it
to Applications. Build separately for Apple Silicon and Intel Macs. These are
development bundles without Developer ID signing or notarization; downloaded
copies may require approval in macOS Privacy & Security before opening.

On Windows, the result is `dist/Auto-Zettelkasten/Auto-Zettelkasten.exe`.
Distribute the entire `Auto-Zettelkasten` directory, including `_internal`, and
run the executable after extracting it. The app uses the [Microsoft Edge
WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
and .NET Framework 4.8; these are operating-system prerequisites, not Python
dependencies. The build is unsigned and Windows may ask for approval.

Zotero Desktop is external and must be running with its local API enabled.
Obsidian is optional; select an existing vault when exporting to it. ChatGPT
connection uses an external compatible Codex installation and its sign-in.
Both API-key and ChatGPT/Codex connections are exposed on macOS and Windows.
If automatic detection misses your CLI, expand **Choose a Codex installation**
and select its native binary. The path is remembered, without copying login
credentials into preferences. On Windows, choose the compatible `codex.exe`;
an npm `codex.cmd` shim is not used. Run the Windows app as your ordinary,
non-elevated user so private credential directories have that user's owner.
Windows workers use a private ACL and a Job Object to contain their processes.
Windows uses the existing text or local OCR/image fallback for Codex input;
the native PDF companion is currently verified only on Apple Silicon macOS.
Windows runtime validation must
pass on a Windows host before the Windows build can be called verified.
The desktop bundle does not install Codex,
Zotero, Obsidian, or WebView2, and does not include user credentials.

## Offline bundle verification

The frozen app supports a diagnostic check without opening a GUI, connecting to
providers, signing in, or mapping user documents:

```sh
# macOS
dist/Auto-Zettelkasten.app/Contents/MacOS/Auto-Zettelkasten --smoke-test
dist/Auto-Zettelkasten.app/Contents/MacOS/Auto-Zettelkasten --local-mapping-smoke-test
```

```powershell
# Windows: wait for the windowed executable and check its exit code
$process = Start-Process -FilePath .\dist\Auto-Zettelkasten\Auto-Zettelkasten.exe -ArgumentList '--smoke-test' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Desktop smoke test failed' }
$process = Start-Process -FilePath .\dist\Auto-Zettelkasten\Auto-Zettelkasten.exe -ArgumentList '--local-mapping-smoke-test' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Local mapping runtime smoke test failed' }
```

The local runtime check imports the MiniLM modules, runs a tiny CPU tensor and
Leiden operation, and tokenizes synthetic text using a temporary vocabulary. It
does not open a cached model, download weights, or map user documents. This
checks bundled dependencies; validate actual MiniLM encoding separately with an
explicitly selected local model before distributing a release.

The `Desktop builds` GitHub Actions workflow builds Apple Silicon, Intel macOS,
and Windows x64 bundles on their own hosts, runs offline tests and frozen smoke
checks, and uploads artifacts. It runs on relevant pull requests and manual
dispatch. It does not publish releases, sign binaries, or perform live mapping.

The offline checks do not verify native GUI rendering, WebView2 installation,
live login, or paid mapping. Validate these on each target platform before
distributing a release.

Packaging follows [PyInstaller's native windowed bundle
options](https://pyinstaller.org/en/stable/usage.html) and [pywebview platform
requirements](https://pywebview.flowrl.com/guide/installation.html). Runner
architectures are listed in [GitHub's hosted runner
reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
