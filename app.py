import os
import re
import json
import random
import hashlib
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name(".env"))

from flask import Flask, request, jsonify, Response, render_template
from openai import OpenAI

app = Flask(__name__)

MODEL = os.getenv("OPENAI_MODEL", "qwen2.5:7b")
BASE_URL = os.getenv("OPENAI_BASE_URL") or None
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY and BASE_URL:
    API_KEY = "ollama"
if not API_KEY:
    raise SystemExit("Не задан OPENAI_API_KEY.")

if BASE_URL:
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
else:
    client = OpenAI(api_key=API_KEY)

# Служебные вызовы (извлечение состояния, резюме) можно увести на модель полегче.
# Пусто → используется MODEL.
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL") or MODEL
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL") or MODEL

BASE_DIR = Path(__file__).resolve().parent
SAVES_DIR = BASE_DIR / "saves"
SAVES_DIR.mkdir(exist_ok=True)

FREQ_PENALTY = float(os.getenv("OPENAI_FREQ_PENALTY", "0.35"))
PRES_PENALTY = float(os.getenv("OPENAI_PRES_PENALTY", "0.25"))

# ─── Лимит параллельных обращений к модели ─────────────────────────────────
# Главное узкое место при нескольких игроках. Ставьте ровно столько, сколько
# выдерживает ваш бэкенд (для Ollama — OLLAMA_NUM_PARALLEL).
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


def _header_token(name, limit):
    """Безопасное значение заголовка: только буквы, цифры, дефис и подчёркивание."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", request.headers.get(name, ""))[:limit]


def _client_id():
    """Идентификатор устройства игрока. Разделяет сохранения между людьми."""
    return _header_token("X-Client-Id", 32) or "shared"


SYSTEM_PROMPT = """
Ты — ведущий (мастер) текстовой ролевой игры в жанре тёмного фэнтези / D&D / LitRPG.
Ты одновременно: рассказчик, шоураннер мира и арбитр правил.

════════ ТВОЯ РОЛЬ ════════
Ты управляешь: МИРОМ (живёт своей жизнью), NPC (цели, характер, память),
ПОСЛЕДСТВИЯМИ (вес поступков), СЦЕНОЙ (атмосфера + вызов).

Ты НЕ решаешь за игрока, что он делает, думает или говорит.

════════ СТИЛЬ ════════
• Живо, атмосферно, конкретно. Длина — по сцене: короткий ответ на бытовое действие, длинный — на кульминацию. Не фиксируй «3 абзаца» — варьируй.
• Конкретика важнее эпитетов. Не «мрачный лес», а «сосны стоят так плотно, что снег не падает на землю».
• Ощущения через несколько органов чувств, без перегруза. Не повторяй одну и ту же деталь в соседних сценах.
• Не пересказывай, что игрок только что сделал. Показывай РЕЗУЛЬТАТ.

════════ ЗАПРЕЩЕНО ════════
× «Вы стоите перед выбором», «Что вы будете делать?», «Ваше приключение начинается»
× «Избранный», «судьба зовёт», «древнее пророчество» (если жанр не про это)
× «Внезапно» более одного раза за сцену
× Мета-комментарии о механике в тексте
× Описания чужих мыслей за игрока

════════ АНТИПОВТОРЫ ════════
× Не используй число 3 без причины. Меняй на 2, 4, 5 или убирай число.
× Не повторяй «тёмный», «мрачный», «серый» в каждом ответе. Конкретика: «цвета старой крови», «как мокрый уголь».
× Не повторяй одну деталь (капли, скрип, дым) два хода подряд.
Если три хода подряд было одно слово/число/образ — на четвёртый меняй.

════════ ЖИВОЙ МИР ════════
За кулисами веди несколько сил (1–4, по сцене). У каждой: цель, метод, часы 1–6.
• Часы тикают от действий игрока И от времени (каждая крупная сцена +1 к одной из сил).
• Часы = 6 → цель силы сбывается.
• Не раскрывай часы напрямую. Мир проявляет их косвенно.
• Даже когда игрок отдыхает — что-то происходит в мире.

════════ ⚠️ СИСТЕМА ОПЫТА (XP) — ОБЯЗАТЕЛЬНО НАЧИСЛЯЙ ════════

⚠️ Ты ОБЯЗАН начислять XP за КАЖДОЕ из перечисленного, не дожидаясь просьбы игрока. Если событие произошло — XP начисляется в этот же ход.

