"""Optional one-call narration protocol. Metadata never enters the visible transcript."""
import json
import math

START = '[[STATE_DELTA]]'
END = '[[/STATE_DELTA]]'
MAX_DELTA_CHARS = 24000
OBJECTS = set('scene elapsed clock_end pending_check cancel_check rest_event ability_event purchase exchange resource_events stats level_upgrade'.split())
ARRAYS = set('schedule resolve_events reward_outcomes facts npc_actions inventory inventory_remove item_transfers notes quests conditions relationships faction_clocks dc_modifier_reasons proficiencies skills abilities languages world_entities effects shop_offers blueprints'.split())
STRINGS = set('name race class background position cover weapon_in_hand hp_event'.split())
INTS = set('world_minute hp max_hp hp_delta gold silver copper dc_modifier'.split())
IGNORED = set('xp xp_next level level_pending xp_event_ids quest_xp_awards quest_xp_paid_ids level_history last_state_event_id'.split())


def validate_delta(value):
    """Check envelope and basic types; semantic checks remain in extract_state."""
    if not isinstance(value, dict):
        return None
    def safe(v, depth=0):
        if depth > 12:
            return False
        if v is None or isinstance(v, bool):
            return True
        if isinstance(v, str):
            return len(v) <= 12000
        if type(v) is int:
            return abs(v) <= 10**9
        if type(v) is float:
            return math.isfinite(v) and abs(v) <= 10**9
        if isinstance(v, list):
            return len(v) <= 100 and all(safe(x, depth+1) for x in v)
        if isinstance(v, dict):
            return len(v) <= 100 and all(isinstance(k, str) and len(k) <= 100 and safe(x, depth+1) for k,x in v.items())
        return False
    if not safe(value):
        return None
    cleaned = {}
    for key, val in value.items():
        if key in IGNORED:
            continue
        if key in OBJECTS: valid = isinstance(val, dict)
        elif key in ARRAYS: valid = isinstance(val, list)
        elif key in STRINGS: valid = isinstance(val, str)
        elif key in INTS: valid = type(val) is int
        elif key == 'restrained': valid = type(val) is bool
        else: return None
        if not valid:
            return None
        cleaned[key] = val
    return cleaned


class DeltaStream:
    """Incrementally hide a trailing metadata block, even across chunk boundaries."""
    def __init__(self):
        self.pending = ''
        self.hidden = ''
        self.in_delta = False
        self.overflow = False

    def feed(self, text):
        if self.in_delta:
            self._append(text)
            return ''
        self.pending += text
        index = self.pending.find(START)
        if index >= 0:
            visible = self.pending[:index]
            self.in_delta = True
            self._append(self.pending[index+len(START):])
            self.pending = ''
            return visible
        # Preserve only a suffix that might be the start of the marker.
        keep = next((n for n in range(min(len(START)-1,len(self.pending)),0,-1)
                     if self.pending.endswith(START[:n])),0)
        visible = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ''
        return visible

    def _append(self, text):
        if len(self.hidden)+len(text) > MAX_DELTA_CHARS:
            self.overflow = True
        if not self.overflow:
            self.hidden += text

    def finish(self):
        # A partial marker is not story content; discard it at end of stream.
        self.pending = ''
        if not self.in_delta or self.overflow or self.hidden.count(END) != 1:
            return None
        payload, _, trailing = self.hidden.partition(END)
        if trailing.strip():
            return None
        try:
            return validate_delta(json.loads(payload))
        except (ValueError, RecursionError):
            return None


PROTOCOL_PROMPT = '''
ПРОТОКОЛ ХОДА: сначала пиши обычный видимый ответ мастера по всем игровым правилам.
Затем в самом конце, после повествования и карточек, добавь служебный блок:
[[STATE_DELTA]]
{...}
[[/STATE_DELTA]]
Внутри строго один JSON-объект по правилам обновления состояния ниже. Не используй Markdown вокруг JSON.
Возвращай только новые изменения, подтверждённые видимой частью текущего ответа. Если ничего не изменилось, верни {}.
Если ход сложный и не можешь надёжно сформировать полный JSON, верни null: сервер применит прежний способ обновления.
Не показывай служебные поля в повествовании. Не придумывай результат серверного броска, HP после него, опыт и уровень.
'''
