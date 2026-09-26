from __future__ import annotations

from itertools import combinations
import sqlite3
import time
from typing import Any

from rapidfuzz import fuzz

from ..matching import normalize_company_name
from .osha import DETAIL_URL, OshaSearchRow
from .osha_bulk import MAX_CANDIDATES_PER_QUERY
from .osha_operational import OperationalOshaEstablishmentSource


CONNECTOR_TOKENS = {"and", "the", "of", "for", "at", "in", "on"}
MAX_PAIR_FALLBACKS = 6


def bounded_fts_queries(normalized_name: str) -> list[str]:
    """Build high-signal FTS queries without falling back to generic single words.

    Exact normalized-name lookup is handled separately. For multi-token names we first
    require all meaningful tokens, then at most a few two-token combinations. This
    preserves typo/name-variation discovery without turning names such as "American
    Contractors and Associates" into a 4,000-row search for the word "contractors".
    """
    tokens = [token for token in normalized_name.split() if token]
    meaningful = [token for token in tokens if token not in CONNECTOR_TOKENS]
    if not meaningful:
        meaningful = tokens
    if not meaningful:
        return []

    queries: list[str] = []
    if len(meaningful) == 1:
        return [f'"{meaningful[0]}"']

    strict = " AND ".join(f'"{token}"' for token in meaningful)
    queries.append(strict)

    # Prefer longer/more distinctive words when deciding which bounded pair fallbacks
    # to try. Numeric identifiers remain useful and are retained.
    ranked = sorted(
        dict.fromkeys(meaningful),
        key=lambda token: (len(token), any(ch.isdigit() for ch in token), token),
        reverse=True,
    )[:4]
    pair_queries = [
        " AND ".join(f'"{token}"' for token in pair)
        for pair in combinations(ranked, 2)
    ]
    queries.extend(pair_queries[:MAX_PAIR_FALLBACKS])
    return list(dict.fromkeys(queries))


