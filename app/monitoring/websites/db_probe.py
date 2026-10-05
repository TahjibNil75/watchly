"""Database probes: whether a database server answers as its engine does,
without logging in.

A port that takes connections proves little: a proxy or load balancer takes
them while the database behind it is down, and so does a server still
starting up. So each probe goes as far as the server's first real answer,
which needs no user name or password from anyone:

- PostgreSQL: ask for TLS, and use it when offered, then start a session as
  `watchly`. The server answers with the kind of password it wants, or with
  why it cannot take connections: starting up, shutting down, in recovery,
  too many clients. Watchly hangs up at the password prompt, which
  PostgreSQL does not log.
- MySQL and MariaDB: the server greets every connection with its version, or
  says why it will not take it. Watchly then logs in as `watchly` with no
  password, over TLS when offered, which fails, and hangs up. Hanging up
  straight after the greeting would count against max_connect_errors, and
  after enough of those MariaDB blocks Watchly's address; a failed login
  does not count.
- Redis and Valkey: PING. `PONG`, or `NOAUTH` from a server that wants a
  password, is up; `LOADING`, `BUSY`, `MASTERDOWN` or a full client list is
  down.
- MongoDB and DocumentDB: `hello`, which needs no login and says whether the
  server is a primary, a secondary or a mongos.

Up when the server answers as its engine does, whatever it thinks of logging
in. Down when nothing answers, something other than the engine answers, or
the server says it cannot take connections now. What none of this can tell
is whether a query would succeed.

Redis and MongoDB cannot switch to TLS mid-connection, so a check of a server
that needs it (ElastiCache with in-transit encryption, DocumentDB, Atlas)
asks for TLS from the start. Certificates are not verified: nothing secret is
sent either way.

Like the other probes, this one never raises for the database being down.
"""

import asyncio
import contextlib
import errno
import ipaddress
import socket
import ssl
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio

from app.monitoring import egress
from app.monitoring.websites.models import DbEngine

#: The user a session is started as, where the protocol asks for one: never
#: a real account, and never sent with a password.
PROBE_USER = "watchly"
DEFAULT_PORTS = {
    DbEngine.POSTGRESQL: 5432,
    DbEngine.MYSQL: 3306,
    DbEngine.REDIS: 6379,
    DbEngine.MONGODB: 27017,
}
LABELS = {
    DbEngine.POSTGRESQL: "PostgreSQL",
    DbEngine.MYSQL: "MySQL",
    DbEngine.REDIS: "Redis",
    DbEngine.MONGODB: "MongoDB",
}
#: The engines that cannot switch to TLS mid-connection, and so take a check's
#: `db_tls` to use it from the start. PostgreSQL and MySQL offer it in their
#: handshake.
TLS_FROM_START = frozenset({DbEngine.REDIS, DbEngine.MONGODB})
#: Seconds allowed for resolving the host name, apart from the timeout.
DNS_TIMEOUT = 5.0
#: The most read of one answer; a handshake's runs to a few hundred bytes.
MAX_ANSWER_BYTES = 65_536


def split_endpoint(endpoint: str) -> tuple[str, int]:
    """`host:port` or `[v6]:port`, as a check stores it, apart."""
    host, _, port = endpoint.rpartition(":")
    return host.removeprefix("[").removesuffix("]"), int(port)


def join_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(slots=True)
class DbResult:
    """What one probe found. `error` is set only when it is down."""

    engine: DbEngine
    port: int
    is_up: bool = False
    #: The IP address connected to, or the one the egress policy refused.
    address: str | None = None
    #: What the server said, in words, e.g. `asks for a SCRAM-SHA-256 password`.
    state: str | None = None
    #: The product, where it is not the engine itself (MariaDB), and the
    #: version the server announced (MySQL and MariaDB do, in their greeting).
    product: str | None = None
    version: str | None = None
    #: Whether the session went over TLS, and which version; None when it
    #: never got that far.
    tls: bool | None = None
    tls_version: str | None = None
    #: MongoDB: `primary`, `secondary`, `arbiter`, `mongos` or `standalone`,
    #: and the replica set it belongs to.
    role: str | None = None
    replica_set: str | None = None
    #: The server's own code for what it said: a SQLSTATE, a MySQL error
    #: number, a Redis error prefix, a MongoDB code name.
    code: str | None = None
    #: And its own message, as sent.
    message: str | None = None
    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    #: From the request to the server's answer; for MySQL, which speaks
    #: first, from the connection to its greeting.
    answer_ms: int | None = None
    #: From start to answer, everything included.
    response_time_ms: int | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def name(self) -> str:
        """`MySQL 8.4.2`, `MariaDB 11.4.2`, `PostgreSQL`."""
        name = self.product or LABELS[self.engine]
        return f"{name} {self.version}" if self.version else name

    @property
    def summary(self) -> str:
        if not self.is_up:
            return self.error or f"{LABELS[self.engine]} did not answer."
        text = f"{self.name} answered in {self.answer_ms} ms"
        return f"{text}: {self.state}" if self.state else text

    def as_dict(self) -> dict:
        """What a check keeps, besides the times and address it has columns for."""
        return {
            "engine": self.engine.value,
            "port": self.port,
            "product": self.product,
            "version": self.version,
            "state": self.state,
            "tls": self.tls,
            "tls_version": self.tls_version,
            "role": self.role,
            "replica_set": self.replica_set,
            "code": self.code,
            "message": self.message,
        }


