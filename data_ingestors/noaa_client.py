from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class HourlyForecast:
    station: str
    generated_at: datetime
    temperatures_f: list[float]
    raw: dict[str, Any]

    @property
    def forecast_max_f(self) -> float | None:
        if not self.temperatures_f:
            return None
        return max(self.temperatures_f)

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.generated_at).total_seconds() / 60


class NoaaClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.weather.gov",
            timeout=timeout_seconds,
            headers={
                "Accept": "application/geo+json",
                "User-Agent": "safe-weather-paper-bot/0.1 contact=research@example.com",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> HourlyForecast:
        point_response = await self._client.get(f"/points/{lat:.4f},{lon:.4f}")
        point_response.raise_for_status()
        point_payload = point_response.json()
        hourly_url = point_payload["properties"]["forecastHourly"]

        hourly_response = await self._client.get(hourly_url)
        hourly_response.raise_for_status()
        hourly_payload = hourly_response.json()
        properties = hourly_payload.get("properties", {})
        generated_at = _parse_dt(properties.get("generatedAt"))
        temps = [
            float(period["temperature"])
            for period in properties.get("periods", [])[:24]
            if period.get("temperatureUnit") == "F" and period.get("temperature") is not None
        ]
        return HourlyForecast(
            station=station,
            generated_at=generated_at,
            temperatures_f=temps,
            raw=hourly_payload,
        )


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
