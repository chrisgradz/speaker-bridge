# SoundTouch Bridge

SoundTouch Bridge is a local Ubuntu service for keeping Bose SoundTouch
speakers usable when the original cloud endpoints are unavailable.

It provides a small local replacement for the SoundTouch cloud APIs, plus
browser-based tools for presets, station search, and push-to-speaker playback.

## Features

| Area | Status |
| --- | --- |
| Local SoundTouch cloud endpoints | Working |
| Speaker registration by IP | Working |
| Existing preset import from `:8090/presets` | Working |
| Speaker migration over diagnostic telnet port `17000` | Working |
| SQLite persistence | Working |
| `/admin` preset editor | Working |
| `/play` push-to-speaker page | Working |
| SiriusXM login, catalog, presets, and playback | Working with valid account |
| TuneIn search, presets, and playback | Working |
| iHeart search, presets, and playback | Working |

The service is designed for trusted LAN use. Do not expose it directly to the
public internet.

## Quick Start

Install on an Ubuntu server:

```bash
sudo apt update
sudo apt install -y git python3 curl

git clone git@github.com:chrisgradz/SoundTouch.git
cd SoundTouch

python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://BRIDGE_IP:8000
```

Open:

```text
http://BRIDGE_IP:8000/admin
http://BRIDGE_IP:8000/play
```

Use a stable LAN IP or DHCP reservation for the Ubuntu server. Migrated
SoundTouch speakers store the literal bridge URL.

## Documentation

- [Install from GitHub](INSTALL_FROM_GITHUB.md)
- [Deployment guide](DEPLOYMENT.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md)

## Configuration

Authenticated services read credentials from:

```text
/etc/soundtouch-bridge/siriusxm.env
```

Start from the example file:

```bash
sudo install -d -m 750 -o root -g soundtouch /etc/soundtouch-bridge
sudo cp soundtouch-bridge.env.example /etc/soundtouch-bridge/siriusxm.env
sudo nano /etc/soundtouch-bridge/siriusxm.env
sudo chown root:soundtouch /etc/soundtouch-bridge/siriusxm.env
sudo chmod 640 /etc/soundtouch-bridge/siriusxm.env
```

## Network Notes

The Ubuntu server should have a stable LAN IP. If the bridge IP changes, the
speaker will continue calling the old migrated cloud URL until it is migrated
again.

Speaker IP changes are less critical for playback, but they matter for admin
actions such as importing presets, storing presets on the speaker, or
remigrating. Re-add the speaker with its new IP to update the stored entry.

## Testing

Run the unit tests:

```bash
python -m unittest discover -s tests
```

## Known Limitations

- This project depends on observed SoundTouch behavior and may need adjustment
  for different models or firmware versions.
- SiriusXM playback requires a valid streaming account.
- Third-party service APIs and stream formats can change.
- The `/admin` page is responsive but best suited for desktop or tablet use.
  The `/play` page is the better phone-facing surface.

## Attribution And License

SoundTouch Bridge includes original Ubuntu service work and portions derived
from or informed by public SixBack protocol research.

See:

- [LICENSE.md](LICENSE.md)
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- [licenses/SIXBACK_LICENSE](licenses/SIXBACK_LICENSE)

SoundTouch Bridge is not affiliated with, endorsed by, or sponsored by Bose,
SiriusXM, TuneIn, or iHeart. Users are responsible for complying with the terms
of any services they configure or access through this project.