class _Down(Exception):
    """Ends a probe: the database is down, for the reason given."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _printable(data: bytes, limit: int = 60) -> str | None:
    """The start of `data` as text, when it is text."""
    text = data[:limit].decode("ascii", errors="replace").strip()
    if not text or any(not (ch.isprintable() or ch in "\r\n\t") for ch in text):
        return None
    return " ".join(text.split())


def _tls_context() -> ssl.SSLContext:
    """TLS without verification: a database's certificate is often signed by
    its own CA (RDS's, MySQL's self-made one) or names another host, and the
    probe sends nothing secret."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


_TLS = _tls_context()


class _Session:
    """One connection to the server, its steps timed into `result`."""

    def __init__(
        self,
        result: DbResult,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        timeout: float,
    ) -> None:
        self.result = result
        self.reader = reader
        self.writer = writer
        self.host = host
        self.timeout = timeout
        self._asked_at: float | None = None

    @property
    def label(self) -> str:
        return LABELS[self.result.engine]

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        try:
            await asyncio.wait_for(self.writer.drain(), self.timeout)
        except (OSError, TimeoutError) as exc:
            raise _Down("connection_closed", f"The connection broke while sending: {exc}") from exc

    def asked(self) -> None:
        """The request the answer time is measured from has gone out."""
        self._asked_at = time.perf_counter()

    def _answered(self) -> None:
        if self.result.answer_ms is None and self._asked_at is not None:
            self.result.answer_ms = _ms_since(self._asked_at)

    def _closed_hint(self) -> str:
        if self.result.engine in TLS_FROM_START and not self.result.tls:
            return (
                f": not {self.label}, a proxy with nothing behind it, or a server "
                "that needs TLS (turn on TLS for this check)."
            )
        return f": not {self.label}, or a proxy with nothing behind it."

    async def read(self, size: int, awaiting: str) -> bytes:
        """Exactly `size` bytes of `awaiting` (`its greeting`, …)."""
        return await self._receive(self.reader.readexactly(size), awaiting)

    async def read_line(self, awaiting: str) -> bytes:
        return await self._receive(self.reader.readuntil(b"\n"), awaiting)

    async def _receive(self, reading: Awaitable[bytes], awaiting: str) -> bytes:
        try:
            data = await asyncio.wait_for(reading, self.timeout)
        except TimeoutError as exc:
            raise _Down(
                "read_timeout",
                f"Connected, but {awaiting} did not come within {self.timeout:g} s.",
            ) from exc
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                raise await self.not_engine(exc.partial) from exc
            raise _Down(
                "connection_closed",
                f"The server closed the connection before {awaiting}{self._closed_hint()}",
            ) from exc
        except asyncio.LimitOverrunError as exc:
            raise await self.not_engine(b"") from exc
        except OSError as exc:
            raise _Down("connection_closed", f"The connection broke before {awaiting}: {exc}") from exc
        self._answered()
        return data

    async def start_tls(self) -> None:
        started = time.perf_counter()
        hint = (
            " If the server does not use TLS, turn TLS off for this check."
            if self.result.engine in TLS_FROM_START
            else ""
        )
        sni = None if _is_ip(self.host) else self.host
        try:
            await asyncio.wait_for(
                self.writer.start_tls(_TLS, server_hostname=sni), self.timeout
            )
        except TimeoutError as exc:
            raise _Down(
                "tls_error", f"The TLS handshake did not finish within {self.timeout:g} s.{hint}"
            ) from exc
        except OSError as exc:
            reason = str(exc).strip() or "the server hung up"
            raise _Down("tls_error", f"TLS handshake failed: {reason}.{hint}") from exc
        self.result.tls_ms = _ms_since(started)
        self.result.tls = True
        ssl_object = self.writer.get_extra_info("ssl_object")
        self.result.tls_version = ssl_object.version() if ssl_object else None

    async def not_engine(self, data: bytes) -> _Down:
        """The failure for an answer the engine would never give, quoting it
        when it is text (an SSH banner, an HTTP response…)."""
        with contextlib.suppress(Exception):
            data += await asyncio.wait_for(self.reader.read(60), 0.5)
        preview = _printable(data)
        message = f"Something answered on port {self.result.port}, but not as {self.label} does"
        return _Down(
            "protocol_error",
            f"{message}: “{preview}”." if preview else f"{message}. Is the port right?",
        )


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

