from __future__ import annotations

import concurrent.futures
import hashlib
import re
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .config import DATA_DIR, Settings, load_json_file
from .models import Listing


CATALOG = load_json_file(DATA_DIR / "catalog_terms.json")
CURRENCY_PATTERN = "(?:" + "|".join(
    re.escape(value) for value in CATALOG.get("currency_patterns", ["BHD", "BD", "د.ب"])
) + ")"
USED_TERMS = tuple(CATALOG.get("used_terms", ["مستعمل", "مستعملة"]))
NEW_TERMS = tuple(CATALOG.get("new_terms", ["جديد", "جديدة"]))
POSTED_PATTERN = CATALOG.get(
    "posted_pattern",
    r"(?:\b\d+\s*(?:minutes?|hours?|days?|weeks?|months?)\s*ago\b|منذ\s+[^,]+)",
)


class RateLimitedError(RuntimeError):
    def __init__(self, url: str, retry_after: float) -> None:
        self.url = url
        self.retry_after = max(1.0, retry_after)
        super().__init__(
            f"Source returned HTTP 429; retry after {round(self.retry_after)} seconds"
        )


def retry_after_seconds(response: requests.Response, fallback: float = 120.0) -> float:
    raw = str(response.headers.get("Retry-After", "")).strip()
    if raw:
        try:
            return max(1.0, min(3600.0, float(raw)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw).timestamp()
                return max(1.0, min(3600.0, retry_at - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
    return max(1.0, min(3600.0, fallback))


class DomainRateLimiter:
    """Serialize requests by host and keep a conservative gap between them."""

    def __init__(self, minimum_gap_seconds: float) -> None:
        self.minimum_gap_seconds = max(0.5, minimum_gap_seconds)
        self._state_lock = threading.Lock()
        self._next_request: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}

    def host_lock(self, url: str) -> threading.Lock:
        host = urlparse(url).netloc.lower()
        with self._state_lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        now = time.monotonic()
        with self._state_lock:
            start_at = max(now, self._next_request.get(host, 0.0))
            self._next_request[host] = start_at + self.minimum_gap_seconds
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)

    def block(self, url: str, seconds: float) -> None:
        host = urlparse(url).netloc.lower()
        with self._state_lock:
            self._next_request[host] = max(
                self._next_request.get(host, 0.0),
                time.monotonic() + max(1.0, seconds),
            )


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_price(text: str) -> float | None:
    text = text.replace(",", "")
    patterns = (
        rf"{CURRENCY_PATTERN}\s*([0-9]+(?:\.[0-9]+)?)",
        rf"([0-9]+(?:\.[0-9]+)?)\s*{CURRENCY_PATTERN}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 0 < value < 1_000_000:
            return value
    return None


def detect_condition(text: str) -> str | None:
    lowered = text.lower()
    if any(term in lowered for term in ("used", *USED_TERMS)):
        return "used"
    if any(term in lowered for term in ("new", *NEW_TERMS)):
        return "new"
    return None


def detect_posted_text(text: str) -> str | None:
    match = re.search(POSTED_PATTERN, text, re.I)
    return match.group(0) if match else None


def extract_id(href: str, text: str, source: str) -> str:
    match = re.search(r"(?:ID|id)[-_]?([0-9]{5,})", href)
    if match:
        return f"{source}:{match.group(1)}"
    digest = hashlib.sha1(
        f"{source}|{href}|{text[:300]}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{source}:{digest}"


def title_from_card(text: str, price: float | None, image_alt: str = "") -> str:
    candidate = clean_text(image_alt) or clean_text(text)
    if price is not None:
        candidate = re.sub(
            rf"{CURRENCY_PATTERN}\s*[0-9,.]+|[0-9,.]+\s*{CURRENCY_PATTERN}",
            "",
            candidate,
            flags=re.I,
        )
    candidate = re.sub(
        r"\b\d+\s*(?:minutes?|hours?|days?|weeks?|months?)\s*ago\b",
        "",
        candidate,
        flags=re.I,
    )
    return clean_text(candidate)[:220]


def parse_listings(
    html: str,
    page_url: str,
    source: str,
    limit: int = 100,
) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[Listing] = []
    seen_ids: set[str] = set()

    if source == "dubizzle":
        cards = [
            card for card in soup.select("article")
            if card.select_one('a[href*="/ad/"]')
        ]
        for card in cards:
            anchor = card.select_one('a[href*="/ad/"]')
            if not anchor:
                continue
            href = str(anchor.get("href", "")).strip()
            absolute = urljoin(page_url, href)
            title_node = card.select_one('[aria-label="Title"] h2, h2')
            price_node = card.select_one('[aria-label="Price"]')
            image = card.select_one("img")
            title = clean_text(
                title_node.get_text(" ", strip=True)
                if title_node else str(anchor.get("title", ""))
            )
            card_text = clean_text(card.get_text(" ", strip=True))
            price_text = clean_text(
                price_node.get_text(" ", strip=True) if price_node else card_text
            )
            combined = clean_text(f"{title} {price_text} {card_text}")
            price = parse_price(combined)
            if price is None or len(title) < 3:
                continue
            listing_id = extract_id(absolute, combined, source)
            if listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)
            image_url = None
            if image and image.get("src"):
                image_url = urljoin(page_url, str(image.get("src")))
            results.append(Listing(
                source=source,
                url=absolute,
                listing_id=listing_id,
                title=title[:220],
                price_bhd=price,
                condition=detect_condition(combined),
                posted_text=detect_posted_text(combined),
                image_urls=[image_url] if image_url else [],
                raw_text=combined[:1200],
            ))
            if len(results) >= limit:
                break
        return results

    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(page_url, href)
        if source == "opensooq" and not any(
            marker in absolute for marker in ("/post/", "/listing/", "/en/", "/ar/")
        ):
            continue
        card_text = clean_text(anchor.get_text(" ", strip=True))
        image = anchor.find("img")
        image_alt = clean_text(str(image.get("alt", ""))) if image else ""
        image_url = (
            urljoin(page_url, str(image.get("src")))
            if image and image.get("src") else None
        )
        combined = clean_text(f"{image_alt} {card_text}")
        price = parse_price(combined)
        if price is None or len(combined) < 8:
            continue
        listing_id = extract_id(absolute, combined, source)
        if listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)
        title = title_from_card(card_text, price, image_alt)
        if len(title) < 3:
            continue
        results.append(Listing(
            source=source,
            url=absolute,
            listing_id=listing_id,
            title=title,
            price_bhd=price,
            condition=detect_condition(combined),
            posted_text=detect_posted_text(combined),
            image_urls=[image_url] if image_url else [],
            raw_text=combined[:1200],
        ))
        if len(results) >= limit:
            break
    return results


class HttpCollector:
    def __init__(
        self,
        settings: Settings,
        limiter: DomainRateLimiter | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.limiter = limiter or DomainRateLimiter(settings.http_min_gap_seconds)
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": settings.user_agent,
            "Accept-Language": "en,ar;q=0.8",
        })

    def fetch(self, url: str) -> str:
        with self.limiter.host_lock(url):
            self.limiter.wait(url)
            response = self.session.get(url, timeout=self.settings.http_timeout_seconds)
            if response.status_code == 429:
                retry_after = retry_after_seconds(response)
                self.limiter.block(url, retry_after)
                raise RateLimitedError(url, retry_after)
            response.raise_for_status()
            if len(response.text) > 20_000_000:
                raise RuntimeError(f"Unexpectedly large page rejected: {url}")
            return response.text

    def collect(self, url: str) -> list[Listing]:
        source = (
            "opensooq" if "opensooq" in url
            else "dubizzle" if "dubizzle" in url
            else "other"
        )
        return parse_listings(
            self.fetch(url),
            url,
            source,
            self.settings.max_results_per_source,
        )

    def enrich(self, listing: Listing) -> Listing:
        soup = BeautifulSoup(self.fetch(listing.url), "html.parser")
        meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = clean_text(str(meta.get("content", ""))) if meta else ""
        if not description:
            main = soup.select_one("main") or soup.body
            description = clean_text(main.get_text(" ", strip=True) if main else "")
        listing.description = description[:4000] or None
        for image in soup.select('meta[property="og:image"], img[src]'):
            candidate = image.get("content") or image.get("src")
            if candidate:
                absolute = urljoin(listing.url, str(candidate))
                if absolute not in listing.image_urls:
                    listing.image_urls.append(absolute)
            if len(listing.image_urls) >= 5:
                break
        listing.raw_text = clean_text(
            f"{listing.raw_text} {listing.description or ''}"
        )[:4000]
        return listing


def collect_all(settings: Settings) -> tuple[list[Listing], list[str]]:
    """Collect configured public pages concurrently and deduplicate their cards."""
    from .pricing import listing_signature

    limiter = DomainRateLimiter(settings.http_min_gap_seconds)

    def collect_one(url: str) -> tuple[list[Listing], str | None]:
        try:
            items = HttpCollector(settings, limiter=limiter).collect(url)
            return items, None
        except RateLimitedError as exc:
            return [], f"{url}: rate limited; retry after {round(exc.retry_after)} seconds"
        except requests.RequestException as exc:
            return [], f"{url}: request failed ({exc.__class__.__name__})"
        except Exception as exc:
            return [], f"{url}: {exc}"

    worker_count = max(1, min(settings.collect_workers, len(settings.source_urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        batches = list(executor.map(collect_one, settings.source_urls))

    unique: dict[str, Listing] = {}
    errors: list[str] = []
    for items, error in batches:
        if error:
            errors.append(error)
        for listing in items:
            signature = listing_signature(listing)
            if signature not in unique:
                listing.listing_id = signature
                unique[signature] = listing
    return list(unique.values()), errors

