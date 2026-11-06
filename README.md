# Subtitle Translator

A small, self-hosted web app that translates subtitle files in your media library using your own
[LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) instance. Timestamps, styling and
formatting tags are preserved; only the text is translated.

- **Browse** your media folder and translate selected files.
- **Scan** a folder tree for subtitles that don't have a translation yet and translate them in bulk.
- **Search** subtitle files by name and translate the matches.
- Supported formats: `.srt`, `.vtt`, `.ass`, `.ssa` and MicroDVD text `.sub`.
- Output is written next to the original, e.g. `Movie.en.srt` → `Movie.fr.srt`. Existing files are skipped
  unless you choose to overwrite.
- Single user with username/password login, configured in a one-time setup wizard.

## Install (Docker)

### 1. Prerequisites

- A Linux server (or any machine) with **Docker** and the **Docker Compose plugin**
  (`docker compose version` should work).
- A LibreTranslate instance the server can reach, **or** use the bundled one (step 3).

### 2. Get the code

```sh
git clone https://github.com/bendesmedt/subtitle-translator
cd subtitle-translator
```

### 3. Configure `.env`

All server-specific settings live in `.env`, which git ignores. Never edit `docker-compose.yml` on the
server; that keeps updates a plain `git pull`.

```sh
cp .env.example .env
nano .env        # or any editor
```

Set at least:

- **`MEDIA_PATH`** (required) — host folder with your movies/series, e.g. `/srv/media`. It is mounted
  at `/media` in the container. `docker compose` refuses to start while this is unset.
- **`PUID` / `PGID`** — the user that owns your media, so the app can write translated files next to the
  originals. Find them with `id` (e.g. `uid=1000(you) gid=1000(you)`); the default is 1000/1000.
- **`HOST_PORT`** — defaults to `8790`. Use `192.168.1.10:8790` to listen on one interface only.

Every variable is explained in `.env.example`.

Create the config folder yourself so it is owned by that user (otherwise Docker creates it as root and the
app cannot save its settings):

```sh
mkdir -p config
```

**Bundled LibreTranslate (optional)** — if you don't run LibreTranslate yet, uncomment
`COMPOSE_PROFILES=libretranslate` in `.env` and set `LT_LOAD_ONLY` to the language codes you need (fewer
languages = less disk and RAM). All `docker compose` commands then include it automatically. It is only
reachable from the app container; in the wizard you use `http://libretranslate:5000`.

### 4. Start

```sh
docker compose up -d --build
```

This also starts the bundled LibreTranslate if you enabled its profile in `.env`. The bundled LibreTranslate downloads its language models on first start; this can take several minutes
(`docker compose logs -f libretranslate`).

### 5. Get the one-time setup token

On first start the app is in setup mode and prints a setup token in its log:

```sh
docker compose logs subtitle-translator | grep "SETUP TOKEN"
```

The token changes on every restart until setup is completed.

### 6. Open the app

By default the app listens on port **8790** on all of the server's network interfaces. Find the server's LAN IP
(on the server):

```sh
hostname -I        # e.g. 192.168.1.10 ...
```

and browse from any device on your network to `http://SERVER_IP:8790`, e.g. <http://192.168.1.10:8790>.