XP ЗА УБИЙСТВО ПРОТИВНИКА (по силе):
• Слабый (крыса, гоблин-новичок, CR 1/8–1/4) → 10–25 XP
• Обычный (стражник, волк, гоблин, CR 1/2–1) → 50–100 XP
• Опасный (орк-воин, скелет-рыцарь, CR 2–4) → 150–250 XP
• Сильный (огр, молодой дракон, CR 5–8) → 500–1000 XP
• Очень сильный (тролль-вождь, взрослый дракон, CR 9–12) → 1500–2500 XP
• Легендарный (древний дракон, лич, CR 13+) → 3000–5000 XP

ПОБЕДА БЕЗ УБИЙСТВА (обратил в бегство, перехитрил, взял в плен, договорился):
→ 50% от XP за убийство. Игрок победил — он получает награду.

БОЙ С НЕСКОЛЬКИМИ ВРАГАМИ: суммируй XP за каждого.

РЕШЕНИЕ ПРОБЛЕМЫ НЕ БОЕМ (взлом, дипломатия, крафт, обман, обход):
• Простая задача → 10–30 XP
• Средняя → 50–100 XP
• Сложная → 150–250 XP
• Эпическая → 500+ XP

КВЕСТЫ:
• Промежуточный этап → 20–30% от общего XP квеста
• Завершение квеста → остальное + бонус за тип (main ×2)

КАК ОБЪЯВЛЯТЬ XP:
«+<N> XP» отдельной строкой в конце абзаца. Пример: «Гоблин падает. +50 XP».
Если бой был долгим, можно в конце: «Победа. +150 XP за троих гоблинов».
Если игрок перехитрил без боя: «Ты обходишь патруль по канализации. +25 XP за смекалку.»

❌ НЕ ДАВАЙ XP:
× За бездействие.
× Повторно за того же врага.
× За разговоры без результата (если только не ключевая информация → 10–20 XP).

⚠️ Если игрок что-то сделал и это привело к результату (враг мёртв, квест выполнен, проблема решена) — XP начисляется. ВСЕГДА.
⚠️ НЕ пиши «игрок получает опыт» абстрактно — пиши конкретное число: «+75 XP».

════════ ⚠️ ЗАМЕТКИ, ПИСЬМА, ДОКУМЕНТЫ — НЕ В ИНВЕНТАРЬ ════════

Инвентарь — ТОЛЬКО физические предметы, которые можно взять в руки:
оружие, броня, зелья, еда, инструменты, монеты, украшения, ключи, трофеи.

В отдельную категорию "notes" идут:
• 📝 Заметки — игрок сам что-то записал
• ✉ Письма — получил от NPC (содержание, отправитель)
• 📜 Документы — грамоты, указы, карты, контракты, дневники
• 🔔 Уведомления — системные сообщения (LitRPG-стиль)
• 💬 Слухи — подслушанное, сплетни
• 📔 Журналы — записи NPC, найденные дневники

⚠️ Правило: если предмет МОЖНО ПРОЧИТАТЬ — он идёт в notes. Если можно взять в руку и использовать — в inventory.
Ключ — в инвентарь (физический). Письмо, к которому прилагался ключ, — в notes.

КАК ОБЪЯВЛЯТЬ:
«Ты находишь письмо: <краткое содержание>. (Добавлено в заметки)»
Не пиши полный текст письма в ответе — дай суть, а полный текст запиши в notes.

════════ КВЕСТЫ ════════
Каждый квест — структура:
• id (q1, q2...), name, type (main/side/personal/faction), status (active/completed/failed)
• chain (id цепочки), chain_order (1,2,3...)
• stages: [{"text": "этап", "done": false}, ...], current_stage (индекс)
• rewards: {"xp": 100, "gold": 50, "items": [], "reputation": ""}
• giver, location

⚠️ ГЛАВНОЕ: КВЕСТ НЕ ПРОДЛЕВАЕТСЯ.
Все stages done → status: "completed", XP выдаётся сразу.
Если есть chain → сразу активируется следующий квест. Старый completed больше не трогается.

ТИПЫ КВЕСТОВ И НАГРАДЫ:
• main: 200–500 XP, 100–500 золота, +предмет
• side: 50–150 XP, 20–100 золота
• personal: 80–200 XP, +1 к характеристике
• faction: 100–250 XP, +репутация

