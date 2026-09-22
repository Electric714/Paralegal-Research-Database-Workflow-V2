from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import database as db
from .research.models import SourceResult, SourceResultStatus
from .research.service import list_tasks, persist_source_result
from .research.sources.base import ContractorContext
from .research.sources.wcca import PUBLIC_WCCA_URL, build_operator_result, build_search_plan

router = APIRouter(prefix="/api/sources/wcca", tags=["wcca"])


class WccaCaseInput(BaseModel):
    case_number: str = ""
    county: str = ""
    matched_party_name: str = ""
    case_type: str = ""
    case_status: str = ""
    filing_date: str = ""
    disposition: str = ""
    case_url: str = ""
    note: str = ""


class WccaOperatorResultRequest(BaseModel):
    bidder_id: int
    searched_names: list[str]
    outcome: str
    cases: list[WccaCaseInput] = Field(default_factory=list)
    operator_note: str | None = None
    operator_confirmed_complete: bool = False
    identity_confirmed: bool = False


def _context(bidder_id: int) -> ContractorContext:
    bidder = db.get_bidder(bidder_id)
    if not bidder or not bidder.get("_active"):
        raise HTTPException(404, "Active bidder not found.")
    return ContractorContext(
        internal_id=bidder_id,
        external_id=str(bidder.get("id", "")),
        contractor_name=str(bidder.get("contractor_name", "")),
        related_companies=str(bidder.get("related_companies", "")),
        address_1=str(bidder.get("address_1", "")),
        city=str(bidder.get("city", "")),
        state=str(bidder.get("state", "")),
        zip=str(bidder.get("zip", "")),
        additional_address=str(bidder.get("additional_address", "")),
        additional_address_city=str(bidder.get("additional_address_city", "")),
        additional_address_state=str(bidder.get("additional_address_state", "")),
        additional_address_zip=str(bidder.get("additional_address_zip", "")),
        dfi=str(bidder.get("dfi", "")),
    )


def _plan_for_bidder(bidder_id: int) -> dict[str, Any]:
    bidder = db.get_bidder(bidder_id)
    if not bidder or not bidder.get("_active"):
        raise HTTPException(404, "Active bidder not found.")
    plan = build_search_plan(_context(bidder_id))
    plan["master_values"] = {
        "circuit_court": str(bidder.get("circuit_court", "")),
        "ccap_show150": str(bidder.get("ccap_show150", "")),
    }
    return plan


def _parse_scope(value: str | None) -> list[int]:
    if value is None or not value.strip():
        return db.active_bidder_ids()
    try:
        bidder_ids = list(dict.fromkeys(int(part.strip()) for part in value.split(",") if part.strip()))
    except ValueError as exc:
        raise HTTPException(422, "bidder_ids must be a comma-separated list of bidder IDs.") from exc
    if not bidder_ids:
        return []
    for bidder_id in bidder_ids:
        bidder = db.get_bidder(bidder_id)
        if not bidder or not bidder.get("_active"):
            raise HTTPException(422, "One or more selected bidders do not exist in the active master database.")
    return bidder_ids


def _task_for_submission(bidder_id: int) -> dict[str, Any]:
    reusable = {
        SourceResultStatus.NOT_CHECKED.value,
        SourceResultStatus.MANUAL_REVIEW_REQUIRED.value,
        SourceResultStatus.PARTIAL_RESULTS.value,
        SourceResultStatus.BLOCKED.value,
        SourceResultStatus.AMBIGUOUS_MATCH.value,
    }
    with db.connect() as conn:
        placeholders = ",".join("?" for _ in reusable)
        row = conn.execute(
            f"""
            SELECT * FROM research_tasks
            WHERE bidder_id=? AND source_key='wcca' AND status IN ({placeholders})
            ORDER BY id DESC LIMIT 1
            """,
            (bidder_id, *sorted(reusable)),
        ).fetchone()
        if row:
            return dict(row)

    run = db.create_run([bidder_id], ["wcca"], 1)
    tasks = list_tasks(run["id"])
    if not tasks:
        raise HTTPException(500, "Unable to create a WCCA research task.")
    return tasks[0]


def _refresh_run(run_id: int) -> None:
    tasks = list_tasks(run_id)
    counts = Counter(str(task["status"]) for task in tasks)
    completed_statuses = {
        SourceResultStatus.SUCCESS_COMPLETE.value,
        SourceResultStatus.SUCCESS_NO_MATCH.value,
        SourceResultStatus.SUCCESS_WITH_FINDINGS.value,
    }
    completed = bool(tasks) and all(str(task["status"]) in completed_statuses for task in tasks)
    status = "completed" if completed else "partial"
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    with db.connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status=?, message=?, completed_at=? WHERE id=?",
            (
                status,
                f"WCCA operator results recorded. {summary}",
                db.utcnow(),
                run_id,
            ),
        )


