from flip_finder.ai.providers import (
    OpenAICompatibleProvider,
    ProviderChain,
    load_json_loose,
    normalize_ai_result,
)
from flip_finder.config import ProviderSettings
from flip_finder.models import Listing


def candidate() -> Listing:
    return Listing("test", "https://example.com/1", "1", "LG 27GN800", 50)


def valid_payload() -> dict:
    return {
        "normalized_product": {
            "brand": "LG",
            "model": "27GN800",
            "category": "monitor",
            "key_specs": ["27 inch"],
            "condition": "used",
        },
        "identity_confidence": 0.9,
        "expected_sale_price_bhd": 85,
        "recommended_max_buy_price_bhd": 65,
        "net_profit_bhd": 30,
        "roi_percent": 54.5,
        "confidence": 0.8,
        "buy_decision": "buy_candidate",
        "missing_information": [],
        "seller_questions": [],
        "risks": ["Test ports"],
        "reason": "Enough matching evidence.",
    }


def test_load_json_loose_accepts_fenced_json() -> None:
    assert load_json_loose('```json\n{"ok": true}\n```') == {"ok": True}


def test_invalid_decision_and_confidence_are_normalized() -> None:
    payload = valid_payload()
    payload["buy_decision"] = "definitely_buy"
    payload["confidence"] = 5
    result = normalize_ai_result(
        payload,
        candidate(),
        {"total_cost_bhd": 55, "buy_decision": "buy_candidate"},
    )
    assert result["buy_decision"] == "manual_check"
    assert result["confidence"] == 1


def test_profit_mismatch_downgrades_buy_decision() -> None:
    payload = valid_payload()
    payload["net_profit_bhd"] = 100
    result = normalize_ai_result(
        payload,
        candidate(),
        {"total_cost_bhd": 55, "buy_decision": "buy_candidate"},
    )
    assert result["calculation_check"] == "warning_profit_mismatch"
    assert result["buy_decision"] == "manual_check"


def test_missing_identity_downgrades_buy_decision() -> None:
    payload = valid_payload()
    payload["normalized_product"]["model"] = ""
    result = normalize_ai_result(
        payload,
        candidate(),
        {"total_cost_bhd": 55, "buy_decision": "buy_candidate"},
    )
    assert result["buy_decision"] == "manual_check"


def test_valid_ai_result_passes_deterministic_check() -> None:
    result = normalize_ai_result(
        valid_payload(),
        candidate(),
        {"total_cost_bhd": 55, "buy_decision": "buy_candidate"},
    )
    assert result["calculation_check"] == "pass"
    assert result["buy_decision"] == "buy_candidate"


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls = 0

    def post(self, *args, **kwargs) -> FakeResponse:
        response = self.responses[self.calls]
        self.calls += 1
        return response


def test_provider_uses_next_key_after_rate_limit() -> None:
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "30"}),
        FakeResponse(200, {
            "choices": [{"message": {"content": '{"status":"connection_ok"}'}}]
        }),
    ])
    config = ProviderSettings(
        name="cloud5",
        base_url="https://example.com/v1",
        model="example-model",
        api_keys=("first", "second"),
        timeout_seconds=5,
        max_retries=0,
        retry_base_seconds=0.1,
        cooldown_seconds=30,
    )
    provider = OpenAICompatibleProvider(config, session=session)
    result = provider.ping()
    assert result["status"] == "ok"
    assert session.calls == 2


def test_provider_chain_falls_back_to_second_provider() -> None:
    class StubProvider:
        enabled = True

        def __init__(self, result, name):
            self.result = result
            self.name = name

        def analyze(self, *args):
            return self.result, {"provider": self.name, "status": "ok" if self.result else "error"}

    expected = {"status": "ok", "provider": "second"}
    chain = ProviderChain([
        StubProvider(None, "first"),
        StubProvider(expected, "second"),
    ])
    result, attempts = chain.analyze(candidate(), [], {}, None)
    assert result == expected
    assert [item["provider"] for item in attempts] == ["first", "second"]
