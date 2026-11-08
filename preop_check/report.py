# -*- coding: utf-8 -*-
"""Формирование отчета (модуль М12 ТЗ): карточка + таблица + сводка."""
from datetime import date, datetime
from typing import Optional

from analyzer import CaseAnalysis
from parser import PatientCard


def _show(f) -> str:
    s = _show_value(f)
    conv = getattr(f, "conversion", None)
    if conv and not (f.recognition or "").startswith("Не распознано"):
        s += f" (пересчет: {conv})"
    fd = getattr(f, "from_date", None)
    return s + (f" [от {fd.strftime('%d.%m')}]" if fd else "")


def _show_value(f) -> str:
    """Значение для столбца «Результат». Неподтвержденное значение помечается,
    отбракованное (невозможное) не выдается за результат."""
    rec = f.recognition or ""
    if rec.startswith("Не распознано"):
        raw = f.value_raw or ""
        return f"{f.analyte}: не распознано" + (f" (OCR: {raw})" if raw else "")
    if rec.startswith("Проверить"):
        alt = f.alt_raw if f.alt_raw is not None and str(f.alt_raw) != str(f.value_raw or f.qualitative) else None
        return f"{f.analyte} {f.fmt()}" + (f" / {alt}" if alt else "") + " (сверить)"
    return f"{f.analyte} {f.fmt()}"


