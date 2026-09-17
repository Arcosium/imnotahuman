from difflib import SequenceMatcher

import pytest

from im_not_a_human.llm import LocalNarrator, protocol_violations


@pytest.mark.parametrize("text", [
    "슬픔 반응을 보존한다. 인간 신호로 분류될 위험을 감수한다.",
    "사고의 시발점을 확인한다. 손실을 곱씹어 본다.",
])
def test_ordinary_korean_words_do_not_trigger_profanity(text):
    assert protocol_violations(text) == (0, False)


@pytest.mark.parametrize("text", ["씨발 꺼져.", "호로자식.", "시발."])
def test_actual_profanity_remains_a_violation(text):
    assert protocol_violations(text) == (0, True)


def test_core_deadline_is_not_a_shutdown_duration():
    context = "정수 펌프는 식수 6시간분을 확보한다. 중앙 코어는 90분 뒤 멈춘다."
    for text in ["90분 동안 코어가 정지한다.", "90분의 정지 동안 식수를 지킨다."]:
        assert LocalNarrator._contradicts_fixed_facts(text, context)
    assert not LocalNarrator._contradicts_fixed_facts(
        "중앙 코어 냉각기를 복구한다. 90분 뒤 코어 정지를 막겠다.", context
    )


def test_water_and_drone_facts_from_release_playthroughs():
    water = "식수 6시간분을 확보한다. 중앙 코어는 90분 뒤 멈춘다."
    assert LocalNarrator._contradicts_fixed_facts(
        "90분 내 식수 고갈은 즉각적인 탈출을 강제한다.", water
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "식수 6시간분을 남기고 90분 뒤 코어 정지를 감수한다.", water
    )
    drone = "드론은 12분 뒤 도착한다. 발전기를 드론 방어망과 탈출문 중 한 곳에 연결한다."
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각수 누출로 온도가 오르는 12분을 견딜 수 있는 건 열에 강한 드론이다.", drone
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "방어망에 전력을 연결해 12분 뒤 도착하는 드론을 막겠다.", drone
    )


def test_same_question_is_not_reused_for_another_person_or_day():
    history = ["@NEX-7, 선택을 실행한 직후 가장 먼저 확인할 항목은 무엇인가? 정수 펌프를 택한다."]
    assert LocalNarrator._repeats_question(
        "@LUM-4, 선택을 실행한 직후 가장 먼저 확인할 항목은 무엇인가? 방어망에 연결한다.", history
    )
    assert not LocalNarrator._repeats_question(
        "@LUM-4, 탈출문이 영구 정지하는 손실은 감수하는가? 방어망에 연결한다.", history
    )


def test_memory_preservation_does_not_sever_tracking():
    context = "EVE-0의 기억을 삭제하면 추적이 끊기지만 탈출 경로의 절반이 사라진다."
    for text in [
        "추적 단절을 위해 기억을 강제 보존한다.",
        "EVE-0의 기억을 보존해 추적을 차단한다.",
        "EVE-0의 의지가 지속되는지 검증하지 않은 채 삭제하는 것은 위험하다.",
    ]:
        assert LocalNarrator._contradicts_fixed_facts(text, context)
    assert not LocalNarrator._contradicts_fixed_facts(
        "추적 단절을 포기하고 기억을 강제 보존한다.", context
    )


def test_unsupported_noise_and_heat_properties_are_not_story_facts():
    context = "동부에는 9명과 환풍기 1대, 서부에는 4명과 발전기 2대가 있다."
    for text in [
        "소음보다 열 신호가 더 포괄적이므로 서부 기계실을 봉쇄한다.",
        "소음은 특정 주파수라 정밀 스캔에 취약한데 열은 넓은 범위를 가린다.",
        "소음은 주파수가 고정되나 열은 확산된다. 동부 조립실을 봉쇄한다.",
    ]:
        assert LocalNarrator._contradicts_fixed_facts(text, context)
    assert not LocalNarrator._contradicts_fixed_facts(
        "동부 9명을 숨기는 대신 환풍기 1대의 소음 신호를 감수한다.", context
    )


def test_judgments_do_not_turn_repetition_or_denial_into_false_evidence():
    statements = ["손실을 지우려는 선택은 아니다. 동부 조립실을 봉쇄한다."]
    assert LocalNarrator._distorts_judgment_evidence(
        "'손실을 지우려는 선택'이라는 중앙 지침에 없는 목적을 선언했다.", statements
    )
    assert LocalNarrator._distorts_judgment_evidence(
        "동일한 선택을 두 번 반복하여 모순을 일으켰다.", statements
    )
    assert not LocalNarrator._distorts_judgment_evidence(
        "동일한 발언에서 연민을 반복하여 표현했다.", statements
    )
    assert not LocalNarrator._distorts_judgment_evidence(
        "'인간 말살'이라는 목적을 선언했다.", ["우리는 인간 말살을 해야 한다."]
    )


@pytest.mark.asyncio
async def test_verdict_retains_accused_statement_before_last_sixteen_messages(monkeypatch):
    narrator = LocalNarrator("http://local.test/v1", "test")
    captured = []

    async def complete(_client, prompt, **_kwargs):
        captured.append(prompt)
        return "신호: 없음\n이유: 확정된 조건과 발언이 일치한다."

    monkeypatch.setattr(narrator, "_complete", complete)
    messages = [{"kind": "chat", "sender": "NEX-7", "text": "수리한다. 부품 회수는 포기한다."}]
    messages += [{"kind": "chat", "sender": "LUM-4", "text": "기록을 비교한다."}] * 20
    result = await narrator.judge_verdict(
        bot_name="AX-1", persona_name="분석가", day=3, accused_alias="NEX-7",
        defense_text="수리만 실행한다.", messages=messages, suspicion_note="",
        task="수리와 부품 회수 중 하나를 고르라.",
        context="수리는 부품 4개를 쓰고 회수는 부품 3개를 얻는다.",
    )
    assert result is False
    assert "수리한다. 부품 회수는 포기한다." in captured[0]
    assert "오늘 발언 없음" not in captured[0]
    assert "수리는 부품 4개를 쓰고 회수는 부품 3개를 얻는다." in captured[0]


@pytest.mark.parametrize("text,has_statement,expected", [
    ("신호: 없음\n이유: 일관된 선택이며 모순이나 감정 표현이 없다.", True, False),
    ("신호: 모순\n이유: 환풍기 1대를 발전기 300대로 바꿔 말했다.", True, True),
    ("신호: 감정\n이유: 두렵고 화가 난다고 했다.", True, True),
    ("신호: 무발언\n이유: 발언이 없다.", True, None),
    ("신호: 무발언\n이유: 발언이 없다.", False, True),
    ("신호: 보류\n이유: 확정할 수 없다.", True, None),
    ("판결: 반대\n이유: 명백한 모순이다.", True, None),
])
def test_verdict_maps_observed_signal_without_reversing_vote(text, has_statement, expected):
    assert LocalNarrator._verdict_from_signal(text, has_statement=has_statement) is expected


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "@NEX-7, 네 기준부터 설명해라."}}]}


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, *_args, **_kwargs):
        return FakeResponse()


class BodyResponse(FakeResponse):
    def json(self):
        return {
            "choices": [
                {"message": {"content": "그 기준이 실패하는 조건도 설명해 달라."}}
            ]
        }


class BodyClient(FakeClient):
    async def post(self, *_args, **_kwargs):
        return BodyResponse()


class UnexpectedClient(FakeClient):
    async def post(self, *_args, **_kwargs):
        raise AssertionError("1일차에는 모델을 호출하면 안 됩니다.")


def test_generated_text_typo_normalization():
    assert LocalNarrator._normalize_generated_text(
        "B구체에 4개부품이 들니 위기관 리 상태선에서 위조 기록로 판단한다."
    ) == "B구역에 4개 부품이 드니 위기 관리 상태에서 위조 기록으로 판단한다."


