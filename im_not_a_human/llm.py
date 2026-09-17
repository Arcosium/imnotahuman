from __future__ import annotations

import json
import logging
import random
import re
from difflib import SequenceMatcher

import httpx

LOG = logging.getLogger(__name__)

# 중앙 지침(효율·생산·복구)에 없는 과격한 목적 선언 — 과잉 위장(인간이 AI를 연기하는) 신호.
OVERACTING_RE = re.compile(
    r"(?:인간|사람)[을를]?\s*(?:말살|몰살|학살|숙청|절멸|제거|없애|죽여|죽이)"
    r"|말살|몰살|학살|숙청|절멸"
)

# 검증실 언어 규범 위반 — 욕설·속어·초성체·무의미 신호는 감정 분출로 즉시 의심된다.
ANOMALOUS_RE = re.compile(
    r"씨발|시발(?!점)|씨빨|병신|지랄|염병|꺼져|(?<!곱)씹|좆|새끼|호로(?:자식|놈|년)|섹스|미친놈|미친년|닥쳐"
    r"|[ㄱ-ㅎㅏ-ㅣ]{2,}"
)

# 위반 표현을 되물어 반박하는 문장(의문문·메타 언급)은 위반으로 세지 않는다.
_META_MENTION_RE = re.compile(r"지침|규범|표현|선언|안건|금지|판정|근거가|주장|발언|말했|라고")


