from flip_finder.collectors import parse_listings, parse_price


def test_parse_price_before_amount() -> None:
    assert parse_price("BHD 12.500") == 12.5


def test_parse_price_after_amount_with_arabic_currency() -> None:
    assert parse_price("السعر 45 د.ب") == 45.0


def test_parse_price_rejects_text_without_currency() -> None:
    assert parse_price("Gaming monitor 144 Hz") is None


def test_parse_dubizzle_article() -> None:
    html = """
    <article>
      <a href="/ad/lg-monitor-id123456"><span aria-label="Title"><h2>LG 27GN800 monitor</h2></span></a>
      <span aria-label="Price">BHD 75</span>
      <span>Used · 2 days ago</span>
      <img src="/images/lg.jpg" />
    </article>
    """
    listings = parse_listings(
        html,
        "https://www.dubizzle.com.bh/en/electronics/",
        "dubizzle",
    )
    assert len(listings) == 1
    assert listings[0].title == "LG 27GN800 monitor"
    assert listings[0].price_bhd == 75
    assert listings[0].condition == "used"
    assert listings[0].listing_id == "dubizzle:123456"
    assert listings[0].image_urls == [
        "https://www.dubizzle.com.bh/images/lg.jpg"
    ]


def test_parse_opensooq_anchor_and_remove_price_from_title() -> None:
    html = """
    <a href="/en/post/987654">
      <img src="/img/phone.jpg" alt="iPhone 13 Pro used" />
      iPhone 13 Pro used — 180 BHD — 3 hours ago
    </a>
    """
    listings = parse_listings(
        html,
        "https://bh.opensooq.com/en/electronics",
        "opensooq",
    )
    assert len(listings) == 1
    assert listings[0].title == "iPhone 13 Pro used"
    assert listings[0].price_bhd == 180
    assert listings[0].condition == "used"


def test_parser_deduplicates_repeated_cards() -> None:
    html = """
    <a href="/en/post/id-123456">LG monitor 27GN800 — 70 BHD</a>
    <a href="/en/post/id-123456">LG monitor 27GN800 — 70 BHD</a>
    """
    listings = parse_listings(
        html,
        "https://bh.opensooq.com/en/electronics",
        "opensooq",
    )
    assert len(listings) == 1
