# MediaGuardian

Lightweight, read-only media library inventory and health dashboard for UGREEN NAS and Docker.

## Safety
- Read-only mount of media files; no automatic deletion, renaming, repair or transcoding.
- No Docker socket, privileged mode, or outbound control plane. The only outbound requests are read-only checks of the apps you list in `services.json` and the notification URLs you configure.
- Single container, single worker, CPU limited to 1, memory limited to 512 MB.
- Scanner skips symlinks and NAS housekeeping folders, waits for files to stop changing before probing them, and performs a bounded number of ffprobe checks per run.
- Scanner does **not** decode all frames, so an "ok" result is not proof a file is uncorrupted.
- Authenticated API, localhost-bound port by default. The dashboard page itself is public on that bound interface.
- Do not expose port 8186 directly to the Internet.

## UGREEN deployment

See **v0.2 upgrade** below for username/password credentials and Tailscale binding. The `compose.yaml` pulls the published GHCR image.

## Development
```bash
pip install -r requirements-dev.txt
pytest -q
ruff check app tests --select E4,E7,E9,F
docker compose config -q
docker build -t mediaguardian:local .
```

## Release
Pull requests run tests and build checks. A tag `v0.1.0` triggers GHCR publishing for `linux/amd64`. **No container image is published before a tag or manual publication workflow succeeds.**

## Roadmap
- **v0.3 (this release):** scheduled scans, scan history, mass-disappearance guard, alerts with ntfy/Discord notifications, media stack status
- **v0.4:** hardlink and orphan reports, Shrinkarr leftover audit, disk capacity forecast
- **v0.5:** Jellyfin/Plex and Sonarr/Radarr cross-checks (library vs disk), permission checks
- **v0.6:** playability flags (audio/subtitle languages, codecs), suggestions for Shrinkarr, muting issues
- **Later:** approval-based repair suggestions with a full audit trail. Repairs run in Shrinkarr or the *arr apps; MediaGuardian stays read-only.


## v0.3 upgrade: stack status, schedules and alerts

Pull the new image and update `compose.yaml` (it adds `TZ` and the scan settings below). Your existing database is upgraded in place.

### Watch the rest of your media stack
Copy `services.example.json` to `/volume1/docker/mediaguardian/services.json`, then edit it to list your apps. Changes are picked up within a minute; no restart needed.

```json
[
  {"name": "Jellyfin", "type": "jellyfin", "url": "http://192.168.1.10:8096"},
  {"name": "Sonarr", "type": "sonarr", "url": "http://192.168.1.10:8989", "api_key": "SONARR_API_KEY"}
]
```

Supported types: `jellyfin`, `emby`, `plex`, `sonarr`, `radarr`, `lidarr`, `readarr`, `prowlarr`, `jellyseerr`, `overseerr`, `qbittorrent`, `shrinkarr`, and `generic` (checks the URL as given). Use the NAS's LAN IP rather than `localhost`, which means the MediaGuardian container itself.

Each app is checked every `MG_SERVICE_INTERVAL` seconds. Any HTTP reply below 500 counts as up, so a login page still shows the app is running. An app is reported down after two failed checks in a row. For Sonarr, Radarr, Lidarr, Readarr and Prowlarr, adding an `api_key` (Settings → General) also shows the app's own health warnings, such as an unreachable download client or failing indexers. The key is only sent to that app and is never shown in the dashboard. `services.json` holds API keys, so run `chmod 600` on it.

### Scheduled scans
| Setting | Default | Meaning |
|---|---|---|
| `MG_SCAN_INTERVAL_MINUTES` | `360` | Minutes between automatic scans. `0` turns them off. |
| `MG_SCAN_WINDOW` | empty | Only start automatic scans in these hours, e.g. `1-7` for 01:00–06:59. Wrapping past midnight (`22-6`) works. |
| `MG_MAX_PROBES_PER_SCAN` | `200` | New or changed files probed with ffprobe per scan. |
| `MG_SETTLE_SECONDS` | `300` | Files modified more recently than this are listed but not probed yet. |
| `MG_EXCLUDE` | empty | Extra folder names to skip, comma-separated, e.g. `Downloads,Extras`. |

