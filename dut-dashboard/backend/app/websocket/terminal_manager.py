import asyncio

from fastapi import WebSocket


class TerminalManager:
    """Raw byte fan-out for the interactive serial terminal (/ws/term).

    Server -> clients: raw serial output bytes (broadcast). Client -> server is
    handled in the endpoint (keystrokes -> SerialWorker.write_raw). Kept separate
    from WebSocketManager (the line-based monitor stream) so terminal escape
    sequences never reach the sysmon parser.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast_bytes(self, data: bytes) -> None:
        dead: list[WebSocket] = []
        for client in self._clients:
            try:
                await client.send_bytes(data)
            except Exception:
                dead.append(client)
        for ws in dead:
            self.disconnect(ws)

    def emit_bytes_from_thread(self, data: bytes) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(asyncio.create_task, self.broadcast_bytes(data))
