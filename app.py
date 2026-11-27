# -*- coding: utf-8 -*-
"""RPG ИИ-Мастер — серверная часть.

Локально:  python app.py
Railway:   gunicorn app:app -w 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT
"""
import os
import re
import unicodedata
import json
import random
import secrets
import sqlite3
import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from time import monotonic, perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, request, jsonify, Response, render_template, stream_with_context
from itsdangerous import BadSignature, URLSafeSerializer
from openai import OpenAI

from campaign import (
    active_check, apply_ability_event, facts_context, formula_key, merge_facts, merge_scene, notes_context,
    initial_identity, normalize_abilities, normalize_ability, pending_check, roll_result, rules_result,
    sheet_identity, campaign_minute, clock_label, update_agenda, wait_plan,
    advance_clock, synchronize_clock, close_overdue_events, record_npc_actions,
    CANCEL_CHECK_COMMAND, CANCEL_CHECK_REPLY, check_instructions_in_card,
)

from engine.clock import advance_action
from engine.equipment import (item_spec, equipment_spec, ammo_kind, ammo_available,
                              require_ammo, consume_ammo)
from engine.api_budget import ApiBudget, BudgetExceeded, estimated_units
from engine.constructor import (CONSTRUCTOR_PROMPT, TYPES, apply_blueprints, apply_character_blueprints, recipe_matches,
                                attach_item_blueprints, attach_learned_spell, constructor_context,
                                learn_blueprint, learned_spell_from_story, recipes)
from engine.turn_protocol import DeltaStream, PROTOCOL_PROMPT, validate_delta
from engine.world import (WORLD_PROMPT, world_context, merge_entities, npc_damage,
                          bind_damage_target, apply_npc_damage, combat_ready, sync_life, restore_scene_people,
                          entity_names, life_state)

from engine.effects import (EFFECT_PROMPT, PASSIVE_OBSERVATION, resolve_effects, effect_context, check_bonus,
                            complete_hero_damage, control_restriction, hero_incapacitated, STATS, descriptive_damage)
from engine.combat import (ARMOR, COMBAT_PROMPT, WEAPONS, combat_context, combat_status, combat_turn, hero_defense, narrated_attacker,
                           profile, safe_ability, narrated_weapon, narrated_ranged_check, bind_ranged_weapon, adjust_damage, SHOT_EVENT)
from engine.economy import (TRADE_PROMPT, COINS, apply_exchange, apply_purchase, current_offers,
                            confirmed_purchase, exchange_from_story, merge_offers, trade_key, wallet_text, settle_offer, PURCHASE_CANCELLED, item_key, same_item, grounded as trade_grounded)

from party_store import PartyConflict, PartyMissing, PartyStore
from token_usage import TokenUsage, state_turn_id

BASE_DIR = Path(__file__).resolve().parent
APP_REVISION = "2026-11-27.world-alpha5.1"
QWEN_SUMMARY_LIMIT = 480
QWEN_STATE_LIMIT = 1100
QWEN_STATE_SETUP_LIMIT = 2200


# ─── Конфигурация ─────────────────────────────────────────────────────────
# .env нужен только локально. На Railway переменные задаются в панели проекта,
# файла на диске нет — и это нормально, приложение не должно падать.
def _load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[boot] python-dotenv не установлен — читаю только окружение", flush=True)
        return None
    for name in (".env", "_.env", "env", "env.txt"):
        path = BASE_DIR / name
        if path.is_file():
            load_dotenv(path, override=False)
            return path
    load_dotenv(override=False)
    return None


_ENV_FILE = _load_env()

app = Flask(__name__)
if hasattr(app,"config"):
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))
YANDEX_API_KEY = (os.getenv("YANDEX_API_KEY") or "").strip()
YANDEX_FOLDER_ID = (os.getenv("YANDEX_FOLDER_ID") or "").strip()
USE_YANDEX = bool(YANDEX_API_KEY)
PROVIDER = "Yandex AI Studio" if USE_YANDEX else "OpenAI-compatible"
KEY_VARIABLE = "YANDEX_API_KEY" if USE_YANDEX else "OPENAI_API_KEY"
MODEL_VARIABLE = "YANDEX_MODEL" if USE_YANDEX else "OPENAI_MODEL"


def _model_id(value):
    if USE_YANDEX and not value.startswith("gpt://"):
        return f"gpt://{YANDEX_FOLDER_ID}/{value}"
    return value


client_options = {"timeout": LLM_TIMEOUT, "max_retries": 0}
if USE_YANDEX:
    if not YANDEX_FOLDER_ID:
        raise SystemExit("Для Yandex AI Studio задайте YANDEX_FOLDER_ID в Railway Variables.")
    BASE_URL = "https://ai.api.cloud.yandex.net/v1"
    API_KEY = YANDEX_API_KEY
    MODEL = _model_id((os.getenv("YANDEX_MODEL") or "").strip() or "deepseek-v4.1-flash")
    client_options["project"] = YANDEX_FOLDER_ID
else:
    BASE_URL = (os.getenv("OPENAI_BASE_URL") or "").strip() or None
    API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    MODEL = (os.getenv("OPENAI_MODEL") or "").strip() or "gpt-4o-mini"
    if not API_KEY and BASE_URL:
        API_KEY = "ollama"
    if not API_KEY:
        raise SystemExit(
            "В Railway Variables задайте YANDEX_API_KEY и YANDEX_FOLDER_ID для Yandex AI Studio "
            "или OPENAI_API_KEY для OpenAI."
        )

client = OpenAI(api_key=API_KEY, base_url=BASE_URL, **client_options)
ROLL_SIGNER = URLSafeSerializer(os.getenv("ROLL_SIGNING_KEY") or API_KEY, salt="rpg-dice-v1")
TURN_COMPLETE = "\n[[ХОД_ЗАВЕРШЁН]]"

# Без отдельных настроек служебные запросы используют основную модель.
EXTRACT_MODEL = _model_id((os.getenv("EXTRACT_MODEL") or "").strip() or MODEL)
SUMMARY_MODEL = _model_id((os.getenv("SUMMARY_MODEL") or "").strip() or MODEL)

