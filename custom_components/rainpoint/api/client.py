"""
RainPoint API client.

This module contains the main RainPointClient class for communicating
with the RainPoint cloud API.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import aiohttp

_LOGGER = logging.getLogger(__name__)

# When the cloud throttles the login endpoint -- an HTTP 403 block or a
# code 9993 "operate too frequently" body -- stop issuing further login
# requests for this long. Every caller (coordinator poll, MQTT credential
# supervisor, config flow) funnels through ensure_logged_in, so this cap
# keeps their combined retry pressure from turning a soft throttle into a
# sustained ban.
_LOGIN_COOLDOWN_SECONDS = 120

# The RainPoint cloud edge (nginx) returns a bare HTTP 403 for Home Assistant's
# default aiohttp User-Agent ("HomeAssistant/<ver> aiohttp/<ver> Python/<ver>")
# before the request reaches the application, so every API call must send a
# different one. The block targets the HomeAssistant User-Agent specifically:
# browser, okhttp, and plain aiohttp User-Agents all pass, so any ordinary
# non-bot value works. okhttp is a common mobile HTTP stack; this is not pinned
# to a captured app version.
_USER_AGENT = "okhttp/4.9.3"

# The cloud returns this application code ("NOT_TOKEN") when the auth token is
# missing or has been invalidated server-side -- which can happen before its
# advertised local expiry, e.g. a login elsewhere under the same deviceId. It
# means "re-authenticate", not "credentials are wrong".
_NOT_TOKEN_CODE = 1001

# The datapoint code controlWorkModeDP carries for a Bluetooth-backed valve
# command. Every committed catalog variant declaring the CTL_BT_WATER control
# identity declares it at this code, so the literal the client sends is
# pinned by the catalog rather than assumed.
_DP_CODE_CTL_BT_WATER = 1


class RainPointApiError(Exception):
    pass


class RainPointThrottledError(RainPointApiError):
    """Login is refused because the server is throttling us, not because the
    credentials are wrong.

    Carries ``retry_after`` (seconds) so callers can surface a "please wait"
    message or back off, instead of treating it as an authentication failure.
    Subclasses RainPointApiError so existing handlers keep working.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _redact_secret(value: str | None) -> str:
    """Render a secret as length + last-4 only -- never the raw value."""
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return f"len={len(value)} <short>"
    return f"len={len(value)} last4={value[-4:]}"


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

        # Serialize logins so concurrent callers (coordinator + MQTT
        # supervisor) coalesce into one request instead of firing several
        # simultaneous login POSTs while the token is invalid.
        self._login_lock = asyncio.Lock()
        # Set when the server throttles the login endpoint; blocks further
        # network login attempts until it passes.
        self._login_cooldown_until: datetime | None = None

        # region host: you had region3; we can later make this configurable
        self._base_url = "https://region3.homgarus.com"

        # Listeners notified when the HTTP layer rotates its token via a
        # re-login (not the initial login). Kept decoupled from any
        # particular subscriber (e.g. the MQTT client) so this module never
        # imports the mqtt layer -- the supervisor must never keep running on
        # credentials the HTTP layer has superseded.
        self._relogin_listeners: list[Callable[[], None]] = []

    # --- token state helpers ---

    def register_relogin_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback fired synchronously after a re-login rotates the token.

        Not fired on the initial login of a session -- only when the token is
        replaced while a previous token was already held.
        """
        self._relogin_listeners.append(callback)

    def _auth_headers(self) -> dict:
        """Generate authentication headers for API calls."""
        if not self._token:
            raise RainPointApiError("Token not available")
        return {
            "auth": self._token,
            "lang": "en",
            "appCode": self._app_code,  # Hardcoded to RainPoint appCode "2"
            "version": "1.16.1065",
            "sceneType": "1",
            "User-Agent": _USER_AGENT,
        }

    def restore_tokens(self, data: dict) -> None:
        """Restore tokens from config entry data."""
        from ..const import CONF_REFRESH_TOKEN, CONF_TOKEN, CONF_TOKEN_EXPIRES_AT

        self._token = data.get(CONF_TOKEN)
        self._refresh_token = data.get(CONF_REFRESH_TOKEN)
        ts = data.get(CONF_TOKEN_EXPIRES_AT)
        if ts is not None:
            try:
                self._token_expires_at = datetime.fromtimestamp(ts, tz=UTC)
            except (TypeError, ValueError, OSError):
                self._token_expires_at = None

    def export_tokens(self) -> dict:
        """Export current token state as a dict for config entry updates."""
        from ..const import CONF_REFRESH_TOKEN, CONF_TOKEN, CONF_TOKEN_EXPIRES_AT

        return {
            CONF_TOKEN: self._token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_TOKEN_EXPIRES_AT: int(self._token_expires_at.timestamp()) if self._token_expires_at else None,
        }

    def _token_valid(self) -> bool:
        if not self._token or not self._token_expires_at:
            return False
        # refresh a little before expiry
        return datetime.now(UTC) < (self._token_expires_at - timedelta(minutes=5))

    def _maybe_invalidate_token(self, code, request_token) -> None:
        """Force a re-login when the server rejects the auth token (code 1001).

        The local expiry is only advisory: a persisted token can be invalidated
        server-side before it expires. Expire -- but keep -- the cached token so
        the next ensure_logged_in re-authenticates while _login still sees a
        prior token and treats the attempt as a relogin, firing the rotation
        listeners (token persistence, MQTT credential refresh) that a genuine
        initial login would skip.

        Only act when the rejected request carried the token that is still
        current: under concurrent requests a slow 1001 for an already-replaced
        token must not invalidate the fresh one.
        """
        if code == _NOT_TOKEN_CODE and request_token is not None and request_token == self._token:
            _LOGGER.info("RainPoint token rejected (NOT_TOKEN); forcing re-login on the next call")
            self._token_expires_at = None

    # --- login / auth ---

    def _cooldown_remaining(self) -> float:
        """Seconds left on the login cooldown, or 0 if not throttled."""
        if self._login_cooldown_until is None:
            return 0.0
        remaining = (self._login_cooldown_until - datetime.now(UTC)).total_seconds()
        return remaining if remaining > 0 else 0.0

    def _enter_login_cooldown(self, reason: str) -> None:
        """Arm the login cooldown after a server throttle signal."""
        self._login_cooldown_until = datetime.now(UTC) + timedelta(seconds=_LOGIN_COOLDOWN_SECONDS)
        _LOGGER.warning(
            "RainPoint login throttled (%s); suppressing login attempts for %ss",
            reason,
            _LOGIN_COOLDOWN_SECONDS,
        )

    async def ensure_logged_in(self) -> None:
        if self._token_valid():
            return
        # Fast-fail without a network call while the server is throttling us.
        remaining = self._cooldown_remaining()
        if remaining > 0:
            raise RainPointThrottledError(f"login throttled by server; cooling down {remaining:.0f}s before retry", remaining)
        async with self._login_lock:
            # Re-check under the lock: a concurrent caller may have already
            # logged in or tripped the cooldown while we waited for the lock.
            if self._token_valid():
                return
            remaining = self._cooldown_remaining()
            if remaining > 0:
                raise RainPointThrottledError(f"login throttled by server; cooling down {remaining:.0f}s before retry", remaining)
            await self._login()

    async def _login(self) -> None:
        """Login with areaCode/email/password and store token info."""
        is_relogin = self._token is not None
        url = f"{self._base_url}/auth/basic/app/login"

        # Client-side MD5 hashing as per app/Postman flow
        # MD5 is mandated by the RainPoint cloud API wire protocol (not at-rest password storage).
        md5 = hashlib.md5(self._password.encode("utf-8"), usedforsecurity=False).hexdigest()

        # Device ID is required; generate deterministic 16 bytes hex
        device_id = hashlib.md5(f"{self._email}{self._area_code}".encode(), usedforsecurity=False).hexdigest()

        payload = {
            "areaCode": self._area_code,
            "phoneOrEmail": self._email,
            "password": md5,
            "deviceId": device_id,
        }

        _LOGGER.debug("RainPoint login request for %s with appCode=%s", self._email, self._app_code)

        login_headers = {
            "Content-Type": "application/json",
            "lang": "en",
            "appCode": self._app_code,
            "User-Agent": _USER_AGENT,
        }
        async with self._session.post(url, json=payload, headers=login_headers) as resp:
            if resp.status == 403:
                # A hard edge/WAF block -- the server is refusing us outright,
                # typically after sustained request volume. Back off hard.
                self._enter_login_cooldown("HTTP 403")
                raise RainPointThrottledError(f"Login HTTP 403; cooling down {_LOGIN_COOLDOWN_SECONDS}s", _LOGIN_COOLDOWN_SECONDS)
            if resp.status != 200:
                raise RainPointApiError(f"Login HTTP {resp.status}")
            data = await resp.json()

        code = data.get("code")
        if code == 9993:
            # Soft rate limit ("operate too frequently"). Honor it before it
            # escalates into a 403.
            self._enter_login_cooldown("code 9993 operate too frequently")
            raise RainPointThrottledError(
                f"Login rate-limited (code 9993); cooling down {_LOGIN_COOLDOWN_SECONDS}s", _LOGIN_COOLDOWN_SECONDS
            )
        if code != 0 or "data" not in data:
            _LOGGER.debug("Login failed response: %s", data)
            raise RainPointApiError(f"Login failed: code {code}")

        # A clean login clears any prior throttle state.
        self._login_cooldown_until = None

        d = data["data"]
        self._token = d["token"]
        self._refresh_token = d.get("refreshToken")
        token_expired_secs = d.get("tokenExpired", 0)
        ts_server = data.get("ts")  # ms since epoch
        base = datetime.fromtimestamp(ts_server / 1000, tz=UTC) if ts_server else datetime.now(UTC)
        self._token_expires_at = base + timedelta(seconds=token_expired_secs)

        _LOGGER.info("RainPoint login successful; token expires in %s seconds", token_expired_secs)

        if is_relogin:
            # Token rotation: notify subscribers (e.g. the MQTT credential
            # supervisor) so they re-fetch rather than keep running on
            # credentials the HTTP layer just superseded. The initial login
            # of a session does not fire -- there is nothing to supersede yet.
            for callback in self._relogin_listeners:
                # Isolate each listener: a raising listener must not propagate out
                # of _login() (called from ensure_logged_in() at the top of every
                # API method) nor skip the listeners registered after it.
                try:
                    callback()
                except Exception:
                    _LOGGER.exception("RainPoint relogin listener raised; continuing")

    # --- API calls ---

    async def list_homes(self) -> list[dict]:
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/member/appHome/list"
        _LOGGER.debug("API call: list_homes URL=%s", url)
        request_token = self._token
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"list_homes HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: list_homes data=%s", data)
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
            _LOGGER.debug("list_homes failed response: %s", data)
            raise RainPointApiError(f"list_homes failed: code {data.get('code')}")
        return data.get("data", [])

    async def get_product_catalog(self) -> list[dict]:
        """Fetch RainPoint's full productModel catalog as a list of model entries.

        Used only by the maintainer refresh script (scripts/refresh_product_catalog.py)
        to regenerate the committed, trimmed catalog snapshot -- the running
        integration never calls this at runtime.

        The endpoint does not return a bare list. Its "data" is an object whose
        "models" key holds the per-model entries, alongside catalog-wide keys
        ("version", "addGroups", "replaceGroups", "codePushKeys") the snapshot
        does not use. This unwraps to the models list so callers get the one
        shape they care about. A "data" that is already a list is passed
        through, so a future response shape that drops the envelope keeps
        working.
        """
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/common/core/productModel"
        _LOGGER.debug("API call: get_product_catalog URL=%s", url)
        request_token = self._token
        async with self._session.get(url, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"get_product_catalog HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_product_catalog code=%s", data.get("code"))
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
            _LOGGER.debug("get_product_catalog failed response: %s", data)
            raise RainPointApiError(f"get_product_catalog failed: code {data.get('code')}")
        payload = data.get("data") or []
        if isinstance(payload, dict):
            payload = payload.get("models") or []
        if not isinstance(payload, list):
            _LOGGER.debug("get_product_catalog returned an unusable data shape: %s", type(payload).__name__)
            return []
        return payload

    async def get_devices_by_hid(self, hid: int) -> list[dict]:
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/getDeviceByHid"
        params = {"hid": hid}
        _LOGGER.debug("API call: get_devices_by_hid URL=%s params=%s", url, params)
        request_token = self._token
        async with self._session.get(url, headers=self._auth_headers(), params=params) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"getDeviceByHid HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_devices_by_hid data=%s", data)
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
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
            device_list.append(
                {"deviceName": device.get("deviceName", ""), "mid": device["mid"], "productKey": device.get("productKey", "")}
            )

        payload = {"devices": device_list}
        _LOGGER.debug("API call: get_multiple_device_status URL=%s payload=%s", url, payload)
        request_token = self._token
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"multipleDeviceStatus HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_multiple_device_status data=%s", data)
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
            _LOGGER.debug("multipleDeviceStatus failed response: %s", data)
            raise RainPointApiError(f"multipleDeviceStatus failed: code {data.get('code')}")

        # Convert response format to match individual device status format
        # Response has: [{"propVer": X, "status": [...], "mid": Y, "iotId": Z}, ...]
        # We need: [{"mid": Y, "subDeviceStatus": [...]}]
        converted_data = []
        for device_data in data.get("data", []):
            converted_data.append({"mid": device_data["mid"], "subDeviceStatus": device_data.get("status", [])})

        return converted_data

    async def get_device_status(self, mid: int) -> dict:
        """Get status for a single device by MID."""
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/getDeviceStatus"
        params = {"mid": mid}
        _LOGGER.debug("API call: get_device_status URL=%s params=%s", url, params)
        request_token = self._token
        async with self._session.get(url, headers=self._auth_headers(), params=params) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"getDeviceStatus HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: get_device_status data=%s", data)
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
            _LOGGER.debug("getDeviceStatus failed response: %s", data)
            raise RainPointApiError(f"getDeviceStatus failed: code {data.get('code')}")
        return data.get("data", {})

    async def get_subscribe_status(self, device_name: str, product_key: str, mid: int, hid) -> dict:
        """Fetch fresh per-session MQTT observer credentials from subscribeStatus.

        device_name/product_key/mid identify the hub (sourced from the hub
        record, not a second login call); hid is the home the hub belongs to.
        The server requires the full subscribe envelope -- hid, hidList, a
        subscribe device list (which must carry the mid), and userInfo -- and
        rejects a bare {deviceName, productKey} with code 9999 "must not be
        null". The response carries deviceSecret; it must never be logged in the
        clear.
        """
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/subscribeStatus"
        hid_str = str(hid)
        payload = {
            "hid": hid_str,
            "hidList": [hid_str],
            "subscribe": [{"deviceName": device_name, "mid": mid, "productKey": product_key}],
            "unsubscribe": [],
            "userInfo": {
                "deviceName": device_name,
                "deviceType": 1,
                "notice": 0,
                "productKey": product_key,
                "pushId": uuid.uuid4().hex,
            },
        }
        _LOGGER.debug(
            "API call: get_subscribe_status URL=%s deviceName=%s productKey=%s mid=%s hid=%s",
            url,
            device_name,
            product_key,
            mid,
            hid_str,
        )
        request_token = self._token
        async with self._session.post(url, json=payload, headers=self._auth_headers()) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"subscribeStatus HTTP {resp.status}")
            data = await resp.json()

        resp_data = data.get("data") or {}
        # The response carries deviceSecret, so log only the key set -- never the
        # secret itself, not even redacted.
        _LOGGER.debug(
            "API response: get_subscribe_status keys=%s",
            sorted(resp_data.keys()),
        )
        if data.get("code") != 0:
            self._maybe_invalidate_token(data.get("code"), request_token)
            _LOGGER.debug("subscribeStatus failed response: code=%s", data.get("code"))
            raise RainPointApiError(f"subscribeStatus failed: code {data.get('code')}")
        return resp_data

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
        request_token = self._token
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"Failed to set device state: {resp.status}")
            data = await resp.json()
            if data.get("code") != 0:
                self._maybe_invalidate_token(data.get("code"), request_token)
                raise RainPointApiError(f"Set device state API error: {data.get('msg')}")
            return True

    async def update_main_param(self, mid: int, param: str) -> bool:
        """Write a hub's top-level `param` blob back to the cloud.

        `param` is the same pipe-delimited string the poll reads off the hub
        record, spliced by the caller so only the field it understands
        changes. Code 0 is the only success verdict; there is no code-4
        idempotent branch here as there is on `control_work_mode` -- code 4 is
        that endpoint's already-in-state signal, never observed on this one,
        and inventing the branch would ship an untested success path.
        """
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/main/update"
        payload = {"mid": mid, "param": param}
        request_token = self._token
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"main/update HTTP {resp.status}")
            data = await resp.json()

        code = data.get("code")
        # `param` is a cloud-supplied string being echoed back to the cloud,
        # exactly the free text the house logging rule keeps out of log
        # lines; only the integer mid and code are logged, matching
        # control_work_mode_dp's redaction rather than control_work_mode's
        # payload dump.
        _LOGGER.debug("API call: update_main_param mid=%s code=%s", mid, code)
        if code != 0:
            self._maybe_invalidate_token(code, request_token)
            raise RainPointApiError(f"main/update failed: code {code}")
        return True

    async def control_work_mode(
        self,
        mid: int,
        addr: int,
        device_name: str,
        product_key: str,
        port: int,
        mode: int,
        duration: int | None = None,
    ) -> str | None:
        """Open or close a valve zone on a hub sub-device, or address the hub itself.

        Args:
            mid: Hub device ID.
            addr: Sub-device address (e.g. 1 for the first RF valve, 0 for the hub itself).
            device_name: Hub deviceName (MAC-based identifier).
            product_key: Hub productKey.
            port: Zone/port number (1-based).
            mode: 1 = open, 0 = close.
            duration: Run time in seconds for an RF valve call. An int is sent as-is,
                including 0: pass 0 on a close, since the device ignores this field on
                close commands but it must still be present in the request. Pass None
                (the default) to omit the "duration" key from the payload entirely,
                which is what a hub-addressed call (addr=0) needs: the app sends no
                duration field there, and there is no evidence an omission and an
                explicit 0 are equivalent at that address.

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
            # The RainPoint app sends this field on every controlWorkMode call
            # (empty for an RF valve) and the server accepts its absence, so
            # this is an alignment change rather than a fix. The hub broadcast
            # one-shot needs param expressible on this same method, so it is
            # added here rather than introduced blind later.
            "param": "",
        }
        # Omitted rather than defaulted to 0: an absent duration and an
        # explicit 0 are different requests, and only the omission matches
        # what the app sends for a hub-addressed (addr=0) call.
        if duration is not None:
            payload["duration"] = duration
        _LOGGER.debug("API call: control_work_mode URL=%s payload=%s", url, payload)
        request_token = self._token
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"controlWorkMode HTTP {resp.status}")
            data = await resp.json()
        _LOGGER.debug("API response: control_work_mode data=%s", data)

        code = data.get("code")
        if code == 4:
            # Code 4 = device already in requested state or transitioning, not fatal
            _LOGGER.info("controlWorkMode: device already in requested state (code 4, idempotent): %s", data)
        elif code != 0:
            self._maybe_invalidate_token(code, request_token)
            _LOGGER.debug("controlWorkMode failed response: %s", data)
            raise RainPointApiError(f"controlWorkMode failed: code {code}")
        resp_data = data.get("data")
        if isinstance(resp_data, dict):
            state = resp_data.get("state")
            if state is None:
                _LOGGER.warning("controlWorkMode: 'data' dict has no 'state' key; full data: %s", resp_data)
            return state
        if isinstance(resp_data, str):
            return resp_data
        if resp_data is not None:
            _LOGGER.warning(
                "controlWorkMode: unexpected 'data' type %s; value: %s",
                type(resp_data).__name__,
                resp_data,
            )
        else:
            _LOGGER.debug("controlWorkMode: API returned code=0 but no 'data' key; optimistic update skipped")
        return None

    async def control_work_mode_dp(
        self,
        mid: int,
        addr: int,
        device_name: str,
        product_key: str,
        port: int,
        mode: int,
        param: str,
    ) -> str | None:
        """Open or close a Bluetooth-backed valve zone over the datapoint control endpoint.

        Args:
            mid: Hub device ID.
            addr: Sub-device address.
            device_name: Hub deviceName (MAC-based identifier).
            product_key: Hub productKey.
            port: Zone/port number (1-based).
            mode: 1 = open, 0 = close.
            param: The pre-encoded 4-byte little-endian hex duration string this
                endpoint reads in place of a ``duration`` field. Callers build it
                with ``_encode_dp_duration_param``; this method sends no
                ``duration`` key at all.

        Returns:
            The value of ``data["data"]`` if it is a string, or
            ``data["data"]["state"]`` if ``data["data"]`` is a dict. Returns None
            if neither condition produces a value (including when the dict
            response omits the "state" key). Callers should treat None as "no
            optimistic update available" rather than an error. Also returns
            normally (without raising) when the API returns code 4 (device
            already in the requested state); callers cannot distinguish this
            from a code-0 success based on the return value alone.
        """
        await self.ensure_logged_in()
        url = f"{self._base_url}/app/device/controlWorkModeDP"
        payload = {
            "mid": mid,
            "productKey": product_key,
            "deviceName": device_name,
            "mode": mode,
            "addr": addr,
            "port": port,
            "param": param,
            "dpCode": _DP_CODE_CTL_BT_WATER,
        }
        # Deliberately does not log the payload or response dict the way
        # control_work_mode's debug lines do: this body carries deviceName
        # (the hub MAC) and productKey, both scrubbed by name in the capture
        # record. Only integers and the caller's own encoded string are
        # logged, per the house rule that a cloud-record log line never
        # carries cloud-supplied free text.
        _LOGGER.debug(
            "API call: control_work_mode_dp URL=%s mode=%s port=%s param=%s",
            url,
            mode,
            port,
            param,
        )
        request_token = self._token
        async with self._session.post(url, headers=self._auth_headers(), json=payload) as resp:
            if resp.status != 200:
                raise RainPointApiError(f"controlWorkModeDP HTTP {resp.status}")
            data = await resp.json()

        code = data.get("code")
        if code == 4:
            # Code 4 = device already in requested state or transitioning, not fatal
            _LOGGER.info("controlWorkModeDP: device already in requested state (code 4, idempotent): code=%s", code)
        elif code != 0:
            self._maybe_invalidate_token(code, request_token)
            _LOGGER.debug("controlWorkModeDP failed response: code=%s", code)
            raise RainPointApiError(f"controlWorkModeDP failed: code {code}")
        resp_data = data.get("data")
        if isinstance(resp_data, dict):
            state = resp_data.get("state")
            if state is None:
                _LOGGER.warning("controlWorkModeDP: 'data' dict has no 'state' key")
            return state
        if isinstance(resp_data, str):
            return resp_data
        if resp_data is not None:
            _LOGGER.warning(
                "controlWorkModeDP: unexpected 'data' type %s",
                type(resp_data).__name__,
            )
        else:
            _LOGGER.debug("controlWorkModeDP: API returned code=0 but no 'data' key; optimistic update skipped")
        return None
