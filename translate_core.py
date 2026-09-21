# -*- coding: utf-8 -*-
"""
Переводчик RU <-> прусский, версия для запуска в браузере через Pyodide.

load_data() принимает уже загруженные JSON-строки (не читает файлы с диска).
translate_sentence_json() (RU -> PR) и translate_pr_sentence_json() (PR -> RU)
возвращают результат как JSON-строку для вызова из JavaScript.
"""
import json
import re

import pymorphy3

POS_PREFIX_MAP = {
    "aj": "ADJF", "av": "ADVB", "pn": "NPRO", "prp": "PREP",
    "crd": "NUMR", "ord": "NUMR", "ij": "INTJ", "cj": "CONJ",
    "pcl": "PRCL", "encl": "PRCL", "pc": "PRTF",
}

PYMORPHY_TO_LOCAL = {
    "NOUN": "NOUN", "ADJF": "ADJF", "ADJS": "ADJF", "PRTF": "PRTF",
    "PRTS": "PRTF", "VERB": "VERB", "INFN": "VERB", "GRND": "VERB",
    "ADVB": "ADVB", "NPRO": "NPRO", "PREP": "PREP", "NUMR": "NUMR",
    "CONJ": "CONJ", "PRCL": "PRCL", "INTJ": "INTJ",
}

CASE_MAP = {"nomn": "nom", "gent": "gen", "datv": "dat", "accs": "akk", "ablt": None, "loct": None}
CASE_MAP_INV = {"nom": "nomn", "gen": "gent", "dat": "datv", "akk": "accs"}
RU_CASE_MARKER_TO_LOCAL = {"acc": "akk", "dat": "dat", "gen": "gen", "loc": "dat", "instr": "akk"}
PYMORPHY_CASE_TO_RU_MARKER = {"accs": "acc", "datv": "dat", "gent": "gen", "loct": "loc", "ablt": "instr"}
GENDER_MAP = {"masc": "masc", "femn": "fem", "neut": "neut"}
GENDER_MAP_INV = {"masc": "masc", "fem": "femn", "neut": "neut"}
NUMBER_MAP = {"sing": "sg", "plur": "pl"}
NUMBER_MAP_INV = {"sg": "sing", "pl": "plur"}
PERSON_ORDER = [
    ("1per", "sing"), ("2per", "sing"), ("3per", "sing"),
    ("1per", "plur"), ("2per", "plur"), ("3per", "plur"),
]
TENSE_MAP = {"pres": "present", "past": "past", "futr": "future"}

# для диакритики, которую пишущий может не набрать (макроны и т.п.);
# š/č/ž/ģ и т.д. НЕ сворачиваются - в проекте это отдельные буквы
_DIACRITIC_FOLD = str.maketrans({
    "ā": "a", "â": "a", "à": "a", "á": "a",
    "ē": "e", "ê": "e", "è": "e", "é": "e",
    "ī": "i", "î": "i", "ì": "i", "í": "i",
    "ō": "o", "ô": "o", "ò": "o", "ó": "o",
    "ū": "u", "û": "u", "ù": "u", "ú": "u",
})


def fold_diacritics(s):
    return s.translate(_DIACRITIC_FOLD)


morph = pymorphy3.MorphAnalyzer()

# заполняются load_data()
_ru_index = {}
_override_by_id = {}
_preposition_index = {}
_ru_max_phrase_words = 1
_pr_index = {}          # точная форма (в нижнем регистре) -> [(entry, label), ...]
_pr_index_folded = {}   # то же, но без макронов - запасной вариант поиска
_pr_max_phrase_words = 1

# Русские варианты предлогов перед словами с беглой гласной: словарь обычно
# хранит базовую форму ("с", "в", "к"), а пользователь пишет "со", "во",
# "ко" и т.п. Нормализуем только для поиска, в отладке оставляем ввод.
PREPOSITION_ALIASES = {
    "во": "в",
    "со": "с",
    "ко": "к",
    "об": "о",
    "обо": "о",
    "ото": "от",
    "изо": "из",
    "безо": "без",
    "надо": "над",
    "подо": "под",
    "предо": "перед",
    "передо": "перед",
}

