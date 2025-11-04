import os
import json
import hashlib
import datetime
import subprocess
import tempfile
import threading
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# ============================
# إعدادات JSONBin والمفتاح السري للمشرف
# ============================
JSONBIN_ID = "68f4ef18ae596e708f1cc0d9"
JSONBIN_KEY = "$2a$10$BV..TadGPZnl8Hs6rUs4h.kJFEnRDmK6YPqd8onbIEhfCKSixLI66"
JSONBIN_BASE = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
ADMIN_SECRET_KEY = "RESIS_TIK_PRO1"

if not ADMIN_SECRET_KEY:
    raise ValueError("⛔️ خطأ فادح: متغير البيئة ADMIN_SECRET_KEY غير معين.")

_jsonbin_session = requests.Session()
_jsonbin_session.headers.update({
    "X-Master-Key": JSONBIN_KEY,
    "Content-Type": "application/json; charset=utf-8"
})
DB_LOCK = threading.Lock()

# ============================
# دوال التخزين (Database Functions)
# ============================
def load_db():
    with DB_LOCK:
        try:
            r = _jsonbin_session.get(JSONBIN_BASE)
            if r.status_code == 404: return {"codes": []}
            r.raise_for_status()
            data = r.json().get("record", {"codes":[]})
            if "codes" not in data:
                data["codes"] = []
            return data
        except (requests.exceptions.RequestException, json.JSONDecodeError):
            return {"codes": []}

def save_db(data):
    with DB_LOCK:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        r = _jsonbin_session.put(JSONBIN_BASE, data=payload)
        r.raise_for_status()

# ============================
# أدوات مساعدة (Helper Functions)
# ============================
def now_iso(): return datetime.datetime.utcnow().isoformat()
def hash_device(device_info: str) -> str: return hashlib.sha256((device_info or "").encode()).hexdigest()

def find_key(db, key: str):
    for row in db.get("codes", []):
        if row.get("key") == key: return row
    return None

def calc_expiry(activated_on_str: str | None, duration_days: int):
    if not activated_on_str: return None
    try:
        activated_on = datetime.datetime.fromisoformat(activated_on_str)
        return activated_on + datetime.timedelta(days=duration_days)
    except (ValueError, TypeError): return None

def ensure_bound_or_bind(db, row, device: str, device_name: str | None):
    dev_hash = hash_device(device)
    if not row.get("device_hash"):
        row["device_hash"] = dev_hash
        row["device_name"] = device_name
        if not row.get("activated_on"): row["activated_on"] = now_iso()
        save_db(db)
        return True
    return row["device_hash"] == dev_hash

# ============================
# إعداد التطبيق (App Setup)
# ============================
app = FastAPI(title="4TIK PRO Service API")
BASE_DIR = Path(__file__).resolve().parent
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# الحد الأقصى لحجم الملف المسموح به (200 ميجابايت)
MAX_FILE_SIZE = 200 * 1024 * 1024

async def verify_admin_key(admin_key: str = Header(..., alias="X-Admin-Key")):
    if admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="غير مصرح لك بالقيام بهذه العملية")

async def verify_content_length(content_length: int = Header(...)):
    if content_length > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"حجم الملف كبير جدًا. الحد الأقصى المسموح به هو {MAX_FILE_SIZE // 1024 // 1024} ميجابايت."
        )

# ============================
# دالة الاشتراك المعدلة فقط
# ============================
@app.get("/subscribe", summary="إضافة اشتراك من المتصفح مباشرة")
async def add_subscription(key: str, duration_days: int = 30, admin_key: str = ""):
    if admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="مفتاح المشرف غير صحيح")
    
    db = load_db()

    # تحقق إذا المفتاح موجود مسبقًا
    if find_key(db, key):
        return JSONResponse(
            content={"message": f"المفتاح '{key}' موجود مسبقًا."},
            media_type="application/json; charset=utf-8"
        )

    # إضافة الاشتراك الجديد
    new_key = {
        "key": key,
        "duration_days": duration_days,
        "activated_on": None,
        "device_hash": "",
        "device_name": None,
        "last_used": None
    }
    db["codes"].append(new_key)
    save_db(db)

    return JSONResponse(
        content={"message": f"تمت إضافة الاشتراك '{key}' لمدة {duration_days} يومًا."},
        media_type="application/json; charset=utf-8"
    )

