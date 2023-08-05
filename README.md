# mpv integration for Home Assistant

An Home Assistant Media Player integration for the [mpv][mpv] media player, using mpv's [JSON IPC][mpv-ipc] API.

## Installation

Clone the `custom_components/mpv` directory into your Home Assistant config directory.

### Configuration

Start mpv with the `input-ipc-server` option set to the socket location:
```bash
$ mpv --input-ipc-server=/path/to/mpv-socket
```

Configure the location of the socket in Home Assistant:
```yaml
media_player:
  - platform: mpv
    server:
      path: /path/to/mpv-socket
```

Enjoy!

[mpv]: https://mpv.io/
[mpv-ipc]: https://mpv.io/manual/stable/#json-ipc