WORD_TOKEN_RE = re.compile(r"^[\wа-яёА-ЯЁ-]+$", re.UNICODE)
PUNCT_TOKEN_RE = re.compile(r"^[^\wа-яёА-ЯЁ]+$", re.UNICODE)
PR_WORD_TOKEN_RE = re.compile(r"^[\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]+$", re.UNICODE)
PR_PUNCT_TOKEN_RE = re.compile(r"^[^\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž]+$", re.UNICODE)


def classify_pos(entry, override):
    s = entry.get("s", "").lower()
    # В поле s часть речи не всегда стоит первой: например,
    # "gen māise, pn po 1 sg ..." для majs. Поэтому смотрим все токены.
    s_tokens = set(re.findall(r"[a-z]+", s))
    # У предлогов иногда есть дополнительная помета "(av)"; предлог должен
    # выигрывать у наречия.
    if "prp" in s_tokens:
        return "PREP"
    for code, pos in POS_PREFIX_MAP.items():
        if code != "prp" and code in s_tokens:
            return pos
    if override:
        if isinstance(override.get("conjugation"), dict):
            return "VERB"
        para = override.get("paradigm")
        if isinstance(para, dict) and "positive" in para:
            return "ADJF"
        if isinstance(para, dict):
            return "NOUN"
    w = entry.get("w", "")
    if w.endswith(("tun", "twei", "tuns")):
        return "VERB"
    return "NOUN"


def _ru_case_marker_from_chunk(chunk):
    m = re.search(r"\b(acc|dat|gen|loc|instr)\b", chunk.lower())
    return m.group(1) if m else None


def _clean_ru_chunk(chunk):
    """Убирает пояснения в скобках, падежные пометы и знаки препинания
    вокруг слова/фразы, чтобы 'привет!' стало 'привет' и совпало с тем,
    что вернёт морфоанализатор или что введёт пользователь."""
    chunk = re.sub(r"\(.*?\)", "", chunk)
    chunk = re.sub(r"\b(acc|dat|gen|loc|instr)\b", "", chunk)
    chunk = chunk.strip(" \t!?.,;:—-").strip().lower()
    chunk = re.sub(r"\s+", " ", chunk)
    return chunk


def _clean_ru_token(token):
    return re.sub(r"[^\wа-яёА-ЯЁ-]", "", token, flags=re.UNICODE).lower()


def _split_ru_field(ru_field):
    # Кроме запятых/слэшей/точек с запятой в словаре встречаются пары фраз
    # через вопросительный или восклицательный знак: "Как дела? Как поживаешь?".
    return re.split(r"[,/;!?]+", ru_field)


def _clean_pr_phrase(text):
    """Нормализует прусскую фразу для поиска многословных заголовков.
    В отличие от простого lower(), убирает конечную пунктуацию и схлопывает
    пробелы, чтобы 'Kāigi tebbei ēit?' находилось по токенам без вопроса."""
    text = re.sub(r"[^\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _expand_pr_slash_alternatives(form):
    """Разворачивает варианты через слэш в таблицах форм.

    В overrides часто встречается "a / b", а иногда "aux a / b". Если
    индексировать такую строку только целиком, пользовательский ввод одного
    варианта не находится. Для "asma aulaūwuns / aulaūwusi" добавляем и
    "asma aulaūwuns", и "asma aulaūwusi".
    """
    form = (form or "").strip()
    if not form:
        return []
    if "/" not in form:
        return [form]

    parts = [p.strip() for p in form.split("/") if p.strip()]
    if not parts:
        return [form]

    out = [form]
    first_words = parts[0].split()
    prefix = " ".join(first_words[:-1]) if len(first_words) > 1 else ""
    for idx, part in enumerate(parts):
        alt = part
        if idx > 0 and prefix and len(part.split()) == 1:
            alt = f"{prefix} {part}"
        if alt not in out:
            out.append(alt)
    return out


def _iter_pr_form_keys(form):
    seen = set()
    for alt in _expand_pr_slash_alternatives(form):
        for key in (alt.lower(), _clean_pr_phrase(alt)):
            if key and key not in seen:
                seen.add(key)
                yield key


def _collect_forms(node, label=""):
    """Рекурсивно собирает все прусские словоформы из paradigm/conjugation
    вместе с грамматической меткой (путь по ключам, плюс подпись лица для
    таблиц спряжения)."""
    out = []
    if isinstance(node, dict):
        if "f" in node and isinstance(node["f"], str):
            person = node.get("p")
            full_label = f"{label} {person}".strip() if person else label
            out.append((node["f"], full_label))
            return out
        for k, v in node.items():
            if k in ("g", "s", "title", "p"):
                continue
            out.extend(_collect_forms(v, f"{label}.{k}" if label else k))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_forms(item, label))
    elif isinstance(node, str):
        if node.strip():
            out.append((node, label))
    return out


