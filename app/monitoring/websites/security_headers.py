"""The security headers a site sends, and how well each protects its visitors.

Each successful HTTP check keeps the raw values of the five headers below from
the final response (`capture`), on the site only, not on every check: a CSP can
run to kilobytes. They are graded when read (`grade`), so the rules can change
without touching stored data.

A header is `ok`, `weak` (sent, but set so it protects little) or `missing`.
The score is how many are ok, out of five, and the letter follows from it.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Longest value kept per header.
MAX_VALUE_LENGTH = 1024

#: HSTS shorter than this is the "weak" side of what browsers' preload lists
#: and most scanners ask for (180 days).
HSTS_MIN_AGE = 15_552_000

#: (key, header name as displayed), in the order the site's page lists them.
HEADERS = (
    ("hsts", "Strict-Transport-Security"),
    ("csp", "Content-Security-Policy"),
    ("frame", "X-Frame-Options"),
    ("nosniff", "X-Content-Type-Options"),
    ("referrer", "Referrer-Policy"),
)

_GRADES = {5: "A", 4: "B", 3: "C", 2: "D"}


def capture(url: str, headers) -> dict:
    """What to store of a response: its URL, and each header's value or None.
    `headers` is a case-insensitive mapping, like httpx's."""
    return {
        "url": url,
        "headers": {
            name.lower(): (value[:MAX_VALUE_LENGTH] if (value := headers.get(name)) else None)
            for _, name in HEADERS
        },
    }


@dataclass(frozen=True, slots=True)
class HeaderGrade:
    key: str
    header: str
    #: `ok`, `weak` or `missing`.
    status: str
    value: str | None
    #: Why it is weak or missing, or what it does; None when there is nothing to add.
    note: str | None = None


def _directives(csp: str | None) -> dict[str, str]:
    """A CSP as {directive: its sources}, lower-cased."""
    out: dict[str, str] = {}
    for part in (csp or "").split(";"):
        name, _, rest = part.strip().partition(" ")
        if name:
            out.setdefault(name.lower(), rest.strip().lower())
    return out


def _hsts(value: str | None, https: bool) -> tuple[str, str | None]:
    if not https:
        return "missing", "Browsers only honour HSTS over HTTPS; serve the site over HTTPS."
    if value is None:
        return "missing", "Browsers may connect over plain HTTP first, where the traffic can be tampered with."
    match = re.search(r"max-age\s*=\s*\"?(\d+)", value, re.IGNORECASE)
    if not match:
        return "weak", "No max-age, so browsers ignore it."
    age = int(match.group(1))
    if age == 0:
        return "weak", "max-age=0 switches HSTS off."
    if age < HSTS_MIN_AGE:
        return "weak", f"max-age is {age // 86_400} days; 180 days or more is recommended."
    return "ok", None


def _csp(value: str | None) -> tuple[str, str | None]:
    if value is None:
        return "missing", "Nothing limits where scripts may load from, so an injected script runs."
    directives = _directives(value)
    scripts = directives.get("script-src", directives.get("default-src"))
    if scripts is None:
        return "weak", "Sets no script-src or default-src, so it does not limit scripts."
    # A nonce or hash makes browsers that support them ignore 'unsafe-inline'.
    guarded = "'nonce-" in scripts or "'sha256-" in scripts or "'strict-dynamic'" in scripts
    if "'unsafe-inline'" in scripts and not guarded:
        return "weak", "Allows 'unsafe-inline' scripts, which is what injected scripts need."
    if "'unsafe-eval'" in scripts:
        return "weak", "Allows 'unsafe-eval'."
    return "ok", None


def _frame(value: str | None, csp: str | None) -> tuple[str, str | None]:
    if "frame-ancestors" in _directives(csp):
        return "ok", "Covered by the CSP's frame-ancestors."
    if value is None:
        return "missing", "Other sites can frame this one, which enables clickjacking."
    if value.strip().upper() in {"DENY", "SAMEORIGIN"}:
        return "ok", None
    return "weak", f"“{value}” is not DENY or SAMEORIGIN, which browsers ignore."


def _nosniff(value: str | None) -> tuple[str, str | None]:
    if value is None:
        return "missing", "Browsers may guess a file's type and run it as a script."
    if value.strip().lower() == "nosniff":
        return "ok", None
    return "weak", "The only valid value is nosniff."


def _referrer(value: str | None) -> tuple[str, str | None]:
    if value is None:
        return "missing", "Browsers fall back to their default policy."
    # A list gives fallbacks; browsers use the last one they know.
    policy = value.split(",")[-1].strip().lower()
    if policy in {"unsafe-url", "no-referrer-when-downgrade"}:
        return "weak", f"{policy} sends full URLs, paths and queries included, to other sites."
    return "ok", None


@dataclass(frozen=True, slots=True)
class SecurityReport:
    url: str
    items: list[HeaderGrade]

    @property
    def score(self) -> int:
        return sum(item.status == "ok" for item in self.items)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def grade(self) -> str:
        return _GRADES.get(self.score, "F")


def grade(stored: dict | None) -> SecurityReport | None:
    """Grade what `capture` stored; None when nothing is."""
    if not stored:
        return None
    url = stored.get("url") or ""
    values = stored.get("headers") or {}

    def get(name: str) -> str | None:
        return values.get(name.lower())

    https = urlsplit(url).scheme == "https"
    csp = get("Content-Security-Policy")
    rules = {
        "hsts": _hsts(get("Strict-Transport-Security"), https),
        "csp": _csp(csp),
        "frame": _frame(get("X-Frame-Options"), csp),
        "nosniff": _nosniff(get("X-Content-Type-Options")),
        "referrer": _referrer(get("Referrer-Policy")),
    }
    return SecurityReport(
        url=url,
        items=[
            HeaderGrade(
                key=key,
                header=header,
                status=rules[key][0],
                value=get(header),
                note=rules[key][1],
            )
            for key, header in HEADERS
        ],
    )
