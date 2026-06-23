import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from score import calc_score
from hazard import (
    fetch_slope_score, fetch_typhoon_score, fetch_coastal_score, fetch_flood_risk,
    fetch_landslide_zone, fetch_inland_flood_zone, fetch_tsunami_zone,
    _load_landslide_zones, _load_inland_flood_zones, _load_tsunami_zones,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# .env 読み込み（python-dotenv があれば使用、なければシンプル実装）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env_path):
        try:
            with open(_env_path, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        os.environ.setdefault(_k.strip(), _v.strip())
        except OSError as e:
            logger.warning(".env 読み込み失敗: %s", e)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


# ── セキュリティヘッダー ──────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response


# ── シンプルなレート制限（メモリベース） ─────────────────
import time as _time
from collections import defaultdict

_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 20       # リクエスト数
RATE_WINDOW = 60.0    # 秒

def _check_rate_limit(ip: str) -> bool:
    now = _time.monotonic()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        return False
    _rate_store[ip].append(now)
    return True


# ── ライフサイクル（非推奨 on_event → lifespan）────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    from pathlib import Path
    from hazard import _load_flood_mesh

    logger.info("ポリゴンデータのプリロードを開始...")
    data_dir = Path(__file__).parent / "data"
    # DISABLE_FLOOD_POLYGON=1 の場合は洪水ポリゴンを読み込まない（省メモリモード）
    if os.getenv("DISABLE_FLOOD_POLYGON") != "1":
        for path in sorted(data_dir.glob("flood_*.json")):
            mesh = int(path.stem.replace("flood_", ""))
            _load_flood_mesh(mesh)
    else:
        logger.info("省メモリモード: 洪水ポリゴンを無効化（地質推定でフォールバック）")
    _load_landslide_zones()
    _load_inland_flood_zones()
    _load_tsunami_zones()
    logger.info("ポリゴンデータのプリロード完了")
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)


PREF_NAMES = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県",
    "05": "秋田県", "06": "山形県", "07": "福島県", "08": "茨城県",
    "09": "栃木県", "10": "群馬県", "11": "埼玉県", "12": "千葉県",
    "13": "東京都", "14": "神奈川県", "15": "新潟県", "16": "富山県",
    "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県",
    "25": "滋賀県", "26": "京都府", "27": "大阪府", "28": "兵庫県",
    "29": "奈良県", "30": "和歌山県", "31": "鳥取県", "32": "島根県",
    "33": "岡山県", "34": "広島県", "35": "山口県", "36": "徳島県",
    "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県",
    "45": "宮崎県", "46": "鹿児島県", "47": "沖縄県",
}

GSI_GEOCODE        = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GSI_REVERSE        = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
NOMINATIM          = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_UA       = "cocohaza-prototype/1.0"
JSHIS_API          = "https://www.j-shis.bosai.go.jp/map/api/pshm/Y2020/AVR/TTL_MTTL/meshinfo.geojson"
GEOLOGY_API        = "https://gbank.gsj.jp/seamless/v2/api/1.2/legend.json"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# 日本の緯度・経度範囲
LAT_MIN, LAT_MAX = 20.0, 46.0
LNG_MIN, LNG_MAX = 122.0, 154.0


def _clean_google_address(formatted: str) -> str:
    s = re.sub(r"〒\d{3}-\d{4}\s*", "", formatted)
    s = s.replace("日本、", "").replace("日本,", "").strip()
    return s


async def geocode(address: str) -> tuple[float, float, str]:
    if GOOGLE_MAPS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                r = await client.get(GOOGLE_GEOCODE_URL, params={
                    "address": address, "key": GOOGLE_MAPS_API_KEY,
                    "language": "ja", "region": "jp",
                })
            data = r.json()
            if data.get("status") == "OK" and data.get("results"):
                result = data["results"][0]
                lat    = result["geometry"]["location"]["lat"]
                lng    = result["geometry"]["location"]["lng"]
                title  = _clean_google_address(result["formatted_address"])
                return lat, lng, title
        except Exception as e:
            logger.warning("Google Geocoding API 失敗: %s", e)

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        r = await client.get(GSI_GEOCODE, params={"q": address})
    data = r.json()
    if not data:
        raise HTTPException(status_code=404, detail="住所が見つかりませんでした")
    feat  = data[0]
    lng, lat = feat["geometry"]["coordinates"]
    title = feat["properties"].get("title", address)
    return lat, lng, title


