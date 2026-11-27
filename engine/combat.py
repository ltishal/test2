"""Resolve declared combat moves before narration."""
from copy import deepcopy
from random import randint
import re

from campaign import ROLL_COMMAND, ability_key, apply_ability_event, formula_key, pending_check
from engine.effects import change_pool, effect_context, check_bonus, hero_incapacitated
from engine.clock import advance_action
from engine.equipment import equipped, item_spec, require_ammo, consume_ammo, WEAPONS, ARMOR
from engine.world import combat_ready, encounter_position, entity_names, key, records, text, is_present, life_state

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


def narrated_attacker(entities, proposal, reply, *, hero_name=None, location=None):
    if location:
        entities = [entry for entry in entities if key(entry.get("location")) in ("", key(location))]
    candidate = proposal.get("actor_id") if isinstance(proposal, dict) else None
    if isinstance(candidate, str):
        npc = next((entry for entry in entities if entry["id"] == candidate
                    and entry.get("kind") in ("npc", "creature") and is_present(entry)), None)
        if npc and any(key(name) in reply.casefold() for name in entity_names(npc)):
            return npc
    announced = re.search(r"\bбросок\s+за\s+[«„\"*]*(\w+(?:-\w+)?)", reply, re.I)
    if not announced:
        command = ROLL_COMMAND.search(reply)
        if not command:
            return None
        nearby = re.sub(r'[*_`]', '', reply[max(0, command.start() - 400):command.start()])
        named_hero = key(hero_name)
        hero = re.escape(named_hero[:-1] if named_hero.endswith(("а", "я")) else named_hero)
        recipient = r"(?:тебя|героя"
        if hero:
            recipient += rf"|{hero}\w{{0,2}}"
        recipient += ")"
        matches = []
        for entity in entities:
            if entity.get("kind") not in ("npc", "creature") or not is_present(entity):
                continue
            for name in entity_names(entity):
                pattern = (rf"\b{re.escape(key(name))}\b[^.!?;]{{0,65}}?"
                           rf"\bатакует\s+{recipient}\b")
                if re.search(pattern, nearby.casefold().replace("ё", "е")):
                    matches.append(entity)
                    break
        return matches[0] if len(matches) == 1 else None
    reference = key(announced[1])
    matches = []
    for npc in entities:
        if npc.get("kind") not in ("npc", "creature") or not is_present(npc):
            continue
        for name in entity_names(npc):
            normalized = key(name)
            if (normalized == reference or len(reference) >= 4 and len(normalized) == len(reference)
                    and reference[:-1] == normalized[:-1]):
                matches.append(npc)
                break
    return matches[0] if len(matches) == 1 else None


MAX_ENEMIES = 8


def encounter_roster(state, battle):
    """Resolve stable roster IDs against the current world records."""
    roster = battle.get("enemy_ids")
    if not isinstance(roster, list):
        roster = [battle.get("target_id")]
    local = {entity["id"]: entity for entity in records(state)
             if entity.get("kind") in ("npc", "creature")}
    return [local[ident] for ident in dict.fromkeys(
        ident for ident in roster[:MAX_ENEMIES] if isinstance(ident, str)) if ident in local]


def combatant_available(entity, location):
    return (key(entity.get("location")) == key(location) and is_present(entity)
            and type(entity.get("hp")) is int and entity["hp"] > 0)


def combat_context(state):
    combat = state.get("combat")
    scene = state.get("scene")
    if not isinstance(combat, dict) or not isinstance(scene, dict) or not state.get("hp"):
        return None
    location = scene.get("location")
    if not location or combat.get("location") != location:
        return None
    live = [enemy["id"] for enemy in encounter_roster(state, combat) if combatant_available(enemy, location)]
    if not live:
        return None
    target_id = combat.get("target_id")
    return combat if target_id in live else {**combat, "target_id": live[0]}


def combat_status(state, battle):
    location = battle["location"]
    return [{"id": row["id"], "name": row["name"],
             "status": ("active" if combatant_available(row, location) else
                        life_state(row) if life_state(row) != "alive" else "left"),
             "hp": row.get("hp"), "max_hp": row.get("max_hp")}
            for row in encounter_roster(state, battle)]


def start_encounter(state, enemies, roller, log):
    hero = roller(1, 20) + hero_stat(state, "dex") + check_bonus(state, "dex")
    initiative = [(None, hero, state.get("name") or "Герой")]
    for entity in enemies:
        bonus = max(0, profile(entity)["attack"] - 3) + check_bonus(state, "dex", entity["id"])
        initiative.append((entity["id"], roller(1, 20) + bonus, entity["name"]))
    initiative.sort(key=lambda actor: actor[1], reverse=True)
    scores = ", ".join(f"{name} {score}" for ident, score, name in initiative)
    log.append(f"Инициатива: {scores}. Первым действует «{initiative[0][2]}».")
    return {"target_id": enemies[0]["id"], "location": state["scene"]["location"], "round": 0,
            "enemy_ids": [enemy["id"] for enemy in enemies],
            "initiative": [actor[0] for actor in initiative], "enemy_cooldowns": {}}


