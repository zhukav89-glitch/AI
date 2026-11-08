# -*- coding: utf-8 -*-
"""
Парсер документов: карточка пациента, классификация документа, лабораторные показатели.

Каждый показатель сохраняет источник (имя файла, страница) и статус распознавания.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from text_extractor import ExtractedDoc, PageText

# ----------------------------------------------------------------------------
# Карточка пациента (клиническая зона; в аналитическую зону уходит без ФИО)
# ----------------------------------------------------------------------------
@dataclass
class PatientCard:
    fio: Optional[str] = None
    birth_date: Optional[date] = None
    sex: Optional[str] = None
    age: Optional[int] = None          # рассчитывается на дату операции
    age_direct: Optional[int] = None   # "Полных лет" из документа

    def to_analytic_dict(self) -> Dict:
        """Данные для аналитической зоны: без прямых идентификаторов."""
        return {"age": self.age, "sex": self.sex}


# ----------------------------------------------------------------------------
# Лабораторный показатель
# ----------------------------------------------------------------------------
@dataclass
class Finding:
    analyte: str                       # каноническое имя (напр. "гемоглобин")
    raw_name: str                     # как напечатано в документе
    value: Optional[float] = None
    value_raw: Optional[str] = None    # исходная строка ("6,80", "отриц.")
    unit: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None
    ref_raw: Optional[str] = None
    qualitative: Optional[str] = None  # для инфекций: "не обнаружено" и т.п.
    source_file: str = ""
    source_page: int = 1
    recognition: str = "Подтверждено авто"   # см. validator.py
    source_kind: str = "text_layer"     # text_layer | ocr
    alt_value: Optional[float] = None    # значение из второго чтения OCR
    alt_raw: Optional[str] = None
    readings: int = 1                    # сколько чтений дали значение
    candidate: Optional[float] = None    # вероятное значение при потере запятой
    range_value: Optional[tuple] = None  # осадок мочи: «0-1 в п/зр» -> (0, 1)
    conversion: Optional[str] = None     # пересчет единиц, «было -> стало»
    from_date: Optional[date] = None     # дата бланка, если показатель взят из одного из нескольких

    @property
    def in_range(self) -> Optional[bool]:
        if self.range_value and self.ref_high is not None:
            return self.range_value[1] <= self.ref_high
        if self.value is None or (self.ref_low is None and self.ref_high is None):
            return None
        if self.ref_low is not None and self.value < self.ref_low:
            return False
        if self.ref_high is not None and self.value > self.ref_high:
            return False
        return True

    def fmt(self) -> str:
        if self.qualitative:
            return self.qualitative
        if self.value is None:
            return self.value_raw or "—"
        s = ("%g" % self.value).replace(".", ",")
        return f"{s} {self.unit or ''}".strip()


# ----------------------------------------------------------------------------
# Классификация документа
# ----------------------------------------------------------------------------
DOC_TYPES = {
    # ключ: (заголовок, {термин: вес}) — специфичные фразы весят больше
    "ter":  {"title": "Осмотр терапевта",      "terms": {"врача-терапевт": 15, "терапевта первичный": 8, "осмотр терапевт": 5, "прием (осмотр": 2}},
    "anes": {"title": "Осмотр анестезиолога",  "terms": {"анестезиолог": 15, "наркоз": 3}},
    "oak":  {"title": "ОАК (развернутый)",     "terms": {"общий анализ крови": 3, "гематокрит": 2, "лейкоцит": 1, "гемоглобин": 1}},
    "oam":  {"title": "ОАМ",                   "terms": {"общий анализ мочи": 3, "микроскопия осадка мочи": 3, "удельный вес": 1}},
    "bio":  {"title": "Биохимия крови",        "terms": {"биохимический анализ крови": 3, "билирубин": 1, "креатинин": 1, "мочевин": 1}},
    "coag": {"title": "Коагулограмма",         "terms": {"коагулограмма": 3, "фибриноген": 2, "протромбиновое": 2, "международное нормализованное": 3}},
    "elec": {"title": "Электролиты",           "terms": {"электролит": 3}},
    "grp":  {"title": "Группа крови, Rh, антитела", "terms": {"группа крови": 3, "kell": 2, "анти-d": 2, "резус": 1}},
    "inf":  {"title": "Инфекции (HBV/HCV/HIV)","terms": {"hbsag": 3, "anti-hcv": 3, "вирусные гепатиты": 3, "гепатит": 1, "вич": 2, "hiv": 2, "иммунодефицит": 2}},
    "sif":  {"title": "Сифилис",               "terms": {"бледной трепонем": 3, "treponema": 3, "сифилис": 3, "rpr": 2}},
    "ekg":  {"title": "ЭКГ",                   "terms": {"электрокардиограф": 3, "экг": 2}},
    "flg":  {"title": "ФЛГ/рентген грудной клетки", "terms": {"флюорограф": 3, "рентген груд": 3, "флг": 2}},
    "uzdg": {"title": "УЗДС вен нижних конечностей", "terms": {"дуплексное сканирование вен": 8, "дуплекс": 3, "уздс": 3, "вен нижних": 2}},
    "gyn":  {"title": "Осмотр гинеколога",         "terms": {"врача-акушера-гинеколог": 12, "акушер-гинеколог": 8, "гинеколог": 4}},
    "flora": {"title": "Мазок на флору",          "terms": {"мазок из урогенитальн": 10, "микроскопия мазка": 5,
                                                          "отделяемого мочеполовых": 10}},
    "bca":  {"title": "УЗИ (дуплекс) брахиоцефальных артерий", "terms": {"брахиоцефальн": 5, "сосудов шеи": 5}},
    "echo": {"title": "ЭхоКГ",                "terms": {"эхокардиограф": 3}},
    "ter2":  {"title": "Осмотр терапевта",     "terms": {}},
    "psa":  {"title": "ПСА",                  "terms": {"простат-специф": 3, "пса": 2}},
    "egds": {"title": "ФГДС",                 "terms": {"фгдс": 3, "гастроскопия": 3, "эзофагогастродуоденоскопия": 3}},
    "uzi":  {"title": "УЗИ",                  "terms": {"ультразвуковое исследован": 2, "узи ": 1}},
    "ddim": {"title": "D-димер",              "terms": {"д-димер": 3, "d-димер": 3, "d dimer": 3}},
    "hba1c": {"title": "Гликированный гемоглобин", "terms": {"гликированн": 3}},
    "cyto": {"title": "Цитологическое исследование", "terms": {}},
    "mammo": {"title": "Маммография", "terms": {}},
    "tumor": {"title": "Онкомаркеры", "terms": {"roma": 3}},
    "stool": {"title": "Кал на скрытую кровь", "terms": {}},
}


@dataclass
class Document:
    name: str
    doc_type: Optional[str] = None     # ключ DOC_TYPES или None
    doc_title: str = "Не классифицирован"
    exam_date: Optional[date] = None
    patient_fio: Optional[str] = None
    birth_date: Optional[date] = None
    sex: Optional[str] = None
    text: str = ""
    source: str = "text_layer"        # text_layer | ocr | none
    confidence: Optional[float] = None
    findings: List[Finding] = field(default_factory=list)
    all_dates: List[date] = field(default_factory=list)
    ocr_error: Optional[str] = None     # причина сбоя OCR, если был
    source_pages: List[int] = field(default_factory=list)
    date_basis: str = ""
    extra_types: List[str] = field(default_factory=list)   # другие бланки на том же листе

    @property
    def all_types(self) -> List[str]:
        return ([self.doc_type] if self.doc_type else []) + [t for t in self.extra_types if t != self.doc_type]


# ----------------------------------------------------------------------------
# Справочник показателей: каноническое имя -> (регексп названия, ед., границы достоверности)
# Границы достоверности — это НЕ нормы, а пределы возможного у живого человека
# для выявления ошибок распознавания (модуль М5 ТЗ).
# ----------------------------------------------------------------------------
ANALYTES: Dict[str, Dict] = {
    "гемоглобин":   {"rx": r"(?:HGB|Hb\b|гемоглобин)", "plaus": (30, 250),
                     "neg": r"средн|концентрац|гликир|содерж|в моче|ритроцит|гастро|тестин"},
    "лейкоциты":    {"rx": r"(?:WBC|лейкоцит)", "plaus": (0.5, 100), "neg": r"в моче|формул"},
    "эритроциты":   {"rx": r"(?:RBC|эритроцит)", "plaus": (1.0, 8.0),
                     "neg": r"распредел|расй|ширин|средн|объ[её]м|в моче|оседан|эритроцитов\s*по|в эритроцит"},
    "тромбоциты":   {"rx": r"(?:PLT|тромбоцит)", "plaus": (10, 1000),
                     "neg": r"крупн|P-?LCR|PLR|средн|объ[её]м|ширин|распредел|тромбокрит|PCT|PDW|MPV"},
    "гематокрит":   {"rx": r"(?:HCT|гематокрит)", "plaus": (15, 70)},
    "соэ":          {"rx": r"(?:СОЭ|\b[СсCc][ОоOo0][ЭэЗз3]\b|(?:\b|(?<=\b[lI]))cos\b|оседания эритроцитов|по\s+Панченков\w*)", "plaus": (0, 150)},
    "глюкоза":      {"rx": r"глюкоз", "plaus": (1.0, 40), "neg": r"в моче"},
    "креатинин":    {"rx": r"креатинин", "plaus": (20, 2000)},
    "мочевина":     {"rx": r"мочевин", "plaus": (0.5, 50)},
    "билирубин":    {"rx": r"билирубин", "plaus": (2, 800), "neg": r"прям|непрям|связан|в моче"},
    "алт":          {"rx": r"\b(?:АЛТ|ALT|[АA][лl][АA][ТT])\b|аланинаминотрансфераз", "plaus": (2, 10000)},
    "аст":          {"rx": r"\b(?:АСТ|AST|ACT|[АA][сc][АA][ТT])\b|аспартатаминотрансфераз", "plaus": (2, 10000)},
    "общий белок":  {"rx": r"общий белок|белок общий", "plaus": (30, 120)},
    "холестерин":   {"rx": r"холестерин", "plaus": (1, 20)},
    "калий":        {"rx": r"(?:калий|K\+)", "plaus": (1.5, 8.0)},
    "натрий":       {"rx": r"(?:натрий|Na\+)", "plaus": (100, 175)},
    "хлор":         {"rx": r"хлор", "plaus": (80, 130)},
    "ачтв":         {"rx": r"(?:АЧТВ|АПТВ)", "plaus": (10, 150)},
    "протромбин_время": {"rx": r"протромбиновое время", "plaus": (5, 60)},
    "антитромбин":  {"rx": r"антитромбин", "plaus": (10, 200)},
    "пти":          {"rx": r"(?:процент протромбина|\bПТИ\b|протромбиновое отношение по|протромбинов(?:ый|ого)\s*(?:индекс)?)", "plaus": (30, 200)},
    "мно":          {"rx": r"международное нормализованное|\b[МM][НH][ОO]\b", "plaus": (0.2, 10)},
    "фибриноген":   {"rx": r"фибриноген", "plaus": (0.3, 20)},
    "d_димер":      {"rx": r"[DД]-димер", "plaus": (0.0, 20000)},
    "пса":          {"rx": r"(?:ПСА|PSA)", "plaus": (0.0, 1000)},
    "гликир_гемоглобин": {"rx": r"гликированн", "plaus": (3.0, 20.0)},
    "ттг":          {"rx": r"(?:ТТГ|тиреотропн)", "plaus": (0.01, 100)},
}

# Общий анализ мочи: отдельный словарь, чтобы «гемоглобин в моче» не стал
# гемоглобином крови. Значение может быть качественным («отрицательно»).
URINE_ANALYTES: Dict[str, Dict] = {
    "моча: pH":            {"rx": r"^\W*pH\b", "plaus": (4.0, 9.5)},
    "моча: удельный вес":  {"rx": r"удельный вес|относ\w*\s+плотност", "plaus": (1.000, 1.050)},
    "моча: белок":         {"rx": r"белок", "plaus": (0, 20)},
    "моча: глюкоза":       {"rx": r"глюкоз|^\W*гл\w{1,3}[зж]ог?а\b", "plaus": (0, 60)},
    "моча: кетоны":        {"rx": r"кетон", "plaus": (0, 20)},
    "моча: билирубин":     {"rx": r"билиру", "plaus": (0, 200)},
    "моча: гемоглобин":    {"rx": r"гемоглобин|^\W*кро[вк]ь", "plaus": (0, 1000)},
    "моча: нитриты":       {"rx": r"нитрит", "plaus": (0, 100)},
    "моча: лейкоциты":     {"rx": r"лейкоцит", "plaus": (0, 10000)},
    "моча: эритроциты":    {"rx": r"эритроцит", "plaus": (0, 10000)},
}
# OCR часто печатает «Не» латиницей («He обнаружено») и путает «о» с «с»/«0»
# («He сбнаружено»); слово бывает обрезано краем ячейки («Не обнаруж»).
QUALITATIVE_RX = re.compile(r"([НH][еe]\s*[оoсc0]б\w{1,3}р\w{0,3}ж\w*|отрицательн(?:о|ый|ая)|отр\.?(?=\s|$)|"
                            r"[оo]бнаружен[оы]?|положительн(?:о|ый|ая)|следы|норма(?=\s|$|[.,;|])|"
                            r"negative|positive)", re.I)
NEGATIVE_WORDS = ("не обнаруж", "отрицат", "отр", "negative", "неопред", "норма")


def _canon_qual(q: str) -> str:
    """Каноническая запись качественного результата (не меняет смысл)."""
    ql = q.lower().strip()
    if re.match(r"[нh][еe]\s*[оoсc0]б\w{1,3}р\w{0,3}ж", ql):
        return "не обнаружено"
    if ql.startswith(("обнаруж", "oбнаруж")):
        return "обнаружено"
    return ql

_DATE_RX = re.compile(r"\b(\d{2})[./](\d{2})[./](\d{4})\b")
_NUM = re.compile(r"^-?\d{1,6}([.,]\d{1,3})?$")


def _parse_date(s: str) -> Optional[date]:
    m = re.search(r"\b(\d{2})[./](\d{2})[./](\d{4})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


LAB_TYPES = {"oak", "bio", "coag", "elec", "ddim", "psa", "hba1c"}
# Бланки, которые ЛИС печатает по несколько на одном листе
PROTOCOL_TYPES = {"echo", "uzi", "uzdg", "bca", "egds", "mammo", "flg"}
MULTI_BLANK_TYPES = {"oak", "bio", "coag", "oam", "inf", "sif", "ekg", "stool"}
CONSULT_RX = re.compile(r"при[её]м\s*\(\s*осмотр|консультаци[яи]\s+врача|"
                        r"осмотр[а]?\s+(?:врача|анестезиолог|терапевт|гинеколог)|протокол\s+осмотра", re.I)


# «Данные о пациенте» с ошибками OCR: «Данные о пацненте», «Данн ie о пацненте»
_BLANK_START = re.compile(r"(?im)^[^\n]{0,40}Данн[^\n]{0,4}?\s*[оo]\s+па[a-zа-яё]{2,6}нт")


def _split_blanks(text: Optional[str], force_pre: bool = False) -> List[str]:
    """Несколько бланков ЛИС на одном листе («Данные о пациенте» у каждого).

    Текст до первого бланка (например, заключение ЭКГ над анализом мочи)
    становится отдельной частью, если в нем есть содержательный текст.
    """
    text = text or ""
    starts = [m.start() for m in _BLANK_START.finditer(text)]
    if not starts:
        return [text]
    pre = text[:starts[0]]
    pre_is_doc = len(re.findall(r"[А-Яа-яЁё]", pre)) >= 60 and (
        DocumentParser._classify_by_header(_flat(pre)) is not None or force_pre)
    if len(starts) == 1 and not pre_is_doc:
        return [text]
    parts = [pre] if pre_is_doc else []
    if not pre_is_doc and parts == [] and starts[0] > 0:
        starts[0] = 0  # шапку организации отдаем первому бланку
    for i, st in enumerate(starts):
        parts.append(text[st: starts[i + 1] if i + 1 < len(starts) else len(text)])
    return parts


def _has_doc_prefix(text: Optional[str]) -> bool:
    """Над первой шапкой ЛИС есть отдельный документ (например, ЭКГ)."""
    m = _BLANK_START.search(text or "")
    if not m:
        return False
    pre = text[:m.start()]
    return len(re.findall(r"[А-Яа-яЁё]", pre)) >= 60 and \
        DocumentParser._classify_by_header(_flat(pre)) is not None


def _words(t: Optional[str]) -> set:
    return {w.lower() for w in re.findall(r"[А-Яа-яЁё]{4,}", t or "")}


def _pair_chunks(chunks: List[str], other: List[str]) -> List[Optional[str]]:
    """Сопоставление бланков другого чтения с бланками основного по словам.

    Число бланков в чтениях может отличаться (OCR потерял заголовок в одном
    из них), поэтому порядковый номер не годится.
    """
    if not other:
        return [None] * len(chunks)
    if len(other) == len(chunks) == 1:
        return list(other)
    out, used = [], set()
    for c in chunks:
        wc = _words(c)
        best, best_s = None, 0.0
        for j, o in enumerate(other):
            if j in used:
                continue
            wo = _words(o)
            sim = len(wc & wo) / max(1, len(wc | wo))
            if sim > best_s:
                best, best_s = j, sim
        if best is not None and best_s >= 0.15:
            used.add(best)
            out.append(other[best])
        else:
            out.append(None)
    return out


def _pair_or_share(chunks: List[str], other: List[str]):
    """Как _pair_chunks, но неразделившееся чтение отдается бланкам без пары.

    Такое чтение («общее») годится для голосования по показателям (значение
    ищется по названию), но не для определения типа бланка.
    """
    out = _pair_chunks(chunks, other)
    shared = [False] * len(out)
    if len(other) == 1 and len(chunks) > 1:
        for i, o in enumerate(out):
            if o is None:
                out[i], shared[i] = other[0], True
    return out, shared


def _flat(text: str) -> str:
    """Текст без переносов строк: «врача-\\nтерапевта» -> «врача-терапевта».

    Разные версии PyMuPDF по-разному режут длинные заголовки на строки;
    классификация не должна от этого зависеть.
    """
    t = re.sub(r"-[ \t]*\n[ \t]*", "-", text or "")
    return re.sub(r"[ \t]*\n[ \t]*", " ", t)


class DocumentParser:
    """Разбор одного документа: классификация, карточка, показатели."""

    CONSULT_TYPES = {"anes", "ter", "gyn", "uzdg"}

    def parse_many(self, name: str, doc: ExtractedDoc) -> List[Document]:
        """Консультации сохраняем целиком; пакеты разбираем по страницам/бланкам.

        Продолжение консультации не становится отдельным анализом только
        потому, что врач перечислил исследования в анамнезе.
        """
        if not doc.pages:
            return [self.parse(name, doc)]
        first_flat = _flat(doc.pages[0].text)[:1500]
        first = self._classify_by_header(first_flat)
        if first in self.CONSULT_TYPES or (doc.pages[0].source == "text_layer"
                                            and CONSULT_RX.search(first_flat)):
            return [self.parse(name, doc)]
        out = []
        for page in doc.pages:
            # Три разных бланка на одном листе: каждый со своей датой.
            starts = list(re.finditer(r"(?im)^[^\n]*Анализ крови на (?:сифилис|ВИЧ|гепатит)[^\n]*",
                                      page.text))
            chunks = []
            if len(starts) > 1:
                for i, match in enumerate(starts):
                    # Отрезаем предыдущий бланк до шапки следующей организации.
                    end = starts[i + 1].start() if i + 1 < len(starts) else len(page.text)
                    section = page.text[match.start():end]
                    section = re.split(r"(?m)^(?:ООО|000)\s", section)[0]
                    chunks.append(section)
            else:
                chunks = _split_blanks(page.text)
            # Те же границы бланков в других чтениях; сопоставляем по содержимому.
            tab = getattr(page, "table_text", None)
            alt2 = getattr(page, "alt2_text", None)
            readings = [r for r in (page.alt_text, alt2, tab) if r]
            # Заголовок бланка над первой шапкой ЛИС (ЭКГ над анализом мочи) мог
            # потеряться в основном чтении, но сохраниться в другом.
            if len(starts) <= 1 and any(_has_doc_prefix(r) for r in readings):
                chunks = _split_blanks(page.text, force_pre=True)
            alt_p, alt_s = _pair_or_share(chunks, _split_blanks(page.alt_text, True) if page.alt_text else [])
            alt2_p, alt2_s = _pair_or_share(chunks, _split_blanks(alt2, True) if alt2 else [])
            tab_p, tab_s = _pair_or_share(chunks, _split_blanks(tab, True) if tab else [])
            for ci, text in enumerate(chunks):
                pt = PageText(page.page, text, page.source, page.confidence,
                              alt_text=alt_p[ci], table_text=tab_p[ci], alt2_text=alt2_p[ci])
                pt.shared = {n for n, fl in (("alt", alt_s[ci]), ("alt2", alt2_s[ci]),
                                             ("table", tab_s[ci])) if fl}
                ed = ExtractedDoc(name, [pt], ocr_error=doc.ocr_error)
                d = self.parse(name, ed)
                d.ocr_error = doc.ocr_error
                prev = out[-1] if out else None
                # Продолжение бланка: явная «стр. 2 из 2» или подряд идущие листы ЭКГ.
                # Вторая страница протокола (ЭхоКГ, УЗИ, ФГДС): своего заголовка нет,
                # тип мог быть выведен только по словам («осмотр» в рекомендациях).
                protocol_cont = (prev is not None and prev.doc_type in PROTOCOL_TYPES
                                 and (page.source != "ocr" or (page.confidence or 0) >= 50)
                                 and len(chunks) == 1 and prev.source_pages
                                 and prev.source_pages[-1] == page.page - 1
                                 and self._classify_by_header(_flat(unicodedata.normalize("NFKC", text))) is None
                                 and not re.search(r"(?:осмотр|консультац)\w*\s+(?:врача|терапевт|анестезиолог|гинеколог)",
                                                   _flat(text)[:400], re.I)
                                 # новый документ: свои данные пациента
                                 and not re.search(r"Пациент|Ф\.?\s?И\.?\s?О|Дата\s+рождения", text, re.I)
                                 # лист без текста (схема, лента ЭКГ) — не продолжение протокола
                                 and len(re.findall(r"[А-Яа-яЁё]{4,}", text)) >= 8)
                continuation = prev is not None and (
                    protocol_cont
                    or (d.doc_type is None and re.search(r"стр\.?\s*2\s*из\s*2", text, re.I))
                    or (prev.doc_type == "ekg" and prev.source_pages
                        and prev.source_pages[-1] == page.page - 1
                        and (d.doc_type == "ekg"
                             # лента ЭКГ без текста сразу за листом ЭКГ того же файла
                             or (d.doc_type is None and page.source == "ocr"
                                 and (page.confidence or 0) < 60))))
                if continuation:
                    if protocol_cont:
                        prev.date_basis = (prev.date_basis or "") + \
                            f"; стр. {page.page} без заголовка присоединена как продолжение (проверить)"
                    elif d.doc_type is None and prev.doc_type == "ekg":
                        prev.date_basis = (prev.date_basis or "") + \
                            f"; стр. {page.page} без текста присоединена как лента ЭКГ (проверить)"
                    prev.source_pages.append(page.page)
                    prev.text += "\n" + text
                    if prev.exam_date is None and d.exam_date is not None:
                        prev.exam_date, prev.date_basis = d.exam_date, d.date_basis
                    prev.findings.extend(f for f in d.findings
                                         if f.analyte not in {x.analyte for x in prev.findings})
                    continue
                if len(doc.pages) > 1 or len(chunks) > 1:
                    d.name = f"{name} [стр. {page.page}, бланк {len(out) + 1}]"
                out.append(d)
        for d in out:
            if len(d.source_pages) > 1 and "[стр." in d.name:
                d.name = re.sub(r"\[стр\. \d+,", f"[стр. {d.source_pages[0]}–{d.source_pages[-1]},", d.name)
        return out

    def parse(self, name: str, doc: ExtractedDoc) -> Document:
        full = unicodedata.normalize("NFKC", doc.full_text)
        d = Document(name=name, text=full, source=doc.source,
                     confidence=doc.avg_confidence, source_pages=[p.page for p in doc.pages])

        # --- Карточка пациента ---
        m = re.search(r"Ф\.И\.О\.?\s*([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+)", full)
        if m:
            d.patient_fio = m.group(1)
        m = re.search(r"(?:Дат[аa]\s+рождения|Д\.Р\.)(?:\s*\(возраст\))?:?[\s\n]*(\d{2}[./]\d{2}[./]\d{4})", full, re.I)
        if not m:
            # «Возраст 26.06.1960» — в части бланков в поле «Возраст» печатают дату рождения
            m = re.search(r"Возраст:?\s*(\d{2}[./]\d{2}[./]\d{4})", full, re.I)
        if m:
            d.birth_date = _parse_date(m.group(1))
        m = re.search(r"Пол:\s*\n?\s*(Муж|Жен)", full, re.I)
        if m:
            d.sex = m.group(1)
        else:
            # бланки, где пол оторван от метки ("ЖЕНСКИЙ" отдельной строкой)
            m = re.search(r"\n\s*(ЖЕНСКИЙ|МУЖСКОЙ)\s*\n", full)
            if m:
                d.sex = "Жен" if m.group(1).startswith("ЖЕН") else "Муж"
        m = re.search(r"Полных лет:?\s*(\d{1,3})", full)
        if m:
            d.age_direct = int(m.group(1))
        # все даты документа (для защиты от даты рождения на роль даты обследования);
        # строки лицензий исключаем — их даты не относятся к обследованию
        d.all_dates = sorted({dt for dt in (
            _parse_date(m.group(0)) for line in full.splitlines()
            if not re.search(r"лицензи|№ЛО-|Lot |серия|рожден", line, re.I)
            for m in _DATE_RX.finditer(line)) if dt})
        m = re.search(r"Дата забора:?\s*\n?\s*(\d{2}[./]\d{2}[./]\d{4})", full)
        if m:
            d.exam_date = _parse_date(m.group(1))
        if d.exam_date is None:
            # «Дата и время забора биоматериала 10,04,2026» — дата на той же строке;
            # OCR часто читает точки как запятые
            m = re.search(r"Дата\s+и\s+время\s+забора\s+биоматериала[ \t:]*(\d{2})[.,](\d{2})[.,](\d{4})", full, re.I)
            if m:
                d.exam_date = _parse_date(".".join(m.groups()))
                if d.exam_date:
                    d.date_basis = "Дата забора биоматериала"
        if d.exam_date is None:
            # «10.04.2026 11:56:53» строкой выше подписи «Дата и время забора биоматериала»
            m = re.search(r"(\d{2})[.,](\d{2})[.,](\d{4})[ \t]+\d{1,2}:\d{2}(?::\d{2})?[^\n]*\n"
                          r"[^\n]{0,10}Дата\s+и\s+время\s+забора", full, re.I)
            if m:
                d.exam_date = _parse_date(".".join(m.groups()))
                if d.exam_date:
                    d.date_basis = "Дата забора биоматериала"
        if d.exam_date is None:
            # порядок важен: дата взятия биоматериала точнее даты поступления в лабораторию
            for label in ("Дата взятия биоматериала", "Дата взятия материала", "Дата взятия образца",
                           "Дата осмотра", "Анализ выполнен", "Дата проведения исследования", "Время исследования",
                           "Дата исследования", "Дата обследования", "Дата выполнения исследования",
                           "Дата выполнения", "Дата получения материала", "Дата взятия",
                           "Дата поступления в лабораторию"):
                pattern = r"\s+".join(map(re.escape, label.split()))
                m = re.search(pattern + r":?\s*(\d{2}[./]\d{2}[./]\d{4})", full, re.I)
                if m:
                    d.exam_date = _parse_date(m.group(1))
                    d.date_basis = label
                    break
        if d.exam_date is None:
            # OCR искажает подпись: «Дата взятия матернала:14.10.2026»
            alt = "\n".join(unicodedata.normalize("NFKC", t) for p in doc.pages
                            for t in (p.alt_text, getattr(p, "alt2_text", None),
                                      getattr(p, "table_text", None)) if t)
            rx = (r"Д[аи][тн]а\s+\S?[взэ]?[зэв]яти[яи]\s+[а-яёa-z]{0,14}[:;\s]*"
                  r"(\d{2})[./,:](\d{2})[./,:](\d{4})")
            m = re.search(rx, full, re.I) or re.search(rx, alt, re.I)
            if m:
                d.exam_date = _parse_date(".".join(m.groups()))
                if d.exam_date:
                    d.date_basis = "Дата взятия материала"
        if d.exam_date is None:
            m = re.search(r"(?m)^Дата:\s*(\d{2}[./]\d{2}[./]\d{4})", full)
            if m:
                d.exam_date = _parse_date(m.group(1))
                d.date_basis = "Дата в шапке документа"
        if d.exam_date is None:
            d.exam_date = self._most_likely_doc_date(full, d.birth_date)
            d.date_basis = "Единственная дата вне служебных полей; проверить" if d.exam_date else "Дата требует проверки"
        if d.exam_date is None and doc.source == "ocr":
            # OCR читает точку как двоеточие («02.10:2026»); дата могла сохраниться
            # только во втором чтении. Год обязателен, поэтому время не спутать.
            fix = lambda t: re.sub(r"(?<!\d)(\d{2})[.:,;](\d{2})[.:,;]((?:19|20)\d{2})(?!\d)", r"\1.\2.\3", t)
            alt_all = "\n".join(unicodedata.normalize("NFKC", t) for p in doc.pages
                                for t in (p.text, p.alt_text, getattr(p, "alt2_text", None)) if t)
            # «Дата:06.10.2026» в блоке «Данные о направлении» — дата направления
            alt_all = re.sub(r"Д[аеи][тн]а\s*:\s*\d{2}\S\d{2}\S\d{4}", " ", fix(alt_all))
            dt = self._most_likely_doc_date(alt_all, d.birth_date)
            if dt:
                d.exam_date = dt
                d.date_basis = "Дата по нескольким чтениям OCR; проверить"

        # --- Классификация ---
        # 1) по заголовку: специфичная фраза, встретившаяся РАНЬШЕ ВСЕХ в тексте
        #    (защита от «осмотр терапевта» в рекомендациях анестезиолога и т.п.)
        flat = _flat(full)
        d.doc_type = self._classify_by_header(flat)
        # 2) фолбэк: взвешенные термины по всему тексту
        if d.doc_type is None:
            low = flat.lower()
            best_type, best_score = None, 0
            for key, spec in DOC_TYPES.items():
                score = sum(w for t, w in spec.get("terms", {}).items()
                            if re.search((r"\b" + re.escape(t) + r"\b") if len(t) <= 4
                                         else re.escape(t), low))
                if score > best_score:
                    best_type, best_score = key, score
            if best_type and best_score >= 2:
                d.doc_type = best_type
        # 3) второе чтение OCR: если основное потеряло заголовок
        if d.doc_type is None:
            alt = "\n".join(t for p in doc.pages
                            for n, t in (("alt", p.alt_text), ("alt2", getattr(p, "alt2_text", None)))
                            if t and n not in getattr(p, "shared", ()))
            if alt:
                d.doc_type = self._classify_by_header(_flat(unicodedata.normalize("NFKC", alt)))
                if d.doc_type:
                    d.date_basis = (d.date_basis or "") + "; тип по второму чтению OCR"
        # 4) несколько бланков ЛИС на одном листе (анализ мочи под заключением ЭКГ,
        #    сифилис + гепатиты + ВИЧ): дополнительные типы по всем чтениям OCR.
        if doc.source == "ocr" and (d.doc_type is None or d.doc_type in MULTI_BLANK_TYPES):
            # сначала основное чтение: его тип точнее относится к этому бланку
            readings = [unicodedata.normalize("NFKC", t) for p in doc.pages
                        for n, t in (("text", p.text), ("alt", p.alt_text),
                                     ("alt2", getattr(p, "alt2_text", None)),
                                     ("table", getattr(p, "table_text", None)))
                        if t and n not in getattr(p, "shared", ())]
            found = []
            for t in readings:
                for k in self._header_types(_flat(t)):
                    if k in MULTI_BLANK_TYPES and k not in found:
                        found.append(k)
            main_types = [k for k in self._header_types(_flat(readings[0])) if k in MULTI_BLANK_TYPES] \
                if readings else []
            if d.doc_type is None and found:
                d.doc_type = found[0]
                d.date_basis = (d.date_basis or "") + "; тип по другому чтению OCR"
            elif main_types and d.doc_type not in main_types and "по второму чтению" in (d.date_basis or ""):
                # тип из второго чтения относился к соседнему бланку листа
                d.doc_type = main_types[0]
            d.extra_types = [k for k in found if k != d.doc_type]
        if d.doc_type:
            d.doc_title = DOC_TYPES[d.doc_type]["title"]
            if d.extra_types:
                d.doc_title += " + " + " + ".join(DOC_TYPES[k]["title"] for k in d.extra_types)

        # --- Показатели (только для лабораторных бланков) ---
        types = d.all_types
        if any(t in LAB_TYPES for t in types):
            d.findings = self._parse_findings(name, doc, ANALYTES)
        if "oam" in types:
            d.findings += self._parse_findings(name, doc, URINE_ANALYTES)

        return d


    # ------------------------------------------------------------------
    HEADER_PATTERNS: Dict[str, str] = {
        # ключ -> регэксп заголовочной фразы (ищем в начале документа)
        "anes": r"(?:осмотр[а]?|при[её]м)[^\n]{0,80}анестезиолог",
        "ter":  r"(?:осмотр|при[её]м)[^\n]{0,80}терапевт",
        "gyn":  r"(?:осмотр|при[её]м)[^\n]{0,100}гинеколог",
        "bca":  r"сосудов\s+шеи|брахиоцефальн|\bбца\b|экстракраниальн",
        "uzdg": r"дуплексное сканирование(?!\W+(?:брахиоцеф|сосудов\s+шеи|экстракран|артерий))|уздс|\bвен\s+нижних\s+конечност",
        "uzi":  r"(?:[оo©0]?рг\w{0,2}н(?:ы|ов)|\bУЗИ\W+\w+)\W+(?:малого\s*таза|брюшной\s+полости)",
        "flora": r"мазок|отделяемого\s+мочеполов",
        "stool": r"скрытую\s+кровь|кал\s+на\s+скрыт",
        "grp":  r"группа крови",
        "ekg":  r"электрокардиограф|электрокардио\w{0,3}р[ао]м|(?<!эхо)(?<!эхо-)кардиограмм|\bэкг\b",
        "flg":  r"флюорограф|рентгенологическ\w*\s+исследован",
        "egds": r"фгдс|гастроскоп|эзофагогастродуоденоскопия",
        "echo": r"эхокардиограф",
        "psa":  r"простат-специф|\bпса\b",
        "oak":  r"общий анализ крови|клинический анализ крови|гематологическ\w*\s+исследован",
        "bio":  r"би[оo]?[хxч]имическ\w*\s+(?:исследован|неследован|анализ)",
        "oam":  r"анализ\W{0,3}моч[и]?|осадка\s+мочи",
        "coag": r"коагулограмма|исследование системы гемостаза",
        "sif":  r"сифилис|трепонем|treponema|pallidum",
        "inf":  r"гепатит|\bвич\b|\bhiv\b|hbsag|anti-?hcv",
    }

    @classmethod
    def _header_types(cls, full: str) -> List[str]:
        """Все типы, чьи заголовочные фразы встречаются в тексте (по порядку)."""
        hits = []
        for key, pat in cls.HEADER_PATTERNS.items():
            m = re.search(pat, full, re.I)
            if m:
                hits.append((m.start(), key))
        if re.search(r"Интервал\s+(?:PQ|QRS)|\bQRS\b|ритм\s+синусов", full, re.I):
            hits.append((0, "ekg"))
        return [k for _, k in sorted(hits)]

    @classmethod
    def _classify_by_header(cls, full: str) -> Optional[str]:
        """Тип по самой ранней заголовочной фразе в первых 2000 символах."""
        head = full[:2000]
        # Рекомендации и анамнез не являются выполненными обследованиями.
        head = re.split(r"ЖАЛОБЫ|АНАМНЕЗ|РЕКОМЕНДАЦИИ", head, maxsplit=1, flags=re.I)[0]
        if re.search(r"группа крови", head, re.I) and re.search(r"фенотип|антител|резус", head, re.I):
            return "grp"
        if re.search(r"цитологическое исследование|цитограмм", head, re.I):
            return "cyto"
        if re.search(r"маммограф|обследование молочных желез|bi-?rads", head, re.I):
            return "mammo"
        if re.search(r"ультразвуковое исследование органов малого таза", head, re.I):
            return "uzi"
        if re.search(r"онкомаркер|\b[CС][AА]\s*-?\s*125\b|\bROMA\b", head, re.I):
            return "tumor"
        if re.search(r"Интервал\s+(?:PQ|QRS)", full, re.I):
            return "ekg"
        best, best_pos = None, None
        for key, pat in cls.HEADER_PATTERNS.items():
            m = re.search(pat, head, re.I)
            if m and (best_pos is None or m.start() < best_pos):
                best, best_pos = key, m.start()
        return best

    @staticmethod
    def _most_likely_doc_date(text: str, birth):
        """Самая частая дата вне строк лицензий и не дата рождения."""
        from collections import Counter
        candidates = Counter()
        for line in text.splitlines():
            if re.search(r"лицензи|№ЛО-|Lot |серия|рожден|сертификат|действител|планиру|прием пищи|прием жидкости|на\s+\d|печат", line, re.I):
                continue
            for m in _DATE_RX.finditer(line):
                dt = _parse_date(m.group(0))
                if dt and dt != birth:
                    candidates[dt] += 1
        if not candidates:
            return None
        # Частота даты не делает ее датой обследования. При неоднозначности
        # оставляем поле незаполненным, а не выбираем максимум/минимум.
        return next(iter(candidates)) if len(candidates) == 1 else None

    # ------------------------------------------------------------------
    def _parse_findings(self, name: str, doc: ExtractedDoc, dictionary: Dict) -> List[Finding]:
        """Показатели по нескольким чтениям OCR одной страницы.

        Чтения: таблица по ячейкам (если найдена сетка), основное, второе.
        Значение подтверждено, если его дали хотя бы два чтения. Иначе берется
        первое допустимое (порядок: таблица, основное, второе) со статусом «Проверить».
        """
        result: List[Finding] = []
        for page in doc.pages:
            texts = [t for t in (getattr(page, "table_text", None), page.text, page.alt_text,
                                 getattr(page, "alt2_text", None)) if t]
            if page.source != "ocr":
                texts = [page.text]
            parsed = [self._parse_lines(t, dictionary, name, page.page) for t in texts]
            order: List[str] = []
            for pr in parsed:
                order += [a for a in pr if a not in order]
            for analyte in order:
                cands = [pr[analyte] for pr in parsed if analyte in pr]
                f = self._merge_readings(cands, dictionary[analyte].get("plaus"))
                f.source_kind = page.source
                if page.source == "ocr" and len(texts) < 2:
                    f.readings = 1
                result.append(f)
        seen, uniq = set(), []
        for f in result:
            if f.analyte in seen:
                continue
            seen.add(f.analyte)
            uniq.append(f)
        return uniq

    @staticmethod
    def _merge_readings(cands: List[Finding], plaus) -> Finding:
        def usable(x):
            if x.qualitative:
                return True
            if x.value_raw and re.fullmatch(r"-?0\d+", x.value_raw.strip()):
                return False  # «094» вместо «0.94»: потерян разделитель
            return x.value is not None and (not plaus or plaus[0] <= x.value <= plaus[1])

        def key(x):
            if x.range_value:
                return ("r", tuple(x.range_value))
            if x.qualitative:
                return ("q", _is_negative(x.qualitative))
            # 14,0 г/дл (пересчет → 140 г/л) и 140 г/л — одна величина
            return ("n", round(x.value, 6)) if x.value is not None else None

        # Потеря запятой — частая ошибка OCR: если одно чтение дало «1.5», а другое
        # «15» (те же цифры без разделителя), чтение без разделителя не голосует.
        def digits(x):
            return re.sub(r"\D", "", x.value_raw or "")
        with_sep: Dict[str, list] = {}
        for c in cands:
            if c.value_raw and re.search(r"[.,]\d", c.value_raw) and c.value is not None:
                with_sep.setdefault(digits(c), []).append(c.value)

        def lost_sep(c):
            if not c.value_raw or re.search(r"[.,]\d", c.value_raw) or c.value is None:
                return False
            vals = with_sep.get(digits(c), [])
            return bool(vals) and all(abs(c.value - v) > 1e-9 for v in vals)
        voters = [c for c in cands if not lost_sep(c)]
        groups: Dict = {}
        for c in voters:
            k = key(c)
            if k is not None and usable(c):
                groups.setdefault(k, []).append(c)
        sizes = sorted((len(g) for g in groups.values()), reverse=True)
        best_group = max(groups.values(), key=len) if groups else []
        # Подтверждение: не меньше двух совпавших чтений и строгое большинство
        # (2 против 2 — не подтверждено).
        if len(best_group) >= 2 and (len(sizes) < 2 or sizes[0] > sizes[1]):
            # если одно из чтений дало пересчет единиц, показываем его («было → стало»)
            f = next((m for m in best_group if m.conversion), best_group[0])
            readings = len(best_group)
            members = best_group
        else:
            usable_c = [c for c in voters if usable(c)] or [c for c in cands if usable(c)]
            f = usable_c[0] if usable_c else cands[0]
            readings, members = 1, [f]
        f.readings = readings
        others = [c for c in cands if c is not f and key(c) != key(f) and
                  (c.value is not None or c.qualitative)]
        if others:
            f.alt_value, f.alt_raw = others[0].value, (others[0].value_raw or others[0].qualitative)
        elif readings >= 2:
            f.alt_value, f.alt_raw = f.value, (f.value_raw or f.qualitative)
        # Референс: принимаем совпавший у двух чтений; один — если другого нет;
        # разные — не угадываем, оценку не делаем.
        refs = [(c.ref_low, c.ref_high, c.ref_raw) for c in cands
                if (c.ref_low, c.ref_high) != (None, None)]
        if refs:
            # «0-05» и «0-0.5»: в референсе тоже теряется запятая — такие не голосуют
            def rdig(raw):
                return re.sub(r"\D", "", str(raw or ""))
            sep_refs = {rdig(r): (lo, hi) for lo, hi, r in refs if re.search(r"\d[.,]\d", str(r or ""))}
            refs = [(lo, hi, r) for lo, hi, r in refs
                    if not (rdig(r) in sep_refs and sep_refs[rdig(r)] != (lo, hi))]
            counts: Dict = {}
            for lo, hi, raw in refs:
                counts.setdefault((lo, hi), []).append(raw)
            (lo, hi), raws = max(counts.items(), key=lambda kv: len(kv[1]))
            if len(raws) >= 2 or len(counts) == 1:
                f.ref_low, f.ref_high, f.ref_raw = lo, hi, raws[0]
            else:
                f.ref_raw = "сверить: " + " / ".join(str(r) for r in (rr for _, _, rr in refs))
                f.ref_low = f.ref_high = None
        if not f.unit:
            f.unit = next((c.unit for c in members + cands if c.unit), None)
        return f

    def _parse_lines(self, text: Optional[str], dictionary: Dict, fname: str, page: int) -> Dict[str, Finding]:
        found = self._parse_lines_raw(text, dictionary, fname, page)
        hb = found.get("гемоглобин")
        if hb is not None:
            _hb_g_dl(hb, text or "")
        for analyte, f in found.items():
            _sanitize_ref(f, dictionary[analyte].get("plaus"), analyte)
        sg = found.get("моча: удельный вес")
        if sg is not None:
            # 1023 и 1.023 — одна величина в разной записи (не исправление значения)
            if sg.value is not None and 1000 <= sg.value <= 1050:
                sg.value = round(sg.value / 1000, 3)
            if sg.ref_low is not None and sg.ref_low >= 1000:
                sg.ref_low = round(sg.ref_low / 1000, 3)
            if sg.ref_high is not None and sg.ref_high >= 1000:
                sg.ref_high = round(sg.ref_high / 1000, 3)
        return found

    def _parse_lines_raw(self, text: Optional[str], dictionary: Dict, fname: str, page: int) -> Dict[str, Finding]:
        found: Dict[str, Finding] = {}
        lines = [l.strip() for l in (text or "").splitlines()]
        lines = [l for l in lines if l]
        for i, line in enumerate(lines):
            for analyte, spec in dictionary.items():
                if analyte in found and found[analyte].value is not None:
                    continue
                if not re.search(spec["rx"], line, re.I):
                    continue
                if spec.get("neg") and re.search(spec["neg"], line, re.I):
                    continue
                f = self._build_finding(analyte, spec, dictionary, lines, i, fname, page)
                if f and (analyte not in found or f.value is not None or f.qualitative):
                    found[analyte] = f
                break
        return found

    def _build_finding(self, analyte: str, spec: Dict, dictionary: Dict, lines: List[str], i: int,
                       fname: str, page: int) -> Optional[Finding]:
        raw_name = lines[i]
        f = Finding(analyte=analyte, raw_name=raw_name,
                    source_file=fname, source_page=page)
        # OCR обычно выдает строку таблицы целиком, а не отдельные ячейки.
        match = re.search(spec["rx"], raw_name, re.I)
        tail = raw_name[match.end():] if match else ""
        # ЛИС: «WBC Лейкоциты 4,44», «СОЭ по Панченкову 19» — за кодом идет русское
        # название (часто искаженное OCR); пропускаем 1–3 слова, если за ними число.
        if match and (re.fullmatch(r"[A-Z]{2,5}", match.group(0)) or re.match(r"\s*по\s", tail)):
            tail = re.sub(r"^\s*(?:[A-Za-zА-Яа-яЁё\-]{2,}[,.:]?\s+){1,3}(?=-?\d)", " ", tail)
        tail = re.sub(r"^[а-яё]*(?:\s+(?:общий|общая|тела|[вB] крови|[вB] моче|по квику))?\s*(?:[({][^)}]*[)}])?\s*\)?\s*",
                      "", tail, flags=re.I)
        # «(качественный)» с ошибками OCR и без скобок: «кзчественный)»
        tail = re.sub(r"^\s*[({]?\s*[кk]\w{0,3}честв\w*\s*[)}]?\.?\s*", " ", tail, flags=re.I)
        # «… 1 (WBC)» — короткий мусор OCR перед кодом показателя
        tail = re.sub(r"^.{0,4}?\([A-Za-zА-Яа-я]{2,5}\)?", "", tail)
        # «Антитромбин III» -> OCR «Il», «И!»: римская цифра в названии, не значение
        tail = re.sub(r"^\s*[IlІИ!|]{1,3}!?(?=\s)", " ", tail)
        tail = re.sub(r"^[\s|:;_\-—–'‘’‚“”„°\"«»,.~`*\[\]/\\]+", " ", tail)
        # Одиночный мусорный знак OCR между названием и значением («АПТВ (AYTB) В 21.6»,
        # «Нитриты у ‚ отрицательно»): убираем 1–2 буквы, только если за ними идет
        # значение. Слово «не» в «не обнаружено» не трогаем.
        for _ in range(2):
            if re.match(r"^\s*(?:-?\d|" + QUALITATIVE_RX.pattern + ")", tail, re.I):
                break
            new = re.sub(r"^\s*[A-Za-zА-Яа-яЁё]{1,4}[!.,:;‚]?(?=\s)[\s|:;_\-—–'‘’‚“”„°\"«»,.~`*\[\]/\\]*", " ", tail)
            if new == tail:
                break
            tail = new
        # Микроскопия осадка мочи: результат записан диапазоном «0-1 в п/зр».
        rm = re.match(r"^\s*(\d{1,3})\s*[-–]\s*(\d{1,3})(?!\s*[.,]\d)(?=\s|$|[|в])", tail) \
            if analyte.startswith("моча:") else None
        if rm and int(rm.group(1)) <= int(rm.group(2)):
            lo_v, hi_v = int(rm.group(1)), int(rm.group(2))
            f.range_value = (lo_v, hi_v)
            f.value_raw = f"{lo_v}-{hi_v}"
            f.qualitative = f"{lo_v}-{hi_v} в п/зр"
            ref = re.search(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", tail[rm.end():])
            if ref:
                f.ref_raw = ref.group(0)
                f.ref_low, f.ref_high = float(ref.group(1)), float(ref.group(2))
            return f
        q = re.match(r"^\s*" + QUALITATIVE_RX.pattern, tail, re.I)
        if q:
            f.qualitative = _canon_qual(q.group(1))
            # числовой референс у качественного результата — обычно из соседней строки; не берем
            return f
        inline = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)(?=[.,]?(?:\s|$|[|%]))", tail)
        if inline:
            remainder = tail[inline.end():]
            # Не принимать 10^9/л (единицу) или 117-155 (референс) за результат.
            if not re.match(r"\s*(?:\^|[-–]\s*\d)", remainder):
                f.value_raw = inline.group(1)
                f.value = float(f.value_raw.replace(",", "."))
                unit = re.search(r"(?<![\w^])(?:ммоль/л|мкмоль/л|г/дл|г/л|ед/л|сек\.?|%|мм/час|кл/мкл|у\.е\.)(?![\d\w])",
                                 remainder, re.I)
                f.unit = unit.group(0) if unit else None
                ref = re.search(r"\d+(?:[.,]\d+)?\s*[-–]\s*\d+(?:[.,]\d+)?", remainder) or \
                    re.search(r"[<≤]\s*\d+(?:[.,]\d+)?", remainder)
                if ref:
                    f.ref_raw = ref.group(0)
                    f.ref_low, f.ref_high = self._parse_ref(f.ref_raw)
                return f

        # ищем в следующих строках: значение, единицы, референс
        window = lines[i + 1: i + 6]
        value_line, unit_line, ref_line = None, None, None
        for w in window:
            wl = w.strip()
            if any(re.search(a["rx"], wl, re.I) for a in dictionary.values()):
                break  # не заимствовать число из следующего показателя
            if value_line is None and _NUM.match(wl):
                value_line = wl
                continue
            if value_line is not None and unit_line is None and \
               re.match(r"^[a-zA-Zа-яА-ЯёЁ/№^%\d\s.х*-]+$", wl) and not _NUM.match(wl):
                unit_line = wl
                continue
            if re.search(r"\d\s*[-–]\s*\d|<\s*\d|>\s*\d|Взрослые|нормальные|пограничн", wl):
                ref_line = wl
                break
            qm = QUALITATIVE_RX.fullmatch(wl.lower().strip())
            if qm and value_line is None:
                f.qualitative = _canon_qual(wl)
                return f

        if value_line:
            f.value_raw = value_line
            try:
                f.value = float(value_line.replace(",", "."))
            except ValueError:
                f.value = None
            f.unit = (unit_line or "").strip() or None
            if ref_line:
                f.ref_raw = ref_line
                lo, hi = self._parse_ref(ref_line)
                f.ref_low, f.ref_high = lo, hi
        else:
            # значение не найдено — не распознано
            f.recognition = "Не распознано (значение не найдено)"
        return f

    @staticmethod
    def _parse_ref(s: str):
        """'3,9 - 6,0' -> (3.9, 6.0); '< 35' -> (None, 35); 'Взрослые: 66-83...' -> (66, 83)."""
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)", s)
        if m:
            return float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        m = re.search(r"<\s*(\d+(?:[.,]\d+)?)", s)
        if m:
            return None, float(m.group(1).replace(",", "."))
        m = re.search(r">\s*(\d+(?:[.,]\d+)?)", s)
        if m:
            return float(m.group(1).replace(",", ".")), None
        return None, None


def _hb_g_dl(f: "Finding", text: str) -> None:
    """Гемоглобин в г/дл (14,0 г/дл = 140 г/л): пересчет единиц, не исправление.

    Пересчитываем, только если единица г/дл напечатана в строке показателя или
    референс бланка тоже в г/дл (обе границы 5–25). Запись «было → стало» видна в отчете.
    """
    unit_gdl = bool(re.search(r"г\s*/\s*дл", f.raw_name or "", re.I)) or (f.unit or "").lower() == "г/дл"
    ref_gdl = (f.ref_low is not None and f.ref_high is not None
               and 5 <= f.ref_low <= 25 and 5 <= f.ref_high <= 25)
    if f.value is None or not (3 <= f.value <= 25) or not (unit_gdl or ref_gdl):
        return
    old = f"{f.value_raw} г/дл"
    f.value = round(f.value * 10, 1)
    if f.ref_low is not None:
        f.ref_low = round(f.ref_low * 10, 1)
    if f.ref_high is not None:
        f.ref_high = round(f.ref_high * 10, 1)
    f.unit = "г/л"
    f.conversion = f"{old} → {f.value:g} г/л"


def _sanitize_ref(f: "Finding", plaus, analyte: str) -> None:
    """Отбросить референс, который не может быть верным (ошибка OCR).

    Нижняя граница больше верхней или граница вне пределов возможного
    (pH 45–8 вместо 4,5–8; эритроциты 38–54 вместо 3,8–5,4): референс
    не используется, показатель не оценивается и уходит в «Референс не найден».
    """
    lo, hi = f.ref_low, f.ref_high
    if lo is None and hi is None:
        return
    if analyte == "моча: удельный вес":
        lo = lo / 1000 if lo is not None and lo >= 1000 else lo
        hi = hi / 1000 if hi is not None and hi >= 1000 else hi
    bad = lo is not None and hi is not None and lo > hi
    if plaus and not bad:
        p0, p1 = plaus
        bad = any(v is not None and not (p0 - 1e-9 <= v <= p1 + 1e-9) for v in (lo, hi) if v != 0)
    if bad:
        f.ref_raw = f"не принят (ошибка OCR?): {f.ref_raw}"
        f.ref_low = f.ref_high = None


def _is_negative(q: Optional[str]) -> bool:
    return bool(q) and q.lower().startswith(NEGATIVE_WORDS)


def build_patient_card(docs: List[Document]) -> PatientCard:
    """Карточка пациента из всех документов. Противоречия фиксируются отдельно.

    Дата рождения выбирается большинством документов; дата, совпадающая с датой
    обследования или отстоящая от нее меньше чем на год, — ошибка разбора OCR.
    """
    from collections import Counter
    card = PatientCard()
    births = Counter()
    for d in docs:
        if d.birth_date and not _implausible_birth(d):
            births[d.birth_date] += 3 if d.source == "text_layer" else 1
        elif d.birth_date:
            d.birth_date = None
    if births:
        card.birth_date = births.most_common(1)[0][0]
    for d in docs:
        if card.fio is None and d.patient_fio:
            card.fio = d.patient_fio
        if card.sex is None and d.sex:
            card.sex = d.sex
        if card.age_direct is None and getattr(d, "age_direct", None):
            card.age_direct = d.age_direct
    return card


def _implausible_birth(d: Document) -> bool:
    b = d.birth_date
    if b is None:
        return False
    if b > date.today():
        return True
    if d.exam_date and (d.exam_date - b).days < 365:
        return True
    return any(abs((x - b).days) < 365 for x in d.all_dates if x != b) and \
        all((x - b).days < 365 for x in d.all_dates if x != b)


def calc_age(birth: Optional[date], on: Optional[date]) -> Optional[int]:
    if not birth or not on:
        return None
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))
