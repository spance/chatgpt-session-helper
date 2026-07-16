#!/usr/bin/env python3
"""
Safely rename/move a local Codex project directory and migrate its persisted
workspace/session path references.

Targets:
  - ~/.codex/state_*.sqlite                  threads.cwd
  - ~/.codex/sessions/**/*.jsonl             path-valued metadata
  - ~/.codex/archived_sessions/**/*.jsonl    path-valued metadata
  - ~/.codex/session_index.jsonl
  - ~/.codex/.codex-global-state.json
  - ~/.codex/.codex-global-state.json.bak
  - ~/.codex/config.toml

The script:
  1. Refuses to run while Codex appears active (unless overridden).
  2. Preflights SQLite and JSON/JSONL files.
  3. Creates a consistent backup under ~/.codex/backups/.
  4. Renames the project directory.
  5. Migrates path references atomically.
  6. Verifies the result.
  7. Automatically rolls back on failure.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCRIPT_VERSION = "1.0.0"
BACKUP_DIR_NAME = "backups"

# Keys whose values are expected to contain filesystem paths. The normalized
# comparison also catches camelCase, snake_case and kebab-case spellings.
PATH_KEY_NAMES = {
    "cwd",
    "workdir",
    "workingdir",
    "workingdirectory",
    "directory",
    "directories",
    "dir",
    "dirs",
    "folder",
    "folders",
    "path",
    "paths",
    "root",
    "roots",
    "projectpath",
    "projectroot",
    "projectroots",
    "workspace",
    "workspaces",
    "workspacepath",
    "workspacepaths",
    "workspaceroot",
    "workspaceroots",
    "writableroot",
    "writableroots",
    "readonlyroot",
    "readonlyroots",
    "adddir",
    "adddirs",
    "selectedroot",
    "selectedroots",
    "capabilityroot",
    "capabilityroots",
}

KNOWN_JSON_FILES = (
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
)

KNOWN_JSONL_FILES = (
    "session_index.jsonl",
)

KNOWN_TEXT_FILES = (
    "config.toml",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PathPair:
    old: str
    new: str


class PathMapper:
    """
    Replace an exact project path or a descendant path while respecting path
    component boundaries. Includes a Windows native-namespace variant when
    applicable.
    """

    def __init__(self, old: Path, new: Path):
        old_s = str(old)
        new_s = str(new)
        pairs: list[PathPair] = [PathPair(old_s, new_s)]

        # Windows spelling variants. They are harmless on POSIX and useful when
        # SQLite and rollout JSONL disagree about \\?\ prefixes or separators.
        if re.match(r"^[A-Za-z]:[\\/]", old_s):
            old_back = old_s.replace("/", "\\")
            new_back = new_s.replace("/", "\\")
            pairs.append(PathPair(old_back, new_back))
            pairs.append(PathPair(r"\\?\{}".format(old_back), r"\\?\{}".format(new_back)))

            old_forward = old_s.replace("\\", "/")
            new_forward = new_s.replace("\\", "/")
            pairs.append(PathPair(old_forward, new_forward))

        # Longest variant first so a namespace-prefixed path wins.
        unique: dict[str, PathPair] = {}
        for pair in pairs:
            unique[pair.old] = pair
        self.pairs = tuple(sorted(unique.values(), key=lambda p: len(p.old), reverse=True))

    @staticmethod
    def _boundary_match(value: str, prefix: str) -> bool:
        if value == prefix:
            return True
        if not value.startswith(prefix):
            return False
        if prefix.endswith(("/", "\\")):
            return True
        return len(value) > len(prefix) and value[len(prefix)] in ("/", "\\")

    def replace_path(self, value: str) -> tuple[str, bool]:
        for pair in self.pairs:
            if self._boundary_match(value, pair.old):
                return pair.new + value[len(pair.old):], True
        return value, False

    def exact_match(self, value: str) -> bool:
        return any(value == pair.old for pair in self.pairs)

    def raw_text_replace(self, text: str) -> tuple[str, int]:
        """
        Broad textual replacement for config.toml only. Handles raw and escaped
        backslash spellings.
        """
        total = 0
        result = text
        candidates: list[PathPair] = list(self.pairs)
        for pair in self.pairs:
            if "\\" in pair.old:
                candidates.append(
                    PathPair(pair.old.replace("\\", "\\\\"), pair.new.replace("\\", "\\\\"))
                )

        seen: set[tuple[str, str]] = set()
        for pair in sorted(candidates, key=lambda p: len(p.old), reverse=True):
            key = (pair.old, pair.new)
            if key in seen or pair.old == pair.new:
                continue
            seen.add(key)
            count = result.count(pair.old)
            if count:
                result = result.replace(pair.old, pair.new)
                total += count
        return result, total

    def raw_line_might_contain_old_path(self, line: str) -> bool:
        for pair in self.pairs:
            if pair.old in line:
                return True
            if "\\" in pair.old and pair.old.replace("\\", "\\\\") in line:
                return True
        return False


@dataclass
class FilePlan:
    path: Path
    kind: str
    broad: bool
    changes: int
    parse_errors: int = 0


@dataclass
class DatabasePlan:
    path: Path
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    integrity: str = ""


@dataclass
class MigrationPlan:
    databases: list[DatabasePlan]
    files: list[FilePlan]
    affected_thread_ids: set[str]

    @property
    def total_db_rows(self) -> int:
        return sum(len(db.rows) for db in self.databases)

    @property
    def total_file_changes(self) -> int:
        return sum(item.changes for item in self.files)


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def expand_absolute(raw: str, *, relative_to: Path | None = None) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        base = relative_to if relative_to is not None else Path.cwd()
        path = base / path
    return Path(os.path.abspath(str(path)))


def resolve_destination(old: Path, raw_new: str) -> Path:
    """
    A plain basename is interpreted as a sibling rename. Any relative path that
    contains a separator is interpreted relative to the current directory.
    """
    expanded = os.path.expandvars(os.path.expanduser(raw_new))
    candidate = Path(expanded)

    if not candidate.is_absolute() and len(candidate.parts) == 1:
        candidate = old.parent / candidate
    elif not candidate.is_absolute():
        candidate = Path.cwd() / candidate

    return Path(os.path.abspath(str(candidate)))


def normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_path_key(key: object) -> bool:
    normalized = normalize_key(key)
    if normalized in PATH_KEY_NAMES:
        return True
    return normalized.endswith(("cwd", "path", "paths", "root", "roots", "dir", "dirs", "directory", "directories"))


def transform_json_value(
    value: Any,
    mapper: PathMapper,
    *,
    broad: bool,
    path_context: bool = False,
) -> tuple[Any, int]:
    """
    Recursively transform paths.

    broad=True:
      Replace any string that is exactly the old project path or a descendant.
      Used for global state and lightweight metadata.

    broad=False:
      Replace only under path-like keys, plus exact string matches anywhere.
      Used for conversation JSONL so normal prose is not rewritten.
    """
    if isinstance(value, dict):
        changed = 0
        output: dict[Any, Any] = {}
        for key, child in value.items():
            new_key = key
            if isinstance(key, str):
                replaced_key, key_changed = mapper.replace_path(key)
                if key_changed:
                    new_key = replaced_key
                    changed += 1

            child_context = path_context or is_path_key(key)
            new_child, child_changes = transform_json_value(
                child,
                mapper,
                broad=broad,
                path_context=child_context,
            )
            output[new_key] = new_child
            changed += child_changes
        return output, changed

    if isinstance(value, list):
        output_list: list[Any] = []
        changed = 0
        for child in value:
            new_child, child_changes = transform_json_value(
                child,
                mapper,
                broad=broad,
                path_context=path_context,
            )
            output_list.append(new_child)
            changed += child_changes
        return output_list, changed

    if isinstance(value, str):
        should_try = broad or path_context or mapper.exact_match(value)
        if should_try:
            replaced, did_change = mapper.replace_path(value)
            return replaced, int(did_change)

    return value, 0


def iter_session_files(codex_home: Path) -> Iterator[Path]:
    for root_name in ("sessions", "archived_sessions"):
        root = codex_home / root_name
        if not root.is_dir():
            continue
        yield from sorted(root.rglob("*.jsonl"))


def inspect_jsonl(path: Path, mapper: PathMapper, *, broad: bool) -> tuple[int, int]:
    changes = 0
    parse_errors = 0

    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors += 1
                if mapper.raw_line_might_contain_old_path(line):
                    raise MigrationError(
                        f"{path}:{line_number}: invalid JSON contains the old path; "
                        f"refusing an unsafe textual edit: {exc}"
                    ) from exc
                continue
            _, line_changes = transform_json_value(obj, mapper, broad=broad)
            changes += line_changes

    return changes, parse_errors


def rewrite_jsonl(path: Path, mapper: PathMapper, *, broad: bool) -> int:
    mode = path.stat().st_mode
    changes = 0
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")

    try:
        with path.open("r", encoding="utf-8", newline="") as source, temp_path.open(
            "w", encoding="utf-8", newline="\n"
        ) as target:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    target.write(line)
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    if mapper.raw_line_might_contain_old_path(line):
                        raise MigrationError(
                            f"{path}:{line_number}: invalid JSON contains the old path: {exc}"
                        ) from exc
                    target.write(line)
                    continue

                transformed, line_changes = transform_json_value(obj, mapper, broad=broad)
                if line_changes:
                    target.write(
                        json.dumps(
                            transformed,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    target.write("\n")
                    changes += line_changes
                else:
                    target.write(line)

            target.flush()
            os.fsync(target.fileno())

        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        fsync_directory(path.parent)
        return changes
    finally:
        if temp_path.exists():
            temp_path.unlink()


def inspect_json_file(path: Path, mapper: PathMapper, *, broad: bool) -> int:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path}: invalid JSON: {exc}") from exc
    _, changes = transform_json_value(obj, mapper, broad=broad)
    return changes


def rewrite_json_file(path: Path, mapper: PathMapper, *, broad: bool) -> int:
    original = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(original)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path}: invalid JSON: {exc}") from exc

    transformed, changes = transform_json_value(obj, mapper, broad=broad)
    if not changes:
        return 0

    multiline = "\n" in original.rstrip("\n")
    trailing_newline = original.endswith("\n")
    if multiline:
        rendered = json.dumps(transformed, ensure_ascii=False, indent=2)
    else:
        rendered = json.dumps(transformed, ensure_ascii=False, separators=(",", ":"))
    if trailing_newline:
        rendered += "\n"

    atomic_write_text(path, rendered)
    return changes


def inspect_text_file(path: Path, mapper: PathMapper) -> int:
    text = path.read_text(encoding="utf-8")
    _, changes = mapper.raw_text_replace(text)
    return changes


def rewrite_text_file(path: Path, mapper: PathMapper) -> int:
    text = path.read_text(encoding="utf-8")
    transformed, changes = mapper.raw_text_replace(text)
    if changes:
        atomic_write_text(path, transformed)
    return changes


def atomic_write_text(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def discover_state_databases(codex_home: Path) -> list[Path]:
    candidates = set(codex_home.glob("state_*.sqlite"))
    for name in ("state.sqlite",):
        path = codex_home / name
        if path.exists():
            candidates.add(path)
    return sorted(path for path in candidates if path.is_file())


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
    return {str(row[1]) for row in rows}


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def inspect_database(path: Path, mapper: PathMapper) -> DatabasePlan:
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "unknown"
        if integrity.lower() != "ok":
            raise MigrationError(f"{path}: SQLite integrity_check failed: {integrity}")

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "threads" not in tables:
            return DatabasePlan(path=path, integrity=integrity)

        columns = table_columns(connection, "threads")
        if not {"id", "cwd"}.issubset(columns):
            raise MigrationError(
                f"{path}: threads table lacks required id/cwd columns: {sorted(columns)}"
            )

        rows: list[tuple[str, str, str]] = []
        for thread_id, cwd in connection.execute(
            "SELECT id, cwd FROM threads WHERE cwd IS NOT NULL"
        ):
            if not isinstance(cwd, str):
                continue
            replacement, changed = mapper.replace_path(cwd)
            if changed:
                rows.append((str(thread_id), cwd, replacement))

        return DatabasePlan(path=path, rows=rows, integrity=integrity)
    finally:
        connection.close()


def update_database(plan: DatabasePlan) -> int:
    if not plan.rows:
        return 0

    connection = sqlite3.connect(plan.path, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        updated = 0
        for thread_id, old_cwd, new_cwd in plan.rows:
            cursor = connection.execute(
                "UPDATE threads SET cwd=? WHERE id=? AND cwd=?",
                (new_cwd, thread_id, old_cwd),
            )
            if cursor.rowcount != 1:
                raise MigrationError(
                    f"{plan.path}: expected to update one row for thread {thread_id}, "
                    f"updated {cursor.rowcount}"
                )
            updated += 1
        connection.commit()

        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity_row or str(integrity_row[0]).lower() != "ok":
            raise MigrationError(
                f"{plan.path}: integrity_check failed after update: {integrity_row}"
            )

        # Fold a WAL into the main database while Codex is stopped.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sqlite_consistent_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def backup_files(
    codex_home: Path,
    backup_dir: Path,
    database_paths: Sequence[Path],
    ordinary_paths: Sequence[Path],
) -> None:
    for source in database_paths:
        relative = source.relative_to(codex_home)
        sqlite_consistent_backup(source, backup_dir / "files" / relative)

    for source in ordinary_paths:
        relative = source.relative_to(codex_home)
        destination = backup_dir / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def restore_files_from_backup(
    codex_home: Path,
    backup_dir: Path,
    file_relpaths: Sequence[str],
    database_relpaths: Sequence[str],
) -> None:
    for relative_raw in file_relpaths:
        relative = Path(relative_raw)
        backup = backup_dir / "files" / relative
        destination = codex_home / relative
        if not backup.exists():
            raise MigrationError(f"Backup file missing: {backup}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, destination)

    for relative_raw in database_relpaths:
        relative = Path(relative_raw)
        backup = backup_dir / "files" / relative
        destination = codex_home / relative
        if not backup.exists():
            raise MigrationError(f"Backup database missing: {backup}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(destination) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        shutil.copy2(backup, destination)


def make_manifest(
    *,
    old: Path,
    new: Path,
    codex_home: Path,
    metadata_only: bool,
    plan: MigrationPlan,
) -> dict[str, Any]:
    ordinary = [str(item.path.relative_to(codex_home)) for item in plan.files]
    databases = [str(item.path.relative_to(codex_home)) for item in plan.databases if item.rows]
    return {
        "format": 1,
        "script_version": SCRIPT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "planned",
        "old_path": str(old),
        "new_path": str(new),
        "codex_home": str(codex_home),
        "metadata_only": metadata_only,
        "folder_renamed": False,
        "ordinary_files": ordinary,
        "database_files": databases,
        "affected_thread_ids": sorted(plan.affected_thread_ids),
        "db_rows": plan.total_db_rows,
        "file_replacements": plan.total_file_changes,
    }


def write_manifest(backup_dir: Path, manifest: dict[str, Any]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / "manifest.json"
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        atomic_write_text(path, rendered)
    else:
        with path.open("w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)


def find_codex_processes() -> list[str]:
    """
    Best-effort process check. False negatives are possible on restricted
    systems; the backup and SQLite locking checks remain the final safeguards.
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []
        matches = []
        for line in result.stdout.splitlines():
            lowered = line.lower()
            if '"codex.exe"' in lowered or '"codex app.exe"' in lowered:
                matches.append(line)
        return matches

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []

    current_pid = os.getpid()
    parent_pid = os.getppid()
    patterns = (
        "/Codex.app/",
        "codex app-server",
        "codex-app-server",
        "codex desktop",
    )
    matches: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first, _, command = stripped.partition(" ")
        try:
            pid = int(first)
        except ValueError:
            continue
        if pid in (current_pid, parent_pid):
            continue

        lowered = command.lower()
        if any(pattern.lower() in lowered for pattern in patterns):
            matches.append(stripped)
            continue

        # Catch a standalone Codex CLI/TUI process, but not this script.
        if re.search(r"(^|[/\s])codex(?:\s|$)", command) and "codex_project_rename.py" not in command:
            matches.append(stripped)

    return matches


