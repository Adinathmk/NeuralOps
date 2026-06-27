import gzip
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

class GZipRequestMiddleware:
    """
    Middleware to decompress incoming gzip-encoded requests.
    FastAPI/Starlette natively supports gzip responses via GZipMiddleware,
    but it does not support decompressing gzip requests out of the box.
    """
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        if headers.get(b"content-encoding", b"").lower() == b"gzip":
            body = b""
            more_body = True
            while more_body:
                message = await receive()
                body += message.get("body", b"")
                more_body = message.get("more_body", False)

            try:
                uncompressed_body = gzip.decompress(body)
            except Exception:
                response = PlainTextResponse("Invalid gzip payload", status_code=400)
                return await response(scope, receive, send)

            async def new_receive() -> dict:
                return {
                    "type": "http.request",
                    "body": uncompressed_body,
                    "more_body": False,
                }

            # Remove content-encoding and update content-length
            new_headers = []
            for k, v in scope["headers"]:
                if k.lower() not in (b"content-encoding", b"content-length"):
                    new_headers.append((k, v))
            new_headers.append((b"content-length", str(len(uncompressed_body)).encode()))
            scope["headers"] = new_headers

            return await self.app(scope, new_receive, send)

        return await self.app(scope, receive, send)
