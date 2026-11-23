"""Introduce accounts first so an operator can resolve existing membership IDs."""

from alembic import op
from alembic.util import CommandError

revision = "0002_principals"
down_revision = "0001_operational"


def upgrade():
    op.execute("""
        CREATE TABLE principal (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            login text COLLATE "C" NOT NULL UNIQUE CHECK (login ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            state text NOT NULL CHECK (state IN ('active','inactive'))
        );
        CREATE TABLE password_credential (
            principal_id uuid PRIMARY KEY REFERENCES principal,
            salt bytea NOT NULL CHECK (length(salt)=16),
            verifier bytea NOT NULL CHECK (length(verifier)=32)
        );
    """)


def downgrade():
    raise CommandError("Forward only. Restore a verified pre-upgrade backup into a new database.")