def validate_paths(
    old: Path,
    new: Path,
    *,
    metadata_only: bool,
) -> tuple[bool, Path | None]:
    if old == new:
        raise MigrationError("Old and new paths are identical.")

    if new == old or old in new.parents:
        raise MigrationError("The destination cannot be inside the source directory.")

    case_only_same_entry = False
    temporary_case_path: Path | None = None

    if metadata_only:
        if not new.is_dir():
            raise MigrationError(
                f"--metadata-only requires the renamed destination to exist: {new}"
            )
        return False, None

    if not old.is_dir():
        raise MigrationError(f"Source project directory does not exist: {old}")

    if not new.parent.is_dir():
        raise MigrationError(f"Destination parent directory does not exist: {new.parent}")

    if new.exists():
        try:
            case_only_same_entry = os.path.samefile(old, new)
        except OSError:
            case_only_same_entry = False

        if not case_only_same_entry:
            raise MigrationError(f"Destination already exists: {new}")

        # Case-only rename on a case-insensitive filesystem.
        temporary_case_path = old.parent / f".{old.name}.codex-rename-{os.getpid()}"
        if temporary_case_path.exists():
            raise MigrationError(f"Temporary rename path already exists: {temporary_case_path}")

    return case_only_same_entry, temporary_case_path


