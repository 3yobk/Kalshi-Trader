from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import erf, sqrt

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

        sigma = self._config.same_day_sigma_f if hours_to_expiration is not None and hours_to_expiration <= 24 else self._config.base_sigma_f
        sigma += _forecast_disagreement_penalty(forecast.raw, self._config.disagreement_sigma_threshold_f, self._config.disagreement_sigma_penalty_per_f)
        mean = forecast_max
        forecast_age = forecast.age_minutes
        metar_age = None
        max_age = forecast_age

        if metar and metar.temperature_f is not None:
            metar_age = metar.age_minutes
            max_age = max(max_age, metar_age)
            # Current observations anchor the forecast modestly without overfitting a single station report.
            mean = (1 - self._config.metar_weight) * forecast_max + self._config.metar_weight * max(
                forecast_max, metar.temperature_f
            )

        probability = _probability(mean, sigma, contract.threshold_f, contract.comparison, contract.upper_f)
        if hours_to_expiration is not None:
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
