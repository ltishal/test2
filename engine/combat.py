"""Resolve declared combat moves before narration."""
from copy import deepcopy
from random import randint
import re

from campaign import ability_key, apply_ability_event, formula_key
from engine.effects import change_pool, effect_context, check_bonus, hero_incapacitated
from engine.clock import advance_action
from engine.equipment import equipped, WEAPONS, ARMOR
from engine.world import combat_ready, entity_names, key, records, text, life_state

ELEMENTS = {"physical", "fire", "cold", "lightning", "poison", "arcane", "radiant", "shadow"}
STYLE = {"balanced", "aggressive", "defensive", "caster"}
PROFILES = {
    "minor": (10, 2, "1d4", 4),
    "normal": (12, 3, "1d6+1", 8),
    "dangerous": (14, 5, "1d8+2", 12),
    "elite": (16, 7, "2d6+3", 18),
    "legendary": (18, 9, "2d8+4", 24),
}
ELEMENT_LABEL = {"physical": "физический", "fire": "огонь", "cold": "холод",
                 "lightning": "молния", "poison": "яд", "arcane": "магия",
                 "radiant": "свет", "shadow": "тьма"}
BUILTIN_ABILITIES = {
    "огненный снаряд": {"dice": "1d10", "element": "fire", "cost": 0, "cooldown": 0},
    "магическая стрела": {"dice": "1d4+1", "element": "arcane", "cost": 2,
                          "cooldown": 0, "hits": 3, "automatic": True},
    "волшебная стрела": {"dice": "1d4+1", "element": "arcane", "cost": 2,
                         "cooldown": 0, "hits": 3, "automatic": True},
}
VICTORY_XP = {"minor": 25, "normal": 50, "dangerous": 100, "elite": 200, "legendary": 400}


def bounded_int(value, low, high, default):
    return value if type(value) is int and low <= value <= high else default


