#!/usr/bin/env python3
"""从本地源码安装或更新 Relay，不联网，只使用 Python 标准库。"""
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
    """检查目标和父目录，避免通过符号链接改到其他位置。"""
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise InstallError(f"路径含符号链接，停止处理该路径： {part}")


def read_regular(path):
    reject_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError(f"该路径必须是文件，不能是目录或其他类型： {path}")
    return path.read_bytes()


def package(source):
    reject_links(source)
    if not source.is_dir():
        raise InstallError(f"找不到 Skill 源目录： {source}")
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise InstallError(f"源码中含符号链接，未执行安装： {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"源码路径不是普通文件： {path}")
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == ".DS_Store" or relative == RECEIPT:
            continue
        files[relative] = path.read_bytes()
    missing = set(REQUIRED) - files.keys()
    if missing:
        raise InstallError(f"Skill 缺少必要文件： {sorted(missing)}")
    source_receipt = read_regular(source / RECEIPT)
    if source_receipt is not None:
        check_owned({**files, RECEIPT: source_receipt})
        owned = json.loads(source_receipt)["files"]
        files = {key: files[key] for key in owned}
    text = files["SKILL.md"].decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise InstallError("SKILL.md 开头的 YAML 格式无效")
    front = text.split("---\n", 2)[1]
    if not re.search(r"^name: relay$", front, re.M):
        raise InstallError("Skill 名称必须是 relay")
    if not re.search(r"^description: \S.+$", front, re.M):
        raise InstallError("缺少 Skill 描述")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if "://" not in target and target not in files:
            raise InstallError(f"SKILL.md 引用的文件不存在： {target}")
    if b"__SKILL_PATH__" not in files["assets/AGENTS.snippet.md"]:
        raise InstallError("全局配置片段缺少 __SKILL_PATH__ 路径占位符")
    return files


def merge_agents(original, snippet):
    """只更新 Relay 专属区域，保留其他内容和换行。"""
    original = original or b""
    original.decode("utf-8")
    snippet.decode("utf-8")
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise InstallError("Relay 配置标记缺失或重复，未修改文件")
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    snippet = snippet.strip(b"\r\n").replace(b"\r\n", b"\n").replace(b"\n", newline)
    if snippet.count(START) != 1 or snippet.count(END) != 1:
        raise InstallError("全局配置片段必须包含一对 Relay 边界标记")
    if START in original:
        start, end = original.index(START), original.index(END)
        if start >= end:
            raise InstallError("Relay 配置的结束标记出现在开始标记之前")
        for token, pos in ((START, start), (END, end)):
            if (pos and original[pos - 1:pos] != b"\n") or original[pos + len(token):pos + len(token) + 1] not in (b"", b"\n", b"\r"):
                raise InstallError("Relay 配置的每个边界标记必须单独占一行")
        return original[:start] + snippet + original[end + len(END):]
    separator = b"" if not original else (newline if original.endswith(b"\n") else newline * 2)
    return original + separator + snippet + newline


def inventory(directory):
    reject_links(directory)
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise InstallError(f"该路径必须是目录： {directory}")
    result = {}
    for path in sorted(directory.rglob("*")):
        reject_links(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError(f"已有安装中含非普通文件，未修改： {path}")
        result[path.relative_to(directory).as_posix()] = path.read_bytes()
    return result


def check_owned(existing):
    if existing is None:
        return
    if RECEIPT not in existing:
        raise InstallError("已有 Skill 缺少 .relay-install.json 安装记录，保留原内容，请先核对来源")
    try:
        receipt = json.loads(existing[RECEIPT])
        hashes = receipt["files"]
        if receipt["schema"] != 1 or receipt["name"] != NAME or not isinstance(hashes, dict) or not hashes:
            raise ValueError("安装记录无效")
        for relative, expected in hashes.items():
            if relative == RECEIPT or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("安装记录中包含不安全的文件路径")
            if relative not in existing or digest(existing[relative]) != expected:
                raise InstallError(f"Relay 安装文件被本地修改或缺失： {relative}；已停止更新，不覆盖原内容")
    except (ValueError, KeyError, TypeError) as exc:
        raise InstallError("安装记录无效，已保留原安装") from exc


def atomic_write(path, data, expected):
    if read_regular(path) != expected:
        raise InstallError(f"文件在读取后又被修改，已停止覆盖： {path}")
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
            raise InstallError(f"文件在读取后又被修改，已停止覆盖： {path}")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def default_skills_dir(home, codex_home):
    modern = home / ".agents" / "skills"
    codex_skills = codex_home / "skills"
    candidates = [p for p in dict.fromkeys((modern, codex_skills))
                  if (p / NAME).exists() or (p / NAME).is_symlink()]
    if len(candidates) > 1:
        raise InstallError("两处用户 Skill 目录都有 Relay，请先确认客户端实际使用的位置")
    return candidates[0] if candidates else modern


def install(source, skills_dir, codex_home, check=False):
    source, skills_dir, codex_home = map(absolute, (source, skills_dir, codex_home))
    dest, agents = skills_dir / NAME, codex_home / "AGENTS.md"
    for path in (dest, agents, codex_home / "AGENTS.override.md"):
        reject_links(path)
    override = read_regular(codex_home / "AGENTS.override.md")
    if override and override.strip():
        raise InstallError("AGENTS.override.md 有内容，AGENTS.md 不会生效；请先确认配置方式")
    files = package(source)
    existing = inventory(dest)
    check_owned(existing)
    original = read_regular(agents)
    slot = str(dest)
    if any(char in slot for char in "`\n\r"):
        raise InstallError("安装路径含反引号或换行，暂时无法安全写入全局配置")
    snippet = files["assets/AGENTS.snippet.md"].replace(b"__SKILL_PATH__", slot.encode())
    merged = merge_agents(original, snippet)
    desired = dict(existing or {})  # 用户自行新增的文件不属于安装文件，更新时保留。
    if existing:
        old_owned = json.loads(existing[RECEIPT])["files"]
        for relative in old_owned:
            desired.pop(relative, None)
        desired.pop(RECEIPT, None)
        collisions = (files.keys() & desired.keys()) - {RECEIPT}
        if collisions:
            raise InstallError(f"新版本文件与用户新增文件同名，已停止更新： {sorted(collisions)}")
    desired.update(files)
    desired[RECEIPT] = (json.dumps({"schema": 1, "name": NAME,
        "files": {key: digest(value) for key, value in files.items()}}, indent=2, sort_keys=True) + "\n").encode()
    changed_skill, changed_agents = existing != desired, original != merged
    report = {"skill_path": str(dest), "global_rules": str(agents),
              "files_validated": len(files),
              "changed_skill": changed_skill,
              "changed_global_rules": changed_agents, "backup": None,
              "discovery": "待验证：请在新 Codex 会话中确认 Relay 可用、全局配置已加载",
              "warnings": ["全局配置超过默认 32 KiB 读取限制，客户端可能无法加载全部内容"] if len(merged) > 32768 else []}
    if check:
        if changed_skill or changed_agents:
            raise InstallError("安装文件或 Relay 专属配置与这份源码不一致，检查未通过")
        report["status"] = "verified-files-only"
        return report
    if not (changed_skill or changed_agents):
        report["status"] = "already-installed"
        return report

    codex_home.mkdir(parents=True, exist_ok=True)
    lock = codex_home / ".relay-install.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError as exc:
        raise InstallError("无法创建安装锁；请检查权限，并确认是否有另一安装操作仍在运行") from exc
    stage = None
    switched = False
    previous = None
    try:
        if read_regular(agents) != original or inventory(dest) != existing:
            raise InstallError("安装前发现文件已被其他操作修改，已停止写入")
        backup = codex_home / ".relay-backups" / uuid.uuid4().hex
        reject_links(backup)
        backup.mkdir(parents=True, mode=0o700)
        report["backup"] = str(backup)
        if original is not None:
            atomic_write(backup / "AGENTS.md", original, None)
        if existing is not None:
            shutil.copytree(dest, backup / "skill")
        if changed_skill:
            skills_dir.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".relay-stage-", dir=skills_dir))
            for relative, data in desired.items():
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if existing and relative in existing:
                    os.chmod(target, stat.S_IMODE((dest / relative).stat().st_mode))
            if inventory(dest) != existing:
                raise InstallError("替换前发现 Skill 已被其他操作修改，已停止覆盖")
            if existing is not None:
                previous = skills_dir / (".relay-previous-" + uuid.uuid4().hex)
                os.rename(dest, previous)
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
                    report["warnings"].append(f"未能清理更新前的目录，已保留： {previous}")
            else:
                report["warnings"].append(f"更新前的目录又被修改，已保留： {previous}")
        report["status"] = "installed"
        return report
    except BaseException:
        # 只恢复本次操作且未被别人改动的内容，避免覆盖用户新编辑。
        if switched and inventory(dest) == desired:
            shutil.rmtree(dest)
            if previous is not None:
                os.rename(previous, dest)
                previous = None
        elif previous is not None and not dest.exists():
            os.rename(previous, dest)
            previous = None
        raise
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage)
        if previous and previous.exists():
            # 发现新编辑、无法安全恢复时，保留原目录供后续核对。
            report["retained_previous"] = str(previous)
        lock.unlink()
    # Skill 目录和全局配置分别替换；中断后可能只完成一处，需要核对备份。


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--skills-dir", type=Path, help="指定已确认的用户 Skill 发现目录")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--check", action="store_true", help="只检查安装文件和 Relay 专属配置，不修改文件")
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
