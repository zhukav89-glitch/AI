# -*- coding: utf-8 -*-
"""Локальный веб-сервер «ПреОпКонтроль». Закрытый контур: без CDN и внешних скриптов."""
import json

from version import VERSION, install_check
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

from orchestrator import Orchestrator            # noqa: E402
from settings import Settings                    # noqa: E402

settings = Settings.from_env()
orchestrator = Orchestrator(settings)

DIRECTIONS = ["хирургия", "урология", "гинекология", "пластика"]
COMORB_KEYS = [
    ("сд", "Сахарный диабет"), ("ибс", "ИБС/АГ/ХСН"), ("аритмия", "Аритмии"),
    ("хобл", "ХОБЛ/астма"), ("язва", "Язвенная болезнь ЖКТ"),
    ("щитовидка", "Щитовидная железа"), ("анемия", "Анемия"),
    ("тромбозы", "Тромбозы/варикоз"), ("неврология", "Неврология (ОНМК/эпилепсия)"),
]

HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ПреОпКонтроль — проверка предоперационного обследования</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#eef2f8;color:#0b2b3d;padding:20px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px} .sub{color:#5a6e85;font-size:13px;margin-bottom:16px}
.panel{background:#fff;border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:0 4px 16px rgba(15,23,42,.06)}
label{font-size:13px;font-weight:600;display:block;margin:10px 0 4px}
input,select{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.drop{border:2px dashed #94a3b8;border-radius:12px;padding:24px;text-align:center;cursor:pointer;background:#f8fafc;margin-top:8px}
.drop.hover{background:#e0eaff}
ul.files{list-style:none;margin-top:8px} ul.files li{background:#f1f5f9;border-radius:8px;padding:6px 10px;margin-top:4px;font-size:13px;display:flex;justify-content:space-between}
button.primary{margin-top:16px;width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(135deg,#1e3c72,#2a5298);color:#fff;font-size:15px;cursor:pointer}
button.primary:disabled{opacity:.5}
.status{margin-top:12px;font-size:13px;color:#5a6e85;min-height:18px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.chip{padding:5px 10px;border:1px solid #cbd5e1;border-radius:999px;font-size:12px;cursor:pointer;background:#fff}
.chip.on{background:#1e3c72;color:#fff;border-color:#1e3c72}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
th{background:#1e3c72;color:#fff;padding:6px 8px;text-align:left;position:sticky;top:0}
td{border-bottom:1px solid #e2e8f0;padding:5px 8px;vertical-align:top}
.tbl-wrap{overflow-x:auto;max-height:70vh;overflow-y:auto;border:1px solid #e2e8f0;border-radius:8px}
pre{white-space:pre-wrap;font-family:inherit}
.hidden{display:none}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{padding:6px 14px;border:1px solid #cbd5e1;border-radius:999px;cursor:pointer;font-size:13px;background:#fff}
.tab.on{background:#1e3c72;color:#fff;border-color:#1e3c72}
.note{font-size:12px;color:#5a6e85;margin-top:10px}
</style></head><body><div class="wrap">
<h1>ПреОпКонтроль</h1>
<div class="sub">Проверка предоперационного обследования по приказу №41-ОД. Внутренний контур, данные не покидают сервер. Итоговое заключение выносит врач.</div>

<div class="panel">
<div class="grid">
 <div><label>Дата операции</label><input id="surgeryDate" type="date"></div>
 <div><label>Направление</label><select id="direction">
   <option value="">— не указано —</option>""" + "".join(
    f'<option>{d}</option>' for d in DIRECTIONS) + """</select></div>
 <div><label>Операция (текст)</label><input id="operation" placeholder="например: ТУР простаты"></div>
 <div><label>Анестезия</label><input id="anesthesia" placeholder="например: эндотрахеальный наркоз"></div>
</div>
<label>Сопутствующие патологии (учитываются отдельными строками приказа)</label>
<div class="chips" id="comorbs">""" + "".join(
    f'<div class="chip" data-key="{k}">{t}</div>' for k, t in COMORB_KEYS) + """</div>
</div>

<div class="panel">
<label>Документы пациента (PDF, JPG, PNG — до 20 МБ)</label>
<div class="drop" id="drop">Перетащите файлы или нажмите для выбора</div>
<input id="fileInput" type="file" multiple accept=".pdf,.jpg,.jpeg,.png" class="hidden">
<ul class="files" id="fileList"></ul>
<button class="primary" id="run">Проверить обследование</button>
<div class="status" id="status"></div>
</div>

<div class="panel hidden" id="resultPanel">
<div class="tabs"><div class="tab on" data-tab="table">Таблица</div><div class="tab" data-tab="summary">Отчет (текст)</div><div class="tab" data-tab="json">JSON</div></div>
<div class="tbl-wrap hidden" id="tab-table"></div>
<pre class="hidden" id="tab-summary"></pre>
<pre class="hidden" id="tab-json"></pre>
<div class="note">Отчет вспомогательный. «Не найдено» = не найдено в загруженных документах. Решение принимает врач.</div>
</div>
</div></body>
<script>
let files=[];let comorbs=new Set();
const $=id=>document.getElementById(id);
function renderFiles(){$('fileList').innerHTML=files.map((f,i)=>`<li><span>${f.name} (${(f.size/1048576).toFixed(1)} МБ)</span><span data-i="${i}" class="rm" style="cursor:pointer;color:#b91c1c">✕</span></li>`).join('');}
$('fileInput').onchange=e=>{files.push(...e.target.files);renderFiles()};
const drop=$('drop');
drop.onclick=()=>$('fileInput').click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('hover')};
drop.ondragleave=()=>drop.classList.remove('hover');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('hover');files.push(...e.dataTransfer.files);renderFiles()};
$('fileList').onclick=e=>{if(e.target.classList.contains('rm')){files.splice(+e.target.dataset.i,1);renderFiles()}};
$('comorbs').onclick=e=>{if(e.target.classList.contains('chip')){const k=e.target.dataset.key;if(comorbs.has(k)){comorbs.delete(k);e.target.classList.remove('on')}else{comorbs.add(k);e.target.classList.add('on')}}};
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));t.classList.add('on');['table','summary','json'].forEach(n=>$('tab-'+n).classList.add('hidden'));$('tab-'+t.dataset.tab).classList.remove('hidden')});

function esc(s){const d=document.createElement('div');d.textContent=s==null?'—':String(s);return d.innerHTML}
function mdTable(md){
  const lines=md.split('\\n').filter(l=>l.trim());
  const rows=lines.filter(l=>l.startsWith('|'));
  if(!rows.length)return '<pre>'+esc(md)+'</pre>';
  let html='<table>';
  rows.forEach((r,i)=>{const cells=r.split('|').slice(1,-1);
    if(i===1&&/^[-: ]+$/.test(rows[i].replace(/\\|/g,'')))return;
    html+='<tr>'+cells.map(c=>(i===0?'<th>':'<td>')+esc(c.trim())+(i===0?'</th>':'</td>')).join('')+'</tr>';});
  return html+'</table>';
}

// Дата операции по умолчанию — текущая системная дата (можно изменить вручную)
(function(){const t=new Date();const p=n=>String(n).padStart(2,'0');
  if(!$('surgeryDate').value)$('surgeryDate').value=t.getFullYear()+'-'+p(t.getMonth()+1)+'-'+p(t.getDate());})();

$('run').onclick=async()=>{
  if(!files.length){alert('Прикрепите документы');return}
  $('run').disabled=true;$('status').textContent='Обработка: извлечение текста, проверка распознавания, анализ по приказу…';
  try{
    const payload={action:'analyze',direction:$('direction').value,operation:$('operation').value,
      anesthesia:$('anesthesia').value,surgery_date:$('surgeryDate').value,
      comorbidities:[...comorbs],files:[]};
    for(const f of files){const b64=await new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f)});
      payload.files.push({name:f.name,type:f.type||'application/octet-stream',size:f.size,content:b64});}
    const r=await fetch(window.location.origin+'/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(!r.ok||!j.success)throw new Error(j.error||'Ошибка сервера');
    $('resultPanel').classList.remove('hidden');
    const summary=j.report_markdown||'';
    const tblStart=summary.indexOf('| №'),tblEnd=summary.indexOf('## В.');
    $('tab-table').innerHTML=mdTable(tblStart>=0?summary.slice(tblStart,tblEnd>0?tblEnd:undefined):summary);
    $('tab-summary').textContent=summary;
    $('tab-json').textContent=JSON.stringify(j.audit||j,null,2);
    $('status').textContent='Готово. Документов: '+(j.docs||[]).length;
  }catch(e){$('status').textContent='Ошибка: '+e.message}
  $('run').disabled=false;
};
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "null")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            import shutil
            tcmd = settings.tesseract_cmd or shutil.which("tesseract")
            # реальный самотест: распознаем тестовую картинку через tesseract
            ocr_test = {"ok": False, "error": ""}
            try:
                from text_extractor import TextExtractor
                te = TextExtractor(settings)
                from PIL import Image, ImageDraw
                import io as _io
                img = Image.new("L", (320, 80), 255)
                try:
                    from PIL import ImageFont
                    font = ImageFont.load_default(size=32)
                except TypeError:
                    font = None
                ImageDraw.Draw(img).text((10, 20), "TEST 12345", fill=0, font=font)
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                pt = te._image_to_page(1, buf.getvalue())
                if pt and "12345" in (pt.text or ""):
                    ocr_test = {"ok": True, "recognized": pt.text.strip(),
                                "confidence": pt.confidence}
                else:
                    ocr_test = {"ok": False,
                                "error": te._last_ocr_error or "пустой результат"}
            except Exception as e:
                ocr_test = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send(200, json.dumps({
                "success": True, "service": "preop-check",
                "version": VERSION,
                "install_check": install_check(),
                "tesseract_cmd": tcmd,
                "tesseract_available": ocr_test["ok"],
                "ocr_selftest": ocr_test,
                "hint": ("Укажите TESSERACT_CMD в .env и перезапустите сервер"
                         if not tcmd else
                         ("OCR работает" if ocr_test["ok"]
                          else f"OCR НЕ РАБОТАЕТ: {ocr_test['error']}")),
            }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self):
        if self.path != "/api/analyze":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = orchestrator.process(
                files=payload.get("files") or [],
                direction=payload.get("direction") or "",
                operation=payload.get("operation") or "",
                anesthesia=payload.get("anesthesia") or "",
                surgery_date_str=payload.get("surgery_date") or "",
                comorbidity_keys=payload.get("comorbidities") or [],
            )
            self._send(200, json.dumps(result, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")
        except Exception as e:
            logging.exception("analyze error")
            self._send(500, json.dumps({"success": False, "error": str(e)},
                                       ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.command, self.path)


if __name__ == "__main__":
    srv = ThreadingHTTPServer((settings.host, settings.port), Handler)
    print(f"ПреОпКонтроль запущен: http://{settings.host}:{settings.port}")
    print("Хранилище оригиналов:", os.path.abspath(settings.storage_dir))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")
