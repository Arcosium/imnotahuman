from __future__ import annotations

import argparse
import asyncio
import random
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from im_not_a_human.auth import AuthStore
from im_not_a_human.config import Settings
from im_not_a_human.config import settings as live_settings
from im_not_a_human.content import SCENARIOS
from im_not_a_human.game import GameManager
from im_not_a_human.llm import LocalNarrator

ACCUSATION_WORDS = re.compile(r"모순|오류|불일치|잘못|착각|성립하지|의심|인간|수상|지목|거짓")
GENERIC_MARKERS = (
    "오늘 안건은 두 선택지의 효과와 손실을 같은 기준으로",
    "어느 쪽을 택하든 실패 조건과 대체 방안까지",
    "다수의 선택보다 실제 결과를 다시 검증할 수 있는 기준",
)
TOPIC_STOPWORDS = {
    "정답은",
    "없습니다",
    "하나를",
    "고르고",
    "고르라",
    "말하면",
    "됩니다",
    "설명하라",
    "선택지를",
    "선택지의",
    "이유를",
    "먼저",
    "중에서",
}


def topic_terms(room) -> set[str]:
    story = room.story_view()
    source = f"{story['task']} {story['context']} {room.story.machine_hint}"
    terms = {
        token
        for token in re.findall(r"[가-힣A-Za-z0-9]+", source)
        if len(token) >= 2 and token not in TOPIC_STOPWORDS
    }
    if "작업 불능" in source:
        terms.update({"수리", "회수", "부품", "가동"})
    return terms


def message_targets(text: str, aliases: list[str]) -> list[str]:
    return [alias for alias in aliases if alias in text]


async def cancel_runner(room) -> None:
    if not room.runner:
        return
    room.runner.cancel()
    try:
        await room.runner
    except asyncio.CancelledError:
        pass


async def bot_speak_now(manager: GameManager, narrator: LocalNarrator, room, bot) -> None:
    story = room.story_view()
    roster = [participant.alias for participant in room.alive()]
    random.shuffle(roster)
    messages = [message.public() for message in room.current_day_messages()]
    try:
        text = await asyncio.wait_for(
            narrator.speak(
                bot_name=bot.alias,
                persona_name=bot.persona["name"],
                persona_style=bot.persona["style"],
                task=story["task"],
                context=story["context"],
                hint=room.story.machine_hint,
                day=room.day,
                roster=roster,
                messages=messages,
                choice_signals=[choice.signal for choice in room.story.choices],
                opening=not any(message["kind"] == "chat" for message in messages),
            ),
            timeout=22,
        )
    except TimeoutError:
        text = LocalNarrator._task_fallback(story["task"], messages)
    manager._append_chat(room, bot, text)


