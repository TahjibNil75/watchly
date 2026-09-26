"""Reinstate a suspended account from the server, bypassing the API's rules.

Only a user whose role may reinstate an account can lift its suspension
through the API — for an admin, only another admin. When there is no such user
(an admin suspended before wrong passwords stopped suspending accounts, say),
this is the way back in. It also lifts a sign-in lockout from too many wrong
passwords without waiting for it to run out. Anyone who can run it already
holds the database.

Run it directly:  python -m app.db.reactivate <username or email>
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy import or_, select

from app.db.models.user import User
from app.db.session import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)


async def reactivate(identifier: str) -> bool:
    """Reinstate the user with this username or email. False if there is none."""
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(
                or_(User.username == identifier, User.email == identifier)
            )
        )
        if user is None:
            logger.error("No user with username or email %r.", identifier)
            return False
        user.is_active = True
        user.failed_login_attempts = 0
        user.locked_until = None
        await session.commit()
        logger.info("Reactivated user %r.", user.username)
        return True


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("identifier", help="username or email of the account")
    args = parser.parse_args()
    try:
        return 0 if await reactivate(args.identifier) else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
