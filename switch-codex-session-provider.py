#!/usr/bin/env python3
"""Switch persisted Codex sessions between the custom and openai providers."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


PROVIDERS = ("custom", "openai")
SESSION_DIRS = ("sessions", "archived_sessions")


@dataclass(frozen=True)
class FileUpdate:
    path: Path
    content: bytes
    records: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在 custom 与 openai 之间切换 Codex 会话的 model_provider。"
            "参数表示切换后的目标 provider。"
        )
    )
    parser.add_argument(
        "target",
        choices=PROVIDERS,
        help="目标 provider：custom 表示 openai -> custom，openai 表示 custom -> openai",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
        help="Codex 数据目录（默认：~/.codex）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将发生的修改，不写入文件或数据库",
    )
    return parser.parse_args()


def source_for(target: str) -> str:
    return "openai" if target == "custom" else "custom"


def iter_session_files(codex_home: Path) -> list[Path]:
    files: list[Path] = []
    for directory_name in SESSION_DIRS:
        directory = codex_home / directory_name
        if directory.is_dir():
            files.extend(directory.rglob("*.jsonl"))
    return sorted(files)


def split_line_ending(raw_line: bytes) -> tuple[bytes, bytes]:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2], b"\r\n"
    if raw_line.endswith(b"\n") or raw_line.endswith(b"\r"):
        return raw_line[:-1], raw_line[-1:]
    return raw_line, b""


def plan_file_update(path: Path, source: str, target: str) -> FileUpdate | None:
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    changed_records = 0

    for index, raw_line in enumerate(lines):
        # Provider names can also occur in conversation text. Parse only lines
        # that might be the rollout's session_meta record.
        if b'"session_meta"' not in raw_line or b'"model_provider"' not in raw_line:
            continue

        body, ending = split_line_ending(raw_line)
        try:
            record = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法解析 {path} 第 {index + 1} 行: {exc}") from exc

        if not isinstance(record, dict):
            continue

        payload = record.get("payload")
        if (
            record.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("model_provider") != source
        ):
            continue

        payload["model_provider"] = target
        lines[index] = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + ending
        )
        changed_records += 1

    if not changed_records:
        return None
    return FileUpdate(path=path, content=b"".join(lines), records=changed_records)


def plan_file_updates(
    codex_home: Path, source: str, target: str
) -> tuple[list[Path], list[FileUpdate]]:
    files = iter_session_files(codex_home)
    updates = [
        update
        for path in files
        if (update := plan_file_update(path, source, target)) is not None
    ]
    return files, updates


def atomic_write(update: FileUpdate) -> None:
    mode = stat.S_IMODE(update.path.stat().st_mode)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=update.path.parent,
            prefix=f".{update.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(update.content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chmod(temp_path, mode)
        os.replace(temp_path, update.path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def database_paths(codex_home: Path) -> list[Path]:
    return sorted(codex_home.glob("state_*.sqlite"))


def count_database_rows(path: Path, source: str) -> int:
    with sqlite3.connect(path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone()
        if table is None:
            raise RuntimeError(f"数据库 {path} 中不存在 threads 表")

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
        }
        if "model_provider" not in columns:
            raise RuntimeError(f"数据库 {path} 的 threads 表缺少 model_provider 字段")

        row = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE model_provider = ?", (source,)
        ).fetchone()
        return int(row[0])


def update_database(path: Path, source: str, target: str) -> int:
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider = ?",
            (target, source),
        )
        return cursor.rowcount


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    source = source_for(args.target)
    target = args.target

    if not codex_home.is_dir():
        print(f"错误：Codex 数据目录不存在：{codex_home}", file=sys.stderr)
        return 1

    try:
        session_files, file_updates = plan_file_updates(codex_home, source, target)
        databases = database_paths(codex_home)
        database_counts = {
            path: count_database_rows(path, source) for path in databases
        }
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"预检查失败：{exc}", file=sys.stderr)
        return 1

    record_count = sum(update.records for update in file_updates)
    database_row_count = sum(database_counts.values())
    action = "预计修改" if args.dry_run else "正在修改"
    print(f"切换方向：{source} -> {target}")
    print(
        f"{action} {len(file_updates)}/{len(session_files)} 个会话文件，"
        f"共 {record_count} 条 session_meta 记录。"
    )
    print(
        f"{action} {database_row_count} 条数据库记录"
        f"（扫描 {len(databases)} 个 state_*.sqlite 数据库）。"
    )

    if args.dry_run:
        print("Dry run 完成，未写入任何数据。")
        return 0

    try:
        for update in file_updates:
            atomic_write(update)
        updated_rows = sum(
            update_database(path, source, target)
            for path, count in database_counts.items()
            if count
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"修改失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"切换完成：已修改 {len(file_updates)} 个会话文件、"
        f"{record_count} 条 session_meta 记录和 {updated_rows} 条数据库记录。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