def rename_project(
    old: Path,
    new: Path,
    *,
    case_only_same_entry: bool,
    temporary_case_path: Path | None,
) -> None:
    try:
        if case_only_same_entry:
            assert temporary_case_path is not None
            os.rename(old, temporary_case_path)
            try:
                os.rename(temporary_case_path, new)
            except Exception:
                os.rename(temporary_case_path, old)
                raise
        else:
            os.rename(old, new)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise MigrationError(
                "Source and destination are on different filesystems. "
                "This script intentionally refuses a non-atomic cross-device move. "
                "Move/copy the project yourself, then rerun with --metadata-only."
            ) from exc
        raise


def rollback_folder(old: Path, new: Path) -> None:
    if old.exists():
        return
    if not new.exists():
        return
    old.parent.mkdir(parents=True, exist_ok=True)
    os.rename(new, old)


def collect_file_plans(codex_home: Path, mapper: PathMapper) -> list[FilePlan]:
    plans: list[FilePlan] = []

    for name in KNOWN_JSON_FILES:
        path = codex_home / name
        if path.is_file():
            changes = inspect_json_file(path, mapper, broad=True)
            if changes:
                plans.append(FilePlan(path=path, kind="json", broad=True, changes=changes))

    for name in KNOWN_JSONL_FILES:
        path = codex_home / name
        if path.is_file():
            changes, errors = inspect_jsonl(path, mapper, broad=True)
            if changes:
                plans.append(
                    FilePlan(
                        path=path,
                        kind="jsonl",
                        broad=True,
                        changes=changes,
                        parse_errors=errors,
                    )
                )

    for name in KNOWN_TEXT_FILES:
        path = codex_home / name
        if path.is_file():
            changes = inspect_text_file(path, mapper)
            if changes:
                plans.append(FilePlan(path=path, kind="text", broad=True, changes=changes))

    for path in iter_session_files(codex_home):
        changes, errors = inspect_jsonl(path, mapper, broad=False)
        if changes:
            plans.append(
                FilePlan(
                    path=path,
                    kind="jsonl",
                    broad=False,
                    changes=changes,
                    parse_errors=errors,
                )
            )

    return plans


