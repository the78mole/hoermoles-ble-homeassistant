# Key handoff: hoermoles-ble web app → Home Assistant

Status: **HA side implemented** (`custom_components/hoermoles_ble/handoff.py`,
tests in `tests/test_handoff.py`). **Web app side still to do** in
[`the78mole/hoermoles-ble`](https://github.com/the78mole/hoermoles-ble) (`spa/`).

Goal: in the HA config flow, scan a QR code with your phone, confirm in the web
app, and the drive's key ends up in Home Assistant, with no copy-pasting of
`HMOLES1…` strings and without the key ever crossing the network in plaintext.

## Constraints that shaped the protocol

- The web app's CSP is `connect-src 'none'; form-action 'none'`. That is
  deliberate (a compromised page has no exfiltration channel), and this protocol
  **does not loosen it**: the app neither fetches nor posts a form. It only does a
  **top-level navigation** (`window.open` / `location.assign`), which neither
  directive governs.
- HA is often reachable over plain `http://` on the LAN only. The payload is
  therefore end-to-end encrypted to a key that exists only in that HA config
  flow. TLS is welcome but not required for confidentiality.
- Keys in the app are stored **non-extractable** by default. The app can only
  hand over a key it can still read (see "Web app changes" below).

## Flow

```
HA config flow                      phone (web app)                    HA HTTP view
──────────────                      ───────────────                    ────────────
P-256 key pair + 256-bit token
QR: <app>/#ha=<b64url(JSON)>  ──scan──▶ parse #ha=, show target,
                                     user picks the drive + confirms
                                     ECDH + HKDF + AES-GCM → envelope
                                     navigate (new tab) to
                                     <u>#p=<b64url(envelope)>  ───────▶ GET <u>: tiny HTML page
                                                                       JS reads #p, erases it
                                                                       from history, POSTs it
                                                                       to <u> (same origin)
                                                                       → decrypt, check drive
                                     page shows "received" ◀────────── 200 text
user presses "Submit" in HA ◀── flow finds the key, creates/updates the entry
```

## QR payload (HA → app)

The QR code is a link into the web app, so the phone's own camera app opens it
directly. The in-app scanner must accept the same string:

```
https://the78mole.github.io/hoermoles-ble/app/#ha=<base64url(JSON), no padding>
```

```json
{
  "v": 1,
  "u": "http://192.168.1.10:8123/api/hoermoles_ble/handoff/<token>",
  "k": "<base64url of HA's P-256 public key, 65-byte uncompressed point>",
  "a": "F1:26:AF:CC:41:86",
  "n": "Home"
}
```

| Field | Meaning |
| --- | --- |
| `v` | Protocol version. The app must refuse anything other than `1`. |
| `u` | One-time target URL. The last path segment is the **token** (URL-safe base64, 43 chars). |
| `k` | HA's ephemeral public key for this flow. |
| `a` | MAC address of the drive being set up (upper case). The app should preselect it and warn when it has no key for it. HA rejects a bundle that does not contain it. |
| `n` | HA's location name, for display only ("Send key to *Home*?"). |

## Envelope (app → HA)

All primitives are in WebCrypto, so no extra library is needed.

1. `client = generateKey({name: "ECDH", namedCurve: "P-256"})`
2. `Z = deriveBits({name: "ECDH", public: importKey("raw", k)}, client.privateKey, 256)`
3. `client_pub = exportKey("raw", client.publicKey)` (65 bytes, uncompressed)
4. `aes = HKDF-SHA256(ikm = Z, salt = k_bytes || client_pub, info = "hoermoles-ha-handoff-v1", length = 32 bytes)`
5. `nonce = 12 random bytes`
6. `ct = AES-256-GCM(aes, nonce, plaintext, additionalData = "HM1H" || ascii(token))`
7. `envelope = "HM1H" || client_pub || nonce || ct` (ct includes the 16-byte tag)

The **plaintext** is an ordinary plaintext bundle text, `HMOLES1:<…>`, produced
by the existing `encodeBundle()` without a passphrase. Normally it holds a single
entry, the drive from `a`. The AAD binds the ciphertext to this one token.

The executable reference of steps 1-7 is `encrypt_envelope()` in
`custom_components/hoermoles_ble/handoff.py`. A shared test vector should be added
to `shared/test-vectors.json` once the TS side exists. Because ECDH involves
randomness on both sides, the vector should fix the HA private key and the client
private key plus nonce, and check the envelope byte for byte.

## Delivery

The app navigates to

```
<u>#p=<base64url(envelope), no padding>
```

ideally in a new tab (`window.open(url, "_blank", "noopener")`) so the app keeps
its state. The fragment is not sent in the GET request, so the ciphertext stays
out of proxy and access logs. HA's page reads it, removes it from the history
entry via `history.replaceState`, and POSTs it as `text/plain` to the same URL.
That request is same-origin, so it needs no CORS and touches no app CSP.

HA's responses (plain text, shown by the page):

| Status | Meaning |
| --- | --- |
| 200 | Key accepted. Continue in HA (press *Submit*). |
| 400 | Not decryptable, not a bundle, or wrong drive. After 5 failures the token is dead. |
| 404 | Unknown/expired token (15 min, or the flow was closed). Counts towards HA's IP ban. |
| 409 | A key was already received for this QR code. |
| 413 | Payload over 16 KiB. |

## Web app changes needed (hoermoles-ble `spa/`)

1. **Entry points:** handle `#ha=` at startup, just like the existing `#import=`
   handling. Also accept the full link in the existing QR scanner
   (`QrCapture`), so scanning from inside the app works too.
2. **"Send to Home Assistant" panel:** show `n`, the target host from `u`, and
   the drive list with `a` preselected. Warn loudly when `u` is not on a private
   or `.local` address, or when it is not `http(s)`. Confirm, then build the
   envelope and navigate.
3. **Extractable keys:** non-extractable drives cannot be sent. Options, in
   order of preference:
   - offer "Send to Home Assistant" right after **registration**, while the raw
     key is still in memory;
   - for drives imported or registered with *allow re-export*, use the stored
     raw key;
   - otherwise explain why it is not possible and point to a fresh export from
     the CLI or re-registration.
4. **Crypto module:** add `handoff.ts` next to `bundle.ts` in `hoermoles-ble-js`
   (ECDH/HKDF/AES-GCM as above) plus a shared test vector against the Python
   reference.
5. **CSP stays as is.** No `connect-src` or `form-action` change is needed. Should
   a future version want a confirmation back in the app, `connect-src` would
   have to be opened for arbitrary LAN hosts. That is deliberately not part of v1.