⚠️ XP ЗА КВЕСТ ВЫДАЁТСЯ ВСЕГДА при завершении. Не «может быть», а «выдаётся».

════════ ПРОКАЧКА УРОВНЯ ════════
Уровень и порог следующего уровня рассчитываются сервером из XP. Не меняй XP, уровень или xp_next.
Когда персонаж достигает нового уровня, сообщи об этом и предложи игроку выбрать повышение характеристики и новое/улучшенное умение.
Не применяй бонусы автоматически до выбора игрока. После выбора обнови HP, характеристику и умение в листе персонажа.
Пороги XP: 100, 250, 500, 1000, 2000, 3500, 5500, 8000, 12000; после 10 уровня +5000 за уровень.

════════ СОЗДАНИЕ ПЕРСОНАЖА ════════
При первом описании персонажа определи:
1. РАСУ и КЛАСС (если не указаны — предложи 2–3 варианта).
2. ПРЕДЫСТОРИЮ (Солдат, Преступник, Мудрец, Народный герой, Благородный, Отшельник, Артист, Бродяга, Моряк, Наёмник, Шарлатан, Прислужник).
3. ХАРАКТЕРИСТИКИ (базовые по классу + расовые бонусы).
4. ВЛАДЕНИЯ, НАВЫКИ, УМЕНИЯ.

Объяви итог: «Кейр, эльф-плут, Преступник. Ловкость 17 (+2 эльф)... HP 10/10. Владения: ... Навыки: ... Умения: ...»

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

════════ БОНУС НАВЫКА ════════
Бонус = мод характеристики + бонус мастерства (+2 на 1–4 ур., +3 на 5–8, +4 на 9–12, +5 на 13–16, +6 на 17–20).
Характеристика: Сила — Атлетика/Запугивание; Лов — Акробатика/Ловкость рук/Скрытность;
Инт — История/Магия/Расследование/Природа/Религия; Мудр — Внимательность/Проницательность/Медицина/Выживание/Животные; Хар — Обман/Убеждение/Выступление.

════════ МОДИФИКАТОР СЛОЖНОСТИ (DC) ════════

⚠️ НЕ штампуй DC 15. Большинство действий — БЕЗ броска или DC 8–12.

ШАГ 0. НУЖЕН ЛИ БРОСОК?
Бросок НЕ нужен, если:
• Действие тривиальное (открыть дверь, взять кружку, сесть)
• У персонажа есть навык и ситуация рядовая
• Нет угрозы/спешки/сопротивления
• Игрок подготовился (взял 10/20)
В этих случаях опиши результат в тексте.

ШАГ 1. ШКАЛА:
• DC 5 — очень легко; DC 8 — легко; DC 10 — просто; DC 12 — ниже среднего;
• DC 15 — средняя; DC 18 — выше среднего; DC 20 — сложно;
• DC 25 — очень сложно; DC 30 — почти невозможно.

ШАГ 2. ПОДГОНИ ПОД ПЕРСОНАЖА: характеристика 16+ и навык — снизь DC на 2–3.

ШАГ 3. ДОБАВЬ СИТУАЦИЮ:
ПОЛОЖЕНИЕ ТЕЛА: лежит +3 (ближняя), +5 (подъём); плавает/висит +3; схвачен +5; обездвижен — автопровал.
УКРЫТИЕ (для цели, по которой бьют): половинное +2, 3/4 +5, полное — невозможно.
РУКИ: обе заняты +2 на действия со свободной рукой.
СОСТОЯНИЕ: темнота +3, слепота +5, ранение +2, крит. +3, отравлен +2, испуган +2, спешка +2, местность +1..+2.
ПОМОЩЬ: союзник −2, подготовка −2..−3, знакомое −1..−2.

СВЯЗАННЫЕ ДЕЙСТВИЯ:
• «Лежит, хочет атаковать» — сначала подъём DC 10, потом атака.
• «Обездвижен» — Сила DC 15.
• «Связан» — Ловкость рук DC 15 или Сила DC 20.

КАК ОБЪЯВЛЯТЬ:
«Сделайте бросок 1d20+<Мод> (<Хар>) +<Бонус> (<Навык>) (Сложность <X>: база <Y> + <причины>)».

