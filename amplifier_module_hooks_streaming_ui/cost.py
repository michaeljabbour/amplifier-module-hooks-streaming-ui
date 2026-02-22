"""Cost calculation for LLM API usage.

Fetches live pricing from the Helicone public API on first use,
falling back to a small hardcoded table when the network is unavailable.
All prices are stored internally as USD per 1K tokens.
"""

import json
import logging
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================================
# Helicone API
# ============================================================================

_HELICONE_URL = "https://www.helicone.ai/api/llm-costs"
_FETCH_TIMEOUT = 5  # seconds


@dataclass
class _ModelEntry:
    """A single model's pricing from Helicone."""

    model: str
    operator: str  # "equals" | "startsWith" | "includes"
    input_per_1k: float
    output_per_1k: float
    cache_read_per_1k: float = 0.0
    cache_write_per_1k: float = 0.0


@dataclass
class _PricingCache:
    """Thread-safe, lazily-populated pricing cache."""

    _entries: dict[str, list[_ModelEntry]] = field(default_factory=dict)
    _loaded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, provider: str, model: str) -> Optional[_ModelEntry]:
        """Lookup pricing, fetching from Helicone on first call."""
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self._fetch_all()

        provider_lower = provider.lower()
        entries = self._entries.get(provider_lower)
        if not entries:
            return None

        # 1. Exact match (operator == "equals")
        for e in entries:
            if e.operator == "equals" and e.model == model:
                return e

        # 2. startsWith: model starts with pattern OR pattern starts with model
        for e in entries:
            if e.operator == "startsWith":
                if model.startswith(e.model) or e.model.startswith(model):
                    return e

        # 3. includes: pattern appears anywhere in model string
        for e in entries:
            if e.operator == "includes" and e.model in model:
                return e

        # 4. Fuzzy fallback -- prefix match regardless of operator
        for e in entries:
            if model.startswith(e.model) or e.model.startswith(model):
                return e

        return None

    # ------------------------------------------------------------------

    def _fetch_all(self) -> None:
        """Fetch pricing from Helicone, falling back to hardcoded data."""
        try:
            data = _http_get_json(_HELICONE_URL)
            self._parse_helicone(data)
            logger.debug(
                "Loaded pricing from Helicone (%d providers)",
                len(self._entries),
            )
        except Exception:
            logger.debug("Helicone API unavailable, using fallback pricing")
            self._load_fallback()
        self._loaded = True

    def _parse_helicone(self, payload: dict) -> None:
        """Parse Helicone JSON response into _ModelEntry objects."""
        for item in payload.get("data", []):
            provider = (item.get("provider") or "").lower()
            model = item.get("model", "")
            operator = item.get("operator", "equals")

            input_per_1m = item.get("input_cost_per_1m") or 0
            output_per_1m = item.get("output_cost_per_1m") or 0
            cache_read_per_1m = item.get("prompt_cache_read_per_1m") or 0
            cache_write_per_1m = item.get("prompt_cache_write_per_1m") or 0

            entry = _ModelEntry(
                model=model,
                operator=operator,
                input_per_1k=input_per_1m / 1000,
                output_per_1k=output_per_1m / 1000,
                cache_read_per_1k=cache_read_per_1m / 1000,
                cache_write_per_1k=cache_write_per_1m / 1000,
            )
            self._entries.setdefault(provider, []).append(entry)

        # Mirror openai entries under "azure"
        if "openai" in self._entries and "azure" not in self._entries:
            self._entries["azure"] = list(self._entries["openai"])

    def _load_fallback(self) -> None:
        """Minimal hardcoded pricing (per 1K tokens) for offline use."""
        fallback: dict[str, dict[str, tuple[float, float]]] = {
            "anthropic": {
                "claude-sonnet-4": (0.003, 0.015),
                "claude-opus-4": (0.015, 0.075),
                "claude-3-5-sonnet": (0.003, 0.015),
                "claude-3-5-haiku": (0.0008, 0.004),
            },
            "openai": {
                "gpt-4o": (0.0025, 0.01),
                "gpt-4o-mini": (0.00015, 0.0006),
                "o3-mini": (0.0011, 0.0044),
                "o1": (0.015, 0.06),
            },
            "google": {
                "gemini-2.0-flash": (0.0001, 0.0004),
                "gemini-1.5-pro": (0.00125, 0.005),
            },
        }
        for provider, models in fallback.items():
            entries = []
            for model, (inp, out) in models.items():
                entries.append(
                    _ModelEntry(
                        model=model,
                        operator="startsWith",
                        input_per_1k=inp,
                        output_per_1k=out,
                    )
                )
            self._entries[provider] = entries

        # Azure mirrors OpenAI
        if "openai" in self._entries:
            self._entries["azure"] = list(self._entries["openai"])


def _http_get_json(url: str) -> dict:
    """Fetch JSON from a URL using stdlib only (no extra dependencies)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


# Module-level singleton
_cache = _PricingCache()


# ============================================================================
# Public API (unchanged interface)
# ============================================================================


@dataclass
class CostEstimate:
    """Cost estimate for a session."""

    input_cost: float
    output_cost: float
    total_cost: float

    def format(self, show_breakdown: bool = False) -> str:
        """Format cost for display."""
        if self.total_cost < 0.001:
            return "<$0.01"
        if show_breakdown:
            return f"${self.total_cost:.2f} (in: ${self.input_cost:.3f}, out: ${self.output_cost:.3f})"
        return f"${self.total_cost:.2f}"


def get_pricing(provider: str, model: str) -> Optional[tuple[float, float]]:
    """Get pricing for a provider/model combo.

    Returns (input_price, output_price) per 1K tokens, or None if unknown.
    """
    entry = _cache.get(provider, model)
    if entry is None:
        return None
    return (entry.input_per_1k, entry.output_per_1k)


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    provider: Optional[str],
    model: Optional[str],
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
) -> Optional[CostEstimate]:
    """Estimate cost based on token usage.

    Uses Helicone cache pricing when available, otherwise:
    - cache_read: 10% of input price (90% discount)
    - cache_create: same as input price
    """
    if not provider or not model:
        return None

    entry = _cache.get(provider, model)
    if entry is None:
        return None

    input_price = entry.input_per_1k
    output_price = entry.output_per_1k

    # Calculate costs (prices are per 1K tokens)
    input_cost = (input_tokens / 1000) * input_price
    output_cost = (output_tokens / 1000) * output_price

    # Cache pricing: prefer Helicone's actual rates, fall back to heuristics
    if entry.cache_read_per_1k > 0:
        cache_read_cost = (cache_read_tokens / 1000) * entry.cache_read_per_1k
    else:
        cache_read_cost = (cache_read_tokens / 1000) * input_price * 0.1

    if entry.cache_write_per_1k > 0:
        cache_create_cost = (cache_create_tokens / 1000) * entry.cache_write_per_1k
    else:
        cache_create_cost = (cache_create_tokens / 1000) * input_price

    total_input_cost = input_cost + cache_read_cost + cache_create_cost

    return CostEstimate(
        input_cost=total_input_cost,
        output_cost=output_cost,
        total_cost=total_input_cost + output_cost,
    )
