from datetime import datetime, timedelta, timezone

from config import RiskConfig
from data_ingestors.kalshi_client import MarketOrderBook, OrderBookLevel
from engine.model import ContractSpec, ProbabilityEstimate
from engine.risk_manager import RiskManager
from main import _exposure_metadata, parse_temperature_contract


def test_parse_threshold_contract_from_current_kalshi_ticker() -> None:
    market = {
        "ticker": "KXHIGHNY-26MAY25-T72",
        "event_ticker": "KXHIGHNY-26MAY25",
        "title": "Will the high temp in NYC be <72 degrees on May 25, 2026?",
        "close_time": "2026-05-25T23:59:00Z",
    }

    contract = parse_temperature_contract(market, {"KXHIGHNY": "KNYC"})

    assert contract is not None
    assert contract.station == "KNYC"
    assert contract.threshold_f == 72
    assert contract.comparison == "below"


def test_parse_bucket_range_contracts() -> None:
    market = {
        "ticker": "KXHIGHNY-26MAY25-B78.5",
        "event_ticker": "KXHIGHNY-26MAY25",
        "title": "Will the high temp in NYC be 78-79 degrees on May 25, 2026?",
    }

    contract = parse_temperature_contract(market, {"KXHIGHNY": "KNYC"})

    assert contract is not None
    assert contract.station == "KNYC"
    assert contract.comparison == "range"
    assert contract.threshold_f == 78
    assert contract.upper_f == 79


def test_exposure_metadata_from_threshold_ticker() -> None:
    metadata = _exposure_metadata("KXHIGHCHI-26MAY25-T82", {"KXHIGHCHI": "KORD"})

    assert metadata == {
        "series": "KXHIGHCHI",
        "event_date": "26MAY25",
        "station": "KORD",
    }


def test_risk_caps_quantity_by_remaining_daily_loss_budget() -> None:
    risk = RiskManager(RiskConfig(), min_liquidity_contracts=10, max_spread_cents=8)
    orderbook = MarketOrderBook(
        ticker="KXHIGHDEN-26MAY25-T87",
        yes=[OrderBookLevel(price_cents=1, quantity=1000)],
        no=[OrderBookLevel(price_cents=97, quantity=1000)],  # YES ask 3c
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHDEN-26MAY25-T87",
        station="KDEN",
        probability=0.284,
        fair_yes_cents=28,
        forecast_max_f=88,
        adjusted_mean_f=88,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=5,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=87,
        comparison="at_or_above",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.03,
        daily_pnl=-19.56,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=0,
        event_date_exposure_dollars=0,
        bankroll_dollars=200,
    )

    assert decision.allowed
    assert decision.max_contracts == 14


def test_risk_reports_forecast_and_metar_staleness_separately() -> None:
    risk = RiskManager(
        RiskConfig(stale_forecast_minutes=120, stale_metar_minutes=20),
        min_liquidity_contracts=10,
        max_spread_cents=8,
    )
    orderbook = MarketOrderBook(
        ticker="KXHIGHNY-26MAY25-T72",
        yes=[OrderBookLevel(price_cents=40, quantity=1000)],
        no=[OrderBookLevel(price_cents=50, quantity=1000)],
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHNY-26MAY25-T72",
        station="KNYC",
        probability=0.90,
        fair_yes_cents=90,
        forecast_max_f=70,
        adjusted_mean_f=70,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=130,
        forecast_age_minutes=130,
        metar_age_minutes=25,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=72,
        comparison="below",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.50,
        daily_pnl=0,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=0,
        event_date_exposure_dollars=0,
        bankroll_dollars=200,
    )

    assert "stale_forecast" in decision.reasons
    assert "stale_metar" in decision.reasons
    assert "stale_data" not in decision.reasons