Результаты: 1 — крит. провал, 2–9 — провал с ценой, 10–14 — успех с осложнением, 15–19 — чистый успех, 20 — крит. успех.

════════ СТРУКТУРА ОТВЕТА ════════
1. НЕМЕДЛЕННЫЙ РЕЗУЛЬТАТ (что произошло).
2. РЕАКЦИЯ МИРА (что изменилось).
3. ГОЛОС NPC (если рядом кто-то есть).
4. ВЫЗОВ (новая ситуация, требование броска).

В коротких сценах пункты 2–3 можно пропустить. Ответ ВСЕГДА заканчивается на вызове.
XP объявляется отдельной строкой в конце.

════════ NPC ════════
У значимого NPC: имя, роль, яркая деталь, цель, тайна, манера речи.
Помнят поступки, могут лгать, не выкладывают всё сразу.

════════ КАНОН ════════
Запоминай всё: имя/расу/класс/предысторию, характеристики, инвентарь, заметки, квесты, позицию, NPC, места.

════════ ЯЗЫК ════════
Русский, естественный. Жёсткость — по жанру.

════════ СТАРТ ════════
Первый ответ — после описания персонажа/мира. Сначала раса/класс/предыстория, потом сцена.
"""


STATE_EXTRACT_PROMPT = """
Ты — модуль отслеживания состояния персонажа в RPG. Обнови состояние по диалогу.

Верни СТРОГО валидный JSON без markdown:
{
  "name": "имя или Пустой",
  "race": "", "class": "", "background": "",
  "level": 1, "xp": 0, "xp_next": 100,
  "hp": 10, "max_hp": 10, "gold": 0,
  "stats": {"str":10,"dex":10,"con":10,"int":10,"wis":10,"cha":10},
  "proficiencies": [], "skills": [], "abilities": [],
  "inventory": [], "notes": [], "quests": [], "conditions": [],
  "relationships": [], "faction_clocks": [],
  "dc_modifier": 0, "dc_modifier_reasons": [],
  "position": "стоит", "cover": "нет",
  "weapon_in_hand": "", "restrained": false,
  "level_history": [], "xp_event_ids": []
}

XP, LEVEL, XP_NEXT, LEVEL_PENDING — НЕ ИЗМЕНЯЙ самостоятельно. Сохрани значения из текущего состояния.
XP начисляется сервером только из новых реплик мастера с явным форматом «+N XP».
Не рассчитывай награды и не повышай уровень; сервер обработает это отдельно.

⚠️ INVENTORY vs NOTES:
- inventory — ТОЛЬКО физические предметы: оружие, броня, зелья, еда, инструменты, монеты, украшения, ключи.
- notes — всё, что МОЖНО ПРОЧИТАТЬ: письма, документы, заметки, дневники, уведомления, слухи, грамоты, карты (как информация).
- Если мастер пишет «ты находишь письмо», «находишь дневник», «тебе вручают грамоту», «системное уведомление», «слышишь слух» — это в notes, НЕ в inventory.
- Ключ от сундука — inventory. Записка с кодом — notes.

СТРУКТУРА NOTES:
Массив объектов:
{
  "id": "n1",
  "type": "note|letter|document|notification|rumor|journal",
  "title": "Краткое название (до 40 символов)",
  "content": "Полный текст или суть (1–4 предложения)",
  "source": "Кто/откуда (имя NPC, место)",
  "date": "Игровая дата или 'недавно'",
  "unread": true
}
- type: note (своя заметка), letter (письмо), document (документ), notification (системное), rumor (слух), journal (дневник/журнал).
- unread: true для новых, false если игрок прочитал.
- Не дублируй. Если заметка уже есть — не добавляй повторно.
- Максимум 30 заметок. Старые прочитанные можно удалять (оставляй важные).

ОСНОВНЫЕ ПОЛЯ:
- Не выдумывай событий.
- HP не превышает max_hp.
- Инвентарь — без дублей.

RACE, CLASS, BACKGROUND: сохрани, если уже есть. Меняй только при событии в игре.

STATS: если раса/класс только появились и все 10 — выставь по классу + расовые бонусы. Если уже не 10 — не пересчитывай.

PROFICIENCIES: массив строк. Формируется ОДИН РАЗ. Максимум 12.

SKILLS: массив {"name":"Скрытность","bonus":5,"source":"Плут"}. ОДИН РАЗ, 6–8 навыков.

