# SixBack Ubuntu MVP

This is a small Ubuntu-targeted MVP for replacing the core ESP32 SixBack flow
with a normal Linux service.

It supports:

- manual SoundTouch speaker IP registration,
- importing existing presets from `http://speaker-ip:8090/presets`,
- migrating the speaker over Bose diagnostic telnet on port `17000`,
- serving the key local Bose cloud endpoints on port `8000`,
- TuneIn station resolution,
- SQLite persistence.

It does not yet attempt full SixBack parity. Spotify, the polished ESP32 Web UI,
DLNA browsing, OTA handling, SSDP auto-discovery, and group orchestration are
outside this MVP.

This MVP is derived from the public SixBack protocol work and includes SixBack
data assets. See `SIXBACK_LICENSE`; noncommercial terms apply to those parts.

## Run

Use a stable LAN IP for the Ubuntu host. A DHCP reservation is strongly
recommended because the SoundTouch speaker stores the literal cloud URL.

```bash
cd /path/to/SoundTouch/sixback_ubuntu
python3 -m sixback_ubuntu --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000
```

If `--public-base` is omitted, the server guesses a LAN IP, but an explicit
value is safer.

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

## Useful Checks

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/speakers
curl http://localhost:8000/bmx/registry/v1/services
```

## Install As A Service

Create `/etc/systemd/system/sixback-ubuntu.service`:

```ini
[Unit]
Description=SixBack Ubuntu MVP
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/sixback_ubuntu
ExecStart=/usr/bin/python3 -m sixback_ubuntu --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000 --db /var/lib/sixback-ubuntu/state.sqlite3
Restart=on-failure
User=sixback
Group=sixback

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo useradd --system --home /var/lib/sixback-ubuntu --create-home sixback
sudo mkdir -p /opt/sixback_ubuntu
sudo cp -a . /opt/sixback_ubuntu/
sudo systemctl daemon-reload
sudo systemctl enable --now sixback-ubuntu
```
