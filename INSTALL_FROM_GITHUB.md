# Install From GitHub

This guide pulls the SoundTouch Ubuntu MVP from GitHub onto an Ubuntu server and
runs it as a local Bose SoundTouch cloud replacement.

Examples below assume:

```text
GitHub repo: https://github.com/chrisgradz/SoundTouch.git
Ubuntu server IP: 192.168.1.25
SoundTouch speaker IP: 192.168.1.50
SixBack cloud URL: http://192.168.1.25:8000
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

## 2. Clone The Repository

If this is a fresh server:

```bash
cd ~
git clone https://github.com/chrisgradz/SoundTouch.git
cd SoundTouch
```

If GitHub prompts for a password and then says password authentication is not
supported, use one of these options:

### Option A: Clone With A GitHub Personal Access Token

Create a GitHub Personal Access Token with repository read access, then clone
with your GitHub username and the token as the password.

```bash
cd ~
git clone https://github.com/chrisgradz/SoundTouch.git
```

When prompted:

```text
Username for 'https://github.com': chrisgradz
Password for 'https://chrisgradz@github.com': PASTE_YOUR_GITHUB_TOKEN
```

Do not use your GitHub account password; GitHub rejects passwords for Git.

### Option B: Clone With SSH

If the Ubuntu server has an SSH key added to GitHub:

```bash
cd ~
git clone git@github.com:chrisgradz/SoundTouch.git
cd SoundTouch
```

### Option C: Make The Repo Public

If the repository does not need to be private, make it public in GitHub. Then
the HTTPS clone should work without a username or token:

```bash
git clone https://github.com/chrisgradz/SoundTouch.git
```

If the repository already exists on the server:

```bash
cd ~/SoundTouch
git pull origin main
```

The Ubuntu MVP is in:

```bash
cd sixback_ubuntu
```

## 3. Run The Server Manually

Start the service from the `sixback_ubuntu` folder:

```bash
python3 -m sixback_ubuntu \
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

The first command should return JSON with `"ok": true`. The second should return
the Bose BMX service registry JSON.

## 4. Add A SoundTouch Speaker

Find your speaker IP from your router, DHCP lease table, or existing
SoundTouch/Bose app setup.

```bash
curl -X POST http://192.168.1.25:8000/api/speakers \
  -H 'Content-Type: application/json' \
  -d '{"ip":"192.168.1.50"}'
```

The response should include a `device_id`. Save that value.

You can list registered speakers with:

```bash
curl http://192.168.1.25:8000/api/speakers
```

## 5. Import Existing Presets

Replace `DEVICE_ID` with the value returned by the add-speaker command:

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/import-presets
```

This reads the current presets from:

```text
http://SPEAKER_IP:8090/presets
```

and stores them in SQLite.

## 6. Migrate The Speaker

```bash
curl -X POST http://192.168.1.25:8000/api/speakers/DEVICE_ID/migrate
```

This connects to the speaker on Bose diagnostic telnet port `17000`, rewrites
the speaker's cloud URLs to `http://192.168.1.25:8000`, and reboots the speaker.

Wait 1-2 minutes after migration before pressing preset buttons.

## 7. Watch The First Speaker Requests

After the speaker reboots, the Python server should show requests like:

```text
/bmx/registry/v1/services
/streaming/account/.../full
/streaming/account/.../device/.../presets
/bmx/tunein/v1/playback/station/...
/v1/scmudc/...
```

Press one of the six preset buttons and watch the server output.

## 8. Install As A Systemd Service

After manual testing works, install it permanently:

```bash
sudo useradd --system --home /var/lib/sixback-ubuntu --create-home sixback
sudo mkdir -p /opt/sixback_ubuntu
sudo cp -a ~/SoundTouch/sixback_ubuntu/. /opt/sixback_ubuntu/
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

Create the service file:

```bash
sudo nano /etc/systemd/system/sixback-ubuntu.service
```

Paste this, replacing the IP address if needed:

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

Check SiriusXM auth status. The login command is optional; playback will also
log in automatically when a SiriusXM preset is pressed after a service restart.

```bash
curl http://192.168.1.25:8000/api/siriusxm/session
curl -X POST http://192.168.1.25:8000/api/siriusxm/session/login
```

## 9. Firewall

If Ubuntu firewall is enabled:

```bash
sudo ufw allow 8000/tcp
```

The Ubuntu server must also be able to reach each speaker on:

```text
TCP 8090
TCP 17000
```

## 10. Updating Later

To pull future changes from GitHub:

```bash
cd ~/SoundTouch
git pull origin main
sudo systemctl stop sixback-ubuntu
sudo cp -a sixback_ubuntu/. /opt/sixback_ubuntu/
sudo chown -R sixback:sixback /opt/sixback_ubuntu
sudo systemctl start sixback-ubuntu
```

## 11. Troubleshooting

Check speaker reachability:

```bash
curl http://192.168.1.50:8090/info
curl http://192.168.1.50:8090/presets
```

Check server state:

```bash
curl http://192.168.1.25:8000/api/speakers
```

Check service logs:

```bash
journalctl -u sixback-ubuntu -f
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
