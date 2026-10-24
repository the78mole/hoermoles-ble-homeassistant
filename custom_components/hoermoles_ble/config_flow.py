"""Config flow for Hoermoles BLE.

A drive is found by its advertisement (discovery or a pick list). The key is
then imported - pasted as a hoermoles credential bundle, or handed over from the
hoermoles-ble web app by scanning a QR code (see handoff.py) - or skipped, which
leaves a fully working sensors-only entry that can get a key later through
"reconfigure".
"""

from __future__ import annotations

from typing import Any

import probatio
from hoermoles_ble import AdvertisementInfo, BundleEntry, BundleError, decode_bundle, is_encrypted_bundle
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DISCRETE_OPEN_CLOSE,
    CONF_PRODUCT_CLASS,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_NAME,
    CONF_QR_PREFIX,
    CONF_ROOT_ID,
    CONF_ROOT_KEY,
    CONF_SERIAL_NO,
    DOMAIN,
    MANUFACTURER_ID,
)
from .handoff import HandoffSession, async_get_registry

CONF_BUNDLE = "bundle"
CONF_PASSPHRASE = "passphrase"
CONF_HA_URL = "ha_url"
CONF_QR_CODE = "qr_code"

CREDENTIAL_KEYS = (CONF_ROOT_ID, CONF_ROOT_KEY, CONF_QR_PREFIX)


def _product_data(info: AdvertisementInfo | None) -> dict[str, Any]:
    if info is None or info.product_class is None:
        return {}
    return {
        CONF_PRODUCT_CLASS: info.product_class,
        CONF_PRODUCT_ID: info.product_id,
        CONF_PRODUCT_NAME: info.product_name,
        CONF_SERIAL_NO: None if info.serial_no is None else str(info.serial_no),
    }


def _parse_service_info(service_info: BluetoothServiceInfoBleak) -> AdvertisementInfo | None:
    payload = service_info.manufacturer_data.get(MANUFACTURER_ID)
    if payload is None:
        return None
    return AdvertisementInfo.from_scan(service_info.address, service_info.rssi, [payload])


class HoermolesConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._info: AdvertisementInfo | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._handoff: HandoffSession | None = None
        self._base_url: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HoermolesOptionsFlow:
        return HoermolesOptionsFlow()

    # --- finding the drive -------------------------------------------------

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._info = _parse_service_info(discovery_info)
        self.context["title_placeholders"] = {"name": self._default_title()}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_credentials()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._default_title(), "address": self._address or ""},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            self._info = _parse_service_info(self._discovered[address])
            return await self.async_step_credentials()

        configured = self._async_current_ids(include_ignore=False)
        for service_info in async_discovered_service_info(self.hass, connectable=False):
            if MANUFACTURER_ID in service_info.manufacturer_data and service_info.address not in configured:
                self._discovered[service_info.address] = service_info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        choices = {address: f"{info.name} ({address})" for address, info in self._discovered.items()}
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({probatio.Required(CONF_ADDRESS): probatio.In(choices)}),
        )

    # --- the key -----------------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        self._address = entry.unique_id
        return await self.async_step_credentials()

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="credentials",
            menu_options=["handoff", "paste", "sensors_only"],
            description_placeholders={"address": self._address or ""},
        )

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            text = user_input[CONF_BUNDLE]
            passphrase = user_input.get(CONF_PASSPHRASE) or None
            if is_encrypted_bundle(text) and passphrase is None:
                errors[CONF_PASSPHRASE] = "passphrase_required"
            else:
                try:
                    entries = await self.hass.async_add_executor_job(decode_bundle, text, passphrase)
                except BundleError:
                    errors["base"] = "invalid_passphrase" if passphrase else "invalid_bundle"
                else:
                    if (entry := self._matching_entry(entries)) is not None:
                        return await self._async_finish(entry)
                    errors["base"] = "wrong_device"

        return self.async_show_form(
            step_id="paste",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {
                        probatio.Required(CONF_BUNDLE): TextSelector(TextSelectorConfig(multiline=True)),
                        probatio.Optional(CONF_PASSPHRASE): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
                        ),
                    }
                ),
                {CONF_BUNDLE: user_input[CONF_BUNDLE]} if user_input else {},
            ),
            errors=errors,
            description_placeholders={"address": self._address or ""},
        )

    async def async_step_handoff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address is not None
        errors: dict[str, str] = {}
        if self._handoff is None or self._handoff.expired:
            self._handoff = async_get_registry(self.hass).create(self._address)
        if self._base_url is None:
            try:
                self._base_url = get_url(self.hass, allow_cloud=False)
            except NoURLAvailableError:
                self._base_url = ""

        if user_input is not None:
            new_url = user_input.get(CONF_HA_URL, "").strip().rstrip("/")
            if new_url and new_url != self._base_url:
                # Same session, new QR code pointing at the other address.
                self._base_url = new_url
            elif self._handoff.entry is not None:
                entry = self._handoff.entry
                self._close_handoff()
                return await self._async_finish(entry)
            elif not self._base_url:
                errors[CONF_HA_URL] = "url_required"
            else:
                errors["base"] = "not_received"

        schema: dict[Any, Any] = {}
        if self._base_url:
            schema[probatio.Optional(CONF_QR_CODE)] = QrCodeSelector(
                QrCodeSelectorConfig(
                    data=self._handoff.qr_data(self._base_url, self.hass.config.location_name),
                    scale=4,
                    error_correction_level=QrErrorCorrectionLevel.MEDIUM,
                )
            )
        schema[probatio.Optional(CONF_HA_URL, default=self._base_url)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        )
        return self.async_show_form(
            step_id="handoff",
            data_schema=probatio.Schema(schema),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    async def async_step_sensors_only(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_finish(None)

    # --- helpers -----------------------------------------------------------

    def _matching_entry(self, entries: list[BundleEntry]) -> BundleEntry | None:
        assert self._address is not None
        for entry in entries:
            if entry.credentials.device_address.upper() == self._address.upper():
                return entry
        return None

    def _default_title(self) -> str:
        if self._info is not None and self._info.product_name:
            return f"{self._info.product_name} {self._address}"
        return f"Hörmann {self._address}"

    async def _async_finish(self, bundle_entry: BundleEntry | None) -> ConfigFlowResult:
        data: dict[str, Any] = {}
        title = self._default_title()
        if bundle_entry is not None:
            credentials = bundle_entry.credentials
            data |= {
                CONF_ROOT_ID: credentials.root_id,
                CONF_ROOT_KEY: credentials.root_key.hex(),
                CONF_QR_PREFIX: credentials.qr_prefix,
            }
            if (device_info := bundle_entry.device_info) is not None:
                data |= {
                    CONF_PRODUCT_CLASS: device_info.product_class,
                    CONF_PRODUCT_ID: device_info.product_id,
                    CONF_PRODUCT_NAME: device_info.product_name,
                    CONF_SERIAL_NO: None if device_info.serial_no is None else str(device_info.serial_no),
                }
                title = device_info.label or title

        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            kept = {key: value for key, value in entry.data.items() if key not in CREDENTIAL_KEYS}
            return self.async_update_reload_and_abort(entry, data={**kept, **data})

        return self.async_create_entry(title=title, data={**_product_data(self._info), **data})

    def _close_handoff(self) -> None:
        if self._handoff is not None:
            async_get_registry(self.hass).close(self._handoff)
            self._handoff = None

    @callback
    def async_remove(self) -> None:
        """The flow was aborted or finished elsewhere - invalidate its QR code."""
        self._close_handoff()


class HoermolesOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Required(CONF_DISCRETE_OPEN_CLOSE, default=False): bool}),
                self.config_entry.options,
            ),
        )
