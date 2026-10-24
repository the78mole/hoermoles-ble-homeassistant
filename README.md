# Hoermoles BLE for Home Assistant

[![Release](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/release.yml/badge.svg)](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/release.yml)
[![Tests](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/test.yml/badge.svg)](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/test.yml)
[![Validate](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/validate.yml/badge.svg)](https://github.com/the78mole/hoermoles-ble-homeassistant/actions/workflows/validate.yml)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![PyPI - hoermoles-ble](https://img.shields.io/pypi/v/hoermoles-ble?label=hoermoles-ble)](https://pypi.org/project/hoermoles-ble/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Renovate](https://img.shields.io/badge/renovate-enabled-brightgreen.svg)](https://renovatebot.com)

A Home Assistant integration for Hörmann garage door drives with BlueSecur
Bluetooth (e.g. Supramatic Serie 4). It works locally, with no Hörmann app or
cloud, on top of the reverse-engineered
[`hoermoles-ble`](https://github.com/the78mole/hoermoles-ble) library.

> **Status: early.** The protocol has been verified live against a Supramatic E4,
> but **not yet through Home Assistant's Bluetooth stack**. The drive drops the
> connection about 100-150 ms after the first chunk of a command. A local
> adapter should make that window; a transparent ESPHome Bluetooth proxy may not.
> Sensors are unaffected, because they need no connection at all.

## What you get

**Without a key**, read passively from the drive's advertisement (no pairing):

- Position (%), moving, low battery, maintenance required
- Diagnostics: protection, vacation mode, emergency mode, "accepting
  registration", signal strength

**With a key:**

- A `cover` (garage door). Open, close and stop send the **verified impulse
  channel**. Since impulse is a toggle, the reported position suppresses
  commands that would do the opposite of what was asked.
- Buttons: impulse, partial opening (verified), ventilation position (verified,
  disabled by default because not every model has it), light (**unverified**,
  disabled by default)
- Option: use the direction channels 4/5 for open and close (**unverified**)

A drive that is idle sends only half of its advertisement (position, not the
status flags). Status flags such as low battery can therefore stay *unknown*
for a while after a restart. "Moving" falls back to watching the position.

## Installation

HACS → ⋮ → *Custom repositories* → `https://github.com/the78mole/hoermoles-ble-homeassistant`,
type *Integration*. Install it, then restart Home Assistant.

Requirements: Home Assistant 2026.10 or newer, with the Bluetooth integration
and a Bluetooth adapter or proxy in range of the drive.

## Setup

Drives are discovered automatically by their advertisement (Hörmann
manufacturer id 1972). Alternatively: *Settings → Devices & services → Add
integration → Hoermoles BLE*.

Then choose how the key gets into Home Assistant:

1. **Scan a QR code with the hoermoles-ble web app.** HA shows a QR code. Scan it
   with your phone camera, confirm in the
   [web app](https://the78mole.github.io/hoermoles-ble/app/), then press
   *Submit* in HA. The key is end-to-end encrypted for this one setup (ECDH P-256
   + AES-GCM), so plain `http://` on the LAN is fine. Protocol:
   [docs/key-handoff.md](docs/key-handoff.md).
   *The web app side of this is not released yet; until then, use option 2.*
2. **Paste an exported key**, from `hoermoles-ble export --stdout` (CLI) or the
   web app's *Export* tab. `HMOLES1E:…` bundles additionally ask for their
   passphrase.
3. **Sensors only.** Add a key later via the entry's *Reconfigure*.

The drive must be registered once, with the
[CLI](https://pypi.org/project/hoermoles-ble-cli/) or the web app. Registration
from within Home Assistant is not supported (yet).

## Security

- The root key **opens your garage door**. Home Assistant stores it like every
  other integration secret: in plain text in `.storage/core.config_entries`,
  protected by file permissions only. Anyone who controls your HA host or its
  backups controls the door.
- The QR handoff endpoint (`/api/hoermoles_ble/handoff/<token>`) is
  unauthenticated by necessity. It only accepts a payload encrypted to the
  current flow's ephemeral key, dies after 15 minutes, after 5 bad payloads or
  when the flow closes, and unknown tokens count towards HA's IP ban.
- The integration makes no outbound internet connections.

## Development

```bash
uv sync             # Home Assistant + pytest-homeassistant-custom-component
uv run pytest       # tests + coverage (fails below 80 %)
uv run ruff check . && uv run ruff format --check .
pre-commit install
```

Releases: every push to `main` that touches the integration creates a release
via `paulhatch/semantic-version` (`feat:` → minor, `…!:` → major, otherwise
patch). The release workflow writes the version into `manifest.json` and attaches
`hoermoles_ble.zip`, which is what HACS installs.

The `hoermoles-ble` version is pinned in `manifest.json` (`requirements`) and in
`pyproject.toml` (dev). Bump both together.

## License

MIT. Not affiliated with Hörmann. "Hörmann" and "BlueSecur" are trademarks of
their respective owners.
