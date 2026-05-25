from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class RobotAnswer:
    question: str
    answer: str
    raw: dict[str, Any]


class RobotApiClient:
    """Client for the user's private LLM/Chromium bridge."""

    def __init__(self, api_url: str, api_key: str, timeout_seconds: float = 60.0) -> None:
        self._api_url = api_url
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(3))
    async def ask(self, question: str) -> RobotAnswer:
        response = await self._client.post(self._api_url, json={"question": question})
        response.raise_for_status()
        payload = response.json()
        answer = payload.get("answer") or payload.get("response") or payload.get("text") or ""
        return RobotAnswer(question=question, answer=str(answer), raw=payload)
