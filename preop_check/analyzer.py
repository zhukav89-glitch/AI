# -*- coding: utf-8 -*-
"""
Анализ: комплектность, сроки, сравнение с референсами (модули М8–М11 ТЗ).

Логика:
1. Для каждого применимого правила ищем документ соответствующего типа.
2. Комплектность: Да / Частично (нет обязательных компонентов) / Не найдено.
3. Срок: дата обследования + validity_days >= дата операции, иначе Просрочено.
4. Значения: сравнение с референсом бланка (приоритет 1) или запасным справочником.
5. Отдельно: документы вне плана, нераспознанные значения, противоречия.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from parser import Document, Finding, _is_negative, LAB_TYPES
from rules import Rule, FALLBACK_REFS


@dataclass
class RuleResult:
    rule: Rule
    found: str = "Не найдено"            # Найдено / Частично / Не найдено / Не определено
    matched_doc: Optional[str] = None
    exam_date: Optional[date] = None
    date_status: str = "—"               # Действует / Просрочено (N дн) / Дата не указана / —
    values: List[Finding] = field(default_factory=list)
    deviations: List[str] = field(default_factory=list)
    no_reference: List[str] = field(default_factory=list)
    matched_docs: List[str] = field(default_factory=list)


@dataclass
class CaseAnalysis:
    rule_results: List[RuleResult]
    outside_plan: List[str]              # документы, не подошедшие ни под одно правило
    unrecognized: List[str]              # поля «не распознано»
    contradictions: List[str]
    unclassified: List[str] = field(default_factory=list)   # документы без типа
    to_verify: List[str] = field(default_factory=list)      # значения OCR без подтверждения


# Исследования, которые не равны требованию, но о которых врачу стоит знать.
ALTERNATIVES = {"DIR_2_3_3": ("mammo", "маммография")}


def _same_sheet(a: Document, b: Document) -> bool:
    fa, fb = a.name.split(" [")[0], b.name.split(" [")[0]
    return fa == fb and bool(set(a.source_pages) & set(b.source_pages))


def analyze_case(docs: List[Document], rules: List[Rule],
                 surgery_date: Optional[date]) -> CaseAnalysis:
    results = []
    matched_docs = set()

    for rule in rules:
        rr = RuleResult(rule=rule)
        # --- поиск документа нужного типа (самый свежий из подходящих) ---
        cands = [d for d in docs if set(d.all_types) & set(rule.doc_types)]
        # Бланк, найденный только как «соседний» на листе (тип из другого чтения),
        # не нужен, если на том же листе есть бланк с этим основным типом.
        primary = [d for d in cands if d.doc_type in rule.doc_types]
        cands = primary + [d for d in cands if d not in primary and not any(
            _same_sheet(d, p) for p in primary)]
        if not cands and rule.required_analytes:
            # резерв: правило матчится по показателям в любом документе
            cands = [d for d in docs
                     if any(f.analyte in rule.required_analytes for f in d.findings)]
        if cands:
            cands.sort(key=lambda d: d.exam_date or date.min, reverse=True)
            newest = cands[0]
            # Один комплект может состоять из нескольких бланков (ВИЧ, HBsAg, HCV
            # отдельными листами; ЭКГ на нескольких страницах). Берем все бланки
            # этого типа в пределах 30 дней от самого свежего; срок — по самому старому.
            group = [c for c in cands if c is newest or c.exam_date is None or newest.exam_date is None
                     or (newest.exam_date - c.exam_date).days <= 30]
            d = _merge_group(group)
            rr.matched_doc = d.name
            rr.matched_docs = [c.name for c in group]
            dated = [c.exam_date for c in group if c.exam_date]
            rr.exam_date = min(dated) if dated else None
            d.exam_date = rr.exam_date
            matched_docs.update(rr.matched_docs)

            # --- комплектность по обязательным компонентам ---
            if rule.required_analytes:
                have = {f.analyte for f in d.findings
                        if (f.value is not None or f.qualitative is not None)
                        and not f.recognition.startswith("Не распознано")}
                missing = [a for a in rule.required_analytes if a not in have]
                rr.found = "Найдено" if not missing else \
                    f"Частично (нет: {', '.join(missing)})"
            else:
                rr.found = "Найдено"

            # --- значения и отклонения ---
            # На листе с несколькими бланками (холестерин + сифилис + ВИЧ) показатели
            # соседнего бланка к требованию без списка компонентов не относятся.
            multi = any(c.extra_types for c in group)
            if rule.required_analytes:
                rr.values = [f for f in d.findings if f.analyte in rule.required_analytes]
            elif multi:
                # лист с несколькими бланками: показываем только показатели своего бланка
                urine = "oam" in rule.doc_types
                rr.values = [f for f in d.findings if f.analyte.startswith("моча:") == urine] \
                    if urine or set(rule.doc_types) & LAB_TYPES else []
            else:
                rr.values = list(d.findings)
            for f in rr.values:
                _check_deviation(f, rr, FALLBACK_REFS)

            # --- срок годности ---
            rr.date_status = _date_status(d.exam_date, surgery_date,
                                           rule.validity_days)
            undated = sum(1 for c in group if c.exam_date is None)
            if undated and len(group) > 1:
                rr.date_status += f"; дата не найдена у бланков: {undated} из {len(group)}, сверить"
            if rr.exam_date and all(c.doc_type not in rule.doc_types and set(c.extra_types) & set(rule.doc_types)
                                    for c in group if c.exam_date):
                # бланк найден только как соседний на листе: дата принадлежит другому бланку
                rr.date_status += "; дата с соседнего бланка на том же листе, сверить"
        else:
            rr.found = "Не найдено"
            rr.date_status = "—"
            alt = ALTERNATIVES.get(rule.rule_id)
            if alt:
                alts = [d for d in docs if alt[0] in d.all_types]
                if alts:
                    dt = max((d.exam_date for d in alts if d.exam_date), default=None)
                    rr.date_status = (f"В пакете есть {alt[1]}" +
                                      (f" от {dt.strftime('%d.%m.%Y')}" if dt else "") +
                                      " — заменяет ли она требование, решает врач")

        results.append(rr)

    # Бланки одного листа делят общее чтение OCR — одна и та же строка не повторяется.
    seen_lines = set()

    def first_time(d, f):
        k = (d.name.split(" [")[0], f.source_page, f.analyte, f.value_raw or f.qualitative or f.raw_name)
        if k in seen_lines:
            return False
        seen_lines.add(k)
        return True

    to_verify = []
    for d in docs:
        for f in d.findings:
            if f.recognition.startswith("Проверить") and first_time(d, f):
                to_verify.append(f"{d.name}, стр. {f.source_page}: {f.analyte} = "
                                 f"{f.fmt()} — {f.recognition}")
    outside = [d.name for d in docs
               if d.name not in matched_docs and d.doc_type is not None]
    unclassified = [{"name": d.name, "source": d.source,
                     "text_chars": len(d.text or ""),
                     "ocr_conf": d.confidence,
                     "error": d.ocr_error}
                    for d in docs if d.doc_type is None]
    unrec = []
    for d in docs:
        for f in d.findings:
            if f.recognition.startswith("Не распознано") and first_time(d, f):
                unrec.append(f"{d.name}: {f.raw_name[:40]} — {f.recognition}")
    contradictions = _find_contradictions(docs)

    return CaseAnalysis(results, outside, unrec, contradictions, unclassified, to_verify)


def _merge_group(group: List[Document]) -> Document:
    if len(group) == 1:
        return group[0]
    from copy import copy
    d = copy(group[0])
    # Один показатель в нескольких бланках (ОАК от 14.10 и от 16.10): берем
    # лучше распознанный, при равенстве — из более свежего бланка (группа уже
    # отсортирована от свежего к старому). Значения не смешиваются.
    def rank(f):
        r = f.recognition or ""
        return 0 if r.startswith(("Подтверждено", "Исправлено")) else 1 if r.startswith("Проверить") else 2
    best: Dict[str, Finding] = {}
    order: List[str] = []
    dates = {g.exam_date for g in group if g.exam_date}
    for g in group:
        for f in g.findings:
            if len(dates) > 1 and g.exam_date and not getattr(f, "from_date", None):
                f.from_date = g.exam_date
            if f.analyte not in best:
                order.append(f.analyte)
                best[f.analyte] = f
            elif rank(f) < rank(best[f.analyte]):
                best[f.analyte] = f
    d.findings = [best[a] for a in order]
    d.name = "; ".join(g.name for g in group)
    return d


def _date_status(exam: Optional[date], surgery: Optional[date], validity_days) -> str:
    if surgery is None:
        return "Не проверен: дата операции не указана"
    if exam is None:
        return "Дата не указана"
    if validity_days is None:
        return "Не регламентирован"
    delta = (surgery - exam).days
    if delta < 0:
        return f"Дата позже даты операции ({-delta} дн)"
    if delta > validity_days:
        return f"Просрочено ({delta} дн > {validity_days})"
    return f"Действует ({delta}/{validity_days} дн)"


def _check_deviation(f: Finding, rr: RuleResult, fallback: Dict) -> None:
    """Сравнение с референсом: бланк -> запасной справочник -> оценка невозможна.

    Оцениваются только подтвержденные значения (текстовый слой, два совпавших
    чтения OCR или автоисправление, подтвержденное вторым чтением).
    """
    if not f.recognition.startswith(("Подтверждено", "Исправлено")):
        return
    if f.qualitative:
        if getattr(f, "range_value", None):
            _check_range(f, rr)
            return
        if not _is_negative(f.qualitative):
            rr.deviations.append(f"{f.analyte}: {f.qualitative} (положительный результат)")
        return
    if f.value is None:
        return
    if f.ref_low is not None or f.ref_high is not None:
        ok = f.in_range
        src = "реф. бланка"
    elif f.analyte in fallback:
        unit, lo, hi = fallback[f.analyte]
        ok = (lo is None or f.value >= lo) and (hi is None or f.value <= hi)
        src = "запасной справочник"
        f.ref_low, f.ref_high = lo, hi
        if f.unit is None:
            f.unit = unit
    else:
        msg = f"{f.analyte}: {f.fmt()}"
        if msg not in rr.no_reference:
            rr.no_reference.append(msg)
        return
    lo_s = f"{f.ref_low:g}" if f.ref_low is not None else ""
    hi_s = f"{f.ref_high:g}" if f.ref_high is not None else ""
    if ok is False:
        side = "НИЖЕ" if f.ref_low is not None and f.value < f.ref_low else "ВЫШЕ"
        rr.deviations.append(f"{f.analyte}: {f.fmt()} {side} нормы {lo_s}–{hi_s} ({src})")


def _check_range(f: Finding, rr: RuleResult) -> None:
    """Осадок мочи «0-1 в п/зр»: верхняя граница результата против референса."""
    lo, hi = f.range_value
    if f.ref_high is None:
        msg = f"{f.analyte}: {f.fmt()}"
        if msg not in rr.no_reference:
            rr.no_reference.append(msg)
        return
    if hi > f.ref_high:
        rr.deviations.append(f"{f.analyte}: {f.fmt()} ВЫШЕ нормы {f.ref_low or 0:g}–{f.ref_high:g} (реф. бланка)")


def _find_contradictions(docs: List[Document]) -> List[str]:
    """Простые противоречия: разные ФИО/дата рождения в документах."""
    out = []
    fios = {d.patient_fio for d in docs if d.patient_fio}
    if len(fios) > 1:
        out.append(f"В документах разные ФИО: {len(fios)} варианта — проверить принадлежность файлов одному пациенту")
    text_births = {d.birth_date for d in docs if d.birth_date and d.source == "text_layer"}
    ocr_births: Dict = {}
    for d in docs:
        if d.birth_date and d.source != "text_layer":
            ocr_births.setdefault(d.birth_date, []).append(d.name)
    if len(text_births) > 1:
        out.append(f"Разные даты рождения в документах с текстовым слоем: "
                   f"{', '.join(b.strftime('%d.%m.%Y') for b in sorted(text_births))}")
    elif len(text_births) == 1:
        tb = next(iter(text_births))
        for b, names in sorted(ocr_births.items()):
            if b != tb:
                out.append(f"Дата рождения {b.strftime('%d.%m.%Y')} в скане ({names[0]}"
                           f"{f' и еще {len(names) - 1}' if len(names) > 1 else ''}) отличается от "
                           f"текстового слоя ({tb.strftime('%d.%m.%Y')}): вероятна ошибка OCR, сверить")
    elif len(ocr_births) > 1:
        out.append(f"Разные даты рождения в сканах: "
                   f"{', '.join(b.strftime('%d.%m.%Y') for b in sorted(ocr_births))} — сверить")
    return out
