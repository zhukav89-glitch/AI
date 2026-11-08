# -*- coding: utf-8 -*-
"""
Обезличивание (модуль М7 ТЗ, MVP).

Удаляет/замещает прямые идентификаторы из текста и метаданных:
- ФИО пациента (если извлечено парсером);
- дата рождения;
- телефоны, e-mail;
- номера полисов/снимков/заказов;
- ограничивает историю дат.

ВАЖНО (границы MVP): это базовая маскация. Порядок обезличивания по
приказу Роскомнадзора №140 и модели угроз клиники утверждается отдельным
локальным актом до промышленной эксплуатации.
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class DeidResult:
    text: str
    removed_items: list


class Deidentifier:
    def __init__(self, patient_fio: Optional[str] = None):
        self.fio = patient_fio

    def scrub(self, text: str) -> DeidResult:
        removed = []
        t = text or ""

        # ФИО пациента (если известно)
        if self.fio:
            for token in self.fio.split():
                if len(token) > 2:
                    t2 = re.sub(re.escape(token), "[ПАЦИЕНТ]", t)
                    if t2 != t:
                        removed.append("ФИО")
                        t = t2
            # инициалы вида К.Д.В. / Кутырев Д. В.
            surname = self.fio.split()[0]
            initials = re.compile(
                re.escape(surname) + r"|[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ]\.", re.M)
            t2 = initials.sub("[ПАЦИЕНТ]", t)
            if t2 != t:
                removed.append("ФИО (инициалы)")
                t = t2

        # Дата рождения
        t2, n = re.subn(r"(\d{2}\.\d{2}\.\d{4})", "[ДАТА]", t)
        if n:
            removed.append(f"даты ({n})")
            t = t2

        # Телефоны
        t2, n = re.subn(r"(?:\+?\d[\d ()-]{8,}\d)", "[ТЕЛЕФОН]", t)
        if n:
            removed.append(f"телефоны ({n})")
            t = t2

        # E-mail
        t2, n = re.subn(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", t)
        if n:
            removed.append(f"e-mail ({n})")
            t = t2

        # СНИЛС
        t2, n = re.subn(r"\b\d{3}-\d{3}-\d{3}\s?\d{2}\b", "[СНИЛС]", t)
        if n:
            removed.append(f"СНИЛС ({n})")
            t = t2

        return DeidResult(text=t, removed_items=removed)
