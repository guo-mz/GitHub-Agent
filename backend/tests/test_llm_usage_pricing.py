"""价格匹配、缓存计价、未知价格、币种换算。"""

from __future__ import annotations

from decimal import Decimal

from app.services.llm_usage.pricing import (
    DISPLAY_CURRENCY,
    FX_SOURCE,
    ModelPrice,
    estimate_cost,
    reset_price_cache,
)


def test_unknown_price_returns_none_not_zero():
    reset_price_cache()
    r = estimate_cost(
        provider="ollama",
        model="qwen2.5:7b",
        input_tokens=100,
        output_tokens=50,
    )
    assert r.estimated_cost is None
    assert r.currency is None


def test_no_tokens_returns_none():
    r = estimate_cost(provider="aliyun", model="qwen-plus", input_tokens=None, output_tokens=None)
    assert r.estimated_cost is None


def test_qwen_plus_cny():
    reset_price_cache()
    r = estimate_cost(
        provider="aliyun",
        model="qwen-plus",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert r.estimated_cost is not None
    assert r.currency == DISPLAY_CURRENCY
    assert r.native_currency == "CNY"
    assert r.price_version
    assert r.pricing_snapshot
    # 0.8 + 2.0 = 2.8
    assert abs(float(r.estimated_cost) - 2.8) < 1e-6


def test_cached_input_pricing():
    catalog = [
        ModelPrice(
            "aliyun",
            "qwen-plus",
            Decimal("1.0"),
            Decimal("2.0"),
            cached_input_per_1m=Decimal("0.1"),
            currency="CNY",
        )
    ]
    r = estimate_cost(
        provider="aliyun",
        model="qwen-plus",
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=400_000,
        catalog=catalog,
    )
    # non-cached 600k * 1.0 + cached 400k * 0.1 = 0.6 + 0.04 = 0.64
    assert r.estimated_cost is not None
    assert abs(float(r.estimated_cost) - 0.64) < 1e-6


def test_usd_to_cny_with_fx_metadata():
    reset_price_cache()
    r = estimate_cost(
        provider="deepseek",
        model="deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert r.estimated_cost is not None
    assert r.currency == "CNY"
    assert r.native_currency == "USD"
    assert r.fx_rate is not None
    assert r.fx_source == FX_SOURCE
    assert r.fx_as_of
    assert r.pricing_snapshot and "fx" in r.pricing_snapshot
