"""Host-owned morning briefing for JARVIS.

Everything spoken here is deterministic and composed on the host: the language
model may ask for a briefing, but never invents its content. Data comes from
the read-only Cannanas zone summary, live Home Assistant temperatures and an
owner-only speaker file that holds each person's title and their own todos.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_SPOKEN_CHARS = 900
MAX_TODOS = 3
MAX_ZONES = 4
DEFAULT_TITLE = "Gärtner"

STAGE_LABELS = {
    "FLOWERING": "Blüte",
    "VEGETATIVE": "Wachstum",
    "HARVEST": "Ernte",
    "GERMINATION": "Keimung",
    "SEEDLING": "Sämling",
    "SEED": "Saat",
}


def greeting_for(moment: datetime) -> str:
    """German time-of-day greeting for one local moment."""
    hour = moment.hour
    if 5 <= hour < 11:
        return "Guten Morgen"
    if 11 <= hour < 17:
        return "Hallo"
    return "Guten Abend"


def stage_label(status: str) -> str:
    key = str(status or "").strip().upper()
    return STAGE_LABELS.get(key, key.title() or "Status")


def _german_decimal(value: float) -> str:
    return f"{float(value):.1f}".replace(".", ",")


def _parse_created(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _day_count(created: datetime | None, now: datetime) -> int:
    if created is None:
        return 0
    return max(0, (now.date() - created.date()).days)


def _diary_age(created: datetime | None, now: datetime) -> str:
    if created is None:
        return ""
    days = _day_count(created, now)
    if days <= 0:
        return "heute"
    if days == 1:
        return "gestern"
    return f"vor {days} Tagen"


def _active_stage(entry: dict) -> dict | None:
    stages = [stage for stage in entry.get("stages") or []
              if isinstance(stage, dict) and stage.get("oldest_created_at")]
    if not stages:
        return None

    def key(stage: dict) -> tuple[int, int]:
        status = str(stage.get("status") or "").upper()
        # Open cultivation stages win over finished ones, then the oldest run.
        rank = {"FLOWERING": 0, "VEGETATIVE": 1, "SEEDLING": 2,
                "GERMINATION": 3, "HARVEST": 9}.get(status, 5)
        return rank, -int(stage.get("count") or 0)

    return sorted(stages, key=key)[0]


def _zone_sentence(entry: dict, temperature, now: datetime) -> str:
    name = " ".join(str(entry.get("name") or "").split()) or "Raum"
    parts: list[str] = []
    stage = _active_stage(entry)
    if stage is not None:
        days = _day_count(_parse_created(stage.get("oldest_created_at")), now)
        parts.append(f"{stage_label(stage.get('status'))} Tag {days}")
    elif not entry.get("batch_count"):
        parts.append("keine offene Charge")
    else:
        parts.append(f"{int(entry.get('batch_count') or 0)} Chargen")
    plants = int(entry.get("plant_count") or 0)
    if plants:
        parts.append(f"{plants} Pflanzen")
    if temperature is not None:
        parts.append(f"{_german_decimal(temperature)} Grad")
    sentence = f"{name}: " + ", ".join(parts) + "."
    diary = entry.get("last_diary")
    if isinstance(diary, dict) and diary.get("text"):
        age = _diary_age(_parse_created(diary.get("created_at")), now)
        text = str(diary["text"]).strip().rstrip(".")
        prefix = f"Letzter Eintrag {age}" if age else "Letzter Eintrag"
        sentence += f" {prefix}: {text}."
    return sentence


def compose_briefing(
    *,
    speaker: str,
    title: str = DEFAULT_TITLE,
    todos=(),
    zones=(),
    temperatures: dict | None = None,
    now: datetime | None = None,
    max_chars: int = MAX_SPOKEN_CHARS,
) -> str:
    """One spoken briefing: greeting, own todos, then a line per room."""
    moment = now or datetime.now(timezone.utc)
    temperatures = dict(temperatures or {})
    address = (title or speaker or DEFAULT_TITLE).strip()
    if speaker and address.lower().startswith("gärtner"):
        # The Gärtner roll call uses the name; a title like Head of Grow stands alone.
        address = f"{address} {speaker}".strip()
    sentences = [f"{greeting_for(moment)}, {address}."]

    listed = [str(item).strip() for item in todos if str(item).strip()]
    if listed:
        spoken_todos = listed[:MAX_TODOS]
        if len(spoken_todos) == 1:
            joined = spoken_todos[0]
        else:
            joined = ", ".join(spoken_todos[:-1]) + " und " + spoken_todos[-1]
        sentences.append(f"Auf dem Zettel: {joined}.")

    entries = [entry for entry in zones if isinstance(entry, dict)]
    for entry in entries[:MAX_ZONES]:
        name = " ".join(str(entry.get("name") or "").split())
        sentences.append(_zone_sentence(entry, temperatures.get(name), moment))
    if len(entries) > MAX_ZONES:
        sentences.append(f"Und {len(entries) - MAX_ZONES} weitere Räume.")

    spoken = " ".join(sentences)
    if len(spoken) <= max_chars:
        return spoken
    # Keep every complete sentence that fits; never cut mid-word.
    kept: list[str] = []
    total = 0
    for sentence in sentences:
        if total + len(sentence) + 1 > max_chars and kept:
            break
        kept.append(sentence)
        total += len(sentence) + 1
    return " ".join(kept)


@dataclass(frozen=True)
class Speaker:
    key: str
    name: str
    title: str = DEFAULT_TITLE
    todos: tuple[str, ...] = field(default=())


class SpeakerBook:
    """Owner-only speaker file: title plus each person's own todos."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._speakers: dict[str, Speaker] = {}
        self._default = ""
        self._load()

    def _load(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if self.path.is_symlink():
            raise PermissionError("speaker file must not be a symlink")
        if info.st_mode & 0o077:
            raise PermissionError("speaker file must be owner-only (0600)")
        if info.st_uid != os.getuid():
            raise PermissionError("speaker file has a foreign owner")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        raw = payload.get("speakers")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                todos = value.get("todos")
                self._speakers[str(key)] = Speaker(
                    key=str(key),
                    name=" ".join(str(value.get("name") or key).split()),
                    title=" ".join(str(value.get("title") or DEFAULT_TITLE).split()),
                    todos=tuple(
                        " ".join(str(item).split()) for item in (todos or ())
                        if str(item).strip()
                    ),
                )
        default = str(payload.get("default") or "").strip()
        if default:
            self._default = default

    def get(self, key: str | None) -> Speaker:
        wanted = str(key or "").strip().lower()
        if wanted and wanted in self._speakers:
            return self._speakers[wanted]
        if self._default and self._default in self._speakers:
            return self._speakers[self._default]
        if wanted:
            return Speaker(key=wanted, name=wanted.title())
        return Speaker(key="gaertner", name="Gärtner")


class GrowroomBriefing:
    """Collect live data once, then speak one bounded briefing."""

    def __init__(self, *, host, speakers: SpeakerBook, temperatures, now=None) -> None:
        self._host = host
        self._speakers = speakers
        self._temperatures = temperatures
        self._now = now or (lambda: datetime.now(timezone.utc))

    def for_speaker(self, key: str | None) -> str:
        speaker = self._speakers.get(key)
        zones: list[dict] = []
        try:
            result = self._host.call_read("zone_growth_summary", {"limit": 50})
            zones = [entry for entry in result.get("zones") or []
                     if isinstance(entry, dict)]
        except Exception:
            # Cannanas down: the briefing still greets and lists the todos.
            zones = []
        try:
            temperatures = dict(self._temperatures() or {})
        except Exception:
            temperatures = {}
        if not zones and temperatures:
            # Cannanas is unavailable: still report the live room temperatures
            # instead of silently dropping the whole room section.
            zones = [
                {"name": room, "batch_count": 0, "plant_count": 0,
                 "stages": [], "last_diary": None}
                for room in sorted(temperatures)
            ]
        return compose_briefing(
            speaker=speaker.name,
            title=speaker.title,
            todos=speaker.todos,
            zones=zones,
            temperatures=temperatures,
            now=self._now(),
        )