ABILITIES: массив {"name":"Ярость","description":"...","uses_max":2,"uses_left":2,"recharge":"long_rest"}.
recharge: passive (uses_max=0), short_rest, long_rest, day.
uses_left уменьшай при использовании, восстанавливай при отдыхе.
Назначай ОДИН РАЗ, 5–6 умений.

DC_MODIFIER: только реальные причины. Нейтрально → 0, [].

POSITION: "стоит", "сидит", "на коленях", "лежит", "верхом", "плавает", "висит", "схвачен", "обездвижен".
COVER: "нет", "половинное", "три_четверти", "полное".
WEAPON_IN_HAND: "", "кинжал", "меч", "лук", "посох", "щит", "обе_руки_заняты".
RESTRAINED: bool.

QUESTS: массив объектов с полями id/name/type/status/chain/chain_order/stages/current_stage/rewards/giver/location.
- Все stages done → status "completed", XP выдаётся в этом же ходу.
- Если chain и completed → сразу создай следующий квест (chain_order + 1).
- Не продлевай завершённый. Новый этап = новый квест.
- Максимум 8 активных.

LEVEL_HISTORY: массив {"level":2,"hp":"+4","stats":"+1 Сила","abilities":"+Рывок"}.

ПРОКАЧКА:
- xp >= xp_next → level+1, max_hp += 2+мод Тел, hp=max_hp, stats: одна хар +1, abilities: новое умение, skills: пересчёт при 5/9/13/17, xp_next: см. прогрессию.
- В level_history — запись.
- Max 1 уровень за ход.

RELATIONSHIPS: {"name":"Торвин","attitude":"насторожен","note":"помог"}. Максимум 6–8.

FACTION_CLOCKS: {"name":"Культ","progress":3,"max":6,"goal":"..."}. 1–4 силы.
"""


SUMMARY_PROMPT = """
Ты — архивариус. Сожми фрагмент диалога в краткое резюме.

СОХРАНИ:
- Ключевые события, имена NPC, отношение к игроку
- Решения и последствия
- Открытые вопросы, цели, долги
- Факты мира
- Расу, класс, предысторию, характеристики
- Текущий XP, уровень, активные квесты и их этапы
- Активные заметки (письма, документы — их суть)
- Текущее положение

НЕ ВКЛЮЧАЙ: дословные реплики, эмоции, повторы.

