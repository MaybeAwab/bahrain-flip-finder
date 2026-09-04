from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any

from .ai import ProviderChain
from .collectors import DomainRateLimiter, HttpCollector, RateLimitedError, collect_all
from .config import Settings, configured_secret, load_provider_settings
from .models import Listing
from .pricing import comparables_for, evaluate_opportunity, rule_based_analysis
from .storage import SeenStore
from .telegram import TEXTS, format_alert, safe_send, send_message, telegram_batches


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def analyze_listing(
    candidate: Listing,
    all_listings: list[Listing],
    settings: Settings,
    provider_chain: ProviderChain | None = None,
) -> dict[str, Any]:
    comparables = comparables_for(candidate, all_listings)
    rule = rule_based_analysis(candidate, comparables, settings)
    ai_review = None
    ai_attempts: list[dict[str, Any]] = []
    if provider_chain is not None:
        ai_review, ai_attempts = provider_chain.analyze(
            candidate, comparables, rule, settings
        )
    opportunity = evaluate_opportunity(
        rule,
        ai_review,
        settings,
        ai_requested=provider_chain is not None,
    )
    return {
        "listing": candidate.to_dict(),
        "comparables": [item.to_dict() for item in comparables[:12]],
        "rule_based": rule,
        "ai_review": ai_review,
        "ai_attempts": ai_attempts,
        "opportunity": opportunity,
    }


