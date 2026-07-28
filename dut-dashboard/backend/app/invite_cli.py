"""Mint an invite from the command line.

For the launcher and for recovering access: it writes straight to the database,
so it works before (or without) the backend being up, and needs no admin
session. Prints ONLY the raw token on stdout -- the caller composes the URL --
so it can be captured with `TOKEN=$(python3 -m app.invite_cli mint ...)`.

    python3 -m app.invite_cli mint --role engineer [--label L]
                                   [--expires-hours N] [--max-uses N]
"""

from __future__ import annotations

import argparse
import sys

from app.db.workspace import init_db
from app.services import invite_service
from app.services.auth_service import ROLES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invite_cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mint = sub.add_parser("mint", help="create an invite and print its token")
    mint.add_argument("--role", required=True, choices=list(ROLES))
    mint.add_argument("--label", default=None)
    mint.add_argument(
        "--expires-hours",
        type=float,
        default=invite_service.DEFAULT_EXPIRY_HOURS,
        help="hours until the invite expires; 0 means never",
    )
    mint.add_argument("--max-uses", type=int, default=1)

    args = parser.parse_args(argv)
    if args.command != "mint":  # pragma: no cover - argparse enforces this
        parser.error(f"unknown command: {args.command}")

    init_db()
    try:
        invite = invite_service.create_invite(
            role=args.role,
            label=args.label,
            created_by="launcher",
            expires_in_hours=args.expires_hours or None,
            max_uses=args.max_uses,
        )
    except ValueError as exc:
        print(f"invite_cli: {exc}", file=sys.stderr)
        return 2
    print(invite["token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
