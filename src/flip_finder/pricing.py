from __future__ import annotations

import hashlib
import re
import statistics
from collections.abc import Iterable
from typing import Any

from .config import DATA_DIR, Settings, load_json_file
from .models import Listing


CATALOG = load_json_file(DATA_DIR / "catalog_terms.json")
STOPWORDS = set(CATALOG.get("stopwords", [])) | {
    "in", "for", "sale", "used", "new", "the", "and", "with",
    "monitor", "keyboard", "mouse", "gaming", "bahrain", "price", "offer",
}
CATEGORY_KEYWORDS = {
    name: set(values)
    for name, values in (CATALOG.get("categories", {}) or {}).items()
}


def normalize_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^\w\u0600-\u06ff]+", " ", text.lower())
    return {
        token for token in normalized.split()
        if len(token) >= 3 and token not in STOPWORDS
    }


def category_for(title: str) -> str:
    lowered = title.lower()
    printer = CATEGORY_KEYWORDS.get("printer", set())
    television = CATEGORY_KEYWORDS.get("television", set())
    soundbar = CATEGORY_KEYWORDS.get("soundbar", set())
    if any(term in lowered for term in printer):
        return "printer"
    has_television = any(term in lowered for term in television)
    has_soundbar = any(term in lowered for term in soundbar)
    if has_television and has_soundbar:
        return "bundle"
    if has_television:
        return "television"
    if has_soundbar:
        return "soundbar"
    scores = {
        category: sum(1 for keyword in keywords if keyword in lowered)
        for category, keywords in CATEGORY_KEYWORDS.items()
    }
    if not scores:
        return "other"
    best = max(scores, key=scores.get)
    return best if scores[best] else "other"


def listing_signature(listing: Listing) -> str:
    normalized_title = " ".join(sorted(normalize_tokens(listing.title)))
    raw = f"{listing.source}|{normalized_title}|{listing.price_bhd}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"{listing.source}:signature:{digest}"


def product_identifier_tokens(text: str) -> set[str]:
    return {
        token for token in normalize_tokens(text)
        if len(token) >= 4 and any(character.isdigit() for character in token)
    }


def comparable_match_score(candidate: Listing, item: Listing) -> tuple[int, str]:
    candidate_tokens = normalize_tokens(candidate.title)
    item_tokens = normalize_tokens(item.title)
    identifier_overlap = (
        product_identifier_tokens(candidate.title)
        & product_identifier_tokens(item.title)
    )
    overlap = len(candidate_tokens & item_tokens)
    same_category = category_for(candidate.title) == category_for(item.title)
    if identifier_overlap and same_category:
        return 100 + len(identifier_overlap) * 20 + overlap, "exact_model"
    if not same_category:
        return 0, "weak"
    if overlap >= 2:
        return 50 + overlap, "same_category"
    return 0, "weak"


def comparables_for(candidate: Listing, listings: Iterable[Listing]) -> list[Listing]:
    scored: list[tuple[int, str, Listing]] = []
    for item in listings:
        if item.listing_id == candidate.listing_id or item.price_bhd is None:
            continue
        score, match_type = comparable_match_score(candidate, item)
        if score:
            scored.append((score, match_type, item))
    scored.sort(key=lambda row: row[0], reverse=True)
    exact = [item for _, match_type, item in scored if match_type == "exact_model"]
    selected = exact if len(exact) >= 2 else [item for _, _, item in scored]
    return selected[:30]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def filter_price_outliers(prices: Iterable[float]) -> list[float]:
    ordered = sorted(float(price) for price in prices)
    if len(ordered) < 4:
        return ordered
    q1 = percentile(ordered, 0.25)
    q3 = percentile(ordered, 0.75)
    iqr = q3 - q1
    if iqr == 0:
        return ordered
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inliers = [price for price in ordered if lower <= price <= upper]
    return inliers if len(inliers) >= 2 else ordered


def calculate_profit(expected_sale_price: float, total_cost: float) -> tuple[float, float | None]:
    profit = round(expected_sale_price - total_cost, 2)
    roi = round(profit / total_cost * 100, 1) if total_cost else None
    return profit, roi


def recommended_max_buy_price(expected_sale_price: float, settings: Settings) -> float:
    return round(
        expected_sale_price
        - settings.transport_bhd
        - settings.repair_reserve_bhd
        - settings.selling_fee_bhd
        - settings.target_profit_bhd,
        2,
    )


