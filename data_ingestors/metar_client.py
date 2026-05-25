from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class MetarObservation:
    station: str
    observed_at: datetime
    temperature_c: float | None
    raw_text: str
    raw: dict[str, Any]

    @property
    def temperature_f(self) -> float | None:
        if self.temperature_c is None:
            return None
        return self.temperature_c * 9 / 5 + 32

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.observed_at).total_seconds() / 60


class MetarClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://aviationweather.gov",
            timeout=timeout_seconds,
            headers={"User-Agent": "safe-weather-paper-bot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def get_latest(self, station: str) -> MetarObservation | None:
        response = await self._client.get("/api/data/metar", params={"ids": station, "format": "json"})
        response.raise_for_status()
        payload = response.json()
        if not payload:
            return None
        item = payload[0]
        return MetarObservation(
            station=station,
            observed_at=_parse_observation_time(item),
            temperature_c=_optional_float(item.get("temp")),
            raw_text=str(item.get("rawOb", "")),
            raw=item,
        )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _parse_observation_time(item: dict[str, Any]) -> datetime:
    value = item.get("obsTime") or item.get("reportTime")
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)
