# Install SoundTouch Bridge From GitHub

This guide installs SoundTouch Bridge from GitHub onto an Ubuntu server.

Use placeholders below:

```text
BRIDGE_IP   Ubuntu server LAN IP
SPEAKER_IP  SoundTouch speaker LAN IP
DEVICE_ID   SoundTouch speaker device ID returned by the API
```

Example bridge URL:

```text
http://BRIDGE_IP:8000
```

## 1. Prepare Ubuntu

```bash
sudo apt update
sudo apt install -y git python3 curl
```

Give the Ubuntu server a stable LAN IP, preferably with a DHCP reservation.
Migrated speakers store the literal bridge URL.

## 2. Clone Or Update The Repository

Fresh install:

```bash
cd ~
git clone git@github.com:chrisgradz/SoundTouch.git
cd SoundTouch
```

Existing checkout:

```bash
cd ~/SoundTouch
git pull origin main
```

## 3. Run The Server Manually

```bash
python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://BRIDGE_IP:8000
```

Verify from another terminal:

```bash
curl http://BRIDGE_IP:8000/healthz
curl http://BRIDGE_IP:8000/bmx/registry/v1/services
```

Open:

```text
http://BRIDGE_IP:8000/admin
http://BRIDGE_IP:8000/play
```

## 4. Add A SoundTouch Speaker

Find the speaker IP from your router, DHCP lease table, or existing app setup.

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"SPEAKER_IP"}'
```

Save the `device_id` from the response.

## 5. Import Existing Presets

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the current presets from `http://SPEAKER_IP:8090/presets` and stores
them in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the speaker on Bose diagnostic telnet port `17000`, rewrites
the speaker's cloud URLs to `http://BRIDGE_IP:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before pressing presets.

## 7. Configure Service Credentials

Create the env file:

```bash
sudo install -d -m 750 -o root -g soundtouch /etc/soundtouch-bridge
sudo cp soundtouch-bridge.env.example /etc/soundtouch-bridge/siriusxm.env
sudo nano /etc/soundtouch-bridge/siriusxm.env
```

Set values as needed:

```bash
SIRIUSXM_USERNAME='your-siriusxm-login'
SIRIUSXM_PASSWORD='your-siriusxm-password'
IHEART_SOURCE_ACCOUNT='your-iheart-login-or-source-account'
```

Lock down the file:

```bash
sudo chown root:soundtouch /etc/soundtouch-bridge/siriusxm.env
sudo chmod 640 /etc/soundtouch-bridge/siriusxm.env
```

## 8. Install As A Systemd Service

```bash
sudo useradd --system --home /var/lib/soundtouch-bridge --create-home soundtouch 2>/dev/null || true
sudo install -d -m 755 -o soundtouch -g soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
sudo cp -a soundtouch_bridge LICENSE.md THIRD_PARTY_NOTICES.md licenses /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
```

Create the service:

```bash
sudo nano /etc/systemd/system/soundtouch-bridge.service
```

Paste this, replacing `BRIDGE_IP`:

```ini
[Unit]
Description=SoundTouch Bridge
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/soundtouch-bridge
EnvironmentFile=-/etc/soundtouch-bridge/siriusxm.env
ExecStart=/usr/bin/python3 -m soundtouch_bridge --host 0.0.0.0 --port 8000 --public-base http://BRIDGE_IP:8000 --db /var/lib/soundtouch-bridge/state.sqlite3
Restart=on-failure
User=soundtouch
Group=soundtouch

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now soundtouch-bridge
sudo systemctl status soundtouch-bridge
```

Follow logs:

```bash
journalctl -u soundtouch-bridge -f
```

## 9. Updating Later

```bash
cd ~/SoundTouch
git pull origin main
sudo systemctl stop soundtouch-bridge
sudo cp -a soundtouch_bridge LICENSE.md THIRD_PARTY_NOTICES.md licenses /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge
sudo systemctl start soundtouch-bridge
```

## 10. Troubleshooting

Check speaker reachability:

```bash
curl http://SPEAKER_IP:8090/info
curl http://SPEAKER_IP:8090/presets
```

Check bridge state:

```bash
curl http://BRIDGE_IP:8000/api/speakers
```

Check SiriusXM auth status:

```bash
curl http://BRIDGE_IP:8000/api/siriusxm/session
curl -X POST http://BRIDGE_IP:8000/api/siriusxm/session/login
```

If migration succeeds but the speaker does not call the bridge, verify that
`--public-base` uses the correct stable Ubuntu LAN IP.
