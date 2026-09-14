from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.main import configure_cors, configure_response_compression


class CorsConfigTests(unittest.TestCase):
    def test_preflight_allows_configured_production_and_local_origins(self) -> None:
        app = FastAPI()

        @app.put("/api/v1/account/sync/preferences")
        def put_sync_preferences() -> dict[str, bool]:
            return {"ok": True}

        configure_cors(
            app,
            (
                "https://busapp.avianjay.sbs",
                "http://localhost:8080",
                "http://127.0.0.1:8080",
            ),
        )
        client = TestClient(app)

        for origin in (
            "https://busapp.avianjay.sbs",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ):
            with self.subTest(origin=origin):
                response = client.options(
                    "/api/v1/account/sync/preferences",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "PUT",
                        "Access-Control-Request-Headers": "authorization,content-type",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["access-control-allow-origin"],
                    origin,
                )
                self.assertIn("PUT", response.headers["access-control-allow-methods"])

    def test_preflight_rejects_unconfigured_origin(self) -> None:
        app = FastAPI()
        configure_cors(app, ("http://localhost:8080",))
        client = TestClient(app)

        response = client.options(
            "/api/v1/account/sync/preferences",
            headers={
                "Origin": "http://localhost:64088",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)


class ResponseCompressionTests(unittest.TestCase):
    def test_brotli_is_used_when_requested(self) -> None:
        app = FastAPI()
        body = "Brotli response compression\n" * 100

        @app.get("/payload")
        def get_payload() -> PlainTextResponse:
            return PlainTextResponse(body)

        configure_response_compression(app)
        client = TestClient(app)

        response = client.get("/payload", headers={"Accept-Encoding": "br"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "br")
        self.assertEqual(response.text, body)


if __name__ == "__main__":
    unittest.main()
