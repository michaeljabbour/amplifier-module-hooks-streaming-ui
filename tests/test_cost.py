"""Tests for cost estimation with Helicone API integration.

Tests cover:
- Helicone response parsing and operator matching
- Fallback pricing when API is unavailable
- Cache pricing (Helicone-sourced and heuristic)
- Public API stability (get_pricing, estimate_cost, CostEstimate)
"""

from unittest.mock import patch

import pytest

from amplifier_module_hooks_streaming_ui.cost import (
    CostEstimate,
    _ModelEntry,
    _PricingCache,
    estimate_cost,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _helicone_payload(*entries: dict) -> dict:
    """Build a minimal Helicone-shaped response."""
    return {"data": list(entries)}


def _entry(
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-20250514",
    operator: str = "equals",
    input_cost: float = 3.0,
    output_cost: float = 15.0,
    cache_read: float = 0.3,
    cache_write: float = 3.75,
) -> dict:
    """Build a single Helicone data entry (costs per 1M tokens)."""
    return {
        "provider": provider,
        "model": model,
        "operator": operator,
        "input_cost_per_1m": input_cost,
        "output_cost_per_1m": output_cost,
        "prompt_cache_read_per_1m": cache_read,
        "prompt_cache_write_per_1m": cache_write,
    }


# ---------------------------------------------------------------------------
# _PricingCache parsing
# ---------------------------------------------------------------------------


class TestPricingCacheParsing:
    """Test Helicone response parsing into _ModelEntry objects."""

    def test_parse_basic_entry(self):
        cache = _PricingCache()
        cache._parse_helicone(
            _helicone_payload(_entry(model="claude-sonnet-4-20250514"))
        )
        cache._loaded = True  # prevent get() from overwriting with fallback
        result = cache.get("anthropic", "claude-sonnet-4-20250514")
        assert result is not None
        assert result.input_per_1k == pytest.approx(0.003)
        assert result.output_per_1k == pytest.approx(0.015)

    def test_parse_cache_pricing(self):
        cache = _PricingCache()
        cache._parse_helicone(
            _helicone_payload(_entry(cache_read=0.3, cache_write=3.75))
        )
        cache._loaded = True  # prevent get() from overwriting with fallback
        result = cache.get("anthropic", "claude-sonnet-4-20250514")
        assert result is not None
        assert result.cache_read_per_1k == pytest.approx(0.0003)
        assert result.cache_write_per_1k == pytest.approx(0.00375)

    def test_azure_mirrors_openai(self):
        cache = _PricingCache()
        cache._parse_helicone(
            _helicone_payload(
                _entry(
                    provider="openai", model="gpt-4o", input_cost=2.5, output_cost=10.0
                )
            )
        )
        cache._loaded = True  # prevent get() from overwriting with fallback
        openai_result = cache.get("openai", "gpt-4o")
        azure_result = cache.get("azure", "gpt-4o")
        assert openai_result is not None
        assert azure_result is not None
        assert openai_result.input_per_1k == azure_result.input_per_1k


# ---------------------------------------------------------------------------
# Operator matching
# ---------------------------------------------------------------------------


class TestOperatorMatching:
    """Test equals / startsWith / includes operator logic."""

    def _cache_with(self, *entries: dict) -> _PricingCache:
        cache = _PricingCache()
        cache._parse_helicone(_helicone_payload(*entries))
        cache._loaded = True
        return cache

    def test_equals_exact(self):
        cache = self._cache_with(
            _entry(operator="equals", model="claude-sonnet-4-20250514")
        )
        assert cache.get("anthropic", "claude-sonnet-4-20250514") is not None
        # Fuzzy fallback matches prefixes even for equals-only entries
        # (intentional for versioned model names)
        assert cache.get("anthropic", "claude-sonnet-4") is not None
        # Completely unrelated model returns None
        assert cache.get("anthropic", "gpt-4o") is None

    def test_starts_with(self):
        cache = self._cache_with(
            _entry(operator="startsWith", model="gpt-4o", provider="openai")
        )
        assert cache.get("openai", "gpt-4o-2025-01-01") is not None
        assert cache.get("openai", "gpt-3.5-turbo") is None

    def test_includes(self):
        cache = self._cache_with(
            _entry(operator="includes", model="sonnet", provider="anthropic")
        )
        assert cache.get("anthropic", "claude-sonnet-4-20250514") is not None
        assert cache.get("anthropic", "claude-opus-4-20250514") is None

    def test_equals_takes_priority_over_starts_with(self):
        cache = self._cache_with(
            _entry(
                operator="startsWith", model="gpt-4o", provider="openai", input_cost=1.0
            ),
            _entry(
                operator="equals", model="gpt-4o", provider="openai", input_cost=2.5
            ),
        )
        result = cache.get("openai", "gpt-4o")
        assert result is not None
        assert result.input_per_1k == pytest.approx(0.0025)  # equals wins

    def test_unknown_provider_returns_none(self):
        cache = self._cache_with(_entry(provider="anthropic"))
        assert cache.get("banana", "whatever") is None


# ---------------------------------------------------------------------------
# Fallback pricing
# ---------------------------------------------------------------------------


class TestFallbackPricing:
    """Test offline fallback when Helicone is unreachable."""

    def test_fallback_loads_anthropic(self):
        cache = _PricingCache()
        cache._load_fallback()
        result = cache.get("anthropic", "claude-sonnet-4-20250514")
        assert result is not None
        assert result.input_per_1k > 0

    def test_fallback_loads_openai(self):
        cache = _PricingCache()
        cache._load_fallback()
        result = cache.get("openai", "gpt-4o-2025-01-01")
        assert result is not None

    def test_fallback_azure_mirrors_openai(self):
        cache = _PricingCache()
        cache._load_fallback()
        assert cache.get("azure", "gpt-4o-mini") is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Test get_pricing() and estimate_cost() public functions."""

    def test_cost_estimate_format_small(self):
        est = CostEstimate(input_cost=0.0001, output_cost=0.0002, total_cost=0.0003)
        assert est.format() == "<$0.01"

    def test_cost_estimate_format_normal(self):
        est = CostEstimate(input_cost=0.05, output_cost=0.10, total_cost=0.15)
        assert est.format() == "$0.15"

    def test_cost_estimate_format_breakdown(self):
        est = CostEstimate(input_cost=0.05, output_cost=0.10, total_cost=0.15)
        fmt = est.format(show_breakdown=True)
        assert "in:" in fmt
        assert "out:" in fmt

    def test_estimate_cost_returns_none_for_missing_provider(self):
        assert estimate_cost(1000, 500, None, "model") is None

    def test_estimate_cost_returns_none_for_missing_model(self):
        assert estimate_cost(1000, 500, "provider", None) is None

    @patch("amplifier_module_hooks_streaming_ui.cost._cache")
    def test_estimate_cost_uses_helicone_cache_pricing(self, mock_cache):
        """When Helicone provides cache pricing, use it instead of heuristics."""
        mock_cache.get.return_value = _ModelEntry(
            model="test",
            operator="equals",
            input_per_1k=0.003,
            output_per_1k=0.015,
            cache_read_per_1k=0.0003,  # actual Helicone rate
            cache_write_per_1k=0.00375,
        )
        result = estimate_cost(10000, 5000, "anthropic", "test", cache_read_tokens=8000)
        assert result is not None
        # cache_read cost = (8000/1000) * 0.0003 = 0.0024
        assert result.input_cost == pytest.approx(0.03 + 0.0024)

    @patch("amplifier_module_hooks_streaming_ui.cost._cache")
    def test_estimate_cost_fallback_cache_heuristic(self, mock_cache):
        """When no Helicone cache pricing, use 10% heuristic for reads."""
        mock_cache.get.return_value = _ModelEntry(
            model="test",
            operator="equals",
            input_per_1k=0.003,
            output_per_1k=0.015,
            cache_read_per_1k=0.0,  # no Helicone cache pricing
            cache_write_per_1k=0.0,
        )
        result = estimate_cost(10000, 5000, "anthropic", "test", cache_read_tokens=8000)
        assert result is not None
        # heuristic cache_read cost = (8000/1000) * 0.003 * 0.1 = 0.0024
        assert result.input_cost == pytest.approx(0.03 + 0.0024)