def hostile(entity):
    return bool(entity.get("encounter_position") or re.match(
        r"^(?:hostile|враждеб|напада|атакует)", key(entity.get("status"))
    ))


def npc_turn(state, actor, enemy, guarding, enemy_guard, round_number, npc_cooldowns, roller):
    control = active_control(state, actor["id"])
    if control & {"sleep", "paralysis", "stun"}:
        return f"«{actor['name']}» не может действовать из-за активного эффекта."
    if enemy_guard:
        return f"«{actor['name']}» защищается и получает +2 к КБ."
    move = next((ability for ability in enemy["abilities"]
                 if cooldown_ready(npc_cooldowns, ability["name"], round_number)
                 and (not ability["cost"] or mana_for(actor, enemy["mana_pool"]) >= ability["cost"])
                 and ("silence" not in control or ability["element"] == "physical")), None)
    if move and (enemy["style"] in ("caster", "aggressive") or round_number % 2 == 0):
        spend_move(actor, move, npc_cooldowns, round_number, enemy["mana_pool"])
    else:
        move = {"name": f"Атака: {actor['name']}", "dice": enemy["dice"], "element": "physical"}
    stat = "int" if move["element"] != "physical" else "str"
    return strike(actor, state, move, enemy["attack"] + check_bonus(state, stat, actor["id"]),
                  hero_defense(state) + (2 if guarding else 0), {}, roller)


def award_victory(state, target):
    if target["hp"] == 0 and not target.get("combat_xp_paid"):
        threat = target.get("threat")
        xp = VICTORY_XP.get(threat if isinstance(threat, str) else "", VICTORY_XP["normal"])
        state["xp"] = bounded_int(state.get("xp"), 0, 2_000_000_000, 0) + xp
        target["combat_xp_paid"] = True


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


SHOT_INTENT = re.compile(r'\b(?:стреляю|выстреливаю|выпускаю(?:\s+\w+){0,5}\s+(?:стрелу|болт))\b', re.I)
SHOT_EVENT = re.compile(
    r'\b(?:стреляешь|выстреливаешь|промахиваешься|выпускаешь[^.!?]{0,70}\b(?:стрелу|болт))\b'
    r'|\b(?:стрела|болт)\s+(?:летит|попада\w*|угожда\w*|вреза\w*|срыва\w*|пролет\w*|проход\w*)\b', re.I
)


def narrated_weapon(state, action):
    for clause in re.split(r'[.!?;\n]', action):
        shot = SHOT_INTENT.search(clause)
        if shot and not re.search(r'\b(?:если|собираюсь|хочу)\b|\bне\s+(?:буду\s+)?$', clause[:shot.start()], re.I):
            action = clause
            break
    else:
        return None
    candidates = []
    for item in state.get('inventory', []):
        spec = item_spec(state, item)
        if not spec or not spec.get('ammo'):
            continue
        name = item.get('name') if isinstance(item, dict) else item
        words = re.findall(r'\w+', key(name))
        if words and all(len(word) >= 3 and re.search(r'\b' + re.escape(word[:max(3, len(word) - 2)]) + r'\w*\b', action, re.I)
                         for word in words):
            return spec
        candidates.append(spec)
    held = equipped(state, 'weapon')
    if held and held.get('ammo'):
        return held
    return candidates[0] if len(candidates) == 1 else None


def bind_ranged_weapon(state, check, weapon):
    """Use the bow's mechanics even when a target has no world record."""
    if not check or check.get('kind') != 'attack' or formula_key(check['expr'])[1] != 20:
        return None
    stat = weapon['stat']
    check.update(announced_expr=check['expr'], expr=f'1d20{2 + hero_stat(state, stat):+}',
                 stat=stat, effect_bonus=check_bonus(state, stat), ammo=weapon['ammo'])
    return check


def shot_cover(reply, target_name=""):
    """Recognize an explicit defense adjustment for the announced shot."""
    clean = re.sub(r'[*_`]', '', reply)
    adjustments = []
    for clause in re.split(r'[.!?;\n]', clean):
        normalized = key(clause)
        if re.search(r'\b(?:если|возможно|без укрытия)\b', clause, re.I):
            continue
        refers_to_target = re.search(r'\b(?:цел[ьи]|целям|противник\w*|ему|ей|его|е[её])\b', normalized)
        if not refers_to_target and (not target_name or key(target_name) not in normalized):
            continue
        if re.search(r'три четверти.*укрыти|укрыти\w* (?:на )?три четверти', clause, re.I):
            adjustments.append(5)
        elif re.search(r'половинн\w* укрыти|частичн\w* укрыти', clause, re.I):
            adjustments.append(2)
    return max(adjustments, default=0)


