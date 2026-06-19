# SixBack Ubuntu Deployment Guide

This guide installs the Ubuntu MVP service for SoundTouch speakers.

The examples assume:

```text
Ubuntu server IP: 192.168.1.25
SixBack cloud URL: http://192.168.1.25:8000
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

Copy the local `sixback_ubuntu` folder to the Ubuntu server.

From the machine that has this workspace:

```bash
scp -r sixback_ubuntu user@192.168.1.25:~/soundtouch/
```

Then SSH into the Ubuntu server:

```bash
ssh user@192.168.1.25
cd ~/soundtouch/sixback_ubuntu
```

## 3. Start The Server Manually

```bash
python3 -m sixback_ubuntu \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://192.168.1.25:8000
```

In another terminal, verify the service:

```bash
curl http://192.168.1.25:8000/healthz
curl http://192.168.1.25:8000/bmx/registry/v1/services
```

The first command should return JSON with `"ok": true`. The second should return
the Bose BMX service registry JSON.

## 4. Add A SoundTouch Speaker

Find the speaker IP from your router, DHCP lease table, or existing SoundTouch
app setup.

```bash
curl -X POST http://192.168.1.25:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.50"}'
```

The response should include a `device_id`. Use that value in the next commands.

You can list registered speakers with:

```bash
curl http://192.168.1.25:8000/api/speakers
```

## 5. Import Existing Presets

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the speaker's current preset data from:

```text
http://SPEAKER_IP:8090/presets
```

and stores it in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the Bose diagnostic shell on TCP port `17000`, rewrites the
speaker's cloud URLs to `http://192.168.1.25:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before testing preset buttons.

## 7. Watch Logs During First Test

Keep the manual Python process visible during the first test. The speaker should
call endpoints like:

```text
/bmx/registry/v1/services
/streaming/account/.../full
/streaming/account/.../device/.../presets
/bmx/tunein/v1/playback/station/...
/v1/scmudc/...
```

Press one of the speaker's six preset buttons and watch the server output.

## 8. Install As A Systemd Service

After manual testing works, install the service permanently.

```bash
sudo useradd --system --home /var/lib/sixback-ubuntu --create-home sixback
sudo mkdir -p /opt/sixback_ubuntu
sudo cp -a ~/soundtouch/sixback_ubuntu/. /opt/sixback_ubuntu/
sudo chown -R sixback:sixback /opt/sixback_ubuntu /var/lib/sixback-ubuntu
```

Optional but recommended for SiriusXM presets:

```bash
sudo install -d -m 750 -o root -g sixback /etc/sixback-ubuntu
sudo nano /etc/sixback-ubuntu/siriusxm.env
```

Put your SiriusXM streaming login in that file:

```bash
SIRIUSXM_USERNAME='your-siriusxm-login'
SIRIUSXM_PASSWORD='your-siriusxm-password'
```

Then lock it down:

```bash
sudo chown root:sixback /etc/sixback-ubuntu/siriusxm.env
sudo chmod 640 /etc/sixback-ubuntu/siriusxm.env
```

Create `/etc/systemd/system/sixback-ubuntu.service`:

```bash
sudo nano /etc/systemd/system/sixback-ubuntu.service
```

Paste:

```ini
[Unit]
Description=SixBack Ubuntu MVP
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/sixback_ubuntu
EnvironmentFile=-/etc/sixback-ubuntu/siriusxm.env
ExecStart=/usr/bin/python3 -m sixback_ubuntu --host 0.0.0.0 --port 8000 --public-base http://192.168.1.25:8000 --db /var/lib/sixback-ubuntu/state.sqlite3
Restart=on-failure
User=sixback
Group=sixback

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sixback-ubuntu
sudo systemctl status sixback-ubuntu
```

Follow logs:

```bash
journalctl -u sixback-ubuntu -f
```

Check SiriusXM auth status:

```bash
curl http://192.168.1.25:8000/api/siriusxm/session
curl -X POST http://192.168.1.25:8000/api/siriusxm/session/login
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

Check MVP state:

```bash
curl http://192.168.1.25:8000/api/speakers
```

Check service logs:

```bash
journalctl -u sixback-ubuntu -f
```

If migration succeeds but the speaker does not call the Ubuntu server, confirm
that `--public-base` used the correct Ubuntu LAN IP and that the server IP has
not changed.
