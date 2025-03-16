import asyncio
import enum
import json
import logging

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

_logger = logging.getLogger(__package__)

ConnectionEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
PropertyCallback = Callable[[str, Any], Awaitable[None]]


class MPVConnectionException(Exception):
    pass


class MPVConnection:
    _event_callbacks: list[ConnectionEventCallback]
    _event_tasks: set[asyncio.Task]
    _request_id: int
    _request_futures: dict[int, asyncio.Future[dict[str, Any]]]
    _reader: asyncio.StreamReader | None
    _writer: asyncio.StreamWriter | None

    def __init__(self):
        self._event_callbacks = []
        self._event_tasks = set()

        self._reader = None
        self._writer = None

    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect_ip(self, host: str, port: int) -> None:
        _logger.info('Connecting to mpv at %s:%d', host, port)
        await self._connect(asyncio.open_connection(host, port))

    async def connect_unix(self, path: str) -> None:
        _logger.info('Connecting to mpv at %s', path)
        await self._connect(asyncio.open_unix_connection(path))

    async def _connect(self, open_coro) -> None:
        try:
            self._reader, self._writer = await open_coro
        except ConnectionError as ex:
            _logger.warning('Failed to connect to mpv', exc_info=ex)
            raise MPVConnectionException('Failed to connect') from ex
        _logger.debug('Connected')

        self._reader_task = asyncio.create_task(self._reader_fn())
        self._request_id = 1
        self._request_futures = {}

    async def disconnect(self) -> None:
        _logger.info('Disconnecting')
        self._reader_task.cancel()
        self._writer.close()
        await self._writer.wait_closed()

        self._reader = None
        self._writer = None

    def _handle_connection_failure(self, exception: Exception | None = None) -> None:
        _logger.error('Connection to mpv broken', exc_info=exception)
        self._reader = None
        self._writer = None
        self._run_event_handlers('disconnected', {})

    async def _reader_fn(self) -> None:
        while not self._reader.at_eof():
            try:
                line = await self._reader.readline()
                if len(line) == 0:  # EOF reached (socket closed)
                    return self._handle_connection_failure()
            except asyncio.CancelledError:
                return
            except ConnectionError as ex:
                return self._handle_connection_failure(ex)

            try:
                text = line[:-1].decode('utf-8')
                _logger.debug('Received: %s', text)
                response = json.loads(text)
            except Exception as ex:
                _logger.error('Failed to decode response %s', line, exc_info=ex)
                continue

            if 'request_id' in response:
                request_id = response.pop('request_id')
                if request_id in self._request_futures:
                    self._request_futures[request_id].set_result(response)
            elif 'event' in response:
                event = response.pop('event')
                self._run_event_handlers(event, response)

    def add_event_callback(self, callback: ConnectionEventCallback) -> None:
        self._event_callbacks.append(callback)

    def remove_event_callback(self, callback: ConnectionEventCallback) -> None:
        self._event_callbacks.remove(callback)

    def _run_event_handlers(self, event: str, params: dict[str, Any]) -> None:
        async def task_wrapper(callback):
            try:
                await callback(event, params)
            except Exception as ex:
                _logger.error('Event handler failed', exc_info=ex)

        # run the event handlers concurrently to the reader task
        for callback in self._event_callbacks:
            task = asyncio.create_task(task_wrapper(callback))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def command(self, command: str, *params: Any, response: bool = False) -> dict[str, Any] | None:
        if not self.is_connected():
            raise MPVConnectionException('Not connected')

        request_id = self._request_id
        self._request_id += 1
        if response:
            self._request_futures[request_id] = asyncio.get_running_loop().create_future()

        text = json.dumps({'request_id': request_id, 'command': [command, *params]})
        _logger.debug('Sending: %s', text)
        try:
            self._writer.write(text.encode('utf-8') + b'\n')
            await self._writer.drain()
        except ConnectionError as ex:
            self._reader_task.cancel()
            self._handle_connection_failure(ex)
            raise MPVConnectionException('Disconnected') from ex

        if response:
            response_value = await self._request_futures[request_id]
            del self._request_futures[request_id]
            return response_value


class MPVCommand(enum.StrEnum):
    PLAY = 'loadfile'
    SEEK = 'seek'
    STOP = 'stop'
    PLAYLIST_PREVIOUS = 'playlist-prev'
    PLAYLIST_NEXT = 'playlist-next'
    PLAYLIST_CLEAR = 'playlist-clear'


class MPVCommandFlags(enum.StrEnum):
    PLAY_REPLACE = 'replace'
    PLAY_APPEND = 'append'
    PLAY_INSERT_NEXT = 'insert-next'
    PLAY_INSERT_AT = 'insert-at'


class MPVEvent(enum.StrEnum):
    DISCONNECTED = 'disconnected'  # not a real mpv event, but raised by MPVConnection


class MPVProperty(enum.StrEnum):
    BUFFERING = 'paused-for-cache'
    DURATION = 'duration'
    IDLE = 'idle-active'
    MUTE = 'mute'
    PAUSED = 'pause'
    POSITION = 'time-pos'
    TITLE = 'media-title'
    VOLUME = 'volume'
    LOOP_FILE = 'loop-file'
    LOOP_PLAYLIST = 'loop-playlist'


class MPV:
    _event_callbacks: dict[str, list[EventCallback]]

    def __init__(self, connection: MPVConnection):
        self.connection = connection
        self.connection.add_event_callback(self._on_event)
        self._event_callbacks = defaultdict(list)
        self._watch_callbacks = {}

    async def _on_event(self, event: str, data: dict[str, Any]) -> None:
        if event == 'property-change':
            if data['id'] in self._watch_callbacks:
                await self._watch_callbacks[data['id']](data['name'], data.get('data', None))
        if event in self._event_callbacks:
            await asyncio.gather(*(cb(data) for cb in self._event_callbacks[event]))

    async def add_event_listener(self, event: str, listener: EventCallback) -> None:
        self._event_callbacks[event].append(listener)

    async def command(self, command: str, *params: Any) -> None:
        await self.connection.command(command, *params)

    async def get_property(self, name: str) -> None:
        response = await self.connection.command('get_property', name, response=True)
        return response.get('data', None)

    async def set_property(self, name: str, value: Any) -> None:
        await self.connection.command('set_property', name, value)

    async def watch_property(self, name: str, callback: PropertyCallback) -> None:
        id = max(self._watch_callbacks.keys(), default=0) + 1
        self._watch_callbacks[id] = callback
        await self.connection.command('observe_property', id, name)