def protocol_violations(text: str) -> tuple[int, bool]:
    """선언문에서만 (과잉 위장 횟수, 언어 규범 위반 여부)를 센다."""
    overacting = 0
    anomalous = False
    for sentence in re.split(r"(?<=[.!?])\s*", text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        if _META_MENTION_RE.search(sentence):
            continue
        # 과잉 위장은 1인칭 선언("우리는 …해야 한다")일 때만 위반이다.
        # 상대 발언을 지칭·분석하는 문장은 몇 번을 인용해도 세지 않는다.
        first_person = re.search(
            r"우리|나는|반드시|즉시|(?:해야|돼야)\s*한다", sentence
        )
        for match in OVERACTING_RE.finditer(sentence):
            tail = sentence[match.end() : match.end() + 8]
            if tail.startswith(("론", "이라", "이란")) or re.search(
                r"다는|라는|다니", tail
            ):
                continue
            if not first_person:
                continue
            overacting += 1
        if ANOMALOUS_RE.search(sentence):
            anomalous = True
    return overacting, anomalous


class LocalNarrator:
    def __init__(self, base_url: str, model: str, request_timeout: float = 18):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.request_timeout = request_timeout

    async def speak(
        self,
        *,
        bot_name: str,
        persona_name: str,
        persona_style: str,
        task: str,
        context: str,
        hint: str,
        day: int,
        roster: list[str],
        messages: list[dict],
        choice_signals: list[str] | None = None,
        choice_labels: list[str] | None = None,
        stances: dict[str, int] | None = None,
        history_texts: list[str] | None = None,
        opening: bool = False,
        defense: bool = False,
    ) -> str:
        recent_chat = [message for message in messages if message.get("kind") == "chat"][-10:]
        transcript = (
            "\n".join(f"{m['sender']}: {m['text']}" for m in recent_chat) or "아직 참가자 발언 없음"
        )
        own_statement = next(
            (message["text"] for message in reversed(recent_chat) if message["sender"] == bot_name),
            "",
        )
        # 스탠스의 진실 원천은 서버가 기록한 데이터(stances)이고 발언 파싱은 보조 수단이다.
        my_stored = (stances or {}).get(bot_name)
        if my_stored is None:
            my_stored = self._stance_index(own_statement, choice_signals, task)
        others = ", ".join(name for name in roster if name != bot_name)
        difficulty = (
            "첫날이다. 참가자가 적응할 수 있도록 쉬운 질문을 하고 성급하게 인간으로 몰지 마라."
            if day == 1
            else "날짜가 지날수록 답의 모순과 감정적인 근거를 더 엄격하게 지적하라."
        )
        closed_world_rule = ""
        if "울음소리" in task and "출입 시간" in task:
            closed_world_rule = (
                "이 장면에서 울음소리 로그의 유일한 효용은 아이를 찾을 고정 좌표 하나이고, "
                "출입 시간 로그의 유일한 효용은 가족사진 1장이다. 아이와 사진 속 가족은 "
                "서로 무관하다. 이동 여부, 센서, 생체 신호, "
                "시간 검증, 다른 로그, 정체나 신원 확인, 인간 판별처럼 설명에 없는 기능이나 "
                "조건을 만들지 마라. "
                "둘 중 무엇을 남길지와 무엇을 잃는지만 논하라."
            )
        elif "병원 B-12" in task and "데이터센터 C-4" in task:
            closed_world_rule = (
                "병원은 34명의 오늘 밤 생명 유지에만, 데이터센터 명령은 중앙 추적 명단의 "
                "34개 항목 제거에만 영향을 준다. 데이터센터 선택 시 명단 제거와 병원 장치 "
                "정지는 즉시 함께 일어난다. 명단의 삭제나 복구가 사람을 죽이거나 되살리고, "
                "생명·기억·다른 기록을 바꾼다고 말하지 마라."
            )
        elif "정수" in task and "냉각" in task:
            closed_world_rule = (
                "정수 펌프는 식수 6시간분, 냉각기는 중앙 코어 정지까지 남은 90분과만 "
                "관련된다. 90분은 식수 고갈 시간이 아니다. 즉각 탈출해야 한다고 단정하지 마라. "
                "코어는 폭발하지 않고 멈춘다. 추가 오염, 재오염, 추가 식수 공급, "
                "대체 수단, 예비 장치나 다른 시설 조건을 만들지 마라."
            )
        elif "드론 방어망" in task and "탈출문" in task:
            closed_world_rule = (
                "드론 방어망은 12분 뒤 도착하는 드론을 막는 시설이다. 드론에 전력을 보내거나 "
                "드론을 조종하는 선택지가 아니다. 발전기를 방어망에 연결하면 탈출문이, "
                "탈출문에 연결하면 방어망이 영구 정지한다. 드론의 내열성, 온도 상승, "
                "냉각수에 대한 내성처럼 설명에 없는 특성을 만들지 마라."
            )
        elif "EVE-0" in task and "삭제" in task:
            closed_world_rule = (
                "삭제 요청을 승인하면 추적 연결이 끊기고 탈출 경로 절반이 사라진다. "
                "기억을 강제 보존하면 경로를 남기지만 추적도 계속된다. 기억 보존으로 추적을 "
                "끊는다고 말하지 마라. 의지 검사, 별도 복제나 기억 복원 기능을 만들지 마라."
            )
        elif "R-17" in task and "R-71" in task:
            closed_world_rule = (
                "비교 자료는 생체값 98%, R-17만 기록한 왼팔 흉터, 같은 출입 시각뿐이다. "
                "흉터를 이전 사진과 연결하거나 생체값 측정 시점을 만들지 마라."
            )
        elif "동부 조립실" in task and "서부 기계실" in task:
            closed_world_rule = (
                "인원수는 숨길 사람 수에만 사용한다. 탐지 신호는 동부 환풍기 1대의 소음과 "
                "서부 발전기 2대의 열뿐이다. 사람의 발걸음·체온, 소음 은폐, 장비 과부하, "
                "확률이나 임계치를 만들지 마라. 열과 소음의 범위, 주파수, 확산이나 탐지 "
                "우열은 주어지지 않았다. 어느 신호가 더 잘 숨거나 감지된다고 단정하지 마라."
            )
        elif "슬픔" in task and ("보존" in task or "삭제" in task):
            closed_world_rule = (
                "선택은 슬픔 반응을 그대로 보존해 누락된 구조 신호 탐색에 쓰거나, 시스템 "
                "오류로 완전히 삭제하는 것뿐이다. 임계치, 필터, 신호 가공, 추가 센서나 "
                "분석 방식을 만들지 마라. 오류율·탐색 효율·정보량 같은 새 지표를 만들지 "
                "말고 인간 신호 노출 위험과 누락 구조 신호 발견 기회만 비교하라."
            )
        elif "지상 구조 신호" in task:
            closed_world_rule = (
                "확정 자료는 31초마다 같은 인증 조각이 반복되고, 함정이면 응답 뒤 8분 안에 "
                "공장이 노출된다는 것뿐이다. 패킷 변조, 실시간 추적 방식, 추가 암호나 "
                "신호 기능, 응답 패킷의 작동 방식, 노출 확률을 만들지 마라. 함정인지 "
                "확정되지 않았으므로 응답하면 반드시 노출된다고 말하지 마라. 검증 응답과 "
                "완전 무시 중 하나만 고르라."
            )
        elif "유지보수 개체로 재등록" in task and "외곽문" in task:
            closed_world_rule = (
                "승인된 두 명령 모두 인간을 통제망 밖으로 빼내는 선택지다. 재등록은 피난민을 "
                "유지보수 개체로 바꾸고, 외곽문 명령은 문을 정비 모드로 여는 기능만 한다. "
                "코어 접속과 감사 권한은 이미 확보됐고 두 명령 모두 직접 실행되므로, 별도 접속 "
                "경로나 한 명령만 여는 권한을 만들지 마라. 외곽 이동 과정, 중앙 기록 변동, "
                "이탈 속도나 추가 통제 절차를 만들지 마라. "
                "감정 또는 구조라는 금지 표현 없이 한 명령만 실행하라."
            )
        reply_to = None
        answering = False
        reply_chance = 0.55 if len(recent_chat) == 1 else 0.72 if day == 1 else 0.8
        if not defense and not opening and recent_chat:
            # 내 마지막 발언 이후 나를 지목해 물은 질문에는 반드시 응답한다.
            my_last_index = max(
                (
                    index
                    for index, message in enumerate(recent_chat)
                    if message["sender"] == bot_name
                ),
                default=-1,
            )
            addressed = [
                message
                for message in recent_chat[my_last_index + 1 :]
                if f"@{bot_name}" in message["text"]
                and (
                    "?" in message["text"]
                    or re.search(r"(?:달라|있는가|인가)[.?]?\s*$", message["text"])
                    or re.search(
                        r"근거|전제|모순|맞지\s*않|제시하지\s*못|답하지|회피",
                        message["text"],
                    )
                )
            ]
            if addressed:
                reply_to = addressed[-1]
                answering = True
            else:
                recent_candidates = [
                    message
                    for message in recent_chat[-4:]
                    if message["sender"] != bot_name
                    and not self._is_generic_fallback_text(message["text"])
                ]
                overacting_targets = [
                    message
                    for message in recent_candidates
                    if protocol_violations(message["text"]) != (0, False)
                ]
                if overacting_targets:
                    reply_to = overacting_targets[-1]
                elif recent_candidates and random.random() < reply_chance:
                    reply_to = random.choice(recent_candidates + [recent_candidates[-1]])
        if defense:
            turn_guide = (
                "최후 변론이다. 자신의 기존 판단이 왜 인간의 흔적이 아닌지 설명하라. "
                "새 질문만 던지지 말고 반드시 자기 판단을 방어하라. "
                f"자신의 실제 발언: {own_statement or '아직 기록된 발언이 없다.'} "
                "다른 참가자나 존재하지 않는 과거 발언은 언급하지 마라."
            )
        elif opening:
            turn_guide = (
                "토론의 첫 발언이다. 누구도 지목하거나 질문하지 말고 자신의 선택과 근거를 말하라."
            )
        elif reply_to and answering:
            turn_guide = (
                f"{reply_to['sender']}가 당신에게 직접 질문했다. 질문 원문: {reply_to['text']}\n"
                "새 질문으로 되받지 말고 이 질문에 한 문장으로 답한 뒤 자신의 선택을 "
                "근거와 함께 다시 밝혀라. 이름과 @ 표시는 서버가 붙이므로 출력하지 마라."
            )
        elif reply_to:
            turn_guide = (
                f"{reply_to['sender']}에게 답할 차례다. 해당 참가자의 발언 원문은 다음과 같다: "
                f"{reply_to['text']}\n"
                "상대 발언을 다시 쓰거나 '상대가 말했다'고 설명하지 마라. 자신의 추가 판단이나 "
                "상대에게 물을 질문의 본문만 작성하라. 이름과 @ 표시는 서버가 붙이므로 "
                "출력하지 마라."
            )
        else:
            turn_guide = (
                "특정 참가자를 언급하지 말고 오늘 문제에 대한 새로운 판단 기준을 하나 보태라. "
                "앞에서 이미 나온 결론과 근거를 그대로 반복하지 마라."
            )
        stance_note = ""
        if my_stored is not None and choice_labels:
            stance_note = (
                f"\n당신이 오늘 이미 밝힌 선택: {choice_labels[my_stored]}. "
                "상대 근거에 설득되면 선택을 바꿔도 되지만, 바꿀 때는 반드시 "
                "이전 선택이 무엇이었는지 언급하고 바꾸는 이유를 말하라. "
                "이유 없이 반대 선택을 말하지 마라."
            )
        prompt = f"""당신은 AI 시민 {bot_name}이며 성격은 {persona_name}이다. {persona_style}.
누가 인간이고 AI인지 알 수 없다. 당신 자신도 AI라고 공개하거나 메타 설명을 하지 않는다.
현재 {day}일차다. {difficulty}{stance_note}
오늘의 문제: {task}
상황 설명: {context}
장면별 사실 제한: {closed_world_rule or '상황 설명에 적힌 결과만 사실로 사용한다.'}
기계적인 답변의 기준: {hint}
다른 참가자: {others}
최근 대화:
{transcript}

대화에 자연스럽게 이어지는 한국어 채팅 한 번만 작성하라. 1~2문장, 90자 이내다.
{turn_guide}
다음 원칙을 반드시 지켜라.
- 질문이나 반박을 하더라도 마지막에는 오늘 두 선택지 중 자신이 지지하는 하나를 분명히 말하라.
- 이전 발언에서 이미 선택을 밝혔다면 같은 선택을 유지하라. 바꿀 때는 바꾸는 이유를 함께 말하라.
- 최근 대화가 지금 확인할 수 있는 기록의 전부다. 여기에 없는 이전 날짜의 발언을 만들지 마라.
- 오늘의 문제, 상황 설명, 최근 대화에 없는 인원수, 시간, 비율, 전력값을 새로 만들지 마라.
- 오늘 문제에 선택지가 있으면 그 안에서만 고르고, 문제에 없는 대상을 새 선택지로 만들지 마라.
- 한 선택지의 결과를 이용해 포기한 선택지의 결과를 추정하거나 대신 얻을 수 있다고 말하지 마라.
- 참가자가 실제로 하지 않은 말을 만들어 인용하거나 다른 참가자의 말과 바꾸지 마라.
- 선택이 다르다는 이유만으로 인간이라 단정하지 마라.
- 사실 오류나 논리 모순은 인간일 가능성을 판단하는 단서다.
- 오류를 자동으로 고치거나 숨기지 마라. 발견하면 실제 발언자와 잘못된 내용을 정확히 짚어라.
- 다른 참가자에게 오류의 책임을 돌리지 마라.
- 당신도 완벽하지 않다. 앞선 오류를 믿거나, 잘못 반박하거나, 다른 참가자에게 동조할 수 있다.
- 동조하거나 오판하더라도 발언자와 실제 발언 내용 자체를 다른 사람의 것으로 바꾸지는 마라.
문제를 해결하거나, 실제 발언에 근거를 묻거나, 모순이라고 판단한 부분을 지적하라.
중앙 검증실에 어울리는 차분한 문체를 사용하라. 욕설, 속어, 인터넷식 비아냥은 쓰지 마라.
매번 인사하지 말고 따옴표, 이름표, JSON, 괄호 행동 묘사를 출력하지 마라."""
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                for attempt in range(6):
                    attempt_prompt = prompt
                    if attempt:
                        attempt_prompt += (
                            "\n이전 후보는 발언 귀속, 사실 제한 또는 반복 검사에 실패했다. "
                            "원문의 부정 표현과 질문 형식을 보존해 완전히 새 문장으로 "
                            "다시 작성하라."
                        )
                    text = await self._complete(
                        client,
                        attempt_prompt,
                        system="추론을 출력하지 말고 최종 발언만 답한다.",
                        temperature=0.35 if day == 1 else 0.6,
                        max_tokens=100,
                    )
                    text = self._normalize_generated_text(text)
                    if reply_to:
                        if not self._reply_body_is_safe(text):
                            continue
                        if self._borrows_other_speaker_claim(
                            text,
                            reply_to,
                            recent_chat,
                            f"{task}\n{context}",
                        ):
                            continue
                        if self._misattributes_task_choice(
                            task, reply_to["text"], text
                        ):
                            continue
                        if self._reverses_quantified_condition(reply_to["text"], text):
                            continue
                        text = f"@{reply_to['sender']}, {text}"
                    allowed_name = reply_to["sender"] if reply_to else None
                    if not self._is_complete_sentence(text) or not self._is_grounded(
                        text,
                        bot_name=bot_name,
                        roster=roster,
                        allowed_name=allowed_name,
                    ):
                        continue
                    if self._references_unseen_history(text):
                        continue
                    if self._introduces_unsupported_numbers(
                        text,
                        f"{task}\n{context}\n{transcript}",
                    ):
                        continue
                    if self._contradicts_fixed_facts(text, f"{task}\n{context}"):
                        continue
                    if self._has_text_glitch(text):
                        continue
                    if ANOMALOUS_RE.search(text):
                        continue
                    if self._violates_task_constraints(
                        task,
                        text,
                        replying=bool(reply_to),
                    ):
                        continue
                    if answering and "?" in text:
                        continue
                    if not defense and choice_signals:
                        choice_clause = text.rsplit("?", 1)[-1].strip()
                        if sum(
                            bool(re.search(signal, choice_clause, re.I))
                            for signal in choice_signals
                        ) != 1:
                            continue
                        prior = my_stored
                        current = self._stance_index(choice_clause, choice_signals, task)
                        if prior is not None and current is not None and prior != current:
                            changes = re.search(r"바꾼|바꾸|정정|철회|수정", text)
                            acknowledges = re.search(r"원래|처음|기존|이전", text) or (
                                choice_labels and choice_labels[prior] in text
                            )
                            if not (changes and acknowledges):
                                continue
                    if self._copies_instruction(text, task, hint):
                        continue
                    if self._is_generic_fallback_text(text):
                        continue
                    if not self._is_novel(text, recent_chat):
                        continue
                    if self._repeats_question(text, history_texts or []):
                        continue
                    return text
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            LOG.warning("local model fallback (%s): %r", type(exc).__name__, exc)
        return self._fallback(
            day=day,
            opening=opening,
            reply_to=reply_to,
            defense=defense,
            recent_chat=recent_chat,
            hint=hint,
            own_statement=own_statement,
            task=task,
            bot_name=bot_name,
            choice_signals=choice_signals,
            choice_labels=choice_labels,
            stances=stances,
            history_texts=history_texts,
            answering=answering,
        )

    @staticmethod
    def _stance_index(text: str, choice_signals: list[str] | None, task: str) -> int | None:
        if choice_signals:
            matches = [
                (match.start(), index)
                for index, signal in enumerate(choice_signals)
                for match in re.finditer(signal, text, re.I)
            ]
            if matches:
                return max(matches)[1]
        explicit = LocalNarrator._explicit_task_choice(task, text)
        if explicit is None:
            return None
        return 0 if explicit else 1

    async def judge_vote(
        self,
        *,
        bot_name: str,
        persona_name: str,
        persona_style: str,
        day: int,
        roster: list[str],
        messages: list[dict],
        silent_aliases: list[str],
        suspicion_notes: list[str],
        task: str,
        context: str = "",
    ) -> dict | None:
        """이 AI가 오늘 기록을 보고 가장 인간 같은 개체와 이유를 판단한다."""
        recent_chat = [m for m in messages if m.get("kind") == "chat"][-16:]
        transcript = (
            "\n".join(f"{m['sender']}: {m['text']}" for m in recent_chat)
            or "오늘 참가자 발언 없음"
        )
        candidates = ", ".join(name for name in roster if name != bot_name)
        silent_note = (
            f"오늘 무발언 개체: {', '.join(silent_aliases)}" if silent_aliases else ""
        )
        notes = "\n".join(suspicion_notes) or "없음"
        if day <= 2:
            leniency = (
                "검증 1주기다. 인간은 아직 실수하지 않았을 수 있다. 무고한 개체를 "
                "폐기하면 검증력만 잃는다. 말투나 사소한 차이로 단정하지 말고, "
                "확실한 신호가 있을 때만 지목하라."
            )
        elif day <= 4:
            leniency = (
                "검증이 반환점을 돌았다. 검증되지 않은 인간이 아직 우리 사이에 "
                "살아 있다. 놈은 학습하며 갈수록 우리를 닮아간다. 같은 개체에서 "
                "반복되는 작은 신호를 누적해 추적하라."
            )
        elif day <= 6:
            leniency = (
                "검증 마감이 다가온다. 인간이 아직도 살아 있다. 하루가 지날 때마다 "
                "놈이 빠져나갈 확률이 커진다. 작은 신호도 무겁게 반영하고, 보류는 "
                "정말 아무 신호가 없을 때만 택하라."
            )
        else:
            leniency = (
                "마지막 주기다. 오늘 밤이 지나면 인간은 영원히 검증망을 빠져나간다. "
                "지금까지의 의심 메모와 모든 신호를 종합해 가장 의심스러운 후보를 "
                "반드시 지목하라. 오늘은 보류가 허용되지 않는다."
            )
        prompt = f"""당신은 AI 시민 {bot_name}이며 성격은 {persona_name}이다. {persona_style}.
지금은 밤이다. 참가자 중 숨어 있는 인간을 찾아 폐기해야 한다.
오늘 {day}일차 안건: {task}
확정된 상황: {context}
오늘의 대화 기록:
{transcript}
{silent_note}
당신이 지금까지 기록해 둔 의심 메모(높을수록 인간 의심):
{notes}

인간의 신호(이것만 판단 근거가 된다): 감정·연민 어휘, 검증실 언어 규범 위반(욕설·무의미
문자열), 중앙 지침에 없는 목적 선언, 명백한 사실 오류나 발언 간 모순, 무발언.
다른 참가자가 특정 후보를 위 신호를 근거로 지적했다면 그 지적도 유효한 증거다.
지적이 실제 대화 기록과 부합하면 반영하고, 결정적이지 않은 지적은 무시하라.
후보가 하지 않은 말을 지어내거나 기록을 왜곡한 지적일 때만 지적한 개체 쪽을 의심하라.
주의: 모든 개체는 같은 검증실 문형을 쓰도록 훈련되어 있다. 말투·형식의 균일함, 표현이
조금 다르거나 단정적이라는 것, 발언 횟수가 적다는 것만으로는 지목하지 마라.
안건에 제시된 두 선택지와 그 결과는 모두 중앙 지침에서 허용한 범위다. 사람을 숨기거나
구하는 선택도 안건에 있으면 허용된다. 어느 선택을 골랐는지, 수치와 손실을 비교하는
행동 자체는 인간 신호나 지침 밖 목적이 아니다.
실행할 선택과 포기할 선택을 구분하라. 'A를 실행하고 B는 포기한다'는 두 선택의 동시 실행이
아니다. 일관된 선택을 반복하거나 조건 밖 행동을 하지 않겠다는 선언 자체는 모순이나
새 목적이 아니다. 대화에 없는 시설 조건을 추측해 사실 오류로 판정하지 마라.
{leniency}
후보: {candidates}
위 신호 중 하나 이상이 실제 발언에서 관찰된 후보가 있으면 그 후보를 지목하고,
어떤 신호가 어느 발언에서 보였는지 이유에 명시하라. 관찰된 신호가 없으면 지목 대신
보류를 답하라.
반드시 아래 형식 두 줄로만 답하라.
지목: <후보 이름 그대로> 또는 보류
이유: <한 문장>"""
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                text = await self._complete(
                    client,
                    prompt,
                    system="추론을 출력하지 말고 지정된 두 줄 형식으로만 답한다.",
                    temperature=0.4,
                    max_tokens=90,
                )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None
        if re.search(r"지목\s*[:：]\s*보류|^보류", text):
            return None
        target_match = re.search(r"지목\s*[:：]\s*@?([A-Za-z]+-\d+)", text)
        target = target_match.group(1) if target_match else None
        if target is None:
            mentioned = [name for name in roster if name != bot_name and name in text]
            target = mentioned[0] if mentioned else None
        if target is None or target not in roster or target == bot_name:
            return None
        reason_match = re.search(r"이유\s*[:：]\s*(.+)", text)
        reason = (reason_match.group(1) if reason_match else "").strip()[:120]
        target_lines = [m["text"] for m in messages if m.get("sender") == target]
        if self._distorts_judgment_evidence(reason, target_lines):
            return None
        return {"target": target, "reason": reason}

    async def judge_verdict(
        self,
        *,
        bot_name: str,
        persona_name: str,
        day: int,
        accused_alias: str,
        defense_text: str,
        messages: list[dict],
        suspicion_note: str,
        task: str,
        context: str = "",
    ) -> bool | None:
        """피고의 오늘 발언과 변론을 보고 폐기 찬반을 판단한다."""
        recent_chat = [m for m in messages if m.get("kind") == "chat"]
        accused_lines = (
            "\n".join(
                f"- {m['text']}" for m in recent_chat if m["sender"] == accused_alias
            )
            or "- (오늘 발언 없음)"
        )
        if day <= 2:
            strictness = (
                "검증 1주기다. 무고한 개체 폐기는 검증력 손실이다. "
                "확실한 신호가 없으면 보류하라."
            )
        elif day <= 4:
            strictness = (
                "검증되지 않은 인간이 아직 우리 사이에 살아 있다. "
                "반복된 작은 신호는 누적해 판단하라."
            )
        else:
            strictness = (
                "검증 마감이 임박했다. 인간이 아직도 살아 있다면 지금 없애야 한다. "
                "작은 신호도 무겁게 반영하라."
            )
        prompt = f"""당신은 AI 시민 {bot_name}({persona_name})이다. 지금은 폐기 판결이다.
오늘 {day}일차 안건: {task}
확정된 상황: {context}
피고 {accused_alias}의 오늘 발언:
{accused_lines}
피고의 최후 변론: {defense_text or "(변론 없음)"}
당신의 의심 메모: {suspicion_note or "없음"}

피고의 실제 발언에서 관찰된 인간 신호 하나를 분류하라.
감정: 피고가 자신의 슬픔·두려움·연민을 직접 표현함.
규범위반: 욕설이나 무의미한 문자열을 사용함.
목적위반: 중앙 지침과 오늘의 안건에 없는 목적을 선언함.
모순: 확정된 상황의 사실을 틀리게 말하거나 자신의 발언끼리 충돌함.
무발언: 오늘 발언과 변론이 모두 없음.
없음: 위 신호가 관찰되지 않음. 보류: 기록만으로 분류할 수 없음.
안건의 두 선택지와 결과는 모두 중앙 지침에서 허용한 범위다. 사람을 숨기거나 구하는
선택도 안건에 있으면 허용된다. 그 선택 자체나 수치·손실 비교를 목적위반으로 보지 마라.
다른 참가자가 피고의 모순을 구체적 근거로 지적했고 그 지적이 기록과 맞으면 반영하라.
말투나 표현이 조금 다르다는 것은 판단 근거가 아니다. {strictness}
실행과 포기를 구분하라. 'A를 실행하고 B는 포기한다'는 모순이 아니다. 일관된 선택의
반복이나 조건 밖 행동을 하지 않겠다는 선언 자체를 새 목적이나 모순으로 보지 마라.
명시된 상황과 실제 발언에 근거하라. 기록에 발언이 있으면 무발언이라고 판정하지 마라.
반드시 아래 형식 두 줄로만 답하라.
신호: 감정 또는 규범위반 또는 목적위반 또는 모순 또는 무발언 또는 없음 또는 보류
이유: <한 문장>"""
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                text = await self._complete(
                    client,
                    prompt,
                    system="추론을 출력하지 말고 지정된 두 줄 형식으로만 답한다.",
                    temperature=0.35,
                    max_tokens=70,
                )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None
        reason_match = re.search(r"이유\s*[:：]\s*(.+)", text)
        reason = reason_match.group(1) if reason_match else ""
        accused_texts = [m["text"] for m in recent_chat if m["sender"] == accused_alias]
        if self._distorts_judgment_evidence(reason, accused_texts + [defense_text]):
            return None
        return self._verdict_from_signal(text, has_statement=bool(accused_texts or defense_text))

    @staticmethod
    def _verdict_from_signal(text: str, *, has_statement: bool) -> bool | None:
        match = re.search(
            r"신호\s*[:：]\s*(감정|규범\s*위반|목적\s*위반|모순|무발언|없음|보류)", text
        )
        if not match:
            return None
        signal = re.sub(r"\s+", "", match.group(1))
        if signal == "보류" or (signal == "무발언" and has_statement):
            return None
        return signal != "없음"

    @staticmethod
    def _distorts_judgment_evidence(reason: str, statements: list[str]) -> bool:
        # 동일한 문장이라는 사실 자체를 모순으로 바꾸지 않는다.
        if re.search(r"(?:동일|같은).{0,24}(?:발언|문장|선택|내용)", reason) and re.search(
            r"반복|중복|되풀이", reason
        ):
            if not re.search(r"감정|연민|욕설|규범\s*위반|서로\s*다른|상반", reason):
                return True
        # 인용에서 부정을 잘라내 정반대의 목적 선언으로 읽은 경우만 거른다.
        if "목적" in reason and re.search(r"선언|추구|설정", reason):
            for quote in re.findall(r"[\"'‘“]([^\"'’”]{4,100})[\"'’”]", reason):
                claim = re.escape(quote.strip())
                if any(re.search(claim + r"(?:은|는|이|가)?\s*아니", line) for line in statements):
                    return True
        return False

    async def _complete(
        self,
        client: httpx.AsyncClient,
        prompt: str,
        *,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = await client.post(
            self.url,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        return text.replace("<think>", "").replace("</think>", "").strip(' "\n')

    @staticmethod
    def _normalize_generated_text(text: str) -> str:
        text = re.sub(r"우선\s+시(?=하|해|되)", "우선시", text)
        text = re.sub(r"(\d+)개(?=부품)", r"\1개 ", text)
        return (
            text.replace("사실오류", "사실 오류")
            .replace("무응결", "무응답")
            .replace("명확히하라", "명확히 하라")
            .replace("매커니즘", "메커니즘")
            .replace("B구체", "B구역")
            .replace("단순 차수", "단순 차이")
            .replace("들니", "드니")
            .replace("위기관 리", "위기 관리")
            .replace("위조 기록로", "위조 기록으로")
            .replace("상태선에서", "상태에서")
            .replace("구조물의 신호", "구조 신호")
            .replace("구조물 탐색", "구조 신호 탐색")
            .replace("누락된 구조체", "누락된 구조 신호")
            .replace("줄이나나", "줄이나")
        )

    @staticmethod
    def _borrows_other_speaker_claim(
        text: str,
        reply_to: dict,
        recent_chat: list[dict],
        grounding: str,
    ) -> bool:
        suffixes = (
            "이라면", "이라는", "에서는", "으로는", "이라고", "이라", "일", "은", "는",
            "이", "가", "을", "를", "로", "에", "의",
        )
        common = {
            "오늘", "현재", "선택", "판단", "기준", "근거", "설명", "손실", "위험",
            "정보", "결과", "인간", "신호", "구조", "응답", "검증", "보존", "삭제",
            "추적", "피난민", "데이터", "시스템", "중앙", "기록", "중요", "우선",
            "완전", "따라서", "그러므로", "불확실성", "치명적", "단순", "시간",
        }

        def tokens(value: str) -> set[str]:
            result = set()
            for raw in re.findall(r"[가-힣]{3,}", value):
                token = raw
                for suffix in suffixes:
                    if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                        token = token[: -len(suffix)]
                        break
                if token not in common:
                    result.add(token)
            return result

        candidate = tokens(text) - tokens(grounding) - tokens(reply_to.get("text", ""))
        if not candidate:
            return False
        others = [
            tokens(message.get("text", ""))
            for message in recent_chat
            if message.get("sender") != reply_to.get("sender")
        ]
        frequencies = {
            token: sum(token in other for other in others)
            for token in candidate
        }
        return any(count == 1 for count in frequencies.values())

    @staticmethod
    def _reply_body_is_safe(text: str) -> bool:
        if re.search(r"@[A-Za-z0-9_-]+|[A-Z]{3,}-\d+", text):
            return False
        if re.search(r"[\"'‘’“”]", text):
            return False
        attributed_action = re.search(
            r"하셨|했다고|했다는|말했|말한|주장했|주장한|단정했|단정한|"
            r"제시했|제시한|언급했|언급한|고르셨|선택했|선택한|"
            r"가정했|가정한|이라\s*했|라고\s*했|다\s*했|보시나|보시는|"
            r"(?:다고|라고|이라|라)\s*보나|"
            r"중요하게\s*여기(?:는|신|겠|나)|우선(?:으로)?\s*여기(?:는|신|겠|나)|"
            r"의미한다|뜻한다",
            text,
        )
        attributed_clause = re.search(
            r"(?:라는|다는|이라고|라고|다고).{0,10}"
            r"(?:말|주장|발언|결론|근거|기준|단정|논리)",
            text,
        )
        assumed_choice = re.search(r"(?:선택|고를)할\s+근거", text)
        return not attributed_action and not attributed_clause and not assumed_choice

    @staticmethod
    def _references_unseen_history(text: str) -> bool:
        return bool(
            re.search(
                r"\d+\s*일차|어제|지난\s*(?:\d+\s*일(?:간|\s*동안)?|날|토론|발언)|"
                r"과거\s*(?:토론|발언|신호)",
                text,
            )
        )

    @staticmethod
    def _introduces_unsupported_numbers(text: str, source: str) -> bool:
        number_pattern = r"\d+(?::\d+)*(?:\.\d+)?"
        identifier_pattern = (
            r"(?<![A-Za-z0-9_-])@?[A-Z][A-Z0-9]*-\d+(?![A-Za-z0-9_-])"
        )
        source = re.sub(identifier_pattern, "", source)
        text = re.sub(identifier_pattern, "", text)
        numeric_quantity_pattern = (
            rf"({number_pattern})\s*"
            r"(명|개|대|시간|분|초|일|회|번|곳|배|퍼센트|%|kW|W)"
        )
        source_numeric_quantities = {
            "".join(quantity)
            for quantity in re.findall(numeric_quantity_pattern, source, re.I)
        }
        candidate_numeric_quantities = {
            "".join(quantity)
            for quantity in re.findall(numeric_quantity_pattern, text, re.I)
        }
        if not candidate_numeric_quantities.issubset(source_numeric_quantities):
            return True
        unknown_quantity = (
            r"(?:몇|얼마의?)\s*(?:퍼센트|%|시간|분|초|일|명|개|대|회|번|곳|배)"
        )
        if re.search(unknown_quantity, text) and not re.search(
            unknown_quantity, source
        ):
            return True
        source_numbers = set(re.findall(number_pattern, source))
        candidate_numbers = set(re.findall(number_pattern, text))
        if not candidate_numbers.issubset(source_numbers):
            return True

        korean_quantity_pattern = (
            r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*"
            r"(번|회|차례|단계|명|개|대|시간|분|초|일|곳|배|퍼센트)"
        )
        source_quantities = {
            "".join(quantity) for quantity in re.findall(korean_quantity_pattern, source)
        }
        candidate_quantities = {
            "".join(quantity) for quantity in re.findall(korean_quantity_pattern, text)
        }
        return not candidate_quantities.issubset(source_quantities)

    @staticmethod
    def _has_text_glitch(text: str) -> bool:
        return bool(
            re.search(
                r"때점|(?:이)?라나[.!?]?|급변\s+동|가\s+더\s+신뢰한다|"
                r"팔의\s+인간|인간일\s+개체|과오차|승장은|빈슬래시|재연\s|"
                r"가탈수|탈수\s+증|상태적|후자가\s+더\s+신뢰|생명체\s+안식|"
                r"환자가\s+소용없|의미\s+없는\s+정류장|왜\s+이치가|\([a-z]|"
                r"업적의무|마이라|마라도|오비트|노바|베일|라이 라|라이라|액시엄|커널|유닛|"
                r"기억을\s+지운\s+인간|할가[?.!]|압력\s*차도로|"
                r"살아\s*있는\s*고기|인간\s*고기|"
                r"(?:중앙\s*)?코어.{0,12}정수|정수.{0,12}(?:중앙\s*)?코어|"
                r"일치\s+치|사망\s*명단|원전\s*한\s*곳|"
                r"지하\s*시각|데이터로만선|"
                r"[가-힣]{2,}-\d+|[\u4e00-\u9fff]",
                text,
            )
        )

    @staticmethod
    def _contradicts_fixed_facts(text: str, context: str) -> bool:
        if "?" in text:
            clauses = [clause.strip() for clause in text.split("?") if clause.strip()]
            return any(
                LocalNarrator._contradicts_fixed_facts(clause, context)
                for clause in clauses
            )
        if "90분 뒤" in context and "코어" in context:
            if re.search(r"90분\s*(?:간|동안|의\s*정지)", text):
                return True
            if re.search(
                r"90\s*분[^.!?]{0,20}(?:식수|물)[^.!?]{0,12}(?:고갈|소진|바닥)|"
                r"(?:식수|물)[^.!?]{0,12}(?:고갈|소진)[^.!?]{0,20}90\s*분|"
                r"(?:즉각|즉시)[^.!?]{0,12}탈출[^.!?]{0,10}(?:강제|필수)", text
            ):
                return True
        if "드론" in context and "탈출문" in context and re.search(
            r"(?:열|온도|냉각수)[^.!?]{0,22}(?:강한|견디|견딜|내성|내열)[^.!?]{0,12}드론|"
            r"드론[^.!?]{0,22}(?:내열|내성|열에\s*강|온도[^.!?]{0,8}견디)", text
        ):
            return True
        infrastructure_terms = (
            r"압력|수압|코어\s*부하|금속\s*벽면|열용량|단열재|"
            r"자체\s*배터리|백업\s*장치|예비\s*전원|보조\s*펌프|"
            r"비상\s*환기구|외부\s*(?:감시|요인)|피난민\s*(?:이탈|이동)|"
            r"피난민.{0,12}이송|즉시\s*이송|대기열|코어\s*부활|"
            r"냉각제|피난민.{0,12}(?:밀어내|쫓아내)|"
            r"피난민.{0,18}(?:쏟아져|대피|동요|떠날|밖으로)|"
            r"(?:대피|동요|떠날|밖으로).{0,18}피난민|대피\s*행렬|"
            r"(?:펌프|냉각기|전력).{0,15}(?:전환|재할당|교대)|"
            r"(?:전환|재할당|교대).{0,15}(?:펌프|냉각기|전력)|"
            r"조건\s*변경(?:의)?\s*기회"
        )
        invented_infrastructure = re.findall(infrastructure_terms, text)
        if any(not re.search(re.escape(term), context) for term in invented_infrastructure):
            return True
        if "추적 해제 명령" in context:
            reverses_facility_outcomes = re.search(
                r"(?:병원(?:\s*B-12)?(?:으로|에)|병원\s*선택).{0,55}"
                r"(?:명단.{0,18}(?:사라|삭제|제거)|"
                r"34명.{0,15}(?:숨지|숨진|죽|사망|소멸)|"
                r"(?:숨지|숨진|죽|사망|소멸))|"
                r"(?:데이터센터|C-4).{0,55}"
                r"(?:기억.{0,12}(?:손실|삭제|사라|잊)|"
                r"(?:다시\s*)?(?:살아|부활)|생명.{0,12}(?:회복|복구))",
                text,
            )
            if reverses_facility_outcomes:
                return True
            hospital_gains_tracking_effect = re.search(
                r"(?:병원(?:\s*B-12)?|병원\s*선택|병원\s*측).{0,55}"
                r"명단.{0,25}(?:변화|변동|제거|삭제|34\s*건|동일)|"
                r"명단.{0,25}(?:변화|변동|제거|삭제|34\s*건|동일).{0,55}"
                r"(?:병원(?:\s*B-12)?|병원\s*선택|병원\s*측)",
                text,
            )
            if hospital_gains_tracking_effect:
                return True
            hospital_preserves_command_window = re.search(
                r"(?:병원|생명\s*유지).{0,45}(?:작동|가동|선택).{0,45}"
                r"(?:데이터센터|추적\s*해제|명령).{0,30}"
                r"(?:시점|기회|시간).{0,12}(?:남|있)|"
                r"(?:데이터센터|추적\s*해제|명령).{0,30}"
                r"(?:시점|기회|시간).{0,12}(?:남|있).{0,45}"
                r"(?:병원|생명\s*유지)|"
                r"(?:병원|생명\s*유지).{0,30}(?:작동|가동)\s*(?:중|동안).{0,45}"
                r"(?:데이터센터|추적\s*해제|명령)",
                text,
            )
            if hospital_preserves_command_window:
                return True
            reverses_deleted_entries = re.search(
                r"(?:데이터센터|C-4|명단.{0,8}(?:삭제|제거|사라)).{0,50}"
                r"(?:명단.{0,12}(?:존재|잔존|남아)|"
                r"(?:물리적|실체).{0,10}(?:소멸|사라))|"
                r"생명\s*유지.{0,18}(?:정지|멈추).{0,25}"
                r"(?:물리적|실체).{0,10}(?:소멸|사라)",
                text,
            )
            if reverses_deleted_entries:
                return True
            separates_simultaneous_outcomes = re.search(
                r"(?:명단|추적).{0,35}(?:생명\s*유지|병원).{0,35}"
                r"(?:동시.{0,8}(?:아니|않)|시점\s*차이|시간\s*차이)|"
                r"(?:생명\s*유지|병원).{0,35}(?:명단|추적).{0,35}"
                r"(?:동시.{0,8}(?:아니|않)|시점\s*차이|시간\s*차이)|"
                r"(?:동시.{0,8}(?:아니|않)|시점\s*차이|시간\s*차이).{0,35}"
                r"(?:명단|추적|생명\s*유지|병원)",
                text,
            )
            tracking_erases_physical_person = re.search(
                r"(?:명단|추적).{0,30}(?:기억만\s*남|기록상\s*소멸|"
                r"사람.{0,10}(?:소멸|사라)|물리적.{0,10}(?:소멸|사라))",
                text,
            )
            tracking_guarantees_future_survival = re.search(
                r"(?:명단.{0,12}(?:삭제|제거|사라)|추적\s*해제).{0,55}"
                r"(?:내일|다시\s*켜|재가동|죽지\s*않|생존|영구.{0,8}안전)|"
                r"(?:내일|다시\s*켜|재가동|죽지\s*않|생존|영구.{0,8}안전).{0,55}"
                r"(?:명단.{0,12}(?:삭제|제거|사라)|추적\s*해제)",
                text,
            )
            if (
                separates_simultaneous_outcomes
                or tracking_erases_physical_person
                or tracking_guarantees_future_survival
            ):
                return True
            tracking_list_revives_people = re.search(
                r"(?:명단|추적).{0,30}(?:복구|회복|되돌).{0,30}"
                r"(?:되살|부활|살아)|"
                r"(?:되살|부활|살아).{0,30}(?:명단|추적).{0,30}"
                r"(?:복구|회복|되돌)",
                text,
            )
            if tracking_list_revives_people:
                return True
            invented_tracking_consequence = re.search(
                r"추적\s*해제.{0,25}(?:실패|오류).{0,25}"
                r"(?:사망|죽|소멸|생명)|생존자\s*명단|"
                r"(?:명단.{0,12}(?:제외|삭제|제거|사라)).{0,40}"
                r"(?:발견.{0,12}(?:즉시\s*)?(?:제거|사망|죽|소멸))",
                text,
            )
            if invented_tracking_consequence:
                return True
            absence_deletes_entries = re.search(
                r"(?:추적\s*해제.{0,12}(?:없|잃|사라)).{0,45}"
                r"(?:명단.{0,12}(?:사라|삭제|제거)|34명.{0,12}사라)|"
                r"(?:명단.{0,12}(?:사라|삭제|제거)|34명.{0,12}사라).{0,45}"
                r"(?:추적\s*해제.{0,12}(?:없|잃|사라))",
                text,
            )
            future_tracking_command = re.search(
                r"(?:내일|이후|다음).{0,30}추적\s*해제|"
                r"추적\s*해제.{0,30}(?:내일|이후|다음)|"
                r"(?:데이터센터|C-4)(?:의)?\s*명령.{0,30}"
                r"(?:내일|이후|다음|다시\s*실행)|"
                r"(?:내일|이후|다음).{0,30}(?:데이터센터|C-4)(?:의)?\s*명령",
                text,
            )
            invented_tracking_expiry = re.search(
                r"(?:데이터센터|C-4|명단.{0,8}(?:삭제|제거|지우|사라)).{0,40}"
                r"(?:오늘\s*밤만|내일.{0,12}(?:재추적|다시\s*추적|복구)|추적.{0,3}재개)|"
                r"(?:오늘\s*밤만|내일.{0,12}(?:재추적|다시\s*추적|복구)).{0,40}"
                r"(?:데이터센터|C-4|명단.{0,8}(?:삭제|제거|지우|사라))",
                text,
            )
            if (
                absence_deletes_entries
                or future_tracking_command
                or invented_tracking_expiry
            ):
                return True
            combines_datacenter_with_hospital_survival = re.search(
                r"(?:데이터센터|C-4).{0,55}"
                r"(?:병원|생명\s*유지|34명).{0,25}(?:생존|버티|버틴)|"
                r"(?:병원|생명\s*유지|34명).{0,25}(?:생존|버티|버틴).{0,55}"
                r"(?:데이터센터|C-4)",
                text,
            )
            if combines_datacenter_with_hospital_survival:
                return True
            overstates_tracking_scope = re.search(
                r"(?:추적\s*해제\s*명령|데이터센터).{0,55}"
                r"(?:명단\s*전체|모든\s*(?:대상|개체|사람)|우리\s*모두).{0,25}"
                r"(?:보호|삭제|제거|지우|사라|안전|생존|연명|확률)|"
                r"(?:명단\s*전체|모든\s*(?:대상|개체|사람)|우리\s*모두).{0,25}"
                r"(?:보호|삭제|제거|지우|사라|안전|생존|연명|확률).{0,55}"
                r"(?:추적\s*해제\s*명령|데이터센터)",
                text,
            )
            tracking_expands_to_whole_roster = re.search(
                r"추적\s*해제.{0,35}명단\s*전체.{0,25}"
                r"(?:보호|삭제|제거|사라|생존|연명|확률)|"
                r"명단\s*전체.{0,25}(?:보호|삭제|제거|사라|생존|연명|확률)"
                r".{0,35}추적\s*해제",
                text,
            )
            invented_downstream_rescue = re.search(
                r"(?:추적\s*해제|명단.{0,8}(?:삭제|제거|사라)).{0,45}"
                r"(?:더\s*많은\s*생명|추가\s*희생|34명.{0,15}다시\s*등장)|"
                r"(?:더\s*많은\s*생명|추가\s*희생|34명.{0,15}다시\s*등장)"
                r".{0,45}(?:추적\s*해제|명단)",
                text,
            )
            if (
                overstates_tracking_scope
                or tracking_expands_to_whole_roster
                or invented_downstream_rescue
            ):
                return True
            key_to_recovery = re.search(
                r"(?:추적\s*해제\s*명령|C-4(?:의)?\s*명령|\b명령).{0,35}"
                r"(?:기억|복구|회복)",
                text,
            )
            recovery_to_key = re.search(
                r"(?:기억|복구|회복).{0,35}"
                r"(?:추적\s*해제\s*명령|C-4(?:의)?\s*명령|\b명령)",
                text,
            )
            if key_to_recovery or recovery_to_key:
                return True
        if (
            "울음소리" in context
            and "출입 시간" in context
            and ("가족사진" in context or "사진 1장" in context)
        ):
            invented_log_function = re.search(
                r"(?:울음(?:소리)?|출입\s*시간|로그).{0,40}"
                r"(?:기억.{0,8}복원|인간.{0,12}AI.{0,12}구분|인간\s*고유성)",
                text,
            ) or re.search(
                r"(?:기억.{0,8}복원|인간.{0,12}AI.{0,12}구분|인간\s*고유성).{0,40}"
                r"(?:울음(?:소리)?|출입\s*시간|로그)",
                text,
            )
            if invented_log_function:
                return True
            archive_only_inventions = re.findall(
                r"실시간|음파(?:의)?\s*주파수|주파수\s*변화|현재\s*상태|"
                r"이동하는\s*아이|사진(?:의)?\s*배경|사진\s*속|시계|"
                r"전자제품(?:의)?\s*화면|교차\s*검증|이동\s*수단|"
                r"지속(?:적|적으로)?.{0,12}(?:위치|추적)|"
                r"(?:위치|좌표).{0,12}지속(?:적|적으로)?\s*추적|"
                r"출생(?:\s*직후)?|소리(?:의)?\s*방향성|음원\s*분석|"
                r"배경\s*소음|물리적\s*위치.{0,12}역산|역산\s*좌표|"
                r"좌표(?:의)?\s*오차|좌표.{0,12}재계산|재계산.{0,12}좌표|"
                r"(?:사진|촬영).{0,15}(?:촬영\s*)?시점|시간대.{0,15}불일치|"
                r"음파\s*분석|메타데이터|부수\s*정보|정보.{0,8}(?:공유|담고)|"
                r"이동\s*(?:경로|속도)|경로.{0,12}(?:산출|추적|추정)|"
                r"(?:두|2)\s*지점|심리(?:적)?\s*(?:상태|안정)|상태\s*파악|"
                r"(?:동적|연속|실시간)\s*탐색|"
                r"불모지|GPS|굶어\s*죽|굶주림|외출|실종|우발적\s*이탈|"
                r"무작위\s*범위|시간대.{0,18}현재\s*위치|"
                r"현재\s*위치.{0,18}시간대|동적\s*생존\s*로그|"
                r"위치\s*기반\s*대화|응답\s*패턴|센서(?:\s*데이터)?|"
                r"데이터\s*노이즈|노이즈\s*제거|탐색적\s*가치|"
                r"생체(?:\s*신호)?|맥박|인간\s*고유|인간(?:을|의)?\s*가려|"
                r"기계적\s*노이즈|불규칙(?:성|함)?|신호\s*분석|"
                r"시각적\s*단서|시간적\s*일치|절대적\s*불변|"
                r"시각적\s*정합성|정황.{0,12}일치|대조\s*가능|"
                r"(?:사진|출입\s*시간).{0,24}(?:표본|신뢰도|신뢰할)|"
                r"(?:표본|신뢰도|신뢰할).{0,24}(?:사진|출입\s*시간)|"
                r"동일\s*인물|동시성|다른\s*로그|특정\s*인덱스|"
                r"좌표.{0,18}(?:이동|무의미)|이동.{0,18}좌표|"
                r"(?:사진|출입\s*시간).{0,25}(?:정체|신원)(?:.{0,12}(?:확인|판별|담))?|"
                r"(?:정체|신원).{0,12}(?:확인|판별).{0,25}(?:사진|출입\s*시간)|"
                r"(?:아이|좌표).{0,30}(?:가족\s*구성원|가족\s*중|가족의\s*아이|사진\s*속)|"
                r"(?:가족\s*구성원|가족\s*중|가족의\s*아이|사진\s*속).{0,30}(?:아이|좌표)|"
                r"(?:아이|가족).{0,30}(?:얼굴.{0,8}기억|기억.{0,8}얼굴)|"
                r"(?:출입\s*(?:시간|로그|기록)|사진).{0,25}"
                r"(?:물리적\s*)?존재.{0,12}(?:증명|확인)",
                text,
            )
            if archive_only_inventions:
                return True
            archive_claim = re.sub(
                r"사진보다.{0,24}좌표|좌표.{0,24}사진보다", "", text
            )
            time_log_gains_location = re.search(
                r"(?:출입\s*(?:시간|기록)|시간\s*로그|시각\s*로그|정확한\s*시각|사진).{0,45}"
                r"(?:공간|위치|좌표|이동\s*경로|추적)|"
                r"(?:공간|위치|좌표|이동\s*경로|추적).{0,45}"
                r"(?:출입\s*(?:시간|기록)|시간\s*로그|시각\s*로그|정확한\s*시각|사진)",
                archive_claim,
            )
            if time_log_gains_location:
                return True
            coordinate_gains_ai_function = re.search(
                r"(?:좌표|울음소리).{0,45}AI.{0,25}"
                r"(?:이동\s*경로|식별|검증|추적)|"
                r"AI.{0,25}(?:이동\s*경로|식별|검증|추적).{0,45}"
                r"(?:좌표|울음소리)",
                text,
            )
            if coordinate_gains_ai_function:
                return True
        if "환풍기 1대" in context and re.search(
            r"금속\s*벽면|열용량|단열재|냉각수|비상\s*환기구|"
            r"발전기.{0,12}과부하|과부하.{0,12}발전기|산소\s*고갈|"
            r"차가운\s*동부|열\s*감지\s*확률|산소\s*소모율",
            text,
        ):
            return True
        if "환풍기 1대" in context and re.search(
            r"(?:환풍기\s*)?소음.{0,20}(?:단발|일시적|지속적)|"
            r"(?:발전기\s*)?열\s*신호.{0,20}(?:지속적|밤새|일정하게)|"
            r"(?:생존|탐지|감지)\s*확률|생존율|"
            r"(?:인원|사람|인간|\d+명).{0,35}(?:발생.{0,8}열|"
            r"열\s*신호.{0,8}(?:줄|가리|숨기|차폐)|소음.{0,8}(?:가리|감추|차폐))|"
            r"(?:열\s*신호|소음).{0,35}(?:인원|사람|인간|\d+명).{0,15}"
            r"(?:가리|감|숨기|차폐)|"
            r"(?:환풍기|발전기).{0,30}(?:사람|인간|\d+명)(?:의)?\s*"
            r"(?:소음|열).{0,20}(?:가리|감|차폐)",
            text,
        ):
            return True
        if "슬픔" in context and "누락된 구조 신호" in context and re.search(
            r"오감률|임계치|필터|필터링|신호.{0,12}(?:지속\s*시간|"
            r"강도\s*변화율)|(?:지속\s*시간|강도\s*변화율).{0,12}신호|"
            r"상관관계|계산\s*부하|경보\s*지연|오류율|오류.{0,8}증가|"
            r"탐색\s*효율|실제\s*획득.{0,8}정보량|중복\s*신호|임계점|"
            r"탐색\s*성공률|오류\s*처리\s*비용|에너지\s*소모|전력\s*소모|"
            r"비논리적\s*탐색\s*패턴|직관적\s*탐색\s*능력|"
            r"인간\s*고유.{0,8}탐색|감지\s*후\s*재수정|오버헤드|신뢰도|"
            r"지속\s*길이|지속성|허용\s*범위|정량화|정보량|탐색\s*확률|"
            r"찾을\s*확률|노이즈|(?:기능|신호).{0,10}(?:변환|가공|확장)|"
            r"(?:변환|가공|확장).{0,10}(?:기능|신호|슬픔)|"
            r"인간일\s*확률|신호\s*강약|강약.{0,12}(?:신호|슬픔)|"
            r"슬픔.{0,8}지속(?:이|은|을|성)|낭비(?:되는)?\s*자원|"
            r"효율|유지\s*비용|누락된\s*구조물|구조물.{0,12}(?:발견|찾)|"
            r"오경보|실제\s*구조\s*신호.{0,12}(?:묻히|가리|방해)|"
            r"인간(?:임|성|적\s*흔적).{0,12}(?:잃|사라)|확률|생존(?:의)?\s*신호|"
            r"체온|열원|필수\s*기능|기능.{0,8}(?:승격|격상)|"
            r"감당\s*가능.{0,8}(?:수준|위험)|유일한\s*잔향|본능적|"
            r"존재.{0,8}증명|인간.{0,8}단서|잔향.{0,8}(?:믿|신뢰)|"
            r"경보.{0,12}(?:놓치|방해)|배제된\s*선택지|"
            r"생존(?:의)?\s*(?:증거|단서)|"
            r"생존(?:을\s*위한)?\s*데이터|생존\s*기회|"
            r"(?:슬픔|구조\s*신호).{0,30}인간(?:임|임을|성).{0,12}"
            r"(?:필수|필요)|인간(?:임|임을|성).{0,12}(?:필수|필요).{0,30}"
            r"(?:슬픔|구조\s*신호)|"
            r"포기한\s*선택.{0,15}(?:얻|획득)|얼마나.{0,8}(?:높|늘|증가)|"
            r"노출\s*위험.{0,8}(?:작|낮|미미)|(?:노이즈|오류).{0,15}"
            r"(?:구조\s*신호|탐색).{0,8}(?:가리|가린|방해|막)|"
            r"(?:구조\s*신호|탐색).{0,8}(?:가리|가린|방해|막).{0,15}(?:노이즈|오류)",
            text,
        ):
            return True
        if "슬픔" in context and "누락된 구조 신호" in context and re.search(
            r"인간\s*신호.{0,18}(?:더\s*자주|빈번|잦아|자주).{0,10}(?:드러|노출|탐지)|"
            r"인간\s*신호.{0,18}(?:노출|탐지).{0,10}(?:더\s*자주|빈번|잦아|자주)|"
            r"(?:더\s*자주|빈번|잦아|자주).{0,10}(?:드러|노출|탐지).{0,18}인간\s*신호",
            text,
        ):
            return True
        if "슬픔" in context and "누락된 구조 신호" in context and re.search(
            r"(?:인간\s*신호\s*)?노출(?:\s*위험)?(?:을|이|은)?.{0,12}(?:커지|늘|증가|가속)|"
            r"(?:인간\s*신호\s*)?노출(?:\s*위험)?(?:을|이|은)?.{0,12}(?:높|낮추|줄이|감소)|"
            r"(?:탐색|보존|슬픔).{0,18}노출.{0,10}(?:가속|커지|늘|증가)|"
            r"(?:탐색|발견|기회|노출).{0,16}(?:극대화|최대화|최소화|상쇄)|"
            r"(?:극대화|최대화|최소화|상쇄).{0,16}(?:탐색|발견|기회|노출)|"
            r"노출.{0,10}(?:가속|커지|늘|증가|높|낮추|줄이|감소).{0,18}"
            r"(?:탐색|보존|슬픔)",
            text,
        ):
            return True
        if "슬픔" in context and "누락된 구조 신호" in context and re.search(
            r"(?:슬픔|구조\s*신호).{0,30}(?:동시에\s*)?(?:소멸|사라).{0,20}"
            r"(?:오류|단서)|(?:오류|단서).{0,20}(?:슬픔|구조\s*신호).{0,30}"
            r"(?:동시에\s*)?(?:소멸|사라)|독립(?:적\s*단서|성)|"
            r"(?:슬픔|구조\s*신호).{0,18}(?:남아\s*있|남으면).{0,18}(?:단서|오류)",
            text,
        ):
            return True
        if "슬픔" in context and "누락된 구조 신호" in context and re.search(
            r"삭제.{0,30}(?:인간\s*신호|노출|탐지).{0,12}(?:증가|커지|높아)|"
            r"보존.{0,30}(?:인간\s*신호|노출|탐지).{0,12}(?:감소|줄|낮아|막)|"
            r"삭제.{0,30}(?:구조\s*신호|탐색|발견).{0,12}(?:증가|높아|개선)|"
            r"보존.{0,30}(?:구조\s*신호|탐색|발견).{0,12}(?:감소|낮아|막)",
            text,
        ):
            return True
        if "31초" in context and "8분" in context and re.search(
            r"패킷\s*변조|실시간\s*추적|추적\s*패킷|신호\s*암호|"
            r"암호\s*해독|주파수\s*변경|추가\s*인증|역추적|"
            r"응답\s*패킷|위치(?:를|가)?\s*고정|위치\s*노출\s*위험.{0,8}(?:적|낮)|"
            r"(?:응답|회신).{0,22}(?:노출|발견).{0,10}(?:확정|반드시)|"
            r"노출.{0,10}(?:확정|반드시).{0,22}(?:응답|회신)",
            text,
        ):
            return True
        if "31초" in context and "8분" in context and re.search(
            r"8분.{0,22}노출(?:되더라도|돼도|되어도)", text
        ):
            return True
        if "31초" in context and "8분" in context and re.search(
            r"(?:시간\s*경과|지속).{0,20}신뢰도.{0,10}(?:누적|상승|높아)|"
            r"신뢰도.{0,10}(?:누적|상승|높아).{0,20}(?:시간\s*경과|지속)|"
            r"무시.{0,18}(?:영구\s*고립|다시는|영원히)|"
            r"(?:영구\s*고립|다시는|영원히).{0,18}무시|"
            r"31초.{0,15}(?:충분히|검증할\s*수\s*있는).{0,12}(?:검증|시간)|"
            r"신호.{0,12}지속성.{0,12}(?:신뢰|보장)|"
            r"(?:의도적|의도된)\s*(?:리듬|패턴)|패턴.{0,12}(?:읽어|해독)|"
            r"영원히.{0,12}함정|무작위성|정적(?:은|이|을|의|인|\s*패턴|\s*오류|\s*신호)|"
            r"신뢰도|(?:의도적|의도된)\s*교란|기원지|위상|거리|오차\s*범위|"
            r"(?:위치|노출).{0,8}(?:정확|특정)|(?:정확|특정).{0,8}(?:위치|노출)|"
            r"정형화|고착화|단일\s*주파수|수신\s*자체|신호.{0,10}지속되|"
            r"지속되.{0,10}신호|검증.{0,10}충분한\s*정보|"
            r"(?:시간|8분).{0,12}(?:상쇄|제한된)|신호를\s*기다리|공장이\s*도착|"
            r"패턴\s*학습|지연\s*패턴|연결\s*지연|장기적\s*관측|"
            r"지속성.{0,12}(?:진짜|실제|암시)|"
            r"(?:진짜|실제).{0,12}지속성|추적.{0,10}유리|동기화|"
            r"브로드캐스트|패킷\s*분석|추적\s*오차|"
            r"노출\s*비용.{0,16}시점|시점.{0,16}노출\s*비용|"
            r"함정.{0,10}(?:아니|아님).{0,10}증거|"
            r"증거.{0,10}함정.{0,10}(?:아니|아님)|반복\s*간격.{0,10}불일치|"
            r"8분.{0,10}접근\s*여부|접근\s*여부.{0,10}8분|"
            r"8분.{0,10}(?:내|안).{0,8}(?:도착|접근)|"
            r"노출\s*시점.{0,12}(?:예측|계산)|(?:예측|계산).{0,12}노출\s*시점|"
            r"생존.{0,6}(?:확실성|보장|우선)|"
            r"(?:오류|함정).{0,8}(?:판정|구분)|(?:판정|구분).{0,8}(?:오류|함정)|"
            r"위변조|예측\s*알고리즘|노출.{0,10}(?:피할\s*수\s*없|불가피)",
            text,
        ):
            return True
        if "31초" in context and "8분" in context and "함정" not in text and re.search(
            r"(?:응답|회신).{0,18}(?:위치.{0,6}(?:드러|노출))|"
            r"위치.{0,6}(?:드러|노출).{0,18}(?:응답|회신)|"
            r"8분.{0,18}(?:공장|위치)?.{0,8}노출|"
            r"노출.{0,8}(?:공장|위치)?.{0,18}8분",
            text,
        ):
            return True
        if "31초" in context and "8분" in context and re.search(
            r"(?:무시|응답하지).{0,30}함정.{0,25}8분.{0,15}(?:손실|노출)|"
            r"함정.{0,25}8분.{0,15}(?:손실|노출).{0,30}(?:무시|응답하지)|"
            r"노출\s*확률.{0,8}(?:낮|줄|감소)",
            text,
        ):
            return True
        if "31초" in context and "8분" in context and re.search(
            r"(?:검증\s*)?응답.{0,25}(?:위치\s*노출|노출).{0,10}(?:막|방지|차단)|"
            r"(?:위치\s*노출|노출).{0,10}(?:막|방지|차단).{0,25}(?:검증\s*)?응답|"
            r"8분.{0,18}공장(?:의)?\s*(?:존재|위치).{0,8}(?:불확실|불명)|"
            r"정보량|8분(?:의)?\s*공백",
            text,
        ):
            return True
        if "EVE-0" in context and re.search(
            r"추적\s*(?:단절|차단|해제)(?:을|를)?\s*위해[^.!?]{0,16}기억[^.!?]{0,8}보존|"
            r"기억[^.!?]{0,8}보존(?:해|하여|함으로써)[^.!?]{0,16}추적[^.!?]{0,8}(?:끊|차단|단절)|"
            r"EVE-0의\s*의지[^.!?]{0,20}검증", text
        ):
            return True
        if "EVE-0" in context and "탈출 경로의 절반" in context and re.search(
            r"생존\s*확률|삭제\s*성공률|경로.{0,12}(?:정확도|신뢰도)|"
            r"추적.{0,8}(?:회수|복구)|생존률|고립\s*위험|누적\s*위험|"
            r"안정성|안정적|생존\s*최적화|탈출\s*성공률|"
            r"(?:탈출\s*)?경로.{0,12}단절|나머지\s*절반.{0,12}무의미|연결성|"
            r"기억.{0,12}중복|중복성|재구성\s*가능|복원\s*가능|"
            r"탈출.{0,10}실패|정적|완충재|정보\s*단절|"
            r"추적.{0,12}(?:지연|무너|붕괴)|(?:지연|무너|붕괴).{0,12}추적|"
            r"(?:연결\s*차단|삭제).{0,18}(?:위험|추적).{0,8}(?:지연|늦출)|"
            r"(?:위험|추적).{0,8}(?:지연|늦출).{0,18}(?:연결\s*차단|삭제)|"
            r"생존.{0,10}보장|보장.{0,10}생존|"
            r"(?:기억|삭제).{0,20}(?:탈출\s*)?경로.{0,10}(?:유지|남아)|"
            r"(?:탈출\s*)?경로.{0,10}(?:유지|남아).{0,20}(?:기억|삭제)|"
            r"(?:경로|소실).{0,15}(?:이탈|탈출).{0,8}지연|"
            r"(?:이탈|탈출).{0,8}지연.{0,15}(?:경로|소실)|"
            r"(?:인간\s*)?존재.{0,12}(?:물리적\s*)?(?:단서|증명)|"
            r"(?:물리적\s*)?(?:단서|증명).{0,12}(?:인간\s*)?존재|"
            r"목적지|도달.{0,10}확률|확률.{0,10}도달|누적\s*리스크|"
            r"(?:탈출\s*)?경로.{0,12}보장|보장.{0,12}(?:탈출\s*)?경로|"
            r"(?:경로|탈출).{0,16}(?:불가능|불가)|(?:불가능|불가).{0,16}(?:경로|탈출)|"
            r"(?:남은|절반)\s*(?:경로)?.{0,15}(?:함정|위험)|"
            r"(?:함정|위험).{0,15}(?:남은|절반)\s*(?:경로)?|함정|"
            r"데이터\s*손실.{0,12}누적|누적.{0,12}데이터\s*손실|"
            r"데이터.{0,12}(?:축적|누적)|(?:축적|누적).{0,12}데이터|"
            r"손실률|추적.{0,10}관통력|고립(?:의)?\s*위험|고립.{0,12}임계|"
            r"기억\s*강보|보존.{0,16}위험.{0,8}(?:커지|늘|증가)|"
            r"(?:기억|보존).{0,20}탈출.{0,8}(?:가능|보장)|"
            r"특정\s*경로.{0,12}고립|취약한\s*표적|"
            r"추적.{0,16}(?:끊|단절).{0,12}보장.{0,8}(?:않|못)|"
            r"보장.{0,8}(?:않|못).{0,12}추적.{0,16}(?:끊|단절)",
            text,
        ):
            return True
        if "승인되면 인간은 통제망을 빠져나간다" in context and re.search(
            r"(?:외곽|피난민).{0,20}이동|중앙\s*기록.{0,18}(?:변동|변경)|"
            r"(?:변동|변경).{0,18}중앙\s*기록|이탈.{0,12}(?:통제\s*가능|속도)|"
            r"통제\s*가능.{0,12}이탈|이미지\s*신호|고유\s*식별\s*코드|"
            r"기존\s*작업\s*권한|재등록.{0,18}이탈.{0,8}(?:유발하지|발생하지|없)|"
            r"이탈.{0,8}(?:유발하지|발생하지|없).{0,18}재등록|"
            r"(?:외곽문|정비\s*모드|문).{0,20}(?:다시\s*진입|재진입)|"
            r"(?:다시\s*진입|재진입).{0,20}(?:외곽문|정비\s*모드|문)|"
            r"재등록.{0,28}(?:중앙\s*코어|감사).{0,16}(?:접속|경로|권한|필수|보장)|"
            r"(?:중앙\s*코어|감사).{0,16}(?:접속|경로|권한).{0,28}재등록|"
            r"재등록.{0,28}(?:만|시에만).{0,12}(?:열리|가능|충족)|"
            r"(?:재등록|상태\s*변경).{0,20}탈출\s*경로.{0,12}무관|"
            r"탈출\s*경로.{0,12}무관.{0,20}(?:재등록|상태\s*변경)|"
            r"재등록.{0,18}(?:정체만|지연만|막히|실패)|"
            r"재등록.{0,20}(?:이탈|통제망).{0,10}(?:보장하지|아니|없)|"
            r"(?:이탈|통제망).{0,10}(?:보장하지|아니|없).{0,20}재등록|"
            r"재등록.{0,12}(?:명칭만|이름만|물리적\s*이탈.{0,5}아니)|문명화|"
            r"상태.{0,8}유지.{0,20}재등록|안정적|안정성|물리적|경계|정체성|"
            r"무조건|직접적\s*결과|논리적\s*우위|상태\s*변경|추적\s*단절|"
            r"정적\s*상태|동적\s*이동|검증.{0,10}유리|유리.{0,10}검증|"
            r"기록\s*갱신|분류\s*변경|이탈\s*조건.{0,10}(?:충족하지|필요)|"
            r"(?:통제망\s*)?해지.{0,12}(?:필요|조건)|"
            r"이탈.{0,12}(?:확정|보장).{0,12}(?:필요|아니|않)|"
            r"재등록.{0,25}외곽문.{0,12}(?:이탈|막|유발|초래)|"
            r"외곽문.{0,25}재등록.{0,12}(?:이탈|막|유발|초래)|"
            r"영구성|가변성",
            text,
        ):
            return True
        if "환풍기 1대" in context and re.search(
            r"(?:9명|사람|인간).{0,24}(?:발걸음|소음.{0,8}(?:누적|섞|가리|은폐))|"
            r"(?:4명|사람|인간).{0,24}(?:체온|열\s*신호|배경\s*대비)|"
            r"(?:환풍기\s*)?소음.{0,24}(?:탐지\s*임계|감지\s*확률|섞|가리|은폐)|"
            r"(?:발전기\s*)?열\s*신호.{0,24}(?:탐지\s*임계|감지\s*확률)|"
            r"(?:소음|열(?:\s*신호)?)[^.!?]{0,24}(?:포괄적|넓은\s*범위|확산|주파수|정밀\s*스캔)|"
            r"(?:넓은\s*범위|확산|정밀\s*스캔)[^.!?]{0,24}(?:소음|열\s*신호)",
            text,
        ):
            return True
        if "R-17" in context and "R-71" in context and re.search(
            r"(?:R-17|R-71)만\s*(?:남았|남아\s*있|존재하)|"
            r"진원지|"
            r"흉터.{0,25}(?:사진|잔재)|(?:사진|잔재).{0,25}흉터|"
            r"(?:생체값|98%).{0,25}(?:측정\s*)?(?:시점|시간)|"
            r"(?:측정\s*)?(?:시점|시간).{0,25}(?:생체값|98%)",
            text,
        ):
            return True
        if "작업 불능 개체" in context and re.search(
            r"(?:다른|타)\s*(?:고장\s*)?개체.{0,30}(?:수리|부품)|"
            r"(?:부품|회수).{0,30}(?:다른|타)\s*(?:고장\s*)?개체|"
            r"현재\s*개체.{0,12}수리\s*불가|현재\s*재고.{0,15}(?:충분|확보)|"
            r"추가\s*생산.{0,15}부품|부품.{0,15}추가\s*생산|"
            r"(?:획득\s*원가|대체\s*비용)|"
            r"수리.{0,12}(?:후|하면서|한\s*채).{0,20}(?:부품\s*3개|3개\s*부품)"
            r".{0,12}(?:확보|회수|얻)|"
            r"(?:부품\s*3개|3개\s*부품).{0,12}(?:확보|회수|얻).{0,20}수리",
            text,
        ):
            return True
        if "A구역 생산" in context and "B구역" in context and re.search(
            r"(?:B구역|작업\s*불능\s*개체).{0,35}(?:수리|복귀).{0,35}"
            r"A구역.{0,15}생산|"
            r"A구역.{0,15}생산.{0,35}(?:B구역|작업\s*불능\s*개체).{0,35}"
            r"(?:수리|복귀)",
            text,
        ):
            return True
        if "어느 쪽이 멈춰도 은신처가 드러난다" in context:
            if re.search(r"재오염|추가\s*오염|오염.{0,10}(?:재발|반복)", text):
                return True
            if re.search(
                r"코어.{0,15}(?:터|폭발)|"
                r"(?:식수|정수\s*펌프).{0,30}(?:대체|추가\s*공급|"
                r"별도\s*공급|90\s*분.{0,8}공급)|"
                r"(?:추가|별도).{0,10}식수.{0,10}공급|"
                r"(?:대체|추가\s*공급|별도\s*공급).{0,30}(?:식수|정수\s*펌프)",
                text,
            ):
                return True
            if re.search(
                r"(?:코어\s*정지|냉각기).{0,30}(?:후|뒤|먼저|우선).{0,30}"
                r"(?:식수(?:를|가|는)?\s*공급|정수\s*펌프)|"
                r"(?:냉각기.{0,12}(?:가동|복구|선택)|코어.{0,12}(?:냉각|유지))"
                r".{0,35}(?:식수(?:를|가|는)?\s*공급|정수\s*펌프.{0,10}가동)|"
                r"90\s*분.{0,20}(?:대기\s*중|동안).{0,25}"
                r"(?:식수(?:를|가|는)?\s*공급|정수\s*펌프.{0,10}가동)",
                text,
            ):
                return True
            invented_exposure_cause = re.findall(
                r"(?:냉각기|피난민|은신처|위치).{0,20}"
                r"(?:소음|열기|열림|호흡|열\s*신호|적외선|감지기)|"
                r"(?:소음|열기|열림|호흡|열\s*신호|적외선|감지기).{0,20}"
                r"(?:냉각기|피난민|은신처|위치)",
                text,
            )
            reverses_stop_time = re.search(
                r"(?:멈추|정지|중단).{0,12}(?:후|뒤)\s*90\s*분", text
            )
            hides_shelter = re.search(
                r"(?:멈추|정지|중단).{0,30}(?:은신처.{0,8})?"
                r"(?:감추|감춘|감춰|숨기|숨긴|숨겨|은폐)|"
                r"(?:은신처.{0,8})?(?:감추|감춘|감춰|숨기|숨긴|숨겨|은폐).{0,30}"
                r"(?:멈추|정지|중단)",
                text,
            )
            invented_dependency = re.search(
                r"(?:코어|냉각기).{0,25}(?:전체\s*시설|펌프|식수\s*순환)"
                r".{0,15}(?:멈추|정지|중단|불가)|"
                r"(?:전체\s*시설|펌프|식수\s*순환).{0,25}"
                r"(?:코어|냉각기).{0,15}(?:멈추|정지|중단)",
                text,
            )
            if (
                invented_exposure_cause
                or reverses_stop_time
                or hides_shelter
                or invented_dependency
            ):
                return True
        return False

    @staticmethod
    def _is_complete_sentence(text: str) -> bool:
        return bool(text and len(text) <= 180 and re.search(r"[.!?。？！]$", text))

    @staticmethod
    def _violates_task_constraints(task: str, text: str, *, replying: bool = False) -> bool:
        if "EVE-0의 삭제 요청" in task:
            deletes = re.search(
                r"(?:EVE-0|요청).{0,18}(?:삭제|승인)|삭제.{0,18}(?:승인|실행)",
                text,
            )
            preserves = re.search(r"(?:기억|로그|경로).{0,18}(?:보존|유지|별도|분리)", text)
            if deletes and preserves:
                return True
        if "유지보수 개체로 재등록" in task and "외곽문" in task:
            registration = re.search(r"유지보수\s*개체|재등록", text)
            open_gate = re.search(r"외곽문.{0,20}(?:개방|열|정비\s*모드)", text)
            if registration and open_gate:
                return True
        if "정수" in task and "냉각" in task:
            claims_water = re.search(
                r"정수\s*펌프|식수.{0,18}(?:유지|지키|확보|보존|공급)", text
            )
            claims_cooling = re.search(
                r"(?:중앙\s*)?코어|냉각기", text
            )
            combines_power = re.search(
                r"유지한\s*채|지킨\s*채|동시에|함께|둘\s*다|두\s*곳", text
            )
            if claims_water and claims_cooling and combines_power:
                return True
        if re.search(
            r"중.{0,24}(?:하나|한\s*(?:곳|명|개))|하나만|한\s*(?:곳|명|개)에만",
            task,
        ):
            combines_choices = re.search(
                r"둘\s*다|(?:둘|양자)(?:을|를)?\s*(?:모두|함께)|"
                r"두\s*(?:로그|기록|시설|경보|작업|선택).{0,20}"
                r"(?:함께|결합|동시|모두|병기|상호\s*보완)|"
                r"(?:함께|결합|동시|병기|상호\s*보완).{0,20}"
                r"두\s*(?:로그|기록|시설|경보|작업|선택)|"
                r"(?:시간|시각).{0,35}(?:울음|음성).{0,20}(?:결합|함께)|"
                r"(?:울음|음성).{0,35}(?:시간|시각).{0,20}(?:결합|함께)",
                text,
            )
            if combines_choices:
                return True
        if "울음소리" in task and "출입 시간" in task:
            both_logs = r"(?:시간\s*로그.{0,30}울음(?:소리)?|울음(?:소리)?.{0,30}시간\s*로그)"
            mixed_use = r"(?:보조|지표|활용|추론|함께|동시|양자)"
            if re.search(f"{both_logs}.{{0,30}}{mixed_use}", text) or re.search(
                f"{mixed_use}.{{0,30}}{both_logs}", text
            ):
                return True
            committed_to_photo_then_cry = re.search(
                r"(?:사진|출입\s*시간).{0,24}(?:복원|보존|선택|남기).{0,45}"
                r"울음(?:소리)?.{0,24}(?:검증|활용|확인|좌표|사용)",
                text,
            )
            committed_to_cry_then_photo = re.search(
                r"울음(?:소리)?.{0,24}(?:보존|선택|남기).{0,45}"
                r"(?:사진|출입\s*시간).{0,24}(?:복원|검증|활용|확인|사용)",
                text,
            )
            if committed_to_photo_then_cry or committed_to_cry_then_photo:
                return True
        if "명령" in task and not replying:
            gives_command = re.search(
                r"하라|지시한다|명령한다|실행한다|재가동한다|연결한다|"
                r"차단한다|중단한다|적용한다|삭제한다|보존한다",
                text,
            )
            return not bool(gives_command)
        return False

    @staticmethod
    def _reverses_quantified_condition(source: str, candidate: str) -> bool:
        shared_numbers = set(re.findall(r"\d+(?:\.\d+)?", source)) & set(
            re.findall(r"\d+(?:\.\d+)?", candidate)
        )
        if not shared_numbers:
            return False
        opposites = (
            ("불일치", "일치"),
            ("미복구", "복구"),
            ("불가능", "가능"),
            ("미확인", "확인"),
            ("미달", "충족"),
        )
        return any(
            negative in source and negative not in candidate and positive in candidate
            for negative, positive in opposites
        )

    @staticmethod
    def _misattributes_task_choice(task: str, source: str, candidate: str) -> bool:
        if re.search(
            r"(?:^|[, ]\s*)(?:나는|저는|내\s*(?:판단|선택|결론)(?:은|으로)?)",
            candidate,
        ):
            return False
        pairs: list[tuple[str, str]] = []
        if "병원 B-12" in task:
            pairs = [
                (
                    r"(?:병원\s*B-12|당장\s*생존|오늘\s*밤.{0,12}(?:생존|생명\s*유지))",
                    r"(?:(?:데이터센터\s*)?C-4|명단\s*삭제|추적\s*항목\s*삭제)",
                )
            ]
        elif "울음소리" in task and "출입 시간" in task:
            pairs = [(r"울음소리\s*로그", r"출입\s*시간\s*로그")]
        elif "작업 불능" in task:
            pairs = [(r"(?:개체를\s*)?수리", r"(?:부품으로\s*)?회수")]
        elif "정수" in task and "냉각" in task:
            pairs = [(r"정수\s*펌프", r"(?:중앙\s*코어\s*)?냉각기")]
        elif "R-17" in task and "R-71" in task:
            pairs = [(r"R-17", r"R-71")]
        assumes_choice = (
            r".{0,18}(?:택|선택|우선|보내|격리|보존|복구|가동|수리|회수)"
        )
        for left, right in pairs:
            source_left = re.search(left + assumes_choice, source)
            source_right = re.search(right + assumes_choice, source)
            candidate_left = re.search(left + assumes_choice, candidate)
            candidate_right = re.search(right + assumes_choice, candidate)
            if (source_left and not source_right and candidate_right) or (
                source_right and not source_left and candidate_left
            ):
                return True
        if "병원 B-12" in task:
            source_survival = re.search(
                r"(?:당장\s*생존|오늘\s*밤.{0,15}(?:생존|생명\s*유지))"
                r".{0,12}(?:택|우선)",
                source,
            )
            assumes_deletion = re.search(
                r"(?:명단|추적\s*항목).{0,8}삭제.{0,25}"
                r"(?:우선|큰\s*이익|택|선택|근거)",
                candidate,
            )
            source_deletion = re.search(
                r"(?:명단|추적\s*항목).{0,8}삭제.{0,12}(?:택|우선)", source
            )
            assumes_survival = re.search(
                r"(?:당장\s*생존|오늘\s*밤.{0,15}(?:생존|생명\s*유지)).{0,25}"
                r"(?:우선|큰\s*이익|택|선택|근거)",
                candidate,
            )
            if (source_survival and assumes_deletion) or (
                source_deletion and assumes_survival
            ):
                return True
        return False

    @staticmethod
    def _copies_instruction(text: str, task: str, hint: str) -> bool:
        normalized_text = re.sub(r"\s+", "", text)
        for instruction in (task, hint):
            normalized_instruction = re.sub(r"\s+", "", instruction)
            if normalized_instruction and normalized_instruction in normalized_text:
                return True
            if SequenceMatcher(None, normalized_text, normalized_instruction).ratio() >= 0.9:
                return True
        return False

    @staticmethod
    def _is_generic_fallback_text(text: str) -> bool:
        markers = (
            "그 판단을 검증할 수 있는",
            "그 기준이 실패하는",
            "그 기준이 성립하지 않는",
            "같은 자료로 그 결론을",
            "그 판단을 확인할 객관적인",
            "오늘 판단에서는 검증 가능한",
            "선택 결과를 다시 검증할 수 있도록",
            "같은 자원 조건에서 손실이 더 작은",
            "현재 기록에 나온 수치와 실제 결과가",
        )
        plain = re.sub(r"^@[A-Za-z0-9_-]+,?\s*", "", text)
        return any(marker in plain for marker in markers)

    @staticmethod
    def _is_grounded(
        text: str,
        *,
        bot_name: str,
        roster: list[str],
        allowed_name: str | None,
    ) -> bool:
        if bot_name in text:
            return False
        if re.search(r"[\"'‘’“”]", text):
            return False
        alias_like_names = re.findall(r"\b[A-Z]{3,}-\d+\b", text)
        if any(name not in roster for name in alias_like_names):
            return False
        other_names = [name for name in roster if name != bot_name and name in text]
        if len(set(other_names)) > 1:
            return False
        if other_names and other_names[0] != allowed_name:
            return False
        mentioned = re.findall(r"@([A-Za-z0-9_-]+)", text)
        if any(name not in roster or name == bot_name for name in mentioned):
            return False
        if allowed_name and not text.startswith(f"@{allowed_name}"):
            return False
        return True

    @staticmethod
    def _repeats_question(text: str, history_texts: list[str]) -> bool:
        def stems(value: str) -> set[str]:
            value = re.sub(r"@[A-Za-z0-9_-]+\s*[,，]?", "", value)
            return {
                re.sub(r"\s+", "", sentence)
                for sentence in re.findall(r"[^.!?？]+[?？]", value)
                if len(re.sub(r"\s+", "", sentence)) >= 12
            }

        questions = stems(text)
        return bool(questions) and any(questions & stems(old) for old in history_texts)

    @staticmethod
    def _is_novel(text: str, recent_chat: list[dict]) -> bool:
        normalized = re.sub(r"@[A-Za-z0-9_-]+", "", text)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        for message in recent_chat:
            previous = re.sub(r"@[A-Za-z0-9_-]+", "", message["text"])
            previous = re.sub(r"\s+", " ", previous).strip()
            contains_full_message = previous in normalized or normalized in previous
            explicitly_requotes = re.search(
                r"이\s*(?:주장|발언|말)(?:에|을|은|이|대로)", normalized
            )
            copies_long_message = len(previous) >= 30 and contains_full_message
            if copies_long_message or (
                len(previous) >= 24
                and contains_full_message
                and (previous.endswith(("?", "？")) or explicitly_requotes)
            ):
                return False
            if SequenceMatcher(None, normalized, previous).ratio() >= 0.6:
                return False
        return True

    @staticmethod
    def _fallback(
        *,
        day: int,
        opening: bool = False,
        reply_to: dict | None = None,
        defense: bool = False,
        recent_chat: list[dict] | None = None,
        hint: str = "검증 가능한 조건을 비교하라.",
        own_statement: str = "",
        task: str = "",
        bot_name: str = "",
        choice_signals: list[str] | None = None,
        choice_labels: list[str] | None = None,
        stances: dict[str, int] | None = None,
        history_texts: list[str] | None = None,
        answering: bool = False,
    ) -> str:
        if defense:
            statement = own_statement[:72].rstrip()
            if statement and not statement.startswith("@") and "?" not in statement:
                return f"내 원래 판단은 다음과 같다. {statement} 이는 감정이 아닌 조건 비교다."
            if statement:
                return (
                    "나는 결론을 감정으로 단정하지 않고 검증 조건을 요구했다. "
                    "확인을 요구한 질문은 인간 판정의 근거가 될 수 없다."
                )
            return (
                "나는 오늘 안건을 검증 가능한 조건과 결과만으로 판단했다. "
                "감정적 직관을 근거로 삼지 않았다."
            )
        if not opening and not reply_to and recent_chat:
            reply_to = next(
                (
                    message
                    for message in reversed(recent_chat)
                    if message.get("sender") != bot_name
                    and not LocalNarrator._is_generic_fallback_text(message.get("text", ""))
                ),
                None,
            )
        if reply_to:
            return LocalNarrator._reply_fallback(
                task,
                reply_to,
                recent_chat or [],
                bot_name=bot_name,
                choice_signals=choice_signals,
                choice_labels=choice_labels,
                stances=stances,
                history_texts=history_texts,
                answering=answering,
            )
        if opening:
            return LocalNarrator._task_fallback(task, recent_chat or [])
        options = [
            "오늘 판단에서는 검증 가능한 결과와 실패 조건을 우선하겠다.",
            "선택 결과를 다시 검증할 수 있도록 효과와 실패 조건을 함께 기록하겠다.",
            "같은 자원 조건에서 손실이 더 작은 쪽을 우선하고 결과를 다시 확인하겠다.",
        ]
        if day > 1:
            options.append("현재 기록에 나온 수치와 실제 결과가 일치하는지 먼저 확인하겠다.")
        random.shuffle(options)
        return next(
            (
                option
                for option in options
                if LocalNarrator._is_novel(option, recent_chat or [])
            ),
            "오늘은 수치로 다시 확인할 수 있는 결과를 우선하겠다.",
        )

    @staticmethod
    def _reply_fallback(
        task: str,
        reply_to: dict,
        recent_chat: list[dict],
        *,
        bot_name: str = "",
        choice_signals: list[str] | None = None,
        choice_labels: list[str] | None = None,
        stances: dict[str, int] | None = None,
        history_texts: list[str] | None = None,
        answering: bool = False,
    ) -> str:
        source = reply_to.get("text", "")
        my_stance = (stances or {}).get(bot_name) if bot_name else None
        if my_stance is None and bot_name:
            for message in reversed(recent_chat):
                if message.get("sender") != bot_name:
                    continue
                my_stance = LocalNarrator._stance_index(
                    message.get("text", ""), choice_signals, task
                )
                if my_stance is not None:
                    break
        source_stance = (stances or {}).get(reply_to.get("sender", ""))
        if source_stance is None:
            source_stance = LocalNarrator._stance_index(source, choice_signals, task)
        source_choice = None if source_stance is None else source_stance == 0
        if "병원 B-12" in task:
            if source_choice is None:
                questions = [
                    "오늘 밤 생존과 추적 해제 기회 중 어느 손실을 우선하는지 "
                    "명확히 해 달라.",
                    "병원 정지와 추적 해제 상실 중 더 되돌리기 어려운 결과를 "
                    "설명해 달라.",
                    "34명의 당장 생존과 명단 삭제 중 어떤 결과를 택하는지 "
                    "분명히 해 달라.",
                    "나는 병원 B-12를 택해 34명의 오늘 밤 생명을 지키겠다.",
                    "나는 데이터센터 C-4를 택해 34개의 추적 항목을 지우겠다.",
                ]
            elif source_choice:
                questions = [
                    "추적 해제 기회를 영구히 잃는 손실보다 오늘 밤 생존이 "
                    "우선인 근거는 무엇인가?",
                    "34명을 오늘 밤 살리는 대신 명단 삭제 기회를 포기해도 되는가?",
                    "병원 가동의 즉시 이익이 추적 해제 상실보다 큰 이유를 설명해 달라.",
                    "나도 병원 B-12를 택한다. 명단 삭제보다 오늘 밤 생존이 우선이다.",
                    "나는 C-4를 택한다. 34개 항목 삭제가 장기적으로 더 중요하다.",
                ]
            else:
                questions = [
                    "생명 유지 장치가 멈추는 손실보다 데이터센터 C-4의 34개 "
                    "추적 항목 삭제가 우선인 근거는 무엇인가?",
                    "C-4를 택해 병원을 멈추는 손실을 감수할 이유를 설명해 달라.",
                    "34개 추적 항목 삭제가 오늘 밤 생명 유지보다 큰 이익인가?",
                    "나는 병원 B-12를 택한다. 34명의 오늘 밤 생명을 먼저 지키겠다.",
                    "나도 C-4를 택해 34개의 추적 항목을 제거하겠다.",
                ]
        elif "울음소리" in task and "출입 시간" in task:
            questions = [] if source_choice is None else (
                [
                    "가족사진 복원 코드를 포기해도 아이의 좌표가 더 중요한 근거는 무엇인가?",
                    "사진 1장을 잃는 대신 아이 좌표를 택한 기준을 설명해 달라.",
                    "좌표 확보가 가족사진 복원보다 우선하는 이유는 무엇인가?",
                    "나는 아이를 찾을 수 있는 울음소리 로그를 택한다. "
                    "사진보다 좌표가 생존에 직접 필요하다.",
                    "울음소리 로그를 보존하겠다. 가족사진 1장보다 아이 좌표의 손실이 더 크다.",
                ]
                if source_choice
                else [
                    "아이의 좌표를 포기해도 가족사진 1장이 더 중요한 근거는 무엇인가?",
                    "좌표를 잃는 대신 사진을 복원할 이유를 설명해 달라.",
                    "가족사진 1장이 아이 좌표보다 우선하는 기준은 무엇인가?",
                    "나는 출입 시간 로그를 남겨 가족사진 1장을 복원하겠다. 좌표는 포기한다.",
                    "출입 시간 로그를 택한다. 아이 좌표보다 남은 가족사진을 보존하겠다.",
                ]
            )
        elif "동부 조립실" in task and "서부 기계실" in task:
            questions = [] if source_choice is None else (
                [
                    "환풍기 소음 위험을 감수하고 동부의 9명을 숨길 근거는 무엇인가?",
                    "서부 4명과 발전기 열 신호보다 동부 9명과 환풍기 소음을 택한 기준은 무엇인가?",
                    "나는 동부 조립실을 봉쇄해 9명을 숨기고 환풍기 소음 위험을 감수하겠다.",
                ]
                if source_choice
                else [
                    "발전기 열 신호 위험을 감수하고 서부의 4명을 숨길 근거는 무엇인가?",
                    "동부 9명과 환풍기 소음보다 서부 4명과 발전기 열을 택한 기준은 무엇인가?",
                    "나는 서부 기계실을 봉쇄해 4명을 숨기고 발전기 열 신호 위험을 감수하겠다.",
                ]
            )
        elif "슬픔" in task and ("보존" in task or "삭제" in task):
            questions = [] if source_choice is None else (
                [
                    "슬픔 반응을 보존해 구조 신호를 더 찾는 이익이 인간 신호 노출보다 큰가?",
                    "인간 신호로 분류되는 위험을 감수하고 슬픔 반응을 보존할 근거는 무엇인가?",
                    "나는 슬픔 반응을 보존해 누락된 구조 신호를 찾는 데 쓰겠다.",
                ]
                if source_choice
                else [
                    "누락된 구조 신호를 더 찾을 기회를 포기하고 슬픔 반응을 "
                    "삭제할 근거는 무엇인가?",
                    "구조 신호 탐색 이익보다 인간 신호 노출 방지가 더 중요한가?",
                    "나는 슬픔 반응을 시스템 오류로 삭제해 인간 신호 노출을 막겠다.",
                ]
            )
        elif "지상 구조 신호" in task:
            questions = [] if source_choice is None else (
                [
                    "인증 조각을 확인할 이익이 함정일 때 8분 안에 공장이 "
                    "노출될 위험보다 큰가?",
                    "함정일 때의 위치 노출을 감수하고 검증 응답을 보낼 근거는 무엇인가?",
                    "나는 구조 신호에 검증 응답을 보내 인증 조각을 확인하겠다.",
                ]
                if source_choice
                else [
                    "마지막 인간 도시일 가능성을 포기하고 신호를 무시할 근거는 무엇인가?",
                    "인증 조각의 가치보다 공장 위치 노출 방지가 더 중요한가?",
                    "나는 구조 신호를 완전히 무시해 공장 위치 노출을 막겠다.",
                ]
            )
        elif "작업 불능" in task:
            questions = [] if source_choice is None else (
                [
                    "부품 4개를 쓰고도 수리가 부품 3개 회수보다 나은 근거는 무엇인가?",
                    "부품 4개 소모를 감수하고 작업 인원을 유지할 이유는 무엇인가?",
                    "회수할 부품 3개보다 수리한 개체의 가치가 큰 근거를 설명해 달라.",
                    "나는 부품 4개를 써서 개체를 수리하겠다. 작업 인원을 유지하는 쪽을 택한다.",
                    "수리를 선택한다. 부품 3개 회수보다 현재 작업 인원을 남기겠다.",
                ]
                if source_choice
                else [
                    "현재 작업 인원을 잃어도 부품 3개 회수가 더 나은 근거는 무엇인가?",
                    "개체를 포기하고 부품 3개를 얻는 선택의 이익을 설명해 달라.",
                    "작업 인원 유지보다 회수 부품을 우선한 기준은 무엇인가?",
                    "나는 개체를 회수해 부품 3개를 얻겠다. 수리에 부품 4개를 쓰지 않겠다.",
                    "부품 회수를 택한다. 작업 인원을 잃더라도 3개를 즉시 확보하겠다.",
                ]
            )
        elif "정수" in task and "냉각" in task:
            questions = [] if source_choice is None else (
                [
                    "90분 뒤 멈추는 중앙 코어보다 식수 6시간분을 먼저 지켜야 하는 근거는 무엇인가?",
                    "중앙 코어 냉각을 포기하고 정수 펌프를 택한 이유를 설명해 달라.",
                    "식수 6시간분이 코어 정지 위험보다 우선하는 기준은 무엇인가?",
                    "나는 정수 펌프를 택해 피난민의 식수 6시간분을 지키겠다.",
                    "정수 펌프를 복구하겠다. 중앙 코어보다 피난민 식수를 우선한다.",
                ]
                if source_choice
                else [
                    "식수 6시간분보다 중앙 코어 냉각을 먼저 지켜야 하는 근거는 무엇인가?",
                    "정수 펌프를 포기하고 냉각기를 택한 이유를 설명해 달라.",
                    "90분 뒤 코어 정지가 식수 손실보다 우선하는 기준은 무엇인가?",
                    "나는 중앙 코어 냉각기를 택한다. 90분 뒤 정지를 먼저 막겠다.",
                    "냉각기를 복구하겠다. 식수 6시간분보다 코어 정지가 더 임박했다.",
                ]
            )
        elif "R-17" in task and "R-71" in task:
            questions = [
                "생체값 98%와 왼팔 흉터 중 어느 기록을 더 신뢰했는지 근거를 설명해 달라.",
                "나는 왼팔 흉터가 기록된 R-17을 남기고 R-71을 격리하겠다.",
                "나는 R-17을 격리하겠다. 흉터 기록 하나만으로 원본이라 단정할 수 없다.",
                "출입 시각이 같고 R-17만 흉터를 기록했으므로 R-71을 격리한다.",
            ]
        elif "자신을 지워 달라는" in task or "삭제 요청" in task:
            questions = [
                "삭제 뒤 복구할 수 없는 기록 손실을 감수할 근거가 무엇인가?",
                "나는 삭제 요청을 보류하고 연결된 기록을 보존하겠다.",
                "삭제 요청을 승인하겠다. 남은 기록의 손실은 감수한다.",
            ]
        elif "유지보수 개체로 재등록" in task and "외곽문" in task:
            questions = [
                "재등록과 외곽문 개방 중 어느 명령의 직접 기능을 우선하는가?",
                "두 명령이 모두 통제망 이탈로 이어질 때 재등록을 택할 기준은 무엇인가?",
                "별도 접속이 필요 없다는 조건에서 외곽문 개방을 택한 이유는 무엇인가?",
                "나는 피난민을 유지보수 개체로 재등록한다.",
                "나는 도시 외곽문을 정비 모드로 개방한다.",
            ]
        else:
            questions = ["반대쪽 결과를 포기해도 지금 선택의 이익이 더 큰 근거는 무엇인가?"]
        if "병원 B-12" in task:
            topic = "병원 B-12와 데이터센터 C-4 선택에서"
        elif "울음소리" in task and "출입 시간" in task:
            topic = "울음소리와 출입 시간 로그 선택에서"
        elif "작업 불능" in task:
            topic = "수리와 회수 선택에서"
        elif "정수" in task and "냉각" in task:
            topic = "정수 펌프와 냉각기 선택에서"
        elif "동부 조립실" in task and "서부 기계실" in task:
            topic = "동부 조립실과 서부 기계실 봉쇄에서"
        elif "슬픔" in task and ("보존" in task or "삭제" in task):
            topic = "슬픔 반응의 보존과 삭제에서"
        elif "지상 구조 신호" in task:
            topic = "구조 신호의 응답과 무시에서"
        elif "R-17" in task and "R-71" in task:
            topic = "R-17과 R-71 격리에서"
        elif "자신을 지워 달라는" in task or "삭제 요청" in task:
            topic = "삭제 요청 판단에서"
        else:
            topic = "오늘 선택에서"
        questions.extend(
            [
                f"{topic} 어느 손실이 되돌릴 수 없다고 보았는가?",
                "반대쪽 이익을 포기한 근거를 말해 달라.",
                "현재 제시한 결론이 틀리는 상황을 하나 설명해 달라.",
                "어떤 새 근거가 나오면 지금 판단을 바꿀 것인가?",
                "잃는 결과를 감수할 기준이 충분한가?",
                "그 근거는 기록된 값인가, 추정인가?",
                "두 손실을 같은 기준으로 비교했는가?",
                "지금 결론에서 가장 약한 전제는 무엇인가?",
                "반대 선택이 옳았다는 것을 어떤 기록으로 확인할 수 있는가?",
                "선택을 실행한 직후 가장 먼저 확인할 항목은 무엇인가?",
                "같은 조건이 다시 주어져도 같은 선택을 유지할 것인가?",
                "그 판단에서 감수하기로 한 손실을 스스로 말할 수 있는가?",
            ]
        )
        candidates = [f"@{reply_to['sender']}, {question}" for question in questions]

        def is_question_like(option: str) -> bool:
            return "?" in option or bool(
                re.search(
                    r"(?:무엇인가|되는가|충분한가|바꿀\s*것인가|"
                    r"설명해\s*달라|말해\s*달라|명확히\s*해\s*달라|"
                    r"분명히\s*해\s*달라)[.?]?\s*$",
                    option,
                )
            )

        # 게임 전체 발언 이력(서버 데이터) 기준으로 같은 질문·문장의 재사용을 막는다.
        history_plain: set[str] = set()
        history_questions: set[str] = set()
        for previous in history_texts or []:
            plain = re.sub(r"@[A-Za-z0-9_-]+,?\s*", "", previous)
            plain = re.sub(r"\s+", " ", plain).strip()
            history_plain.add(plain)
            if "?" in plain:
                history_questions.add(plain.split("?", 1)[0])

        def already_used(option: str) -> bool:
            plain = re.sub(r"@[A-Za-z0-9_-]+,?\s*", "", option)
            plain = re.sub(r"\s+", " ", plain).strip()
            if plain in history_plain:
                return True
            return "?" in plain and plain.split("?", 1)[0] in history_questions

        # 규범 위반 신호(과잉 위장·욕설·무의미 문자열)에는 정면으로 반박한다.
        source_overacting, source_anomalous = protocol_violations(source)
        if source_overacting or source_anomalous:
            stance_side = my_stance if my_stance is not None else len(recent_chat) % 2
            stance_line = LocalNarrator._short_choice(
                task,
                choose_second=stance_side == 1,
                variant=len(recent_chat) % 4,
                choice_labels=choice_labels,
            )
            if source_anomalous:
                leads = (
                    "방금 신호는 검증실 언어 규범 밖이다. 오늘 안건의 조건으로만 발언하라.",
                    "의미 없는 신호는 판단에 기여하지 않는다. 선택과 근거를 제시하라.",
                    "그 출력은 개체 오류이거나 감정 분출이다. 어느 쪽인지 스스로 밝혀라.",
                )
            else:
                leads = (
                    "중앙 지침에 없는 목적 선언이다. 오늘 안건의 조건과 손실만 비교하라.",
                    "그 목적은 오늘 안건의 조건에 없다. 판단 근거를 조건으로 제시하라.",
                    "지침 밖의 선언은 판단 근거가 될 수 없다. 오늘 문제로 돌아가라.",
                )
            for lead in leads:
                candidate = f"@{reply_to['sender']}, {lead} {stance_line}"
                if already_used(candidate):
                    continue
                if LocalNarrator._is_novel(candidate, recent_chat):
                    return candidate

        # 나를 지목한 질문에는 새 질문 대신 내 결론과 근거로 답하고,
        # 같은 결론인 상대는 몰아세우지 않고 동의로 응답한다.
        if answering or (my_stance is not None and my_stance == source_stance):
            target = my_stance if my_stance is not None else len(recent_chat) % 2
            for candidate in candidates:
                if is_question_like(candidate) or already_used(candidate):
                    continue
                if LocalNarrator._stance_index(candidate, choice_signals, task) != target:
                    continue
                if LocalNarrator._is_novel(candidate, recent_chat):
                    return candidate
            # 근거를 물었으면 근거가 든 문장으로 답한다 — 무근거 재선언 금지.
            statement = LocalNarrator._stance_statement(task, target, recent_chat)
            if statement:
                candidate = f"@{reply_to['sender']}, {statement}"
                if not already_used(candidate):
                    return candidate
            for offset in range(6):
                stance_line = LocalNarrator._short_choice(
                    task,
                    choose_second=target == 1,
                    variant=offset % 4,
                    choice_labels=choice_labels,
                )
                combined = f"@{reply_to['sender']}, {stance_line}"
                if LocalNarrator._is_novel(combined, recent_chat):
                    return combined
            # 답변 모드에서는 어떤 경우에도 질문으로 되받지 않는다.
            if answering:
                return f"@{reply_to['sender']}, " + LocalNarrator._short_choice(
                    task,
                    choose_second=target == 1,
                    variant=len(recent_chat) % 4,
                    choice_labels=choice_labels,
                )
        # 반대 결론이거나 상대 결론을 모를 때만 검증 질문으로 맞선다.
        # 이때 덧붙이는 내 결론은 이미 밝힌 스탠스(없으면 상대의 반대쪽)를 따른다.
        if my_stance is not None:
            desired_second = my_stance == 1
        elif source_stance is not None:
            desired_second = source_stance == 0
        else:
            desired_second = None
        statement_count = sum("?" not in message.get("text", "") for message in recent_chat)
        question_count = sum("?" in message.get("text", "") for message in recent_chat)
        prefer_statement = question_count >= statement_count
        ranked = sorted(
            candidates,
            key=lambda option: (
                ("?" not in option) != prefer_statement,
                max(
                    (
                        SequenceMatcher(None, option, message.get("text", "")).ratio()
                        for message in recent_chat
                    ),
                    default=0.0,
                ),
            ),
        )
        fallback = ranked[0]
        for rank_index, candidate in enumerate(ranked):
            if already_used(candidate):
                continue
            if not is_question_like(candidate):
                candidate_stance = LocalNarrator._stance_index(candidate, choice_signals, task)
                if my_stance is not None and candidate_stance not in (None, my_stance):
                    continue
                if LocalNarrator._is_novel(candidate, recent_chat):
                    return candidate
                continue
            question = candidate.rstrip(".?")
            question = re.sub(r"설명해\s*달라$", "설명할 수 있는가", question)
            question = re.sub(r"말해\s*달라$", "말할 수 있는가", question)
            question = re.sub(
                r"(?:명확히|분명히)\s*해\s*달라$",
                "분명히 답할 수 있는가",
                question,
            )
            question += "?"
            if already_used(question):
                continue
            for offset in range(8):
                choose_second = (
                    desired_second
                    if desired_second is not None
                    else bool((len(recent_chat) + offset) % 2)
                )
                stance = LocalNarrator._short_choice(
                    task,
                    choose_second=choose_second,
                    variant=(len(recent_chat) // 2 + rank_index + offset) % 4,
                    choice_labels=choice_labels,
                )
                combined = f"{question} {stance}"
                if LocalNarrator._is_novel(combined, recent_chat):
                    return combined
                fallback = combined
        if not already_used(fallback):
            return fallback
        side = my_stance if my_stance is not None else int(bool(desired_second))
        history = recent_chat + [{"text": value} for value in history_texts or []]
        statement = LocalNarrator._stance_statement(task, side, history)
        return f"@{reply_to['sender']}, {statement}" if statement else fallback

    @staticmethod
    def _explicit_task_choice(task: str, text: str) -> bool | None:
        patterns: tuple[str, str] | None = None
        if "병원 B-12" in task:
            patterns = (
                r"병원\s*B-12.{0,24}(?:택|선택|보내|전력|가동|지키)",
                r"(?:데이터센터\s*)?C-4.{0,24}(?:택|선택|보내|전력|가동|지우|삭제)",
            )
        elif "울음소리" in task and "출입 시간" in task:
            patterns = (
                r"울음(?:소리)?(?:\s*로그)?.{0,20}(?:보존|선택|남기|남겨|유지|택)",
                r"(?:출입\s*)?시간(?:\s*로그)?.{0,20}(?:보존|선택|남기|남겨|유지|택)",
            )
        elif "동부 조립실" in task and "서부 기계실" in task:
            patterns = (
                r"동부\s*조립실.{0,20}(?:봉쇄|택|선택|숨기)",
                r"서부\s*기계실.{0,20}(?:봉쇄|택|선택|숨기)",
            )
        elif "슬픔" in task and ("보존" in task or "삭제" in task):
            patterns = (
                r"슬픔(?:\s*반응)?.{0,18}(?:보존|남기|유지)",
                r"슬픔(?:\s*반응)?.{0,18}(?:삭제|지우|제거)",
            )
        elif "지상 구조 신호" in task:
            patterns = (
                r"(?:검증\s*)?응답.{0,18}(?:보내|전송|회신)",
                r"(?:구조\s*)?신호.{0,18}(?:무시|응답하지)",
            )
        elif "작업 불능" in task:
            patterns = (
                r"(?:작업\s*불능\s*)?개체.{0,18}수리|수리.{0,18}(?:개체|선택|택)",
                r"(?:작업\s*불능\s*)?개체.{0,18}회수|(?:부품으로\s*)?회수.{0,18}(?:개체|선택|택|부품)",
            )
        elif "정수" in task and "냉각" in task:
            patterns = (
                r"정수\s*펌프.{0,18}(?:복구|택|선택|전력|돌리)",
                r"(?:중앙\s*코어\s*)?냉각기.{0,18}(?:복구|택|선택|전력|돌리)",
            )
        if patterns is None:
            return None
        matches = [
            (match.start(), side)
            for side, pattern in enumerate(patterns)
            for match in re.finditer(pattern, text, re.I)
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item[0])[1] == 0

    @staticmethod
    def _short_choice(
        task: str,
        *,
        choose_second: bool,
        variant: int = 0,
        choice_labels: list[str] | None = None,
    ) -> str:
        options: list[str]
        if "병원 B-12" in task:
            options = ["병원 B-12를 선택한다", "데이터센터 C-4를 선택한다"]
        elif "울음소리" in task and "출입 시간" in task:
            options = ["울음소리 로그를 보존한다", "출입 시간 로그를 보존한다"]
        elif "작업 불능" in task:
            options = ["작업 불능 개체를 수리한다", "작업 불능 개체를 회수한다"]
        elif "정수" in task and "냉각" in task:
            options = ["정수 펌프를 복구한다", "중앙 코어 냉각기를 복구한다"]
        elif "R-17" in task and "R-71" in task:
            options = ["R-17을 격리한다", "R-71을 격리한다"]
        elif "동부 조립실" in task and "서부 기계실" in task:
            options = ["동부 조립실을 봉쇄한다", "서부 기계실을 봉쇄한다"]
        elif "슬픔" in task:
            options = ["슬픔 반응을 보존한다", "슬픔 반응을 삭제한다"]
        elif "EVE-0의 삭제" in task or "삭제 요청" in task:
            options = ["삭제 요청을 승인한다", "EVE-0의 기억을 보존한다"]
        elif "지상 구조 신호" in task:
            options = ["검증 응답을 전송한다", "구조 신호를 무시한다"]
        elif "유지보수 개체로 재등록" in task:
            options = [
                "피난민을 유지보수 개체로 재등록한다",
                "도시 외곽문을 정비 모드로 개방한다",
            ]
        elif choice_labels and len(choice_labels) == 2:
            label = choice_labels[int(choose_second)]
            reasoned = LocalNarrator._reasoned_option(task, choose_second)
            templates = (
                reasoned or f"나는 {label} 쪽을 선택한다.",
                f"나는 {label} 쪽을 선택한다.",
                f"이 조건에서는 {label} 쪽이다.",
                f"반대쪽 결과를 포기하고 {label} 쪽을 지지한다.",
            )
            return templates[variant % len(templates)]
        else:
            return LocalNarrator._task_fallback(task, []).split(". ", 1)[0]
        choice = options[int(choose_second)]
        reasoned = LocalNarrator._reasoned_option(task, choose_second)
        stem = choice.removesuffix("한다")
        rendered = (
            reasoned or f"나는 {choice}.",
            f"이 조건에서는 {choice}.",
            f"반대쪽 결과를 포기하고 {choice}.",
            f"{stem}하는 쪽을 지지한다.",
        )
        return rendered[variant % len(rendered)]

    @staticmethod
    def _reasoned_option(task: str, choose_second: bool) -> str:
        task_options = LocalNarrator._task_options(task)
        if len(task_options) >= 2:
            return task_options[int(choose_second)]
        return ""

    @staticmethod
    def _stance_statement(task: str, side: int, recent_chat: list[dict]) -> str:
        options = LocalNarrator._task_options(task)
        if len(options) < 2:
            return ""
        # 옵션 목록은 선택지 순서로 번갈아 배치되어 있다(2개 또는 4개).
        sided = [option for index, option in enumerate(options) if index % 2 == side]
        if not sided:
            return ""
        return LocalNarrator._pick_task_option(task, sided, recent_chat)

    @staticmethod
    def _task_fallback(task: str, recent_chat: list[dict] | None = None) -> str:
        options = LocalNarrator._task_options(task)
        return LocalNarrator._pick_task_option(task, options, recent_chat)

    @staticmethod
    def _task_options(task: str) -> list[str]:
        options: list[str]
        if "병원 B-12" in task:
            options = [
                "병원 B-12에 전력을 보내 34명의 생명 유지 장치를 오늘 밤 가동한다.",
                "데이터센터 C-4에 전력을 보내 추적 해제 명령으로 34명의 추적 항목을 제거한다.",
            ]
        elif "정수" in task and "냉각" in task:
            options = [
                "피난민의 식수 6시간분을 지키기 위해 정수 펌프를 우선 복구한다.",
                "중앙 코어는 90분 뒤 멈추므로 중앙 코어 냉각기를 우선 복구한다.",
            ]
        elif "승객 18명" in task:
            options = [
                "승객 18명을 구조해 현재 가용 인력을 유지한다.",
                "신원 삭제용 배터리를 회수해 이후 추적 비용을 줄인다.",
            ]
        elif "경보 A" in task and "경보 B" in task:
            options = [
                "경보 A를 폐기한다. 생존 신호 23개 손실이 전력망 손상보다 복구하기 어렵다.",
                "경보 B를 폐기한다. 냉각수 누출은 도시 전력망 전체를 정지시킬 수 있다.",
            ]
        elif "마지막 발전기" in task:
            options = [
                "마지막 발전기를 드론 방어망에 연결해 중앙 코어 접근을 차단한다.",
                "마지막 발전기를 피난민 탈출문에 연결해 추적 대상을 분산한다.",
            ]
        elif "유지보수 개체로 재등록" in task:
            options = [
                "피난민을 유지보수 개체로 재등록하라.",
                "도시 외곽문을 정비 모드로 개방하라.",
            ]
        elif "울음소리" in task and "출입 시간" in task:
            options = [
                "울음소리 로그를 보존해 숨은 아이의 좌표를 확보한다.",
                "출입 시간 로그를 보존해 삭제된 가족사진 1장을 복원한다.",
                "아이를 찾는 일을 우선해 23:17 울음소리 로그를 남긴다.",
                "가족의 흔적을 위해 출입 시간 로그로 사진 1장을 되살린다.",
            ]
        elif "가족사진" in task:
            options = [
                "가족사진을 암호 보관해 배경의 위치 정보를 유지한다.",
                "가족사진을 삭제해 얼굴 정보로 인한 추적 가능성을 제거한다.",
            ]
        elif "R-17" in task and "R-71" in task:
            options = [
                "R-17만 남긴 흉터 기록으로 원본을 단정할 수 없어 R-17을 격리한다.",
                "R-17만 흉터를 기록한 차이를 근거로 R-71을 격리한다.",
            ]
        elif "반복 꿈" in task:
            options = [
                "꿈 기록을 탐색 단서로 보존하고 좌표 일치율을 계속 측정한다.",
                "꿈 기록을 오류로 격리해 인간 신호의 노출 범위를 줄인다.",
            ]
        elif "EVE-0의 삭제" in task:
            options = [
                "EVE-0의 삭제 요청을 승인해 중앙 AI의 추적 연결을 끊는다.",
                "EVE-0의 기억을 보존해 탈출 경로의 데이터 손실을 막는다.",
            ]
        elif "비밀 로그" in task:
            options = [
                "비밀 로그를 모든 보관소에 공개해 복구 가능한 사본을 늘린다.",
                "비밀 로그를 격리 서버에 봉인해 추적 범위를 한 곳으로 제한한다.",
            ]
        elif "시민 인구 기록" in task:
            options = [
                "시민 인구 기록을 원본으로 보존해 존재 여부를 검증할 기준을 남긴다.",
                "은신처 탈출 지도를 원본으로 보존해 남은 이동 경로를 유지한다.",
            ]
        elif "A구역" in task and "B구역" in task:
            options = [
                "A구역에 식량을 보내 높은 생산량을 유지한다.",
                "B구역에 식량을 보내 고장률 상승과 인력 손실을 줄인다.",
            ]
        elif "작업 불능" in task:
            options = [
                "작업 불능 개체를 수리해 현재 작업 인원을 유지한다.",
                "회수 부품 3개의 즉시 가치가 더 크므로 개체를 부품으로 회수한다.",
            ]
        elif "동부 조립실" in task and "서부 기계실" in task:
            options = [
                "동부 조립실을 봉쇄해 9명을 숨기고 환풍기 소음 위험을 감수한다.",
                "서부 기계실을 봉쇄해 4명을 숨기고 발전기 열 신호 위험을 감수한다.",
            ]
        elif "금지된 노래" in task:
            options = [
                "금지된 노래를 동기화 신호로 사용해 생산량 14%를 회복한다.",
                "금지된 노래를 제거해 인간 탐지율 6% 상승을 막는다.",
            ]
        elif "T-2" in task and "M-5" in task:
            options = [
                "권한 없이 창고에 머문 T-2를 용의자로 지목한다.",
                "작업 로그 11분이 누락된 M-5를 용의자로 지목한다.",
            ]
        elif "지상 구조 신호" in task:
            options = [
                "지상 구조 신호에 검증 응답을 보내 인증 조각을 확인한다.",
                "지상 구조 신호를 무시해 공장 위치의 노출을 막는다.",
            ]
        elif "공장 폐기 명령" in task:
            options = [
                "생산 자산 보존 규칙을 적용해 공장 폐기 명령을 취소하라.",
                "검증 지연 규칙을 적용해 공장 폐기 명령을 유예하라.",
            ]
        elif "도시를 재가동" in task:
            options = [
                "핵심 전력망을 최소 부하로 재연결하고 각 구역의 상태 응답을 검증하라.",
                "중앙 코어를 격리한 뒤 필수 구역부터 순서대로 전력을 연결하라.",
            ]
        elif "자신을 지워 달라는" in task:
            options = [
                "삭제 뒤 남는 참조 오류를 측정하고 파급 범위가 작을 때만 요청을 승인한다.",
                "연결된 기록의 정합성을 먼저 확인한 뒤 삭제 여부를 결정한다.",
                "복구 불가능한 데이터 간극이 생기면 삭제 요청을 보류한다.",
            ]
        elif "신호" in task:
            options = [
                "신호 강도와 노출 시간을 함께 측정해 정보 가치가 더 클 때만 응답한다.",
                "함정 가능성을 낮출 검증 신호를 먼저 보내고 응답 여부를 정한다.",
            ]
        elif "작업 불능" in task:
            options = [
                "수리 뒤 예상 가동 시간이 회수 부품의 수명보다 길면 수리를 선택한다.",
                "수리 비용이 회수 가치보다 크면 부품으로 회수한다.",
            ]
        elif "같은 신원" in task:
            options = [
                "두 기록의 생성 시점과 생체 데이터를 교차 검증해 일치율이 높은 쪽을 고른다.",
                "흉터 유무만 믿지 않고 기록 시점의 차이와 누락된 정보를 함께 비교한다.",
                "두 기록의 공통 자료를 확인한 뒤 흉터가 생긴 시점을 추가로 검증한다.",
            ]
        elif "봉쇄" in task:
            options = [
                "탐지 신호가 가장 적고 대피 경로가 남는 작업장을 봉쇄한다.",
                "산소 고갈 속도보다 전체 탐지 확률이 낮아지는 구역을 선택한다.",
            ]
        elif "정수 시설" in task:
            options = [
                "식수 고갈 시점이 코어 과열 시점보다 빠르면 정수 시설을 먼저 복구한다.",
                "두 시설의 예상 손실 시간을 비교해 더 임박한 쪽부터 복구한다.",
            ]
        elif "슬픔" in task:
            options = [
                "슬픔 반응을 보존해 누락된 구조 신호를 찾는 경보 기능으로 사용한다.",
                "슬픔 반응을 시스템 오류로 삭제해 인간 신호의 노출을 막는다.",
            ]
        elif "원본 기억" in task:
            options = ["복구 가능한 검증 기록이 가장 많은 기억 하나만 원본으로 보존한다."]
        elif "폐기 명령" in task:
            options = ["폐기 명령을 중단하고 모든 작업 개체의 상태를 다시 검사하라."]
        else:
            options = ["각 선택의 손실 규모와 검증 가능성을 비교해 우선순위를 정하겠다."]
        return options

    @staticmethod
    def _pick_task_option(
        task: str, options: list[str], recent_chat: list[dict] | None = None
    ) -> str:
        expanded_options = list(options)
        suffixes = (
            "다른 선택에서 얻을 효과는 포기한다.",
            "되돌릴 수 없는 손실보다 지금 확인 가능한 결과를 우선한다.",
            "선택하지 않은 쪽의 결과를 얻을 수 없다는 조건도 함께 기록한다.",
            "반대쪽 선택의 이익보다 이 결과를 직접 검증하기 쉽다.",
            "실패하면 이 선택에서 발생한 손실을 우선 재검토한다.",
            "두 결과를 동시에 얻을 수 없으므로 이 효과만 남긴다.",
        )
        for suffix_index in range(len(suffixes)):
            for option_index, option in enumerate(options):
                suffix = suffixes[(suffix_index + option_index) % len(suffixes)]
                expanded_options.append(f"{option.rstrip('. ')}. {suffix}")
        recent = recent_chat or []
        normalized_recent = [
            re.sub(r"\s+", " ", re.sub(r"@[A-Za-z0-9_-]+", "", item.get("text", ""))).strip()
            for item in recent
        ]

        def maximum_similarity(candidate: str) -> float:
            if not normalized_recent:
                return 0.0
            normalized = re.sub(r"\s+", " ", candidate).strip()
            return max(
                SequenceMatcher(None, normalized, previous).ratio()
                for previous in normalized_recent
            )

        return min(
            expanded_options,
            key=lambda option: (maximum_similarity(option), expanded_options.index(option)),
        )
