"""Saved scene, pending checks, and deterministic dice resolution."""

import re
from hashlib import sha256
from html import unescape
from uuid import uuid4

ROLL_COMMAND = re.compile(r"/roll\s+((?:\d*)d\d+(?:k[hl]\d+)?(?:[+-]\d+)?)", re.I)
DICE_PARTS = re.compile(r"(\d*)d(\d+)(?:(k[hl])(\d+))?([+-]\d+)?", re.I)
DIFFICULTY = re.compile(r"(?:\bDC|КД|КЗ|КБ)\s*[*: ]*(\d+)", re.I)
KINDS = {"check", "attack", "save", "damage", "heal", "initiative"}
SCENE_FIELDS = ("id", "location", "time", "participants", "goal", "situation")
FACT_FIELDS = ("id", "subject", "detail", "source", "holder", "status")
FACT_STATUSES = {"confirmed", "reported", "suspected", "resolved"}
IDENTITY_FIELDS = ("name", "race", "class", "background")
CANCEL_CHECK_COMMAND = "/cancel-check"
CANCEL_CHECK_REPLY = "Проверка отменена. Выберите следующее действие."
CHECK_IN_MINUTES = 30
CLOCK_VALUE = r"(?:(?:День\s+(?P<day>\d{1,5}),?\s*)?)(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)"
CLOCK_RE = re.compile(r"(?<!\d)" + CLOCK_VALUE + r"(?!\d)", re.I)
CURRENT_CLOCK_RE = re.compile(
    r"(?P<label>\b(?:(?:сейчас|теперь|время теперь|текущее время)\s*(?:[—–:=]\s*)?"
    r"|время\s*[—–:=]\s*)(?:\*\*)?)"
    + CLOCK_VALUE + r"(?!\d)", re.I,
)
COMPLETED_ARRIVAL_RE = re.compile(
    r"\b[Кк]\s+" + CLOCK_VALUE + r"(?!\d)\s+(?=[^.\n]{0,90}\b"
    r"(?:добрал\w*|добира\w*|вернул\w*|верну[влш]\w*|вновь|снова|прибыл\w*|приш[ёллаи]+)\b)",
    re.I,
)


def sheet_identity(reply):
    """Read the same identity shown in the character sheet."""
    if reply.count("[[ЛИСТ]]") != 1 or reply.count("[[/ЛИСТ]]") != 1:
        return {}
    _, _, suffix = reply.partition("[[ЛИСТ]]")
    sheet, end, _ = suffix.partition("[[/ЛИСТ]]")
    if not end:
        return {}
    for line in sheet.splitlines():
        identity = line.strip().replace("**", "")
        if not identity.startswith("👤"):
            continue
        identity = identity.removeprefix("👤").lstrip("️ ").strip()
        parts = re.split(r"\s+[-—–]\s+", identity, maxsplit=1)
        if len(parts) != 2:
            return {}
        values = [parts[0], *parts[1].split("·")]
        if len(values) == len(IDENTITY_FIELDS) and all(value.strip() for value in values):
            return dict(zip(IDENTITY_FIELDS, (value.strip() for value in values)))
        return {}
    return {}


def initial_identity(reply, selected):
    identity = sheet_identity(reply)
    if not identity:
        return {}
    for key in IDENTITY_FIELDS:
        value = selected.get(key)
        if isinstance(value, str) and value.strip() and (key != "name" or value.strip() != "Пустой"):
            identity[key] = value.strip()
    return identity



def _text(value, limit=180):
    return value.strip()[:limit] if isinstance(value, str) else ""


def formula_key(expr):
    if not isinstance(expr, str):
        return None
    match = DICE_PARTS.fullmatch(expr.strip())
    if not match:
        return None
    count, sides, keep, keep_count, bonus = match.groups()
    if any(len(value or "") > 8 for value in (count, sides, keep_count, bonus)):
        return None
    count, sides = int(count or 1), int(sides)
    keep_count = int(keep_count or count)
    if not (1 <= count <= 100 and 2 <= sides <= 1000 and 1 <= keep_count <= count):
        return None
    return count, sides, (keep or "").lower(), keep_count, int(bonus or 0)


