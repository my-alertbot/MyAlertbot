"""Transport interface definitions for AlertBot.

This module defines the protocol and data classes for transport implementations.
Transports handle sending messages to various platforms (e.g., Telegram).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    """Feature flags for a transport implementation."""

    supports_threads: bool = False
    supports_files: bool = False
    supports_templates: bool = False
    supports_edit: bool = False
    supports_mentions: bool = False


@dataclass(frozen=True, slots=True)
class TransportMessage:
    """Message payload to be sent via a transport."""

    text: str
    chat_id: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    parse_mode: str | None = None  # "html", "markdown", or None for plain text
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class IncomingCommand:
    """Represents an inbound command from a user."""

    command: str  # e.g., "status", "run"
    args: str  # everything after the command
    chat_id: str
    user_id: str | None = None
    message_id: str | None = None


class Transport(Protocol):
    """Protocol defining the transport interface.

    All methods are async to support non-blocking I/O across different platforms.
    """

    id: str
    capabilities: TransportCapabilities

    async def send(self, message: TransportMessage) -> str | None:
        """Send a message.

        Returns message_id on success, None on failure after retries are exhausted.
        """
        ...

    async def edit(self, message_id: str, message: TransportMessage) -> bool:
        """Edit an existing message.

        Returns True on success, False if unsupported or failed.
        """
        ...

    async def delete(self, message_id: str, chat_id: str | None = None) -> bool:
        """Delete a message.

        Returns True on success, False if unsupported or failed.
        """
        ...

    async def close(self) -> None:
        """Clean up resources (webhooks, connection pools, etc.)."""
        ...

    async def start_polling(
        self, on_command: Callable[[IncomingCommand], Awaitable[None]]
    ) -> None:
        """Start listening for inbound commands.

        Optional for send-only transports.
        """
        ...