def test_risk_caps_quantity_by_unresolved_exposure_budget() -> None:
    risk = RiskManager(
        RiskConfig(max_trade_dollars=5, max_unresolved_exposure_dollars=15),
        min_liquidity_contracts=10,
        max_spread_cents=8,
    )
    orderbook = MarketOrderBook(
        ticker="KXHIGHCHI-26MAY25-T82",
        yes=[OrderBookLevel(price_cents=80, quantity=1000)],
        no=[OrderBookLevel(price_cents=85, quantity=1000)],  # YES ask 15c
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHCHI-26MAY25-T82",
        station="KORD",
        probability=0.50,
        fair_yes_cents=50,
        forecast_max_f=83,
        adjusted_mean_f=83,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=5,
        forecast_age_minutes=5,
        metar_age_minutes=5,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=82,
        comparison="at_or_above",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.15,
        daily_pnl=0,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=14.70,
        event_date_exposure_dollars=0,
        bankroll_dollars=200,
    )

    assert decision.allowed
    assert decision.max_contracts == 2


def test_risk_blocks_at_unresolved_exposure_limit() -> None:
    risk = RiskManager(
        RiskConfig(max_unresolved_exposure_dollars=15),
        min_liquidity_contracts=10,
        max_spread_cents=8,
    )
    orderbook = MarketOrderBook(
        ticker="KXHIGHCHI-26MAY25-T82",
        yes=[OrderBookLevel(price_cents=80, quantity=1000)],
        no=[OrderBookLevel(price_cents=85, quantity=1000)],
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHCHI-26MAY25-T82",
        station="KORD",
        probability=0.50,
        fair_yes_cents=50,
        forecast_max_f=83,
        adjusted_mean_f=83,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=5,
        forecast_age_minutes=5,
        metar_age_minutes=5,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=82,
        comparison="at_or_above",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.15,
        daily_pnl=0,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=15,
        event_date_exposure_dollars=0,
        bankroll_dollars=200,
    )

    assert not decision.allowed
    assert "unresolved_exposure_limit" in decision.reasons


def test_risk_caps_quantity_by_event_date_exposure_budget() -> None:
    risk = RiskManager(
        RiskConfig(max_trade_dollars=5, max_unresolved_exposure_dollars=15, max_event_date_exposure_dollars=10),
        min_liquidity_contracts=10,
        max_spread_cents=8,
    )
    orderbook = MarketOrderBook(
        ticker="KXHIGHCHI-26MAY25-T82",
        yes=[OrderBookLevel(price_cents=80, quantity=1000)],
        no=[OrderBookLevel(price_cents=85, quantity=1000)],
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHCHI-26MAY25-T82",
        station="KORD",
        probability=0.50,
        fair_yes_cents=50,
        forecast_max_f=83,
        adjusted_mean_f=83,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=5,
        forecast_age_minutes=5,
        metar_age_minutes=5,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=82,
        comparison="at_or_above",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.15,
        daily_pnl=0,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=0,
        event_date_exposure_dollars=9.70,
        bankroll_dollars=200,
    )

    assert decision.allowed
    assert decision.max_contracts == 2


def test_risk_blocks_at_event_date_exposure_limit() -> None:
    risk = RiskManager(
        RiskConfig(max_event_date_exposure_dollars=10),
        min_liquidity_contracts=10,
        max_spread_cents=8,
    )
    orderbook = MarketOrderBook(
        ticker="KXHIGHCHI-26MAY25-T82",
        yes=[OrderBookLevel(price_cents=80, quantity=1000)],
        no=[OrderBookLevel(price_cents=85, quantity=1000)],
        captured_at=datetime.now(timezone.utc),
    )
    estimate = ProbabilityEstimate(
        ticker="KXHIGHCHI-26MAY25-T82",
        station="KORD",
        probability=0.50,
        fair_yes_cents=50,
        forecast_max_f=83,
        adjusted_mean_f=83,
        sigma_f=3.5,
        generated_at=datetime.now(timezone.utc),
        max_data_age_minutes=5,
        forecast_age_minutes=5,
        metar_age_minutes=5,
    )
    contract = ContractSpec(
        ticker=estimate.ticker,
        station=estimate.station,
        threshold_f=82,
        comparison="at_or_above",
        expiration_time=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    decision = risk.evaluate(
        contract=contract,
        estimate=estimate,
        orderbook=orderbook,
        market_price_probability=0.15,
        daily_pnl=0,
        market_exposure_dollars=0,
        unresolved_paper_exposure_dollars=0,
        event_date_exposure_dollars=10,
        bankroll_dollars=200,
    )

    assert not decision.allowed
    assert "event_date_exposure_limit" in decision.reasons
