"""Key handoff from the hoermoles-ble web app to Home Assistant.

The config flow shows a QR code. Scanned with the phone camera it opens the
web app, which asks which drive's key to hand over, encrypts the credential
bundle to an ephemeral key that only this flow knows, and then navigates (a
plain top-level navigation - the app's CSP forbids fetch and form posts) to the
one-time URL below with the ciphertext in the URL fragment. The tiny page served
here reads the fragment, POSTs it back to the same URL and erases it from the
browser history. The full wire format is in docs/key-handoff.md.

Envelope: b"HM1H" || client_pub(65, P-256 uncompressed) || nonce(12) || AES-256-GCM
ciphertext, AES key = HKDF-SHA256(ECDH secret, salt=ha_pub||client_pub,
info=b"hoermoles-ha-handoff-v1"), AAD = b"HM1H" || token. The plaintext is an
ordinary HMOLES1 bundle text.

The transport therefore does not need TLS for confidentiality: the URL, the
access log of a reverse proxy and the phone's history only ever see ciphertext
for a key that is discarded when the flow ends.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus

from aiohttp import web
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from hoermoles_ble import BundleEntry, BundleError, decode_bundle
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import HANDOFF_MAX_FAILURES, HANDOFF_SPA_URL, HANDOFF_TTL, HANDOFF_URL_BASE

_LOGGER = logging.getLogger(__name__)

HANDOFF_VERSION = 1
ENVELOPE_MAGIC = b"HM1H"
HKDF_INFO = b"hoermoles-ha-handoff-v1"
_PUBKEY_SIZE = 65
_NONCE_SIZE = 12

DATA_HANDOFF: HassKey[HandoffRegistry] = HassKey("hoermoles_ble_handoff")


class HandoffError(Exception):
    """A handoff payload that cannot be accepted; the message goes to the phone."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise HandoffError("Malformed payload encoding.") from exc


def public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def derive_key(shared_secret: bytes, ha_pub: bytes, client_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ha_pub + client_pub, info=HKDF_INFO).derive(shared_secret)


def encrypt_envelope(
    ha_pub: bytes,
    token: str,
    plaintext: bytes,
    *,
    client_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> bytes:
    """Client side of the envelope - what the web app implements in WebCrypto.
    Lives here for the tests and as the executable reference of the spec;
    `client_key` and `nonce` are fixed only to generate the shared test vector."""
    client_key = client_key or ec.generate_private_key(ec.SECP256R1())
    client_pub = public_key_bytes(client_key.public_key())
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ha_pub)
    aes_key = derive_key(client_key.exchange(ec.ECDH(), peer), ha_pub, client_pub)
    nonce = nonce or secrets.token_bytes(_NONCE_SIZE)
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, ENVELOPE_MAGIC + token.encode("ascii"))
    return ENVELOPE_MAGIC + client_pub + nonce + ciphertext


@dataclass
class HandoffSession:
    """One QR code: a token, an ephemeral key pair and, once received, the bundle."""

    address: str
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    created: float = field(default_factory=lambda: time.monotonic())
    failures: int = 0
    entry: BundleEntry | None = None
    _private_key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))

    @property
    def public_key(self) -> bytes:
        return public_key_bytes(self._private_key.public_key())

    @property
    def path(self) -> str:
        return f"{HANDOFF_URL_BASE}/{self.token}"

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > HANDOFF_TTL

    def qr_data(self, base_url: str, instance_name: str) -> str:
        """The QR code content: a web-app link whose fragment tells the app where
        to send the key and what to encrypt it to."""
        payload = {
            "v": HANDOFF_VERSION,
            "u": base_url.rstrip("/") + self.path,
            "k": b64url_encode(self.public_key),
            "a": self.address.upper(),
            "n": instance_name,
        }
        encoded = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{HANDOFF_SPA_URL}#ha={encoded}"

    def decrypt(self, envelope: bytes) -> bytes:
        header = len(ENVELOPE_MAGIC) + _PUBKEY_SIZE + _NONCE_SIZE
        if len(envelope) <= header or not envelope.startswith(ENVELOPE_MAGIC):
            raise HandoffError("Not a hoermoles handoff envelope.")
        client_pub = envelope[len(ENVELOPE_MAGIC) : len(ENVELOPE_MAGIC) + _PUBKEY_SIZE]
        nonce = envelope[len(ENVELOPE_MAGIC) + _PUBKEY_SIZE : header]
        try:
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub)
        except ValueError as exc:
            raise HandoffError("Invalid sender key.") from exc
        aes_key = derive_key(self._private_key.exchange(ec.ECDH(), peer), self.public_key, client_pub)
        try:
            return AESGCM(aes_key).decrypt(nonce, envelope[header:], ENVELOPE_MAGIC + self.token.encode("ascii"))
        except InvalidTag as exc:
            raise HandoffError("Could not decrypt - the QR code is outdated or the data was altered.") from exc

    def accept(self, payload: str) -> BundleEntry:
        """Decrypt, decode and match the bundle against this session's drive."""
        plaintext = self.decrypt(b64url_decode(payload.strip()))
        try:
            entries = decode_bundle(plaintext.decode("utf-8"))
        except (BundleError, UnicodeDecodeError) as exc:
            raise HandoffError(f"Invalid credential bundle: {exc}") from exc
        for entry in entries:
            if entry.credentials.device_address.upper() == self.address.upper():
                self.entry = entry
                return entry
        raise HandoffError(f"The key sent is not for the drive {self.address.upper()} being set up in Home Assistant.")