_PG_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
_PG_PROTOCOL_3 = 196608
_PG_TERMINATE = b"X\0\0\0\4"
#: What each authentication request asks for; 10 (SASL) names its mechanisms.
_PG_AUTH = {
    2: "a Kerberos ticket",
    3: "a password in clear text",
    5: "an MD5 password",
    7: "GSSAPI credentials",
    9: "SSPI credentials",
}
#: SQLSTATE classes in which the server cannot take a connection now:
#: insufficient resources (53: too many clients, out of memory or disk),
#: operator intervention (57: starting up, shutting down, in recovery) and
#: system errors (58).
_PG_DOWN_CLASSES = frozenset({"53", "57", "58"})
_PG_TOO_MANY_CLIENTS = "53300"


def _pg_fields(body: bytes) -> dict[str, str]:
    """An ErrorResponse's fields by their one-letter codes: C (SQLSTATE), M (message)…"""
    return {
        chr(part[0]): part[1:].decode("utf-8", errors="replace")
        for part in body.split(b"\0")
        if part
    }


async def _postgresql(session: _Session) -> None:
    result = session.result
    await session.send(_PG_SSL_REQUEST)
    reply = await session.read(1, "an answer to its request for TLS")
    if reply == b"S":
        await session.start_tls()
    elif reply == b"N":
        result.tls = False
    else:
        raise await session.not_engine(reply)

    params = b"".join(
        key + b"\0" + value + b"\0"
        for key, value in (
            (b"user", PROBE_USER.encode()),
            (b"database", b"postgres"),
            (b"application_name", b"watchly"),
        )
    )
    params += b"\0"
    session.asked()
    await session.send(struct.pack("!ii", 8 + len(params), _PG_PROTOCOL_3) + params)
    # A notice or a protocol version negotiation may come before the answer.
    for _ in range(8):
        kind = await session.read(1, "an answer to its session request")
        (length,) = struct.unpack("!i", await session.read(4, "the rest of the answer"))
        if not 4 <= length <= MAX_ANSWER_BYTES:
            raise await session.not_engine(kind)
        body = await session.read(length - 4, "the rest of the answer")
        if kind == b"R":
            _pg_auth_request(result, body)
            if body[:4] == b"\0\0\0\0":
                # Authenticated without a password (trust): end the session
                # properly, should the role exist.
                with contextlib.suppress(_Down):
                    await session.send(_PG_TERMINATE)
            return
        if kind == b"E":
            _pg_error(result, _pg_fields(body))
            return
        if kind not in (b"N", b"v"):
            raise await session.not_engine(kind + body)
    raise _Down("protocol_error", "PostgreSQL sent notices, but never an answer.")


def _pg_auth_request(result: DbResult, body: bytes) -> None:
    (code,) = struct.unpack_from("!i", body)
    result.is_up = True
    if code == 0:
        result.state = "asks for no password (trust)"
    elif code == 10:
        mechanisms = [m.decode("ascii", "replace") for m in body[4:].split(b"\0") if m]
        plain = [m for m in mechanisms if not m.endswith("-PLUS")] or mechanisms
        result.state = f"asks for a {plain[0]} password" if plain else "asks for a password"
    else:
        result.state = f"asks for {_PG_AUTH.get(code, 'credentials')}"


