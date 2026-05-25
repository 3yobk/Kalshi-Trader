from __future__ import annotations

from dataclasses import replace

from data_ingestors.noaa_client import HourlyForecast
from data_ingestors.open_meteo_client import ExternalForecast


def build_consensus_forecast(
    noaa: HourlyForecast,
    external_forecasts: list[ExternalForecast],
    source_weights: dict[str, float],
    max_external_weight: float,
) -> HourlyForecast:
    weighted = [
        (forecast, max(0.0, source_weights.get(forecast.source, 0.0)))
        for forecast in external_forecasts
        if forecast.forecast_max_f is not None
    ]
    weighted = [(forecast, weight) for forecast, weight in weighted if weight > 0]
    if not weighted or max_external_weight <= 0:
        return noaa

    noaa_max = noaa.forecast_max_f
    if noaa_max is None:
        return noaa

    requested_weight = sum(weight for _, weight in weighted)
    capped_external_weight = min(max(0.0, max_external_weight), requested_weight)
    scale = capped_external_weight / requested_weight
    scaled = [(forecast, weight * scale) for forecast, weight in weighted]
    consensus_max = noaa_max * (1 - capped_external_weight) + sum(float(forecast.forecast_max_f) * weight for forecast, weight in scaled)
    shift = consensus_max - noaa_max
    return replace(
        noaa,
        temperatures_f=[temperature + shift for temperature in noaa.temperatures_f],
        raw={
            **noaa.raw,
            "weather_bot_consensus": {
                "noaa_max_f": noaa_max,
                "consensus_max_f": consensus_max,
                "external_weight": capped_external_weight,
                "external_sources": [
                    {
                        "source": forecast.source,
                        "max_f": forecast.forecast_max_f,
                        "age_minutes": forecast.age_minutes,
                        "weight": weight,
                    }
                    for forecast, weight in scaled
                ],
            },
        },
    )


def consensus_source_weights(config: object) -> dict[str, float]:
    return {
        "open_meteo": float(getattr(config, "open_meteo_weight", 0.0)),
        "tomorrow_io": float(getattr(config, "tomorrow_weight", 0.0)),
        "visual_crossing": float(getattr(config, "visual_crossing_weight", 0.0)),
        "weatherapi": float(getattr(config, "weatherapi_weight", 0.0)),
        "meteostat": float(getattr(config, "meteostat_weight", 0.0)),
        "climatology": float(getattr(config, "climatology_weight", 0.0)),
    }