def load_data(dictionary_json_str, overrides_json_str):
    global _ru_index, _override_by_id, _preposition_index, _ru_max_phrase_words
    global _pr_index, _pr_index_folded, _pr_max_phrase_words
    dictionary = json.loads(dictionary_json_str)
    overrides = json.loads(overrides_json_str)

    _override_by_id = {o["id"]: o for o in overrides if "id" in o}

    ru_index = {}
    ru_max_phrase_words = 1
    for e in dictionary:
        ru_field = e.get("ru", "")
        if not ru_field:
            continue
        for raw_chunk in _split_ru_field(ru_field):
            chunk = _clean_ru_chunk(raw_chunk)
            if chunk:
                ru_index.setdefault(chunk, []).append(e)
                if " " in chunk:
                    ru_max_phrase_words = max(ru_max_phrase_words, len(chunk.split()))
    # В исходном словаре некоторые формы местоимений вынесены как cross-ref
    # без русского gloss (например, tenēi "они"). Добавляем безопасные
    # лемматические алиасы, чтобы pymorphy3 мог склонять их через overrides.
    by_headword = {e.get("w"): e for e in dictionary}
    if "они" not in ru_index and by_headword.get("tāns"):
        ru_index["они"] = [by_headword["tāns"]]

    _ru_index = ru_index
    _ru_max_phrase_words = ru_max_phrase_words

    preposition_index = {}
    for e in dictionary:
        s = e.get("s", "")
        if "prp" not in set(re.findall(r"[a-z]+", s.lower())):
            continue
        gov = "akk"
        gm = re.search(r"\b(acc|dat|gen)\b", s.lower())
        if gm:
            gov = RU_CASE_MARKER_TO_LOCAL[gm.group(1)]
        ru_field = e.get("ru", "")
        for raw_chunk in _split_ru_field(ru_field):
            chunk_clean = _clean_ru_chunk(raw_chunk)
            if chunk_clean:
                # marker описывает русский падеж в помете словаря ("в acc" vs
                # "в loc"); gov — падеж, которым управляет прусский предлог.
                marker = _ru_case_marker_from_chunk(raw_chunk)
                preposition_index.setdefault(chunk_clean, []).append({
                    "word": e.get("w"),
                    "gov": gov,
                    "ru_case": marker,
                    "entry": e,
                })
    _preposition_index = preposition_index

    pr_index = {}
    pr_max_phrase_words = 1

    def add_pr_form(entry, form, label):
        nonlocal pr_max_phrase_words
        for key in _iter_pr_form_keys(form):
            pr_index.setdefault(key, []).append((entry, label))
            if " " in key:
                pr_max_phrase_words = max(pr_max_phrase_words, len(key.split()))

    for e in dictionary:
        headword = e.get("w", "")
        if headword:
            add_pr_form(e, headword, "")
        override = _override_by_id.get(e["i"])
        if override:
            for form, label in _collect_forms(override.get("paradigm")):
                add_pr_form(e, form, label)
            for form, label in _collect_forms(override.get("conjugation")):
                add_pr_form(e, form, label)
    _pr_index = pr_index
    _pr_max_phrase_words = pr_max_phrase_words

    pr_index_folded = {}
    for form_lower, matches in pr_index.items():
        pr_index_folded.setdefault(fold_diacritics(form_lower), []).extend(matches)
    _pr_index_folded = pr_index_folded

    return {"words": len(dictionary), "overrides": len(_override_by_id), "prepositions": len(preposition_index)}


def pick_noun_form(override, gender_guess, number, case):
    para = override.get("paradigm")
    if not isinstance(para, dict):
        return None
    for g in [gender_guess] + [x for x in ("masc", "fem", "neut") if x != gender_guess]:
        gv = para.get(g)
        if not isinstance(gv, dict):
            continue
        nv = gv.get(number)
        if isinstance(nv, dict) and nv.get(case):
            return nv[case]
    return None


