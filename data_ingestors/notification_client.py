from __future__ import annotations

import httpx


class NotificationClient:
    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, title: str, message: str) -> None:
        payload = {"content": f"**{title}**\n{message}", "title": title, "message": message}
        response = await self._client.post(self._webhook_url, json=payload)
        response.raise_for_status()
