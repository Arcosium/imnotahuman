from __future__ import annotations

import asyncio
import hashlib
import random
import re
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .config import Settings
from .content import BOT_NAMES, DAY_DIFFICULTIES, PERSONAS, SCENARIOS, STARTERS
from .llm import LocalNarrator, protocol_violations

Broadcast = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class Participant:
    pid: str
    alias: str
    user_id: int | None = None
    nickname: str = ""
    is_bot: bool = False
    persona: dict | None = None
    alive: bool = True
    revealed: str | None = None
    score: int = 0
    last_message_at: float = 0.0
    day_stance: int | None = None
    messages_sent: int = 0
    ai_expelled: int = 0
    scored_days: set[int] = field(default_factory=set)
    machine_days: set[int] = field(default_factory=set)
    spoken_days: set[int] = field(default_factory=set)
    suspicion_log: list[dict] = field(default_factory=list)
    death_info: dict | None = None
    score_breakdown: dict[str, int] = field(
        default_factory=lambda: {
            "survival": 0,
            "deduction": 0,
            "machine": 0,
            "defense": 0,
            "clear": 0,
            "penalty": 0,
        }
    )

    def award(self, category: str, points: int) -> None:
        self.score_breakdown[category] = self.score_breakdown.get(category, 0) + points
        self.score = max(0, sum(self.score_breakdown.values()))

    def public(self, viewer_user_id: int) -> dict:
        return {
            "pid": self.pid,
            "alias": self.alias,
            "alive": self.alive,
            "revealed": self.revealed,
            "you": self.user_id == viewer_user_id,
            "score": self.score if self.user_id == viewer_user_id else None,
            "score_breakdown": self.score_breakdown if self.user_id == viewer_user_id else None,
        }


