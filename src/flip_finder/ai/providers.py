from __future__ import annotations

import json
import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from ..config import ProviderSettings, Settings, load_provider_settings
from ..models import Listing
from ..pricing import calculate_profit


ANALYST_PROMPT = """Check this used-electronics listing for resale in Bahrain.

Work only with the listing, local comparisons and price calculation in the input.
Do not guess a model, specification or sale price. The price calculation from the
program is final; your job is to check the item identity and point out missing details
or risks.

Return JSON in this shape:
{
  "normalized_product": {"brand": "", "model": "", "category": "", "key_specs": [], "condition": ""},
  "identity_confidence": 0.0,
  "expected_sale_price_bhd": null,
  "recommended_max_buy_price_bhd": null,
  "net_profit_bhd": null,
  "roi_percent": null,
  "confidence": 0.0,
  "buy_decision": "buy_candidate|manual_check|skip",
  "missing_information": [],
  "seller_questions": [],
  "risks": [],
  "reason": ""
}

Only return buy_candidate when the model is clear and the supplied price calculation
is profitable. Otherwise return manual_check. Return JSON only.
"""

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_COOLDOWNS: dict[tuple[str, int], float] = {}
_COOLDOWN_LOCK = threading.Lock()


def load_json_loose(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


def extract_chat_content(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def normalize_ai_result(
    result: dict[str, Any],
    candidate: Listing,
    rule: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(result)
    product = normalized.get("normalized_product")
    if not isinstance(product, dict):
        product = {}
    normalized["normalized_product"] = {
        "brand": str(product.get("brand") or "").strip(),
        "model": str(product.get("model") or "").strip(),
        "category": str(product.get("category") or "").strip(),
        "key_specs": [
            str(item) for item in product.get("key_specs", [])
            if isinstance(item, (str, int, float))
        ][:12] if isinstance(product.get("key_specs"), list) else [],
        "condition": str(product.get("condition") or candidate.condition or "").strip(),
    }

    decision = normalized.get("buy_decision")
    if decision not in {"buy_candidate", "manual_check", "skip"}:
        decision = "manual_check"
    normalized["buy_decision"] = decision

    confidence = _number(normalized.get("confidence"))
    normalized["confidence"] = round(max(0.0, min(1.0, confidence or 0.0)), 2)
    identity_confidence = _number(normalized.get("identity_confidence"))
    normalized["identity_confidence"] = round(
        max(0.0, min(1.0, identity_confidence or 0.0)), 2
    )

    numeric_fields = (
        "expected_sale_price_bhd",
        "recommended_max_buy_price_bhd",
        "net_profit_bhd",
        "roi_percent",
    )
    for field in numeric_fields:
        normalized[field] = _number(normalized.get(field))
    for field in ("missing_information", "seller_questions", "risks"):
        values = normalized.get(field)
        normalized[field] = (
            [str(item).strip() for item in values if str(item).strip()][:12]
            if isinstance(values, list) else []
        )
    normalized["reason"] = str(normalized.get("reason") or "").strip()

    sale_price = normalized.get("expected_sale_price_bhd")
    total_cost = _number(rule.get("total_cost_bhd"))
    normalized["calculation_check"] = "insufficient_numbers"
    if sale_price is not None and sale_price > 0 and total_cost is not None:
        calculated_profit, calculated_roi = calculate_profit(sale_price, total_cost)
        normalized["calculated_net_profit_bhd"] = calculated_profit
        normalized["calculated_roi_percent"] = calculated_roi
        normalized["calculation_check"] = "pass"
        reported_profit = normalized.get("net_profit_bhd")
        reported_roi = normalized.get("roi_percent")
        if reported_profit is not None and abs(reported_profit - calculated_profit) > 1.0:
            normalized["calculation_check"] = "warning_profit_mismatch"
        if (
            reported_roi is not None
            and calculated_roi is not None
            and abs(reported_roi - calculated_roi) > 5.0
        ):
            normalized["calculation_check"] = "warning_roi_mismatch"

    has_identity = bool(
        normalized["normalized_product"]["brand"]
        and normalized["normalized_product"]["model"]
    )
    deterministic_buy = rule.get("buy_decision") == "buy_candidate"
    if normalized["buy_decision"] == "buy_candidate" and (
        not has_identity
        or not deterministic_buy
        or normalized["calculation_check"] != "pass"
    ):
        normalized["buy_decision"] = "manual_check"
        normalized["reason"] = "Identity or price checks need a manual review."
    normalized["status"] = "ok"
    return normalized


def _retry_after(response: requests.Response, fallback: float) -> float:
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


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ProviderSettings,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.name = config.name
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _request(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        last_attempt = {"provider": self.name, "status": "unavailable"}
        for key_index, api_key in enumerate(self.config.api_keys):
            cooldown_key = (self.name, key_index)
            with _COOLDOWN_LOCK:
                remaining = _COOLDOWNS.get(cooldown_key, 0.0) - time.time()
            if remaining > 0:
                last_attempt = {
                    "provider": self.name,
                    "status": "cooldown",
                    "retry_after_seconds": round(remaining),
                }
                continue

            for attempt in range(self.config.max_retries + 1):
                try:
                    response = self.session.post(
                        f"{self.config.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        json=payload,
                        timeout=self.config.timeout_seconds,
                    )
                except requests.RequestException as exc:
                    last_attempt = {
                        "provider": self.name,
                        "status": "connection_error",
                        "error": exc.__class__.__name__,
                    }
                    if attempt < self.config.max_retries:
                        time.sleep(min(30.0, self.config.retry_base_seconds * (2 ** attempt)))
                        continue
                    with _COOLDOWN_LOCK:
                        _COOLDOWNS[cooldown_key] = time.time() + self.config.cooldown_seconds
                    break

                status = response.status_code
                if 200 <= status < 300:
                    try:
                        body = response.json()
                    except ValueError:
                        return None, {"provider": self.name, "status": "invalid_response"}
                    if not isinstance(body, dict):
                        return None, {"provider": self.name, "status": "invalid_response"}
                    return body, {
                        "provider": self.name,
                        "model": self.config.model,
                        "status": "ok",
                    }

                if status in {401, 403}:
                    with _COOLDOWN_LOCK:
                        _COOLDOWNS[cooldown_key] = time.time() + 3600
                    last_attempt = {
                        "provider": self.name,
                        "status": "authentication_error",
                        "http_status": status,
                    }
                    break

                if status == 429:
                    wait_seconds = _retry_after(response, self.config.cooldown_seconds)
                    with _COOLDOWN_LOCK:
                        _COOLDOWNS[cooldown_key] = time.time() + wait_seconds
                    last_attempt = {
                        "provider": self.name,
                        "status": "rate_limited",
                        "http_status": 429,
                        "retry_after_seconds": round(wait_seconds),
                    }
                    break

                if status in RETRYABLE_STATUS_CODES:
                    last_attempt = {
                        "provider": self.name,
                        "status": "temporary_error",
                        "http_status": status,
                    }
                    if attempt < self.config.max_retries:
                        time.sleep(min(30.0, self.config.retry_base_seconds * (2 ** attempt)))
                        continue
                    with _COOLDOWN_LOCK:
                        _COOLDOWNS[cooldown_key] = time.time() + self.config.cooldown_seconds
                    break

                last_attempt = {
                    "provider": self.name,
                    "status": "http_error",
                    "http_status": status,
                }
                break
        return None, last_attempt

    def analyze(
        self,
        candidate: Listing,
        comparables: list[Listing],
        rule: dict[str, Any],
        settings: Settings,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        evidence = {
            "candidate_listing": candidate.to_dict(),
            "local_comparables": [item.to_dict() for item in comparables[:12]],
            "price_analysis": rule,
            "cost_assumptions": {
                "transport_bhd": settings.transport_bhd,
                "repair_reserve_bhd": settings.repair_reserve_bhd,
                "selling_fee_bhd": settings.selling_fee_bhd,
                "target_profit_bhd": settings.target_profit_bhd,
                "minimum_roi_percent": settings.minimum_roi_percent,
            },
        }
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {
                    "role": "user",
                    "content": ANALYST_PROMPT + "\n\nEvidence:\n" + json.dumps(
                        evidence, ensure_ascii=False
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 1800,
        }
        if self.name == "deepseek":
            payload["response_format"] = {"type": "json_object"}
            payload["thinking"] = {"type": "disabled"}
        body, attempt = self._request(payload)
        if body is None:
            return None, attempt
        content = extract_chat_content(body)
        parsed = load_json_loose(content)
        if not parsed:
            return None, {
                "provider": self.name,
                "model": self.config.model,
                "status": "invalid_json",
            }
        normalized = normalize_ai_result(parsed, candidate, rule)
        normalized["provider"] = self.name
        normalized["model"] = self.config.model
        return normalized, attempt

    def ping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": 'Return {"status":"connection_ok"} as JSON.'},
            ],
            "temperature": 0,
            "max_tokens": 40,
        }
        if self.name == "deepseek":
            payload["response_format"] = {"type": "json_object"}
            payload["thinking"] = {"type": "disabled"}
        body, attempt = self._request(payload)
        if body is None:
            return attempt
        parsed = load_json_loose(extract_chat_content(body))
        return {
            "provider": self.name,
            "model": self.config.model,
            "status": "ok" if parsed else "invalid_json",
        }


class ProviderChain:
    def __init__(self, providers: list[OpenAICompatibleProvider]) -> None:
        self.providers = [provider for provider in providers if provider.enabled]

    @classmethod
    def from_env(cls) -> "ProviderChain":
        return cls([
            OpenAICompatibleProvider(config)
            for config in load_provider_settings()
        ])

    def analyze(
        self,
        candidate: Listing,
        comparables: list[Listing],
        rule: dict[str, Any],
        settings: Settings,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        for provider in self.providers:
            result, attempt = provider.analyze(candidate, comparables, rule, settings)
            attempts.append(attempt)
            if result and result.get("status") == "ok":
                return result, attempts
        return None, attempts
