from __future__ import annotations

from auto_zettelkasten.literature import (
    _CheckpointedReasonerCalls,
    _schedule_cluster_writers,
    build_coverage_register,
)
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import EvidenceProfile, MapRequest
from auto_zettelkasten.pipeline import (
    _RELATIONSHIP_BATCH_MAX_JOBS,
    _RunProgress,
    _canonical_workspace_graph_inputs,
    _pack_relationship_rows,
    _write_profile_packets,
)
from auto_zettelkasten.literature import LITERATURE_FAMILY_PLAN_PROMPT_VERSION
from auto_zettelkasten.readers import (
    LITERATURE_FAMILY_PLAN_MAX_OUTPUT_TOKENS,
    _relationship_adjudication_system_prompt,
)
from auto_zettelkasten.relationships import (
    RELATIONSHIP_DISCOVERY_PROMPT_VERSION,
    RELATIONSHIP_PROMPT_VERSION,
)


def test_v29_4_relationship_packet_and_family_plan_limits() -> None:
    prompt = _relationship_adjudication_system_prompt()
    assert RELATIONSHIP_PROMPT_VERSION == "18"
    assert RELATIONSHIP_DISCOVERY_PROMPT_VERSION == "16"
    assert "relationship prompt v18" in prompt
    assert "choose the tier before the subtype" in prompt
    assert "Use contextual_connection" in prompt
    assert "every ID appears exactly once" in prompt
    assert _RELATIONSHIP_BATCH_MAX_JOBS == 8
    assert LITERATURE_FAMILY_PLAN_MAX_OUTPUT_TOKENS == 128_000
    assert LITERATURE_FAMILY_PLAN_PROMPT_VERSION == "10"

    rows = list(range(31))
    packets = _pack_relationship_rows(
        rows,
        pair_for=lambda value: (f"left-{value}", f"right-{value}"),
        profile_by_source={
            source_id: {"source_id": source_id}
            for value in rows
            for source_id in (f"left-{value}", f"right-{value}")
        },
        context_for=lambda packet: {"jobs": list(packet)},
        max_chars=1_000_000,
        max_rows=_RELATIONSHIP_BATCH_MAX_JOBS,
    )
    assert [len(packet) for packet in packets] == [8, 8, 8, 7]


def test_cluster_scheduler_uses_all_explicitly_available_calls() -> None:
    calls = object.__new__(_CheckpointedReasonerCalls)
    calls.max_calls = 32
    calls.cumulative_provider_calls = 10
    runnable = [{"cluster_id": f"cluster-{index}"} for index in range(22)]

    scheduled, completion_pending = _schedule_cluster_writers(calls, runnable)

    assert len(scheduled) == 22
    assert completion_pending == []


def test_v25_duplicate_document_hash_yields_one_canonical_work(tmp_path) -> None:
    notes = [
        {
            "source_id": "canonical",
            "note_id": "note-canonical",
            "zotero_item_key": "AAAA1111",
            "title": "The complete report",
            "item_type": "report",
        },
        {
            "source_id": "alias",
            "note_id": "note-alias",
            "zotero_item_key": "BBBB2222",
            "title": "download.pdf",
            "item_type": "attachment",
        },
    ]
    profiles = [
        {"source_id": "canonical", "note_id": "note-canonical", "source_hash": "same"},
        {"source_id": "alias", "note_id": "note-alias", "source_hash": "same"},
    ]

    full_notes, _, canonical_notes, _, identity, relations = (
        _canonical_workspace_graph_inputs(tmp_path, notes, profiles, None)
    )

    assert len(full_notes) == 2
    assert len(canonical_notes) == 1
    assert identity["duplicate_alias_count"] == 1
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "alias_of"


def test_v25_coverage_prefers_profile_state_and_counts_aliases() -> None:
    coverage = build_coverage_register(
        [
            {"source_id": "analytical", "note_id": "note-a", "analytical": True},
            {"source_id": "limited", "note_id": "note-l", "analytical": False},
            {"source_id": "alias", "note_id": "note-x", "analytical": True},
        ],
        source_set={
            "rows": [
                {"source_id": "analytical", "note_id": "note-a", "terminal_status": "limited_note"},
                {"source_id": "limited", "note_id": "note-l", "terminal_status": "validated_note"},
                {"source_id": "alias", "note_id": "note-x", "terminal_status": "duplicate_alias"},
            ]
        },
    )

    assert coverage["counts"] == {
        "validated_note": 1,
        "limited_note": 1,
        "duplicate_alias": 1,
        "parked_for_review": 0,
        "partial": 0,
        "pending": 0,
    }
    assert all(row["attempted_route"] == ["not_recorded_legacy"] for row in coverage["records"])


def test_v25_progress_reconciles_canonical_terminal_statuses(tmp_path) -> None:
    progress = _RunProgress(
        tmp_path / "progress.yml",
        "run",
        [
            {"key": "AAAA1111", "terminal_status": "validated_note"},
            {"key": "BBBB2222", "terminal_status": "validated_note"},
        ],
        resume=False,
    )

    progress.reconcile_terminal_statuses(
        [
            {"zotero_key": "AAAA1111", "terminal_state": "validated_note"},
            {"zotero_key": "BBBB2222", "terminal_state": "duplicate_alias"},
        ]
    )

    payload = read_yaml(tmp_path / "progress.yml")
    assert payload["validated_note_count"] == 1
    assert payload["duplicate_alias_count"] == 1
    assert payload["terminal_count"] == 2


def test_v25_canonical_profile_packets_remove_stale_tail(tmp_path) -> None:
    packet_root = tmp_path / "literature" / "packets"
    packet_root.mkdir(parents=True)
    write_yaml(packet_root / "packet-0002.yml", {"profiles": [{"source_id": "alias"}]})

    result = _write_profile_packets(
        tmp_path / "literature",
        [EvidenceProfile(source_id="canonical", note_id="note-canonical")],
        source_set={"source_set_id": "canonical", "dependency_hash": "hash"},
        request=MapRequest(workspace=tmp_path),
        reasoner=None,
        progress=None,
    )

    assert result["packet_count"] == 1
    assert (packet_root / "packet-0001.yml").is_file()
    assert not (packet_root / "packet-0002.yml").exists()
