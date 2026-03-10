"""Transport manager for AlertBot.

Handles transport selection, initialization, and provides send/close functions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING

from alertbot.transport import Transport, TransportMessage

if TYPE_CHECKING:
    from alertbot.transports import TelegramTransport


# Global transport instance (lazy initialized)
_transport: Transport | None = None
_transport_lock = asyncio.Lock()
_transport_init_lock = threading.Lock()


def _create_transport(transport_name: str) -> Transport:
    """Create a transport instance by name.

    Args:
        transport_name: Name of the transport (e.g., "telegram", "matrix").

    Returns:
        Transport instance.

    Raises:
        ValueError: If transport name is unknown.
    """
    if transport_name == "telegram":
        from alertbot.transports.telegram_transport import TelegramTransport

        return TelegramTransport()
    if transport_name == "matrix":
        from alertbot.transports.matrix_transport import MatrixTransport

        return MatrixTransport()
    else:
        raise ValueError(f"Unknown transport: {transport_name}")


def get_transport() -> Transport:
    """Get the active transport instance (lazy singleton).

    The transport is selected based on ALERTBOT_TRANSPORT env var.
    Defaults to "telegram" if not set.

    Returns:
        Transport instance.
    """
    global _transport

    if _transport is not None:
        return _transport

    with _transport_init_lock:
        if _transport is not None:
            return _transport

        transport_name = os.getenv("ALERTBOT_TRANSPORT", "telegram").lower()
        logging.info("Initializing transport: %s", transport_name)

        _transport = _create_transport(transport_name)
        return _transport


async def close_transport() -> None:
    """Close the active transport and clean up resources."""
    global _transport

    async with _transport_lock:
        if _transport is not None:
            logging.info("Closing transport: %s", _transport.id)
            await _transport.close()
            _transport = None


async def send_alert_async(
    text: str,
    chat_id: str | None = None,
    parse_mode: str | None = None,
) -> str | None:
    """Send an alert via the active transport (async version).

    Args:
        text: Message text to send.
        chat_id: Target chat ID. Uses transport default if not specified.
        parse_mode: Parse mode ("html", "markdown", or None).

    Returns:
        Message ID on success, None on failure.
    """
    transport = get_transport()
    message = TransportMessage(
        text=text,
        chat_id=chat_id,
        parse_mode=parse_mode,
    )
    return await transport.send(message)


def send_alert(
    text: str,
    chat_id: str | None = None,
    parse_mode: str | None = None,
) -> str | None:
    """Send an alert via the active transport (sync wrapper).

    This is safe to call from synchronous code running in worker threads
    (bots run via asyncio.to_thread). It creates a temporary event loop
    for each call.

    Args:
        text: Message text to send.
        chat_id: Target chat ID. Uses transport default if not specified.
        parse_mode: Parse mode ("html", "markdown", or None).

    Returns:
        Message ID on success, None on failure.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(send_alert_async(text, chat_id, parse_mode))
    raise RuntimeError("send_alert() called from async context; use send_alert_async()")