def run_scan(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    chain = ProviderChain.from_env() if args.ai else None
    if args.ai and chain is not None and not chain.providers:
        log("AI requested, but no Cloud5 or DeepSeek key is configured.")

    telegram_sent = 0
    telegram_failed = 0
    if args.telegram:
        ok, error = safe_send(
            f"{TEXTS.get('startup', 'Bahrain Flip Finder started.')}\n"
            f"المصادر: {len(settings.source_urls)}\n"
            f"الوقت: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        telegram_sent += int(ok)
        telegram_failed += int(not ok)
        if error:
            log(f"Telegram startup message failed: {error}")

    log("Collecting public listings...")
    listings, collection_errors = collect_all(settings)
    for error in collection_errors:
        log(f"Source warning: {error}")
    log(f"Collected {len(listings)} unique listings.")

    outputs: list[dict[str, Any]] = []
    alerts: list[str] = []
    with SeenStore(settings.db_path) as store:
        new_items = [item for item in listings if not store.is_seen(item.listing_id)]
        if args.bootstrap:
            for item in listings:
                store.mark_seen(item)
            print(json.dumps({
                "status": "bootstrapped",
                "listings": len(listings),
                "collection_errors": collection_errors,
            }, ensure_ascii=False, indent=2))
            return 0

        chosen = new_items[:args.limit] if args.limit else new_items
        limiter = DomainRateLimiter(settings.http_min_gap_seconds)
        for index, listing in enumerate(chosen, start=1):
            log(f"Analyzing {index}/{len(chosen)}: {listing.title[:70]}")
            if args.details:
                try:
                    HttpCollector(settings, limiter=limiter).enrich(listing)
                except RateLimitedError as exc:
                    log(
                        f"Detail page rate limited; using card data "
                        f"(retry after {round(exc.retry_after)}s)."
                    )
                except Exception as exc:
                    log(f"Detail page unavailable: {exc.__class__.__name__}")

            analysis = analyze_listing(listing, listings, settings, chain)
            outputs.append(analysis)
            store.save_analysis(listing.listing_id, analysis)
            if args.ai and analysis["ai_review"] is None:
                store.schedule_retry(
                    listing.listing_id,
                    "No configured AI provider returned valid JSON",
                )
            else:
                store.mark_seen(listing)
            if args.telegram and analysis["opportunity"]["telegram_eligible"]:
                alerts.append(format_alert(analysis))

        if args.telegram:
            for batch in telegram_batches(alerts):
                ok, error = safe_send(batch)
                telegram_sent += int(ok)
                telegram_failed += int(not ok)
                if error:
                    log(f"Telegram alert failed: {error}")
            if not alerts:
                ok, error = safe_send(TEXTS.get(
                    "no_opportunities",
                    "No qualifying opportunity was found in this cycle.",
                ))
                telegram_sent += int(ok)
                telegram_failed += int(not ok)
                if error:
                    log(f"Telegram summary failed: {error}")

        result = {
            "status": "ok",
            "total_collected": len(listings),
            "new": len(new_items),
            "processed": len(outputs),
            "opportunities": len(alerts),
            "retry_pending": store.pending_count(),
            "collection_errors": collection_errors,
            "telegram_messages_sent": telegram_sent,
            "telegram_messages_failed": telegram_failed,
            "analyses": outputs,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_watch(args: argparse.Namespace) -> int:
    interval = max(60, int(args.interval or Settings.from_env().poll_seconds))
    log(f"Watch mode enabled; next cycle every {interval} seconds.")
    try:
        while True:
            run_scan(args)
            log(f"Next scan in {interval} seconds.")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("Watch stopped.")
        return 0


def run_test_ai(_: argparse.Namespace) -> int:
    Settings.from_env()
    chain = ProviderChain.from_env()
    if not chain.providers:
        log("No AI provider is configured. Add a Cloud5 or DeepSeek key to .env.")
        return 2
    attempts = []
    for provider in chain.providers:
        result = provider.ping()
        attempts.append(result)
        if result.get("status") == "ok":
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    print(json.dumps({"status": "failed", "attempts": attempts}, indent=2))
    return 1


def run_test_telegram(_: argparse.Namespace) -> int:
    Settings.from_env()
    try:
        send_message(TEXTS.get("test", "Bahrain Flip Finder test message."))
    except Exception as exc:
        log(f"Telegram test failed: {exc.__class__.__name__}")
        return 1
    print("Telegram test message sent.")
    return 0


def run_doctor(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    provider_status = [
        {
            "name": provider.name,
            "enabled": provider.enabled,
            "model": provider.model,
            "base_url": provider.base_url,
            "configured_keys": len(provider.api_keys),
        }
        for provider in load_provider_settings()
    ]
    telegram_ready = bool(
        configured_secret(os.getenv("TELEGRAM_BOT_TOKEN", ""))
        and os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    print(json.dumps({
        "database": str(settings.db_path),
        "sources": len(settings.source_urls),
        "http_timeout_seconds": settings.http_timeout_seconds,
        "http_min_gap_seconds": settings.http_min_gap_seconds,
        "environment_file": str(settings.env_file) if settings.env_file else None,
        "providers": provider_status,
        "telegram_configured": telegram_ready,
        "secrets_shown": False,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flip-finder",
        description="Evaluate public used-electronics listings in Bahrain.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Run one collection and analysis cycle")
    scan.add_argument("--limit", type=int, default=20, help="Maximum new listings to process; 0 means all")
    scan.add_argument("--details", action="store_true", help="Read each public detail page")
    scan.add_argument("--ai", action="store_true", help="Enable configured AI provider fallback")
    scan.add_argument("--telegram", action="store_true", help="Send qualifying opportunities to Telegram")
    scan.add_argument("--bootstrap", action="store_true", help="Mark current listings as seen without analysis")
    scan.add_argument("--watch", action="store_true", help="Repeat scans until stopped")
    scan.add_argument("--interval", type=int, default=0, help="Watch interval in seconds")
    scan.set_defaults(func=lambda args: run_watch(args) if args.watch else run_scan(args))

    test_ai = commands.add_parser("test-ai", help="Test AI providers in configured order")
    test_ai.set_defaults(func=run_test_ai)

    test_telegram = commands.add_parser("test-telegram", help="Send one Telegram test message")
    test_telegram.set_defaults(func=run_test_telegram)

    doctor = commands.add_parser("doctor", help="Show safe configuration diagnostics")
    doctor.set_defaults(func=run_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))

