# -*- coding: utf-8 -*-
"""
Переводчик RU -> прусский, версия для запуска в браузере через Pyodide.
Логика идентична translate2.py (тестировался локально в CPython), но
load_data() здесь принимает уже загруженные JSON-строки, а не читает файлы
с диска, и translate_sentence_json() возвращает результат как JSON-строку
для удобного вызова из JavaScript.
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
GENDER_MAP = {"masc": "masc", "femn": "fem", "neut": "neut"}
NUMBER_MAP = {"sing": "sg", "plur": "pl"}
PERSON_ORDER = [
    ("1per", "sing"), ("2per", "sing"), ("3per", "sing"),
    ("1per", "plur"), ("2per", "plur"), ("3per", "plur"),
]
TENSE_MAP = {"pres": "present", "past": "past", "futr": "future"}

morph = pymorphy3.MorphAnalyzer()

# заполняется load_data()
_ru_index = {}
_override_by_id = {}
_preposition_index = {}


def classify_pos(entry, override):
    s = entry.get("s", "")
    m = re.match(r"^([a-z]{2,4})\b", s)
    if m and m.group(1) in POS_PREFIX_MAP:
        return POS_PREFIX_MAP[m.group(1)]
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


def load_data(dictionary_json_str, overrides_json_str):
    global _ru_index, _override_by_id, _preposition_index
    dictionary = json.loads(dictionary_json_str)
    overrides = json.loads(overrides_json_str)

    _override_by_id = {o["id"]: o for o in overrides if "id" in o}

    ru_index = {}
    for e in dictionary:
        ru_field = e.get("ru", "")
        if not ru_field:
            continue
        for chunk in re.split(r"[,/;]", ru_field):
            chunk = re.sub(r"\(.*?\)", "", chunk)
            chunk = re.sub(r"\b(acc|dat|gen|loc|instr)\b", "", chunk)
            chunk = chunk.strip().lower()
            if chunk:
                ru_index.setdefault(chunk, []).append(e)
    _ru_index = ru_index

    preposition_index = {}
    case_word_to_key = {"acc": "akk", "dat": "dat", "gen": "gen", "loc": "dat", "instr": "akk"}
    for e in dictionary:
        s = e.get("s", "")
        if not s.startswith("prp"):
            continue
        gov = "akk"
        gm = re.search(r"\b(acc|dat|gen)\b", s)
        if gm:
            gov = case_word_to_key[gm.group(1)]
        ru_field = e.get("ru", "")
        for chunk in re.split(r"[,/;]", ru_field):
            chunk = re.sub(r"\(.*?\)", "", chunk)
            chunk_clean = re.sub(r"\b(acc|dat|gen|loc|instr)\b", "", chunk).strip().lower()
            if chunk_clean:
                preposition_index.setdefault(chunk_clean, []).append((e.get("w"), gov))
    _preposition_index = preposition_index

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
    scored = []
    for e in candidates:
        override = _override_by_id.get(e["i"])
        pos = classify_pos(e, override)
        scored.append((pos == wanted_pos, e, override))
    scored.sort(key=lambda t: not t[0])
    return scored[0][1], scored[0][2]


def translate_word(token, forced_case=None):
    clean = re.sub(r"[^\wа-яёА-ЯЁ-]", "", token, flags=re.UNICODE).lower()
    if not clean:
        return token, None, None

    if clean in _preposition_index:
        word, gov_case = _preposition_index[clean][0]
        return word, f"предлог, управляет {gov_case}", gov_case

    parse = morph.parse(clean)[0]
    lemma = parse.normal_form
    tag = parse.tag
    wanted_pos = pymorphy_pos_to_local(str(tag)) or "NOUN"

    candidates = _ru_index.get(lemma) or _ru_index.get(clean)
    if not candidates:
        return f"[{token}?]", "нет в словаре", None

    entry, override = choose_candidate(candidates, wanted_pos)
    base_form = entry.get("w")

    if override:
        if wanted_pos == "VERB":
            person = next((p for p in ("1per", "2per", "3per") if p in tag), "3per")
            number = "plur" if "plur" in tag else "sing"
            tense = "past" if "past" in tag else ("futr" if "futr" in tag else "pres")
            form = pick_verb_form(override, person, number, tense)
            if form:
                return form, f"{base_form}: {tense}/{person}/{number}", None
        elif wanted_pos in ("NOUN", "ADJF", "PRTF"):
            gender_guess = GENDER_MAP.get(tag.gender, "masc")
            number = NUMBER_MAP.get(tag.number, "sg")
            case = forced_case or CASE_MAP.get(tag.case)
            if case:
                picker = pick_adj_form if wanted_pos == "ADJF" else pick_noun_form
                form = picker(override, gender_guess, number, case)
                if form:
                    tail = " (падеж задан предлогом)" if forced_case else ""
                    return form, f"{base_form}: {case}/{number}{tail}", None

    note = "словарная форма (без склонения)" if override else "нет парадигмы в overrides"
    return base_form or f"[{token}?]", note, None


def translate_sentence_json(sentence):
    """Возвращает JSON-строку {"translation": "...", "words": [{"src":..,"out":..,"note":..}, ...]}"""
    tokens = re.findall(r"[\wа-яёА-ЯЁ-]+|[^\w\s]", sentence, flags=re.UNICODE)
    out_words = []
    debug = []
    pending_case = None
    for tok in tokens:
        if re.match(r"^[^\wа-яёА-ЯЁ]+$", tok):
            out_words.append(tok)
            continue
        prus, note, gov_case = translate_word(tok, forced_case=pending_case)
        out_words.append(prus)
        debug.append({"src": tok, "out": prus, "note": note})
        pending_case = gov_case

    result = " ".join(out_words).replace(" ,", ",").replace(" .", ".")
    return json.dumps({"translation": result, "words": debug}, ensure_ascii=False)