def rule_based_analysis(
    candidate: Listing,
    comparables: list[Listing],
    settings: Settings,
) -> dict[str, Any]:
    prices = [float(item.price_bhd) for item in comparables if item.price_bhd is not None]
    total_cost = round(
        (candidate.price_bhd or 0)
        + settings.transport_bhd
        + settings.repair_reserve_bhd
        + settings.selling_fee_bhd,
        2,
    )
    missing: list[str] = []
    if not candidate.condition:
        missing.append("Product condition")
    if not candidate.description:
        missing.append("Detailed defects and specifications")
    if not candidate.image_urls:
        missing.append("Clear product photos")

    if not prices:
        return {
            "buy_decision": "manual_check",
            "category": category_for(candidate.title),
            "estimated_market_price_bhd": None,
            "expected_sale_price_bhd": None,
            "total_cost_bhd": total_cost,
            "net_profit_bhd": None,
            "roi_percent": None,
            "recommended_max_buy_price_bhd": None,
            "comparables_count": 0,
            "exact_model_comparables": 0,
            "inlier_comparables": 0,
            "confidence": 0.20,
            "missing_information": missing + ["At least three comparable prices"],
            "risks": ["Insufficient comparable-price evidence"],
            "reason": "Insufficient evidence to estimate a resale price.",
        }

    match_types = [comparable_match_score(candidate, item)[1] for item in comparables]
    exact_count = match_types.count("exact_model")
    inliers = filter_price_outliers(prices)
    market_price = statistics.median(inliers)
    expected_sale = round(market_price * settings.sale_realization_rate, 2)
    profit, roi = calculate_profit(expected_sale, total_cost)
    confidence = min(
        0.95,
        0.30 + min(len(inliers), 10) * 0.045 + min(exact_count, 5) * 0.08,
    )
    if len(prices) < settings.minimum_comparables:
        confidence = min(confidence, 0.45)
        missing.append(
            f"More comparables required; found {len(prices)}, need {settings.minimum_comparables}"
        )
    risks: list[str] = []
    if category_for(candidate.title) in {"monitor", "laptop", "gpu"}:
        risks.append("Check ports, heat, hidden faults, and dead pixels before payment")
    if expected_sale < (candidate.price_bhd or 0):
        risks.append("Asking price is high compared with current listings")
    decision = "buy_candidate" if all((
        confidence >= settings.minimum_confidence,
        roi is not None and roi >= settings.minimum_roi_percent,
        profit >= settings.target_profit_bhd,
    )) else "manual_check"

    return {
        "buy_decision": decision,
        "category": category_for(candidate.title),
        "estimated_market_price_bhd": round(market_price, 2),
        "expected_sale_price_bhd": expected_sale,
        "total_cost_bhd": total_cost,
        "net_profit_bhd": profit,
        "roi_percent": roi,
        "recommended_max_buy_price_bhd": recommended_max_buy_price(
            expected_sale, settings
        ),
        "comparables_count": len(prices),
        "exact_model_comparables": exact_count,
        "inlier_comparables": len(inliers),
        "price_range_bhd": [round(min(prices), 2), round(max(prices), 2)],
        "confidence": round(confidence, 2),
        "missing_information": missing,
        "risks": risks or ["Verify condition, ownership, and invoice before payment"],
        "reason": (
            f"Median of {len(inliers)} inlier asking prices with a "
            f"{round((1 - settings.sale_realization_rate) * 100)}% sale-price adjustment."
        ),
    }


def evaluate_opportunity(
    rule: dict[str, Any],
    ai: dict[str, Any] | None,
    settings: Settings,
    ai_requested: bool,
) -> dict[str, Any]:
    ai = ai or {}
    ai_ok = ai.get("status") == "ok"
    sale_candidates = [
        float(value)
        for value in (rule.get("expected_sale_price_bhd"), ai.get("expected_sale_price_bhd"))
        if isinstance(value, (int, float)) and value > 0
    ]
    sale_price = round(min(sale_candidates), 2) if sale_candidates else None
    total_cost = rule.get("total_cost_bhd")
    profit, roi = (None, None)
    if isinstance(sale_price, (int, float)) and isinstance(total_cost, (int, float)):
        profit, roi = calculate_profit(sale_price, total_cost)
    confidence = min(
        float(rule.get("confidence") or 0),
        float(ai.get("confidence") or 1) if ai_ok else 1,
    )
    has_comparables = int(rule.get("comparables_count") or 0) >= settings.minimum_comparables
    decision = ai.get("buy_decision") if ai_ok else rule.get("buy_decision")
    ai_requirement_met = not (
        settings.require_ai_for_telegram and ai_requested and not ai_ok
    )
    eligible = bool(
        profit is not None
        and roi is not None
        and profit >= settings.target_profit_bhd
        and roi >= settings.minimum_roi_percent
        and confidence >= settings.minimum_confidence
        and has_comparables
        and decision == "buy_candidate"
        and ai_requirement_met
    )
    if eligible:
        reason = "Meets the configured profit, ROI, confidence, and evidence thresholds."
    elif profit is None:
        reason = "No reliable resale estimate is available."
    elif not has_comparables:
        reason = "Not enough local comparable listings."
    elif not ai_requirement_met:
        reason = "AI review was requested but no provider returned a valid result."
    elif decision != "buy_candidate":
        reason = "The pricing or AI review requires a manual check."
    elif confidence < settings.minimum_confidence:
        reason = "Confidence is below the configured threshold."
    else:
        reason = "Profit or ROI is below the configured threshold."
    return {
        "telegram_eligible": eligible,
        "expected_sale_price_bhd": sale_price,
        "net_profit_bhd": profit,
        "roi_percent": roi,
        "confidence": round(confidence, 2),
        "evidence": "local_comparables" if has_comparables else "insufficient",
        "reason": reason,
    }