def pending_check(reply, proposed=None, scene=None, *, defense_dc=None):
    """A check must be announced in the actual reply. Older replies can be inferred."""
    if not isinstance(reply, str):
        return None
    commands = list(ROLL_COMMAND.finditer(reply))
    if not commands:
        return None
    formulas = {formula_key(match[1]) for match in commands}
    if None in formulas or len(formulas) != 1:
        return None
    difficulties = set()
    preceding = 0
    for match in commands:
        targets = list(DIFFICULTY.finditer(reply[max(preceding, match.start() - 470):match.start()].replace("*", "")))
        if targets:
            difficulties.add(int(targets[-1][1]))
        preceding = match.end()
    if len(difficulties) > 1:
        return None
    command = commands[0]
    formula = command[1].lower()
    if formula_key(formula) is None:
        return None
    nearby = reply[max(0, command.start() - 470):command.start()].replace("*", "")
    targets = list(DIFFICULTY.finditer(nearby))
    dc = defense_dc if defense_dc is not None else next(iter(difficulties), None)
    suggested = proposed if isinstance(proposed, dict) else {}
    title = _text(suggested.get("title"))
    kind = suggested.get("kind")
    label = (title or nearby[-230:]).casefold()
    if not isinstance(kind, str) or kind not in KINDS:
        last_dc = targets[-1].start() if targets else -1
        tail = nearby.lower()
        if tail.rfind("урон") > last_dc and ("брось урон" in tail or "урон арбалета" in tail):
            kind = "damage"
        elif dc is not None:
            if "спасброс" in label:
                kind = "save"
            elif any(marker in label for marker in ("атак", "выпад", "укус")):
                kind = "attack"
            else:
                kind = "check"
        elif any(marker in label for marker in ("лечен", "второе дыхание", "восстанавлива")):
            kind = "heal"
        elif "инициатив" in label:
            kind = "initiative"
        elif "урон" in label:
            kind = "damage"
        else:
            return None
    if kind == "damage" and dc is not None:
        # The previous comparison can be quoted in a request for damage.
        if not suggested and nearby.rfind("урон") < targets[-1].start():
            kind = "attack"
    if kind in {"check", "attack", "save"} and (dc is None or not 1 <= dc <= 100):
        return None
    if kind not in {"check", "attack", "save"}:
        dc = None
    effect = suggested.get("effect")
    ability = _text(suggested.get("ability"), 100)
    if kind == "heal" and "второе дыхание" in (label + nearby.casefold()):
        ability = "Второе дыхание"
    if kind == "damage" and effect != "hero_damage" and not suggested:
        if re.search(r"попада\w+ в (?:тебя|рагара)", nearby, re.I):
            effect = "hero_damage"
    if kind == "damage" and "дыхани" in (label + nearby.casefold()) and "дракон" in nearby.casefold():
        ability = "Дыхание дракона"
    return {
        "id": uuid4().hex,
        "scene_id": _text((scene or {}).get("id"), 80),
        "kind": kind,
        "title": title or _text(nearby, 180),
        "description": _text(nearby[-440:], 440),
        "target": _text(suggested.get("target"), 100),
        "dc": dc,
        "expr": formula,
        "effect": effect if (kind, effect) in (("damage", "hero_damage"), ("heal", "hero_heal")) else "",
        "ability": ability,
    }


def check_instructions_in_card(reply, check):
    """Show an announced formula in its check card and keep other commands in prose."""
    commands = list(ROLL_COMMAND.finditer(reply))
    if not commands or any(formula_key(match[1]) != formula_key(check.get("announced_expr", check["expr"])) for match in commands):
        return reply
    if (check.get("ammo") or check.get("actor_id")) and type(check.get("dc")) is int:
        # The card carries the resolved formula and DC. Keep mechanical
        # numbers mentioned in prose consistent with that card.
        reply = re.sub(r"(\*\*|`)(" + ROLL_COMMAND.pattern + r")\s*,?\s*"
                       + DIFFICULTY.pattern + r"(?:\*\*|`)", "", reply, flags=re.I)
        reply = DIFFICULTY.sub(lambda match: match[0][:match.start(1) - match.start()] + str(check["dc"]), reply)
        reply = re.sub(r":\s*\.(?=\s|$)", ".", reply)
    return re.sub(
        r"(?:\*\*|\x60)*(?:команда\s*:\s*)?(?:\*\*|\x60)*"
        + ROLL_COMMAND.pattern + r"(?:\*\*|\x60)*(?:\.(?:\*\*|\x60)*)?",
        lambda match: "\n\n" if match.end() < len(reply) and reply[match.end()].isupper() else "",
        reply, flags=re.I,
    ).strip()


