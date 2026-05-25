from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from data_ingestors.open_meteo_client import ExternalForecast


class TomorrowClient:
    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url="https://api.tomorrow.io", timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        response = await self._client.get(
            "/v4/weather/forecast",
            params={"location": f"{lat:.4f},{lon:.4f}", "timesteps": "1h", "units": "imperial", "apikey": self._api_key},
        )
        response.raise_for_status()
        payload = response.json()
        intervals = payload.get("timelines", {}).get("hourly", [])
        temps = [_optional_float(item.get("values", {}).get("temperature")) for item in intervals[:24]]
        return _forecast("tomorrow_io", station, [temp for temp in temps if temp is not None], payload)


class VisualCrossingClient:
    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url="https://weather.visualcrossing.com",
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        response = await self._client.get(
            f"/VisualCrossingWebServices/rest/services/timeline/{lat:.4f},{lon:.4f}/next24hours",
            params={"unitGroup": "us", "include": "hours", "elements": "datetime,temp", "key": self._api_key},
        )
        response.raise_for_status()
        payload = response.json()
        hours = []
        for day in payload.get("days", []):
            hours.extend(day.get("hours", []))
        temps = [_optional_float(hour.get("temp")) for hour in hours[:24]]
        return _forecast("visual_crossing", station, [temp for temp in temps if temp is not None], payload)


class WeatherApiClient:
    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url="https://api.weatherapi.com", timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        response = await self._client.get(
            "/v1/forecast.json",
            params={"key": self._api_key, "q": f"{lat:.4f},{lon:.4f}", "days": 1, "aqi": "no", "alerts": "no"},
        )
        response.raise_for_status()
        payload = response.json()
        hours = []
        for forecast_day in payload.get("forecast", {}).get("forecastday", []):
            hours.extend(forecast_day.get("hour", []))
        temps = [_optional_float(hour.get("temp_f")) for hour in hours[:24]]
        return _forecast("weatherapi", station, [temp for temp in temps if temp is not None], payload)


class MeteostatClient:
    def __init__(self, rapidapi_key: str, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://meteostat.p.rapidapi.com",
            timeout=timeout_seconds,
            headers={
                "x-rapidapi-key": rapidapi_key,
                "x-rapidapi-host": "meteostat.p.rapidapi.com",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        today = date.today().isoformat()
        response = await self._client.get(
            "/point/hourly",
            params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "start": today, "end": today},
        )
        response.raise_for_status()
        payload = response.json()
        temps_c = [_optional_float(item.get("temp")) for item in payload.get("data", [])[:24]]
        temps_f = [temp * 9 / 5 + 32 for temp in temps_c if temp is not None]
        return _forecast("meteostat", station, temps_f, payload)


class ClimatologyClient:
    async def close(self) -> None:
        return None

    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        normal = _monthly_normal_high(station, datetime.now(timezone.utc).month)
        return _forecast("climatology", station, [normal], {"station": station, "monthly_normal_high_f": normal})


def _forecast(source: str, station: str, temps: list[float], raw: dict[str, Any]) -> ExternalForecast:
    return ExternalForecast(
        source=source,
        station=station,
        generated_at=datetime.now(timezone.utc),
        temperatures_f=temps,
        raw=raw,
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _monthly_normal_high(station: str, month: int) -> float:
    normals = {
        "KNYC": [39, 42, 50, 62, 72, 80, 85, 83, 76, 64, 54, 44],
        "KORD": [32, 36, 47, 59, 70, 80, 84, 82, 75, 62, 48, 36],
        "KDEN": [46, 49, 57, 64, 73, 86, 92, 89, 81, 67, 53, 45],
        "KLAX": [68, 68, 69, 71, 72, 75, 79, 80, 80, 77, 72, 68],
        "KATL": [52, 57, 65, 73, 80, 87, 90, 89, 83, 73, 63, 54],
        "KMCI": [39, 44, 56, 67, 76, 85, 90, 88, 80, 68, 54, 42],
    }
    return float(normals.get(station, [60] * 12)[month - 1])
