"""Tests for the RainPoint API client."""

import asyncio
import hashlib
import importlib.util
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.rainpoint.api.product_catalog as product_catalog
from custom_components.rainpoint.api import RainPointApiError, RainPointClient, RainPointThrottledError
from custom_components.rainpoint.api.client import _USER_AGENT, _redact_secret
from tests.helpers import make_mock_session_client, mock_json_response

# scripts/ is not a package (it's a standalone maintainer-tool directory, not
# shipped inside custom_components/), so it is loaded here via importlib
# rather than a normal import.
_REFRESH_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_product_catalog.py"
_refresh_spec = importlib.util.spec_from_file_location("refresh_product_catalog", _REFRESH_SCRIPT_PATH)
refresh_product_catalog = importlib.util.module_from_spec(_refresh_spec)
_refresh_spec.loader.exec_module(refresh_product_catalog)
trim_catalog = refresh_product_catalog.trim_catalog


def _make_client() -> RainPointClient:
    """Delegate to tests.helpers.make_mock_session_client (the one implementation)."""
    return make_mock_session_client()


def _mock_response(json_data: dict, status: int = 200) -> AsyncMock:
    """Delegate to tests.helpers.mock_json_response (the one implementation)."""
    return mock_json_response(json_data, status)


class TestControlWorkModeCode4:
    """controlWorkMode must treat response code 4 as success, not error.

    Code 4 means the device is already in the requested state. This was a
    real bug -- the client used to raise RainPointApiError on code 4, causing
    spurious failures when toggling a valve that was already open/closed.
    """

    def _make_client(self) -> RainPointClient:
        """Make client helper."""
        return _make_client()

    def _mock_response(self, json_data: dict, status: int = 200) -> AsyncMock:
        """Mock response helper."""
        return _mock_response(json_data, status)

    @pytest.mark.asyncio
    async def test_control_work_mode_code_4_is_success(self):
        """Code 4 with data.state returns normally (no exception)."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 4,
            "msg": "device already in requested state",
            "data": {"state": "11#somestate"},
        }
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        # Must NOT raise
        result = await client.control_work_mode(
            mid=123,
            addr=1,
            device_name="AABBCCDD",
            product_key="pk123",
            port=1,
            mode=1,
            duration=300,
        )
        assert result == "11#somestate"

    @pytest.mark.asyncio
    async def test_control_work_mode_code_4_no_data_returns_none(self):
        """Code 4 with no 'data' key returns None (not an error)."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 4, "msg": "already in state"}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        result = await client.control_work_mode(
            mid=123,
            addr=1,
            device_name="AABBCCDD",
            product_key="pk123",
            port=1,
            mode=1,
            duration=300,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_control_work_mode_other_error_code_raises(self):
        """Non-zero, non-4 code raises RainPointApiError."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 5, "msg": "real error"}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        with pytest.raises(RainPointApiError, match="controlWorkMode failed"):
            await client.control_work_mode(
                mid=123,
                addr=1,
                device_name="AABBCCDD",
                product_key="pk123",
                port=1,
                mode=1,
                duration=300,
            )

    @pytest.mark.asyncio
    async def test_control_work_mode_unexpected_data_type_returns_none(self):
        """An int 'data' field hits the unexpected-type warning branch and returns None."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0, "data": 12345}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        result = await client.control_work_mode(
            mid=123,
            addr=1,
            device_name="AABBCCDD",
            product_key="pk123",
            port=1,
            mode=1,
            duration=300,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_control_work_mode_http_error_raises(self):
        """An HTTP 500 status raises controlWorkMode HTTP 500."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.post = MagicMock(return_value=self._mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="controlWorkMode HTTP 500"):
            await client.control_work_mode(
                mid=1,
                addr=1,
                device_name="X",
                product_key="pk",
                port=1,
                mode=1,
                duration=0,
            )

    @pytest.mark.asyncio
    async def test_control_work_mode_dict_without_state_returns_none(self):
        """A 'data' dict missing the 'state' key logs a warning and returns None."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        # dict without "state" key should return None (not raise)
        json_body = {"code": 0, "data": {"other": "x"}}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        result = await client.control_work_mode(
            mid=1,
            addr=1,
            device_name="X",
            product_key="pk",
            port=1,
            mode=0,
            duration=0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_control_work_mode_string_data_returned_directly(self):
        """A plain string 'data' is returned as-is."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0, "data": "11#AABBCC"}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        result = await client.control_work_mode(
            mid=1,
            addr=1,
            device_name="X",
            product_key="pk",
            port=1,
            mode=1,
            duration=60,
        )
        assert result == "11#AABBCC"

    @pytest.mark.asyncio
    async def test_control_work_mode_body_carries_empty_param(self):
        """The RF path now sends param alongside duration, matching observed app traffic."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.post = MagicMock(return_value=self._mock_response({"code": 0, "data": "11#state"}))

        await client.control_work_mode(
            mid=1,
            addr=1,
            device_name="X",
            product_key="pk",
            port=1,
            mode=1,
            duration=60,
        )

        body = client._session.post.call_args.kwargs["json"]
        assert body["param"] == ""
        assert body["duration"] == 60


class TestControlWorkModeDp:
    """controlWorkModeDP's full verdict matrix, mirroring TestControlWorkModeCode4's shape."""

    def _make_client(self) -> RainPointClient:
        """Make client helper."""
        return _make_client()

    def _mock_response(self, json_data: dict, status: int = 200) -> AsyncMock:
        """Mock response helper."""
        return _mock_response(json_data, status)

    @pytest.mark.asyncio
    async def test_close_posts_zeroed_param_with_no_duration_key(self):
        """A close (mode=0) posts param '00000000' and no duration key at all."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        json_body = {"code": 0, "data": {"state": "0,D800AF00000000B700000000"}}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        await client.control_work_mode_dp(
            mid=1,
            addr=3,
            device_name="MAC-x",
            product_key="pk",
            port=1,
            mode=0,
            param="00000000",
        )

        body = client._session.post.call_args.kwargs["json"]
        assert body["mode"] == 0
        assert body["param"] == "00000000"
        assert "duration" not in body

    @pytest.mark.asyncio
    async def test_code_0_dict_state_returns_state(self):
        """A code-0 response whose data is a dict returns data['state']."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(
            return_value=self._mock_response({"code": 0, "data": {"state": "1,D821AF3C000000B7D1230B1A"}})
        )

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
        )

        assert result == "1,D821AF3C000000B7D1230B1A"

    @pytest.mark.asyncio
    async def test_code_4_with_state_returns_normally(self):
        """Code 4 (already in requested state) returns the state blob without raising."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(
            return_value=self._mock_response({"code": 4, "data": {"state": "0,D800AF00000000B700000000"}})
        )

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=0, param="00000000"
        )

        assert result == "0,D800AF00000000B700000000"

    @pytest.mark.asyncio
    async def test_other_error_code_raises(self):
        """A non-zero, non-4 code raises RainPointApiError with a controlWorkModeDP prefix."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 3, "msg": "wrong endpoint"}))

        with pytest.raises(RainPointApiError, match="controlWorkModeDP failed: code 3"):
            await client.control_work_mode_dp(
                mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
            )

    @pytest.mark.asyncio
    async def test_non_token_error_code_raises_and_leaves_the_token_alone(self):
        """A non-zero code that is not the token-rejection code raises without expiring the token.

        Pairs with the NOT_TOKEN case below. Only the token-rejection code may
        force a re-login, so an unrelated server error must leave both the
        cached token and its expiry untouched: expiring on any error would make
        every transient failure cost a round trip to re-authenticate.
        """
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 1004, "msg": "some other error"}))
        request_token = client._token
        expires_at = client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(RainPointApiError, match="controlWorkModeDP failed: code 1004"):
            await client.control_work_mode_dp(
                mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
            )

        assert client._token == request_token
        assert client._token_expires_at == expires_at

    @pytest.mark.asyncio
    async def test_not_token_code_expires_the_token_then_raises(self):
        """The token-rejection code expires the request's own token, keeps it, then raises.

        The kept-but-expired shape is deliberate and is what makes this endpoint
        match the RF path: clearing the token outright would make the next login
        look like a first login, skipping the rotation listeners that persist the
        new token and refresh the push credentials.
        """
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 1001, "msg": "NOT_TOKEN"}))
        request_token = client._token
        client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(RainPointApiError, match="controlWorkModeDP failed: code 1001"):
            await client.control_work_mode_dp(
                mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
            )

        assert client._token_expires_at is None
        assert client._token == request_token

    @pytest.mark.asyncio
    async def test_not_token_code_for_a_superseded_token_leaves_the_current_one(self):
        """A late token rejection for an already-replaced token must not expire the fresh one.

        Under concurrent requests a slow rejection can arrive after another call
        has already re-authenticated. Expiring on it would throw away a token
        the server never rejected, and each such rejection would expire the
        replacement in turn.
        """
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        expires_at = client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)

        def _replace_token_mid_flight(*_args, **_kwargs):
            client._token = "a-freshly-rotated-token"
            return self._mock_response({"code": 1001, "msg": "NOT_TOKEN"})

        client._session.post = MagicMock(side_effect=_replace_token_mid_flight)

        with pytest.raises(RainPointApiError, match="controlWorkModeDP failed: code 1001"):
            await client.control_work_mode_dp(
                mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
            )

        assert client._token == "a-freshly-rotated-token"
        assert client._token_expires_at == expires_at

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        """A non-200 HTTP status raises RainPointApiError with a controlWorkModeDP HTTP prefix."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="controlWorkModeDP HTTP 500"):
            await client.control_work_mode_dp(
                mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
            )

    @pytest.mark.asyncio
    async def test_dict_without_state_key_warns_and_returns_none(self):
        """A code-0 dict response missing 'state' warns and returns None."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 0, "data": {"other": "x"}}))

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_unexpected_data_type_warns_and_returns_none(self):
        """A code-0 response whose data is neither dict nor str warns and returns None."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 0, "data": 12345}))

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_string_data_returned_directly(self):
        """A plain string 'data' is returned as-is, without the dict-shape branch."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 0, "data": "1,D800AF00000000B700000000"}))

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=0, param="00000000"
        )

        assert result == "1,D800AF00000000B700000000"

    @pytest.mark.asyncio
    async def test_no_data_key_returns_none(self):
        """A code-0 response with no 'data' key at all returns None without warning."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 0}))

        result = await client.control_work_mode_dp(
            mid=1, addr=3, device_name="MAC-x", product_key="pk", port=1, mode=1, param="3C000000"
        )

        assert result is None


class TestLogin:
    """Tests for the _login method including MD5 hashing and token storage."""

    @pytest.mark.asyncio
    async def test_login_success(self):
        """Successful login stores token, refresh token, and exact expiry."""
        client = _make_client()
        client._token = None  # Reset so we're actually testing login

        ts_ms = 1700000000000
        token_expired = 3600
        json_body = {
            "code": 0,
            "data": {
                "token": "tok123",
                "refreshToken": "ref456",
                "tokenExpired": token_expired,
            },
            "ts": ts_ms,
        }
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        await client._login()

        assert client._token == "tok123"
        assert client._refresh_token == "ref456"
        # Expiry is deterministic: server ts + tokenExpired seconds
        expected_expires_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) + timedelta(seconds=token_expired)
        assert client._token_expires_at == expected_expires_at

    @pytest.mark.asyncio
    async def test_login_http_error(self):
        """HTTP 401 raises RainPointApiError with Login HTTP 401."""
        client = _make_client()
        client._token = None

        client._session.post = MagicMock(return_value=_mock_response({}, status=401))

        with pytest.raises(RainPointApiError, match="Login HTTP 401"):
            await client._login()

    @pytest.mark.asyncio
    async def test_login_api_error_code(self):
        """Non-zero API code raises RainPointApiError with code info."""
        client = _make_client()
        client._token = None

        json_body = {"code": 1, "msg": "bad creds"}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="Login failed: code 1"):
            await client._login()

    @pytest.mark.asyncio
    async def test_login_no_data_key_raises(self):
        """A 200 response with code 0 but no 'data' key still raises Login failed."""
        client = _make_client()
        client._token = None

        # Code 0 alone is not enough: login expects the 'data' envelope too.
        json_body = {"code": 0}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="Login failed"):
            await client._login()

    @pytest.mark.asyncio
    async def test_login_md5_password(self):
        """Login payload contains MD5-hashed password."""
        client = _make_client()
        client._token = None

        json_body = {
            "code": 0,
            "data": {"token": "tok", "refreshToken": "ref", "tokenExpired": 3600},
            "ts": 1700000000000,
        }
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        await client._login()

        # Extract the payload passed to session.post
        call_kwargs = client._session.post.call_args
        payload = call_kwargs.kwargs.get("json")
        if payload is None:
            _args, kwargs = call_kwargs
            payload = kwargs.get("json")

        expected_md5 = hashlib.md5(b"testpass").hexdigest()
        assert expected_md5 == "179ad45c6ce2cb97cf1029e212046e81"
        assert payload["password"] == expected_md5

        # Login must also send the app-like User-Agent; the default one is 403'd.
        headers = call_kwargs.kwargs.get("headers")
        assert headers["User-Agent"] == _USER_AGENT

    @pytest.mark.asyncio
    async def test_login_device_id_deterministic(self):
        """Login payload deviceId is deterministic MD5 of email+area_code."""
        client = _make_client()
        client._token = None

        json_body = {
            "code": 0,
            "data": {"token": "tok", "refreshToken": "ref", "tokenExpired": 3600},
            "ts": 1700000000000,
        }
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        await client._login()

        _args, kwargs = client._session.post.call_args
        payload = kwargs.get("json")

        # email="test@example.com", area_code="1" => MD5("test@example.com1")
        expected_device_id = hashlib.md5(b"test@example.com1").hexdigest()
        assert payload["deviceId"] == expected_device_id


class TestLoginThrottling:
    """Login throttle handling: cooldown on 403 / code 9993, and the login lock.

    Both the coordinator poll and the MQTT credential supervisor funnel through
    ensure_logged_in. When the cloud throttles the login endpoint, the client
    must stop hammering it -- otherwise their combined retries turn a soft
    rate limit into a sustained ban.
    """

    @staticmethod
    def _ok_body() -> dict:
        # Far-future server ts so the stored token reads as valid (not expired)
        # in the concurrency test, where later callers must short-circuit.
        return {
            "code": 0,
            "data": {"token": "tok", "refreshToken": "ref", "tokenExpired": 3600},
            "ts": 4102444800000,
        }

    @pytest.mark.asyncio
    async def test_403_arms_cooldown(self):
        """An HTTP 403 arms the cooldown so later attempts fast-fail."""
        client = _make_client()
        client._token = None
        client._session.post = MagicMock(return_value=_mock_response({}, status=403))

        with pytest.raises(RainPointThrottledError, match="Login HTTP 403") as exc:
            await client._login()

        assert exc.value.retry_after > 0
        assert client._cooldown_remaining() > 0

    @pytest.mark.asyncio
    async def test_code_9993_arms_cooldown(self):
        """A code 9993 'operate too frequently' body arms the cooldown."""
        client = _make_client()
        client._token = None
        json_body = {"code": 9993, "msg": "operate too frequently"}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointThrottledError, match="rate-limited") as exc:
            await client._login()

        assert exc.value.retry_after > 0
        assert client._cooldown_remaining() > 0

    @pytest.mark.asyncio
    async def test_ensure_logged_in_fast_fails_during_cooldown(self):
        """While cooling down, ensure_logged_in raises without any network call."""
        client = _make_client()
        client._token = None
        client._login_cooldown_until = datetime.now(UTC) + timedelta(seconds=60)
        client._session.post = MagicMock()

        with pytest.raises(RainPointThrottledError, match="throttled by server") as exc:
            await client.ensure_logged_in()

        assert exc.value.retry_after > 0
        client._session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_retry(self):
        """A cooldown in the past no longer blocks a login attempt."""
        client = _make_client()
        client._token = None
        client._login_cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        client._session.post = MagicMock(return_value=_mock_response(self._ok_body()))

        await client.ensure_logged_in()

        assert client._token == "tok"

    @pytest.mark.asyncio
    async def test_successful_login_clears_cooldown(self):
        """A clean login clears any prior throttle state."""
        client = _make_client()
        client._token = None
        client._login_cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        client._session.post = MagicMock(return_value=_mock_response(self._ok_body()))

        await client._login()

        assert client._login_cooldown_until is None

    @pytest.mark.asyncio
    async def test_concurrent_callers_coalesce_into_one_login(self):
        """Under genuine contention, concurrent callers coalesce into exactly
        one login. The first holds the lock (gated on an Event) while the others
        queue on it, so a broken lock would let more than one login through."""
        import asyncio

        client = _make_client()
        client._token = None
        release = asyncio.Event()
        login_calls = 0

        async def fake_login():
            nonlocal login_calls
            login_calls += 1
            await release.wait()  # hold the lock until every other caller has queued
            client._token = "tok"
            client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)

        client._login = fake_login
        first = asyncio.create_task(client.ensure_logged_in())
        await asyncio.sleep(0)  # first acquires the lock, blocks on release
        others = [asyncio.create_task(client.ensure_logged_in()) for _ in range(4)]
        await asyncio.sleep(0)  # the other four run and block on the held lock
        release.set()
        await asyncio.gather(first, *others)

        assert login_calls == 1

    @pytest.mark.asyncio
    async def test_in_lock_recheck_short_circuits_on_token(self):
        """A caller queued on the lock returns without a second login once the
        first caller has established a valid token."""
        import asyncio

        client = _make_client()
        client._token = None
        release = asyncio.Event()
        login_calls = 0

        async def fake_login():
            nonlocal login_calls
            login_calls += 1
            # Hold the lock until the test releases it, so the second caller is
            # guaranteed to be queued on the lock before this one finishes.
            await release.wait()
            client._token = "tok"
            client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)

        client._login = fake_login
        first = asyncio.create_task(client.ensure_logged_in())
        await asyncio.sleep(0)  # first acquires the lock, blocks on release
        second = asyncio.create_task(client.ensure_logged_in())
        await asyncio.sleep(0)  # second runs, blocks on the held lock
        release.set()
        await asyncio.gather(first, second)  # second re-checks token, returns

        # The in-lock recheck must short-circuit the second caller: exactly one
        # login, even though both callers passed the top-level check while the
        # token was still unset.
        assert login_calls == 1

    @pytest.mark.asyncio
    async def test_in_lock_recheck_honors_cooldown(self):
        """A caller queued on the lock fast-fails if the first caller armed the
        cooldown while holding it."""
        import asyncio

        client = _make_client()
        client._token = None
        release = asyncio.Event()

        async def fake_login():
            await release.wait()
            client._enter_login_cooldown("test")
            raise RainPointApiError("throttled")

        client._login = fake_login
        first = asyncio.create_task(client.ensure_logged_in())
        await asyncio.sleep(0)
        second = asyncio.create_task(client.ensure_logged_in())
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert all(isinstance(r, RainPointApiError) for r in results)
        assert "throttled" in str(results[0])
        assert "cooling down" in str(results[1])


class TestReloginListeners:
    """Re-login notifies registered listeners; initial login does not."""

    @staticmethod
    def _login_json_body() -> dict:
        return {
            "code": 0,
            "data": {"token": "tok", "refreshToken": "ref", "tokenExpired": 3600},
            "ts": 1700000000000,
        }

    @pytest.mark.asyncio
    async def test_initial_login_does_not_fire_listener(self):
        """The first _login() of a session (no prior token) does not invoke listeners."""
        client = _make_client()
        client._token = None  # no prior token => this is the initial login

        listener = MagicMock()
        client.register_relogin_listener(listener)

        client._session.post = MagicMock(return_value=_mock_response(self._login_json_body()))

        await client._login()

        listener.assert_not_called()

    @pytest.mark.asyncio
    async def test_relogin_fires_listener_exactly_once(self):
        """A second _login() while a token is already held (re-login) fires the listener once."""
        client = _make_client()
        # _make_client() already sets a token, so this call is a re-login.
        assert client._token is not None

        listener = MagicMock()
        client.register_relogin_listener(listener)

        client._session.post = MagicMock(return_value=_mock_response(self._login_json_body()))

        await client._login()

        listener.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_relogin_fires_all_registered_listeners(self):
        """Multiple registered listeners all fire on re-login."""
        client = _make_client()
        assert client._token is not None

        listener_one = MagicMock()
        listener_two = MagicMock()
        client.register_relogin_listener(listener_one)
        client.register_relogin_listener(listener_two)

        client._session.post = MagicMock(return_value=_mock_response(self._login_json_body()))

        await client._login()

        listener_one.assert_called_once_with()
        listener_two.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_relogin_listener_exception_is_isolated(self, caplog):
        """A raising listener does not propagate out of _login() and does not
        prevent later-registered listeners from firing."""
        client = _make_client()
        assert client._token is not None

        raising = MagicMock(side_effect=RuntimeError("listener boom"))
        after = MagicMock()
        client.register_relogin_listener(raising)
        client.register_relogin_listener(after)

        client._session.post = MagicMock(return_value=_mock_response(self._login_json_body()))

        with caplog.at_level(logging.ERROR):
            await client._login()  # must not raise despite the raising listener

        raising.assert_called_once_with()
        after.assert_called_once_with()  # a listener after the raising one still fires
        assert any("relogin listener raised" in r.message for r in caplog.records)


class TestTokenManagement:
    """Tests for token lifecycle: validity checks, restore, export, ensure_logged_in."""

    def test_token_valid_when_fresh(self):
        """Token is valid when expiry is in the future."""
        client = _make_client()
        client._token = "tok"
        client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        assert client._token_valid() is True

    def test_token_invalid_when_expired(self):
        """Token is invalid when expiry is in the past."""
        client = _make_client()
        client._token = "tok"
        client._token_expires_at = datetime.now(UTC) - timedelta(hours=1)
        assert client._token_valid() is False

    def test_token_invalid_when_none(self):
        """Token is invalid when not set."""
        client = _make_client()
        client._token = None
        client._token_expires_at = None
        assert client._token_valid() is False

    def test_token_invalid_near_expiry(self):
        """Token is invalid when within 5-minute buffer of expiry."""
        client = _make_client()
        client._token = "tok"
        # 3 minutes from now, within the 5-min buffer
        client._token_expires_at = datetime.now(UTC) + timedelta(minutes=3)
        assert client._token_valid() is False

    def test_restore_tokens(self):
        """restore_tokens sets _token, _refresh_token, and _token_expires_at."""
        client = _make_client()
        client._token = None

        client.restore_tokens(
            {
                "token": "t1",
                "refresh_token": "r1",
                "token_expires_at": 1700000000,
            }
        )

        assert client._token == "t1"
        assert client._refresh_token == "r1"
        assert client._token_expires_at is not None
        assert isinstance(client._token_expires_at, datetime)

    def test_restore_tokens_missing_fields(self):
        """restore_tokens with empty dict leaves token and expiry as None."""
        client = _make_client()
        client._token = None

        client.restore_tokens({})

        assert client._token is None
        assert client._token_expires_at is None

    def test_restore_tokens_bad_timestamp_falls_back_to_none(self):
        """A non-numeric token_expires_at is caught and _token_expires_at stays None."""
        from custom_components.rainpoint.const import (
            CONF_REFRESH_TOKEN,
            CONF_TOKEN,
            CONF_TOKEN_EXPIRES_AT,
        )

        client = _make_client()
        client._token = None
        client._token_expires_at = None

        client.restore_tokens(
            {
                CONF_TOKEN: "t",
                CONF_REFRESH_TOKEN: "r",
                CONF_TOKEN_EXPIRES_AT: "not-a-number",
            }
        )

        assert client._token == "t"
        assert client._refresh_token == "r"
        assert client._token_expires_at is None

    def test_export_tokens(self):
        """export_tokens returns dict with token, refresh_token, and int timestamp."""
        client = _make_client()
        client._token = "t1"
        client._refresh_token = "r1"
        client._token_expires_at = datetime(2024, 1, 1, tzinfo=UTC)

        result = client.export_tokens()

        assert result["token"] == "t1"
        assert result["refresh_token"] == "r1"
        assert isinstance(result["token_expires_at"], int)

    @pytest.mark.asyncio
    async def test_ensure_logged_in_skips_when_valid(self):
        """ensure_logged_in does not call _login when token is valid."""
        client = _make_client()
        client._token = "tok"
        client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        client._login = AsyncMock()

        await client.ensure_logged_in()

        client._login.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_logged_in_calls_login_when_invalid(self):
        """ensure_logged_in calls _login when no valid token exists."""
        client = _make_client()
        client._token = None
        client._token_expires_at = None
        client._login = AsyncMock()

        await client.ensure_logged_in()

        client._login.assert_awaited_once()


class TestNotTokenInvalidation:
    """A NOT_TOKEN (code 1001) response forces a re-login on the next call while
    keeping the token (so _login treats it as a rotation and fires its
    listeners), and never invalidates a token a concurrent request replaced."""

    @pytest.mark.asyncio
    async def test_not_token_forces_relogin_but_keeps_token(self):
        """code 1001 expires the token (forcing re-login) but keeps it so the
        subsequent _login is a rotation, not an initial login."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()
        client._token = "stale-token"
        client._token_expires_at = datetime.now(UTC) + timedelta(days=60)
        client._session.get = MagicMock(return_value=_mock_response({"code": 1001, "msg": "NOT_TOKEN"}))

        with pytest.raises(RainPointApiError, match="code 1001"):
            await client.get_devices_by_hid(182509)

        assert client._token == "stale-token"  # kept -> is_relogin true, listeners fire
        assert client._token_expires_at is None  # expired -> ensure_logged_in re-authenticates
        assert client._token_valid() is False

    def test_maybe_invalidate_expires_matching_token(self):
        """The token used by the failed request is expired but retained."""
        client = _make_client()
        client._token = "T1"
        client._token_expires_at = datetime.now(UTC) + timedelta(days=60)

        client._maybe_invalidate_token(1001, "T1")

        assert client._token == "T1"
        assert client._token_expires_at is None

    def test_maybe_invalidate_ignores_superseded_token(self):
        """A slow 1001 for a token already replaced by a relogin is ignored."""
        client = _make_client()
        client._token = "T2"
        fresh_expiry = datetime.now(UTC) + timedelta(days=60)
        client._token_expires_at = fresh_expiry

        client._maybe_invalidate_token(1001, "T1")  # request carried the old token

        assert client._token == "T2"
        assert client._token_expires_at == fresh_expiry  # fresh token untouched

    def test_maybe_invalidate_noop_on_non_auth_code(self):
        """A non-1001 code never touches the token."""
        client = _make_client()
        client._token = "T1"
        expiry = datetime.now(UTC) + timedelta(days=60)
        client._token_expires_at = expiry

        client._maybe_invalidate_token(1, "T1")

        assert client._token == "T1"
        assert client._token_expires_at == expiry


