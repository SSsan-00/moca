from __future__ import annotations

import math
from typing import Any


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write", "responses")

# shortcut: 既知モデルの標準単価のみ（2026-11-24）、JetBrainsの料金表が変わったら更新する。
# https://www.jetbrains.com/help/jetbrains-console/plans-and-pricing.html
MODEL_PRICES = {
    "claude-sonnet-4-20250514": (3, 15, 0.3, 3.75),
    "claude-sonnet-4-5-20250929": (3, 15, 0.3, 3.75),
    "claude-sonnet-4-6": (3, 15, 0.3, 3.75),
    "claude-opus-4-5-20251101": (5, 25, 0.5, 6.25),
    "claude-opus-4-6": (5, 25, 0.5, 6.25),
    "gpt-5": (1.25, 10, 0.125, None),
    "gpt-5.2": (1.75, 14, 0.175, None),
    "gpt-5.4": (2.5, 15, 0.25, None),
    "gemini-3-flash-preview": (0.5, 3, 0.05, None),
}


def _estimated_model_cost(model: Any, counts: list[int]) -> float | None:
    prices = MODEL_PRICES.get(model) if isinstance(model, str) else None
    if not prices or any(count and price is None for count, price in zip(counts, prices, strict=True)):
        return None
    try:
        cost = sum(count * (price or 0) for count, price in zip(counts, prices, strict=True)) / 1_000_000
        return cost if 0 < cost < 1_000_000 and math.isfinite(cost) else None
    except OverflowError:
        return None


class UsageDelta(list):
    def __init__(self, counts=None, cost_usd=0.0, cost_known=0):
        super().__init__([0] * len(USAGE_FIELDS) if counts is None else counts)
        self.cost_usd = cost_usd
        self.cost_known = cost_known

    def record(self, event: Any) -> None:
        agent = event.get("agentEvent") if isinstance(event, dict) else None
        if not isinstance(agent, dict) or agent.get("kind") != "LlmResponseMetadataEvent":
            return
        usage = agent.get("modelUsage")
        if not isinstance(usage, list):
            return
        for entry in usage:
            if not isinstance(entry, dict) or "inputTokens" not in entry or "outputTokens" not in entry:
                continue
            counts = [entry.get(key, 0) for key in ("inputTokens", "outputTokens", "cacheInputTokens", "cacheCreateTokens")]
            if all(type(value) is int and value >= 0 for value in counts):
                uncached, output, cache_read, cache_write = counts
                # 入力合計はキャッシュ読み込み分と作成分を含む。
                input_total = uncached + cache_read + cache_write
                for i, value in enumerate((input_total, output, cache_read, cache_write, 1)):
                    self[i] += value
                # shortcut: 未公開ログの正のcostを米ドルとして扱い、公式の費用APIがあれば移行する。
                cost = entry.get("cost")
                if not (type(cost) in (int, float) and 0 < cost < 1_000_000 and math.isfinite(cost)):
                    cost = _estimated_model_cost(entry.get("model"), counts)
                if cost is not None and 0 < cost < 1_000_000:
                    self.cost_usd += cost
                    self.cost_known += 1