def pick_adj_form(override, gender_guess, number, case, degree="positive"):
    para = override.get("paradigm")
    if not isinstance(para, dict):
        return None
    deg = para.get(degree) or para.get("positive")
    if not isinstance(deg, dict):
        return None
    for g in [gender_guess] + [x for x in ("masc", "fem", "neut") if x != gender_guess]:
        gv = deg.get(g)
        if not isinstance(gv, dict):
            continue
        nv = gv.get(number)
        if isinstance(nv, dict) and nv.get(case):
            return nv[case]
    return None


def pick_verb_form(override, person, number, tense):
    conj = override.get("conjugation")
    if not isinstance(conj, dict):
        return None
    indic = conj.get("indicative")
    if not isinstance(indic, dict):
        return None
    rows = indic.get(TENSE_MAP.get(tense))
    if not isinstance(rows, list):
        return None
    try:
        idx = PERSON_ORDER.index((person, number))
    except ValueError:
        return None
    return rows[idx].get("f") if idx < len(rows) else None


def pymorphy_pos_to_local(tag):
    for key, val in PYMORPHY_TO_LOCAL.items():
        if key in tag:
            return val
    return None


def choose_candidate(candidates, wanted_pos):
    """Выбирает наиболее подходящую словарную статью.

    Раньше выбор был просто "первое совпадение нужной части речи", из-за чего
    часто выигрывали омонимы без парадигмы. Теперь при прочих равных
    предпочитаем точную часть речи и наличие override — такую статью можно
    склонять/спрягать.
    """
    scored = []
    for order, e in enumerate(candidates):
        override = _override_by_id.get(e["i"])
        pos = classify_pos(e, override)
        pos_match = (wanted_pos is None) or (pos == wanted_pos)
        # pymorphy3 помечает притяжательные местоимения (мой/моя/моё) как ADJF,
        # а в словаре они могут быть pn. Считаем это совместимым.
        compatible_pronoun = wanted_pos == "ADJF" and pos == "NPRO"
        score = (
            0 if pos_match or compatible_pronoun else 1,
            0 if override else 1,
            order,
        )
        scored.append((score, e, override))
    scored.sort(key=lambda t: t[0])
    return scored[0][1], scored[0][2]


def _preferred_gender_for_override(override, fallback_gender):
    """Пол прусского слова лучше брать из его собственной парадигмы.
    Например, русский "дом" masc, но прусский buttan — neut; прилагательное
    должно согласоваться с buttan, а не с русским родом.
    """
    if not override:
        return fallback_gender
    para = override.get("paradigm")
    if not isinstance(para, dict):
        return fallback_gender
    if "positive" in para and isinstance(para.get("positive"), dict):
        para = para["positive"]
    genders = [g for g in ("masc", "fem", "neut") if isinstance(para.get(g), dict)]
    if len(genders) == 1:
        return genders[0]
    return fallback_gender if fallback_gender in genders else (genders[0] if genders else fallback_gender)


def _declension_picker(wanted_pos, override):
    para = override.get("paradigm") if override else None
    # Прилагательные имеют уровни positive/comparative/..., местоимения и
    # существительные — сразу masc/fem/neut. Для ADJF без positive (мой -> majs)
    # используем именную парадигму.
    if wanted_pos in ("ADJF", "PRTF") and isinstance(para, dict) and "positive" in para:
        return pick_adj_form
    return pick_noun_form


def _parse_ru(clean):
    return morph.parse(clean)[0]


def _local_pos_for_parse(parse):
    return pymorphy_pos_to_local(str(parse.tag)) or "NOUN"


def _grammemes_from_parse(parse, forced_case=None, agreement=None):
    tag = parse.tag
    gender_guess = GENDER_MAP.get(tag.gender, "masc")
    number = NUMBER_MAP.get(tag.number, "sg")
    case = forced_case or CASE_MAP.get(tag.case)
    if agreement:
        gender_guess = agreement.get("gender") or gender_guess
        number = agreement.get("number") or number
        case = agreement.get("case") or case
    return gender_guess, number, case


def _preposition_key(clean):
    return clean if clean in _preposition_index else PREPOSITION_ALIASES.get(clean)