class TestAuthHeaders:
    """Tests for _auth_headers method."""

    def test_auth_headers_with_token(self):
        """_auth_headers returns dict with auth token and appCode."""
        client = _make_client()
        client._token = "mytoken"

        headers = client._auth_headers()

        assert headers["auth"] == "mytoken"
        assert headers["appCode"] == "2"
        # The RainPoint edge 403s Home Assistant's default User-Agent, so every
        # authed call must present the app-like one.
        assert headers["User-Agent"] == _USER_AGENT
        assert "HomeAssistant" not in _USER_AGENT

    def test_auth_headers_no_token_raises(self):
        """_auth_headers raises RainPointApiError when no token is set."""
        client = _make_client()
        client._token = None

        with pytest.raises(RainPointApiError):
            client._auth_headers()


class TestListHomes:
    """Tests for list_homes API method."""

    @pytest.mark.asyncio
    async def test_list_homes_success(self):
        """list_homes returns list of homes on success."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0, "data": [{"hid": 1, "homeName": "Home"}]}
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        result = await client.list_homes()

        assert len(result) == 1
        assert result[0]["hid"] == 1

    @pytest.mark.asyncio
    async def test_list_homes_http_error(self):
        """list_homes raises RainPointApiError on HTTP 500."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.get = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="list_homes HTTP 500"):
            await client.list_homes()

    @pytest.mark.asyncio
    async def test_list_homes_api_error(self):
        """list_homes raises RainPointApiError on non-zero API code."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 2}
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="list_homes failed: code 2"):
            await client.list_homes()


class TestGetProductCatalog:
    """Tests for get_product_catalog API method (maintainer refresh tooling only)."""

    @pytest.mark.asyncio
    async def test_get_product_catalog_success(self):
        """get_product_catalog opens with ensure_logged_in and returns data on code 0."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 0,
            "data": [{"model": "HTV245FRF", "dp": [{"dpCode": 9, "identity": "STA_TEM"}]}],
        }
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        result = await client.get_product_catalog()

        client.ensure_logged_in.assert_awaited_once()
        assert result == json_body["data"]

    @pytest.mark.asyncio
    async def test_get_product_catalog_unwraps_the_models_envelope(self):
        """The live endpoint wraps the model list in an object; the list is what callers want.

        The real response is {"models": [...], "version": ..., "addGroups": [],
        "replaceGroups": [], "codePushKeys": []}. Returning that object
        unchanged makes trim_catalog iterate dict keys and fail on the first
        entry, which is exactly what shipped before this test existed.
        """
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        models = [{"model": "HTV245FRF", "dp": [{"dpCode": 9, "identity": "STA_TEM"}]}]
        json_body = {
            "code": 0,
            "data": {
                "models": models,
                "version": 1784773919607,
                "addGroups": [],
                "replaceGroups": [],
                "codePushKeys": [],
            },
        }
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        assert await client.get_product_catalog() == models

    @pytest.mark.asyncio
    async def test_get_product_catalog_envelope_without_models_is_empty(self):
        """An envelope carrying no models list yields [], not a raise."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.get = MagicMock(return_value=_mock_response({"code": 0, "data": {"version": 1}}))

        assert await client.get_product_catalog() == []

    @pytest.mark.asyncio
    async def test_get_product_catalog_unusable_data_shape_is_empty(self):
        """A data payload that is neither list nor object degrades to [] rather than raising.

        The refresh script already refuses to write an empty catalog, so an
        empty return is a safe failure; a raise mid-pull is not.
        """
        client = _make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.get = MagicMock(return_value=_mock_response({"code": 0, "data": "nonsense"}))

        assert await client.get_product_catalog() == []

    @pytest.mark.asyncio
    async def test_get_product_catalog_api_error_code(self):
        """A non-zero API code raises RainPointApiError."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 1, "msg": "bad"}
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="get_product_catalog failed: code 1"):
            await client.get_product_catalog()

    @pytest.mark.asyncio
    async def test_get_product_catalog_http_error(self):
        """A non-200 HTTP status raises RainPointApiError."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.get = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="get_product_catalog HTTP 500"):
            await client.get_product_catalog()


class TestTrimCatalog:
    """Tests for scripts/refresh_product_catalog.py::trim_catalog (pure transform, no network)."""

    def test_drops_non_rainpoint_model(self):
        """A model without one of the fork's prefixes is dropped entirely."""
        raw = [
            {"model": "SOMEOTHERBRAND", "dp": [{"dpCode": 1, "identity": "STA_TEM"}]},
        ]

        result = trim_catalog(raw)

        assert result == {}

    def test_keeps_only_the_five_dp_fields(self):
        """A kept RainPoint model's dp entries keep exactly the five needed fields.

        portNumber is deliberately not among them: RainPoint declares it once
        per model, not per dp entry, so it lives on the variant record.
        """
        raw = [
            {
                "model": "HTV245FRF",
                "portNumber": 1,
                "dp": [
                    {
                        "dpCode": 9,
                        "identity": "STA_TEM",
                        "dpPort": 1,
                        "dpDataType": "S16",
                        "dpLen": 2,
                        "dpId": 254,
                        "endpoint": 7,
                        "name": "Temperature",
                        "mode": "ro",
                    }
                ],
            }
        ]

        result = trim_catalog(raw)

        assert result["HTV245FRF"]["*"] == {
            "portNumber": 1,
            "dp": [{"dpCode": 9, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "S16", "dpLen": 2}],
        }

    def test_returns_model_keyed_dict(self):
        """Output is keyed by model string, one entry per kept model."""
        raw = [
            {
                "model": "HTV245FRF",
                "dp": [{"dpCode": 9, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "int16", "portNumber": 1}],
            },
            {
                "model": "HCS021FRF",
                "dp": [{"dpCode": 10, "identity": "STA_RH", "dpPort": 1, "dpDataType": "uint8", "portNumber": 1}],
            },
            {"model": "NOTRAINPOINT", "dp": []},
        ]

        result = trim_catalog(raw)

        assert set(result.keys()) == {"HTV245FRF", "HCS021FRF"}
        assert isinstance(result, dict)

    def test_sorts_dp_entries_by_dpcode_regardless_of_rainpoint_order(self):
        """Two runs with the same dp entries in a different order produce identical output.

        The RainPoint API does not guarantee dp array order across calls; trim_catalog
        must sort by dpCode so --check/write output is stable and does not produce
        spurious drift on an otherwise-unchanged catalog.
        """
        entry_a = {"dpCode": 9, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "S16", "dpLen": 2}
        entry_b = {"dpCode": 32, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "U8", "dpLen": 1}

        first_order = trim_catalog([{"model": "HTV245FRF", "dp": [entry_a, entry_b]}])
        second_order = trim_catalog([{"model": "HTV245FRF", "dp": [entry_b, entry_a]}])

        assert first_order == second_order
        assert [dp["dpCode"] for dp in first_order["HTV245FRF"]["*"]["dp"]] == [9, 32]

    def test_dp_entries_missing_dpcode_sort_last(self):
        """A dp entry with no dpCode does not crash the sort and sorts after coded entries."""
        raw = [
            {
                "model": "HTV245FRF",
                "dp": [
                    {"dpCode": None, "identity": "STA_UNKNOWN", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
                    {"dpCode": 9, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "S16", "dpLen": 2},
                ],
            }
        ]

        result = trim_catalog(raw)

        assert [dp["dpCode"] for dp in result["HTV245FRF"]["*"]["dp"]] == [9, None]

    def test_variants_sharing_a_model_string_are_kept_apart(self):
        """Two modelCodes under one model must both survive the trim.

        A flat model-keyed snapshot silently kept whichever variant came last,
        which is how one variant's port metadata could end up annotating the
        other's payload.
        """
        raw = [
            {
                "model": "HIC801W",
                "modelCode": 278,
                "portNumber": 0,
                "dp": [{"dpCode": 1, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "S16", "dpLen": 2}],
            },
            {
                "model": "HIC801W",
                "modelCode": 279,
                "portNumber": 8,
                "dp": [{"dpCode": 1, "identity": "STA_TEM", "dpPort": 2, "dpDataType": "S16", "dpLen": 2}],
            },
        ]

        result = trim_catalog(raw)

        assert set(result["HIC801W"]) == {"278", "279"}
        assert result["HIC801W"]["278"]["portNumber"] == 0
        assert result["HIC801W"]["279"]["portNumber"] == 8

    def test_drops_non_status_and_non_control_identities(self):
        """P_/C_/S_/ATTR_ provisioning metadata is not shipped to users.

        Only STA_ (status, what the decoder sees) and CTL_ (control, what the
        generic-control allowlist is built from) are kept.
        """
        raw = [
            {
                "model": "HTV245FRF",
                "portNumber": 1,
                "dp": [
                    {"dpCode": 9, "identity": "STA_TEM", "dpPort": 1, "dpDataType": "S16", "dpLen": 2},
                    {"dpCode": 40, "identity": "CTL_WATER", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
                    {"dpCode": 60, "identity": "S_SMART_VOICE", "dpPort": 0, "dpDataType": "U8", "dpLen": 1},
                    {"dpCode": 61, "identity": "P_TIME", "dpPort": 0, "dpDataType": "U32", "dpLen": 4},
                    {"dpCode": 62, "identity": "C_RF_POWER", "dpPort": 0, "dpDataType": "U8", "dpLen": 1},
                    {"dpCode": 63, "identity": None, "dpPort": 0, "dpDataType": "U8", "dpLen": 1},
                ],
            }
        ]

        result = trim_catalog(raw)

        assert [dp["identity"] for dp in result["HTV245FRF"]["*"]["dp"]] == ["STA_TEM", "CTL_WATER"]

    def test_model_with_no_kept_identities_still_records_its_port_count(self):
        """A model whose dp entries are all filtered out keeps an empty dp list.

        Dropping the model entirely would lose its declared port count and make
        the variant look absent rather than empty.
        """
        raw = [
            {
                "model": "HIC801W",
                "modelCode": 278,
                "portNumber": 0,
                "dp": [{"dpCode": 60, "identity": "S_SMART_VOICE", "dpDataType": "U8", "dpLen": 1}],
            }
        ]

        result = trim_catalog(raw)

        assert result["HIC801W"]["278"] == {"portNumber": 0, "dp": []}

    def test_provenance_flags_are_carried_onto_the_variant_record(self):
        """hasDistribution and friends survive the trim so a maintainer can triage
        an unfamiliar model without re-fetching the raw RainPoint response.

        This is what would have answered "is HCS003FRF a real product?" directly:
        it is the only kind of record that is unpairable in the app.
        """
        raw = [
            {
                "model": "HCS003FRF",
                "modelCode": 35,
                "portNumber": 1,
                "hasDistribution": False,
                "isMainDevice": False,
                "accessoryFlag": False,
                "dp": [{"dpCode": 2, "identity": "CTL_SOCK", "dpDataType": "", "dpLen": 2}],
            }
        ]

        record = trim_catalog(raw)["HCS003FRF"]["35"]

        assert record["hasDistribution"] is False
        assert record["isMainDevice"] is False
        assert record["accessoryFlag"] is False

    def test_non_boolean_provenance_flags_are_dropped(self):
        """A RainPoint field that is not a bool is omitted rather than committed as junk,
        matching how a non-integer portNumber degrades.
        """
        raw = [
            {
                "model": "HCS021FRF",
                "hasDistribution": "yes",
                "dp": [{"dpCode": 10, "identity": "STA_RH"}],
            }
        ]

        record = trim_catalog(raw)["HCS021FRF"]["*"]

        assert "hasDistribution" not in record

    def test_non_integer_port_number_degrades_to_none(self):
        """A junk model-level portNumber is dropped rather than committed."""
        raw = [{"model": "HCS021FRF", "portNumber": "four", "dp": [{"dpCode": 10, "identity": "STA_RH"}]}]

        result = trim_catalog(raw)

        assert result["HCS021FRF"]["*"]["portNumber"] is None

    def test_boolean_port_number_degrades_to_none(self):
        """bool is an int subclass in Python; True must not be committed as 1 port.

        The script duplicates this guard from product_catalog._normalize_variant_record
        because it is standalone. Testing both copies is what stops them drifting:
        line coverage cannot tell them apart, since the string case above already
        executes the same branch.
        """
        raw = [{"model": "HCS021FRF", "portNumber": True, "dp": [{"dpCode": 10, "identity": "STA_RH"}]}]

        assert trim_catalog(raw)["HCS021FRF"]["*"]["portNumber"] is None

    def test_entry_without_a_model_code_lands_in_the_uncoded_bucket(self):
        """Most RainPoint entries carry no code; they become the model-level default."""
        raw = [{"model": "HCS021FRF", "dp": [{"dpCode": 10, "identity": "STA_RH"}]}]

        result = trim_catalog(raw)

        assert set(result["HCS021FRF"]) == {"*"}

    def test_uncoded_bucket_key_matches_the_component_loader(self):
        """The script duplicates this constant; drift would break enrichment silently."""
        assert refresh_product_catalog.UNCODED_VARIANT == product_catalog.UNCODED_VARIANT


class TestRefreshScriptDrift:
    """Tests for the --check drift report.

    Reporting every model as changed whenever the trim starts keeping a new
    record key would drown the one thing --check exists to surface: a RainPoint
    datapoint that actually moved.
    """

    def test_provenance_only_changes_are_reported_separately(self, capsys):
        """A snapshot that only gains RainPoint metadata is called out as such."""
        committed = {"HTV245FRF": {"303": {"portNumber": 2, "dp": [{"dpCode": 1}]}}}
        fresh = {"HTV245FRF": {"303": {"portNumber": 2, "hasDistribution": True, "dp": [{"dpCode": 1}]}}}

        assert refresh_product_catalog._print_drift(committed, fresh) is True
        out = capsys.readouterr().out
        assert "changed only in RainPoint metadata (1): HTV245FRF" in out
        assert "changed datapoints or ports" not in out

    def test_datapoint_drift_is_reported_with_the_fields_that_moved(self, capsys):
        """Real drift names the keys, so metadata noise never hides it."""
        committed = {"HTV245FRF": {"303": {"portNumber": 2, "dp": [{"dpCode": 1}]}}}
        fresh = {"HTV245FRF": {"303": {"portNumber": 4, "hasDistribution": True, "dp": [{"dpCode": 1}, {"dpCode": 2}]}}}

        assert refresh_product_catalog._print_drift(committed, fresh) is True
        out = capsys.readouterr().out
        assert "HTV245FRF (dp, hasDistribution, portNumber)" in out
        assert "changed only in RainPoint metadata" not in out

    def test_a_variant_present_on_one_side_only_counts_as_drift(self, capsys):
        """An added modelCode must not report as a change with no fields."""
        committed = {"HIC801W": {"278": {"portNumber": 0, "dp": []}}}
        fresh = {
            "HIC801W": {
                "278": {"portNumber": 0, "dp": []},
                "279": {"portNumber": 8, "dp": [{"dpCode": 7}]},
            }
        }

        assert refresh_product_catalog._print_drift(committed, fresh) is True
        assert "HIC801W (dp, portNumber)" in capsys.readouterr().out

    def test_identical_catalogs_report_no_drift(self, capsys):
        """The clean path still says so and returns False."""
        catalog = {"HTV245FRF": {"303": {"portNumber": 2, "dp": []}}}

        assert refresh_product_catalog._print_drift(catalog, dict(catalog)) is False
        assert "No drift" in capsys.readouterr().out


class TestRefreshScriptMain:
    """Tests for scripts/refresh_product_catalog.py::main safety guards.

    These guards are the only thing standing between a bad RainPoint pull and a
    corrupted committed catalog, so each refusal branch is exercised directly.
    Every test stubs the network fetch; nothing here talks to RainPoint.
    """

    @pytest.fixture(autouse=True)
    def _restore_current_event_loop(self):
        """Put the thread's current event loop back after main() runs.

        main() drives its fetch through asyncio.run, which closes the loop it
        created and leaves the thread with no current loop. The Home Assistant
        test plugin's autouse fixtures call asyncio.get_event_loop() during
        setup, so without this the very next test in the session errors out.
        """
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None
        yield
        if loop is not None:
            asyncio.set_event_loop(loop)

    @staticmethod
    def _stub_fetch(monkeypatch, trimmed, captured: dict | None = None):
        """Replace the script's network fetch with one returning trimmed.

        Mirrors the real signature including timeout_seconds, so a caller that
        stops threading the timeout through fails here rather than only against
        the live endpoint. Pass captured to inspect the arguments main() used.
        """

        async def _fake_fetch(email, password, area_code, timeout_seconds):
            if captured is not None:
                captured.update(email=email, password=password, area_code=area_code, timeout_seconds=timeout_seconds)
            return trimmed

        monkeypatch.setattr(refresh_product_catalog, "_fetch_trimmed_catalog", _fake_fetch)

    @staticmethod
    def _stub_credentials(monkeypatch):
        """Set the env vars main() requires before it will do any work."""
        monkeypatch.setenv("RAINPOINT_EMAIL", "user@example.com")
        monkeypatch.setenv("RAINPOINT_PASSWORD", "secret")

    @staticmethod
    def _forbid_write(monkeypatch):
        """Make any catalog write an outright test failure."""

        def _explode(trimmed, path):
            raise AssertionError("refused write path still called _write_catalog")

        monkeypatch.setattr(refresh_product_catalog, "_write_catalog", _explode)

    def test_write_refuses_empty_pull(self, monkeypatch, capsys):
        """An empty live pull never overwrites the committed catalog."""
        self._stub_credentials(monkeypatch)
        self._stub_fetch(monkeypatch, {})
        self._forbid_write(monkeypatch)
        monkeypatch.setattr(refresh_product_catalog, "_load_committed_catalog", lambda path: {"HCS777ARF": []})

        assert refresh_product_catalog.main([]) == 1
        assert "Refusing to write an empty catalog" in capsys.readouterr().err

    def test_write_refuses_drastic_model_drop(self, monkeypatch, capsys):
        """A pull that loses more than half the committed models is refused."""
        self._stub_credentials(monkeypatch)
        self._stub_fetch(monkeypatch, {"HTV245FRF": []})
        self._forbid_write(monkeypatch)
        monkeypatch.setattr(
            refresh_product_catalog,
            "_load_committed_catalog",
            lambda path: {"A1": [], "B2": [], "C3": [], "D4": []},
        )

        assert refresh_product_catalog.main([]) == 1
        assert "model count dropped from 4 to 1" in capsys.readouterr().err

    def test_write_persists_a_healthy_pull(self, monkeypatch, capsys):
        """A pull that clears both guards is written and reported."""
        self._stub_credentials(monkeypatch)
        self._stub_fetch(monkeypatch, {"HTV245FRF": [], "HCS021FRF": []})
        monkeypatch.setattr(refresh_product_catalog, "_load_committed_catalog", lambda path: {"HTV245FRF": []})
        written = {}
        monkeypatch.setattr(refresh_product_catalog, "_write_catalog", lambda trimmed, path: written.update(trimmed))

        assert refresh_product_catalog.main([]) == 0
        assert set(written) == {"HTV245FRF", "HCS021FRF"}
        assert "Wrote 2 models" in capsys.readouterr().out

    def test_check_treats_empty_pull_as_fetch_failure(self, monkeypatch, capsys):
        """--check reports a fetch failure rather than "every model removed"."""
        self._stub_credentials(monkeypatch)
        self._stub_fetch(monkeypatch, {})

        def _explode(committed, fresh):
            raise AssertionError("empty pull still reached the drift report")

        monkeypatch.setattr(refresh_product_catalog, "_print_drift", _explode)

        assert refresh_product_catalog.main(["--check"]) == 1
        assert "treating as a fetch failure, not drift" in capsys.readouterr().err

    def test_timeout_defaults_and_is_overridable(self, monkeypatch):
        """The fetch is always given an explicit deadline, and --timeout overrides it.

        Without one the session inherits aiohttp's five-minute default, which is
        long enough that a stalled pull looks like a wedged process and gets
        killed by hand before it ever fails.
        """
        self._stub_credentials(monkeypatch)
        monkeypatch.setattr(refresh_product_catalog, "_load_committed_catalog", lambda path: {"HTV245FRF": []})

        captured: dict = {}
        self._stub_fetch(monkeypatch, {"HTV245FRF": []}, captured)
        refresh_product_catalog.main(["--check"])
        assert captured["timeout_seconds"] == refresh_product_catalog._DEFAULT_TIMEOUT_SECONDS

        captured.clear()
        self._stub_fetch(monkeypatch, {"HTV245FRF": []}, captured)
        refresh_product_catalog.main(["--check", "--timeout", "5"])
        assert captured["timeout_seconds"] == 5.0

    def test_timeout_bounds_the_whole_fetch_not_each_request(self, monkeypatch, capsys):
        """--timeout is an end-to-end deadline, not a per-request one.

        get_product_catalog logs in and then fetches, so the session's
        per-request cap would let a slow login and a slow fetch together run
        well past the stated limit. The stub never returns, so only an outer
        deadline can end this.
        """
        import custom_components.rainpoint.api.client as client_module

        class _StalledClient:
            def __init__(self, area_code, email, password, session):
                pass

            async def get_product_catalog(self):
                await asyncio.sleep(30)
                raise AssertionError("the outer deadline never fired")

        monkeypatch.setattr(client_module, "RainPointClient", _StalledClient)

        fetch = refresh_product_catalog._fetch_trimmed_catalog("user@example.com", "secret", "1", 0.05)
        with pytest.raises(TimeoutError):
            asyncio.run(fetch)
        assert "Timed out after 0.05s" in capsys.readouterr().err

    def test_check_reports_drift_on_a_non_empty_pull(self, monkeypatch):
        """A non-empty pull still routes through the normal drift report."""
        self._stub_credentials(monkeypatch)
        self._stub_fetch(monkeypatch, {"HTV245FRF": []})
        monkeypatch.setattr(refresh_product_catalog, "_load_committed_catalog", lambda path: {})

        assert refresh_product_catalog.main(["--check"]) == 1

    def test_missing_credentials_exit_code(self, monkeypatch):
        """No credentials and no TTY is a usage error, not a crash or a prompt."""
        monkeypatch.delenv("RAINPOINT_EMAIL", raising=False)
        monkeypatch.delenv("RAINPOINT_PASSWORD", raising=False)
        monkeypatch.setattr(refresh_product_catalog.sys.stdin, "isatty", lambda: False)

        assert refresh_product_catalog.main([]) == 2

    def test_password_comes_from_env_without_prompting(self, monkeypatch):
        """RAINPOINT_PASSWORD is used directly, never routed through a prompt."""
        monkeypatch.setenv("RAINPOINT_PASSWORD", "from-env")
        monkeypatch.setattr(
            refresh_product_catalog.getpass,
            "getpass",
            lambda prompt="": pytest.fail("prompted despite RAINPOINT_PASSWORD being set"),
        )

        assert refresh_product_catalog._resolve_password() == "from-env"

    def test_password_prompts_interactively_when_env_is_unset(self, monkeypatch):
        """An interactive run with no env var falls back to a hidden prompt."""
        monkeypatch.delenv("RAINPOINT_PASSWORD", raising=False)
        monkeypatch.setattr(refresh_product_catalog.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(refresh_product_catalog.getpass, "getpass", lambda prompt="": "typed-in")

        assert refresh_product_catalog._resolve_password() == "typed-in"

    def test_password_is_none_when_prompt_is_empty(self, monkeypatch):
        """An empty prompt response is treated as no password, not as an empty one."""
        monkeypatch.delenv("RAINPOINT_PASSWORD", raising=False)
        monkeypatch.setattr(refresh_product_catalog.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(refresh_product_catalog.getpass, "getpass", lambda prompt="": "")

        assert refresh_product_catalog._resolve_password() is None

    def test_area_code_defaults_when_env_var_is_empty(self, monkeypatch):
        """CI exports an unset optional secret as "", which must not beat the default."""
        monkeypatch.setenv("RAINPOINT_AREA_CODE", "")

        assert refresh_product_catalog._parse_args([]).area_code == "1"

    def test_area_code_honours_a_configured_value(self, monkeypatch):
        """A populated RAINPOINT_AREA_CODE is passed through untouched."""
        monkeypatch.setenv("RAINPOINT_AREA_CODE", "44")

        assert refresh_product_catalog._parse_args([]).area_code == "44"


class TestGetDevicesByHid:
    """Tests for get_devices_by_hid API method."""

    @pytest.mark.asyncio
    async def test_get_devices_success(self):
        """get_devices_by_hid returns list of devices on success."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 0,
            "data": [{"mid": 100, "model": "HTV245FRF", "subDevices": []}],
        }
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        result = await client.get_devices_by_hid(hid=42)

        assert len(result) == 1
        assert result[0]["mid"] == 100

    @pytest.mark.asyncio
    async def test_get_devices_http_error(self):
        """get_devices_by_hid raises RainPointApiError on HTTP 500."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.get = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError):
            await client.get_devices_by_hid(hid=42)

    @pytest.mark.asyncio
    async def test_get_devices_api_error_code(self):
        """200 with non-zero API code raises getDeviceByHid failed: code N."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 1, "msg": "bad"}
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="getDeviceByHid failed: code 1"):
            await client.get_devices_by_hid(hid=42)


class TestGetMultipleDeviceStatus:
    """Tests for get_multiple_device_status API method."""

    @pytest.mark.asyncio
    async def test_get_multiple_status_success(self):
        """get_multiple_device_status converts status->subDeviceStatus."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 0,
            "data": [
                {
                    "mid": 100,
                    "status": [{"id": "D1", "value": "10#AA"}],
                    "propVer": 1,
                    "iotId": "x",
                }
            ],
        }
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        result = await client.get_multiple_device_status(devices=[{"mid": 100, "deviceName": "DEV", "productKey": "pk"}])

        assert len(result) == 1
        assert result[0]["mid"] == 100
        assert "subDeviceStatus" in result[0]
        assert len(result[0]["subDeviceStatus"]) == 1

    @pytest.mark.asyncio
    async def test_get_multiple_status_error(self):
        """get_multiple_device_status raises RainPointApiError on non-zero code."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 3}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError):
            await client.get_multiple_device_status(devices=[{"mid": 100}])

    @pytest.mark.asyncio
    async def test_get_multiple_status_missing_data_key(self):
        """code=0 with no 'data' key returns an empty list, not an error."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        result = await client.get_multiple_device_status(devices=[{"mid": 100}])

        assert result == []

    @pytest.mark.asyncio
    async def test_get_multiple_status_http_error_raises(self):
        """A non-200 status raises multipleDeviceStatus HTTP N."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.post = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="multipleDeviceStatus HTTP 500"):
            await client.get_multiple_device_status(devices=[{"mid": 100}])


class TestGetDeviceStatus:
    """Tests for get_device_status API method."""

    @pytest.mark.asyncio
    async def test_get_device_status_success(self):
        """get_device_status returns data dict with subDeviceStatus."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 0,
            "data": {"subDeviceStatus": [{"id": "D1", "value": "10#BB"}]},
        }
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        result = await client.get_device_status(mid=100)

        assert "subDeviceStatus" in result

    @pytest.mark.asyncio
    async def test_get_device_status_error(self):
        """get_device_status raises RainPointApiError on HTTP 404."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.get = MagicMock(return_value=_mock_response({}, status=404))

        with pytest.raises(RainPointApiError):
            await client.get_device_status(mid=100)

    @pytest.mark.asyncio
    async def test_get_device_status_api_error_code(self):
        """200 with non-zero API code raises getDeviceStatus failed: code N."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 1, "msg": "err"}
        client._session.get = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError, match="getDeviceStatus failed: code 1"):
            await client.get_device_status(mid=100)


class TestSetDeviceState:
    """Tests for set_device_state API method."""

    @pytest.mark.asyncio
    async def test_set_device_state_success(self):
        """set_device_state returns True on success."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        result = await client.set_device_state(home_id=1, device_name="dev", mid=100, product_key="pk", state={"mode": 1})

        assert result is True

    @pytest.mark.asyncio
    async def test_set_device_state_api_error(self):
        """set_device_state raises RainPointApiError on non-zero API code."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 5, "msg": "fail"}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError):
            await client.set_device_state(home_id=1, device_name="dev", mid=100, product_key="pk", state={})

    @pytest.mark.asyncio
    async def test_set_device_state_http_error(self):
        """set_device_state raises RainPointApiError on HTTP 500."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.post = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError):
            await client.set_device_state(home_id=1, device_name="dev", mid=100, product_key="pk", state={})


class TestGetSubscribeStatus:
    """get_subscribe_status() fetches fresh per-session MQTT credentials."""

    def _make_client(self) -> RainPointClient:
        """Make client helper."""
        return _make_client()

    def _mock_response(self, json_data: dict, status: int = 200) -> AsyncMock:
        """Mock response helper."""
        return _mock_response(json_data, status)

    @pytest.mark.asyncio
    async def test_subscribe_status_success_returns_data(self):
        """A code-0 response returns the response's data dict verbatim."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {
            "code": 0,
            "data": {
                "deviceSecret": "SEKRIT-value-9f3a",
                "deviceName": "name-A",
                "productKey": "pk123",
                "mqttHostUrl": "pk123.iot-as-mqtt.us-west-1.aliyuncs.com:1883",
            },
        }
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        result = await client.get_subscribe_status("hub-device", "pk123", 236547, 182509)

        assert result == json_body["data"]

    @pytest.mark.asyncio
    async def test_subscribe_status_sends_full_envelope(self):
        """The payload carries hid/hidList/subscribe(with mid)/unsubscribe/userInfo.

        A bare {deviceName, productKey} is rejected by the server with code 9999
        "must not be null".
        """
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({"code": 0, "data": {}}))

        await client.get_subscribe_status("hub-device", "pk123", 236547, 182509)

        _args, kwargs = client._session.post.call_args
        payload = kwargs["json"]
        assert payload["hid"] == "182509"
        assert payload["hidList"] == ["182509"]
        assert payload["subscribe"] == [{"deviceName": "hub-device", "mid": 236547, "productKey": "pk123"}]
        assert payload["unsubscribe"] == []
        user_info = payload["userInfo"]
        assert user_info["deviceName"] == "hub-device"
        assert user_info["productKey"] == "pk123"
        assert user_info["deviceType"] == 1
        assert user_info["notice"] == 0
        assert user_info["pushId"]  # a generated push id is present
        assert kwargs["headers"]["User-Agent"] == _USER_AGENT

    @pytest.mark.asyncio
    async def test_subscribe_status_http_error_raises(self):
        """An HTTP 500 status raises subscribeStatus HTTP 500."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        client._session.post = MagicMock(return_value=self._mock_response({}, status=500))

        with pytest.raises(RainPointApiError, match="subscribeStatus HTTP 500"):
            await client.get_subscribe_status("hub-device", "pk123", 236547, 182509)

    @pytest.mark.asyncio
    async def test_subscribe_status_code_error_raises(self):
        """A non-zero app-level code raises subscribeStatus failed: code N."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        json_body = {"code": 1, "msg": "error"}
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        with pytest.raises(RainPointApiError, match="code 1"):
            await client.get_subscribe_status("hub-device", "pk123", 236547, 182509)

    @pytest.mark.asyncio
    async def test_subscribe_status_never_logs_device_secret(self, caplog):
        """deviceSecret never appears in any DEBUG log line."""
        client = self._make_client()
        client.ensure_logged_in = AsyncMock()
        json_body = {
            "code": 0,
            "data": {
                "deviceSecret": "SEKRIT-value-9f3a",
                "deviceName": "name-A",
                "productKey": "pk123",
            },
        }
        client._session.post = MagicMock(return_value=self._mock_response(json_body))

        with caplog.at_level(logging.DEBUG):
            await client.get_subscribe_status("hub-device", "pk123", 236547, 182509)

        assert "SEKRIT-value-9f3a" not in caplog.text

    def test_redact_secret_short_value(self):
        """A short (<=4 char) secret is rendered as length + <short>, never the raw value."""
        assert _redact_secret("ab") == "len=2 <short>"

    def test_redact_secret_empty_value(self):
        """An empty/None secret is rendered as <empty>."""
        assert _redact_secret(None) == "<empty>"
        assert _redact_secret("") == "<empty>"

    def test_redact_secret_long_value(self):
        """A secret longer than 4 chars is rendered as length + last-4 only."""
        assert _redact_secret("SEKRIT-value-9f3a") == "len=17 last4=9f3a"
