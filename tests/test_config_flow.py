"""Config, reconfigure and options flows."""

from __future__ import annotations

import json
from http import HTTPStatus
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hoermoles_ble.config_flow import CONF_BUNDLE, CONF_HA_URL, CONF_PASSPHRASE
from custom_components.hoermoles_ble.const import (
    CONF_DISCRETE_OPEN_CLOSE,
    CONF_PRODUCT_NAME,
    CONF_ROOT_ID,
    CONF_ROOT_KEY,
    DOMAIN,
)
from custom_components.hoermoles_ble.handoff import (
    async_get_registry,
    b64url_decode,
    b64url_encode,
    encrypt_envelope,
)

from .conftest import ADDRESS, KEY_DATA, OTHER_ADDRESS, ROOT_KEY, bundle_text, full_payload, service_info


@pytest.fixture(autouse=True)
def no_entry_setup():
    with patch("custom_components.hoermoles_ble.async_setup_entry", return_value=True) as mock:
        yield mock


async def _discovered(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(full_payload()[:6])
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "credentials"
    return result


async def _choose(hass: HomeAssistant, result: dict, option: str) -> dict:
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": option})


async def test_discovery_paste_plain_bundle(hass: HomeAssistant) -> None:
    result = await _choose(hass, await _discovered(hass), "paste")
    assert result["step_id"] == "paste"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_BUNDLE: bundle_text()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garage"
    assert result["result"].unique_id == ADDRESS
    assert result["data"] == KEY_DATA


async def test_paste_encrypted_bundle_errors_then_success(hass: HomeAssistant) -> None:
    result = await _choose(hass, await _discovered(hass), "paste")
    flow_id = result["flow_id"]
    text = bundle_text(passphrase="secret")

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BUNDLE: text})
    assert result["errors"] == {CONF_PASSPHRASE: "passphrase_required"}

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BUNDLE: text, CONF_PASSPHRASE: "wrong"})
    assert result["errors"] == {"base": "invalid_passphrase"}

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BUNDLE: text, CONF_PASSPHRASE: "secret"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ROOT_KEY] == ROOT_KEY.hex()


async def test_paste_invalid_and_wrong_device(hass: HomeAssistant) -> None:
    result = await _choose(hass, await _discovered(hass), "paste")
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BUNDLE: "nonsense"})
    assert result["errors"] == {"base": "invalid_bundle"}
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BUNDLE: bundle_text(OTHER_ADDRESS)})
    assert result["errors"] == {"base": "wrong_device"}


async def test_sensors_only(hass: HomeAssistant) -> None:
    result = await _choose(hass, await _discovered(hass), "sensors_only")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # A single (short) discovery packet does not tell the model yet - the
    # coordinator learns it later from the full advertisement.
    assert result["title"] == f"Hörmann {ADDRESS}"
    assert result["data"] == {}


async def test_discovery_already_configured(hass: HomeAssistant, entry_with_key: MockConfigEntry) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(full_payload()[6:23])
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_handoff(hass: HomeAssistant, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    hass.config.internal_url = "http://ha.local:8123"
    result = await _choose(hass, await _discovered(hass), "handoff")
    flow_id = result["flow_id"]
    assert result["step_id"] == "handoff"
    qr = result["data_schema"].schema["qr_code"].config["data"]
    assert qr.startswith("https://the78mole.github.io/hoermoles-ble/app/#ha=")

    # Nothing sent yet
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_HA_URL: "http://ha.local:8123"})
    assert result["errors"] == {"base": "not_received"}

    # A different address re-renders the QR code for the same session
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_HA_URL: "http://192.168.1.2:8123/"})
    assert result["errors"] == {}
    qr = result["data_schema"].schema["qr_code"].config["data"]
    assert json.loads(b64url_decode(qr.split("#ha=", 1)[1]))["u"].startswith("http://192.168.1.2:8123/api/")

    # The phone delivers the key
    flow = hass.config_entries.flow._progress[flow_id]
    session = flow._handoff
    payload = b64url_encode(encrypt_envelope(session.public_key, session.token, bundle_text().encode()))
    client = await hass_client_no_auth()
    assert (await client.post(session.path, data=payload)).status == HTTPStatus.OK

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_HA_URL: "http://192.168.1.2:8123"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ROOT_ID] == 7
    assert async_get_registry(hass).get(session.token) is None


async def test_handoff_without_url_needs_one(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "http", {})
    with patch("custom_components.hoermoles_ble.config_flow.get_url", side_effect=NoURLAvailableError):
        result = await _choose(hass, await _discovered(hass), "handoff")
    assert "qr_code" not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {CONF_HA_URL: "url_required"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HA_URL: "http://ha:8123"})
    assert "qr_code" in result["data_schema"].schema


async def test_aborting_flow_invalidates_qr(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "http", {})
    hass.config.internal_url = "http://ha.local:8123"
    result = await _choose(hass, await _discovered(hass), "handoff")
    session = hass.config_entries.flow._progress[result["flow_id"]]._handoff
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert async_get_registry(hass).get(session.token) is None


async def test_user_step_lists_discovered(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.hoermoles_ble.config_flow.async_discovered_service_info",
        return_value=[service_info(full_payload()[:6])],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["type"] is FlowResultType.MENU
    result = await _choose(hass, result, "sensors_only")
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_step_no_devices(hass: HomeAssistant) -> None:
    with patch("custom_components.hoermoles_ble.config_flow.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_reconfigure_adds_and_removes_key(hass: HomeAssistant, entry_sensors_only: MockConfigEntry) -> None:
    entry_sensors_only.runtime_data = None
    result = await entry_sensors_only.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await _choose(hass, result, "paste")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_BUNDLE: bundle_text()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry_sensors_only.data[CONF_ROOT_KEY] == ROOT_KEY.hex()

    result = await entry_sensors_only.start_reconfigure_flow(hass)
    result = await _choose(hass, result, "sensors_only")
    assert result["reason"] == "reconfigure_successful"
    assert CONF_ROOT_KEY not in entry_sensors_only.data
    assert entry_sensors_only.data[CONF_PRODUCT_NAME] == "Supramatic Serie 4"


async def test_options(hass: HomeAssistant, entry_with_key: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry_with_key.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_DISCRETE_OPEN_CLOSE: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry_with_key.options == {CONF_DISCRETE_OPEN_CLOSE: True}