def _pg_error(result: DbResult, fields: dict[str, str]) -> None:
    code, message = fields.get("C"), fields.get("M") or "an error"
    result.code, result.message = code, message
    if code and code[:2] in _PG_DOWN_CLASSES:
        raise _Down(
            "db_too_many_connections" if code == _PG_TOO_MANY_CLIENTS else "db_unavailable",
            f"PostgreSQL cannot take connections: {message}.",
        )
    # Turned away by its pg_hba.conf, or let in and then told the role does
    # not exist: either way the server is there and answering.
    result.is_up = True
    result.state = f"refuses {PROBE_USER}: {message}"


# ---------------------------------------------------------------------------
# MySQL and MariaDB
# ---------------------------------------------------------------------------

_CLIENT_LONG_PASSWORD = 0x1
_CLIENT_PROTOCOL_41 = 0x200
_CLIENT_SSL = 0x800
_CLIENT_SECURE_CONNECTION = 0x8000
_CLIENT_PLUGIN_AUTH = 0x80000
#: utf8mb4_general_ci, which MySQL and MariaDB both know.
_MYSQL_CHARSET = 45
_MYSQL_COM_QUIT = b"\x01"
#: Errors in which the server cannot take a connection now: too many
#: connections, shutting down, offline mode.
_MYSQL_TOO_MANY = 1040
_MYSQL_DOWN = frozenset({_MYSQL_TOO_MANY, 1053, 3032})
_MYSQL_HOST_BLOCKED = 1129
_MYSQL_HOST_NOT_ALLOWED = 1130
_MYSQL_ACCESS_DENIED = 1045


def _mysql_frame(seq: int, payload: bytes) -> bytes:
    return struct.pack("<I", len(payload))[:3] + bytes([seq & 0xFF]) + payload


async def _mysql_packet(session: _Session, awaiting: str) -> tuple[int, bytes]:
    header = await session.read(4, awaiting)
    length = int.from_bytes(header[:3], "little")
    if length > MAX_ANSWER_BYTES:
        raise await session.not_engine(header)
    payload = await session.read(length, awaiting) if length else b""
    return header[3], payload


def _mysql_error(payload: bytes) -> tuple[int, str]:
    """An ERR packet's number and message; the `#SQLSTATE` marker comes only
    once the protocol is agreed, so not on an error sent instead of a greeting."""
    (code,) = struct.unpack_from("<H", payload, 1)
    rest = payload[3:]
    if rest[:1] == b"#":
        rest = rest[6:]
    return code, rest.decode("utf-8", errors="replace")


def _mysql_greeting(payload: bytes) -> tuple[str, int, str | None]:
    """A Handshake v10's server version, capabilities and auth plugin."""
    end = payload.index(b"\0", 1)
    version = payload[1:end].decode("ascii", errors="replace")
    # Connection id, the scramble's first 8 bytes and a filler byte.
    pos = end + 1 + 4 + 8 + 1
    (capabilities,) = struct.unpack_from("<H", payload, pos)
    pos += 2
    plugin = None
    if len(payload) > pos:
        # Character set and status flags, then the capabilities' upper half.
        pos += 3
        (upper,) = struct.unpack_from("<H", payload, pos)
        capabilities |= upper << 16
        scramble_length = payload[pos + 2]
        pos += 3 + 10
        if capabilities & _CLIENT_SECURE_CONNECTION:
            pos += max(13, scramble_length - 8)
        if capabilities & _CLIENT_PLUGIN_AUTH and pos < len(payload):
            name_end = payload.find(b"\0", pos)
            plugin = payload[pos : name_end if name_end >= 0 else None].decode("ascii", "replace")
    return version, capabilities, plugin or None


def _mysql_version(result: DbResult, raw: str) -> None:
    """MariaDB 10 prefixes its version with `5.5.5-` for old clients."""
    raw = raw.removeprefix("5.5.5-")
    if "mariadb" in raw.lower():
        result.product = "MariaDB"
        raw = raw[: raw.lower().index("-mariadb")] if "-mariadb" in raw.lower() else raw
    result.version = raw[:64] or None


