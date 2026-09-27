"""Reading and changing notification settings. No HTTP concerns.

Three levels, most specific first: a project's own override, the global
override an admin sets, and the built-in default. Each field resolves on its
own — a project can rewrite the subject and inherit everything else.
"""

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.monitoring.alerts.base import NotificationKind
from app.monitoring.notifications.catalog import CATALOG, KINDS, PROTECTED_KIND
from app.monitoring.notifications.models import NotificationSetting
from app.monitoring.notifications.templating import (
    BODY_MAX,
    SUBJECT_MAX,
    clean,
    unknown_placeholders,
)

FIELDS = ("email_enabled", "slack_enabled", "telegram_enabled", "subject", "body")
#: The per-channel switches among FIELDS.
SWITCHES = ("email_enabled", "slack_enabled", "telegram_enabled")


class NotificationSettingsError(Exception):
    """Base class for settings failures."""


class InvalidTemplateError(NotificationSettingsError):
    def __init__(self, field: str, unknown: list[str], allowed: list[str]) -> None:
        self.field = field
        self.unknown = unknown
        names = ", ".join("{{" + name + "}}" for name in unknown)
        valid = ", ".join("{{" + name + "}}" for name in allowed)
        super().__init__(f"The {field} uses unknown placeholder(s) {names}. Available: {valid}.")


class TemplateTooLongError(NotificationSettingsError):
    def __init__(self, field: str, limit: int) -> None:
        super().__init__(f"The {field} is too long; the limit is {limit} characters.")


class ProtectedNotificationError(NotificationSettingsError):
    def __init__(self) -> None:
        super().__init__(
            "The “Site down” alert cannot be switched off on every channel — "
            "nobody would hear about an outage. Turn off the whole project instead "
            "(mark it inactive), or leave one channel on."
        )


@dataclass(frozen=True, slots=True)
class EffectiveSetting:
    """What a kind does at one level, after inheritance."""

    kind: NotificationKind
    email_enabled: bool
    slack_enabled: bool
    telegram_enabled: bool
    subject: str
    body: str
    #: Per field: `project`, `global` or `default` — where the value came from.
    sources: dict[str, str]
    #: What this level itself stores; None means "inherits".
    overrides: dict[str, bool | str | None]
    #: The subject and body this level would fall back to if it stopped
    #: overriding them — what a blank field means.
    inherited_subject: str
    inherited_body: str


def default_setting(kind: NotificationKind) -> EffectiveSetting:
    """The built-in behaviour, used when no row exists (or the lookup fails)."""
    info = CATALOG[kind]
    return EffectiveSetting(
        kind=kind,
        email_enabled=True,
        slack_enabled=True,
        telegram_enabled=True,
        subject=info.default_subject,
        body=info.default_body,
        sources=dict.fromkeys(FIELDS, "default"),
        overrides=dict.fromkeys(FIELDS),
        inherited_subject=info.default_subject,
        inherited_body=info.default_body,
    )


def _merge(
    kind: NotificationKind,
    project_row: NotificationSetting | None,
    global_row: NotificationSetting | None,
    *,
    project_level: bool,
) -> EffectiveSetting:
    """Resolve one kind. `project_level` says which level is being viewed: it
    decides whose row counts as "this level's own overrides", and it cannot be
    inferred from `project_row` being None, since a project with no row of its
    own is still being viewed at project level."""
    base = default_setting(kind)
    values: dict[str, bool | str] = {}
    sources: dict[str, str] = {}
    for field in FIELDS:
        for scope, row in (("project", project_row), ("global", global_row)):
            value = getattr(row, field) if row is not None else None
            if value is not None:
                values[field], sources[field] = value, scope
                break
        else:
            values[field], sources[field] = getattr(base, field), "default"

    level_row = project_row if project_level else global_row
    overrides = {field: getattr(level_row, field) if level_row else None for field in FIELDS}
    # One level up from the one being viewed: the global row for a project,
    # nothing (so the built-in default) for the global level itself.
    upper_row = global_row if project_level else None
    inherited = {
        field: (getattr(upper_row, field) if upper_row is not None else None) or getattr(base, field)
        for field in ("subject", "body")
    }
    return EffectiveSetting(
        kind=kind,
        email_enabled=values["email_enabled"],
        slack_enabled=values["slack_enabled"],
        telegram_enabled=values["telegram_enabled"],
        subject=values["subject"],
        body=values["body"],
        sources=sources,
        overrides=overrides,
        inherited_subject=inherited["subject"],
        inherited_body=inherited["body"],
    )


class NotificationSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- reading -----------------------------------------------------------

    async def _rows(
        self, project_id: int | None, kind: NotificationKind | None = None
    ) -> tuple[dict[str, NotificationSetting], dict[str, NotificationSetting]]:
        """(project rows, global rows), each keyed by kind value."""
        scopes = [NotificationSetting.project_id.is_(None)]
        if project_id is not None:
            scopes.append(NotificationSetting.project_id == project_id)
        query = select(NotificationSetting).where(or_(*scopes))
        if kind is not None:
            query = query.where(NotificationSetting.kind == kind.value)

        project_rows: dict[str, NotificationSetting] = {}
        global_rows: dict[str, NotificationSetting] = {}
        for row in await self.session.scalars(query):
            (global_rows if row.project_id is None else project_rows)[row.kind] = row
        return project_rows, global_rows

    async def effective(
        self, kind: NotificationKind, project_id: int | None = None
    ) -> EffectiveSetting:
        """`project_id` None resolves the global level."""
        project_rows, global_rows = await self._rows(project_id, kind)
        return _merge(
            kind,
            project_rows.get(kind.value),
            global_rows.get(kind.value),
            project_level=project_id is not None,
        )

    async def effective_all(self, project_id: int | None = None) -> list[EffectiveSetting]:
        project_rows, global_rows = await self._rows(project_id)
        return [
            _merge(
                kind,
                project_rows.get(kind.value),
                global_rows.get(kind.value),
                project_level=project_id is not None,
            )
            for kind in KINDS
        ]

    # -- writing -----------------------------------------------------------

    @staticmethod
    def normalize_template(kind: NotificationKind, field: str, value: str | None) -> str | None:
        """Blank means "inherit". Unknown placeholders are refused, not saved
        and silently rendered as literal braces in somebody's inbox."""
        if value is None or not value.strip():
            return None
        limit = SUBJECT_MAX if field == "subject" else BODY_MAX
        # Collapse a subject's whitespace but do not truncate it here: a
        # subject that is too long is refused, not silently cut short.
        text = " ".join(clean(value).split()) if field == "subject" else clean(value).strip()
        if len(text) > limit:
            raise TemplateTooLongError(field, limit)
        allowed = list(CATALOG[kind].placeholders)
        unknown = unknown_placeholders(text, allowed)
        if unknown:
            raise InvalidTemplateError(field, unknown, allowed)
        return text

    async def save(
        self,
        kind: NotificationKind,
        *,
        project_id: int | None,
        email_enabled: bool | None,
        slack_enabled: bool | None,
        telegram_enabled: bool | None,
        subject: str | None,
        body: str | None,
        actor_id: int | None,
    ) -> EffectiveSetting:
        """Replace this level's overrides. A field left None inherits; all of
        them None removes the row, which is what "reset to default" means."""
        values = {
            "email_enabled": email_enabled,
            "slack_enabled": slack_enabled,
            "telegram_enabled": telegram_enabled,
            "subject": self.normalize_template(kind, "subject", subject),
            "body": self.normalize_template(kind, "body", body),
        }

        project_rows, global_rows = await self._rows(project_id, kind)
        row = (global_rows if project_id is None else project_rows).get(kind.value)

        if all(value is None for value in values.values()):
            if row is not None:
                await self.session.delete(row)
        else:
            if row is None:
                row = NotificationSetting(project_id=project_id, kind=kind.value)
                self.session.add(row)
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_by_id = actor_id

        await self.session.flush()
        if kind is PROTECTED_KIND:
            try:
                await self._assert_protected_kind_reachable()
            except ProtectedNotificationError:
                await self.session.rollback()
                raise
        await self.session.commit()
        return await self.effective(kind, project_id)

    async def reset(self, kind: NotificationKind, *, project_id: int | None) -> EffectiveSetting:
        return await self.save(
            kind,
            project_id=project_id,
            email_enabled=None,
            slack_enabled=None,
            telegram_enabled=None,
            subject=None,
            body=None,
            actor_id=None,
        )

    async def _assert_protected_kind_reachable(self) -> None:
        """No level may end up with the protected kind off on every channel.

        Checked across the global level and every project override, because a
        global change can silence a project that overrode only the other channel.
        """
        rows = list(
            await self.session.scalars(
                select(NotificationSetting).where(NotificationSetting.kind == PROTECTED_KIND.value)
            )
        )
        global_row = next((r for r in rows if r.project_id is None), None)

        def resolved(field: str, project_row: NotificationSetting | None) -> bool:
            for row in (project_row, global_row):
                value = getattr(row, field) if row is not None else None
                if value is not None:
                    return value
            return True

        for project_row in [None, *(r for r in rows if r.project_id is not None)]:
            if not any(resolved(switch, project_row) for switch in SWITCHES):
                raise ProtectedNotificationError
