import asyncio
import json
import math
import time
from pathlib import Path
from typing import Optional
import httpx
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

GSI_ELEVATION = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"

# ── ポリゴンデータ（起動時1回ロード）──────────────────────
_FLOOD_INDEXES: dict[int, tuple[STRtree, list]] = {}      # mesh → (tree, data)
_LANDSLIDE_TREE: Optional[STRtree] = None
_LANDSLIDE_DATA: list = []
_INLAND_FLOOD_TREE: Optional[STRtree] = None
_INLAND_FLOOD_DATA: list = []
_TSUNAMI_TREE: Optional[STRtree] = None
_TSUNAMI_DATA: list = []

def _load_flood_mesh(mesh: int) -> tuple[Optional[STRtree], list]:
    """data/flood_{mesh}.json を読み込んでSTRtreeを返す（キャッシュ済みなら即返す）"""
    if mesh in _FLOOD_INDEXES:
        return _FLOOD_INDEXES[mesh]

    path = Path(__file__).parent / "data" / f"flood_{mesh}.json"
    if not path.exists():
        return None, []

    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    geoms = [shape(d["geometry"]) for d in data]
    tree  = STRtree(geoms)
    _FLOOD_INDEXES[mesh] = (tree, data)
    print(f"[flood] メッシュ{mesh} ロード完了: {len(data)}ポリゴン ({time.time()-t0:.1f}秒)")
    return tree, data


def _load_landslide_zones() -> None:
    """土砂災害警戒区域（A33）をロード"""
    global _LANDSLIDE_TREE, _LANDSLIDE_DATA
    if _LANDSLIDE_TREE is not None:
        return
    path = Path(__file__).parent / "data" / "landslide_5339.json"
    if not path.exists():
        return
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        _LANDSLIDE_DATA = json.load(f)
    _LANDSLIDE_TREE = STRtree([shape(d["geometry"]) for d in _LANDSLIDE_DATA])
    print(f"[landslide] ロード完了: {len(_LANDSLIDE_DATA)}ポリゴン ({time.time()-t0:.1f}秒)")


def _load_inland_flood_zones() -> None:
    """内水浸水想定区域（A51）をロード"""
    global _INLAND_FLOOD_TREE, _INLAND_FLOOD_DATA
    if _INLAND_FLOOD_TREE is not None:
        return
    path = Path(__file__).parent / "data" / "inland_flood_5339.json"
    if not path.exists():
        return
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        _INLAND_FLOOD_DATA = json.load(f)
    _INLAND_FLOOD_TREE = STRtree([shape(d["geometry"]) for d in _INLAND_FLOOD_DATA])
    print(f"[inland_flood] ロード完了: {len(_INLAND_FLOOD_DATA)}ポリゴン ({time.time()-t0:.1f}秒)")


def _load_tsunami_zones() -> None:
    """津波浸水想定区域（A40）をロード"""
    global _TSUNAMI_TREE, _TSUNAMI_DATA
    if _TSUNAMI_TREE is not None:
        return
    path = Path(__file__).parent / "data" / "tsunami_5339.json"
    if not path.exists():
        return
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        _TSUNAMI_DATA = json.load(f)
    _TSUNAMI_TREE = STRtree([shape(d["geometry"]) for d in _TSUNAMI_DATA])
    print(f"[tsunami] ロード完了: {len(_TSUNAMI_DATA)}ポリゴン ({time.time()-t0:.1f}秒)")


def _get_mesh_code(lat: float, lng: float) -> int:
    return int(lat * 1.5) * 100 + int(lng - 100)

# 産総研地質区分 → 洪水リスクスコア（1〜10）
# 沖積低地・埋立地など水害に弱い地形ほど高スコア
GEOLOGY_FLOOD_MAP = {
    "後背湿地":   10,
    "旧河道":     10,
    "三角州":      9,
    "海岸低地":    9,
    "干拓地":      9,
    "埋立地":      8,
    "盛り土":      8,
    "砂州":        7,
    "自然堤防":    6,
    "沖積":        6,
    "谷底平野":    5,
    "扇状地":      4,
    "段丘":        2,
    "台地":        2,
    "丘陵":        2,
    "山地":        1,
    "岩盤":        1,
    "岩石":        1,
}

# 産総研地質区分 → 液状化リスクスコア
GEOLOGY_LIQUEFACTION_MAP = {
    "埋立地":      9,
    "干拓地":      9,
    "盛り土":      8,
    "砂州":        8,
    "後背湿地":    7,
    "三角州":      7,
    "旧河道":      7,
    "沖積":        6,
    "自然堤防":    5,
    "谷底平野":    4,
    "扇状地":      3,
    "段丘":        2,
    "台地":        2,
    "丘陵":        1,
    "山地":        1,
    "岩盤":        1,
}


