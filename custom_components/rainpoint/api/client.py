"""
RainPoint API client.

This module contains the main RainPointClient class for communicating
with the RainPoint cloud API.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

_LOGGER = logging.getLogger(__name__)


class RainPointApiError(Exception):
    pass



class RainPointClient:
    def __init__(self, area_code: str, email: str, password: str, session: aiohttp.ClientSession):
        self._area_code = area_code
        self._email = email
        self._password = password  # cleartext, HA will store
        self._session = session
        self._app_code = "2"

        _LOGGER.info("RainPointClient initialized with app_code: %s", self._app_code)

        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: datetime | None = None

        # region host: you had region3; we can later make this configurable
        self._base_url = "https://region3.homgarus.com"

    # --- token state helpers ---

    def _auth_headers(self) -> dict:
        """Generate authentication headers for API calls."""
        if not self._token:
            raise RainPointApiError("Token not available")
        return {
            "auth": self._token, 
            "lang": "en", 
            "appCode": self._app_code,  # Hardcoded to RainPoint appCode "2"
            "version": "1.16.1065",
            "sceneType": "1"
        }

    def restore_tokens(self, data: dict) -> None:
        """Restore tokens from config entry data."""
        from ..const import CONF_TOKEN, CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRES_AT

        self._token = data.get(CONF_TOKEN)
        self._refresh_token = data.get(CONF_REFRESH_TOKEN)
        ts = data.get(CONF_TOKEN_EXPIRES_AT)
        if ts is not None:
            try:
                self._token_expires_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                self._token_expires_at = None

    def export_tokens(self) -> dict:
        """Export current token state as a dict for config entry updates."""
        from ..const import CONF_TOKEN, CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRES_AT

        return {
            CONF_TOKEN: self._token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_TOKEN_EXPIRES_AT: int(self._token_expires_at.timestamp()) if self._token_expires_at else None,
        }

    def _token_valid(self) -> bool:
        if not self._token or not self._token_expires_at:
            return False
        # refresh a little before expiry
        return datetime.now(timezone.utc) < (self._token_expires_at - timedelta(minutes=5))

    # --- login / auth ---

    async def ensure_logged_in(self) -> None:
        if self._token_valid():
            return
        await self._login()

    async def _login(self) -> None:
        """Login with areaCode/email/password and store token info."""
        url = f"{self._base_url}/auth/basic/app/login"

        # Client-side MD5 hashing as per app/Postman flow
        md5 = hashlib.md5(self._password.encode("utf-8")).hexdigest()

        # Device ID is required; generate deterministic 16 bytes hex
        device_id = hashlib.md5(f"{self._email}{self._area_code}".encode("utf-8")).hexdigest()

        payload = {
            "areaCode": self._area_code,
            "phoneOrEmail": self._email,
            "password": md5,
            "deviceId": device_id,
        }

        _LOGGER.debug("RainPoint login request for %s with appCode=%s", self._email, self._app_code)

        async with self._session.post(url, json=payload, headers={"Content-Type": "application/json", "lang": "en", "appCode": self._app_code}) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"Login HTTP {resp.status}")
            data = await resp.json()

        if data.get("code") != 0 or "data" not in data:
            _LOGGER.debug("Login failed response: %s", data)
            raise RainPointApiError(f"Login failed: code {data.get('code')}")

        d = data["data"]
        self._token = d["token"]
        self._refresh_token = d.get("refreshToken")
        token_expired_secs = d.get("tokenExpired", 0)
        ts_server = data.get("ts")  # ms since epoch
        if ts_server:
            base = datetime.fromtimestamp(ts_server / 1000, tz=timezone.utc)
        else:
            base = datetime.now(timezone.utc)
        self._token_expires_at = base + timedelta(seconds=token_expired_secs)

        _LOGGER.info("RainPoint login successful; token expires in %s seconds", token_expired_secs)

    # --- API calls ---

    async def list_homes(self) -> list[dict]:
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/member/appHome/list"
        _LOGGER.debug("API call: list_homes URL=%s", url)
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"list_homes HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: list_homes data=%s", data)
        if data.get("code") != 0:
            _LOGGER.debug("list_homes failed response: %s", data)
            raise RainPointApiError(f"list_homes failed: code {data.get('code')}")
        return data.get("data", [])

    async def get_devices_by_hid(self, hid: int) -> list[dict]:
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/getDeviceByHid"
        params = {"hid": hid}
        _LOGGER.debug("API call: get_devices_by_hid URL=%s params=%s", url, params)
        async with self._session.get(url, headers=self._auth_headers(), params=params) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"getDeviceByHid HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_devices_by_hid data=%s", data)
        if data.get("code") != 0:
            _LOGGER.debug("getDeviceByHid failed response: %s", data)
            raise RainPointApiError(f"getDeviceByHid failed: code {data.get('code')}")
        return data.get("data", [])

    async def get_multiple_device_status(self, devices: list[dict]) -> list[dict]:
        """Get status for multiple devices in one API call (more efficient)."""
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/multipleDeviceStatus"

        # Format devices array as expected by API
        device_list = []
        for device in devices:
            device_list.append({
                "deviceName": device.get("deviceName", ""),
                "mid": device["mid"],
                "productKey": device.get("productKey", "")
            })

        payload = {"devices": device_list}
        _LOGGER.debug("API call: get_multiple_device_status URL=%s payload=%s", url, payload)
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"multipleDeviceStatus HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_multiple_device_status data=%s", data)
        if data.get("code") != 0:
            _LOGGER.debug("multipleDeviceStatus failed response: %s", data)
            raise RainPointApiError(f"multipleDeviceStatus failed: code {data.get('code')}")

        # Convert response format to match individual device status format
        # Response has: [{"propVer": X, "status": [...], "mid": Y, "iotId": Z}, ...]
        # We need: [{"mid": Y, "subDeviceStatus": [...]}]
        converted_data = []
        for device_data in data.get("data", []):
            converted_data.append({
                "mid": device_data["mid"],
                "subDeviceStatus": device_data.get("status", [])
            })

        return converted_data

    async def get_device_status(self, mid: int) -> dict:
        """Get status for a single device by MID."""
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/getDeviceStatus"
        params = {"mid": mid}
        _LOGGER.debug("API call: get_device_status URL=%s params=%s", url, params)
        async with self._session.get(url, headers=self._auth_headers(), params=params) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"getDeviceStatus HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_device_status data=%s", data)
        if data.get("code") != 0:
            _LOGGER.debug("getDeviceStatus failed response: %s", data)
            raise RainPointApiError(f"getDeviceStatus failed: code {data.get('code')}")
        return data.get("data", {})

    async def set_device_state(self, home_id: int, device_name: str, mid: int, product_key: str, state: dict) -> bool:
        """Set device state."""
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/setDeviceStatus"
        payload = {
            "homeId": home_id,
            "deviceName": device_name,
            "mid": mid,
            "productKey": product_key,
            "status": state,
        }
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"Failed to set device state: {resp.status}")
            data = await resp.json()
            if data.get("code") != 0:
                raise RainPointApiError(f"Set device state API error: {data.get('msg')}")
            return True

    async def control_work_mode(
        self,
        mid: int,
        addr: int,
        device_name: str,
        product_key: str,
        port: int,
        mode: int,
        duration: int,
    ) -> str | None:
        """Open or close a valve zone on a hub sub-device.

        Args:
            mid: Hub device ID.
            addr: Sub-device address (e.g. 1 for the first RF valve).
            device_name: Hub deviceName (MAC-based identifier).
            product_key: Hub productKey.
            port: Zone/port number (1-based).
            mode: 1 = open, 0 = close.
            duration: Run time in seconds. Pass 0 when mode=0 — the device ignores
                this field on close commands, but it must still be present in the request.

        Returns:
            The value of ``data["data"]`` if it is a string, or
            ``data["data"]["state"]`` if ``data["data"]`` is a dict. Returns None
            if neither condition produces a value (including when the dict response
            omits the "state" key). Callers should treat None as "no optimistic
            update available" rather than an error. Also returns normally
            (without raising) when the API returns code 4 (device already in
            the requested state); callers cannot distinguish this from a
            code-0 success based on the return value alone.
        """
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/controlWorkMode"
        payload = {
            "mid": mid,
            "addr": addr,
            "deviceName": device_name,
            "productKey": product_key,
            "port": port,
            "mode": mode,
            "duration": duration,
        }
        _LOGGER.debug("API call: control_work_mode URL=%s payload=%s", url, payload)
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"controlWorkMode HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: control_work_mode data=%s", data)

        code = data.get("code")
        if code == 4:
            # Code 4 = device already in requested state or transitioning — not fatal
            _LOGGER.info(
                "controlWorkMode: device already in requested state (code 4, idempotent): %s", data
            )
        elif code != 0:
            _LOGGER.debug("controlWorkMode failed response: %s", data)
            raise RainPointApiError(f"controlWorkMode failed: code {code}")
        resp_data = data.get("data")
        if isinstance(resp_data, dict):
            state = resp_data.get("state")
            if state is None:
                _LOGGER.warning(
                    "controlWorkMode: 'data' dict has no 'state' key; full data: %s", resp_data
                )
            return state
        if isinstance(resp_data, str):
            return resp_data
        if resp_data is not None:
            _LOGGER.warning(
                "controlWorkMode: unexpected 'data' type %s; value: %s",
                type(resp_data).__name__, resp_data,
            )
        else:
            _LOGGER.debug("controlWorkMode: API returned code=0 but no 'data' key; optimistic update skipped")
        return None