def _select_preposition(clean, next_parse=None):
    key = _preposition_key(clean)
    if not key or key not in _preposition_index:
        return None
    choices = _preposition_index[key]
    if next_parse is not None:
        ru_case = PYMORPHY_CASE_TO_RU_MARKER.get(next_parse.tag.case)
        if ru_case:
            for choice in choices:
                if choice.get("ru_case") == ru_case:
                    return choice
            local_case = CASE_MAP.get(next_parse.tag.case)
            if local_case:
                for choice in choices:
                    if choice.get("gov") == local_case:
                        return choice
    return choices[0]


def _next_word_parse(tokens, start_idx, max_words=5):
    """Ищет разбор управляемого слова после предлога.

    Для "в большом доме" нельзя смотреть только на первое слово после
    предлога: это прилагательное, и его изолированный разбор бывает
    неоднозначен. Поэтому предпочитаем ближайшее существительное/местоимение,
    а первый разбор держим только как запасной вариант.
    """
    seen_words = 0
    first_parse = None
    for j in range(start_idx + 1, len(tokens)):
        tok = tokens[j]
        if PUNCT_TOKEN_RE.match(tok):
            return first_parse
        if not WORD_TOKEN_RE.match(tok):
            continue
        seen_words += 1
        clean = _clean_ru_token(tok)
        if clean:
            parse = _parse_ru(clean)
            if first_parse is None:
                first_parse = parse
            if _local_pos_for_parse(parse) in ("NOUN", "NPRO"):
                return parse
        if seen_words >= max_words:
            break
    return first_parse


def _agreement_from_following(tokens, start_idx, forced_case=None, max_words=5):
    """Согласование прилагательного/местоимения с ближайшим существительным.

    pymorphy3 без контекста часто разбирает формы вроде "молодой" как
    femn/gent или loct. Смотрим на следующее существительное и используем его
    число/падеж, а род — по возможности из выбранного прусского слова.
    """
    seen_words = 0
    for j in range(start_idx + 1, len(tokens)):
        tok = tokens[j]
        if PUNCT_TOKEN_RE.match(tok):
            return None
        if not WORD_TOKEN_RE.match(tok):
            continue
        seen_words += 1
        clean = _clean_ru_token(tok)
        if not clean:
            continue
        if _preposition_key(clean):
            return None
        parse = _parse_ru(clean)
        pos = _local_pos_for_parse(parse)
        if pos in ("VERB", "PREP", "CONJ", "PRCL", "INTJ", "ADVB"):
            return None
        if pos in ("NOUN", "NPRO"):
            lemma = parse.normal_form
            candidates = _ru_index.get(lemma) or _ru_index.get(clean)
            override = None
            if candidates:
                _, override = choose_candidate(candidates, pos)
            gender_guess = GENDER_MAP.get(parse.tag.gender, "masc")
            gender = _preferred_gender_for_override(override, gender_guess)
            number = NUMBER_MAP.get(parse.tag.number, "sg")
            case = forced_case or CASE_MAP.get(parse.tag.case) or "nom"
            return {"gender": gender, "number": number, "case": case}
        if seen_words >= max_words:
            return None
    return None


def _direct_object_case_for_parse(parse):
    """Возвращает akk для вероятного прямого дополнения после переходного
    глагола. Это исправляет русские омонимии nom/acc и gen/acc: "вижу дом",
    "вижу молодого человека". Явные dat/loct/ablt не трогаем.
    """
    pos = _local_pos_for_parse(parse)
    if pos not in ("NOUN", "NPRO", "ADJF", "PRTF"):
        return None
    if parse.tag.case in ("nomn", "gent", "accs"):
        return "akk"
    return None


def _subject_agreement_for_parse(parse):
    """Лицо/число ближайшего подлежащего для русских прошедших форм.
    В русском прошедшем времени лицо не выражено, а в прусской таблице оно
    есть: "мы были" должно брать 1pl, а не 3pl.
    """
    pos = _local_pos_for_parse(parse)
    if parse.tag.case not in ("nomn", None):
        return None
    number = "plur" if "plur" in parse.tag else "sing"
    person = next((p for p in ("1per", "2per", "3per") if p in parse.tag), None)
    if pos == "NPRO":
        person = person or "3per"
    elif pos == "NOUN":
        person = "3per"
    else:
        return None
    return {"person": person, "number": number}


# ---------------------------------------------------------------------------
# RU -> PR
# ---------------------------------------------------------------------------

