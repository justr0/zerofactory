"""Unit tests for profile_manager.py."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import profile_manager as PM


class TestProfileManagerUnit(unittest.TestCase):
    """Test profile setup, script deployment, symlinks, and env/config updates."""

    def test_ensure_zf_profiles(self):
        """Bootstraps the 3 Zero Factory profiles under fake home."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with patch("profile_manager.get_hermes_home", return_value=fake_home):
                res = PM.ensure_zf_profiles()
                self.assertIsInstance(res, dict)
                profiles_found = set(res["created"] + res["existing"] + res["updated"])
                for p in PM.ZF_PROFILES:
                    self.assertIn(p, profiles_found)
                    p_dir = fake_home / "profiles" / p
                    self.assertTrue(p_dir.exists())
                    self.assertTrue((p_dir / "SOUL.md").exists())
                    self.assertTrue((p_dir / "config.yaml").exists())

    def test_ensure_plugin_symlinks(self):
        """Creates symlink to zerofactory only in zf-* profiles (never root)."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            # A plain directory as the plugin source: the real one sits in a git
            # worktree when these tests run from one, which the guardrail refuses.
            plugin_root = fake_home / "plugin-src"
            plugin_root.mkdir()
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
            ):
                PM.ensure_zf_profiles()
                res = PM.ensure_plugin_symlinks()
                self.assertIsInstance(res, dict)
                self.assertFalse((fake_home / "plugins").exists())
                for role in PM.ZF_PROFILES:
                    link = fake_home / "profiles" / role / "plugins" / "zerofactory"
                    # A junction on Windows, a symlink elsewhere: either way it
                    # must lead back to the plugin's own root.
                    self.assertTrue(link.exists())
                    self.assertEqual(link.resolve(), plugin_root.resolve())

    def test_ensure_plugin_symlinks_relinks_existing_link(self):
        """A repeated setup keeps a correct link and replaces a foreign or broken one.

        On Windows the link is a junction, which ``Path.is_symlink()`` reports as
        False; it must be swapped with ``os.rmdir`` and never recursed into.
        """
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            plugin_root = fake_home / "plugin-src"
            plugin_root.mkdir()
            foreign = fake_home / "old-plugin"
            foreign.mkdir()
            (foreign / "keep.txt").write_text("old install\n", encoding="utf-8")
            gone = fake_home / "vanished-plugin"
            gone.mkdir()
            links = [
                fake_home / "profiles" / role / "plugins" / "zerofactory"
                for role in PM.ZF_PROFILES
            ]

            def link_all(source: Path) -> dict:
                with (
                    patch("profile_manager.get_hermes_home", return_value=fake_home),
                    patch("profile_manager.get_plugin_root", return_value=source),
                ):
                    PM.ensure_zf_profiles()
                    return PM.ensure_plugin_symlinks()

            link_all(foreign)
            self.assertTrue(all(link.resolve() == foreign.resolve() for link in links))
            # Relink away from the previous install: its files must survive.
            res = link_all(plugin_root)
            self.assertTrue(
                all(link.resolve() == plugin_root.resolve() for link in links)
            )
            self.assertTrue((foreign / "keep.txt").exists())
            self.assertEqual(sorted(res["linked"]), sorted(str(link) for link in links))
            created = [os.lstat(link).st_ctime_ns for link in links]
            # A correct link is left alone: nothing re-created, no warning.
            with self.assertNoLogs("zerofactory.profiles", level="WARNING"):
                link_all(plugin_root)
            self.assertEqual([os.lstat(link).st_ctime_ns for link in links], created)
            # A link whose target vanished is replaced as well.
            link_all(gone)
            shutil.rmtree(gone)
            link_all(plugin_root)
            self.assertTrue(
                all(link.resolve() == plugin_root.resolve() for link in links)
            )

    def test_ensure_plugin_symlinks_removes_directory_symlink_without_rmtree(self):
        """Windows directory symlinks must be unlinked without recursive deletion."""
        if os.name != "nt":
            self.skipTest("Windows directory-symlink attributes")

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            plugin_root = fake_home / "plugin-src"
            plugin_root.mkdir()
            link = (
                fake_home / "profiles" / PM.ZF_PROFILES[0] / "plugins" / "zerofactory"
            )
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
            ):
                PM.ensure_zf_profiles()
                os.rmdir(link)  # Remove the junction created by setup.
                link.mkdir()  # Simulate an existing directory symlink without privileges.

                original_is_symlink = Path.is_symlink
                original_is_junction = PM._is_junction

                def is_directory_symlink(path: Path) -> bool:
                    return path == link or original_is_symlink(path)

                def is_junction(path: Path) -> bool:
                    return False if path == link else original_is_junction(path)

                with (
                    patch.object(Path, "is_symlink", is_directory_symlink),
                    patch("profile_manager._is_junction", is_junction),
                    patch(
                        "profile_manager.shutil.rmtree", wraps=shutil.rmtree
                    ) as rmtree,
                ):
                    PM.ensure_plugin_symlinks()

                rmtree.assert_not_called()
                self.assertEqual(link.resolve(), plugin_root.resolve())

    def test_ensure_script_files(self):
        """Copies background scripts as real files only into zf-* profile scripts/."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with patch("profile_manager.get_hermes_home", return_value=fake_home):
                # Ensure profile directories exist first
                PM.ensure_zf_profiles()
                res = PM.ensure_script_files()
                self.assertIsInstance(res, dict)
                copied = res.get("copied", [])
                self.assertGreater(len(copied), 0)
                self.assertFalse((fake_home / "scripts").exists())

                expected_scripts = [
                    "zf_daily_stats.py",
                    "zf_openwiki_gate.py",
                ]
                for s in expected_scripts:
                    for role in PM.ZF_PROFILES:
                        prof_s = fake_home / "profiles" / role / "scripts" / s
                        self.assertTrue(prof_s.exists(), f"Missing {prof_s}")
                        self.assertFalse(prof_s.is_symlink())

    def test_update_env_file(self):
        """update_env_file preserves unrelated keys and comments."""
        with tempfile.TemporaryDirectory() as td:
            env_p = Path(td) / ".env"
            env_p.write_text(
                "# Custom comment\nOPENROUTER_API_KEY=existing-key\nOTHER_VAR=123\n",
                encoding="utf-8",
            )
            updates = {
                "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-sample",
                "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-sample",
            }
            PM.update_env_file(env_p, updates)
            lines = env_p.read_text(encoding="utf-8").splitlines()
            self.assertIn("# Custom comment", lines)
            self.assertIn("OPENROUTER_API_KEY=existing-key", lines)
            self.assertIn("OTHER_VAR=123", lines)
            self.assertIn("HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-sample", lines)
            self.assertIn("HERMES_LANGFUSE_SECRET_KEY=sk-lf-sample", lines)

            # Update in-place
            PM.update_env_file(env_p, {"HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-modified"})
            lines2 = env_p.read_text(encoding="utf-8").splitlines()
            self.assertIn("HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-modified", lines2)
            self.assertNotIn("HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-sample", lines2)
            self.assertIn("OPENROUTER_API_KEY=existing-key", lines2)

    def test_update_env_file_is_noop_when_unchanged(self):
        """Re-applying the same desired state must not rewrite the file.

        The gateway rescans a served profile whenever its .env changes, which tears
        down that profile's running workers; a sync that rewrites identical content on
        every provisioning must therefore be a no-op (returns False, mtime untouched)."""
        with tempfile.TemporaryDirectory() as td:
            env_p = Path(td) / ".env"
            updates = {"HERMES_LANGFUSE_ENV": "zerofactory"}

            self.assertTrue(PM.update_env_file(env_p, updates), "first apply writes")
            changed = {"HERMES_LANGFUSE_ENV": "e2e-test"}
            self.assertTrue(PM.update_env_file(env_p, changed), "changed value writes")
            self.assertIn("HERMES_LANGFUSE_ENV=e2e-test", env_p.read_text("utf-8"))

            stable_mtime = env_p.stat().st_mtime_ns
            self.assertFalse(
                PM.update_env_file(env_p, changed), "identical state is not rewritten"
            )
            self.assertEqual(env_p.stat().st_mtime_ns, stable_mtime)

            # Removing keys that are absent is likewise a no-op.
            self.assertFalse(
                PM.update_env_file(
                    env_p, {}, remove_keys={"HERMES_LANGFUSE_SECRET_KEY"}
                )
            )
            self.assertEqual(env_p.stat().st_mtime_ns, stable_mtime)

            # A real change (removing a present key) writes again.
            self.assertTrue(
                PM.update_env_file(env_p, {}, remove_keys={"HERMES_LANGFUSE_ENV"})
            )
            self.assertNotIn("HERMES_LANGFUSE_ENV", env_p.read_text(encoding="utf-8"))

    def test_check_setup_status_detects_outdated_profiles(self):
        """check_setup_status flags zf-* profiles whose SOUL.md differs from templates."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.check_gh_auth_status", return_value=True),
            ):
                PM.ensure_zf_profiles()
                status_fresh = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_fresh["profiles_ready"])
                self.assertTrue(status_fresh["profiles_in_sync"])
                self.assertEqual(status_fresh["outdated_profiles"], [])

                # Simulate an outdated SOUL.md on zf-orchestrator
                (fake_home / "profiles" / "zf-orchestrator" / "SOUL.md").write_text(
                    "# Outdated prompt\n", encoding="utf-8"
                )
                status_stale = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_stale["profiles_ready"])
                self.assertFalse(status_stale["profiles_in_sync"])
                self.assertEqual(status_stale["outdated_profiles"], ["zf-orchestrator"])

                # Syncing prompts resolves the drift
                PM.ensure_zf_profiles(update_prompts=True)
                status_synced = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_synced["profiles_in_sync"])
                self.assertEqual(status_synced["outdated_profiles"], [])

    def test_check_plugin_update(self):
        """check_plugin_update reads Hermes Marketplace catalog (cache + live), compares semver, and fails silently."""
        import json
        import os

        class _FakeResp:
            def __init__(self, payload: dict):
                self._body = json.dumps(payload).encode("utf-8")

            def read(self, _n: int = -1) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as td:
            fake_root = Path(td) / "plugin"
            fake_root.mkdir()
            fake_home = Path(td) / "hermes"
            fake_home.mkdir()
            (fake_root / "plugin.yaml").write_text(
                'name: zerofactory\nversion: "1.4.1"\n', encoding="utf-8"
            )
            with (
                patch("profile_manager.get_plugin_root", return_value=fake_root),
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("ZEROFACTORY_SKIP_UPDATE_CHECK", None)

                # Live Marketplace catalog newer version
                with patch(
                    "urllib.request.urlopen",
                    return_value=_FakeResp(
                        {"entries": [{"name": "zerofactory", "version": "1.5.0"}]}
                    ),
                ):
                    res = PM.check_plugin_update(use_cache=False)
                    self.assertEqual(res["plugin_version"], "1.4.1")
                    self.assertEqual(res["latest_plugin_version"], "1.5.0")
                    self.assertTrue(res["plugin_update_available"])

                # Live Marketplace catalog same or older version -> no warning
                with patch(
                    "urllib.request.urlopen",
                    return_value=_FakeResp(
                        {"entries": [{"name": "zerofactory", "version": "1.2.0"}]}
                    ),
                ):
                    res = PM.check_plugin_update(use_cache=False)
                    self.assertFalse(res["plugin_update_available"])

                # Local Hermes cache hit skips network call
                cache_file = fake_home / "cache" / "plugin-catalog.json"
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(
                        {"entries": [{"name": "zerofactory", "version": "1.6.0"}]}
                    ),
                    encoding="utf-8",
                )
                with patch("urllib.request.urlopen") as mock_open:
                    res = PM.check_plugin_update(use_cache=False)
                    self.assertTrue(res["plugin_update_available"])
                    self.assertEqual(res["latest_plugin_version"], "1.6.0")
                    mock_open.assert_not_called()
                cache_file.unlink()

                # Offline / network failure -> fail silent
                with patch(
                    "urllib.request.urlopen", side_effect=OSError("network down")
                ):
                    res = PM.check_plugin_update(use_cache=False)
                    self.assertFalse(res["plugin_update_available"])
                    self.assertIsNone(res["latest_plugin_version"])

                # Opt-out env var skips network call
                os.environ["ZEROFACTORY_SKIP_UPDATE_CHECK"] = "1"
                with patch("urllib.request.urlopen") as mock_open:
                    res = PM.check_plugin_update(use_cache=False)
                    self.assertFalse(res["plugin_update_available"])
                    mock_open.assert_not_called()

    def test_update_config_yaml_plugins(self):
        """update_config_yaml_plugins adds and removes plugins cleanly."""
        with tempfile.TemporaryDirectory() as td:
            cfg_p = Path(td) / "config.yaml"
            cfg_p.write_text(
                yaml.dump(
                    {
                        "plugins": {"enabled": ["zerofactory"]},
                        "model": {"default": "test"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            # Enable langfuse
            PM.update_config_yaml_plugins(cfg_p, enable_plugin="langfuse")
            loaded = yaml.safe_load(cfg_p.read_text(encoding="utf-8"))
            self.assertIn("langfuse", loaded["plugins"]["enabled"])
            self.assertIn("zerofactory", loaded["plugins"]["enabled"])

            # Disable langfuse
            PM.update_config_yaml_plugins(cfg_p, disable_plugin="langfuse")
            loaded_after = yaml.safe_load(cfg_p.read_text(encoding="utf-8"))
            self.assertNotIn("langfuse", loaded_after["plugins"]["enabled"])
            self.assertIn("zerofactory", loaded_after["plugins"]["enabled"])


if __name__ == "__main__":
    unittest.main()
