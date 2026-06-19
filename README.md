# SoundTouch Ubuntu Cloud MVP

This repository contains a small Ubuntu-targeted MVP for keeping Bose
SoundTouch speakers usable after the original cloud service retirement.

The implementation lives in [`sixback_ubuntu`](sixback_ubuntu/). It ports the
core SixBack idea from ESP32 firmware into a normal Linux service:

- register a SoundTouch speaker by IP,
- import its existing six presets from `:8090/presets`,
- edit TuneIn and direct-stream preset slots from a browser,
- preserve imported SiriusXM presets and refresh playback with a local
  SiriusXM login/session manager,
- migrate the speaker over the Bose diagnostic telnet port `17000`,
- serve the local Bose cloud replacement on port `8000`,
- resolve TuneIn preset playback requests,
- persist speaker and preset state in SQLite.

This is an MVP, not full SixBack feature parity. Spotify, DLNA browsing, SSDP
auto-discovery, OTA flows, and SiriusXM channel search are not included yet.

## Quick Start

```bash
cd sixback_ubuntu
python3 -m sixback_ubuntu \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://YOUR_UBUNTU_IP:8000
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

- [Ubuntu MVP README](sixback_ubuntu/README.md)
- [Deployment guide](sixback_ubuntu/DEPLOYMENT.md)

## Important Network Note

Give the Ubuntu server a stable LAN IP, preferably using a DHCP reservation.
The SoundTouch speaker stores the literal migrated cloud URL, so if the Ubuntu
server IP changes, the speaker will keep calling the old address until it is
remigrated.

Speaker IP changes are less critical for playback, but they matter for future
admin actions such as importing presets or remigrating. Re-adding the speaker
with its new IP updates the stored device entry.

## License

This MVP is derived from public SixBack protocol work and includes SixBack data
assets. See [`sixback_ubuntu/SIXBACK_LICENSE`](sixback_ubuntu/SIXBACK_LICENSE);
noncommercial terms apply to those parts.
