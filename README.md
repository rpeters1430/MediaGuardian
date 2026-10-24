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
- **v0.3:** scheduled scans, scan history, mass-disappearance guard, alerts with ntfy/Discord notifications, media stack status
- **v0.4:** hardlink and orphan reports, Shrinkarr leftover audit, disk capacity forecast
- **v0.5:** Jellyfin/Emby/Plex and Sonarr/Radarr cross-checks (library vs disk), stuck imports, permission checks
- **v0.6:** playback flags (audio/subtitle languages, codecs), Shrinkarr re-encode estimates, muting findings
- **v0.7 (this release):** Settings screen on the dashboard for every setting and your app list
- **Later:** approval-based repair suggestions with a full audit trail. Repairs run in Shrinkarr or the *arr apps; MediaGuardian stays read-only.


## v0.7 upgrade: Settings screen

Choose **Settings** at the top of the dashboard to change everything that used to need `compose.yaml`, `.env` or `services.json` edits. Changes take effect right away, with no container restart.

- **Apps:** add, edit or remove apps. **Test** checks that the app answers and its API key is accepted. It also shows where each of its library folders is found on the media share and suggests a path-map line for any that aren't. Saving writes `services.json`, so an existing file keeps working and shows up here. API keys are never sent back to the browser; leave the field blank to keep the saved key.
- **Notifications:** ntfy URL and token, Discord webhook, and **Send test notification**.
- **Scanning, Alerts, Downloads, App checks, Playback:** every setting described below, with its limits checked when you save.

Each setting shows where its value comes from: **Saved here**, **From compose / .env**, or **Default**. A value saved here wins over the environment, and **Reset** goes back to the compose/.env value or the default. So an existing `compose.yaml` that sets these variables keeps working, and you can trim it down to the new, shorter `compose.yaml` whenever you like.

A few things stay in `compose.yaml` and `.env` because Docker needs them before the app starts: folder mounts (a download folder still needs its read-only mount line), the port and bind IP, the container user, `TZ`, and the login. If `MG_SERVICES` is set in `compose.yaml`, the app list is read from there and can't be edited on the Settings screen.

Settings are stored in `/volume1/docker/mediaguardian/settings.json`. That file and `services.json` are written readable only by the container user, because they can hold API keys and notification tokens.

New endpoints: `GET/POST /api/settings`, `PUT /api/settings/services`, `POST /api/settings/test-service` and `POST /api/settings/test-notification`.


## v0.6 upgrade: playback checks and muting

Pull the new image and update `compose.yaml`. The database is upgraded in place.

### Playback
ffprobe now also records audio and subtitle languages, subtitle formats, bitrates and channels. Files probed by older versions are probed again a few at a time, using the same `MG_MAX_PROBES_PER_SCAN` budget as new files, so the **Playback** panel fills in over the first few scans. Like before, probing reads only the file headers.

| Finding | Meaning |
|---|---|
| Foreign-language audio without subtitles | No audio track in `MG_LANGUAGES` and no subtitles in those languages, either inside the file or as `Movie.en.srt` beside it. An unlabeled `Movie.srt` counts as a match, and files with untagged audio are skipped |
| Only image-based subtitles | Your-language subtitles exist only as PGS/VobSub images. Turning them on makes most clients burn them in with a full video transcode, while an SRT alongside avoids that |
| Video codec most devices can't play directly | Not in `MG_DIRECT_PLAY_VIDEO`, for example MPEG-2, VC-1 or XviD |
| No audio track most devices can play directly | No track in `MG_DIRECT_PLAY_AUDIO`, for example DTS-only or TrueHD-only audio |
| Old container format | AVI, WMV or MPG |
| Could shrink with Shrinkarr | Old codecs (about 60% smaller re-encoded), or H.264 above 8 Mbps at 1080p, 4 Mbps at 720p, 25 Mbps at 2160p or 2 Mbps below that (about 50%). The panel adds up the estimated savings |

MediaGuardian doesn't queue anything in Shrinkarr itself; that would need Shrinkarr credentials that can change files. Shrinkarr finds these files on its own scans, so this list shows what you would gain and which libraries to point it at first.

### Muting
Every finding in Cleanup, Library checks and Playback has **Mute** and **Mute folder** buttons. Muted findings are hidden and don't raise alerts. Muting a folder covers everything inside it, which is useful for an anime folder you expect to be Japanese-only. Muted items are listed in a **Muted** card, where you can unmute them.

### New API endpoints
`GET /api/playback`, `GET /api/muted`, `POST /api/findings/mute` and `POST /api/findings/unmute` (body `{"kind": "...", "path": "..."}`).


## v0.5 upgrade: library checks

Pull the new image. The database is upgraded in place. If you use the `services.json` from v0.3, the new checks start as soon as the apps in it have an `api_key`.

### What gets checked
The **Library checks** panel compares each app with the files MediaGuardian indexed on disk. It runs after every scan and every `MG_CROSSCHECK_MINUTES` (default 60) in between. These runs only call the apps' APIs; they don't read the disks. **Check now** runs it immediately.