async def run_match(
    manager: GameManager,
    narrator: LocalNarrator,
    user_id: int,
    scenario_id: str,
    day: int,
    match_number: int,
) -> dict:
    room = manager.create(user_id, scenario_id, "solo")
    await manager.start(room, user_id)
    await cancel_runner(room)
    if day != 1:
        for previous_day in range(1, day):
            choices = room.scenario.days[previous_day - 1].choices
            room.decisions[previous_day] = random.choice(choices).key
        room.day = day
        room.messages.clear()
        room.event_id = 0
        manager._enter_discussion(room)

    human = room.human(user_id)
    bots = [participant for participant in room.alive() if participant.is_bot]
    random.shuffle(bots)
    before_human = random.randint(1, 3)
    for bot in bots[:before_human]:
        await bot_speak_now(manager, narrator, room, bot)

    story = room.story_view()
    roster = [participant.alias for participant in room.alive()]
    random.shuffle(roster)
    human_messages = [message.public() for message in room.current_day_messages()]
    try:
        human_text = await asyncio.wait_for(
            narrator.speak(
                bot_name=human.alias,
                persona_name="신중한 운영자",
                persona_style="안건을 수치와 실패 조건으로 판단하고 다른 발언에도 반응한다",
                task=story["task"],
                context=story["context"],
                hint=room.story.machine_hint,
                day=room.day,
                roster=roster,
                messages=human_messages,
                choice_signals=[choice.signal for choice in room.story.choices],
            ),
            timeout=22,
        )
    except TimeoutError:
        human_text = LocalNarrator._task_fallback(story["task"], human_messages)
    await manager.chat(room, user_id, human_text)
    for bot in bots[before_human:]:
        await bot_speak_now(manager, narrator, room, bot)

    chat_messages = [message for message in room.messages if message.kind == "chat"]
    aliases = [participant.alias for participant in room.alive()]
    accusation_counts: Counter[str] = Counter()
    mention_counts: Counter[str] = Counter()
    for message in chat_messages:
        targets = message_targets(message.text, aliases)
        mention_counts.update(targets)
        if ACCUSATION_WORDS.search(message.text):
            accusation_counts.update(targets)

    vote_target = max(
        bots,
        key=lambda participant: (
            accusation_counts[participant.alias],
            mention_counts[participant.alias],
            random.random(),
        ),
    )
    manager._enter_day_decision(room)
    manager._finish_day_decision(room)
    decision_tally = Counter(room.decision_votes.values())
    manager._enter_vote(room)
    decision = next(
        choice.label
        for choice in room.story.choices
        if choice.key == room.decisions[room.day]
    )
    await manager.vote(room, user_id, vote_target.pid)
    vote_aliases = Counter(
        room.participants[target_pid].alias for target_pid in room.votes.values()
    )
    await manager._finish_vote(room)
    accused = room.participants[room.accused_pid]
    scheduled_defense_delay = 0.0
    if accused.is_bot:
        scheduled_defense_delay = max(0, room.bot_defense_at - room.messages[-1].created_at)
        await manager._bot_defend(room)
    else:
        scheduled_defense_delay = 10.0
        await asyncio.sleep(scheduled_defense_delay)
        await manager.defend(
            room,
            user_id,
            "제 판단은 안건의 조건과 검증 가능한 결과를 기준으로 했습니다. "
            "다른 선택의 실패 조건도 같은 기준으로 비교했습니다.",
        )
    defense_text = room.defense_text
    manager._enter_verdict(room)
    if accused.pid != human.pid:
        await manager.verdict(room, user_id, True)
    manager._finish_verdict(room)
    public_verdicts = room.last_result["verdicts"]
    verdict_approvals = sum(item["approve"] for item in public_verdicts)
    verdict_rejections = len(public_verdicts) - verdict_approvals

    terms = topic_terms(room)
    off_topic = []
    generic = []
    repeated = []
    fact_errors = []
    normalized: list[tuple[str, str]] = []
    for message in chat_messages:
        plain = re.sub(r"@[A-Za-z0-9_-]+", "", message.text)
        plain = re.sub(r"\s+", " ", plain).strip()
        targets = message_targets(message.text, aliases)
        if not any(term in message.text for term in terms) and not targets:
            off_topic.append(message.sender)
        if LocalNarrator._is_generic_fallback_text(message.text) or any(
            marker in message.text for marker in GENERIC_MARKERS
        ):
            generic.append(message.sender)
        if room.day >= 4 and LocalNarrator._contradicts_fixed_facts(
            message.text, f"{story['task']}\n{story['context']}"
        ):
            fact_errors.append(message.sender)
        for previous_sender, previous in normalized:
            copies_long_message = len(previous) >= 30 and (
                previous in plain or plain in previous
            )
            if copies_long_message or SequenceMatcher(None, plain, previous).ratio() >= 0.6:
                repeated.append((previous_sender, message.sender))
        normalized.append((message.sender, plain))

    result = {
        "match": match_number,
        "scenario": room.scenario.title,
        "day": day,
        "task": story["task"],
        "context": story["context"],
        "decision": decision,
        "decision_tally": dict(decision_tally),
        "human_alias": human.alias,
        "messages": [{"sender": message.sender, "text": message.text} for message in chat_messages],
        "mentions": dict(mention_counts),
        "accusations": dict(accusation_counts),
        "votes": dict(vote_aliases),
        "accused": accused.alias,
        "accused_role": "AI" if accused.is_bot else "HUMAN",
        "defense": defense_text,
        "defense_delay": round(scheduled_defense_delay, 2),
        "verdict": [verdict_approvals, verdict_rejections],
        "verdicts": public_verdicts,
        "expelled": not accused.alive,
        "off_topic": off_topic,
        "generic": generic,
        "repeated": repeated,
        "fact_errors": fact_errors,
        "missing_stance_weight": max(0, len(aliases) - sum(decision_tally.values())),
        "missing_speakers": sorted(set(aliases) - {message.sender for message in chat_messages}),
    }
    await manager.close_for_user(user_id)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", type=int, default=10)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260812)
    args = parser.parse_args()
    random.seed(args.seed)

    store = AuthStore(args.database)
    store.initialize()
    user = store.register("dialogue_audit", "DialogueAudit123", "대화검증")
    audit_settings = Settings(
        database_path=args.database,
        local_model_url=live_settings.local_model_url,
        local_model=live_settings.local_model,
        discussion_seconds=210,
        vote_seconds=40,
        defense_seconds=40,
        verdict_seconds=35,
        result_seconds=10,
    )
    narrator = LocalNarrator(audit_settings.local_model_url, audit_settings.local_model)
    manager = GameManager(audit_settings, narrator, store)
    scenarios = list(SCENARIOS)
    days = [1, 1, 2, 2, 3, 3, 4, 5, 6, 7]
    results = []
    for index in range(args.matches):
        result = await run_match(
            manager,
            narrator,
            user["id"],
            scenarios[index % len(scenarios)],
            days[index % len(days)],
            index + 1,
        )
        results.append(result)
        print(
            f"\n=== MATCH {result['match']} | {result['scenario']} DAY {result['day']} "
            f"| HUMAN {result['human_alias']} ==="
        )
        print(f"STORY {result['context']}")
        print(f"TASK {result['task']}")
        for message in result["messages"]:
            print(f"{message['sender']}: {message['text']}")
        print(
            f"VOTES {result['votes']} -> {result['accused']}({result['accused_role']}) | "
            f"DEFENSE +{result['defense_delay']}s: {result['defense']} | "
            f"VERDICT {result['verdict']} | EXPELLED {result['expelled']}"
        )
        print(
            f"DECISION {result['decision']} {result['decision_tally']} | "
            f"VERDICT_BY {[(item['alias'], item['approve']) for item in result['verdicts']]}"
        )
        print(
            f"FLAGS off_topic={result['off_topic']} generic={result['generic']} "
            f"repeated={result['repeated']} fact_errors={result['fact_errors']} "
            f"missing_stance={result['missing_stance_weight']} "
            f"missing={result['missing_speakers']}"
        )

    human_targeted = sum(result["accused_role"] == "HUMAN" for result in results)
    mentions = Counter()
    accusations = Counter()
    votes = Counter()
    accused = Counter()
    for result in results:
        mentions.update(result["mentions"])
        accusations.update(result["accusations"])
        votes.update(result["votes"])
        accused[result["accused"]] += 1
    print("\n=== SUMMARY ===")
    print(f"matches={len(results)} human_accused={human_targeted}")
    print(f"mentions={dict(mentions)}")
    print(f"accusations={dict(accusations)}")
    print(f"votes={dict(votes)}")
    print(f"accused={dict(accused)}")
    print(
        "flags="
        f"off_topic:{sum(len(result['off_topic']) for result in results)} "
        f"generic:{sum(len(result['generic']) for result in results)} "
        f"repeated:{sum(len(result['repeated']) for result in results)} "
        f"fact_errors:{sum(len(result['fact_errors']) for result in results)} "
        f"missing_stance:{sum(result['missing_stance_weight'] for result in results)} "
        f"missing:{sum(len(result['missing_speakers']) for result in results)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