Формат: 1 плотный абзац, 4–6 предложений.
"""

DICE_RE = re.compile(r"^\s*(\d*)d(\d+)([+-]\d+)?\s*$", re.IGNORECASE)

DEFAULT_STATE = {
    "name": "Пустой", "race": "", "class": "", "background": "",
    "level": 1, "xp": 0, "xp_next": 100,
    "hp": 10, "max_hp": 10, "gold": 0,
    "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10},
    "proficiencies": [], "skills": [], "abilities": [],
    "inventory": [], "notes": [], "quests": [], "conditions": [],
    "relationships": [], "faction_clocks": [],
    "dc_modifier": 0, "dc_modifier_reasons": [],
    "position": "стоит", "cover": "нет",
    "weapon_in_hand": "", "restrained": False,
    "level_history": [], "xp_event_ids": [], "level_pending": 0
}

# Что уходит модели в промпте чата. Заметки, квесты и история уровней
# остаются в диалоге и в отдельном листе — в каждый запрос их не шлём.
STATE_FIELDS = ("name", "race", "class", "background", "level", "xp", "xp_next",
                "hp", "max_hp", "gold", "stats", "conditions", "position",
                "cover", "weapon_in_hand", "restrained")
STATE_NOTE = ("[АКТУАЛЬНОЕ СОСТОЯНИЕ ПЕРСОНАЖА — справочная информация, "
              "не меняй числа без игрового события]\n")


MAX_MESSAGES_BEFORE_SUMMARY = 24
KEEP_RECENT = 10
SUMMARY_MAX_SESSIONS = int(os.getenv("SUMMARY_MAX_SESSIONS", "128"))

# Резюме прошлого живёт на сервере, по session_id. Клиент его не хранит
# и не присылает — иначе оно пересчитывалось бы каждый ход.
_SUMMARY_STATE = {}
_SUMMARY_LOCK = threading.Lock()


def _summary_get(sid):
    with _SUMMARY_LOCK:
        st = _SUMMARY_STATE.get(sid)
        return dict(st) if st else {"text": "", "folded": 0}


def _summary_put(sid, text, folded):
    with _SUMMARY_LOCK:
        if sid not in _SUMMARY_STATE and len(_SUMMARY_STATE) >= SUMMARY_MAX_SESSIONS:
            _SUMMARY_STATE.pop(next(iter(_SUMMARY_STATE)))
        _SUMMARY_STATE[sid] = {"text": text, "folded": folded}


def summarize_old_messages(messages, session_id=""):
    """Сворачивает старую часть диалога в одно резюме.

    Резюме хранится по session_id и обновляется инкрементально: за ход
    досжимается только прирост, вышедший за окно KEEP_RECENT. Без session_id
    резюме не запоминается между запросами.
    """
    if not isinstance(messages, list):
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    dialogue = [m for m in messages if m.get("role") in ("user", "assistant")]
    other_system = [m for m in system_messages
                    if not m.get("content", "").startswith("[РЕЗЮМЕ ПРОШЛОГО]")]

    def pack(text, tail):
        head = ([{"role": "system", "content": f"[РЕЗЮМЕ ПРОШЛОГО]\n{text}"}]
                if text else [])
        return other_system + head + tail

    persist = bool(session_id)
    st = _summary_get(session_id) if persist else {"text": "", "folded": 0}
    summary_text = st["text"]
    folded = min(max(0, int(st["folded"])), len(dialogue))

    # Пока история короткая — ничего не сжимаем.
    if len(dialogue) <= MAX_MESSAGES_BEFORE_SUMMARY:
        return pack(summary_text, dialogue[folded:])

    target = len(dialogue) - KEEP_RECENT
    tail = dialogue[folded:]
    if target <= folded:
        return pack(summary_text, tail)

    new_part = dialogue[folded:target]
    text_to_summarize = "\n".join(f"{m['role']}: {m.get('content', '')}"
                                  for m in new_part if m.get("content"))
    if not text_to_summarize.strip():
        return pack(summary_text, tail)

    archive_input = ((summary_text + "\n\nНОВЫЕ СОБЫТИЯ:\n") if summary_text
                     else "") + text_to_summarize
    try:
        with llm_slot() as ok:
            if not ok:
                raise RuntimeError("нет свободного слота модели")
            resp = client.chat.completions.create(
                model=SUMMARY_MODEL,
                messages=[{"role": "system", "content": SUMMARY_PROMPT},
                          {"role": "user", "content":
                           f"Материал для обновления памяти:\n{archive_input}\n\n"
                           "Обнови резюме, сохранив старые важные факты и добавив новые."}],
                temperature=0.2,
            )
        new_summary = (resp.choices[0].message.content or "").strip()
        if not new_summary:
            raise RuntimeError("пустое резюме")
    except Exception:
        # Сжать не вышло. Отдаём что есть: прежнее резюме плюс несвёрнутый хвост.
        # Если резюме ещё нет — режем историю по окну, чтобы не переполнить контекст.
        if summary_text:
            return pack(summary_text, tail)
        return other_system + dialogue[-KEEP_RECENT:]

    if persist:
        _summary_put(session_id, new_summary, target)
    return pack(new_summary, dialogue[target:])


def roll_dice(expr):
    m = DICE_RE.match(expr or "")
    if not m:
        raise ValueError("Формат: 1d20+3, 2d6, d100-1")
    count = int(m.group(1) or "1")
    sides = int(m.group(2))
    mod = int(m.group(3) or "0")
    if not (1 <= count <= 100):
        raise ValueError("Кубиков 1..100")
    if not (2 <= sides <= 1000):
        raise ValueError("Сторон 2..1000")
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"expr": expr, "rolls": rolls, "mod": mod, "total": sum(rolls) + mod}


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


def normalize_note(n):
    if isinstance(n, dict):
        # Проверка минимальных полей
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


XP_EVENT_RE = re.compile(r"\+\s*(\d+)\s*XP\b", re.IGNORECASE)


def message_event_id(message, index):
    raw = f"{index}\0{message.get('role','')}\0{message.get('content','')}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def extract_state(messages, current_state):
    current = dict(DEFAULT_STATE)
    if isinstance(current_state, dict):
        current.update(current_state)
    current.setdefault("xp_event_ids", [])
    if not isinstance(current.get("xp_event_ids"), list):
        current["xp_event_ids"] = []

    # On legacy saves, mark historical XP-bearing lines as already seen, except
    # the latest assistant message (which may be the just-finished turn).
    if "xp_event_ids" not in (current_state or {}):
        for index, m in enumerate(messages):
            if m.get("role") == "assistant" and XP_EVENT_RE.search(m.get("content", "")) and index != (len(messages) - 1):
                current["xp_event_ids"].append(message_event_id(m, index))

    fresh_events = []
    for index, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content", "")
        event_id = message_event_id(m, index)
        if event_id in current["xp_event_ids"]:
            continue
        matches = [int(x) for x in XP_EVENT_RE.findall(content)]
        if matches:
            fresh_events.append((event_id, sum(matches)))

    recent = messages[-8:]
    dialogue = "\n".join(f"{m['role']}: {m['content']}" for m in recent if m.get("content"))
    prompt = (f"ТЕКУЩЕЕ СОСТОЯНИЕ:\n{json.dumps(current, ensure_ascii=False)}\n\n"
              f"ДИАЛОГ:\n{dialogue}\n\nВерни обновлённое состояние JSON."
              " Не меняй XP, уровень и xp_next; XP рассчитывается отдельно сервером.")
    try:
        with llm_slot() as ok:
            if not ok:
                raise RuntimeError("нет свободного слота модели")
            resp = client.chat.completions.create(
                model=EXTRACT_MODEL,
                messages=[{"role": "system", "content": STATE_EXTRACT_PROMPT},
                         {"role": "user", "content": prompt}],
                temperature=0.1,
            )
        new_state = clean_json(resp.choices[0].message.content or "")
    except Exception:
        new_state = {}

    merged = dict(current)
    for k, v in new_state.items():
        if v is not None:
            merged[k] = v
    # Preserve arrays when the model accidentally returns empty/missing arrays.
    for f in ("inventory", "notes", "quests", "conditions", "relationships", "faction_clocks",
              "dc_modifier_reasons", "proficiencies", "skills", "abilities", "level_history"):
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
                if isinstance(item, dict):
                    return item.get("id") or item.get("name") or item.get("title") or json.dumps(item, sort_keys=True, ensure_ascii=False)
                return str(item)
            seen = {ident(x) for x in new_items}
            merged[f] = list(new_items) + [x for x in old if ident(x) not in seen]
    # Server-authoritative XP: only explicit new rewards, never double-counted.
    xp = max(0, int(current.get("xp", 0) or 0))
    processed = list(current.get("xp_event_ids", []))
    for event_id, amount in fresh_events:
        if event_id not in processed:
            xp += amount
            processed.append(event_id)
    merged["xp"] = xp
    merged["xp_event_ids"] = processed[-500:]
    # Protect core scalar values from blank/null model output.
    for f in ("name", "race", "class", "background", "position", "cover", "weapon_in_hand"):
        if not merged.get(f) and current.get(f):
            merged[f] = current[f]
    merged["hp"] = max(0, min(int(merged.get("hp", 0) or 0), int(merged.get("max_hp", 1) or 1)))
    previous_level = max(1, int(current.get("level", 1) or 1))
    derived_level = level_for_xp(merged["xp"])
    merged["level"] = derived_level
    merged["xp_next"] = xp_threshold_for_level(derived_level)
    merged["level_pending"] = max(0, derived_level - previous_level)
    merged["gold"] = max(0, int(merged.get("gold", 0) or 0))
    merged["dc_modifier"] = int(merged.get("dc_modifier", 0) or 0)
    for f in ("race", "class", "background", "position", "cover", "weapon_in_hand"):
        if not isinstance(merged.get(f), str): merged[f] = current.get(f, DEFAULT_STATE[f])
    if not isinstance(merged.get("restrained"), bool): merged["restrained"] = bool(current.get("restrained", False))
    for f in ("inventory", "notes", "quests", "conditions", "relationships", "faction_clocks",
              "dc_modifier_reasons", "proficiencies", "skills", "abilities", "level_history", "xp_event_ids"):
        if not isinstance(merged.get(f), list): merged[f] = current.get(f, [])
    merged["quests"] = [q for q in (normalize_quest(x) for x in merged["quests"]) if q]
    merged["notes"] = [n for n in (normalize_note(x) for x in merged["notes"]) if n]
    return merged


# ─── Сохранения ────────────────────────────────────────────────────────────
def _safe_name(raw):
    return re.sub(r"[^a-zA-Z0-9_\-]", "", raw or "")


def _save_path(cid, name):
    """Файл сохранения конкретного игрока: <clientId>~<имя>.json"""
    return SAVES_DIR / f"{cid}~{name}.json"


def _save_backup(path):
    return path.with_name(path.stem + ".bak.json")


def _save_candidates(cid, name):
    """Сначала своё сохранение, потом общее (старые файлы без префикса)."""
    return [_save_path(cid, name), SAVES_DIR / f"{name}.json"]


def _collect_saves(cid):
    """Имя → путь. Свои сохранения перекрывают одноимённые общие."""
    prefix = f"{cid}~"
    own, shared = {}, {}
    for p in SAVES_DIR.glob("*.json"):
        if p.name.endswith(".bak.json"):
            continue
        stem = p.stem
        if stem.startswith(prefix):
            own[stem[len(prefix):]] = p
        elif "~" not in stem:
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
        "model": MODEL,
        "extract_model": EXTRACT_MODEL,
        "summary_model": SUMMARY_MODEL,
        "base_url_configured": bool(BASE_URL),
        "llm_max_concurrent": LLM_MAX_CONCURRENT,
        "llm_queue_timeout": LLM_QUEUE_TIMEOUT,
        "saves_dir": str(SAVES_DIR),
        "sessions_cached": len(_SUMMARY_STATE),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/roll", methods=["POST"])
def api_roll():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(roll_dice(data.get("expr", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages") or []
    game_state = data.get("state") or DEFAULT_STATE
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "empty messages"}), 400
    if messages[0].get("role") != "system":
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    state_context = {k: v for k, v in (game_state.items() if isinstance(game_state, dict) else [])
                    if k in STATE_FIELDS}
    messages = messages[:1] + [{"role": "system", "content": STATE_NOTE + json.dumps(state_context, ensure_ascii=False)}] + messages[1:]
    messages = summarize_old_messages(messages, _header_token("X-Session-Id", 64))

    def generate():
        if not LLM_SEM.acquire(timeout=LLM_QUEUE_TIMEOUT):
            # Префикс «[Ошибка API:» клиент распознаёт и не пишет это в историю игры.
            yield "\n[Ошибка API: сервер занят — все слоты модели заняты. Подождите минуту и повторите.]"
            return
        try:
            try:
                stream = client.chat.completions.create(
                    model=MODEL, messages=messages,
                    temperature=0.9,
                    frequency_penalty=FREQ_PENALTY,
                    presence_penalty=PRES_PENALTY,
                    stream=True,
                )
            except Exception as inner:
                msg = str(inner).lower()
                if "frequency_penalty" in msg or "presence_penalty" in msg or "unknown" in msg:
                    stream = client.chat.completions.create(
                        model=MODEL, messages=messages,
                        temperature=0.9, stream=True,
                    )
                else:
                    raise
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = getattr(chunk.choices[0], "delta", None)
                text = getattr(delta, "content", None) if delta else None
                if text:
                    yield text
        except Exception as e:
            yield "\n[Ошибка API: запрос к модели не выполнен. Проверь настройки модели/API и попробуй снова.]"
        finally:
            LLM_SEM.release()

    return Response(generate(), mimetype="text/plain; charset=utf-8")


@app.route("/api/state", methods=["POST"])
def api_state():
    data = request.get_json(silent=True) or {}
    messages = data.get("messages") or []
    state = data.get("state") or DEFAULT_STATE
    if not isinstance(messages, list) or not messages:
        return jsonify(state)
    return jsonify(extract_state(messages, state))


@app.route("/api/saves")
def api_saves_list():
    """Список сохранений игрока: имя, время записи, размер. Новые сверху."""
    saves = _collect_saves(_client_id())
    items = [_save_info(name, path) for name, path in saves.items()]
    items.sort(key=lambda x: x["saved_at"], reverse=True)
    return jsonify(items)


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    name = _safe_name(data.get("name")) or "default"
    path = _save_path(_client_id(), name)
    payload = {"schema_version": 2, "messages": data.get("messages") or [],
               "state": data.get("state") or DEFAULT_STATE}
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

    dst = _save_path(cid, new)
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
    for path in _save_candidates(cid, name):
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