| Finding | Apps | Meaning |
|---|---|---|
| Sonarr/Radarr expect a file that isn't on disk | Sonarr, Radarr | Radarr says a movie has a file that's gone, or Sonarr lists more episode files for a series than are on disk |
| Download stuck in the queue | Sonarr, Radarr | Queue items in `importPending`, `importBlocked`, `importFailed` or `failedPending`, or flagged with a warning or error, with Sonarr/Radarr's own message |
| Folder Sonarr/Radarr don't manage | Sonarr, Radarr | Video folders inside a root folder that don't belong to any series or movie, such as manual copies or leftovers from deleted entries |
| Media server item whose file is gone | Jellyfin, Emby, Plex | The server still lists the item, but the file was moved or deleted |
| File your media server hasn't picked up | Jellyfin, Emby, Plex | A video in one of the server's movie or TV library folders, more than a day old, that the server doesn't list. Usually a naming problem, a permission problem or a missed library scan |
| File or folder MediaGuardian can't read | (scan) | Permission denied during a scan |

Extras (`Featurettes/`, `Trailers/`, `*-trailer.mkv` and similar) and sample clips are ignored. Jellyfin and Emby report only one file per movie, so alternate versions (`Movie - 4K.mkv`) can show as "not picked up". Plex reports all of them.

New items raise alerts. Missing files and stuck downloads are warnings; untracked folders and files the server hasn't picked up are info. An app that can't be reached, or rejects its key, raises one warning and keeps its previous findings until it answers again.

### API keys
Add `api_key` to each app in `services.json`:
- **Sonarr / Radarr:** Settings → General → API Key.
- **Jellyfin / Emby:** Dashboard → API Keys → add one.
- **Plex:** your `X-Plex-Token` ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).

MediaGuardian only calls read endpoints. It never changes anything in these apps.

### Paths
Each app sees your media under its own container path, such as `/data/media/movies` or `/tv`. MediaGuardian matches each library or root folder to a folder in its own media share by the end of the path, ignoring case. So `/data/media/movies` finds `Movies`, and `/tv` finds `TV`. If a folder can't be matched, the panel names it. Add a `path_map` from the app's path to the matching folder inside the media share (`""` is the share itself):

```json
{"name": "Radarr", "type": "radarr", "url": "http://192.168.1.10:7878", "api_key": "...",
 "path_map": {"/data/media": ""}}
```

### Permissions
A file MediaGuardian can't read is listed under **File or folder MediaGuardian can't read**. The check uses MediaGuardian's own user (`user:` in `compose.yaml`, `1000:10` by default), so it respects ACLs. Run MediaGuardian as the same user and group as Jellyfin or Plex, and anything listed here is also something your media server can't play.

### New API endpoints
`GET /api/consistency` and `POST /api/consistency/run`. `GET /api/findings?kind=` accepts the new kinds.


## v0.4 upgrade: space and cleanup

Pull the new image and update `compose.yaml`. The database is upgraded in place; the first scan after upgrading fills in the new data.

### Space
The **Space** panel shows how full the media volume is, sampled hourly, and how fast it is growing over the last 30 days. Once there is a day of history it estimates when the volume will be full. You get one alert when free space drops below `MG_DISK_WARN_PERCENT` (default 10%) or the estimate falls under `MG_DISK_WARN_DAYS` (default 30), and another when it recovers. A download folder on a separate volume gets its own card.

### Downloads stored twice
Sonarr and Radarr can hardlink an imported download into your library, so a torrent can keep seeding without using extra space. When that silently falls back to copying, every import uses its space twice. To check for this, mount your download folder read-only and set `MG_DOWNLOADS`:

```yaml
    environment:
      MG_DOWNLOADS: "/downloads"
    volumes:
      - /volume1/Downloads:/downloads:ro
```

After the next scan, **Cleanup** lists:
- **Download stored twice (not hardlinked):** a download with the same contents as a library file (same size; the first and last 64 KiB compared) but a separate copy on disk. If the panel says the download folder is on a different volume, hardlinks can't work there at all. In that case, move downloads onto the same volume or shared folder as the media.
- **Download with no library copy:** a video older than `MG_UNIMPORTED_DAYS` (default 7) that exists only in downloads. It was never imported, or the library copy was replaced, for example re-encoded by Shrinkarr, so the seeding copy now uses its own space.

If the download folder is inside the media share (for example `/media/Downloads`), set `MG_DOWNLOADS` to that path. It is then left out of the library totals.

### Leftovers and clutter in the library
Every complete scan also lists:
- **Shrinkarr leftover files** (`*.shrinkarr.bak`, `*.shrinkarr.bak.txn`, `*.shrinkarr.tmp.*`) older than a day. These mean a replacement was interrupted, so check the original plays before deleting anything. New ones raise a warning.
- **Incomplete downloads** (`.part`, `.!qB`, `.!ut`, `.crdownload`, `.partial`) older than a day.
- **Sample clips** under 500 MiB with "sample" in the name.
- **Empty folders.** Folders holding only NAS thumbnail folders like `@eaDir` count as empty.
- **Folders with subtitles or `.nfo` files but no video**, usually what's left after a movie or episode was deleted. Folders containing audio files are ignored, so music libraries aren't flagged.

A file that changes and shrinks by more than `MG_SHRINK_ALERT_PERCENT` (default 90%) in one go raises a warning. A normal re-encode rarely loses that much, so it's worth checking the file still plays in full.

MediaGuardian only reports. It never deletes or moves anything; the media and download mounts stay read-only.

### New API endpoints
`GET /api/space` and `GET /api/findings?kind=<kind>`.


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

You can also put the same list in `compose.yaml` as `MG_SERVICES` (there is a commented example). When it is set, `services.json` is ignored, and changes need a `docker compose up -d` to take effect.

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