def build_plan(codex_home: Path, mapper: PathMapper) -> MigrationPlan:
    database_plans: list[DatabasePlan] = []
    affected_thread_ids: set[str] = set()

    for path in discover_state_databases(codex_home):
        plan = inspect_database(path, mapper)
        if plan.rows:
            database_plans.append(plan)
            affected_thread_ids.update(row[0] for row in plan.rows)

    file_plans = collect_file_plans(codex_home, mapper)
    return MigrationPlan(
        databases=database_plans,
        files=file_plans,
        affected_thread_ids=affected_thread_ids,
    )


def apply_file_plan(item: FilePlan, mapper: PathMapper) -> int:
    if item.kind == "jsonl":
        return rewrite_jsonl(item.path, mapper, broad=item.broad)
    if item.kind == "json":
        return rewrite_json_file(item.path, mapper, broad=item.broad)
    if item.kind == "text":
        return rewrite_text_file(item.path, mapper)
    raise MigrationError(f"Unknown file plan kind: {item.kind}")


def verify_database_paths(
    database_paths: Sequence[Path],
    mapper: PathMapper,
    expected_new_prefix: str,
) -> tuple[int, list[str]]:
    old_remaining = 0
    problems: list[str] = []

    for path in database_paths:
        plan = inspect_database(path, mapper)
        old_remaining += len(plan.rows)

        connection = sqlite3.connect(path)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                problems.append(f"{path}: integrity_check={result}")
        finally:
            connection.close()

    return old_remaining, problems


