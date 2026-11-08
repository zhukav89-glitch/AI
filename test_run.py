# -*- coding: utf-8 -*-
"""Проверка конвейера на тестовых документах (без веб-сервера)."""
import base64, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "preop_check"))
from settings import Settings
from orchestrator import Orchestrator

DOCS_DIR = os.path.expanduser(
    "~/.perplexity/attachments/08246471dc734dad84dcba79a42d8f26")
files = []
for name in ["OAK.PDF", "OAM.PDF", "BKh.PDF", "koagul.pdf", "elektro.pdf",
             "D.PDF", "grkr.pdf", "infektsii.pdf", "sif.pdf", "terapevt.pdf",
             "uzdg.pdf", "EKG.PDF"]:
    p = os.path.join(DOCS_DIR, name)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            files.append({"name": name, "content": base64.b64encode(fh.read()).decode()})

settings = Settings.from_env()
orch = Orchestrator(settings)
result = orch.process(
    files=files,
    direction="хирургия",
    operation="Герниопластика",
    anesthesia="спинальная анестезия",
    surgery_date_str="05.11.2026",
    comorbidity_keys=[],
)
print(result["report_markdown"])
print("\n=== AUDIT ===")
for s in result["audit_log"]:
    print(s["stage"], "|", s["detail"])
print("\n=== DOCS ===")
for d in result["docs"]:
    print(d["name"], "->", d["type"], "| date:", d["date"], "| findings:", d["findings"])
