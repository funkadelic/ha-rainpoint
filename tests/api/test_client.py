"""Tests for RainPoint API client (COVR-06)."""

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.api import RainPointClient, RainPointApiError


def _make_client() -> RainPointClient:
    """Create a RainPointClient with a mock session.

    Constructor args: area_code, email, password, session.
    We set _token directly so _auth_headers() does not raise.
    """
    mock_session = MagicMock()
    client = RainPointClient(
        area_code="1",
        email="test@example.com",
        password="testpass",
        session=mock_session,
    )
    client._token = "fake-token-for-test"
    return client


def _mock_response(json_data: dict, status: int = 200) -> AsyncMock:
    """Create a mock aiohttp response context manager."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    # aiohttp uses async context manager for session.post()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


class TestControlWorkModeCode4:
    """controlWorkMode must treat response code 4 as success, not error.

    Code 4 means the device is already in the requested state. This was a
    real bug -- the client used to raise RainPointApiError on code 4, causing
    spurious failures when toggling a valve that was already open/closed.
    """

    def _make_client(self) -> RainPointClient:
        return _make_client()

    def _mock_response(self, json_data: dict, status: int = 200) -> AsyncMock:
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
            mid=123, addr=1, device_name="AABBCCDD",
            product_key="pk123", port=1, mode=1, duration=300,
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
            mid=123, addr=1, device_name="AABBCCDD",
            product_key="pk123", port=1, mode=1, duration=300,
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
                mid=123, addr=1, device_name="AABBCCDD",
                product_key="pk123", port=1, mode=1, duration=300,
            )


class TestLogin:
    """Tests for the _login method including MD5 hashing and token storage."""

    @pytest.mark.asyncio
    async def test_login_success(self):
        """Successful login stores token, refresh token, and expiry."""
        client = _make_client()
        client._token = None  # Reset so we're actually testing login

        json_body = {
            "code": 0,
            "data": {
                "token": "tok123",
                "refreshToken": "ref456",
                "tokenExpired": 3600,
            },
            "ts": 1700000000000,
        }
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        await client._login()

        assert client._token == "tok123"
        assert client._refresh_token == "ref456"
        assert client._token_expires_at is not None

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
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        # Try positional then keyword args
        if payload is None:
            args, kwargs = call_kwargs
            payload = kwargs.get("json")

        expected_md5 = hashlib.md5(b"testpass").hexdigest()
        assert expected_md5 == "179ad45c6ce2cb97cf1029e212046e81"
        assert payload["password"] == expected_md5

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

        args, kwargs = client._session.post.call_args
        payload = kwargs.get("json")

        # email="test@example.com", area_code="1" => MD5("test@example.com1")
        expected_device_id = hashlib.md5(b"test@example.com1").hexdigest()
        assert payload["deviceId"] == expected_device_id


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
        # 3 minutes from now — within the 5-min buffer
        client._token_expires_at = datetime.now(UTC) + timedelta(minutes=3)
        assert client._token_valid() is False

    def test_restore_tokens(self):
        """restore_tokens sets _token, _refresh_token, and _token_expires_at."""
        client = _make_client()
        client._token = None

        client.restore_tokens({
            "token": "t1",
            "refresh_token": "r1",
            "token_expires_at": 1700000000,
        })

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

        client._login.assert_called_once()


class TestAuthHeaders:
    """Tests for _auth_headers method."""

    def test_auth_headers_with_token(self):
        """_auth_headers returns dict with auth token and appCode."""
        client = _make_client()
        client._token = "mytoken"

        headers = client._auth_headers()

        assert headers["auth"] == "mytoken"
        assert headers["appCode"] == "2"

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

        result = await client.get_multiple_device_status(
            devices=[{"mid": 100, "deviceName": "DEV", "productKey": "pk"}]
        )

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


class TestSetDeviceState:
    """Tests for set_device_state API method."""

    @pytest.mark.asyncio
    async def test_set_device_state_success(self):
        """set_device_state returns True on success."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 0}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        result = await client.set_device_state(
            home_id=1, device_name="dev", mid=100, product_key="pk", state={"mode": 1}
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_set_device_state_api_error(self):
        """set_device_state raises RainPointApiError on non-zero API code."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        json_body = {"code": 5, "msg": "fail"}
        client._session.post = MagicMock(return_value=_mock_response(json_body))

        with pytest.raises(RainPointApiError):
            await client.set_device_state(
                home_id=1, device_name="dev", mid=100, product_key="pk", state={}
            )

    @pytest.mark.asyncio
    async def test_set_device_state_http_error(self):
        """set_device_state raises RainPointApiError on HTTP 500."""
        client = _make_client()
        client.ensure_logged_in = AsyncMock()

        client._session.post = MagicMock(return_value=_mock_response({}, status=500))

        with pytest.raises(RainPointApiError):
            await client.set_device_state(
                home_id=1, device_name="dev", mid=100, product_key="pk", state={}
            )