def narrated_ranged_check(state, weapon, action, reply, proposal):
    if not weapon:
        return None
    scene = state.get('scene') or {}
    local = [entity for entity in records(state) if entity.get('kind') in ('npc', 'creature')
             and key(entity.get('location')) == key(scene.get('location')) and is_present(entity)]
    proposed = proposal if isinstance(proposal, dict) else {}
    targets = []
    for entity in local:
        for name in entity_names(entity):
            words = set(re.findall(r'\w{3,}', key(name)))
            if (key(name) == key(proposed.get('target')) or key(name) in key(action)
                    or (words and all(re.search(r'\b' + re.escape(word[:max(3, len(word) - 2)]) + r'\w*\b',
                                                action, re.I) for word in words))):
                targets.append(entity)
                break
    if len(targets) != 1 and (position := encounter_position(action)):
        targets = [entity for entity in local if entity.get('encounter_position') == position]
    if len(targets) != 1:
        clean = re.sub(r'[*_`]', '', reply)
        targets = [entity for entity in local if any(re.search(r'\b' + re.escape(key(name)) + r'\b',
                   clean.casefold().replace('ё', 'е')) for name in entity_names(entity))]
    if len(targets) != 1:
        # A generic target can be resolved only when one living opponent matches.
        reference = re.search(r'\b(?:в|по)\s+(?:\w+\s+){0,2}(гоблин|голем|волк|волк[ау])\w*\b', action, re.I)
        matching = [entity for entity in local if reference and reference[1].casefold() in key(entity['name'])]
        targets = matching if len(matching) == 1 else []
    if len(targets) != 1:
        return None
    target, = targets
    spec = profile(target)
    check = pending_check(reply, {'kind': 'attack', 'title': 'Выстрел', 'target': target['name']},
                          scene, defense_dc=spec['defense'] + shot_cover(reply, target['name']))
    if not check or formula_key(check["expr"])[1] != 20:
        return None
    combat_ready(target, 'normal')
    if type(target.get('hp')) is not int:
        return None
    bind_ranged_weapon(state, check, weapon)
    check.update(world_target_id=target['id'], ranged_dice=weapon['dice'], ranged_element=weapon['element'])
    return check


