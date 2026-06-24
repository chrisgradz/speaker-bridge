# Speaker Bridge Deployment Guide

This is the canonical deployment guide for installing Speaker Bridge as a
local Bose SoundTouch cloud replacement on Ubuntu.

For a GitHub clone workflow, see [INSTALL_FROM_GITHUB.md](INSTALL_FROM_GITHUB.md).

Use placeholders below:

```text
BRIDGE_IP   Ubuntu server LAN IP
SPEAKER_IP  SoundTouch speaker LAN IP
DEVICE_ID   SoundTouch speaker device ID returned by the API
```

## 1. Prepare Ubuntu

```bash
sudo apt update
sudo apt install -y python3 git curl
```

Give the Ubuntu server a stable LAN IP, preferably with a DHCP reservation in
your router. The SoundTouch speaker stores the literal cloud URL, so the bridge
IP should not change after migration.

## 2. Copy Or Clone The Project

From GitHub:

```bash
git clone git@github.com:chrisgradz/speaker-bridge.git
cd speaker-bridge
```

Or copy from a local checkout:

```bash
scp -r soundtouch_bridge LICENSE.md THIRD_PARTY_NOTICES.md licenses user@BRIDGE_IP:~/speaker-bridge/
ssh user@BRIDGE_IP
cd ~/speaker-bridge
```

## 3. Start The Server Manually

```bash
python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://BRIDGE_IP:8000
```

Verify the service:

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

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"SPEAKER_IP"}'
```

The response should include a `device_id`.

## 5. Import Existing Presets

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the speaker's current preset data from
`http://SPEAKER_IP:8090/presets` and stores it in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://BRIDGE_IP:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the Bose diagnostic shell on TCP port `17000`, rewrites the
speaker's cloud URLs to `http://BRIDGE_IP:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before testing preset buttons.

## 7. Configure Service Credentials

The credentials file is:

```text
/etc/speaker-bridge/siriusxm.env
```

Create it:

```bash
sudo install -d -m 750 -o root -g soundtouch /etc/speaker-bridge
sudo cp speaker-bridge.env.example /etc/speaker-bridge/siriusxm.env
sudo nano /etc/speaker-bridge/siriusxm.env
```

Set values as needed:

```bash
SIRIUSXM_USERNAME='your-siriusxm-login'
SIRIUSXM_PASSWORD='your-siriusxm-password'
IHEART_SOURCE_ACCOUNT='your-iheart-login-or-source-account'
```

Lock down the file:

```bash
sudo chown root:soundtouch /etc/speaker-bridge/siriusxm.env
sudo chmod 640 /etc/speaker-bridge/siriusxm.env
```

## 8. Install As A Systemd Service

```bash
sudo useradd --system --home /var/lib/speaker-bridge --create-home soundtouch 2>/dev/null || true
sudo install -d -m 755 -o soundtouch -g soundtouch /opt/speaker-bridge /var/lib/speaker-bridge
sudo cp -a soundtouch_bridge LICENSE.md THIRD_PARTY_NOTICES.md licenses /opt/speaker-bridge/
sudo chown -R soundtouch:soundtouch /opt/speaker-bridge /var/lib/speaker-bridge
```

Create `/etc/systemd/system/speaker-bridge.service`:

```ini
[Unit]
Description=Speaker Bridge
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/speaker-bridge
EnvironmentFile=-/etc/speaker-bridge/siriusxm.env
ExecStart=/usr/bin/python3 -m soundtouch_bridge --host 0.0.0.0 --port 8000 --public-base http://BRIDGE_IP:8000 --db /var/lib/speaker-bridge/state.sqlite3
Restart=on-failure
User=soundtouch
Group=soundtouch

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now speaker-bridge
sudo systemctl status speaker-bridge
```

Follow logs:

```bash
journalctl -u speaker-bridge -f
```

## 9. Firewall

If Ubuntu firewall is enabled:

```bash
sudo ufw allow from LOCAL_LAN_CIDR to any port 8000 proto tcp
```

The Ubuntu server must also be able to reach each speaker on:

```text
TCP 8090
TCP 17000
```

Do not expose port `8000` directly to the public internet.

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

Check service logs:

```bash
journalctl -u speaker-bridge -f
```
