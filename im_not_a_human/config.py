from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path = Path(
        os.getenv("INH_DATABASE_PATH", "/home/arcosium/vault/im-not-a-human/game.db")
    )
    local_model_url: str = os.getenv("INH_LOCAL_MODEL_URL", "http://127.0.0.1:11434/v1")
    local_model: str = os.getenv("INH_LOCAL_MODEL", "qwen3.6-35b-a3b-uncensored")
    cookie_secure: bool = os.getenv("INH_COOKIE_SECURE", "false").lower() == "true"
    discussion_seconds: int = int(os.getenv("INH_DISCUSSION_SECONDS", "210"))
    decision_seconds: int = int(os.getenv("INH_DECISION_SECONDS", "25"))
    vote_seconds: int = int(os.getenv("INH_VOTE_SECONDS", "40"))
    defense_seconds: int = int(os.getenv("INH_DEFENSE_SECONDS", "40"))
    verdict_seconds: int = int(os.getenv("INH_VERDICT_SECONDS", "35"))
    result_seconds: int = int(os.getenv("INH_RESULT_SECONDS", "10"))
    session_days: int = 7


settings = Settings()
