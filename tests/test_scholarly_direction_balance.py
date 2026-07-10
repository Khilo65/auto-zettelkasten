from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

from auto_zettelkasten.scholarly import SemanticScholarProvider


class _JsonResponse:
    status = 200

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def _opener_with_counts(
    counts: Mapping[str, int],
    calls: list[tuple[str, int, int]],
):
    def opener(request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        relation = "citations" if "/citations" in parsed.path else "references"
        query = dict(urllib.parse.parse_qsl(parsed.query))
        offset = int(query.get("offset", 0))
        limit = int(query["limit"])
        calls.append((relation, offset, limit))

        stop = min(offset + limit, counts[relation])
        key = "citingPaper" if relation == "citations" else "citedPaper"
        data = [
            {
                key: {
                    "paperId": f"{relation}-{index}",
                    "title": f"{relation} {index}",
                    "year": 2024,
                    "authors": [{"name": f"Author {index}"}],
                    "externalIds": {"DOI": f"10.9000/{relation}-{index}"},
                }
            }
            for index in range(offset, stop)
        ]
        next_offset = stop if stop < counts[relation] else None
        return _JsonResponse({"data": data, "next": next_offset})

    return opener


def test_citation_neighbors_tops_up_from_references_when_citations_are_empty() -> None:
    calls: list[tuple[str, int, int]] = []
    provider = SemanticScholarProvider(
        opener=_opener_with_counts({"citations": 0, "references": 8}, calls),
        page_size=2,
    )

    rows = provider.citation_neighbors({"provider_ids": {"semantic_scholar": "SEED"}}, limit=5)

    assert len(rows) == 5
    assert {row["relation_type"] for row in rows} == {"cites"}
    assert calls == [
        ("citations", 0, 2),
        ("references", 0, 2),
        ("references", 2, 2),
        ("references", 4, 1),
    ]


def test_citation_neighbors_tops_up_from_citations_when_references_are_empty() -> None:
    calls: list[tuple[str, int, int]] = []
    provider = SemanticScholarProvider(
        opener=_opener_with_counts({"citations": 8, "references": 0}, calls),
        page_size=2,
    )

    rows = provider.citation_neighbors({"provider_ids": {"semantic_scholar": "SEED"}}, limit=5)

    assert len(rows) == 5
    assert {row["relation_type"] for row in rows} == {"cited_by"}
    assert calls == [
        ("citations", 0, 2),
        ("citations", 2, 1),
        ("references", 0, 2),
        ("citations", 3, 2),
    ]