def _mysql_refusal(result: DbResult, code: int, message: str, *, greeting: bool) -> None:
    """Judge an error the server sent instead of its greeting, or at login.

    Turning Watchly's address away is the server answering, so up. So is
    refusing the login, which is the point. Any other error instead of a
    greeting means it serves no one."""
    result.code, result.message = str(code), message
    if code in _MYSQL_DOWN or (
        greeting and code not in (_MYSQL_HOST_BLOCKED, _MYSQL_HOST_NOT_ALLOWED)
    ):
        raise _Down(
            "db_too_many_connections" if code == _MYSQL_TOO_MANY else "db_unavailable",
            f"MySQL cannot take connections: {message}",
        )
    result.is_up = True
    if code == _MYSQL_ACCESS_DENIED:
        result.state = "asks for a password"
    elif code == _MYSQL_HOST_BLOCKED:
        result.state = (
            "blocks Watchly's address after too many broken connections; "
            "FLUSH HOSTS unblocks it"
        )
    elif code == _MYSQL_HOST_NOT_ALLOWED:
        result.state = "takes no logins from Watchly's address"
    else:
        result.state = f"refuses {PROBE_USER}: {message}"


async def _mysql(session: _Session) -> None:
    result = session.result
    # MySQL speaks first: its answer time runs from the connection.
    session.asked()
    _, payload = await _mysql_packet(session, "its greeting")
    if payload[:1] == b"\xff":
        try:
            code, message = _mysql_error(payload)
        except struct.error:
            raise await session.not_engine(payload) from None
        _mysql_refusal(result, code, message, greeting=True)
        return
    if payload[:1] != b"\x0a":
        raise await session.not_engine(payload)
    try:
        raw_version, capabilities, plugin = _mysql_greeting(payload)
    except (ValueError, IndexError, struct.error):
        raise await session.not_engine(payload) from None
    _mysql_version(result, raw_version)
    result.is_up = True
    result.tls = False
    result.state = "sent its greeting"
    await _mysql_sign_off(session, capabilities, plugin)


async def _mysql_sign_off(session: _Session, capabilities: int, plugin: str | None) -> None:
    """Log in as `watchly` with no password, which fails, then hang up.

    The greeting already showed the server up; this is so it counts a failed
    login rather than a broken handshake. Broken handshakes count towards
    max_connect_errors (100 by default), after which the server blocks
    Watchly's address; so does a login it refuses for want of TLS, which is
    why TLS is used whenever the server offers it. A server that never
    answers the login is still up."""
    result = session.result
    flags = _CLIENT_LONG_PASSWORD | _CLIENT_PROTOCOL_41 | _CLIENT_SECURE_CONNECTION
    flags |= capabilities & (_CLIENT_PLUGIN_AUTH | _CLIENT_SSL)
    head = struct.pack("<IIB23x", flags, 1 << 24, _MYSQL_CHARSET)
    # The user, then an empty auth response: no password.
    login = head + PROBE_USER.encode() + b"\0" + b"\0"
    if flags & _CLIENT_PLUGIN_AUTH and plugin:
        login += plugin.encode() + b"\0"
    seq = 1
    try:
        if flags & _CLIENT_SSL:
            await session.send(_mysql_frame(seq, head))
            await session.start_tls()
            seq += 1
        await session.send(_mysql_frame(seq, login))
        for _ in range(3):
            seq, reply = await _mysql_packet(session, "an answer to its login")
            if reply[:1] == b"\x00":
                result.state = f"lets {PROBE_USER} in without a password"
                await session.send(_mysql_frame(0, _MYSQL_COM_QUIT))
                return
            if reply[:1] == b"\xff":
                _mysql_refusal(result, *_mysql_error(reply), greeting=False)
                return
            if reply[:1] not in (b"\xfe", b"\x01"):
                return
            # Asked to switch auth plugin, or for more: still no password.
            await session.send(_mysql_frame(seq + 1, b""))
    except (_Down, struct.error):
        return


# ---------------------------------------------------------------------------
# Redis and Valkey
# ---------------------------------------------------------------------------

_REDIS_PING = b"*1\r\n$4\r\nPING\r\n"
#: Error prefixes from a server that cannot serve now: loading its dataset,
#: running a long script, a replica cut off from its primary.
_REDIS_DOWN = frozenset({"LOADING", "BUSY", "MASTERDOWN", "TRYAGAIN"})
#: Error prefixes from a server that wants a password.
_REDIS_AUTH = frozenset({"NOAUTH", "WRONGPASS"})