def active_check(state, messages):
    if "pending_check" in state:
        value = state["pending_check"]
        return value if isinstance(value, dict) else None
    last = messages[-1] if messages else {}
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return None
    check = pending_check(last.get("content"))
    if check:
        # Legacy saves have only the announcement text. Its ID must survive retry and reload.
        check["id"] = sha256(last["content"].encode()).hexdigest()[:32]
    return check


def roll_result(check, roll):
    """Recalculate the total and resolve comparison from saved values."""
    if not isinstance(roll, dict):
        return None
    key = formula_key(roll.get("expr"))
    if not key:
        raise ValueError("Не удалось прочитать формулу броска.")
    if check and (roll.get("check_id") != check.get("id") or key != formula_key(check.get("expr"))):
        raise ValueError("Этот бросок относится к другой проверке.")
    if not check and roll.get("check_id"):
        raise ValueError("Эта проверка уже завершена или заменена.")
    count, sides, mode, keep_count, bonus = key
    dice = roll.get("rolls")
    if not isinstance(dice, list) or len(dice) != count or any(
        type(value) is not int or not 1 <= value <= sides for value in dice
    ):
        raise ValueError("Не удалось прочитать выпавшие кубики.")
    kept = sorted(dice, reverse=mode == "kh")[:keep_count] if mode else dice
    result = {"expr": roll["expr"], "rolls": dice, "kept": kept, "mod": bonus, "total": sum(kept) + bonus}
    if check:
        result["check"] = check
        effect_bonus = check.get("effect_bonus", 0)
        if type(effect_bonus) is int and -10 <= effect_bonus <= 10 and effect_bonus:
            result["effect_bonus"] = effect_bonus
            result["total"] += effect_bonus
        dc = check.get("dc")
        if type(dc) is int:
            result["success"] = result["total"] >= dc
        if check.get("ammo") and check.get("kind") == "attack" and count == 1 and sides == 20:
            result["success"] = dice[0] == 20 or (dice[0] != 1 and result["success"])
            if type(roll.get("ranged_damage")) is int and 0 <= roll["ranged_damage"] <= 300:
                result["ranged_damage"] = roll["ranged_damage"] if result["success"] else 0
        damage = roll.get("npc_attack_damage")
        if check.get("kind") == "attack" and check.get("actor_id") and isinstance(result.get("success"), bool):
            if not result["success"] or check.get("practice"):
                result["npc_attack_damage"] = 0
            elif type(damage) is int and 0 <= damage <= 300:
                result["npc_attack_damage"] = damage
    return result


def rules_result(state, result):
    """Apply only effects that were explicitly declared for the player character."""
    if not result or not result.get("check"):
        return {}
    check = result["check"]
    effect = check.get("effect")
    attack_damage = result.get("npc_attack_damage")
    second_wind = check.get("kind") == "heal" and check.get("ability", "").casefold() == "второе дыхание"
    if effect not in {"hero_damage", "hero_heal"} and not second_wind and type(attack_damage) is not int:
        return {}
    hp = state.get("hp", 0)
    max_hp = state.get("max_hp", 1)
    if type(hp) is not int or type(max_hp) is not int:
        return {}
    amount = max(0, attack_damage if type(attack_damage) is int else result["total"])
    damage = effect == "hero_damage" or type(attack_damage) is int
    after = max(0, min(max_hp, hp - amount if damage else hp + amount))
    return {"hp_before": hp, "hp_after": after, "max_hp": max_hp}


