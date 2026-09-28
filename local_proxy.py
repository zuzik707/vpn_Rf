"""
Local proxy bridge for Chrome → Bright Data.

Chrome --proxy-server doesn't support inline credentials.
CDP Fetch breaks with nodriver. Chrome extensions unreliable in headless.

Solution: local HTTP CONNECT proxy on 127.0.0.1 that forwards to
upstream proxy with Proxy-Authorization header.

Usage:
    python local_proxy.py  (reads PROXY_URL from .env)
    Or started automatically by main.py
"""

import asyncio
import base64
import logging
import os
import signal
import sys
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18880


def parse_upstream(proxy_url: str) -> tuple[str, int, str | None]:
    """Parse proxy URL → (host, port, base64_auth or None)."""
    parsed = urlparse(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 22225
    auth = None
    if parsed.username:
        creds = f"{parsed.username}:{parsed.password or ''}"
        auth = base64.b64encode(creds.encode()).decode()
    return host, port, auth


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        upstream_host: str, upstream_port: int, auth_header: str | None):
    """Handle one client connection — forward to upstream proxy with auth."""
    try:
        first_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not first_line:
            writer.close()
            return

        # Read remaining headers
        headers = [first_line]
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line == b"\r\n" or line == b"\n" or not line:
                break
            # Strip any existing Proxy-Authorization
            if not line.lower().startswith(b"proxy-authorization:"):
                headers.append(line)

        # Add our auth header
        if auth_header:
            headers.append(f"Proxy-Authorization: Basic {auth_header}\r\n".encode())
        headers.append(b"\r\n")

        # Connect to upstream
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(upstream_host, upstream_port), timeout=15)

        # Send modified request to upstream
        for h in headers:
            up_writer.write(h)
        await up_writer.drain()

        # Bidirectional pipe
        await asyncio.gather(
            _pipe(reader, up_writer),
            _pipe(up_reader, writer),
        )
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    except Exception as e:
        logger.debug("Client handler error: %s", e)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Copy data from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_local_proxy(proxy_url: str, local_port: int = DEFAULT_PORT) -> asyncio.Server | None:
    """Start local proxy bridge. Returns server object."""
    upstream_host, upstream_port, auth = parse_upstream(proxy_url)
    if not auth:
        logger.info("No proxy credentials — local bridge not needed")
        return None

    async def client_cb(reader, writer):
        await handle_client(reader, writer, upstream_host, upstream_port, auth)

    server = await asyncio.start_server(client_cb, "127.0.0.1", local_port)
    logger.info("Local proxy bridge: 127.0.0.1:%d → %s:%d (auth: yes)",
                local_port, upstream_host, upstream_port)
    return server


def get_local_proxy_url(worker_id: int = 0) -> str:
    """Return the local proxy URL for Chrome."""
    port = DEFAULT_PORT + worker_id
    return f"http://127.0.0.1:{port}"


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    proxy_url = os.getenv("PROXY_URL", "")
    if not proxy_url:
        print("Set PROXY_URL in .env")
        sys.exit(1)

    async def main():
        server = await start_local_proxy(proxy_url)
        if not server:
            print("No auth needed")
            return
        print(f"Listening on 127.0.0.1:{DEFAULT_PORT}")
        await server.serve_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
