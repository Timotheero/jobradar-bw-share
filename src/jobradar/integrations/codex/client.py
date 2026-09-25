"""Small newline-delimited JSON client for ``codex app-server``.

The process is never started during import.  Callers must explicitly call ``start``;
the web application only does so after the global CODEX_ENABLED gate has passed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


class CodexUnavailableError(RuntimeError):
    """Raised when the optional Codex subprocess cannot be used."""


class CodexProtocolError(RuntimeError):
    """Raised when the App Server returns an error or malformed response."""


JsonObject = dict[str, Any]
NotificationPredicate = Callable[[JsonObject], bool]

_CODEX_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "CODEX_HOME",
    "COMSPEC",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}
_CODEX_FORBIDDEN_ENV = {
    "APP_SECRET_KEY",
    "DATABASE_URL",
    "FIRECRAWL_API_KEY",
    "OPENAI_API_KEY",
    "POSTGRES_PASSWORD",
}
_PROTOCOL_STREAM_LIMIT = 4 * 1024 * 1024




class CodexAppServerClient:
    """Manage one Codex App Server subprocess and its request/notification stream."""

    def __init__(
        self,
        command: str | Sequence[str] = "codex app-server",
        *,
        request_timeout: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._request_timeout = request_timeout
        self._environment = dict(environment or {})
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._notifications: list[JsonObject] = []
        self._notification_condition = asyncio.Condition()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _arguments(self) -> list[str]:
        if isinstance(self._command, str):
            arguments = shlex.split(self._command, posix=os.name != "nt")
        else:
            arguments = list(self._command)
        if not arguments:
            raise CodexUnavailableError("CODEX_COMMAND ist leer.")
        return arguments

    def _subprocess_environment(self) -> dict[str, str]:
        """Return only operating-system values needed by the Codex process.

        Application and database secrets must not become visible to a later
        agent turn or shell tool.  Explicit constructor values can add benign
        process configuration, but known project/API secrets are removed even
        if they were supplied accidentally.
        """

        environment = {
            key: value for key, value in os.environ.items() if key.upper() in _CODEX_ENV_ALLOWLIST
        }
        environment.update(self._environment)
        for key in tuple(environment):
            upper_key = key.upper()
            if upper_key in _CODEX_FORBIDDEN_ENV or upper_key.endswith("_PASSWORD"):
                environment.pop(key, None)
        return environment

    async def start(self) -> None:
        """Start the process and perform the required initialize handshake."""

        async with self._start_lock:
            if self.running:
                return
            environment = self._subprocess_environment()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._arguments(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    limit=_PROTOCOL_STREAM_LIMIT,
                )
            except (FileNotFoundError, OSError) as exc:
                self._process = None
                raise CodexUnavailableError(
                    "Der Codex App Server ist nicht installiert oder nicht startbar."
                ) from exc

            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            try:
                await self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "jobradar-bw",
                            "title": "Jobradar BW",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self.notify("initialized", {})
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()

        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(CodexUnavailableError("Der Codex App Server wurde beendet."))

    async def __aenter__(self) -> CodexAppServerClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        wait_seconds: float | None = None,
    ) -> Any:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise CodexUnavailableError("Der Codex App Server laeuft nicht.")

        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        payload: JsonObject = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        try:
            await self._write(payload)
            return await asyncio.wait_for(
                future,
                timeout=self._request_timeout if wait_seconds is None else wait_seconds,
            )
        except TimeoutError as exc:
            raise CodexUnavailableError(f"Zeitueberschreitung bei {method}.") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: JsonObject = {"method": method}
        if params is not None:
            payload["params"] = dict(params)
        await self._write(payload)

    async def next_notification(
        self,
        predicate: NotificationPredicate | None = None,
        *,
        wait_seconds: float = 120.0,
    ) -> JsonObject:
        """Return and consume the first matching notification, including buffered ones."""

        matcher = predicate or (lambda _message: True)

        async def wait_for_match() -> JsonObject:
            async with self._notification_condition:
                while True:
                    for index, message in enumerate(self._notifications):
                        if matcher(message):
                            return self._notifications.pop(index)
                    await self._notification_condition.wait()

        try:
            return await asyncio.wait_for(wait_for_match(), timeout=wait_seconds)
        except TimeoutError as exc:
            raise CodexUnavailableError("Keine rechtzeitige Antwort vom Codex App Server.") from exc

    async def begin_chatgpt_device_login(self) -> JsonObject:
        result = await self.request(
            "account/login/start", {"type": "chatgptDeviceCode"}, wait_seconds=60
        )
        return _object(result, "account/login/start")

    async def account(self) -> JsonObject:
        return _object(await self.request("account/read", {"refreshToken": False}), "account/read")

    async def rate_limits(self) -> JsonObject:
        return _object(await self.request("account/rateLimits/read"), "account/rateLimits/read")

    async def usage(self) -> JsonObject:
        return _object(await self.request("account/usage/read"), "account/usage/read")

    async def logout(self) -> None:
        await self.request("account/logout")

    async def _write(self, payload: JsonObject) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise CodexUnavailableError("Der Codex App Server laeuft nicht.")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise CodexUnavailableError("Verbindung zum Codex App Server verloren.") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("Codex App Server lieferte eine unlesbare Protokollzeile.")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    self._resolve_response(message)
                elif "method" in message:
                    async with self._notification_condition:
                        self._notifications.append(message)
                        if len(self._notifications) > 500:
                            del self._notifications[:100]
                        self._notification_condition.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fehler beim Lesen des Codex-App-Server-Protokolls.")
        finally:
            self._fail_pending(CodexUnavailableError("Der Codex App Server wurde beendet."))

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while line := await process.stderr.readline():
                logger.debug("Codex App Server: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise

    def _resolve_response(self, message: JsonObject) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                detail = str(error)
            future.set_exception(CodexProtocolError(detail))
        else:
            future.set_result(message.get("result"))

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)


def _object(value: Any, operation: str) -> JsonObject:
    if not isinstance(value, dict):
        raise CodexProtocolError(f"Unerwartete Antwort auf {operation}.")
    return value
