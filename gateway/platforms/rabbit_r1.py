"""
Rabbit R1 platform adapter for Hermes.

Speaks the OpenClaw/clawdbot-gateway WebSocket protocol so the Rabbit R1
device can talk to Hermes AI (full memory, skills, crons) from anywhere —
not just home WiFi.

Architecture:
    R1 (anywhere with internet)
        ->  wss://your-tunnel.trycloudflare.com  (TLS via Cloudflare)
    Server
        ->  rabbit_r1.py  (BasePlatformAdapter)
        ->  Hermes gateway -> LLM (full memory, skills, crons)

Protocol reference:
    QR payload:  {"type":"clawdbot-gateway","version":1,"ips":["..."],"port":443,"token":"<hex32>","protocol":"wss"}
    Handshake:   connect.challenge -> connect -> node.pair.approved -> connect.ok
    Chat:        chat.send (R1->server) / chat event (server->R1)
"""

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import time
import uuid
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

try:
    import websockets
    try:
        from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol
    except ImportError:
        from websockets.server import WebSocketServerProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketServerProtocol = Any

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def check_rabbit_r1_requirements() -> bool:
    """Return True if all required dependencies are available."""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("Rabbit R1: 'websockets' package not installed. Run: pip install websockets")
        return False
    return True


# ---------------------------------------------------------------------------
# Tunnel helpers
# ---------------------------------------------------------------------------

def _get_cloudflare_tunnel_url(port: int) -> Optional[str]:
    """Start a Cloudflare Quick Tunnel and return the wss:// URL."""
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(60):  # wait up to ~30s
            line = proc.stderr.readline()
            match = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
            if match:
                https_url = match.group(0)
                return https_url.replace("https://", "wss://")
        return None
    except (FileNotFoundError, Exception) as e:
        logger.warning(f"Rabbit R1: Cloudflare Tunnel unavailable: {e}")
        return None


