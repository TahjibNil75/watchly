"""Seed the database with the bootstrap admin account.

Run it directly:  python -m app.db.seed
"""

import asyncio
import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import generate_hash_password
from app.db.models.user import User, UserRole
from app.db.session import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)


async def seed_admin(session: AsyncSession) -> User:
    """Create the admin user if it is not there yet. Safe to run repeatedly."""
    existing = await session.scalar(
        select(User).where(
            or_(
                User.username == settings.FIRST_ADMIN_USERNAME,
                User.email == settings.FIRST_ADMIN_EMAIL,
            )
        )
    )
    if existing is not None:
        logger.info("Admin user %r already exists, skipping seed.", existing.username)
        return existing

    admin = User(
        username=settings.FIRST_ADMIN_USERNAME,
        email=settings.FIRST_ADMIN_EMAIL,
        full_name=settings.FIRST_ADMIN_FULL_NAME,
        password_hash=generate_hash_password(settings.FIRST_ADMIN_PASSWORD),
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(admin)
    await session.commit()
    await session.refresh(admin)
    logger.info("Created admin user %r.", admin.username)
    return admin


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        async with AsyncSessionLocal() as session:
            await seed_admin(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