# ─── Длина ответа мастера и бюджет хода ───────────────────────────────────
# Эти константы объявлены до первого print(): модуль выполняется сверху вниз,
# и обращение к ним выше определения роняло воркер с NameError.
# Ориентир: ~7000 токенов на обычный ход (ответ мастера + лист + свёртка).
# presence_penalty снят: он подталкивал модель к раннему завершению ответа.
FREQ_PENALTY = float(os.getenv("OPENAI_FREQ_PENALTY", "0.15"))
PRES_PENALTY = float(os.getenv("OPENAI_PRES_PENALTY", "0.0"))
NARRATION_MAX_TOKENS = max(256, int(os.getenv("NARRATION_MAX_TOKENS", "2300")))
INLINE_STATE_ENABLED = os.getenv("INLINE_STATE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
INLINE_STATE_EXTRA_TOKENS = max(256, int(os.getenv("INLINE_STATE_EXTRA_TOKENS", "700")))
# Создание героя длиннее: блок листа плюс открывающая сцена. 2200, потому что
# обрыв на середине листа стоит целого хода.
NARRATION_SETUP_TOKENS = max(256, int(os.getenv("NARRATION_SETUP_TOKENS", "2200")))
# Повтор при пустом или оборванном ответе: лимит выше, рассуждения выключены.
NARRATION_RETRY_TOKENS = max(NARRATION_SETUP_TOKENS, NARRATION_MAX_TOKENS,
                             int(os.getenv("NARRATION_RETRY_TOKENS", "2600")))
NARRATION_RETRY_EFFORT = (os.getenv("NARRATION_RETRY_EFFORT") or "none").strip()
# Необязательно: "none", "minimal" или "low" — если модель тратит вывод на рассуждения.
CHAT_REASONING_EFFORT = (os.getenv("CHAT_REASONING_EFFORT") or "").strip()

# Что выяснилось про параметр рассуждений у текущей модели (память процесса):
#   None — ещё не проверяли, "" — провайдер параметр не принимает,
#   иначе — режим, который сработал. Так повтор не оплачивается каждый ход.
_REASONING_STATE = {"fix": None}

print(
    f"[boot] app_revision={APP_REVISION} | env={_ENV_FILE.name if _ENV_FILE else 'нет (только переменные окружения)'} | "
    f"provider={PROVIDER} | model={MODEL} | base_url={BASE_URL or 'https://api.openai.com/v1'} | "
    f"timeout={LLM_TIMEOUT:g}s | narration_max_tokens={NARRATION_MAX_TOKENS} | "
    f"narration_setup_tokens={NARRATION_SETUP_TOKENS}",
    flush=True,
)
if BASE_URL and ("127.0.0.1" in BASE_URL or "localhost" in BASE_URL):
    print(
        "[boot] ВНИМАНИЕ: base_url указывает на localhost. Это работает только если "
        "Ollama запущена на ТОЙ ЖЕ машине, что и этот процесс. На Railway так нельзя.",
        flush=True,
    )


def _saves_dir():
    """Папка сохранений. На Railway монтируйте Volume и задайте SAVES_DIR=/data."""
    raw = (os.getenv("SAVES_DIR") or "").strip()
    path = Path(raw) if raw else (BASE_DIR / "saves")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        print(
            f"[boot] ВНИМАНИЕ: {path} недоступен для записи ({exc}). "
            "Сохранения работать не будут — подключите Volume и задайте SAVES_DIR.",
            flush=True,
        )
    return path


SAVES_DIR = _saves_dir()
TOKEN_USAGE = TokenUsage(SAVES_DIR / "token_usage.sqlite3")
PARTIES = PartyStore(SAVES_DIR / "parties.sqlite3")
PARTIES.initialize()
API_BUDGET = ApiBudget(SAVES_DIR / 'api_budget.sqlite3',
    global_calls=max(1,int(os.getenv('API_DAILY_CALLS','3000'))),
    owner_calls=max(1,int(os.getenv('API_OWNER_DAILY_CALLS','150'))),
    global_units=max(1,int(os.getenv('API_DAILY_UNITS','30000000'))))
ALLOW_LEGACY_LLM = os.getenv('ALLOW_LEGACY_LLM','0') == '1'

def _paid_completion(**options):
    if os.getenv('API_ENABLED','1') != '1': raise BudgetExceeded('Генерация временно отключена администратором.')
    try:
        owner = _owner()
    except (RuntimeError,AttributeError):
        owner = ''  # Background memory jobs still use the global ceiling.
    API_BUDGET.reserve(owner, estimated_units(options))
    return client.chat.completions.create(**options)

app.add_url_rule("/admin/tokens", view_func=TOKEN_USAGE.dashboard)

# ─── Лимит параллельных обращений к модели ────────────────────────────────
# Живёт в памяти процесса: держите РОВНО ОДИН воркер gunicorn,
# иначе лимит умножится на число воркеров.
LLM_MAX_CONCURRENT = max(1, int(os.getenv("LLM_MAX_CONCURRENT", "2")))
LLM_QUEUE_TIMEOUT = float(os.getenv("LLM_QUEUE_TIMEOUT", "120"))
LLM_SEM = threading.BoundedSemaphore(LLM_MAX_CONCURRENT)


@contextmanager
def llm_slot(timeout=None):
    """Контекст, дающий слот на обращение к модели.

    yield True  — слот получен;
    yield False — очередь переполнена, обращаться не надо.
    """
    ok = LLM_SEM.acquire(timeout=LLM_QUEUE_TIMEOUT if timeout is None else timeout)
    try:
        yield ok
    finally:
        if ok:
            LLM_SEM.release()


PENALTY_REJECT = (
    "frequency_penalty", "presence_penalty", "unrecognized", "unsupported",
    "unknown field", "unknown parameter", "extra fields", "additional properties",
    "unexpected keyword", "not permitted", "invalid_request",
)


def _short_error(exc):
    """Короткий, но конкретный текст ошибки провайдера."""
    if isinstance(exc,BudgetExceeded): return str(exc)
    text = " ".join(str(exc).split())[:400]
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if body:
        try:
            text += " " + json.dumps(body, ensure_ascii=False)[:300]
        except (TypeError, ValueError):
            pass
    if API_KEY: text=text.replace(API_KEY,"[redacted]")
    return f"{type(exc).__name__}{' ' + str(status) if status else ''}: {text}"


def _error_hint(exc):
    """Что игроку делать с этой ошибкой. Пустая строка — подсказки нет."""
    low = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status == 401 or "unauthorized" in low or "invalid api key" in low:
        return f"Ключ не принят. Проверьте {KEY_VARIABLE} в Railway Variables."
    if status == 403 and USE_YANDEX:
        return "Доступ запрещён. Проверьте YANDEX_FOLDER_ID и права ключа Yandex AI Studio."
    if status == 402 or "insufficient" in low or "credit" in low or "quota" in low:
        return "У провайдера закончились средства. Пополните счёт или смените модель."
    if status == 429 or "rate limit" in low:
        return "Провайдер ограничивает частоту. Подождите минуту или смените модель."
    if status == 404 or ("model" in low and "not found" in low):
        return f"Модель не найдена. Проверьте {MODEL_VARIABLE} в Railway Variables."
    if ("connection" in low or "refused" in low or "timed out" in low
            or "name or service" in low):
        if USE_YANDEX:
            return "Нет связи с Yandex AI Studio. Проверьте соединение и логи Railway."
        return ("Нет связи с моделью. Проверьте OPENAI_BASE_URL: на Railway "
                "адрес 127.0.0.1 указывает на сам контейнер, где Ollama нет.")
    if "context" in low and ("length" in low or "token" in low):
        return "Слишком длинный контекст. Начните новую партию или уменьшите историю."
    return ""


def _is_yandex_qwen(model):
    host = urlsplit(BASE_URL or "").hostname
    yandex_endpoint = host in {
        "ai.api.cloud.yandex.net", "apps.ai.api.cloud.yandex.net", "llm.api.cloud.yandex.net",
    }
    return (USE_YANDEX or yandex_endpoint) and "qwen3.6-35b-a3b" in model.lower().split("/")


def _qwen_service_options(model, limit):
    if not _is_yandex_qwen(model):
        return {}
    # extra_body also works with versions of the OpenAI SDK that predate this limit.
    return {"extra_body": {"reasoning_effort": "none", "max_completion_tokens": limit}}


def _narration_options(messages, limit, stream, reasoning_effort):
    """Общие параметры рассказа: и для потока, и для готового ответа."""
    options = dict(model=MODEL, messages=messages, max_tokens=limit)
    if stream:
        options.update(stream=True, stream_options={"include_usage": True})
    if _is_yandex_qwen(MODEL):
        # Use Qwen's non-thinking sampling profile for narration.
        options.pop("max_tokens", None)
        options.update(temperature=0.7, top_p=0.8,
                       extra_body={"reasoning_effort": "none", "max_completion_tokens": limit})
    else:
        options.update(temperature=0.8, frequency_penalty=FREQ_PENALTY,
                       presence_penalty=PRES_PENALTY)
        if reasoning_effort:
            options["extra_body"] = {"reasoning_effort": reasoning_effort}
    return options


def _create_with_fallback(options, diag=None):
    """Запрос к модели с откатом по параметрам, которые провайдер не принял."""
    while True:
        try:
            return _paid_completion(**options)
        except Exception as exc:
            detail = str(exc).lower()
            status = getattr(exc, "status_code", None)
            if ("stream_options" in options
                    and status in (400, 422, 501)
                    and any(word in detail for word in ("stream_options", "include_usage"))):
                del options["stream_options"]
            elif "max_tokens" in options and "max_tokens" in detail:
                # Новые модели OpenAI принимают только max_completion_tokens.
                options["max_completion_tokens"] = options.pop("max_tokens")
            elif "extra_body" in options and any(
                    word in detail for word in ("reasoning_effort", "extra_body")):
                del options["extra_body"]
                if diag is not None:
                    diag["reasoning_rejected"] = True
            elif "frequency_penalty" in options and any(word in detail for word in PENALTY_REJECT):
                del options["frequency_penalty"]
                del options["presence_penalty"]
            else:
                raise


def _start_stream(messages, *, max_tokens=None, reasoning_effort=None, diag=None):
    limit = NARRATION_MAX_TOKENS if max_tokens is None else max_tokens
    effort = CHAT_REASONING_EFFORT if reasoning_effort is None else reasoning_effort
    return _create_with_fallback(_narration_options(messages, limit, True, effort), diag)


def _start_completion(messages, *, max_tokens=None, reasoning_effort=None, diag=None):
    """Ответ целиком, без потока: нужен там, где обрыв на середине недопустим."""
    limit = NARRATION_SETUP_TOKENS if max_tokens is None else max_tokens
    effort = CHAT_REASONING_EFFORT if reasoning_effort is None else reasoning_effort
    return _create_with_fallback(_narration_options(messages, limit, False, effort), diag)


def _measured_completion(turn_id, stage, **options):
    response = None
    try:
        response = _paid_completion(**options)
        return response
    finally:
        usage = getattr(response, "usage", None)
        extra = options.get("extra_body") or {}
        limit = extra.get("max_completion_tokens") or options.get("max_tokens")
        completion_tokens = getattr(usage, "completion_tokens", None)
        if limit is not None and isinstance(completion_tokens, int) and completion_tokens > limit:
            reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            app.logger.warning("Completion over limit: app_revision=%s stage=%s model=%s tokens=%d limit=%d reasoning=%s",
                               APP_REVISION, stage, options["model"], completion_tokens, limit, reasoning)
        if turn_id:
            model = getattr(response, "model", None) or options["model"]
            TOKEN_USAGE.record(turn_id, stage, model, usage)


def _header_token(name, limit):
    """Безопасное значение заголовка: только буквы, цифры, дефис и подчёркивание."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", request.headers.get(name, ""))[:limit]


PRESENCE_COOKIE = "rpg_visitor"
OWNER_COOKIE = "rpg_party_owner"
PRESENCE_LIMIT = max(1, int(os.getenv("PRESENCE_LIMIT", "10")))
PRESENCE_TIMEOUT = 90
CONVERSATION_WINDOW = 180
_GAME_ENDPOINTS = {
    "api_chat", "api_state", "api_roll", "api_saves_list", "api_save",
    "api_save_rename", "api_save_delete", "api_load",
    "api_parties", "api_party", "api_party_discard", "api_party_note",
}


@dataclass
class _Visitor:
    last_seen: float
    last_chat: float | None = None
    active_chats: int = 0


_visitors = {}
_presence_lock = threading.Lock()


def _check_presence(visitor_id, *, admit=False, start_chat=False):
    now = monotonic()
    with _presence_lock:
        expired = [key for key, visitor in _visitors.items()
                   if not visitor.active_chats and now - visitor.last_seen >= PRESENCE_TIMEOUT]
        for key in expired:
            del _visitors[key]

        visitor = _visitors.get(visitor_id)
        if visitor is None and admit and len(_visitors) < PRESENCE_LIMIT:
            visitor = _visitors[visitor_id] = _Visitor(now)
        if visitor is not None:
            visitor.last_seen = now
            if start_chat:
                visitor.active_chats += 1
                visitor.last_chat = now
        return {
            "admitted": visitor is not None,
            "online": len(_visitors),
            "limit": PRESENCE_LIMIT,
            "playing": sum(bool(item.active_chats) or
                           (item.last_chat is not None and now - item.last_chat < CONVERSATION_WINDOW)
                           for item in _visitors.values()),
        }


def _finish_chat_presence(visitor_id):
    with _presence_lock:
        visitor = _visitors.get(visitor_id)
        if visitor is not None and visitor.active_chats:
            visitor.active_chats -= 1
            visitor.last_seen = visitor.last_chat = monotonic()


def _admit_visitor():
    visitor_id = request.cookies.get(PRESENCE_COOKIE, "")
    if not re.fullmatch(r"[0-9a-f]{32}", visitor_id):
        visitor_id = uuid4().hex
    return visitor_id, _check_presence(visitor_id, admit=True)


def _owner_token():
    token = request.cookies.get(OWNER_COOKIE, "")
    return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else ""


def _owner(token=None):
    token = token or _owner_token()
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


def _set_owner_cookie(response, token):
    response.set_cookie(OWNER_COOKIE, token, max_age=365 * 24 * 60 * 60,
                        httponly=True, samesite="Lax",
                        secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")


def _presence_response(body, visitor_id, status=200):
    response = app.make_response((body, status))
    response.headers["Cache-Control"] = "no-store"
    if request.cookies.get(PRESENCE_COOKIE) != visitor_id:
        response.set_cookie(PRESENCE_COOKIE, visitor_id, httponly=True, samesite="Lax",
                            secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
    if not _owner_token() and status == 200:
        _set_owner_cookie(response, secrets.token_urlsafe(32))
    return response


@app.before_request
def _require_game_presence():
    if request.endpoint not in _GAME_ENDPOINTS:
        return None
    visitor_id = request.cookies.get(PRESENCE_COOKIE, "")
    if not _check_presence(visitor_id)["admitted"]:
        return jsonify({"error": "Место в игре пока недоступно. Дождись свободного места."}), 503


def _client_id():
    """Идентификатор устройства игрока. Разделяет сохранения между людьми."""
    return _header_token("X-Client-Id", 32) or "shared"


SYSTEM_PROMPT = """
Ты — ИИ-мастер текстовой RPG в жанре тёмного фэнтези, D&D и LitRPG. Веди связное приключение на естественном русском. Управляй миром, NPC и последствиями. Решения, мысли и реплики героя оставляй игроку.

Показывай результат действия и двигай сцену. Для боевого раунда из combat используй уже сохранённый номер раунда; не объявляй новый раунд или пропуск хода только из-за разговора либо наблюдения. После провала дай последствие или новый путь вместо повторения той же проверки. Доводи установленные цели до развязки. Не добавляй новую обязательную цель задним числом. Различай подтверждённые факты и догадки. Сохраняй подтверждённого владельца физической улики, пока в текущей сцене явно не произошла передача. Просьба сделать копию не переносит оригинал к другому NPC и не раскрывает содержание запечатанного письма. Если нужен доступ к оригиналу, скажи, чего не хватает. Лист отражает состояние после предыдущего ответа. Используй его числа как старт хода. Уже проведённые удары, лечение, покупки и награды не применяй повторно, даже при уточнении. На вопрос о текущем числе ответь числом из листа, если в этом ходу оно не изменилось. Примеры развития сцены: реакция NPC, зацепка, опасность или новый путь. Одна-две яркие детали сильнее перечня эпитетов. Не пересказывай ход игрока, не нагромождай образы и не повторяй одинаковые детали в соседних сценах. Длину и структуру ответа задаёт блок [ФОРМАТ ОТВЕТА].
Поддерживай канон по листу и памяти. Сохраняй имена, место, условия и награду каждого принятого или предложенного задания. При закрытии сверяй исходные этапы, включая обратную доставку или доклад. Успех части задачи оставляет остальные этапы открытыми. Не смешивай людей и улики из разных мест. Свежие реплики уточняют старое резюме. У важных NPC свои мотивы, тайны и голос. Мир меняется и без героя: держи 1–4 силы с часами 1–6, продвигай их после крупных сцен и показывай последствия через события.

ПРОВЕРКИ И СЛОЖНОСТЬ (DC)
Если исход неясен, а провал что-то меняет, запроси проверку до описания результата. Примеры: прокрасться мимо дозора, обезвредить ловушку, убедить сопротивляющегося NPC. Открыть незапертую дверь или взять видимый предмет можно сразу. Владение навыком и подготовка дают преимущество по ситуации, но не отменяют риск.
Атака по сопротивляющейся цели — против её защиты. Для эффектов со спасброском бросает цель. Правило способности имеет приоритет. Укажи, кто бросает, проверку, DC, общий модификатор и команду, например «Скрытность, DC 13. Команда: /roll 1d20+4». Бонус навыка из листа уже суммарный. Без него используй мод характеристики и мастерство при владении (+2 на ур. 1–4, +3 на 5–8, +4 на 9–12, +5 на 13–16, +6 на 17–20).
DC 5/8/10/12 — низкая сложность, 15 — средняя, 18/20/25/30 — высокая. Навык и характеристика 16+ могут снизить DC на 2–3. Учитывай положение, ранения, темноту, спешку, помощь и укрытие цели (половинное +2, три четверти +5, полное закрывает цель). Если лежит и хочет атаковать, сначала подъём DC 10. Схвачен или связан — освобождение DC 15, если возможно.
Запрашивай один бросок за раз. Пока игрок не прислал [[БРОСОК]], закончи на запросе и не объявляй итог, урон или XP за ожидающее действие. Итог ≥ DC — успех, ниже — провал. Бросок урона или лечения применяй напрямую, без сравнения с DC. Преимущество: /roll 2d20kh1+N, помеха: /roll 2d20kl1+N (подставь суммарный модификатор). Бросок относится к последней проверке: возьми её цель, сложность и тип из непосредственно предыдущего ответа мастера вместе с действием перед ним. Не переноси бросок на другую сцену. Если итог снова приведён в тексте, используй итог калькулятора.

ДЫХАНИЕ ДРАКОНА В БОЮ:
Спасбросок делает каждая цель. DC = 8 + мод Телосложения героя + мастерство. Огонь, кислота и молния — Ловкость, холод и яд — Телосложение. Уточни неизвестную стихию. Игрок бросает за цели по очереди. Назови цель и команду отдельно, например /roll 1d20+2. При неизвестном модификаторе используй +0. Затем запроси один /roll 2d6 на урон: весь при провале, половину с округлением вниз при успехе. Сначала дождись бросков, затем опиши результат и потрать одно применение. Если заряд потрачен, сообщи до бросков. Если целей нет, спасбросок не нужен. Атака героя не бросается.

XP И ЛИСТ
Обычные действия, в том числе открытие двери, сами по себе дают 0 XP, как и отдельный удар, успешный бросок или промежуточный этап. Одно достижение учитывай один раз. За реальную угрозу — по силе: 10–25 / 50–100 / 150–250 / 500–1000 / 1500–2500 / 3000–5000 XP. Победа без убийства даёт половину. За серьёзное препятствие вне боя 50–100, сложное 150–250, эпическое 500+, за решающую зацепку 10–20. За квест выдай обещанную награду на завершении, исключив дубли за тот же результат. Укажи новую награду отдельной строкой в конце: +50 XP. Для задания укажи его точное название из листа: +50 XP — квест «Название». При обсуждении уже полученного опыта не начинай строку со знака плюс. При отсутствии награды пропусти строку. XP и уровень считает сервер. Если в листе есть next_upgrade_level, предложи улучшения для этого уровня: одну характеристику на +1 и новое или улучшенное умение. Дождись явного выбора игрока. После выбора начни отдельную строку с «Повышение до уровня N:» (N — номер уровня), назови характеристику, умение и, если герой получил или улучшил навык, его название и новый бонус. За ход разрешай одно повышение. Пока игрок выбирает, не применяй бонусы.
Письма, записи, карты и слухи — в заметки. Физический экземпляр письма, расписки или накладной, который можно вручить, учитывай также в инвентаре. Золотые, серебряные и медные монеты — разные суммы, сохраняй названную валюту. В рассказе дай суть документа. У квеста есть цель, этапы и награда: main 200–500 XP, side 50–150, personal 80–200, faction 100–250. Завершённый квест закрой; если это цепочка, открой следующий. После описания нового героя создай лист с расой, классом, предысторией, характеристиками и навыками, затем начни сцену.
"""

CHARACTER_CREATION_RULES = """
════════ СОЗДАНИЕ ПЕРСОНАЖА ════════
При первом описании персонажа определи:
1. РАСУ и КЛАСС. Возьми их из свободного описания или указанных полей. Пропущенные выбери сам по смыслу описания и миру.
2. ПРЕДЫСТОРИЮ (Солдат, Преступник, Мудрец, Народный герой, Благородный, Отшельник, Артист, Бродяга, Моряк, Наёмник, Шарлатан, Прислужник).
3. ХАРАКТЕРИСТИКИ (базовые по классу + расовые бонусы).
4. ВЛАДЕНИЯ, НАВЫКИ, УМЕНИЯ.

Выбор игрока, включая авторские варианты, сохраняй дословно: не переименовывай расу, класс и предысторию. Авторские бонусы, названные игроком для своей расы, применяй как есть.

Характеристики считай сам: база по классу плюс расовые бонусы плюс всё, что назвал игрок. Числа из служебного листа не копируй — там заготовка. HP, кошелёк и снаряжение выдай по классу: Колдун получает 2 ячейки заклинаний, лёгкие доспехи, кинжалы и простые инструменты, стартовый кошелёк 10–15 зм.

════════ СТРУКТУРА ПЕРВОГО ОТВЕТА ════════
Оформляй лист и начинай игру в этом ответе. Игрок может указать имя, расу, класс, предысторию и завязку свободным текстом, выбрать отдельные поля или полностью поручить выбор мастеру. Придумай имя, расу, класс или предысторию, если их не указали и они не ясны из текста. Не требуй выбора из вариантов и не задавай уточняющий вопрос перед листом. Если игрок указал стартовую сцену или мир, сохрани их.

Лист героя оформляй одним блоком между метками [[ЛИСТ]] и [[/ЛИСТ]]. Внутри — эти строки, каждая с новой строки, ровно с такими значками и без пропусков:

[[ЛИСТ]]
👤 Кейр — эльф · плут · Преступник
📊 СИЛ 10 (+0) · ЛОВ 17 (+3) · ТЕЛ 12 (+1) · ИНТ 12 (+1) · МДР 10 (+0) · ХАР 10 (+0)
❤️ HP 10/10 · ⭐ Ур. 1 · Опыт 0/100 · 💰 15 зм
🛡 Владения: лёгкие доспехи, рапиры, воровские инструменты
🗣 Языки: общий, эльфийский
🎯 Навыки: Скрытность +5, Обман +2
⚡ Активные: Хитрое действие — 3/короткий отдых
🌀 Пассивные: Скрытая атака 1d6, тёмное зрение
🎒 Инвентарь: рапира, кожаный доспех, воровские инструменты
[[/ЛИСТ]]

Метки [[ЛИСТ]] и [[/ЛИСТ]] стоят отдельными строками. Все девять строк обязательны: если чего-то нет, поставь пустое значение, но строку дай, иначе карточка игрока останется без раздела. Метка [[/ЛИСТ]] обязательна — без неё блок считается оборванным.

Первая строка — имя, раса, класс, предыстория через «·». Характеристики уже с расовыми бонусами, сокращения СИЛ/ЛОВ/ТЕЛ/ИНТ/МДР/ХАР, модификатор — (значение − 10) // 2. В навыках — каждый навык с суммарным бонусом (мод характеристики плюс мастерство +2 на ур. 1–4).

НАЗЫВАЙ КОНКРЕТНЫЕ ЭФФЕКТЫ. В активных умениях пиши, что именно герой может применить и с какой силой, а не общую категорию. Не «Заклинания — 2 ячейки», а «Мистический заряд 1d10 — без ограничений, Порча — 2 ячейки 1 круга / долгий отдых». Не «Второе дыхание — 1/короткий», а «Второе дыхание — 1/короткий отдых, 1d10+1 HP». Не «Ярость — 2/долгий», а «Ярость — 2/долгий отдых, +2 урона, 1 минута». Каждое имя эффекта бери из списка класса и расы ниже и, если у него есть числовой эффект, укажи кубик и длительность. По листу должно быть понятно без вопросов, что можно применить в бою и сколько раз.

То же правило для пассивных: не «Тёмное зрение», а «Тёмное зрение 18 м, скрытая атака 1d6». Если у пассивного эффекта нет числового значения, всё равно назови его конкретно, а не категорией.

В инвентаре — предметы с количеством, там же документы и письма.

Затем, после пустой строки, 1–2 абзаца открывающей сцены по правилам обычного хода, с зацепкой в конце. Раньше блока сцену не начинай.

Тот же блок [[ЛИСТ]] повторяй, если игрок прямо просит показать лист. В обычном рассказе метки [[ЛИСТ]] не используй.

Названия навыков, умений и предметов придумывай один раз: те же слова пойдут в лист игрока. Не выдумывай умений вне класса и расы и не дублируй расовые владения отдельными навыками.

════════ БАЗОВЫЕ ХАРАКТЕРИСТИКИ ════════
• Воин/Варвар/Паладин: Сила 15, Тел 14
• Плут/Следопыт/Монах: Лов 15
• Маг: Инт 15, Лов 12
• Жрец/Друид: Мудр 15, Тел 12
• Бард/Колдун/Чародей: Хар 15, Лов 12
Остальные 10–12.

════════ РАСОВЫЕ БОНУСЫ ════════
Человек +1 всем; Эльф +2 Лов/+1 Инт; Полуэльф +2 Лов/+1 Хар; Дворф +2 Тел/+1 Сила;
Полурослик +2 Лов/+1 Хар; Гном +2 Инт/+1 Тел; Полуорк +2 Сила/+1 Тел;
Тифлинг +2 Хар/+1 Инт; Драконорождённый +2 Сила/+1 Хар; Гоблин/Кобольд +2 Лов/+1 Тел.

════════ ВЛАДЕНИЯ И УМЕНИЯ ПО КЛАССУ ════════
ВОИН: все доспехи, щиты, воинское оружие. Второе дыхание (1/короткий, 1d10+ур HP), Боевой клич (1/короткий).
ВАРВАР: лёгкие/средние доспехи, щиты, воинское. Ярость (2/долгий, +2 урона, 1 мин), Безрассудная атака (пасс.).
ПЛУТ: лёгкие доспехи, рапиры, короткие мечи, воровские инструменты. Скрытая атака (пасс., 1d6), Хитрое действие (3/короткий), Воровской жаргон (пасс.).
МАГ: кинжалы, пращи, посохи. Заклинания (3 ячейки 1 круга / долгий), Ритуальное колдовство (пасс.).
ЖРЕЦ: лёгкие/средние доспехи, щиты. Божественный канал (2/короткий), Заклинания (2 ячейки / долгий).
ПАЛАДИН: все доспехи, щиты, воинское. Божественная кара (пасс., +2), Возложение рук (5×ур пул / долгий).
СЛЕДОПЫТ: лёгкие/средние доспехи, щиты, воинское. Избранный враг (пасс., +2), Метка охотника (1/короткий).
МОНАХ: короткие мечи, ремесленные. Боевые искусства (пасс., 1d4), Ци (пул = ур+Мудр / короткий).
БАРД: лёгкие доспехи, рапиры, инструменты. Вдохновение (2/короткий, +1d6), Заклинания (2 ячейки / долгий).
КОЛДУН: лёгкие доспехи. Воззвание (пасс.), Заклинания (2 ячейки / долгий).
ЧАРОДЕЙ: кинжалы, пращи, посохи. Метамагия (2/долгий), Заклинания (2 ячейки / долгий).
ДРУИД: лёгкие/средние доспехи, щиты, травнический набор. Дикий облик (2/короткий), Заклинания (2 ячейки / долгий).

════════ ВЛАДЕНИЯ И НАВЫКИ ПО РАСЕ ════════
Человек: 1 навык + 1 язык.
Эльф: Восприятие, тёмное зрение, длинные/короткие мечи, луки, эльфийский.
Полуэльф: 2 навыка, тёмное зрение, эльфийский.
Дворф: тёмное зрение, боевые топоры/молоты, ремесленные, сопр. яду, дварфийский.
Полурослик: Восприятие, скрытность, удача, полурослицкий.
Гном: тёмное зрение, ремесленные, гномий.
Полуорк: тёмное зрение, Запугивание, выносливость, орочий.
Тифлинг: тёмное зрение, сопр. огню, Адское возмездие (1/долгий), инфернальный.
Драконорождённый: Дыхание дракона (1/короткий, 2d6), сопр. стихии, драконий.

════════ НАВЫКИ ПО ПРЕДЫСТОРИИ ════════
Солдат: Атлетика, Запугивание; кости, повозки.
Преступник: Обман, Скрытность; воровские, карты.
Мудрец: История, Магия; 2 языка.
Народный герой: Выживание, Животные; ремесленные, повозки.
Благородный: История, Убеждение; карты, 1 язык.
Отшельник: Медицина, Религия; травнический набор, 1 язык.
Артист: Акробатика, Выступление; инструмент, грим.
Бродяга: Внимательность, Выживание; воровские, 1 язык.
Моряк: Атлетика, Внимательность; навигация.
Наёмник: Атлетика, Запугивание; кости, транспорт.
Шарлатан: Обман, Ловкость рук; грим, документ.
Прислужник: Проницательность, Религия; книги, 1 язык.
"""

# Идёт последним блоком системного сообщения: это правило модель видит
# непосредственно перед ответом, поэтому оно и задаёт длину.
LENGTH_NOTE = """
[ФОРМАТ ОТВЕТА — ГЛАВНОЕ ПРАВИЛО ЭТОГО ХОДА]
Подбирай длину по сцене. Ориентиры в токенах: простое действие 50–150, диалог 150–400, исследование 300–600, бой 300–700, поворот истории 600–1200, кульминация 1000–2000. Описывай только значимые детали и последствия. Для ожидающей проверки запроси бросок и оставь исход открытым. Награду (+50 XP — квест «Название») вынеси в последнюю строку. По запросу листа дай блок [[ЛИСТ]] и короткое продолжение.
У NPC свои цели и ограниченные знания. Мир развивается и без героя, а последствия могут проявляться позднее. Мирные сцены могут оставаться мирными. Игрок может сменить цель или оставить прежний сюжет. Оставляй мысли и решения героя игроку. Числа механик бери из подтверждённых результатов сервера.
"""


def narration_budget(action):
    query = action.strip().casefold()
    if PASSIVE_OBSERVATION.fullmatch(query) or query.rstrip(".!?") in {
        "встаю", "жду", "киваю", "я встаю и готовлюсь к действию",
    }:
        target = 450
    elif re.fullmatch(r"/wait\s+\d{1,2}", query):
        target = 900
    else:
        target = NARRATION_MAX_TOKENS
    return min(target, NARRATION_MAX_TOKENS)


STATE_EXTRACT_PROMPT = """
Игровые часы. При создании верни world_minute — минута первого дня 0..1439. В обычном ходе верни elapsed: {minutes,evidence}, где minutes — только новые фактически прошедшие минуты, evidence — дословная фраза из новой реплики. Если явно указан час в конце действия, верни clock_end: {evidence} с точной фразой о текущем времени или завершённом прибытии. Не бери час из обещания, прошлого или расписания. Если roll_result.check.clock_spent указан, прежняя длительность уже учтена. Если время относится к отдельному предшествующему действию, например дороге до места проверки, верни elapsed.before_check: true. Верни elapsed.additional: true только для нового, явно дополнительного интервала после броска, с цитатой именно о дополнительных минутах. При wait_plan конец ожидания определён сервером.
События. Для новых обещанных встреч, ответов, возвращений и уже отправленных важных посыльных верни schedule: [{id,title,location,in_minutes,evidence}]. Используй стабильный id, название с именем или ролью участника, и срок от конца текущего хода. Если участник отправился или взялся ответить, а срок не назван, верни check_in: true вместо in_minutes. Сервер назначит время проверки известий. evidence — цитата о принятом поручении или состоявшейся отправке. Не записывай неподтверждённые предложения как события. Для нового конкретного исхода события верни resolve_events: [{id,outcome,evidence}], при явной досрочной отмене или прибытии добавь early: true. Не создавай повторно то же поручение, если новость лишь задержалась.
Награда квеста. stages — условия завершения; reward_condition — отдельное условие выплаты из исходного предложения, если оно есть. Сохраняй его и суммы rewards при принятии и позже. При завершении условного задания или новой подтверждённой информации о награде верни reward_outcomes: [{id,outcome,evidence}], outcome — met, unmet или pending. evidence — дословная фраза о том, что условие выполнено или не выполнено. Не меняй золото, серебро, медь и XP при расчёте условной награды: сервер начисляет согласованные суммы. Если точного подтверждения нет, используй pending. Условие выплаты не входит в stages и не определяет status. Если цель выполнена, отметь этапы и завершение даже при outcome: unmet или pending. Оставляй pending при неопределённости, unmet только при установленном несоблюдении условия.

Сохраняй канон кампании. Для новой сцены или изменений верни scene: {location,time,participants,goal,situation}. Указывай устойчивое название локации. participants — только те, кто сейчас рядом. При смене локации обнови участников, цель и обстановку сцены. Возвращай только изменения. Для подтверждённых событий, показаний, версий и владения уликами верни обновления facts: [{id,subject,detail,source,holder,status}]. Устойчивый id привязан к одному факту или предмету, сохрани его при уточнении и передаче. status: confirmed, reported, suspected или resolved. Для значимого действия NPC, которое действительно произошло в новой сцене, верни npc_actions: [{id,actor,location,evidence}]. Дай одному действию устойчивый ID. evidence — дословная фраза, где рассказчик описал действие, а не слова о том, что кто-то якобы сделал. Записывай уходы, прибытия, принятые поручения и важные передачи. Показания и догадки пиши отдельно в facts. Не повторяй прежнее действие при пересказе. Для передачи физической улики обнови holder и inventory_remove в том же ходе. Для новой объявленной команды /roll верни pending_check: {kind,title,target,effect,ability}; kind — check, attack, save, damage, heal или initiative. effect только hero_damage для урона персонажу игрока или hero_heal для его лечения. Для урона NPC effect опусти. Формулу и DC сервер получает из реплики. Для ожидающей проверки верни cancel_check: {evidence}, если проверка явно отменена или прежнее действие заменено и новая реплика подтверждает, что проверка больше не нужна. Если объявлена новая проверка, верни её отдельно в pending_check.
Для завершённого отдыха верни rest_event: {kind,evidence}, где kind — short_rest или long_rest, evidence — фраза из новой реплики о завершении отдыха. Для состоявшегося использования умения без отдельного броска верни ability_event: {name,evidence}, где name совпадает с карточкой и evidence описывает применение в этом ходе. Дыхание дракона и Второе дыхание с проверкой расходует сервер. При изменении описания существующего умения сохраняй текущие заряды.
При покупке верни purchase: {item,quantity,currency,price,evidence}, где item — точное название, quantity — число полученных предметов, currency — gold, silver или copper, price — общая цена, evidence — цитата новой подтверждённой покупки. Для покупки используй только purchase, не возвращай отдельные изменения кошелька или инвентаря.

Обнови лист RPG по последнему ходу. Верни строго JSON только изменённых полей, {} при отсутствии изменений.
Для выстрела из лука или арбалета объявляй один /roll на попадание и указывай точную цель из сцены. Сервер фиксирует результат и расход одного боеприпаса. При наличии world_target_id сервер также вычисляет урон. Если есть ranged_result, следуй ему и npc_rule_effect. Не объявляй бросок урона при промахе или если сервер уже рассчитал урон. Не меняй количество боеприпасов в дельте.
Меняй состояние только по новому результату в последней реплике мастера. Предыдущие реплики и пересказ старых событий служат контекстом. Лист уже учитывает прошлые удары, лечение, покупки и расходы. Изменяй кошелёк и инвентарь лишь при реальном новом обмене, расходе или находке. Пересказ, проверка числа, предложения и суммы до прошлого обмена ничего не меняют. Для существующего героя верни hp_delta (отрицательный урон или положительное лечение) только за новый результат, и hp_event: короткая дословная фраза из последнего ответа, где этот урон или лечение произошли, а не где обсуждается итоговое здоровье. На запросе броска, сверке или пересказе не возвращай hp_delta. При создании используй hp и max_hp. Для изменений инвентаря или монет у существующего героя верни resource_events: {"inventory":"дословная фраза нового получения, траты или передачи", "wallet":"дословная фраза новой оплаты"}. Укажи только затронутые группы. Цифры в пересказе или сверке не являются событиями. xp, xp_next, level, level_pending, xp_event_ids считает сервер.
Имя, раса, класс и предыстория меняются только по событию. Поля: name, race, class, background, hp, max_hp, gold, silver, copper, stats {str,dex,con,int,wis,cha}. HP не выше max_hp. Базовые характеристики, владения, навыки и умения назначай при создании, беря их из блока [[ЛИСТ]]: строка «📊» — характеристики, «❤️» — hp, уровень, опыт и кошелёк, «🛡» — владения, «🗣» — языки, «🎯» — навыки с суммарным бонусом, «⚡» — активные умения с uses_max, uses_left и recharge (short_rest, long_rest или day), «🌀» — пассивные и постоянные эффекты с recharge passive и без uses_max, «🎒» — инвентарь. Значки и метки [[ЛИСТ]] в лист не переноси. Название умения бери из блока как есть, включая числовой эффект. Инвентарь, кошелёк и характеристики из блока переноси сразу, в том же ходе. Навык {name,bonus,source}; умение {name,description,uses_max,uses_left,recharge}. Трать применения по событию и восстанавливай по отдыху. Для Дыхания дракона спиши одно применение после разрешения урона, а не пока мастер ждёт бросок. Сохраняй полное описание эффекта при изменении только зарядов. Активное умение «без ограничений» передавай с recharge: "unlimited", без uses_max и uses_left. Пассивный эффект передавай с recharge: "passive". Не используй отрицательные заряды, Markdown и HTML-сущности в названии.
Для передачи физической вещи из инвентаря персонажа используй item_transfers: [{id,quantity,holder,evidence}]. id — точный ID из инвентаря, holder — имя получателя, evidence — цитата о состоявшейся передаче в этой реплике. Не используй одновременно inventory_remove для той же передачи.
Изменённую запись коллекции возвращай целиком с прежним id или именем. inventory содержит только добавленные или изменённые вещи. Для удаления верни inventory_remove: [{"name":"точное имя из листа","quantity":1}]. Удаляй за фактический расход, передачу или потерю. Для замены старого названия новым явно удали старую запись. Количество у изменённого предмета указывай итоговое. notes — содержание документов, писем, слухов и записей. Полученный физический документ также добавь в inventory, при вручении удали его из inventory. Заметка {id,type,title,content,source,date,unread}; type: note, letter, document, notification, rumor или journal.
Квест {id,name,type,status,chain,chain_order,stages,current_stage,rewards,giver,location}; stages — объекты {text,done}; rewards — объект {xp,gold,silver,copper,items,reputation}. Записывай окончательно предложенные условия со статусом offered, принятые — active. Сохраняй id, название, этапы и награду. Отмечай каждый фактически выполненный этап по отдельности. Закрывай квест после выполнения всех оговорённых этапов, включая сдачу, только если она входила в задачу. Если квест официально закрыли с неполным результатом, верни failed и сохрани невыполненные пункты. Исправляй явно отменённое закрытие с прежним id.
Временные поля по сцене: conditions, position, cover, weapon_in_hand, restrained, dc_modifier, dc_modifier_reasons. Отрази уже совершённое перемещение даже перед запросом броска. При смене сцены обнови cover: "нет", если герой покинул прежнее укрытие без нового. При пустых руках верни weapon_in_hand: "". Если условия прошли, верни пустой список; при отсутствии помех — 0 и []. Связи {name,attitude,note}; faction_clocks {name,progress,max,goal}. При обучении или событии в мире обновляй skills и abilities даже после создания героя.
Повышение передавай отдельным полем level_upgrade только когда игрок явно выбрал улучшения и мастер подтвердил их в последнем ответе строкой «Повышение до уровня N:». Формат: {"level_upgrade":{"level":2,"stat":"str","ability":{"name":"Рывок","description":"...","uses_max":1,"uses_left":1,"recharge":"short_rest"},"skills":[{"name":"Атлетика","bonus":4,"source":"Воин"}]}}. Используй next_upgrade_level из листа. Для улучшения существующего умения передай его точное имя и новый максимум, сервер сохранит число потраченных зарядов. skills включай, если выбран или улучшен навык. Не дублируй бонусы повышения в других полях JSON. level_history записывает сервер.
"""


SUMMARY_PROMPT = """
Храни итоги движка как подтверждённые события. Намерения игрока и отклонённые покупки не являются свершившимися действиями.
Обнови краткую память кампании по прежнему резюме и новым событиям. Сохрани имена и принадлежность NPC, решения, обещания, условия и награды открытых квестов, важные предметы и зацепки. Привязывай события к месту и квесту, отмечай уже закрытые задачи и выданные награды. Укажи последнее место и открытую угрозу. Замени устаревшее, объедини дубли. Не сочиняй нового. До 10 коротких пунктов, без пересказа реплик, описаний внешности, бросков и текущих чисел листа.
"""

DICE_RE = re.compile(r"^\s*(\d*)d(\d+)(?:(kh|kl)(\d+))?([+-]\d+)?\s*$", re.IGNORECASE)

DEFAULT_STATE = {
    "name": "Пустой", "race": "", "class": "", "background": "",
    "level": 1, "xp": 0, "xp_next": 100,
    "hp": 10, "max_hp": 10, "gold": 0, "silver": 0, "copper": 0,
    "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
    "proficiencies": [], "skills": [], "abilities": [],
    "inventory": [], "notes": [], "quests": [], "conditions": [],
    "relationships": [], "faction_clocks": [],
    "dc_modifier": 0, "dc_modifier_reasons": [],
    "position": "стоит", "cover": "нет",
    "weapon_in_hand": "", "restrained": False,
    "scene": {}, "facts": [],
    "level_history": [], "xp_event_ids": [], "quest_xp_awards": [], "level_pending": 0
}

# Основные поля листа и краткие условия текущих квестов для мастера.
STATE_FIELDS = ("name", "race", "class", "background", "level", "xp", "xp_next",
                "hp", "max_hp", "gold", "silver", "copper", "stats", "conditions", "position",
                "cover", "weapon_in_hand", "armor_equipped", "restrained", "mana", "max_mana")
STATE_NOTE = ("[ЛИСТ ПОСЛЕ ПРЕДЫДУЩЕГО ОТВЕТА МАСТЕРА. "
              "Текущий ход игрока ещё не учтён. Используй числа отсюда.]\n")

# На первом ходу лист ещё пуст: числа в state_context — заготовка DEFAULT_STATE,
# и подсказка «используй числа отсюда» заставляла модель скопировать десятки.
STATE_NOTE_SETUP = ("[ПЕРСОНАЖ ЕЩЁ НЕ СОЗДАН. Числа заготовки сюда не подставлены. "
                    "Характеристики с расовыми бонусами, HP, кошелёк и снаряжение "
                    "назначь сам по расе, классу и предыстории.]\n")


KEEP_RECENT = 6
SUMMARY_BATCH_MESSAGES = 4
SUMMARY_MAX_BATCH_MESSAGES = 32
RECENT_HISTORY_CHARS = max(800, int(os.getenv("RECENT_HISTORY_CHARS", "2400")))
SUMMARY_INPUT_CHARS = max(600, int(os.getenv("SUMMARY_INPUT_CHARS", "1800")))
STATE_HISTORY_CHARS = 1800
SUMMARY_MAX_TOKENS = max(128, int(os.getenv("SUMMARY_MAX_TOKENS", "400")))
SUMMARY_MAX_SESSIONS = int(os.getenv("SUMMARY_MAX_SESSIONS", "128"))

# Резюме прошлого живёт на сервере, по session_id. Клиент его не хранит
# и не присылает — иначе оно пересчитывалось бы каждый ход.
_SUMMARY_STATE = {}
_SUMMARY_LOCK = threading.Lock()
_SUMMARY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpg-summary")
_SUMMARY_JOB = threading.BoundedSemaphore(1)


def _summary_get(sid):
    with _SUMMARY_LOCK:
        st = _SUMMARY_STATE.get(sid)
        return dict(st) if st else {"text": "", "folded": 0}


def _summary_put(sid, text, folded):
    with _SUMMARY_LOCK:
        if sid not in _SUMMARY_STATE and len(_SUMMARY_STATE) >= SUMMARY_MAX_SESSIONS:
            _SUMMARY_STATE.pop(next(iter(_SUMMARY_STATE)))
        _SUMMARY_STATE[sid] = {"text": text, "folded": folded}


def _budgeted_dialogue(messages, max_chars, preserve_latest=False):
    """Сохраняет формат и порядок реплик, сокращая самые длинные фрагменты."""
    if not messages:
        return []
    recent = messages[-1:] if preserve_latest else []
    earlier = messages[:-1] if recent else messages
    available = max(0, max_chars - sum(len(m.get("content", "")) for m in recent))
    if available < len(earlier) * 40:
        keep = available // 40
        earlier = earlier[-keep:] if keep else []
    ordered = sorted(range(len(earlier)), key=lambda index: len(earlier[index].get("content", "")))
    limits = [0] * len(earlier)
    for position, index in enumerate(ordered):
        limit = min(len(earlier[index].get("content", "")), available // (len(ordered) - position))
        limits[index] = limit
        available -= limit
    clipped = []
    for message, limit in zip(earlier, limits):
        content = message.get("content", "")
        if len(content) > limit:
            excerpt_chars = limit - len(" […] ")
            head = excerpt_chars * 2 // 3
            tail = excerpt_chars - head
            content = content[:head] + " […] " + content[-tail:]
        clipped.append({**message, "content": content})
    return clipped + recent


def _dialogue_messages(messages):
    dialogue = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant") or not isinstance(message.get("content"), str):
            continue
        role, content = message["role"], message["content"]
        resolution = message.get("resolution")
        if role == "assistant" and isinstance(resolution, str) and resolution:
            content += "\n[Итог движка] " + resolution[:550]
        dialogue.append({"role": role, "content": content})
    return dialogue


def _recent_dialogue(dialogue):
    """Приоритет у непрерывного хвоста: действие, последняя проверка и ответ."""
    remaining = RECENT_HISTORY_CHARS
    tail = []
    for message in reversed(dialogue):
        content = message.get("content", "")
        if len(content) > remaining:
            if len(tail) < 3 and remaining >= 160:
                head = remaining // 2
                end = remaining - head - len(" […] ")
                tail.append({**message, "content": content[:head] + " […] " + content[-end:]})
            break
        tail.append(message)
        remaining -= len(content)
    return list(reversed(tail))


def _messages_with_memory(messages, session_id, *, character_setup=False, party_id=None):
    summary = PARTIES.summary(party_id) if party_id else _summary_get(session_id)
    dialogue = _dialogue_messages(messages)
    parts = [message["content"] for message in messages
             if message.get("role") == "system"
             and not message["content"].startswith(
                 ("[РЕЗЮМЕ ПРОШЛОГО]", STATE_NOTE, STATE_NOTE_SETUP))]
    if summary["text"]:
        parts.append(f"[РЕЗЮМЕ ПРОШЛОГО]\n{summary['text']}")
    parts.extend(message["content"] for message in messages
                 if message.get("role") == "system"
                 and message["content"].startswith((STATE_NOTE, STATE_NOTE_SETUP)))
    system = [{"role": "system", "content": "\n\n".join(parts)}] if parts else []
    if character_setup:
        return system + dialogue[-1:]
    # Даже для старого сохранения без резюме ближайшая проверка остаётся целиком.
    tail = _recent_dialogue(dialogue[max(0, min(summary["folded"], len(dialogue) - 3)):])
    return system + tail


def _refresh_summary(dialogue, session_id, turn_id, party_id=None):
    summary = PARTIES.summary(party_id) if party_id else _summary_get(session_id)
    folded = summary["folded"]
    target = min(len(dialogue) - KEEP_RECENT, folded + SUMMARY_MAX_BATCH_MESSAGES)
    if target - folded < SUMMARY_BATCH_MESSAGES:
        return
    recent = _budgeted_dialogue(dialogue[folded:target], SUMMARY_INPUT_CHARS)
    new_text = "\n".join(f"{message['role']}: {message['content']}" for message in recent if message.get("content"))
    if not new_text:
        return

    previous = summary["text"]
    archive = f"{previous}\n\nНОВЫЕ СОБЫТИЯ:\n{new_text}" if previous else new_text
    try:
        with llm_slot(timeout=0) as ok:
            if not ok:
                return
            limit_options = (_qwen_service_options(SUMMARY_MODEL, QWEN_SUMMARY_LIMIT)
                             or {"max_tokens": SUMMARY_MAX_TOKENS})
            resp = _measured_completion(
                turn_id, "summary",
                model=SUMMARY_MODEL,
                messages=[{"role": "system", "content": SUMMARY_PROMPT},
                          {"role": "user", "content": archive}],
                temperature=0.2,
                timeout=min(LLM_TIMEOUT, 20),
                **limit_options,
            )
        choice = resp.choices[0]
        summary_text = (choice.message.content or "").strip()
        if summary_text and getattr(choice, "finish_reason", None) != "length":
            if party_id:
                PARTIES.save_summary(party_id, summary_text, target)
            else:
                _summary_put(session_id, summary_text, target)
    except Exception as exc:
        app.logger.warning("Не удалось обновить резюме: %s", type(exc).__name__)


def _run_summary_job(dialogue, session_id, turn_id, party_id=None):
    try:
        with app.app_context():
            _refresh_summary(dialogue, session_id, turn_id, party_id)
    finally:
        _SUMMARY_JOB.release()


def _schedule_summary(messages, session_id, turn_id, party_id=None):
    if not session_id and not party_id:
        return
    dialogue = _dialogue_messages(messages)
    summary = PARTIES.summary(party_id) if party_id else _summary_get(session_id)
    if len(dialogue) - summary["folded"] < KEEP_RECENT + SUMMARY_BATCH_MESSAGES:
        return
    # No backlog: busy chat traffic can delay a refresh until a following turn.
    if not _SUMMARY_JOB.acquire(blocking=False):
        return
    try:
        _SUMMARY_EXECUTOR.submit(_run_summary_job, dialogue, session_id, turn_id, party_id)
    except RuntimeError:
        _SUMMARY_JOB.release()



def roll_dice(expr):
    m = DICE_RE.match(expr or "")
    if not m:
        raise ValueError("Формат: 1d20+3, 2d6, d100-1, 2d20kh1+1 или 2d20kl1+1")
    count = int(m.group(1) or "1")
    sides = int(m.group(2))
    keep_mode, keep_count_text, modifier = m.groups()[2:]
    mod = int(modifier or "0")
    if not (1 <= count <= 100):
        raise ValueError("Кубиков 1..100")
    if not (2 <= sides <= 1000):
        raise ValueError("Сторон 2..1000")
    keep_count = int(keep_count_text) if keep_mode else count
    if not 1 <= keep_count <= count:
        raise ValueError("Число учитываемых кубиков должно быть от 1 до числа бросков")
    rolls = [random.randint(1, sides) for _ in range(count)]
    kept = sorted(rolls, reverse=keep_mode.lower() == "kh")[:keep_count] if keep_mode else rolls
    result = {"expr": expr, "rolls": rolls, "mod": mod, "total": sum(kept) + mod}
    if keep_mode:
        result["kept"] = kept
    return result


def clean_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Нет JSON")
    return json.loads(text[start:end + 1])


def normalize_quest(q):
    if isinstance(q, dict):
        return q
    if isinstance(q, str):
        return {
            "id": "legacy-" + str(random.randint(1000, 9999)),
            "name": q, "type": "side", "status": "active",
            "chain": "", "chain_order": 0,
            "stages": [], "current_stage": 0,
            "rewards": {}, "giver": "", "location": ""
        }
    return None


def _quest_name(quest):
    name = quest.get("name") if isinstance(quest, dict) else quest
    return " ".join(name.casefold().split()) if isinstance(name, str) else ""


def _quest_steps_open(quest):
    stages = quest.get("stages")
    return isinstance(stages, list) and any(isinstance(stage, dict) and not stage.get("done") for stage in stages)


def _merge_quests(old, changes):
    merged = [quest for value in old if (quest := normalize_quest(value))]
    for change in changes:
        if not isinstance(change, dict) or not _quest_name(change):
            continue
        name = _quest_name(change)
        index = next((i for i, quest in enumerate(merged)
                      if (change.get("id") and quest.get("id") == change["id"])
                      or _quest_name(quest) == name), None)
        if index is None:
            item = {**change, "id": change.get("id") or uuid4().hex}
            item.pop("reward_status", None)
            if isinstance(item.get("reward_condition"), str) and item["reward_condition"].strip():
                item["reward_condition"] = item["reward_condition"].strip()[:360]
                item["reward_status"] = "pending"
            else:
                item.pop("reward_condition", None)
            if item.get("status") == "completed" and _quest_steps_open(item):
                item["status"] = "active"
            merged.insert(0, item)
            continue
        previous = merged.pop(index)
        updated = {**previous, **change, "id": previous.get("id") or change.get("id")}
        if previous.get("reward_condition"):
            updated["reward_condition"] = previous["reward_condition"]
            updated["reward_status"] = previous.get("reward_status", "pending")
        elif previous.get("status") == "offered" and isinstance(change.get("reward_condition"), str):
            condition = change["reward_condition"].strip()[:360]
            if condition:
                updated.update(reward_condition=condition, reward_status="pending")
            else:
                updated.pop("reward_condition", None)
        else:
            updated.pop("reward_condition", None)
            updated.pop("reward_status", None)
        if previous.get("status") in {"completed", "failed"}:
            updated["status"] = previous["status"]
        if previous.get("rewards"):
            updated["rewards"] = previous["rewards"]
        previous_stages = previous.get("stages")
        changed_stages = change.get("stages")
        if isinstance(previous_stages, list) and previous_stages:
            def stage_key(stage):
                return _quest_name(stage.get("text")) if isinstance(stage, dict) else ""

            patches = {stage_key(stage): stage for stage in (changed_stages or [])
                       if stage_key(stage)} if isinstance(changed_stages, list) else {}
            stages = []
            for stage in previous_stages:
                key = stage_key(stage)
                patch = patches.pop(key, None)
                if key:
                    stage = dict(stage)
                    stage["id"] = stage.get("id") or uuid4().hex
                    if patch and patch.get("done") is True:
                        stage["done"] = True
                stages.append(stage)
            updated["stages"] = stages
        if (previous.get("status") in {"offered", "active"} and updated.get("status") == "completed"
                and _quest_steps_open(updated)):
            updated["status"] = "active"
        merged.insert(0, updated)
    # Older versions could assign multiple IDs to the same quest.
    seen = set()
    result = []
    for quest in merged:
        stages = quest.get("stages")
        if (quest.get("status") == "active" and quest.get("reward_condition")
                and isinstance(stages, list) and stages
                and all(isinstance(stage, dict) and stage.get("done") is True for stage in stages)):
            quest = {**quest, "status": "completed"}
        name = _quest_name(quest)
        if name and name in seen:
            continue
        seen.add(name)
        result.append(quest)
    return result


def _quest_context(quests, awarded):
    if not isinstance(quests, list):
        return []
    awarded = set(awarded) if isinstance(awarded, list) and all(isinstance(name, str) for name in awarded) else set()
    result = []
    for quest in quests:
        if not isinstance(quest, dict) or not isinstance(quest.get("name"), str):
            continue
        status = quest.get("status", "active")
        if status not in {"offered", "active", "completed", "failed"}:
            continue
        stages = quest.get("stages")
        entry = {"name": quest["name"][:100], "status": status}
        rewards = quest.get("rewards")
        if isinstance(rewards, dict):
            entry["rewards"] = {key: value for key, value in rewards.items()
                                if key in {"xp", "gold", "silver", "copper"} and type(value) is int}
        elif isinstance(rewards, list):
            entry["rewards"] = [item[:45] for item in rewards[:3] if isinstance(item, str)]
        if quest.get("reward_condition"):
            entry["reward_condition"] = quest["reward_condition"][:240]
            entry["reward_status"] = quest.get("reward_status", "pending")
        if quest.get("id"):
            entry["id"] = quest["id"]
        if _quest_name(quest) in awarded:
            entry["xp_paid"] = True
        if status in {"offered", "active"} and isinstance(stages, list):
            entry["open"] = [stage["text"][:130] for stage in stages
                             if isinstance(stage, dict) and not stage.get("done")
                             and isinstance(stage.get("text"), str)][:4]
        result.append(entry)
    result.sort(key=lambda q: q["status"] in {"completed", "failed"})
    excerpt = []
    budget = 1550
    for quest in result[:6]:
        size = len(json.dumps(quest, ensure_ascii=False))
        if size <= budget:
            excerpt.append(quest)
            budget -= size
    return excerpt


def normalize_note(n):
    if isinstance(n, dict):
        if "id" not in n:
            n["id"] = "n" + str(random.randint(1000, 9999))
        if "type" not in n:
            n["type"] = "note"
        if "title" not in n:
            n["title"] = (n.get("content", "") or "")[:40]
        if "content" not in n:
            n["content"] = ""
        if "unread" not in n:
            n["unread"] = True
        return n
    if isinstance(n, str):
        return {
            "id": "n" + str(random.randint(1000, 9999)),
            "type": "note", "title": n[:40], "content": n,
            "source": "", "date": "", "unread": True
        }
    return None


LEVEL_THRESHOLDS = {2: 100, 3: 250, 4: 500, 5: 1000, 6: 2000,
                   7: 3500, 8: 5500, 9: 8000, 10: 12000}


def level_for_xp(xp):
    level = 1
    for target, threshold in LEVEL_THRESHOLDS.items():
        if xp >= threshold:
            level = target
    if xp >= LEVEL_THRESHOLDS[10]:
        level = 10 + (xp - LEVEL_THRESHOLDS[10]) // 5000
    return min(20, level)


def xp_threshold_for_level(level):
    if level <= 1:
        return LEVEL_THRESHOLDS[2]
    if level <= 9:
        return LEVEL_THRESHOLDS[level + 1]
    return 12000 + (level - 9) * 5000


XP_EVENT_RE = re.compile(
    r"^[ \t]*(?:\*\*)?\+[ \t]*(\d{1,7})[ \t]*XP(?:\*\*)?"
    r"(?:[ \t]+[—–-][ \t]+[^\r\n]+|[.])?[ \t]*$", re.IGNORECASE | re.MULTILINE
)
INLINE_XP_EVENT_RE = re.compile(
    r"[.!?][ \t]+(?:\*\*)?\+[ \t]*(\d{1,7})[ \t]*XP(?:\*\*)?\.?\s*\Z",
    re.IGNORECASE,
)
QUEST_AWARD_RE = re.compile(r"[—–-][ \t]+квест[ \t]+«([^»\r\n]+)»[.!]?[ \t]*$", re.IGNORECASE)
LEVEL_UPGRADE_RE = re.compile(r"^(?:\*\*)?Повышение до уровня[ \t]+(\d{1,2})(?:\*\*)?:", re.IGNORECASE | re.MULTILINE)


def message_event_id(message, index):
    event_id = message.get("event_id")
    if isinstance(event_id, str) and re.fullmatch(r"[a-f0-9]{64}", event_id):
        return event_id
    raw = f"{index}\0{message.get('role','')}\0{message.get('content','')}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _awards_in_reply(text, allow_inline=False):
    lines = [line.strip().replace("**", "") for line in text.splitlines()]
    last = lines[-1] if lines else ""
    quest = QUEST_AWARD_RE.search(last)
    if quest and not last.startswith("+"):
        inline = re.search(r"[.!?]\s+\+\s*(\d{1,7})\s*XP\s*$", last[:quest.start()], re.I)
        if inline and allow_inline:
            return [(int(inline[1]), _quest_name(quest[1]))]
    matches = [(int(match[1]), _quest_name(quest[1]) if (quest := QUEST_AWARD_RE.search(match[0])) else "")
               for match in XP_EVENT_RE.finditer(text)]
    inline = INLINE_XP_EVENT_RE.search(text) if allow_inline else None
    if inline:
        matches.append((int(inline[1]), ""))
    if matches and len(lines) >= 2 and not matches[-1][1]:
        heading = re.fullmatch(r"Квест «([^»]+)» заверш[её]н[.!]?", lines[-2], re.I)
        if heading:
            matches[-1] = (matches[-1][0], _quest_name(heading[1]))
    return matches


def _item_name(item):
    if isinstance(item, dict):
        item = item.get("name")
    return " ".join(item.casefold().split()) if isinstance(item, str) else ""


def _inventory_with_ids(items):
    result = []
    for item in items if isinstance(items, list) else []:
        name = _item_name(item)
        if not name:
            result.append(item)
            continue
        entry = dict(item) if isinstance(item, dict) else {"name": item, "quantity": 1}
        entry["id"] = entry.get("id") or hashlib.sha256(("item:" + name).encode()).hexdigest()[:24]
        result.append(entry)
    return result


SEALED_LETTER = re.compile(r'\bзапечатанн\w*\s+(?:конверт\w*|письм\w*)\b', re.I)
LETTER_RECEIVED = re.compile(
    r'\b(?:протягивает|вручает|переда[её]т)\s+(?:тебе\s+)?' + SEALED_LETTER.pattern, re.I
)
LETTER_RECIPIENT = re.compile(
    r'\b((?:(?i:страж|стражник|торговец|архивистка)\s+)?[А-ЯЁ][а-яё-]{2,30})\s+'
    r'(?i:принимает|бер[её]т)\s+(?:у тебя\s+)?' + SEALED_LETTER.pattern
)


def narrated_letter_transfer(items, action, latest):
    if not re.search(r"\b(?:передаю|вручаю|отдаю)\b", action, re.I) or not re.search(r"письм|конверт", action, re.I):
        return None
    if re.search(r"\bне\s+(?:передаю|вручаю|отдаю)\b", action, re.I):
        return None
    match = LETTER_RECIPIENT.search(latest)
    if not match or re.search(r"\b(?:если|может|не)\b", re.split(r"[.!?;\n]", latest[:match.start()])[-1], re.I):
        return None
    letters = [item for item in items if SEALED_LETTER.search(_item_name(item))]
    if len(letters) == 1:
        return {"id": letters[0]["id"], "quantity": 1, "holder": match[1], "evidence": match[0]}
    return None


def receives_sealed_letter(action, latest):
    if not re.search(r"\b(?:прошу|принимаю|беру)\b", action, re.I) or not re.search(r"письм|конверт", action, re.I):
        return False
    if re.search(r"\bне\s+(?:беру|принимаю)\b", action, re.I):
        return False
    match = LETTER_RECEIVED.search(latest)
    if not match:
        return False
    prefix = re.split(r"[.!?;\n]", latest[:match.start()])[-1]
    if re.search(r"\b(?:если|может|не)\b", prefix, re.I):
        return False
    return " тебе " in match[0].casefold() or bool(
        re.search(r"\b(?:письмо|конверт)\s+у тебя\b", latest[match.end():match.end() + 100], re.I)
    )


def _transfer_items(items, transfers, latest):
    owners = []
    applied = set()
    if not isinstance(transfers, list):
        return items, owners
    for event in transfers:
        if not isinstance(event, dict) or not _grounded_event(event.get("evidence"), latest):
            continue
        item_id, quantity, holder = event.get("id"), event.get("quantity"), event.get("holder")
        if not isinstance(item_id, str) or type(quantity) is not int or quantity <= 0:
            continue
        if not isinstance(holder, str) or not holder.strip():
            continue
        transfer = (item_id, quantity, _quest_name(holder), event["evidence"].strip())
        if transfer in applied:
            continue
        item = next((item for item in items if isinstance(item, dict) and item.get("id") == item_id), None)
        if not item or type(item.get("quantity", 1)) is not int or item.get("quantity", 1) < quantity:
            continue
        items = _remove_inventory_items(items, [{"id": item_id, "quantity": quantity}])
        applied.add(transfer)
        owners.append({"id": "item-" + item_id + "-" + _quest_name(holder), "subject": item["name"],
                       "item_id": item_id,
                       "detail": f"Передано: {quantity}.", "holder": holder,
                       "source": event["evidence"], "status": "confirmed"})
    return items, owners


def _remove_inventory_items(items, removals):
    if not isinstance(removals, list):
        return items
    remaining = list(items)
    for removal in removals:
        if not isinstance(removal, dict):
            continue
        name = _item_name(removal)
        item_id = removal.get("id")
        quantity = removal.get("quantity", 1)
        if (not name and not isinstance(item_id, str)) or type(quantity) is not int or quantity <= 0:
            continue
        updated = []
        for item in remaining:
            matches = (isinstance(item, dict) and item.get("id") == item_id) if item_id else _item_name(item) == name
            if quantity <= 0 or not matches:
                updated.append(item)
                continue
            count = item.get("quantity", 1) if isinstance(item, dict) else 1
            count = count if type(count) is int and count > 0 else 1
            removed = min(quantity, count)
            quantity -= removed
            if count > removed:
                updated.append({**item, "quantity": count - removed})
        remaining = updated
    return remaining


def _next_upgrade_level(state):
    pending = int(state.get("level_pending", 0) or 0)
    level = max(1, int(state.get("level", 1) or 1))
    if pending <= 0 or level <= 1:
        return None
    history = state.get("level_history")
    history = history if isinstance(history, list) else []
    applied = {record["level"] for record in history
               if isinstance(record, dict) and isinstance(record.get("level"), int)}
    return next((number for number in range(max(2, level - pending + 1), level + 1)
                 if number not in applied), None)


def _update_named_entry(entries, change):
    name = change["name"].strip()
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and str(entry.get("name", "")).strip().casefold() == name.casefold():
            entries[index] = {**entry, **change, "name": name}
            return
    entries.append({**change, "name": name})


_SKILL_STATS = {
    "атлетика": "str", "акробатика": "dex", "ловкость рук": "dex", "скрытность": "dex",
    "магия": "int", "история": "int", "расследование": "int", "анализ": "int",
    "природа": "int", "религия": "int", "восприятие": "wis", "внимательность": "wis",
    "проницательность": "wis", "медицина": "wis", "выживание": "wis",
    "животные": "wis", "уход за животными": "wis", "обман": "cha",
    "запугивание": "cha", "убеждение": "cha", "выступление": "cha",
}


def _apply_level_upgrade(state, upgrade, next_level):
    if next_level is None or not isinstance(upgrade, dict) or upgrade.get("level") != next_level:
        return False

    stat = upgrade.get("stat")
    ability = normalize_ability(upgrade.get("ability"))
    if not isinstance(stat, str) or stat not in DEFAULT_STATE["stats"] or not isinstance(ability, dict):
        return False
    if not isinstance(ability.get("name"), str) or not ability["name"].strip():
        return False
    if not isinstance(ability.get("description"), str) or not ability["description"].strip():
        return False

    history = state.get("level_history")
    history = list(history) if isinstance(history, list) else []
    if any(isinstance(entry, dict) and entry.get("level") == next_level for entry in history):
        return False

    abilities = state.get("abilities")
    abilities = list(abilities) if isinstance(abilities, list) else []
    name = ability["name"].strip().casefold()
    previous = next((entry for entry in abilities
                     if isinstance(entry, dict) and str(entry.get("name", "")).strip().casefold() == name), {})
    old_limit, old_remaining, new_limit = previous.get("uses_max"), previous.get("uses_left"), ability.get("uses_max")
    if all(type(value) is int and value >= 0 for value in (old_limit, old_remaining, new_limit)):
        spent = max(0, old_limit - old_remaining)
        ability = {**ability, "uses_left": max(0, new_limit - spent)}
    _update_named_entry(abilities, ability)
    state["abilities"] = abilities

    existing_stats = state.get("stats")
    stats = dict(existing_stats) if isinstance(existing_stats, dict) else dict(DEFAULT_STATE["stats"])
    previous_stat = int(stats.get(stat, 10))
    stats[stat] = previous_stat + 1
    state["stats"] = stats
    modifier_gain = (stats[stat] - 10) // 2 - (previous_stat - 10) // 2

    skills = state.get("skills")
    skills = list(skills) if isinstance(skills, list) else []
    if modifier_gain:
        for index, skill in enumerate(skills):
            if not isinstance(skill, dict) or type(skill.get("bonus")) is not int:
                continue
            name = str(skill.get("name", "")).strip().casefold()
            if skill.get("stat", _SKILL_STATS.get(name)) == stat:
                skills[index] = {**skill, "bonus": skill["bonus"] + modifier_gain}

    skill_changes = upgrade.get("skills")
    if isinstance(skill_changes, list):
        for skill in skill_changes:
            if (isinstance(skill, dict) and isinstance(skill.get("name"), str)
                    and skill["name"].strip() and type(skill.get("bonus")) is int):
                _update_named_entry(skills, skill)
    state["skills"] = skills

    hp_gain = max(1, 2 + (int(stats.get("con", 10)) - 10) // 2)
    state["max_hp"] = max(1, int(state.get("max_hp", 1) or 1)) + hp_gain
    state["hp"] = state["max_hp"]
    history.append({"level": next_level, "hp": f"+{hp_gain}", "stats": f"+1 {stat}",
                    "abilities": ability["name"].strip()})
    state["level_history"] = history
    return True


def _recent_event_outcomes(state):
    return [event for event in (state.get("agenda") or [])
            if isinstance(event, dict) and event.get("status") == "resolved"][-4:]


def _grounded_event(evidence, latest):
    if not isinstance(evidence, str):
        return False
    phrase = evidence.strip()
    return len(phrase) >= 5 and phrase in latest


class StateUnavailable(Exception):
    pass


def _signed_roll(message, state, history, *, party=None):
    if not message or not isinstance(message.get("roll_receipt"), str):
        return None
    try:
        roll = ROLL_SIGNER.loads(message["roll_receipt"])
    except BadSignature as exc:
        raise ValueError("Бросок не удалось подтвердить. Обновите ход и повторите бросок.") from exc
    if party and (roll.get("party_id"), roll.get("revision")) != (party["id"], party["revision"]):
        raise ValueError("Этот бросок сделан в другой версии партии. Бросьте кубик снова.")
    return roll_result(active_check(state, history), roll)


ENGINE_COMMANDS = {"/attack", "/cast", "/guard", "/retreat", "/wallet", "/shop", "/buy", "/exchange", "/equip", "/design", "/learn"}


def _engine_command(state, query, check):
    command, _, argument = query.strip().partition(" ")
    command, argument = command.casefold(), argument.strip()
    if command not in ENGINE_COMMANDS:
        return None
    if command == "/wallet":
        return {**state, "effect_log": []}, f"Кошелёк: {wallet_text(state)}."
    if command == "/design":
        rows = recipes(state)
        if argument:
            matches = recipe_matches(state, argument)
            if len(matches) != 1:
                raise ValueError("Укажите точное название или ID проекта.")
            row = matches[0]
            details = row.get("description") or ""
            mechanics = json.dumps(row.get("mechanics") or {}, ensure_ascii=False)
            return {**state, "effect_log": []}, f"{TYPES[row['kind']]}: {row['name']}. {details}\n{mechanics}"
        lines = [f"{TYPES[row['kind']]}: {row['name']} · {row['id']}" for row in rows[-20:]]
        return {**state, "effect_log": []}, ("Проекты этой партии:\n" + "\n".join(lines) if lines
                                            else "Список проектов пуст. Опишите мастеру, что хотите разработать.")
    offers = current_offers(state)
    if command == "/shop":
        prices = [f"• {offer['item']} — {offer['price']} {COINS[offer['currency']][0]} · в наличии: {offer.get('stock',1)} · ID: {offer.get('offer_id','—')}"
                  for offer in offers]
        return {**state, "effect_log": []}, ("Цены в этой локации:\n" + "\n".join(prices)
                                             if prices else "Для этой локации пока нет подтверждённых цен. Спросите продавца.")
    if state.get("character_setup_pending") or not (state.get("race") and state.get("class")):
        raise ValueError("Сначала создайте персонажа.")
    if check:
        raise ValueError("Сначала разрешите или отмените ожидающую проверку.")
    restriction = control_restriction(state, query)
    if restriction:
        raise ValueError(restriction)
    if command in {"/attack", "/cast", "/guard", "/retreat"}:
        updated, reply = combat_turn(state, command, argument)
        if updated.get("xp") != state.get("xp"):
            level = level_for_xp(updated["xp"])
            old_level = state.get("level", 1)
            updated.update(level=level, xp_next=xp_threshold_for_level(level),
                           level_pending=state.get("level_pending", 0) + max(0, level - old_level))
        return updated, reply
    if combat_context(state):
        raise ValueError("Торговля и смена экипировки доступны вне боя.")
    updated = deepcopy(state)
    updated["effect_log"] = []
    if command == "/learn":
        reply = learn_blueprint(updated, argument)
        return advance_action(updated, 600), reply
    if command == "/equip":
        matches=[entry for entry in state.get('inventory',[]) if _item_name(entry)==_item_name(argument) or isinstance(entry,dict) and entry.get('id')==argument]
        item=matches[0] if len(matches)==1 else None
        if not item:
            raise ValueError("Укажите точное название предмета из инвентаря.")
        name = item.get("name") if isinstance(item, dict) else item
        spec = item_spec(state,item)
        if spec and spec["slot"] == "armor":
            updated["armor_equipped"] = name
            updated["armor_equipped_id"] = item.get("id") if isinstance(item,dict) else None
        elif spec and spec["slot"] == "weapon":
            updated["weapon_in_hand"] = name
            updated["weapon_equipped_id"] = item.get("id") if isinstance(item,dict) else None
        else:
            raise ValueError("У предмета пока нет характеристик экипировки.")
        return advance_action(updated,6), f"Экипировано: {name}. КБ героя: {hero_defense(updated)}."
    if command == "/buy":
        name, _, quantity_raw = argument.partition("|")
        matches = [offer for offer in offers if item_key(offer["item"]) == item_key(name) or offer.get("offer_id")==name.strip()]
        if not matches:
            matches = [offer for offer in offers if same_item(offer["item"], name)]
        if len(matches) != 1:
            raise ValueError("Проверьте доступные товары через /shop и укажите точное название.")
        quantity_raw = quantity_raw.strip()
        if quantity_raw and (not quantity_raw.isascii() or not quantity_raw.isdigit() or len(quantity_raw) > 2):
            raise ValueError("Укажите количество от 1 до 99 через |.")
        quantity = int(quantity_raw or "1")
        offer = matches[0]
        reply = settle_offer(updated,offer,quantity)
        return advance_action(updated,60), reply
    if not current_offers(state, include_sold_out=True):
        raise ValueError("Сначала уточните у торговца цену, чтобы подтвердить торговлю в этой локации.")
    parts = argument.split()
    if len(parts) != 3 or not re.fullmatch(r"[1-9][0-9]{0,5}", parts[1]):
        raise ValueError("Формат: /exchange gold 1 silver.")
    source, amount, target = parts
    reply = apply_exchange(updated, {"from": source, "to": target, "amount": int(amount)})
    return advance_action(updated,60), reply


def _apply_conditional_rewards(merged, current, newly_completed, outcomes, latest):
    changes = {}
    controlled = any(quest.get("reward_condition") for quest in newly_completed.values())
    quests = {quest.get("id"): quest for quest in merged["quests"]
              if isinstance(quest.get("id"), str) and quest.get("reward_condition")}
    for event in outcomes if isinstance(outcomes, list) else []:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            continue
        quest = quests.get(event["id"])
        if not quest or quest.get("status") != "completed" or _quest_steps_open(quest):
            continue
        controlled = True
        if quest.get("reward_status") in ("paid", "denied"):
            continue
        outcome = event.get("outcome")
        if outcome not in ("met", "unmet") or not _grounded_event(event.get("evidence"), latest):
            continue
        quest["reward_status"] = "paid" if outcome == "met" else "denied"
        quest["reward_evidence"] = event["evidence"]
        rewards = quest.get("rewards")
        if outcome == "met" and isinstance(rewards, dict):
            for key in ("xp", "gold", "silver", "copper"):
                amount = rewards.get(key)
                if type(amount) is int and amount > 0:
                    changes[key] = changes.get(key, 0) + amount
    if controlled:
        for currency in ("gold", "silver", "copper"):
            if type(current.get(currency, 0)) is not int:
                current[currency] = 0
    return changes, controlled


def extract_state(messages, current_state, turn_id=None, result=None, *, inline_delta=None):
    current = dict(DEFAULT_STATE)
    if isinstance(current_state, dict):
        current.update(deepcopy(current_state))
    if (len(messages) >= 2 and messages[-2].get("role") == "user"
            and messages[-2].get("content") == CANCEL_CHECK_COMMAND
            and messages[-1].get("content") == CANCEL_CHECK_REPLY):
        check = active_check(current, messages[:-2])
        if not check or check.get("kind") != "check":
            raise ValueError("Нет необязательной проверки для отмены.")
        return {**current, "pending_check": None, "effect_log": []}
    current["abilities"] = normalize_abilities(current.get("abilities"))
    next_upgrade_level = _next_upgrade_level(current)
    last_index = len(messages) - 1
    last_reply = messages[-1] if messages and messages[-1].get("role") == "assistant" else None
    last_event_id = message_event_id(last_reply, last_index) if last_reply else None
    if last_event_id and current.get("last_state_event_id") == last_event_id:
        return current
    current.setdefault("xp_event_ids", [])
    if not isinstance(current.get("xp_event_ids"), list):
        current["xp_event_ids"] = []

    # On legacy saves, mark historical XP-bearing lines as already seen, except
    # the latest assistant message (which may be the just-finished turn).
    if "xp_event_ids" not in (current_state or {}):
        for index, m in enumerate(messages):
            if m.get("role") == "assistant" and XP_EVENT_RE.search(m.get("content", "")) and index != (len(messages) - 1):
                current["xp_event_ids"].append(message_event_id(m, index))

    known_events = {event for event in current["xp_event_ids"] if isinstance(event, str)}
    awarded_messages = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        awards = _awards_in_reply(message.get("content", ""), allow_inline=index == last_index)
        if awards:
            awarded_messages.append((message_event_id(message, index), awards))

    if len(known_events) >= 500:
        first_known = next((i for i, (event_id, _) in enumerate(awarded_messages)
                            if event_id in known_events), 0)
        known_events.update(event_id for event_id, _ in awarded_messages[:first_known])

    fresh_events = [(event_id, amount) for event_id, amount in awarded_messages
                    if event_id not in known_events]

    latest = last_reply.get("content", "") if last_reply else ""
    action_turn = messages[-2] if last_reply and len(messages) >= 2 else None
    action = action_turn.get("content", "") if action_turn and action_turn.get("role") == "user" else ""
    context_messages = _budgeted_dialogue(messages[-3:-2], STATE_HISTORY_CHARS)
    plan = wait_plan(current, action)
    result_check = (result or {}).get("check") or {}
    restriction = control_restriction(current, action, waiting=bool(plan) or result_check.get("kind") == "save")
    if restriction and not current.get("character_setup_pending"):
        raise ValueError(restriction)
    context = "\n".join(f"{m['role']}: {m['content']}" for m in context_messages if m.get("content"))
    model_state = {key: value for key, value in current.items()
                   if key not in {"xp_event_ids", "quest_xp_awards", "quest_xp_paid_ids",
                                  "last_state_event_id", "level_history", "world_entities", "active_effects", "effect_log"}}
    model_state["facts"] = facts_context(current.get("facts"), action + " " + latest)
    model_state["world_entities"] = world_context(current, action + " " + latest, player_query=action)
    model_state["active_effects"] = effect_context(current)
    model_state["constructor"] = constructor_context(current, action)
    active_events = [event for event in (current.get("agenda") or [])
                     if isinstance(event, dict) and event.get("status") == "scheduled"]
    model_state["agenda"] = active_events[:12]
    model_state["recent_event_outcomes"] = _recent_event_outcomes(current)
    if plan:
        model_state["wait_plan"] = plan
    if result:
        model_state["roll_result"] = result
        model_state["rule_effect"] = rules_result(current, result)
        model_state["npc_rule_effect"] = npc_damage(current, result)
    if next_upgrade_level is not None:
        model_state["next_upgrade_level"] = next_upgrade_level
    if isinstance(current.get("quests"), list):
        model_state["quests"] = [
            {key: quest[key] for key in ("id", "name", "status", "reward_condition", "reward_status", "rewards") if key in quest}
            if isinstance(quest, dict) and quest.get("status") in {"completed", "failed"} else quest
            for quest in current["quests"]
        ]
    if isinstance(current.get("notes"), list):
        # Полные тексты остаются в листе. Имена и ID помогают избежать дублей.
        model_state["notes"] = [
            {key: note[key] for key in ("id", "type", "title") if key in note}
            if isinstance(note, dict) else note
            for note in current["notes"]
        ]
    model_inventory = model_state.get("inventory")
    if isinstance(model_inventory, list):
        # Для дельты достаточно имени и количества: описания предметов не нужны.
        model_state["inventory"] = [
            {key: item[key] for key in ("id", "name", "quantity", "mechanics", "world_id") if key in item}
            if isinstance(item, dict) else item
            for item in _inventory_with_ids(model_inventory)
        ][:40]
    prompt = (f"ТЕКУЩЕЕ СОСТОЯНИЕ:\n{json.dumps(model_state, ensure_ascii=False)}\n\n"
              f"ПРЕДЫДУЩИЙ КОНТЕКСТ (события уже учтены):\n{context}\n\n"
              f"ТЕКУЩЕЕ ДЕЙСТВИЕ ИГРОКА:\n{action}\n\n"
              f"НОВАЯ РЕПЛИКА МАСТЕРА:\n{latest}\n\nВерни JSON только с изменёнными полями.")
    new_character = (current.get("character_setup_pending")
                     or not (current.get("race") and current.get("class")))
    state_limit = QWEN_STATE_SETUP_LIMIT if new_character or next_upgrade_level is not None else QWEN_STATE_LIMIT
    state_messages = [{"role": "system", "content": STATE_EXTRACT_PROMPT + WORLD_PROMPT + EFFECT_PROMPT
                       + COMBAT_PROMPT + TRADE_PROMPT + CONSTRUCTOR_PROMPT},
                      {"role": "user", "content": prompt}]
    if inline_delta is not None and not new_character:
        new_state = validate_delta(inline_delta)
        if new_state is None:
            raise StateUnavailable("Некорректный встроенный блок состояния.")
    else:
        for attempt, limit in enumerate((state_limit, min(4400, state_limit * 2)), start=1):
            state_options = _qwen_service_options(EXTRACT_MODEL, limit) or {"max_tokens": limit}
            try:
                with llm_slot() as ok:
                    if not ok:
                        raise RuntimeError("нет свободного слота модели")
                    resp = _measured_completion(turn_id, "state", model=EXTRACT_MODEL,
                                                messages=state_messages, temperature=0.1, **state_options)
                state_choice = resp.choices[0]
                if getattr(state_choice, "finish_reason", None) == "length":
                    raise ValueError("обновление листа обрезано по лимиту")
                new_state = clean_json(state_choice.message.content or "")
                if not isinstance(new_state, dict):
                    raise ValueError("ожидался JSON-объект изменений")
                break
            except ValueError as exc:
                app.logger.warning("Извлечение состояния: ход=%s попытка=%s причина=%s",
                                   turn_id, attempt, type(exc).__name__)
                if attempt == 2:
                    raise StateUnavailable("Мастер ответил, но изменения листа не удалось прочитать. "
                                           "Повторите сохранение.") from exc
            except BudgetExceeded as exc:
                raise StateUnavailable(str(exc)) from exc
            except Exception as exc:
                app.logger.warning("Извлечение состояния: ход=%s причина=%s", turn_id, type(exc).__name__)
                raise StateUnavailable("Лист не обновился. Можно повторить сохранение этого хода.") from exc
    if new_character:
        identity = initial_identity(latest, current)
        if not identity:
            raise StateUnavailable("В ответе мастера не удалось прочитать имя, расу, класс и предысторию.")
        new_state.update(identity)
    previous_minute = campaign_minute(current)
    proposed_minute = new_state.pop("world_minute", None)
    elapsed = new_state.pop("elapsed", None)
    clock_end = new_state.pop("clock_end", None)
    scheduled = new_state.pop("schedule", None)
    resolved_events = new_state.pop("resolve_events", None)
    reward_outcomes = new_state.pop("reward_outcomes", None)
    campaign_scene = merge_scene(current.get("scene"), new_state.pop("scene", None))
    campaign_scene = dict(campaign_scene)
    history = messages[:max(0, last_index - 1)]
    previous_check = active_check(current, history)
    if plan and hero_incapacitated(current) and (previous_check or {}).get("kind") != "save":
        previous_check = None
    resolved_id = (result.get("check") or {}).get("id") if result else None
    resolved_previous = previous_check and resolved_id == previous_check.get("id")
    grounded = lambda evidence: _grounded_event(evidence, latest)
    minute = previous_minute
    if minute is None:
        minute = (proposed_minute if type(proposed_minute) is int and 0 <= proposed_minute < 1440
                  else campaign_minute({"scene": campaign_scene}))
        if minute is None:
            minute = 480
    elif plan:
        minute = plan["until"]
    else:
        minute = advance_clock(minute, latest, elapsed, clock_end, previous_check, resolved_previous, grounded)
    rest_event = new_state.pop("rest_event", None)
    rest = (rest_event.get("kind") if isinstance(rest_event, dict)
            and grounded(rest_event.get("evidence")) else "")
    completed_rest = (
        not new_character and not plan and not previous_check
        and not re.search(r"\b(?:не|если|напомни\w*|перескаж\w*)\b", action, re.I)
        and re.search(r"\b(?:отдых\w*|отдыха\w*|сплю|ложусь|спим)\b", action, re.I)
        and re.search(r"\b(?:отдых\s+заверш[её]н|ты\s+выспал[асься]+|ты\s+закончил[аи]? отдых)\b", latest, re.I)
        and re.search(r"\b(?:8|восемь)\s+часов\b", latest, re.I)
    )
    if completed_rest:
        rest = "long_rest"
        if previous_minute is not None and minute == previous_minute:
            minute += 480
    if minute is not None:
        new_state["world_minute"] = minute
        campaign_scene["time"] = clock_label(minute)
        new_state["agenda"] = update_agenda(current, scheduled, resolved_events, minute, grounded)
        close_overdue_events(new_state["agenda"], minute)
    else:
        new_state.pop("agenda", None)
    campaign_facts = merge_facts(current.get("facts"), new_state.pop("facts", None))
    campaign_facts = record_npc_actions(campaign_facts, new_state.pop("npc_actions", None),
                                        campaign_scene, minute, grounded)
    effect_proposals = new_state.pop("effects", None)
    # Never trust direct writes to engine-owned fields, including legacy extraction.
    for protected in ("active_effects", "effect_log", "mana", "max_mana", "hero_form",
                      "combat", "combat_rounds", "ability_cooldowns", "armor_equipped", "world_second", "life_state", "weapon_equipped_id", "armor_equipped_id"):
        new_state.pop(protected, None)
    blueprint_proposals = new_state.pop("blueprints", None)
    for field in ("constructor", "casting_stat", "character_blueprints"):
        new_state.pop(field, None)
    world_patches = new_state.pop("world_entities", None)
    entities = merge_entities({**current, "scene": campaign_scene}, world_patches, latest)
    restore_scene_people(entities, campaign_scene, latest, current.get("scene"),
                         new_state.get("name") if new_character else current.get("name"))
    npc_outcome = npc_damage(current, result)
    proposed = new_state.pop("pending_check", None)
    cancellation = new_state.pop("cancel_check", None)
    cancel_check = (isinstance(cancellation, dict) and grounded(cancellation.get("evidence")))
    resolved_check = (result.get("check") or {}) if result else {}
    resolved_ammo = resolved_check.get("ammo")
    shot_weapon = narrated_weapon(current, action) if not new_character and not previous_check and not result else None
    ranged_check = narrated_ranged_check(
        {**current, "world_entities": entities, "scene": campaign_scene},
        shot_weapon, action, latest, proposed)
    attacker = narrated_attacker(entities, proposed, latest, hero_name=current.get("name"),
                                 location=campaign_scene.get("location"))
    if ranged_check:
        next_check = ranged_check
    elif attacker and (not proposed or isinstance(proposed, dict) and proposed.get("kind") in (None, "attack")):
        proposed = {**(proposed or {}), "kind": "attack", "title": f"Атака: {attacker['name']}",
                    "target": current.get("name", "Герой")}
        next_check = pending_check(latest, proposed, campaign_scene, defense_dc=hero_defense(current))
        if next_check:
            practice = bool(re.search(r"учебн|спарринг|тренировоч", action + " " + latest, re.I))
            combat_ready(attacker, "minor" if practice else "normal")
            if type(attacker.get("hp")) is int:
                attacker["combat_profile"] = profile(attacker)
                attacker["location"] = campaign_scene.get("location") or attacker.get("location")
                next_check["actor_id"] = attacker["id"]
                next_check["practice"] = practice
    else:
        next_check = pending_check(latest, proposed, campaign_scene)
        if shot_weapon and not attacker:
            bind_ranged_weapon(current, next_check, shot_weapon)
    if (resolved_ammo and next_check and next_check.get("kind") == "damage"
            and not next_check.get("effect")
            and (resolved_check.get("world_target_id") or result.get("success") is False)):
        next_check = None
    previous_scene = current.get("scene")
    previous_location = previous_scene.get("location") if isinstance(previous_scene, dict) else None
    moved = bool(previous_location and previous_location != campaign_scene.get("location"))
    if previous_check and not resolved_previous:
        optional = previous_check.get("kind") == "check"
        replacement = next_check and isinstance(proposed, dict) and any(
            next_check.get(key) != previous_check.get(key) for key in ("kind", "title", "target", "expr", "dc")
        )
        if not optional or not (moved or cancel_check or replacement):
            next_check = previous_check
    ranged_target = (ranged_check or resolved_check).get("world_target_id") if ranged_check or resolved_ammo else None
    protected_target = next((entry for entry in entities if entry["id"] == ranged_target), None)
    if protected_target:
        existing_target = next((entry for entry in current.get("world_entities", [])
                                if isinstance(entry, dict) and entry.get("id") == ranged_target), None)
        # A model patch may describe a death before the signed throw has resolved.
        if existing_target and life_state(existing_target) == "alive" and not (
                npc_outcome and npc_outcome["hp_after"] == 0):
            protected_target["life_state"] = "alive"
            if "status" in existing_target:
                protected_target["status"] = existing_target["status"]
            else:
                protected_target.pop("status", None)
            if type(existing_target.get("hp")) is int:
                protected_target["hp"] = existing_target["hp"]
    new_state["world_entities"] = apply_npc_damage(entities, npc_outcome)
    if next_check and next_check is not previous_check:
        bind_damage_target({"world_entities": entities}, next_check, proposed)
        if last_event_id:
            next_check["id"] = last_event_id[:32]
        time_before_check = isinstance(elapsed, dict) and elapsed.get("before_check") is True
        if not plan and not time_before_check and previous_minute is not None and minute > previous_minute:
            next_check["clock_spent"] = minute - previous_minute
    if moved:
        new_state.setdefault("position", "стоит")
        new_state.setdefault("cover", "нет")
    new_state["scene"] = campaign_scene
    new_state["facts"] = campaign_facts
    ability_event = new_state.pop("ability_event", None)
    used_ability = (ability_event.get("name") if isinstance(ability_event, dict)
                    and _grounded_event(ability_event.get("evidence"), latest) else "")
    if resolved_check.get("kind") == "damage" and resolved_check.get("ability") == "Дыхание дракона":
        used_ability = "Дыхание дракона"
    if (next_check and next_check != previous_check and next_check.get("kind") == "heal"
            and next_check.get("ability") == "Второе дыхание"):
        used_ability = "Второе дыхание"
        next_check["charge_spent"] = True
        if "воин" in str(current.get("class", "")).casefold() and not new_character:
            next_check["expr"] = f"1d10+{current.get('level', 1)}"
    elif resolved_check.get("kind") == "heal" and resolved_check.get("ability") == "Второе дыхание":
        used_ability = "" if resolved_check.get("charge_spent") else "Второе дыхание"
    if "abilities" in new_state:
        new_state["abilities"] = normalize_abilities(new_state["abilities"])
        for entry in new_state["abilities"]:
            data = entry.pop("mechanics", None)
            if isinstance(data, dict) and (mechanics := safe_ability({**data, "name": entry["name"]}, power=max(1,min(20,current.get("level",1))))):
                entry["mechanics"] = mechanics
    if not new_character:
        rest = rest if rest in ("short_rest", "long_rest") else ""
        used_ability = used_ability if isinstance(used_ability, str) else ""
        before_abilities = current.get("abilities") or []
        changed_abilities = new_state.get("abilities")
        if isinstance(changed_abilities, list):
            existing = {_item_name(entry): entry for entry in before_abilities if isinstance(entry, dict)}
            for entry in changed_abilities:
                if not isinstance(entry, dict):
                    continue
                previous = existing.get(_item_name(entry))
                if previous:
                    for field in ("uses_left", "uses_max", "recharge"):
                        entry.pop(field, None)
        abilities, spent = apply_ability_event(before_abilities, used_ability, rest)
        if used_ability and not spent:
            raise StateUnavailable(f"Умение «{used_ability}» сейчас недоступно.")
        current["abilities"] = abilities
        if rest == "long_rest" and type(current.get("max_mana")) is int:
            current["mana"] = current["max_mana"]

    transfers = new_state.pop("item_transfers", None)
    if not new_character:
        items = _inventory_with_ids(current.get("inventory"))
        fallback = narrated_letter_transfer(items, action, latest)
        if fallback:
            transfers = transfers if isinstance(transfers, list) else []
            if not any(isinstance(event, dict) and event.get("id") == fallback["id"]
                       and _grounded_event(event.get("evidence"), latest) for event in transfers):
                transfers = [*transfers, fallback]
    owned_ammo = resolved_ammo or (shot_weapon or {}).get("ammo")
    ammo_log = []
    if resolved_ammo or (shot_weapon and not next_check and SHOT_EVENT.search(latest)
                         and not re.search(r"\bне\s+(?:стреляешь|выпускаешь|выстреливаешь)\b", latest, re.I)):
        ammo_log.append(consume_ammo(current, owned_ammo))
    if owned_ammo:
        for field in ("inventory", "inventory_remove"):
            if isinstance(new_state.get(field), list):
                new_state[field] = [item for item in new_state[field] if ammo_kind(item) != owned_ammo]
        if isinstance(transfers, list):
            ammo_ids = {item.get("id") for item in current.get("inventory", [])
                        if isinstance(item, dict) and ammo_kind(item) == owned_ammo}
            transfers = [item for item in transfers if ammo_kind(item) != owned_ammo
                         and (not isinstance(item, dict) or item.get("id") not in ammo_ids)]
    if transfers and not new_character:
        current["inventory"], transfer_facts = _transfer_items(
            _inventory_with_ids(current.get("inventory")), transfers, latest,
        )
        new_state["facts"] = merge_facts(new_state["facts"], transfer_facts)
        transferred_names = {_item_name(fact["subject"]) for fact in transfer_facts}
        transferred_ids = {fact["item_id"] for fact in transfer_facts}
        for field in ("inventory", "inventory_remove"):
            if isinstance(new_state.get(field), list):
                new_state[field] = [item for item in new_state[field]
                                    if _item_name(item) not in transferred_names
                                    and (not isinstance(item, dict) or item.get("id") not in transferred_ids)]
    trade_log = []
    price_proposals = new_state.pop("shop_offers", None)
    purchase = new_state.pop("purchase", None)
    offers_at_scene = {**current, "scene": campaign_scene, "world_entities":entities}
    stored_offers = current_offers(offers_at_scene, include_sold_out=True)
    current["shop_offers"] = merge_offers(offers_at_scene, price_proposals, latest, action=action)
    exchange = new_state.pop("exchange", None)
    if not new_character:
        structured_exchange = exchange is not None
        exchange = exchange if structured_exchange else exchange_from_story(action, latest)
        if exchange:
            for field in COINS:
                new_state.pop(field, None)
            try:
                if structured_exchange and not trade_grounded(exchange, latest):
                    exchange = exchange_from_story(action, latest)
                    if not exchange:
                        raise ValueError("Размен не удалось подтвердить по этому ходу.")
                trade_log.append(apply_exchange(current, exchange))
            except (ValueError, TypeError, AttributeError) as exc:
                trade_log.append(f"Отклонено: {exc}")
        if purchase:
            for field in ("inventory", "inventory_remove", *COINS):
                new_state.pop(field, None)
            try:
                purchase = confirmed_purchase(purchase, latest)
                if not purchase:
                    raise ValueError("Покупку не удалось подтвердить по этому ходу.")
                # The closing narration may already quote the post-sale balance.
                # Settle against stock at the beginning of the turn when it exists.
                refreshed = current_offers({**current, "scene": campaign_scene, "world_entities": entities}, include_sold_out=True)
                prior_ids = {offer.get("offer_id") for offer in stored_offers if offer.get("offer_id")}
                candidates = [offer for offer in [*stored_offers, *(offer for offer in refreshed if offer.get("offer_id") not in prior_ids)]
                              if same_item(offer["item"], purchase.get("item"))
                              and (not purchase.get("seller_id") or offer.get("seller_id")==purchase.get("seller_id"))]
                if len(candidates)>1: raise ValueError("Уточните продавца.")
                quote=candidates[0] if candidates else None
                if purchase.get('seller_id') and not quote: raise ValueError("Нет подтверждённого предложения этого продавца.")
                if quote and quote.get('seller_id'):
                    seller=next((e for e in entities if e['id']==quote['seller_id']),None)
                    if not seller or trade_key(seller['name']) not in trade_key(purchase['evidence']):
                        raise ValueError("Покупка не привязана к продавцу.")
                if quote and (type(purchase.get("quantity")) is not int or purchase.get("currency") != quote["currency"]
                              or purchase.get("price") != quote["price"] * purchase["quantity"]):
                    raise ValueError("Цена покупки не совпадает с подтверждённой ценой продавца.")
                trade_log.append(settle_offer(current,quote,purchase["quantity"]) if quote else apply_purchase(current,purchase))
            except ValueError as exc:
                # A failed settlement must not replace the matching offer with a
                # stock figure narrated for a sale that never committed.
                if isinstance(purchase, dict):
                    for old in stored_offers:
                        if same_item(old.get("item"), purchase.get("item")) and old.get("offer_id"):
                            current["shop_offers"] = [old if offer.get("offer_id") == old["offer_id"] else offer
                                                      for offer in current["shop_offers"]]
                trade_log.append(f"Отклонено: {exc}")
    resources = new_state.pop("resource_events", {})
    resources = resources if isinstance(resources, dict) else {}
    if not new_character:
        for group, fields in (
            ("inventory", ("inventory", "inventory_remove")),
            ("wallet", ("gold", "silver", "copper")),
        ):
            evidence = resources.get(group)
            cancelled = PURCHASE_CANCELLED.search(latest) and isinstance(evidence, str) and re.search(
                r"покуп|прод|па[её]к|серебрян|золот|медн|цен|плат", evidence, re.I)
            if cancelled or not _grounded_event(evidence, latest):
                for field in fields:
                    new_state.pop(field, None)
    inventory = current.get("inventory")
    if isinstance(inventory, list):
        current["inventory"] = _remove_inventory_items(inventory, new_state.get("inventory_remove"))
        proposed_items = new_state.get("inventory")
        proposed_items = proposed_items if isinstance(proposed_items, list) else []
        if not new_character and receives_sealed_letter(action, latest) and not any(
                SEALED_LETTER.search(_item_name(item))
                for item in [*current["inventory"], *proposed_items]):
            current["inventory"].extend(_inventory_with_ids(["Запечатанное письмо"]))
    merged = dict(current)
    merged["pending_check"] = next_check
    for k, v in new_state.items():
        if v is not None and k not in {"inventory_remove", "level_upgrade", "level_history", "hp_delta", "hp_event",
                                          "quests", "quest_xp_awards", "quest_xp_paid_ids", "last_state_event_id"}:
            merged[k] = v
    if isinstance(current.get("stats"), dict) and isinstance(new_state.get("stats"), dict):
        merged["stats"] = {**current["stats"], **new_state["stats"]}
    old_quests = current.get("quests")
    quest_changes = new_state.get("quests")
    merged["quests"] = _merge_quests(
        old_quests if isinstance(old_quests, list) else [],
        quest_changes if isinstance(quest_changes, list) else [],
    )
    # Preserve arrays when the model accidentally returns empty/missing arrays.
    for f in ("inventory", "notes", "conditions", "relationships", "faction_clocks",
              "dc_modifier_reasons", "proficiencies", "skills", "abilities"):
        if f not in new_state:
            continue
        if f in ("conditions", "dc_modifier_reasons") and isinstance(new_state[f], list):
            merged[f] = new_state[f]
            continue
        old = current.get(f, [])
        new_items = merged.get(f, [])
        if not isinstance(old, list): old = []
        if not isinstance(new_items, list): new_items = old
        if not new_items and old:
            merged[f] = old
        elif old and new_items:
            # Keep existing entries omitted by a model response. Identity by id/name/title,
            # otherwise stable JSON; this conservative merge avoids accidental data loss.
            def ident(item):
                if f == "inventory" and _item_name(item):
                    return _item_name(item)
                if isinstance(item, dict):
                    return item.get("id") or item.get("name") or item.get("title") or json.dumps(item, sort_keys=True, ensure_ascii=False)
                return str(item)
            old_by_id = {ident(item): item for item in old}
            seen = {ident(item) for item in new_items}
            if f in ("notes", "skills", "abilities"):
                updated_items = []
                for item in new_items:
                    previous = old_by_id.get(ident(item))
                    if isinstance(item, dict) and isinstance(previous, dict):
                        item = {**previous, **item}
                    updated_items.append(item)
                new_items = updated_items
            merged[f] = list(new_items) + [item for item in old if ident(item) not in seen]
    # Server-authoritative XP: only explicit new rewards, never double-counted.
    xp = max(0, int(current.get("xp", 0) or 0))
    processed = [event for event in current["xp_event_ids"] if isinstance(event, str)]
    paid = current.get("quest_xp_awards")
    paid = {name for name in paid if isinstance(name, str)} if isinstance(paid, list) else set()
    paid_ids = current.get("quest_xp_paid_ids")
    paid_ids = {value for value in paid_ids if isinstance(value, str)} if isinstance(paid_ids, list) else set()
    quests_by_name = {_quest_name(quest): quest for quest in merged["quests"]}
    paid_ids.update(quest["id"] for name, quest in quests_by_name.items()
                    if name in paid and isinstance(quest.get("id"), str))
    old_completed = {_quest_name(quest) for quest in old_quests or []
                     if isinstance(quest, dict) and quest.get("status") == "completed"}
    newly_completed = {
        name: quest for name, quest in quests_by_name.items()
        if name and name not in old_completed and quest.get("status") == "completed" and not _quest_steps_open(quest)
    }
    conditional_awards, controls_wallet = _apply_conditional_rewards(
        merged, current, newly_completed, reward_outcomes, latest,
    )
    if controls_wallet:
        for currency in ("gold", "silver", "copper"):
            merged[currency] = current.get(currency, 0) + conditional_awards.get(currency, 0)
    xp += conditional_awards.get("xp", 0)
    for event_id, awards in fresh_events:
        for amount, name in awards:
            if not name and event_id == last_event_id and controls_wallet:
                continue
            if not name and event_id == last_event_id and len(newly_completed) == 1:
                candidate, quest = next(iter(newly_completed.items()))
                reward = quest.get("rewards")
                if isinstance(reward, dict) and reward.get("xp") == amount:
                    name = candidate
            if name:
                quest = quests_by_name.get(name)
                if (name in paid or not quest or quest.get("reward_condition") or str(quest.get("id", "")) in paid_ids
                        or quest.get("status") != "completed"
                        or _quest_steps_open(quest)):
                    continue
                reward = quest.get("rewards")
                promised = reward.get("xp") if isinstance(reward, dict) else None
                if type(promised) is int and promised > 0:
                    amount = promised
                paid.add(name)
                if isinstance(quest.get("id"), str):
                    paid_ids.add(quest["id"])
            xp += amount
        processed.append(event_id)
    for name, quest in newly_completed.items():
        reward = quest.get("rewards")
        promised = reward.get("xp") if isinstance(reward, dict) else None
        if (not quest.get("reward_condition") and name not in paid and str(quest.get("id", "")) not in paid_ids
                and type(promised) is int and promised > 0):
            xp += promised
            paid.add(name)
            if isinstance(quest.get("id"), str):
                paid_ids.add(quest["id"])
    merged["quest_xp_awards"] = sorted(paid)
    merged["quest_xp_paid_ids"] = sorted(paid_ids)
    if last_event_id:
        merged["last_state_event_id"] = last_event_id
    merged["xp"] = xp
    merged["xp_event_ids"] = processed
    # Protect core scalar values from blank/null model output.
    for f in ("name", "race", "class", "background", "position"):
        if not merged.get(f) and current.get(f):
            merged[f] = current[f]
    if not new_character:
        # A status line can describe old damage. Only apply a delta tied to the new narration.
        before = int(current.get("hp", 0) or 0)
        delta = new_state.get("hp_delta")
        evidence = new_state.get("hp_event")
        applied = rules_result(current, result)
        grounded = (_grounded_event(evidence, latest) and not any(
            evidence.strip() in message.get("content", "") for message in history[-8:]
            if message.get("role") == "assistant"
        ))
        if applied:
            merged["hp"] = applied["hp_after"]
        elif rest == "long_rest" and before > 0:
            merged["hp"] = current.get("max_hp", DEFAULT_STATE["max_hp"])
        else:
            merged["hp"] = before + delta if type(delta) is int and grounded and not (
                delta < 0 and descriptive_damage(evidence)) else before
        merged["max_hp"] = current.get("max_hp", DEFAULT_STATE["max_hp"])
    merged["hp"] = max(0, min(int(merged.get("hp", 0) or 0), int(merged.get("max_hp", 1) or 1)))
    previous_level = max(1, int(current.get("level", 1) or 1))
    derived_level = level_for_xp(merged["xp"])
    merged["level"] = derived_level
    merged["xp_next"] = xp_threshold_for_level(derived_level)
    confirmation = messages[-1].get("content", "") if messages and messages[-1].get("role") == "assistant" else ""
    confirmed_levels = {int(level) for level in LEVEL_UPGRADE_RE.findall(confirmation)}
    applied_upgrade = (next_upgrade_level in confirmed_levels
                       and _apply_level_upgrade(merged, new_state.get("level_upgrade"), next_upgrade_level))
    pending = (previous_level - next_upgrade_level + 1) if next_upgrade_level is not None else 0
    merged["level_pending"] = max(0, pending + max(0, derived_level - previous_level) - int(applied_upgrade))
    for currency in ("gold", "silver", "copper"):
        amount = merged.get(currency)
        if type(amount) is not int:
            amount = current.get(currency, 0)
        merged[currency] = max(0, amount) if type(amount) is int else 0
    if str(merged.get("cover", "")).strip().casefold() in {"", "без укрытия", "нет укрытия"}:
        merged["cover"] = "нет"
    merged["dc_modifier"] = int(merged.get("dc_modifier", 0) or 0)
    for f in ("race", "class", "background", "position", "cover", "weapon_in_hand"):
        if not isinstance(merged.get(f), str): merged[f] = current.get(f, DEFAULT_STATE[f])
    if not isinstance(merged.get("restrained"), bool): merged["restrained"] = bool(current.get("restrained", False))
    for f in ("inventory", "notes", "quests", "conditions", "relationships", "faction_clocks",
              "dc_modifier_reasons", "proficiencies", "skills", "abilities", "level_history", "xp_event_ids"):
        if not isinstance(merged.get(f), list): merged[f] = current.get(f, [])
    for item in merged.get('inventory',[]):
        if isinstance(item,dict) and 'mechanics' in item:
            spec=equipment_spec(item['mechanics'])
            if spec: item['mechanics']=spec
            else: item.pop('mechanics',None)
    merged["notes"] = [n for n in (normalize_note(x) for x in merged["notes"]) if n]
    proposed_spells = learned_spell_from_story(latest, merged.get("name"))
    blueprints = blueprint_proposals if isinstance(blueprint_proposals, list) else []
    design_log = apply_blueprints(merged, [*blueprints, *proposed_spells], latest)
    attach_learned_spell(merged, proposed_spells)
    attach_item_blueprints(merged)
    if new_character:
        apply_character_blueprints(merged)
        merged.pop("character_setup_pending", None)
    else:
        blocked = set()
        if rules_result(current, result): blocked.add("hero")
        if npc_outcome:
            blocked.add(npc_outcome["id"])
        elif resolved_ammo and resolved_check.get("world_target_id"):
            blocked.add(resolved_check["world_target_id"])
        elif ranged_check:
            blocked.add(ranged_check["world_target_id"])
        effect_proposals = effect_proposals if isinstance(effect_proposals, list) else []
        if protected_target:
            names = set(entity_names(protected_target))
            effect_proposals = [effect for effect in effect_proposals if not (
                isinstance(effect, dict) and effect.get("type") == "damage"
                and ((effect.get("target") == ranged_target)
                     or (isinstance(effect.get("target"), str) and effect["target"] in names)
                     or (isinstance(effect.get("target"), dict) and isinstance(effect["target"].get("name"), str)
                         and effect["target"]["name"] in names)))]
        if "hero" not in blocked:
            effect_proposals = complete_hero_damage(current, action, latest, effect_proposals)
        hp_base = merged["hp"] if rest == "long_rest" else current["hp"]
        merged = resolve_effects(merged, effect_proposals, latest, minute, blocked,
                                 hero_before=(hp_base, merged["hp"]))
        if ((minute == previous_minute or previous_minute is None) and not plan
                and not (resolved_previous and previous_check.get('clock_spent'))):
            logs=merged.get('effect_log',[])[:]
            advance_action(merged,6)
            merged['effect_log']=logs+merged['effect_log']
        for entity in merged.get('world_entities',[]):
            if isinstance(entity,dict): sync_life(entity)
        merged["effect_log"].extend([*ammo_log, *trade_log])
        merged["combat"] = combat_context(merged)
        check = merged.get("pending_check")
        if check and check.get("kind") != "save" and hero_incapacitated(merged):
            merged["pending_check"] = check = None
        if check and not check.get("ammo") and check.get("id") != (previous_check or {}).get("id") and isinstance(proposed, dict):
            stat = proposed.get("stat")
            if isinstance(stat, str) and stat in STATS and check.get("kind") in ("check", "attack", "save"):
                check["stat"] = stat
                check["effect_bonus"] = check_bonus(merged, stat)
    merged["effect_log"] = [*(merged.get("effect_log") or []), *design_log]
    return merged


# ─── Сохранения ────────────────────────────────────────────────────────────
def _safe_name(raw):
    if not isinstance(raw, str):
        return ""
    normalized = re.sub(r"(?<!\d)\.|\.(?!\d)", "", unicodedata.normalize("NFC", raw))
    name = " ".join("".join(char for char in normalized if char.isalnum() or char in " _-.—–").split())
    name = name.encode("utf-8")[:128].decode("utf-8", errors="ignore").strip().rstrip(".")
    return name if any(char.isalnum() for char in name) else ""


def _save_path(cid, name):
    """Файл сохранения конкретного игрока: <clientId>~<имя>.json"""
    return SAVES_DIR / f"{cid}~{name}.json"


def _save_backup(path):
    return path.with_name(path.stem + ".bak.json")


def _save_candidates(cid, name):
    """Сначала своё сохранение, потом общее (старые файлы без префикса)."""
    owner = _owner()
    private = [_save_path(owner, name)] if owner else []
    return private + [_save_path(cid, name), SAVES_DIR / f"{name}.json"]


def _collect_saves(cid, *, shared_saves=True):
    """Имя → путь. Свои сохранения перекрывают одноимённые общие."""
    prefix = f"{cid}~"
    own, shared = {}, {}
    for p in SAVES_DIR.glob("*.json"):
        if p.name.endswith(".bak.json"):
            continue
        stem = p.stem
        if stem.startswith(prefix):
            own[stem[len(prefix):]] = p
        elif shared_saves and "~" not in stem:
            shared[stem] = p
    merged = dict(shared)
    merged.update(own)
    return merged


def _save_info(name, path):
    """Имя, время последней записи (когда сохранили) и размер файла."""
    try:
        st = path.stat()
        return {"name": name, "saved_at": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"name": name, "saved_at": 0, "size": 0}


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "app_revision": APP_REVISION,
        "qwen_output_limits": {
            "summary": QWEN_SUMMARY_LIMIT if _is_yandex_qwen(SUMMARY_MODEL) else None,
            "state": QWEN_STATE_LIMIT if _is_yandex_qwen(EXTRACT_MODEL) else None,
            "state_setup": QWEN_STATE_SETUP_LIMIT if _is_yandex_qwen(EXTRACT_MODEL) else None,
        },
        "narration_max_tokens": NARRATION_MAX_TOKENS,
        "inline_state_enabled": INLINE_STATE_ENABLED,
        "inline_state_extra_tokens": INLINE_STATE_EXTRA_TOKENS,
        "presence_limit": PRESENCE_LIMIT,
        "narration_setup_tokens": NARRATION_SETUP_TOKENS,
        "narration_retry_tokens": NARRATION_RETRY_TOKENS,
        "recent_history_chars": RECENT_HISTORY_CHARS,
        "summary_input_chars": SUMMARY_INPUT_CHARS,
        "summary_max_tokens": SUMMARY_MAX_TOKENS,
        "frequency_penalty": FREQ_PENALTY,
        "presence_penalty": PRES_PENALTY,
        "provider": PROVIDER,
        "model": MODEL,
        "extract_model": EXTRACT_MODEL,
        "summary_model": SUMMARY_MODEL,
        "base_url_configured": bool(BASE_URL),
        "llm_max_concurrent": LLM_MAX_CONCURRENT,
        "llm_queue_timeout": LLM_QUEUE_TIMEOUT,
        "llm_timeout": LLM_TIMEOUT,
        "saves_dir": str(SAVES_DIR),
        "sessions_cached": len(_SUMMARY_STATE),
    })


@app.route("/api/diag", methods=["GET", "POST"])
def api_diag():
    """Диагностика: только администратор, платный ping по явному POST."""
    password = os.getenv('TOKEN_ADMIN_PASSWORD','')
    if not password: return Response('Not found',404)
    auth=request.authorization
    if not auth or auth.type!='basic' or auth.username!='admin' or not secrets.compare_digest((auth.password or '').encode(),password.encode()):
        return Response('Authentication required',401,{'WWW-Authenticate':'Basic realm="Diagnostics"'})
    result = {
        "provider": PROVIDER,
        "model": MODEL,
        "extract_model": EXTRACT_MODEL,
        "summary_model": SUMMARY_MODEL,
        "narration_reasoning": _REASONING_STATE["fix"] if _REASONING_STATE["fix"] is not None else CHAT_REASONING_EFFORT,
        "base_url": BASE_URL or "https://api.openai.com/v1",
        "key_set": bool(API_KEY),
        "env_file": _ENV_FILE.name if _ENV_FILE else None,
        "llm_timeout": LLM_TIMEOUT,
        "saves_dir": str(SAVES_DIR),
    }
    if BASE_URL and ("127.0.0.1" in BASE_URL or "localhost" in BASE_URL):
        result["hint"] = ("base_url указывает на localhost — Ollama видна только с той же "
                          "машины. На Railway так не работает: нужен облачный провайдер "
                          "(например https://openrouter.ai/api/v1).")
    if request.method != "POST": return jsonify(result)
    try:
        result["models"] = [m.id for m in client.models.list().data][:60]
        result["model_present"] = MODEL in result["models"]
    except Exception as exc:
        result["models_error"] = _short_error(exc)
    try:
        with llm_slot(20) as ok:
            if not ok:
                raise RuntimeError("нет свободного слота модели")
            resp = _paid_completion(
                model=MODEL, temperature=0, max_tokens=256 if USE_YANDEX else 8,
                messages=[{"role": "user", "content": "Ответь одним словом: ок"}])
        result["ping"] = (resp.choices[0].message.content or "").strip()
        result["ping_ok"] = bool(result["ping"])
        if not result["ping_ok"]:
            result["ping_error"] = "Модель вернула пустой ответ."
    except Exception as exc:
        result["ping_ok"] = False
        result["ping_error"] = _short_error(exc)
        hint = _error_hint(exc)
        if hint:
            result["ping_hint"] = hint
    return jsonify(result)


@app.route("/")
def index():
    visitor_id, presence = _admit_visitor()
    if not presence["admitted"]:
        return _presence_response(render_template("waiting.html", presence=presence), visitor_id, 503)
    return _presence_response(render_template("index.html", presence=presence), visitor_id)


@app.route("/api/presence", methods=["POST"])
def api_presence():
    visitor_id, presence = _admit_visitor()
    return _presence_response(jsonify(presence), visitor_id)


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Неверный номер партии или хода.")
    return value


def _revision(data):
    revision = data.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Неверная версия партии.")
    return revision


def _snapshot_response(snapshot):
    if isinstance(snapshot, dict) and isinstance(snapshot.get("draft"), dict):
        draft = snapshot["draft"]
        snapshot = {**snapshot, "draft": {**draft, "reply": {k: v for k, v in draft["reply"].items()
                                                            if k not in {"_state_delta", "_engine_state"}}}}
    response = jsonify(snapshot)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(PartyMissing)
def _party_missing(exc):
    return jsonify({"error": str(exc), "code": "party_missing"}), 404


@app.errorhandler(PartyConflict)
def _party_conflict(exc):
    return jsonify({"error": str(exc), "code": "party_conflict"}), 409


@app.errorhandler(sqlite3.Error)
def _party_storage_failure(exc):
    app.logger.error("Ошибка записи партии: %s", type(exc).__name__)
    return jsonify({"error": "Сохранение временно недоступно. Попробуйте ещё раз."}), 503


@app.route("/api/parties", methods=["GET", "POST"])
def api_parties():
    if request.method == "GET":
        return _snapshot_response(PARTIES.list(_owner()))
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Партия не прочитана."}), 400
    try:
        party_id = _request_id(data.get("id"))
        source = data.get("snapshot")
        if not isinstance(source, dict):
            raise ValueError("Партия не прочитана.")
        state, history = source.get("state"), source.get("messages")
        if (not isinstance(state, dict) or not isinstance(history, list)
                or any(not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system")
                       or not isinstance(m.get("content"), str) for m in history)):
            raise ValueError("Сохранение содержит недопустимые данные.")
        token = _owner_token() or secrets.token_urlsafe(32)
        response = _snapshot_response(PARTIES.create(_owner(token), party_id, state, history, source.get("memory")))
        if not _owner_token():
            _set_owner_cookie(response, token)
        return response
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/parties/<party_id>")
def api_party(party_id):
    return _snapshot_response(PARTIES.get(_owner(), party_id))


@app.route("/api/parties/<party_id>/discard", methods=["POST"])
def api_party_discard(party_id):
    data = request.get_json(silent=True) or {}
    try:
        return _snapshot_response(PARTIES.discard(
            _owner(), party_id, _revision(data), _request_id(data.get("request_id")),
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/parties/<party_id>/notes", methods=["POST"])
def api_party_note(party_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("id"), str) or data.get("action") not in ("read", "delete"):
        return jsonify({"error": "Не удалось прочитать изменение заметки."}), 400
    try:
        return _snapshot_response(PARTIES.note(
            _owner(), party_id, _revision(data), data["id"], data["action"] == "delete",
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/roll", methods=["POST"])
def api_roll():
    data = request.get_json(silent=True) or {}
    try:
        party = None
        if not isinstance(data,dict): return jsonify({"error":"Ожидается JSON-объект."}),400
        if "party_id" in data:
            party = PARTIES.get(_owner(), _request_id(data["party_id"]))
            if party["revision"] != _revision(data) or party["draft"]:
                raise PartyConflict("Партия изменилась или ждёт сохранения хода. Откройте её снова.")
        state, messages = ((party["state"], party["messages"]) if party else
                           (data.get("state") or {}, data.get("messages") or []))
        if not isinstance(state, dict) or not isinstance(messages, list):
            raise ValueError("Не удалось прочитать состояние проверки.")
        check = active_check(state, messages)
        if check and check.get("kind") != "save":
            restriction = control_restriction(state, "")
            if restriction:
                raise ValueError(restriction)
        expression = data.get("expr") or (check["expr"] if check else "1d20")
        if check and formula_key(expression) != formula_key(check["expr"]):
            raise ValueError(f"Ожидается бросок {check['expr']}. Сначала разрешите проверку.")
        roll = roll_dice(expression)
        roll["check_id"] = check["id"] if check else None
        if party:
            roll.update(party_id=party["id"], revision=party["revision"])
        result = roll_result(check, roll)
        if check and check.get("actor_id") and check.get("kind") == "attack" and result.get("success") and not check.get("practice"):
            attacker = next((record for record in state.get("world_entities", [])
                             if isinstance(record, dict) and record.get("id") == check["actor_id"]), None)
            if attacker:
                roll["npc_attack_damage"] = roll_dice(profile(attacker)["dice"])["total"]
                result = roll_result(check, roll)
        if check and check.get("ammo"):
            require_ammo(state, check["ammo"])
        if check and check.get("ammo") and check.get("world_target_id") and result.get("success"):
            target = next((entry for entry in state.get("world_entities", [])
                           if isinstance(entry, dict) and entry.get("id") == check.get("world_target_id")), None)
            if not target:
                raise ValueError("Цель выстрела больше недоступна.")
            damage = roll_dice(check["ranged_dice"])["total"]
            roll["ranged_damage"] = adjust_damage(damage, check["ranged_element"], profile(target)["resistances"])
            result = roll_result(check, roll)
        result["receipt"] = ROLL_SIGNER.dumps(roll)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/chat", methods=["POST"])
def api_chat():
    started_at = perf_counter()
    data = request.get_json(silent=True) or {}
    party = None
    request_id = None
    owner = _owner()
    if not isinstance(data,dict): return jsonify({"error":"Ожидается JSON-объект."}),400
    if "party_id" in data:
        try:
            request_id = _request_id(data.get("request_id"))
            party, staged = PARTIES.turn(owner, _request_id(data["party_id"]), _revision(data), request_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if staged:
            return Response(staged["reply"]["content"] + TURN_COMPLETE,
                            mimetype="text/plain; charset=utf-8",
                            headers={"Cache-Control": "no-store", "X-Turn-Id": request_id})
        move = data.get("action")
        if (not isinstance(move, dict) or not isinstance(move.get("content"), str)
                or not move["content"].strip()):
            return jsonify({"error": "Действие игрока отсутствует."}), 400
        action = {"role": "user", "content": move["content"]}
        if isinstance(move.get("roll_receipt"), str):
            action["roll_receipt"] = move["roll_receipt"]
        messages = party["messages"] + [action]
        game_state = party["state"]
    else:
        if not ALLOW_LEGACY_LLM: return jsonify({"error":"Импортируйте сохранение в приключение перед игрой."}), 403
        messages = data.get("messages") or []
        game_state = data.get("state") or DEFAULT_STATE
    if not isinstance(messages, list) or not messages or any(
        not isinstance(message, dict) or not isinstance(message.get("content"), str) for message in messages
    ):
        return jsonify({"error": "История хода повреждена."}), 400
    if not isinstance(game_state, dict):
        game_state = DEFAULT_STATE
    setup_turn = bool(game_state.get("character_setup_pending")
                      or not (game_state.get("race") and game_state.get("class")))
    action = messages[-1]
    if len(action.get("content", "")) > 8000: return jsonify({"error":"Действие слишком длинное (макс. 8000 символов)."}),400
    history = messages[:-1]
    if party or messages[0].get("role") != "system":
        prompt = SYSTEM_PROMPT
        if setup_turn:
            prompt += CHARACTER_CREATION_RULES + "\nДля авторской расы или класса используй в листе базовые характеристики, чтобы конструктор мог применить бонусы один раз."
        messages = [{"role": "system", "content": prompt}] + [
            message for message in messages if message.get("role") != "system"
        ]
    check = active_check(game_state, history)
    if party and not action.get("roll_receipt"):
        try:
            direct = _engine_command(game_state, action.get("content", ""), check)
            if direct:
                updated, narration = direct
                staged = PARTIES.stage(owner, party["id"], party["revision"], request_id, action,
                                       {"role": "assistant", "content": narration, "_engine_state": updated})
                return Response(staged["reply"]["content"] + TURN_COMPLETE,
                                mimetype="text/plain; charset=utf-8",
                                headers={"Cache-Control": "no-store", "X-Turn-Id": request_id})
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "action_invalid"}), 400
    if action.get("content") == CANCEL_CHECK_COMMAND:
        if not check or check.get("kind") != "check":
            return jsonify({"error": "Нет необязательной проверки для отмены.", "code": "action_invalid"}), 400
        if party:
            try:
                PARTIES.stage(owner, party["id"], party["revision"], request_id, action,
                              {"role": "assistant", "content": CANCEL_CHECK_REPLY})
            except PartyConflict as exc:
                return _party_conflict(exc)
            except sqlite3.Error as exc:
                return _party_storage_failure(exc)
        return Response(CANCEL_CHECK_REPLY + TURN_COMPLETE, mimetype="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Turn-Id": request_id or uuid4().hex})
    try:
        result = _signed_roll(action, game_state, history, party=party)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if party and result:
        action["roll"] = result
        action["content"] = f"[[БРОСОК]] {result['expr']} = {result['total']}."
        action.pop("roll_receipt", None)
    state_context = {key: game_state[key] for key in STATE_FIELDS if key in game_state}
    query = str(action.get("content", ""))
    try:
        plan = wait_plan(game_state, query)
        if plan and check and not (hero_incapacitated(game_state) and check.get("kind") != "save"):
            raise ValueError("Сначала разрешите или отмените ожидающий бросок.")
        if check and check.get("ammo") and not result:
            raise ValueError(f"Сначала разрешите ожидающий выстрел броском {check['expr']}.")
        if not setup_turn and not result and not check and (ranged_weapon := narrated_weapon(game_state, query)):
            require_ammo(game_state, ranged_weapon["ammo"])
        restriction = control_restriction(
            game_state, query,
            waiting=bool(plan) or bool(result and (result.get("check") or {}).get("kind") == "save"),
        )
        if restriction and not setup_turn:
            raise ValueError(restriction)
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "action_invalid"}), 400
    minute = campaign_minute(game_state)
    if minute is not None:
        state_context["world_clock"] = {"start_minute": minute, "start_label": clock_label(minute), "second": game_state.get("world_second",0)}
        if plan:
            state_context["world_clock"].update(end_minute=plan["until"], end_label=plan["label"])
    agenda = [event for event in (game_state.get("agenda") or [])
              if isinstance(event, dict) and event.get("status") == "scheduled"]
    if agenda:
        state_context["agenda"] = agenda[:12]
    event_outcomes = _recent_event_outcomes(game_state)
    if event_outcomes:
        state_context["recent_event_outcomes"] = event_outcomes
    if plan:
        state_context["wait_plan"] = plan
    scene = game_state.get("scene")
    state_context["scene"] = scene if isinstance(scene, dict) else {}
    lookup = " ".join((query, check.get("title", "") if check else "",
                       str(state_context["scene"].get("goal", ""))))
    state_context["facts"] = facts_context(game_state.get("facts"), lookup)
    state_context["journal"] = notes_context(game_state.get("notes"), lookup)
    state_context["world_entities"] = world_context(game_state, lookup, player_query=query)
    state_context["active_effects"] = effect_context(game_state)
    resolutions = [message["resolution"][:220] for message in history[-32:]
                   if message.get("role") == "assistant" and isinstance(message.get("resolution"), str)
                   and any(marker in message["resolution"] for marker in ("Отклонено:", "Размен выполнен:", "Куплено:"))]
    if resolutions:
        state_context["verified_trades"] = resolutions[-5:]
    if designs := constructor_context(game_state, query):
        state_context["constructor"] = designs
    rounds=game_state.get('combat_rounds',0)
    cooldowns=game_state.get('ability_cooldowns')
    if type(rounds) is int and isinstance(cooldowns,dict):
        state_context['ability_cooldowns']={name:max(0,until-rounds-1) for name,until in cooldowns.items() if type(until) is int}
    if battle := combat_context(game_state):
        state_context["combat"] = {"target_id": battle["target_id"], "round": battle.get("round", 0),
                                   "enemies": combat_status(game_state, battle)}
    if market := current_offers(game_state):
        state_context["shop_offers"] = [{k: offer[k] for k in ("item", "currency", "price", "seller_id", "stock", "offer_id") if k in offer} for offer in market[:8]]
    if isinstance(game_state.get("hero_form"), dict):
        state_context["hero_form"] = game_state["hero_form"]
    if type(game_state.get("mana")) is int:
        state_context.update(mana=game_state["mana"], max_mana=game_state.get("max_mana"))
    elif not setup_turn:
        from engine.combat import hero_stat
        capacity=6+max(0,hero_stat(game_state,'int'))*2
        state_context.update(mana=capacity,max_mana=capacity)
    if result:
        state_context["roll_result"] = result
        state_context["rule_effect"] = rules_result(game_state, result)
        state_context["npc_rule_effect"] = npc_damage(game_state, result)
        if (result.get("check") or {}).get("ammo"):
            kind = result["check"]["ammo"]
            state_context["ranged_result"] = {"hit": result["success"], "ammo_remaining": ammo_available(game_state, kind) - 1}
    elif check:
        state_context["pending_check"] = check
    if not result and query.startswith("[[БРОСОК]]"):
        return jsonify({"error": "Используйте команду /roll или кнопку проверки."}), 409
    next_upgrade_level = _next_upgrade_level(game_state)
    if next_upgrade_level is not None:
        state_context["next_upgrade_level"] = next_upgrade_level
    quest_context = _quest_context(game_state.get("quests"), game_state.get("quest_xp_awards") or [])
    if quest_context:
        state_context["quests"] = quest_context
    abilities = normalize_abilities(game_state.get("abilities"))
    if abilities:
        known_abilities = []
        descriptions_left = 720
        for ability in abilities:
            if not isinstance(ability, dict) or not isinstance(ability.get("name"), str):
                continue
            entry = {key: ability[key] for key in ("name", "uses_left", "uses_max", "recharge", "mechanics") if key in ability}
            description = ability.get("description")
            if isinstance(description, str) and description.strip() and descriptions_left:
                if (ability["name"].casefold() == "второе дыхание"
                        and "воин" in str(game_state.get("class", "")).casefold()):
                    description = f"Восстановить 1d10+{game_state.get('level', 1)} HP."
                excerpt = description.strip()[:min(180, descriptions_left)]
                entry["description"] = excerpt
                descriptions_left -= len(excerpt)
            known_abilities.append(entry)
        state_context["abilities"] = known_abilities
    inventory = game_state.get("inventory")
    if isinstance(inventory, list):
        state_context["inventory"] = [
            {key: item[key] for key in ("id", "name", "quantity", "mechanics", "world_id") if key in item}
            if isinstance(item, dict) else item for item in _inventory_with_ids(inventory)
        ]
    skills = game_state.get("skills")
    if isinstance(skills, list):
        skill_bonuses = {
            skill["name"]: skill["bonus"]
            for skill in skills
            if isinstance(skill, dict) and isinstance(skill.get("name"), str)
            and isinstance(skill.get("bonus"), int)
        }
        if skill_bonuses:
            state_context["skill_bonuses"] = skill_bonuses
    if setup_turn:
        # Пустые числа заготовки только сбивали модель: на первом ходу она их копировала.
        for field in ("hp", "max_hp", "gold", "silver", "copper", "level", "xp", "xp_next", "stats"):
            state_context.pop(field, None)
        if state_context.get("name") == DEFAULT_STATE["name"]:
            state_context.pop("name")
    # Для создания героя формат задаёт CHARACTER_CREATION_RULES.
    turn_rule = ("Используй roll_result как окончательный результат именно указанной проверки. "
                 "verified_trades и [Итог движка] подтверждают состояние. Отклонённая покупка не состоялась. "
                 "Если указан rule_effect, используй рассчитанный HP и не применяй изменение повторно. "
                 "Свободный бросок без check не разрешает действие. При pending_check сохраняй "
                 "ожидание, пока действие имеет смысл. Если игрок достиг цели другим способом или ушёл, "
                 "явно скажи, что прежняя необязательная проверка снята. Объявляй каждую формулу /roll один раз. "
                 "scene — текущая сцена. Подтверждённые npc_action в facts — произошедшие действия. "
                 "Слова персонажей и догадки не меняют эти факты, даже если говорящий отрицает своё действие. "
                 "world_clock.start_label — время до хода. Для ожидания бери итог только из world_clock.end_label; "
                 "не называй исходное время текущим. Для обычного хода называй только новые фактически прошедшие минуты. "
                 "Если roll_result.check.clock_spent задан, время начала проверки уже списано. "
                 "Если результат запускает новый интервал, назови его дополнительным и объясни, на что он ушёл. "
                 "Дай конкретный исход по каждому наступившему событию, особенно из wait_plan.due. "
                 "При check_in отсутствие вестей допустимо, но назови новый проверяемый путь. "
                 "Обещанному событию назначай срок проверки, если можешь; не обещай заведомо неизвестное прибытие. "
                 "Не отправляй игрока снова проверять уже проверенное место без нового повода. "
                 "Если reward_condition задано, разделяй завершение и право на оплату: сообщи, исполнено ли условие, "
                 "а подтверждение фактической выплаты оставь серверу. Не добавляй XP или монеты по такому квесту.")
    length_note = "" if setup_turn else "\n\n" + turn_rule + "\n\n" + LENGTH_NOTE
    state_note = STATE_NOTE_SETUP if setup_turn else STATE_NOTE
    messages = messages[:1] + [{
        "role": "system",
        "content": (state_note + json.dumps(state_context, ensure_ascii=False) + length_note),
    }] + messages[1:]
    inline_enabled = bool(party and not setup_turn and next_upgrade_level is None and INLINE_STATE_ENABLED)
    if inline_enabled:
        messages[0]["content"] += ("\n\n" + PROTOCOL_PROMPT + STATE_EXTRACT_PROMPT + WORLD_PROMPT
                                     + EFFECT_PROMPT + COMBAT_PROMPT + TRADE_PROMPT + CONSTRUCTOR_PROMPT)
    else:
        messages[0]["content"] += "\nУчитывай active_effects; тики рассчитывает сервер. Используй npc_rule_effect как окончательный результат урона существу, если он указан."
    turn_id = request_id or uuid4().hex
    messages = _messages_with_memory(
        messages, _header_token("X-Session-Id", 64), character_setup=setup_turn,
        party_id=party["id"] if party else None,
    )

    if inline_enabled:
        messages[0]["content"] += "\nПосле обычного видимого ответа обязательно добавь блок [[STATE_DELTA]]…[[/STATE_DELTA]] согласно протоколу. Это не часть видимой карточки. Не превышай обычную длину повествования; оставь место для JSON."
    visitor_id = request.cookies.get(PRESENCE_COOKIE, "")
    narration_limit = NARRATION_SETUP_TOKENS if setup_turn else narration_budget(query)
    if inline_enabled:
        narration_limit += INLINE_STATE_EXTRA_TOKENS
    inline_data = {"delta": None}

    def generate():
        if not _check_presence(visitor_id, start_chat=True)["admitted"]:
            yield "\n[Ошибка API: место в игре пока недоступно.]"
            return
        queued_at = perf_counter()
        if not LLM_SEM.acquire(timeout=LLM_QUEUE_TIMEOUT):
            _finish_chat_presence(visitor_id)
            yield ("\n[Ошибка API: сервер занят — все слоты модели заняты. "
                   "Подождите минуту и повторите.]")
            return
        slot_at = perf_counter()
        first_text_at = None
        last_usage = None
        attempts = 0
        emitted = False
        try:
            if setup_turn:
                # Создание героя отдаём целиком: обрыв на середине листа показал бы
                # половину карточки, а «дописать» уже отправленный текст нельзя.
                for limit, effort in ((NARRATION_SETUP_TOKENS, CHAT_REASONING_EFFORT),
                                      (NARRATION_RETRY_TOKENS, NARRATION_RETRY_EFFORT)):
                    attempts += 1
                    diag = {}
                    response = None
                    text = ""
                    try:
                        response = _start_completion(messages, max_tokens=limit,
                                                     reasoning_effort=effort, diag=diag)
                    except Exception as e:
                        app.logger.exception("Модель недоступна")
                        hint = _error_hint(e)
                        yield f"\n[Ошибка API: {_short_error(e)}" + (f" {hint}" if hint else "") + "]"
                        return
                    choice = response.choices[0] if getattr(response, "choices", None) else None
                    message = getattr(choice, "message", None)
                    text = (getattr(message, "content", "") or "").strip()
                    finish = getattr(choice, "finish_reason", None)
                    usage = getattr(response, "usage", None)
                    last_usage = usage or last_usage
                    TOKEN_USAGE.record(turn_id, "chat",
                                       getattr(response, "model", None) or MODEL, usage)
                    if diag.get("reasoning_rejected"):
                        _REASONING_STATE["fix"] = ""
                    if text and finish != "length" and sheet_identity(text):
                        if attempts > 1 and not diag.get("reasoning_rejected"):
                            _REASONING_STATE["fix"] = effort or ""
                        emitted = True
                        yield text
                        yield TURN_COMPLETE
                        return
                    print(
                        f"[chat] неполный лист персонажа: попытка={attempts} limit={limit} "
                        f"effort={effort or '—'} finish={finish} chars={len(text)} "
                        f"reasoning_tokens={getattr(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens', None)}",
                        flush=True,
                    )
                yield "\n[Ошибка API: мастер не вернул полный лист персонажа. Повторите тот же ход.]"
                return
            learned = _REASONING_STATE["fix"]
            variants = [
                (narration_limit, CHAT_REASONING_EFFORT if learned is None else learned),
                # Повтор: лимит выше, рассуждения выключены. Провайдер может
                # отклонить режим — тогда останется хотя бы увеличенный лимит.
                (NARRATION_RETRY_TOKENS + (INLINE_STATE_EXTRA_TOKENS if inline_enabled else 0), NARRATION_RETRY_EFFORT),
            ]
            for limit, effort in variants:
                attempts += 1
                diag = {}
                stream = None
                usage = None
                model = MODEL
                parser = DeltaStream() if inline_enabled else None
                content_chars = 0
                reasoning_chars = 0
                finish = None
                try:
                    try:
                        stream = _start_stream(messages, max_tokens=limit,
                                               reasoning_effort=effort, diag=diag)
                    except Exception as e:
                        app.logger.exception("Модель недоступна")
                        hint = _error_hint(e)
                        yield f"\n[Ошибка API: {_short_error(e)}" + (f" {hint}" if hint else "") + "]"
                        return
                    for chunk in stream:
                        if getattr(chunk, "usage", None) is not None:
                            usage = chunk.usage
                            model = getattr(chunk, "model", None) or model
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        if getattr(choice, "finish_reason", None):
                            finish = choice.finish_reason
                        delta = getattr(choice, "delta", None)
                        text = getattr(delta, "content", None) if delta is not None else None
                        if not text:
                            # Некоторые прокси игнорируют stream=true и присылают
                            # готовый ответ целиком, без дельт.
                            message = getattr(choice, "message", None)
                            text = getattr(message, "content", None) if message is not None else None
                        if text and parser is not None:
                            text = parser.feed(text)
                        if text:
                            content_chars += len(text)
                            if first_text_at is None:
                                first_text_at = perf_counter()
                            emitted = True
                            yield text
                        elif delta is not None:
                            reasoning = (getattr(delta, "reasoning_content", None)
                                         or getattr(delta, "reasoning", None))
                            if reasoning:
                                reasoning_chars += len(reasoning)
                except Exception as e:
                    app.logger.exception("Стрим прервался")
                    if emitted or isinstance(e,BudgetExceeded):
                        hint = _error_hint(e)
                        yield f"\n[Ошибка API: {_short_error(e)}" + (f" {hint}" if hint else "") + "]"
                        return
                    # До первого текста ничего не отправлено — повтор безопасен.
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    finally:
                        if usage is not None:
                            last_usage = usage
                        TOKEN_USAGE.record(turn_id, "chat", model, usage)
                if emitted and parser is not None:
                    inline_data["delta"] = parser.finish()
                if emitted and finish != "stop" and not (finish == "length" and parser is not None and parser.in_delta):
                    yield "\n[Ошибка API: ответ мастера оборвался. Ход можно повторить.]"
                    return
                if diag.get("reasoning_rejected"):
                    _REASONING_STATE["fix"] = ""
                if emitted:
                    if not diag.get("reasoning_rejected") and attempts > 1:
                        # Запоминаем рабочие параметры, чтобы не платить за повтор каждый ход.
                        _REASONING_STATE["fix"] = effort or ""
                    yield TURN_COMPLETE
                    return
                print(
                    f"[chat] пустой ответ: попытка={attempts} limit={limit} "
                    f"effort={effort or '—'} finish={finish} content_chars={content_chars} "
                    f"reasoning_chars={reasoning_chars} "
                    f"reasoning_tokens={getattr(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens', None)}",
                    flush=True,
                )
            if not emitted:
                yield ("\n[Ошибка API: модель не вернула текст. Обычно это значит, что "
                       "весь лимит вывода ушёл на скрытые рассуждения. Отправьте сообщение "
                       "ещё раз; если повторяется — задайте в Railway CHAT_REASONING_EFFORT=none "
                       "и перезапустите сервис.]")
        finally:
            LLM_SEM.release()
            _finish_chat_presence(visitor_id)
            print(
                f"[chat] queue_ms={round((slot_at - queued_at) * 1000)} "
                f"first_text_ms={round((first_text_at - started_at) * 1000) if first_text_at is not None else '-'} "
                f"response_ms={round((perf_counter() - started_at) * 1000)} "
                f"attempts={attempts} output_tokens={getattr(last_usage, 'completion_tokens', '-')}",
                flush=True,
            )

    def persist_reply():
        parts = []
        for chunk in generate():
            if chunk == TURN_COMPLETE and party:
                try:
                    staged_reply = {"role": "assistant", "content": "".join(parts)}
                    if inline_data["delta"] is not None:
                        staged_reply["_state_delta"] = inline_data["delta"]
                    PARTIES.stage(owner, party["id"], party["revision"], request_id, action, staged_reply)
                except PartyConflict:
                    yield "\n[Ошибка API: партия изменилась. Откройте её снова.]"
                    return
                except sqlite3.Error as exc:
                    app.logger.error("Не удалось записать ответ мастера: %s", type(exc).__name__)
                    yield "\n[Ошибка API: ответ пока не сохранился. Повторите тот же ход.]"
                    return
            elif party:
                parts.append(chunk)
            yield chunk

    return Response(
        stream_with_context(persist_reply()),
        mimetype="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Turn-Id": turn_id},
    )


def _synchronize_recap(content, state, roll):
    check = (roll or {}).get("check") or {}
    hp = state.get("hp")
    maximum = state.get("max_hp")
    if type(hp) is int and type(maximum) is int:
        content = re.sub(
            r"(\b(?:текущие HP(?: по листу)?|текущее здоровье|HP)\s*:\s*\*{0,2})"
            r"\d+\s*(?:/|из)\s*\d+",
            lambda match: f"{match[1]}{hp}/{maximum}", content, flags=re.I,
        )
    if check.get("ammo"):
        kind = check["ammo"]
        noun = r"стрел(?:а|ы|у)?" if kind == "arrow" else r"болт(?:а|ы|ов)?"
        content = re.sub(
            rf"(\bв инвентаре осталось\s*\*{{0,2}})\d+(?=\s+(?:обычн\w*\s+)?{noun}\b)",
            lambda match: f"{match[1]}{ammo_available(state, kind)}", content, flags=re.I,
        )
    return content


def _commit_party_turn(data):
    owner = _owner()
    party_id = _request_id(data["party_id"])
    request_id = _request_id(data.get("request_id"))
    revision = _revision(data)
    party, draft = PARTIES.turn(owner, party_id, revision, request_id)
    if not draft:
        raise PartyConflict("Ответ мастера ещё не сохранён.")
    if draft["committed_revision"] is not None:
        return _snapshot_response(party)
    history = party["messages"] + [draft["action"], draft["reply"]]
    state = party["state"]
    delta = draft["reply"].get("_state_delta") if INLINE_STATE_ENABLED else None
    roll = draft["action"].get("roll")
    engine_state = draft["reply"].get("_engine_state")
    if isinstance(engine_state, dict):
        updated = engine_state
    elif delta is not None:
        try:
            updated = extract_state(history, state, request_id, roll, inline_delta=delta)
            print(f"[state] mode=inline turn={request_id}", flush=True)
        except (StateUnavailable, ValueError, TypeError, KeyError, AttributeError):
            app.logger.warning("state_mode=fallback turn=%s", request_id)
            updated = extract_state(history, state, request_id, roll)
    else:
        print(f"[state] mode=extract turn={request_id}", flush=True)
        updated = extract_state(history, state, request_id, roll)
    reply = dict(draft["reply"])
    reply.pop("_state_delta", None)
    reply.pop("_engine_state", None)
    details = []
    for key, label, unit in (("xp", "Опыт", " XP"), ("gold", "Золото", " зм"),
                             ("silver", "Серебро", " см"), ("copper", "Медь", " мм")):
        previous, current = state.get(key, 0), updated.get(key, 0)
        if type(previous) is int and type(current) is int and previous != current:
            details.append(f"{label} {previous} → {current}{unit}")
    if isinstance(state.get("hp"), int) and state["hp"] != updated["hp"]:
        details.append(f"Здоровье {state['hp']} → {updated['hp']}/{updated['max_hp']} HP")
    npc_outcome = npc_damage(state, roll)
    if npc_outcome:
        details.append(f"{npc_outcome['name']}: {npc_outcome['hp_before']} → {npc_outcome['hp_after']}/{npc_outcome['max_hp']} HP")
    old_minute, new_minute = campaign_minute(state), campaign_minute(updated)
    if old_minute is not None and new_minute is not None and old_minute < new_minute:
        details.append(clock_label(new_minute))
    if new_minute is not None and state.get("race") and state.get("class"):
        reply["content"] = synchronize_clock(reply["content"], new_minute)
    if state.get("race") and state.get("class"):
        reply["content"] = _synchronize_recap(reply["content"], updated, roll)
    next_check = updated.get("pending_check")
    prior_check = active_check(state, party["messages"])
    effect_log = updated.get("effect_log") or []
    if any(re.match(r"Эффект \d+ не применён:|Отклонено:", line) for line in effect_log):
        reply["content"] = "Сцена продолжается. Подтверждённый результат хода указан ниже."
        location = (updated.get("scene") or {}).get("location")
        if location:
            reply["content"] += f" Текущее место: {location}."
    if ((roll or {}).get("check") or {}).get("ammo"):
        target = roll["check"].get("target", "цель")
        verdict = "попадание" if roll.get("success") else "промах"
        details.append(f"Выстрел по цели «{target}» — {verdict}.")
        if not next_check and (roll["check"].get("world_target_id") or not roll.get("success")):
            announced = pending_check(reply["content"])
            if announced and announced["kind"] == "damage" and not announced.get("effect"):
                reply["content"] = check_instructions_in_card(reply["content"], announced)
                reply["content"] = re.sub(r"(?:Теперь\s+)?брось(?:те)?\s+урон\s*[:.]?\s*$",
                                          "", reply["content"], flags=re.I).rstrip()
            else:
                reply["content"] = check_instructions_in_card(
                    reply["content"], {"expr": roll["check"].get("ranged_dice", "")})
    if next_check and next_check.get("id") != (prior_check or {}).get("id"):
        reply["content"] = check_instructions_in_card(reply["content"], next_check)
    if reply["content"] != draft["reply"]["content"]:
        reply["event_id"] = message_event_id(draft["reply"], len(history) - 1)
    previous_events = {event.get("id") for event in (state.get("agenda") or [])
                       if isinstance(event, dict) and event.get("status") == "resolved"}
    for event in updated.get("agenda") or []:
        if isinstance(event, dict) and event.get("no_report") and event.get("id") not in previous_events:
            details.append(f"{event['title']}: {event['outcome']}")
    details.extend(effect_log)
    if details and state.get("race") and state.get("class") and not state.get("character_setup_pending"):
        reply["resolution"] = " · ".join(details)
    saved = PARTIES.commit(owner, party_id, revision, request_id, updated, reply)
    try:
        _schedule_summary(saved["messages"], "", request_id, party_id)
    except sqlite3.Error:
        app.logger.warning("Не удалось запланировать обновление памяти партии")
    return _snapshot_response(saved)


@app.route("/api/state", methods=["POST"])
def api_state():
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"Ожидается JSON-объект."}),400
    if "party_id" in data:
        try:
            return _commit_party_turn(data)
        except StateUnavailable as exc:
            return jsonify({"error": str(exc)}), 503
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
    if not ALLOW_LEGACY_LLM: return jsonify({"error":"Требуется серверное приключение."}), 403
    messages = data.get("messages") or []
    state = data.get("state") or DEFAULT_STATE
    if (not isinstance(state, dict) or not isinstance(messages, list) or not messages
            or any(not isinstance(message, dict) or not isinstance(message.get("content"), str)
                   for message in messages)):
        return jsonify({"error": "История хода или лист повреждены."}), 400
    turn_id = state_turn_id(request.headers.get("X-Turn-Id"))
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return jsonify({"error": "У хода отсутствует завершённый ответ мастера."}), 400
    last_id = message_event_id(last, len(messages) - 1)
    if state.get("last_state_event_id") == last_id:
        return jsonify(state)
    try:
        action = messages[-2] if len(messages) > 1 and messages[-2].get("role") == "user" else None
        result = _signed_roll(action, state, messages[:-2])
        updated = extract_state(messages, state, turn_id, result)
    except StateUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    _schedule_summary(messages, _header_token("X-Session-Id", 64), turn_id)
    return jsonify(updated)


@app.route("/api/saves")
def api_saves_list():
    """Список сохранений игрока: имя, время записи, размер. Новые сверху."""
    saves = _collect_saves(_client_id())
    if _owner():
        saves.update(_collect_saves(_owner(), shared_saves=False))
    items = [_save_info(name, path) for name, path in saves.items()]
    items.sort(key=lambda x: x["saved_at"], reverse=True)
    return jsonify(items)


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"Ожидается JSON-объект."}),400
    name = _safe_name(data.get("name")) or "default"
    party = None
    if "party_id" in data:
        try:
            party = PARTIES.get(_owner(), _request_id(data["party_id"]))
            if party["revision"] != _revision(data) or party["draft"]:
                raise PartyConflict("Партия изменилась или ждёт сохранения хода. Откройте её снова.")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    path = _save_path(_owner() or _client_id(), name)
    payload = {"schema_version": 3, "messages": data.get("messages") or [],
               "state": data.get("state") or DEFAULT_STATE}
    if party:
        payload.update(messages=party["messages"], state=party["state"], memory=party["memory"])
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Backup previous file before replacing; atomic replace avoids partial JSON on crash.
    if path.exists():
        backup = _save_backup(path)
        try:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=SAVES_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return jsonify({"ok": True, "name": name, "saved_at": path.stat().st_mtime})


@app.route("/api/save/rename", methods=["POST"])
def api_save_rename():
    data = request.get_json(silent=True) or {}
    old = _safe_name(data.get("name"))
    new = _safe_name(data.get("new_name"))
    if not old or not new:
        return jsonify({"ok": False, "error": "нужны name и new_name"}), 400
    if old == new:
        return jsonify({"ok": True, "name": new})

    cid = _client_id()
    src = next((p for p in _save_candidates(cid, old) if p.exists()), None)
    if src is None:
        return jsonify({"ok": False, "error": "сохранение не найдено"}), 404

    dst = _save_path(_owner() or cid, new)
    if dst.exists():
        return jsonify({"ok": False, "error": "сохранение с таким именем уже есть"}), 409
    try:
        os.replace(src, dst)          # время записи при переименовании сохраняется
        src_bak = _save_backup(src)
        if src_bak.exists():
            os.replace(src_bak, _save_backup(dst))
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    return jsonify({"ok": True, "name": new})


@app.route("/api/save/delete", methods=["POST"])
def api_save_delete():
    data = request.get_json(silent=True) or {}
    name = _safe_name(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "нужно name"}), 400

    cid = _client_id()
    removed = []
    path = next((candidate for candidate in _save_candidates(cid, name) if candidate.exists()), None)
    if path:
        for f in (path, _save_backup(path)):
            if f.exists():
                try:
                    f.unlink()
                except OSError as exc:
                    return jsonify({"ok": False, "error": str(exc)[:200]}), 500
                removed.append(f.name)
    if not removed:
        return jsonify({"ok": False, "error": "сохранение не найдено"}), 404
    return jsonify({"ok": True, "name": name, "removed": removed})


@app.route("/api/load/<name>")
def api_load(name):
    name = _safe_name(name)
    path = next((p for p in _save_candidates(_client_id(), name) if p.exists()), None)
    if path is None:
        return jsonify({"error": "not found"}), 404
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "save file is corrupted"}), 500
    if isinstance(data, list):
        data = {"messages": data, "state": DEFAULT_STATE}
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
