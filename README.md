# SoundTouch Bridge

This repository contains a small Ubuntu-targeted service for keeping Bose
SoundTouch speakers usable after the original cloud service retirement.

The implementation currently lives in [`sixback_ubuntu`](sixback_ubuntu/).
That package name is retained for compatibility with the working prototype, but
the service, documentation, and admin UI are now named SoundTouch Bridge.

- register a SoundTouch speaker by IP,
- import its existing six presets from `:8090/presets`,
- edit TuneIn and direct-stream preset slots from a browser,
- search and assign TuneIn, SiriusXM, and iHeart stations from the admin UI,
- refresh SiriusXM playback with a local login/session manager,
- migrate the speaker over the Bose diagnostic telnet port `17000`,
- serve the local Bose cloud replacement on port `8000`,
- persist speaker and preset state in SQLite.

This is not full cloud feature parity. Spotify, DLNA browsing, SSDP
auto-discovery, OTA flows, and group orchestration are outside the current
scope.

## Quick Start

```bash
cd sixback_ubuntu
python3 -m sixback_ubuntu \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://YOUR_UBUNTU_IP:8000 \
  --db /var/lib/soundtouch-bridge/state.sqlite3
```

Then check:

```bash
curl http://YOUR_UBUNTU_IP:8000/healthz
```

Open the admin UI:

```text
http://YOUR_UBUNTU_IP:8000/admin
```

## Documentation

- [SoundTouch Bridge README](sixback_ubuntu/README.md)
- [Deployment guide](sixback_ubuntu/DEPLOYMENT.md)
- [Install from GitHub](INSTALL_FROM_GITHUB.md)

## Important Network Note

Give the Ubuntu server a stable LAN IP, preferably using a DHCP reservation.
The SoundTouch speaker stores the literal migrated cloud URL, so if the Ubuntu
server IP changes, the speaker will keep calling the old address until it is
remigrated.

Speaker IP changes are less critical for playback, but they matter for future
admin actions such as importing presets or remigrating. Re-adding the speaker
with its new IP updates the stored device entry.

## License And Attribution

SoundTouch Bridge includes original Ubuntu service work and portions derived
from or informed by public SixBack protocol work. See [`LICENSE.md`](LICENSE.md)
for this repository's license language and
[`sixback_ubuntu/SIXBACK_LICENSE`](sixback_ubuntu/SIXBACK_LICENSE) for the
SixBack license terms that continue to apply to SixBack-derived parts.
