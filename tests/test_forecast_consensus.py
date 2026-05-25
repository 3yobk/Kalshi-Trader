from datetime import datetime, timezone

from data_ingestors.noaa_client import HourlyForecast
from data_ingestors.open_meteo_client import ExternalForecast
from config import ModelConfig
from engine.forecast_consensus import build_consensus_forecast, consensus_source_weights
from engine.model import ContractSpec, TemperatureProbabilityModel


def test_build_consensus_forecast_shifts_noaa_temperatures_toward_external_max() -> None:
    now = datetime.now(timezone.utc)
    noaa = HourlyForecast(
        station="KATL",
        generated_at=now,
        temperatures_f=[70, 80],
        raw={"source": "noaa"},
    )
    open_meteo = ExternalForecast(
        source="open_meteo",
        station="KATL",
        generated_at=now,
        temperatures_f=[72, 84],
        raw={"source": "open_meteo"},
    )

    consensus = build_consensus_forecast(
        noaa,
        [open_meteo],
        source_weights={"open_meteo": 0.25},
        max_external_weight=0.55,
    )

    assert consensus.forecast_max_f == 81
    assert consensus.temperatures_f == [71, 81]
    assert consensus.raw["weather_bot_consensus"]["noaa_max_f"] == 80
    assert consensus.raw["weather_bot_consensus"]["consensus_max_f"] == 81


def test_model_widens_sigma_when_forecasts_disagree() -> None:
    now = datetime.now(timezone.utc)
    noaa = HourlyForecast("KATL", now, [70, 80], {})
    open_meteo = ExternalForecast("open_meteo", "KATL", now, [72, 90], {})
    consensus = build_consensus_forecast(
        noaa,
        [open_meteo],
        source_weights={"open_meteo": 0.25},
        max_external_weight=0.55,
    )
    contract = ContractSpec("TEST", "KATL", 82, "at_or_above", None)
    model = TemperatureProbabilityModel(
        ModelConfig(base_sigma_f=3.5, disagreement_sigma_threshold_f=3.0, disagreement_sigma_penalty_per_f=0.5)
    )

    estimate = model.estimate(contract, consensus, None)

    assert estimate is not None
    assert estimate.sigma_f == 7.0


def test_model_disagreement_penalty_ignores_climatology() -> None:
    now = datetime.now(timezone.utc)
    forecast = HourlyForecast(
        "KORD",
        now,
        [84],
        {
            "weather_bot_consensus": {
                "noaa_max_f": 84,
                "external_sources": [{"source": "climatology", "max_f": 70}],
            }
        },
    )
    contract = ContractSpec("TEST", "KORD", 84, "at_or_above", None)

    estimate = TemperatureProbabilityModel(ModelConfig(base_sigma_f=3.5)).estimate(contract, forecast, None)

    assert estimate is not None
    assert estimate.sigma_f == 3.5


def test_consensus_weights_are_source_specific_and_capped() -> None:
    now = datetime.now(timezone.utc)
    noaa = HourlyForecast("KNYC", now, [80], {})
    open_meteo = ExternalForecast("open_meteo", "KNYC", now, [84], {})
    tomorrow = ExternalForecast("tomorrow_io", "KNYC", now, [88], {})

    consensus = build_consensus_forecast(
        noaa,
        [open_meteo, tomorrow],
        source_weights={"open_meteo": 0.25, "tomorrow_io": 0.25},
        max_external_weight=0.40,
    )

    assert consensus.forecast_max_f == 82.4
    sources = consensus.raw["weather_bot_consensus"]["external_sources"]
    assert [source["weight"] for source in sources] == [0.2, 0.2]


def test_consensus_source_weights_reads_model_config() -> None:
    weights = consensus_source_weights(ModelConfig(tomorrow_weight=0.33, climatology_weight=0.02))

    assert weights["tomorrow_io"] == 0.33
    assert weights["climatology"] == 0.02


def test_model_scores_range_contracts() -> None:
    now = datetime.now(timezone.utc)
    forecast = HourlyForecast("KNYC", now, [78.5], {})
    contract = ContractSpec("TEST", "KNYC", 78, "range", None, upper_f=79)

    estimate = TemperatureProbabilityModel(ModelConfig(base_sigma_f=0.35)).estimate(contract, forecast, None)

    assert estimate is not None
    assert estimate.probability > 0.80
    assert estimate.threshold_f == 78
    assert estimate.upper_f == 79
