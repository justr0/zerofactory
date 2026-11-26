"""Playwright browser E2E tests for the Zero Factory React dashboard UI."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dashboard" / "dist"
REACT_UMD = REPO_ROOT / "node_modules" / "react" / "umd" / "react.production.min.js"
REACT_DOM_UMD = (
    REPO_ROOT / "node_modules" / "react-dom" / "umd" / "react-dom.production.min.js"
)


def _find_chromium_executable() -> str | None:
    for candidate in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _start_http_bridge(
    app: FastAPI,
) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    client = TestClient(app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length")
            }
            resp = client.request(
                self.command, self.path, content=body, headers=headers
            )
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in (
                    "content-length",
                    "transfer-encoding",
                    "content-encoding",
                ):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def do_PUT(self) -> None:
            self._forward()

        def do_PATCH(self) -> None:
            self._forward()

        def do_DELETE(self) -> None:
            self._forward()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, port


def _build_test_app() -> FastAPI:
    from dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/zerofactory")

    @app.get("/assets/react.js")
    def _react():
        return FileResponse(REACT_UMD, media_type="application/javascript")

    @app.get("/assets/react-dom.js")
    def _react_dom():
        return FileResponse(REACT_DOM_UMD, media_type="application/javascript")

    @app.get("/dist/index.js")
    def _bundle():
        return FileResponse(DIST_DIR / "index.js", media_type="application/javascript")

    @app.get("/dist/style.css")
    def _css():
        return FileResponse(DIST_DIR / "style.css", media_type="text/css")

    @app.get("/", response_class=HTMLResponse)
    def _index():
        return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Zero Factory UI E2E</title>
  <link rel="stylesheet" href="/dist/style.css" />
</head>
<body class="bg-slate-950 text-slate-100">
  <div id="root"></div>
  <script src="/assets/react.js"></script>
  <script src="/assets/react-dom.js"></script>
  <script>
    window.__HERMES_PLUGIN_SDK__ = {
      React: window.React,
      hooks: window.React,
      fetchJSON: async function(url, opts) {
        const res = await fetch(url, opts || {});
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      },
      utils: {}
    };
    window.__HERMES_PLUGINS__ = {
      _reg: {},
      register: function(name, Comp) { this._reg[name] = Comp; }
    };
  </script>
  <script src="/dist/index.js"></script>
  <script>
    const Comp = window.__HERMES_PLUGINS__._reg["zerofactory"];
    const root = window.ReactDOM.createRoot(document.getElementById("root"));
    root.render(window.React.createElement(Comp));
  </script>
</body>
</html>"""

    return app


