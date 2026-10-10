#!/usr/bin/env python3
"""离线安全安装，只使用 Python 标准库，不执行下载来的程序。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

NAME = "relay"
RECEIPT = ".relay-install.json"
START = b"<!-- relay:start -->"
END = b"<!-- relay:end -->"
LEGACY_NAME = "agent-continuity"
LEGACY_RECEIPT = ".agent-continuity-install.json"
LEGACY_START = b"<!-- agent-continuity:start -->"
LEGACY_END = b"<!-- agent-continuity:end -->"
REQUIRED = (
    "SKILL.md", "agents/openai.yaml", "assets/AGENTS.snippet.md",
    "assets/PROJECT.template.md", "assets/HANDOFF.template.md",
    "references/install.md", "scripts/install.py", "scripts/memory.py",
)


class InstallError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


def reject_links(path):
    """不跟随目标路径及其父目录中的符号链接。"""
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise InstallError(f"拒绝跟随符号链接： {part}")


def read_regular(path):
    reject_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError(f"应为普通文件： {path}")
    return path.read_bytes()


def package(source):
    reject_links(source)
    if not source.is_dir():
        raise InstallError(f"Skill 源目录缺失： {source}")
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise InstallError(f"拒绝源文件符号链接： {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"源文件不是普通文件： {path}")
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == ".DS_Store" or relative == RECEIPT:
            continue
        files[relative] = path.read_bytes()
    missing = set(REQUIRED) - files.keys()
    if missing:
        raise InstallError(f"缺少必要资源： {sorted(missing)}")
    source_receipt = read_regular(source / RECEIPT)
    if source_receipt is not None:
        check_owned({**files, RECEIPT: source_receipt})
        owned = json.loads(source_receipt)["files"]
        files = {key: files[key] for key in owned}
    text = files["SKILL.md"].decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise InstallError("SKILL 元数据格式无效")
    front = text.split("---\n", 2)[1]
    if not re.search(r"^name: relay$", front, re.M):
        raise InstallError("Skill 名称不正确")
    if not re.search(r"^description: \S.+$", front, re.M):
        raise InstallError("缺少 Skill 描述")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in target and target not in files:
            raise InstallError(f"SKILL 中的引用无效： {target}")
    if b"__SKILL_PATH__" not in files["assets/AGENTS.snippet.md"]:
        raise InstallError("全局规则模板缺少安装路径占位标记")
    return files


def merge_agents(original, snippet):
    """保留唯一托管区之外的每个字节。"""
    original = original or b""
    original.decode("utf-8")
    snippet.decode("utf-8")
    pairs = [(START, END), (LEGACY_START, LEGACY_END)]
    present = []
    for first, last in pairs:
        if original.count(first) != original.count(last) or original.count(first) > 1:
            raise InstallError("AGENTS 标记不明确或重复，未写入")
        if first in original:
            present.append((first, last))
    if len(present) > 1:
        raise InstallError("新旧托管块同时存在，先核对再迁移")
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    snippet = snippet.strip(b"\r\n").replace(b"\r\n", b"\n").replace(b"\n", newline)
    if snippet.count(START) != 1 or snippet.count(END) != 1:
        raise InstallError("全局规则模板无效")
    if present:
        first, last = present[0]
        start, end = original.index(first), original.index(last)
        if start >= end:
            raise InstallError("AGENTS 起止标记顺序错误")
        for token, pos in ((first, start), (last, end)):
            if (pos and original[pos - 1:pos] != b"\n") or original[pos + len(token):pos + len(token) + 1] not in (b"", b"\n", b"\r"):
                raise InstallError("AGENTS 标记必须独占一行")
        return original[:start] + snippet + original[end + len(last):]
    separator = b"" if not original else (newline if original.endswith(b"\n") else newline * 2)
    return original + separator + snippet + newline


def inventory(directory):
    reject_links(directory)
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise InstallError(f"应为目录： {directory}")
    result = {}
    for path in sorted(directory.rglob("*")):
        reject_links(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"已安装内容不是普通文件： {path}")
        result[path.relative_to(directory).as_posix()] = path.read_bytes()
    return result


def check_owned(existing, name=NAME, receipt_key=RECEIPT):
    if existing is None:
        return
    if receipt_key not in existing:
        raise InstallError("已有 Skill 缺少安装归属凭据，保留原内容并人工核对")
    try:
        receipt = json.loads(existing[receipt_key])
        hashes = receipt["files"]
        if receipt["schema"] != 1 or receipt["name"] != name or not isinstance(hashes, dict) or not hashes:
            raise ValueError("安装凭据无效")
        for relative, expected in hashes.items():
            if relative in (RECEIPT, LEGACY_RECEIPT) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("安装凭据含不安全路径")
            if relative not in existing or digest(existing[relative]) != expected:
                raise InstallError(f"托管文件已被本地修改或缺失： {relative}；不覆盖")
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError("安装归属凭据无效，保留原安装") from exc


def atomic_write(path, data, expected):
    if read_regular(path) != expected:
        raise InstallError(f"发现并发改动： {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        if read_regular(path) != expected:
            raise InstallError(f"发现并发改动： {path}")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def default_skills_dir(home, codex_home):
    modern = home / ".agents" / "skills"
    legacy = codex_home / "skills"
    candidates = [p for p in dict.fromkeys((modern, legacy))
                  for name in (NAME, LEGACY_NAME)
                  if (p / name).exists() or (p / name).is_symlink()]
    if len(candidates) > 1:
        raise InstallError("现代与旧目录存在重复安装，先确认客户端发现位置再安装")
    return candidates[0] if candidates else modern


def install(source, skills_dir, codex_home, check=False):
    source, skills_dir, codex_home = map(absolute, (source, skills_dir, codex_home))
    dest, agents = skills_dir / NAME, codex_home / "AGENTS.md"
    legacy_dest = skills_dir / LEGACY_NAME
    for path in (dest, legacy_dest, agents, codex_home / "AGENTS.override.md"):
        reject_links(path)
    override = read_regular(codex_home / "AGENTS.override.md")
    if override and override.strip():
        raise InstallError("非空全局 AGENTS.override.md 覆盖 AGENTS.md，需先明确解决")
    files = package(source)
    current, legacy_existing = inventory(dest), inventory(legacy_dest)
    if current is not None and legacy_existing is not None:
        raise InstallError("relay 与 agent-continuity 同时存在，先核对再迁移")
    migrating = legacy_existing is not None
    origin = legacy_dest if migrating else dest
    existing = legacy_existing if migrating else current
    receipt_key = LEGACY_RECEIPT if migrating else RECEIPT
    check_owned(existing, LEGACY_NAME if migrating else NAME, receipt_key)
    original = read_regular(agents)
    slot = str(dest)
    if any(char in slot for char in "`\n\r"):
        raise InstallError("规则路径包含反引号或换行，暂不支持")
    snippet = files["assets/AGENTS.snippet.md"].replace(b"__SKILL_PATH__", slot.encode())
    merged = merge_agents(original, snippet)
    desired = dict(existing or {})  # 保留非托管的用户新增文件
    if existing:
        old_owned = json.loads(existing[receipt_key])["files"]
        for relative in old_owned:
            desired.pop(relative, None)
        desired.pop(receipt_key, None)
        collisions = (files.keys() & desired.keys()) - {RECEIPT}
        if collisions:
            raise InstallError(f"新版本与用户新增文件同名： {sorted(collisions)}")
    desired.update(files)
    desired[RECEIPT] = (json.dumps({"schema": 1, "name": NAME,
        "files": {key: digest(value) for key, value in files.items()}}, indent=2, sort_keys=True) + "\n").encode()
    changed_skill, changed_agents = migrating or existing != desired, original != merged
    report = {"skill_path": str(dest), "global_rules": str(agents),
              "files_validated": len(files), "migrated_from": str(origin) if migrating else None,
              "changed_skill": changed_skill,
              "changed_global_rules": changed_agents, "backup": None,
              "discovery": "未验证：请在新的 Codex 会话中检查实际发现能力",
              "warnings": ["全局规则超过默认 32 KiB 大小预算"] if len(merged) > 32768 else []}
    if check:
        if changed_skill or changed_agents:
            raise InstallError("安装文件或全局托管块与源码不一致，检查未通过")
        report["status"] = "verified-files-only"
        return report
    if not (changed_skill or changed_agents):
        report["status"] = "already-installed"
        return report

    codex_home.mkdir(parents=True, exist_ok=True)
    locks = [codex_home / ".relay-install.lock"]
    if migrating:
        locks.insert(0, codex_home / ".agent-continuity-install.lock")
    acquired = []
    try:
        for lock in locks:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            acquired.append(lock)
    except OSError as exc:
        for lock in acquired:
            lock.unlink()
        raise InstallError("无法获取安装锁，检查权限并确认其他安装器是否仍在运行") from exc
    stage = None
    switched = False
    previous = None
    try:
        if read_regular(agents) != original or inventory(origin) != existing or (migrating and inventory(dest) is not None):
            raise InstallError("安装前发现并发改动")
        backup = codex_home / ".relay-backups" / uuid.uuid4().hex
        reject_links(backup)
        backup.mkdir(parents=True, mode=0o700)
        report["backup"] = str(backup)
        if original is not None:
            atomic_write(backup / "AGENTS.md", original, None)
        if existing is not None:
            shutil.copytree(origin, backup / "skill")
        if changed_skill:
            skills_dir.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".relay-stage-", dir=skills_dir))
            for relative, data in desired.items():
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if existing and relative in existing:
                    os.chmod(target, stat.S_IMODE((origin / relative).stat().st_mode))
            if inventory(origin) != existing or (migrating and inventory(dest) is not None):
                raise InstallError("替换前发现 Skill 并发改动")
            if existing is not None:
                previous = skills_dir / (".relay-previous-" + uuid.uuid4().hex)
                os.rename(origin, previous)
            os.rename(stage, dest)
            stage = None
            switched = True
        if changed_agents:
            atomic_write(agents, merged, original)
        if previous is not None:
            if inventory(previous) == existing:
                try:
                    shutil.rmtree(previous)
                    previous = None
                except OSError:
                    report["warnings"].append(f"保留可恢复的旧目录： {previous}")
            else:
                report["warnings"].append(f"旧目录发生并发改动，已保留： {previous}")
        report["status"] = "installed"
        return report
    except BaseException:
        # 只回滚我们尚未被改动的输出，不覆盖用户并发编辑。
        if switched and inventory(dest) == desired:
            shutil.rmtree(dest)
            if previous is not None:
                os.rename(previous, origin)
                previous = None
        elif previous is not None and not origin.exists():
            os.rename(previous, origin)
            previous = None
        raise
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage)
        if previous and previous.exists():
            # 并发编辑阻止回滚时保留可恢复副本。
            report["retained_previous"] = str(previous)
        for lock in acquired:
            lock.unlink()
    # 各文件写入独立原子化；两处写入之间中断可恢复，但不是跨目标事务。


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--skills-dir", type=Path, help="显式指定已确认的用户级 Skill 发现目录")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--check", action="store_true", help="只读检查已安装文件的完整性")
    args = parser.parse_args()
    try:
        chosen = args.skills_dir or default_skills_dir(Path.home(), absolute(args.codex_home))
        result = install(args.source, chosen, args.codex_home, args.check)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (InstallError, OSError, UnicodeError) as exc:
        print(f"安装失败： {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
