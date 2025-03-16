from __future__ import annotations
import asyncio

import logging
from typing import Any
from homeassistant.components.media_player.browse_media import BrowseMedia
from homeassistant.components.media_player.const import MediaType

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    RepeatMode,
)
from homeassistant.const import CONF_NAME, CONF_HOST, CONF_PATH, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt

from .const import CONF_SERVER, CONF_PROXY_MEDIA
from .mpv import MPV, MPVCommand, MPVCommandFlags, MPVConnection, MPVConnectionException, MPVEvent, MPVProperty

_logger = logging.getLogger(__package__)

DEFAULT_NAME = 'mpv'
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_SERVER): vol.Any(
        {
            vol.Required(CONF_HOST): cv.string,
            vol.Required(CONF_PORT): cv.port,
        },
        {
            vol.Required(CONF_PATH): cv.string,
        }
    ),
    vol.Optional(CONF_PROXY_MEDIA, default=True): cv.boolean,
})


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None
) -> None:
    server = config[CONF_SERVER]
    add_entities([
        MpvEntity(
            name=config[CONF_NAME],
            host=server.get(CONF_HOST),
            port=server.get(CONF_PORT),
            socket=server.get(CONF_PATH),
            proxy_media=config[CONF_PROXY_MEDIA]
        )
    ])


