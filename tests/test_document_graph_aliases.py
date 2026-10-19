from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentService


REPOSITORY = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None or not (REPOSITORY / "node_modules" / "markdown-it").is_dir(),
    reason="Backend-to-graph integration requires Node and installed web dependencies",
)
GRAPH_SCRIPT = """
import fs from "node:fs";
import { buildLinkIndex, buildLibraryIndex, graphData, parseLibraryDocument } from "./tools/workspace_links.js";
const snapshots = JSON.parse(fs.readFileSync(0, "utf8"));
const results = snapshots.flatMap((snapshot) => {
  const metadata = { document_identities: snapshot.document_identities };
  return [
    buildLinkIndex(snapshot.documents, [], null, metadata),
    buildLibraryIndex(snapshot.documents.map(parseLibraryDocument), [], null, metadata),
  ].map((index) => ({ graph: graphData(index), sources: index.references.map((ref) => ref.sourcePath) }));
});
console.log(JSON.stringify(results));
"""


@pytest.mark.parametrize("personal_overrides", [False, True])
def test_backend_verified_aliases_retain_distinct_relative_destinations(tmp_path, personal_overrides):
    if personal_overrides and not documents._SAFE_WRITES:
        pytest.skip("Safe override writes are unavailable")
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("engine_version: test\n")
    canonical = "03_literature_synthesis/maps/map-one/clusters/Cluster.md"
    compatibility = "03_literature_synthesis/clusters/Cluster.md"
    header = "---\ntype: literature_cluster\ncluster_id: cluster-one\n---\n"
    original = header + "# Generated cluster\n[Context](./Context.md)\n"
    contexts = set()
    for path in (canonical, compatibility):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original)
        context = target.with_name("Context.md")
        context.write_text(f"# Context\nDistinct context for {path}\n")
        contexts.add(context.relative_to(root).as_posix())
    service = DocumentService(journal_root=tmp_path / "journal", override_root=tmp_path / "overrides")
    try:
        workspace = service.open(root)
        effective = header + "# Personal cluster\n[Context](./Context.md)\n" if personal_overrides else original
        if personal_overrides:
            for path in (canonical, compatibility):
                note = service.read(workspace["id"], path)
                service.save_override(workspace["id"], note["id"], note["revision"], effective)
        legacy = service.index(workspace["id"])
        page = service.index_page(workspace["id"])
        rows = page["documents"][:]
        while not page["complete"]:
            page = service.index_page(workspace["id"], page["next_cursor"])
            rows.extend(page["documents"])
        paged = {"documents": rows, "document_identities": page["document_identities"]}
        expected_identity = [{"canonical_path": canonical, "path_aliases": [compatibility], "verified": True}]
        for snapshot in (legacy, paged):
            assert snapshot["document_identities"] == expected_identity
            assert {row["text"] for row in snapshot["documents"] if row["path"] in {canonical, compatibility}} == {effective}
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", GRAPH_SCRIPT],
            input=json.dumps([legacy, paged]), text=True, capture_output=True, cwd=REPOSITORY, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        for output in json.loads(result.stdout):
            graph = output["graph"]
            assert set(output["sources"]) == {canonical, compatibility}
            assert len(graph["nodes"]) == 3
            assert len(graph["edges"]) == 2
            assert {edge["source"] for edge in graph["edges"]} == {canonical}
            assert {edge["target"] for edge in graph["edges"]} == contexts
            assert {node["path"]: node["degree"] for node in graph["nodes"]} == {canonical: 2, **dict.fromkeys(contexts, 1)}
            assert graph["partial"] is False
        assert all((root / path).read_text() == original for path in (canonical, compatibility))
    finally:
        service.close()