def merge_scene(previous, update):
    previous = previous if isinstance(previous, dict) else {}
    if not isinstance(update, dict):
        return previous
    new_location = _text(update.get("location"), 360)
    moved = bool(new_location and new_location != previous.get("location"))
    scene = {} if moved else dict(previous)
    for field in SCENE_FIELDS:
        if field not in update or field == "id":
            continue
        value = update[field]
        if field == "participants":
            if isinstance(value, list):
                scene[field] = [_text(name, 100) for name in value[:16] if _text(name, 100)]
        elif isinstance(value, str):
            scene[field] = _text(value, 360)
    if scene and (not scene.get("id") or scene.get("location") != previous.get("location")):
        scene["id"] = uuid4().hex
    return scene


def merge_facts(previous, updates):
    previous = previous if isinstance(previous, list) else []
    facts = {_text(fact.get("id"), 90).casefold(): fact for fact in previous if isinstance(fact, dict)}
    for patch in (updates[:12] if isinstance(updates, list) else []):
        if not isinstance(patch, dict):
            continue
        key = _text(patch.get("id"), 90).casefold()
        if not key or key.startswith("npc-action:"):
            continue
        entry = dict(facts.pop(key, {}))
        entry.update({field: _text(patch[field], 340) for field in FACT_FIELDS if isinstance(patch.get(field), str)})
        entry["id"] = key
        if entry.get("status") not in FACT_STATUSES:
            entry["status"] = "reported"
        if entry.get("subject") and entry.get("detail"):
            facts[key] = entry
    return list(facts.values())


def record_npc_actions(facts, patches, scene, minute, grounded):
    records = list(facts)
    known = {fact["id"] for fact in facts if isinstance(fact.get("id"), str)}
    for patch in patches[:8] if isinstance(patches, list) else []:
        if not isinstance(patch, dict) or not grounded(patch.get("evidence")):
            continue
        actor = _text(patch.get("actor"), 100)
        event = _text(patch.get("id"), 75).casefold()
        key = "npc-action:" + event
        if not actor or not event or key in known:
            continue
        records.append({
            "id": key, "kind": "npc_action", "subject": actor,
            "detail": _text(patch["evidence"], 340), "status": "confirmed",
            "location": _text(patch.get("location") or scene.get("location"), 160),
            "minute": minute,
        })
        known.add(key)
    return records


def notes_context(notes, query):
    if not isinstance(notes, list):
        return []
    words = set(re.findall(r"\w{4,}", query.casefold()))
    entries = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        entry = {key: _text(note.get(key), 260) for key in ("title", "content", "source", "type")}
        keywords = set(re.findall(r"\w{4,}", (entry["title"] + " " + entry["content"]).casefold()))
        title_words = set(re.findall(r"\w{4,}", entry["title"].casefold()))
        entries.append((len(words & keywords) + 2 * len(words & title_words), entry))
    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in entries[:5]]


def facts_context(facts, query):
    if not isinstance(facts, list):
        return []
    words = {word[:5] for word in re.findall(r"\w{4,}", query.casefold())}
    entries = [entry for entry in reversed(facts) if isinstance(entry, dict)]
    def relevance(entry):
        values = " ".join(str(entry.get(key, "")) for key in ("subject", "detail", "location", "holder"))
        matches = words.intersection(word[:5] for word in re.findall(r"\w{4,}", values.casefold()))
        return len(matches)

    ranked = sorted(entries, key=relevance, reverse=True)
    actions = [entry for entry in ranked if entry.get("kind") == "npc_action"][:4]
    return actions + [entry for entry in ranked if entry not in actions][:12 - len(actions)]


def clean_ability_name(name):
    if not isinstance(name, str):
        return ""
    return " ".join(unescape(name).replace("**", "").split())


def ability_key(name):
    label = clean_ability_name(name).casefold()
    for standard in ("дыхание дракона", "второе дыхание", "мистический заряд"):
        if label == standard or re.match(re.escape(standard) + r"[,:( —-]", label):
            return standard
    return label


