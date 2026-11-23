"""Initial operational schema. DDL and Alembic stamp commit atomically on PostgreSQL."""
from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0001_operational"
down_revision = None


def upgrade():
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade():
    raise CommandError("Forward-only revision. Restore the verified backup into a fresh database.")
