"""Bounded public HTTP exchanges for research, including search redirects.

Production connections reuse scouting's public DNS validation and pinned
sockets. The complete exchange runs in a killable worker: socket timeouts
alone cannot enforce a deadline during DNS, TLS, or HTTP header parsing.
An explicitly injected httpx transport is the offline testing seam only.
"""

from __future__ import annotations

import http.client
import multiprocessing
import queue
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .independence_lexicon import host_is_denied
from .provenance import is_public_http_url
from .scouting import (
    _connect_pinned,
    _receive_isolated_payload,
    _remaining_seconds,
    _resolve_public_addresses,
    _stop_fetch_process,
)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class PublicResponse:
    url: str
    headers: dict[str, str]
    body: bytes


def is_pdf_response(url: str, content_type: str) -> bool:
    return content_type == "application/pdf" or (
        content_type in {"application/octet-stream", ""}
        and httpx.URL(url).path.lower().endswith(".pdf")
    )


def _check_target(url: str) -> None:
    if not is_public_http_url(url):
        raise ValueError(f"non-public URL: {url!r}")
    if host_is_denied(url):
        raise ValueError("excluded source host")


def _body_limit(
    url: str, headers: httpx.Headers, max_bytes: int, pdf_max_bytes: int | None
) -> int:
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in {"", "identity"}:
        raise ValueError("source ignored the identity content-encoding requirement")
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    cap = (
        pdf_max_bytes
        if pdf_max_bytes is not None and is_pdf_response(url, content_type)
        else max_bytes
    )
    declared = headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise ValueError("invalid source Content-Length") from exc
        if size < 0:
            raise ValueError("invalid source Content-Length")
        if size > cap:
            raise ValueError("source response exceeds the byte limit")
    return cap


class _SocketBody(httpx.SyncByteStream):
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        network_socket: socket.socket,
        deadline: float,
        limit: int,
    ) -> None:
        self.response = response
        self.connection = connection
        self.network_socket = network_socket
        self.deadline = deadline
        self.limit = limit

    def __iter__(self) -> Iterator[bytes]:
        received = 0
        while True:
            self.network_socket.settimeout(_remaining_seconds(self.deadline))
            # At most one sentinel byte beyond the limit is ever read.
            chunk = self.response.read1(min(65_536, self.limit - received + 1))
            _remaining_seconds(self.deadline)
            if not chunk:
                return
            received += len(chunk)
            if received > self.limit:
                raise ValueError("source response exceeds the byte limit")
            yield chunk

    def close(self) -> None:
        self.response.close()
        self.connection.close()
        self.network_socket.close()


class PinnedPublicTransport(httpx.BaseTransport):
    """Single-hop HTTP transport; never resolves a checked hostname twice."""

    def __init__(self, *, deadline: float, max_bytes: int, pdf_max_bytes: int | None):
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.pdf_max_bytes = pdf_max_bytes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        _check_target(url)
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        targets = _resolve_public_addresses(host, port, deadline=self.deadline)
        network_socket = _connect_pinned(targets, deadline=self.deadline)
        connection = None
        response = None
        try:
            if request.url.scheme == "https":
                network_socket.settimeout(_remaining_seconds(self.deadline))
                network_socket = ssl.create_default_context().wrap_socket(
                    network_socket, server_hostname=host
                )
            connection = http.client.HTTPConnection(host, port)
            connection.sock = network_socket
            network_socket.settimeout(_remaining_seconds(self.deadline))
            connection.request(
                "GET", request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
            )
            network_socket.settimeout(_remaining_seconds(self.deadline))
            response = http.client.HTTPResponse(network_socket, method="GET")
            response.begin()
            headers = httpx.Headers(response.headers.items())
            # Redirect/error bodies are never consumed. Only final successful
            # responses need a body allowance or content-encoding validation.
            limit = 0
            if 200 <= response.status < 300:
                limit = _body_limit(url, headers, self.max_bytes, self.pdf_max_bytes)
            return httpx.Response(
                response.status, headers=headers,
                stream=_SocketBody(response, connection, network_socket, self.deadline, limit),
            )
        except BaseException:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            network_socket.close()
            raise


