"""Tests for RainPoint API client (COVR-06)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.rainpoint.api import RainPointClient, RainPointApiError


class TestControlWorkModeCode4:
    """controlWorkMode must treat response code 4 as success, not error.

    Code 4 means the device is already in the requested state. This was a
    real bug -- the client used to raise RainPointApiError on code 4, causing
    spurious failures when toggling a valve that was already open/closed.
    """

    def _make_client(self) -> RainPointClient:
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

    def _mock_response(self, json_data: dict, status: int = 200) -> AsyncMock:
        """Create a mock aiohttp response context manager."""
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=json_data)
        # aiohttp uses async context manager for session.post()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

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