def _translate_word_info(token, forced_case=None, agreement=None, preposition_choice=None, forced_case_reason=None, verb_agreement=None):
    clean = _clean_ru_token(token)
    if not clean:
        return {"out": token, "note": None, "gov_case": None, "pos": None, "case_used": None, "consumes_case": False}

    if preposition_choice or _preposition_key(clean):
        choice = preposition_choice or _select_preposition(clean)
        if choice:
            return {
                "out": choice["word"],
                "note": f"предлог, управляет {choice['gov']}",
                "gov_case": choice["gov"],
                "pos": "PREP",
                "case_used": None,
                "consumes_case": False,
            }

    parse = _parse_ru(clean)
    lemma = parse.normal_form
    tag = parse.tag
    wanted_pos = _local_pos_for_parse(parse)

    candidates = _ru_index.get(lemma) or _ru_index.get(clean)
    if not candidates:
        return {"out": f"[{token}?]", "note": "нет в словаре", "gov_case": None, "pos": wanted_pos, "case_used": None, "consumes_case": wanted_pos in ("NOUN", "NPRO")}

    entry, override = choose_candidate(candidates, wanted_pos)
    base_form = entry.get("w")
    entry_pos = classify_pos(entry, override)

    if override:
        if wanted_pos == "VERB" or entry_pos == "VERB":
            person = next((p for p in ("1per", "2per", "3per") if p in tag), None)
            number = "plur" if "plur" in tag else "sing"
            tense = "past" if "past" in tag else ("futr" if "futr" in tag else "pres")
            if verb_agreement and (person is None or tense == "past"):
                person = verb_agreement.get("person") or person
                number = verb_agreement.get("number") or number
            person = person or "3per"
            form = pick_verb_form(override, person, number, tense)
            if form:
                return {
                    "out": form,
                    "note": f"{base_form}: {tense}/{person}/{number}",
                    "gov_case": None,
                    "pos": "VERB",
                    "case_used": None,
                    "consumes_case": False,
                    "is_transitive": "tran" in tag,
                }
        elif wanted_pos in ("NOUN", "ADJF", "PRTF", "NPRO") or entry_pos in ("NOUN", "ADJF", "NPRO"):
            gender_guess, number, case = _grammemes_from_parse(parse, forced_case, agreement)
            gender_guess = _preferred_gender_for_override(override, gender_guess) if entry_pos in ("NOUN", "NPRO") else gender_guess
            if case:
                picker = _declension_picker(wanted_pos, override)
                form = picker(override, gender_guess, number, case)
                if form:
                    if forced_case:
                        if forced_case_reason == "direct_object":
                            tail = " (прямое дополнение)"
                        else:
                            tail = " (падеж задан предлогом)"
                    elif agreement:
                        tail = " (согласовано с существительным)"
                    else:
                        tail = ""
                    return {
                        "out": form,
                        "note": f"{base_form}: {case}/{number}{tail}",
                        "gov_case": None,
                        "pos": entry_pos if entry_pos != "NOUN" else wanted_pos,
                        "case_used": case,
                        "consumes_case": (entry_pos in ("NOUN", "NPRO") and not (agreement and wanted_pos in ("ADJF", "PRTF"))),
                    }

    note = "словарная форма (без склонения)" if override else "нет парадигмы в overrides"
    return {"out": base_form or f"[{token}?]", "note": note, "gov_case": None, "pos": entry_pos, "case_used": None, "consumes_case": entry_pos in ("NOUN", "NPRO")}


def translate_word(token, forced_case=None):
    info = _translate_word_info(token, forced_case=forced_case)
    return info["out"], info["note"], info["gov_case"]


def _match_ru_phrase(tokens, start_idx):
    if _ru_max_phrase_words <= 1:
        return None
    parts = []
    positions = []
    j = start_idx
    while j < len(tokens) and len(parts) < _ru_max_phrase_words:
        tok = tokens[j]
        if PUNCT_TOKEN_RE.match(tok):
            break
        if WORD_TOKEN_RE.match(tok):
            clean = _clean_ru_token(tok)
            if not clean:
                break
            parts.append(clean)
            positions.append(j)
            phrase = " ".join(parts)
            # Нужен самый длинный вариант, поэтому не возвращаем сразу.
        else:
            break
        j += 1
    for size in range(len(parts), 1, -1):
        phrase = " ".join(parts[:size])
        candidates = _ru_index.get(phrase)
        if candidates:
            entry, _ = choose_candidate(candidates, None)
            return {
                "end": positions[size - 1] + 1,
                "src": " ".join(tokens[start_idx:positions[size - 1] + 1]),
                "out": entry.get("w") or phrase,
                "note": "многословная статья словаря",
            }
    return None