def safe_ability(data, *, power=1):
    if not isinstance(data, dict):
        return None
    name = text(data.get("name"), 100)
    dice = text(data.get("dice"), 30)
    parts = formula_key(dice)
    if not (name and parts and parts[0] <= 6 and parts[1] <= 20 and not parts[2]
            and -5 <= parts[4] <= 12):
        return None
    element = data.get("element")
    element = element if isinstance(element, str) and element in ELEMENTS else "physical"
    ability = {"name": name, "dice": dice, "element": element,
            "cost": bounded_int(data.get("cost"), 0, 24, 0),
            "cooldown": bounded_int(data.get("cooldown"), 0, 8, 0),
            "hits": bounded_int(data.get("hits"), 1, 4, 1),
            "automatic": data.get("automatic") is True}

    # Cap burst and require resources/cooldown for damage above a tier budget.
    power = bounded_int(power, 1, 20, 1)
    burst = max(0, parts[0]*parts[1]+parts[4])*ability['hits']
    baseline = 10 + power*3
    if burst > baseline*3 or (burst > baseline and (ability['cost'] < max(1,(burst-baseline+7)//8) or ability['cooldown'] < 1)):
        return None
    if ability['automatic'] and burst > baseline and ability['cost'] < 2: return None
    return ability


def profile(entity, proposed=None):
    existing = entity.get("combat_profile")
    raw = proposed if isinstance(proposed, dict) else existing
    raw = raw if isinstance(raw, dict) else {}
    threat = entity.get("threat")
    defense, attack, dice, mana = PROFILES.get(threat if isinstance(threat, str) else "", PROFILES["normal"])
    resistances = {}
    source = raw.get("resistances")
    if isinstance(source, dict):
        for element, modifier in source.items():
            if element in ELEMENTS and modifier in ("resistant", "immune", "vulnerable"):
                resistances[element] = modifier
    abilities = []
    for candidate in raw.get("abilities", [])[:8] if isinstance(raw.get("abilities"), list) else []:
        if ability := safe_ability(candidate, power={"minor":1,"normal":2,"dangerous":5,"elite":9,"legendary":16}.get(threat if isinstance(threat,str) else "",1)):
            if not any(key(entry["name"]) == key(ability["name"]) for entry in abilities):
                abilities.append(ability)
    style = raw.get("style")
    return {"defense": defense, "attack": attack, "dice": dice, "mana_pool": mana,
            "style": style if isinstance(style, str) and style in STYLE else "balanced",
            "resistances": resistances, "abilities": abilities}


def hero_stat(state, stat):
    stats = state.get("stats")
    score = stats.get(stat) if isinstance(stats, dict) else None
    return (bounded_int(score, 1, 30, 10) - 10) // 2


def hero_defense(state):
    armor = equipped(state, 'armor')
    modifier = hero_stat(state, 'dex')
    return max(5, (armor['defense']+(min(modifier,armor['dex_cap']) if armor['dex_cap'] else 0) if armor else 10+modifier))


def total(dice, roller):
    count, sides, _, _, bonus = formula_key(dice)
    return max(0, sum(roller(1, sides) for _ in range(count)) + bonus)


def adjust_damage(value, element, resistances):
    match = resistances.get(element)
    if match == "immune":
        return 0
    if match == "resistant":
        return value // 2
    if match == "vulnerable":
        return value * 2
    return value


def find_combatant(state, ref):
    scene = state.get("scene") or {}
    local = key(scene.get("location")) if isinstance(scene, dict) else ""
    matches = [entity for entity in records(state) if entity.get("kind") in {"npc", "creature"}
               and (entity["id"] == ref or any(key(name) == key(ref) for name in entity_names(entity)))]
    matches = [entity for entity in matches if local and key(entity.get("location")) == local]
    exact = [entity for entity in matches if entity["id"] == ref]
    if exact: matches = exact
    if len(matches) != 1:
        raise ValueError("Укажите точное имя или ID одного персонажа в текущей локации.")
    return matches[0]


def narrated_attacker(entities, proposal, reply):
    candidate = proposal.get("actor_id") if isinstance(proposal, dict) else None
    if isinstance(candidate, str):
        npc = next((entry for entry in entities if entry["id"] == candidate
                    and entry.get("kind") in ("npc", "creature")), None)
        if npc and any(key(name) in reply.casefold() for name in entity_names(npc)):
            return npc
    announced = re.search(r"\bбросок\s+за\s+[«„\"*]*(\w+(?:-\w+)?)", reply, re.I)
    if not announced:
        return None
    reference = key(announced[1])
    matches = []
    for npc in entities:
        if npc.get("kind") not in ("npc", "creature"):
            continue
        for name in entity_names(npc):
            normalized = key(name)
            if (normalized == reference or len(reference) >= 4 and len(normalized) == len(reference)
                    and reference[:-1] == normalized[:-1]):
                matches.append(npc)
                break
    return matches[0] if len(matches) == 1 else None


def combat_context(state):
    combat = state.get("combat")
    if not isinstance(combat, dict) or not isinstance(combat.get("target_id"), str):
        return None
    target = next((entity for entity in records(state) if entity["id"] == combat["target_id"]), None)
    if not target or life_state(target)!="alive" or type(target.get("hp")) is not int or not target["hp"] or not state.get("hp"):
        return None
    scene = state.get("scene") or {}
    if not isinstance(scene, dict) or combat.get("location") != scene.get("location"):
        return None
    return combat


def hero_ability(state, name):
    owned = state.get("abilities")
    owned = [a for a in owned if isinstance(a, dict)] if isinstance(owned, list) else []
    card = next((entry for entry in owned if ability_key(entry.get("name")) == ability_key(name)), None)
    if not card or card.get("recharge") == "passive":
        raise ValueError("Эта боевая способность ещё не изучена героем.")
    raw = card.get("mechanics")
    if not isinstance(raw, dict):
        raw = BUILTIN_ABILITIES.get(key(card.get("name")))
    ability = safe_ability({**raw, "name": card["name"]}, power=bounded_int(state.get("level"),1,20,1)) if isinstance(raw, dict) else None
    if not ability and raw is not None and raw is BUILTIN_ABILITIES.get(key(card.get("name"))):
        ability = {**raw, "name":card["name"]}
    if not ability:
        raise ValueError("У способности нет механики боя. Опишите применение мастеру обычным текстом.")
    if card.get("recharge") == "unlimited" or type(card.get("uses_max")) is int:
        abilities, used = apply_ability_event(owned, card["name"])
        if not used:
            raise ValueError(f"У «{card['name']}» нет доступных зарядов.")
        state["abilities"] = abilities
    return ability


def mana_for(actor, pool):
    if type(actor.get("mana")) is not int or type(actor.get("max_mana")) is not int:
        actor.update(mana=pool, max_mana=pool)
    return actor["mana"]


def strike(attacker, defender, ability, bonus, defense, resistances, roller):
    move = ability["name"]
    target = defender.get("name") or "Герой"
    if ability.get("automatic"):
        hit = f"{move}: автоматическое попадание по цели «{target}»."
    else:
        die = roller(1, 20)
        check = die + bonus
        hit = f"{move}: {die}{bonus:+} = {check} против КБ {defense}."
        if die == 1 or (die != 20 and check < defense):
            return f"{hit} Промах."
    parts = [adjust_damage(total(ability["dice"], roller), ability["element"], resistances)
             for _ in range(ability.get("hits", 1))]
    damage = sum(parts)
    lost = -change_pool(defender, "hp", -damage)
    display = "+".join(map(str, parts))
    if ability["element"] != "physical":
        display += f", {ELEMENT_LABEL[ability['element']]}"
    return f"{hit} Урон: {display}. «{target}» теряет {lost} HP и остаётся с {defender['hp']}/{defender['max_hp']} HP."


def active_control(state, ident):
    return {effect.get("control") for effect in effect_context(state)
            if effect.get("type") == "control" and effect.get("target") == ident}


def cooldown_ready(cooldowns, name, round_number):
    return bounded_int(cooldowns.get(key(name)), 0, 2_000_000_000, 0) <= round_number


def spend_move(actor, ability, cooldowns, round_number, capacity):
    cost = ability["cost"]
    if not cooldown_ready(cooldowns, ability["name"], round_number):
        raise ValueError(f"«{ability['name']}» ещё восстанавливается.")
    if cost and mana_for(actor, capacity) < cost:
        raise ValueError(f"Для «{ability['name']}» нужно {cost} маны.")
    if cost:
        actor["mana"] -= cost
    if ability["cooldown"]:
        cooldowns[key(ability["name"])] = round_number + ability["cooldown"] + 1


def combat_turn(original, command, argument, roller=None):
    roller = roller or randint
    state = deepcopy(original)
    if hero_incapacitated(state): raise ValueError("Герой не может действовать из-за активного эффекта.")
    combat = combat_context(state)
    target_arg = argument
    if command == "/cast":
        ability_name, separator, target_arg = argument.partition("|")
        ability_name, target_arg = ability_name.strip(), target_arg.strip() if separator else ""
        if not ability_name:
            raise ValueError("Укажите /cast способность | цель.")
    if command == "/retreat":
        if not combat:
            raise ValueError("Сейчас нет активного боя.")
        target = find_combatant(state, combat["target_id"])
        die = roller(1, 20)
        modifier = hero_stat(state, "dex") + check_bonus(state,"dex")
        difficulty = profile(target)["defense"]
        if die + modifier >= difficulty:
            state["combat"] = None
            state["combat_rounds"] = bounded_int(state.get("combat_rounds"),0,2_000_000_000,0)+1
            return advance_action(state,6), f"Отступление: {die}{modifier:+} против КБ {difficulty}. Герой выходит из боя."
        command = "/guard"
        retreat = f"Отступление: {die}{modifier:+} против КБ {difficulty}. Противник перекрывает путь."
    else:
        retreat = ""
    if command == "/guard" and not combat:
        raise ValueError("Сейчас нет активного боя.")
    if not bounded_int(state.get("hp"), 1, 2_000_000_000, 0):
        raise ValueError("Герой выведен из строя.")
    if combat and not target_arg:
        target_arg = combat["target_id"]
    if not target_arg:
        raise ValueError("Укажите цель, например /attack имя.")
    target = find_combatant(state, target_arg)
    if combat and target["id"] != combat["target_id"]:
        raise ValueError("Сначала завершите текущий бой или попробуйте отступить.")
    combat_ready(target, "normal")
    if life_state(target)!="alive" or not bounded_int(target.get("hp"), 1, 2_000_000_000, 0):
        raise ValueError("Этот противник недоступен для боя.")
    if not bounded_int(target.get("max_hp"), target["hp"], 2_000_000_000, 0):
        raise ValueError("У противника повреждена запись здоровья.")
    enemy = target["combat_profile"] = profile(target)
    location = state["scene"]["location"]
    log = [retreat] if retreat else []
    if not combat:
        hero_init = roller(1, 20) + hero_stat(state, "dex") + check_bonus(state,"dex")
        enemy_init = roller(1, 20) + max(0, enemy["attack"] - 3) + check_bonus(state,"dex",target["id"])
        combat = {"target_id": target["id"], "location": location, "round": 0,
                  "hero_first": hero_init >= enemy_init, "npc_cooldowns": {}}
        first = (state.get("name") or "Герой") if combat["hero_first"] else target["name"]
        log.append(f"Инициатива: герой {hero_init}, {target['name']} {enemy_init}. Первым действует «{first}».")
    round_number = bounded_int(combat.get("round"), 0, 1_000_000, 0) + 1
    combat["round"] = round_number
    cooldowns = state.get("ability_cooldowns")
    cooldowns = cooldowns if isinstance(cooldowns, dict) else {}
    state["ability_cooldowns"] = cooldowns
    # Cooldowns survive encounter boundaries. They count completed combat rounds.
    hero_round = bounded_int(state.get("combat_rounds"), 0, 2_000_000_000, 0) + 1
    state["combat_rounds"] = hero_round
    guarding = command == "/guard"
    if guarding:
        log.append("Герой держит оборону. КБ повышается на 2 до конца раунда.")
    prepared = None
    if command == "/cast":
        prepared = deepcopy(state)
        ready_cooldowns = dict(cooldowns)
        move = hero_ability(prepared, ability_name)
        if "silence" in active_control(state, "hero") and move["element"] != "physical":
            raise ValueError("Безмолвие мешает применить эту способность.")
        spend_move(prepared, move, ready_cooldowns, hero_round, 6 + max(0, hero_stat(state, "int")) * 2)
    elif not guarding:
        weapon = key(state.get("weapon_in_hand"))
        move = {"name": "Атака" if not weapon else f"Атака: {weapon}",
                "dice": WEAPONS.get(weapon, "1d4"), "element": "physical"}
        spec = equipped(state,"weapon")
        if spec: move.update(dice=spec["dice"],element=spec["element"],stat=spec["stat"])
    log.append(f"Раунд {round_number}.")
    npc_cooldowns = combat.get("npc_cooldowns")
    if not isinstance(npc_cooldowns, dict):
        npc_cooldowns = combat["npc_cooldowns"] = {}
    enemy_guard = enemy["style"] == "defensive" and 3 * target["hp"] <= target["max_hp"] and round_number % 2 == 0
    for hero_turn in (combat["hero_first"], not combat["hero_first"]):
        if state["hp"] <= 0 or target["hp"] <= 0:
            break
        if hero_turn:
            if not guarding:
                if prepared is not None:
                    state.update({field: prepared[field] for field in ("abilities", "mana", "max_mana") if field in prepared})
                    state["ability_cooldowns"] = ready_cooldowns
                stat = state.get("casting_stat", "int") if command == "/cast" else move.get("stat", "str")
                log.append(strike(state, target, move, 2 + hero_stat(state, stat) + check_bonus(state,stat),
                                  enemy["defense"] + (2 if enemy_guard else 0), enemy["resistances"], roller))
            continue
        if active_control(state, target["id"]) & {"sleep", "paralysis", "stun"}:
            log.append(f"«{target['name']}» не может действовать из-за активного эффекта.")
            continue
        if enemy_guard:
            log.append(f"«{target['name']}» защищается и получает +2 к КБ.")
            continue
        npc_move = next((ability for ability in enemy["abilities"]
                         if cooldown_ready(npc_cooldowns, ability["name"], round_number)
                         and (not ability["cost"] or mana_for(target, enemy["mana_pool"]) >= ability["cost"])
                         and ("silence" not in active_control(state, target["id"]) or ability["element"] == "physical")), None)
        if npc_move and (enemy["style"] in ("caster", "aggressive") or round_number % 2 == 0):
            spend_move(target, npc_move, npc_cooldowns, round_number, enemy["mana_pool"])
        else:
            npc_move = {"name": f"Атака: {target['name']}", "dice": enemy["dice"], "element": "physical"}
        log.append(strike(target, state, npc_move, enemy["attack"] + check_bonus(state,"int" if npc_move["element"] != "physical" else "str",target["id"]),
                          hero_defense(state) + (2 if guarding else 0), {}, roller))
    advance_action(state,6)
    if state["hp"] <= 0 or target["hp"] <= 0:
        state["combat"] = None
        log.append("Бой завершён.")
        if target["hp"] == 0 and not target.get("combat_xp_paid"):
            threat = target.get("threat")
            xp = VICTORY_XP.get(threat if isinstance(threat, str) else "", VICTORY_XP["normal"])
            state["xp"] = bounded_int(state.get("xp"), 0, 2_000_000_000, 0) + xp
            target["combat_xp_paid"] = True
    else:
        state["combat"] = combat
    return state, "\n".join(log)


COMBAT_PROMPT = """
У существ и NPC при вступлении в бой указывай threat и combat_profile:{style,resistances,abilities}. style: balanced/aggressive/defensive/caster. resistances: ключи physical/fire/cold/lightning/poison/arcane/radiant/shadow со значением resistant/immune/vulnerable. abilities: [{name,dice,element,cost,cooldown,hits,automatic}]. cost в мане, cooldown в раундах. Эти параметры проверяет сервер: сильный суммарный урон требует затрат маны и перезарядки, учитывается уровень героя или угроза существа. ability_cooldowns — оставшиеся боевые раунды, 0 означает готовность. Не объявляй применение недоступной способности. Для уже изученной способности героя можно передать такой же объект в abilities[].mechanics. При обычной атаке NPC объявляй имя атакующего и одну формулу, DC атаки по герою определит сервер. Результаты combat и rule_effect считай готовыми фактами.
"""