def combat_turn(original, command, argument, roller=None):
    roller = roller or randint
    state = deepcopy(original)
    if hero_incapacitated(state): raise ValueError("Герой не может действовать из-за активного эффекта.")
    combat = combat_context(state)
    target_arg = argument
    extra_targets = []
    if command == "/attack" and "|" in argument:
        target_arg, *extra_targets = [part.strip() for part in argument.split("|")]
        if len(extra_targets) >= MAX_ENEMIES or not all([target_arg, *extra_targets]):
            raise ValueError(f"Укажите от 1 до {MAX_ENEMIES} целей, разделённых |.")
        if combat:
            raise ValueError("В активном бою указывайте одну цель из списка.")
    if command == "/cast":
        ability_name, separator, target_arg = argument.partition("|")
        ability_name, target_arg = ability_name.strip(), target_arg.strip() if separator else ""
        if not ability_name:
            raise ValueError("Укажите /cast способность | цель.")
    if command == "/retreat":
        if not combat:
            raise ValueError("Сейчас нет активного боя.")
        die = roller(1, 20)
        modifier = hero_stat(state, "dex") + check_bonus(state,"dex")
        difficulty = max(profile(enemy)["defense"] for enemy in encounter_roster(state, combat)
                         if combatant_available(enemy, combat["location"]))
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
    combat_ready(target, "normal")
    location = state["scene"]["location"]
    if not combatant_available(target, location):
        raise ValueError("Этот противник недоступен для боя.")
    log = [retreat] if retreat else []
    if combat:
        enemies = encounter_roster(state, combat)
        if target["id"] not in {entity["id"] for entity in enemies}:
            raise ValueError("Выберите участника текущего боя или попробуйте отступить.")
        if "enemy_ids" not in combat:
            # Active Alpha 4 saves already have a fixed initiative and cooldowns.
            ident = target["id"]
            combat.update(enemy_ids=[ident], initiative=[None, ident] if combat.get("hero_first") else [ident, None],
                          enemy_cooldowns={ident: combat.get("npc_cooldowns") or {}})
    else:
        selected = [find_combatant(state, reference) for reference in extra_targets]
        if not extra_targets and hostile(target):
            selected = [entity for entity in records(state) if entity["id"] != target["id"]
                        and entity.get("kind") in ("npc", "creature") and hostile(entity)
                        and key(entity.get("location")) == key(location) and is_present(entity)][:MAX_ENEMIES - 1]
        enemies = list({entity["id"]: entity for entity in [target, *selected]}.values())
        for entity in enemies:
            combat_ready(entity, "normal")
            if extra_targets and not combatant_available(entity, location):
                raise ValueError(f"«{entity['name']}» недоступен для боя.")
        enemies = [entity for entity in enemies if combatant_available(entity, location)]
        combat = start_encounter(state, enemies, roller, log)
    combat["target_id"] = target["id"]
    active_enemies = {entity["id"]: entity for entity in enemies if combatant_available(entity, location)}
    if any(not bounded_int(entity.get("max_hp"), entity["hp"], 2_000_000_000, 0)
           for entity in active_enemies.values()):
        raise ValueError("У противника повреждена запись здоровья.")
    profiles = {ident: profile(entity) for ident, entity in active_enemies.items()}
    for ident, entity in active_enemies.items():
        entity["combat_profile"] = profiles[ident]
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
        if spec:
            move.update(spec)
            if move.get("ammo"):
                require_ammo(state, move["ammo"])
    log.append(f"Раунд {round_number}.")
    cooldowns_by_enemy = combat.get("enemy_cooldowns")
    if not isinstance(cooldowns_by_enemy, dict):
        cooldowns_by_enemy = combat["enemy_cooldowns"] = {}
    guards = {ident for ident, entity in active_enemies.items()
              if profiles[ident]["style"] == "defensive" and 3 * entity["hp"] <= entity["max_hp"]
              and round_number % 2 == 0}
    saved_order = combat.get("initiative")
    saved_order = saved_order[:MAX_ENEMIES + 1] if isinstance(saved_order, list) else []
    actors = [ident for ident in saved_order if ident is None or isinstance(ident, str)]
    order = dict.fromkeys([*actors, None, *active_enemies])
    for ident in order:
        if state["hp"] <= 0:
            break
        if ident is None:
            if guarding:
                continue
            if prepared is not None:
                state.update({field: prepared[field] for field in ("abilities", "mana", "max_mana") if field in prepared})
                state["ability_cooldowns"] = ready_cooldowns
            if move.get("ammo"):
                log.append(consume_ammo(state, move["ammo"]))
            stat = state.get("casting_stat", "int") if command == "/cast" else move.get("stat", "str")
            enemy = profiles[target["id"]]
            log.append(strike(state, target, move, 2 + hero_stat(state, stat) + check_bonus(state, stat),
                              enemy["defense"] + (2 if target["id"] in guards else 0),
                              enemy["resistances"], roller))
            award_victory(state, target)
            continue
        actor = active_enemies.get(ident)
        if not actor or not combatant_available(actor, location):
            continue
        npc_cooldowns = cooldowns_by_enemy.get(ident)
        if not isinstance(npc_cooldowns, dict):
            npc_cooldowns = cooldowns_by_enemy[ident] = {}
        log.append(npc_turn(state, actor, profiles[ident], guarding, ident in guards,
                            round_number, npc_cooldowns, roller))
    advance_action(state, 6)
    state["combat"] = combat
    state["combat"] = combat_context(state)
    if not state["combat"]:
        log.append("Бой завершён.")
    elif target["hp"] == 0:
        log.append("Раунд завершён. В бою остаются другие противники.")
    return state, "\n".join(log)


COMBAT_PROMPT = """
У существ и NPC при вступлении в бой указывай threat и combat_profile:{style,resistances,abilities}. style: balanced/aggressive/defensive/caster. resistances: ключи physical/fire/cold/lightning/poison/arcane/radiant/shadow со значением resistant/immune/vulnerable. abilities: [{name,dice,element,cost,cooldown,hits,automatic}]. cost в мане, cooldown в раундах. Эти параметры проверяет сервер: сильный суммарный урон требует затрат маны и перезарядки, учитывается уровень героя или угроза существа. ability_cooldowns — оставшиеся боевые раунды, 0 означает готовность. Не объявляй применение недоступной способности. Для уже изученной способности героя можно передать такой же объект в abilities[].mechanics. При обычной атаке NPC объявляй имя атакующего и одну формулу, DC атаки по герою определит сервер. Результаты combat и rule_effect считай готовыми фактами. В combat.enemies перечислены участники текущего боя. Учитывай состояние каждого по его ID. После победы над одной целью не объявляй завершение, пока в списке есть active. Для подтверждённого бегства запиши status «сбежал» и дословную фразу о событии в world_entities. Сохрани HP беглеца.
"""
