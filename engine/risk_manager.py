from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import RiskConfig
from data_ingestors.kalshi_client import MarketOrderBook
from engine.model import ContractSpec, ProbabilityEstimate


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    max_contracts: int = 0
    kelly_fraction: float = 0.0


class RiskManager:
    def __init__(self, config: RiskConfig, min_liquidity_contracts: int, max_spread_cents: int) -> None:
        self._config = config
        self._min_liquidity_contracts = min_liquidity_contracts
        self._max_spread_cents = max_spread_cents

    def evaluate(
            self,
            contract: ContractSpec,
            estimate: ProbabilityEstimate,
            orderbook: MarketOrderBook,
            market_price_probability: float,
            daily_pnl: float,
            market_exposure_dollars: float,
            unresolved_paper_exposure_dollars: float,
            event_date_exposure_dollars: float,
            bankroll_dollars: float,
            halted: bool = False,
            abnormal_volatility: bool = False,
    ) -> RiskDecision:
        reasons: list[str] = []
        edge = estimate.probability - market_price_probability
        ask = orderbook.best_yes_ask_cents
        spread = orderbook.spread_cents

        if halted:
            reasons.append("market_halted")
        if abnormal_volatility:
            reasons.append("abnormal_volatility")
        if edge < self._config.min_edge:
            reasons.append("edge_below_threshold")
        if estimate.forecast_age_minutes > self._config.stale_forecast_minutes:
            reasons.append("stale_forecast")
        if estimate.metar_age_minutes is None:
            reasons.append("missing_metar")
        elif estimate.metar_age_minutes > self._config.stale_metar_minutes:
            reasons.append("stale_metar")
        if ask is None:
            reasons.append("missing_executable_ask")
        if spread is None or spread > self._max_spread_cents:
            reasons.append("spread_too_wide")
        if self._visible_depth(orderbook) < self._min_liquidity_contracts:
            reasons.append("thin_orderbook")
        if daily_pnl <= -self._config.max_daily_loss_dollars:
            reasons.append("daily_loss_limit")
        if unresolved_paper_exposure_dollars >= self._config.max_unresolved_exposure_dollars:
            reasons.append("unresolved_exposure_limit")
        if event_date_exposure_dollars >= self._config.max_event_date_exposure_dollars:
            reasons.append("event_date_exposure_limit")
        if bankroll_dollars > 0 and market_exposure_dollars / bankroll_dollars >= self._config.max_market_exposure_fraction:
            reasons.append("market_exposure_limit")
        if self._near_expiration(contract) and estimate.probability < self._config.high_confidence_threshold:
            reasons.append("near_expiration_without_high_confidence")

        # ── Kelly-informed position sizing ──────────────────────────────────
        # Instead of flat cap, use half-Kelly to scale size by edge quality.
        # High edge (40%+) → larger position. Low edge (12%) → smaller position.
        kelly_fraction = 0.0
        max_contracts = 0

        if ask:
            price_dollars = ask / 100
            payout_dollars = 1.0  # each contract pays $1

            # Half-Kelly: f* = edge / (1 - win_prob) * 0.5
            # Capped at 25% of bankroll per trade for safety
            if edge > 0 and estimate.probability > 0:
                raw_kelly = (edge / (1 - estimate.probability)) * 0.5
                kelly_fraction = min(0.25, max(0.0, raw_kelly))

            kelly_dollars = bankroll_dollars * kelly_fraction if bankroll_dollars > 0 else self._config.max_trade_dollars
            kelly_contracts = int(kelly_dollars / price_dollars) if price_dollars > 0 else 0

            # Apply all hard caps (Kelly is the starting point, caps are the ceiling)
            trade_cap_contracts = int(self._config.max_trade_dollars / price_dollars)
            remaining_daily_budget = max(0.0, self._config.max_daily_loss_dollars + daily_pnl)
            daily_cap_contracts = int(remaining_daily_budget / price_dollars)
            remaining_exposure_budget = max(0.0, self._config.max_unresolved_exposure_dollars - unresolved_paper_exposure_dollars)
            exposure_cap_contracts = int(remaining_exposure_budget / price_dollars)
            remaining_event_budget = max(0.0, self._config.max_event_date_exposure_dollars - event_date_exposure_dollars)
            event_cap_contracts = int(remaining_event_budget / price_dollars)

            max_contracts = max(0, min(
                kelly_contracts,
                trade_cap_contracts,
                daily_cap_contracts,
                exposure_cap_contracts,
                event_cap_contracts,
            ))

            if daily_cap_contracts <= 0 and "daily_loss_limit" not in reasons:
                reasons.append("daily_loss_limit")
            if exposure_cap_contracts <= 0 and "unresolved_exposure_limit" not in reasons:
                reasons.append("unresolved_exposure_limit")
            if event_cap_contracts <= 0 and "event_date_exposure_limit" not in reasons:
                reasons.append("event_date_exposure_limit")

        return RiskDecision(
            allowed=not reasons and max_contracts > 0,
            reasons=reasons,
            max_contracts=max_contracts,
            kelly_fraction=kelly_fraction,
        )

    def _visible_depth(self, orderbook: MarketOrderBook) -> int:
        return int(sum(level.quantity for level in orderbook.yes) + sum(level.quantity for level in orderbook.no))

    def _near_expiration(self, contract: ContractSpec) -> bool:
        if not contract.expiration_time:
            return False
        minutes = (contract.expiration_time - datetime.now(timezone.utc)).total_seconds() / 60
        return minutes < self._config.near_expiration_minutes