def verify_files(file_plans: Sequence[FilePlan], mapper: PathMapper) -> tuple[int, list[str]]:
    remaining = 0
    problems: list[str] = []

    for item in file_plans:
        try:
            if item.kind == "jsonl":
                changes, _ = inspect_jsonl(item.path, mapper, broad=item.broad)
            elif item.kind == "json":
                changes = inspect_json_file(item.path, mapper, broad=item.broad)
            elif item.kind == "text":
                changes = inspect_text_file(item.path, mapper)
            else:
                problems.append(f"{item.path}: unknown kind {item.kind}")
                continue
            remaining += changes
        except Exception as exc:
            problems.append(f"{item.path}: {exc}")

    return remaining, problems


def print_plan(old: Path, new: Path, codex_home: Path, plan: MigrationPlan, metadata_only: bool) -> None:
    print(f"Codex project path migration {SCRIPT_VERSION}")
    print(f"  old:        {old}")
    print(f"  new:        {new}")
    print(f"  codex home: {codex_home}")
    print(f"  mode:       {'metadata only' if metadata_only else 'rename + metadata'}")
    print(f"  DB rows:    {plan.total_db_rows}")
    print(f"  files:      {len(plan.files)}")
    print(f"  replacements in files: {plan.total_file_changes}")

    if plan.databases:
        print("\nSQLite:")
        for db in plan.databases:
            print(f"  {db.path}: {len(db.rows)} thread row(s), integrity={db.integrity}")

    if plan.files:
        print("\nMetadata files:")
        for item in plan.files:
            note = f", existing parse errors={item.parse_errors}" if item.parse_errors else ""
            print(f"  {item.path}: {item.changes} replacement(s){note}")


