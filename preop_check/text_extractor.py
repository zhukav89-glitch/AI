# -*- coding: utf-8 -*-
"""
Извлечение текста из документов.

Приоритет: текстовый слой PDF (точно) -> OCR (Tesseract, с уверенностью).
Оригиналы не меняются, извлеченный текст хранится с координатной привязкой
к источнику (страница + смещение в тексте).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("preop.text")

try:
    import pymupdf  # PyMuPDF
except ImportError:
    pymupdf = None

try:
    import pytesseract
    from PIL import Image
    import io as _io
except ImportError:
    pytesseract = None

try:
    from pdf2image import convert_from_bytes  # noqa: F401  (резерв)
except ImportError:
    convert_from_bytes = None


@dataclass
class PageText:
    page: int                      # номер страницы (с 1)
    text: str
    source: str                    # "text_layer" | "ocr"
    confidence: Optional[float] = None  # средняя уверенность OCR (0-100)
    alt_text: Optional[str] = None      # второе независимое чтение OCR (другая предобработка)
    skew: Optional[float] = None        # угол выравнивания скана, градусы
    table_text: Optional[str] = None    # третье чтение: таблица, разрезанная по линиям на ячейки
    alt2_text: Optional[str] = None     # еще одно чтение: бинаризация (см. _image_to_page)


@dataclass
class ExtractedDoc:
    name: str
    pages: List[PageText] = field(default_factory=list)
    error: Optional[str] = None
    ocr_error: Optional[str] = None   # причина сбоя OCR (если был attempted и не удался)

    @property
    def full_text(self) -> str:
        return "\n".join(f"[стр. {p.page}]\n{p.text}" for p in self.pages)

    @property
    def source(self) -> str:
        if any(p.source == "ocr" for p in self.pages):
            return "ocr"
        if self.pages and all(p.source == "none" for p in self.pages):
            return "none"
        if self.pages:
            return "text_layer"
        return "none"

    @property
    def avg_confidence(self) -> Optional[float]:
        vals = [p.confidence for p in self.pages if p.confidence is not None]
        return sum(vals) / len(vals) if vals else None


class TextExtractor:
    MIN_TEXT_LAYER_CHARS = 40   # меньше — считаем слой пустым и делаем OCR
    OCR_TIMEOUT = 180           # секунд на одно чтение страницы

    def __init__(self, settings=None):
        self.settings = settings
        self._last_ocr_error = None
        if pytesseract and settings and settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    def extract(self, name: str, data: bytes) -> ExtractedDoc:
        low = name.lower()
        try:
            if data[:4] == b"%PDF" and pymupdf:
                return self._from_pdf(name, data)
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return self._from_image(name, data)
            return ExtractedDoc(name=name, error="Неподдерживаемый формат файла")
        except Exception as e:
            log.error("Ошибка извлечения %s: %s", name, e)
            return ExtractedDoc(name=name, error=str(e))

    # ---------- PDF: сначала текстовый слой, OCR только при необходимости ----------
    def _from_pdf(self, name: str, data: bytes) -> ExtractedDoc:
        doc = ExtractedDoc(name=name)
        pdf = pymupdf.open(stream=data, filetype="pdf")
        max_pages = min(len(pdf), self.settings.max_pdf_pages if self.settings else 15)
        if max_pages < len(pdf):
            doc.ocr_error = f"Обработано только {max_pages} из {len(pdf)} страниц: превышен лимит"
        for i in range(max_pages):
            page = pdf[i]
            layer = (page.get_text() or "").strip()
            # Видимые символы: невидимые слои (нулевая ширина, только пробелы)
            # не считаем текстом -> отправляем страницу на OCR
            visible = re.sub(r"[\s\u200b-\u200f\u2060\ufeff\u00ad\x00]", "", layer)
            if len(visible) >= 20:
                doc.pages.append(PageText(page=i + 1, text=layer, source="text_layer"))
                continue
            # нет слоя (скан) -> рендерим в картинку и распознаем
            pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3))
            img_bytes = pix.tobytes("png")
            pt = self._image_to_page(i + 1, img_bytes)
            if pt:
                doc.pages.append(pt)
            else:
                if not doc.ocr_error:
                    doc.ocr_error = self._last_ocr_error or "OCR вернул пустой результат"
                doc.pages.append(PageText(page=i + 1, text="", source="none"))
        pdf.close()
        return doc

    # ---------- Изображение ----------
    def _from_image(self, name: str, data: bytes) -> ExtractedDoc:
        doc = ExtractedDoc(name=name)
        pt = self._image_to_page(1, data)
        if pt:
            doc.pages.append(pt)
        else:
            doc.error = "Не удалось распознать изображение"
        return doc

    def _image_to_page(self, page_num: int, img_bytes: bytes) -> Optional[PageText]:
        self._last_ocr_error = None
        if not pytesseract:
            self._last_ocr_error = "pytesseract не установлен (pip install pytesseract)"
            log.error(self._last_ocr_error)
            return None
        try:
            image = Image.open(_io.BytesIO(img_bytes))
            # Ориентация: снимки часто приходят боком — определяем через OSD
            image = self._auto_orient(image)
            gray = prepare_gray(image)
            clean = remove_table_lines(gray)
            # Основное чтение: страница без линий таблицы, режим psm 6 (таблицы бланков)
            best = self._ocr_pil(clean, page_num, psm=6)
            # Слабый результат — пробуем повороты (запасной путь, если OSD не справился)
            if self._is_poor(best):
                for angle in (90, 180, 270):
                    g2 = prepare_gray(image.rotate(angle, expand=True))
                    c2 = remove_table_lines(g2)
                    cand = self._ocr_pil(c2, page_num, psm=6)
                    if cand is not None and self._better(cand, best):
                        best, gray, clean = cand, g2, c2
            # Нетабличные страницы (лента ЭКГ, заключения): psm 6 дает мусор,
            # автоматическая разметка psm 3 читает их лучше
            if best is None or (best.confidence or 0) < 60:
                cand = self._ocr_pil(gray, page_num, psm=3)
                if cand is not None and (best is None or (cand.confidence or 0) > (best.confidence or 0)):
                    best = cand
            if best is not None and not best.text.strip():
                self._last_ocr_error = "OCR выполнен, но текст не получен"
                return None
            if best is not None:
                best.skew = getattr(gray, "_skew", None)
                lang = self.settings.ocr_lang if self.settings else "rus+eng"
                # Чтение без удаления линий. На чистых сканах (например, бланки
                # «Инвитро» с линиями под каждой строкой) стирание линий задевает
                # цифры: «5.2» -> «65.2», «138» -> «BB». Основным становится
                # чтение с большей уверенностью, второе идет в голосование.
                plain = self._ocr_pil(gray, page_num, psm=6)
                if plain is not None and plain.text.strip():
                    if (plain.confidence or 0) > (best.confidence or 0) + 2:
                        plain.skew = best.skew
                        best, plain = plain, best
                    best.alt_text = plain.text
                # Еще одно чтение: бинаризация (другая предобработка).
                try:
                    best.alt2_text = pytesseract.image_to_string(
                        clean.point(lambda v: 0 if v < 170 else 255),
                        lang=lang, config="--psm 6", timeout=self.OCR_TIMEOUT)
                except Exception as e:
                    log.warning("Чтение после бинаризации не выполнено: %s", e)
                # Чтение таблицы по ячейкам: таблицу режем по линиям и собираем
                # строки «показатель  значение  единицы  референс» заново. Так значение
                # не «съезжает» в соседнюю строку, если строки бланка перекошены.
                try:
                    timg = table_rows_image(gray)
                    if timg is not None:
                        best.table_text = pytesseract.image_to_string(
                            timg, lang=lang, config="--psm 6", timeout=self.OCR_TIMEOUT)
                except Exception as e:
                    log.warning("Чтение таблицы по ячейкам не выполнено: %s", e)
            return best
        except Exception as e:
            self._last_ocr_error = f"{type(e).__name__}: {e}"
            log.error("OCR ошибка: %s", e)
            return None

    def _auto_orient(self, image):
        """Разворот по данным OSD (Tesseract сам определяет угол скана)."""
        try:
            osd = pytesseract.image_to_osd(image, output_type=pytesseract.Output.DICT, timeout=15)
            rot = int(osd.get("rotate") or 0)
            if rot:
                # OSD дает градусы ПО часовой, PIL крутит ПРОТИВ часовой
                return image.rotate(-rot, expand=True)
        except Exception as e:
            log.debug("OSD недоступен: %s", e)
        return image

    @staticmethod
    def _is_poor(pt: Optional[PageText]) -> bool:
        """Слабое распознавание: мало кириллицы и низкая уверенность.

        Русские документы при верной ориентации дают много кириллицы;
        снятый боком скан читается как бессмыслица (латиница-двойники).
        """
        if pt is None:
            return True
        cyr = TextExtractor._cyr_count(pt.text or "")
        conf = pt.confidence or 0
        # Высокая уверенность без кириллицы (латиница/цифры) — не повод крутить страницу
        return conf < 65 or (cyr < 10 and conf < 80)

    @staticmethod
    def _cyr_count(text: str) -> int:
        return sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")

    @staticmethod
    def _better(a: PageText, b: Optional[PageText]) -> bool:
        """a лучше b: больше кириллицы, при равенстве — выше уверенность."""
        def score(pt):
            return (TextExtractor._cyr_count(pt.text or ""), pt.confidence or 0)
        if b is None:
            return True
        # поворот с заметно меньшей уверенностью — мусор, даже если в нем есть кириллица
        if (a.confidence or 0) + 10 < (b.confidence or 0):
            return False
        return score(a) > score(b)

    def _ocr_pil(self, image, page_num: int, psm: int = 6) -> Optional[PageText]:
        """OCR одного изображения (PIL), с восстановлением строк."""
        try:
            # Данные по словам: текст + уверенность каждого слова
            data = pytesseract.image_to_data(
                image, lang=self.settings.ocr_lang if self.settings else "rus+eng",
                output_type=pytesseract.Output.DICT,
                config=f"--psm {psm}",
                timeout=self.OCR_TIMEOUT,
            )
            words, confs = [], []
            for w, c in zip(data["text"], data["conf"]):
                w = (w or "").strip()
                if not w:
                    continue
                words.append(w)
                try:
                    ci = float(c)
                    if ci >= 0:
                        confs.append(ci)
                except (TypeError, ValueError):
                    pass
            # Восстанавливаем строки по координатам pytesseract (block/par/line)
            lines: Dict[tuple, list] = {}
            for w, b, p, ln, top in zip(data["text"], data["block_num"],
                                        data["par_num"], data["line_num"], data["top"]):
                w = (w or "").strip()
                if not w:
                    continue
                key = (int(b), int(p), int(ln))
                lines.setdefault(key, []).append((int(top), w))
            ordered = sorted(lines.items(), key=lambda kv: kv[1][0][0])
            text = "\n".join(" ".join(w for _, w in ws) for _, ws in ordered)
            conf = sum(confs) / len(confs) if confs else None
            return PageText(page=page_num, text=text, source="ocr", confidence=conf)
        except Exception as e:
            self._last_ocr_error = f"{type(e).__name__}: {e}"
            log.error("OCR ошибка: %s", e)
            return None


# ----------------------------------------------------------------------------
# Предобработка сканов (только Pillow, без OpenCV — работает на любом Python)
# ----------------------------------------------------------------------------
def _dark_profile(img, axis: int):
    """Средняя «темнота» строк (axis=0) или столбцов (axis=1)."""
    from PIL import ImageOps as _ops
    w, h = img.size
    inv = _ops.invert(img)
    return list(inv.resize((1, h) if axis == 0 else (w, 1), Image.BOX).getdata())


def estimate_skew(gray, max_angle: float = 5.0) -> float:
    """Угол наклона скана по линиям таблицы/строк текста (градусы для PIL.rotate)."""
    small = gray.copy()
    small.thumbnail((1200, 1200))
    bw = small.point(lambda v: 0 if v < 160 else 255)
    w, h = bw.size

    def score(a):
        r = bw.rotate(a, fillcolor=255).crop((int(w * .05), int(h * .05), int(w * .95), int(h * .95)))
        p = sorted(_dark_profile(r, 0))
        return sum(p[-10:])

    steps = [a / 2 for a in range(int(-max_angle * 2), int(max_angle * 2) + 1)]
    coarse = max((score(a), -abs(a), a) for a in steps)[2]
    fine = max((score(coarse + d / 10), -abs(coarse + d / 10), coarse + d / 10) for d in range(-5, 6))[2]
    return round(fine, 2)


def prepare_gray(image):
    """Оттенки серого + контраст + выравнивание наклона."""
    from PIL import ImageOps as _ops
    gray = _ops.autocontrast(_ops.grayscale(image))
    try:
        angle = estimate_skew(gray)
    except Exception:
        angle = 0.0
    if abs(angle) >= 0.3:
        gray = gray.rotate(angle, fillcolor=255, resample=Image.BICUBIC)
    gray._skew = angle
    return gray


def remove_table_lines(gray):
    """Стирает длинные горизонтальные и вертикальные линии таблицы."""
    w, h = gray.size
    bw = gray.point(lambda v: 0 if v < 140 else 255)
    out = gray.copy()
    px, bpx = out.load(), bw.load()
    # Нижняя граница длины: на маленьких изображениях не стирать штрихи букв
    for length, horizontal in ((max(int(w * 0.15), 150), True), (max(int(h * 0.04), 60), False)):
        outer, inner = (h, w) if horizontal else (w, h)
        for o in range(outer):
            run = 0
            for i in range(inner + 1):
                dark = i < inner and bpx[(i, o) if horizontal else (o, i)] == 0
                if dark:
                    run += 1
                    continue
                if run >= length:
                    for j in range(i - run, i):
                        px[(j, o) if horizontal else (o, j)] = 255
                run = 0
    return out


# ---------------------------------------------------------------------------
# Чтение таблицы по ячейкам (только PIL)
# ---------------------------------------------------------------------------
def _line_segments(gray, thr: int = 140):
    """Горизонтальные (y, x0, x1) и вертикальные (x, y0, y1) отрезки линий."""
    w, h = gray.size
    b = gray.point(lambda v: 0 if v < thr else 255).load()
    hl, vl = max(int(w * 0.06), 80), max(int(h * 0.012), 25)
    H, V = [], []
    for y in range(h):
        run = 0
        for x in range(w + 1):
            if x < w and b[x, y] == 0:
                run += 1
                continue
            if run >= hl:
                H.append((y, x - run, x - 1))
            run = 0
    for x in range(w):
        run = 0
        for y in range(h + 1):
            if y < h and b[x, y] == 0:
                run += 1
                continue
            if run >= vl:
                V.append((x, y - run, y - 1))
            run = 0
    return H, V


def _cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def table_rows_image(gray):
    """Собирает изображение, где каждая строка таблицы — одна строка текста.

    Возвращает None, если сетка таблицы не найдена (меньше 3 столбцов или 3 строк).
    """
    w, h = gray.size
    H, V = _line_segments(gray)
    vx = {}
    for x, y0, y1 in V:
        vx[x] = vx.get(x, 0) + (y1 - y0)
    xs = _cluster([x for x, l in vx.items() if l > h * 0.05], 6)
    cols = [(xs[i], xs[i + 1]) for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 40]
    if len(cols) < 2:
        return None

    def col_lines(a, b):
        xc = (a + b) / 2
        return _cluster([y for y, x0, x1 in H if x0 <= xc <= x1 and x1 - x0 >= (b - a) * 0.6], 5)

    lines = [col_lines(a, b) for a, b in cols]
    ref = lines[0]
    if len(ref) < 4:
        return None

    def near(ys, y):
        c = min(ys, key=lambda v: abs(v - y)) if ys else None
        return c if c is not None and abs(c - y) <= 15 else y

    rows = []
    for k in range(len(ref) - 1):
        y0, y1 = ref[k], ref[k + 1]
        if y1 - y0 < 20:
            continue
        cells = []
        for (a, b), ys in zip(cols, lines):
            cy0, cy1 = near(ys, y0), near(ys, y1)
            if cy1 - cy0 < 15:
                cy0, cy1 = y0, y1
            cells.append(gray.crop((int(a) + 6, int(cy0) + 5, int(b) - 5, int(cy1) - 4)))
        rows.append(cells)
    if len(rows) < 3:
        return None
    gap = 70
    width = sum(c.size[0] for c in rows[0]) + gap * len(cols) + 40
    height = sum(max(c.size[1] for c in r) + 24 for r in rows) + 40
    out = Image.new("L", (width, height), 255)
    y = 20
    for r in rows:
        x, rh = 20, max(c.size[1] for c in r)
        for c in r:
            out.paste(c, (x, y + (rh - c.size[1]) // 2))
            x += c.size[0] + gap
        y += rh + 24
    return remove_table_lines(out)
