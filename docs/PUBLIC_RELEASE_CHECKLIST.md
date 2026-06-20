# Public Release Checklist

Use this checklist before changing the GitHub repository visibility to public.

## Pre-Release Secret And Privacy Audit

- Confirm ignored local files are not tracked:

  ```bash
  git status --short
  git ls-files
  ```

- Confirm private local artifacts were never committed:

  ```bash
  git log --all -- Current_speaker_presets.json
  ```

- Search tracked files for private values before release:

  ```bash
  git grep -n -E "password|token|secret|cookie|authorization|real-account|real-device-id"
  ```

- Do not publish speaker preset exports, account IDs, speaker device IDs,
  cookies, browser session captures, local SQLite databases, or real
  credentials.

## Required Public-Facing Files

- `SECURITY.md`
- `soundtouch-bridge.env.example`
- `THIRD_PARTY_NOTICES.md`
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `.github/workflows/ci.yml`

## README Polish

- Clear project summary.
- Feature table.
- Quick start.
- Configuration section.
- Network notes.
- Testing command.
- Known limitations.
- License and attribution.
- Unofficial-project disclaimer.

## Documentation Cleanup

- Use `BRIDGE_IP`, `SPEAKER_IP`, and `DEVICE_ID` placeholders.
- Keep one canonical deployment guide.
- Keep GitHub install steps in `INSTALL_FROM_GITHUB.md`.
- Ensure service names consistently use `soundtouch-bridge`.
- Document the env file location clearly:

  ```text
  /etc/soundtouch-bridge/siriusxm.env
  ```

## Code And Tooling Review

- Keep browser-session import tooling out of the public repo.
- Confirm the project still runs without external Python dependencies beyond
  the standard library, or add dependency documentation if that changes.
- Run the full test suite before release:

  ```bash
  python -m unittest discover -s tests
  ```

## GitHub Repository Settings

- Add a short repository description.
- Add topics such as:
  - `bose-soundtouch`
  - `soundtouch`
  - `python`
  - `home-audio`
  - `siriusxm`
  - `tunein`
  - `iheart`
- Consider adding issue templates after the first public release.

## Release Gate

Do not make the repository public until:

- Secret/history audit is clean.
- Public-facing docs are in place.
- License and third-party notices are clear.
- Tests pass.
- Sensitive local files are confirmed untracked.
