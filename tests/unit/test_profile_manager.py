"""Unit tests for profile_manager.py."""

from __future__ import annotations

import os
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

    def test_fallback_skill_copies_resync_and_report_stale_status(self):
        """Fallback skill copies refresh on setup and stale copies fail sync status."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "hermes"
            plugin_root = root / "plugin"
            skill_source = plugin_root / "skills" / "zf-builder-example"
            skill_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text("original skill\n", encoding="utf-8")
            (skill_source / "companion.md").write_text(
                "upstream companion\n", encoding="utf-8"
            )
            (skill_source / "unchanged.txt").write_text(
                "stable content\n", encoding="utf-8"
            )
            for role in PM.ZF_PROFILES:
                template_dir = plugin_root / "templates" / role
                template_dir.mkdir(parents=True)
                (template_dir / "SOUL.md").write_text(f"{role}\n", encoding="utf-8")
                (template_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            original_symlink_to = Path.symlink_to

            def reject_skill_symlinks(
                path: Path, target: str | Path, target_is_directory: bool = False
            ) -> None:
                if path.parent.name == "skills":
                    raise OSError("symlinks unavailable for profile skills")
                original_symlink_to(
                    path, target, target_is_directory=target_is_directory
                )

            profile_skill = (
                fake_home
                / "profiles"
                / "zf-builder"
                / "skills"
                / "zf-builder-example"
                / "SKILL.md"
            )
            profile_skills = profile_skill.parent.parent
            unchanged_copy = profile_skill.parent / "unchanged.txt"
            user_file = profile_skill.parent / "user-notes.txt"
            unrelated_skill_file = profile_skills / "user-owned" / "SKILL.md"
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
                patch("profile_manager.check_gh_auth_status", return_value=True),
                patch("profile_manager.check_plugin_update", return_value={}),
                patch.object(
                    Path, "symlink_to", autospec=True, side_effect=reject_skill_symlinks
                ),
            ):
                PM.ensure_zf_profiles()
                self.assertEqual(
                    profile_skill.read_text(encoding="utf-8"), "original skill\n"
                )
                self.assertFalse(profile_skill.parent.is_symlink())
                user_file.write_text("keep my notes\n", encoding="utf-8")
                unrelated_skill_file.parent.mkdir()
                unrelated_skill_file.write_text("user skill\n", encoding="utf-8")
                old_mtime_ns = 1_600_000_000_000_000_000
                os.utime(unchanged_copy, ns=(old_mtime_ns, old_mtime_ns))

                (skill_source / "SKILL.md").write_text(
                    "updated skill\n", encoding="utf-8"
                )
                (skill_source / "companion.md").rename(
                    skill_source / "renamed-companion.md"
                )
                status_stale = PM.check_setup_status(use_cache=False)
                self.assertFalse(status_stale["profiles_in_sync"])
                self.assertEqual(status_stale["outdated_profiles"], ["zf-builder"])

                PM.ensure_zf_profiles()
                self.assertEqual(
                    profile_skill.read_text(encoding="utf-8"), "updated skill\n"
                )
                self.assertFalse((profile_skill.parent / "companion.md").exists())
                self.assertEqual(
                    (profile_skill.parent / "renamed-companion.md").read_text(
                        encoding="utf-8"
                    ),
                    "upstream companion\n",
                )
                self.assertEqual(unchanged_copy.stat().st_mtime_ns, old_mtime_ns)
                self.assertEqual(
                    user_file.read_text(encoding="utf-8"), "keep my notes\n"
                )
                self.assertEqual(
                    unrelated_skill_file.read_text(encoding="utf-8"), "user skill\n"
                )
                status_synced = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_synced["profiles_in_sync"])
                self.assertEqual(status_synced["outdated_profiles"], [])

                PM.ensure_zf_profiles()
                self.assertEqual(unchanged_copy.stat().st_mtime_ns, old_mtime_ns)
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

    def test_live_skill_symlinks_are_not_relinked_or_reported_outdated(self):
        """Setup preserves healthy and noncanonical live skill symlinks."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "hermes"
            plugin_root = root / "plugin"
            skill_source = plugin_root / "skills" / "zf-builder-example"
            skill_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text("plugin skill\n", encoding="utf-8")
            for role in PM.ZF_PROFILES:
                template_dir = plugin_root / "templates" / role
                template_dir.mkdir(parents=True)
                (template_dir / "SOUL.md").write_text(f"{role}\n", encoding="utf-8")
                (template_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
                patch("profile_manager.check_gh_auth_status", return_value=True),
                patch("profile_manager.check_plugin_update", return_value={}),
            ):
                PM.ensure_zf_profiles()
                installed_link = (
                    fake_home
                    / "profiles"
                    / "zf-builder"
                    / "skills"
                    / "zf-builder-example"
                )
                self.assertTrue(installed_link.is_symlink())
                self.assertEqual(installed_link.resolve(), skill_source.resolve())
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

                alternate_target = root / "alternate-skill"
                alternate_target.mkdir()
                (alternate_target / "SKILL.md").write_text(
                    "custom skill\n", encoding="utf-8"
                )
                installed_link.unlink()
                installed_link.symlink_to(alternate_target, target_is_directory=True)

                status_before_sync = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_before_sync["profiles_in_sync"])
                self.assertEqual(status_before_sync["outdated_profiles"], [])

                PM.ensure_zf_profiles()
                self.assertTrue(installed_link.is_symlink())
                self.assertEqual(installed_link.resolve(), alternate_target.resolve())
                status_after_sync = PM.check_setup_status(use_cache=False)
                self.assertTrue(status_after_sync["profiles_in_sync"])
                self.assertEqual(status_after_sync["outdated_profiles"], [])

                installed_link.unlink()
                installed_link.symlink_to(
                    root / "missing-skill", target_is_directory=True
                )
                status_broken = PM.check_setup_status(use_cache=False)
                self.assertFalse(status_broken["profiles_in_sync"])
                self.assertEqual(status_broken["outdated_profiles"], ["zf-builder"])

                PM.ensure_zf_profiles()
                self.assertEqual(installed_link.resolve(), skill_source.resolve())
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

    def test_fallback_skill_copies_reconcile_file_directory_transitions(self):
        """Managed file/directory transitions preserve unrelated copied files."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "hermes"
            plugin_root = root / "plugin"
            skill_source = plugin_root / "skills" / "zf-builder-transitions"
            skill_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text("skill\n", encoding="utf-8")
            (skill_source / "file-to-dir").write_text("old file\n", encoding="utf-8")
            source_dir_to_file = skill_source / "dir-to-file"
            source_dir_to_file.mkdir()
            (source_dir_to_file / "child.txt").write_text(
                "old child\n", encoding="utf-8"
            )
            for role in PM.ZF_PROFILES:
                template_dir = plugin_root / "templates" / role
                template_dir.mkdir(parents=True)
                (template_dir / "SOUL.md").write_text(f"{role}\n", encoding="utf-8")
                (template_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            original_symlink_to = Path.symlink_to

            def reject_skill_symlinks(
                path: Path, target: str | Path, target_is_directory: bool = False
            ) -> None:
                if path.parent.name == "skills":
                    raise OSError("symlinks unavailable for profile skills")
                original_symlink_to(
                    path, target, target_is_directory=target_is_directory
                )

            copied_skill = (
                fake_home
                / "profiles"
                / "zf-builder"
                / "skills"
                / "zf-builder-transitions"
            )
            user_file = copied_skill / "user-notes.txt"
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
                patch("profile_manager.check_gh_auth_status", return_value=True),
                patch("profile_manager.check_plugin_update", return_value={}),
                patch.object(
                    Path, "symlink_to", autospec=True, side_effect=reject_skill_symlinks
                ),
            ):
                PM.ensure_zf_profiles()
                user_file.write_text("keep this\n", encoding="utf-8")

                (skill_source / "file-to-dir").unlink()
                (skill_source / "file-to-dir").mkdir()
                (skill_source / "file-to-dir" / "child.txt").write_text(
                    "new child\n", encoding="utf-8"
                )
                (source_dir_to_file / "child.txt").unlink()
                source_dir_to_file.rmdir()
                source_dir_to_file.write_text("new file\n", encoding="utf-8")

                self.assertFalse(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )
                PM.ensure_zf_profiles()

                self.assertFalse((copied_skill / "file-to-dir").is_file())
                self.assertEqual(
                    (copied_skill / "file-to-dir" / "child.txt").read_text(
                        encoding="utf-8"
                    ),
                    "new child\n",
                )
                self.assertTrue((copied_skill / "dir-to-file").is_file())
                self.assertEqual(
                    (copied_skill / "dir-to-file").read_text(encoding="utf-8"),
                    "new file\n",
                )
                self.assertEqual(user_file.read_text(encoding="utf-8"), "keep this\n")
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

    def test_fallback_skill_sync_does_not_follow_nested_symlinks(self):
        """Nested symlinks cannot redirect writes or deletions outside the copy."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "hermes"
            plugin_root = root / "plugin"
            skill_source = plugin_root / "skills" / "zf-builder-nested-link"
            nested_source = skill_source / "nested"
            nested_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text("skill\n", encoding="utf-8")
            source_keep = nested_source / "keep.txt"
            source_remove = nested_source / "remove.txt"
            source_keep.write_text("initial\n", encoding="utf-8")
            source_remove.write_text("remove me\n", encoding="utf-8")
            for role in PM.ZF_PROFILES:
                template_dir = plugin_root / "templates" / role
                template_dir.mkdir(parents=True)
                (template_dir / "SOUL.md").write_text(f"{role}\n", encoding="utf-8")
                (template_dir / "config.yaml").write_text("{}\n", encoding="utf-8")

            original_symlink_to = Path.symlink_to

            def reject_skill_symlinks(
                path: Path, target: str | Path, target_is_directory: bool = False
            ) -> None:
                if path.parent.name == "skills":
                    raise OSError("symlinks unavailable for profile skills")
                original_symlink_to(
                    path, target, target_is_directory=target_is_directory
                )

            copied_skill = (
                fake_home
                / "profiles"
                / "zf-builder"
                / "skills"
                / "zf-builder-nested-link"
            )
            outside = root / "outside"
            outside.mkdir()
            outside_keep = outside / "keep.txt"
            outside_remove = outside / "remove.txt"
            with (
                patch("profile_manager.get_hermes_home", return_value=fake_home),
                patch("profile_manager.get_plugin_root", return_value=plugin_root),
                patch("profile_manager.check_gh_auth_status", return_value=True),
                patch("profile_manager.check_plugin_update", return_value={}),
                patch.object(
                    Path, "symlink_to", autospec=True, side_effect=reject_skill_symlinks
                ),
            ):
                PM.ensure_zf_profiles()
                copied_nested = copied_skill / "nested"
                outside_keep.write_text("initial\n", encoding="utf-8")
                outside_remove.write_text("remove me\n", encoding="utf-8")
                for copied_file in copied_nested.iterdir():
                    copied_file.unlink()
                copied_nested.rmdir()
                copied_nested.symlink_to(outside, target_is_directory=True)
                source_keep.write_text("updated\n", encoding="utf-8")
                source_remove.unlink()

                self.assertFalse(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )
                PM.ensure_zf_profiles()

                self.assertFalse(copied_nested.is_symlink())
                self.assertEqual(
                    (copied_nested / "keep.txt").read_text(encoding="utf-8"),
                    "updated\n",
                )
                self.assertFalse((copied_nested / "remove.txt").exists())
                self.assertEqual(outside_keep.read_text(encoding="utf-8"), "initial\n")
                self.assertEqual(
                    outside_remove.read_text(encoding="utf-8"), "remove me\n"
                )
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

                (copied_skill / ".zerofactory-managed-files").write_bytes(b"\xff")
                self.assertFalse(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )
                PM.ensure_zf_profiles()
                self.assertTrue(
                    PM.check_setup_status(use_cache=False)["profiles_in_sync"]
                )

    def test_ensure_plugin_symlinks(self):
        """Creates symlink to zerofactory only in zf-* profiles (never root)."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            with patch("profile_manager.get_hermes_home", return_value=fake_home):
                PM.ensure_zf_profiles()
                res = PM.ensure_plugin_symlinks()
                self.assertIsInstance(res, dict)
                self.assertFalse((fake_home / "plugins").exists())
                for role in PM.ZF_PROFILES:
                    self.assertTrue(
                        (
                            fake_home / "profiles" / role / "plugins" / "zerofactory"
                        ).is_symlink()
                    )

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
