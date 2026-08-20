"""Tests for gateway/platforms/rabbit_r1.py — Rabbit R1 adapter."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.rabbit_r1 import (
    RabbitR1Adapter,
    check_rabbit_r1_requirements,
)
from gateway.platforms.base import SendResult


def test_rabbit_r1_requirements():
    assert check_rabbit_r1_requirements() is True


def test_rabbit_r1_format_message():
    adapter = RabbitR1Adapter(PlatformConfig())
    raw = "Here is **bold** text and _italic_ with `code` and [link](https://example.com) and # Heading\n```python\nprint(1)\n```"
    formatted = adapter.format_message(raw)
    assert "**" not in formatted
    assert "```" not in formatted
    assert "`code`" not in formatted
    assert "bold" in formatted
    assert "italic" in formatted


@pytest.mark.asyncio
async def test_rabbit_r1_get_chat_info():
    adapter = RabbitR1Adapter(PlatformConfig())
    info = await adapter.get_chat_info("device-123")
    assert info["name"] == "Rabbit R1"
    assert info["chat_id"] == "device-123"
    assert info["connected"] is False


@pytest.mark.asyncio
async def test_rabbit_r1_send_disconnected():
    adapter = RabbitR1Adapter(PlatformConfig())
    result = await adapter.send("nonexistent", "hello")
    assert isinstance(result, SendResult)
    assert result.success is False
    assert "not connected" in result.error


@pytest.mark.asyncio
async def test_rabbit_r1_auth_handshake():
    cfg = PlatformConfig()
    cfg.token = "test-token-12345"
    adapter = RabbitR1Adapter(cfg)

    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    mock_ws.close = AsyncMock()

    # Test bad token
    bad_msg = {
        "id": "1",
        "params": {
            "auth": {"token": "wrong-token"},
            "device": {"id": "device-r1"},
        },
    }
    dev_id = await adapter._handle_connect(mock_ws, bad_msg, "127.0.0.1:5000")
    assert dev_id is None
    assert mock_ws.close.called

    # Test good token
    mock_ws.close.reset_mock()
    good_msg = {
        "id": "2",
        "params": {
            "auth": {"token": "test-token-12345"},
            "device": {"id": "device-r1"},
        },
    }
    dev_id = await adapter._handle_connect(mock_ws, good_msg, "127.0.0.1:5000")
    assert dev_id == "device-r1"
    assert adapter._clients["device-r1"] == mock_ws

    # Test send to paired device
    send_res = await adapter.send("device-r1", "Hello from Hermes")
    assert send_res.success is True
    assert mock_ws.send.called
