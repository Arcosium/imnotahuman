from pathlib import Path

import pytest

from im_not_a_human.auth import AuthStore


def test_registration_session_and_onboarding(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    user = store.register("survivor_01", "MachinePass123", "생존자01")
    assert user["nickname"] == "생존자01"
    assert store.nickname_available("다른생존자") is True
    assert store.nickname_available("생존자01") is False
    assert user["onboarding_complete"] is False
    assert store.login("survivor_01", "wrong-password1") is None
    assert store.login("survivor_01", "MachinePass123")["id"] == user["id"]

    token, csrf = store.create_session(user["id"], "test-agent")
    session = store.lookup_session(token, "test-agent")
    assert session and session[1] == csrf
    assert store.lookup_session(token, "different-agent") is None

    store.complete_onboarding(user["id"])
    assert store.lookup_session(token, "test-agent")[0]["onboarding_complete"] is True


def test_rejects_weak_or_duplicate_accounts(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    with pytest.raises(ValueError):
        store.register("bad", "onlyletters")
    store.register("survivor", "SecureMachine1")
    with pytest.raises(ValueError):
        store.register("SURVIVOR", "AnotherPass2")
    with pytest.raises(ValueError, match="닉네임"):
        store.register("different", "AnotherPass2", "survivor")


def test_migrates_legacy_users_to_unique_nicknames(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                   password_hash TEXT NOT NULL,
                   onboarding_complete INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL,
                   last_login_at TEXT NOT NULL
               )"""
        )
        db.execute(
            "INSERT INTO users(username,password_hash,created_at,last_login_at) VALUES(?,?,?,?)",
            ("legacy_user", "unused", "2026-01-01", "2026-01-01"),
        )
    store = AuthStore(path)
    store.initialize()
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT nickname FROM users WHERE username='legacy_user'").fetchone()[0]
            == "legacy_user"
        )


def test_records_and_orders_rankings(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    first = store.register("rank_one", "MachinePass123")
    second = store.register("rank_two", "MachinePass456")
    common = {
        "scenario_id": "blackout",
        "mode": "solo",
        "party_size": 1,
        "day_reached": 7,
        "cleared": True,
        "ai_expelled": 3,
        "messages_sent": 12,
        "skills_used": 1,
    }
    assert store.record_result(
        room_code="AAAA",
        user_id=first["id"],
        score=2200,
        duration_seconds=900,
        breakdown={"survival": 1050, "deduction": 300, "machine": 350, "clear": 500},
        **common,
    )
    assert store.record_result(
        room_code="BBBB",
        user_id=second["id"],
        score=2500,
        duration_seconds=1100,
        breakdown={"survival": 1050, "deduction": 600, "machine": 350, "clear": 500},
        **common,
    )
    assert not store.record_result(
        room_code="AAAA",
        user_id=first["id"],
        score=9999,
        duration_seconds=1,
        breakdown={"survival": 9999},
        **common,
    )
    rankings = store.rankings("blackout")
    assert [entry["nickname"] for entry in rankings] == ["rank_two", "rank_one"]
    assert all("username" not in entry for entry in rankings)
    history = store.history(first["id"])
    assert history["summary"] == {"plays": 1, "clears": 1, "best_score": 2200, "average_day": 7.0}
    assert history["entries"][0]["party_size"] == 1


def test_ranking_tie_uses_deduction_not_time(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    fast = store.register("fast_player", "MachinePass123")
    detective = store.register("detective", "MachinePass456")
    common = {
        "scenario_id": "archive",
        "mode": "multi",
        "party_size": 3,
        "score": 2000,
        "day_reached": 7,
        "cleared": True,
        "messages_sent": 10,
        "skills_used": 1,
        "breakdown": {"survival": 1050, "clear": 950},
    }
    store.record_result(
        room_code="FAST",
        user_id=fast["id"],
        duration_seconds=100,
        ai_expelled=1,
        **common,
    )
    store.record_result(
        room_code="WISE",
        user_id=detective["id"],
        duration_seconds=9999,
        ai_expelled=3,
        **common,
    )
    rankings = store.rankings("archive")
    assert [entry["nickname"] for entry in rankings] == ["detective", "fast_player"]


def test_ranking_always_orders_higher_score_first(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    cleared = store.register("cleared_low", "MachinePass123")
    survivor = store.register("survivor_high", "MachinePass456", "high_score")
    common = {
        "scenario_id": "undercity",
        "mode": "solo",
        "party_size": 1,
        "duration_seconds": 1000,
        "ai_expelled": 2,
        "messages_sent": 9,
        "skills_used": 1,
    }
    store.record_result(
        room_code="LOW1",
        user_id=cleared["id"],
        score=1800,
        breakdown={"survival": 1050, "clear": 750},
        day_reached=7,
        cleared=True,
        **common,
    )
    store.record_result(
        room_code="HIGH",
        user_id=survivor["id"],
        score=2100,
        breakdown={"survival": 900, "deduction": 1200},
        day_reached=6,
        cleared=False,
        **common,
    )
    rankings = store.rankings("undercity")
    assert [entry["score"] for entry in rankings] == [2100, 1800]


def test_history_orders_personal_records_by_recency(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    player = store.register("history_player", "MachinePass123", "history")
    common = {
        "user_id": player["id"],
        "scenario_id": "blackout",
        "mode": "solo",
        "party_size": 1,
        "duration_seconds": 500,
        "ai_expelled": 1,
        "messages_sent": 5,
        "skills_used": 0,
    }
    store.record_result(
        room_code="HIGH",
        score=1050,
        breakdown={"survival": 750, "deduction": 300, "penalty": 0},
        day_reached=6,
        cleared=False,
        **common,
    )
    store.record_result(
        room_code="LOW1",
        score=300,
        breakdown={"survival": 300, "deduction": 0, "penalty": 0},
        day_reached=3,
        cleared=False,
        **common,
    )
    history = store.history(player["id"])["entries"]
    assert [entry["score"] for entry in history] == [
        300,
        1050,
    ]
    assert [entry["rank"] for entry in history] == [1, 2]


def test_best_per_account_and_recent_ten_keep_lifetime_stats(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    player = store.register("repeat_player", "MachinePass123", "repeat")
    other = store.register("another_player", "MachinePass123", "another")
    common = dict(mode="solo", party_size=1, duration_seconds=500,
                  messages_sent=5, skills_used=0, ai_expelled=1)
    for index in range(25):
        # The oldest record is the best, and must outlive the recent-ten display.
        score = 3000 - index * 50
        store.record_result(room_code=f"T{index:03}", user_id=player["id"],
                            scenario_id="blackout" if index % 2 == 0 else "archive",
                            score=score, breakdown={"survival":score}, day_reached=7,
                            cleared=True, **common)
    store.record_result(room_code="OTHR", user_id=other["id"], scenario_id="blackout",
                        score=2000, breakdown={"survival":2000}, day_reached=3,
                        cleared=False, **common)
    entries = store.rankings()
    assert [(e["user_id"], e["score"]) for e in entries] == [(player["id"],3000),(other["id"],2000)]
    assert len(store.rankings(limit=2)) == 2
    assert [(e["user_id"], e["score"]) for e in store.rankings("archive")] == [(player["id"],2950)]
    history = store.history(player["id"])
    assert len(history["entries"]) == 10
    assert [e["score"] for e in history["entries"]] == [3000-i*50 for i in range(24,14,-1)]
    assert history["summary"] == {"plays":25,"clears":25,"best_score":3000,"average_day":7.0}
    filtered = store.history(player["id"], "archive")
    assert len(filtered["entries"]) == 10
    assert all(e["scenario_id"] == "archive" for e in filtered["entries"])
    assert filtered["summary"] == history["summary"]
    assert len(store.history(player["id"], limit=500)["entries"]) == 10
    empty = store.history(other["id"], "undercity")
    assert empty["entries"] == []
    assert empty["summary"] == {"plays":1,"clears":0,"best_score":2000,"average_day":3.0}


def test_profile_average_and_empty_profile(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    player = store.register("new_player", "MachinePass123", "newuser")
    assert store.history(player["id"])["summary"] == {
        "plays":0,"clears":0,"best_score":0,"average_day":0.0
    }
    for index, day in enumerate([1, 3, 7]):
        store.record_result(room_code=f"AVG{index}", user_id=player["id"],
            scenario_id="archive", mode="solo", party_size=1, score=day*100,
            day_reached=day, cleared=day==7, duration_seconds=500, ai_expelled=1,
            messages_sent=5, skills_used=0, breakdown={"survival":day*100})
    assert store.history(player["id"])["summary"] == {
        "plays":3,"clears":1,"best_score":700,"average_day":3.7
    }


def test_record_result_recalculates_score_from_breakdown(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    player = store.register("score_player", "MachinePass123", "score")
    store.record_result(
        room_code="SCORE",
        user_id=player["id"],
        scenario_id="blackout",
        mode="solo",
        party_size=1,
        score=950,
        day_reached=6,
        cleared=False,
        duration_seconds=500,
        ai_expelled=1,
        messages_sent=8,
        skills_used=0,
        breakdown={"survival": 750, "deduction": 300, "penalty": -100},
    )
    entry = store.history(player["id"])["entries"][0]
    assert entry["score"] == 1050
    assert entry["breakdown"]["penalty"] == 0


def test_migrates_old_death_penalty_by_recalculating_breakdown(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    player = store.register("penalty_player", "MachinePass123", "penalty")
    store.record_result(
        room_code="OLD1",
        user_id=player["id"],
        scenario_id="blackout",
        mode="solo",
        party_size=1,
        score=950,
        day_reached=6,
        cleared=False,
        duration_seconds=500,
        ai_expelled=1,
        messages_sent=8,
        skills_used=0,
        breakdown={"survival": 750, "deduction": 300, "penalty": -100},
    )

    store.initialize()

    entry = store.history(player["id"])["entries"][0]
    assert entry["score"] == 1050
    assert entry["breakdown"]["penalty"] == 0


def test_delete_account_removes_only_its_sessions_saved_game_and_results(tmp_path: Path):
    store = AuthStore(tmp_path / "auth.db")
    store.initialize()
    target = store.register("delete_target", "MachinePass123", "삭제대상")
    other = store.register("keep_player", "MachinePass123", "유지대상")
    target_session, _ = store.create_session(target["id"], "qa-browser")
    other_session, _ = store.create_session(other["id"], "qa-browser")
    store.save_game(target["id"], {"scenario_id": "blackout", "messages": ["private"]})
    store.save_game(other["id"], {"scenario_id": "archive"})
    for player in (target, other):
        store.record_result(
            room_code="DEL1", user_id=player["id"], scenario_id="blackout", mode="multi",
            party_size=2, score=150, day_reached=1, cleared=False, duration_seconds=60,
            ai_expelled=0, messages_sent=1, skills_used=0, breakdown={"survival": 150},
        )
    store.delete_account(target["id"])
    assert store.login("delete_target", "MachinePass123") is None
    assert store.lookup_session(target_session, "qa-browser") is None
    assert store.load_game(target["id"]) is None
    assert store.history(target["id"])["entries"] == []
    assert store.lookup_session(other_session, "qa-browser") is not None
    assert store.load_game(other["id"]) == {"scenario_id": "archive"}
    assert len(store.history(other["id"])["entries"]) == 1
