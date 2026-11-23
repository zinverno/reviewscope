"""Interactive, operator-only account provisioning using the migration connection."""

import argparse
import getpass
import sys
import warnings
from uuid import UUID

import psycopg

from .security import credential, normalize, server_key


def provision(conn, login, password, key, *, workspace=None, legacy_id=None, role="owner"):
    salt, verifier = credential(password, key)
    with conn.transaction():
        if legacy_id is not None:
            if (
                conn.execute(
                    "SELECT FROM workspace_membership WHERE principal_id=%s LIMIT 1", (legacy_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("legacy membership required")
        (principal,) = conn.execute(
            """INSERT INTO principal(id,login,state)
            VALUES (coalesce(%s,gen_random_uuid()),%s,'active') RETURNING id""",
            (legacy_id, normalize(login)),
        ).fetchone()
        conn.execute(
            "INSERT INTO password_credential VALUES (%s,%s,%s)", (principal, salt, verifier)
        )
        if workspace is not None:
            conn.execute(
                "INSERT INTO workspace_membership VALUES (%s,%s,%s,'active')",
                (workspace, principal, role),
            )
    return principal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace", type=UUID)
    target.add_argument("--verified-legacy-id", type=UUID)
    parser.add_argument("--role", choices=("owner", "analyst", "viewer"))
    args = parser.parse_args()
    if (args.workspace is None) != (args.role is None):
        parser.error("Specify --role only and always with --workspace.")
    if not sys.stdin.isatty():
        parser.exit(1, "A private interactive terminal is required.\n")
    warnings.simplefilter("error", getpass.GetPassWarning)
    try:
        key = server_key()
        login = input("Verified account name: ")
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            raise ValueError
        with psycopg.connect("") as conn:
            provision(
                conn,
                login,
                password,
                key,
                workspace=args.workspace,
                legacy_id=args.verified_legacy_id,
                role=args.role,
            )
    except (OSError, KeyError, ValueError, EOFError, getpass.GetPassWarning, psycopg.Error):
        parser.exit(1, "Provisioning declined. Check input, mapping, privileges and revision.\n")
    print("Account provisioned.")


if __name__ == "__main__":
    main()