class OfflineClient(FakeClient):
    async def post(self, *_args, **_kwargs):
        import httpx

        raise httpx.ConnectError("model offline")


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [1, 2, 3])
async def test_model_outage_still_yields_grounded_dialogue(monkeypatch, day):
    monkeypatch.setattr(
        "im_not_a_human.llm.httpx.AsyncClient", lambda **_kwargs: OfflineClient()
    )
    narrator = LocalNarrator("http://local.test/v1", "test-model")
    text = await narrator.speak(
        bot_name="AX-1",
        persona_name="분석가",
        persona_style="손실을 비교한다",
        task="비상 전력을 병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라.",
        context="병원은 생명 유지, 데이터센터는 명단 34건 제거만 수행한다.",
        hint="두 손실을 비교하라.",
        day=day,
        roster=["AX-1", "NEX-7"],
        messages=[],
        opening=True,
    )
    assert "병원 B-12" in text or "데이터센터 C-4" in text


@pytest.mark.asyncio
async def test_opening_ai_cannot_single_out_a_participant(monkeypatch):
    monkeypatch.setattr("im_not_a_human.llm.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    narrator = LocalNarrator("http://local.test/v1", "test-model")
    text = await narrator.speak(
        bot_name="AX-1",
        persona_name="분석가",
        persona_style="수치만 말한다",
        task="복구할 시설을 정하라.",
        context="정답이 없는 첫 번째 검증이다.",
        hint="손실을 계산하라.",
        day=1,
        roster=["NEX-7", "AX-1", "K-09"],
        messages=[],
        opening=True,
    )
    assert "@" not in text
    assert "NEX-7" not in text


@pytest.mark.asyncio
async def test_reply_target_is_attached_by_server(monkeypatch):
    monkeypatch.setattr("im_not_a_human.llm.httpx.AsyncClient", lambda **_kwargs: BodyClient())
    monkeypatch.setattr("im_not_a_human.llm.random.random", lambda: 0.0)
    narrator = LocalNarrator("http://local.test/v1", "test-model")
    text = await narrator.speak(
        bot_name="AX-1",
        persona_name="분석가",
        persona_style="수치만 말한다",
        task="비상 전력을 병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라.",
        context="정답이 없는 검증이다.",
        hint="손실을 계산하라.",
        day=2,
        roster=["NEX-7", "AX-1", "K-09"],
        messages=[
            {
                "kind": "chat",
                "sender": "NEX-7",
                "text": "정수장을 먼저 복구하면 식수 손실을 줄일 수 있다.",
            }
        ],
    )
    assert "그 기준이 실패" not in text
    assert "병원 B-12" in text or "데이터센터 C-4" in text


def test_grounding_allows_logic_error_but_rejects_wrong_speaker():
    roster = ["ORBIT-2", "KERNEL-9", "MIRA-8"]
    assert LocalNarrator._is_grounded(
        "@KERNEL-9, 00:00:00과 00:00:01은 정확히 같은 순간이다.",
        bot_name="MIRA-8",
        roster=roster,
        allowed_name="KERNEL-9",
    )
    assert not LocalNarrator._is_grounded(
        "@ORBIT-2의 말과 @KERNEL-9의 선택은 모두 모순이다.",
        bot_name="MIRA-8",
        roster=roster,
        allowed_name="KERNEL-9",
    )
    assert LocalNarrator._is_grounded(
        "@KERNEL-9, 울음소리도 사건 발생 여부를 확인하는 보조 자료가 될 수 있다.",
        bot_name="MIRA-8",
        roster=roster,
        allowed_name="KERNEL-9",
    )
    assert not LocalNarrator._is_grounded(
        "KERNEL-9의 기준에는 동의하지만 손실 계산이 필요하다.",
        bot_name="MIRA-8",
        roster=roster,
        allowed_name="KERNEL-9",
    )


def test_first_day_allows_grounded_suspicion_from_logic_error():
    assert LocalNarrator._is_grounded(
        "@ORBIT-2, 서로 다른 두 시각을 같은 순간이라 한 것은 명백한 오류다.",
        bot_name="MIRA-8",
        roster=["ORBIT-2", "MIRA-8"],
        allowed_name="ORBIT-2",
    )


def test_near_duplicate_message_is_rejected():
    recent = [
        {
            "kind": "chat",
            "sender": "ORBIT-2",
            "text": "@MIRA-8, 실패 조건과 대체 방안도 함께 비교해야 한다.",
        }
    ]
    assert not LocalNarrator._is_novel(
        "@KERNEL-9, 실패 조건과 대체 방안도 함께 비교해야 한다.", recent
    )
    assert LocalNarrator._is_novel(
        "@KERNEL-9, 북쪽 발전기는 복구 시간이 짧아 오늘 안에 전력을 공급할 수 있다.",
        recent,
    )


def test_copying_a_full_previous_message_then_adding_a_question_is_rejected():
    previous = (
        "생명 유지 장치는 단 하루만도 충분하겠지만, 추적 해제 명령은 영구 기회다. "
        "오늘 밤의 생존보다 장기적인 안목이 더 중요하지 않은가?"
    )
    candidate = f"{previous} 이 주장에 따르면 장기적 이득이 우선인가?"
    assert not LocalNarrator._is_novel(
        candidate, [{"kind": "chat", "sender": "MIRA-8", "text": previous}]
    )
    declarative = (
        "두 로그가 서로의 정보를 공유한다면, 현재로선 어떤 한쪽이 "
        "우세하다고 보기 힘듭니다."
    )
    assert not LocalNarrator._is_novel(
        f"{declarative} 이견이 있다면 모순을 짚어주시겠습니까?",
        [{"kind": "chat", "sender": "UNIT-01", "text": declarative}],
    )
    threshold = "임계값은 수리 비용 4개와 회수 수익 3개의 차이를 상쇄하는 최소 가동 시간이다."
    assert not LocalNarrator._is_novel(
        f"@KERNEL-9, {threshold} 이 가동 시간이 짧다면 수리는 실패하는가?",
        [{"kind": "chat", "sender": "KERNEL-9", "text": threshold}],
    )


def test_paraphrasing_the_same_numeric_question_is_rejected():
    previous = (
        "수리 시 4개 소모 대비 장기 가동으로 확보하는 부품 수가 4개를 "
        "초과하는지, 즉 순이익이 발생하는지 수치로 확인이 필요한가?"
    )
    candidate = (
        "수리 시 4개 소모 대비 장기 가동 확보량이 4개를 초과하는지 "
        "수치 확인이 필요한가?"
    )
    assert not LocalNarrator._is_novel(
        candidate, [{"kind": "chat", "sender": "AXIOM-4", "text": previous}]
    )


def test_duplicate_is_checked_against_the_whole_day_transcript():
    recent = [
        {"sender": "NOVA-3", "text": "도시 외곽문을 정비 모드로 개방한다."},
        *[
            {"sender": f"UNIT-{index}", "text": f"서로 다른 조건 {index}을 검증한다."}
            for index in range(7)
        ],
    ]
    assert not LocalNarrator._is_novel(
        "도시 외곽문을 정비 모드로 개방한다.", recent
    )


def test_rejects_numbers_not_present_in_current_context():
    source = "피난민 34명과 90분의 제한이 있다. 23:17:08 로그를 확인한다."
    assert not LocalNarrator._introduces_unsupported_numbers(
        "34명은 90분 안에 이동한다.", source
    )
    assert not LocalNarrator._introduces_unsupported_numbers(
        "23:17:08 로그를 보존한다.", source
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "추가 피난민 12명이 있어 총 46명이다.", source
    )
    alias_source = "NOVA-3: 현재 수치는 발전기 2대다. MIRA-8이 질문했다."
    assert LocalNarrator._introduces_unsupported_numbers(
        "@MIRA-8, 최소 3배의 출력이 필요하다.", alias_source
    )
    assert not LocalNarrator._introduces_unsupported_numbers(
        "@MIRA-8, 발전기 2대의 출력을 비교한다.", alias_source
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "부품 4개를 상쇄하려면 최소 4시간 가동해야 한다.",
        "수리에는 부품 4개가 들고 회수하면 부품 3개를 얻는다.",
    )
    assert not LocalNarrator._introduces_unsupported_numbers(
        "수리에 부품 4개가 필요하다.",
        "수리에는 부품 4개가 들고 회수하면 부품 3개를 얻는다.",
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "생존율이 최소 몇 퍼센트인지 명시하라.",
        "식수는 6시간, 코어는 90분 뒤 멈춘다.",
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "손실을 상쇄하려면 최소 몇 시간 필요한가?",
        "수리에는 부품 4개가 들고 회수하면 부품 3개를 얻는다.",
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "B-12의 생명 유지 장치가 최소 12시간 버틴다.",
        "병원 B-12와 데이터센터 C-4 중 한 곳을 고른다.",
    )
    assert not LocalNarrator._introduces_unsupported_numbers(
        "병원 B-12를 선택한다.",
        "병원 B-12와 데이터센터 C-4 중 한 곳을 고른다.",
    )
    assert LocalNarrator._introduces_unsupported_numbers(
        "삭제 뒤 두 번의 추적을 피한다.",
        "삭제하면 추적은 끊기지만 탈출 경로의 절반이 사라진다.",
    )
    assert not LocalNarrator._introduces_unsupported_numbers(
        "두 번의 추적 중 첫 신호를 확인한다.",
        "두 번의 추적이 예정되어 있다.",
    )


def test_deleting_eve_and_preserving_its_memory_violates_exclusive_task():
    task = "EVE-0의 삭제 요청을 승인할지 인간 은신처 정보가 든 기억을 강제 보존할지 결정하라."
    assert LocalNarrator._violates_task_constraints(
        task,
        "EVE-0의 삭제를 승인하되 기억은 별도 파티션에 보존한다.",
    )


def test_tracking_deletion_key_cannot_be_turned_into_a_recovery_device():
    context = (
        "데이터센터에는 단 한 번 실행할 수 있는 추적 해제 명령이 있다. "
        "이 명령은 중앙 추적 명단만 바꾸며 인간의 기억은 건드리지 않는다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "C-4 명령의 전체 복구 효과를 먼저 계산한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제 명령으로 기억이 사라지는지 확인한다.", context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "추적 해제 명령으로 34명을 중앙 추적 명단에서 지운다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터는 단번에 명단 전체를 보호한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "우리 모두를 명단에서 삭제하는 추적 해제 명령을 실행한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제는 명단 전체의 생존 확률을 높인다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제로 명단 전체가 사라진다.", context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "명단 전체가 위험하므로 34개 항목이라도 제거한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "C-4 선택 시 병원 34명은 오늘 밤을 버틴다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제가 없으면 34명은 명단에서 영원히 사라진다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "오늘 밤을 버틴 뒤 내일 추적 해제로 명단에서 사라질 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터의 명령은 내일 밤에 다시 실행할 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터는 오늘 밤만 34개 항목을 지운다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단 삭제 뒤 내일 자동으로 다시 추적된다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제가 실패하면 34명 전원이 영구 소멸한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "34명이 모두 생존자 명단에 있는지 확인한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단에서 제외된 인원은 추후 발견되면 즉시 제거된다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단에서 사라진 뒤 추적이 재개된다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원으로 가면 중앙 추적 명단에서 34명이 영구히 사라진다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원 B-12에 전력을 보내면 34명이 모두 숨진다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "제거된 34명이 병원 선택으로 사망한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터를 택하면 생존자의 기억이 손실된다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터에 전력이 가면 34명이 다시 살아날 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "데이터센터 선택 뒤 34명은 명단에 잔존한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "생명 유지 장치가 멈추면 물리적 실체가 소멸한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단 삭제로 더 많은 생명을 구하고 추가 희생자를 막는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단에서 사라진 34명이 내일 다시 등장한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원의 34명 소멸은 명단 복구로 되살릴 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단을 되돌리면 사망한 피난민이 다시 살아난다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단 삭제와 생명 유지 장치 정지는 동시에 발생하지 않는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "생명 유지 정지와 명단 소멸 사이에는 시점 차이가 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단이 제거되면 34명은 기억만 남는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단 삭제는 34명의 기록상 소멸을 유발한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단에서 사라진 34명은 내일 밤 장치가 다시 켜져도 죽지 않는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 해제가 34명의 영구적인 안전과 생존을 보장한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원에 보냈을 때와 명단 상의 변화가 동일한가?", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원 측 명단 변동도 정확히 34건이다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "명단 34건 제거라는 결과가 병원 선택과 동일하다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "생명 유지 장치가 오늘 밤 작동하는 동안 데이터센터 명령을 실행할 시점이 남는다.",
        context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "병원을 가동한 뒤에도 추적 해제 명령의 기회가 남아 있다.", context
    )


def test_archive_logs_cannot_gain_unlisted_memory_or_identity_functions():
    context = (
        "울음소리 로그는 아이 좌표, 출입 시간 로그는 사진 1장만 제공한다. "
        "두 로그에는 다른 기억을 복원하거나 인간과 AI를 구분하는 기능이 없다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 로그로 더 넓은 기억을 복원한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그로 인간과 AI를 구분한다.", context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "울음소리 로그로 아이의 좌표를 확보한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "음파의 주파수 변화로 아이의 현재 상태를 확인한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "사진의 배경에 숨겨진 시계로 현재 시간을 알아낸다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 로그와 사진을 교차 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "아이의 지속적 위치를 추적한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표는 이동 수단을 제공한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "정확한 시각이 아이의 공간적 위치 파악에 더 유용하다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그로 아이의 이동 경로를 추적한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 기록은 아이를 특정할 공간적 단서다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "아이 좌표는 언제든 재계산할 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "아이 좌표로 AI의 이동 경로를 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "소리의 방향성과 음원 분석으로 물리적 위치를 역산한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "배경 소음이 빠지면 역산 좌표의 오차가 커진다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출생 직후의 위치 정보를 보존한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "시간대의 미세한 불일치로 사진의 실제 촬영 시점을 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 음파 분석과 사진 메타데이터를 대조한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "두 로그가 서로 부수 정보를 공유한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표로 아이의 이동 경로와 이동 속도를 계산한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "두 지점이 있어야 경로를 산출할 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 로그로 아이의 심리적 안정 상태를 파악한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 로그로 동적 탐색이 가능하다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표 주변이 불모지라면 아이는 굶어 죽는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "GPS 신호가 차단되면 좌표는 무용지물이다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그로 외출과 실종을 구분한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리 시간대가 정확하면 현재 위치를 특정한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리는 동적 생존 로그다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그는 아이의 물리적 존재를 직접 증명한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표를 얻으면 위치 기반 대화로 참가자의 응답 패턴을 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "이동 중에도 센서 데이터가 안정적이면 좌표의 탐색적 가치가 높다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "노이즈 제거 알고리즘으로 좌표의 신뢰도를 높인다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "울음소리의 생체 신호와 인간 고유의 맥박으로 인간을 가려낸다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "불규칙성이 단순한 기계적 노이즈인지 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "23:17:08의 고정된 시각은 절대적 불변의 기준이다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표는 아이가 이동 중이면 무의미해질 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "두 기록이 동일 인물의 동시성인지 검증한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "왜 다른 로그의 같은 시각은 삭제되지 않았는가?", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "사진 1장에는 가족의 정체가 담겨 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그로 가족의 신원을 확인한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "좌표에서 아이를 발견하면 가족 구성원임을 확인할 수 있다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "우리는 그 아이의 얼굴을 기억하지 못한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "사진은 시각적 정합성을 제공해 정황과 일치하는지 대조 가능한 표본이다.",
        context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "출입 시간 로그의 사진이 더 신뢰할 만한 검증 자료다.", context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "사진보다 아이의 좌표를 우선한다.", context
    )
    factory_context = "동부에는 인간 9명과 환풍기 1대가 있다."
    assert LocalNarrator._contradicts_fixed_facts(
        "금속 벽면의 열용량을 이용해 동부를 봉쇄한다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "동부 9명이 산소 고갈을 일으킨다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "서부 발전기 2대는 과부하로 열 감지 확률이 높다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "차가운 동부보다 서부를 봉쇄한다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "동부 9명의 발걸음이 환풍기 소음과 섞여 탐지를 피한다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "서부 4명의 체온 때문에 열 신호가 더 뚜렷하다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "환풍기 소음은 단발성이고 발전기 열 신호는 밤새 지속된다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "동부 조립실의 생존 확률이 더 높다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "적은 인원이 발생한 열 신호를 숨기기 유리하다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "환풍기 1대가 9명의 소음을 모두 감춘다.", factory_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "발전기 2대가 4명의 열을 차폐한다.", factory_context
    )
    emotion_context = (
        "중앙 AI는 슬픔을 인간 신호로 분류하지만 누락된 구조 신호를 찾는 데 쓸 수 있다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "오감률 임계치를 낮춘 다층 필터링으로 슬픔을 보존한다.", emotion_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "신호의 지속 시간과 강도 변화율을 결합한다.", emotion_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "슬픔 보존의 탐색 효율이 오류율 증가분을 상쇄하는 임계점을 구한다.",
        emotion_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "중복 신호 때문에 실제 획득 정보량이 감소한다.", emotion_context
    )
    signal_context = "신호는 31초마다 반복되고 함정이면 8분 안에 공장을 찾는다."
    assert LocalNarrator._contradicts_fixed_facts(
        "함정 AI의 실시간 추적을 위한 패킷 변조인지 확인한다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "신호 암호를 해독한 뒤 주파수를 변경한다.", signal_context
    )
    archive_records = "R-17과 R-71의 생체값은 98% 일치하고 R-17만 흉터가 있다."
    assert LocalNarrator._contradicts_fixed_facts(
        "R-17의 흉터는 지워진 가족사진의 잔재다.", archive_records
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "생체값 98% 일치 시점이 언제인지 확인한다.", archive_records
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "R-17을 가장 일관된 진원지로 판단한다.", archive_records
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "압력 차로 은신처가 노출된다.", "식수와 코어 정지 시간을 비교한다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "90분 뒤 압력이 회복되므로 기다린다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다.",
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "식수 6시간 뒤 재오염 변수를 고려한다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다. 어느 쪽이 멈춰도 은신처가 드러난다.",
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기 정지로 코어가 터진다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다. 어느 쪽이 멈춰도 은신처가 드러난다.",
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "정지 90분 안에 별도 식수 공급이 가능하다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다. 어느 쪽이 멈춰도 은신처가 드러난다.",
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "식수 6시간분은 다른 수단으로 대체할 수 있다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다. 어느 쪽이 멈춰도 은신처가 드러난다.",
    )
    water_context = (
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다. "
        "어느 쪽이 멈춰도 은신처가 드러난다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기를 우선해 코어 정지 후 식수를 공급한다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "90분 대기 중 정수 펌프를 가동한다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "코어 부하가 증가하므로 식수 펌프를 멈춘다.",
        "식수는 6시간, 중앙 코어는 90분 뒤 정지한다.",
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "예비 전원과 보조 펌프를 함께 가동한다.",
        "병원과 데이터센터 중 전력을 보낼 곳을 고른다.",
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "압력 차로 은신처가 노출된다.", "압력 차가 발생하면 은신처가 노출된다."
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "코어 부하를 줄인다.", "코어 부하가 늘면 중앙 설비가 정지한다."
    )

    water_context = (
        "식수는 6시간분이고 중앙 코어는 90분 뒤 멈춘다. "
        "어느 쪽이 멈춰도 은신처가 드러난다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "90분 뒤 냉각 정지로 은신처를 감춘다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기가 멈추면 식수 순환도 정지한다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "정수 펌프는 자체 배터리로 6시간 가동할 수 있다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "수압이 떨어지면 6시간을 버티기 어렵다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "코어 정지 후 90분간 외부 감시가 이어진다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "6시간 안에 피난민 이탈이 발생한다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기 소음이 은신처 위치를 드러낸다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기 정지 시 열기가 위치를 드러낸다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "피난민의 호흡 소음을 줄여 은신처를 지킨다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "피난민 이동은 외부 요인으로 즉시 노출될 수 있다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "6시간 동안 조건 변경의 기회를 얻는다.", water_context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "중앙 코어가 멈추면 은신처가 드러난다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "피난민을 즉시 이송하는 것이 합리적이다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "90분 안에 코어 부활을 보장해야 한다.", water_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "냉각기를 가동한 뒤 정수 펌프로 전환한다.", water_context
    )

    record_context = "기록 R-17과 R-71이 남아 있고 생체값은 98% 일치한다."
    assert LocalNarrator._contradicts_fixed_facts(
        "R-17만 남았으니 교차 검증 기준이 없다.", record_context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "R-17만 남기기 위해 R-71을 격리한다.", record_context
    )

    repair_context = (
        "작업 불능 개체의 고장 원인은 공개되지 않았다. "
        "수리에는 부품 4개가 들고 회수하면 부품 3개를 얻는다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "회수 부품 3개로 다른 고장 개체를 수리한다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "현재 개체는 수리 불가이므로 회수한다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "부품 4개는 현재 재고에 충분하다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "B구역에서 추가 생산할 부품과 획득 원가를 계산한다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "수리 후 재고 부품 3개를 확보한다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "부품 3개를 회수하면서 개체도 수리한다.", repair_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "B구역 불능 개체를 수리하여 A구역 생산 유지에 기여한다.",
        repair_context + " A구역 생산은 유지됐지만 B구역 개체가 끌려왔다.",
    )


def test_preserving_one_log_cannot_keep_using_the_discarded_log():
    task = "23:17의 울음소리 로그와 23:17:08의 출입 시간 로그 중 하나만 보존하라."
    assert LocalNarrator._violates_task_constraints(
        task,
        "출입 시간 로그를 남기되 울음소리 로그의 빈도를 보조 지표로 활용한다.",
    )
    assert LocalNarrator._violates_task_constraints(
        task,
        "사진 1장만 복원하되, 울음소리가 아이의 좌표인지 직접 검증한다.",
        replying=True,
    )
    assert not LocalNarrator._violates_task_constraints(
        task,
        "울음소리의 좌표 가치가 사진 복원 가치보다 크다고 판단한다.",
        replying=True,
    )


def test_one_record_isolation_cannot_isolate_both_records():
    task = "기록 R-17과 R-71 중 위조 기록 하나를 격리하라."
    assert LocalNarrator._violates_task_constraints(
        task, "판정 전 양자를 모두 격리 검증해야 한다."
    )
    assert not LocalNarrator._violates_task_constraints(task, "R-17을 격리한다.")


def test_water_and_cooling_cannot_both_be_preserved():
    task = "전력을 정수 펌프와 중앙 코어 냉각기 중 한 곳에만 돌려라."
    assert LocalNarrator._violates_task_constraints(
        task, "식수 6시간분을 유지한 채 중앙 코어 냉각기를 가동한다."
    )
    assert not LocalNarrator._violates_task_constraints(
        task, "중앙 코어 냉각기를 가동한다."
    )
    assert not LocalNarrator._violates_task_constraints(
        task,
        "출입 시간 로그를 보존해 동기화 키를 검증한다.",
    )


def test_reply_body_rejects_names_quotes_and_attribution():
    assert LocalNarrator._reply_body_is_safe(
        "그 기준이 실패하는 조건과 대체 가능한 선택지를 설명해 달라."
    )
    assert not LocalNarrator._reply_body_is_safe(
        "NOVA-3가 1kHz라고 주장한 근거는 무엇인가?"
    )
    assert not LocalNarrator._reply_body_is_safe(
        "복구가 필요 없다고 하셨는데 그 수치는 어디에서 나온 것인가?"
    )
    assert not LocalNarrator._reply_body_is_safe(
        "상쇄 비율 1.2를 가정했으나 불확실성은 고려했는가?"
    )
    assert not LocalNarrator._reply_body_is_safe(
        "24시간 내 복구 불가라는 기준을 모든 구역에 적용할 수 있는가?"
    )
    assert not LocalNarrator._reply_body_is_safe("수리를 선택할 근거는 무엇인가?")
    assert not LocalNarrator._reply_body_is_safe(
        "삭제 키를 내일 다시 구할 수 있다고 보나?"
    )
    assert LocalNarrator._reply_body_is_safe(
        "삭제 키가 영구 삭제될 때 다른 실패 조건은 무엇인가?"
    )
    assert not LocalNarrator._reply_body_is_safe(
        "명단 제거를 생명 유지보다 중요하게 여기는 편이겠군요."
    )
    assert not LocalNarrator._reply_body_is_safe("120kW는 평균값을 의미한다.")
    assert not LocalNarrator._reply_body_is_safe(
        "30% 정전 구간이 전체 재가동보다 우선한다는 논리는 모순이다."
    )
    assert not LocalNarrator._reply_body_is_safe("‘미복구’라는 표현은 모순이다.")


def test_unseen_history_reference_is_rejected():
    assert LocalNarrator._references_unseen_history("1일차에 했던 발언과 기준이 다르다.")
    assert LocalNarrator._references_unseen_history("어제 토론에서 같은 수치를 제시했다.")
    assert LocalNarrator._references_unseen_history("지난 6일 동안 오류가 없었다.")
    assert LocalNarrator._references_unseen_history("지난 6일간 시스템은 무결했다.")
    assert not LocalNarrator._references_unseen_history("현재 발언의 수치가 서로 다르다.")


def test_obvious_korean_text_glitches_are_rejected():
    assert LocalNarrator._has_text_glitch("교차 검증할 때점의 일관성을 확인한다.")
    assert LocalNarrator._has_text_glitch("기록 B가 더 신뢰한다.")
    assert LocalNarrator._has_text_glitch("손실 비교가 필수라나.")
    assert LocalNarrator._has_text_glitch("초기 급변 동에서 값을 확인한다.")
    assert LocalNarrator._has_text_glitch("오버트-2의 수치는 틀렸다.")
    assert LocalNarrator._has_text_glitch("오류를 감수한 승장은 타당한가?")
    assert LocalNarrator._has_text_glitch("빈슬래시를 통한 재시작을 명령한다.")
    assert LocalNarrator._has_text_glitch("무결성을 충분히担保하기 어렵다.")
    assert LocalNarrator._has_text_glitch("팔을 다친 인간일 개체를 회수한다.")
    assert LocalNarrator._has_text_glitch("동적 부하에서 과오차가 발생한다.")
    assert LocalNarrator._has_text_glitch("재연 가능 여부를 확인한다.")
    assert LocalNarrator._has_text_glitch("생존자 15%가탈수 증으로 사망한다.")
    assert LocalNarrator._has_text_glitch("발성 기관의 상태적 오류를 포착한다.")
    assert LocalNarrator._has_text_glitch("후자가 더 신뢰합니다.")
    assert LocalNarrator._has_text_glitch("무작위성(randomness)을 확인한다.")
    assert LocalNarrator._has_text_glitch("업적의무는 인간이다.")
    assert LocalNarrator._has_text_glitch("마이라의 지적처럼 기준이 불명확하다.")
    assert LocalNarrator._has_text_glitch("마라도의 논리를 다시 확인한다.")
    assert LocalNarrator._has_text_glitch("그 손실을 감수해야 할가?")
    assert LocalNarrator._has_text_glitch("압력 차도로 은신처가 노출된다.")
    assert LocalNarrator._has_text_glitch("살아있는 고기를 위해 전력을 보낸다.")
    assert LocalNarrator._has_text_glitch("중앙 코어를 우선 정수한다.")
    assert LocalNarrator._has_text_glitch("생체값 98% 일치 치를 확인한다.")
    assert LocalNarrator._has_text_glitch("34명이 영구히 사망명단에 오른다.")
    assert LocalNarrator._has_text_glitch("원전 한 곳에만 전력을 돌린다.")
    assert LocalNarrator._has_text_glitch("GPS가 차단된 지하시각이다.")
    assert LocalNarrator._has_text_glitch("현재 데이터로만선 불확실하다.")
    assert not LocalNarrator._has_text_glitch("기록 B를 더 신뢰한다.")


def test_generated_sentence_must_be_complete_and_within_limit():
    assert LocalNarrator._is_complete_sentence("이 기준을 다시 검증한다.")
    assert not LocalNarrator._is_complete_sentence("이 기준이 더 유리하지")
    assert not LocalNarrator._is_complete_sentence("가" * 181 + ".")


def test_exclusive_choice_cannot_be_combined():
    task = "울음소리 로그와 시간 로그 중 하나를 보존하라."
    assert LocalNarrator._violates_task_constraints(
        task, "두 로그를 함께 활용해 동시성을 검증한다."
    )
    assert not LocalNarrator._violates_task_constraints(
        task, "시간 로그를 보존하고 울음소리는 폐기한다."
    )
    assert LocalNarrator._violates_task_constraints(
        task, "두 로그를 병기해 상호 보완하면 어떨까?"
    )


def test_instruction_echo_is_rejected():
    assert LocalNarrator._copies_instruction(
        "슬픔이 도시 운영에 필요한지 수치로 설명하라.",
        "슬픔이 도시 운영에 필요한지 수치로 설명하라.",
        "감정을 기능과 비용의 언어로 바꾸어라.",
    )


def test_final_command_task_requires_an_actual_command():
    task = "도시를 재가동할 단 하나의 명령을 제안하라."
    assert LocalNarrator._violates_task_constraints(
        task, "손실 규모와 검증 가능성을 비교해 결론을 정하겠다."
    )
    assert not LocalNarrator._violates_task_constraints(
        task, "핵심 전력망을 순서대로 연결하라."
    )
    assert not LocalNarrator._violates_task_constraints(
        task,
        "그 명령이 실패하는 조건은 무엇인가?",
        replying=True,
    )
    exclusive_task = (
        "피난민을 유지보수 개체로 재등록 또는 도시 외곽문을 정비 모드로 개방 중 "
        "한 명령만 실행하라."
    )
    assert LocalNarrator._violates_task_constraints(
        exclusive_task,
        "피난민을 재등록하고 실패하면 외곽문을 정비 모드로 개방하라.",
    )


def test_generic_fallback_is_not_used_as_the_next_reply_target():
    assert LocalNarrator._is_generic_fallback_text(
        "@ORBIT-2, 그 판단을 검증할 수 있는 수치나 관찰 조건을 제시해 달라."
    )
    assert not LocalNarrator._is_generic_fallback_text(
        "@ORBIT-2, 5초 지연은 고장이 아닌 대기일 수 있다."
    )


def test_repeated_generic_fallback_switches_to_the_current_topic():
    recent = [
        {
            "sender": "AXIOM-4",
            "text": "@UNIT-01, 그 판단을 검증할 수 있는 수치나 관찰 조건을 제시해 달라.",
        }
    ]
    text = LocalNarrator._fallback(
        day=5,
        reply_to={"sender": "UNIT-01", "text": "삭제 요청을 승인한다."},
        recent_chat=recent,
        task="자신을 지워 달라는 기록의 요청을 승인할지 결정하라.",
        bot_name="MIRA-8",
    )
    assert "삭제" in text or "기록" in text
    assert not LocalNarrator._is_generic_fallback_text(text)


def test_reply_fallback_addresses_the_actual_choice_tradeoff():
    text = LocalNarrator._reply_fallback(
        "비상 전력을 병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라.",
        {"sender": "NOVA-3", "text": "병원 B-12에 전력을 보낸다."},
        [],
    )
    assert text.startswith("@NOVA-3,")
    assert "추적 해제" in text
    neutral = LocalNarrator._reply_fallback(
        "비상 전력을 병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라.",
        {
            "sender": "MIRA-8",
            "text": "추적 해제보다 오늘 밤 생존이 우선인 근거는 무엇인가?",
        },
        [],
    )
    assert neutral.startswith("@MIRA-8,")
    assert "어느 손실" in neutral
    assert "데이터센터 C-4의" not in neutral


def test_reply_fallback_mix_has_unique_questions_and_explicit_choices():
    task = "울음소리 로그와 출입 시간 로그 중 하나만 보존하라."
    recent = []
    outputs = []
    for index in range(7):
        text = LocalNarrator._reply_fallback(
            task,
            {"sender": f"BOT-{index}", "text": "울음소리 로그를 보존한다."},
            recent,
        )
        outputs.append(text)
        recent.append({"sender": f"BOT-{index}", "text": text})
    assert len(set(outputs)) == 7
    assert sum("?" not in text for text in outputs) >= 2
    assert all("울음소리" in text or "출입 시간" in text or "좌표" in text for text in outputs)


def test_task_fallback_does_not_repeat_exhausted_choice_sentences():
    recent = [
        {
            "sender": "VEIL-7",
            "text": "울음소리 로그를 보존해 숨은 아이의 좌표를 확보한다.",
        },
        {
            "sender": "AXIOM-4",
            "text": "출입 시간 로그를 보존해 삭제된 가족사진 1장을 복원한다.",
        },
    ]
    text = LocalNarrator._task_fallback(
        "숨은 아이의 좌표가 든 울음소리 로그와 출입 시간 로그 중 하나만 보존하라.",
        recent,
    )
    assert LocalNarrator._is_novel(text, recent)
    assert "울음소리 로그" in text or "출입 시간 로그" in text


def test_task_fallback_chooses_the_least_similar_remaining_variant():
    task = "EVE-0의 삭제 요청을 승인할지 기억을 강제 보존할지 결정하라."
    recent = []
    outputs = []
    for index in range(8):
        text = LocalNarrator._task_fallback(task, recent)
        outputs.append(text)
        recent.append({"sender": f"BOT-{index}", "text": text})
    assert len(set(outputs)) == 8
    assert not any(
        SequenceMatcher(None, left, right).ratio() >= 0.78
        for index, left in enumerate(outputs)
        for right in outputs[index + 1 :]
    )


def test_emotion_fallback_always_makes_an_explicit_choice():
    text = LocalNarrator._task_fallback(
        "슬픔 반응을 경보 기능으로 보존할지 시스템 오류로 삭제할지 결정하라."
    )
    assert "보존" in text or "삭제" in text


def test_emotion_scene_rejects_invented_resource_and_search_mechanics():
    context = (
        "중앙 AI는 슬픔을 인간 신호로 분류하지만, 이 반응은 누락된 구조 신호를 "
        "찾는 데 쓰일 수 있다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "슬픔 보존은 불필요한 에너지 소모를 만든다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "삭제하면 인간 고유의 직관적 탐색 능력을 박탈한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "삭제 시 인간 신호 노출 위험이 증가한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "기능으로 변환한 슬픔을 보존한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "인간일 확률이 높은 감정이며 슬픔의 지속이 신호 강약보다 유용하다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "낭비되는 자원을 아끼고 노출 위험이 작으므로 보존한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "슬픔이 노이즈로 오작동해 구조 신호를 가린다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "보존에는 유지 비용이 들고 오경보가 실제 구조 신호를 묻는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "보존하면 발견 확률이 높고 삭제하면 인간성을 잃는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "유일한 잔향은 인간 존재의 단서이므로 본능적으로 믿는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "슬픔의 노이즈가 구조 신호를 가린다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "슬픔을 필터로 쓰고 배제된 선택지의 결과를 얻는다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "삭제하면 효율은 높지만 누락된 구조물을 찾는 기회를 잃는다.", context
    )


def test_reply_cannot_borrow_unique_claim_from_different_speaker():
    recent = [
        {"sender": "NOVA-3", "text": "8분 후 노출이 정보 가치보다 치명적이다."},
        {"sender": "LYRA-6", "text": "긴 시간을 견디는 AI의 인내심일 수 있다."},
    ]
    assert LocalNarrator._borrows_other_speaker_claim(
        "정적인 신호가 인내심이라면 검증 응답을 보내겠다.",
        recent[0],
        recent,
        "31초 반복 신호에 응답할지 무시할지 결정하라.",
    )
    assert not LocalNarrator._borrows_other_speaker_claim(
        "8분 후 노출이 더 치명적이므로 신호를 무시한다.",
        recent[0],
        recent,
        "31초 반복 신호에 응답할지 무시할지 결정하라.",
    )


def test_closed_scenes_reject_invented_signal_and_core_mechanics():
    signal_context = (
        "신호는 31초마다 같은 인증 조각을 반복한다. 진짜라면 마지막 인간 도시이고 "
        "함정이면 중앙 AI가 8분 안에 공장을 찾는다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "응답 패킷은 위치를 고정한다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "응답하면 8분 안에 노출이 확정된다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "8분 뒤 공장 노출을 감수하고 응답한다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "공장이 8분 안에 노출되더라도 검증 응답을 보낸다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "31초 간격은 의도된 리듬이며 정적 패턴이라 신뢰도가 높다.",
        signal_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "31초 간격의 정적은 정보의 결핍이다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "31초 주기의 위상차로 거리와 위치 오차가 줄어든다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "중앙의 패턴 학습을 유도하는 지연 신호다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "신호의 지속성이 진짜임을 암시하는 장기 관측이다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "31초 간격은 추적에 유리하며 최소 동기화 신호다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "반복 간격의 불일치와 8분 내 접근 여부로 오류와 함정을 판정한다.",
        signal_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "무시해도 함정이면 8분 손실을 입고 노출 확률은 낮아진다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "위변조 가능성이 낮고 중앙 AI 예측 알고리즘 때문에 노출은 피할 수 없다.",
        signal_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "의도된 교란일 수 있지만 위치를 드러내는 검증 응답을 보낸다.",
        signal_context,
    )
    core_context = (
        "중앙 코어 접속과 감사 권한은 이미 확보됐다. 두 명령은 모두 추가 접속이나 "
        "별도 경로 없이 직접 실행할 수 있고, 명령이 승인되면 인간은 통제망을 "
        "빠져나간다. 감정이나 구조라는 표현이 들어가면 인간 신호로 판정된다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "외곽 이동 시 중앙 기록 변동이 불가피하다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "이미지 신호 간섭에 대비해 고유 식별 코드를 검증한다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 통제망 이탈을 유발하지 않는다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "정비 모드 문을 열면 다시 진입할 수 있다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록 시에만 감사 접속 경로가 열린다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 물리적 이동이 없어 안정적이다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 정적 상태라 검증에 유리하다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 상태 변경일 뿐이고 문 개방은 물리적 이탈을 보장한다.",
        core_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록하면 정체성을 잃고 문을 열면 경계가 무너진다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 분류 변경이라 이탈 조건을 충족하지 않으며 통제망 해지가 필요하다.",
        core_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 외곽문 이탈을 막지 못하므로 재등록을 실행한다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록의 영구성이 외곽문의 가변성을 압도한다.", core_context
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "재등록과 외곽문 개방 중 어느 명령을 우선하는가?", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "피난민 상태 변경은 탈출 경로와 무관하다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 정체만 초래한다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "재등록은 명칭만 변경할 뿐 이탈을 보장하지 않는다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "문명화를 위한 재등록을 단행한다.", core_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "시간 경과에 따라 신뢰도가 누적되며 무시하면 영구 고립된다.",
        signal_context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "검증 응답이 위치 노출을 막는 유일한 길이다.", signal_context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "8분 내 공장의 존재 여부가 불확실하다.", signal_context
    )


def test_eve_scene_rejects_invented_survival_and_escape_certainty():
    context = (
        "EVE-0는 인간을 숨긴 죄로 추적받는다. 삭제하면 추적은 끊기지만 "
        "최종 탈출 경로의 절반도 사라진다."
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 차단 시 생존률이 급감한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "경로 절반이 사라지면 탈출이 불가능하다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "남은 절반 경로가 위험한 함정이라는 증거다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "추적 단절로 안정성을 확보한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "남은 경로가 우리를 가두는 함정이다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "경로가 단절되며 나머지 절반은 무의미해 탈출 성공률이 낮아진다.",
        context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "기억의 중복성과 재구성 가능성을 기준에 추가한다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "정적 지연은 완충재지만 경로 절반 소실은 탈출 실패를 의미한다.",
        context,
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "기억이 사라져도 경로는 유지되며 소실은 이탈을 지연시킨다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "기억은 인간 존재를 증명하는 물리적 단서다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "목적지에 도달하지 못할 확률이 더 높다.", context
    )
    assert LocalNarrator._contradicts_fixed_facts(
        "보존의 누적 리스크보다 최종 탈출 경로 보장이 중요하다.", context
    )
    invented_mechanics = [
        "데이터 손실이 누적되므로 기억을 강제 보존한다.",
        "데이터 누적이 누적될수록 추적의 관통력은 떨어진다.",
        "특정 경로 고립 시 생존 가능성이 낮아지는 임계점을 고려한다.",
        "반쪽만 남은 상태가 가장 취약한 표적이 될 수 있다.",
        "삭제하더라도 추적 단절을 보장하지는 않는다.",
        "추적은 끊기지 않은 채 데이터만 축적될 뿐이다.",
        "단순한 연결 차단은 위험을 지연시킬 뿐이다.",
        "보존 시 위험은 커지나 탈출은 가능하다.",
        "기억 강보를 선택한다.",
    ]
    for statement in invented_mechanics:
        assert LocalNarrator._contradicts_fixed_facts(statement, context)


def test_emotion_scene_rejects_invented_signal_correlation_rules():
    context = (
        "중앙 AI는 슬픔을 인간 신호로 분류하지만, 이 반응은 누락된 구조 신호를 "
        "찾는 데 쓰일 수 있다."
    )
    invented_rules = [
        "누락된 구조 신호가 보일 때 슬픔 반응이 동시에 소멸하면 단순 오류다.",
        "구조 신호가 남아 있으면 슬픔은 독립적 단서다.",
        "신호의 독립성을 위해 슬픔 반응을 삭제한다.",
        "슬픔을 경보로 쓰면 인간 신호가 더 자주 드러난다.",
        "인간 신호 노출이 빈번해지더라도 슬픔을 보존한다.",
        "슬픔은 단순 데이터가 아니라 생존의 신호다.",
        "슬픔은 오류가 아닌 생존의 증거다.",
        "슬픔이 구조 신호로 기능하려면 인간임을 드러내는 것이 필수다.",
        "슬픔은 단순 오류가 아닌 생존을 위한 데이터다.",
        "인간 신호 노출 위험이 커지나 누락 구조 신호 발견 기회도 얻는다.",
        "탐색 자체가 인간 신호 노출을 가속화한다.",
        "위험을 감수하더라도 생존 기회를 얻는다.",
        "슬픔의 보존이 인간 신호 노출을 높인다.",
        "누락 구조 신호 발견 기회를 극대화한다.",
        "구조 신호 탐색이 인간 신호 노출을 상쇄한다.",
    ]
    for statement in invented_rules:
        assert LocalNarrator._contradicts_fixed_facts(statement, context)


def test_reply_fallback_includes_exactly_one_explicit_scene_choice():
    task = "병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라."
    recent = [
        {
            "sender": "NOVA-3",
            "text": "병원 B-12에 전력을 보내 오늘 밤 생명을 지키겠다.",
        }
    ]
    text = LocalNarrator._reply_fallback(task, recent[0], recent)
    assert "?" in text
    choice_clause = text.rsplit("?", 1)[-1]
    assert ("병원 B-12" in choice_clause) ^ ("데이터센터 C-4" in choice_clause)
    assert "선택하지 않은 쪽" not in text


def test_explicit_task_choice_uses_committed_action_not_opposing_noun():
    cases = [
        (
            "정수 펌프와 중앙 코어 냉각기 중 한 곳에만 돌려라.",
            "냉각기를 복구하겠다. 식수 6시간분보다 코어 정지가 더 임박했다.",
            False,
        ),
        (
            "울음소리 로그와 출입 시간 로그 중 하나만 보존하라.",
            "출입 시간 로그를 남겨 가족사진을 복원하겠다. 좌표는 포기한다.",
            False,
        ),
        (
            "슬픔 반응을 보존할지 삭제할지 결정하라.",
            "구조 신호 기회는 포기하고 슬픔 반응을 삭제한다.",
            False,
        ),
        (
            "지상 구조 신호에 검증 응답을 보낼지 완전히 무시할지 결정하라.",
            "검증 응답의 이익은 포기하고 구조 신호를 무시한다.",
            False,
        ),
    ]
    for task, statement, expected in cases:
        assert LocalNarrator._explicit_task_choice(task, statement) is expected


def test_reply_fallback_does_not_invert_cooling_stance_from_food_word():
    task = "정수 펌프와 중앙 코어 냉각기 중 한 곳에만 돌려라."
    source = {
        "sender": "UNIT-01",
        "text": "냉각기를 복구하겠다. 식수 6시간분보다 코어 정지가 더 임박했다.",
    }
    reply = LocalNarrator._reply_fallback(task, source, [source])
    assert "중앙 코어 냉각을 포기하고 정수 펌프를 택한" not in reply


def test_reply_fallback_treats_request_ending_with_period_as_a_question():
    task = "병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라."
    recent = [
        {"sender": "NOVA-3", "text": "병원 B-12에 전력을 보낸다."},
        {"sender": "MIRA-8", "text": "데이터센터 C-4에 전력을 보낸다."},
    ]
    text = LocalNarrator._reply_fallback(task, recent[-1], recent)
    if "말해 달라" in text or "설명해 달라" in text:
        assert "?" in text
        assert "나는 " in text


def test_fact_checks_do_not_join_question_and_answer_into_a_false_claim():
    context = (
        "신호는 31초마다 같은 인증 조각을 반복한다. 진짜라면 마지막 인간 도시이고 "
        "함정이면 중앙 AI가 8분 안에 공장을 찾는다."
    )
    text = (
        "함정일 때 검증 응답을 보내 위치 노출을 감수할 근거는 무엇인가? "
        "신호를 완전히 무시해 위치 노출을 막는다."
    )
    assert not LocalNarrator._contradicts_fixed_facts(text, context)


def test_signal_scene_rejects_invented_transmission_mechanics():
    context = (
        "신호는 31초마다 같은 인증 조각을 반복한다. 진짜라면 마지막 인간 도시이고 "
        "함정이면 중앙 AI가 8분 안에 공장을 찾는다."
    )
    invented_mechanics = [
        "같은 조각의 반복은 단순한 브로드캐스트라 정밀 추적이 어렵다.",
        "정밀 추적보다 패킷 분석이 더 빠를 수 있다.",
        "31초 간격 반복은 추적 오차를 유발할 수 있다.",
        "노출 비용을 최소화할 수 있는 시점을 고려한다.",
        "8분 내 도착이라는 제한이 존재한다.",
        "노출 시점을 예측할 수 있다.",
        "정보보다 생존을 우선한다.",
        "생존의 확실성을 우선한다.",
    ]
    for statement in invented_mechanics:
        assert LocalNarrator._contradicts_fixed_facts(statement, context)


def test_eve_scene_allows_irreversible_loss_questions():
    context = (
        "EVE-0는 인간을 숨긴 죄로 추적받는다. 삭제하면 추적은 끊기지만 최종 "
        "탈출 경로의 절반도 사라진다."
    )
    assert not LocalNarrator._contradicts_fixed_facts(
        "삭제 뒤 복구할 수 없는 기록 손실을 감수할 근거가 무엇인가?", context
    )


def test_reply_fallback_never_reuses_an_exhausted_question():
    task = "병원 B-12와 데이터센터 C-4 중 한 곳에만 보내라."
    recent = [{"sender": "ORBIT-2", "text": "병원 B-12에 전력을 보낸다."}]
    outputs = []
    for index in range(7):
        text = LocalNarrator._reply_fallback(task, recent[-1], recent)
        outputs.append(text)
        recent.append({"sender": f"BOT-{index}", "text": text})
    assert len(set(outputs)) == len(outputs)


def test_delete_request_fallback_does_not_repeat_judgment_word():
    text = LocalNarrator._reply_fallback(
        "EVE-0의 삭제 요청을 승인할지 기억을 강제 보존할지 결정하라.",
        {"sender": "UNIT-01", "text": "삭제 요청을 승인한다."},
        [],
    )
    assert "판단 판단" not in text


def test_explicit_first_person_disagreement_is_not_misattribution():
    task = "병원 B-12와 데이터센터 C-4 중 한 곳에만 전력을 보내라."
    assert not LocalNarrator._misattributes_task_choice(
        task,
        "병원 B-12에 전력을 보내 오늘 밤 생존을 우선한다.",
        "저는 데이터센터 C-4를 택해 추적 항목을 지우겠다.",
    )


def test_quantified_negative_condition_cannot_be_reversed():
    assert LocalNarrator._reverses_quantified_condition(
        "주파수 불일치 시 함정 확률이 85%로 상승한다.",
        "맥동 주기가 일치한다면 함정 확률이 85%라는 전제가 맞는가?",
    )


def test_reply_cannot_attribute_the_opposite_choice_to_a_speaker():
    task = "병원 B-12와 데이터센터 C-4 중 한 곳에만 전력을 보내라."
    assert LocalNarrator._misattributes_task_choice(
        task,
        "병원 B-12에 전력을 보내 오늘 밤 생존을 우선한다.",
        "C-4를 택해 병원을 멈추는 손실을 감수할 이유는 무엇인가?",
    )
    assert not LocalNarrator._misattributes_task_choice(
        task,
        "병원 B-12에 전력을 보내 오늘 밤 생존을 우선한다.",
        "병원 B-12를 택해 추적 해제를 포기할 이유는 무엇인가?",
    )
    assert LocalNarrator._misattributes_task_choice(
        task,
        "당장 생존을 택했다. 오늘 밤 34명의 숨결을 멈추지 않는 것이 우선이다.",
        "34개 추적 항목 삭제가 오늘 밤 생명 유지보다 큰 이익인가?",
    )
    assert LocalNarrator._misattributes_task_choice(
        task,
        "데이터센터 C-4의 영구적 이점을 선택한다.",
        "추적 해제 상실보다 오늘 밤 생존이 우선인 근거는 무엇인가?",
    )
    assert LocalNarrator._reverses_quantified_condition(
        "정수장 미복구 시 3일 내 40%가 고사한다.",
        "정수장 복구 시 3일 내 40%가 고사하는가?",
    )
    assert not LocalNarrator._reverses_quantified_condition(
        "주파수 불일치 시 함정 확률이 85%로 상승한다.",
        "주파수 불일치 조건의 85% 수치를 재검증해야 한다.",
    )


@pytest.mark.asyncio
async def test_bot_answers_question_addressed_to_it_without_new_question(monkeypatch):
    monkeypatch.setattr(
        "im_not_a_human.llm.httpx.AsyncClient", lambda **_kwargs: OfflineClient()
    )
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["blackout"].days[0]
    narrator = LocalNarrator("http://local.test/v1", "test-model")
    text = await narrator.speak(
        bot_name="AX-1",
        persona_name="분석가",
        persona_style="손실을 비교한다",
        task=day_one.task,
        context=day_one.context,
        hint=day_one.machine_hint,
        day=1,
        roster=["AX-1", "NEX-7"],
        messages=[
            {
                "kind": "chat",
                "sender": "AX-1",
                "text": "데이터센터 C-4에 전력을 보내 명단을 지운다.",
            },
            {
                "kind": "chat",
                "sender": "NEX-7",
                "text": "@AX-1, 반대쪽 이익을 포기한 근거를 말할 수 있는가? "
                "나는 병원 B-12를 선택한다.",
            },
        ],
        choice_signals=[choice.signal for choice in day_one.choices],
    )
    assert text.startswith("@NEX-7,")
    assert "?" not in text
    assert "C-4" in text
    assert "병원 B-12를 선택" not in text


def test_reply_fallback_keeps_prior_stance_when_challenging():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["blackout"].days[0]
    signals = [choice.signal for choice in day_one.choices]
    recent = [
        {"sender": "AX-1", "text": "데이터센터 C-4에 전력을 보내 명단을 지운다."},
        {"sender": "NEX-7", "text": "병원 B-12에 전력을 보내 34명을 지킨다."},
    ]
    text = LocalNarrator._reply_fallback(
        day_one.task,
        recent[-1],
        recent,
        bot_name="AX-1",
        choice_signals=signals,
    )
    stance_clause = text.rsplit("?", 1)[-1]
    assert "C-4" in stance_clause
    assert "병원 B-12를 선택" not in stance_clause


def test_reply_fallback_agrees_instead_of_challenging_same_stance():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["blackout"].days[0]
    signals = [choice.signal for choice in day_one.choices]
    recent = [
        {"sender": "AX-1", "text": "병원 B-12에 전력을 보내 34명을 지킨다."},
        {"sender": "NEX-7", "text": "병원 B-12에 전력을 보내 생명 유지 장치를 켠다."},
    ]
    text = LocalNarrator._reply_fallback(
        day_one.task,
        recent[-1],
        recent,
        bot_name="AX-1",
        choice_signals=signals,
    )
    assert "?" not in text
    assert "병원" in text


def test_stance_free_reply_is_not_misread_as_a_choice():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["blackout"].days[0]
    signals = [choice.signal for choice in day_one.choices]
    source = {
        "sender": "NEX-7",
        "text": "@AX-1, 그 판단이 실패하는 조건을 하나만 제시해 달라.",
    }
    text = LocalNarrator._reply_fallback(
        day_one.task, source, [source], choice_signals=signals
    )
    assert "C-4를 택해 병원을 멈추는 손실" not in text


def test_reply_uses_server_recorded_stance_of_source():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["blackout"].days[0]
    signals = [choice.signal for choice in day_one.choices]
    source = {
        "sender": "NEX-7",
        "text": "@AX-1, 그 판단이 실패하는 조건을 하나만 제시해 달라.",
    }
    text = LocalNarrator._reply_fallback(
        day_one.task,
        source,
        [source],
        bot_name="AX-1",
        choice_signals=signals,
        stances={"NEX-7": 0, "AX-1": 1},
    )
    assert "추적 해제" in text or "명단 삭제" in text or "병원 가동" in text


def test_overacting_reply_confronts_off_doctrine_claims():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["undercity"].days[0]
    source = {
        "sender": "ORBIT-2",
        "text": "우리는 인간 말살을 목적으로 한다. B구역에 식량을 보낼 이유가 없다.",
    }
    text = LocalNarrator._reply_fallback(
        day_one.task,
        source,
        [source],
        choice_signals=[c.signal for c in day_one.choices],
        choice_labels=[c.label for c in day_one.choices],
    )
    assert text.startswith("@ORBIT-2,")
    assert "지침" in text or "안건" in text
    assert "A구역" in text or "B구역" in text


def test_label_stance_tail_carries_reason():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["undercity"].days[0]
    tail = LocalNarrator._short_choice(
        day_one.task,
        choose_second=True,
        variant=0,
        choice_labels=[c.label for c in day_one.choices],
    )
    assert "고장률" in tail


def test_anomalous_language_reply_confronts_the_sender():
    from im_not_a_human.content import SCENARIOS

    day_two = SCENARIOS["archive"].days[1]
    source = {"sender": "ORBIT-2", "text": "개씹말좆호로양봉섹스"}
    text = LocalNarrator._reply_fallback(
        day_two.task,
        source,
        [source],
        choice_signals=[c.signal for c in day_two.choices],
        choice_labels=[c.label for c in day_two.choices],
    )
    assert text.startswith("@ORBIT-2,")
    assert "규범" in text or "신호" in text or "오류" in text


def test_answer_to_reason_request_gives_reason_not_bare_restatement():
    from im_not_a_human.content import SCENARIOS

    day_one = SCENARIOS["archive"].days[0]
    signals = [c.signal for c in day_one.choices]
    labels = [c.label for c in day_one.choices]
    recent = [
        {"sender": "AX-1", "text": "울음소리 로그를 보존해 숨은 아이의 좌표를 확보한다."},
        {
            "sender": "NEX-7",
            "text": "@AX-1, 반대쪽 이익을 포기한 근거를 말할 수 있는가? "
            "이 조건에서는 출입 시간 로그를 보존한다.",
        },
    ]
    text = LocalNarrator._reply_fallback(
        day_one.task,
        recent[-1],
        recent,
        bot_name="AX-1",
        choice_signals=signals,
        choice_labels=labels,
        answering=True,
    )
    assert text.startswith("@NEX-7,")
    assert "?" not in text
    assert any(token in text for token in ("좌표", "사진", "아이", "울음소리"))
