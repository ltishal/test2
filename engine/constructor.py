"""Campaign-local recipes evaluated by the existing rule engines."""
from copy import deepcopy
import re
from uuid import uuid4

from engine.combat import bounded_int, safe_ability
from engine.equipment import equipment_spec
from engine.world import key, text

TYPES = {'item': 'Предмет', 'race': 'Раса', 'class': 'Класс',
         'spell': 'Заклинание', 'ability': 'Способность'}
STATS = ('str', 'dex', 'con', 'int', 'wis', 'cha')
RECIPE_LIMIT = 180
LEARNED_SPELL = re.compile(
    r'\b(?:изучил[аи]?|освоил[аи]?|изучает|осваивает)\s+[*_`]*[«"]([^»"\n]{3,100})[»"][*_`]*', re.I
)
NUMBER_WORDS = {'одна': 1, 'одну': 1, 'один': 1, 'две': 2, 'два': 2, 'три': 3,
                'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8}
NUMBER_TEXT = r'(?:[0-9]{1,2}|' + '|'.join(NUMBER_WORDS) + ')'
ELEMENT_NAMES = {'огн': 'fire', 'холод': 'cold', 'молни': 'lightning', 'ядом': 'poison',
                 'магией': 'arcane', 'светом': 'radiant', 'тьмой': 'shadow', 'физическ': 'physical'}


def recipe_matches(state, reference):
    value = key(reference)
    entries = recipes(state)
    exact = [row for row in entries if row['id'] == reference or key(row['name']) == value]
    return exact or [row for row in entries if names_match(row['name'], reference)]


def names_match(left, right):
    a, b = key(left).split(), key(right).split()
    return bool(a and b and a[1:] == b[1:] and (a[0] == b[0] or
                (len(a[0]) >= 4 and len(b[0]) >= 4 and a[0][-1] in 'аяую'
                 and b[0][-1] in 'аяую' and a[0][:-1] == b[0][:-1])))


def recipes(state):
    raw = state.get('constructor')
    return [row for row in raw if isinstance(row, dict) and isinstance(row.get('id'), str)
            and isinstance(row.get('kind'), str) and row['kind'] in TYPES and text(row.get('name'), 100)] if isinstance(raw, list) else []


def attribute_bonuses(raw):
    if not isinstance(raw, dict):
        return {}
    return {stat: score for stat in STATS
            if type(score := raw.get(stat)) is int and 1 <= score <= 2}


def validate_blueprint(proposal, level):
    name = text(proposal.get('name'), 100)
    kind = proposal.get('kind')
    if not name or not isinstance(kind, str) or kind not in TYPES:
        raise ValueError('У проекта нужны известный тип и название.')
    raw = proposal.get('mechanics')
    raw = raw if isinstance(raw, dict) else {}
    description = text(proposal.get('description'), 450)
    if kind == 'item':
        mechanics = equipment_spec(raw) if raw else None
        if raw and not mechanics:
            raise ValueError('Характеристики оружия или брони выходят за пределы конструктора.')
    elif kind in ('spell', 'ability'):
        mechanics = safe_ability({**raw, 'name': name}, power=level)
        if not mechanics:
            raise ValueError('Формула, число попаданий или стоимость способности выходят за допустимые пределы.')
        if kind == 'spell' and mechanics['element'] != 'physical' and mechanics['cost'] == 0:
            mechanics['cost'] = 1
    else:
        bonuses = attribute_bonuses(raw.get('bonuses'))
        if sum(bonuses.values()) > (3 if kind == 'race' else 2):
            raise ValueError('Сумма бонусов характеристик слишком велика.')
        mechanics = {'bonuses': bonuses, 'trait': text(raw.get('trait'), 180)}
        if kind == 'class':
            hp_die = raw.get('hp_die', 8)
            stat = raw.get('casting_stat', 'int')
            if hp_die not in (6, 8, 10, 12) or type(hp_die) is not int or stat not in ('int', 'wis', 'cha'):
                raise ValueError('Класс требует кость здоровья d6–d12 и характеристику int, wis или cha.')
            mechanics.update(hp_die=hp_die, casting_stat=stat)
    return {'kind': kind, 'name': name, 'description': description, 'mechanics': mechanics}


def apply_blueprints(state, proposals, reply):
    catalog = deepcopy(recipes(state))
    reports = []
    level = bounded_int(state.get('level'), 1, 20, 1)
    for proposal in proposals[:8] if isinstance(proposals, list) else []:
        if not isinstance(proposal, dict):
            continue
        evidence = text(proposal.get('evidence'), 600)
        if len(evidence) < 5 or evidence not in reply:
            continue
        try:
            recipe = validate_blueprint(proposal, level)
            quoted = re.findall(r'[«“"]([^«“»”"\n]{3,100})[»”"]', evidence)
            normalized = " ".join(evidence.casefold().replace("ё", "е").split())
            if key(recipe['name']) not in normalized and not any(names_match(recipe['name'], name) for name in quoted):
                raise ValueError('Название проекта отсутствует в подтверждённой фразе.')
            existing = next((entry for entry in catalog if entry['kind'] == recipe['kind']
                             and names_match(entry['name'], recipe['name'])), None)
            if existing:
                continue
            if len(catalog) >= RECIPE_LIMIT:
                raise ValueError('Список проектов этой партии заполнен.')
            recipe['id'] = uuid4().hex
            catalog.append(recipe)
            reports.append(f"Проект записан: {recipe['name']}.")
        except ValueError as error:
            reports.append(f"Проект отклонён: {text(proposal.get('name'), 100) or 'без названия'}. {error}")
    state['constructor'] = catalog
    return reports


def apply_character_blueprints(state):
    """Apply custom starting traits once, while preserving older sheets."""
    chosen = {}
    for kind in ('race', 'class'):
        match = next((entry for entry in recipes(state) if entry['kind'] == kind
                      and key(entry['name']) == key(state.get(kind))), None)
        if match:
            chosen[kind] = match
    if not chosen or state.get('character_blueprints'):
        return
    stats = state.get('stats')
    stats = dict(stats) if isinstance(stats, dict) else {}
    for entry in chosen.values():
        mechanics = entry.get('mechanics') or {}
        for stat, bonus in attribute_bonuses(mechanics.get('bonuses')).items():
            stats[stat] = min(20, bounded_int(stats.get(stat), 1, 30, 10) + bonus)
    state['stats'] = stats
    if archetype := chosen.get('class'):
        mechanics = archetype['mechanics']
        state['casting_stat'] = mechanics['casting_stat']
        hp = max(1, mechanics['hp_die'] + (bounded_int(stats.get('con'), 1, 30, 10) - 10) // 2)
        state.update(hp=hp, max_hp=hp)
    state['character_blueprints'] = {kind: recipe['id'] for kind, recipe in chosen.items()}


def learn_blueprint(state, reference):
    matches = recipe_matches(state, reference)
    if len(matches) != 1 or matches[0]['kind'] not in ('spell', 'ability'):
        raise ValueError('Укажите точное имя или ID одного заклинания или умения из /design.')
    recipe = matches[0]
    owned = state.get('abilities')
    owned = owned if isinstance(owned, list) else []
    if any(isinstance(card, dict) and key(card.get('name')) == key(recipe['name']) for card in owned):
        raise ValueError('Герой уже знает эту способность.')
    if sum(isinstance(card, dict) and bool(card.get('recipe_id')) for card in owned) >= 2 + bounded_int(state.get('level'), 1, 20, 1):
        raise ValueError('Для текущего уровня все места для изучаемых проектов уже заняты.')
    mechanics = safe_ability(recipe.get('mechanics'), power=bounded_int(state.get('level'), 1, 20, 1))
    if not mechanics:
        raise ValueError('Правила способности пока слишком сильны для героя.')
    state['abilities'] = [*owned, {'name': recipe['name'], 'description': recipe['description'],
                                  'recipe_id': recipe['id'], 'mechanics': mechanics, 'recharge': 'unlimited'}]
    return f"Изучено: {recipe['name']}."


def attach_item_blueprints(state):
    item_recipes = {key(row['name']): row for row in recipes(state) if row['kind'] == 'item'}
    inventory = state.get('inventory')
    if not isinstance(inventory, list) or not item_recipes:
        return
    changed = []
    for item in inventory:
        entry = item if isinstance(item, dict) else {'name': item}
        recipe = item_recipes.get(key(entry.get('name')))
        if recipe and recipe.get('mechanics') and (not entry.get('recipe_id') or entry.get('recipe_id') == recipe['id']):
            changed.append({**entry, 'recipe_id': recipe['id'], 'mechanics': recipe['mechanics']})
        else:
            changed.append(item)
    state['inventory'] = changed


def narrated_spell_mechanics(value):
    clean = re.sub(r'[*_`]', '', value)
    formulae = re.findall(r'\b([1-6]d[0-9]{1,2}(?:[+-][0-9]{1,2})?)\s+(?:урона\s+)?([а-яё]+)', clean, re.I)
    costs = re.findall(rf'\b({NUMBER_TEXT})\s+(?:единиц\w*\s+)?ман[ыуа]\b', clean, re.I)
    if len(set(formulae)) != 1 or len(set(costs)) != 1:
        return None
    dice, damage = formulae[0]
    element = next((kind for stem, kind in ELEMENT_NAMES.items() if damage.casefold().startswith(stem)), None)
    if not element:
        return None
    cooldown = re.search(rf'\b(?:восстановление|перезарядка|перезаряжается за)\s*[:—–-]?\s*({NUMBER_TEXT})\s+раунд', clean, re.I)

    def parse_count(value):
        value = value.casefold()
        return int(value) if value.isdigit() else NUMBER_WORDS[value]

    return {'dice': dice, 'element': element, 'cost': parse_count(costs[0]),
            'cooldown': parse_count(cooldown[1]) if cooldown else 0}


def learned_spell_from_story(reply, hero_name=None):
    """Recover a completed lesson with an explicit hero, formula and cost."""
    heroes = ['ты']
    if key(hero_name):
        heroes.append(re.escape(key(hero_name)))
    subject = re.compile(r'(?:^|\s)(?:' + '|'.join(heroes) + r')\b', re.I)
    learned = []
    for match in LEARNED_SPELL.finditer(reply):
        prefix = re.split(r'[.!?;:\n«»"—]', reply[:match.start()])[-1]
        prefix = re.sub(r'[*_`]', '', prefix).casefold().replace('ё', 'е').strip()
        if not subject.match(prefix) or re.search(r'\b(?:не|если|бы|мож\w*|говор\w*|сказ\w*)\b', prefix, re.I):
            continue
        tail = re.split(r'[.!?\n]', reply[match.end():match.end()+220].lstrip(' :—–'), 1)[0]
        mechanics = narrated_spell_mechanics(tail)
        if mechanics:
            learned.append({'kind': 'spell', 'name': match[1], 'description': 'Изучено в ходе приключения.',
                            'mechanics': mechanics, 'evidence': match[0]})
    if learned:
        return learned

    # An instructor may dictate the formula first, then confirm a single taught spell.
    evidence = reply[-600:].strip()
    if not re.search(r'\bурок\s+заверш[её]н\b|\bзаклинание\s+изучено\b', evidence, re.I):
        return []
    if re.search(r'\b(?:не изучен\w*|не выучен\w*|не заверш[её]н\w*|если (?:ты|урок))\b', evidence, re.I):
        return []
    named = re.findall(r'\b(?:теперь\s+)?ты\s+знаешь\s+[«“"]([^»”"\n]{3,100})[»”"]', evidence, re.I)
    if len(set(named)) != 1:
        return []
    mechanics = narrated_spell_mechanics(evidence)
    if not mechanics:
        return []
    return [{'kind': 'spell', 'name': named[0], 'description': 'Изучено в ходе приключения.',
             'mechanics': mechanics, 'evidence': evidence}]


def attach_learned_spell(state, proposals):
    for proposed in proposals:
        recipe = next((row for row in recipe_matches(state, proposed['name']) if row['kind'] == 'spell'), None)
        if not recipe:
            continue
        owned = state.get('abilities') or []
        cards = [card for card in owned if isinstance(card, dict) and names_match(card.get('name'), recipe['name'])]
        card = cards[0] if len(cards) == 1 else None
        if card is not None:
            if not card.get('mechanics'):
                card.update(mechanics=recipe['mechanics'], recipe_id=recipe['id'])
        else:
            try:
                learn_blueprint(state, recipe['id'])
            except ValueError:
                pass


def constructor_context(state, query):
    entries = recipes(state)
    mentioned = key(query)
    ordered = sorted(reversed(entries), key=lambda row: key(row['name']) not in mentioned)
    return [{field: row[field] for field in ('id', 'kind', 'name', 'mechanics') if field in row}
            for row in ordered[:8]]


CONSTRUCTOR_PROMPT = """
КОНСТРУКТОР. Для новых проектов, созданных или изучаемых в этой сцене, верни blueprints:[{kind,name,description,mechanics,evidence}]. evidence — дословная фраза с полным названием. kind item: mechanics оружия {slot:weapon,dice,element,stat} или брони {slot:armor,defense,dex_cap}; для обычного предмета опусти mechanics. kind spell/ability: mechanics {dice,element,cost,cooldown,hits,automatic}; числа проверяет сервер. kind race/class: mechanics {bonuses:{str/dex/con/int/wis/cha:1..2},trait}; для class также hp_die (6/8/10/12) и casting_stat (int/wis/cha). При создании героя с авторской расой или классом указывай характеристики до бонусов конструктора. Проект хранится отдельно: заклинание можно изучить через /learn, предмет можно получить в истории и экипировать. Проект не добавляет вещи в инвентарь автоматически. Используй сохранённые проекты из constructor; не переопределяй уже утверждённые правила.
"""
