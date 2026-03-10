"""Matrix transport implementation for AlertBot.

Send-only implementation using the Matrix Client-Server API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from alertbot.transport import (
    IncomingCommand,
    Transport,
    TransportCapabilities,
    TransportMessage,
)


RATE_LIMIT_STATUS_CODES = {429, 502, 503, 504}

DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0


class MatrixTransport:
    """Matrix Client-Server API transport (send-only for now)."""

    id: str = "matrix"
    capabilities: TransportCapabilities = TransportCapabilities(
        supports_threads=False,
        supports_files=False,
        supports_templates=False,
        supports_edit=False,
        supports_mentions=False,
    )

    def __init__(
        self,
        homeserver_url: str | None = None,
        access_token: str | None = None,
        default_room_id: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    ):
        self._homeserver_url = (homeserver_url or os.getenv("MATRIX_HOMESERVER_URL") or "").rstrip("/")
        self._access_token = access_token or os.getenv("MATRIX_ACCESS_TOKEN")
        self._default_room_id = default_room_id or os.getenv("MATRIX_ROOM_ID")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

        if not self._homeserver_url:
            raise ValueError("MATRIX_HOMESERVER_URL not set")
        if not self._access_token:
            raise ValueError("MATRIX_ACCESS_TOKEN not set")

    def _get_retry_delay(
        self,
        attempt: int,
        headers: dict[str, str] | None = None,
        retry_after_ms: int | None = None,
    ) -> float:
        if retry_after_ms is not None and retry_after_ms >= 0:
            return max(retry_after_ms / 1000.0, 0.0)
        if headers:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return self._retry_base_delay * (2**attempt)

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        last_error: Exception | None = None

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        for attempt in range(self._max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        result = await self._handle_response(resp, url, attempt)
                        if isinstance(result, dict):
                            return result
                        if result == "retry":
                            continue
                        return None
            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError(f"Request timeout for {url}")
                if attempt < self._max_retries:
                    delay = self._get_retry_delay(attempt)
                    logging.warning(
                        "Matrix request timeout, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                logging.error("Matrix request timeout after %d retries", self._max_retries)
                return None
            except aiohttp.ClientError as exc:
                last_error = exc
                logging.error("Matrix request failed: %s", exc)
                return None

        if last_error:
            logging.error("Max retries exceeded for Matrix: %s", last_error)
        return None

    async def _handle_response(
        self,
        resp: aiohttp.ClientResponse,
        url: str,
        attempt: int,
    ) -> dict[str, Any] | str | None:
        payload: dict[str, Any] | None = None
        text_fallback: str | None = None

        try:
            payload = await resp.json(content_type=None)
        except Exception:
            try:
                text_fallback = await resp.text()
            except Exception:
                text_fallback = None

        if resp.status in RATE_LIMIT_STATUS_CODES:
            if attempt < self._max_retries:
                retry_after_ms = None
                if isinstance(payload, dict):
                    raw_retry_ms = payload.get("retry_after_ms")
                    if isinstance(raw_retry_ms, (int, float)):
                        retry_after_ms = int(raw_retry_ms)

                delay = self._get_retry_delay(attempt, dict(resp.headers), retry_after_ms)
                logging.warning(
                    "Matrix API rate limited/error (HTTP %s), retrying in %.1fs (attempt %d/%d)",
                    resp.status,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)
                return "retry"
            logging.error("Matrix API rate limit/server error after %d retries", self._max_retries)
            return None

        if resp.status < 200 or resp.status >= 300:
            if isinstance(payload, dict):
                logging.error("Matrix API error: HTTP %s %s", resp.status, payload)
            else:
                logging.error("Matrix API error: HTTP %s from %s (%s)", resp.status, url, text_fallback or "")
            return None

        if not isinstance(payload, dict):
            logging.error("Matrix API invalid JSON response from %s", url)
            return None
        return payload

    async def send(self, message: TransportMessage) -> str | None:
        room_id = message.chat_id or self._default_room_id
        if not room_id:
            logging.error("No room_id specified and no default configured")
            return None

        txn_id = uuid.uuid4().hex
        encoded_room = quote(room_id, safe="")
        url = (
            f"{self._homeserver_url}/_matrix/client/v3/rooms/"
            f"{encoded_room}/send/m.room.message/{txn_id}"
        )

        payload: dict[str, Any] = {
            "msgtype": "m.text",
            "body": message.text,
        }
        if message.metadata and isinstance(message.metadata, dict):
            payload.update({k: v for k, v in message.metadata.items() if k not in payload})

        result = await self._post_json(url, payload)
        if result and "event_id" in result:
            return str(result["event_id"])
        return None

    async def edit(self, message_id: str, message: TransportMessage) -> bool:
        logging.debug("Matrix transport edit is not implemented")
        return False

    async def delete(self, message_id: str, chat_id: str | None = None) -> bool:
        logging.debug("Matrix transport delete is not implemented")
        return False

    async def close(self) -> None:
        return None

    async def start_polling(
        self, on_command: Callable[[IncomingCommand], Awaitable[None]]
    ) -> None:
        logging.info("Matrix transport polling is not implemented (send-only)")
        return None