def perform_migration(args: argparse.Namespace) -> int:
    codex_home = expand_absolute(args.codex_home)
    old = expand_absolute(args.old)
    new = resolve_destination(old, args.new)

    if not codex_home.is_dir():
        raise MigrationError(f"Codex home does not exist: {codex_home}")

    case_only_same_entry, temporary_case_path = validate_paths(
        old,
        new,
        metadata_only=args.metadata_only,
    )

    if not args.allow_running:
        processes = find_codex_processes()
        if processes:
            joined = "\n  ".join(processes)
            raise MigrationError(
                "Codex appears to be running. Fully quit Codex Desktop/CLI first.\n"
                f"  {joined}\n"
                "Use --allow-running only if this detection is a false positive."
            )

    mapper = PathMapper(old, new)
    plan = build_plan(codex_home, mapper)
    print_plan(old, new, codex_home, plan, args.metadata_only)

    if plan.total_db_rows == 0 and plan.total_file_changes == 0:
        raise MigrationError(
            "No Codex metadata references to the old path were found. "
            "Check the exact absolute path spelling/case and --codex-home."
        )

    if args.dry_run:
        print("\nDry run only; no files or directories were changed.")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = codex_home / BACKUP_DIR_NAME / f"project-rename-{timestamp}-{os.getpid()}"
    manifest = make_manifest(
        old=old,
        new=new,
        codex_home=codex_home,
        metadata_only=args.metadata_only,
        plan=plan,
    )
    write_manifest(backup_dir, manifest)

    database_paths = [db.path for db in plan.databases]
    ordinary_paths = [item.path for item in plan.files]
    backup_files(codex_home, backup_dir, database_paths, ordinary_paths)

    renamed = False
    try:
        if not args.metadata_only:
            rename_project(
                old,
                new,
                case_only_same_entry=case_only_same_entry,
                temporary_case_path=temporary_case_path,
            )
            renamed = True
            manifest["folder_renamed"] = True
            manifest["status"] = "folder-renamed"
            write_manifest(backup_dir, manifest)

        updated_rows = 0
        for db_plan in plan.databases:
            updated_rows += update_database(db_plan)

        updated_file_values = 0
        for file_plan in plan.files:
            updated_file_values += apply_file_plan(file_plan, mapper)

        if not new.is_dir():
            raise MigrationError(f"Destination project directory is missing after migration: {new}")

        old_db_remaining, db_problems = verify_database_paths(database_paths, mapper, str(new))
        old_file_remaining, file_problems = verify_files(plan.files, mapper)

        if old_db_remaining or old_file_remaining or db_problems or file_problems:
            details = [
                f"remaining old DB paths: {old_db_remaining}",
                f"remaining old file paths: {old_file_remaining}",
                *db_problems,
                *file_problems,
            ]
            raise MigrationError("Post-migration verification failed:\n  " + "\n  ".join(details))

        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["updated_db_rows"] = updated_rows
        manifest["updated_file_values"] = updated_file_values
        write_manifest(backup_dir, manifest)

        print("\nMigration complete.")
        print(f"  renamed directory: {'yes' if renamed else 'no (--metadata-only)'}")
        print(f"  updated DB rows:   {updated_rows}")
        print(f"  updated path values in files: {updated_file_values}")
        print(f"  backup: {backup_dir}")
        print("\nYou may now reopen Codex and open the new project path.")
        return 0

    except Exception as exc:
        eprint(f"\nMigration failed: {exc}")
        eprint("Attempting automatic rollback...")

        rollback_errors: list[str] = []
        try:
            restore_files_from_backup(
                codex_home,
                backup_dir,
                manifest["ordinary_files"],
                manifest["database_files"],
            )
        except Exception as restore_exc:
            rollback_errors.append(f"metadata restore failed: {restore_exc}")

        if renamed:
            try:
                rollback_folder(old, new)
            except Exception as rename_exc:
                rollback_errors.append(f"project directory rollback failed: {rename_exc}")

        manifest["status"] = "rollback-failed" if rollback_errors else "rolled-back"
        manifest["failure"] = str(exc)
        manifest["rollback_errors"] = rollback_errors
        try:
            write_manifest(backup_dir, manifest)
        except Exception as manifest_exc:
            rollback_errors.append(f"manifest update failed: {manifest_exc}")

        if rollback_errors:
            raise MigrationError(
                f"Migration failed and rollback was incomplete. Backup: {backup_dir}\n"
                + "\n".join(rollback_errors)
            ) from exc

        raise MigrationError(
            f"Migration failed, but the original directory and metadata were restored. "
            f"Backup retained at: {backup_dir}"
        ) from exc


