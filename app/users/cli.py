"""Local administrator commands; only key issuance prints a raw credential."""

import argparse
import asyncio
import sys
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import create_database
from app.core.settings import Settings
from app.users.repository import UserRepository
from app.users.service import AuthService


def nonblank_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 200:
        raise argparse.ArgumentTypeError("Name must contain 1 to 200 characters")
    return name


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    organization = commands.add_parser("create-organization")
    organization.add_argument("--name", type=nonblank_name, required=True)
    user = commands.add_parser("create-user")
    user.add_argument("--organization", type=UUID, required=True)
    user.add_argument("--name", type=nonblank_name, required=True)
    issue = commands.add_parser("issue-key")
    issue.add_argument("--user", type=UUID, required=True)
    revoke = commands.add_parser("revoke-key")
    revoke.add_argument("--key-id", type=UUID, required=True)
    return result


async def execute(args: argparse.Namespace) -> str:
    settings = Settings()
    if settings.database_url is None:
        raise ValueError("Database configuration is required")
    engine = create_database(settings.database_url.get_secret_value())
    try:
        repository = UserRepository(async_sessionmaker(engine, expire_on_commit=False))
        auth = AuthService(repository)
        match args.command:
            case "create-organization":
                return str(await repository.create_organization(args.name))
            case "create-user":
                return str(await repository.create_user(args.organization, args.name))
            case "issue-key":
                return await auth.issue_key(args.user)
            case "revoke-key":
                await auth.revoke_key(args.key_id)
                return "revoked"
            case _:
                raise ValueError("Unknown command")
    finally:
        await engine.dispose()


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(
            execute(args),
            loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
        )
    except SQLAlchemyError, ValidationError, ValueError:
        print("명령을 처리하지 못했습니다. 설정과 대상 ID를 확인해 주세요.", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
