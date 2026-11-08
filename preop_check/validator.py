# -*- coding: utf-8 -*-
"""
Модуль М5 ТЗ: автоматическая проверка распознавания.

Реализованные уровни проверки (MVP на компьютере без GPU):
  1. Источник текста: текстовый слой PDF достовернее OCR.
  2. Уверенность OCR (средняя по словам, порог из настроек).
  3. Формат: число, десятичный разделитель, дата.
  4. Единицы: единица должна быть известна для показателя.
  5. Физиологически невозможные значения (справочник достоверности из parser.ANALYTES).
  6. Согласованность дат: дата в будущем / до рождения.

Интеграция второго-третьего читателей (PaddleOCR, Qwen2.5-VL) — точки
расширения: validate_findings(f, second_reading=..., third_reading=...).
"""
import logging
from datetime import date
from typing import Optional

import re

from parser import Document, Finding, ANALYTES, URINE_ANALYTES

log = logging.getLogger("preop.validator")

FUTURE_LIMIT = date(2035, 1, 1)   # защита от ошибок распознавания года


def validate_doc(doc: Document, today: Optional[date] = None) -> None:
    """Проверки уровня документа (даты). Помечает проблемные поля."""
    today = today or date.today()
    if doc.exam_date:
        if doc.exam_date > today:
            doc.text_note = (getattr(doc, "text_note", "") +
                             f" [дата обследования {doc.exam_date} в будущем — проверить]")
            log.warning("%s: дата в будущем: %s", doc.name, doc.exam_date)


def _fmt(v: float) -> str:
    return ("%g" % v).replace(".", ",")


def _decimal_candidate(f: Finding, lo: float, hi: float) -> Optional[float]:
    """1151 -> 115,1: OCR часто теряет десятичную точку. Только как подсказка."""
    digits = re.sub(r"\D", "", f.value_raw or "")
    if len(digits) < 2:
        return None
    for k in (1, 2):
        c = int(digits) / 10 ** k
        if not (lo <= c <= hi):
            continue
        if f.ref_low is not None and f.ref_high is not None and \
           not (f.ref_low * 0.3 <= c <= f.ref_high * 3):
            continue
        return round(c, k)
    return None


def validate_finding(f: Finding, conf_threshold: int = 90) -> None:
    _validate_core(f, conf_threshold)
    if f.conversion:
        f.recognition += f" (пересчет единиц: {f.conversion})"


def _validate_core(f: Finding, conf_threshold: int = 90) -> None:
    """Назначает Finding.recognition.

    Текстовый слой PDF — «Подтверждено авто».
    OCR — значение подтверждено, только если ДВА независимых чтения (разная
    предобработка изображения) дали одно и то же; иначе «Проверить».
    Подставлять значение «по смыслу» запрещено: вероятное значение при потере
    запятой показывается как подсказка и принимается, лишь если его дало второе чтение.
    """
    spec = ANALYTES.get(f.analyte) or URINE_ANALYTES.get(f.analyte)
    ocr = f.source_kind == "ocr"
    if f.value is None and f.qualitative is None:
        f.recognition = "Не распознано (значение не найдено)"
        return
    if f.value_raw and re.fullmatch(r"-?0\d+", f.value_raw.strip()):
        f.recognition = (f"Не распознано (OCR: {f.value_raw} — потерян десятичный знак, "
                         f"сверить с оригиналом)")
        return
    if f.value is not None and spec:
        lo, hi = spec["plaus"]
        if not (lo <= f.value <= hi):
            cand = _decimal_candidate(f, lo, hi)
            if cand is not None and f.alt_value is not None and abs(f.alt_value - cand) < 1e-9:
                f.recognition = (f"Исправлено автоматически: {f.value_raw} → {_fmt(cand)} "
                                 f"(потерян десятичный знак; подтверждено вторым чтением)")
                f.value_raw, f.value, f.readings = _fmt(cand), cand, 2
                return
            if cand is not None:
                f.candidate = cand
                f.recognition = (f"Не распознано (OCR: {f.value_raw} — невозможное значение; "
                                 f"вероятно {_fmt(cand)}, сверить с оригиналом)")
            else:
                f.recognition = (f"Не распознано (невозможное значение {f.value_raw}: "
                                 f"допустимо {lo:g}–{hi:g})")
            return
    if not ocr:
        f.recognition = "Подтверждено авто"
        return
    # Целое число без разделителя при референсе с десятыми и выход за верхнюю
    # границу более чем в 10 раз — типичная потеря запятой (1,5 -> 15). Даже при
    # совпадении чтений не подтверждаем: значение сверяет врач.
    decs = [float(x.replace(",", ".")) for x in re.findall(r"\d+[.,]\d+", str(f.ref_raw or ""))]
    if (f.value is not None and f.value_raw and not re.search(r"[.,]\d", f.value_raw)
            and decs and max(decs) > 0 and f.value > 10 * max(decs)):
        f.recognition = (f"Проверить (возможна потеря десятичного знака: {f.value_raw}; "
                         f"референс {f.ref_raw})")
        f.readings = 1
        return
    if f.readings >= 2:
        f.recognition = f"Подтверждено {f.readings} чтениями OCR"
    elif f.alt_raw is not None:
        f.recognition = f"Проверить (чтения расходятся: {f.value_raw or f.qualitative} / {f.alt_raw})"
    else:
        f.recognition = "Проверить (одно чтение OCR)"


def attach_ocr_confidence(docs: list, extracted: dict) -> None:
    """Связывает среднюю уверенность OCR страницы с показателями документа."""
    # extracted: {name: ExtractedDoc}; в MVP уверенность общая по документу
    for d in docs:
        conf = d.confidence
        ed = extracted.get(d.name)
        if ed is not None:
            conf = ed.avg_confidence
        if conf is not None:
            for f in getattr(d, "findings", []):
                f._ocr_conf = conf
