# SoundTouch Bridge

This is a small Ubuntu-targeted service for replacing the core SoundTouch cloud
calls with a local Linux service.

The Python package is still named `sixback_ubuntu` for compatibility with the
working prototype. Operationally, the service is SoundTouch Bridge.

It supports:

- manual SoundTouch speaker IP registration,
- importing existing presets from `http://speaker-ip:8090/presets`,
- setting and clearing TuneIn, SiriusXM, iHeart, and direct-stream presets,
- authenticating to SiriusXM from a root-owned env file and refreshing HLS
  playlist URLs when presets are pressed,
- migrating the speaker over Bose diagnostic telnet on port `17000`,
- serving the key local Bose cloud endpoints on port `8000`,
- SQLite persistence.

It does not attempt full cloud feature parity. Spotify, DLNA browsing, OTA
handling, SSDP auto-discovery, and group orchestration are outside this service.

## SiriusXM Login

Create a root-owned env file on the Ubuntu server:

```bash
sudo install -d -m 750 -o root -g soundtouch /etc/soundtouch-bridge
sudo nano /etc/soundtouch-bridge/siriusxm.env
```

Add:

```bash
SIRIUSXM_USERNAME='your-siriusxm-login'
SIRIUSXM_PASSWORD='your-siriusxm-password'
```

Lock it down:

```bash
sudo chown root:soundtouch /etc/soundtouch-bridge/siriusxm.env
sudo chmod 640 /etc/soundtouch-bridge/siriusxm.env
```

Add this line to the `[Service]` section of
`/etc/systemd/system/soundtouch-bridge.service`:

```ini
EnvironmentFile=-/etc/soundtouch-bridge/siriusxm.env
```

Then restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart soundtouch-bridge
```

Check redacted auth status. The login call is optional; playback will also log
in automatically when a SiriusXM preset is pressed.

```bash
curl http://localhost:8000/api/siriusxm/session
curl -X POST http://localhost:8000/api/siriusxm/session/login
```

## Run

Use a stable LAN IP for the Ubuntu host. A DHCP reservation is strongly
recommended because the SoundTouch speaker stores the literal cloud URL.

```bash
cd /path/to/SoundTouch/sixback_ubuntu
python3 -m sixback_ubuntu --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000
```

If `--public-base` is omitted, the server guesses a LAN IP, but an explicit
value is safer.

Open the admin UI:

```text
http://192.168.1.25:8000/admin
```

## Basic Flow

Add a speaker by IP:

```bash
curl -X POST http://localhost:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.50"}'
```

Import the existing six hardware presets:

```bash
curl -X POST http://localhost:8000/api/speakers/DEVICE_ID/import-presets
```

Migrate the speaker to this Ubuntu service:

```bash
curl -X POST http://localhost:8000/api/speakers/DEVICE_ID/migrate
```

After migration, the speaker is rebooted and should call this server for:

- `/bmx/registry/v1/services`
- `/streaming/account/{account_id}/full`
- `/streaming/account/{account_id}/device/{device_id}/presets`
- `/bmx/tunein/v1/playback/station/{station_id}`

The browser admin UI can then search and save TuneIn, SiriusXM, and iHeart
stations to preset slots.

## Useful Checks

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/speakers
curl http://localhost:8000/bmx/registry/v1/services
```

## Install As A Service

Create `/etc/systemd/system/soundtouch-bridge.service`:

```ini
[Unit]
Description=SoundTouch Bridge
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/soundtouch-bridge
EnvironmentFile=-/etc/soundtouch-bridge/siriusxm.env
ExecStart=/usr/bin/python3 -m sixback_ubuntu --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000 --db /var/lib/soundtouch-bridge/state.sqlite3
Restart=on-failure
User=soundtouch
Group=soundtouch

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo useradd --system --home /var/lib/soundtouch-bridge --create-home soundtouch 2>/dev/null || true
sudo install -d -m 755 -o soundtouch -g soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
sudo cp -a . /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
sudo systemctl daemon-reload
sudo systemctl enable --now soundtouch-bridge
```

## License And Attribution

This service includes original Ubuntu service work and portions derived from or
informed by public SixBack protocol work. See the root `LICENSE.md` for this
repository's license language and `SIXBACK_LICENSE` for the SixBack license
terms that continue to apply to SixBack-derived parts.
