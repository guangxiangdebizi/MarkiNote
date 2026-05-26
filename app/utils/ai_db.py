"""AI 对话与备份元数据的 SQLite 存储层。"""
import glob
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import current_app


def _now() -> str:
    return datetime.now().isoformat()


def db_path() -> str:
    data_dir = current_app.config["USER_DATA_DIR"]
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "markinote.sqlite3")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_ai_storage() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                PRIMARY KEY (id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                ON conversations(user_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS backup_groups (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_backup_groups_user
                ON backup_groups(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS backup_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                op_index INTEGER NOT NULL,
                type TEXT NOT NULL,
                path TEXT NOT NULL,
                description TEXT,
                has_backup INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(group_id, op_index),
                FOREIGN KEY (group_id) REFERENCES backup_groups(id) ON DELETE CASCADE
            );
            """
        )


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def load_conversation(user_id: str, conv_id: str) -> Optional[Dict[str, Any]]:
    init_ai_storage()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": json.loads(row["messages_json"]),
        }


def save_conversation(user_id: str, conv: Dict[str, Any]) -> None:
    init_ai_storage()
    conv_id = conv["id"]
    updated_at = _now()
    conv["updated_at"] = updated_at
    messages_json = json.dumps(conv.get("messages", []), ensure_ascii=False)
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE conversations
                SET title = ?, updated_at = ?, messages_json = ?
                WHERE id = ? AND user_id = ?
                """,
                (conv.get("title", "新对话")[:50], updated_at, messages_json, conv_id, user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO conversations (id, user_id, title, created_at, updated_at, messages_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_id,
                    user_id,
                    conv.get("title", "新对话")[:50],
                    conv.get("created_at") or updated_at,
                    updated_at,
                    messages_json,
                ),
            )


def list_conversations(user_id: str) -> List[Dict[str, Any]]:
    init_ai_storage()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at, messages_json
            FROM conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    result = []
    for row in rows:
        try:
            messages = json.loads(row["messages_json"])
        except json.JSONDecodeError:
            messages = []
        result.append({
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": len([m for m in messages if m.get("role") in ("user", "assistant")]),
        })
    return result


def delete_conversation(user_id: str, conv_id: str) -> bool:
    init_ai_storage()
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        return cur.rowcount > 0


def update_conversation_title(user_id: str, conv_id: str, title: str) -> bool:
    init_ai_storage()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (title[:50], _now(), conv_id, user_id),
        )
        return cur.rowcount > 0


def create_backup_group(user_id: str, group_id: str, conversation_id: Optional[str] = None) -> None:
    init_ai_storage()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO backup_groups (id, user_id, conversation_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, user_id, conversation_id, _now()),
        )


def load_backup_manifest(group_id: str) -> Optional[Dict[str, Any]]:
    init_ai_storage()
    with connect() as conn:
        group = conn.execute("SELECT * FROM backup_groups WHERE id = ?", (group_id,)).fetchone()
        if not group:
            return None
        ops = conn.execute(
            """
            SELECT op_index, type, path, description, has_backup, created_at
            FROM backup_operations WHERE group_id = ? ORDER BY op_index ASC
            """,
            (group_id,),
        ).fetchall()
    return {
        "id": group["id"],
        "timestamp": group["created_at"],
        "conversation_id": group["conversation_id"],
        "operations": [{
            "index": op["op_index"],
            "type": op["type"],
            "path": op["path"],
            "description": op["description"] or "",
            "has_backup": bool(op["has_backup"]),
            "timestamp": op["created_at"],
        } for op in ops],
    }


def append_backup_operation(group_id: str, operation: Dict[str, Any]) -> None:
    init_ai_storage()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO backup_operations
                (group_id, op_index, type, path, description, has_backup, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                operation["index"],
                operation["type"],
                operation["path"],
                operation.get("description", ""),
                1 if operation.get("has_backup") else 0,
                operation.get("timestamp") or _now(),
            ),
        )


def list_backup_groups(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    init_ai_storage()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM backup_groups WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [m for row in rows if (m := load_backup_manifest(row["id"]))]


def delete_backup_groups_for_conversation(user_id: str, conversation_id: str) -> List[str]:
    init_ai_storage()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM backup_groups WHERE user_id = ? AND conversation_id = ?",
            (user_id, conversation_id),
        ).fetchall()
        group_ids = [row["id"] for row in rows]
        if group_ids:
            conn.execute(
                f"DELETE FROM backup_groups WHERE id IN ({','.join('?' * len(group_ids))})",
                group_ids,
            )
    return group_ids


def cleanup_old_backup_groups(user_id: str, max_count: int = 100) -> List[str]:
    init_ai_storage()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM backup_groups WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
    if len(rows) <= max_count:
        return []
    remove_ids = [row["id"] for row in rows[: len(rows) - max_count]]
    with connect() as conn:
        conn.execute(
            f"DELETE FROM backup_groups WHERE id IN ({','.join('?' * len(remove_ids))})",
            remove_ids,
        )
    return remove_ids


def _import_conversation_json(user_id: str, path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            conv = json.load(f)
    except Exception:
        return
    if not conv.get("id") or load_conversation(user_id, conv["id"]):
        return
    save_conversation(user_id, conv)


def _import_backup_manifest(user_id: str, group_dir: str) -> None:
    manifest_path = os.path.join(group_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return
    group_id = manifest.get("id") or os.path.basename(group_dir)
    if load_backup_manifest(group_id):
        return
    create_backup_group(user_id, group_id, manifest.get("conversation_id"))
    for op in manifest.get("operations", []):
        append_backup_operation(group_id, op)


def migrate_legacy_json_storage() -> None:
    init_ai_storage()
    with connect() as conn:
        if _meta_get(conn, "json_migration_v1") == "done":
            return

    user_data = current_app.config["USER_DATA_DIR"]
    users_root = os.path.join(user_data, "users")
    if os.path.isdir(users_root):
        for user_id in os.listdir(users_root):
            user_path = os.path.join(users_root, user_id)
            if not os.path.isdir(user_path):
                continue
            conv_dir = os.path.join(user_path, "conversations")
            if os.path.isdir(conv_dir):
                for path in glob.glob(os.path.join(conv_dir, "*.json")):
                    _import_conversation_json(user_id, path)
            backup_dir = os.path.join(user_path, "backups")
            if os.path.isdir(backup_dir):
                for name in os.listdir(backup_dir):
                    group_dir = os.path.join(backup_dir, name)
                    if os.path.isdir(group_dir):
                        _import_backup_manifest(user_id, group_dir)

    legacy_conv_dir = os.path.abspath(os.path.join(current_app.root_path, "..", ".ai_conversations"))
    if os.path.isdir(legacy_conv_dir) and os.path.isdir(users_root):
        users = [u for u in os.listdir(users_root) if os.path.isdir(os.path.join(users_root, u))]
        if len(users) == 1:
            for path in glob.glob(os.path.join(legacy_conv_dir, "*.json")):
                _import_conversation_json(users[0], path)

    with connect() as conn:
        _meta_set(conn, "json_migration_v1", "done")
