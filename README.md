# mpv integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

An Home Assistant Media Player integration for the [mpv][mpv] media player, using mpv's [JSON IPC][mpv-ipc] API.

## Setup

### Installation

The integration can be installed by adding it as a custom repository to [HACS][hacs]. In Home Assistant, navigate to
HACS > Integrations > Custom repositories (in the top-right menu). Under Repository enter `oxan/home-assistant-mpv`,
and under Category select Integration. The integration should now appear in HACS.

### Configuration

Start mpv with the `input-ipc-server` option set to the socket location:
```sh
mpv --input-ipc-server=/path/to/mpv-socket
```

Configure the integration in the Home Assistant `configuration.yaml` file:
```yaml
media_player:
  - platform: mpv
    name: "MPV Player"
    server:
      path: /path/to/mpv-socket
```

Restart Home Assistant and enjoy!

#### Remote mpv

It is also possible to connect to a remove mpv instance over the network. First, ensure that `socat` is installed, and
create a script that runs socat to expose the mpv socket on a network port (2352 in the following example). It is
important that this script has the extension `.run`, and is executable (run `chmod +x socat.run`):
```sh
#!/bin/sh
exec socat TCP-LISTEN:2352,fork UNIX-CONNECT:/path/to/mpv-socket
```

Start mpv with using the `--script` option to have it run the script on startup:
```sh
mpv --input-ipc-server=/path/to/mpv-socket --script=/path/to/socat.run
```

Finaly, configure the integration to connect over the network:
```yaml
media_player:
  - platform: mpv
    name: "MPV Player"
    server:
      host: 192.168.1.100
      port: 2352
```

#### Other useful mpv options

You can additionally use the `--idle` mpv option to have it remain alive if no media is playing.

#### Playback using local paths

When starting playback through Home Assistant, by default it will stream all media through its own HTTP server. If mpv
and Home Assistant can access the media files using the same filesystem path, you can disable this and play media
directly from the filesystem. This reduces resource usage and allows mpv to find external subtitle files.

```yaml
media_player:
  - platform: mpv
    server:
      path: /path/to/mpv-socket
    proxy_media: false
```

[hacs]: https://hacs.xyz/
[mpv]: https://mpv.io/
[mpv-ipc]: https://mpv.io/manual/stable/#json-ipc
