from __future__ import annotations

import argparse
from pathlib import Path
import sys
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import session_scope
from app.services import import_legacy_workspace_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Import legacy SQLite/settings data into a workspace.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()

    with session_scope() as db:
        result = import_legacy_workspace_data(
            db,
            workspace_id=UUID(args.workspace_id),
            user_id=UUID(args.user_id),
        )

    print(
        f"Imported workspace={result.workspace_id} contacts={result.imported_contacts} "
        f"sync_runs={result.imported_sync_runs} campaigns={result.imported_campaigns} "
        f"settings={result.imported_settings}"
    )


if __name__ == "__main__":
    main()