def perform_restore(args: argparse.Namespace) -> int:
    backup_dir = expand_absolute(args.backup)
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise MigrationError(f"Backup manifest not found: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid backup manifest: {manifest_path}: {exc}") from exc

    codex_home = expand_absolute(str(manifest["codex_home"]))
    old = expand_absolute(str(manifest["old_path"]))
    new = expand_absolute(str(manifest["new_path"]))

    if not args.allow_running:
        processes = find_codex_processes()
        if processes:
            joined = "\n  ".join(processes)
            raise MigrationError(
                "Codex appears to be running. Fully quit it before restore.\n"
                f"  {joined}"
            )

    restore_files_from_backup(
        codex_home,
        backup_dir,
        manifest.get("ordinary_files", []),
        manifest.get("database_files", []),
    )

    if manifest.get("folder_renamed"):
        if old.exists() and new.exists():
            raise MigrationError(
                f"Both old and new project paths exist; refusing to choose during restore:\n"
                f"  old: {old}\n  new: {new}"
            )
        if not old.exists() and new.exists():
            rollback_folder(old, new)

    manifest["status"] = "manually-restored"
    manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(backup_dir, manifest)

    print("Restore complete.")
    print(f"  backup: {backup_dir}")
    print(f"  project: {old if old.exists() else new}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rename/move a local Codex project and migrate all known local "
            "workspace/session path references."
        )
    )
    parser.add_argument("--version", action="version", version=SCRIPT_VERSION)

    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser(
        "migrate",
        help="Rename the project and migrate Codex metadata.",
    )
    migrate.add_argument("old", help="Existing absolute or relative project directory.")
    migrate.add_argument(
        "new",
        help=(
            "New directory basename or path. A plain basename renames within "
            "the same parent directory."
        ),
    )
    migrate.add_argument(
        "--codex-home",
        default="~/.codex",
        help="Codex state directory (default: ~/.codex).",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show exactly what would change without modifying anything.",
    )
    migrate.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "Do not rename the directory. Use when the project has already been "
            "renamed/moved and only Codex metadata still contains the old path."
        ),
    )
    migrate.add_argument(
        "--allow-running",
        action="store_true",
        help="Override the best-effort Codex process check (not recommended).",
    )
    migrate.set_defaults(func=perform_migration)

    restore = subparsers.add_parser(
        "restore",
        help="Restore metadata and, when applicable, the old folder name from a backup.",
    )
    restore.add_argument("backup", help="Backup directory containing manifest.json.")
    restore.add_argument(
        "--allow-running",
        action="store_true",
        help="Override the best-effort Codex process check (not recommended).",
    )
    restore.set_defaults(func=perform_restore)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MigrationError as exc:
        eprint(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