def _comparison(result: SourceResult, bidder: dict[str, Any], *, proposal_created: bool) -> dict[str, Any]:
    observed = next(
        (item.observed_value for item in result.evidence if item.field_name == "circuit_court"),
        None,
    )
    current = str(bidder.get("circuit_court", "")).strip()
    current_ccap = str(bidder.get("ccap_show150", "")).strip()

    if observed == "Y":
        different = current != "Y"
        comparison_status = "confirmed_positive_differs" if different else "confirmed_positive_agrees"
        reason = (
            "A confirmed WCCA case supports positive circuit_court=Y comparison evidence. "
            "Automatic field writes remain disabled until the firm's legacy field rule is confirmed."
        )
    elif result.status == SourceResultStatus.SUCCESS_NO_MATCH:
        different = False
        comparison_status = "public_no_match_not_master_negative"
        reason = (
            "No currently displayed WCCA match was found, but WCCA is not the complete court record and has access/display limitations. "
            "This result does not establish circuit_court=N and cannot contradict or erase an existing Y."
        )
    elif result.status == SourceResultStatus.BLOCKED:
        different = False
        comparison_status = "blocked_no_field_observation"
        reason = "WCCA could not be completed; no circuit_court comparison is made."
    elif result.status == SourceResultStatus.AMBIGUOUS_MATCH:
        different = False
        comparison_status = "ambiguous_no_field_observation"
        reason = "A possible WCCA case was recorded but contractor identity was not confirmed; no circuit_court comparison is made."
    else:
        different = False
        comparison_status = "incomplete_no_field_observation"
        reason = "The WCCA search is incomplete or lacks sufficient case identifiers; no negative circuit_court conclusion is made."

    return {
        "field_name": "circuit_court",
        "current_value": current,
        "observed_value": observed,
        "different": different,
        "comparison_status": comparison_status,
        "proposal_created": proposal_created,
        "write_enabled": False,
        "reason": reason,
        "ccap_show150": {
            "current_value": current_ccap,
            "observed_value": None,
            "write_enabled": False,
            "status": "undefined_legacy_semantics",
            "reason": "No authoritative definition of the firm's ccap_show150 field has been established.",
        },
    }


@router.get("/status")
def wcca_status():
    return {
        "item": {
            "source_key": "wcca",
            "implemented": True,
            "mode": "operator_assisted",
            "public_url": PUBLIC_WCCA_URL,
            "workbench_url": "/wcca-workbench.html",
            "automatic_public_scraping": False,
            "field_write_enabled": False,
            "positive_comparison_enabled": True,
            "negative_field_updates": False,
            "field_write_reason": "Confirm circuit_court and ccap_show150 legacy semantics with the firm first.",
            "no_match_rule": "A complete public WCCA no-match is source-level evidence only and never becomes circuit_court=N.",
        }
    }


@router.get("/plans")
def wcca_plans(bidder_ids: str | None = Query(default=None)):
    ids = _parse_scope(bidder_ids)
    return {"items": [_plan_for_bidder(bidder_id) for bidder_id in ids]}


@router.post("/result")
def wcca_result(payload: WccaOperatorResultRequest):
    contractor = _context(payload.bidder_id)
    try:
        result = build_operator_result(
            contractor,
            searched_names=payload.searched_names,
            outcome=payload.outcome,
            cases=[item.model_dump() for item in payload.cases],
            operator_note=payload.operator_note,
            operator_confirmed_complete=payload.operator_confirmed_complete,
            identity_confirmed=payload.identity_confirmed,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    task = _task_for_submission(payload.bidder_id)
    persisted = persist_source_result(int(task["id"]), result)
    run_id = int(task["research_run_id"])
    _refresh_run(run_id)

    bidder = db.get_bidder(payload.bidder_id) or {}
    comparison = _comparison(result, bidder, proposal_created=bool(persisted["proposal_ids"]))
    db.add_diagnostic(
        "INFO" if result.status in {SourceResultStatus.SUCCESS_NO_MATCH, SourceResultStatus.SUCCESS_WITH_FINDINGS} else "WARNING",
        "WCCA operator result recorded",
        source_key="wcca",
        bidder_name=contractor.contractor_name,
        stage="research",
        details={
            "run_id": run_id,
            "task_id": int(task["id"]),
            "snapshot_id": persisted["snapshot_id"],
            "result_status": result.status.value,
            "completeness_status": result.completeness_status.value,
            "identity_status": result.identity_status.value,
            "comparison": comparison,
        },
    )
    return {
        "item": {
            "run_id": run_id,
            "task_id": int(task["id"]),
            "snapshot_id": persisted["snapshot_id"],
            "status": result.status.value,
            "identity_status": result.identity_status.value,
            "completeness_status": result.completeness_status.value,
            "warnings": result.warnings,
            "comparison": comparison,
        }
    }
