"""Alembic environment. Uses a synchronous psycopg connection built from app settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from opspilot_api.config import get_settings
from opspilot_api.incidents.models import Base
from opspilot_api.investigations import models as investigation_models  # noqa: F401
from opspilot_api.knowledge import models as knowledge_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
