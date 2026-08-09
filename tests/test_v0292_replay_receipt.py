from __future__ import annotations

import os
from pathlib import Path

import pytest

import auto_zettelkasten.api as api_module
from auto_zettelkasten.api import build_map, initialize_workspace
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import LiteratureMappingPolicy, NavigationPolicy


def _receipt_identity() -> str:
    return api_module._build_map_receipt_identity(
        provider="ollama",
        model="fake-1",
        question=None,
        policy=LiteratureMappingPolicy(),
        navigation=NavigationPolicy(),
        comparison_collection_keys=(),
        source_set=None,
    )


def _seed_replay_receipt(root: Path) -> tuple[Path, Path]:
    note = root / "02_source_memory" / "notes" / "source.md"
    profile = root / "02_source_memory" / "profiles" / "note-source.yml"
    graph = root / "03_literature_synthesis" / "graph.yml"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("A stable source note.\n", encoding="utf-8")
    write_yaml(profile, {"source_id": "source", "note_id": "note-source"})
    write_yaml(graph, {"relationships": []})
    receipt = (
        root / "11_state" / "runs" / "receipt-replay" / "semantic_build_receipt.yml"
    )
    write_yaml(
        receipt,
        {
            "receipt_schema_version": "2",
            "identity": _receipt_identity(),
            "upstream_fingerprint": "upstream-v1",
            "status": "built",
            "created_at": "2026-08-08T00:00:00+00:00",
            "semantic_replayable": True,
            "upstream_inputs": [
                api_module._upstream_input_row(
                    root,
                    note,
                    kind="semantic_note",
                    semantic_sha256=api_module.semantic_note_hash(
                        note.read_text(encoding="utf-8")
                    ),
                ),
                api_module._upstream_input_row(root, profile, kind="profile"),
            ],
            "workspace_note_paths": [str(note.relative_to(root))],
            "workspace_profile_paths": [str(profile.relative_to(root))],
            "artifacts": api_module._artifact_inventory(root, [graph]),
            "summary": {"cluster_count": 0},
        },
    )
    return note, graph


def test_schema_two_receipt_returns_before_note_parsing_or_provider_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_workspace(tmp_path)
    _seed_replay_receipt(tmp_path)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("semantic replay must return before graph setup")

    monkeypatch.setattr(api_module, "all_workspace_note_rows", unexpected)
    monkeypatch.setattr(api_module, "provider_from_name", unexpected)

    result = build_map(
        tmp_path,
        run_id="receipt-replay",
        provider="ollama",
        model="fake-1",
        resume=True,
    )

    assert result.status == "built"
    assert result.metadata == {"cluster_count": 0}


def test_receipt_tolerates_mtime_only_changes_but_not_output_content_changes(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    note, graph = _seed_replay_receipt(tmp_path)
    identity = _receipt_identity()

    for path in (note, graph):
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is not None

    graph.write_text("relationships: changed\n", encoding="utf-8")
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None


def test_receipt_hashes_equal_size_equal_mtime_inputs_and_outputs(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    note, graph = _seed_replay_receipt(tmp_path)
    identity = _receipt_identity()
    note_stat = note.stat()
    graph_stat = graph.stat()

    original_note = note.read_text(encoding="utf-8")
    note.write_text(original_note.replace("note", "mote"), encoding="utf-8")
    os.utime(note, ns=(note_stat.st_atime_ns, note_stat.st_mtime_ns))
    assert note.stat().st_size == note_stat.st_size
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None

    _seed_replay_receipt(tmp_path)
    graph_stat = graph.stat()
    original_graph = graph.read_text(encoding="utf-8")
    graph.write_text(original_graph.replace("[]", "{}"), encoding="utf-8")
    os.utime(graph, ns=(graph_stat.st_atime_ns, graph_stat.st_mtime_ns))
    assert graph.stat().st_size == graph_stat.st_size
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None


def test_schema_one_receipt_is_a_safe_cache_miss(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    _seed_replay_receipt(tmp_path)
    receipt = (
        tmp_path
        / "11_state"
        / "runs"
        / "receipt-replay"
        / "semantic_build_receipt.yml"
    )
    payload = api_module.read_yaml(receipt, {})
    write_yaml(receipt, {**payload, "receipt_schema_version": "1"})

    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        _receipt_identity(),
        workspace_wide_selection=True,
    ) is None


def test_receipt_invalidates_on_upstream_content_or_workspace_membership_change(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    note, _ = _seed_replay_receipt(tmp_path)
    identity = _receipt_identity()

    note.write_text("A materially changed source note.\n", encoding="utf-8")
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None

    _seed_replay_receipt(tmp_path)
    profile = tmp_path / "02_source_memory" / "profiles" / "note-source.yml"
    write_yaml(profile, {"source_id": "different"})
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None

    _seed_replay_receipt(tmp_path)
    (note.parent / "new-source.md").write_text("New source.\n", encoding="utf-8")
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        identity,
        workspace_wide_selection=True,
    ) is None


def test_machine_graph_state_and_generated_source_sets_do_not_change_fingerprint(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    typed_links = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    generated_source_set = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / "generated.yml"
    )
    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "provenance": "machine",
                }
            ]
        },
    )
    write_yaml(generated_source_set, {"source_ids": ["A"]})
    arguments = {
        "note_rows": [],
        "source_set": {"source_set_id": "workspace", "source_ids": []},
        "provider": "ollama",
        "model": "fake-1",
        "question": None,
        "policy": LiteratureMappingPolicy(),
        "navigation": NavigationPolicy(),
    }
    before = api_module._build_map_semantic_fingerprint(tmp_path, **arguments)

    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "C",
                    "relation_type": "contrasts",
                    "provenance": "machine",
                }
            ]
        },
    )
    write_yaml(generated_source_set, {"source_ids": ["A", "B", "C"]})
    assert api_module._build_map_semantic_fingerprint(tmp_path, **arguments) == before

    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "provenance": "human_authored",
                }
            ]
        },
    )
    assert api_module._build_map_semantic_fingerprint(tmp_path, **arguments) != before


