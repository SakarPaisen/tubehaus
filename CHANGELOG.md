# Changelog

## Unreleased

### Added
- `--doctor` self-check: `docker compose exec tubehaus python3 /app/server/tubehaus_server.py --doctor` tests WARP,
  YouTube, Invidious, a real video lookup and the optional tools
- `GET /api/p/health` and a Docker healthcheck for the TubeHaus container
- Optional built-in login for the docker compose setup (`TUBEHAUS_AUTH=basic`)
- [Troubleshooting](docs/TROUBLESHOOTING.md) and [Updating and backups](docs/UPGRADING.md) guides

### Changed
- Server log lines are tagged `[tag]` instead of `ha[tag]`

## v0.1.0 — 2026-10-22

First public release.

- YouTube-identical web app (home, search, channels, playlists, subscriptions, history, library, trending)
- Cloudflare WARP egress with automatic re-connect when YouTube flags the address
- Live streams with live captions (whisper.cpp)
- One library shared by every device, send to TV, TV mode
- Full-quality offline downloads
- 11 languages
- docker compose setup for x86-64 and ARM64
