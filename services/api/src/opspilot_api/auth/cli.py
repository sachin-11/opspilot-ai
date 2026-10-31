"""Operator CLI: ``python -m opspilot_api.auth.cli create-user``.

Needed because self-service signup is off by default. The password is read from the
``OPSPILOT_NEW_USER_PASSWORD`` environment variable or prompted for, never passed as an argument.
"""

import argparse
import asyncio
import getpass
import os
import sys
from uuid import UUID

from opspilot_api.auth.models import Role
from opspilot_api.auth.service import add_member, create_user
from opspilot_api.config import get_settings
from opspilot_api.infrastructure import create_db_engine, create_session_factory

MIN_PASSWORD_LENGTH = 12


async def run(email: str, name: str, password: str, workspace_id: UUID | None, role: Role) -> None:
    engine = create_db_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            user = await create_user(session, email, name, password)
            if workspace_id is not None:
                await add_member(session, workspace_id, user.id, role)
            print(f"Created user {user.email} ({user.id})")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="opspilot_api.auth.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create-user", help="create a user, optionally joining a workspace"
    )
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--workspace-id", type=UUID)
    create.add_argument("--role", choices=["viewer", "responder", "admin"], default="admin")
    args = parser.parse_args()
    password = os.environ.get("OPSPILOT_NEW_USER_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    asyncio.run(run(args.email, args.name, password, args.workspace_id, Role.parse(args.role)))


if __name__ == "__main__":
    main()
