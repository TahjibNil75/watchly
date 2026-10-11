"""Mail probes: whether an SMTP server takes a login, without sending mail.

A port that takes connections proves little, and neither does a greeting: a
provider that has revoked or rotated the SMTP credentials still greets, and
then refuses every message. So the probe goes one step further than a mail
client does before it sends:

1. Connect, with TLS from the first byte when the check uses implicit TLS
   (SMTPS, 465).
2. Read the greeting, which must be 220. A 421 or 554 is the server saying
   it will not take mail now.
3. EHLO, and with STARTTLS (submission, 587) switch to TLS and EHLO again.
4. With a user name, AUTH PLAIN (or LOGIN, where PLAIN is not offered),
   which must be 235.
5. QUIT, always, and wait for the 221: a client that hangs up mid-session
   looks like an attack to some servers, and enough of those gets an address
   blocked.

Up when the login is accepted, or with no user name when the server greets
and answers EHLO. No message is sent, so no quota is used. What this cannot
tell is whether a message would be delivered: that is the provider's quota
and reputation, which `mail_quota` reads for SES and Resend.

Unlike `db_probe`, this one sends a password, so the certificate is verified,
and a password is never sent over a session that is not encrypted.

Like the other probes, this one never raises for the server being down.
"""

import asyncio
import base64
import contextlib
import ssl
import time
from dataclasses import dataclass, field

from app.monitoring import egress
from app.monitoring.websites.db_probe import (
    _connect,
    _Down,
    _is_ip,
    _ms_since,
    _printable,
    _resolve,
)
from app.monitoring.websites.models import MailTls

#: Each TLS mode's usual port.
DEFAULT_PORTS = {MailTls.STARTTLS: 587, MailTls.IMPLICIT: 465, MailTls.NONE: 25}
#: What the probe calls itself in EHLO. Servers take any name from a client
#: that logs in; none of them looks it up.
HELO_NAME = "watchly-monitor"
#: Lines read of one reply at most; an EHLO reply runs to a dozen.
MAX_REPLY_LINES = 64
#: How long the goodbye may take: the result is known by then.
QUIT_TIMEOUT = 2.0
#: The mechanisms the probe can log in with, in the order it prefers them.
SUPPORTED_MECHANISMS = ("PLAIN", "LOGIN")
#: Login refusals that are about the credentials, not the server.
_AUTH_REFUSED = {"530", "534", "535", "538", "500", "501", "504"}


@dataclass(slots=True)
class MailResult:
    """What one probe found. `error` is set only when it is down."""

    port: int
    tls_mode: MailTls
    is_up: bool = False
    #: The IP address connected to, or the one the egress policy refused.
    address: str | None = None
    #: The greeting after its code, e.g. `email-smtp.amazonaws.com ESMTP
    #: SimpleEmailService-d-ABC123`.
    banner: str | None = None
    #: The EHLO keywords offered (after STARTTLS, when used): `SIZE`, `AUTH`,
    #: `8BITMIME`, `PIPELINING`…
    extensions: list[str] = field(default_factory=list)
    #: The largest message the server takes, from SIZE; None when it did not say.
    max_size: int | None = None
    auth_mechanisms: list[str] = field(default_factory=list)
    #: Whether the session went over TLS, and which version.
    tls: bool | None = None
    tls_version: str | None = None
    #: True once logged in; None when the check has no user name, or never
    #: got as far as logging in.
    authenticated: bool | None = None
    auth_mechanism: str | None = None
    #: The server's reply code and text where it refused something.
    code: str | None = None
    message: str | None = None
    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    #: From connecting to the greeting.
    greeting_ms: int | None = None
    #: From AUTH to the server's verdict.
    auth_ms: int | None = None
    response_time_ms: int | None = None
    error: str | None = None
    error_type: str | None = None

    @property
    def summary(self) -> str:
        if not self.is_up:
            return self.error or "The SMTP server did not answer."
        if self.authenticated:
            return f"SMTP login accepted in {self.response_time_ms} ms"
        return f"SMTP server answered in {self.response_time_ms} ms"

    def as_dict(self) -> dict:
        """What a check keeps, besides the times and address it has columns for."""
        return {
            "port": self.port,
            "tls_mode": self.tls_mode.value,
            "banner": self.banner,
            "extensions": self.extensions,
            "max_size": self.max_size,
            "auth_mechanisms": self.auth_mechanisms,
            "tls": self.tls,
            "tls_version": self.tls_version,
            "authenticated": self.authenticated,
            "auth_mechanism": self.auth_mechanism,
            "code": self.code,
            "message": self.message,
            "greeting_ms": self.greeting_ms,
            "auth_ms": self.auth_ms,
        }


def _verified_context(host: str) -> ssl.SSLContext:
    """TLS that checks the certificate, and the name on it unless `host` is
    an address: a password goes over it."""
    context = ssl.create_default_context()
    if _is_ip(host):
        context.check_hostname = False
    return context


@dataclass(slots=True)
class _Reply:
    code: str
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(line for line in self.lines if line)

    @property
    def ok(self) -> bool:
        return self.code.startswith("2")


