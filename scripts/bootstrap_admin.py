from __future__ import annotations

import argparse

from app.database import session_scope
from app.services import bootstrap_admin


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the first platform admin and workspace.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--workspace-slug")
    args = parser.parse_args()

    with session_scope() as db:
        result = bootstrap_admin(
            db,
            email=args.email,
            password=args.password,
            display_name=args.display_name,
            workspace_name=args.workspace_name,
            workspace_slug=args.workspace_slug,
        )

    print(
        f"Created admin {result.email} user_id={result.user_id} "
        f"workspace_id={result.workspace_id} workspace_slug={result.workspace_slug}"
    )


if __name__ == "__main__":
    main()
