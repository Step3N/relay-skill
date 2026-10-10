"""验证确定性的文件保护约束，不将其解释为模型每次都遵循指令。"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).absolute().parents[1]
SKILL = ROOT / "skills" / "relay"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install")
memory = load("memory")


def snapshot(root):
    """Hash-free full byte/mode/mtime snapshot detects writes, including unchanged content."""
    return {p.relative_to(root).as_posix():
            (p.is_dir(), p.read_bytes() if p.is_file() else None,
             stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in root.rglob("*") if not p.is_symlink()}


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var or /tmp can be platform symlinks; fixtures use canonical paths.
        self.base = Path(self.temp.name).resolve()
        self.skills = self.base / "user skills"
        self.home = self.base / "codex home"
        self.home.mkdir()
        self.agents = self.home / "AGENTS.md"
        self.agents.write_bytes(b"# Existing rules\r\nUse TypeScript.\r\nDo not alter the database.")
        self.original = self.agents.read_bytes()
        self.project = self.base / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def install(self, **kwargs):
        return installer.install(SKILL, self.skills, self.home, **kwargs)

    def legacy_install(self):
        target = self.skills / installer.LEGACY_NAME
        target.mkdir(parents=True)
        files = {key: value.replace(b"relay", b"agent-continuity")
                 for key, value in installer.package(SKILL).items()}
        for key, value in files.items():
            path = target / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        receipt = {"schema": 1, "name": installer.LEGACY_NAME,
                   "files": {key: installer.digest(value) for key, value in files.items()}}
        (target / installer.LEGACY_RECEIPT).write_text(json.dumps(receipt))
        block = files["assets/AGENTS.snippet.md"].replace(b"__SKILL_PATH__", str(target).encode())
        self.agents.write_bytes(self.original + b"\r\n\r\n" + block.replace(b"\n", b"\r\n") + b"\r\n# User suffix\r\n")
        return target

    def test_migration_preserves_rules_user_files_modes_and_backup(self):
        old = self.legacy_install()
        (old / "custom.txt").write_text("user-added note")
        (old / "custom.txt").chmod(0o640)
        before = installer.inventory(old)
        agents_before = self.agents.read_bytes()
        result = self.install()
        dest = self.skills / installer.NAME
        self.assertFalse(old.exists())
        self.assertEqual(result["migrated_from"], str(old))
        self.assertEqual(installer.inventory(Path(result["backup"]) / "skill"), before)
        self.assertEqual((Path(result["backup"]) / "AGENTS.md").read_bytes(), agents_before)
        self.assertEqual((dest / "custom.txt").read_text(), "user-added note")
        self.assertEqual(stat.S_IMODE((dest / "custom.txt").stat().st_mode), 0o640)
        output = self.agents.read_bytes()
        self.assertTrue(output.startswith(self.original + b"\r\n\r\n"))
        self.assertTrue(output.endswith(b"\r\n# User suffix\r\n"))
        self.assertEqual(output.count(installer.START), 1)
        self.assertNotIn(installer.LEGACY_START, output)
        self.assertFalse((dest / installer.LEGACY_RECEIPT).exists())
        snap = snapshot(self.base)
        self.assertEqual(self.install()["status"], "already-installed")
        self.install(check=True)
        self.assertEqual(snap, snapshot(self.base))

    def test_migration_check_is_zero_write_and_does_not_migrate(self):
        self.legacy_install()
        snap = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install(check=True)
        self.assertEqual(snap, snapshot(self.base))

    def test_modified_legacy_install_refused_without_writes(self):
        old = self.legacy_install()
        (old / "SKILL.md").write_text("user edit")
        snap = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(snap, snapshot(self.base))

    def test_both_names_refused_without_writes(self):
        self.legacy_install()
        (self.skills / installer.NAME).mkdir()
        snap = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(snap, snapshot(self.base))

    def test_mixed_or_broken_legacy_markers_refused(self):
        cases = [installer.LEGACY_START, installer.LEGACY_END,
                 installer.LEGACY_END + b"\n" + installer.LEGACY_START,
                 b"prose " + installer.LEGACY_START + b"\n" + installer.LEGACY_END,
                 installer.LEGACY_START + b"\n" + installer.LEGACY_END + b"\n" + installer.START + b"\n" + installer.END]
        for bad in cases:
            with self.subTest(bad=bad):
                self.agents.write_bytes(self.original + b"\n" + bad)
                snap = snapshot(self.base)
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(snap, snapshot(self.base))

    def test_migration_failure_restores_old_name_and_global_rules(self):
        old = self.legacy_install()
        before = installer.inventory(old)
        agents_before = self.agents.read_bytes()
        real_write = installer.atomic_write

        def fail_agents(path, data, expected):
            if path == self.agents:
                raise OSError("simulated denied global write")
            return real_write(path, data, expected)

        with patch.object(installer, "atomic_write", side_effect=fail_agents):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(before, installer.inventory(old))
        self.assertFalse((self.skills / installer.NAME).exists())
        self.assertEqual(agents_before, self.agents.read_bytes())
        self.assertFalse((self.home / ".relay-install.lock").exists())
        self.assertFalse((self.home / ".agent-continuity-install.lock").exists())

    def test_legacy_lock_blocks_migration_without_target_changes(self):
        self.legacy_install()
        (self.home / ".agent-continuity-install.lock").write_text("old installer active")
        snap = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(snap, snapshot(self.base))

    def test_discovery_selection_detects_legacy_name_and_cross_name_duplicates(self):
        old = self.base / ".agents" / "skills" / installer.LEGACY_NAME
        old.mkdir(parents=True)
        self.assertEqual(installer.default_skills_dir(self.base, self.home), old.parent)
        (old.parent / installer.NAME).mkdir()
        with self.assertRaises(installer.InstallError):
            installer.default_skills_dir(self.base, self.home)

    def test_package_resources_links_and_python_syntax(self):
        files = installer.package(SKILL)
        self.assertTrue(set(installer.REQUIRED).issubset(files))
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_install_preserves_rules_backups_and_permissions(self):
        self.agents.chmod(0o640)
        result = self.install()
        output = self.agents.read_bytes()
        self.assertTrue(output.startswith(self.original))
        self.assertEqual(output.count(installer.START), 1)
        self.assertEqual(output.count(installer.END), 1)
        self.assertEqual((Path(result["backup"]) / "AGENTS.md").read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.agents.stat().st_mode), 0o640)
        self.assertFalse((self.project / ".ai").exists())
        self.assertEqual(result["status"], "installed")

    def test_repeated_install_is_no_write_including_mtime(self):
        self.install()
        before = snapshot(self.base)
        self.assertEqual(self.install()["status"], "already-installed")
        self.assertEqual(before, snapshot(self.base))

    def test_check_is_read_only(self):
        self.install()
        before = snapshot(self.base)
        self.assertEqual(self.install(check=True)["status"], "verified-files-only")
        self.assertEqual(before, snapshot(self.base))

    def test_os_metadata_does_not_trigger_install_or_integrity_change(self):
        source = self.base / "source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        installer.install(source, self.skills, self.home)
        (source / ".DS_Store").write_bytes(b"OS folder metadata")
        (source / "assets" / ".DS_Store").write_bytes(b"OS nested metadata")
        before = snapshot(self.base)
        installer.install(source, self.skills, self.home, check=True)
        self.assertEqual(installer.install(source, self.skills, self.home)["status"], "already-installed")
        self.assertEqual(before, snapshot(self.base))

    def test_check_does_not_install_missing_files(self):
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install(check=True)
        self.assertEqual(before, snapshot(self.base))

    def test_update_preserves_outside_content_and_added_files(self):
        self.install()
        dest = self.skills / installer.NAME
        custom = dest / "my-note.txt"
        custom.write_bytes(b"user notes")
        custom.chmod(0o640)
        suffix = b"\r\n# Later user rule\r\nNo deploy.\r\n"
        self.agents.write_bytes(self.agents.read_bytes() + suffix)
        source = self.base / "new-source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__"))
        skill_text = (source / "SKILL.md").read_text().replace('version: "1.0.2"', 'version: "1.0.3"')
        (source / "SKILL.md").write_text(skill_text)
        result = installer.install(source, self.skills, self.home)
        self.assertTrue(result["changed_skill"])
        self.assertTrue(self.agents.read_bytes().startswith(self.original))
        self.assertTrue(self.agents.read_bytes().endswith(suffix))
        self.assertEqual(custom.read_bytes(), b"user notes")
        self.assertEqual(stat.S_IMODE(custom.stat().st_mode), 0o640)
        self.assertFalse(list(self.skills.glob(".relay-previous-*")))
        self.assertEqual(installer.install(source, self.skills, self.home)["status"], "already-installed")

    def test_installed_source_check_preserves_user_additions(self):
        self.install()
        dest = self.skills / installer.NAME
        (dest / "custom.txt").write_text("user-owned")
        before = snapshot(self.base)
        installer.install(dest, self.skills, self.home, check=True)
        self.assertEqual(before, snapshot(self.base))

    def test_modified_owned_skill_aborts_without_writes(self):
        self.install()
        (self.skills / installer.NAME / "SKILL.md").write_text("user custom Skill")
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(before, snapshot(self.base))

    def test_unknown_existing_skill_is_preserved(self):
        target = self.skills / installer.NAME
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("existing independent installation")
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(before, snapshot(self.base))

    def test_upgrade_collision_preserves_user_file(self):
        self.install()
        (self.skills / installer.NAME / "new.txt").write_bytes(b"user data")
        source = self.base / "new-source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "new.txt").write_bytes(b"new package content")
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            installer.install(source, self.skills, self.home)
        self.assertEqual(before, snapshot(self.base))

    def test_retired_owned_file_is_backed_up_not_left_discoverable(self):
        source = self.base / "source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "retired.txt").write_text("old owned file")
        installer.install(source, self.skills, self.home)
        result = self.install()
        self.assertFalse((self.skills / installer.NAME / "retired.txt").exists())
        self.assertEqual((Path(result["backup"]) / "skill" / "retired.txt").read_text(), "old owned file")

    def test_ambiguous_markers_abort(self):
        cases = [installer.START, installer.END,
                 installer.START + b"\n" + installer.START + b"\n" + installer.END + b"\n" + installer.END,
                 installer.END + b"\n" + installer.START,
                 b"prose " + installer.START + b"\n" + installer.END]
        for bad in cases:
            with self.subTest(bad=bad):
                self.agents.write_bytes(self.original + b"\n" + bad)
                before = snapshot(self.base)
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(before, snapshot(self.base))

    def test_managed_block_update_keeps_prefix_and_suffix_exact(self):
        prefix, suffix = b"# Before\r\n", b"\r\n# After\r\nNo publish."
        original = prefix + installer.START + b"\r\nold block\r\n" + installer.END + suffix
        self.agents.write_bytes(original)
        self.install()
        updated = self.agents.read_bytes()
        self.assertTrue(updated.startswith(prefix))
        self.assertTrue(updated.endswith(suffix))
        self.assertEqual(updated.count(installer.START), 1)

    def test_nonempty_override_aborts_and_remains_untouched(self):
        (self.home / "AGENTS.override.md").write_text("user override")
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(before, snapshot(self.base))

    def test_empty_override_is_not_modified(self):
        override = self.home / "AGENTS.override.md"
        override.write_bytes(b"")
        before = override.stat().st_mtime_ns
        self.install()
        self.assertEqual(override.read_bytes(), b"")
        self.assertEqual(before, override.stat().st_mtime_ns)

    def test_skill_symlink_is_not_followed(self):
        self.skills.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"do not touch")
        (self.skills / installer.NAME).symlink_to(outside, target_is_directory=True)
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(before, snapshot(self.base))

    def test_agents_symlink_is_not_followed(self):
        self.agents.unlink()
        other = self.base / "other-rules"
        other.write_bytes(self.original)
        self.agents.symlink_to(other)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(other.read_bytes(), self.original)

    def test_missing_template_aborts_before_write(self):
        source = self.base / "broken-source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "assets" / "PROJECT.template.md").unlink()
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            installer.install(source, self.skills, self.home)
        self.assertEqual(before, snapshot(self.base))

    def test_competing_lock_aborts_without_target_changes(self):
        lock = self.home / ".relay-install.lock"
        lock.write_text("another installer")
        before = snapshot(self.base)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(before, snapshot(self.base))

    def test_global_write_failure_rolls_back_first_install(self):
        real_write = installer.atomic_write

        def fail_agents(path, data, expected):
            if path == self.agents:
                raise OSError("simulated denied global write")
            return real_write(path, data, expected)

        with patch.object(installer, "atomic_write", side_effect=fail_agents):
            with self.assertRaises(OSError):
                self.install()
        self.assertFalse((self.skills / installer.NAME).exists())
        self.assertEqual(self.agents.read_bytes(), self.original)
        self.assertFalse((self.home / ".relay-install.lock").exists())
        backups = list((self.home / ".relay-backups").glob("*/AGENTS.md"))
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_upgrade_failure_restores_previous_skill(self):
        self.install()
        before_skill = installer.inventory(self.skills / installer.NAME)
        source = self.base / "new-source"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__"))
        (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + "\nNew version note.\n")
        # Force a global managed-block update as part of this upgrade.
        snippet = source / "assets" / "AGENTS.snippet.md"
        snippet.write_text(snippet.read_text().replace("## Relay", "## Relay V1.1"))
        before_agents = self.agents.read_bytes()
        real_write = installer.atomic_write

        def fail_agents(path, data, expected):
            if path == self.agents:
                raise OSError("simulated denied global write")
            return real_write(path, data, expected)

        with patch.object(installer, "atomic_write", side_effect=fail_agents):
            with self.assertRaises(OSError):
                installer.install(source, self.skills, self.home)
        self.assertEqual(before_skill, installer.inventory(self.skills / installer.NAME))
        self.assertEqual(before_agents, self.agents.read_bytes())

    def test_atomic_write_refuses_concurrent_user_change(self):
        self.agents.write_bytes(b"new user edit")
        with self.assertRaises(installer.InstallError):
            installer.atomic_write(self.agents, b"replace", self.original)
        self.assertEqual(self.agents.read_bytes(), b"new user edit")

    def test_discovery_selection_prefers_modern_for_new_install(self):
        self.assertEqual(installer.default_skills_dir(self.base, self.home), self.base / ".agents" / "skills")

    def test_legacy_existing_install_selected_once(self):
        old = self.home / "skills" / installer.NAME
        old.mkdir(parents=True)
        self.assertEqual(installer.default_skills_dir(self.base, self.home), old.parent)
        modern = self.base / ".agents" / "skills" / installer.NAME
        modern.mkdir(parents=True)
        with self.assertRaises(installer.InstallError):
            installer.default_skills_dir(self.base, self.home)

    def test_cli_honors_codex_home_environment_without_project_writes(self):
        env = dict(os.environ, CODEX_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        command = [sys.executable, str(SKILL / "scripts" / "install.py"), "--skills-dir", str(self.skills)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["global_rules"], str(self.agents))
        self.assertFalse((self.project / ".ai").exists())
        result = subprocess.run(command + ["--check"], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "verified-files-only")

    def test_init_creates_only_missing_memory_and_no_project_rules(self):
        self.assertEqual(memory.initialize(self.project)["created"], list(memory.NAMES))
        self.assertFalse((self.project / "AGENTS.md").exists())
        self.assertFalse((self.project / ".gitignore").exists())
        before = snapshot(self.project)
        self.assertEqual(memory.initialize(self.project)["created"], [])
        self.assertEqual(before, snapshot(self.project))

    def test_init_preserves_existing_memory_and_repairs_one_missing_file(self):
        ai = self.project / ".ai"
        ai.mkdir()
        long_term = ai / "PROJECT.md"
        long_term.write_bytes(b"User-authored goals with custom sections\r\n")
        stamp = long_term.stat().st_mtime_ns
        result = memory.initialize(self.project)
        self.assertEqual(result["created"], ["HANDOFF.md"])
        self.assertEqual(long_term.read_bytes(), b"User-authored goals with custom sections\r\n")
        self.assertEqual(long_term.stat().st_mtime_ns, stamp)

    def test_empty_memory_is_preserved_not_overwritten(self):
        (self.project / ".ai").mkdir()
        path = self.project / ".ai" / "PROJECT.md"
        path.write_bytes(b"")
        memory.initialize(self.project)
        self.assertEqual(path.read_bytes(), b"")

    def test_inspect_missing_memory_is_zero_write(self):
        before = snapshot(self.project)
        result = memory.inspect(self.project)
        self.assertFalse(result["files"]["PROJECT.md"]["exists"])
        self.assertEqual(before, snapshot(self.project))

    def test_inspect_existing_memory_is_zero_write(self):
        memory.initialize(self.project)
        before = snapshot(self.project)
        result = memory.inspect(self.project)
        self.assertEqual(len(result["files"]["HANDOFF.md"]["sha256"]), 64)
        self.assertEqual(before, snapshot(self.project))

    def test_symlink_memory_is_refused_without_outside_writes(self):
        outside = self.base / "outside-memory"
        outside.mkdir()
        (self.project / ".ai").symlink_to(outside, target_is_directory=True)
        before = snapshot(outside)
        for operation in (memory.initialize, memory.inspect):
            with self.assertRaises(ValueError):
                operation(self.project)
        self.assertEqual(before, snapshot(outside))

    def test_nonregular_memory_preflight_before_any_new_file(self):
        ai = self.project / ".ai"
        ai.mkdir()
        (ai / "HANDOFF.md").mkdir()
        before = snapshot(self.project)
        with self.assertRaises(ValueError):
            memory.initialize(self.project)
        self.assertEqual(before, snapshot(self.project))


if __name__ == "__main__":
    unittest.main()
