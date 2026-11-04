"""The contract between the integration's registries and the pinned aiorainpoint library."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from aiorainpoint import RainPointApiError, is_hand_written_model

from custom_components.rainpoint.const import (
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES_AT,
    HAND_WRITTEN_MODELS,
    MODEL_DISPLAY_HUB,
    MODEL_VALVE_245,
    VALVE_MODELS,
)
from custom_components.rainpoint.coordinator import DECODER_REGISTRY
from custom_components.rainpoint.generic_control import _response_code_from_error
from tests.helpers import make_mock_session_client, mock_json_response
from tests.payload_samples import SAMPLE_HTV245_FULL_IDLE_PAYLOAD


class TestIsHandWrittenModel:
    """is_hand_written_model must recognize every trusted model and no others."""

    def test_true_for_every_decoder_registry_key(self):
        """Every registered decoder marks its model as hand-written."""
        for model in DECODER_REGISTRY:
            assert is_hand_written_model(model) is True

    def test_true_for_every_valve_model(self):
        """No valve model may ever reach the generic decode path."""
        for model in VALVE_MODELS:
            assert is_hand_written_model(model) is True


class TestHandWrittenModelsDriftGuard:
    """HAND_WRITTEN_MODELS must stay in lockstep with DECODER_REGISTRY."""

    def test_matches_decoder_registry_plus_display_hub(self):
        """Adding a decoder without updating the constant would open the boundary."""
        assert set(DECODER_REGISTRY) | {MODEL_DISPLAY_HUB} == HAND_WRITTEN_MODELS


def test_export_tokens_keys_match_config_entry_keys():
    """The client owns its token keys, so they must not drift from the stored entry's."""
    client = make_mock_session_client()

    assert set(client.export_tokens()) == {CONF_TOKEN, CONF_REFRESH_TOKEN, CONF_TOKEN_EXPIRES_AT}


def test_library_decoder_lines_log_under_the_aiorainpoint_root(caplog):
    """Library lines log under their own root, so an absence check aimed at them
    must set this logger, and this test proves the root is live."""
    with caplog.at_level(logging.DEBUG, logger="aiorainpoint"):
        DECODER_REGISTRY[MODEL_VALVE_245](SAMPLE_HTV245_FULL_IDLE_PAYLOAD)

    assert any(r.name == "aiorainpoint.decoders" and r.levelno == logging.DEBUG for r in caplog.records)


async def test_control_failure_text_yields_its_response_code():
    """The command-failed Repairs card id is parsed from the library's error text."""
    client = make_mock_session_client()
    client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    client._session.post = lambda *_a, **_k: mock_json_response({"code": 5})

    with pytest.raises(RainPointApiError) as excinfo:
        await client.control_work_mode(mid=1, addr=1, device_name="d", product_key="p", port=1, mode=1, duration=60)

    assert _response_code_from_error(excinfo.value) == "5"
