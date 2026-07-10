from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Sequence

from . import ENGINE_VERSION
from .files import now_iso, sha256_bytes, sha256_text
from .identity import first_author, normalize_doi, normalize_title, work_metadata

GRAPH_BASE_URL = "https://api.semanticscholar.org/graph/v1"
RECOMMENDATIONS_URL = "https://api.semanticscholar.org/recommendations/v1/papers/"
CROSSREF_BASE_URL = "https://api.crossref.org"
PAPER_FIELDS = "paperId,title,year,authors,externalIds,url"


class ScholarlyProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class SemanticScholarProvider:
    """Identifier/metadata-only client for Semantic Scholar and Crossref."""

    name: str = "semantic-scholar"
    is_network: bool = True
    timeout: float = 20.0
    page_size: int = 100
    max_retries: int = 3
    opener: Callable[..., Any] = urllib.request.urlopen
    sleeper: Callable[[float], None] = time.sleep
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def resolve_work(self, work: Mapping[str, Any]) -> Mapping[str, Any] | None:
        metadata = work_metadata(work)
        identifier = self._semantic_identifier(metadata)
        result: Mapping[str, Any] | None = None
        if identifier:
            result = self._get_paper(identifier)
        elif metadata.get("title"):
            query_text = " ".join(
                value
                for value in (
                    str(metadata.get("title") or ""),
                    str((metadata.get("authors") or [""])[0]),
                    str(metadata.get("year") or ""),
                )
                if value
            )
            query = urllib.parse.urlencode({"query": query_text, "fields": PAPER_FIELDS})
            value = self._request_json(f"{GRAPH_BASE_URL}/paper/search/match?{query}")
            if isinstance(value, Mapping) and value.get("paperId") and _metadata_match(metadata, value):
                result = value
        if result:
            return work_metadata(result)
        if metadata.get("doi"):
            return self._crossref_doi(str(metadata["doi"]))
        return None

    def citation_neighbors(
        self,
        work: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        if limit < 1:
            return []
        routes = ("citations", "references")
        route_results: dict[str, list[dict[str, Any]]] = {route: [] for route in routes}
        route_cursors: dict[str, str | None] = {route: None for route in routes}
        route_done = {route: False for route in routes}

        def fill_route(route: str, target_count: int) -> None:
            route_rows = route_results[route]
            while len(route_rows) < target_count and not route_done[route]:
                cursor = route_cursors[route]
                page = self.citation_neighbors_page(
                    work,
                    relation=route,
                    cursor=cursor,
                    limit=min(self.page_size, target_count - len(route_rows)),
                )
                remaining = target_count - len(route_rows)
                page_rows = [dict(row) for row in page.get("rows", []) if isinstance(row, Mapping)]
                route_rows.extend(page_rows[:remaining])
                next_cursor = str(page.get("next_cursor")) if page.get("next_cursor") is not None else None
                route_cursors[route] = next_cursor
                route_done[route] = bool(page.get("done")) or next_cursor is None or next_cursor == cursor

        initial_targets = {
            "citations": (limit + 1) // 2,
            "references": limit // 2,
        }
        for route in routes:
            fill_route(route, initial_targets[route])

        while sum(len(rows) for rows in route_results.values()) < limit:
            before = sum(len(rows) for rows in route_results.values())
            for route in routes:
                if route_done[route]:
                    continue
                remaining = limit - sum(len(rows) for rows in route_results.values())
                fill_route(route, len(route_results[route]) + remaining)
                if sum(len(rows) for rows in route_results.values()) >= limit:
                    break
            if sum(len(rows) for rows in route_results.values()) == before:
                break

        rows: list[dict[str, Any]] = []
        for index in range(max((len(route_results[route]) for route in routes), default=0)):
            for route in routes:
                if index < len(route_results[route]):
                    rows.append(route_results[route][index])
        return rows[:limit]

    def citation_neighbors_page(
        self,
        work: Mapping[str, Any],
        *,
        relation: str,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any]:
        if relation not in {"citations", "references"}:
            raise ValueError("relation must be citations or references")
        metadata = work_metadata(work)
        paper_id = str((metadata.get("provider_ids") or {}).get("semantic_scholar") or "")
        if not paper_id:
            resolved = self.resolve_work(metadata)
            paper_id = str(((resolved or {}).get("provider_ids") or {}).get("semantic_scholar") or "")
        if not paper_id or limit < 1:
            return {"rows": [], "next_cursor": None, "done": True}
        try:
            offset = max(0, int(cursor or 0))
        except ValueError as exc:
            raise ValueError("citation cursor must be a non-negative integer") from exc
        page_limit = min(self.page_size, limit)
        query = urllib.parse.urlencode({"offset": offset, "limit": page_limit, "fields": PAPER_FIELDS})
        value = self._request_json(
            f"{GRAPH_BASE_URL}/paper/{urllib.parse.quote(paper_id, safe='')}/{relation}?{query}"
        )
        page = value.get("data", []) if isinstance(value, Mapping) else []
        key, relation_type = ("citingPaper", "cited_by") if relation == "citations" else ("citedPaper", "cites")
        rows = []
        for row in page if isinstance(page, list) else []:
            paper = row.get(key) if isinstance(row, Mapping) else None
            if isinstance(paper, Mapping) and paper.get("paperId"):
                rows.append({**work_metadata(paper), "relation_type": relation_type, "provider_relevance": 1.0})
        next_offset = value.get("next") if isinstance(value, Mapping) else None
        done = next_offset is None or len(page) < page_limit
        return {"rows": rows, "next_cursor": None if done else str(next_offset), "done": done}

    def recommendations(
        self,
        positive_paper_ids: Sequence[str],
        *,
        negative_paper_ids: Sequence[str] = (),
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        positive = sorted({str(value) for value in positive_paper_ids if value})
        if not positive or limit < 1:
            return []
        query = urllib.parse.urlencode({"limit": min(limit, 500), "fields": PAPER_FIELDS})
        body = json.dumps(
            {
                "positivePaperIds": positive,
                "negativePaperIds": sorted({str(value) for value in negative_paper_ids if value}),
            },
            sort_keys=True,
        ).encode("utf-8")
        value = self._request_json(f"{RECOMMENDATIONS_URL}?{query}", method="POST", body=body)
        papers = value.get("recommendedPapers", []) if isinstance(value, Mapping) else []
        if not isinstance(papers, list):
            return []
        return [
            {**work_metadata(row), "relation_type": "recommended_similar", "provider_relevance": 1.0}
            for row in papers[:limit]
            if isinstance(row, Mapping)
        ]

    def drain_attempts(self) -> Sequence[Mapping[str, Any]]:
        rows = list(self.attempts)
        self.attempts.clear()
        return rows

    def _semantic_identifier(self, metadata: Mapping[str, Any]) -> str:
        paper_id = str((metadata.get("provider_ids") or {}).get("semantic_scholar") or "")
        if paper_id:
            return paper_id
        if metadata.get("doi"):
            return f"DOI:{metadata['doi']}"
        return ""

    def _get_paper(self, identifier: str) -> Mapping[str, Any] | None:
        query = urllib.parse.urlencode({"fields": PAPER_FIELDS})
        value = self._request_json(
            f"{GRAPH_BASE_URL}/paper/{urllib.parse.quote(identifier, safe=':')}?{query}",
            allow_not_found=True,
        )
        return value if isinstance(value, Mapping) and value.get("paperId") else None

    def _crossref_doi(self, doi: str) -> Mapping[str, Any] | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        value = self._request_json(
            f"{CROSSREF_BASE_URL}/works/{urllib.parse.quote(normalized, safe='')}",
            allow_not_found=True,
        )
        message = value.get("message") if isinstance(value, Mapping) else None
        if not isinstance(message, Mapping):
            return None
        title_rows = message.get("title", [])
        authors = []
        for row in message.get("author", []) if isinstance(message.get("author"), list) else []:
            if isinstance(row, Mapping):
                authors.append(" ".join(part for part in (str(row.get("given") or ""), str(row.get("family") or "")) if part))
        year = ""
        issued = message.get("issued", {}) if isinstance(message.get("issued"), Mapping) else {}
        date_parts = issued.get("date-parts", []) if isinstance(issued, Mapping) else []
        if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            year = str(date_parts[0][0])
        return work_metadata(
            {
                "title": str(title_rows[0]) if isinstance(title_rows, list) and title_rows else "",
                "year": year,
                "authors": authors,
                "doi": normalized,
                "url": str(message.get("URL") or ""),
            }
        )

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        if not (url.startswith(f"{GRAPH_BASE_URL}/") or url.startswith(RECOMMENDATIONS_URL) or url.startswith(f"{CROSSREF_BASE_URL}/")):
            raise ValueError("scholarly provider URL must use a fixed official HTTPS endpoint")
        request_hash = sha256_text(method + "|" + url + "|" + sha256_bytes(body or b""))
        endpoint = urllib.parse.urlsplit(url).path
        for attempt_number in range(1, self.max_retries + 1):
            started_at = now_iso()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"auto-zettelkasten/{ENGINE_VERSION} (scholarly metadata client)",
            }
            api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY") or os.getenv("S2_API_KEY")
            if api_key and url.startswith("https://api.semanticscholar.org/"):
                headers["x-api-key"] = api_key
            request = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    payload = response.read()
                    status = int(getattr(response, "status", 200))
                try:
                    decoded = json.loads(payload.decode("utf-8") or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self.attempts.append(
                        {
                            "provider": self.name if url.startswith("https://api.semanticscholar.org/") else "crossref",
                            "endpoint": endpoint,
                            "method": method,
                            "request_hash": request_hash,
                            "response_hash": sha256_bytes(payload),
                            "status": "failed",
                            "reason": "invalid_json",
                            "http_status": status,
                            "attempt": attempt_number,
                            "started_at": started_at,
                            "completed_at": now_iso(),
                        }
                    )
                    raise ScholarlyProviderError("scholarly metadata provider returned invalid JSON") from exc
                self.attempts.append(
                    {
                        "provider": self.name if url.startswith("https://api.semanticscholar.org/") else "crossref",
                        "endpoint": endpoint,
                        "method": method,
                        "request_hash": request_hash,
                        "response_hash": sha256_bytes(payload),
                        "status": "succeeded",
                        "http_status": status,
                        "attempt": attempt_number,
                        "started_at": started_at,
                        "completed_at": now_iso(),
                    }
                )
                return decoded
            except urllib.error.HTTPError as exc:
                try:
                    payload = exc.read(1024)
                    headers_obj = exc.headers
                finally:
                    exc.close()
                status = int(exc.code)
                self.attempts.append(
                    {
                        "provider": self.name if url.startswith("https://api.semanticscholar.org/") else "crossref",
                        "endpoint": endpoint,
                        "method": method,
                        "request_hash": request_hash,
                        "response_hash": sha256_bytes(payload),
                        "status": "not_found" if status == 404 else "failed",
                        "http_status": status,
                        "attempt": attempt_number,
                        "started_at": started_at,
                        "completed_at": now_iso(),
                    }
                )
                if status == 404 and allow_not_found:
                    return None
                if status not in {429, 500, 502, 503, 504} or attempt_number >= self.max_retries:
                    raise ScholarlyProviderError(f"HTTP {status} from scholarly metadata provider") from exc
                self.sleeper(_retry_delay(headers_obj, attempt_number))
            except urllib.error.URLError as exc:
                self.attempts.append(
                    {
                        "provider": self.name if url.startswith("https://api.semanticscholar.org/") else "crossref",
                        "endpoint": endpoint,
                        "method": method,
                        "request_hash": request_hash,
                        "response_hash": "",
                        "status": "failed",
                        "reason": type(exc.reason).__name__,
                        "attempt": attempt_number,
                        "started_at": started_at,
                        "completed_at": now_iso(),
                    }
                )
                if attempt_number >= self.max_retries:
                    raise ScholarlyProviderError("cannot reach scholarly metadata provider") from exc
                self.sleeper(min(2 ** (attempt_number - 1), 8))
        raise ScholarlyProviderError("scholarly metadata request exhausted retries")


def _metadata_match(requested: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    left = work_metadata(requested)
    right = work_metadata(candidate)
    if normalize_title(left.get("title")) != normalize_title(right.get("title")):
        return False
    if left.get("year") and right.get("year") and str(left["year"]) != str(right["year"]):
        return False
    left_authors = left.get("authors") or []
    right_authors = right.get("authors") or []
    if left_authors and right_authors:
        left_name = first_author(left_authors)
        right_name = first_author(right_authors)
        if left_name and right_name and left_name != right_name:
            return False
    return True


def _retry_delay(headers: Mapping[str, Any], attempt_number: int) -> float:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is not None:
        try:
            return min(max(float(value), 0.0), 30.0)
        except (TypeError, ValueError):
            try:
                delay = (parsedate_to_datetime(str(value)).timestamp() - time.time())
                return min(max(delay, 0.0), 30.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return float(min(2 ** (attempt_number - 1), 8))
