from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from app.core import settings


engine = create_async_engine(
    url=settings.get_db_url,
    echo=True,
    max_overflow=10,
    pool_size=10,
)

session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)