# ============================
# باقي المسارات لم تمس
# ============================
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home():
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Welcome! Server is running.</h1>", status_code=200)
    return FileResponse(str(index_path))

@app.get("/me", summary="الحصول على معلومات الاشتراك الحالية")
async def me(request: Request):
    key = request.headers.get("X-KEY")
    device = request.headers.get("X-DEVICE")
    device_name = request.headers.get("X-DEVICE-NAME")
    if not key or not device:
        raise HTTPException(status_code=401, detail="المفتاح (X-KEY) ومعرف الجهاز (X-DEVICE) مطلوبان")
    db = load_db()
    row = find_key(db, key)
    if not row:
        raise HTTPException(status_code=404, detail="المفتاح غير صالح")
    if not ensure_bound_or_bind(db, row, device, device_name):
        raise HTTPException(status_code=403, detail="هذا المفتاح مربوط بجهاز آخر")
    
    expires_on = calc_expiry(row.get("activated_on"), row.get("duration_days", 30))
    now = datetime.datetime.utcnow()
    is_expired = expires_on and now >= expires_on
    
    if is_expired:
        raise HTTPException(status_code=403, detail="⛔ انتهت صلاحية هذا المفتاح")
    
    days_left = 0 if is_expired else ((expires_on - now).days if expires_on else row.get("duration_days", 30))
    last_used_time = now_iso()
    row["last_used"] = last_used_time
    save_db(db)
    
    return {
        "key_masked": row["key"][:4] + "****",
        "device_name": row.get("device_name"),
        "activated_on": row.get("activated_on"),
        "expires_on": expires_on.isoformat() if expires_on else None,
        "days_left": days_left,
        "is_active": not is_expired,
        "last_used": last_used_time
    }

@app.post("/process", summary="معالجة الفيديو للمستخدمين المشتركين")
async def process_video(request: Request, file: UploadFile = File(...)):
    key = request.headers.get("X-KEY")
    device = request.headers.get("X-DEVICE")
    if not key or not device:
        raise HTTPException(status_code=401, detail="المفتاح أو معرف الجهاز مطلوب")
    
    db = load_db()
    row = find_key(db, key)
    if not row:
        raise HTTPException(status_code=401, detail="المفتاح غير صحيح")
    if not ensure_bound_or_bind(db, row, device, None):
        raise HTTPException(status_code=403, detail="المفتاح مربوط بجهاز آخر")

    expires_on = calc_expiry(row.get("activated_on"), row.get("duration_days", 30))
    if not expires_on or datetime.datetime.utcnow() >= expires_on:
        raise HTTPException(status_code=403, detail="انتهت صلاحية هذا المفتاح")

    row["last_used"] = now_iso()
    save_db(db)

    try:
        suffix = Path(file.filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_in:
            contents = await file.read()
            tmp_in.write(contents)
            tmp_in_path = tmp_in.name

        tmp_out_path = tmp_in_path.replace(suffix, f"_enhanced{suffix}")
        
        # أمر Real-ESRGAN CPU
        cmd = [
            "realesrgan-ncnn-vulkan",  # على Render استبدل بالنسخة CPU إذا لزم
            "-i", tmp_in_path,
            "-o", tmp_out_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')

        return FileResponse(tmp_out_path, filename=f"enhanced_{file.filename}")
    
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"خطأ في معالجة الفيديو: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"حدث خطأ غير متوقع: {str(e)}")