async def _redis(session: _Session) -> None:
    result = session.result
    session.asked()
    await session.send(_REDIS_PING)
    line = await session.read_line("an answer to PING")
    text = line.decode("utf-8", errors="replace").strip()
    if line[:1] == b"+":
        result.is_up = True
        result.state = "answers without a password"
        return
    if line[:1] != b"-":
        raise await session.not_engine(line)
    message = text[1:]
    prefix = message.split(" ", 1)[0]
    result.code, result.message = prefix, message
    if prefix in _REDIS_DOWN:
        raise _Down("db_unavailable", f"Redis cannot serve now: {message}")
    if "max number of clients" in message:
        raise _Down("db_too_many_connections", f"Redis cannot take connections: {message}")
    result.is_up = True
    result.state = "asks for a password" if prefix in _REDIS_AUTH else message


# ---------------------------------------------------------------------------
# MongoDB and DocumentDB
# ---------------------------------------------------------------------------

_OP_MSG = 2013
_MONGO_COMMAND_NOT_FOUND = 59
#: BSON types of a fixed size, by type byte, which the reply's other fields
#: may use and the probe skips.
_BSON_FIXED = {0x07: 12, 0x09: 8, 0x0A: 0, 0x11: 8, 0x13: 16, 0x7F: 0, 0xFF: 0}


def _bson_encode(fields: dict[str, int | str]) -> bytes:
    """A flat document of int32s and strings: a command is no more."""
    body = b""
    for key, value in fields.items():
        name = key.encode() + b"\0"
        if isinstance(value, int):
            body += b"\x10" + name + struct.pack("<i", value)
        else:
            data = value.encode() + b"\0"
            body += b"\x02" + name + struct.pack("<i", len(data)) + data
    return struct.pack("<i", len(body) + 5) + body + b"\0"


def _bson_decode(data: bytes) -> dict:
    """A BSON document. Values the probe has no use for (binary, ObjectId,
    timestamps…) come back as None."""
    (length,) = struct.unpack_from("<i", data)
    if not 5 <= length <= len(data):
        raise ValueError("bad document length")
    out: dict = {}
    pos = 4
    while pos < length - 1:
        kind = data[pos]
        name_end = data.index(b"\0", pos + 1)
        name = data[pos + 1 : name_end].decode("utf-8", errors="replace")
        pos = name_end + 1
        value = None
        match kind:
            case 0x01:
                (value,) = struct.unpack_from("<d", data, pos)
                pos += 8
            case 0x02 | 0x0D | 0x0E:
                (size,) = struct.unpack_from("<i", data, pos)
                value = data[pos + 4 : pos + 3 + size].decode("utf-8", errors="replace")
                pos += 4 + size
            case 0x03 | 0x04:
                (size,) = struct.unpack_from("<i", data, pos)
                value = _bson_decode(data[pos : pos + size])
                if kind == 0x04:
                    value = list(value.values())
                pos += size
            case 0x05:
                (size,) = struct.unpack_from("<i", data, pos)
                pos += 5 + size
            case 0x08:
                value = data[pos] != 0
                pos += 1
            case 0x0B:
                pos = data.index(b"\0", data.index(b"\0", pos) + 1) + 1
            case 0x10:
                (value,) = struct.unpack_from("<i", data, pos)
                pos += 4
            case 0x12:
                (value,) = struct.unpack_from("<q", data, pos)
                pos += 8
            case _ if kind in _BSON_FIXED:
                pos += _BSON_FIXED[kind]
            case _:
                raise ValueError(f"BSON type {kind:#x}")
        out[name] = value
    return out


async def _mongo_command(session: _Session, request_id: int, command: str) -> dict:
    document = _bson_encode({command: 1, "$db": "admin"})
    # No flags, then one body section (kind 0) holding the command.
    body = struct.pack("<I", 0) + b"\0" + document
    await session.send(struct.pack("<iiii", 16 + len(body), request_id, 0, _OP_MSG) + body)
    header = await session.read(16, f"an answer to {command}")
    length, _, _, opcode = struct.unpack("<iiii", header)
    if opcode != _OP_MSG or not 21 <= length <= MAX_ANSWER_BYTES:
        raise await session.not_engine(header)
    reply = await session.read(length - 16, f"the rest of the answer to {command}")
    if reply[4:5] != b"\0":
        raise await session.not_engine(header + reply)
    try:
        return _bson_decode(reply[5:])
    except (ValueError, IndexError, struct.error):
        raise await session.not_engine(header + reply) from None


