# SoundTouch Bridge

SoundTouch Bridge is an Ubuntu-targeted local cloud replacement for Bose
SoundTouch speakers.

It supports:

- registering SoundTouch speakers by IP,
- importing existing presets from `:8090/presets`,
- editing TuneIn, SiriusXM, iHeart, and direct-stream preset slots from a browser,
- refreshing SiriusXM playback with a local login/session manager,
- migrating the speaker over the Bose diagnostic telnet port `17000`,
- serving the local Bose cloud replacement on port `8000`,
- persisting speaker and preset state in SQLite.

The active Python package is [`soundtouch_bridge`](soundtouch_bridge/), so the
manual run command is:

```bash
python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://YOUR_UBUNTU_IP:8000 \
  --db /var/lib/soundtouch-bridge/state.sqlite3
```

The SiriusXM env file is located at:

```text
/etc/soundtouch-bridge/siriusxm.env
```

Open the admin UI:

```text
http://YOUR_UBUNTU_IP:8000/admin
```

## Documentation

- [Install from GitHub](INSTALL_FROM_GITHUB.md)
- [Deployment guide](DEPLOYMENT.md)

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
[`licenses/SIXBACK_LICENSE`](licenses/SIXBACK_LICENSE) for the SixBack license
terms that continue to apply to SixBack-derived parts.
