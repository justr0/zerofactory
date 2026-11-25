import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from dashboard.db import init_db
from dashboard.models import TaskCreate, TaskSplitRequest, TaskUpdate
from dashboard.routes.tasks import create_task, split_task, update_task


class TestTriageSplit(unittest.TestCase):
    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tf.name)
        os.environ["ZEROFACTORY_DB"] = str(self.db_path)
        os.environ["ZEROFACTORY_SKIP_DISPATCHER"] = "1"
        init_db(force=True)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO boards (slug, description, created_at, updated_at) VALUES ('test-board', 'Test Board', 1, 1)"
            )
            conn.execute(
                "INSERT INTO board_repositories (board_slug, repo_alias, git_url, created_at, updated_at) VALUES ('test-board', 'frontend', 'https://github.com/org/front', 1, 1)"
            )
            conn.execute(
                "INSERT INTO board_repositories (board_slug, repo_alias, git_url, created_at, updated_at) VALUES ('test-board', 'backend', 'https://github.com/org/back', 1, 1)"
            )
            conn.commit()

    def tearDown(self):
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception:
                pass

    def test_create_triage_task_with_target_repos(self):
        req = TaskCreate(
            title="[Triage] [Bug] Auth token mismatch across frontend and backend",
            description="Client sends JWT format not accepted by server",
            board_slug="test-board",
            status="triage",
            priority="P1",
            target_repos=["frontend", "backend"],
        )
        res = create_task(req)
        self.assertTrue(res["ok"])
        task_id = res["id"]

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self.assertEqual(row["status"], "triage")
            meta = json.loads(row["metadata"])
            self.assertEqual(meta.get("target_repos"), ["frontend", "backend"])

    def test_task_path_components_reject_absolute_and_parent_paths(self):
        invalid_values = ("/tmp/outside", "../outside")
        for value in invalid_values:
            with self.subTest(field="task_id", value=value):
                with self.assertRaises(ValidationError):
                    TaskCreate(title="Unsafe task id", task_id=value)
            with self.subTest(field="repo_alias", value=value):
                with self.assertRaises(ValidationError):
                    TaskCreate(title="Unsafe repo alias", repo_alias=value)
                with self.assertRaises(ValidationError):
                    TaskUpdate(repo_alias=value)
            with self.subTest(field="target_repos", value=value):
                with self.assertRaises(ValidationError):
                    TaskCreate(title="Unsafe target repo", target_repos=[value])
                with self.assertRaises(ValidationError):
                    TaskUpdate(target_repos=[value])

    def test_create_and_update_accept_only_linked_repository_aliases(self):
        created = create_task(
            TaskCreate(
                title="Use valid linked alias",
                board_slug="test-board",
                repo_alias="frontend",
                target_repos=["frontend", "backend"],
            )
        )
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT repo_alias FROM tasks WHERE id = ?", (created["id"],)
            ).fetchone()
        self.assertEqual(row[0], "frontend")

        for request in (
            TaskCreate(
                title="Unknown alias",
                board_slug="test-board",
                repo_alias="unlinked",
            ),
            TaskCreate(
                title="Unknown target repo",
                board_slug="test-board",
                target_repos=["frontend", "unlinked"],
            ),
        ):
            with self.subTest(request=request):
                with self.assertRaises(HTTPException) as exc:
                    create_task(request)
                self.assertEqual(exc.exception.status_code, 400)

        for request in (
            TaskUpdate(repo_alias="unlinked"),
            TaskUpdate(target_repos=["unlinked"]),
        ):
            with self.subTest(request=request):
                with self.assertRaises(HTTPException) as exc:
                    update_task(created["id"], request)
                self.assertEqual(exc.exception.status_code, 400)

    def test_create_task_rejects_unknown_explicit_board(self):
        req = TaskCreate(title="Task for unknown board", board_slug="typo-board")

        with self.assertRaises(HTTPException) as exc:
            create_task(req)

        self.assertEqual(exc.exception.status_code, 400)
        with sqlite3.connect(str(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )

    def test_create_task_without_board_or_with_all_uses_first_board(self):
        for board_slug in (None, "all"):
            with self.subTest(board_slug=board_slug):
                req = TaskCreate(
                    title=f"Task using default board {board_slug}",
                    board_slug=board_slug,
                )

                res = create_task(req)

                with sqlite3.connect(str(self.db_path)) as conn:
                    row = conn.execute(
                        "SELECT board_slug FROM tasks WHERE id = ?", (res["id"],)
                    ).fetchone()
                self.assertEqual(row[0], "test-board")

    def test_idle_scanner_can_create_only_remaining_todo_capacity(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('idle_scan_max_todo_test-board', '2', 1)"
            )
            conn.commit()
        create_task(
            TaskCreate(
                title="Existing queued task", board_slug="test-board", status="todo"
            )
        )

        with patch.dict(
            os.environ,
            {
                "ZEROFACTORY_IDLE_SCAN": "1",
                "ZEROFACTORY_IDLE_SCAN_BOARD": "test-board",
            },
        ):
            created = create_task(
                TaskCreate(
                    title="Scanner queued task", board_slug="test-board", status="todo"
                )
            )
            self.assertTrue(created["ok"])
            with self.assertRaises(HTTPException) as exc:
                create_task(
                    TaskCreate(
                        title="Scanner over-cap task",
                        board_slug="test-board",
                        status="todo",
                    )
                )

        self.assertEqual(exc.exception.status_code, 409)
        with sqlite3.connect(str(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE board_slug = 'test-board' AND status = 'todo'"
                ).fetchone()[0],
                2,
            )

    def test_idle_scanner_zero_cap_and_manual_creation(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('idle_scan_max_todo_test-board', '0', 1)"
            )
            conn.commit()

        with patch.dict(
            os.environ,
            {
                "ZEROFACTORY_IDLE_SCAN": "1",
                "ZEROFACTORY_IDLE_SCAN_BOARD": "test-board",
            },
        ):
            with self.assertRaises(HTTPException) as exc:
                create_task(
                    TaskCreate(
                        title="Scanner task at zero cap",
                        board_slug="test-board",
                        status="todo",
                    )
                )
        self.assertEqual(exc.exception.status_code, 409)

        # Ordinary/manual task creation is not subject to scanner backlog policy.
        with patch.dict(
            os.environ,
            {"ZEROFACTORY_IDLE_SCAN": "0", "ZEROFACTORY_IDLE_SCAN_BOARD": "test-board"},
        ):
            manual = create_task(
                TaskCreate(
                    title="Manual queued task", board_slug="test-board", status="todo"
                )
            )
        self.assertTrue(manual["ok"])

    def test_concurrent_idle_scanner_creations_share_capacity(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('idle_scan_max_todo_test-board', '1', 1)"
            )
            conn.commit()

        def create_scanner_task(index):
            try:
                return create_task(
                    TaskCreate(
                        title=f"Concurrent scanner task {index}",
                        board_slug="test-board",
                        status="todo",
                    )
                )
            except HTTPException as exc:
                return exc.status_code

        with patch.dict(
            os.environ,
            {
                "ZEROFACTORY_IDLE_SCAN": "1",
                "ZEROFACTORY_IDLE_SCAN_BOARD": "test-board",
            },
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(create_scanner_task, (1, 2)))

        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(result == 409 for result in results), 1)
        with sqlite3.connect(str(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE board_slug = 'test-board' AND status = 'todo'"
                ).fetchone()[0],
                1,
            )

    def test_split_triage_task_into_todo_tasks(self):
        # 1. Create parent triage task
        req = TaskCreate(
            title="[Triage] [Bug] #42 WebRTC handshake failing between client and server",
            description="Signaling protocol payload format changed",
            board_slug="test-board",
            status="triage",
            priority="P0",
            target_repos=["frontend", "backend"],
        )
        parent_res = create_task(req)
        parent_id = parent_res["id"]

        # 2. Split task
        split_res = split_task(parent_id)
        self.assertTrue(split_res["ok"])
        self.assertEqual(split_res["parent_task_id"], parent_id)
        created = split_res["created_tasks"]
        self.assertEqual(len(created), 2)

        front_task = next(c for c in created if c["repo_alias"] == "frontend")
        back_task = next(c for c in created if c["repo_alias"] == "backend")

        self.assertEqual(front_task["status"], "todo")
        self.assertEqual(front_task["assignee"], "zf-builder")
        self.assertIn("frontend", front_task["title"])

        self.assertEqual(back_task["status"], "todo")
        self.assertEqual(back_task["assignee"], "zf-builder")
        self.assertIn("backend", back_task["title"])

        # 3. Verify parent transitioned to done with metadata and comment
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            p_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (parent_id,)
            ).fetchone()
            self.assertEqual(p_row["status"], "done")
            self.assertEqual(p_row["assignee"], "zf-orchestrator")
            p_meta = json.loads(p_row["metadata"])
            self.assertEqual(len(p_meta.get("decomposed_into", [])), 2)

            # Check task_links
            links = conn.execute(
                "SELECT * FROM task_links WHERE parent_id = ? ORDER BY child_id ASC",
                (parent_id,),
            ).fetchall()
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0]["link_type"], "relates_to")

            # Check comments recorded
            cmts = conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ?", (parent_id,)
            ).fetchall()
            self.assertGreaterEqual(len(cmts), 1)
            self.assertIn("Triage Decomposition Complete", cmts[0]["body"])

    def test_split_explicit_repos(self):
        req = TaskCreate(
            title="General bug in triage",
            board_slug="test-board",
            status="triage",
        )
        res = create_task(req)

        # Split with explicit repos
        split_res = split_task(
            res["id"], TaskSplitRequest(repos=["backend", "frontend"])
        )
        self.assertTrue(split_res["ok"])
        self.assertEqual(len(split_res["created_tasks"]), 2)


if __name__ == "__main__":
    unittest.main()