async def _mongodb(session: _Session) -> None:
    result = session.result
    session.asked()
    reply = await _mongo_command(session, 1, "hello")
    if not reply.get("ok") and reply.get("code") == _MONGO_COMMAND_NOT_FOUND:
        # Older than 4.4.2 (or 4.2.10, 4.0.21): the old name.
        reply = await _mongo_command(session, 2, "isMaster")
    if not reply.get("ok"):
        result.code = reply.get("codeName") or (str(reply["code"]) if "code" in reply else None)
        result.message = reply.get("errmsg")
        raise _Down("db_unavailable", f"MongoDB answered with an error: {result.message or result.code}")
    result.replica_set = reply.get("setName")
    if reply.get("msg") == "isdbgrid":
        result.role = "mongos"
    elif reply.get("isWritablePrimary") or reply.get("ismaster"):
        result.role = "primary" if result.replica_set else "standalone"
    elif reply.get("secondary"):
        result.role = "secondary"
    elif reply.get("arbiterOnly"):
        result.role = "arbiter"
    else:
        # A member starting up, recovering, removed, or in a set never
        # initiated: it serves neither reads nor writes.
        result.message = reply.get("info")
        raise _Down(
            "db_unavailable",
            "MongoDB is neither primary nor secondary: starting up, recovering, "
            "or in a replica set that was never initiated"
            + (f" ({result.message})." if result.message else "."),
        )
    result.is_up = True
    result.state = (
        f"{result.role} of {result.replica_set}"
        if result.replica_set and result.role in ("primary", "secondary", "arbiter")
        else result.role
    )


_HANDSHAKES: dict[DbEngine, Callable[[_Session], Awaitable[None]]] = {
    DbEngine.POSTGRESQL: _postgresql,
    DbEngine.MYSQL: _mysql,
    DbEngine.REDIS: _redis,
    DbEngine.MONGODB: _mongodb,
}


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


async def _resolve(host: str, port: int, scope: egress.Scope, result: DbResult) -> str:
    """The address to connect to: the first the egress policy allows."""
    if _is_ip(host):
        addresses = [host]
    else:
        started = time.perf_counter()
        try:
            with anyio.fail_after(DNS_TIMEOUT):
                infos = await anyio.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG
                )
        except TimeoutError as exc:
            raise _Down("dns_error", f"DNS lookup of {host} timed out.") from exc
        except OSError as exc:
            raise _Down("dns_error", f"DNS lookup of {host} failed: {exc}") from exc
        result.dns_ms = _ms_since(started)
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    try:
        return egress.vet(addresses, scope)[0]
    except egress.BlockedAddressError as exc:
        result.address = exc.address
        raise _Down("blocked_address", f"Refused by the egress policy: {exc.reason}") from exc


async def _connect(
    address: str, port: int, timeout: float, result: DbResult
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    where = join_endpoint(address, port)
    started = time.perf_counter()
    try:
        streams = await asyncio.wait_for(
            asyncio.open_connection(address, port, limit=MAX_ANSWER_BYTES), timeout
        )
    except TimeoutError as exc:
        raise _Down(
            "connect_timeout",
            f"No answer from {where} within {timeout:g} s: a firewall or security "
            "group drops the packets, or the host is down.",
        ) from exc
    except ConnectionRefusedError as exc:
        raise _Down(
            "connect_refused",
            f"{where} refused the connection: the host is up, but nothing listens on port {port}.",
        ) from exc
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            raise _Down("no_route", f"No route to {where}: {exc.strerror or exc}") from exc
        raise _Down("connect_error", f"Could not connect to {where}: {exc.strerror or exc}") from exc
    result.connect_ms = _ms_since(started)
    return streams


async def probe(
    host: str,
    port: int,
    engine: DbEngine,
    *,
    timeout: float,
    tls: bool = False,
    scope: egress.Scope,
) -> DbResult:
    """Open a session with the server at `host:port` as far as its first
    answer. `timeout` applies to each step: connecting, TLS, the answer.
    `tls` asks for TLS from the start, for the engines in TLS_FROM_START."""
    result = DbResult(engine=engine, port=port)
    started = time.perf_counter()
    writer = None
    try:
        result.address = await _resolve(host, port, scope, result)
        reader, writer = await _connect(result.address, port, timeout, result)
        session = _Session(result, reader, writer, host, timeout)
        if tls and engine in TLS_FROM_START:
            await session.start_tls()
        elif engine in TLS_FROM_START:
            result.tls = False
        await _HANDSHAKES[engine](session)
    except _Down as exc:
        result.is_up = False
        result.error_type, result.error = exc.error_type, exc.message
    finally:
        result.response_time_ms = _ms_since(started)
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
    return result
