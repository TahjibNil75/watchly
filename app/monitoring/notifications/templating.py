"""Filling `{{placeholders}}` in an admin-written subject and body.

Deliberately not a template engine. Subjects and bodies are edited through the
API by people who are not the operator, so the language is one thing only:
`{{name}}`, replaced by a value the event supplies. No expressions, no
attribute access, no includes — nothing a template author could use to reach
anything but the values handed in.

Values are treated as untrusted. Many of them come from the monitored site
(an error message, a reason phrase), so each channel escapes them for its own
markup; the author's own template text is never escaped away.
"""

import re
from collections.abc import Callable, Iterable

from app.monitoring.alerts.base import Message, Notification, escape_mrkdwn
from app.monitoring.notifications.catalog import CATALOG

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
#: Everything but tab and newline. NUL and friends have no business in a mail.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SUBJECT_MAX = 255
BODY_MAX = 4000


def placeholders_in(template: str) -> list[str]:
    """Distinct placeholder names in `template`, in order of appearance."""
    return list(dict.fromkeys(_PLACEHOLDER.findall(template)))


def unknown_placeholders(template: str, allowed: Iterable[str]) -> list[str]:
    allowed = set(allowed)
    return [name for name in placeholders_in(template) if name not in allowed]


def render(
    template: str,
    context: dict[str, str],
    escape: Callable[[str], str] | None = None,
) -> str:
    """Substitute known placeholders; leave any unknown one exactly as written.

    `escape` is applied to the substituted values only, never to the template.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            return match.group(0)
        value = context[name]
        return escape(value) if escape else value

    return _PLACEHOLDER.sub(replace, template)


def clean(value: str) -> str:
    return _CONTROL.sub("", value)


def single_line(text: str, limit: int = SUBJECT_MAX) -> str:
    """Collapse whitespace and cap the length. A subject must be one line, and a
    newline inside a header is how mail header injection works."""
    collapsed = " ".join(clean(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def compose_message(
    event: Notification, subject_template: str, body_template: str
) -> Message:
    """Render both templates against the event and let it build its message."""
    context = {name: clean(value) for name, value in event.context().items()}
    subject = single_line(render(subject_template, context))
    if not subject:
        # A template made only of placeholders that came out empty.
        subject = single_line(render(CATALOG[event.kind].default_subject, context))
    return event.compose(
        subject=subject,
        body=render(body_template, context).strip(),
        slack_body=render(body_template, context, escape=escape_mrkdwn).strip(),
    )
