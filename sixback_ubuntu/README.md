# SixBack Ubuntu MVP

This is a small Ubuntu-targeted MVP for replacing the core ESP32 SixBack flow
with a normal Linux service.

It supports:

- manual SoundTouch speaker IP registration,
- importing existing presets from `http://speaker-ip:8090/presets`,
- setting and clearing TuneIn or direct-stream preset slots,
- preserving and copying imported SiriusXM preset slots,
- migrating the speaker over Bose diagnostic telnet on port `17000`,
- serving the key local Bose cloud endpoints on port `8000`,
- TuneIn station resolution,
- SQLite persistence.

It does not yet attempt full SixBack parity. Spotify, the polished ESP32 Web UI,
DLNA browsing, OTA handling, SSDP auto-discovery, and group orchestration are
outside this MVP.

Imported SiriusXM presets are preserved by replaying the original Bose
`ContentItem` captured from the speaker, and the admin UI can copy them to
another slot. Creating brand-new SiriusXM presets from login or channel search
is not supported yet because that requires the authenticated SiriusXM/Bose
adapter flow rather than only a channel ID.

The MVP also includes a first-pass SiriusXM adapter endpoint for preserved
presets:

```text
/core02/svc-bmx-adapter-siriusxm-everest-eco1/prod/live-adapter/playback/station/{channel}
```

This removes the local `404` when a preserved SiriusXM preset is pressed. If the
speaker then requests `/siriusxm/needs-auth/{channel}`, the remaining missing
piece is authenticated SiriusXM stream URL resolution.

Store SiriusXM channel metadata from the web player:

```bash
curl -X PUT http://localhost:8000/api/siriusxm/channels/firstwave \
  -H 'Content-Type: application/json' \
  -d '{"name":"1st Wave","entity_url":"https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e"}'
```

That web player URL is not a direct audio stream. When a real authenticated
audio URL is available, add it as `stream_url`:

```bash
curl -X PUT http://localhost:8000/api/siriusxm/channels/firstwave \
  -H 'Content-Type: application/json' \
  -d '{"name":"1st Wave","entity_url":"https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e","stream_url":"https://example.com/authenticated-audio.m3u8"}'
```

You can also import a SiriusXM web-player HAR capture. The importer extracts the
HLS playlist URL and stores it as the current `stream_url`. HAR files may contain
session-like URLs, so they are ignored by git.

```bash
cd /opt/sixback_ubuntu
sudo systemctl stop sixback-ubuntu
sudo -u sixback python3 tools/import_siriusxm_har.py \
  --db /var/lib/sixback-ubuntu/state.sqlite3 \
  --station-id firstwave \
  --entity-url https://www.siriusxm.com/player/channel-linear/entity/65f04311-3581-256c-97b9-279838d6ff5e \
  /path/to/www.siriusxm.com.har
sudo systemctl start sixback-ubuntu
```

If the speaker still fails to play after importing a HAR, the captured playlist
URL may have expired or may require browser-only TLS/header behavior. Capture a
fresh HAR while the web player is actively playing and import it again.

Inspect recent speaker event payloads after a failed playback attempt:

```bash
curl http://localhost:8000/api/speakers/DEVICE_ID/events
```

The service also prints compact `[scmudc]` event summaries to `journalctl`.

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

Set a TuneIn preset:

```bash
curl -X PUT http://localhost:8000/api/speakers/DEVICE_ID/presets/1 \
  -H 'Content-Type: application/json' \
  -d '{"source":"TUNEIN","name":"Jazz","station_id":"s12345"}'
```

Set a direct stream preset:

```bash
curl -X PUT http://localhost:8000/api/speakers/DEVICE_ID/presets/2 \
  -H 'Content-Type: application/json' \
  -d '{"source":"LOCAL_INTERNET_RADIO","name":"Local Stream","stream_url":"https://example.com/stream.mp3"}'
```

Clear a preset:

```bash
curl -X DELETE http://localhost:8000/api/speakers/DEVICE_ID/presets/2
```

Copy an imported preset, including preserved SiriusXM raw metadata, from one
slot to another:

```bash
curl -X POST http://localhost:8000/api/speakers/DEVICE_ID/presets/4/copy \
  -H 'Content-Type: application/json' \
  -d '{"source_slot":3}'
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
