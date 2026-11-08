# -*- coding: utf-8 -*-
"""
Оркестратор (модуль М8 ТЗ): последовательная обработка случая.

Конвейер (состояния случая):
ЗАГРУЖЕНО -> ИЗВЛЕЧЕНО ТЕКСТА -> ПРОВЕРЕНО РАСПОЗНАВАНИЕ -> ОБЕЗЛИЧЕНО
-> ПРОАНАЛИЗИРОВАНО -> ОТЧЕТ ГОТОВ

Без внешних вызовов: весь конвейер локальный, LLM не используется (MVP).
"""
import hashlib
import json

from version import VERSION, install_check
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional

from analyzer import analyze_case
from deid import Deidentifier
from parser import DocumentParser, build_patient_card, calc_age
from report import ReportBuilder
from rules import applicable_rules, COMORBIDITIES
from text_extractor import TextExtractor
from validator import validate_doc, validate_finding, attach_ocr_confidence

log = logging.getLogger("preop.orchestrator")


class Orchestrator:
    def __init__(self, settings):
        self.settings = settings
        self.extractor = TextExtractor(settings)
        self.parser = DocumentParser()
        self.reporter = ReportBuilder(settings.protocol_version)
        self.status_log: List[Dict] = []

    # ------------------------------------------------------------------
    def process(self, files: List[Dict], direction: str = "", operation: str = "",
                anesthesia: str = "", surgery_date_str: str = "",
                comorbidity_keys: Optional[List[str]] = None) -> Dict:
        self.status_log = []
        comorbidity_keys = comorbidity_keys or []

        # 1. Прием файлов
        self._log("ЗАГРУЖЕНО", f"{len(files)} файлов")
        saved = self._save_files(files)

        # 2. Извлечение текста
        extracted = {}
        for f in saved:
            doc = self.extractor.extract(f["name"], f["data"])
            extracted[f["name"]] = doc
        self._log("ИЗВЛЕЧЕНО ТЕКСТА",
                  f"{sum(1 for d in extracted.values() if not d.error and not d.ocr_error)} без ошибок извлечения, "
                  f"{sum(1 for d in extracted.values() if d.error)} ошибок, "
                  f"OCR-сбоев: {sum(1 for d in extracted.values() if d.ocr_error)}")

        # 3. Парсинг документов
        docs = []
        for name, ed in extracted.items():
            parts = self.parser.parse_many(name, ed)
            for d in parts:
                d.ocr_error = ed.ocr_error or ed.error
            docs.extend(parts)

        # 4. Карточка пациента и защита дат (до валидации — она зависит от дат)
        card = build_patient_card(docs)
        # дата обследования не может совпадать с датой рождения
        if card.birth_date:
            for d in docs:
                if d.exam_date == card.birth_date:
                    d.exam_date = None
                    d.date_basis = "Совпадение с датой рождения: требуется проверка"

        # 5. Проверка распознавания (М5)
        today = date.today()
        for d in docs:
            validate_doc(d, today)
        attach_ocr_confidence(docs, extracted)
        for d in docs:
            for f in d.findings:
                validate_finding(f, self.settings.ocr_conf_threshold)
        self._log("ПРОВЕРЕНО РАСПОЗНАВАНИЕ",
                  f"показателей: {sum(len(d.findings) for d in docs)}")

        # 6. Возраст и дата операции
        surgery_date = self._parse_user_date(surgery_date_str)
        surgery_date_auto = surgery_date is None
        if surgery_date_auto:
            # Дата не указана — принимается текущая системная дата
            surgery_date = date.today()
        if surgery_date and card.birth_date:
            card.age = calc_age(card.birth_date, surgery_date)
        if card.age is None and card.age_direct is not None:
            card.age = card.age_direct

        # 6. Обезличивание (аналитическая зона)
        deid = Deidentifier(card.fio)
        deid_stats = {}
        for d in docs:
            res = deid.scrub(d.text)
            deid_stats[d.name] = res.removed_items
        self._log("ПРОВЕРКА ИДЕНТИФИКАТОРОВ",
                  "Выполнен поиск идентификаторов; исходный текст остается в локальной зоне. "
                  "Автоматическое обезличивание не гарантируется")

        # 7. Анализ по правилам
        comorb_titles = [COMORBIDITIES.get(k, k) for k in comorbidity_keys]
        rules = applicable_rules(direction, card.age, card.sex, comorbidity_keys)
        analysis = analyze_case(docs, rules, surgery_date)
        self._log("ПРОАНАЛИЗИРОВАНО", f"правил применимо: {len(rules)}")

        # 8. Отчет
        files_meta = [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]}
                      for f in saved]
        report_md = self.reporter.build_markdown(
            card, direction, operation, anesthesia, surgery_date,
            comorb_titles, analysis, files_meta,
            surgery_date_auto=surgery_date_auto)

        # Итоговый ответ — только аналитическая зона (без ФИО)
        extraction_incomplete = any(ed.error or ed.ocr_error for ed in extracted.values())
        return {
            "success": True,
            "status": "ТРЕБУЕТ ПРОВЕРКИ: НЕПОЛНОЕ РАСПОЗНАВАНИЕ" if extraction_incomplete else "ОТЧЕТ ГОТОВ; ТРЕБУЕТ ПРОВЕРКИ ВРАЧОМ",
            "version": VERSION,
            "patient": card.to_analytic_dict(),
            "docs": [{"name": d.name, "type": d.doc_title,
                      "date": str(d.exam_date) if d.exam_date else None,
                      "source": d.source,
                      "pages": d.source_pages,
                      "date_basis": d.date_basis,
                      "ocr_error": d.ocr_error,
                      "findings": len(d.findings),
                      "recognition": "Проверить по оригиналу" if d.source == "ocr" else "Текстовый слой: не гарантия точности"} for d in docs],
            "report_markdown": report_md,
            "audit_log": self.status_log,
            "deid_note": "Оригиналы сохранены локально. Имена файлов могут содержать ПДн; JSON и отчет не предназначены для передачи наружу. Защита папки настраивается администратором.",
        }

    # ------------------------------------------------------------------
    def _save_files(self, files: List[Dict]) -> List[Dict]:
        import base64
        out = []
        os.makedirs(self.settings.storage_dir, exist_ok=True)
        case_dir = datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in files:
            name = os.path.basename(f.get("name") or "file")
            raw = f.get("content") or ""
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            digest = hashlib.sha256(raw).hexdigest()[:16]
            path = os.path.join(self.settings.storage_dir, case_dir,
                                f"{digest}_{name}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(raw)
            out.append({"name": name, "data": raw, "size": len(raw),
                        "sha256": digest, "path": path})
        return out

    @staticmethod
    def _parse_user_date(s: str) -> Optional[date]:
        s = (s or "").strip()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def _log(self, stage: str, detail: str = ""):
        self.status_log.append({"stage": stage, "detail": detail,
                               "time": datetime.now().isoformat(timespec="seconds")})
