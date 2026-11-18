import asyncio
import base64


class FakeUpstream:
    """Imite le proxy HTTP d'une sortie (Gluetun) : vérifie ses identifiants, ouvre un tunnel
    CONNECT qui renvoie `exit=<nom>` puis l'écho de ce qu'il reçoit, et répond aux requêtes
    HTTP simples avec le nom de la sortie et la requête reçue."""

    def __init__(
        self, name: str, user: str = "user", password: str = "password", auth: bool = True
    ):
        # auth=False : proxy sans authentification, comme celui du téléphone.
        self.name = name
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.require_auth = auth
        self.connect_status = 200
        self.heads: list[str] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "FakeUpstream":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            self.heads.append(head)
            if self.require_auth and f"Proxy-Authorization: {self.auth}\r\n" not in head:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                return
            if head.startswith("CONNECT "):
                status = self.connect_status
                writer.write(f"HTTP/1.1 {status} X\r\n\r\n".encode())
                if status != 200:
                    return
                writer.write(f"exit={self.name}\n".encode())
                await writer.drain()
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
                return
            body = f"exit={self.name}\n{head.splitlines()[0]}\n".encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
