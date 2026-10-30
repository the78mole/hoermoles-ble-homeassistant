"""Config flow for Hoermoles BLE.

A drive is found by its advertisement (discovery or a pick list). The key is
then imported - pasted as a hoermoles credential bundle, or handed over from the
hoermoles-ble web app by scanning a QR code (see handoff.py) - or created right
here by registering with the drive's QR label, or skipped, which leaves a fully
working sensors-only entry that can get a key later through "reconfigure".
"""

from __future__ import annotations

import logging
from typing import Any

import probatio
from bleak.exc import BleakError
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from hoermoles_ble import (
    AdvertisementInfo,
    BundleEntry,
    BundleError,
    DeviceInfo,
    decode_bundle,
    is_encrypted_bundle,
    product_class_and_id_from_qr_prefix,
    serial_no_from_qr_prefix,
)
from hoermoles_ble.advertisement import PRODUCT_TYPE_NAMES
from hoermoles_ble.crypto_rsa import load_device_public_key
from hoermoles_ble.protocol import parse_qr_code
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_last_service_info,
)
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
from .controller import async_register
from .handoff import HandoffSession, async_get_registry
from .simulator import async_read_advertisement, is_simulator

_LOGGER = logging.getLogger(__name__)

CONF_BUNDLE = "bundle"
CONF_PASSPHRASE = "passphrase"
CONF_HA_URL = "ha_url"
CONF_QR_CODE = "qr_code"
CONF_QR_LABEL = "qr_label"

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
    # The simulator sends no manufacturer data: None here, its advertisement is
    # read over GATT where it matters (see _async_advertisement).
    payload = service_info.manufacturer_data.get(MANUFACTURER_ID)
    if payload is None:
        return None
    return AdvertisementInfo.from_scan(service_info.address, service_info.rssi, [payload])


def _parse_qr_label(text: str) -> tuple[str, int] | None:
    """(prefix, serial) of a drive's QR label - 31 digits followed by the
    drive's RSA public key in base64 - or None if the text isn't one."""
    try:
        prefix, der = parse_qr_code(text)
        public_key = load_device_public_key(der)
    except ValueError, UnsupportedAlgorithm:  # binascii.Error is a ValueError
        return None
    serial_no = serial_no_from_qr_prefix(prefix)
    if serial_no is None or not isinstance(public_key, RSAPublicKey):
        return None
    return prefix, serial_no


def _device_info_from_qr(address: str, prefix: str, serial_no: int) -> DeviceInfo | None:
    if (product := product_class_and_id_from_qr_prefix(prefix)) is None:
        return None
    product_class, product_id = product
    return DeviceInfo(
        device_address=address,
        product_class=product_class,
        product_id=product_id,
        product_name=PRODUCT_TYPE_NAMES.get(product) or PRODUCT_TYPE_NAMES.get((product_class, None)),
        serial_no=serial_no,
    )


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
            is_drive = MANUFACTURER_ID in service_info.manufacturer_data or is_simulator(service_info)
            if is_drive and service_info.address not in configured:
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
            menu_options=["handoff", "paste", "register", "sensors_only"],
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

    async def async_step_register(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address is not None
        errors: dict[str, str] = {}
        info = await self._async_advertisement()
        if user_input is not None:
            text = user_input[CONF_QR_LABEL]
            if (label := _parse_qr_label(text)) is None:
                errors[CONF_QR_LABEL] = "invalid_qr_label"
            elif (serial_no := self._known_serial(info)) is not None and serial_no != label[1]:
                errors["base"] = "wrong_device"
            elif (ble_device := async_ble_device_from_address(self.hass, self._address, connectable=True)) is None:
                errors["base"] = "no_connectable_path"
            else:
                try:
                    credentials = await async_register(ble_device, self._default_title(), text)
                except TimeoutError:  # RegistrationTimeout included
                    errors["base"] = "not_in_teach_mode"
                except BleakError as exc:
                    _LOGGER.debug("Registration with %s failed: %s", self._address, exc)
                    errors["base"] = "cannot_connect"
                else:
                    device_info = _device_info_from_qr(self._address, *label)
                    return await self._async_finish(BundleEntry(credentials=credentials, device_info=device_info))
        elif info is not None and info.admins_can_be_teached is False:
            # Only a warning: the flag sits in the status packet, which an idle
            # drive rarely sends, so it can be stale - trying stays allowed.
            errors["base"] = "teach_mode_off"

        return self.async_show_form(
            step_id="register",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Required(CONF_QR_LABEL): TextSelector(TextSelectorConfig(multiline=True))}),
                {CONF_QR_LABEL: user_input[CONF_QR_LABEL]} if user_input else {},
            ),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    async def async_step_sensors_only(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_finish(None)

    # --- helpers -----------------------------------------------------------

    async def _async_advertisement(self) -> AdvertisementInfo | None:
        """What the drive advertises, as far as known: the running entry's full
        parse on reconfigure, the simulator's debug characteristic, otherwise
        the single discovery packet (rarely enough for the status flags)."""
        assert self._address is not None
        if self.source == SOURCE_RECONFIGURE:
            runtime_data = getattr(self._get_reconfigure_entry(), "runtime_data", None)
            if runtime_data is not None and runtime_data.coordinator.info is not None:
                return runtime_data.coordinator.info
        service_info = async_last_service_info(self.hass, self._address, connectable=False)
        if (
            service_info is not None
            and is_simulator(service_info)
            and (ble_device := async_ble_device_from_address(self.hass, self._address, connectable=True))
        ):
            try:
                return await async_read_advertisement(ble_device, service_info.rssi)
            except (BleakError, TimeoutError, ValueError, KeyError) as exc:
                _LOGGER.debug("Reading the simulator's advertisement failed: %s", exc)
        return self._info

    def _known_serial(self, info: AdvertisementInfo | None) -> int | None:
        if info is not None and info.serial_no is not None:
            return info.serial_no
        if self.source == SOURCE_RECONFIGURE and (serial_no := self._get_reconfigure_entry().data.get(CONF_SERIAL_NO)):
            return int(serial_no)
        return None

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
