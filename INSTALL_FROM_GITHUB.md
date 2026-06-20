# Install SoundTouch Bridge From GitHub

This guide pulls SoundTouch Bridge from GitHub onto an Ubuntu server and runs it
as a local Bose SoundTouch cloud replacement.

Examples below assume:

```text
GitHub repo: https://github.com/chrisgradz/SoundTouch.git
Ubuntu server IP: 192.168.1.25
SoundTouch speaker IP: 192.168.1.50
SoundTouch Bridge URL: http://192.168.1.25:8000
```

Replace the example IP addresses with your actual LAN addresses.

## 1. Prepare Ubuntu

```bash
sudo apt update
sudo apt install -y git python3 curl
```

Give the Ubuntu server a stable LAN IP, preferably with a DHCP reservation in
your router. This matters because the speaker stores the literal URL
`http://YOUR_UBUNTU_IP:8000` after migration.

## 2. Clone Or Update The Repository

If this is a fresh server:

```bash
cd ~
git clone git@github.com:chrisgradz/SoundTouch.git
cd SoundTouch
```

If the repository already exists on the server:

```bash
cd ~/SoundTouch
git pull origin main
```

The active Python package is:

```bash
soundtouch_bridge
```

## 3. Run The Server Manually

Start the service from the repository root:

```bash
python3 -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://192.168.1.25:8000
```

Keep this terminal open during the first test so you can see requests from the
speaker.

In a second terminal, verify that the service responds:

```bash
curl http://192.168.1.25:8000/healthz
curl http://192.168.1.25:8000/bmx/registry/v1/services
```

Open the admin UI:

```text
http://192.168.1.25:8000/admin
```

## 4. Add A SoundTouch Speaker

Find your speaker IP from your router, DHCP lease table, or existing
SoundTouch/Bose app setup.

```bash
curl -X POST http://192.168.1.25:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.50"}'
```

The response should include a `device_id`. Save that value.

## 5. Import Existing Presets

Replace `DEVICE_ID` with the value returned by the add-speaker command:

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the current presets from `http://SPEAKER_IP:8090/presets` and stores
them in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the speaker on Bose diagnostic telnet port `17000`, rewrites
the speaker's cloud URLs to `http://192.168.1.25:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before pressing preset buttons.

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

If you want native iHeart push-play experiments to use the same iHeart account
identity that was present in old Bose presets, add it too:

```bash
IHEART_SOURCE_ACCOUNT='your-iheart-login-or-source-account'
```

Then lock it down:

```bash
sudo chown root:soundtouch /etc/soundtouch-bridge/siriusxm.env
sudo chmod 640 /etc/soundtouch-bridge/siriusxm.env
```

## 8. Install As A Systemd Service

After manual testing works, install it permanently:

```bash
sudo useradd --system --home /var/lib/soundtouch-bridge --create-home soundtouch 2>/dev/null || true
sudo install -d -m 755 -o soundtouch -g soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
sudo cp -a soundtouch_bridge tools LICENSE.md licenses /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge /var/lib/soundtouch-bridge
```

Create the service file:

```bash
sudo nano /etc/systemd/system/soundtouch-bridge.service
```

Paste this, replacing the IP address if needed:

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

## 9. Updating Later

To pull future changes from GitHub:

```bash
cd ~/SoundTouch
git pull origin main
sudo systemctl stop soundtouch-bridge
sudo cp -a soundtouch_bridge tools LICENSE.md licenses /opt/soundtouch-bridge/
sudo chown -R soundtouch:soundtouch /opt/soundtouch-bridge
sudo systemctl start soundtouch-bridge
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

If migration succeeds but the speaker does not call the Ubuntu server, verify
that `--public-base` is the correct Ubuntu LAN IP and that the Ubuntu IP has not
changed.

If a speaker IP changes later, add it again with the new IP:

```bash
curl -X POST http://192.168.1.25:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"NEW_SPEAKER_IP"}'
```

The speaker's `device_id` should remain the same, so this updates the stored IP.
