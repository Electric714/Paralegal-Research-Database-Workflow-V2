from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz


CORPORATE_SUFFIXES = {
    "llc", "l l c", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "lp", "llp", "pllc",
}


def normalize_text(value: str | None) -> str:
    text = (value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_company_name(value: str | None) -> str:
    tokens = normalize_text(value).split()
    while tokens and " ".join(tokens[-3:]) in CORPORATE_SUFFIXES:
        tokens = tokens[:-3]
    while tokens and " ".join(tokens[-2:]) in CORPORATE_SUFFIXES:
        tokens = tokens[:-2]
    while tokens and tokens[-1] in CORPORATE_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def normalize_address(value: str | None) -> str:
    text = normalize_text(value)
    replacements = {
        "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd",
        "drive": "dr", "lane": "ln", "suite": "ste", "highway": "hwy",
    }
    return " ".join(replacements.get(token, token) for token in text.split())


@dataclass(frozen=True)
class MatchScore:
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    status: str


def score_candidate(
    *,
    master_name: str,
    candidate_name: str,
    master_address: str = "",
    candidate_address: str = "",
    master_city: str = "",
    candidate_city: str = "",
    master_state: str = "",
    candidate_state: str = "",
) -> MatchScore:
    name_score = fuzz.WRatio(normalize_company_name(master_name), normalize_company_name(candidate_name)) / 100.0
    address_score = 0.0
    if master_address and candidate_address:
        address_score = fuzz.WRatio(normalize_address(master_address), normalize_address(candidate_address)) / 100.0
    city_score = 1.0 if normalize_text(master_city) and normalize_text(master_city) == normalize_text(candidate_city) else 0.0
    state_score = 1.0 if normalize_text(master_state) and normalize_text(master_state) == normalize_text(candidate_state) else 0.0

    weights = [(name_score, 0.65)]
    if master_address and candidate_address:
        weights.append((address_score, 0.20))
    if master_city and candidate_city:
        weights.append((city_score, 0.10))
    if master_state and candidate_state:
        weights.append((state_score, 0.05))
    total_weight = sum(weight for _, weight in weights)
    score = sum(value * weight for value, weight in weights) / total_weight

    if score >= 0.92 and name_score >= 0.90:
        status = "HIGH"
    elif score >= 0.78 and name_score >= 0.75:
        status = "MEDIUM"
    else:
        status = "LOW"

    return MatchScore(
        score=round(score, 4),
        name_score=round(name_score, 4),
        address_score=round(address_score, 4),
        city_score=city_score,
        state_score=state_score,
        status=status,
    )
