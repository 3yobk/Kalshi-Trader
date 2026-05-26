from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import erf, sqrt, exp, log

from config import ModelConfig
from data_ingestors.metar_client import MetarObservation
from data_ingestors.noaa_client import HourlyForecast


@dataclass(frozen=True)
class ContractSpec:
    ticker: str
    station: str
    threshold_f: float
    comparison: str
    expiration_time: datetime | None
    upper_f: float | None = None


@dataclass(frozen=True)
class ProbabilityEstimate:
    ticker: str
    station: str
    probability: float
    fair_yes_cents: int
    forecast_max_f: float
    adjusted_mean_f: float
    sigma_f: float
    generated_at: datetime
    max_data_age_minutes: float
    forecast_age_minutes: float = 0.0
    metar_age_minutes: float | None = 0.0
    threshold_f: float | None = None
    comparison: str | None = None
    upper_f: float | None = None


class TemperatureProbabilityModel:
    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def estimate(
            self,
            contract: ContractSpec,
            forecast: HourlyForecast,
            metar: MetarObservation | None,
    ) -> ProbabilityEstimate | None:
        forecast_max = forecast.forecast_max_f
        if forecast_max is None:
            return None

        now = datetime.now(timezone.utc)
        hours_to_expiration = None
        if contract.expiration_time:
            hours_to_expiration = max(0.0, (contract.expiration_time - now).total_seconds() / 3600)

        # ── Smart sigma: shrinks as we approach expiration and METAR anchors ──
        sigma = _adaptive_sigma(
            hours_to_expiration=hours_to_expiration,
            base_sigma=self._config.base_sigma_f,
            same_day_sigma=self._config.same_day_sigma_f,
            metar=metar,
            forecast_max=forecast_max,
        )

        # ── Disagreement penalty from provider spread ──
        sigma += _forecast_disagreement_penalty(
            forecast.raw,
            self._config.disagreement_sigma_threshold_f,
            self._config.disagreement_sigma_penalty_per_f,
        )

        mean = forecast_max
        forecast_age = forecast.age_minutes
        metar_age = None
        max_age = forecast_age

        if metar and metar.temperature_f is not None:
            metar_age = metar.age_minutes
            max_age = max(max_age, metar_age)

            # ── METAR anchoring: weight increases as we approach expiration ──
            # Early in day: METAR is just current temp, low weight
            # Near expiration: METAR is strong signal of where temp will end up
            metar_weight = _dynamic_metar_weight(
                hours_to_expiration=hours_to_expiration,
                base_weight=self._config.metar_weight,
                metar_temp=metar.temperature_f,
                forecast_max=forecast_max,
            )
            mean = (1 - metar_weight) * forecast_max + metar_weight * max(
                forecast_max, metar.temperature_f
            )

        probability = _probability(mean, sigma, contract.threshold_f, contract.comparison, contract.upper_f)

        # ── Kelly-informed edge: only apply decay far from expiration ──
        # Near expiration with fresh METAR, trust the estimate more
        if hours_to_expiration is not None and hours_to_expiration > 2:
            probability = _decay_toward_uncertainty(
                probability=probability,
                hours_to_expiration=hours_to_expiration,
                strength=self._config.expiration_decay_strength,
            )

        probability = min(0.99, max(0.01, probability))
        return ProbabilityEstimate(
            ticker=contract.ticker,
            station=contract.station,
            probability=probability,
            fair_yes_cents=round(probability * 100),
            forecast_max_f=forecast_max,
            adjusted_mean_f=mean,
            sigma_f=sigma,
            generated_at=now,
            max_data_age_minutes=max_age,
            forecast_age_minutes=forecast_age,
            metar_age_minutes=metar_age,
            threshold_f=contract.threshold_f,
            comparison=contract.comparison,
            upper_f=contract.upper_f,
        )


