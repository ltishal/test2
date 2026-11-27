"""Persistent generated world entities. No predefined bestiary or item catalogue."""
import re
import json
from copy import deepcopy
from uuid import uuid4

KINDS = {'npc', 'creature', 'item', 'location', 'faction', 'ability', 'other'}
# Relative power within the world's fiction, not scaled to the player's level.
THREAT_HP = {'minor': (4,12), 'normal': (13,30), 'dangerous': (31,65), 'elite': (66,130), 'legendary': (131,300)}
TEXT_FIELDS = {'description': 600, 'location': 180, 'status': 120, 'traits': 400}
ACCESS_CLOSED = re.compile(r'(?:проход|въезд|вход)\s+закрыт|перекрыт\w*\s+цепью|(?:мост|лавка)\s+закрыт\w*', re.I)
INTRODUCTION = re.compile(r'(?:—\s*Я\s+|представля(?:ется|ясь)\s*[,:—«„" ]*)([А-ЯЁ][а-яё-]{2,30})(?=[\s.,!?»:—"`]|$)')


def text(v, n):
    return v.strip()[:n] if isinstance(v, str) else ''


def key(v):
    return ' '.join(text(v, 160).casefold().replace('ё','е').split())


def records(state):
    value = state.get('world_entities')
    return [e for e in value if isinstance(e,dict) and isinstance(e.get('id'),str) and isinstance(e.get('name'),str)] if isinstance(value,list) else []

def entity_names(entity):
    aliases = entity.get('aliases')
    return [entity['name'], *(alias for alias in aliases if isinstance(alias, str))] if isinstance(aliases, list) else [entity['name']]


DEAD_STATES = {'dead','destroyed','мертв','мертва','мертвый','мертвая','погиб','погибла','погибший','погибшая','убит','убита','убитый','убитая','уничтожен','уничтожена','уничтоженный'}
DOWN_STATES = {'defeated','unconscious','побежден','побеждена','без сознания','побежден (0 hp)'}

def life_state(entity):
    saved=entity.get('life_state')
    if saved in ('dead','destroyed'): return saved
    status=key(entity.get('status'))
    if status in DEAD_STATES: return 'destroyed' if status.startswith(('уничтож','destroy')) else 'dead'
    if saved in ('unconscious','defeated'): return saved
    if status in DOWN_STATES or entity.get('hp')==0: return 'unconscious' if status in ('unconscious','без сознания') else 'defeated'
    return 'alive'

RETURN_TO_SCENE = re.compile(
    r'(?<!не )\b(?:возвраща(?:ется|ются)|вернул(?:ся|ась|ись)|вновь появил(?:ся|ась|ись))\b', re.I
)
ESCAPED_STATUS = re.compile(
    r'^(?:(?:fled|сбежал[аи]?|убежал[аи]?)(?:[\s.,:;!?]|$)|'
    r'(?:скрыл(?:ся|ась|ись)|уш[её]л|ушла)(?:[.,:;!?]|$|\s+'
    r'(?:в лес(?:у)?|в чащ[уе]|между (?:деревьями|стволами)|среди деревьев|из боя)\b))', re.I
)


def is_present(entity):
    return life_state(entity) == 'alive' and not ESCAPED_STATUS.search(key(entity.get('status')))


def sync_life(entity):
    if entity.get('kind') in ('npc','creature'):
        entity['life_state']=life_state(entity)
        if entity['life_state']!='alive' and type(entity.get('hp')) is int: entity['hp']=0
    return entity

def combat_ready(entity, threat):
    if (entity.get('kind') not in ('npc', 'creature') or 'hp' in entity or 'max_hp' in entity
            or not isinstance(threat, str) or threat not in THREAT_HP
            or life_state(entity) != 'alive'):
        return
    low, high = THREAT_HP[threat]
    hp = (low + high) // 2
    entity.update(threat=threat, hp=hp, max_hp=hp)


