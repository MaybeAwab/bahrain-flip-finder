from flip_finder.models import Listing
from flip_finder.pricing import (
    category_for,
    comparable_match_score,
    comparables_for,
    listing_signature,
)


def listing(title: str, price: float = 50, identifier: str = "x") -> Listing:
    return Listing("test", f"https://example.com/{identifier}", identifier, title, price)


def test_category_detection() -> None:
    assert category_for("LG UltraGear gaming monitor 27 inch") == "monitor"
    assert category_for("Canon office printer") == "printer"
    assert category_for("iPhone 15 Pro 256GB") == "phone"


def test_exact_model_match() -> None:
    score, match_type = comparable_match_score(
        listing("LG 27GN800 monitor"),
        listing("Used LG UltraGear 27GN800 screen", identifier="y"),
    )
    assert score >= 120
    assert match_type == "exact_model"


def test_same_category_match_without_model_identifier() -> None:
    score, match_type = comparable_match_score(
        listing("Samsung Odyssey monitor 32 inch"),
        listing("Samsung Odyssey display excellent", identifier="y"),
    )
    assert score > 0
    assert match_type == "same_category"


def test_numeric_overlap_does_not_cross_categories() -> None:
    score, match_type = comparable_match_score(
        listing("LG 27GN800 monitor"),
        listing("Printer cartridge 27GN800", identifier="y"),
    )
    assert score == 0
    assert match_type == "weak"


def test_comparables_prefer_two_exact_models() -> None:
    candidate = listing("LG 27GN800 monitor", 50, "candidate")
    exact_one = listing("LG 27GN800 gaming monitor", 75, "one")
    exact_two = listing("Monitor LG 27GN800 used", 80, "two")
    broad = listing("LG gaming monitor display", 90, "three")
    selected = comparables_for(candidate, [candidate, broad, exact_one, exact_two])
    assert selected == [exact_one, exact_two]


def test_signature_is_stable_for_reordered_title_tokens() -> None:
    first = listing("LG 27GN800 monitor", 70)
    second = listing("monitor 27GN800 LG", 70)
    assert listing_signature(first) == listing_signature(second)


def test_signature_changes_with_price() -> None:
    assert listing_signature(listing("LG 27GN800 monitor", 70)) != listing_signature(
        listing("LG 27GN800 monitor", 80)
    )
