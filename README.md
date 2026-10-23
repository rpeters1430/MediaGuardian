# MediaGuardian

Lightweight, read-only media library inventory and health dashboard for UGREEN NAS and Docker.

## Safety
- Read-only mount of media files; no automatic deletion, renaming, repair or transcoding.
- No Docker socket, privileged mode, or outbound control plane.
- Single container, single worker, CPU limited to 1, memory limited to 512 MB.
- Scanner skips symlinks and performs at most 30 bounded ffprobe checks per run.
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
1. Per-library scan schedules and scan history
2. Jellyfin, Plex and Emby read-only API integration
3. Sonarr/Radarr import diagnostics
4. Approval-based repair suggestions, with full audit trail


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