def test_board_creation_git_slug_fallbacks_playwright(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not (REACT_UMD.is_file() and REACT_DOM_UMD.is_file()):
        pytest.skip("React UMD assets not found in node_modules")
    if not (DIST_DIR / "index.js").is_file():
        pytest.skip("dashboard/dist/index.js not built")

    chromium_bin = _find_chromium_executable()
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(tmp_path / "zf_board_ui.db"))
    monkeypatch.setenv("ZEROFACTORY_DISABLE_DISPATCHER", "1")

    import profile_manager
    from dashboard.db import init_db

    monkeypatch.setattr(
        profile_manager, "check_gh_auth_status", lambda use_cache=True: True
    )
    init_db(force=True)

    submitted_payloads: list[dict] = []
    boards: list[dict] = []

    def handle_boards(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json
            submitted_payloads.append(payload)
            boards.append(
                {
                    **payload,
                    "task_count": 0,
                    "running_count": 0,
                    "repositories": payload["repositories"],
                }
            )
            response = {"slug": payload["slug"]}
        else:
            response = {"boards": boards}
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(response),
        )

    httpd, thread, port = _start_http_bridge(_build_test_app())
    try:
        with sync_api.sync_playwright() as p:
            launch_kwargs: dict[str, object] = {"headless": True}
            if chromium_bin:
                launch_kwargs["executable_path"] = chromium_bin
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            page.route("**/api/plugins/zerofactory/boards", handle_boards)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_placeholder(
                "git@github.com:org/repo.git or https://github.com/org/repo.git"
            ).wait_for(timeout=10000)

            cases = [
                (
                    "https://github.com/Acme/FirstRepo.git",
                    "acme-firstrepo",
                    "",
                    "",
                    "acme-firstrepo",
                    "acme-firstrepo",
                ),
                (
                    "git@github.com:Acme/SecondRepo.git",
                    "acme-secondrepo",
                    "",
                    "",
                    "acme-secondrepo",
                    "acme-secondrepo",
                ),
                (
                    "https://github.com/Acme/ThirdRepo.git",
                    "acme-thirdrepo",
                    "chosen-board",
                    "chosen-alias",
                    "chosen-board",
                    "chosen-alias",
                ),
                ("://", "", "", "fallback-alias", "fallback-alias", "fallback-alias"),
            ]

            for index, (
                git_url,
                suggested_slug,
                explicit_slug,
                explicit_alias,
                expected_slug,
                expected_alias,
            ) in enumerate(cases):
                if index:
                    page.locator('button[aria-haspopup="listbox"]').click()
                    page.get_by_role("button", name="Create New Board").click()

                form = page.locator("form")
                # Exercise blank-alias fallback despite the UI's required field.
                form.evaluate("form => { form.noValidate = true; }")
                page.get_by_placeholder(
                    "git@github.com:org/repo.git or https://github.com/org/repo.git"
                ).fill(git_url)

                slug_field = form.locator("input").first
                if suggested_slug:
                    slug_input = page.locator(f'input[placeholder="{suggested_slug}"]')
                    slug_input.fill(explicit_slug)
                page.get_by_placeholder("e.g. core-api").fill(explicit_alias)
                assert slug_field.input_value() == explicit_slug
                assert (
                    page.get_by_placeholder("e.g. core-api").input_value()
                    == explicit_alias
                )

                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith("/api/plugins/zerofactory/boards")
                    )
                ):
                    page.locator('form button[type="submit"]').click()
                form.wait_for(state="detached", timeout=10000)

                payload = submitted_payloads[index]
                assert payload["slug"] == expected_slug
                assert payload["repositories"][0]["repo_alias"] == expected_alias

            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_task_detail_metadata_formats_and_fallbacks_playwright(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not (REACT_UMD.is_file() and REACT_DOM_UMD.is_file()):
        pytest.skip("React UMD assets not found in node_modules")
    if not (DIST_DIR / "index.js").is_file():
        pytest.skip("dashboard/dist/index.js not built")

    chromium_bin = _find_chromium_executable()
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    db_path = tmp_path / "zf_task_metadata_ui.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(db_path))
    monkeypatch.setenv("ZEROFACTORY_DISABLE_DISPATCHER", "1")

    import profile_manager
    from dashboard.db import init_db

    monkeypatch.setattr(
        profile_manager, "check_gh_auth_status", lambda use_cache=True: True
    )
    init_db(force=True)

    task_id = "zf-meta-test"
    issue_metadata = {
        "external_issue": {"labels": ["automerge"]},
        "zf_bug_report": {
            "title": "Tracked metadata defect",
            "issue_url": "https://github.com/example/zerofactory/issues/1",
        },
    }
    cases = [
        ("object metadata", "demo-off", issue_metadata, True, True),
        ("JSON-string metadata", "demo-off", json.dumps(issue_metadata), True, True),
        (
            "manual-review opt-out",
            "demo-on",
            {
                "external_issue": {"labels": ["manual-review"]},
                "zf_bug_report": issue_metadata["zf_bug_report"],
            },
            False,
            True,
        ),
        (
            "HITL opt-out",
            "demo-on",
            {
                "external_issue": {"labels": ["hitl"]},
                "zf_bug_report": issue_metadata["zf_bug_report"],
            },
            False,
            True,
        ),
        ("malformed JSON metadata", "demo-on", "{not valid JSON", True, False),
    ]

    now = int(time.time())
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO boards (slug, automerge, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [("demo-off", 0, now, now), ("demo-on", 1, now, now)],
        )
        conn.executemany(
            "INSERT INTO board_repositories (board_slug, repo_alias, git_url, created_at, updated_at) "
            "VALUES (?, 'main', 'https://example.com/demo.git', ?, ?)",
            [("demo-off", now, now), ("demo-on", now, now)],
        )
        conn.executemany(
            "INSERT INTO tasks (id, board_slug, repo_alias, title, status, assignee, "
            "priority, tags, metadata, created_at, updated_at) "
            "VALUES (?, ?, 'main', ?, 'todo', 'zf-builder', 'P3', '[]', '{}', ?, ?)",
            [
                (f"{task_id}-{index}", board_slug, case_name, now, now)
                for index, (case_name, board_slug, *_rest) in enumerate(cases)
            ],
        )
        conn.commit()

    httpd, thread, port = _start_http_bridge(_build_test_app())
    try:
        with sync_api.sync_playwright() as p:
            launch_kwargs: dict[str, object] = {"headless": True}
            if chromium_bin:
                launch_kwargs["executable_path"] = chromium_bin
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))
            metadata_for_detail = issue_metadata

            def mock_task_detail(route):
                response = route.fetch()
                body = response.json()
                body["task"]["metadata"] = metadata_for_detail
                route.fulfill(response=response, body=json.dumps(body))

            page.route(f"**/tasks/{task_id}-*", mock_task_detail)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            for (
                case_name,
                _board_slug,
                metadata,
                has_badge,
                has_defect_banner,
            ) in cases:
                metadata_for_detail = metadata
                task_card = page.locator(f'[role="button"][aria-label$="{case_name}"]')
                task_card.wait_for(timeout=10000)
                task_card.click()
                dialog = page.get_by_role("dialog")
                dialog.wait_for(timeout=10000)
                badge = dialog.get_by_title("Auto-Merge Active")
                banner = dialog.get_by_text("Zero Factory Defect Tracked on GitHub")
                assert badge.count() == int(has_badge), case_name
                assert banner.count() == int(has_defect_banner), (
                    case_name,
                    dialog.inner_text(),
                )
                if has_defect_banner:
                    assert (
                        dialog.get_by_text(
                            "Tracked metadata defect", exact=True
                        ).count()
                        == 1
                    ), case_name
                page.keyboard.press("Escape")
                dialog.wait_for(state="detached")

            assert page_errors == [], f"Uncaught page errors: {page_errors}"
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_dashboard_setup_banner_new_task_and_settings_playwright(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not (REACT_UMD.is_file() and REACT_DOM_UMD.is_file()):
        pytest.skip("React UMD assets not found in node_modules")
    if not (DIST_DIR / "index.js").is_file():
        pytest.skip("dashboard/dist/index.js not built")

    chromium_bin = _find_chromium_executable()
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    db_path = tmp_path / "zf_ui.db"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(db_path))
    monkeypatch.setenv("ZEROFACTORY_DISABLE_DISPATCHER", "1")

    import profile_manager
    from dashboard.db import init_db

    monkeypatch.setattr(
        profile_manager, "check_gh_auth_status", lambda use_cache=True: True
    )
    init_db(force=True)

    now = int(time.time())
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO boards (slug, created_at, updated_at) VALUES ('demo', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO board_repositories (board_slug, repo_alias, git_url, created_at, updated_at) "
            "VALUES ('demo', 'main', 'https://example.com/demo.git', ?, ?)",
            (now, now),
        )
        conn.commit()

    httpd, thread, port = _start_http_bridge(_build_test_app())
    try:
        with sync_api.sync_playwright() as p:
            launch_kwargs: dict[str, object] = {"headless": True}
            if chromium_bin:
                launch_kwargs["executable_path"] = chromium_bin
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

            # 1. Setup Wizard banner renders CLI setup & start commands
            page.get_by_text("hermes zerofactory setup").wait_for(timeout=10000)
            page.get_by_text("hermes zerofactory start").wait_for(timeout=10000)

            # 2. Provision zf-* profiles and reload -> start command remains, setup completed
            profile_manager.ensure_zf_profiles()
            page.reload(wait_until="networkidle")
            page.get_by_text("hermes zerofactory start").wait_for(timeout=10000)

            # 3. Create a task via the + New Task modal
            page.get_by_role("button", name="+ New Task").click()
            page.get_by_placeholder("e.g. Implement caching layer for Redis").fill(
                "Playwright UI Task"
            )
            page.get_by_role("button", name="Create Task").click()
            page.get_by_text("Playwright UI Task").wait_for(timeout=10000)

            pr_url = "https://example.com/pull/1"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    "UPDATE tasks SET pr_url = ? WHERE title = ?",
                    (pr_url, "Playwright UI Task"),
                )
            popup_requests: list[str] = []

            def mock_pr_popup(route):
                popup_requests.append(route.request.url)
                route.fulfill(body="Pull request")

            page.context.route(pr_url, mock_pr_popup)
            page.reload(wait_until="networkidle")

            # Card keyboard activation opens details, but child controls retain
            # their native keyboard activation without opening the card.
            task_card = page.locator(
                '[role="button"][aria-label$="Playwright UI Task"]'
            )
            task_card.wait_for(timeout=10000)
            task_id = (
                task_card.get_attribute("aria-label")
                .split(":", 1)[0]
                .replace("Open task ", "", 1)
            )
            for key in ("Enter", "Space"):
                with page.expect_request(
                    lambda request: (
                        request.method == "GET"
                        and request.url.endswith(f"/tasks/{task_id}")
                    )
                ):
                    task_card.focus()
                    page.keyboard.press(key)
                page.get_by_role("dialog").wait_for(timeout=10000)
                page.keyboard.press("Escape")
                page.get_by_role("dialog").wait_for(state="detached")

            task_detail_requests: list[str] = []

            def record_task_detail_request(request):
                if request.method == "GET" and request.url.endswith(
                    f"/tasks/{task_id}"
                ):
                    task_detail_requests.append(request.url)

            page.on("request", record_task_detail_request)
            pr_link = task_card.get_by_role("link")
            with page.expect_popup() as popup_info:
                pr_link.focus()
                page.keyboard.press("Enter")
            popup = popup_info.value
            popup.wait_for_load_state("load")
            assert popup.url == pr_url
            assert popup_requests == [pr_url]
            assert task_detail_requests == []
            assert page.get_by_role("dialog").count() == 0
            popup.close()

            triage_requests: list[dict] = []

            def mock_triage(route):
                triage_requests.append(route.request.post_data_json)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"ok": true}',
                )

            page.route(f"**/tasks/{task_id}/triage", mock_triage)
            grill_button = task_card.get_by_role("button", name="Grill Me")
            for key in ("Enter", "Space"):
                detail_request_count = len(task_detail_requests)
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith(f"/tasks/{task_id}/triage")
                    )
                ):
                    grill_button.focus()
                    page.keyboard.press(key)
                expected_requests = 1 if key == "Enter" else 2
                assert triage_requests == [{"mode": "grill"}] * expected_requests
                assert len(task_detail_requests) == detail_request_count
            assert page.get_by_role("dialog").count() == 0

            # 4. Toggle lane info ("i") panel open and closed
            info_btn = page.get_by_role("button", name="Toggle Human lane information")
            info_btn.click()
            lane_info = page.get_by_role("region", name="Human lane information")
            lane_info.wait_for(timeout=10000)
            assert "All Human Gates (0 Tokens)" in (lane_info.text_content() or "")
            info_btn.click()
            lane_info.wait_for(state="hidden", timeout=10000)

            # 5. Open Settings modal and verify it renders without errors
            page.get_by_role("button", name="⚙️ Settings").click()
            page.get_by_text("Zero Factory Global Settings").wait_for(timeout=10000)

            assert page_errors == [], f"Uncaught page errors: {page_errors}"
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
