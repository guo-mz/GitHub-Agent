"""可维护的模型价格与估算费用（非账单实付）。

- 价格单位：每百万 Token（per_1m）
- 未匹配 → estimated_cost = None
- 写入调用时的 price_version + pricing_snapshot，历史不随改价变动
- 展示币种默认 CNY；外币经配置汇率换算，并记录来源与时间
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 内置默认价目（估算用；非官方账单）。版本变更时递增 PRICE_CATALOG_VERSION。
PRICE_CATALOG_VERSION = "2026-04-01.v1"
DISPLAY_CURRENCY = "CNY"

# 手动配置汇率（非实时行情）
FX_USD_CNY = Decimal("7.20")
FX_AS_OF = "2026-01-01"
FX_SOURCE = "config:manual_static"


@dataclass(frozen=True)
class ModelPrice:
    provider: str  # * = any
    model: str  # exact or prefix*
    input_per_1m: Decimal
    output_per_1m: Decimal
    cached_input_per_1m: Optional[Decimal] = None
    reasoning_per_1m: Optional[Decimal] = None
    currency: str = "CNY"
    notes: str = ""


@dataclass
class CostResult:
    estimated_cost: Optional[Decimal]  # 展示币种 CNY；未匹配则为 None
    currency: Optional[str]  # 展示币种
    native_cost: Optional[Decimal]
    native_currency: Optional[str]
    fx_rate: Optional[Decimal]
    fx_as_of: Optional[str]
    fx_source: Optional[str]
    price_version: Optional[str]
    pricing_snapshot: Optional[Dict[str, Any]]
    estimate_limitations: str


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


def _builtin_prices() -> List[ModelPrice]:
    """默认估算价目。可被 JSON 覆盖。"""
    return [
        # 阿里云百炼（通义）常见公开标价量级，CNY / 百万 tokens
        ModelPrice("aliyun", "qwen-plus", _d("0.8"), _d("2.0"), _d("0.16"), currency="CNY",
                   notes="估算：通义 Plus 量级；缓存按输入折算示例"),
        ModelPrice("aliyun", "qwen-turbo", _d("0.3"), _d("0.6"), currency="CNY"),
        ModelPrice("aliyun", "qwen-max", _d("2.4"), _d("9.6"), currency="CNY"),
        ModelPrice("aliyun", "qwen*", _d("0.8"), _d("2.0"), currency="CNY",
                   notes="通义系列通配估算"),
        # DeepSeek 公开价接近 USD 标价；此处用 USD 再换算
        ModelPrice("deepseek", "deepseek-chat", _d("0.27"), _d("1.10"),
                   cached_input_per_1m=_d("0.07"), currency="USD",
                   notes="估算：DeepSeek chat 量级（USD）"),
        ModelPrice("deepseek", "deepseek-reasoner", _d("0.55"), _d("2.19"), currency="USD"),
        ModelPrice("deepseek", "deepseek*", _d("0.27"), _d("1.10"), currency="USD"),
        # Ollama 本地：费用未知
        # 无条目 → NULL
    ]


_CUSTOM_PATH = Path(__file__).resolve().parents[3] / ".data" / "llm_model_prices.json"
_price_cache: Optional[List[ModelPrice]] = None


def _load_custom() -> List[ModelPrice]:
    if not _CUSTOM_PATH.exists():
        return []
    try:
        raw = json.loads(_CUSTOM_PATH.read_text(encoding="utf-8"))
        items = raw.get("prices") if isinstance(raw, dict) else raw
        out: List[ModelPrice] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            out.append(
                ModelPrice(
                    provider=str(it.get("provider") or "*"),
                    model=str(it.get("model") or ""),
                    input_per_1m=_d(it["input_per_1m"]),
                    output_per_1m=_d(it["output_per_1m"]),
                    cached_input_per_1m=_d(it["cached_input_per_1m"])
                    if it.get("cached_input_per_1m") is not None
                    else None,
                    reasoning_per_1m=_d(it["reasoning_per_1m"])
                    if it.get("reasoning_per_1m") is not None
                    else None,
                    currency=str(it.get("currency") or "CNY").upper(),
                    notes=str(it.get("notes") or ""),
                )
            )
        return out
    except Exception:  # noqa: BLE001
        return []


def get_price_catalog(*, reload: bool = False) -> List[ModelPrice]:
    global _price_cache
    if _price_cache is not None and not reload:
        return _price_cache
    # 自定义优先（同 model 覆盖内置）
    custom = _load_custom()
    builtin = _builtin_prices()
    _price_cache = custom + builtin
    return _price_cache


def reset_price_cache() -> None:
    global _price_cache
    _price_cache = None


def _match_price(
    provider: str,
    model: str,
    catalog: List[ModelPrice],
) -> Optional[ModelPrice]:
    prov = (provider or "").strip().lower()
    mod = (model or "").strip().lower()
    if not mod:
        return None
    exact: List[ModelPrice] = []
    prefix: List[ModelPrice] = []
    for p in catalog:
        p_prov = p.provider.lower()
        if p_prov not in {"*", prov}:
            continue
        pm = p.model.lower()
        if pm.endswith("*"):
            stem = pm[:-1]
            if mod.startswith(stem) or mod == stem.rstrip("-"):
                prefix.append(p)
        elif pm == mod:
            exact.append(p)
    if exact:
        # 优先同 provider 精确
        for p in exact:
            if p.provider.lower() == prov:
                return p
        return exact[0]
    if prefix:
        for p in prefix:
            if p.provider.lower() == prov:
                return p
        return prefix[0]
    return None


def _to_cny(
    amount: Decimal,
    currency: str,
) -> Tuple[Decimal, Optional[Decimal], Optional[str], Optional[str]]:
    cur = (currency or "CNY").upper()
    if cur == "CNY":
        return amount, None, None, None
    if cur == "USD":
        return (
            (amount * FX_USD_CNY).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            FX_USD_CNY,
            FX_AS_OF,
            FX_SOURCE,
        )
    # 未知币种：不换算，费用视为未知
    return amount, None, None, f"unsupported_fx:{cur}"


def estimate_cost(
    *,
    provider: str,
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cached_input_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    catalog: Optional[List[ModelPrice]] = None,
) -> CostResult:
    limitations = (
        "估算费用，非服务商账单实付；不含折扣/批量/代理加价；"
        f"价目版本 {PRICE_CATALOG_VERSION}。"
    )
    empty = CostResult(
        estimated_cost=None,
        currency=None,
        native_cost=None,
        native_currency=None,
        fx_rate=None,
        fx_as_of=None,
        fx_source=None,
        price_version=None,
        pricing_snapshot=None,
        estimate_limitations=limitations + " 未匹配价格或 Token 不可用。",
    )

    # 无任何 token → 无法计费
    if all(v is None for v in (input_tokens, output_tokens, cached_input_tokens, reasoning_tokens)):
        return empty

    price = _match_price(provider, model, catalog or get_price_catalog())
    if price is None:
        return empty

    # 计费：缓存 token 若单独计价则从 input 中拆出（若 input 含 cached）
    inp = Decimal(input_tokens or 0)
    out = Decimal(output_tokens or 0)
    cached = Decimal(cached_input_tokens) if cached_input_tokens is not None else None
    reasoning = Decimal(reasoning_tokens) if reasoning_tokens is not None else None

    billable_input = inp
    if cached is not None and price.cached_input_per_1m is not None and inp > 0:
        # 若 input 已含 cache，按非缓存部分用 input 价、缓存用 cache 价
        non_cached = max(Decimal(0), inp - cached)
        billable_input = non_cached
    elif cached is not None and price.cached_input_per_1m is None:
        limitations += " 该模型未配置缓存单价，缓存 Token 未单独计费。"
        cached = None

    million = Decimal(1_000_000)
    native = Decimal(0)
    native += (billable_input / million) * price.input_per_1m
    native += (out / million) * price.output_per_1m
    if cached is not None and price.cached_input_per_1m is not None:
        native += (cached / million) * price.cached_input_per_1m
    if reasoning is not None:
        if price.reasoning_per_1m is not None:
            native += (reasoning / million) * price.reasoning_per_1m
        else:
            limitations += " 未配置 reasoning 单价，reasoning Token 未计入费用。"

    native = native.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    cny, fx_rate, fx_as_of, fx_source = _to_cny(native, price.currency)
    if fx_source and fx_source.startswith("unsupported_fx"):
        return CostResult(
            estimated_cost=None,
            currency=None,
            native_cost=native,
            native_currency=price.currency,
            fx_rate=None,
            fx_as_of=None,
            fx_source=fx_source,
            price_version=PRICE_CATALOG_VERSION,
            pricing_snapshot={
                **{k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(price).items()},
                "unit": "per_1m_tokens",
            },
            estimate_limitations=limitations + f" 无法将 {price.currency} 换算为 CNY。",
        )

    snapshot = {
        **{k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(price).items()},
        "unit": "per_1m_tokens",
        "matched_model": model,
        "matched_provider": provider,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    if fx_rate is not None:
        snapshot["fx"] = {
            "pair": f"{price.currency}_CNY",
            "rate": str(fx_rate),
            "as_of": fx_as_of,
            "source": fx_source,
        }

    return CostResult(
        estimated_cost=cny,
        currency=DISPLAY_CURRENCY,
        native_cost=native,
        native_currency=price.currency,
        fx_rate=fx_rate,
        fx_as_of=fx_as_of,
        fx_source=fx_source,
        price_version=PRICE_CATALOG_VERSION,
        pricing_snapshot=snapshot,
        estimate_limitations=limitations,
    )


def cost_to_float(v: Optional[Decimal]) -> Optional[float]:
    if v is None:
        return None
    return float(v)
