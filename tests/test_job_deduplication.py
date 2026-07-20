import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.browser_errors import JobAlreadyRunningError
from app.db_models import JobType
from app.services import create_job


class JobDeduplicationTests(unittest.TestCase):
    def test_active_job_returns_structured_conflict_under_workspace_lock(self) -> None:
        workspace_id = uuid.uuid4()
        existing_job_id = uuid.uuid4()
        db = Mock()
        db.scalar.side_effect = [workspace_id, SimpleNamespace(id=existing_job_id)]

        with self.assertRaises(JobAlreadyRunningError) as raised:
            create_job(
                db,
                workspace_id=workspace_id,
                user_id=uuid.uuid4(),
                job_type=JobType.CONTACT_SYNC,
                payload={},
            )

        lock_statement = db.scalar.call_args_list[0].args[0]
        compiled = str(lock_statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", compiled)
        self.assertEqual(raised.exception.error_code, "JOB_ALREADY_RUNNING")
        self.assertEqual(raised.exception.response_payload()["existing_job_id"], str(existing_job_id))


if __name__ == "__main__":
    unittest.main()
