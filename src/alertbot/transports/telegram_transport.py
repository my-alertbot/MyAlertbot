"""Telegram transport implementation for AlertBot.

Uses aiohttp for async HTTP requests to the Telegram Bot API.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from alertbot.transport import (
    IncomingCommand,
    Transport,
    TransportCapabilities,
    TransportMessage,
)


# HTTP status codes that trigger retry with backoff
RATE_LIMIT_STATUS_CODES = {429, 502, 503, 504}

DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0


class TelegramTransport:
    """Telegram Bot API transport implementation."""

    id: str = "telegram"
    capabilities: TransportCapabilities = TransportCapabilities(
        supports_threads=False,
        supports_files=True,
        supports_templates=False,
        supports_edit=True,
        supports_mentions=True,
    )

    def __init__(
        self,
        token: str | None = None,
        default_chat_id: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    ):
        """Initialize Telegram transport.

        Args:
            token: Telegram bot token. If None, reads from TELEGRAM_BOT_TOKEN env var.
            default_chat_id: Default chat ID. If None, reads from TELEGRAM_CHAT_ID env var.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts for rate limit errors.
            retry_base_delay: Base delay for exponential backoff.
        """
        self._token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self._default_chat_id = default_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._base_url = f"https://api.telegram.org/bot{self._token}"

        if not self._token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")

    def _get_retry_delay(self, attempt: int, headers: dict[str, str] | None = None) -> float:
        """Calculate retry delay with exponential backoff.

        Respects Retry-After header if present.
        """
        if headers:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass
        return self._retry_base_delay * (2**attempt)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make an HTTP request to Telegram API with retry logic.

        Creates a new aiohttp session per request to avoid event loop binding issues
        when called from different threads.

        Args:
            method: HTTP method (GET, POST)
            endpoint: API endpoint (e.g., "sendMessage")
            data: Request body data

        Returns:
            Parsed JSON response on success, None on failure.
        """
        url = f"{self._base_url}/{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    if method == "POST":
                        async with session.post(url, data=data) as resp:
                            return await self._handle_response(resp, url, attempt)
                    else:
                        async with session.get(url, params=data) as resp:
                            return await self._handle_response(resp, url, attempt)

            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError(f"Request timeout for {url}")
                if attempt < self._max_retries:
                    delay = self._get_retry_delay(attempt)
                    logging.warning(
                        "Telegram request timeout, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                logging.error("Telegram request timeout after %d retries", self._max_retries)
                return None

            except aiohttp.ClientError as exc:
                logging.error("Telegram request failed: %s", exc)
                return None

        if last_error:
            logging.error("Max retries exceeded for Telegram: %s", last_error)
        return None

    async def _handle_response(
        self,
        resp: aiohttp.ClientResponse,
        url: str,
        attempt: int,
    ) -> dict[str, Any] | None:
        """Handle HTTP response with retry logic for rate limits.

        Returns parsed JSON on success, raises to trigger retry, or returns None on failure.
        """
        if resp.status in RATE_LIMIT_STATUS_CODES:
            if attempt < self._max_retries:
                headers = dict(resp.headers)
                delay = self._get_retry_delay(attempt, headers)
                logging.warning(
                    "Telegram rate limited (HTTP %s), retrying in %.1fs (attempt %d/%d)",
                    resp.status,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)
                # Raise to trigger retry in _request
                raise aiohttp.ClientError(f"Rate limited: HTTP {resp.status}")
            logging.error("Telegram rate limit exceeded after %d retries", self._max_retries)
            return None

        if resp.status != 200:
            logging.error("Telegram API error: HTTP %s", resp.status)
            return None

        try:
            payload = await resp.json()
        except ValueError as exc:
            logging.error("Telegram API invalid JSON: %s", exc)
            return None

        if not payload.get("ok"):
            logging.error("Telegram API response not ok: %s", payload)
            return None

        return payload

    async def send(self, message: TransportMessage) -> str | None:
        """Send a message via Telegram.

        Args:
            message: TransportMessage to send.

        Returns:
            Message ID as string on success, None on failure.
        """
        chat_id = message.chat_id or self._default_chat_id
        if not chat_id:
            logging.error("No chat_id specified and no default configured")
            return None

        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message.text,
        }

        if message.parse_mode:
            data["parse_mode"] = message.parse_mode

        if message.reply_to:
            data["reply_to_message_id"] = message.reply_to

        result = await self._request("POST", "sendMessage", data)
        if result and "result" in result:
            return str(result["result"].get("message_id"))
        return None

    async def edit(self, message_id: str, message: TransportMessage) -> bool:
        """Edit an existing Telegram message.

        Args:
            message_id: ID of the message to edit.
            message: New message content.

        Returns:
            True on success, False on failure.
        """
        chat_id = message.chat_id or self._default_chat_id
        if not chat_id:
            logging.error("No chat_id specified and no default configured")
            return False

        data: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message.text,
        }

        if message.parse_mode:
            data["parse_mode"] = message.parse_mode

        result = await self._request("POST", "editMessageText", data)
        return result is not None

    async def delete(self, message_id: str, chat_id: str | None = None) -> bool:
        """Delete a Telegram message.

        Args:
            message_id: ID of the message to delete.
            chat_id: Chat containing the message. Uses default if not specified.

        Returns:
            True on success, False on failure.
        """
        effective_chat_id = chat_id or self._default_chat_id
        if not effective_chat_id:
            logging.error("No chat_id specified and no default configured")
            return False

        data = {
            "chat_id": effective_chat_id,
            "message_id": message_id,
        }

        result = await self._request("POST", "deleteMessage", data)
        return result is not None

    async def close(self) -> None:
        """Clean up resources.

        No persistent session to close since we create sessions per-request.
        """
        pass

    async def start_polling(
        self, on_command: Callable[[IncomingCommand], Awaitable[None]]
    ) -> None:
        """Start polling for incoming commands.

        Note: The controller uses python-telegram-bot for polling.
        This method is a placeholder for transports that handle their own polling.
        """
        raise NotImplementedError(
            "Telegram polling is handled by python-telegram-bot in the controller"
        )


# Type assertion to verify TelegramTransport implements Transport protocol
def _check_protocol(t: Transport) -> None:
    pass


_check_protocol(TelegramTransport.__new__(TelegramTransport))  # type: ignore[arg-type]
