# SoundTouch Bridge Deployment Guide

This guide installs SoundTouch Bridge as a local Bose SoundTouch cloud
replacement on Ubuntu.

The examples assume:

```text
Ubuntu server IP: 192.168.1.25
SoundTouch Bridge URL: http://192.168.1.25:8000
Speaker IP: 192.168.1.50
```

Replace those values with your actual LAN addresses.

## 1. Prepare Ubuntu

```bash
sudo apt update
sudo apt install -y python3 git curl
```

Give the Ubuntu server a stable LAN IP, preferably with a DHCP reservation in
your router. The SoundTouch speaker stores the literal cloud URL, so the server
IP should not change after migration.

## 2. Copy The Project

Copy the local `soundtouch_bridge` package and support files to the Ubuntu
server.

```bash
scp -r soundtouch_bridge tools LICENSE.md licenses user@192.168.1.25:~/soundtouch-bridge/
ssh user@192.168.1.25
cd ~/soundtouch-bridge
```

## 3. Start The Server Manually

```bash
python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://192.168.1.25:8000
```

In another terminal, verify the service:

```bash
curl http://192.168.1.25:8000/healthz
curl http://192.168.1.25:8000/bmx/registry/v1/services
```

Open the admin UI:

```text
http://192.168.1.25:8000/admin
```

## 4. Add A SoundTouch Speaker

Find the speaker IP from your router, DHCP lease table, or existing SoundTouch
app setup.

```bash
curl -X POST http://192.168.1.25:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.50"}'
```

The response should include a `device_id`. Use that value in the next commands.

## 5. Import Existing Presets

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the speaker's current preset data from
`http://SPEAKER_IP:8090/presets` and stores it in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the Bose diagnostic shell on TCP port `17000`, rewrites the
speaker's cloud URLs to `http://192.168.1.25:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before testing preset buttons.

## 7. SiriusXM Env File

The SiriusXM credentials file is:

```text
/etc/soundtouch-bridge/siriusxm.env
```

Create it with:

```bash
sudo install -d -m 750 -o root -g soundtouch /etc/soundtouch-bridge
sudo nano /etc/soundtouch-bridge/siriusxm.env
```

Put your SiriusXM streaming login in that file:

```bash
SIRIUSXM_USERNAME='your-siriusxm-login'
SIRIUSXM_PASSWORD='your-siriusxm-password'
```

Then lock it down:

```bash
sudo chown root:soundtouch /etc/soundtouch-bridge/siriusxm.env
sudo chmod 640 /etc/soundtouch-bridge/siriusxm.env
```

## 8. Install As A Systemd Service

After manual testing works, install the service permanently:

```bash
sudo useradd --system --home /var/lib/soundtouch-bridge --create-home soundtouch 2>/dev/null || true
sudo install -d -m 755 -o soundtouch -g soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
sudo cp -a soundtouch_bridge tools LICENSE.md licenses /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
```

Create `/etc/systemd/system/soundtouch-bridge.service`:

```ini
[Unit]
Description=SoundTouch Bridge
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/soundtouch-bridge
EnvironmentFile=-/etc/soundtouch-bridge/siriusxm.env
ExecStart=/usr/bin/python3 -m soundtouch_bridge --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000 --db /var/lib/soundtouch-bridge/state.sqlite3
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

## 9. Firewall

If Ubuntu firewall is enabled:

```bash
sudo ufw allow 8000/tcp
```

The Ubuntu server must also be able to reach the speaker on:

```text
TCP 8090
TCP 17000
```

## 10. Troubleshooting

Check speaker reachability:

```bash
curl http://192.168.1.50:8090/info
curl http://192.168.1.50:8090/presets
```

Check server state:

```bash
curl http://192.168.1.25:8000/api/speakers
```

Check SiriusXM auth status:

```bash
curl http://192.168.1.25:8000/api/siriusxm/session
curl -X POST http://192.168.1.25:8000/api/siriusxm/session/login
```

Check service logs:

```bash
journalctl -u soundtouch-bridge -f
```
