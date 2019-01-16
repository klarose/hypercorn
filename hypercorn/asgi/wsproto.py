import asyncio
from enum import auto, Enum
from itertools import chain
from time import time
from typing import List, Optional, Tuple, Type, Union
from urllib.parse import unquote

from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Event,
    Message,
    RejectConnection,
    RejectData,
    Request,
    TextMessage,
)
from wsproto.extensions import PerMessageDeflate
from wsproto.frame_protocol import CloseReason

from ..config import Config
from ..typing import ASGIFramework
from ..utils import suppress_body


class ASGIWebsocketState(Enum):
    # Hypercorn supports the ASGI websocket HTTP response extension,
    # which allows HTTP responses rather than acceptance.
    HANDSHAKE = auto()
    CONNECTED = auto()
    RESPONSE = auto()
    CLOSED = auto()
    HTTPCLOSED = auto()


class UnexpectedMessage(Exception):
    def __init__(self, state: ASGIWebsocketState, message_type: str) -> None:
        super().__init__(f"Unexpected message type, {message_type} given the state {state}")


class FrameTooLarge(Exception):
    pass


class WebsocketBuffer:
    def __init__(self, max_length: int) -> None:
        self.value: Optional[Union[bytes, str]] = None
        self.max_length = max_length

    def extend(self, event: Message) -> None:
        if self.value is None:
            if isinstance(event, TextMessage):
                self.value = ""
            else:
                self.value = b""
        self.value += event.data  # type: ignore
        if len(self.value) > self.max_length:
            raise FrameTooLarge()

    def clear(self) -> None:
        self.value = None

    def to_message(self) -> dict:
        return {
            "type": "websocket.receive",
            "bytes": self.value if isinstance(self.value, bytes) else None,
            "text": self.value if isinstance(self.value, str) else None,
        }


class WebsocketMixin:
    app: Type[ASGIFramework]
    client: Tuple[str, int]
    config: Config
    response: Optional[dict]
    server: Tuple[str, int]
    start_time: float
    state: ASGIWebsocketState

    @property
    def scheme(self) -> str:
        pass

    def response_headers(self) -> List[Tuple[bytes, bytes]]:
        pass

    async def asend(self, event: Event) -> None:
        pass

    async def asgi_put(self, message: dict) -> None:
        """Called by the ASGI server to put a message to the ASGI instance.

        See asgi_receive as the get to this put.
        """
        pass

    async def asgi_receive(self) -> dict:
        """Called by the ASGI instance to receive a message."""
        pass

    async def handle_websocket(self, event: Request) -> None:
        path, _, query_string = event.target.partition("?")
        headers = [(b"host", event.host.encode())]
        headers.extend(event.extra_headers)
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "2.0"},
            "scheme": self.scheme,
            "path": unquote(path),
            "query_string": query_string.encode("ascii"),
            "root_path": self.config.root_path,
            "headers": headers,
            "client": self.client,
            "server": self.server,
            "subprotocols": event.subprotocols,
            "extensions": {"websocket.http.response": {}},
        }
        await self.handle_asgi_app(event)

    async def send_http_error(self, status: int) -> None:
        await self.asend(RejectConnection(status_code=status, headers=self.response_headers()))
        self.config.access_logger.access(
            self.scope, {"status": status, "headers": []}, time() - self.start_time
        )

    async def handle_asgi_app(self, event: Request) -> None:
        self.start_time = time()
        await self.asgi_put({"type": "websocket.connect"})
        try:
            asgi_instance = self.app(self.scope)
            await asgi_instance(self.asgi_receive, self.asgi_send)
        except asyncio.CancelledError:
            pass
        except Exception:
            if self.config.error_logger is not None:
                self.config.error_logger.exception("Error in ASGI Framework")

            if self.state == ASGIWebsocketState.CONNECTED:
                await self.asend(CloseConnection(code=CloseReason.ABNORMAL_CLOSURE))
                self.state = ASGIWebsocketState.CLOSED

        # If the application hasn't accepted the connection (or sent a
        # response) send a 500 for it. Otherwise if the connection
        # hasn't been closed then close it.
        if self.state == ASGIWebsocketState.HANDSHAKE:
            await self.send_http_error(500)
            self.state = ASGIWebsocketState.HTTPCLOSED

    async def asgi_send(self, message: dict) -> None:
        """Called by the ASGI instance to send a message."""
        if message["type"] == "websocket.accept" and self.state == ASGIWebsocketState.HANDSHAKE:
            await self.asend(AcceptConnection(extensions=[PerMessageDeflate()]))
            self.state = ASGIWebsocketState.CONNECTED
            self.config.access_logger.access(
                self.scope, {"status": 101, "headers": []}, time() - self.start_time
            )
        elif (
            message["type"] == "websocket.http.response.start"
            and self.state == ASGIWebsocketState.HANDSHAKE
        ):
            self.response = message
            self.config.access_logger.access(self.scope, self.response, time() - self.start_time)
        elif message["type"] == "websocket.http.response.body" and self.state in {
            ASGIWebsocketState.HANDSHAKE,
            ASGIWebsocketState.RESPONSE,
        }:
            await self._asgi_send_rejection(message)
        elif message["type"] == "websocket.send" and self.state == ASGIWebsocketState.CONNECTED:
            data: Union[bytes, str]
            if message.get("bytes") is not None:
                await self.asend(BytesMessage(data=bytes(message["bytes"])))
            elif not isinstance(message["text"], str):
                raise TypeError(f"{message['text']} should be a str")
            else:
                await self.asend(TextMessage(data=message["text"]))
        elif message["type"] == "websocket.close" and self.state == ASGIWebsocketState.HANDSHAKE:
            await self.send_http_error(403)
            self.state = ASGIWebsocketState.HTTPCLOSED
        elif message["type"] == "websocket.close":
            await self.asend(CloseConnection(code=int(message["code"])))
            self.state = ASGIWebsocketState.CLOSED
        else:
            raise UnexpectedMessage(self.state, message["type"])

    async def _asgi_send_rejection(self, message: dict) -> None:
        body_suppressed = suppress_body("GET", self.response["status"])
        if self.state == ASGIWebsocketState.HANDSHAKE:
            headers = chain(
                [
                    (bytes(key).strip(), bytes(value).strip())
                    for key, value in self.response["headers"]
                ],
                self.response_headers(),
            )
            await self.asend(
                RejectConnection(
                    status_code=int(self.response["status"]),
                    headers=headers,
                    has_body=not body_suppressed,
                )
            )
            self.state = ASGIWebsocketState.RESPONSE
        if not body_suppressed:
            await self.asend(
                RejectData(
                    data=bytes(message.get("body", b"")),
                    body_finished=not message.get("more_body", False),
                )
            )
        if not message.get("more_body", False):
            await self.asgi_put({"type": "websocket.disconnect"})
            self.state = ASGIWebsocketState.HTTPCLOSED
