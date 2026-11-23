"""Operator-supplied libpq environment or service file; no URL in the repository."""
from alembic import context
from alembic.util import CommandError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

engine = create_engine("postgresql+psycopg://", hide_parameters=True)
try:
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
except SQLAlchemyError as error:
    code = getattr(getattr(error, "orig", None), "sqlstate", None) or type(error).__name__
    raise CommandError(f"Operational migration failed ({code}). Verify target, privileges and revision.") from None
finally:
    engine.dispose()