async def fetch_jshis(lat: float, lng: float) -> float:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        r = await client.get(JSHIS_API, params={
            "position": f"{lng},{lat}", "epsg": "4326", "attr": "T30_I60_PS"
        })
    try:
        feats = r.json().get("features", [])
        if feats:
            return float(feats[0]["properties"].get("T30_I60_PS", 0)) * 100
    except Exception as e:
        logger.warning("J-SHIS API 失敗: %s", e)
    return 0.0


async def fetch_geology(lat: float, lng: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            r = await client.get(GEOLOGY_API, params={"point": f"{lat},{lng}"})
        data = r.json()
        if isinstance(data, dict):
            return data.get("lithology_ja", "不明")
        if isinstance(data, list) and data:
            return data[0].get("lithology_ja", "不明")
    except Exception as e:
        logger.warning("地質API 失敗: %s", e)
    return "不明"


def _parse_nominatim_address(a: dict) -> tuple[str, str]:
    pref = a.get("state") or a.get("province") or ""
    pref_code = ""
    if not pref:
        iso = a.get("ISO3166-2-lvl4", "")
        if iso.startswith("JP-"):
            pref_code = iso[3:].zfill(2)
            pref = PREF_NAMES.get(pref_code, "")
    else:
        for code, name in PREF_NAMES.items():
            if name == pref:
                pref_code = code
                break

    city   = a.get("city") or a.get("town") or a.get("village") or ""
    ward   = ""
    suburb = a.get("suburb", "")
    if suburb and "区" in suburb:
        ward = suburb

    local = (a.get("neighbourhood") or a.get("quarter") or a.get("hamlet") or "")
    if not local:
        local = a.get("road") or ""

    parts, seen = [], set()
    for p in [pref, city, ward, local]:
        if p and p not in seen and not any(p in r for r in parts):
            parts.append(p)
            seen.add(p)
    return "".join(parts) or "", pref_code


async def reverse_geocode(lat: float, lng: float) -> tuple[str, str]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            r = await client.get(
                NOMINATIM,
                params={"lat": lat, "lon": lng, "format": "json",
                        "accept-language": "ja", "zoom": 18},
                headers={"User-Agent": NOMINATIM_UA},
            )
        addr_dict = r.json().get("address", {})
        label, pref_code = _parse_nominatim_address(addr_dict)
        if label:
            return label, pref_code
    except Exception as e:
        logger.warning("Nominatim 失敗: %s", e)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            r = await client.get(GSI_REVERSE, params={"lat": lat, "lon": lng})
        results = r.json().get("results", {})
        if results:
            muni_cd   = results.get("muniCd", "")
            pref_code = str(muni_cd).zfill(5)[:2]
            pref      = PREF_NAMES.get(pref_code, "")
            town      = results.get("lv01Nm", "")
            label     = f"{pref}{town}".strip()
            return label or f"{lat:.5f}, {lng:.5f}", pref_code
    except Exception as e:
        logger.warning("GSI 逆ジオコーダ 失敗: %s", e)

    return f"{lat:.5f}, {lng:.5f}", ""


async def _fetch_hazards(lat: float, lng: float, pref_code: str = ""):
    # geology不要な処理とgeologyを並列実行（M-2修正）
    geology, jshis_prob, flood_risk = await asyncio.gather(
        fetch_geology(lat, lng),
        fetch_jshis(lat, lng),
        fetch_flood_risk(lat, lng),
    )
    # geologyが必要な処理を並列実行
    slope_result, coastal_result = await asyncio.gather(
        fetch_slope_score(lat, lng),
        fetch_coastal_score(lat, lng, geology),
    )
    typhoon_score            = fetch_typhoon_score(pref_code)
    landslide_in_zone        = fetch_landslide_zone(lat, lng)
    inland_flood_in_zone     = fetch_inland_flood_zone(lat, lng)
    tsunami_in_zone, tsunami_expl = fetch_tsunami_zone(lat, lng)
    slope_score, slope_deg   = slope_result
    coastal_score, elev_m    = coastal_result
    flood_score, flood_expl  = flood_risk
    return (jshis_prob, geology,
            slope_score, slope_deg,
            typhoon_score,
            coastal_score, elev_m,
            flood_score, flood_expl,
            landslide_in_zone, inland_flood_in_zone,
            tsunami_in_zone, tsunami_expl)


def _build_response(title, lat, lng, jshis_prob, geology,
                    slope_score, slope_deg, typhoon_score,
                    coastal_score, elev_m, pref_code="",
                    flood_score=None, flood_explanation="",
                    landslide_in_zone=False, inland_flood_in_zone=False,
                    tsunami_in_zone=False, tsunami_explanation=""):
    pref_name = PREF_NAMES.get(pref_code, "")
    scores = calc_score(
        jshis_prob, geology, slope_score, slope_deg,
        typhoon_score, pref_name, coastal_score, elev_m,
        flood_score=flood_score, flood_explanation=flood_explanation,
        landslide_in_zone=landslide_in_zone, inland_flood_in_zone=inland_flood_in_zone,
        tsunami_in_zone=tsunami_in_zone, tsunami_explanation=tsunami_explanation,
    )
    return {
        "address": title, "lat": lat, "lng": lng,
        "pref_code": pref_code,
        "jshis_prob": jshis_prob, "geology": geology,
        "slope_deg": slope_deg, "elev_m": elev_m,
        "score": scores,
    }


@app.get("/api/score")
async def get_score(
    request: Request,
    address: str = Query(..., min_length=1, max_length=200),
):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(status_code=429, content={"detail": "リクエストが多すぎます。しばらくお待ちください。"})

    lat, lng, title = await geocode(address)

    # reverse_geocode と _fetch_hazards を並列実行（M-1修正）
    (_, pref_code), hazards = await asyncio.gather(
        reverse_geocode(lat, lng),
        _fetch_hazards(lat, lng),
    )
    (jshis_prob, geology,
     slope_score, slope_deg, _,
     coastal_score, elev_m,
     flood_score, flood_expl,
     landslide_zone, inland_flood_zone,
     tsunami_zone, tsunami_expl) = hazards
    typhoon_score = fetch_typhoon_score(pref_code)

    return _build_response(title, lat, lng, jshis_prob, geology,
                           slope_score, slope_deg, typhoon_score,
                           coastal_score, elev_m, pref_code=pref_code,
                           flood_score=flood_score, flood_explanation=flood_expl,
                           landslide_in_zone=landslide_zone, inland_flood_in_zone=inland_flood_zone,
                           tsunami_in_zone=tsunami_zone, tsunami_explanation=tsunami_expl)


@app.get("/api/score/latlng")
async def get_score_by_latlng(
    request: Request,
    lat: float = Query(..., ge=LAT_MIN, le=LAT_MAX),
    lng: float = Query(..., ge=LNG_MIN, le=LNG_MAX),
):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(status_code=429, content={"detail": "リクエストが多すぎます。しばらくお待ちください。"})

    (title, pref_code), hazards = await asyncio.gather(
        reverse_geocode(lat, lng),
        _fetch_hazards(lat, lng),
    )
    (jshis_prob, geology,
     slope_score, slope_deg, _,
     coastal_score, elev_m,
     flood_score, flood_expl,
     landslide_zone, inland_flood_zone,
     tsunami_zone, tsunami_expl) = hazards
    typhoon_score = fetch_typhoon_score(pref_code)

    return _build_response(title, lat, lng, jshis_prob, geology,
                           slope_score, slope_deg, typhoon_score,
                           coastal_score, elev_m, pref_code=pref_code,
                           flood_score=flood_score, flood_explanation=flood_expl,
                           landslide_in_zone=landslide_zone, inland_flood_in_zone=inland_flood_zone,
                           tsunami_in_zone=tsunami_zone, tsunami_explanation=tsunami_expl)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
