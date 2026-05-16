"""Local email-code auth and per-user workspace helpers."""

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from typing import List, Optional, Tuple

from flask import current_app, jsonify, session


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return datetime.now().isoformat()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _db_path() -> str:
    data_dir = current_app.config["USER_DATA_DIR"]
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "auth.sqlite3")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_storage() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def code_hash(email: str, code: str) -> str:
    raw = f"{normalize_email(email)}:{code}:{current_app.config['SECRET_KEY']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_email_code(email: str, purpose: str = "login") -> str:
    init_auth_storage()
    code = f"{secrets.randbelow(1000000):06d}"
    expires_at = (datetime.now() + timedelta(minutes=10)).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO email_codes (email, code_hash, purpose, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (normalize_email(email), code_hash(email, code), purpose, expires_at, _now()),
        )
    return code


def verify_email_code(email: str, code: str, purpose: str = "login") -> Tuple[bool, str]:
    email = normalize_email(email)
    code = (code or "").strip()
    if not EMAIL_RE.match(email):
        return False, "邮箱格式不正确"
    if not re.fullmatch(r"\d{6}", code):
        return False, "验证码格式不正确"

    init_auth_storage()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_codes
            WHERE email = ? AND purpose = ? AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (email, purpose),
        ).fetchone()
        if not row:
            return False, "请先获取验证码"
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return False, "验证码已过期"
        if row["attempts"] >= 5:
            return False, "验证码尝试次数过多，请重新获取"

        if row["code_hash"] != code_hash(email, code):
            conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            return False, "验证码不正确"

        conn.execute("UPDATE email_codes SET used_at = ? WHERE id = ?", (_now(), row["id"]))
    return True, "验证成功"


def send_email_code(email: str, code: str) -> Tuple[bool, str]:
    """Send a verification code if SMTP is configured; otherwise log it."""
    host = os.environ.get("MARKINOTE_SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("MARKINOTE_SMTP_PORT", "465"))
    user = os.environ.get("MARKINOTE_SMTP_USER") or os.environ.get("MARKINOTE_QQMAIL_USER")
    password = os.environ.get("MARKINOTE_SMTP_PASS") or os.environ.get("MARKINOTE_QQMAIL_AUTH_CODE")
    sender = os.environ.get("MARKINOTE_SMTP_FROM") or user

    if not user or not password or not sender:
        current_app.logger.warning("Finote email code for %s: %s", email, code)
        print(f"[Finote auth] email code for {email}: {code}", flush=True)
        return False, "SMTP 未配置，验证码已写入服务日志"

    msg = EmailMessage()
    msg["Subject"] = "Finote 登录验证码"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(f"你的 Finote 登录验证码是：{code}\n\n验证码 10 分钟内有效。")

    import smtplib

    with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return True, "验证码已发送到邮箱"


def get_or_create_user(email: str) -> dict:
    email = normalize_email(email)
    init_auth_storage()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), row["id"]))
            user = dict(row)
        else:
            user_id = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO users (id, email, created_at, last_login_at) VALUES (?, ?, ?, ?)",
                (user_id, email, _now(), _now()),
            )
            user = {"id": user_id, "email": email}
    ensure_user_workspace(user["id"])
    return user


def current_user() -> Optional[dict]:
    user_id = session.get("user_id")
    email = session.get("email")
    if not user_id or not email:
        return None
    return {"id": user_id, "email": email}


def is_logged_in() -> bool:
    return current_user() is not None


def _library_templates() -> List[str]:
    template_dir = current_app.config["LIBRARY_FOLDER"]
    candidates = ["Welcome.md", "Welcome-EN.md", "README.md", "README_EN.md"]
    return [name for name in candidates if os.path.isfile(os.path.join(template_dir, name))]


def user_root(user_id: Optional[str] = None) -> str:
    uid = user_id or session.get("user_id")
    if not uid:
        raise RuntimeError("missing user id")
    return os.path.join(current_app.config["USER_DATA_DIR"], "users", uid)


def ensure_user_workspace(user_id: str) -> None:
    root = user_root(user_id)
    lib_dir = os.path.join(root, "library")
    os.makedirs(lib_dir, exist_ok=True)

    template_dir = current_app.config["LIBRARY_FOLDER"]
    for name in _library_templates():
        src = os.path.join(template_dir, name)
        dst = os.path.join(lib_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    os.makedirs(os.path.join(root, "conversations"), exist_ok=True)
    os.makedirs(os.path.join(root, "backups"), exist_ok=True)


def active_library_dir(require_login: bool = False) -> Optional[str]:
    user = current_user()
    if user:
        ensure_user_workspace(user["id"])
        return os.path.join(user_root(user["id"]), "library")
    if require_login:
        return None
    return current_app.config["LIBRARY_FOLDER"]


def conversations_dir() -> Optional[str]:
    user = current_user()
    if not user:
        return None
    ensure_user_workspace(user["id"])
    return os.path.join(user_root(user["id"]), "conversations")


def backups_dir() -> Optional[str]:
    user = current_user()
    if not user:
        return None
    ensure_user_workspace(user["id"])
    return os.path.join(user_root(user["id"]), "backups")


def login_required_response():
    return jsonify({"error": "请先登录后再进行操作", "login_required": True}), 401


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return login_required_response()
        return fn(*args, **kwargs)

    return wrapper