def _exchange(
    url: str, *, timeout: float, max_bytes: int, pdf_max_bytes: int | None,
    max_redirects: int, user_agent: str, transport: httpx.BaseTransport | None,
) -> PublicResponse:
    deadline = time.monotonic() + timeout
    transport = transport or PinnedPublicTransport(
        deadline=deadline, max_bytes=max_bytes, pdf_max_bytes=pdf_max_bytes,
    )
    with httpx.Client(
        timeout=timeout, transport=transport, follow_redirects=False, trust_env=False,
        headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
    ) as client:
        current = url
        for hop in range(max_redirects + 1):
            _check_target(current)
            _remaining_seconds(deadline)
            with client.stream("GET", current) as response:
                if response.status_code in REDIRECT_STATUSES:
                    target = response.headers.get("location")
                    if not target:
                        raise ValueError("redirect response omitted its target")
                    if hop >= max_redirects:
                        raise ValueError("too many redirects fetching public source")
                    current = str(httpx.URL(current).join(target))
                    _check_target(current)
                    continue
                response.raise_for_status()
                cap = _body_limit(current, response.headers, max_bytes, pdf_max_bytes)
                chunks: list[bytes] = []
                received = 0
                # Compressed bodies were rejected before this iterator. The
                # bytes iterator also supports preloaded offline mock responses.
                for chunk in response.iter_bytes():
                    _remaining_seconds(deadline)
                    received += len(chunk)
                    if received > cap:
                        raise ValueError("source response exceeds the byte limit")
                    chunks.append(chunk)
                _remaining_seconds(deadline)
                return PublicResponse(current, dict(response.headers), b"".join(chunks))
    raise ValueError("too many redirects fetching public source")  # pragma: no cover


def _worker(sender: Any, url: str, kwargs: dict[str, Any]) -> None:
    try:
        payload = ("ok", _exchange(url, **kwargs, transport=None))
    except Exception as exc:
        payload = ("error", str(exc))
    try:
        sender.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


def fetch_public(
    url: str, *, timeout: float, max_bytes: int, max_redirects: int,
    user_agent: str, pdf_max_bytes: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PublicResponse:
    """Fetch a bounded identity-encoded response, pinning every network hop.

    The injected transport bypasses process isolation and DNS because it is
    a trusted programmatic test seam; browser/API input cannot select one.
    """
    _check_target(url)
    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("fetch limits must be positive (redirects may be zero)")
    if pdf_max_bytes is not None and pdf_max_bytes <= 0:
        raise ValueError("PDF byte limit must be positive")
    kwargs = dict(
        timeout=timeout, max_bytes=max_bytes, max_redirects=max_redirects,
        user_agent=user_agent, pdf_max_bytes=pdf_max_bytes,
    )
    if transport is not None:
        return _exchange(url, **kwargs, transport=transport)

    if multiprocessing.current_process().daemon:
        raise ValueError("killable research transport is unavailable in a daemonic process")
    main_path = getattr(sys.modules.get("__main__"), "__file__", None)
    if isinstance(main_path, str) and main_path.startswith("<"):
        raise ValueError("killable research transport requires a file or module entry point")
    deadline = time.monotonic() + timeout
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    results: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    receive_thread = None
    process = context.Process(
        target=_worker, args=(sender, url, kwargs), name="atlas-research-fetch", daemon=True,
    )
    try:
        process.start()
        sender.close()
        receive_thread = threading.Thread(
            target=_receive_isolated_payload, args=(receiver, results),
            name="atlas-research-recv", daemon=True,
        )
        receive_thread.start()
        try:
            receive_status, payload = results.get(timeout=_remaining_seconds(deadline))
        except queue.Empty as exc:
            raise ValueError("public source request exceeded the timeout") from exc
        _remaining_seconds(deadline)
        if receive_status != "ok":
            raise ValueError("isolated research transport exited without a result")
        if isinstance(payload, tuple) and len(payload) == 2:
            if payload[0] == "ok" and isinstance(payload[1], PublicResponse):
                return payload[1]
            if payload[0] == "error":
                raise ValueError(str(payload[1]))
        raise ValueError("invalid isolated research transport result")
    except (OSError, RuntimeError) as exc:
        raise ValueError("killable research transport could not be started") from exc
    finally:
        sender.close()
        _stop_fetch_process(process)
        receiver.close()
        if receive_thread is not None:
            receive_thread.join(timeout=0.2)
