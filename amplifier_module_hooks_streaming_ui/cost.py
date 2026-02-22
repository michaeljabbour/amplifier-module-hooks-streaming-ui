"""Cost calculation for LLM API usage.

Provides pricing data and cost estimation based on token usage.
"""

from dataclasses import dataclass
from typing import Optional

# ============================================================================
# Pricing Data (per 1K tokens)
# ============================================================================

# Last updated: 2025-01
# Prices in USD per 1,000 tokens
PRICING: dict[str, dict[str, tuple[float, float]]] = {
    # Anthropic
    "anthropic": {
        # (input_price, output_price) per 1K tokens
        "claude-sonnet-4-20250514": (0.003, 0.015),
        "claude-opus-4-20250514": (0.015, 0.075),
        "claude-3-5-sonnet-20241022": (0.003, 0.015),
        "claude-3-5-haiku-20241022": (0.0008, 0.004),
        "claude-3-opus-20240229": (0.015, 0.075),
        "claude-3-sonnet-20240229": (0.003, 0.015),
        "claude-3-haiku-20240307": (0.00025, 0.00125),
        # Aliases
        "claude-sonnet-4": (0.003, 0.015),
        "claude-opus-4": (0.015, 0.075),
    },
    # OpenAI
    "openai": {
        "gpt-4o": (0.005, 0.015),
        "gpt-4o-mini": (0.00015, 0.0006),
        "gpt-4-turbo": (0.01, 0.03),
        "gpt-4": (0.03, 0.06),
        "gpt-3.5-turbo": (0.0005, 0.0015),
        "o1": (0.015, 0.06),
        "o1-mini": (0.003, 0.012),
        "o1-preview": (0.015, 0.06),
        "o3-mini": (0.0011, 0.0044),
    },
    # Azure (same as OpenAI)
    "azure": {
        "gpt-4o": (0.005, 0.015),
        "gpt-4o-mini": (0.00015, 0.0006),
        "gpt-4-turbo": (0.01, 0.03),
        "gpt-4": (0.03, 0.06),
    },
    # Google
    "google": {
        "gemini-1.5-pro": (0.00125, 0.005),
        "gemini-1.5-flash": (0.000075, 0.0003),
        "gemini-2.0-flash": (0.0001, 0.0004),
    },
}


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
    provider_lower = provider.lower()

    if provider_lower not in PRICING:
        return None

    provider_pricing = PRICING[provider_lower]

    # Exact match
    if model in provider_pricing:
        return provider_pricing[model]

    # Try prefix matching for versioned models
    for known_model, prices in provider_pricing.items():
        if model.startswith(known_model) or known_model.startswith(model):
            return prices

    return None


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    provider: Optional[str],
    model: Optional[str],
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
) -> Optional[CostEstimate]:
    """Estimate cost based on token usage.

    Cache tokens are typically charged at different rates:
    - cache_read: Usually 90% discount (we use 10% of input price)
    - cache_create: Usually same as input price
    """
    if not provider or not model:
        return None

    pricing = get_pricing(provider, model)
    if not pricing:
        return None

    input_price, output_price = pricing

    # Calculate costs (prices are per 1K tokens)
    input_cost = (input_tokens / 1000) * input_price
    output_cost = (output_tokens / 1000) * output_price

    # Cache read is typically 90% cheaper
    cache_read_cost = (cache_read_tokens / 1000) * input_price * 0.1
    # Cache creation is typically same as input
    cache_create_cost = (cache_create_tokens / 1000) * input_price

    total_input_cost = input_cost + cache_read_cost + cache_create_cost

    return CostEstimate(
        input_cost=total_input_cost,
        output_cost=output_cost,
        total_cost=total_input_cost + output_cost,
    )