class ReportBuilder:
    def __init__(self, protocol_version: str = "Приказ №41-ОД, ред. 1.0"):
        self.protocol_version = protocol_version

    def build_markdown(self, card: PatientCard, direction: str, operation: str,
                       anesthesia: str, surgery_date: Optional[date],
                       comorbidities, analysis: CaseAnalysis,
                       files_meta: list, surgery_date_auto: bool = False) -> str:
        L = []
        L.append("# Предоперационный аудит «ПреОпКонтроль»")
        L.append("")
        L.append(f"**Версия правил:** {self.protocol_version}  ")
        L.append(f"**Сформирован:** {datetime.now().strftime('%d.%m.%Y %H:%M')}  ")
        L.append(f"**Файлов обработано:** {len(files_meta)}")
        L.append("")

        # --- Часть А. Карточка ---
        L.append("## А. Карточка пациента и исходные условия")
        L.append("")
        age = card.age if card.age is not None else "не определен"
        sex = card.sex or "не указан"
        L.append(f"- Возраст на дату операции: **{age}** (пол: {sex})")
        L.append(f"- Направление: **{direction or 'не указано'}**")
        L.append(f"- Операция: **{operation or 'не указана'}**")
        L.append(f"- Анестезия: **{anesthesia or 'не указана'}**")
        sd = surgery_date.strftime('%d.%m.%Y') if surgery_date else 'не указана'
        note = " (не указана, принята текущая системная дата)" if surgery_date and surgery_date_auto else ""
        L.append(f"- Дата операции: **{sd}**{note}")
        if comorbidities:
            L.append(f"- Сопутствующие патологии: **{', '.join(comorbidities)}**")
        if not surgery_date:
            L.append("> ⚠️ **Дата операции не указана** — сроки годности не проверены. "
                     "Укажите дату операции для корректной проверки.")
        if not direction:
            L.append("> ⚠️ **Направление не указано** — дополнительные обследования по направлению (2.x) не проверялись.")
        L.append("")

        # --- Часть Б. Таблица ---
        L.append("## Б. Проверка обследований по приказу")
        L.append("")
        L.append("| № | Требование | Основание | Найдено | Дата обсл. | Срок | Результат | Референс | Оценка | Распознавание |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")

        for rr in analysis.rule_results:
            r = rr.rule
            found_mark = {"Найдено": "✅", }.get(rr.found, "")
            if rr.found == "Не найдено":
                found_mark = "❌"
            elif rr.found.startswith("Частично"):
                found_mark = "⚠️"
            elif rr.found == "Найдено":
                found_mark = "✅"

            if rr.found == "Не найдено":
                row_vals, row_refs, row_eval, row_rec = "—", "—", "—", "—"
            else:
                vals = [_show(f) for f in rr.values] or ["—"]
                row_vals = "; ".join(vals)
                refs = []
                for f in rr.values:
                    if f.ref_low is not None or f.ref_high is not None:
                        lo = f"{f.ref_low:g}" if f.ref_low is not None else ""
                        hi = f"{f.ref_high:g}" if f.ref_high is not None else ""
                        rng = f"{lo}–{hi}" if lo and hi else (f"<{hi}" if hi else f">{lo}")
                        refs.append(f"{f.analyte} {rng}")
                row_refs = "; ".join(refs) or "—"
                evals = []
                for f in rr.values:
                    if f.recognition.startswith(("Не распознано", "Проверить")):
                        evals.append(f"{f.analyte}: сверить")
                        continue
                    if f.in_range is True:
                        evals.append(f"{f.analyte}: норма")
                    elif f.in_range is False:
                        evals.append(f"{f.analyte}: ОТКЛОНЕНИЕ")
                    elif f.qualitative:
                        evals.append(f"{f.analyte}: {f.qualitative}")
                    else:
                        evals.append(f"{f.analyte}: нет референса")
                row_eval = "; ".join(evals) or "—"
                recs = {}
                for f in rr.values:
                    key = f.recognition.split(" (")[0].split(":")[0]
                    if key.startswith("Подтверждено") and "чтениями OCR" in key:
                        key = "Подтверждено 2–3 чтениями OCR"
                    recs[key] = recs.get(key, 0) + 1
                row_rec = "; ".join(f"{k}: {v}" for k, v in sorted(recs.items())) if recs else "—"
                if len(rr.matched_docs) > 1:
                    row_rec += f" (бланков: {len(rr.matched_docs)})"

            L.append(f"| {r.order} | {r.name} | {r.basis} | {found_mark} {rr.found} | "
                     f"{rr.exam_date.strftime('%d.%m.%Y') if rr.exam_date else '—'} | "
                     f"{rr.date_status} | {row_vals} | {row_refs} | {row_eval} | {row_rec} |")
        L.append("")

        # --- Часть В. Сводка ---
        L.append("## В. Сводка")
        L.append("")
        L.append("Результат предварительный: наличие бланка не подтверждает распознавание всех его компонентов. "
                 "Ноль автоматически отмеченных отклонений не означает отсутствие отклонений. "
                 "Значения со сканов оцениваются, только если два независимых чтения OCR совпали; "
                 "остальные вынесены в список для сверки (п. 7а).")
        L.append("")
        missing = [f"{rr.rule.order} {rr.rule.name}" for rr in analysis.rule_results
                   if rr.found == "Не найдено"]
        partial = [f"{rr.rule.order} {rr.rule.name}" for rr in analysis.rule_results
                   if rr.found.startswith("Частично")]
        expired = [f"{rr.rule.order} {rr.rule.name} ({rr.date_status})"
                   for rr in analysis.rule_results if rr.date_status.startswith("Просрочено")]
        no_date = [f"{rr.rule.order} {rr.rule.name}"
                   for rr in analysis.rule_results if rr.date_status == "Дата не указана"]
        deviations, no_ref = [], []
        for rr in analysis.rule_results:
            deviations.extend(d for d in rr.deviations if d not in deviations)
            no_ref.extend(d for d in rr.no_reference if d not in no_ref)

        L.append(f"**1. Не найдено в загруженных документах ({len(missing)}):**")
        L.extend(f"- ❌ {m}" for m in missing) or ["- отсутствуют"]
        L.append("")
        L.append(f"**2. Найдено частично ({len(partial)}):**")
        L.extend(f"- ⚠️ {p}" for p in partial) or ["- отсутствуют"]
        L.append("")
        L.append(f"**3. Просрочено к дате операции ({len(expired)}):**")
        L.extend(f"- ❌ {e}" for e in expired) or ["- отсутствуют"]
        L.append("")
        L.append(f"**4. Отклонения от референсов среди подтвержденных значений ({len(deviations)}):**")
        L.extend(f"- 🚩 {d}" for d in deviations) or ["- отсутствуют"]
        L.append("")
        if no_ref:
            L.append(f"**4а. Референс не найден — оценка невозможна ({len(no_ref)}):**")
            L.extend(f"- {d}" for d in no_ref)
            L.append("")
        if no_date:
            L.append(f"**5. Дата обследования не указана ({len(no_date)}):**")
            L.extend(f"- ? {n}" for n in no_date)
            L.append("")
        if analysis.unclassified:
            L.append(f"**⚠️ Документы, которые НЕ УДАЛОСЬ РАСПОЗНАТЬ и классифицировать ({len(analysis.unclassified)}):**")
            for u in analysis.unclassified:
                if u.get("error"):
                    reason = f"OCR не выполнен: {u['error']}"
                elif u["source"] == "none" or not u["text_chars"]:
                    reason = ("текст ВООБЩЕ не извлечен — скорее всего OCR не запущен "
                              "(проверьте .env с TESSERACT_CMD и перезапуск сервера)")
                elif u["source"] == "ocr":
                    reason = (f"распознано OCR, но уверенность низкая "
                              f"({(u['ocr_conf'] or 0):.0f}%) или бланк не подходит под шаблоны — "
                              f"улучшите качество фото")
                else:
                    reason = "текст извлечен, но бланк не подходит под шаблоны — пришлите файл, добавлю шаблон"
                L.append(f"- {u['name']} — {reason}")
            L.append("")
        if analysis.outside_plan:
            note = " — направление не указано, правила 2.x не применялись" if not direction else ""
            L.append(f"**6. Документы вне плана приказа ({len(analysis.outside_plan)}){note}:**")
            L.extend(f"- {n}" for n in analysis.outside_plan)
            L.append("")
        if analysis.unrecognized:
            L.append(f"**7. Не удалось распознать ({len(analysis.unrecognized)}):**")
            L.extend(f"- ? {u}" for u in analysis.unrecognized)
            L.append("")
        if analysis.to_verify:
            L.append(f"**7а. Сверить с оригиналом: значения OCR без двойного подтверждения ({len(analysis.to_verify)}):**")
            L.extend(f"- {u}" for u in analysis.to_verify)
            L.append("")
        if analysis.contradictions:
            L.append("**8. Противоречия в исходных данных:**")
            L.extend(f"- ⚠️ {c}" for c in analysis.contradictions)
            L.append("")

        L.append("---")
        L.append("*Отчет сформирован автоматически и является вспомогательным инструментом. "
                 "Итоговое заключение о готовности к операции выносит врач. "
                 "«Не найдено» означает отсутствие в загруженных документах, а не отсутствие обследования.*")
        return "\n".join(L)