def _adaptive_sigma(
        hours_to_expiration: float | None,
        base_sigma: float,
        same_day_sigma: float,
        metar: MetarObservation | None,
        forecast_max: float,
) -> float:
    """
    Dynamic uncertainty that shrinks as we get more information:
    - Far from expiration (>24h): use base_sigma (most uncertain)
    - Same day (<24h): use same_day_sigma as floor
    - Near expiration (<6h) with fresh METAR: further reduce sigma
    - Very near (<2h) with obs close to forecast: very tight sigma

    This is the biggest model improvement — the old code used fixed sigma
    regardless of how much information was available.
    """
    if hours_to_expiration is None:
        return base_sigma

    if hours_to_expiration > 24:
        return base_sigma

    # Same-day base
    sigma = same_day_sigma

    # Scale down further as we approach expiration
    # At 12h: full same_day_sigma. At 1h: 40% of same_day_sigma
    if hours_to_expiration < 12:
        time_factor = max(0.4, hours_to_expiration / 12)
        sigma *= time_factor

    # If we have a fresh METAR and it's close to the forecast, reduce uncertainty
    if metar and metar.temperature_f is not None and metar.age_minutes < 30:
        obs_forecast_diff = abs(metar.temperature_f - forecast_max)
        if obs_forecast_diff < 2 and hours_to_expiration < 6:
            # Observation confirms forecast — high confidence
            sigma *= 0.7
        elif obs_forecast_diff > 5:
            # Observation diverges from forecast — increase uncertainty
            sigma *= 1.3

    return max(0.8, sigma)  # Never below 0.8°F regardless


def _dynamic_metar_weight(
        hours_to_expiration: float | None,
        base_weight: float,
        metar_temp: float,
        forecast_max: float,
) -> float:
    """
    METAR weight increases as we approach expiration.
    Early morning: current temp is irrelevant to daily max -> low weight
    Afternoon near expiration: current temp IS close to daily max -> high weight
    """
    if hours_to_expiration is None:
        return base_weight

    if hours_to_expiration > 12:
        return base_weight  # Early: use base weight

    if hours_to_expiration < 2:
        # Very close to expiration: current temp strongly predicts daily max
        # Only if current temp is ABOVE the forecast (current temp can only be daily max if it's high)
        if metar_temp >= forecast_max * 0.95:
            return min(0.6, base_weight * 4)
        return min(0.4, base_weight * 3)

    # Linearly interpolate weight from base to 3x base over 12h window
    scale = 1 + (1 - hours_to_expiration / 12) * 2
    return min(0.5, base_weight * scale)


def _kelly_position_fraction(edge: float, win_prob: float) -> float:
    """
    Full Kelly: f* = (bp - q) / b
    where b = odds (payout/cost - 1), p = win prob, q = 1-p
    For binary contracts at price c cents: b = (100-c)/c
    Returns fraction of bankroll to bet (capped at 25% for safety).
    """
    if win_prob <= 0 or win_prob >= 1 or edge <= 0:
        return 0.0
    lose_prob = 1 - win_prob
    # Half-Kelly for safety
    kelly = (edge / (1 - win_prob)) * 0.5
    return min(0.25, max(0.0, kelly))


def _probability(mean: float, sigma: float, threshold: float, comparison: str, upper: float | None = None) -> float:
    z = (threshold - mean) / sigma
    cdf = 0.5 * (1 + erf(z / sqrt(2)))
    if comparison in {">", ">=", "above", "at_or_above"}:
        return 1 - cdf
    if comparison in {"<", "<=", "below", "under"}:
        return cdf
    if comparison == "range":
        if upper is None:
            raise ValueError("Range contracts require upper_f")
        upper_z = (upper - mean) / sigma
        upper_cdf = 0.5 * (1 + erf(upper_z / sqrt(2)))
        return max(0.0, upper_cdf - cdf)
    raise ValueError(f"Unsupported comparison: {comparison}")


def _decay_toward_uncertainty(probability: float, hours_to_expiration: float, strength: float) -> float:
    if hours_to_expiration >= 24:
        return probability
    decay = strength * (1 - hours_to_expiration / 24)
    return probability * (1 - decay) + 0.5 * decay


def _forecast_disagreement_penalty(raw: dict, threshold_f: float, penalty_per_f: float) -> float:
    consensus = raw.get("weather_bot_consensus", {}) if isinstance(raw, dict) else {}
    noaa_max = consensus.get("noaa_max_f")
    external_sources = consensus.get("external_sources") or []
    if noaa_max is None or not external_sources:
        return 0.0
    deltas = []
    for source in external_sources:
        if isinstance(source, dict) and source.get("source") == "climatology":
            continue
        max_f = source.get("max_f") if isinstance(source, dict) else None
        if max_f is not None:
            deltas.append(abs(float(max_f) - float(noaa_max)))
    if not deltas:
        return 0.0
    excess = max(deltas) - threshold_f
    return max(0.0, excess) * max(0.0, penalty_per_f)