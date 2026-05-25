from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class ExternalForecast:
    source: str
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


class OpenMeteoClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.open-meteo.com",
            timeout=timeout_seconds,
            headers={"User-Agent": "safe-weather-paper-bot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_hourly_forecast(self, station: str, lat: float, lon: float) -> ExternalForecast:
        response = await self._client.get(
            "/v1/forecast",
            params={
                "latitude": f"{lat:.4f}",
                "longitude": f"{lon:.4f}",
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "forecast_days": 1,
                "timezone": "UTC",
            },
        )
        response.raise_for_status()
        payload = response.json()
        temps = [float(value) for value in payload.get("hourly", {}).get("temperature_2m", [])[:24] if value is not None]
        return ExternalForecast(
            source="open_meteo",
            station=station,
            generated_at=datetime.now(timezone.utc),
            temperatures_f=temps,
            raw=payload,
        )
