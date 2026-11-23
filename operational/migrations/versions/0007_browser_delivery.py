"""Admit synthetic browser deliveries with a session-bound importer role."""

from pathlib import Path

from alembic import op
from alembic.util import CommandError

revision = "0007_browser_delivery"
down_revision = "0006_retention"


def upgrade():
    op.get_bind().exec_driver_sql(
        Path(__file__).with_suffix(".sql").read_text(), execution_options={"no_parameters": True}
    )


def downgrade():
    raise CommandError("Restore independent objects and replay the current deletion ledger.")
