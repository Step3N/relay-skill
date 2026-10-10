"""检查文件保护的实际结果；这些测试不能证明 Agent 每次都遵循提示词。"""
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
    """比较原始内容、权限和修改时间，连内容未变的重复写入也能发现。"""
    return {p.relative_to(root).as_posix():
            (p.is_dir(), p.read_bytes() if p.is_file() else None,
             stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in root.rglob("*") if not p.is_symlink()}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS 临时目录可能经过系统链接，测试先解析为真实路径。
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
        # 让这次更新也修改 Relay 配置，检查全局写入失败时能否恢复原安装。
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

    def test_receipt_rejects_wrong_owner_and_unsafe_paths_without_writes(self):
        self.install()
        receipt_path = self.skills / installer.NAME / installer.RECEIPT
        valid = json.loads(receipt_path.read_bytes())
        invalid = [dict(valid, name="another-skill")]
        for path in (str(self.base / "outside.txt"), "../outside.txt", installer.RECEIPT):
            invalid.append(dict(valid, files={path: installer.digest(b"outside")}))
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                receipt_path.write_text(json.dumps(receipt))
                before = snapshot(self.base)
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(before, snapshot(self.base))

    def test_unrelated_skills_and_managed_rules_remain_untouched(self):
        others = [self.skills / "another-skill", self.home / "skills" / "another-skill"]
        for other in others:
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text("User-owned skill")
            (other / "custom.txt").write_text("User note")
        other_lock = self.home / ".another-skill-install.lock"
        other_lock.write_text("Keep this lock")
        block = b"\r\n<!-- another-skill:start -->\r\nKeep these rules.\r\n<!-- another-skill:end -->\r\n"
        rules = self.original + block
        self.agents.write_bytes(rules)
        before = [snapshot(other) for other in others]
        lock_before = (other_lock.read_bytes(), other_lock.stat().st_mtime_ns)
        self.assertEqual(installer.default_skills_dir(self.base, self.home), self.base / ".agents" / "skills")
        self.install()
        self.install(check=True)
        self.assertTrue(self.agents.read_bytes().startswith(rules))
        self.assertEqual(before, [snapshot(other) for other in others])
        self.assertEqual(lock_before, (other_lock.read_bytes(), other_lock.stat().st_mtime_ns))

    def test_discovery_selection_prefers_modern_for_new_install(self):
        self.assertEqual(installer.default_skills_dir(self.base, self.home), self.base / ".agents" / "skills")

    def test_existing_codex_home_install_selected_once(self):
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

    def test_old_memory_headings_and_custom_content_remain_usable(self):
        ai = self.project / ".ai"
        ai.mkdir()
        old = {
            "PROJECT.md": "# Project\r\n## User goals\r\n离线使用，不新增依赖。\r\n## 自定义说明\r\n保留我的决定。\r\n",
            "HANDOFF.md": "# Previous work\n## Next step\n尚未完成导出。\n用户没有授权开始下一阶段。\n",
        }
        for name, content in old.items():
            (ai / name).write_bytes(content.encode("utf-8"))
        before = snapshot(self.project)
        result = memory.inspect(self.project)
        for name, content in old.items():
            self.assertEqual(result["files"][name]["content"], content)
        self.assertEqual(memory.initialize(self.project)["created"], [])
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
