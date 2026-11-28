# Running with Docker

A Docker image for running `wab-archiver` and `wab-viewer` is published to the GitHub
Container Registry for every release:

```bash
docker pull ghcr.io/auanasgheps/whatsapp-backup-tools:latest
```

Images are tagged with the release version (e.g. `0.90`) and `latest`, for `linux/amd64`
and `linux/arm64`. Each image is built only after the test suite passes on the image's
own Python.

The image is meant for servers and NAS boxes where the WhatsApp folder reaches the
machine by other means (Syncthing, SFTP, a mounted backup). Anything that talks to a
phone over ADB is not supported inside the container: `wab-archiver --from-adb` and
`wab-sweeper`.

## Archiving

Mount the WhatsApp source, the archive and the folder holding `config.toml`, then use
container paths in the config (e.g. `wa_root = "/source/WhatsApp"`,
`output = "/archive"`):

```bash
docker run --rm \
  -v /path/to/WhatsApp:/source/WhatsApp:ro \
  -v /path/to/my-archive:/archive \
  -v /path/to/config:/config:ro \
  ghcr.io/auanasgheps/whatsapp-backup-tools:latest \
  wab-archiver archive --config /config/config.toml
```

Always pass `--config` explicitly: the auto-detection asks for confirmation, which a
non-interactive container cannot answer.

## Viewing

```bash
docker run -d --name wab-viewer \
  -p 5000:5000 \
  -v /path/to/my-archive:/archive:ro \
  ghcr.io/auanasgheps/whatsapp-backup-tools:latest \
  wab-viewer /archive --host 0.0.0.0 --port 5000 --no-browser
```

Then open `http://<server>:5000`. To serve it behind a reverse proxy under a sub-path,
add `-e WAB_BASE_PATH=/whatsapp` (see the [Viewer Guide](viewer/index.md)).

## Docker Compose

```yaml
services:
  viewer:
    image: ghcr.io/auanasgheps/whatsapp-backup-tools:latest
    command: wab-viewer /archive --host 0.0.0.0 --port 5000 --no-browser
    ports:
      - "5000:5000"
    volumes:
      - /path/to/my-archive:/archive:ro
    restart: unless-stopped

  archiver:
    image: ghcr.io/auanasgheps/whatsapp-backup-tools:latest
    command: wab-archiver archive --config /config/config.toml
    volumes:
      - /path/to/WhatsApp:/source/WhatsApp:ro
      - /path/to/my-archive:/archive
      - /path/to/config:/config:ro
    profiles: ["archive"]
```

Run the archiver on demand or from cron with
`docker compose run --rm archiver`.

## File ownership

The tools run as UID/GID `1000`. If your files belong to a different user, run the
container as that user so the archive stays writable and readable:

```bash
docker run --rm --user "$(id -u):$(id -g)" ...
```