Folders starting with `.`, `@` or `#` (`@eaDir`, `#recycle`, `.Trash-1000`), Shrinkarr's `*.shrinkarr.tmp.*` work files and macOS `._` files are always skipped.

Every scan walks the whole share. Frequent scans stop HDDs from spinning down, so keep the interval in hours and use `MG_SCAN_WINDOW` if your drives sleep.

### Alerts and notifications
The **Alerts** panel lists warnings until you dismiss them. Alerts are raised for:
- **Mass disappearance:** if at least 25 files and 10% of the library vanish in one scan, or the share looks empty, MediaGuardian does *not* mark them missing. It raises a critical alert first, usually a share that isn't mounted. If the next scan also can't find them, they are marked missing. Tune with `MG_MISSING_GUARD_MIN` and `MG_MISSING_GUARD_PERCENT`.
- Files that went missing, new zero-byte or unreadable files, and folders that couldn't be read. An unreadable folder no longer stops missing-file detection for the rest of the library, and files inside it aren't reported missing.
- Apps that go down or come back, and new health warnings from the *arr apps.

To get push notifications, set `MG_NTFY_URL` (a full topic URL such as `https://ntfy.sh/your-topic`, plus `MG_NTFY_TOKEN` if the topic is protected) and/or `MG_DISCORD_WEBHOOK` in `.env`. Warnings, critical alerts and app recoveries are sent.

### New API endpoints
`GET /api/stack`, `GET /api/events`, `POST /api/events/ack`, `GET /api/scans`. All require a signed-in session.


## v0.2 upgrade: username/password login

v0.2 replaces `MG_API_TOKEN` completely. **Before updating the image**, add these values to your existing `.env` in `/volume1/docker/mediaguardian/app`:

```ini
MG_USERNAME=admin
MG_PASSWORD=YOUR_UNIQUE_PASSWORD_AT_LEAST_12_CHARACTERS
MG_SESSION_SECRET=YOUR_SEPARATE_RANDOM_SECRET_AT_LEAST_32_CHARACTERS
MG_BIND_IP=100.104.229.117
```

To generate strong values on your NAS:

```bash
openssl rand -hex 24  # copy output for MG_PASSWORD
openssl rand -hex 32  # copy DIFFERENT output for MG_SESSION_SECRET
```

**Important:** protect `.env` (`chmod 600 .env`), never commit it, and don't share credentials. For the existing NAS installation, preserve the SQLite database in `/volume1/docker/mediaguardian`.

For SSH deployments after the new release is published:

```bash
cd /volume1/docker/mediaguardian/app
# Update the Compose source (download main archive or sync git), then:
sudo docker compose pull
sudo docker compose up -d --no-build
sudo docker compose ps
```

Open http://100.104.229.117:8186 on a connected Tailscale device. Login sessions use signed HttpOnly SameSite=Strict cookies (12-hour expiry). Logout clears the local session cookie. The session signature secret should be unique and not reused elsewhere. The password is provided by an environment variable in this first implementation, not stored in plaintext in the SQLite database.

**Network warning:** HTTP over Tailscale is encrypted between Tailscale devices, but browsers still report HTTP and cookies don't have the Secure attribute in this mode. Do not expose the service to the public Internet. For HTTPS behind a trusted proxy, a correctly configured forwarded scheme is needed for Secure cookies.

### New features
- Responsive dashboard with mobile cards, per-top-level-folder inventory, scan indicator and 5-second polling.
- Username/password authentication, in-memory login attempt throttling, signed sessions, and same-origin checks for mutating actions.
- Repeated scans gradually probe previously inventoried media files (up to `MG_MAX_PROBES_PER_SCAN` per run); existing completed FFprobe results are retained for unchanged files.
- SQLite scan writes committed every 100 files so dashboard inventory updates become visible while scans run.

### Known limitations
- No multi-user administration or password reset UI yet; change credentials in `.env` and restart.
- Changing the session secret or username invalidates current login cookies. Old sessions are not individually revocable until expiry.
- Live scan progress is based on the previous inventory total and may be approximate.
- This remains metadata-level scanning, not full-video decoding or automatic repair.