def test_machine_relationship_changes_do_not_invalidate_receipt_but_human_links_do(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    typed_links = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "provenance": "machine",
                }
            ]
        },
    )
    note, _ = _seed_replay_receipt(tmp_path)
    receipt = (
        tmp_path
        / "11_state"
        / "runs"
        / "receipt-replay"
        / "semantic_build_receipt.yml"
    )
    payload = api_module.read_yaml(receipt, {})
    payload["upstream_inputs"] = api_module._build_map_upstream_inventory(
        tmp_path,
        note_rows=[
            {
                "note_id": "note-source",
                "source_id": "source",
                "note_path": str(note.relative_to(tmp_path)),
            }
        ],
        source_set_argument=None,
        run_id="receipt-replay",
    )
    write_yaml(receipt, payload)

    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "C",
                    "relation_type": "contrasts",
                    "provenance": "machine",
                }
            ]
        },
    )
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        _receipt_identity(),
        workspace_wide_selection=True,
    ) is not None

    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "C",
                    "relation_type": "contrasts",
                    "provenance": "human_authored",
                }
            ]
        },
    )
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        _receipt_identity(),
        workspace_wide_selection=True,
    ) is None


def test_new_optional_semantic_input_invalidates_an_absent_marker(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    note, _ = _seed_replay_receipt(tmp_path)
    typed_links = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    typed_links.unlink(missing_ok=True)
    receipt = (
        tmp_path
        / "11_state"
        / "runs"
        / "receipt-replay"
        / "semantic_build_receipt.yml"
    )
    payload = api_module.read_yaml(receipt, {})
    payload["upstream_inputs"] = api_module._build_map_upstream_inventory(
        tmp_path,
        note_rows=[
            {
                "note_id": "note-source",
                "source_id": "source",
                "note_path": str(note.relative_to(tmp_path)),
            }
        ],
        source_set_argument=None,
        run_id="receipt-replay",
    )
    write_yaml(receipt, payload)

    write_yaml(
        typed_links,
        {
            "links": [
                {
                    "source_id": "A",
                    "target_source_id": "B",
                    "relation_type": "supports",
                    "provenance": "human_authored",
                }
            ]
        },
    )
    assert api_module._reusable_build_map_receipt(
        tmp_path,
        "receipt-replay",
        _receipt_identity(),
        workspace_wide_selection=True,
    ) is None


def test_empty_source_set_still_uses_workspace_membership_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_workspace(tmp_path)
    _seed_replay_receipt(tmp_path)
    receipt = (
        tmp_path
        / "11_state"
        / "runs"
        / "receipt-replay"
        / "semantic_build_receipt.yml"
    )
    payload = api_module.read_yaml(receipt, {})
    payload["identity"] = api_module._build_map_receipt_identity(
        provider="ollama",
        model="fake-1",
        question=None,
        policy=LiteratureMappingPolicy(),
        navigation=NavigationPolicy(),
        comparison_collection_keys=(),
        source_set={},
    )
    write_yaml(receipt, payload)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("empty selection must reuse the workspace receipt")

    monkeypatch.setattr(api_module, "all_workspace_note_rows", unexpected)
    result = build_map(
        tmp_path,
        run_id="receipt-replay",
        provider="ollama",
        model="fake-1",
        source_set={},
        resume=True,
    )

    assert result.status == "built"