def _get_tailscale_funnel_url(port: int) -> Optional[str]:
    """Start a Tailscale Funnel on *port* and return the public wss:// URL."""
    import urllib.request
    try:
        proc = subprocess.run(
            ["tailscale", "funnel", "status"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        # Check if funnel already has a status url
        for line in proc.stdout.splitlines():
            if "funnel" in line.lower() and "http" in line:
                host = line.strip().split()[-1]
                return f"wss://{host}"
        # Try to start it
        subprocess.run(
            ["tailscale", "funnel", str(port)],
            check=True, capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        status = json.loads(result.stdout)
        dns_name = status["Self"]["DNSName"].rstrip(".")
        return f"wss://{dns_name}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            KeyError, json.JSONDecodeError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------

class RabbitR1Adapter(BasePlatformAdapter):
    """
    Rabbit R1 platform adapter.

    Runs a WebSocket server that speaks the clawdbot-gateway protocol.
    On startup, optionally opens a Cloudflare Tunnel or Tailscale Funnel
    so the R1 can reach it from anywhere.
    """

    MAX_MESSAGE_LENGTH = 2000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.RABBIT_R1)

        self._port: int = int(os.getenv("RABBIT_R1_PORT", "18789"))
        self._tunnel_mode: str = os.getenv("RABBIT_R1_TUNNEL", "cloudflare").lower()

        # Token: from env, from config, or auto-generate
        token = os.getenv("RABBIT_R1_TOKEN") or getattr(config, "token", None)
        self._token: str = token or secrets.token_hex(32)

        # Runtime state
        self._server = None
        self._server_task: Optional[asyncio.Task] = None
        self._public_url: Optional[str] = None

        # device_id -> websocket mapping
        self._clients: Dict[str, WebSocketServerProtocol] = {}

        # Server->R1 keepalive
        self._keepalive_interval: int = int(
            os.getenv("RABBIT_R1_KEEPALIVE_INTERVAL", "300")
        )
        self._keepalive_tasks: Dict[str, asyncio.Task] = {}

        # Rate limiting
        self._auth_failures: Dict[str, List[float]] = {}
        self._max_auth_failures = 5
        self._auth_window_secs = 300.0

    # ------------------------------------------------------------------
    # BasePlatformAdapter - required methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the WebSocket server and (optionally) open the tunnel."""
        if not check_rabbit_r1_requirements():
            return False

        # Start the tunnel first
        self._public_url = await self._start_tunnel()

        # Start the WebSocket server
        try:
            self._server = await websockets.serve(
                self._handle_connection,
                "0.0.0.0",
                self._port,
            )
            logger.info(f"Rabbit R1: WebSocket server listening on port {self._port}")
        except OSError as e:
            logger.error(f"Rabbit R1: Failed to start WebSocket server: {e}")
            return False

        self._mark_connected()

        # Print the QR code and pairing info
        await self._print_pairing_info()

        return True

    async def disconnect(self) -> None:
        """Stop the WebSocket server."""
        for device_id in list(self._keepalive_tasks):
            self._stop_keepalive(device_id)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()
        self._mark_disconnected()
        logger.info("Rabbit R1: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text reply back to the R1 device."""
        ws = self._clients.get(chat_id)
        if not ws:
            return SendResult(success=False, error=f"Device {chat_id!r} not connected")

        run_id = str(uuid.uuid4())
        payload = {
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": run_id,
                "sessionKey": "main",
                "seq": 1,
                "state": "final",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": content}],
                    "timestamp": _now_ms(),
                    "stopReason": "stop",
                    "usage": {"input": 0, "output": 0, "totalTokens": 0},
                },
            },
        }
        try:
            await ws.send(json.dumps(payload))
            return SendResult(success=True, message_id=run_id)
        except Exception as e:
            logger.warning(f"Rabbit R1: send failed for {chat_id}: {e}")
            return SendResult(success=False, error=str(e))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return metadata about the device."""
        connected = chat_id in self._clients
        return {
            "name": "Rabbit R1",
            "type": "dm",
            "chat_id": chat_id,
            "connected": connected,
        }

    def format_message(self, content: str) -> str:
        """Strip markdown for the R1's small screen."""
        content = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', content)
        content = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', content)
        content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', content)
        content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
        content = re.sub(r'```\w*\n?', '', content)
        content = re.sub(r'`([^`]+)`', r'\1', content)
        return content.strip()

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a 'thinking' state to the R1."""
        ws = self._clients.get(chat_id)
        if not ws:
            return
        payload = {
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": str(uuid.uuid4()),
                "sessionKey": "main",
                "seq": 0,
                "state": "thinking",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "timestamp": _now_ms(),
                },
            },
        }
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            pass

    def supports_typing(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # WebSocket connection handling
    # ------------------------------------------------------------------

    async def _handle_connection(self, ws, path: str = "/") -> None:
        """Handle a new WebSocket connection from an R1 device."""
        remote = f"{ws.remote_address[0]}:{ws.remote_address[1]}"
        logger.debug(f"Rabbit R1: new connection from {remote}")

        # Send challenge immediately
        nonce = str(uuid.uuid4())
        await self._send_msg(ws, {
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": nonce, "ts": _now_ms()},
        })

        device_id: Optional[str] = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"Rabbit R1: invalid JSON from {remote}")
                    continue

                method = msg.get("method") or msg.get("type", "")

                # Auth handshake
                if method in ("connect", "gateway.connect"):
                    device_id = await self._handle_connect(ws, msg, remote)
                    if device_id is None:
                        break
                    continue

                if device_id is None:
                    logger.warning(f"Rabbit R1: unauthenticated message from {remote}")
                    continue

                # Chat
                if method == "chat.send":
                    await self._handle_chat_send(ws, msg, device_id)

                # Heartbeat
                elif method == "system-presence":
                    await self._send_msg(ws, {
                        "type": "res",
                        "id": msg.get("id"),
                        "ok": True,
                        "payload": {"ts": _now_ms()},
                    })

                # Abort
                elif method == "chat.abort":
                    await self._cancel_device_tasks(device_id)
                    await self._send_msg(ws, {"type": "res", "id": msg.get("id"), "ok": True})

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if device_id:
                self._stop_keepalive(device_id)
            if device_id and device_id in self._clients:
                del self._clients[device_id]
                logger.info(f"Rabbit R1: device disconnected: {device_id}")

    async def _handle_connect(
        self,
        ws: WebSocketServerProtocol,
        msg: dict,
        remote: str,
    ) -> Optional[str]:
        """Validate token and complete pairing handshake."""
        msg_id = msg.get("id")

        # Rate-limit
        ip = remote.rsplit(":", 1)[0]
        now = time.time()
        failures = self._auth_failures.get(ip, [])
        failures = [t for t in failures if now - t < self._auth_window_secs]
        self._auth_failures[ip] = failures
        if len(failures) >= self._max_auth_failures:
            logger.warning(f"Rabbit R1: rate-limited {remote}")
            await self._send_msg(ws, {
                "type": "res", "id": msg_id, "ok": False,
                "error": {"code": 429, "message": "Too many failed attempts"},
            })
            await ws.close()
            return None

        params = msg.get("params", {})

        client_token = (
            params.get("auth", {}).get("token")
            or params.get("authToken")
            or msg.get("token")
        )
        device_id = (
            params.get("device", {}).get("id")
            or params.get("deviceId")
            or f"r1-{remote}"
        )

        if not secrets.compare_digest(
            (client_token or "").encode(), self._token.encode()
        ):
            logger.warning(f"Rabbit R1: auth failed from {remote} (bad token)")
            self._auth_failures.setdefault(ip, []).append(now)
            await self._send_msg(ws, {
                "type": "res", "id": msg_id, "ok": False,
                "error": {"code": 401, "message": "Invalid token"},
            })
            await ws.close()
            return None

        # Auth passed
        self._clients[device_id] = ws
        self._start_keepalive(device_id, ws)
        logger.info(f"Rabbit R1: device paired: {device_id} from {remote}")

        await self._send_msg(ws, {
            "type": "event", "event": "node.pair.approved",
            "payload": {"deviceId": device_id, "token": str(uuid.uuid4())},
        })
        await self._send_msg(ws, {
            "type": "res", "id": msg_id, "ok": True,
            "payload": {"status": "paired", "ts": _now_ms()},
        })
        await self._send_msg(ws, {
            "type": "event", "event": "connect.ok",
            "payload": {"deviceId": device_id, "ts": _now_ms()},
        })

        return device_id

    async def _handle_chat_send(
        self,
        ws: WebSocketServerProtocol,
        msg: dict,
        device_id: str,
    ) -> None:
        """Route a chat.send message into the Hermes message pipeline."""
        params = msg.get("params", {})
        text = params.get("message", "").strip()
        if not text:
            return

        # Acknowledge immediately
        await self._send_msg(ws, {"type": "res", "id": msg.get("id"), "ok": True})

        source = self.build_source(
            chat_id=device_id,
            chat_name="Rabbit R1",
            chat_type="dm",
            user_id=device_id,
            user_name="R1 User",
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=params.get("idempotencyKey") or str(uuid.uuid4()),
        )

        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Server->R1 keepalive
    # ------------------------------------------------------------------

    def _start_keepalive(self, device_id: str, ws: WebSocketServerProtocol) -> None:
        self._stop_keepalive(device_id)
        self._keepalive_tasks[device_id] = asyncio.ensure_future(
            self._keepalive_loop(device_id, ws)
        )

    def _stop_keepalive(self, device_id: str) -> None:
        task = self._keepalive_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    async def _keepalive_loop(self, device_id: str, ws: WebSocketServerProtocol) -> None:
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                await self._send_msg(ws, {
                    "type": "event", "event": "system-presence",
                    "payload": {"ts": _now_ms(), "deviceId": device_id},
                })
                logger.debug(f"Rabbit R1: keepalive sent to {device_id}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Rabbit R1: keepalive stopped for {device_id}: {e}")

    async def _cancel_device_tasks(self, device_id: str) -> None:
        """Cancel background tasks for a specific device."""
        # Cancel via base's cancel_background_tasks (cancels all),
        # but for R1 we do a softer cancel.
        await self.cancel_background_tasks()

    # ------------------------------------------------------------------
    # Tunnel
    # ------------------------------------------------------------------

    async def _start_tunnel(self) -> Optional[str]:
        """Start the configured tunnel and return the public wss:// URL."""
        explicit_url = os.getenv("RABBIT_R1_PUBLIC_URL")
        if explicit_url:
            logger.info(f"Rabbit R1: using explicit public URL: {explicit_url}")
            return explicit_url

        if self._tunnel_mode == "none":
            return None

        loop = asyncio.get_event_loop()

        if self._tunnel_mode == "tailscale":
            url = await loop.run_in_executor(
                None, _get_tailscale_funnel_url, self._port
            )
            if url:
                logger.info(f"Rabbit R1: Tailscale Funnel active at {url}")
            else:
                logger.warning("Rabbit R1: Tailscale Funnel unavailable")
            return url

        if self._tunnel_mode == "cloudflare":
            url = await loop.run_in_executor(
                None, _get_cloudflare_tunnel_url, self._port
            )
            if url:
                logger.info(f"Rabbit R1: Cloudflare Tunnel active at {url}")
            else:
                logger.warning("Rabbit R1: Cloudflare Tunnel unavailable")
            return url

        logger.warning(f"Rabbit R1: Unknown tunnel mode {self._tunnel_mode!r}")
        return None

    # ------------------------------------------------------------------
    # QR code / pairing info
    # ------------------------------------------------------------------

    async def _print_pairing_info(self) -> None:
        """Print pairing instructions and save QR code as PNG."""
        if self._public_url:
            host = self._public_url.replace("wss://", "").replace("ws://", "")
            port = 443
        else:
            host = _get_lan_ip()
            port = self._port

        qr_data = json.dumps({
            "type": "clawdbot-gateway",
            "version": 1,
            "ips": [host],
            "port": port,
            "token": self._token,
            "protocol": "wss" if self._public_url else "ws",
        })

        # Save QR as PNG
        qr_png_path = None
        if QRCODE_AVAILABLE:
            try:
                qr_png_path = os.path.expanduser("~/.hermes/rabbit_r1_qr.png")
                os.makedirs(os.path.dirname(qr_png_path), exist_ok=True)
                qr_img = qrcode.make(qr_data)
                qr_img.save(qr_png_path)
                logger.info(f"Rabbit R1: QR code saved to {qr_png_path}")
            except Exception as e:
                logger.warning(f"Rabbit R1: failed to save QR PNG: {e}")
                qr_png_path = None

        print("\n" + "=" * 60)
        print("  Rabbit R1 - Hermes Gateway")
        print("=" * 60)
        if self._public_url:
            print(f"  Public URL : {self._public_url}")
            print(f"  Works from : anywhere (home, cellular, travelling)")
        else:
            print(f"  Local URL  : ws://{host}:{port}")
            print(f"  Works from : home network only")
        masked = self._token[:6] + "..." + self._token[-4:]
        print(f"  Token      : {masked}")
        if qr_png_path:
            print(f"  QR image   : {qr_png_path}")
        print()
        print("  Scan the QR code below with your Rabbit R1:")
        print()

        if QRCODE_AVAILABLE:
            try:
                qr = qrcode.QRCode(border=1)
                qr.add_data(qr_data)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception:
                print(f"  QR payload : {qr_data}")
        else:
            print(f"  QR payload : {qr_data}")
            print("  (Install 'qrcode' for a visual QR: pip install qrcode)")

        print("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_msg(ws: WebSocketServerProtocol, data: dict) -> None:
        try:
            await ws.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            pass


# ---------------------------------------------------------------------------
# Self-test (no PlatformRegistry — built-in adapter uses if/elif in run.py)
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