def translate_sentence_json(sentence):
    tokens = re.findall(r"[\wа-яёА-ЯЁ-]+|[^\w\s]", sentence, flags=re.UNICODE)
    out_words, debug = [], []
    pending_case = None          # управление ближайшим предлогом
    direct_object_pending = False  # после переходного глагола ждём дополнение
    subject_agreement = None      # ближайшее подлежащее для прошедших глаголов
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if PUNCT_TOKEN_RE.match(tok):
            # Многословная словарная статья может уже содержать знак вопроса
            # или восклицания (например, "Kāigi tebbei ēit?"). Не дублируем
            # такой знак из исходного предложения.
            if not (out_words and str(out_words[-1]).endswith(tok)):
                out_words.append(tok)
            pending_case = None
            direct_object_pending = False
            subject_agreement = None
            i += 1
            continue

        clean = _clean_ru_token(tok)
        prep_choice = None
        if clean and _preposition_key(clean):
            prep_choice = _select_preposition(clean, _next_word_parse(tokens, i))
        else:
            phrase = _match_ru_phrase(tokens, i)
            if phrase:
                out_words.append(phrase["out"])
                debug.append({"src": phrase["src"], "out": phrase["out"], "note": phrase["note"]})
                pending_case = None
                i = phrase["end"]
                continue

        parse = _parse_ru(clean) if clean and not prep_choice else None
        direct_case = None if pending_case or not direct_object_pending or not parse else _direct_object_case_for_parse(parse)
        effective_case = pending_case or direct_case
        case_reason = "preposition" if pending_case else ("direct_object" if direct_case else None)

        agreement = None
        if clean and not prep_choice:
            pos = _local_pos_for_parse(parse)
            if pos in ("ADJF", "PRTF"):
                agreement = _agreement_from_following(tokens, i, effective_case)

        info = _translate_word_info(
            tok,
            forced_case=effective_case,
            agreement=agreement,
            preposition_choice=prep_choice,
            forced_case_reason=case_reason,
            verb_agreement=subject_agreement,
        )
        out_words.append(info["out"])
        debug.append({"src": tok, "out": info["out"], "note": info["note"]})

        if info["pos"] == "PREP":
            pending_case = info["gov_case"]
            direct_object_pending = False
        elif pending_case and info.get("consumes_case"):
            pending_case = None
        elif pending_case and info["pos"] in ("VERB", "ADVB", "CONJ", "PRCL", "INTJ"):
            pending_case = None

        if info["pos"] == "VERB":
            direct_object_pending = bool(info.get("is_transitive"))
        elif direct_object_pending and info.get("consumes_case"):
            direct_object_pending = False
        elif direct_object_pending and info["pos"] in ("PREP", "CONJ", "PRCL", "INTJ"):
            direct_object_pending = False

        if parse is not None and not prep_choice and not effective_case:
            subj = _subject_agreement_for_parse(parse)
            if subj:
                subject_agreement = subj
        i += 1

    result = " ".join(out_words).replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")
    return json.dumps({"translation": result, "words": debug}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# PR -> RU
# ---------------------------------------------------------------------------

def _label_to_ru_grammemes(label):
    parts = label.split(".")
    grammemes = set()
    for p in parts:
        base = p.split(" ")[0]
        if base in CASE_MAP_INV:
            grammemes.add(CASE_MAP_INV[base])
        if base in NUMBER_MAP_INV:
            grammemes.add(NUMBER_MAP_INV[base])
        if base in GENDER_MAP_INV:
            grammemes.add(GENDER_MAP_INV[base])
    last = parts[-1] if parts else ""
    if "(я)" in last:
        grammemes.update({"1per", "sing"})
    elif "(ты)" in last:
        grammemes.update({"2per", "sing"})
    elif "(мы)" in last:
        grammemes.update({"1per", "plur"})
    elif "(вы)" in last:
        grammemes.update({"2per", "plur"})
    elif "(он/она/оно)" in last or "(они)" in last:
        grammemes.add("3per")
        grammemes.add("plur" if "они" in last else "sing")
    if "present" in parts:
        grammemes.add("pres")
    if "past" in parts:
        grammemes.add("past")
    if "future" in parts:
        grammemes.add("futr")
    return grammemes


def _ru_gloss_for_entry(entry, label):
    """Возвращает (русское_слово, заметку) для статьи словаря, по
    возможности согласовав русское слово с распознанной грамматической
    формой (падеж/число или лицо/время) через pymorphy3.inflect()."""
    ru_field = entry.get("ru", "")
    first_chunk = _split_ru_field(ru_field)[0] if ru_field else ""
    gloss = _clean_ru_chunk(first_chunk) or ru_field.strip() or "?"

    note_form = label.replace(".", "/") if label else "словарная форма"

    if not gloss or " " in gloss.strip():
        return gloss or "?", note_form  # фразу не склоняем

    grammemes = _label_to_ru_grammemes(label) if label else set()
    if grammemes:
        p = morph.parse(gloss)[0]
        try:
            inflected = p.inflect(grammemes)
        except Exception:
            inflected = None
        if inflected:
            return inflected.word, note_form

    return gloss, note_form


def _entry_has_ru_gloss(entry):
    ru_field = entry.get("ru", "")
    if not ru_field:
        return False
    first_chunk = _split_ru_field(ru_field)[0]
    return bool(_clean_ru_chunk(first_chunk) or ru_field.strip())


def _lookup_pr(clean_key):
    matches = _pr_index.get(clean_key) or _pr_index_folded.get(fold_diacritics(clean_key))
    if not matches:
        return None
    # В словаре бывают дублеты: сначала cross-ref без русского gloss, затем
    # полноценная статья с переводом (например, aulaūwuns). Для обратного
    # перевода выбираем статью, из которой реально можно получить русский
    # перевод; только после этого предпочитаем словарную форму инфлекции.
    matches_sorted = sorted(matches, key=lambda m: (
        not _entry_has_ru_gloss(m[0]),
        m[1] != "",
        bool(m[0].get("x")),
    ))
    return matches_sorted[0]


def _clean_pr_token(token):
    return re.sub(r"[^\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]", "", token, flags=re.UNICODE).lower()


def translate_pr_word(token):
    clean = _clean_pr_token(token)
    if not clean:
        return token, None

    found = _lookup_pr(clean)
    if not found:
        return f"[{token}?]", "нет в словаре"

    entry, label = found
    gloss, note = _ru_gloss_for_entry(entry, label)
    return gloss, f"{entry.get('w')}: {note}"


def _match_pr_phrase(tokens, start_idx):
    if _pr_max_phrase_words <= 1:
        return None
    parts = []
    positions = []
    j = start_idx
    while j < len(tokens) and len(parts) < _pr_max_phrase_words:
        tok = tokens[j]
        if PR_PUNCT_TOKEN_RE.match(tok):
            break
        if PR_WORD_TOKEN_RE.match(tok):
            clean = _clean_pr_token(tok)
            if not clean:
                break
            parts.append(clean)
            positions.append(j)
        else:
            break
        j += 1
    for size in range(len(parts), 1, -1):
        phrase_key = " ".join(parts[:size])
        found = _lookup_pr(phrase_key)
        if found:
            entry, label = found
            gloss, note = _ru_gloss_for_entry(entry, label)
            return {
                "end": positions[size - 1] + 1,
                "src": " ".join(tokens[start_idx:positions[size - 1] + 1]),
                "out": gloss,
                "note": f"{entry.get('w')}: {note}",
            }
    return None


def translate_pr_sentence_json(sentence):
    tokens = re.findall(r"[\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]+|[^\w\s]", sentence, flags=re.UNICODE)
    out_words, debug = [], []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if PR_PUNCT_TOKEN_RE.match(tok):
            if not (out_words and str(out_words[-1]).endswith(tok)):
                out_words.append(tok)
            i += 1
            continue

        phrase = _match_pr_phrase(tokens, i)
        if phrase:
            out_words.append(phrase["out"])
            debug.append({"src": phrase["src"], "out": phrase["out"], "note": phrase["note"]})
            i = phrase["end"]
            continue

        ru, note = translate_pr_word(tok)
        out_words.append(ru)
        debug.append({"src": tok, "out": ru, "note": note})
        i += 1

    result = " ".join(out_words).replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")
    return json.dumps({"translation": result, "words": debug}, ensure_ascii=False)