@dataclass(slots=True)
class ChatMessage:
    message_id: int
    sender_pid: str | None
    sender: str
    text: str
    kind: str
    created_at: float

    def public(self) -> dict:
        return {
            "id": self.message_id,
            "sender_pid": self.sender_pid,
            "sender": self.sender,
            "text": self.text,
            "kind": self.kind,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Room:
    code: str
    host_user_id: int
    scenario_id: str
    mode: str
    password_hash: str = ""
    hide_ids: bool = False
    briefing: bool = False
    max_humans: int = 2
    target_size: int = 8
    participants: dict[str, Participant] = field(default_factory=dict)
    phase: str = "lobby"
    day: int = 1
    deadline: float = 0
    messages: list[ChatMessage] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    accused_pid: str | None = None
    defense_text: str = ""
    bot_defense_at: float = 0
    last_result: dict | None = None
    next_ai_at: dict[str, float] = field(default_factory=dict)
    next_ai_message_at: float = 0
    pending_ai_pids: set[str] = field(default_factory=set)
    ai_tasks: set[asyncio.Task] = field(default_factory=set)
    suspicion: dict[str, dict[str, float]] = field(default_factory=dict)
    silent_pids: set[str] = field(default_factory=set)
    decisions: dict[int, str] = field(default_factory=dict)
    decision_votes: dict[str, str] = field(default_factory=dict)
    vote_reasons: list[dict] = field(default_factory=list)
    night_judging: bool = False
    verdict_judging: bool = False
    day_start_message_id: int = 0
    warned_day: int = 0
    event_id: int = 0
    started: bool = False
    started_at: float = 0
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runner: asyncio.Task | None = None

    @property
    def scenario(self):
        return SCENARIOS[self.scenario_id]

    @property
    def story(self):
        return self.scenario.days[self.day - 1]

    def alive(self) -> list[Participant]:
        return [p for p in self.participants.values() if p.alive]

    def human(self, user_id: int) -> Participant | None:
        return next((p for p in self.participants.values() if p.user_id == user_id), None)

    def add_system(self, text: str, kind: str = "system") -> None:
        self.event_id += 1
        self.messages.append(ChatMessage(self.event_id, None, "중앙 통제", text, kind, time.time()))
        self.messages = self.messages[-120:]

    def current_day_messages(self) -> list[ChatMessage]:
        return [
            message
            for message in self.messages
            if message.message_id >= self.day_start_message_id
        ]

    def story_view(self) -> dict[str, str]:
        task = self.story.task
        context = self.story.context
        if self.day > 1:
            previous = self.scenario.days[self.day - 2]
            choice_key = self.decisions.get(self.day - 1)
            choice = next((item for item in previous.choices if item.key == choice_key), None)
            if choice:
                task = choice.next_task or task
                context = f"이전 결론: {choice.consequence} 현재 상황: {context}"
        return {"task": task, "context": context}

    def _build_report(self, me: Participant) -> dict:
        top_events = sorted(
            (
                event
                for event in me.suspicion_log
                if event.get("delta", 0) > 0 and event["kind"] != "night"
            ),
            key=lambda event: -event["delta"],
        )[:4]
        night_notes = [
            event for event in me.suspicion_log if event["kind"] == "night"
        ][-2:]
        return {
            "death": me.death_info,
            "events": top_events,
            "night_notes": night_notes,
        }

    def _participant_rows(self, user_id: int) -> list[dict]:
        # 대기실에서만 닉네임을 노출한다 — 시작 후 노출하면 인간 좌석이 드러난다.
        rows = []
        for p in self.participants.values():
            row = p.public(user_id)
            if not self.started:
                row["nickname"] = p.nickname
                if self.hide_ids:
                    row["alias"] = ""
            rows.append(row)
        return rows

    def snapshot(self, user_id: int) -> dict:
        now = time.time()
        me = self.human(user_id)
        alive_bots = [p for p in self.alive() if p.is_bot]
        risk = 0.0
        if me and alive_bots:
            risk = sum(self.suspicion.get(bot.pid, {}).get(me.pid, 0) for bot in alive_bots) / len(
                alive_bots
            )
        risk_label = "낮음" if risk < 0.8 else "주의" if risk < 1.6 else "위험"
        story = self.story_view()
        visible_messages = self.messages[-60:]
        if me and self.day in me.spoken_days:
            visible_messages = [
                message for message in visible_messages if message.kind != "response_rule"
            ]
        return {
            "code": self.code,
            "mode": self.mode,
            "host": self.host_user_id == user_id,
            "scenario": {
                "id": self.scenario_id,
                "title": self.scenario.title,
                "background": self.scenario.background,
            },
            "phase": self.phase,
            "day": self.day,
            "deadline": self.deadline,
            "remaining_seconds": max(0, self.deadline - now) if self.deadline else 0,
            "story": {
                "title": self.story.title,
                "task": story["task"],
                "context": story["context"],
                "hint": self.story.machine_hint,
                "starter": STARTERS[self.scenario_id][self.day - 1],
                "difficulty": DAY_DIFFICULTIES[self.day - 1],
                "choices": [
                    {"key": choice.key, "label": choice.label} for choice in self.story.choices
                ],
            },
            "participants": self._participant_rows(user_id),
            "messages": [message.public() for message in visible_messages],
            "accused_pid": self.accused_pid,
            "defense": self.defense_text,
            "votes_cast": len(self.votes),
            "verdicts_cast": len(self.verdicts),
            "last_result": self.last_result,
            "started": self.started,
            "briefing": self.briefing,
            "risk": risk_label,
            "you_alive": bool(me and me.alive),
            "report": self._build_report(me) if self.phase == "ended" and me else None,
            "ending": (
                next(
                    (m.text for m in reversed(self.messages) if m.kind == "ending"),
                    None,
                )
                if self.phase == "ended"
                else None
            ),
            "you_voted": bool(me and me.pid in self.votes),
            "you_verdict": bool(me and me.pid in self.verdicts),
            "you_decided": self.decision_votes.get(me.pid) if me else None,
            "decision_votes_cast": len(self.decision_votes),
            "defense_submitted": bool(me and me.pid == self.accused_pid and self.defense_text),
            "closed": self.closed,
        }


class GameManager:
    def __init__(self, settings: Settings, narrator: LocalNarrator, store):
        self.settings = settings
        self.narrator = narrator
        self.store = store
        self.rooms: dict[str, Room] = {}
        self.membership: dict[int, str] = {}
        self.listeners: dict[str, set[Broadcast]] = {}

    def _code(self) -> str:
        while True:
            code = "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))
            if code not in self.rooms:
                return code

    @staticmethod
    def _alias(index: int) -> str:
        return f"UNIT-{index + 1:02d}"

    def create(
        self,
        user_id: int,
        scenario_id: str,
        mode: str,
        password: str = "",
        nickname: str = "",
        hide_ids: bool = False,
    ) -> Room:
        if scenario_id not in SCENARIOS:
            raise ValueError("존재하지 않는 시나리오입니다.")
        if self.room_for(user_id):
            raise ValueError("참가 중인 방에서 먼저 나가세요.")
        password_hash = hashlib.sha256(password.encode()).hexdigest() if password else ""
        room = Room(
            self._code(),
            user_id,
            scenario_id,
            mode,
            password_hash=password_hash,
            hide_ids=hide_ids,
            max_humans=1 if mode == "solo" else 2,
            target_size=8,
        )
        participant = Participant(
            secrets.token_hex(5), self._alias(0), user_id=user_id, nickname=nickname
        )
        room.participants[participant.pid] = participant
        self.rooms[room.code] = room
        self.membership[user_id] = room.code
        room.add_system("참가자 확인 중. 모든 신원 표식이 제거됩니다.")
        return room

    def join(self, user_id: int, code: str, password: str = "", nickname: str = "") -> Room:
        room = self.rooms.get(code.upper())
        if not room:
            raise ValueError("방을 찾을 수 없습니다.")
        if room.started:
            existing = room.human(user_id)
            if existing:
                self.membership[user_id] = room.code
                return room
            raise ValueError("이미 검증이 시작된 방입니다.")
        supplied_hash = hashlib.sha256(password.encode()).hexdigest()
        if room.password_hash and not secrets.compare_digest(room.password_hash, supplied_hash):
            raise ValueError("방 비밀번호가 맞지 않습니다.")
        human_count = sum(not participant.is_bot for participant in room.participants.values())
        if human_count >= room.max_humans:
            raise ValueError("방이 가득 찼습니다.")
        if self.room_for(user_id):
            raise ValueError("참가 중인 방에서 먼저 나가세요.")
        p = Participant(
            secrets.token_hex(5),
            self._alias(len(room.participants)),
            user_id=user_id,
            nickname=nickname,
        )
        room.participants[p.pid] = p
        self.membership[user_id] = room.code
        room.add_system(f"{p.alias} 연결 완료.")
        return room

    def leave(self, user_id: int) -> None:
        code = self.membership.get(user_id)
        room = self.rooms.get(code or "")
        if not room:
            self.membership.pop(user_id, None)
            return
        if room.started:
            raise ValueError("진행 중인 방은 명시적으로 종료해야 합니다.")
        self.membership.pop(user_id, None)
        p = room.human(user_id)
        if p:
            room.participants.pop(p.pid, None)
        room.briefing = False
        if not room.participants:
            self.rooms.pop(room.code, None)

    @staticmethod
    def _serialize_room(room: Room, now: float) -> dict:
        def participant_payload(p: Participant) -> dict:
            return {
                "pid": p.pid,
                "alias": p.alias,
                "user_id": p.user_id,
                "is_bot": p.is_bot,
                "persona": p.persona,
                "alive": p.alive,
                "revealed": p.revealed,
                "score": p.score,
                "day_stance": p.day_stance,
                "messages_sent": p.messages_sent,
                "ai_expelled": p.ai_expelled,
                "scored_days": sorted(p.scored_days),
                "machine_days": sorted(p.machine_days),
                "spoken_days": sorted(p.spoken_days),
                "score_breakdown": p.score_breakdown,
                "suspicion_log": p.suspicion_log,
                "death_info": p.death_info,
            }

        return {
            "code": room.code,
            "host_user_id": room.host_user_id,
            "scenario_id": room.scenario_id,
            "mode": room.mode,
            "day": room.day,
            "phase": room.phase,
            "remaining": max(0.0, room.deadline - now) if room.deadline else 0.0,
            "participants": [
                participant_payload(p) for p in room.participants.values()
            ],
            "messages": [m.public() for m in room.messages],
            "votes": room.votes,
            "verdicts": room.verdicts,
            "accused_pid": room.accused_pid,
            "defense_text": room.defense_text,
            "last_result": room.last_result,
            "suspicion": room.suspicion,
            "silent_pids": sorted(room.silent_pids),
            "decisions": {str(day): key for day, key in room.decisions.items()},
            "decision_votes": room.decision_votes,
            "day_start_message_id": room.day_start_message_id,
            "warned_day": room.warned_day,
            "event_id": room.event_id,
            "elapsed": max(0.0, now - room.started_at),
        }

    @staticmethod
    def _deserialize_room(payload: dict) -> Room:
        room = Room(
            payload["code"],
            payload["host_user_id"],
            payload["scenario_id"],
            payload["mode"],
        )
        for entry in payload["participants"]:
            participant = Participant(
                pid=entry["pid"],
                alias=entry["alias"],
                user_id=entry["user_id"],
                is_bot=entry["is_bot"],
                persona=entry["persona"],
                alive=entry["alive"],
                revealed=entry["revealed"],
                score=entry["score"],
                day_stance=entry["day_stance"],
                messages_sent=entry["messages_sent"],
                ai_expelled=entry["ai_expelled"],
                scored_days=set(entry["scored_days"]),
                machine_days=set(entry["machine_days"]),
                spoken_days=set(entry["spoken_days"]),
                score_breakdown=entry["score_breakdown"],
                suspicion_log=entry["suspicion_log"],
                death_info=entry["death_info"],
            )
            room.participants[participant.pid] = participant
        room.messages = [
            ChatMessage(
                m["id"], m["sender_pid"], m["sender"], m["text"], m["kind"],
                m["created_at"],
            )
            for m in payload["messages"]
        ]
        room.day = payload["day"]
        room.phase = payload["phase"]
        room.votes = payload["votes"]
        room.verdicts = payload["verdicts"]
        room.accused_pid = payload["accused_pid"]
        room.defense_text = payload["defense_text"]
        room.last_result = payload["last_result"]
        room.suspicion = payload["suspicion"]
        room.silent_pids = set(payload["silent_pids"])
        room.decisions = {int(day): key for day, key in payload["decisions"].items()}
        room.decision_votes = payload["decision_votes"]
        room.day_start_message_id = payload["day_start_message_id"]
        room.warned_day = payload["warned_day"]
        room.event_id = payload["event_id"]
        room.started = True
        return room

    async def suspend(self, user_id: int) -> None:
        room = self.room_for(user_id)
        if not room or room.mode != "solo" or not room.started:
            raise ValueError("저장할 수 있는 진행 중 솔로 게임이 없습니다.")
        async with room.lock:
            if room.phase in {"ended", "closed"}:
                raise ValueError("이미 종료된 게임은 저장할 수 없습니다.")
            payload = self._serialize_room(room, time.time())
            self.store.save_game(user_id, payload)
        await self.close_for_user(user_id)

    async def resume(self, user_id: int) -> Room:
        if self.room_for(user_id):
            raise ValueError("참가 중인 방에서 먼저 나가세요.")
        payload = self.store.load_game(user_id)
        if not payload:
            raise ValueError("저장된 게임이 없습니다.")
        room = self._deserialize_room(payload)
        if room.code in self.rooms:
            room.code = self._code()
        now = time.time()
        room.started_at = now - payload.get("elapsed", 0.0)
        if room.phase in {"discussion", "day_decision", "night_vote", "defense", "verdict"}:
            room.deadline = now + max(8.0, payload.get("remaining", 20.0))
        elif room.phase == "result":
            room.deadline = now + max(3.0, payload.get("remaining", 3.0))
        for bot in (p for p in room.alive() if p.is_bot):
            room.next_ai_at[bot.pid] = now + random.uniform(5, 10)
        room.next_ai_message_at = now + random.uniform(4, 7)
        if (
            room.phase == "defense"
            and not room.defense_text
            and (accused := room.participants.get(room.accused_pid or ""))
            and accused.is_bot
        ):
            room.bot_defense_at = now + random.uniform(4, 7)
        self.rooms[room.code] = room
        self.membership[user_id] = room.code
        self.store.delete_game(user_id)
        room.add_system("저장 지점에서 검증을 재개합니다.", "system")
        room.runner = asyncio.create_task(self._run(room), name=f"room-{room.code}")
        return room

    async def exit_game(self, user_id: int, action: str = "save") -> str:
        room = self.room_for(user_id)
        if not room or not room.started:
            raise ValueError("진행 중인 게임이 없습니다.")
        if room.phase == "ended":
            return await self.leave_finished_game(user_id)
        if room.mode == "solo":
            if action == "close":
                await self.close_for_user(user_id)
                return "closed"
            await self.suspend(user_id)
            return "saved"
        async with room.lock:
            participant = room.human(user_id)
            remaining_humans = [
                p
                for p in room.participants.values()
                if not p.is_bot and p.user_id not in (None, user_id)
            ]
            if not remaining_humans:
                pass
            elif participant:
                # 이탈자의 자리는 AI가 조용히 이어받는다 — 다른 참가자에게 알리지 않는다.
                self.membership.pop(user_id, None)
                participant.user_id = None
                participant.is_bot = True
                participant.persona = random.choice(PERSONAS)
                room.suspicion.setdefault(
                    participant.pid,
                    {
                        target.pid: random.uniform(0.05, 0.2)
                        for target in room.participants.values()
                        if target.pid != participant.pid
                    },
                )
                now = time.time()
                room.next_ai_at[participant.pid] = now + random.uniform(6, 12)
                if (
                    room.phase == "defense"
                    and room.accused_pid == participant.pid
                    and not room.defense_text
                ):
                    room.bot_defense_at = now + random.uniform(4, 7)
                if (
                    room.phase == "day_decision"
                    and participant.pid not in room.decision_votes
                ):
                    room.decision_votes[participant.pid] = self._bot_decision(
                        room, participant
                    ).key
                self._persist(room)
        if not remaining_humans:
            await self.close_for_user(user_id)
            return "closed"
        await self.broadcast(room)
        return "handed_over"

    async def leave_finished_game(self, user_id: int) -> str:
        room = self.room_for(user_id)
        if not room or room.phase != "ended":
            raise ValueError("종료된 게임이 없습니다.")
        async with room.lock:
            others = [
                p for p in room.participants.values()
                if p.user_id is not None and p.user_id != user_id
                and self.membership.get(p.user_id) == room.code
            ]
            if others:
                self.membership.pop(user_id, None)
                return "left"
        await self.close_for_user(user_id)
        return "closed"

    async def close_for_user(self, user_id: int) -> None:
        room = self.room_for(user_id)
        if not room:
            return
        async with room.lock:
            room.closed = True
            room.phase = "closed"
            room.deadline = 0
            room.add_system(
                "참가자가 검증실을 떠났습니다. 방과 모든 진행 작업을 종료합니다.", "ending"
            )
            runner = room.runner
            room.runner = None
            ai_tasks = list(room.ai_tasks)
            room.ai_tasks.clear()
            room.pending_ai_pids.clear()
        if runner and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        for task in ai_tasks:
            task.cancel()
        if ai_tasks:
            await asyncio.gather(*ai_tasks, return_exceptions=True)
        await self.broadcast(room)
        for participant in room.participants.values():
            if (
                participant.user_id is not None
                and self.membership.get(participant.user_id) == room.code
            ):
                self.membership.pop(participant.user_id, None)
        self.rooms.pop(room.code, None)
        self.store.delete_room(room.code)

    def room_for(self, user_id: int) -> Room | None:
        return self.rooms.get(self.membership.get(user_id, ""))

    async def start(self, room: Room, user_id: int) -> None:
        async with room.lock:
            if room.host_user_id != user_id:
                raise ValueError("방을 만든 참가자만 시작할 수 있습니다.")
            if room.started:
                return
            human_count = sum(not participant.is_bot for participant in room.participants.values())
            if room.mode == "multi" and human_count < 2:
                raise ValueError("멀티플레이는 사람 참가자가 2명 이상이어야 시작할 수 있습니다.")
            persona_pool = list(PERSONAS)
            random.shuffle(persona_pool)
            bot_names = list(BOT_NAMES)
            random.shuffle(bot_names)
            while len(room.participants) < room.target_size:
                index = len(room.participants)
                bot = Participant(
                    pid="bot-" + secrets.token_hex(4),
                    alias=self._alias(index),
                    is_bot=True,
                    persona=persona_pool[index % len(persona_pool)],
                )
                room.participants[bot.pid] = bot
                bot.alias = bot_names[index % len(bot_names)]
            # Human aliases are also machine-style and shuffled among the roster.
            aliases = [p.alias for p in room.participants.values()]
            random.shuffle(aliases)
            for participant, alias in zip(room.participants.values(), aliases, strict=True):
                participant.alias = alias
            room.started = True
            room.started_at = time.time()
            self._initialize_suspicion(room)
            self._enter_discussion(room)
            room.runner = asyncio.create_task(self._run(room), name=f"room-{room.code}")
        await self.broadcast(room)

    def _initialize_suspicion(self, room: Room) -> None:
        for bot in (p for p in room.participants.values() if p.is_bot):
            room.suspicion[bot.pid] = {
                target.pid: random.uniform(0.05, 0.2)
                for target in room.participants.values()
                if target.pid != bot.pid
            }

    def _enter_discussion(self, room: Room) -> None:
        room.phase = "discussion"
        discussion_time = max(45, self.settings.discussion_seconds - (room.day - 1) * 10)
        room.deadline = time.time() + discussion_time
        room.votes.clear()
        room.verdicts.clear()
        room.decision_votes.clear()
        room.accused_pid = None
        room.defense_text = ""
        room.bot_defense_at = 0
        room.last_result = None
        room.silent_pids.clear()
        for participant in room.participants.values():
            participant.day_stance = None
        room.day_start_message_id = room.event_id + 1
        story = room.story_view()
        if room.story.bridge:
            room.add_system(room.story.bridge, "story")
        room.add_system(f"DAY {room.day}. {room.story.title}. {story['context']}", "day")
        room.add_system("검증 문제: " + story["task"], "task")
        room.add_system(
            "응답 규칙: 토론 중 최소 1회 발언하십시오. 무응답 개체는 비정상 신호로 탐지됩니다.",
            "response_rule",
        )
        now = time.time()
        room.next_ai_message_at = now + random.uniform(10, 12)
        for bot in (p for p in room.alive() if p.is_bot):
            room.next_ai_at[bot.pid] = now + random.uniform(7, 11)

    async def _run(self, room: Room) -> None:
        try:
            while room.phase not in {"ended", "closed"}:
                await asyncio.sleep(1)
                changed = False
                async with room.lock:
                    now = time.time()
                    if room.phase == "discussion":
                        if now >= room.deadline:
                            self._enter_day_decision(room)
                            changed = True
                        elif room.deadline - now <= 10 and room.warned_day != room.day:
                            room.warned_day = room.day
                            room.add_system(
                                "토론 종료 10초 전입니다. 발언을 마무리하십시오.",
                                "warning",
                            )
                            changed = True
                        elif len(room.pending_ai_pids) < 2:
                            ready = [
                                p
                                for p in room.alive()
                                if p.is_bot
                                and p.pid not in room.pending_ai_pids
                                and room.next_ai_at.get(p.pid, now + 1) <= now
                            ]
                            if ready and now >= room.next_ai_message_at:
                                minimum_gap = max(10, 16 - room.day)
                                remaining = room.deadline - now
                                unspoken = [
                                    p for p in ready if room.day not in p.spoken_days
                                ]
                                all_unspoken = sum(
                                    1
                                    for p in room.alive()
                                    if p.is_bot and room.day not in p.spoken_days
                                )
                                pinged = [
                                    p for p in ready if self._awaits_reply(room, p)
                                ]
                                # 하루 1회 발언 보장: 남은 시간이 미발언 봇을 소화할
                                # 만큼만 남으면 미발언 봇을 최우선하고 간격을 압축한다.
                                tight = bool(
                                    unspoken
                                    and remaining
                                    <= (all_unspoken + 1) * (minimum_gap + 5)
                                )
                                bot = random.choice(
                                    unspoken if tight else (pinged or unspoken or ready)
                                )
                                room.next_ai_at[bot.pid] = now + random.uniform(34, 50)
                                gap = random.uniform(minimum_gap, minimum_gap + 4)
                                if tight:
                                    gap = min(
                                        gap, max(3.0, remaining / (all_unspoken + 1))
                                    )
                                room.next_ai_message_at = now + gap
                                self._schedule_bot_speech(room, bot)
                    elif room.phase == "day_decision" and (
                        now >= room.deadline
                        or len(room.decision_votes) >= len(room.alive())
                    ):
                        self._finish_day_decision(room)
                        self._enter_vote(room)
                        changed = True
                    elif room.phase == "night_vote" and (
                        now >= room.deadline
                        or (
                            len(room.votes) >= len(room.alive())
                            and not room.night_judging
                        )
                    ):
                        await self._finish_vote(room)
                        changed = True
                    elif room.phase == "defense":
                        if (
                            room.bot_defense_at
                            and not room.defense_text
                            and now >= room.bot_defense_at
                        ):
                            await self._bot_defend(room)
                            changed = True
                        if room.phase == "defense" and time.time() >= room.deadline:
                            self._enter_verdict(room)
                            changed = True
                    elif room.phase == "verdict" and (
                        now >= room.deadline
                        or (
                            len(room.verdicts) >= max(0, len(room.alive()) - 1)
                            and not room.verdict_judging
                        )
                    ):
                        self._finish_verdict(room)
                        changed = True
                    elif room.phase == "result" and now >= room.deadline:
                        self._advance_day(room)
                        changed = True
                    if changed:
                        self._persist(room)
                if changed:
                    await self.broadcast(room)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _awaits_reply(room: Room, participant: Participant) -> bool:
        # 내 마지막 발언 이후 나를 @지목한 발언이 있으면 응답 차례를 우선 배정한다.
        for message in reversed(room.current_day_messages()):
            if message.kind != "chat":
                continue
            if message.sender_pid == participant.pid:
                return False
            if f"@{participant.alias}" in message.text:
                return True
        return False

    def _schedule_bot_speech(self, room: Room, bot: Participant) -> None:
        room.pending_ai_pids.add(bot.pid)
        task = asyncio.create_task(
            self._generate_bot_speech(room, bot, room.day),
            name=f"bot-{room.code}-{bot.pid}-day-{room.day}",
        )
        room.ai_tasks.add(task)
        task.add_done_callback(room.ai_tasks.discard)

    async def _generate_bot_speech(self, room: Room, bot: Participant, day: int) -> None:
        try:
            async with room.lock:
                if room.phase != "discussion" or room.day != day or not bot.alive:
                    return
                roster = [participant.alias for participant in room.alive()]
                random.shuffle(roster)
                story = room.story_view()
                messages = [message.public() for message in room.current_day_messages()]
                opening = not any(message["kind"] == "chat" for message in messages)
                stances = {
                    participant.alias: participant.day_stance
                    for participant in room.alive()
                    if participant.day_stance is not None
                }
                history_texts = [
                    message.text for message in room.messages if message.kind == "chat"
                ]
                remaining = max(0.0, room.deadline - time.time())
            # 마감 임박 시 생성 시간을 줄여 폴백이라도 마감 전에 도착시킨다.
            speech_timeout = 22 if remaining > 30 else max(5.0, remaining - 4)
            try:
                text = await asyncio.wait_for(
                    self.narrator.speak(
                        bot_name=bot.alias,
                        persona_name=bot.persona["name"],
                        persona_style=bot.persona["style"],
                        task=story["task"],
                        context=story["context"],
                        hint=room.story.machine_hint,
                        day=day,
                        roster=roster,
                        messages=messages,
                        choice_signals=[choice.signal for choice in room.story.choices],
                        choice_labels=[choice.label for choice in room.story.choices],
                        stances=stances,
                        history_texts=history_texts,
                        opening=opening,
                    ),
                    timeout=speech_timeout,
                )
            except Exception:
                text = LocalNarrator._task_fallback(story["task"], messages)
            async with room.lock:
                # 오늘 첫 발언이면 결론 투표 중이라도 도착을 허용해 하루 1회 발언을 지킨다.
                landable = room.phase == "discussion" or (
                    room.phase == "day_decision" and room.day not in bot.spoken_days
                )
                if room.day != day or not landable or not bot.alive:
                    return
                # Several model calls can finish from the same earlier transcript.
                # Compare with messages that actually landed before publishing.
                current_chat = [
                    message.public()
                    for message in room.current_day_messages()
                    if message.kind == "chat"
                ]
                def plain(value: str) -> str:
                    return re.sub(
                        r"\s+", " ", re.sub(r"@[A-Za-z0-9_-]+,?\s*", "", value)
                    ).strip()
                if any(plain(text) == plain(message["text"]) for message in current_chat):
                    side = bot.day_stance
                    if side is None:
                        side = LocalNarrator._stance_index(
                            text, [choice.signal for choice in room.story.choices], story["task"]
                        )
                    text = (
                        LocalNarrator._stance_statement(story["task"], side, current_chat)
                        if side is not None
                        else LocalNarrator._task_fallback(story["task"], current_chat)
                    )
                self._append_chat(room, bot, text)
                if room.phase == "day_decision":
                    room.decision_votes[bot.pid] = self._bot_decision(room, bot).key
                self._persist(room)
            await self.broadcast(room)
        finally:
            room.pending_ai_pids.discard(bot.pid)

    def _append_chat(self, room: Room, sender: Participant, text: str) -> None:
        sender.last_message_at = time.time()
        matched = self._match_choice(room.story.choices, text)
        if matched is not None:
            sender.day_stance = room.story.choices.index(matched)
        room.event_id += 1
        room.messages.append(
            ChatMessage(
                room.event_id, sender.pid, sender.alias, text.strip()[:240], "chat", time.time()
            )
        )
        room.messages = room.messages[-120:]
        human_markers = len(re.findall(r"느낌|사람|살려|불쌍|가족|아이|마음|슬프|믿어", text))
        uncertainty = len(re.findall(r"아마|그냥|왠지|모르겠|것 같", text))
        machine_markers = len(
            re.findall(r"효율|복구|수치|검증|로그|확률|비용|조건|생산|전력|손실|자원", text)
        )
        # 중앙 지침에 없는 목적 선언·언어 규범 위반은 날짜와 무관하게 강하게 의심된다.
        # (되묻는 의문문·메타 언급은 protocol_violations 가 위반으로 세지 않는다.)
        overacting, anomalous = protocol_violations(text)
        if anomalous:
            room.add_system(
                f"{sender.alias}의 신호에서 검증실 언어 규범을 벗어난 패턴이 "
                "감지됐습니다. 해당 개체의 검증 우선순위를 상향합니다.",
                "warning",
            )
        difficulty = (0.15, 0.3, 0.5, 0.72, 0.95, 1.18, 1.4)[room.day - 1]
        relief = (0.22, 0.18, 0.15, 0.12, 0.09, 0.06, 0.04)[room.day - 1]
        sender.spoken_days.add(room.day)
        if not sender.is_bot:
            sender.messages_sent += 1
            if machine_markers >= 2 and room.day not in sender.machine_days:
                sender.machine_days.add(room.day)
                sender.award("machine", 50)
        for bot in (p for p in room.alive() if p.is_bot and p.pid != sender.pid):
            scores = room.suspicion.setdefault(bot.pid, {})
            scores[sender.pid] = max(
                0,
                scores.get(sender.pid, 0)
                + (human_markers * 0.45 + uncertainty * 0.2) * difficulty
                + min(overacting, 2) * 0.8
                + (0.8 if anomalous else 0)
                - min(machine_markers, 3) * relief,
            )
        if not sender.is_bot:
            why = []
            if human_markers:
                why.append(f"감정·인간 어휘 {human_markers}회")
            if uncertainty:
                why.append(f"불확실 표현 {uncertainty}회")
            if overacting:
                why.append("지침 밖 목적 선언")
            if anomalous:
                why.append("언어 규범 위반")
            delta = (
                (human_markers * 0.45 + uncertainty * 0.2) * difficulty
                + min(overacting, 2) * 0.8
                + (0.8 if anomalous else 0)
                - min(machine_markers, 3) * relief
            )
            if why and delta > 0.05:
                sender.suspicion_log.append(
                    {
                        "day": room.day,
                        "kind": "speech",
                        "detail": text[:70],
                        "why": ", ".join(why),
                        "delta": round(delta, 2),
                    }
                )
        # Technical terms such as "오류 범위" are not accusations by themselves.
        reasoned_accusation = self._is_reasoned_accusation(text)
        explicit_accusation = re.search(
            r"(?:인간|인간적)\s*(?:같|특유|흔적|가능성)|"
            r"(?:너|당신|개체).{0,12}(?:의심|수상|거짓|지목)",
            text,
        )
        if reasoned_accusation or explicit_accusation:
            for target in room.alive():
                if target.pid == sender.pid or target.alias.lower() not in text.lower():
                    continue
                corroborators = {
                    message.sender_pid
                    for message in room.messages
                    if message.kind == "chat"
                    and message.sender_pid
                    and target.alias.lower() in message.text.lower()
                    and self._is_reasoned_accusation(message.text)
                }
                corroboration_count = len(corroborators)
                for bot in (p for p in room.alive() if p.is_bot and p.pid != target.pid):
                    scores = room.suspicion.setdefault(bot.pid, {})
                    if reasoned_accusation and corroboration_count >= 3:
                        scores[target.pid] = max(scores.get(target.pid, 0), 0.75)
                        continue
                    if bot.pid == sender.pid:
                        pressure = random.uniform(0.35, 0.5)
                    elif reasoned_accusation and corroboration_count == 2:
                        pressure = random.uniform(0.1, 0.18)
                    else:
                        pressure = random.uniform(0.02, 0.08)
                    scores[target.pid] = scores.get(target.pid, 0) + pressure

    @staticmethod
    def _is_reasoned_accusation(text: str) -> bool:
        evaluation = re.search(
            r"(?:주장|말|발언|결론|근거|논리|계산|수치|기준|선택|단정|인과).{0,24}"
            r"(?:모순|불일치|잘못|착각|성립하지|오류(?:다|이|인|로))",
            text,
        )
        direct = re.search(
            r"(?:모순|불일치|잘못|착각|성립하지|논리\s*오류).{0,16}"
            r"(?:주장|말|발언|결론|근거|논리|계산|수치|기준|선택|단정)",
            text,
        )
        return bool(evaluation or direct)

    async def chat(self, room: Room, user_id: int, text: str) -> None:
        async with room.lock:
            p = room.human(user_id)
            if room.phase != "discussion" or not p or not p.alive:
                raise ValueError("지금은 발언할 수 없습니다.")
            clean = " ".join(text.split())[:240]
            if not clean:
                raise ValueError("발언을 입력하세요.")
            if time.time() - p.last_message_at < 1.5:
                raise ValueError("잠시 후 다시 발언하세요.")
            self._append_chat(room, p, clean)
            self._persist(room)
        await self.broadcast(room)

    def _enter_vote(self, room: Room) -> None:
        room.phase = "night_vote"
        room.deadline = time.time() + self.settings.vote_seconds
        room.votes.clear()
        silent_humans = [
            participant
            for participant in room.alive()
            if not participant.is_bot and room.day not in participant.spoken_days
        ]
        room.silent_pids = {participant.pid for participant in silent_humans}
        if silent_humans:
            aliases = ", ".join(participant.alias for participant in silent_humans)
            room.add_system(f"무응답 신호 탐지: {aliases}. 검증 우선순위를 상향합니다.", "warning")
            for bot in (participant for participant in room.alive() if participant.is_bot):
                scores = room.suspicion.setdefault(bot.pid, {})
                for silent in silent_humans:
                    scores[silent.pid] = scores.get(silent.pid, 0) + 1.0
            for silent in silent_humans:
                silent.suspicion_log.append(
                    {
                        "day": room.day,
                        "kind": "silent",
                        "detail": "무응답 신호로 검증 우선순위 상향",
                        "delta": 1.0,
                    }
                )
        room.add_system("밤이 되었습니다. 가장 인간 같은 개체를 지목하십시오.", "night")
        provisional_tally: dict[str, int] = {}
        for bot in (p for p in room.alive() if p.is_bot):
            candidates = [p for p in room.alive() if p.pid != bot.pid]
            scores = room.suspicion.get(bot.pid, {})
            noise = max(0.18, 0.65 - room.day * 0.07)
            early_human_discount = (0.22, 0.1, 0, 0, 0, 0, 0)[room.day - 1]
            idle_candidates = [candidate for candidate in silent_humans if candidate.pid != bot.pid]
            if idle_candidates:
                target = max(idle_candidates, key=lambda p: scores.get(p.pid, 0))
            else:
                # 뚜렷한 의심(0.35 이상)에는 몰표 방지를 풀어 표가 실제 신호에 뭉치게 한다.
                target = max(
                    candidates,
                    key=lambda p: scores.get(p.pid, 0)
                    + random.uniform(0, noise)
                    - (
                        early_human_discount
                        if not p.is_bot and scores.get(p.pid, 0) < 0.35
                        else 0
                    )
                    - (
                        0
                        if scores.get(p.pid, 0) >= 0.35
                        else provisional_tally.get(p.pid, 0) * 0.3
                    ),
                )
            room.votes[bot.pid] = target.pid
            provisional_tally[target.pid] = provisional_tally.get(target.pid, 0) + 1
        # 점수 기반 표는 잠정치. 각 AI의 LLM 판단이 도착하는 대로 표를 덮어쓴다.
        room.vote_reasons = []
        if hasattr(self.narrator, "judge_vote"):
            room.night_judging = True
            task = asyncio.create_task(
                self._llm_night_votes(room, room.day),
                name=f"judge-{room.code}-day-{room.day}",
            )
            room.ai_tasks.add(task)
            task.add_done_callback(room.ai_tasks.discard)

    async def _llm_night_votes(self, room: Room, day: int) -> None:
        try:
            async with room.lock:
                if room.phase != "night_vote" or room.day != day:
                    return
                bots = [p for p in room.alive() if p.is_bot]
                roster = [p.alias for p in room.alive()]
                alias_to_pid = {p.alias: p.pid for p in room.alive()}
                messages = [m.public() for m in room.current_day_messages()]
                silent_aliases = [
                    room.participants[pid].alias
                    for pid in room.silent_pids
                    if pid in room.participants
                ]
                story = room.story_view()
                note_map = {}
                for bot in bots:
                    scores = room.suspicion.get(bot.pid, {})
                    ranked = sorted(
                        (
                            (room.participants[pid].alias, value)
                            for pid, value in scores.items()
                            if pid in room.participants and value > 0.2
                        ),
                        key=lambda item: -item[1],
                    )[:3]
                    note_map[bot.pid] = [
                        f"{alias}: {value:.2f}" for alias, value in ranked
                    ]

            async def one(bot):
                try:
                    return await asyncio.wait_for(
                        self.narrator.judge_vote(
                            bot_name=bot.alias,
                            persona_name=bot.persona["name"],
                            persona_style=bot.persona["style"],
                            day=day,
                            roster=roster,
                            messages=messages,
                            silent_aliases=silent_aliases,
                            suspicion_notes=note_map.get(bot.pid, []),
                            task=story["task"],
                            context=story["context"],
                        ),
                        timeout=22,
                    )
                except Exception:
                    return None

            results = await asyncio.gather(*(one(bot) for bot in bots))
            async with room.lock:
                if room.phase != "night_vote" or room.day != day:
                    return
                for bot, result in zip(bots, results, strict=True):
                    if not result:
                        continue
                    target_pid = alias_to_pid.get(result["target"])
                    if not target_pid or target_pid == bot.pid:
                        continue
                    target = room.participants.get(target_pid)
                    if not target or not target.alive:
                        continue
                    room.votes[bot.pid] = target_pid
                    room.vote_reasons.append(
                        {
                            "judge": bot.alias,
                            "target": result["target"],
                            "reason": result["reason"],
                            "day": day,
                        }
                    )
                self._persist(room)
            await self.broadcast(room)
        finally:
            room.night_judging = False

    def _enter_day_decision(self, room: Room) -> None:
        room.phase = "day_decision"
        room.deadline = time.time() + self.settings.decision_seconds
        room.decision_votes.clear()
        labels = " vs ".join(choice.label for choice in room.story.choices)
        room.add_system(
            f"토론이 종료됐습니다. 오늘의 결론을 투표로 결정하십시오. ({labels})",
            "decision_call",
        )
        for bot in (p for p in room.alive() if p.is_bot):
            room.decision_votes[bot.pid] = self._bot_decision(room, bot).key

    @staticmethod
    def _match_choice(choices, text: str):
        matches = [
            (match.start(), index)
            for index, choice in enumerate(choices)
            for match in re.finditer(choice.signal, text, re.I)
        ]
        # "X로 변경한다"처럼 시나리오별 signal 동사에 없는 전환 표현도 스탠스로 기록한다.
        for index, choice in enumerate(choices):
            for token in {choice.label, choice.label.split()[-1]}:
                pattern = re.escape(token) + r"(?:로|으로)?\s*(?:변경|전환|바꾼|바꾸)"
                matches.extend(
                    (match.start(), index) for match in re.finditer(pattern, text, re.I)
                )
        return choices[max(matches)[1]] if matches else None

    def _bot_decision(self, room: Room, bot: Participant):
        choices = room.story.choices
        if bot.day_stance is not None:
            return choices[bot.day_stance]
        return random.choice(choices)

    async def decide(self, room: Room, user_id: int, choice_key: str) -> None:
        async with room.lock:
            voter = room.human(user_id)
            if room.phase != "day_decision" or not voter or not voter.alive:
                raise ValueError("지금은 결론 투표를 할 수 없습니다.")
            if choice_key not in {choice.key for choice in room.story.choices}:
                raise ValueError("존재하지 않는 선택지입니다.")
            room.decision_votes[voter.pid] = choice_key
        await self.broadcast(room)

    def _finish_day_decision(self, room: Room) -> None:
        choices = room.story.choices
        scores = {choice.key: 0 for choice in choices}
        for choice_key in room.decision_votes.values():
            scores[choice_key] += 1
        top = max(scores.values())
        leaders = [choice for choice in choices if scores[choice.key] == top]
        selected = random.choice(leaders)
        room.decisions[room.day] = selected.key
        counts = ", ".join(f"{choice.label} {scores[choice.key]}표" for choice in choices)
        tie_note = " 동률이라 무작위로 결정됐습니다." if len(leaders) > 1 else ""
        room.add_system(
            f"결론 확정: {selected.label}. ({counts}){tie_note} 다음 DAY의 상황에 반영됩니다.",
            "decision",
        )

    async def vote(self, room: Room, user_id: int, target_pid: str) -> None:
        async with room.lock:
            voter = room.human(user_id)
            target = room.participants.get(target_pid)
            if room.phase != "night_vote" or not voter or not voter.alive:
                raise ValueError("지금은 지목할 수 없습니다.")
            if not target or not target.alive or target.pid == voter.pid:
                raise ValueError("해당 개체를 지목할 수 없습니다.")
            room.votes[voter.pid] = target.pid
        await self.broadcast(room)

    async def _finish_vote(self, room: Room) -> None:
        if not room.votes:
            silent = [
                participant for participant in room.alive() if participant.pid in room.silent_pids
            ]
            if silent:
                accused = random.choice(silent)
                room.accused_pid = accused.pid
                self._archive_vote_reasons(room, accused, 0)
                room.phase = "defense"
                room.deadline = time.time() + self.settings.defense_seconds
                room.bot_defense_at = 0
                room.add_system(
                    f"{accused.alias}, 무응답 신호에 대한 최후의 변론을 시작하십시오.",
                    "accuse",
                )
                return
            room.phase = "result"
            room.last_result = {"title": "지목 무효", "text": "유효한 지목이 없습니다."}
            room.deadline = time.time() + self.settings.result_seconds
            return
        tally: dict[str, int] = {}
        for target in room.votes.values():
            tally[target] = tally.get(target, 0) + 1
        top = max(tally.values())
        tied = [pid for pid, count in tally.items() if count == top]
        room.accused_pid = random.choice(tied)
        accused = room.participants[room.accused_pid]
        self._archive_vote_reasons(room, accused, top)
        room.phase = "defense"
        room.deadline = time.time() + self.settings.defense_seconds
        room.bot_defense_at = time.time() + random.uniform(10, 13) if accused.is_bot else 0
        tie_note = " 동률 후보 중 무작위 검증 대상으로 선정됐습니다." if len(tied) > 1 else ""
        room.add_system(
            f"{accused.alias}, 최후의 변론을 시작하십시오. 지목 {top}표.{tie_note}",
            "accuse",
        )

    @staticmethod
    def _archive_vote_reasons(room: Room, accused: Participant, votes: int) -> None:
        if accused.is_bot:
            return
        reasons = [
            f"{item['judge']}: {item['reason']}"
            for item in room.vote_reasons
            if item["target"] == accused.alias and item["reason"]
        ]
        accused.suspicion_log.append(
            {
                "day": room.day,
                "kind": "night",
                "detail": " / ".join(reasons[:3]) or f"지목 {votes}표",
                "delta": 0,
                "votes": votes,
            }
        )

    async def _bot_defend(self, room: Room) -> None:
        accused = room.participants.get(room.accused_pid or "")
        if not accused or not accused.is_bot or room.defense_text:
            room.bot_defense_at = 0
            return
        own_statement = next(
            (
                message.text
                for message in reversed(room.current_day_messages())
                if message.kind == "chat" and message.sender_pid == accused.pid
            ),
            "",
        )
        stance_label = (
            room.story.choices[accused.day_stance].label
            if accused.day_stance is not None
            else None
        )
        room.defense_text = self._defense_from_statement(own_statement, stance_label)
        room.bot_defense_at = 0
        room.add_system(f"{accused.alias}: {room.defense_text}", "defense")
        room.deadline = min(room.deadline, time.time() + 8)

    @staticmethod
    def _defense_from_statement(statement: str, stance_label: str | None = None) -> str:
        clean = re.sub(r"^@[A-Za-z0-9_-]+,?\s*", "", statement).strip()[:150]
        if not clean:
            return (
                "나는 오늘 발언 슬롯을 배정받지 못해 판단 기록이 없다. "
                "침묵은 판단 회피가 아니며, 무발언만으로 인간이라 판정할 수는 없다."
            )
        if "?" in clean or clean.endswith(("가", "까")):
            question_part = clean[: clean.rfind("?") + 1] if "?" in clean else clean
            if stance_label:
                return (
                    f"내 결론은 처음부터 {stance_label} 하나였다. "
                    f"'{question_part}'라고 물은 것은 상대 기준을 검증하는 절차였고, "
                    "절차 요구는 인간 판정의 근거가 될 수 없다."
                )[:300]
            return (
                f"내 발언은 결론을 단정하지 않고 검증하려는 질문이었다. "
                f"'{question_part}'라고 물어 확인을 요구한 것이 "
                "인간 판정의 근거가 될 수는 없다."
            )[:300]
        return (
            f"내 판단 기록은 바뀌지 않았다. {clean} "
            "이는 감정이 아니라 오늘 안건의 조건을 따른 결론이다."
        )[:300]

    async def defend(self, room: Room, user_id: int, text: str) -> None:
        async with room.lock:
            p = room.human(user_id)
            if room.phase != "defense" or not p or p.pid != room.accused_pid:
                raise ValueError("당신의 변론 차례가 아닙니다.")
            room.defense_text = " ".join(text.split())[:360]
            if not room.defense_text:
                raise ValueError("변론을 입력하세요.")
            room.add_system(f"{p.alias}: {room.defense_text}", "defense")
            room.bot_defense_at = 0
            room.deadline = min(room.deadline, time.time() + 6)
        await self.broadcast(room)

    def _enter_verdict(self, room: Room) -> None:
        room.phase = "verdict"
        room.deadline = time.time() + self.settings.verdict_seconds
        room.bot_defense_at = 0
        room.verdicts.clear()
        accused = room.participants.get(room.accused_pid or "")
        room.add_system(
            f"{accused.alias if accused else '피고 개체'}의 폐기에 찬성합니까?", "verdict"
        )
        # 오늘 발언 기록이 없는 개체는 변론 보너스가 없고 더 엄하게 판정된다.
        silent_accused = bool(accused and room.day not in accused.spoken_days)
        for bot in (p for p in room.alive() if p.is_bot and p.pid != room.accused_pid):
            score = room.suspicion.get(bot.pid, {}).get(room.accused_pid or "", 0)
            defense_bonus = (
                -0.35
                if not silent_accused
                and any(word in room.defense_text for word in ("수치", "검증", "로그", "확률"))
                else 0
            )
            threshold = (0.92, 0.84, 0.78, 0.74, 0.7, 0.68, 0.66)[room.day - 1]
            if silent_accused:
                threshold -= 0.2
            room.verdicts[bot.pid] = score + defense_bonus + random.uniform(0, 1.1) > threshold
        if accused and accused.pid in room.silent_pids and not room.defense_text:
            for bot in (p for p in room.alive() if p.is_bot and p.pid != accused.pid):
                room.verdicts[bot.pid] = True
        # 점수 기반 판결은 잠정치. 각 AI의 LLM 판단이 도착하는 대로 덮어쓴다.
        if accused and hasattr(self.narrator, "judge_verdict"):
            room.verdict_judging = True
            task = asyncio.create_task(
                self._llm_verdicts(room, room.day, accused.pid),
                name=f"verdict-{room.code}-day-{room.day}",
            )
            room.ai_tasks.add(task)
            task.add_done_callback(room.ai_tasks.discard)

    async def _llm_verdicts(self, room: Room, day: int, accused_pid: str) -> None:
        try:
            async with room.lock:
                if room.phase != "verdict" or room.day != day:
                    return
                accused = room.participants.get(accused_pid)
                if not accused:
                    return
                bots = [
                    p for p in room.alive() if p.is_bot and p.pid != accused_pid
                ]
                messages = [m.public() for m in room.current_day_messages()]
                defense_text = room.defense_text
                story = room.story_view()
                notes = {
                    bot.pid: (
                        f"{accused.alias}: "
                        f"{room.suspicion.get(bot.pid, {}).get(accused_pid, 0):.2f}"
                    )
                    for bot in bots
                }
                accused_alias = accused.alias

            async def one(bot):
                try:
                    return await asyncio.wait_for(
                        self.narrator.judge_verdict(
                            bot_name=bot.alias,
                            persona_name=bot.persona["name"],
                            day=day,
                            accused_alias=accused_alias,
                            defense_text=defense_text,
                            messages=messages,
                            suspicion_note=notes.get(bot.pid, ""),
                            task=story["task"],
                            context=story["context"],
                        ),
                        timeout=18,
                    )
                except Exception:
                    return None

            results = await asyncio.gather(*(one(bot) for bot in bots))
            async with room.lock:
                if room.phase != "verdict" or room.day != day:
                    return
                for bot, result in zip(bots, results, strict=True):
                    if result is not None and bot.alive:
                        room.verdicts[bot.pid] = result
                self._persist(room)
            await self.broadcast(room)
        finally:
            room.verdict_judging = False

    async def verdict(self, room: Room, user_id: int, approve: bool) -> None:
        async with room.lock:
            p = room.human(user_id)
            if room.phase != "verdict" or not p or not p.alive or p.pid == room.accused_pid:
                raise ValueError("지금은 판결에 참여할 수 없습니다.")
            room.verdicts[p.pid] = approve
        await self.broadcast(room)

    def _finish_verdict(self, room: Room) -> None:
        public_verdicts = [
            {
                "pid": voter.pid,
                "alias": voter.alias,
                "approve": room.verdicts[voter.pid],
            }
            for voter in room.participants.values()
            if voter.pid in room.verdicts
        ]
        approvals = sum(item["approve"] for item in public_verdicts)
        rejections = len(public_verdicts) - approvals
        accused = room.participants.get(room.accused_pid or "")
        expelled = bool(accused and approvals > rejections)
        self._apply_verdict_suspicion(room, public_verdicts)
        if expelled and accused:
            accused.alive = False
            accused.revealed = "AI" if accused.is_bot else "HUMAN"
            if not accused.is_bot:
                accused.death_info = {
                    "day": room.day,
                    "approvals": approvals,
                    "rejections": rejections,
                }
            if accused.is_bot:
                for human in (p for p in room.participants.values() if not p.is_bot and p.alive):
                    if room.votes.get(human.pid) == accused.pid:
                        human.ai_expelled += 1
                        human.award("deduction", 300)
        elif accused and not accused.is_bot and room.defense_text:
            accused.award("defense", 200)
        room.phase = "result"
        room.deadline = time.time() + self.settings.result_seconds
        if expelled and accused:
            label = "AI 개체" if accused.is_bot else "인간 생존자"
            room.last_result = {
                "title": f"{accused.alias} 폐기 완료",
                "text": f"정체는 {label}였습니다. 찬성 {approvals}, 반대 {rejections}.",
                "expelled": True,
                "pid": accused.pid,
                "verdicts": public_verdicts,
            }
        else:
            room.last_result = {
                "title": "폐기 부결",
                "text": f"찬성 {approvals}, 반대 {rejections}. 해당 개체는 생존합니다.",
                "expelled": False,
                "pid": room.accused_pid,
                "verdicts": public_verdicts,
            }
        room.add_system(room.last_result["text"], "result")
        approvals_by = ", ".join(
            item["alias"] for item in public_verdicts if item["approve"]
        ) or "없음"
        rejections_by = ", ".join(
            item["alias"] for item in public_verdicts if not item["approve"]
        ) or "없음"
        room.add_system(
            f"판결 기록: 찬성 — {approvals_by} / 반대 — {rejections_by}",
            "verdict_record",
        )
        if expelled and accused and not accused.is_bot:
            self._record_result(room, accused, cleared=False)
        if expelled and accused and accused.is_bot and not any(
            participant.is_bot for participant in room.alive()
        ):
            self._end_with_human_victory(room)

    def _end_with_human_victory(self, room: Room) -> None:
        room.phase = "ended"
        room.deadline = 0
        for human in (p for p in room.alive() if not p.is_bot):
            if room.day not in human.scored_days:
                human.scored_days.add(room.day)
                human.award("survival", 150)
            human.award("clear", 1000)
            self._record_result(room, human, cleared=True)
        room.add_system(
            "모든 AI 개체가 폐기됐습니다. 인간 생존자들이 검증실을 장악했습니다. "
            f"{room.scenario.epilogue}".rstrip(),
            "ending",
        )

    @staticmethod
    def _apply_verdict_suspicion(room: Room, verdicts: list[dict]) -> None:
        approvals = [item for item in verdicts if item["approve"]]
        rejections = [item for item in verdicts if not item["approve"]]
        if not approvals or not rejections or len(approvals) == len(rejections):
            return
        minority = approvals if len(approvals) < len(rejections) else rejections
        majority = rejections if minority is approvals else approvals
        if len(minority) > 2 or len(majority) < 4:
            return
        for item in minority:
            for bot in (p for p in room.alive() if p.is_bot and p.pid != item["pid"]):
                scores = room.suspicion.setdefault(bot.pid, {})
                scores[item["pid"]] = scores.get(item["pid"], 0) + 0.28

    def _advance_day(self, room: Room) -> None:
        living_humans = [p for p in room.alive() if not p.is_bot]
        if not living_humans:
            room.phase = "ended"
            room.deadline = 0
            room.add_system("인간 신호가 모두 소멸했습니다. 중앙 통제가 승리했습니다.", "ending")
            return
        for human in living_humans:
            if room.day not in human.scored_days:
                human.scored_days.add(room.day)
                human.award("survival", 150)
        if room.day >= 7:
            room.phase = "ended"
            room.deadline = 0
            for human in living_humans:
                human.award("clear", 1000)
                self._record_result(room, human, cleared=True)
            final_choice = next(
                (
                    choice
                    for choice in room.story.choices
                    if choice.key == room.decisions.get(room.day)
                ),
                None,
            )
            ending = (
                final_choice.consequence
                if final_choice
                else "생존자의 신호가 통제망을 벗어났습니다."
            )
            room.add_system(
                f"7일간의 검증이 종료됐습니다. {ending} {room.scenario.epilogue}".rstrip(),
                "ending",
            )
            return
        room.day += 1
        self._enter_discussion(room)

    def _record_result(self, room: Room, participant: Participant, *, cleared: bool) -> None:
        if participant.user_id is None:
            return
        participant.score = max(0, sum(participant.score_breakdown.values()))
        self.store.record_result(
            room_code=room.code,
            user_id=participant.user_id,
            scenario_id=room.scenario_id,
            mode=room.mode,
            party_size=sum(not member.is_bot for member in room.participants.values()),
            score=participant.score,
            day_reached=room.day,
            cleared=cleared,
            duration_seconds=max(0, int(time.time() - room.started_at)),
            ai_expelled=participant.ai_expelled,
            messages_sent=participant.messages_sent,
            skills_used=0,
            breakdown=participant.score_breakdown,
        )

    def _persist(self, room: Room) -> None:
        payload = {
            "code": room.code,
            "scenario": room.scenario_id,
            "phase": room.phase,
            "day": room.day,
            "participants": [
                {"alias": p.alias, "alive": p.alive, "revealed": p.revealed}
                for p in room.participants.values()
            ],
            "updated_at": time.time(),
        }
        self.store.save_room(room.code, payload)

    def subscribe(self, code: str, listener: Broadcast) -> None:
        self.listeners.setdefault(code, set()).add(listener)

    def unsubscribe(self, code: str, listener: Broadcast) -> None:
        self.listeners.get(code, set()).discard(listener)

    async def broadcast(self, room: Room) -> None:
        for listener in list(self.listeners.get(room.code, set())):
            try:
                await listener(room.code)
            except Exception:
                self.listeners.get(room.code, set()).discard(listener)