def _negative_count(value):
    return (isinstance(value, int) and value < 0
            or isinstance(value, str) and re.fullmatch(r"-[1-9]\d*", value.strip()) is not None)


def normalize_ability(entry):
    if not isinstance(entry, dict):
        return None
    name = clean_ability_name(entry.get("name"))
    if not name:
        return None
    result = {**entry, "name": name}
    recharge = result.get("recharge")
    maximum = result.get("uses_max")
    if recharge != "passive" and (
        recharge in ("unlimited", "at_will") or _negative_count(maximum)
        or (maximum is None and _negative_count(result.get("uses_left")))
    ):
        result["recharge"] = "unlimited"
    if result.get("recharge") in ("passive", "unlimited"):
        result.pop("uses_max", None)
        result.pop("uses_left", None)
    return result


def normalize_abilities(entries):
    if not isinstance(entries, list):
        return []
    return [ability for entry in entries if (ability := normalize_ability(entry))]


def apply_ability_event(abilities, name="", rest=""):
    """Return updated charges and whether the requested ability could be used."""
    entries = normalize_abilities(abilities)
    expected = ability_key(name)
    used = False
    for entry in entries:
        recharge = entry.get("recharge")
        matches = bool(expected and ability_key(entry["name"]) == expected)
        if recharge == "unlimited":
            used = used or matches
            continue
        maximum = entry.get("uses_max")
        if type(maximum) is not int or maximum <= 0:
            continue
        if (recharge == "short_rest" and rest in {"short_rest", "long_rest"}
                or recharge == "long_rest" and rest == "long_rest"):
            entry["uses_left"] = maximum
        if matches and not used:
            available = entry.get("uses_left", maximum)
            if type(available) is int and available > 0:
                entry["uses_left"] = min(maximum, available) - 1
                used = True
    return entries, used


WAIT_COMMAND = re.compile(r"/wait\s+(next|dawn|noon|evening|[1-9]\d{0,3})", re.I)
PLAIN_WAIT = re.compile(
    r"(?:я )?(?:жду|ожидаю|пропускаю время) до (рассвета|полудня|вечера|ближайшего события)[.!]?", re.I,
)
WAIT_WORDS = {"рассвета": "dawn", "полудня": "noon", "вечера": "evening", "ближайшего события": "next"}


def campaign_minute(state):
    minute = state.get("world_minute")
    if type(minute) is int and minute >= 0:
        return minute
    scene = state.get("scene")
    label = (scene.get("time") or "").casefold() if isinstance(scene, dict) and isinstance(scene.get("time"), str) else ""
    stated = CLOCK_RE.fullmatch(label.strip())
    if stated:
        return (max(1, int(stated["day"] or 1)) - 1) * 1440 + int(stated["hour"]) * 60 + int(stated["minute"])
    for word, minute in (("рассвет", 360), ("утр", 540), ("полд", 720),
                         ("сумерк", 1140), ("вечер", 1140), ("ноч", 1320), ("день", 960)):
        if word in label:
            return minute
    return None


def clock_label(minute):
    day, remainder = divmod(minute, 1440)
    hour, minute = divmod(remainder, 60)
    return f"День {day + 1}, {hour:02d}:{minute:02d}"


def advance_clock(start, reply, elapsed, end, previous_check, resolving, grounded):
    if (resolving and previous_check and previous_check.get("clock_spent")
            and not (isinstance(elapsed, dict) and elapsed.get("additional") is True
                     and grounded(elapsed.get("evidence"))
                     and re.search(r"\b(?:ещ[её]|дополнительно)\b", elapsed["evidence"], re.I))):
        return start
    anchor = None
    if isinstance(end, dict) and grounded(end.get("evidence")):
        clocks = list(CLOCK_RE.finditer(end["evidence"]))
        if len(clocks) == 1:
            anchor = clocks[0]
    if anchor is None:
        anchors = list(CURRENT_CLOCK_RE.finditer(reply)) + list(COMPLETED_ARRIVAL_RE.finditer(reply))
        if anchors:
            anchor = max(anchors, key=lambda match: match.start())
    if anchor:
        minute_of_day = int(anchor["hour"]) * 60 + int(anchor["minute"])
        day = int(anchor["day"]) - 1 if anchor["day"] else start // 1440
        target = day * 1440 + minute_of_day
        if not anchor["day"] and start - target > 720:
            target += 1440
        if start <= target <= start + 43200:
            return target
    if isinstance(elapsed, dict) and grounded(elapsed.get("evidence")):
        duration = elapsed.get("minutes")
        if type(duration) is int and 1 <= duration <= 43200:
            return start + duration
    return start


