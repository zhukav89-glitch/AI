"""python -m unittest discover -s tests -v (без файлов пациентов)."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preop_check"))
from parser import DocumentParser, Finding
from text_extractor import ExtractedDoc, PageText
from validator import validate_finding
from analyzer import _date_status


def ed(*texts):
    return ExtractedDoc("test.pdf", [PageText(i + 1, text, "text_layer")
                                    for i, text in enumerate(texts)])


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.p = DocumentParser()

    def test_group_under_generic_heading(self):
        d = self.p.parse("test.pdf", ed("Биохимические исследования\nГруппа крови\nB(III)\nРезус фактор\nФенотипирование эритроцитов\n24.04.2026"))
        self.assertEqual(d.doc_type, "grp")
        self.assertEqual(d.findings, [])

    def test_nbsp_date_not_certificate(self):
        d = self.p.parse("test.pdf", ed("Дуплексное сканирование вен нижних конечностей\nДата\u00a0рождения:\n28.02.1983\nВремя\u00a0исследования:\n24.04.2026 11:42",
                                      "Действителен с 24.09.2025 по 24.09.2026"))
        self.assertEqual(d.exam_date, date(2026, 4, 24))

    def test_anesthesia_date_not_instructions(self):
        d = self.p.parse("test.pdf", ed("Первичный осмотра анестезиолога\nДата:\n24.04.2026",
                                      "Рекомендации\nПоследний прием пищи 27.04.2026\nна 28.04.2026"))
        self.assertEqual(d.exam_date, date(2026, 4, 24))

    def test_consultation_not_split(self):
        ds = self.p.parse_many("test.pdf", ed("Прием (осмотр, консультация) врача-терапевта первичный\nАНАМНЕЗ\nЭКГ",
                                             "РЕКОМЕНДАЦИИ\nОбщий анализ крови"))
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].doc_type, "ter")

    def test_bundle_split(self):
        ds = self.p.parse_many("test.pdf", ed("ОБЩИЙ АНАЛИЗ КРОВИ\n22.04.2026",
                                             "Флюорография легких\n10.04.2026"))
        self.assertEqual([d.doc_type for d in ds], ["oak", "flg"])
        self.assertEqual([d.source_pages for d in ds], [[1], [2]])

    def test_mammography_not_breast_ultrasound(self):
        d = self.p.parse("test.pdf", ed("Рентгенологическое обследование молочных желез\nмаммографический комплекс\nРекомендации: УЗИ молочных желез"))
        self.assertEqual(d.doc_type, "mammo")

    def test_ambiguous_date_blank(self):
        self.assertIsNone(self.p._most_likely_doc_date("10.04.2026\n22.04.2026", None))
        self.assertIsNone(self.p._most_likely_doc_date("Действителен с 24.09.2025 по 24.09.2026", None))

    def test_no_surgery_no_expiry(self):
        self.assertIn("не указана", _date_status(date(2026, 4, 22), None, 14))

    def test_inline_numeric(self):
        d = self.p.parse("test.pdf", ed("Креатинин 77.5 мкмоль/л 49-90\nБилирубин общий 9.7 мкмоль/л 5-21"))
        f = {f.analyte: f for f in d.findings}
        self.assertEqual(f["креатинин"].value, 77.5)
        self.assertEqual(f["билирубин"].value, 9.7)

    def test_ocr_needs_review(self):
        f = Finding("мно", "МНО", value=0.94, value_raw="0.94", source_kind="ocr")
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Проверить"))

    def test_ocr_two_readings_confirm(self):
        f = Finding("мно", "МНО", value=0.94, value_raw="0.94", source_kind="ocr",
                    alt_value=0.94, alt_raw="0.94", readings=2)
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Подтверждено 2"))

    def test_ocr_readings_disagree(self):
        f = Finding("гематокрит", "НСТ", value=37.4, value_raw="37.4", source_kind="ocr",
                    alt_value=34.0, alt_raw="34.0")
        validate_finding(f)
        self.assertIn("расходятся", f.recognition)

    def test_lost_decimal_candidate_not_substituted(self):
        f = Finding("пти", "ПТИ", value=1151, value_raw="1151", ref_low=70, ref_high=130,
                    source_kind="ocr")
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Не распознано"))
        self.assertEqual(f.candidate, 115.1)
        self.assertEqual(f.value, 1151)

    def test_lost_decimal_fixed_by_second_reading(self):
        f = Finding("пти", "ПТИ", value=1151, value_raw="1151", ref_low=70, ref_high=130,
                    source_kind="ocr", alt_value=115.1, alt_raw="115.1")
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Исправлено автоматически"))
        self.assertEqual(f.value, 115.1)

    def test_consultation_wrapped_header_not_split(self):
        # PyMuPDF на другой машине может перенести «врача-\nтерапевта» на две строки
        ds = self.p.parse_many("t.pdf", ed("ООО КЛИНИКА\nПрием (осмотр, консультация) врача-\nтерапевта первичный\n"
                                           "Общий анализ мочи от 22.04.2026\nДата осмотра: 24.04.2026",
                                           "Ат к Treponema pallidum от 10/04/26 - отрицательно", "РЕКОМЕНДАЦИИ"))
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].doc_type, "ter")
        self.assertEqual(ds[0].findings, [])

    def test_urine_qualitative(self):
        d = self.p.parse("t.pdf", ed("ОБЩИЙ АНАЛИЗ МОЧИ\nГемоглобин в моче отрицательно мг/л 0-10\n"
                                     "Лейкоциты в моче 2 кл/мкл 0-20\nГлюкоза в моче отрицательно ммоль/л 0-2.8"))
        f = {x.analyte: x for x in d.findings}
        self.assertEqual(f["моча: гемоглобин"].qualitative, "отрицательно")
        self.assertEqual(f["моча: лейкоциты"].value, 2)
        self.assertNotIn("гемоглобин", f)

    def test_rdw_not_erythrocytes(self):
        d = self.p.parse("t.pdf", ed("ОБЩИЙ АНАЛИЗ КРОВИ\nРаспределение эритроцитов по 13.5 % 11-15\n"
                                     "Эритроциты в крови (RBC) 4.22 10^12/L 3.8-5.1"))
        f = {x.analyte: x for x in d.findings}
        self.assertEqual(f["эритроциты"].value, 4.22)

    def test_birth_date_equal_exam_rejected(self):
        from parser import Document, build_patient_card
        a = Document("a", birth_date=date(2026, 4, 22), exam_date=date(2026, 4, 22), source="ocr")
        b = Document("b", birth_date=date(1983, 2, 28), exam_date=date(2026, 4, 24))
        self.assertEqual(build_patient_card([a, b]).birth_date, date(1983, 2, 28))

    def test_no_number_from_pregnancy_qualifier(self):
        d = self.p.parse("test.pdf", ed("Коагулограмма\nФибриноген ы ta Беременные 2 триместр\n2.1-5.7"))
        self.assertIsNone(d.findings[0].value)

    def test_no_numeric_from_next_analyte(self):
        d = self.p.parse("test.pdf", ed("Гемоглобин\nЛейкоциты\n6.2\nГематокрит"))
        f = next(f for f in d.findings if f.analyte == "гемоглобин")
        self.assertIsNone(f.value)

    # --- 1.8: исправления по отчету 27.10.2026 ---
    def _ocr(self, primary, alt):
        return ExtractedDoc("test.pdf", [PageText(1, primary, "ocr", 70.0, alt_text=alt)])

    def test_antithrombin_roman_numeral(self):
        d = self.p.parse("test.pdf", ed("Коагулограмма\nАнтитромбин Il | 93.6 | % 75-125 |"))
        f = next(f for f in d.findings if f.analyte == "антитромбин")
        self.assertEqual(f.value, 93.6)

    def test_urine_qualitative_after_punctuation(self):
        d = self.p.parse("test.pdf", ed("Общий анализ мочи\n[Нитриты . ‚ отрицательно | мг/л | отрицательно"))
        f = next(f for f in d.findings if f.analyte == "моча: нитриты")
        self.assertEqual(f.qualitative, "отрицательно")

    def test_urine_ketones_and_gravity(self):
        d = self.p.parse("test.pdf", ed("Общий анализ мочи\nКетоновые тела 1.5 | ммоль/л | 0-0.5 |\nУдельный вес | 1.023 | 1.015 - 1.022 |"))
        k = next(f for f in d.findings if f.analyte == "моча: кетоны")
        g = next(f for f in d.findings if f.analyte == "моча: удельный вес")
        self.assertEqual(k.value, 1.5)
        self.assertEqual(g.value, 1.023)
        validate_finding(g)
        self.assertEqual(g.recognition, "Подтверждено авто")

    def test_gravity_1023_equals_1_023(self):
        d = self.p.parse("test.pdf", self._ocr("Общий анализ мочи\nУдельный вес | 1.023 | 1.015 - 1.022 |",
                                             "Общий анализ мочи\nУдельный вес | 1023 | 1015-1022 |"))
        g = next(f for f in d.findings if f.analyte == "моча: удельный вес")
        self.assertEqual(g.readings, 2)

    def test_alt_full_name_from_second_reading(self):
        d = self.p.parse("test.pdf", self._ocr("Биохимические исследования\nАланинаминотрансфераза (АЛТ) м мо 05° О",
                                             "Биохимические исследования\nАланинаминотрансфераза (ATT) 14.3 | Ед/л 0-35 |"))
        f = next(f for f in d.findings if f.analyte == "алт")
        self.assertEqual(f.value, 14.3)
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Проверить"))

    def test_impossible_primary_replaced_by_plausible_second_reading(self):
        d = self.p.parse("test.pdf", self._ocr("Общий анализ крови\n|Гематокрит (НСТ) | _ 374 | | 33-45 |",
                                             "Общий анализ крови\n| Гематокрит (НСТ) | 37.1 | % | 33-45 |"))
        f = next(f for f in d.findings if f.analyte == "гематокрит")
        validate_finding(f)
        self.assertEqual(f.value, 37.1)
        self.assertIn("расходятся", f.recognition)

    def test_biochemistry_header(self):
        d = self.p.parse("test.pdf", ed("БИОХИМИЧЕСКИЕ ИССЛЕДОВАНИЯ\nГлюкоза 5.23 ммоль/л"))
        self.assertEqual(d.doc_type, "bio")

    def test_platelet_unit_not_percent(self):
        d = self.p.parse("test.pdf", ed("Общий анализ крови\n[Тромбоциты (PLT) 303 —-10%9/L | 160 - 360 a |"))
        f = next(f for f in d.findings if f.analyte == "тромбоциты")
        self.assertEqual(f.value, 303)
        self.assertNotEqual(f.unit, "%")

    def test_ecg_tape_without_text_joined(self):
        doc = ExtractedDoc("test.pdf", [PageText(1, "ЭКГ\nИнтервал QRS 0,08 сек.", "ocr", 64.0),
                                        PageText(2, "SA LA 9A ле", "ocr", 46.0)])
        ds = self.p.parse_many("test.pdf", doc)
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].source_pages, [1, 2])

    def test_syphilis_sampling_date_with_commas(self):
        d = self.p.parse("test.pdf", ed("Анализ крови на сифилис\nДата и время забора биоматериала 10,04,2026\nЛабораторное отделение 13.04.2026"))
        self.assertEqual(d.exam_date, date(2026, 4, 10))


    # --- 1.9: отчет 28.10.2026 ---
    def test_stray_letter_before_value(self):
        d = self.p.parse("test.pdf", ed("Коагулограмма\n| АПТВ (AYTB) В 21.6 | ск | 20.8 - 28.2"))
        f = next(f for f in d.findings if f.analyte == "ачтв")
        self.assertEqual(f.value, 21.6)

    def test_slash_before_value(self):
        d = self.p.parse("test.pdf", ed("Общий анализ крови\n| Гемоглобин в крови (HGB) / 121 | г/л | 117-155"))
        f = next(f for f in d.findings if f.analyte == "гемоглобин")
        self.assertEqual(f.value, 121)

    def test_stray_letter_before_qualitative(self):
        d = self.p.parse("test.pdf", ed("Общий анализ мочи\n[Нитриты у ‚ отрицательно | мг/л | отриц"))
        f = next(f for f in d.findings if f.analyte == "моча: нитриты")
        self.assertEqual(f.qualitative, "отрицательно")

    def test_not_detected_kept(self):
        d = self.p.parse("test.pdf", ed("Общий анализ мочи\nБелок не обнаружено г/л"))
        f = next(f for f in d.findings if f.analyte == "моча: белок")
        self.assertTrue(f.qualitative.startswith("не"))

    def test_impossible_reference_dropped(self):
        d = self.p.parse("test.pdf", ed("Общий анализ мочи\npH | 5.5 | 45-8 |\nУдельный вес 1.018 105-102"))
        ph = next(f for f in d.findings if f.analyte == "моча: pH")
        sg = next(f for f in d.findings if f.analyte == "моча: удельный вес")
        self.assertIsNone(ph.ref_low); self.assertIsNone(sg.ref_low)
        d = self.p.parse("test.pdf", ed("Общий анализ крови\nЭритроциты в крови (RBC) 4.22 | 38 - 54"))
        self.assertIsNone(next(f for f in d.findings if f.analyte == "эритроциты").ref_low)

    def test_reference_disagreement(self):
        d = self.p.parse("test.pdf", self._ocr("Общий анализ мочи\nКетоновые тела 1.5 | ммоль/л | 0-5 |",
                                             "Общий анализ мочи\nКетоновые тела 1.5 | ммоль/л | 0-0.5 |"))
        f = next(f for f in d.findings if f.analyte == "моча: кетоны")
        self.assertIsNone(f.ref_high)
        self.assertIn("сверить", f.ref_raw)

    def test_hiv_date_above_label(self):
        d = self.p.parse("test.pdf", ed("Анализ крови на ВИЧ № 32969\n10.04.2026 11:56:53\nДата и время забора биоматериала\n17.04.2026"))
        self.assertEqual(d.exam_date, date(2026, 4, 10))


    def test_three_readings_majority(self):
        pt = PageText(1, "Общий анализ крови\nЭритроциты в крови (RBC) | 42 |", "ocr", 70.0,
                      alt_text="Общий анализ крови\nЭритроциты в крови (RBC) 4.22 | 3.8-5.1")
        pt.table_text = "Эритроциты в крови (RBC) 4.22 10412/L 3.8-5.1"
        d = self.p.parse("test.pdf", ExtractedDoc("test.pdf", [pt]))
        f = next(f for f in d.findings if f.analyte == "эритроциты")
        validate_finding(f)
        self.assertEqual(f.value, 4.22)
        self.assertEqual(f.readings, 2)
        self.assertEqual((f.ref_low, f.ref_high), (3.8, 5.1))
        self.assertTrue(f.recognition.startswith("Подтверждено"))

    def test_table_reading_only(self):
        pt = PageText(1, "Общий анализ крови\nГемоглобин в крови (HGB)", "ocr", 70.0, alt_text="Общий анализ крови")
        pt.table_text = "Гемоглобин в крови (HGB) 121 г/л 117-155"
        d = self.p.parse("test.pdf", ExtractedDoc("test.pdf", [pt]))
        f = next(f for f in d.findings if f.analyte == "гемоглобин")
        validate_finding(f)
        self.assertEqual(f.value, 121)
        self.assertTrue(f.recognition.startswith("Проверить"))


    def test_lost_separator_does_not_vote(self):
        pt = PageText(1, "Общий анализ мочи\nКетоновые тела i 15 ммоль/л | 0-05 |", "ocr", 70.0,
                      alt_text="Общий анализ мочи\nКетоновые Tena i 1.5 | ммоль/л | 0-05 |")
        pt.table_text = "Кетоновые тела 15 ммоль/л 0-0.5"
        d = self.p.parse("test.pdf", ExtractedDoc("test.pdf", [pt]))
        f = next(f for f in d.findings if f.analyte == "моча: кетоны")
        validate_finding(f)
        self.assertEqual(f.value, 1.5)
        self.assertTrue(f.recognition.startswith("Проверить"))
        self.assertEqual(f.ref_high, 0.5)

    def test_confirmed_integer_far_above_decimal_ref(self):
        pt = PageText(1, "Общий анализ мочи\nКетоновые тела 15 ммоль/л 0-0.5", "ocr", 70.0,
                      alt_text="Общий анализ мочи\nКетоновые тела 15 ммоль/л 0-0.5")
        d = self.p.parse("test.pdf", ExtractedDoc("test.pdf", [pt]))
        f = next(f for f in d.findings if f.analyte == "моча: кетоны")
        validate_finding(f)
        self.assertIn("потеря десятичного знака", f.recognition)

    def test_leading_zero_not_accepted(self):
        d = self.p.parse("test.pdf", ed("Коагулограмма\nМНО 094 у.е. 0.8 - 1.4"))
        f = next(f for f in d.findings if f.analyte == "мно")
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Не распознано"))

    def test_soe_ocr_variants_and_trailing_dot(self):
        for line in ("co3 11 мм/час 2-15", "lcos I 11 | мм/час | 2-15", "с0э 11 мм/час 2-15"):
            d = self.p.parse("test.pdf", ed("Общий анализ крови\n" + line))
            self.assertEqual(next(f for f in d.findings if f.analyte == "соэ").value, 11, line)
        d = self.p.parse("test.pdf", ed("Общий анализ крови\nЭритроциты в крови (RBC) 4.22. 10^12/1. 3.8 - 5.1"))
        self.assertEqual(next(f for f in d.findings if f.analyte == "эритроциты").value, 4.22)

    # ---------- 1.10 ----------
    def ocr(self, text, alt=None, alt2=None, tab=None, conf=80):
        return ExtractedDoc("scan.pdf", [PageText(1, text, "ocr", conf, alt_text=alt,
                                                  table_text=tab, alt2_text=alt2)])

    def test_he_obnaruzheno_latin(self):
        d = self.p.parse("t.pdf", ed("Общий анализ мочи\nБелок He обнаружено\nКетоновые тела кзчественный) He обнаружено"))
        f = next(f for f in d.findings if f.analyte == "моча: белок")
        self.assertEqual(f.qualitative, "не обнаружено")

    def test_urine_sediment_range(self):
        d = self.p.parse("t.pdf", ed("Общий анализ мочи\nЛейкоциты 0-1 в п/зр 0-5"))
        f = next(f for f in d.findings if f.analyte.startswith("моча: лейк"))
        self.assertEqual(f.range_value, (0, 1))
        self.assertIsNone(f.value)

    def test_alat_asat_pti(self):
        d = self.p.parse("t.pdf", ed("Биохимический анализ крови\nАлАТ 23 Ед/л <31\nАсАТ 27 Ед/л <31"))
        names = {f.analyte: f.value for f in d.findings}
        self.assertEqual(names.get("алт"), 23)
        self.assertEqual(names.get("аст"), 27)
        d = self.p.parse("t.pdf", ed("Коагулограмма\nПротромбиновый индекс 102 % 80-120"))
        self.assertEqual(next(f for f in d.findings if f.analyte == "пти").value, 102)

    def test_hb_g_dl_conversion_visible(self):
        d = self.p.parse("t.pdf", ed("Клинический анализ крови\nГемоглобин 14.0 г/дл 11.7-16.1"))
        f = next(f for f in d.findings if f.analyte == "гемоглобин")
        self.assertEqual(f.value, 140)
        self.assertTrue(f.conversion)
        validate_finding(f)
        self.assertIn("пересчет", f.recognition)

    def test_date_labels_and_birth_in_age(self):
        d = self.p.parse("t.pdf", ed("ИССЛЕДОВАНИЕ ОТДЕЛЯЕМОГО МОЧЕПОЛОВЫХ ОРГАНОВ\nВозраст 26.06.1960\nАнализ выполнен: 19.10.2026г."))
        self.assertEqual(d.doc_type, "flora")
        self.assertEqual(d.exam_date, date(2026, 10, 19))
        self.assertEqual(d.birth_date, date(1960, 6, 26))

    def test_ocr_distorted_sampling_label(self):
        d = self.p.parse("t.pdf", self.ocr("Общий анализ крови\nДата эзятия матернала:14.10.2026 10:10\nДата:06.10.2026"))
        self.assertEqual(d.exam_date, date(2026, 10, 14))

    def test_classification_new_types(self):
        cases = {"Дуплексное сканирование брахиоцефальных артерий": "bca",
                 "ПРОТОКОЛ УЛЬТРАЗВУКОВОГО ИССЛЕДОВАНИЯ\nОрганы малоготаза": "uzi",
                 "Ультразвуковое исследование органов брюшной полости": "uzi",
                 "Регистрация электрокардиограммы": "ekg",
                 "Антитела к Treponema pallidum": "sif"}
        for text, t in cases.items():
            self.assertEqual(self.p.parse("t.pdf", ed(text)).doc_type, t, text)

    def test_two_of_four_readings_not_confirmed(self):
        d = self.p.parse("t.pdf", self.ocr("Общий анализ крови\nЛейкоциты 4.84 4.5-11",
                                           alt="Общий анализ крови\nЛейкоциты 4.84 4.5-11",
                                           alt2="Общий анализ крови\nЛейкоциты 4.64 4.5-11",
                                           tab="Общий анализ крови\nЛейкоциты 4.64 4.5-11"))
        f = next(f for f in d.findings if f.analyte == "лейкоциты")
        self.assertEqual(f.readings, 1)
        validate_finding(f)
        self.assertTrue(f.recognition.startswith("Проверить"), f.recognition)

    def test_multi_blank_sheet_types(self):
        d = self.p.parse("t.pdf", self.ocr("Биохимические исследования\nХолестерин 4.84\n"
                                           "Антитела к Treponema pallidum He обнаружено\nHBsAg He обнаружено"))
        self.assertIn("sif", d.all_types)
        self.assertIn("inf", d.all_types)

    def test_lis_code_then_russian_name(self):
        d = self.p.parse("t.pdf", ed("Общий анализ крови\nWBC Лейкощиты 4,44 4-8\nСОЭ по Панченкову 19 2-15\n"
                                     "PLR Крупные тромбоциты 36 %\nMCH Среднее содержание гемоглобина в эритроците 31"))
        v = {f.analyte: f.value for f in d.findings}
        self.assertEqual(v.get("лейкоциты"), 4.44)
        self.assertEqual(v.get("соэ"), 19)
        self.assertNotIn("тромбоциты", v)
        self.assertNotIn("гемоглобин", v)

    def test_protocol_continuation_page(self):
        doc = ExtractedDoc("scan.pdf", [
            PageText(1, "ЭХОКАРДИОГРАФИЯ\nДата рождения 26.06.1960\nФВ 60%", "ocr", 80),
            PageText(2, "межпредсердная перегородка интактна, сброса крови нет\nшунтирующих потоков не выявлено\nВРАЧ: Иванова № 02.10:2026\nЗаключение", "ocr", 70)])
        ds = self.p.parse_many("scan.pdf", doc)
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0].source_pages, [1, 2])
        self.assertEqual(ds[0].exam_date, date(2026, 10, 2))


if __name__ == "__main__":
    unittest.main()