def geology_to_score(geology_label: str, mapping: dict, default: int = 5) -> int:
    """地質ラベルと辞書を照合してスコアを返す（部分一致）"""
    for key, score in mapping.items():
        if key in geology_label:
            return score
    return default


async def _get_elevation(lat: float, lng: float, client: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await client.get(GSI_ELEVATION,
                             params={"lon": lng, "lat": lat, "outtype": "JSON"},
                             timeout=8)
        data = r.json()
        elev = data.get("elevation")
        if elev is not None and elev != "-----":
            return float(elev)
    except Exception:
        pass
    return None


# 都道府県コード（2桁）→ 台風リスクスコア
# 気象庁「台風の統計資料」過去70年の接近・上陸頻度をもとに作成
TYPHOON_RISK_BY_PREF = {
    "01": 2,  # 北海道
    "02": 3,  # 青森
    "03": 3,  # 岩手
    "04": 3,  # 宮城
    "05": 3,  # 秋田
    "06": 3,  # 山形
    "07": 3,  # 福島
    "08": 4,  # 茨城
    "09": 4,  # 栃木
    "10": 4,  # 群馬
    "11": 4,  # 埼玉
    "12": 5,  # 千葉
    "13": 4,  # 東京
    "14": 5,  # 神奈川
    "15": 4,  # 新潟
    "16": 4,  # 富山
    "17": 4,  # 石川
    "18": 4,  # 福井
    "19": 4,  # 山梨
    "20": 4,  # 長野
    "21": 5,  # 岐阜
    "22": 6,  # 静岡
    "23": 5,  # 愛知
    "24": 6,  # 三重
    "25": 5,  # 滋賀
    "26": 4,  # 京都
    "27": 5,  # 大阪
    "28": 5,  # 兵庫
    "29": 5,  # 奈良
    "30": 7,  # 和歌山
    "31": 4,  # 鳥取
    "32": 4,  # 島根
    "33": 5,  # 岡山
    "34": 5,  # 広島
    "35": 6,  # 山口
    "36": 6,  # 徳島
    "37": 6,  # 香川
    "38": 7,  # 愛媛
    "39": 7,  # 高知
    "40": 7,  # 福岡
    "41": 7,  # 佐賀
    "42": 7,  # 長崎
    "43": 7,  # 熊本
    "44": 7,  # 大分
    "45": 8,  # 宮崎
    "46": 9,  # 鹿児島
    "47": 10, # 沖縄
}


async def fetch_flood_risk(lat: float, lng: float) -> tuple[int, str]:
    """
    ローカルキャッシュの洪水浸水想定区域GeoJSONでポイントインポリゴン判定。
    data/flood_{mesh}.json が存在する場合はそれを使用、なければ (0,"") を返す
    → score.py 側で地質情報へフォールバック。

    データ準備: python3 prepare_flood.py [メッシュコード]
    東京 (5339): python3 prepare_flood.py 5339
    """
    mesh = _get_mesh_code(lat, lng)
    tree, data = _load_flood_mesh(mesh)
    if tree is None:
        return 0, ""  # データなし → 地質でフォールバック

    pt   = Point(lng, lat)
    hits = tree.query(pt, predicate="intersects")
    if not len(hits):
        return 1, "国土数値情報A31: 浸水想定区域外"

    idx = hits[0]
    if idx >= len(data):
        return 1, "国土数値情報A31: 浸水想定区域外"
    d = data[idx]
    return d["score"], f"国土数値情報A31: 浸水深{d['depth']}の想定浸水区域"


def fetch_typhoon_score(pref_or_muni_cd: str) -> int:
    """都道府県コード（2桁）または市区町村コード（5桁）から台風リスクスコアを返す"""
    code = str(pref_or_muni_cd).strip()
    pref_code = code.zfill(2) if len(code) <= 2 else code.zfill(5)[:2]
    return TYPHOON_RISK_BY_PREF.get(pref_code, 5)


# 産総研地質区分 → 津波・高潮リスク補正係数
# 海岸低地・三角州など海に近い低地ほど高スコア
GEOLOGY_COASTAL_MAP = {
    "海岸低地":   10,
    "三角州":      9,
    "干拓地":      9,
    "砂州":        8,
    "埋立地":      8,
    "後背湿地":    7,
    "旧河道":      5,
    "沖積":        5,
    "自然堤防":    4,
    "扇状地":      2,
    "段丘":        1,
    "台地":        1,
    "丘陵":        1,
    "山地":        1,
    "岩盤":        1,
}


async def fetch_coastal_score(lat: float, lng: float, geology_label: str) -> tuple[int, float]:
    """
    標高 + 地質区分で津波・高潮リスクスコアを算出。
    返値: (スコア, 標高m)
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        elev = await _get_elevation(lat, lng, client)

    if elev is None:
        return 3, None  # None = 取得失敗

    # 標高ベースのベーススコア
    if elev <= 2:
        base = 9
    elif elev <= 5:
        base = 7
    elif elev <= 10:
        base = 5
    elif elev <= 20:
        base = 3
    else:
        base = 1

    # 地質補正：山地性地質なら海岸リスクを下げる（内陸の低地と区別）
    geo_score = geology_to_score(geology_label, GEOLOGY_COASTAL_MAP, default=5)
    inland_keywords = ("山地", "丘陵", "岩盤", "岩石", "段丘", "台地", "谷底平野")
    if any(k in geology_label for k in inland_keywords):
        base = max(1, base - 3)

    score = max(1, min(10, round((base + geo_score) / 2)))
    return score, elev


def fetch_landslide_zone(lat: float, lng: float) -> bool:
    """土砂災害警戒区域（A33）内にあるかを判定。返値: True=区域内, False=区域外"""
    _load_landslide_zones()
    if _LANDSLIDE_TREE is None:
        return False
    pt = Point(lng, lat)
    hits = _LANDSLIDE_TREE.query(pt, predicate="intersects")
    return len(hits) > 0


def fetch_inland_flood_zone(lat: float, lng: float) -> bool:
    """内水浸水想定区域（A51）内にあるかを判定。返値: True=区域内, False=区域外"""
    _load_inland_flood_zones()
    if _INLAND_FLOOD_TREE is None:
        return False
    pt = Point(lng, lat)
    hits = _INLAND_FLOOD_TREE.query(pt, predicate="intersects")
    return len(hits) > 0


def fetch_tsunami_zone(lat: float, lng: float) -> tuple[bool, str]:
    """津波浸水想定区域（A40）内にあるかを判定。返値: (区域内フラグ, 浸水深説明)"""
    _load_tsunami_zones()
    if _TSUNAMI_TREE is None:
        return False, ""
    pt = Point(lng, lat)
    hits = _TSUNAMI_TREE.query(pt, predicate="intersects")
    if not len(hits):
        return False, "津波浸水想定区域外"

    # 浸水深ランク（A40_001）を取得
    idx = hits[0]
    if idx >= len(_TSUNAMI_DATA):
        return False, "津波浸水想定区域外"
    depth_rank = _TSUNAMI_DATA[idx].get("depth_rank", 0)
    depth_map = {
        1: "0.0～1.0m",
        2: "1.0～3.0m",
        3: "3.0～5.0m",
        4: "5.0～10.0m",
        5: "10.0～20.0m",
        6: "20.0m超",
    }
    depth_str = depth_map.get(depth_rank, "不明")
    return True, f"国土数値情報A40: 津波浸水深{depth_str}の想定浸水区域"


async def fetch_slope_score(lat: float, lng: float) -> tuple[int, float]:
    """
    周囲4点（約200m間隔）の標高差から最大傾斜を計算し、土砂災害リスクスコアを返す。
    返値: (スコア, 最大傾斜角°)
    傾斜 >30°: 9, 15-30°: 7, 8-15°: 5, 3-8°: 3, <3°: 1
    """
    d_lat = 0.0018   # 約200m
    d_lng = 0.0022   # 約200m（東京緯度基準）

    neighbors = [
        (lat + d_lat, lng),
        (lat - d_lat, lng),
        (lat, lng + d_lng),
        (lat, lng - d_lng),
    ]

    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        results = await asyncio.gather(
            _get_elevation(lat, lng, client),
            *[_get_elevation(n_lat, n_lng, client) for n_lat, n_lng in neighbors],
        )

    center_elev = results[0]
    if center_elev is None:
        return 3, 0.0

    neighbor_elevs = [e for e in results[1:] if e is not None]
    if not neighbor_elevs:
        return 3, 0.0

    max_diff = max(abs(center_elev - e) for e in neighbor_elevs)
    distance = 200  # meters
    slope_deg = math.degrees(math.atan(max_diff / distance))

    if slope_deg >= 30:
        return 9, slope_deg
    elif slope_deg >= 15:
        return 7, slope_deg
    elif slope_deg >= 8:
        return 5, slope_deg
    elif slope_deg >= 3:
        return 3, slope_deg
    else:
        return 1, slope_deg
