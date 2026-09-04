from __future__ import annotations

import os
import re
from typing import Any

import requests

from .config import DATA_DIR, configured_secret, load_json_file


TEXTS = load_json_file(DATA_DIR / "telegram_texts.json")


def _value(value: Any, suffix: str = "") -> str:
    return "غير متوفر" if value in (None, "") else f"{value}{suffix}"


def format_alert(analysis: dict[str, Any]) -> str:
    listing = analysis["listing"]
    rule = analysis["rule_based"]
    ai = analysis.get("ai_review") or {}
    opportunity = analysis["opportunity"]
    decision = ai.get("buy_decision") or rule.get("buy_decision") or "manual_check"
    decision_label = TEXTS.get("decision", {}).get(decision, "تحقق يدوي")

    lines = [
        "📌 فرصة إعادة بيع",
        "━━━━━━━━━━━━━━━━━━",
        f"القرار: {decision_label}",
        f"المنتج: {listing.get('title') or 'بدون عنوان'}",
        f"السعر المطلوب: {_value(listing.get('price_bhd'), ' د.ب')}",
        f"المصدر: {listing.get('source') or 'غير معروف'}",
        f"الرابط: {listing.get('url') or ''}",
        "",
        "الحساب المالي",
        f"سعر البيع المتوقع: {_value(opportunity.get('expected_sale_price_bhd'), ' د.ب')}",
        f"إجمالي التكلفة: {_value(rule.get('total_cost_bhd'), ' د.ب')}",
        f"الربح المتوقع: {_value(opportunity.get('net_profit_bhd'), ' د.ب')}",
        f"العائد: {_value(opportunity.get('roi_percent'), '%')}",
        f"أقصى سعر شراء: {_value(rule.get('recommended_max_buy_price_bhd'), ' د.ب')}",
        "",
        f"الثقة: {_value(opportunity.get('confidence'))}",
        f"المقارنات: {rule.get('comparables_count', 0)}",
        f"الخلاصة: {opportunity.get('reason')}",
    ]

    product = ai.get("normalized_product")
    if isinstance(product, dict) and (product.get("brand") or product.get("model")):
        lines.extend([
            "",
            "هوية المنتج",
            f"العلامة: {product.get('brand') or 'غير محدد'}",
            f"الموديل: {product.get('model') or 'غير محدد'}",
        ])

    risks = list(dict.fromkeys(
        list(rule.get("risks") or []) + list(ai.get("risks") or [])
    ))
    if risks:
        lines.extend(["", "⚠️ قبل الشراء"])
        lines.extend(f"- {item}" for item in risks[:5])
    return "\n".join(lines)[:3900]


def telegram_batches(alerts: list[str], max_chars: int = 3800) -> list[str]:
    batches: list[str] = []
    current = ""
    separator = "\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for alert in alerts:
        block = alert if not current else separator + alert
        if current and len(current) + len(block) > max_chars:
            batches.append(current)
            current = alert
        else:
            current += block
    if current:
        batches.append(current)
    return batches


def send_message(text: str, session: requests.Session | None = None) -> None:
    token = configured_secret(os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing from .env")
    client = session or requests.Session()
    response = client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=max(10, int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "30"))),
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        body = {}
    if body and body.get("ok") is False:
        raise RuntimeError(
            f"Telegram rejected the message: {body.get('description', 'unknown error')}"
        )


def safe_send(text: str) -> tuple[bool, str | None]:
    try:
        send_message(text)
        return True, None
    except Exception as exc:
        error = re.sub(
            r"https://api\.telegram\.org/bot[^/\s]+",
            "https://api.telegram.org/bot<redacted>",
            str(exc),
        )
        return False, error

