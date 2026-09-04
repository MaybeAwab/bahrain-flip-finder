from dataclasses import replace

from flip_finder.config import Settings
from flip_finder.models import Listing
from flip_finder.pricing import (
    calculate_profit,
    evaluate_opportunity,
    filter_price_outliers,
    recommended_max_buy_price,
    rule_based_analysis,
)


def listing(title: str, price: float, identifier: str) -> Listing:
    return Listing(
        "test",
        f"https://example.com/{identifier}",
        identifier,
        title,
        price,
        condition="used",
        description="Working and tested",
        image_urls=["https://example.com/image.jpg"],
    )


def test_iqr_filter_removes_extreme_price() -> None:
    assert filter_price_outliers([100, 102, 105, 107, 1000]) == [100, 102, 105, 107]


def test_small_price_sets_are_not_overfiltered() -> None:
    assert filter_price_outliers([20, 40, 500]) == [20, 40, 500]


def test_profit_and_roi_calculation() -> None:
    assert calculate_profit(85, 55) == (30, 54.5)


def test_zero_cost_has_no_roi() -> None:
    assert calculate_profit(10, 0) == (10, None)


def test_recommended_max_buy_price() -> None:
    settings = Settings()
    assert recommended_max_buy_price(85, settings) == 65


def test_rule_based_analysis_uses_median_inliers() -> None:
    settings = replace(Settings(), sale_realization_rate=0.85)
    candidate = listing("LG 27GN800 monitor", 50, "candidate")
    comparables = [
        listing("LG 27GN800 monitor", price, str(index))
        for index, price in enumerate([100, 102, 104, 106, 1000])
    ]
    result = rule_based_analysis(candidate, comparables, settings)
    assert result["estimated_market_price_bhd"] == 103
    assert result["expected_sale_price_bhd"] == 87.55
    assert result["total_cost_bhd"] == 55
    assert result["net_profit_bhd"] == 32.55
    assert result["roi_percent"] == 59.2
    assert result["recommended_max_buy_price_bhd"] == 67.55
    assert result["buy_decision"] == "buy_candidate"


def test_no_comparables_requires_manual_check() -> None:
    result = rule_based_analysis(
        listing("LG 27GN800 monitor", 50, "candidate"),
        [],
        Settings(),
    )
    assert result["buy_decision"] == "manual_check"
    assert result["expected_sale_price_bhd"] is None
    assert result["comparables_count"] == 0


def test_ai_cannot_inflate_local_sale_price() -> None:
    rule = {
        "expected_sale_price_bhd": 80,
        "total_cost_bhd": 50,
        "comparables_count": 4,
        "confidence": 0.8,
        "buy_decision": "buy_candidate",
    }
    ai = {
        "status": "ok",
        "expected_sale_price_bhd": 120,
        "confidence": 0.9,
        "buy_decision": "buy_candidate",
    }
    result = evaluate_opportunity(rule, ai, Settings(), ai_requested=True)
    assert result["expected_sale_price_bhd"] == 80
    assert result["net_profit_bhd"] == 30


def test_lower_ai_estimate_is_used_conservatively() -> None:
    rule = {
        "expected_sale_price_bhd": 80,
        "total_cost_bhd": 50,
        "comparables_count": 4,
        "confidence": 0.8,
        "buy_decision": "buy_candidate",
    }
    ai = {
        "status": "ok",
        "expected_sale_price_bhd": 70,
        "confidence": 0.7,
        "buy_decision": "buy_candidate",
    }
    result = evaluate_opportunity(rule, ai, Settings(), ai_requested=True)
    assert result["expected_sale_price_bhd"] == 70
    assert result["net_profit_bhd"] == 20
