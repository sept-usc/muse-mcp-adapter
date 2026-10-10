"""Every response from the adapter carries its own security headers."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from muse_mcp_adapter.application.http_app import SECURITY_HEADERS, SecurityHeaders


def _client() -> TestClient:
    async def plain(_: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def custom(_: object) -> PlainTextResponse:
        return PlainTextResponse("x", headers={"cache-control": "max-age=60"})

    app = Starlette(
        routes=[Route("/plain", plain), Route("/custom", custom)],
        middleware=[Middleware(SecurityHeaders)],
    )
    return TestClient(app)


def test_every_response_has_the_security_headers() -> None:
    response = _client().get("/plain")
    for name, value in SECURITY_HEADERS:
        assert response.headers[name.decode()] == value.decode()


def test_a_header_a_route_set_itself_is_kept() -> None:
    response = _client().get("/custom")
    assert response.headers["cache-control"] == "max-age=60"
    assert response.headers["x-content-type-options"] == "nosniff"
