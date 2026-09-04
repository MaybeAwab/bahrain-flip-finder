from flip_finder.config import configured_secret, load_provider_settings, provider_keys


def test_configured_secret_rejects_placeholders() -> None:
    assert configured_secret("your_api_key_here") == ""
    assert configured_secret("${CLOUD5_API_KEY_1}") == ""


def test_numbered_provider_keys_are_ordered_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD5_API_KEY_2", "second")
    monkeypatch.setenv("CLOUD5_API_KEY_1", "first")
    monkeypatch.setenv("CLOUD5_API_KEYS", "second,third")
    assert provider_keys("CLOUD5") == ("first", "second", "third")


def test_provider_order_ignores_unknown_names(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER_ORDER", "deepseek,unknown,cloud5")
    assert [provider.name for provider in load_provider_settings()] == [
        "deepseek", "cloud5"
    ]