def synchronize_clock(reply, minute):
    """Replace a reported current clock reading with the committed time."""
    label = clock_label(minute)
    return CURRENT_CLOCK_RE.sub(lambda match: match["label"] + (
        label if match["day"] else label.rsplit(" ", 1)[1]
    ), reply)


def update_agenda(state, patches, resolutions, minute, grounded):
    stored = state.get("agenda")
    events = {event["id"]: dict(event) for event in stored
              if isinstance(event, dict) and isinstance(event.get("id"), str)} if isinstance(stored, list) else {}
    for patch in patches if isinstance(patches, list) else []:
        if not isinstance(patch, dict) or not grounded(patch.get("evidence")):
            continue
        key = _text(patch.get("id"), 100).casefold()
        title = _text(patch.get("title"), 160)
        delay = patch.get("in_minutes")
        if not key or key in events or not title:
            continue
        check_in = delay is None and patch.get("check_in") is True
        if check_in:
            delay = CHECK_IN_MINUTES
        if type(delay) is not int or not 1 <= delay <= 10080:
            continue
        events[key] = {"id": key, "title": title, "location": _text(patch.get("location"), 160),
                       "due": minute + delay, "status": "scheduled"}
        if check_in:
            events[key]["check_in"] = True
    for patch in resolutions if isinstance(resolutions, list) else []:
        if not isinstance(patch, dict) or not grounded(patch.get("evidence")):
            continue
        event = events.get(_text(patch.get("id"), 100).casefold())
        outcome = _text(patch.get("outcome"), 300)
        if (event and (event["status"] == "scheduled" or event.get("no_report")) and outcome
                and (event["due"] <= minute or event.get("check_in") or patch.get("early") is True)):
            event.update(status="resolved", outcome=outcome)
            event.pop("no_report", None)
    return sorted(events.values(), key=lambda event: event["due"])


def close_overdue_events(events, minute):
    for event in events:
        if event["status"] != "scheduled" or event["due"] > minute:
            continue
        location = event.get("location")
        follow_up = (f"Проверьте у участников события на месте ({location})." if location
                     else "Уточните исход у того, кто обещал сообщить результат.")
        event.update(status="resolved", outcome="К сроку подтверждения нет. " + follow_up, no_report=True)


def wait_plan(state, action):
    action = action.strip()
    match = WAIT_COMMAND.fullmatch(action)
    plain = PLAIN_WAIT.fullmatch(action)
    if not match and not plain:
        if action.casefold().startswith("/wait"):
            raise ValueError("Используйте /wait next, /wait dawn, /wait noon, /wait evening или от 1 до 1440 минут.")
        return None
    token = match[1].lower() if match else WAIT_WORDS[plain[1].lower()]
    start = campaign_minute(state)
    if start is None:
        raise ValueError("В этой партии время ещё не установлено. Продолжите сцену обычным действием.")
    events = [event for event in (state.get("agenda") or [])
              if isinstance(event, dict) and event.get("status") == "scheduled"
              and type(event.get("due")) is int]
    if token == "next":
        if not events:
            raise ValueError("Нет запланированных событий. Можно подождать до рассвета или указать минуты.")
        target = max(start, min(event["due"] for event in events))
    elif token.isdigit():
        if int(token) > 1440:
            raise ValueError("За один ход можно пропустить до 1440 минут.")
        target = start + int(token)
    else:
        minute_of_day = dict(dawn=360, noon=720, evening=1140)[token]
        target = start // 1440 * 1440 + minute_of_day
        if target <= start:
            target += 1440
    return {"from": start, "until": target, "label": clock_label(target),
            "due": [event for event in events if event["due"] <= target]}
