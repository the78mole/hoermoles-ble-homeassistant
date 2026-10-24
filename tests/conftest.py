"""Shared fixtures: advertisement payloads, bundles and a ready config entry."""

from __future__ import annotations

import time

import pytest
from hoermoles_ble import BundleEntry, Credentials, DeviceInfo, encode_bundle
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.api import _get_manager
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hoermoles_ble.const import (
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

ADDRESS = "F1:26:AF:CC:41:86"
OTHER_ADDRESS = "AA:BB:CC:DD:EE:FF"
SERIAL = 302626026414510307
ROOT_KEY = bytes(range(32))


def full_payload(position: float = 0.0, moving: bool = False, low_battery: bool = False, serial: int = SERIAL) -> bytes:
    """Both advertisement packets back to back (without the company id), laid out
    like hoermoles-ble's own test vectors: [product_id][product_class][status]
    [product_data(13)][serial(8)] - product_class 2 = Supramatic."""
    data = bytearray(25)
    data[0] = 2  # product_id
    data[1] = 2  # product_class
    data[2] = 0b00001101 | (0b01000000 if moving else 0)
    data[3] = 0b00000100 if low_battery else 0
    data[7] = int(position * 2)
    data[15:23] = serial.to_bytes(8, "little")
    return bytes(data)


def short_packet(**kwargs) -> bytes:
    return full_payload(**kwargs)[:6]


def long_packet(**kwargs) -> bytes:
    return full_payload(**kwargs)[6:23]


def service_info(payload: bytes, address: str = ADDRESS, rssi: int = -60) -> BluetoothServiceInfoBleak:
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

    manufacturer_data = {MANUFACTURER_ID: payload}
    return BluetoothServiceInfoBleak(
        name="Hoermann",
        address=address,
        rssi=rssi,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, "Hoermann", None),
        advertisement=AdvertisementData(
            local_name="Hoermann",
            manufacturer_data=manufacturer_data,
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=rssi,
            platform_data=(),
        ),
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
        raw=None,
    )


def inject(hass: HomeAssistant, payload: bytes, address: str = ADDRESS) -> None:
    _get_manager(hass).scanner_adv_received(service_info(payload, address))


def bundle_entry(address: str = ADDRESS, label: str | None = "Garage") -> BundleEntry:
    return BundleEntry(
        credentials=Credentials(device_address=address, root_id=7, root_key=ROOT_KEY, qr_prefix="0102"),
        device_info=DeviceInfo(
            device_address=address,
            product_class=2,
            product_id=2,
            product_name="Supramatic Serie 4",
            serial_no=SERIAL,
            label=label,
        ),
    )


def bundle_text(address: str = ADDRESS, passphrase: str | None = None) -> str:
    return encode_bundle([bundle_entry(address)], passphrase)


KEY_DATA = {
    CONF_ROOT_ID: 7,
    CONF_ROOT_KEY: ROOT_KEY.hex(),
    CONF_QR_PREFIX: "0102",
    CONF_PRODUCT_CLASS: 2,
    CONF_PRODUCT_ID: 2,
    CONF_PRODUCT_NAME: "Supramatic Serie 4",
    CONF_SERIAL_NO: str(SERIAL),
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, mock_bluetooth):
    """Load the integration from custom_components with Bluetooth mocked out."""


@pytest.fixture
def entry_with_key(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Garage", data=dict(KEY_DATA))
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def entry_sensors_only(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Garage", data={})
    entry.add_to_hass(hass)
    return entry
