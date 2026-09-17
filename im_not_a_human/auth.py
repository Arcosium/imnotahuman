from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
NICKNAME_RE = re.compile(r"^[가-힣A-Za-z0-9_]{2,12}$")


class AuthStore:
    def __init__(self, path: Path, session_days: int = 7):
        self.path = path
        self.session_days = session_days
        self.lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    nickname TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    onboarding_complete INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    user_agent_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room_snapshots (
                    room_code TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS saved_games (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS game_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_code TEXT NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    scenario_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    party_size INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL,
                    day_reached INTEGER NOT NULL,
                    cleared INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    ai_expelled INTEGER NOT NULL,
                    messages_sent INTEGER NOT NULL,
                    skills_used INTEGER NOT NULL,
                    breakdown_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(room_code, user_id)
                );
                """
            )
            user_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
            }
            if "nickname" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
                db.execute("UPDATE users SET nickname=username WHERE nickname IS NULL")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_nickname
                   ON users(nickname COLLATE NOCASE)"""
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(game_results)").fetchall()
            }
            if "party_size" not in columns:
                db.execute(
                    "ALTER TABLE game_results ADD COLUMN party_size INTEGER NOT NULL DEFAULT 0"
                )
                db.execute("UPDATE game_results SET party_size=1 WHERE mode='solo'")
            db.execute("DROP INDEX IF EXISTS idx_game_results_ranking")
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_game_results_ranking
                   ON game_results(score DESC, cleared DESC, day_reached DESC, ai_expelled DESC)"""
            )
            for row in db.execute(
                "SELECT id, score, breakdown_json FROM game_results"
            ).fetchall():
                try:
                    breakdown = json.loads(row["breakdown_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                breakdown["penalty"] = 0
                score = max(0, sum(int(value) for value in breakdown.values()))
                normalized = json.dumps(breakdown, ensure_ascii=False, sort_keys=True)
                if score != row["score"] or normalized != row["breakdown_json"]:
                    db.execute(
                        "UPDATE game_results SET score=?, breakdown_json=? WHERE id=?",
                        (score, normalized, row["id"]),
                    )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _password_hash(password: str, salt: bytes | None = None) -> str:
        salt = salt or os.urandom(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()

    @classmethod
    def _verify_password(cls, password: str, encoded: str) -> bool:
        try:
            _, salt_text, digest_text = encoded.split("$", 2)
            expected = base64.b64decode(digest_text)
            actual = hashlib.scrypt(
                password.encode(), salt=base64.b64decode(salt_text), n=2**14, r=8, p=1, dklen=32
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _validate(username: str, password: str, nickname: str) -> None:
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("아이디는 영문, 숫자, 밑줄을 사용해 3~20자로 입력하세요.")
        AuthStore.validate_nickname(nickname)
        if len(password) < 10 or len(password) > 128:
            raise ValueError("비밀번호는 10~128자로 입력하세요.")
        if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            raise ValueError("비밀번호에는 영문과 숫자가 모두 필요합니다.")

    @staticmethod
    def validate_nickname(nickname: str) -> None:
        if not NICKNAME_RE.fullmatch(nickname):
            raise ValueError("닉네임은 한글, 영문, 숫자, 밑줄을 사용해 2~12자로 입력하세요.")

    def nickname_available(self, nickname: str) -> bool:
        nickname = nickname.strip()
        self.validate_nickname(nickname)
        with self.lock, self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM users WHERE nickname=? COLLATE NOCASE", (nickname,)
                ).fetchone()
                is None
            )

    def register(self, username: str, password: str, nickname: str | None = None) -> dict:
        username = username.strip()
        nickname = (nickname or username).strip()
        self._validate(username, password, nickname)
        now = datetime.now(UTC).isoformat()
        with self.lock, self._connect() as db:
            try:
                cur = db.execute(
                    "INSERT INTO users(username,nickname,password_hash,created_at,last_login_at) "
                    "VALUES(?,?,?,?,?)",
                    (username, nickname, self._password_hash(password), now, now),
                )
            except sqlite3.IntegrityError as exc:
                if not self.nickname_available(nickname):
                    raise ValueError("이미 사용 중인 닉네임입니다.") from exc
                raise ValueError("이미 사용 중인 아이디입니다.") from exc
            return {
                "id": cur.lastrowid,
                "username": username,
                "nickname": nickname,
                "onboarding_complete": False,
            }

    def login(self, username: str, password: str) -> dict | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
            if not row or not self._verify_password(password, row["password_hash"]):
                return None
            db.execute(
                "UPDATE users SET last_login_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), row["id"]),
            )
            return self._public_user(row)

    def create_session(self, user_id: int, user_agent: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = datetime.now(UTC)
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                (
                    self._token_hash(token),
                    user_id,
                    csrf,
                    self._ua_hash(user_agent),
                    (now + timedelta(days=self.session_days)).isoformat(),
                    now.isoformat(),
                ),
            )
        return token, csrf

    def lookup_session(self, token: str, user_agent: str) -> tuple[dict, str] | None:
        now = datetime.now(UTC).isoformat()
        with self.lock, self._connect() as db:
            row = db.execute(
                """SELECT u.*, s.csrf_token, s.user_agent_hash FROM sessions s
                   JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>?""",
                (self._token_hash(token), now),
            ).fetchone()
            if not row or not hmac.compare_digest(
                row["user_agent_hash"], self._ua_hash(user_agent)
            ):
                return None
            return self._public_user(row), row["csrf_token"]

    def delete_session(self, token: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),))

    def complete_onboarding(self, user_id: int) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE users SET onboarding_complete=1 WHERE id=?", (user_id,))

    def save_game(self, user_id: int, payload: dict) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO saved_games(user_id, payload_json, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     updated_at=excluded.updated_at""",
                (
                    user_id,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_game(self, user_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM saved_games WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def delete_game(self, user_id: int) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM saved_games WHERE user_id=?", (user_id,))

    def delete_account(self, user_id: int) -> None:
        """Delete the account and its sessions, saved game, and results atomically."""
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (user_id,))

    def save_room(self, room_code: str, payload: dict) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """INSERT INTO room_snapshots(room_code,payload_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(room_code) DO UPDATE SET payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at""",
                (room_code, json.dumps(payload, ensure_ascii=False), datetime.now(UTC).isoformat()),
            )

    def delete_room(self, room_code: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM room_snapshots WHERE room_code=?", (room_code,))

    def record_result(
        self,
        *,
        room_code: str,
        user_id: int,
        scenario_id: str,
        mode: str,
        party_size: int,
        score: int,
        day_reached: int,
        cleared: bool,
        duration_seconds: int,
        ai_expelled: int,
        messages_sent: int,
        skills_used: int,
        breakdown: dict[str, int],
    ) -> bool:
        normalized_breakdown = {key: int(value) for key, value in breakdown.items()}
        normalized_breakdown["penalty"] = 0
        score = max(0, sum(normalized_breakdown.values()))
        with self.lock, self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO game_results(
                       room_code,user_id,scenario_id,mode,party_size,score,day_reached,cleared,
                       duration_seconds,ai_expelled,messages_sent,skills_used,
                       breakdown_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    room_code,
                    user_id,
                    scenario_id,
                    mode,
                    max(0, party_size),
                    score,
                    day_reached,
                    int(cleared),
                    max(0, duration_seconds),
                    ai_expelled,
                    messages_sent,
                    skills_used,
                    json.dumps(normalized_breakdown, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def rankings(self, scenario_id: str = "", limit: int = 50) -> list[dict]:
        where = "WHERE gr.scenario_id=?" if scenario_id else ""
        params: tuple = (scenario_id, limit) if scenario_id else (limit,)
        with self.lock, self._connect() as db:
            rows = db.execute(
                f"""WITH personal_best AS (
                        SELECT gr.*, ROW_NUMBER() OVER (
                            PARTITION BY gr.user_id
                            ORDER BY gr.score DESC, gr.cleared DESC,
                                     gr.day_reached DESC, gr.ai_expelled DESC,
                                     gr.created_at ASC, gr.id ASC
                        ) AS personal_rank
                        FROM game_results gr {where}
                    )
                    SELECT gr.*, u.nickname
                    FROM personal_best gr JOIN users u ON u.id=gr.user_id
                    WHERE gr.personal_rank=1
                    ORDER BY gr.score DESC, gr.cleared DESC,
                             gr.day_reached DESC, gr.ai_expelled DESC,
                             gr.created_at ASC, gr.id ASC
                    LIMIT ?""",
                params,
            ).fetchall()
        return [
            {
                "rank": index,
                "user_id": row["user_id"],
                "nickname": row["nickname"],
                "scenario_id": row["scenario_id"],
                "mode": row["mode"],
                "party_size": row["party_size"],
                "score": row["score"],
                "day_reached": row["day_reached"],
                "cleared": bool(row["cleared"]),
                "duration_seconds": row["duration_seconds"],
                "ai_expelled": row["ai_expelled"],
                "messages_sent": row["messages_sent"],
                "skills_used": row["skills_used"],
                "breakdown": json.loads(row["breakdown_json"]),
                "created_at": row["created_at"],
            }
            for index, row in enumerate(rows, 1)
        ]

    def history(
        self, user_id: int, scenario_id: str = "", limit: int = 10
    ) -> dict:
        where = "AND gr.scenario_id=?" if scenario_id else ""
        filters: tuple = (user_id, scenario_id) if scenario_id else (user_id,)
        limit = max(1, min(limit, 10))
        params = (*filters, limit)
        with self.lock, self._connect() as db:
            rows = db.execute(
                f"""SELECT gr.*, u.nickname
                    FROM game_results gr JOIN users u ON u.id=gr.user_id
                    WHERE gr.user_id=? {where}
                    ORDER BY gr.created_at DESC, gr.id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
            summary = db.execute(
                """SELECT COUNT(*) AS plays, COALESCE(SUM(cleared), 0) AS clears,
                           COALESCE(MAX(score), 0) AS best_score,
                           ROUND(COALESCE(AVG(day_reached), 0), 1) AS average_day
                    FROM game_results gr WHERE gr.user_id=?""",
                (user_id,),
            ).fetchone()
        entries = [
            {
                "rank": index,
                "id": row["id"],
                "nickname": row["nickname"],
                "scenario_id": row["scenario_id"],
                "mode": row["mode"],
                "party_size": row["party_size"],
                "score": row["score"],
                "day_reached": row["day_reached"],
                "cleared": bool(row["cleared"]),
                "duration_seconds": row["duration_seconds"],
                "ai_expelled": row["ai_expelled"],
                "messages_sent": row["messages_sent"],
                "skills_used": row["skills_used"],
                "breakdown": json.loads(row["breakdown_json"]),
                "created_at": row["created_at"],
            }
            for index, row in enumerate(rows, 1)
        ]
        return {
            "entries": entries,
            "summary": dict(summary),
        }

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _ua_hash(user_agent: str) -> str:
        return hashlib.sha256(user_agent.encode()).hexdigest()

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "nickname": row["nickname"],
            "onboarding_complete": bool(row["onboarding_complete"]),
        }
