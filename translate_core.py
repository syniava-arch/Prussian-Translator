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
_pr_index = {}          # точная форма (в нижнем регистре) -> [(entry, label), ...]
_pr_index_folded = {}   # то же, но без макронов - запасной вариант поиска


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


def _clean_ru_chunk(chunk):
    """Убирает пояснения в скобках, падежные пометы и знаки препинания
    вокруг слова/фразы, чтобы 'привет!' стало 'привет' и совпало с тем,
    что вернёт морфоанализатор или что введёт пользователь."""
    chunk = re.sub(r"\(.*?\)", "", chunk)
    chunk = re.sub(r"\b(acc|dat|gen|loc|instr)\b", "", chunk)
    chunk = chunk.strip(" \t!?.,;:—-").strip().lower()
    return chunk


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
    global _ru_index, _override_by_id, _preposition_index, _pr_index, _pr_index_folded
    dictionary = json.loads(dictionary_json_str)
    overrides = json.loads(overrides_json_str)

    _override_by_id = {o["id"]: o for o in overrides if "id" in o}

    ru_index = {}
    for e in dictionary:
        ru_field = e.get("ru", "")
        if not ru_field:
            continue
        for chunk in re.split(r"[,/;]", ru_field):
            chunk = _clean_ru_chunk(chunk)
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
            chunk_clean = _clean_ru_chunk(chunk)
            if chunk_clean:
                preposition_index.setdefault(chunk_clean, []).append((e.get("w"), gov))
    _preposition_index = preposition_index

    pr_index = {}
    for e in dictionary:
        headword = e.get("w", "")
        if headword:
            pr_index.setdefault(headword.lower(), []).append((e, ""))
        override = _override_by_id.get(e["i"])
        if override:
            for form, label in _collect_forms(override.get("paradigm")):
                pr_index.setdefault(form.lower(), []).append((e, label))
            for form, label in _collect_forms(override.get("conjugation")):
                pr_index.setdefault(form.lower(), []).append((e, label))
    _pr_index = pr_index

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
    scored = []
    for e in candidates:
        override = _override_by_id.get(e["i"])
        pos = classify_pos(e, override)
        scored.append((pos == wanted_pos, e, override))
    scored.sort(key=lambda t: not t[0])
    return scored[0][1], scored[0][2]


# ---------------------------------------------------------------------------
# RU -> PR
# ---------------------------------------------------------------------------

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
    tokens = re.findall(r"[\wа-яёА-ЯЁ-]+|[^\w\s]", sentence, flags=re.UNICODE)
    out_words, debug = [], []
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
    first_chunk = re.split(r"[,/;]", ru_field)[0] if ru_field else ""
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


def _lookup_pr(clean_key):
    matches = _pr_index.get(clean_key) or _pr_index_folded.get(fold_diacritics(clean_key))
    if not matches:
        return None
    matches_sorted = sorted(matches, key=lambda m: m[1] != "")
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


def translate_pr_sentence_json(sentence):
    tokens = re.findall(r"[\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]+|[^\w\s]", sentence, flags=re.UNICODE)
    out_words, debug = [], []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if re.match(r"^[^\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž]+$", tok):
            out_words.append(tok)
            i += 1
            continue

        # многие возвратные глаголы хранятся как "форма si" (два слова) -
        # сначала пробуем найти такую пару целиком
        if i + 1 < n and re.match(r"^[\wāēīōūâêîôûàèìòùáéíóúŠšČčŽž-]+$", tokens[i + 1]):
            bigram_key = f"{_clean_pr_token(tok)} {_clean_pr_token(tokens[i + 1])}"
            found = _lookup_pr(bigram_key)
            if found:
                entry, label = found
                gloss, note = _ru_gloss_for_entry(entry, label)
                out_words.append(gloss)
                debug.append({"src": f"{tok} {tokens[i + 1]}", "out": gloss, "note": f"{entry.get('w')}: {note}"})
                i += 2
                continue

        ru, note = translate_pr_word(tok)
        out_words.append(ru)
        debug.append({"src": tok, "out": ru, "note": note})
        i += 1

    result = " ".join(out_words).replace(" ,", ",").replace(" .", ".")
    return json.dumps({"translation": result, "words": debug}, ensure_ascii=False)