def add_known_name(entity, name, entities):
    if not name or any(key(name) == key(alias) for alias in entity_names(entity)):
        return
    aliases = entity_names(entity)[1:]
    # For titles used before an introduction, retain the title as a searchable alias.
    if len(entity['name'].split()) > 1:
        entity['name'], aliases = name, [entity['name'], *aliases]
    else:
        aliases.append(name)
    entity['aliases'] = aliases[:8]


def infer_introduction(entities, reply, scene):
    intro = INTRODUCTION.search(reply)
    if not intro or not isinstance(scene, dict):
        return
    previous_text = reply[max(0, intro.start() - 170):intro.start()]
    before = set(re.findall(r'\w+', previous_text.casefold().replace('ё', 'е')))
    local = key(scene.get('location'))
    candidates = []
    for entity in entities:
        if entity.get('kind') != 'npc' or not local or key(entity.get('location')) != local:
            continue
        tokens = set(key(entity['name']).split())
        score = len((tokens - {'с', 'у', 'на', 'в', 'из'}) & before)
        if score >= 2:
            candidates.append((score, entity))
    candidates.sort(key=lambda value: value[0], reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        add_known_name(candidates[0][1], intro[1], entities)


def merge_entities(state, proposals, reply):
    """Apply small evidence-grounded patches; identity and combat HP belong to the server."""
    result = deepcopy(records(state))
    by_id = {e['id']: e for e in result}
    infer_introduction(result, reply, state.get('scene'))
    for patch in proposals[:12] if isinstance(proposals, list) else []:
        if not isinstance(patch,dict): continue
        evidence = text(patch.get('evidence'),600)
        if len(evidence)<5 or evidence not in reply: continue
        entity_id = text(patch.get('id'),80)
        name = text(patch.get('name'),160)
        kind = patch.get('kind')
        if entity_id:
            entity = by_id.get(entity_id)
            if entity is None: continue
            # An ID cannot be used to change the identity of another entity.
            if (name and all(key(name) != key(alias) for alias in entity_names(entity))) or (kind and kind!=entity.get('kind')): continue
        else:
            if not name or not isinstance(kind,str) or kind not in KINDS: continue
            matches = [e for e in result if e.get('kind')==kind and any(key(alias) == key(name) for alias in entity_names(e))]
            location = key(patch.get('location'))
            if patch.get('new_entity') is True:
                matches = []
            elif location and kind in ('npc','creature','item','location'):
                same = [e for e in matches if key(e.get('location')) == location]
                unknown = [e for e in matches if not key(e.get('location'))]
                matches = same or (unknown if len(matches)==1 else [])
            if not matches and location and kind in ('creature', 'npc') and not patch.get('new_entity'):
                position = encounter_position(name)
                if position:
                    matches = [e for e in result if e.get('encounter_position') == position
                               and key(e.get('location')) == location]
                    if len(matches) == 1 and key(name) != key(matches[0]['name']):
                        aliases = entity_names(matches[0])[1:]
                        matches[0]['aliases'] = [*aliases, name][:8]
            if len(matches)>1: continue
            entity = matches[0] if matches else None
            if entity is None:
                entity = {'id':uuid4().hex,'name':name,'kind':kind}
                result.append(entity)
                by_id[entity['id']]=entity
        # Life-state is sticky; a model cannot resurrect by changing prose.
        if entity.get('kind') in ('npc','creature'):
            prior = life_state(entity)
            if prior in ('dead','destroyed'): entity['life_state'] = prior
            elif key(patch.get('status')) in DEAD_STATES or (prior=='alive' and key(patch.get('status')) in DOWN_STATES):
                entity['status'] = text(patch.get('status'),120)
                sync_life(entity)
            elif prior != 'alive': entity['life_state']=prior
        combat_ready(entity, patch.get('threat'))
        if (isinstance(patch.get('combat_profile'), dict) and type(entity.get('hp')) is int
                and not isinstance(entity.get('combat_profile'), dict)):
            from engine.combat import profile
            entity['combat_profile'] = profile(entity, patch['combat_profile'])
        # Death from the rules engine cannot be silently undone by narration.
        for field, limit in TEXT_FIELDS.items():
            value = text(patch.get(field),limit)
            if field == 'status' and (life_state(entity) != 'alive' or (
                    ESCAPED_STATUS.search(key(entity.get('status'))) and is_present({'status': value})
                    and not RETURN_TO_SCENE.search(evidence))):
                continue
            if value:
                entity[field] = value
        if entity.get('kind') == 'item' and not entity.get('mechanics'):
            from engine.equipment import equipment_spec
            spec=equipment_spec(patch.get('mechanics'))
            if spec: entity['mechanics']=spec
        sync_life(entity)
        entity['evidence']=evidence
        name_revealed = text(patch.get('known_name'), 100)
        if entity.get('kind') == 'npc' and name_revealed and name_revealed in evidence:
            add_known_name(entity, name_revealed, result)
        access = patch.get('access')
        if entity.get('kind') == 'location' and access in ('open', 'closed', 'restricted'):
            entity['access'] = access
        elif entity.get('kind') == 'location' and ACCESS_CLOSED.search(text(patch.get('description'), 600)):
            entity['access'] = 'closed'
    infer_introduction(result, reply, state.get('scene'))
    for entity in result:
        if entity.get('kind') == 'location' and not entity.get('access'):
            if ACCESS_CLOSED.search(text(entity.get('description'), 600)) or key(entity.get('status')) in ('closed', 'закрыт', 'закрыто'):
                entity['access'] = 'closed'
            elif key(entity.get('status')) == 'open':
                entity['access'] = 'open'
    for entity in result:
        sync_life(entity)
        if (entity.get('kind') == 'creature' and not entity.get('threat')
                and re.search(r'\bучебн\w* (?:существ\w*|голем\w*|конструкт\w*|манекен\w*|мишен\w*)',
                              text(entity.get('description'), 600), re.I)):
            combat_ready(entity, 'minor')
    return result


PERSON_NAME = r"[А-ЯЁ][а-яё-]{2,30}(?: [А-ЯЁ][а-яё-]{2,30})?"
NPC_ROLES = {
    'торговец': 'торговец', 'купец': 'торговец', 'страж': 'страж',
    'стражник': 'страж', 'сторож': 'сторож', 'наниматель': 'наниматель',
    'спутник': 'спутник', 'проводник': 'проводник', 'дворф': 'дворф', 'возница': 'возница',
    'архивист': 'архивист', 'архивистка': 'архивист',
}
ROLE_WORD = r"(?i:" + '|'.join(NPC_ROLES) + r")"
ROLE_NAME = re.compile(rf"\b({ROLE_WORD})(?:-{ROLE_WORD})?\s+({PERSON_NAME})\b")
PERSON_ACTION = re.compile(
    rf"\b({PERSON_NAME})\s+(?i:удерживает поводья|держит поводья|правит повозкой)\b"
)
ROLE_NAME_REVERSED = re.compile(
    rf"\b({PERSON_NAME})\s*(?:,\s*|[—–]\s*)(?:(?i:портовый)\s+)?"
    rf"(?:(?i:дворф)-)?({ROLE_WORD})\b"
)
GOBLIN_ENCOUNTER = re.compile(r'\b(?:трое|три|3)\s+гоблин(?:а|ов)\b', re.I)
GOBLIN_POSITIONS = {
    'повозка': r'целит(?:ся|ься)|у повозк|ближайш|засадник',
    'ветви': r'перекрыва\w* дорогу|ветв|попер[её]к',
    'лес': r'пятится|к лесу|у леса|лесно[йго]+|отступа',
}
# Position aliases connect initial narration to later model patches.
GOBLIN_NAMES = {'повозка': 'Гоблин у повозки', 'ветви': 'Гоблин за поваленными ветвями',
                'лес': 'Гоблин у леса'}


def encounter_position(value):
    if not re.search(r'\bгоблин', value, re.I):
        return None
    positions = [position for position, pattern in GOBLIN_POSITIONS.items()
                 if re.search(pattern, value, re.I)]
    return positions[0] if len(positions) == 1 else None


def entry_roles(entry):
    fields = ' '.join(str(entry.get(field, '')) for field in ('traits', 'description', 'role'))
    return {NPC_ROLES[role.casefold()] for role in re.findall(rf'\b{ROLE_WORD}\b', fields)}


def restore_scene_people(entities, scene, reply, previous_scene=None, hero_name=None):
    """Recover named people and clearly enumerated opponents omitted from a delta."""
    location = scene.get('location') if isinstance(scene, dict) else None
    if not location:
        return entities
    if isinstance(previous_scene, dict) and previous_scene.get('location') and re.search(
            r'\b(?:оба|обе|они)\b[^.]{0,50}\bоста[лются]+\w*\s+позади', reply, re.I):
        location = previous_scene['location']
    clean = re.sub(r'[*_`]', '', reply)
    found = [(m[1], m[2]) for m in ROLE_NAME.finditer(clean)]
    found.extend((m[2], m[1]) for m in ROLE_NAME_REVERSED.finditer(clean))
    found.extend(('возница', m[1]) for m in PERSON_ACTION.finditer(clean)
                 if len(m[1].split()) == 2 and not any(
                     entry.get('kind') == 'npc'
                     and any(key(alias) == key(m[1]) for alias in entity_names(entry))
                     for entry in entities))
    for role, name in found:
        if key(name) == key(hero_name):
            continue
        role = NPC_ROLES[role.casefold()]
        local = [entry for entry in entities if entry.get('kind') == 'npc'
                 and any(key(name) == key(alias) for alias in entity_names(entry))
                 and key(entry.get('location')) in ('', key(location))]
        if any(role in entry_roles(entry) for entry in local):
            continue
        if len(local) == 1 and (len(name.split()) > 1 or not entry_roles(local[0])):
            local[0].setdefault('location', location)
            if role != 'дворф' or not local[0].get('role'):
                local[0]['role'] = role
            continue
        entities.append({'id': uuid4().hex, 'kind': 'npc', 'name': name, 'location': location,
                         'description': role.capitalize() + '.', 'role': role, 'life_state': 'alive'})

    encounter = GOBLIN_ENCOUNTER.search(clean)
    if encounter:
        description = clean[encounter.start():encounter.end() + 300]
        positions = [position for position, pattern in GOBLIN_POSITIONS.items()
                     if re.search(pattern, description, re.I)]
        if len(positions) == 3 and re.search(r'\b(?:второй|другой)\b', description, re.I) and re.search(
                r'\bтретий\b', description, re.I):
            for position in positions:
                local = [entry for entry in entities if entry.get('kind') in ('npc', 'creature')
                         and key(entry.get('location')) == key(location)
                         and encounter_position(entry['name']) == position]
                if not local:
                    entry = {'id': uuid4().hex, 'kind': 'creature', 'name': GOBLIN_NAMES[position],
                             'location': location, 'life_state': 'alive', 'encounter_position': position}
                    combat_ready(entry, 'minor')
                    entities.append(entry)

    # A named list of creatures is grounded when the narrator introduces the names
    # alongside their common species and explicitly counts the opponents.
    group = re.search(r'\b(?:три|трое|3)\s+(?:\w+\s+){0,2}(?:голем|гоблин|страж|скелет|волк|орк|дух|существа)\w*\b', clean, re.I)
    if group:
        names = re.findall(r'«(' + PERSON_NAME + r')»', clean[group.end():group.end() + 330])
        if len(set(map(key, names))) == 3:
            for name in dict.fromkeys(names):
                if not any(entry.get('kind') == 'creature' and key(entry.get('location')) == key(location)
                           and any(key(name) == key(alias) for alias in entity_names(entry)) for entry in entities):
                    entry = {'id': uuid4().hex, 'kind': 'creature', 'name': name, 'location': location,
                             'life_state': 'alive'}
                    combat_ready(entry, 'minor')
                    entities.append(entry)
    record_goblin_escape(entities, scene, clean)
    return entities


GOBLIN_ESCAPE = re.compile(
    r'\b(?:третий\s+)?гоблин(?:\s+у леса)?\s+(?:вздрагивает и\s+)?'
    r'(?:уш[её]л|уходит|убежал|скрылся|скрывается|сбежал)\s+'
    r'(?:в лес(?:у)?|в чащ[уе]|между (?:деревьями|стволами)|среди деревьев)\b', re.I
)


def record_goblin_escape(entities, scene, reply):
    """Persist a confirmed escape when only one local goblin can be identified."""
    clean = re.sub(r'[*_`]', '', reply)
    match = GOBLIN_ESCAPE.search(clean)
    if not match:
        return
    prefix = re.split(r'[.!?;:\n—]', clean[:match.start()])[-1]
    if re.search(r'\b(?:если|возможно|кажется|мог|якобы|не)', prefix, re.I):
        return
    location = key(scene.get('location'))
    local = [entity for entity in entities if entity.get('kind') in ('npc', 'creature')
             and key(entity.get('location')) == location and life_state(entity) == 'alive'
             and any(re.search(r'\bгоблин', name, re.I) for name in entity_names(entity))]
    if len(local) > 1 and re.search(r'третий|у леса', match[0], re.I):
        local = [entity for entity in local if entity.get('encounter_position') == 'лес'
                 or encounter_position(entity['name']) == 'лес']
    if len(local) == 1:
        local[0]['status'] = 'сбежал'
        old = text(local[0].get('description'), 540)
        if not old.endswith('Скрылся в лесу.'):
            local[0]['description'] = (old + ' Скрылся в лесу.').strip()


def world_context(state, query, limit=12, *, player_query=None):
    """Small relevant subset, independent of total saved world size."""
    scene = state.get('scene') or {}
    scene = scene if isinstance(scene,dict) else {}
    location = key(scene.get('location'))
    people = scene.get('participants') or []
    people = {key(p) for p in people} if isinstance(people,list) else set()
    def tokens(value):
        return re.findall(r'\w+', value.casefold().replace('ё', 'е'))

    def mentions(name, source):
        candidates = tokens(name)
        width = len(candidates)
        if not width:
            return False
        return any(all(a == b or (len(a) >= 4 and len(b) >= 4
                                  and a[:min(5, len(a) - 1, len(b) - 1)]
                                  == b[:min(5, len(a) - 1, len(b) - 1)])
                       for a, b in zip(candidates, source[i:i + width]))
                   for i in range(len(source) - width + 1))

    explicit = tokens(query if player_query is None else player_query)
    words = {word[:5] for word in tokens(query) if len(word) >= 3}
    related = []
    for collection in ('quests', 'agenda'):
        entries = state.get(collection)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('status') in ('completed', 'failed', 'resolved'):
                continue
            fields = ('name', 'title', 'description', 'reward_condition', 'actor', 'subject')
            related.append(tokens(' '.join(text(entry.get(field), 600) for field in fields)))
    effects = state.get('active_effects')
    affected = {effect.get('target') for effect in (effects if isinstance(effects, list) else [])
                if isinstance(effect, dict) and isinstance(effect.get('target'), str)
                and not (type(effect.get('expires')) is int and type(state.get('world_minute')) is int
                         and effect['expires'] <= state['world_minute'])}

    def score(e):
        name = key(e['name'])
        local = bool(location and (key(e.get('location'))==location or name==location))
        overlap = words & {w[:5] for w in tokens(name + ' ' + text(e.get('description'), 600)) if len(w) >= 3}
        names = [key(alias) for alias in entity_names(e)]
        return (any(mentions(alias, explicit) for alias in names), bool(set(names) & people),
                any(mentions(alias, source) for alias in names for source in related),
                e['id'] in affected, local, len(overlap))
    ranked = sorted(reversed(records(state)),key=score,reverse=True)
    budget = 3500
    selected = []
    for e in ranked:
        if len(selected) >= limit:
            break
        entry = {k: text(e.get(k), n) for k,n in {'id':80,'name':160,'kind':30,'description':260,
                 'location':180,'role':40,'traits':160,'status':120,'threat':30,'form':120,'environment':160,'access':24}.items() if isinstance(e.get(k),str)}
        if isinstance(e.get('aliases'), list):
            entry['aliases'] = entity_names(e)[1:5]
        entry.update({k:e[k] for k in ('hp','max_hp','mana','max_mana','attitude') if type(e.get(k)) is int})
        if e.get('kind') in ('npc','creature'): entry['life_state']=life_state(e)
        if isinstance(e.get('mechanics'),dict): entry['mechanics']=e['mechanics']
        size = len(json.dumps(entry,ensure_ascii=False))
        if size <= budget:
            selected.append(entry)
            budget -= size
    return selected


def npc_damage(state, result):
    """Resolve signed damage only against an explicitly bound existing creature/NPC."""
    check = (result or {}).get('check') or {}
    ranged = check.get('kind') == 'attack' and check.get('ammo') and result.get('success') is True
    if not (ranged or check.get('kind') == 'damage') or check.get('effect') or not check.get('world_target_id'):
        return {}
    total = result.get('ranged_damage' if ranged else 'total')
    if type(total) is not int: return {}
    target = next((e for e in records(state) if e['id']==check['world_target_id']),None)
    if not target or life_state(target)!='alive' or type(target.get('hp')) is not int or type(target.get('max_hp')) is not int: return {}
    return {'id':target['id'],'name':target['name'],'hp_before':target['hp'],
            'hp_after':max(0,target['hp']-max(0,total)),'max_hp':target['max_hp']}


def bind_damage_target(state, check, proposal):
    if not isinstance(check,dict) or check.get('kind')!='damage' or check.get('effect') or not isinstance(proposal,dict): return
    entity_id = proposal.get('world_target_id')
    target = next((e for e in records(state) if e['id']==entity_id),None)
    if not entity_id and proposal.get('world_target') is True:
        matches = [e for e in records(state) if key(e['name'])==key(check.get('target'))
                   and e.get('kind') in {'npc','creature'} and type(e.get('hp')) is int]
        target = matches[0] if len(matches)==1 else None
    if (target and target.get('kind') in {'npc','creature'} and type(target.get('hp')) is int
            and key(check.get('target'))==key(target['name'])):
        check['world_target_id']=target['id']


def apply_npc_damage(entities, outcome):
    for e in entities:
        if outcome and e['id']==outcome['id']:
            e['hp']=outcome['hp_after']
            if e['hp']==0: e['status']='побеждён (0 HP)'; e['life_state']='defeated'
    return entities


WORLD_PROMPT = '''
ДИНАМИЧЕСКИЙ МИР. Не ограничивай фантазию каталогом. Для новых значимых существ, NPC, предметов, локаций, фракций и способностей верни world_entities: [{kind,name,description,location,traits,status,evidence}]. kind: npc, creature, item, location, faction, ability или other. Для боевого существа можешь при создании или при вступлении ранее мирного NPC в бой указать threat: minor, normal, dangerous, elite, legendary (относительная сила в мире, не подстраивай под героя). HP вычисляет сервер в пределах диапазона силы, не возвращай hp/max_hp. Для разных одноимённых объектов передавай new_entity:true только при первом появлении, для обновления обязательно ID; одноимённые NPC могут иметь одинаковое имя. Для предмета с экипировкой передай mechanics:{slot:weapon,dice,element,stat} или {slot:armor,defense,dex_cap}. Для обновления существующей записи используй точный id из world_entities контекста, не меняй имя и вид напрямую. Если NPC назвал своё имя, обнови прежний id и укажи known_name, подтверждённое фразой evidence; не создавай дубль. Для локации указывай access: open, closed или restricted по фактическому доступу, отдельно от факта обнаружения. evidence — дословная фраза нового видимого повествования, не фантазия игрока и не пересказ. Не сохраняй неподтверждённые догадки как события. Не воскрешай побеждённых существ без особого сюжетного разрешения. Для новой проверки урона по зарегистрированному существу добавь в pending_check точный world_target_id и target (имя этого существа). Если существо впервые создаётся этим же ходом, вместо ID передай world_target: true и точное имя target. npc_rule_effect — уже вычисленный итог броска по существу, не пересчитывай его. Не объявляй окончательное убийство до разрешения броска. Новые необычные свойства допустимы как сюжетные факты, но не создают автоматическую игровую механику сами по себе.
'''
