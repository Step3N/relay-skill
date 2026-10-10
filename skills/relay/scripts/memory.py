#!/usr/bin/env python3
"""只创建缺少的项目记录或读取已有内容，不调用模型或外部服务。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

NAMES = ("PROJECT.md", "HANDOFF.md")


def safe(path):
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError(f"记忆路径含符号链接，停止处理该路径： {part}")


def root_path(project):
    root = Path(os.path.abspath(Path(project).expanduser()))
    safe(root)
    if not root.is_dir():
        raise ValueError(f"项目根目录不存在： {root}")
    return root


def initialize(project):
    root = root_path(project)
    memory = root / ".ai"
    safe(memory)
    if memory.exists() and not memory.is_dir():
        raise ValueError(".ai 已存在，但不是目录；保留原内容，请先核对")
    # 创建文件前先检查两份目标和模板，避免只写了一份才发现另一份异常。
    assets = Path(__file__).absolute().parents[1] / "assets"
    templates = {}
    for name in NAMES:
        target = memory / name
        safe(target)
        if target.exists() and not target.is_file():
            raise ValueError(f"记录路径不是普通文件，未修改： {target}")
        templates[name] = (assets / name.replace(".md", ".template.md")).read_bytes()
    memory.mkdir(exist_ok=True)
    result = {"root": str(root), "created": [], "preserved": []}
    for name in NAMES:
        target = memory / name
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            safe(target)
            if not target.is_file():
                raise ValueError(f"记录路径在操作中变成非普通文件，已停止： {target}")
            result["preserved"].append(name)
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(templates[name])
            handle.flush()
            os.fsync(handle.fileno())
        result["created"].append(name)
    return result


def inspect(project):
    root = root_path(project)
    memory = root / ".ai"
    safe(memory)
    if memory.exists() and not memory.is_dir():
        raise ValueError(".ai 已存在，但不是目录；保留原内容，请先核对")
    result = {"root": str(root), "files": {}}
    for name in NAMES:
        path = memory / name
        safe(path)
        if not path.exists():
            result["files"][name] = {"exists": False}
            continue
        if not path.is_file():
            raise ValueError(f"记录路径不是普通文件，未修改： {path}")
        data = path.read_bytes()
        result["files"][name] = {"exists": True, "sha256": hashlib.sha256(data).hexdigest(),
                                 "content": data.decode("utf-8")}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "inspect"))
    parser.add_argument("--project", required=True, type=Path, help="指定用户实际要处理的项目根目录")
    args = parser.parse_args()
    try:
        result = initialize(args.project) if args.action == "init" else inspect(args.project)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"未能完成项目记录操作： {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