class IndexedOshaEstablishmentSource(OperationalOshaEstablishmentSource):
    """Production OSHA source with bounded, instrumented local index lookups."""

    adapter_version = "2.3.0"
    parser_version = "2.0.0"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._query_diagnostics: dict[tuple[str, str], dict[str, Any]] = {}

    def prepare(self) -> None:
        self._query_diagnostics.clear()
        super().prepare()

    @staticmethod
    def _detail_from_row(row: dict[str, Any]) -> dict[str, str]:
        return {
            "case_status": "",
            "site_address": " | ".join(
                part
                for part in (
                    str(row.get("site_address") or ""),
                    str(row.get("site_city") or ""),
                    str(row.get("site_state") or ""),
                    str(row.get("site_zip") or ""),
                )
                if part
            ),
            "site_street": str(row.get("site_address") or ""),
            "site_city": str(row.get("site_city") or ""),
            "site_state": str(row.get("site_state") or ""),
            "site_zip": str(row.get("site_zip") or ""),
            "mailing_address": " | ".join(
                part
                for part in (
                    str(row.get("mail_street") or ""),
                    str(row.get("mail_city") or ""),
                    str(row.get("mail_state") or ""),
                    str(row.get("mail_zip") or ""),
                )
                if part
            ),
            "mailing_street": str(row.get("mail_street") or ""),
            "mailing_city": str(row.get("mail_city") or ""),
            "mailing_state": str(row.get("mail_state") or ""),
            "mailing_zip": str(row.get("mail_zip") or ""),
        }

    @staticmethod
    def _search_row(row: dict[str, Any]) -> OshaSearchRow:
        activity = str(row.get("activity_nr") or "")
        return OshaSearchRow(
            activity_number=activity,
            date_opened=str(row.get("open_date") or ""),
            reporting_id=str(row.get("reporting_id") or ""),
            state=str(row.get("site_state") or "").upper(),
            inspection_type=str(row.get("inspection_type") or ""),
            scope=str(row.get("scope") or ""),
            sic=str(row.get("sic") or ""),
            naics=str(row.get("naics") or ""),
            violations=str(row.get("violations") or ""),
            establishment_name=str(row.get("establishment_name") or ""),
            detail_url=f"{DETAIL_URL}?id={activity}",
        )

    def _query_candidates(self, search_name: str, state: str) -> tuple[list[OshaSearchRow], bool]:
        normalized = normalize_company_name(search_name)
        normalized_state = state.upper()
        cache_key = (normalized, normalized_state)
        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return cached
        if not normalized:
            result = ([], True)
            self._candidate_cache[cache_key] = result
            return result

        started = time.monotonic()
        collected: dict[str, dict[str, Any]] = {}
        complete = True
        queries_executed: list[str] = []
        exact_fast_path = False

        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row

            # Fast path: the B-tree normalized-name index answers exact bidder names in
            # milliseconds even on the multi-million-row national dataset.
            exact_sql = "SELECT * FROM inspections WHERE normalized_name = ?"
            exact_params: list[Any] = [normalized]
            if normalized_state and normalized_state.casefold() != "all":
                exact_sql += " AND site_state = ?"
                exact_params.append(normalized_state)
            exact_sql += " LIMIT ?"
            exact_params.append(MAX_CANDIDATES_PER_QUERY + 1)
            exact_rows = conn.execute(exact_sql, exact_params).fetchall()
            queries_executed.append("normalized_name_exact")
            if exact_rows:
                exact_fast_path = True
                if len(exact_rows) > MAX_CANDIDATES_PER_QUERY:
                    complete = False
                    exact_rows = exact_rows[:MAX_CANDIDATES_PER_QUERY]
                for row in exact_rows:
                    data = dict(row)
                    collected[str(data.get("activity_nr") or "")] = data
            else:
                plans = bounded_fts_queries(normalized)
                for query_index, match_query in enumerate(plans):
                    sql = """
                        SELECT i.*
                        FROM inspection_names
                        JOIN inspections AS i ON i.rowid = inspection_names.rowid
                        WHERE inspection_names MATCH ?
                    """
                    params: list[Any] = [match_query]
                    if normalized_state and normalized_state.casefold() != "all":
                        sql += " AND i.site_state = ?"
                        params.append(normalized_state)
                    sql += " LIMIT ?"
                    params.append(MAX_CANDIDATES_PER_QUERY + 1)
                    rows = conn.execute(sql, params).fetchall()
                    queries_executed.append(match_query)
                    if len(rows) > MAX_CANDIDATES_PER_QUERY:
                        complete = False
                        rows = rows[:MAX_CANDIDATES_PER_QUERY]
                    for row in rows:
                        data = dict(row)
                        collected[str(data.get("activity_nr") or "")] = data

                    # The strict all-token query is already stronger than any fuzzy
                    # fallback. When it returns rows, do not fan out into pair searches.
                    if query_index == 0 and rows:
                        break

        rows_out: list[OshaSearchRow] = []
        for row in collected.values():
            candidate_name = str(row.get("establishment_name") or "")
            candidate_normalized = str(row.get("normalized_name") or "")
            name_score = fuzz.WRatio(normalized, candidate_normalized) / 100.0
            containment = normalized in candidate_normalized or candidate_normalized in normalized
            if not containment and name_score < 0.60:
                continue
            search_row = self._search_row(row)
            rows_out.append(search_row)
            self._detail_cache[search_row.activity_number] = self._detail_from_row(row)

        elapsed = time.monotonic() - started
        self._query_diagnostics[cache_key] = {
            "searched_name": search_name,
            "normalized_name": normalized,
            "state": normalized_state,
            "exact_fast_path": exact_fast_path,
            "queries_executed": queries_executed,
            "raw_candidate_count": len(collected),
            "plausible_candidate_count": len(rows_out),
            "complete": complete,
            "elapsed_seconds": round(elapsed, 4),
        }
        result = (rows_out, complete)
        self._candidate_cache[cache_key] = result
        return result

    def search(self, contractor: ContractorContext):
        result = super().search(contractor)
        relevant_names = {
            normalize_company_name(contractor.contractor_name),
            *(
                normalize_company_name(part.strip())
                for part in (contractor.related_companies or "").replace("|", ";").split(";")
                if part.strip()
            ),
        }
        metrics = [
            value
            for (name, _state), value in self._query_diagnostics.items()
            if name in relevant_names
        ]
        if metrics:
            result.normalized_payload["local_index_query_metrics"] = metrics
        return self.validate_result(result)