**Firewall:** Docker publishes ports through its own iptables rules, which are evaluated *before* `ufw`, so
a `ufw` rule is normally not needed (and `ufw deny` does not block it). If your server filters traffic in
another way (e.g. firewalld zones or rules in Docker's `DOCKER-USER` chain), allow TCP port 8790 from your
LAN. To make the app listen on the LAN interface only, set `HOST_PORT=192.168.1.10:8790` (your server's
IP) in `.env`.

> **Security note:** on the LAN, traffic (including your password) is plain HTTP and unencrypted. That is
> fine on a trusted home network, but **do not port-forward 8790 to the internet**. For remote or encrypted
> access, use a VPN or the HTTPS reverse proxy below.

### 7. Setup wizard

1. **Setup token** — paste the token from the log.
2. **Account** — choose a username and a password (min. 12 characters).
3. **LibreTranslate** — URL (e.g. `http://libretranslate:5000` for the bundled one, or
   `http://192.168.1.10:5000`) and an optional API key. Use *Test connection* to check it.
4. **Media folder** — pick the folder inside `/media` that the app may access (it cannot see anything
   outside it).
5. **Default languages** — default source and target language.

After finishing, you are sent to the login page.

## HTTPS with a reverse proxy (optional)

The app has no built-in TLS. For encrypted access (e.g. from outside your network), put it behind a
reverse proxy. Example with [Caddy](https://caddyserver.com/) running on the same host (automatic HTTPS):

```caddyfile
subs.example.com {
    reverse_proxy 127.0.0.1:8790
}
```

Then set in `.env`:

```sh
COOKIE_SECURE=true                # session cookie is only sent over HTTPS
FORWARDED_ALLOW_IPS=172.18.0.1    # gateway IP of the app network, see below
HOST_PORT=127.0.0.1:8790          # optional: only the proxy can reach the app
```

`FORWARDED_ALLOW_IPS` must contain the IP your proxy has *as seen by the container*, so the app trusts the
`X-Forwarded-For` header and login rate limiting works per client. A proxy on the host connects through
the Docker bridge gateway (find it with
`docker network inspect subtitle-translator_default --format '{{(index .IPAM.Config 0).Gateway}}'`). A
proxy running as a container on the same network has its container IP. Never use `*`.

If your proxy rewrites the `Host` header, also set `ALLOWED_ORIGINS=https://subs.example.com`.

Apply the changes with `docker compose up -d`.

## Updating

```sh
cd subtitle-translator
git pull
docker compose up -d --build
```

Your settings and account (`./config`) and your `.env` are kept.

### Migrating from the first version

The first version had the media path written directly in `docker-compose.yml`. If you edited that file on
the server, `git pull` will refuse to overwrite it. Once, before pulling:

```sh
cd subtitle-translator
grep ":/media" docker-compose.yml      # note your media path
git checkout -- docker-compose.yml     # discard the local edit
git pull
cp -n .env.example .env                # keeps an existing .env
nano .env                              # set MEDIA_PATH (and PUID/PGID, COMPOSE_PROFILES, ...)
docker compose up -d --build
```

If you had also changed `COOKIE_SECURE`, `FORWARDED_ALLOW_IPS` or `LT_LOAD_ONLY` in the compose file, put
those values in `.env` too.

## Reset setup / forgotten password

```sh
docker compose stop subtitle-translator
rm config/config.json
docker compose up -d
docker compose logs subtitle-translator | grep "SETUP TOKEN"
```

Then run the setup wizard again (step 7). You can change your password at any time under *Settings* while
logged in.

## Troubleshooting

**"Permission denied" when translating, or the media folder is empty**
- Check that `PUID`/`PGID` in `.env` match the owner of your media (`ls -ln /path/to/your/media`) and
  recreate the container: `docker compose up -d`.
- The user needs write permission on the folders where the translated files are created.
- Hidden files/folders (starting with `.`) and symlinks pointing outside the media folder are not shown.

**The app won't start / settings can't be saved**
- `config/` must be writable by `PUID`: `sudo chown -R $(id -u):$(id -g) config`.
- Check the log: `docker compose logs subtitle-translator`.

**"LibreTranslate is unreachable"**
- Bundled instance: is it running and done downloading models? `docker compose ps`
  and `docker compose logs libretranslate`. Use `http://libretranslate:5000`, not `localhost`.
- External instance: `localhost` inside the container is the container itself. Use the server's LAN IP
  or hostname. Test from the container:
  `docker compose exec subtitle-translator python -c "import urllib.request; print(urllib.request.urlopen('http://HOST:5000/languages').status)"`
- "LibreTranslate rejected the API key": check the key under *Settings*.

**Login is blocked**
- After several failed logins, further attempts are delayed (up to 15 minutes). Wait, or restart the
  container (`docker compose restart subtitle-translator`), which also logs out all sessions.

## Limitations

- Image-based subtitles (VobSub `.sub`/`.idx`, PGS `.sup`) are not supported — only text formats.
- WebVTT cue settings (position/alignment after the timestamp) are not preserved.
- Language is detected from the filename (`Movie.en.srt`, `Movie.eng.srt`, `Movie.English.srt`). For files
  without a language code, use the scan option *without a language code, verified with LibreTranslate detection* or pick the source language manually.
- Sessions and the job queue are kept in memory: a restart logs you out and clears the job history.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Run locally:

```sh
CONFIG_DIR=./config MEDIA_BASE=/path/to/media .venv/bin/uvicorn --factory app.main:create_app --port 8790
```

See [AGENTS.md](AGENTS.md) for architecture and conventions.
