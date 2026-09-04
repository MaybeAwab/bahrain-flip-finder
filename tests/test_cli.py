from flip_finder.cli import analyze_listing, build_parser
from flip_finder.config import Settings
from flip_finder.models import Listing


def test_analysis_runs_without_ai() -> None:
    candidate = Listing("test", "https://example.com/c", "c", "LG 27GN800 monitor", 50)
    comparables = [
        Listing("test", f"https://example.com/{index}", str(index), "LG 27GN800 monitor", price)
        for index, price in enumerate([80, 85, 90])
    ]
    result = analyze_listing(candidate, [candidate, *comparables], Settings())
    assert result["ai_review"] is None
    assert result["rule_based"]["comparables_count"] == 3


def test_scan_parser_supports_required_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["scan", "--ai", "--details"]).command == "scan"
    assert parser.parse_args(["test-ai"]).command == "test-ai"
    assert parser.parse_args(["test-telegram"]).command == "test-telegram"
    assert parser.parse_args(["doctor"]).command == "doctor"
