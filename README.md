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
```bash
mkdir -p /volume1/docker/mediaguardian
sudo chown 1000:10 /volume1/docker/mediaguardian
cp .env.example .env
python3 -c 'import secrets;print(secrets.token_urlsafe(36))'
# Put the generated value in .env as MG_API_TOKEN
docker compose up -d --build
docker compose ps
```

Dashboard: http://127.0.0.1:8186 on the NAS. For a workstation connection use an SSH tunnel:

```bash
ssh -L 8186:127.0.0.1:8186 USER@NAS-IP
```

Then open http://127.0.0.1:8186 and enter the API token.

The initial scan is manually triggered, not scheduled. Initial disk enumeration on very large media trees can take time. Confirm resource usage before increasing scan budgets.

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
