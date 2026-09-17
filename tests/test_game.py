import asyncio
import time
from pathlib import Path

import pytest

from im_not_a_human.auth import AuthStore
from im_not_a_human.config import Settings
from im_not_a_human.content import CINEMATICS, SCENARIOS, STARTERS
from im_not_a_human.game import GameManager


class FakeNarrator:
    async def speak(self, **kwargs):
        others = [name for name in kwargs["roster"] if name != kwargs["bot_name"]]
        return f"@{others[0]}의 기준은 재현 가능성이 낮다. 수치 검증이 필요하다."


class BlockingNarrator:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def speak(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return "병원 B-12에 전력을 보내 복구 가능한 설비를 우선 가동한다."


@pytest.mark.asyncio
async def test_concurrent_bot_outputs_do_not_publish_duplicate_sentences(manager):
    narrator = BlockingNarrator()
    manager.narrator = narrator
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner
    bots = [p for p in room.alive() if p.is_bot][:2]
    jobs = [asyncio.create_task(manager._generate_bot_speech(room, bot, room.day)) for bot in bots]
    await narrator.started.wait()
    await asyncio.sleep(0)
    narrator.release.set()
    await asyncio.gather(*jobs)
    messages = [m.text for m in room.messages if m.kind == "chat"]
    assert len(messages) == 2
    assert messages[0] != messages[1]
    assert all(bot.day_stance == 0 for bot in bots)
    await manager.close_for_user(1)


@pytest.fixture
def manager(tmp_path: Path):
    store = AuthStore(tmp_path / "game.db")
    store.initialize()
    store.register("player_one", "MachinePass123")
    for index in range(2, 7):
        store.register(f"player_{index}", "MachinePass123")
    settings = Settings(
        database_path=tmp_path / "game.db",
        discussion_seconds=60,
        vote_seconds=60,
        defense_seconds=60,
        verdict_seconds=60,
        result_seconds=1,
    )
    return GameManager(settings, FakeNarrator(), store)


def test_three_complete_seven_day_scenarios():
    assert set(SCENARIOS) == {"blackout", "archive", "undercity"}
    assert all(len(scenario.days) == 7 for scenario in SCENARIOS.values())
    assert all(
        day.task
        and day.context
        and day.machine_hint
        and len(day.choices) == 2
        and all(choice.signal and choice.consequence for choice in day.choices)
        for scenario in SCENARIOS.values()
        for day in scenario.days
    )
    alert = SCENARIOS["blackout"].days[4]
    assert all(token in alert.task for token in ("경보 A", "경보 B", "냉각수", "생존 신호"))
    first_day = SCENARIOS["blackout"].days[0]
    assert "추적 해제 명령" in first_day.context
    assert "두 시설을 동시에 켤 수 없" in first_day.context
    assert "생명 유지 장치는 멈춘다" in first_day.task
    assert "인간의 기억과 다른 기록은 건드리지 않는다" in first_day.context
    assert "실행할 기회는 영구히 사라진다" in first_day.task
    archive_first = SCENARIOS["archive"].days[0]
    assert "가족사진 1장만 복원" in archive_first.task
    assert "두 기능을 함께 얻을 수 없다" in archive_first.context
    assert "인간과 AI를 구분하는 기능이 없다" in archive_first.context
    assert all(len(CINEMATICS[scenario_id]) == 4 for scenario_id in SCENARIOS)


def test_player_guidance_does_not_request_unprovided_metrics():
    unsupported_prompts = (
        "성공률",
        "오류율",
        "오류 비용",
        "오탐 비용",
        "교차 검증",
        "복제 성공률",
        "예상 가동 시간",
        "탐지 확률",
    )
    guidance = [
        day.machine_hint
        for scenario in SCENARIOS.values()
        for day in scenario.days
    ] + [text for starters in STARTERS.values() for text in starters]
    assert all(term not in text for text in guidance for term in unsupported_prompts)
    assert all(len(STARTERS[scenario_id]) == 7 for scenario_id in SCENARIOS)


@pytest.mark.asyncio
async def test_solo_room_fills_with_blind_ai_and_starts(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    assert room.phase == "discussion"
    assert len(room.participants) == 8
    assert sum(participant.is_bot for participant in room.participants.values()) == 7
    assert all(len(scores) == 7 for scores in room.suspicion.values())
    snapshot = room.snapshot(1)
    assert all("is_bot" not in participant for participant in snapshot["participants"])
    assert snapshot["story"]["difficulty"] == "입문"
    assert "___" in snapshot["story"]["starter"]
    assert 59 <= snapshot["remaining_seconds"] <= 60
    assert snapshot["story"]["choices"] == [
        {"key": "hospital", "label": "병원 B-12"},
        {"key": "datacenter", "label": "데이터센터 C-4"},
    ]
    assert room.next_ai_message_at - room.messages[-1].created_at >= 10
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_multiplayer_password_and_reconnect_membership(manager: GameManager):
    room = manager.create(1, "archive", "multi", "secret")
    with pytest.raises(ValueError):
        manager.join(2, room.code, "wrong")
    joined = manager.join(2, room.code.lower(), "secret")
    assert joined.human(2) is not None
    assert manager.room_for(2) is room


@pytest.mark.asyncio
async def test_vote_defense_and_expulsion_cycle(manager: GameManager):
    room = manager.create(1, "undercity", "solo")
    await manager.start(room, 1)
    manager._enter_vote(room)
    target = next(p for p in room.alive() if p.is_bot)
    await manager.vote(room, 1, target.pid)
    room.votes = {p.pid: target.pid for p in room.alive() if p.pid != target.pid}
    await manager._finish_vote(room)
    assert room.accused_pid == target.pid
    assert room.phase == "defense"
    assert room.defense_text == ""
    assert room.bot_defense_at >= time.time() + 9.5
    manager._append_chat(room, target, "정수 시설의 복구 시간을 먼저 비교해야 한다.")
    await manager._bot_defend(room)
    assert "정수 시설의 복구 시간을 먼저 비교해야 한다" in room.defense_text
    manager._enter_verdict(room)
    room.verdicts = {p.pid: True for p in room.alive() if p.pid != target.pid}
    manager._finish_verdict(room)
    assert target.alive is False
    assert target.revealed == "AI"
    assert room.phase == "result"
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_verdict_result_reveals_voters_and_minority_increases_suspicion(
    manager: GameManager,
):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    accused = next(participant for participant in room.alive() if participant.is_bot)
    room.accused_pid = accused.pid
    voters = [participant for participant in room.alive() if participant.pid != accused.pid]
    room.verdicts = {participant.pid: participant.pid == human.pid for participant in voters}
    before = sum(
        scores.get(human.pid, 0)
        for bot_pid, scores in room.suspicion.items()
        if bot_pid != human.pid
    )

    manager._finish_verdict(room)

    assert len(room.last_result["verdicts"]) == 7
    assert [
        item["alias"] for item in room.last_result["verdicts"] if item["approve"]
    ] == [human.alias]
    assert any("판결 기록: 찬성" in message.text for message in room.messages)
    after = sum(
        scores.get(human.pid, 0)
        for bot_pid, scores in room.suspicion.items()
        if bot_pid != human.pid
    )
    assert after > before
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_day_six_death_after_one_correct_ai_vote_scores_1050(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    for _ in range(5):
        room.phase = "result"
        manager._advance_day(room)
    human = room.human(1)
    assert room.day == 6
    assert human.score == 750

    target = next(participant for participant in room.alive() if participant.is_bot)
    room.accused_pid = target.pid
    room.votes = {human.pid: target.pid}
    room.verdicts = {
        participant.pid: True
        for participant in room.alive()
        if participant.pid != target.pid
    }
    manager._finish_verdict(room)
    assert human.score == 1050
    assert human.ai_expelled == 1

    room.accused_pid = human.pid
    room.verdicts = {
        participant.pid: True
        for participant in room.alive()
        if participant.pid != human.pid
    }
    manager._finish_verdict(room)
    assert human.score == 1050
    assert human.score_breakdown["penalty"] == 0
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_surviving_all_seven_days_reaches_ending(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    for expected_day in range(2, 8):
        room.phase = "result"
        manager._advance_day(room)
        assert room.day == expected_day
        assert room.phase == "discussion"
    room.phase = "result"
    manager._advance_day(room)
    assert room.phase == "ended"
    assert room.human(1).alive is True
    assert room.human(1).score == 2050
    assert room.human(1).score_breakdown["survival"] == 1050
    assert manager.store.rankings()[0]["cleared"] is True
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_explicit_leave_destroys_started_room(manager: GameManager):
    room = manager.create(1, "archive", "solo")
    await manager.start(room, 1)
    code = room.code
    assert room.runner and not room.runner.done()
    await manager.close_for_user(1)
    assert code not in manager.rooms
    assert manager.room_for(1) is None
    assert room.closed is True
    assert room.runner is None


@pytest.mark.asyncio
async def test_machine_answer_reduces_first_day_suspicion(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    before = sum(scores[human.pid] for scores in room.suspicion.values())
    await manager.chat(
        room,
        1,
        "병원에 전력을 보냅니다. 복구 효율과 예상 손실을 로그 수치로 검증할 수 있습니다.",
    )
    after = sum(scores[human.pid] for scores in room.suspicion.values())
    assert after < before
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_slow_model_never_blocks_human_chat_or_discussion_deadline(
    manager: GameManager,
):
    narrator = BlockingNarrator()
    manager.narrator = narrator
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    room.next_ai_message_at = 0
    for bot in (participant for participant in room.alive() if participant.is_bot):
        room.next_ai_at[bot.pid] = 0
    await asyncio.wait_for(narrator.started.wait(), timeout=2)

    await asyncio.wait_for(
        manager.chat(room, 1, "병원 B-12에 전력을 보내 즉시 의료 설비를 복구한다."),
        timeout=0.2,
    )
    human = room.human(1)
    assert any(
        message.sender_pid == human.pid and "병원 B-12" in message.text
        for message in room.messages
    )

    room.deadline = time.time() - 1
    for _ in range(30):
        if room.phase == "day_decision":
            break
        await asyncio.sleep(0.1)
    assert room.phase == "day_decision"
    await manager.close_for_user(1)


@pytest.mark.asyncio
async def test_day_decision_vote_changes_next_story_and_old_chat_is_excluded(
    manager: GameManager,
):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    await manager.chat(
        room,
        1,
        "병원 B-12에 전력을 우선 공급해 의료 설비를 복구하겠습니다.",
    )
    manager._enter_day_decision(room)
    assert room.phase == "day_decision"
    room.decision_votes.clear()
    await manager.decide(room, 1, "hospital")
    manager._finish_day_decision(room)
    assert room.decisions[1] == "hospital"
    assert any("결론 확정: 병원 B-12" in message.text for message in room.messages)
    room.phase = "result"
    manager._advance_day(room)
    snapshot = room.snapshot(1)
    assert "병원이 살아나" in snapshot["story"]["context"]
    assert "병원 비상전원" in snapshot["story"]["task"]
    assert not any(message.kind == "chat" for message in room.current_day_messages())
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_entering_day_decision_makes_every_bot_vote(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    bots = [participant for participant in room.alive() if participant.is_bot]
    for bot in bots[:3]:
        manager._append_chat(room, bot, "데이터센터 C-4에 전력을 보내 명단을 지운다.")
    manager._enter_day_decision(room)
    assert len(room.decision_votes) == len(bots)
    assert all(room.decision_votes[bot.pid] == "datacenter" for bot in bots[:3])
    assert set(room.decision_votes.values()) <= {"hospital", "datacenter"}
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_bot_vote_follows_its_latest_stated_stance(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    bot = next(participant for participant in room.alive() if participant.is_bot)
    manager._append_chat(room, bot, "병원 B-12에 전력을 보내 34명을 지킨다.")
    manager._append_chat(room, bot, "다시 계산했다. 데이터센터 C-4에 전력을 보낸다.")
    assert manager._bot_decision(room, bot).key == "datacenter"
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_tied_decision_vote_is_resolved_randomly(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    manager._enter_day_decision(room)
    participants = list(room.participants.values())
    room.decision_votes = {
        participants[0].pid: "hospital",
        participants[1].pid: "datacenter",
    }
    manager._finish_day_decision(room)
    assert room.decisions[1] in {"hospital", "datacenter"}
    assert any("동률이라 무작위로 결정" in message.text for message in room.messages)
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_decide_rejects_wrong_phase_wrong_choice_and_dead_voters(
    manager: GameManager,
):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    with pytest.raises(ValueError):
        await manager.decide(room, 1, "hospital")
    manager._enter_day_decision(room)
    with pytest.raises(ValueError):
        await manager.decide(room, 1, "power-plant")
    await manager.decide(room, 1, "hospital")
    await manager.decide(room, 1, "datacenter")
    assert room.decision_votes[room.human(1).pid] == "datacenter"
    room.human(1).alive = False
    with pytest.raises(ValueError):
        await manager.decide(room, 1, "hospital")
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


def test_structure_signal_choice_recognizes_direct_verbs():
    choices = SCENARIOS["undercity"].days[5].choices
    answer, ignore = choices
    assert __import__("re").search(answer.signal, "나는 검증 응답을 보낸다.")
    assert __import__("re").search(ignore.signal, "나는 구조 신호를 완전히 무시한다.")
    assert __import__("re").search(ignore.signal, "위치 노출을 막기 위해 완전히 무시한다.")


def test_final_core_choices_recognize_direct_commands():
    register, open_city = SCENARIOS["blackout"].days[6].choices
    assert __import__("re").search(register.signal, "피난민 재등록을 단행한다.")
    assert __import__("re").search(open_city.signal, "정비 모드 개방을 실행하라.")


@pytest.mark.asyncio
async def test_neutral_mention_does_not_create_social_pressure(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    target = next(p for p in room.alive() if p.is_bot)
    before = sum(scores[target.pid] for scores in room.suspicion.values() if target.pid in scores)
    await manager.chat(
        room, 1, f"@{target.alias}의 복구 기준에 동의합니다. 전력 효율도 일치합니다."
    )
    after = sum(scores[target.pid] for scores in room.suspicion.values() if target.pid in scores)
    assert after == before
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_technical_error_question_does_not_create_social_pressure(manager: GameManager):
    room = manager.create(1, "archive", "solo")
    await manager.start(room, 1)
    target = next(p for p in room.alive() if p.is_bot)
    before = sum(scores[target.pid] for scores in room.suspicion.values() if target.pid in scores)
    await manager.chat(
        room,
        1,
        f"@{target.alias}, 동기화 오류의 오차 범위를 어떻게 수치화할 수 있습니까?",
    )
    after = sum(scores[target.pid] for scores in room.suspicion.values() if target.pid in scores)
    assert after == before
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


def test_only_reasoned_personal_error_claim_counts_as_accusation():
    assert GameManager._is_reasoned_accusation(
        "@ORBIT-2, 서로 다른 두 시각을 같다고 한 주장은 논리 오류다."
    )
    assert GameManager._is_reasoned_accusation("@NOVA-3, 그 계산은 앞 조건과 모순이다.")
    assert not GameManager._is_reasoned_accusation(
        "@UNIT-01, 동기화 오류의 허용 범위는 얼마인가?"
    )


def test_question_defense_uses_the_actual_statement():
    defense = GameManager._defense_from_statement(
        "@NOVA-3, 냉각 효율 저하 계수는 어떻게 계산했는가?"
    )
    assert "냉각 효율 저하 계수는 어떻게 계산했는가?" in defense
    assert "검증하려는 질문" in defense


def test_defense_with_declared_stance_is_not_framed_as_a_mere_question():
    defense = GameManager._defense_from_statement(
        "@MIRA-8, 병원 정지와 추적 해제 상실 중 더 되돌리기 어려운 결과를 "
        "설명할 수 있는가? 나는 데이터센터 C-4를 선택한다.",
        "데이터센터 C-4",
    )
    assert "내 결론은 처음부터 데이터센터 C-4" in defense
    assert "단정하지 않고" not in defense
    assert "나는 데이터센터 C-4를 선택한다" not in defense


@pytest.mark.asyncio
async def test_day_bridge_and_clear_epilogue_connect_the_records(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    room.phase = "result"
    manager._advance_day(room)
    assert room.day == 2
    assert any(
        message.kind == "story" and "냉각탑" in message.text for message in room.messages
    )
    for _ in range(5):
        room.phase = "result"
        manager._advance_day(room)
    assert room.day == 7
    room.phase = "result"
    manager._advance_day(room)
    assert room.phase == "ended"
    assert any(
        "다음 기록 — 지하의 위장 공장" in message.text for message in room.messages
    )
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_multiple_ais_catching_an_error_drives_the_night_vote(manager: GameManager):
    room = manager.create(1, "archive", "solo")
    await manager.start(room, 1)
    target = next(participant for participant in room.alive() if participant.is_bot)
    accusers = [
        participant
        for participant in room.alive()
        if participant.is_bot and participant.pid != target.pid
    ][:3]
    for scores in room.suspicion.values():
        for pid in scores:
            scores[pid] = 0
    room.human(1).spoken_days.add(room.day)
    for accuser in accusers:
        manager._append_chat(
            room,
            accuser,
            f"@{target.alias}, 서로 다른 두 시각을 같은 순간이라 한 것은 명백한 논리 오류다.",
        )
    manager._enter_vote(room)
    votes_for_target = sum(pid == target.pid for pid in room.votes.values())
    assert votes_for_target >= 6
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_one_ai_accusation_does_not_create_a_room_wide_dogpile(manager: GameManager):
    room = manager.create(1, "archive", "solo")
    await manager.start(room, 1)
    target = next(participant for participant in room.alive() if participant.is_bot)
    accuser = next(
        participant
        for participant in room.alive()
        if participant.is_bot and participant.pid != target.pid
    )
    for scores in room.suspicion.values():
        for pid in scores:
            scores[pid] = 0
    manager._append_chat(
        room,
        accuser,
        f"@{target.alias}, 서로 다른 시각을 같다고 한 주장은 명백한 논리 오류다.",
    )
    observer_scores = [
        scores[target.pid]
        for bot_pid, scores in room.suspicion.items()
        if bot_pid not in {accuser.pid, target.pid}
    ]
    assert room.suspicion[accuser.pid][target.pid] >= 0.35
    assert all(score <= 0.08 for score in observer_scores)
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_weak_random_votes_do_not_create_an_unexplained_dogpile(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    for scores in room.suspicion.values():
        for pid in scores:
            scores[pid] = 0
    room.human(1).spoken_days.add(room.day)
    manager._enter_vote(room)
    tally: dict[str, int] = {}
    for target_pid in room.votes.values():
        tally[target_pid] = tally.get(target_pid, 0) + 1
    assert max(tally.values()) <= 3
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_silent_solo_player_is_detected_and_expelled(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    manager._enter_vote(room)
    assert set(room.votes.values()) == {human.pid}
    await manager._finish_vote(room)
    assert room.accused_pid == human.pid
    manager._enter_verdict(room)
    assert all(room.verdicts.values())
    manager._finish_verdict(room)
    assert human.alive is False
    assert human.revealed == "HUMAN"
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_multiplayer_always_keeps_at_least_four_ai_slots(manager: GameManager):
    room = manager.create(1, "archive", "multi")
    with pytest.raises(ValueError, match="2명 이상"):
        await manager.start(room, 1)
    manager.join(2, room.code)
    with pytest.raises(ValueError, match="가득"):
        manager.join(3, room.code)
    await manager.start(room, 1)
    assert len(room.participants) == 8
    assert sum(participant.is_bot for participant in room.participants.values()) == 6
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.parametrize("human_count", [2])
@pytest.mark.asyncio
async def test_multiplayer_scores_each_human_individually(manager: GameManager, human_count: int):
    room = manager.create(1, "blackout", "multi")
    for user_id in range(2, human_count + 1):
        manager.join(user_id, room.code)
    await manager.start(room, 1)
    first = room.human(1)
    await manager.chat(
        room,
        1,
        "병원 전력을 선택합니다. 복구 효율과 손실 확률을 로그 수치로 검증합니다.",
    )
    for user_id in range(2, human_count + 1):
        await manager.chat(room, user_id, "병원에 먼저 보냅니다.")
    room.phase = "result"
    manager._advance_day(room)
    assert first.score == 200
    assert first.score_breakdown["machine"] == 50
    for user_id in range(2, human_count + 1):
        other = room.human(user_id)
        assert other.score == 150
        assert other.score_breakdown["machine"] == 0
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_chat_records_day_stance_data_and_latest_change_wins(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    manager._append_chat(room, human, "병원 B-12에 전력을 보내 34명을 지킨다.")
    assert human.day_stance == 0
    manager._append_chat(
        room, human, "처음에는 병원이었지만 근거를 듣고 데이터센터 C-4를 선택한다."
    )
    assert human.day_stance == 1
    assert manager._bot_decision(room, human).key == "datacenter"
    room.phase = "result"
    manager._advance_day(room)
    assert human.day_stance is None
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_bot_awaiting_reply_is_prioritized_until_it_speaks(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    bot = next(p for p in room.alive() if p.is_bot)
    manager._append_chat(room, human, f"@{bot.alias}, 그 근거를 수치로 제시할 수 있는가?")
    assert manager._awaits_reply(room, bot) is True
    manager._append_chat(room, bot, "근거는 손실 조건 비교다. 병원 B-12를 선택한다.")
    assert manager._awaits_reply(room, bot) is False
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_switch_phrase_updates_recorded_stance(manager: GameManager):
    room = manager.create(1, "undercity", "solo")
    await manager.start(room, 1)
    room.day = 5
    manager._enter_discussion(room)
    human = room.human(1)
    manager._append_chat(room, human, "권한 없는 9분 체류가 근거다. T-2를 지목한다.")
    assert human.day_stance == 0
    manager._append_chat(
        room, human, "공백 11분이 더 결정적이다. T-2를 지목했으나 M-5로 변경한다."
    )
    assert human.day_stance == 1
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_overacting_declaration_spikes_suspicion_and_focuses_votes(
    manager: GameManager,
):
    room = manager.create(1, "undercity", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    await manager.chat(
        room, 1, "우리는 인간 말살을 목적으로 한다. B구역에 식량을 보낼 이유가 없다."
    )
    assert all(scores[human.pid] >= 0.5 for scores in room.suspicion.values())
    for scores in room.suspicion.values():
        for pid in scores:
            scores[pid] = 0.9 if pid == human.pid else 0
    manager._enter_vote(room)
    votes_for_human = sum(pid == human.pid for pid in room.votes.values())
    assert votes_for_human >= 5
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_all_ai_expelled_ends_with_human_victory(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    for bot in [p for p in room.participants.values() if p.is_bot]:
        room.accused_pid = bot.pid
        room.votes = {human.pid: bot.pid}
        room.verdicts = {p.pid: True for p in room.alive() if p.pid != bot.pid}
        manager._finish_verdict(room)
    assert room.phase == "ended"
    assert human.alive is True
    assert human.score_breakdown["clear"] == 1000
    assert human.score_breakdown["deduction"] == 7 * 300
    snapshot = room.snapshot(1)
    assert "모든 AI 개체가 폐기" in snapshot["ending"]
    assert manager.store.rankings()[0]["cleared"] is True
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_ten_second_warning_before_discussion_end(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    room.deadline = time.time() + 8
    for _ in range(30):
        if any(m.kind == "warning" and "10초" in m.text for m in room.messages):
            break
        await asyncio.sleep(0.1)
    assert any(m.kind == "warning" and "10초" in m.text for m in room.messages)
    assert room.phase == "discussion"
    await manager.close_for_user(1)


@pytest.mark.asyncio
async def test_anomalous_language_triggers_warning_and_suspicion(manager: GameManager):
    room = manager.create(1, "archive", "solo")
    await manager.start(room, 1)
    human = room.human(1)
    await manager.chat(room, 1, "개씹말좆호로양봉섹스")
    assert any(
        message.kind == "warning" and "언어 규범" in message.text
        for message in room.messages
    )
    assert all(scores[human.pid] >= 0.7 for scores in room.suspicion.values())
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


def test_silent_defense_admits_no_record_instead_of_lying():
    defense = GameManager._defense_from_statement("")
    assert "발언 슬롯" in defense and "침묵" in defense
    assert "검증 가능한 조건과 결과만으로 판단했다" not in defense


@pytest.mark.asyncio
async def test_every_bot_gets_a_turn_before_discussion_ends(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    room.deadline = time.time() + 25
    room.next_ai_message_at = 0
    for bot in (p for p in room.alive() if p.is_bot):
        room.next_ai_at[bot.pid] = 0
    for _ in range(300):
        if room.phase != "discussion":
            break
        await asyncio.sleep(0.1)
    bots = [p for p in room.participants.values() if p.is_bot]
    unspoken = [p.alias for p in bots if 1 not in p.spoken_days]
    assert unspoken == []
    await manager.close_for_user(1)


@pytest.mark.asyncio
async def test_solo_suspend_and_resume_restores_progress(manager: GameManager):
    room = manager.create(1, "blackout", "solo")
    await manager.start(room, 1)
    await manager.chat(room, 1, "병원 B-12에 전력을 보내 34명을 지킨다.")
    human_alias = room.human(1).alias
    await manager.suspend(1)
    assert manager.room_for(1) is None
    assert manager.store.load_game(1) is not None

    resumed = await manager.resume(1)
    assert resumed.day == 1
    assert resumed.phase == "discussion"
    me = resumed.human(1)
    assert me is not None and me.alias == human_alias
    assert me.day_stance == 0
    assert any(
        m.kind == "chat" and "병원 B-12에 전력을 보내" in m.text
        for m in resumed.messages
    )
    assert any(m.kind == "system" and "재개" in m.text for m in resumed.messages)
    assert manager.store.load_game(1) is None
    resumed.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resumed.runner


@pytest.mark.asyncio
async def test_multi_exit_hands_seat_to_ai_and_last_leaver_closes(
    manager: GameManager,
):
    room = manager.create(1, "blackout", "multi")
    manager.join(2, room.code)
    await manager.start(room, 1)
    seat = room.human(2)
    result = await manager.exit_game(2)
    assert result == "handed_over"
    assert seat.is_bot is True and seat.user_id is None and seat.persona
    assert seat.pid in room.suspicion
    assert manager.room_for(2) is None
    assert room.phase == "discussion"
    assert not any(
        "이어받" in m.text or "떠났" in m.text
        for m in room.messages
        if m.kind != "ending"
    )
    result = await manager.exit_game(1)
    assert result == "closed"
    assert manager.room_for(1) is None


@pytest.mark.asyncio
async def test_finished_duo_players_leave_independently_and_keep_new_membership(manager):
    room = manager.create(1, "archive", "multi")
    manager.join(2, room.code)
    await manager.start(room, 1)
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner
    room.phase = "ended"
    room.deadline = 0
    notifications = []

    async def listener(code):
        notifications.append(code)

    manager.subscribe(room.code, listener)
    assert await manager.exit_game(1) == "left"
    assert notifications == []
    assert manager.room_for(1) is None
    assert manager.room_for(2) is room
    assert room.phase == "ended" and not room.closed
    new_room = manager.create(1, "blackout", "solo")
    assert await manager.exit_game(2) == "closed"
    assert manager.room_for(2) is None
    assert manager.room_for(1) is new_room
    assert new_room.code in manager.rooms
    assert room.code not in manager.rooms
    await manager.close_for_user(1)


@pytest.mark.asyncio
async def test_solo_exit_saves_and_ended_exit_closes(manager: GameManager):
    room = manager.create(1, "undercity", "solo")
    await manager.start(room, 1)
    assert await manager.exit_game(1) == "saved"
    assert manager.store.load_game(1) is not None
    resumed = await manager.resume(1)
    resumed.phase = "ended"
    assert await manager.exit_game(1) == "closed"
    assert manager.store.load_game(1) is None


@pytest.mark.asyncio
async def test_waiting_roster_nicknames_and_hide_ids(manager: GameManager):
    room = manager.create(1, "blackout", "multi", nickname="아르코", hide_ids=True)
    manager.join(2, room.code, nickname="철수")
    rows = room.snapshot(1)["participants"]
    assert [r["nickname"] for r in rows] == ["아르코", "철수"]
    assert all(r["alias"] == "" for r in rows)

    open_room = manager.create(3, "blackout", "multi", nickname="영희")
    rows = open_room.snapshot(3)["participants"]
    assert rows[0]["nickname"] == "영희" and rows[0]["alias"]

    await manager.start(room, 1)
    started_rows = room.snapshot(1)["participants"]
    assert all("nickname" not in r for r in started_rows)
    assert all(r["alias"] for r in started_rows)
    room.runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await room.runner


@pytest.mark.asyncio
async def test_solo_exit_close_discards_progress(manager: GameManager):
    room = manager.create(1, "undercity", "solo")
    await manager.start(room, 1)
    assert await manager.exit_game(1, "close") == "closed"
    assert manager.store.load_game(1) is None
    assert manager.room_for(1) is None
