# Security Policy

## Intended Deployment

Speaker Bridge is intended for trusted home or lab LAN use only. Do not
expose the bridge directly to the public internet.

The bridge can:

- accept commands that make SoundTouch speakers play audio,
- store speaker metadata and presets in SQLite,
- use configured service credentials to refresh authenticated streams,
- expose local admin and play pages on the configured HTTP port.

Keep the service bound to a trusted network and protect it with your firewall,
VPN, or reverse proxy access controls if you need remote access.

## Credentials

Service credentials are read from:

```text
/etc/speaker-bridge/siriusxm.env
```

Recommended permissions:

```bash
sudo chown root:soundtouch /etc/speaker-bridge
sudo chmod 750 /etc/speaker-bridge
sudo chown root:soundtouch /etc/speaker-bridge/siriusxm.env
sudo chmod 640 /etc/speaker-bridge/siriusxm.env
```

The service user needs directory traverse access to `/etc/speaker-bridge`;
otherwise startup can fail with a permission error even when `siriusxm.env`
itself has the correct group and mode.

Diagnostic endpoints that expose stored speaker events or cloud responses are
disabled unless `SPEAKER_BRIDGE_DIAGNOSTIC_TOKEN` is set. Send the token as
`Authorization: Bearer <token>` or `X-Speaker-Bridge-Token: <token>` when
debugging.

Never commit real credentials, speaker preset exports, account IDs, device IDs,
cookies, or browser session captures.

## Reporting A Vulnerability

Open a GitHub issue if the report does not include sensitive details. If the
report includes credentials, private network details, or exploitable steps,
contact the maintainer privately first.

Please include:

- affected version or commit,
- deployment environment,
- steps to reproduce,
- expected and actual behavior,
- any relevant logs with secrets removed.

## Supported Versions

This project is early-stage community software. Security fixes are applied to
the main branch unless a release branch policy is added later.
