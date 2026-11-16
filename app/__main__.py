"""One process for a Cloud Run instance. The port comes from the platform."""

import os
from collections.abc import Mapping

import uvicorn


def listen_port(environment: Mapping[str, str]) -> int:
    raw = environment.get("PORT", "8000")
    if not raw.isascii() or not raw.isdecimal():
        raise SystemExit("PORT must be a TCP port")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise SystemExit("PORT must be a TCP port")
    return port


def main() -> None:
    # The front end reuses idle connections for minutes. Closing sooner turns
    # the next request into a 502. Shutdown leaves time to return database
    # sessions before the platform's ten-second stop.
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=listen_port(os.environ),
        proxy_headers=False,
        server_header=False,
        access_log=False,
        timeout_keep_alive=620,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
