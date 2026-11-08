# -*- coding: utf-8 -*-
"""Настройки MVP «ПреОпКонтроль». Только локальный контур, без внешних вызовов."""
import os
import shutil
from pathlib import Path
from dataclasses import dataclass


def _load_env_file(path: str = ".env") -> None:
    """Мини-загрузчик .env без внешних зависимостей.
    Создайте файл .env рядом с папкой preop_check и перезапустите сервер."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


@dataclass(frozen=True)
class Settings:
    storage_dir: str = "storage"          # локальное хранилище оригиналов
    tesseract_cmd: str = ""               # путь к tesseract, если не в PATH
    ocr_lang: str = "rus+eng"
    ocr_conf_threshold: int = 90          # порог уверенности OCR (0-100)
    max_file_size_mb: int = 20
    max_files: int = 25
    max_pdf_pages: int = 15
    protocol_version: str = "Приказ №41-ОД, ред. 1.0"
    host: str = "127.0.0.1"               # доступ только изнутри сети
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(__file__).resolve().parent.parent
        _load_env_file(str(root / ".env"))
        cmd = os.getenv("TESSERACT_CMD", "") or shutil.which("tesseract") or ""
        if not cmd and os.name == "nt":
            for candidate in (r"D:\OCR\tesseract.exe",
                              r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
                if Path(candidate).is_file():
                    cmd = candidate
                    break
        return cls(
            storage_dir=os.getenv("STORAGE_DIR", "storage"),
            tesseract_cmd=cmd,
            ocr_lang=os.getenv("OCR_LANG", "rus+eng"),
            ocr_conf_threshold=int(os.getenv("OCR_CONF_THRESHOLD", "90")),
            max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "20")),
            max_files=int(os.getenv("MAX_FILES", "25")),
            max_pdf_pages=int(os.getenv("MAX_PDF_PAGES", "15")),
            protocol_version=os.getenv("PROTOCOL_VERSION", "Приказ №41-ОД, ред. 1.0"),
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8080")),
        )

    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024
