# -*- coding: utf-8 -*-
"""Версия сборки и проверка целостности установки.

MANIFEST.json содержит SHA-256 всех модулей сборки. Если архив распакован
не полностью или поверх остались файлы старой версии, /api/health покажет,
какие именно файлы отличаются.
"""
import hashlib
import json
from pathlib import Path

VERSION = "1.10"
_DIR = Path(__file__).resolve().parent


def install_check() -> dict:
    manifest_path = _DIR / "MANIFEST.json"
    if not manifest_path.exists():
        return {"ok": False, "error": "MANIFEST.json не найден — архив распакован не полностью"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed, missing = [], []
    for name, digest in manifest["files"].items():
        p = _DIR / name
        if not p.exists():
            missing.append(name)
        elif hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            changed.append(name)
    return {"ok": not changed and not missing, "build": manifest["version"],
            "changed_files": changed, "missing_files": missing}