class _Session:
    """One SMTP conversation, its steps timed into `result`."""

    def __init__(
        self,
        result: MailResult,
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
        #: Whether the connection still works for a QUIT.
        self.usable = True

    async def send(self, line: str) -> None:
        self.writer.write(line.encode("ascii") + b"\r\n")
        try:
            await asyncio.wait_for(self.writer.drain(), self.timeout)
        except (OSError, TimeoutError) as exc:
            self.usable = False
            raise _Down("connection_closed", f"The connection broke while sending: {exc}") from exc

    async def reply(self, awaiting: str, timeout: float | None = None) -> _Reply:
        """One reply, all its lines: `250-…` continues, `250 …` ends it."""
        code: str | None = None
        lines: list[str] = []
        for _ in range(MAX_REPLY_LINES):
            raw = await self._line(awaiting, timeout)
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if len(text) < 3 or not text[:3].isdigit() or (len(text) > 3 and text[3] not in " -"):
                self.usable = False
                raise self._not_smtp(raw)
            if code is not None and text[:3] != code:
                self.usable = False
                raise self._not_smtp(raw)
            code = text[:3]
            lines.append(text[4:].strip())
            if len(text) == 3 or text[3] == " ":
                return _Reply(code, lines)
        self.usable = False
        raise _Down("protocol_error", f"The server's answer to {awaiting} never ended.")

    async def _line(self, awaiting: str, timeout: float | None) -> bytes:
        try:
            return await asyncio.wait_for(self.reader.readuntil(b"\n"), timeout or self.timeout)
        except TimeoutError as exc:
            self.usable = False
            raise _Down(
                "read_timeout",
                f"Connected, but {awaiting} did not come within {timeout or self.timeout:g} s.",
            ) from exc
        except asyncio.IncompleteReadError as exc:
            self.usable = False
            if exc.partial:
                raise self._not_smtp(exc.partial) from exc
            raise _Down(
                "connection_closed",
                f"The server closed the connection before {awaiting}"
                + (
                    ": a server that wants implicit TLS (465), or a firewall that cuts SMTP."
                    if self.result.tls_mode is not MailTls.IMPLICIT
                    else "."
                ),
            ) from exc
        except asyncio.LimitOverrunError as exc:
            self.usable = False
            raise self._not_smtp(b"") from exc
        except OSError as exc:
            self.usable = False
            raise _Down("connection_closed", f"The connection broke before {awaiting}: {exc}") from exc

    def _not_smtp(self, data: bytes) -> _Down:
        preview = _printable(data)
        message = f"Something answered on port {self.result.port}, but not as an SMTP server does"
        if preview is None and self.result.tls_mode is not MailTls.IMPLICIT:
            message += ": a server that wants implicit TLS (465)?"
            return _Down("protocol_error", message)
        return _Down(
            "protocol_error",
            f"{message}: “{preview}”." if preview else f"{message}. Is the port right?",
        )

    async def start_tls(self) -> None:
        started = time.perf_counter()
        sni = None if _is_ip(self.host) else self.host
        try:
            await asyncio.wait_for(
                self.writer.start_tls(_verified_context(self.host), server_hostname=sni),
                self.timeout,
            )
        except TimeoutError as exc:
            self.usable = False
            raise _Down(
                "tls_error", f"The TLS handshake did not finish within {self.timeout:g} s."
            ) from exc
        except ssl.SSLCertVerificationError as exc:
            self.usable = False
            raise _Down(
                "tls_error",
                f"The server's certificate is not trusted: {exc.verify_message or exc}. "
                "Watchly will not send a password over it.",
            ) from exc
        except OSError as exc:
            self.usable = False
            reason = str(exc).strip() or "the server hung up"
            hint = (
                " Is it a STARTTLS port (587) rather than an implicit-TLS one?"
                if self.result.tls_mode is MailTls.IMPLICIT
                else ""
            )
            raise _Down("tls_error", f"TLS handshake failed: {reason}.{hint}") from exc
        self.result.tls_ms = _ms_since(started)
        self.result.tls = True
        ssl_object = self.writer.get_extra_info("ssl_object")
        self.result.tls_version = ssl_object.version() if ssl_object else None

    def refused(self, reply: _Reply) -> None:
        self.result.code = reply.code
        self.result.message = reply.text[:500] or None

    async def quit(self) -> None:
        """Say goodbye and wait for it to be acknowledged. Never raises."""
        if not self.usable:
            return
        with contextlib.suppress(Exception):
            self.writer.write(b"QUIT\r\n")
            await asyncio.wait_for(self.writer.drain(), QUIT_TIMEOUT)
            await asyncio.wait_for(self.reader.readuntil(b"\n"), QUIT_TIMEOUT)


def _read_ehlo(result: MailResult, reply: _Reply) -> None:
    """The keywords an EHLO reply offers; its first line is the server's name."""
    extensions: list[str] = []
    mechanisms: list[str] = []
    for line in reply.lines[1:]:
        words = line.replace("=", " ", 1).split() if line.upper().startswith("AUTH=") else line.split()
        if not words:
            continue
        keyword = words[0].upper()
        if keyword not in extensions:
            extensions.append(keyword)
        if keyword == "AUTH":
            mechanisms.extend(word.upper() for word in words[1:] if word.upper() not in mechanisms)
        elif keyword == "SIZE" and len(words) > 1 and words[1].isdigit():
            result.max_size = int(words[1]) or None
    result.extensions = extensions
    result.auth_mechanisms = mechanisms


async def _ehlo(session: _Session) -> None:
    await session.send(f"EHLO {HELO_NAME}")
    reply = await session.reply("an answer to EHLO")
    if not reply.ok:
        session.refused(reply)
        raise _Down(
            "smtp_unavailable" if reply.code.startswith("4") else "protocol_error",
            f"The server refused EHLO: {reply.code} {reply.text}".strip(),
        )
    _read_ehlo(session.result, reply)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


async def _login(session: _Session, username: str, password: str) -> None:
    result = session.result
    if not result.tls:
        # The schema refuses a login without TLS; this is the last guard.
        raise _Down(
            "auth_needs_tls", "Watchly will not send a password over a session that is not encrypted."
        )
    mechanism = next((m for m in SUPPORTED_MECHANISMS if m in result.auth_mechanisms), None)
    if mechanism is None:
        offered = ", ".join(result.auth_mechanisms) or "none"
        raise _Down(
            "auth_unsupported",
            f"The server offers no login Watchly can use (offered: {offered}).",
        )
    result.auth_mechanism = mechanism
    started = time.perf_counter()
    if mechanism == "PLAIN":
        await session.send("AUTH PLAIN " + _b64(f"\0{username}\0{password}"))
        reply = await session.reply("an answer to the login")
    else:
        await session.send("AUTH LOGIN")
        reply = await session.reply("the prompt for a user name")
        if reply.code == "334":
            await session.send(_b64(username))
            reply = await session.reply("the prompt for a password")
            if reply.code == "334":
                await session.send(_b64(password))
                reply = await session.reply("an answer to the login")
    result.auth_ms = _ms_since(started)
    if reply.code == "235":
        result.authenticated = True
        return
    session.refused(reply)
    result.authenticated = False
    if reply.code in _AUTH_REFUSED:
        said = f"{reply.code} {reply.text}".strip().rstrip(".")
        raise _Down(
            "auth_failed",
            f"The server refused the login: {said}. Check the user name and "
            "password, or whether they were rotated.",
        )
    raise _Down(
        "smtp_unavailable" if reply.code.startswith("4") else "protocol_error",
        f"The login did not go through: {reply.code} {reply.text}".strip(),
    )


async def _conversation(session: _Session, username: str | None, password: str | None) -> None:
    result = session.result
    started = time.perf_counter()
    greeting = await session.reply("its greeting")
    result.greeting_ms = _ms_since(started)
    if greeting.code != "220":
        session.refused(greeting)
        unavailable = greeting.code.startswith("4") or greeting.code == "554"
        raise _Down(
            "smtp_unavailable" if unavailable else "protocol_error",
            f"The server will not take mail now: {greeting.code} {greeting.text}".strip(),
        )
    result.banner = greeting.text[:255] or None
    await _ehlo(session)

    if result.tls_mode is MailTls.STARTTLS:
        if "STARTTLS" not in result.extensions:
            raise _Down(
                "starttls_unsupported",
                "The server does not offer STARTTLS: use implicit TLS (465), or "
                "check the port.",
            )
        await session.send("STARTTLS")
        reply = await session.reply("an answer to STARTTLS")
        if reply.code != "220":
            session.refused(reply)
            raise _Down(
                "tls_error", f"The server refused STARTTLS: {reply.code} {reply.text}".strip()
            )
        await session.start_tls()
        # What was offered before TLS no longer holds.
        await _ehlo(session)
    elif result.tls_mode is MailTls.NONE:
        result.tls = False

    if username:
        await _login(session, username, password or "")
    result.is_up = True


async def probe(
    host: str,
    port: int,
    tls_mode: MailTls,
    *,
    timeout: float,
    scope: egress.Scope,
    username: str | None = None,
    password: str | None = None,
) -> MailResult:
    """Talk to the SMTP server at `host:port` as far as logging in, then
    hang up. `timeout` applies to each step: connecting, TLS, each answer."""
    result = MailResult(port=port, tls_mode=tls_mode)
    started = time.perf_counter()
    writer = None
    session = None
    try:
        result.address = await _resolve(host, port, scope, result)
        reader, writer = await _connect(result.address, port, timeout, result)
        session = _Session(result, reader, writer, host, timeout)
        if tls_mode is MailTls.IMPLICIT:
            await session.start_tls()
        await _conversation(session, username, password)
    except _Down as exc:
        result.is_up = False
        result.error_type, result.error = exc.error_type, exc.message
    finally:
        result.response_time_ms = _ms_since(started)
        if session is not None:
            await session.quit()
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
    return result
