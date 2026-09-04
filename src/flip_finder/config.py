from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_SOURCE_URLS = (
    "https://bh.opensooq.com/en/electronics",
    "https://www.dubizzle.com.bh/en/electronics-home-appliances/computers-tablets/q-monitor/",
    "https://www.dubizzle.com.bh/en/ads/q-gaming-keyboard/",
    "https://www.dubizzle.com.bh/en/electronics-home-appliances/computers-tablets/q-keyboard/",
)

PLACEHOLDER_MARKERS = (
    "your_",
    "your-",
    "replace_me",
    "replace-with",
    "${",
)


def load_env(path: Path | None = None) -> Path | None:
    """Load a simple local .env without overwriting existing environment values."""
    candidates = [path] if path else [PROJECT_ROOT / ".env", Path.cwd() / ".env"]
    for candidate in dict.fromkeys(item for item in candidates if item is not None):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def configured_secret(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    lowered = value.lower()
    if not value or not value.isascii():
        return ""
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return ""
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _resolve_path(value: str, default: Path) -> Path:
    path = Path(value) if value else default
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    source_urls: tuple[str, ...] = DEFAULT_SOURCE_URLS
    db_path: Path = PROJECT_ROOT / "data" / "flip_finder.sqlite3"
    transport_bhd: float = 2.0
    repair_reserve_bhd: float = 3.0
    selling_fee_bhd: float = 0.0
    sale_realization_rate: float = 0.85
    target_profit_bhd: float = 15.0
    minimum_roi_percent: float = 25.0
    minimum_confidence: float = 0.60
    minimum_comparables: int = 3
    max_results_per_source: int = 100
    poll_seconds: int = 1800
    http_timeout_seconds: int = 15
    http_min_gap_seconds: float = 1.5
    collect_workers: int = 3
    require_ai_for_telegram: bool = True
    user_agent: str = "BahrainFlipFinder/0.1 (+public market research)"
    env_file: Path | None = field(default=None, compare=False)

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Settings":
        loaded = load_env(env_path)
        raw_urls = [item.strip() for item in os.getenv("FLIP_SOURCE_URLS", "").split(",")]
        urls = tuple(item for item in raw_urls if item) or DEFAULT_SOURCE_URLS
        default_db = PROJECT_ROOT / "data" / "flip_finder.sqlite3"
        return cls(
            source_urls=urls,
            db_path=_resolve_path(os.getenv("FLIP_DB_PATH", ""), default_db),
            transport_bhd=_float("TRANSPORT_BHD", 2.0),
            repair_reserve_bhd=_float("REPAIR_RESERVE_BHD", 3.0),
            selling_fee_bhd=_float("SELLING_FEE_BHD", 0.0),
            sale_realization_rate=min(1.0, _float("SALE_REALIZATION_RATE", 0.85)),
            target_profit_bhd=_float("TARGET_PROFIT_BHD", 15.0),
            minimum_roi_percent=_float("MINIMUM_ROI_PERCENT", 25.0),
            minimum_confidence=min(1.0, _float("MINIMUM_CONFIDENCE", 0.60)),
            minimum_comparables=_int("MINIMUM_COMPARABLES", 3, 1),
            max_results_per_source=_int("MAX_RESULTS_PER_SOURCE", 100, 1),
            poll_seconds=_int("POLL_SECONDS", 1800, 60),
            http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", 15, 1),
            http_min_gap_seconds=_float("HTTP_MIN_GAP_SECONDS", 1.5, 0.5),
            collect_workers=_int("COLLECT_WORKERS", 3, 1),
            require_ai_for_telegram=_bool("REQUIRE_AI_FOR_TELEGRAM", True),
            user_agent=os.getenv(
                "FLIP_USER_AGENT",
                "BahrainFlipFinder/0.1 (+public market research)",
            ).strip(),
            env_file=loaded,
        )


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    base_url: str
    model: str
    api_keys: tuple[str, ...]
    timeout_seconds: int
    max_retries: int
    retry_base_seconds: float
    cooldown_seconds: int

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model and self.api_keys)


PROVIDER_DEFAULTS = {
    "cloud5": ("https://tabitoken.com/v1", "glm-5.3-flash"),
    "deepseek": ("https://api.deepseek.com", "deepseek-v4-flash"),
}


def provider_keys(prefix: str) -> tuple[str, ...]:
    values: list[str] = []
    direct = configured_secret(os.getenv(f"{prefix}_API_KEY", ""))
    if direct:
        values.append(direct)
    numbered: list[tuple[int, str]] = []
    pattern = re.compile(rf"^{re.escape(prefix)}_API_KEY_(\d+)$")
    for name, value in os.environ.items():
        match = pattern.match(name)
        if not match:
            continue
        secret = configured_secret(value)
        if secret:
            numbered.append((int(match.group(1)), secret))
    values.extend(value for _, value in sorted(numbered))
    values.extend(
        secret
        for raw in os.getenv(f"{prefix}_API_KEYS", "").split(",")
        if (secret := configured_secret(raw))
    )
    return tuple(dict.fromkeys(values))


def load_provider_settings() -> list[ProviderSettings]:
    order = [item.strip().lower() for item in os.getenv(
        "AI_PROVIDER_ORDER", "cloud5,deepseek"
    ).split(",") if item.strip()]
    providers: list[ProviderSettings] = []
    for name in order:
        if name not in PROVIDER_DEFAULTS:
            continue
        prefix = name.upper()
        default_base, default_model = PROVIDER_DEFAULTS[name]
        providers.append(ProviderSettings(
            name=name,
            base_url=os.getenv(f"{prefix}_BASE_URL", default_base).rstrip("/"),
            model=os.getenv(f"{prefix}_MODEL", default_model).strip(),
            api_keys=provider_keys(prefix),
            timeout_seconds=_int("AI_TIMEOUT_SECONDS", 120, 5),
            max_retries=min(3, _int("AI_MAX_RETRIES", 2)),
            retry_base_seconds=_float("AI_RETRY_BASE_SECONDS", 2.0, 0.1),
            cooldown_seconds=_int("AI_KEY_COOLDOWN_SECONDS", 120, 5),
        ))
    return providers


def load_json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

