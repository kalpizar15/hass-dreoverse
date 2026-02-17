"""WebSocket client for Dreo real-time state updates.

Uses the app-api login (same as the Dreo mobile app) to obtain a token
that the WebSocket endpoint accepts. The open-api token used for REST
polling does not work with the WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

WEBSOCKET_URL = "wss://wsb-{region}.dreo-tech.com/websocket"
APP_API_URL = "https://app-api-{region}.dreo-tech.com"
APP_LOGIN_PATH = "/api/oauth/login"

# Mobile-app OAuth credentials (same as community pydreo library)
APP_CLIENT_ID = "7de37c362ee54dcf9c4561812309347a"
APP_CLIENT_SECRET = "32dfa0764f25451d99f94e1693498791"
APP_USER_AGENT = "dreo/2.8.2"

PING_INTERVAL = 15
PING_MESSAGE = "2"
RECONNECT_DELAY = 5

# Map token region suffix to API/WebSocket region slug
REGION_MAP: dict[str, str] = {
    "NA": "us",
    "US": "us",
    "EU": "eu",
}


async def async_login_app_api(
    username: str,
    password_hash: str,
    region: str,
) -> str | None:
    """Log in via the app-api to get a token the WebSocket accepts.

    Returns the access token string, or None on failure.
    """
    region_slug = REGION_MAP.get(region.upper(), "us")
    url = f"{APP_API_URL.format(region=region_slug)}{APP_LOGIN_PATH}"
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "ua": APP_USER_AGENT,
        "lang": "en",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.9.1",
    }
    body = {
        "acceptLanguage": "en",
        "client_id": APP_CLIENT_ID,
        "client_secret": APP_CLIENT_SECRET,
        "email": username,
        "encrypt": "ciphertext",
        "grant_type": "email-password",
        "himei": "faede31549d649f58864093158787ec9",
        "password": password_hash,
        "scope": "all",
    }
    params = {"timestamp": int(time.time() * 1000)}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=body,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("App-API login failed with status %s", resp.status)
                    return None
                data = await resp.json()
                if data.get("code") != 0:
                    _LOGGER.warning(
                        "App-API login error: %s",
                        data.get("msg", "unknown"),
                    )
                    return None
                token = data.get("data", {}).get("access_token")
                if token:
                    _LOGGER.info("App-API login succeeded for WebSocket")
                return token
    except (aiohttp.ClientError, TimeoutError):
        _LOGGER.warning("App-API login request failed", exc_info=True)
        return None


class DreoWebSocket:
    """Manage a WebSocket connection for real-time Dreo device updates."""

    def __init__(
        self,
        token: str,
        region: str,
        on_message: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Initialize the WebSocket client."""
        self._token = token
        self._region = REGION_MAP.get(region.upper(), "us")
        self._on_message = on_message
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Return True if WebSocket is open."""
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        """Start the WebSocket connection loop."""
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Gracefully close the WebSocket and clean up."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        """Reconnect loop — keeps retrying until stopped."""
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception:
                _LOGGER.debug(
                    "Dreo WebSocket disconnected, reconnecting in %ss",
                    RECONNECT_DELAY,
                    exc_info=True,
                )
            if self._running:
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_listen(self) -> None:
        """Open a WebSocket and consume messages until closed."""
        timestamp = int(time.time() * 1000)
        url = (
            f"{WEBSOCKET_URL.format(region=self._region)}"
            f"?accessToken={self._token}&timestamp={timestamp}"
        )

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url)
            _LOGGER.info("Dreo WebSocket connected to %s region", self._region)

            ping_task = asyncio.create_task(self._ping_loop())
            try:
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._process_message(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                ping_task.cancel()
        finally:
            if self._session and not self._session.closed:
                await self._session.close()
            self._ws = None
            self._session = None

    async def _ping_loop(self) -> None:
        """Send periodic text pings to keep the connection alive."""
        try:
            while self._ws and not self._ws.closed:
                await self._ws.send_str(PING_MESSAGE)
                await asyncio.sleep(PING_INTERVAL)
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _process_message(self, raw: str) -> None:
        """Parse an incoming WebSocket message and dispatch."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        device_sn: str | None = data.get("devicesn")
        reported: dict[str, Any] | None = data.get("reported")

        if device_sn and isinstance(reported, dict) and reported:
            _LOGGER.debug(
                "WebSocket push for %s: %s",
                device_sn,
                reported,
            )
            self._on_message(device_sn, reported)