class MpvEntity(MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_available = False
    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.BROWSE_MEDIA |
        MediaPlayerEntityFeature.PLAY_MEDIA |
        MediaPlayerEntityFeature.PLAY |
        MediaPlayerEntityFeature.PAUSE |
        MediaPlayerEntityFeature.STOP |
        MediaPlayerEntityFeature.SEEK |
        MediaPlayerEntityFeature.PREVIOUS_TRACK |
        MediaPlayerEntityFeature.NEXT_TRACK |
        MediaPlayerEntityFeature.MEDIA_ENQUEUE |
        MediaPlayerEntityFeature.CLEAR_PLAYLIST |
        MediaPlayerEntityFeature.REPEAT_SET |
        MediaPlayerEntityFeature.VOLUME_MUTE |
        MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, name: str, host: str | None = None, port: int | None = None, socket: str | None = None, proxy_media: bool = True):
        self._attr_name = name

        self._host = host
        self._port = port
        self._socket = socket
        self._proxy_media = proxy_media

        self._connect_task = None
        self._refresh_position_task = None

    async def async_added_to_hass(self) -> None:
        await self._connect()

    async def async_will_remove_from_hass(self) -> None:
        await self._disconnect()

    async def _connect(self) -> None:
        async def disconnect_handler(*_):
            self._attr_available = False
            self.schedule_update_ha_state()

            await self._connect()  # automatically try to reconnect

        async def connect_handler():
            attempt = 0
            self._connection = MPVConnection()
            while not self._connection.is_connected():
                attempt += 1
                try:
                    if self._host and self._port:
                        await self._connection.connect_ip(self._host, self._port)
                    elif self._socket:
                        await self._connection.connect_unix(self._socket)
                    else:
                        raise RuntimeError('Invalid configuration')
                except MPVConnectionException as ex:
                    log_level = logging.WARNING if attempt == 1 else logging.DEBUG
                    _logger.log(log_level, 'Failed to establish connection to mpv', exc_info=ex)

                    await asyncio.sleep(2 ** min(attempt, 4) * 5)

            self._mpv = MPV(self._connection)
            await self._mpv.add_event_listener(MPVEvent.DISCONNECTED, disconnect_handler)

            await self._mpv.watch_property(MPVProperty.IDLE, self._refresh_state)
            await self._mpv.watch_property(MPVProperty.PAUSED, self._refresh_state)
            await self._mpv.watch_property(MPVProperty.BUFFERING, self._refresh_state)

            await self._mpv.watch_property(MPVProperty.MUTE, self._on_mute_change)
            await self._mpv.watch_property(MPVProperty.VOLUME, self._on_volume_change)

            await self._mpv.watch_property(MPVProperty.DURATION, self._on_duration_change)
            await self._mpv.watch_property(MPVProperty.TITLE, self._on_title_change)

            await self._mpv.watch_property(MPVProperty.LOOP_FILE, self._on_loop_change)
            await self._mpv.watch_property(MPVProperty.LOOP_PLAYLIST, self._on_loop_change)

            self._attr_available = True
            self._attr_changed()

            self._connect_task = None

        self._connect_task = asyncio.create_task(connect_handler())

    async def _disconnect(self):
        if self._connect_task:
            self._connect_task.cancel()
        else:
            await self._mpv.connection.disconnect()

    def _attr_changed(self):
        if self._attr_available:  # only schedule if entity is available to avoid spamming HA on connect
            self.schedule_update_ha_state()

    async def _refresh_state(self, property: str, value: Any) -> None:
        if await self._mpv.get_property(MPVProperty.IDLE):
            self._attr_state = MediaPlayerState.IDLE
        elif await self._mpv.get_property(MPVProperty.PAUSED):
            self._attr_state = MediaPlayerState.PAUSED
        elif await self._mpv.get_property(MPVProperty.BUFFERING):
            self._attr_state = MediaPlayerState.BUFFERING
        else:  # TODO: check if there's actually anything playing?
            self._attr_state = MediaPlayerState.PLAYING
        await self._refresh_position()  # also calls _attr_changed()

        if self._attr_state == MediaPlayerState.PLAYING and not self._refresh_position_task:
            self._refresh_position_task = asyncio.create_task(self._refresh_position_loop())
        elif self._attr_state != MediaPlayerState.PLAYING and self._refresh_position_task:
            self._refresh_position_task.cancel()

    async def _refresh_position(self) -> None:
        self._attr_media_position = await self._mpv.get_property(MPVProperty.POSITION)
        self._attr_media_position_updated_at = dt.utcnow()
        self._attr_changed()

    async def _refresh_position_loop(self) -> None:
        try:
            while True:
                await self._refresh_position()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            self._refresh_position_task = None

    async def _on_mute_change(self, property: str, value: bool) -> None:
        self._attr_is_volume_muted = value
        self._attr_changed()

    async def _on_volume_change(self, property: str, value: float) -> None:
        self._attr_volume_level = value / 100
        self._attr_changed()

    async def _on_duration_change(self, property: str, value: float) -> None:
        self._attr_media_duration = value
        self._attr_changed()

    async def _on_title_change(self, property: str, value: str) -> None:
        self._attr_media_title = value
        self._attr_changed()

    async def _on_loop_change(self, property: str, value: str) -> None:
        loop_properties = {
            'loop-file': RepeatMode.ONE,
            'loop-playlist': RepeatMode.ALL,
        }

        if value:
            self._attr_repeat = loop_properties[property]
        elif not value and self._attr_repeat == loop_properties[property]:
            self._attr_repeat = RepeatMode.OFF
        self._attr_changed()

    async def async_mute_volume(self, mute: bool) -> None:
        await self._mpv.set_property(MPVProperty.MUTE, mute)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._mpv.set_property(MPVProperty.VOLUME, int(volume * 100))

    async def async_media_play(self) -> None:
        await self._mpv.set_property(MPVProperty.PAUSED, False)

    async def async_media_pause(self) -> None:
        await self._mpv.set_property(MPVProperty.PAUSED, True)

    async def async_media_stop(self) -> None:
        await self._mpv.command(MPVCommand.STOP)

    async def async_media_seek(self, position: float) -> None:
        await self._mpv.command(MPVCommand.SEEK, position, 'absolute')
        await self._refresh_position()

    async def async_browse_media(self, media_content_type, media_content_id):
        return await media_source.async_browse_media(self.hass, media_content_id)

    async def async_play_media(self, media_type, media_id, enqueue: MediaPlayerEnqueue | None = None, **kwargs):
        _logger.debug(f'Playing media with type={media_type} id={media_id}')
        if media_source.is_media_source_id(media_id):
            # It'd be nicer if media_source._get_media_item() was public, but this seems to work perfectly fine
            item = media_source.MediaSourceItem.from_uri(self.hass, media_id, self.entity_id)
            source = item.async_media_source()  # contrary to its name, this function is not async
            if not self._proxy_media and isinstance(source, media_source.local_source.LocalSource):
                source_dir_id, location = source.async_parse_identifier(item)  # idem
                path = source.async_full_path(source_dir_id, location)  # idem
                url = str(path)
            else:
                play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
                url = media_source.async_process_play_media_url(self.hass, play_item.url)
        else:
            url = media_id

        flags = {
            None: MPVCommandFlags.PLAY_REPLACE,
            MediaPlayerEnqueue.ADD: MPVCommandFlags.PLAY_APPEND,
            MediaPlayerEnqueue.NEXT: MPVCommandFlags.PLAY_INSERT_NEXT,
            MediaPlayerEnqueue.PLAY: MPVCommandFlags.PLAY_INSERT_NEXT,
            MediaPlayerEnqueue.REPLACE: MPVCommandFlags.PLAY_REPLACE,
        }

        await self._mpv.command(MPVCommand.PLAY, url, flags[enqueue])
        if enqueue == MediaPlayerEnqueue.PLAY:
            # mpv doesn't have an "insert next and play next" command, so we have to do it manually
            await self._mpv.command(MPVCommand.PLAYLIST_NEXT)

        await self._mpv.set_property(MPVProperty.PAUSED, False)

    async def async_media_previous_track(self) -> None:
        await self._mpv.command(MPVCommand.PLAYLIST_PREVIOUS)

    async def async_media_next_track(self) -> None:
        await self._mpv.command(MPVCommand.PLAYLIST_NEXT)

    async def async_clear_playlist(self) -> None:
        await self._mpv.command(MPVCommand.PLAYLIST_CLEAR)

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        await self._mpv.set_property(MPVProperty.LOOP_FILE, repeat == RepeatMode.ONE)
        await self._mpv.set_property(MPVProperty.LOOP_PLAYLIST, repeat == RepeatMode.ALL)
