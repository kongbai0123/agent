"""Small authenticated HTTP/SSE client for the isolated Hermes service."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional

import requests

from .config import HermesConfig, validate_header_value
from .errors import (
    HermesAuthenticationError,
    HermesProtocolError,
    HermesUnavailableError,
)


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    event_id: str = ""
    retry_ms: Optional[int] = None

    def json(self) -> Any:
        if self.data == "[DONE]":
            return None
        try:
            return json.loads(self.data)
        except json.JSONDecodeError as exc:
            raise HermesProtocolError("Hermes returned an invalid SSE event.") from exc


class SSEEventStream(Iterator[SSEEvent]):
    """Closeable parsed stream so Workbench cancellation can unblock a read."""

    def __init__(self, response: requests.Response, *, max_event_bytes: int) -> None:
        self._response = response
        self._events = iter_sse_events(
            response.iter_lines(decode_unicode=False),
            max_event_bytes=max_event_bytes,
        )
        self._closed = False

    def __iter__(self) -> "SSEEventStream":
        return self

    def __next__(self) -> SSEEvent:
        try:
            return next(self._events)
        except requests.RequestException as exc:
            raise HermesUnavailableError("Hermes event stream is unavailable.") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._events.close()
        except (AttributeError, RuntimeError, ValueError):
            pass
        self._response.close()


def iter_sse_events(
    lines: Iterable[bytes | str],
    *,
    max_event_bytes: int = 1_048_576,
) -> Iterator[SSEEvent]:
    """Parse an SSE line stream without buffering an unbounded event."""

    event_name = "message"
    data_lines: list[str] = []
    event_id = ""
    retry_ms: Optional[int] = None
    event_bytes = 0

    def dispatch() -> Optional[SSEEvent]:
        nonlocal event_name, data_lines, retry_ms, event_bytes
        if not data_lines:
            event_name = "message"
            retry_ms = None
            event_bytes = 0
            return None
        result = SSEEvent(event_name or "message", "\n".join(data_lines), event_id, retry_ms)
        event_name = "message"
        data_lines = []
        retry_ms = None
        event_bytes = 0
        return result

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HermesProtocolError("Hermes returned invalid SSE text.") from exc
        else:
            line = str(raw_line)
        line = line.rstrip("\r\n")
        if line == "":
            result = dispatch()
            if result is not None:
                yield result
            continue
        event_bytes += len(line.encode("utf-8"))
        if event_bytes > max_event_bytes:
            raise HermesProtocolError("Hermes SSE event exceeded the size limit.")
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry":
            try:
                parsed_retry = int(value)
                if parsed_retry >= 0:
                    retry_ms = parsed_retry
            except ValueError:
                pass
    result = dispatch()
    if result is not None:
        yield result


class HermesSidecarClient:
    """Authenticated client that refuses redirects and ambient proxy settings."""

    def __init__(
        self,
        config: HermesConfig,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()
        if hasattr(self._session, "trust_env"):
            self._session.trust_env = False

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Hermes API path must be absolute.")
        return f"{self.config.base_url}{path}"

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        for name, value in (extra or {}).items():
            safe_name = str(name)
            if safe_name not in {
                "Accept",
                "Idempotency-Key",
                "Last-Event-ID",
                "X-Hermes-Session-Id",
                "X-Hermes-Session-Key",
            }:
                raise ValueError(f"Unsupported Hermes header: {safe_name}")
            headers[safe_name] = validate_header_value(
                value, label=f"Hermes {safe_name} header"
            )
        return headers

    def _send(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        stream: bool = False,
    ) -> requests.Response:
        self.config.require_enabled()
        timeout: float | tuple[float, float]
        timeout = (
            (self.config.timeout_seconds, self.config.stream_read_timeout_seconds)
            if stream
            else self.config.timeout_seconds
        )
        try:
            response = self._session.request(
                str(method).upper(),
                self._url(path),
                json=dict(payload) if payload is not None else None,
                params=dict(params) if params else None,
                headers=self._headers(headers),
                timeout=timeout,
                allow_redirects=False,
                # Always defer body consumption so JSON and SSE responses are
                # both read through explicit size-bounded paths below.
                stream=True,
            )
        except requests.RequestException as exc:
            raise HermesUnavailableError("Hermes sidecar is unavailable.") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {401, 403}:
            response.close()
            raise HermesAuthenticationError("Hermes authentication failed.")
        if 300 <= status < 400:
            response.close()
            raise HermesProtocolError("Hermes redirects are not allowed.")
        if status < 200 or status >= 300:
            response.close()
            if status >= 500 or status in {0, 408, 425, 429}:
                raise HermesUnavailableError(f"Hermes is unavailable (HTTP {status or 'unknown'}).")
            raise HermesProtocolError(f"Hermes rejected the request (HTTP {status}).")
        return response

    def _json_response(self, response: requests.Response) -> Dict[str, Any]:
        try:
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    declared_size = int(declared)
                except (TypeError, ValueError) as exc:
                    raise HermesProtocolError(
                        "Hermes returned an invalid response length."
                    ) from exc
                if declared_size < 0 or declared_size > self.config.max_response_bytes:
                    raise HermesProtocolError("Hermes response exceeded the size limit.")
            content_parts: list[bytes] = []
            received = 0
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                received += len(chunk)
                if received > self.config.max_response_bytes:
                    raise HermesProtocolError("Hermes response exceeded the size limit.")
                content_parts.append(chunk)
            content = b"".join(content_parts)
            if len(content) > self.config.max_response_bytes:
                raise HermesProtocolError("Hermes response exceeded the size limit.")
            try:
                decoded = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HermesProtocolError("Hermes returned invalid JSON.") from exc
            if not isinstance(decoded, dict):
                raise HermesProtocolError("Hermes returned an unexpected response shape.")
            return decoded
        finally:
            response.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        return self._json_response(
            self._send(
                method,
                path,
                payload=payload,
                params=params,
                headers=headers,
            )
        )

    def health(self) -> Dict[str, Any]:
        return self.request_json("GET", "/health")

    def capabilities(self) -> Dict[str, Any]:
        return self.request_json("GET", "/v1/capabilities")

    def close(self) -> None:
        """Release pooled sockets when a settings reload replaces the client."""

        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    @contextmanager
    def open_sse(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Iterator[SSEEventStream]:
        merged_headers = {"Accept": "text/event-stream", **dict(headers or {})}
        response = self._send(
            "GET", path, params=params, headers=merged_headers, stream=True
        )
        try:
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if content_type and "text/event-stream" not in content_type:
                raise HermesProtocolError("Hermes did not return an SSE stream.")
            yield SSEEventStream(
                response,
                max_event_bytes=self.config.max_response_bytes,
            )
        finally:
            response.close()
