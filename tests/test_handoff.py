"""Key handoff: envelope crypto, QR payload and the one-time HTTP endpoint."""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hoermoles_ble.const import HANDOFF_MAX_FAILURES, HANDOFF_SPA_URL
from custom_components.hoermoles_ble.handoff import (
    ENVELOPE_MAGIC,
    HandoffError,
    HandoffSession,
    async_get_registry,
    b64url_decode,
    b64url_encode,
    encrypt_envelope,
)

from .conftest import ADDRESS, OTHER_ADDRESS, bundle_text


def _payload(session: HandoffSession, text: str | None = None, token: str | None = None) -> str:
    plaintext = (text if text is not None else bundle_text()).encode()
    return b64url_encode(encrypt_envelope(session.public_key, token or session.token, plaintext))


def test_round_trip_matches_drive() -> None:
    session = HandoffSession(address=ADDRESS.lower())
    entry = session.accept(_payload(session))
    assert entry.credentials.device_address == ADDRESS
    assert session.entry is entry


def test_rejects_other_drive() -> None:
    session = HandoffSession(address=ADDRESS)
    with pytest.raises(HandoffError, match="not for the drive"):
        session.accept(_payload(session, bundle_text(OTHER_ADDRESS)))
    assert session.entry is None


def test_rejects_envelope_bound_to_another_token() -> None:
    session = HandoffSession(address=ADDRESS)
    with pytest.raises(HandoffError, match="decrypt"):
        session.accept(_payload(session, token="other-token"))


def test_rejects_envelope_for_another_session_key() -> None:
    session = HandoffSession(address=ADDRESS)
    other = HandoffSession(address=ADDRESS, token=session.token)
    with pytest.raises(HandoffError, match="decrypt"):
        session.accept(_payload(other))


@pytest.mark.parametrize(
    "envelope",
    [b"", b"XXXX" + bytes(100), ENVELOPE_MAGIC + b"\x04" + bytes(64) + bytes(12) + bytes(20)],
)
def test_rejects_malformed_envelopes(envelope: bytes) -> None:
    session = HandoffSession(address=ADDRESS)
    with pytest.raises(HandoffError):
        session.accept(b64url_encode(envelope))


def test_rejects_non_bundle_plaintext() -> None:
    session = HandoffSession(address=ADDRESS)
    with pytest.raises(HandoffError, match="Invalid credential bundle"):
        session.accept(_payload(session, "hello"))


def test_rejects_bad_base64() -> None:
    with pytest.raises(HandoffError):
        b64url_decode("a")


def test_qr_data_points_to_spa_with_target_and_key() -> None:
    session = HandoffSession(address=ADDRESS.lower())
    data = session.qr_data("http://ha.local:8123/", "Home")
    assert data.startswith(HANDOFF_SPA_URL + "#ha=")
    payload = json.loads(b64url_decode(data.split("#ha=", 1)[1]))
    assert payload == {
        "v": 1,
        "u": f"http://ha.local:8123/api/hoermoles_ble/handoff/{session.token}",
        "k": b64url_encode(session.public_key),
        "a": ADDRESS,
        "n": "Home",
    }
    assert len(session.public_key) == 65


async def test_registry_purges_expired_sessions(hass: HomeAssistant, freezer) -> None:
    await async_setup_component(hass, "http", {})
    registry = async_get_registry(hass)
    session = registry.create(ADDRESS)
    assert registry.get(session.token) is session
    freezer.tick(16 * 60)
    assert registry.get(session.token) is None


async def test_view(hass: HomeAssistant, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    registry = async_get_registry(hass)
    session = registry.create(ADDRESS)
    client = await hass_client_no_auth()

    response = await client.get(session.path)
    assert response.status == HTTPStatus.OK
    assert "fetch(location.pathname" in await response.text()

    response = await client.post(session.path, data=_payload(session, bundle_text(OTHER_ADDRESS)))
    assert response.status == HTTPStatus.BAD_REQUEST
    assert "not for the drive" in await response.text()

    response = await client.post(session.path, data=_payload(session))
    assert response.status == HTTPStatus.OK
    assert ADDRESS in await response.text()
    assert session.entry is not None

    response = await client.post(session.path, data=_payload(session))
    assert response.status == HTTPStatus.CONFLICT


async def test_view_unknown_token(hass: HomeAssistant, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    async_get_registry(hass)
    client = await hass_client_no_auth()
    assert (await client.get("/api/hoermoles_ble/handoff/nope")).status == HTTPStatus.NOT_FOUND
    assert (await client.post("/api/hoermoles_ble/handoff/nope", data="x")).status == HTTPStatus.NOT_FOUND


async def test_view_closes_session_after_repeated_failures(hass: HomeAssistant, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    registry = async_get_registry(hass)
    session = registry.create(ADDRESS)
    client = await hass_client_no_auth()
    for _ in range(HANDOFF_MAX_FAILURES):
        assert (await client.post(session.path, data="garbage")).status == HTTPStatus.BAD_REQUEST
    assert registry.get(session.token) is None
    assert (await client.post(session.path, data=_payload(session))).status == HTTPStatus.NOT_FOUND


async def test_view_rejects_oversized_body(hass: HomeAssistant, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    session = async_get_registry(hass).create(ADDRESS)
    client = await hass_client_no_auth()
    response = await client.post(session.path, data="A" * 20000)
    assert response.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_shared_vector_is_current() -> None:
    """The web app runs its WebCrypto port against this exact file - regenerate
    with scripts/generate_handoff_vector.py and copy it over when this fails."""
    from scripts.generate_handoff_vector import FIXTURE, build

    assert json.loads(FIXTURE.read_text()) == build()