class HandoffRegistry:
    """All open handoff sessions of this Home Assistant instance."""

    def __init__(self) -> None:
        self._sessions: dict[str, HandoffSession] = {}

    @callback
    def create(self, address: str) -> HandoffSession:
        self._purge()
        session = HandoffSession(address=address)
        self._sessions[session.token] = session
        return session

    @callback
    def get(self, token: str) -> HandoffSession | None:
        self._purge()
        return self._sessions.get(token)

    @callback
    def close(self, session: HandoffSession) -> None:
        self._sessions.pop(session.token, None)

    def _purge(self) -> None:
        for token in [token for token, session in self._sessions.items() if session.expired]:
            del self._sessions[token]


@callback
def async_get_registry(hass: HomeAssistant) -> HandoffRegistry:
    """The handoff registry, registering the HTTP view on first use."""
    if (registry := hass.data.get(DATA_HANDOFF)) is None:
        registry = hass.data[DATA_HANDOFF] = HandoffRegistry()
        hass.http.register_view(HandoffView(registry))
    return registry


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Hoermoles &rarr; Home Assistant</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
.ok{color:#1b7f3b}.err{color:#b3261e}</style></head>
<body><h1>Hoermoles &rarr; Home Assistant</h1><p id="s">&hellip;</p>
<script>
(async () => {
  const s = document.getElementById("s");
  const p = new URLSearchParams(location.hash.slice(1)).get("p");
  history.replaceState(null, "", location.pathname);
  if (!p) {
    s.className = "err";
    s.textContent = "No key in this link. / Kein Schl\\u00fcssel in diesem Link.";
    return;
  }
  try {
    const r = await fetch(location.pathname, {method: "POST", body: p, headers: {"Content-Type": "text/plain"}});
    const t = await r.text();
    s.className = r.ok ? "ok" : "err";
    s.textContent = t;
  } catch (e) { s.className = "err"; s.textContent = String(e); }
})();
</script></body></html>
"""


class HandoffView(HomeAssistantView):
    """One-time endpoint the web app hands the encrypted key to.

    Unauthenticated by necessity (the phone has no Home Assistant session), so
    the 256-bit token in the path is the credential: unknown tokens count as a
    failed login for HA's IP ban, and a session dies after a few bad payloads.
    """

    url = HANDOFF_URL_BASE + "/{token}"
    name = "api:hoermoles_ble:handoff"
    requires_auth = False

    def __init__(self, registry: HandoffRegistry) -> None:
        self._registry = registry

    async def _session(self, request: web.Request, token: str) -> HandoffSession | None:
        session = self._registry.get(token)
        if session is None:
            await process_wrong_login(request)
        return session

    async def get(self, request: web.Request, token: str) -> web.Response:
        if await self._session(request, token) is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Unknown or expired link.")
        return web.Response(
            text=_PAGE,
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    async def post(self, request: web.Request, token: str) -> web.Response:
        session = await self._session(request, token)
        if session is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Unknown or expired link.")
        if session.entry is not None:
            return web.Response(status=HTTPStatus.CONFLICT, text="A key was already received for this QR code.")
        body = await request.text()
        if len(body) > 16384:
            return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, text="Payload too large.")
        try:
            entry = session.accept(body)
        except HandoffError as exc:
            session.failures += 1
            if session.failures >= HANDOFF_MAX_FAILURES:
                self._registry.close(session)
            _LOGGER.warning("Rejected key handoff for %s: %s", session.address, exc)
            return web.Response(status=HTTPStatus.BAD_REQUEST, text=str(exc))
        _LOGGER.info("Received key for %s via handoff", entry.credentials.device_address)
        return web.Response(
            text=(
                f"Key for {entry.credentials.device_address.upper()} received - continue in Home Assistant. / "
                "Schlüssel empfangen - weiter in Home Assistant."
            ),
            headers={"Cache-Control": "no-store"},
        )
