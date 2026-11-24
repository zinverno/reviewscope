"""SCRAM credentials for the throwaway cluster, kept out of service environments and logs."""

import os
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROLES = ("rs_migration", "rs_runtime", "rs_worker", "rs_upload", "rs_retention")


def prepare(root):
    os.umask(0o077)
    with (root / "harness.pass").open("x") as harness:
        for role in ("rs_test", *ROLES):
            password = secrets.token_urlsafe(32)
            line = f"*:*:*:{role}:{password}\n"
            with (root / (role + ".pass")).open("x") as service:
                service.write(line)
            harness.write(line)
            if role == "rs_test":
                with (root / "bootstrap.pass").open("x") as bootstrap:
                    bootstrap.write(password + "\n")


def create_roles(root):
    with psycopg.connect("") as admin:
        assert Path(admin.execute("SHOW data_directory").fetchone()[0]) == root / "data"
        assert admin.execute("SELECT current_user").fetchone() == ("rs_test",)
        for role in ROLES:
            password = (root / (role + ".pass")).read_text().strip().rsplit(":", 1)[1]
            verifier = admin.pgconn.encrypt_password(
                password.encode(), role.encode(), b"scram-sha-256"
            )
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.SQL("CREATEDB" if role == "rs_migration" else "NOCREATEDB"),
                    sql.Literal(verifier.decode()),
                )
            )


if __name__ == "__main__":
    {"prepare": prepare, "create": create_roles}[sys.argv[1]](Path(os.environ["PGHOST"]))
