# Contributing

Thanks for helping improve Speaker Bridge.

## Development Setup

The project currently uses Python standard-library modules only.

Run tests from the repository root:

```bash
python -m unittest discover -s tests
```

Run the bridge locally:

```bash
python -m soundtouch_bridge \
  --host 0.0.0.0 \
  --port 8000 \
  --public-base http://BRIDGE_IP:8000
```

## Pull Requests

- Keep changes focused.
- Include tests for behavior changes.
- Do not commit credentials, browser session captures, speaker exports, local
  SQLite databases, or private network details.
- Keep docs in sync when command names, service names, env variables, or routes
  change.

## Testing Against Real Hardware

Real SoundTouch hardware behavior can vary by model and firmware. When a
change depends on device behavior, include:

- speaker model if known,
- firmware version if known,
- source type tested,
- relevant service logs with sensitive values removed.
