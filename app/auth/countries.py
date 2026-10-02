"""The countries each user signs in from, and the email for a new one.

Each sign-in with a known country (see `app/core/geo.py`) bumps that user's
row for it. A country with no row yet, for a user who already has others, is
news: they get an email, in case it was not them. Their very first sign-in
only starts the list, since there is nothing to compare it with.
"""

from datetime import UTC, datetime

from sqlalchemy import exists, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mail
from app.core.config import settings
from app.db.models.login_country import LoginCountry
from app.db.models.user import User


async def record_sign_in(session: AsyncSession, user: User, country: str) -> bool:
    """Note that `user` just signed in from `country`. Returns True when that
    is a new country for a user who has signed in before. Commits."""
    now = datetime.now(UTC)
    had_others = await session.scalar(
        select(exists().where(LoginCountry.user_id == user.id))
    )
    statement = insert(LoginCountry).values(
        user_id=user.id, country=country, first_seen_at=now, last_seen_at=now, sign_ins=1
    )
    # `xmax` is 0 on a row this statement inserted, and not on one it updated.
    inserted = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[LoginCountry.user_id, LoginCountry.country],
            set_={
                "last_seen_at": now,
                "sign_ins": LoginCountry.sign_ins + 1,
            },
        ).returning(literal_column("xmax = 0"))
    )
    await session.commit()
    return bool(inserted and had_others)


async def countries_of(session: AsyncSession, user: User) -> list[LoginCountry]:
    """Where `user` has signed in from, most recent first."""
    result = await session.scalars(
        select(LoginCountry)
        .where(LoginCountry.user_id == user.id)
        .order_by(LoginCountry.last_seen_at.desc(), LoginCountry.country)
    )
    return list(result)


def new_country_email(user: User, country: str) -> mail.LinkEmail:
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    facts = [("Account", user.username), ("Country", country), ("When", when)]
    url = mail.app_url("/profile")
    intro = (
        f"Your {settings.PROJECT_NAME} account was just signed in to from {country}, "
        "a country it has not been used from before."
    )
    note = (
        "If this was you, nothing to do. If not, change your password now: that "
        "signs out every other session. The country is where the connection "
        "appears to come from, so a VPN can show a different one."
    )
    text = (
        f"{intro}\n\n"
        + "\n".join(f"{label}: {value}" for label, value in facts)
        + (f"\n\nChange your password: {url}" if url else "")
        + f"\n\n{note}\n"
    )
    return mail.LinkEmail(
        to=user.email,
        subject=f"New sign-in to {settings.PROJECT_NAME} from {country}",
        kicker="Security",
        title="Sign-in from a new country",
        intro=intro,
        facts=facts,
        button="Review your account",
        url=url,
        note=note,
        text=text,
    )


async def send_new_country_email(user: User, country: str) -> None:
    """Email `user` about a sign-in from `country`. Never raises."""
    await mail.send_link_email(
        new_country_email(user, country),
        f"new-country sign-in alert for user {user.id}",
        needs_link=False,
    )
