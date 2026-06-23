from typing import Optional
from hazard import geology_to_score, GEOLOGY_FLOOD_MAP, GEOLOGY_LIQUEFACTION_MAP

# J-SHIS 全国平均（30年・震度6弱以上）
JSHIS_NATIONAL_AVG = 3.0

# 台風スコア → 頻度説明
TYPHOON_FREQ_DESC = {
    10: "年1〜2回以上の通過",
    9:  "上陸頻度が非常に高い",
    8:  "上陸頻度が高い",
    7:  "上陸・通過が多い",
    6:  "上陸・通過がやや多い",
    5:  "上陸・通過が中程度",
    4:  "上陸・通過が少ない",
    3:  "上陸・通過がまれ",
    2:  "ほぼ上陸・通過しない",
    1:  "ほぼ上陸・通過しない",
}

# 液状化スコア → リスク表現
LIQUEFACTION_RISK_DESC = {
    9: "非常に高い（人工・軟弱地盤）",
    8: "高い（砂質・人工地盤）",
    7: "やや高い（沖積低地）",
    6: "中程度",
    5: "中程度",
    4: "やや低い",
    3: "低い",
    2: "非常に低い（台地・岩盤）",
    1: "非常に低い（台地・岩盤）",
}


def calc_score(
    j_shis_prob: float,
    geology_label: str,
    slope_score: int,
    slope_deg: float,
    typhoon_score: int,
    pref_name: str,
    coastal_score: int,
    elev_m: float,
    flood_score: Optional[int] = None,
    flood_explanation: str = "",
    landslide_in_zone: bool = False,
    inland_flood_in_zone: bool = False,
    tsunami_in_zone: bool = False,
    tsunami_explanation: str = "",
) -> dict:

    # ── 地震（J-SHIS 30年確率 → 段階スコア）──────────────
    p = j_shis_prob
    if   p >= 40: eq_score = 10
    elif p >= 30: eq_score = 9
    elif p >= 20: eq_score = 8
    elif p >= 15: eq_score = 7
    elif p >= 10: eq_score = 5
    elif p >= 6:  eq_score = 3
    elif p >= 3:  eq_score = 2
    else:         eq_score = 1

    ratio = p / JSHIS_NATIONAL_AVG if JSHIS_NATIONAL_AVG > 0 else 0
    if p >= 6:
        eq_expl = f"30年確率 {p:.1f}%（全国平均 {JSHIS_NATIONAL_AVG:.0f}% の約 {ratio:.0f} 倍）"
    elif p >= 1:
        eq_expl = f"30年確率 {p:.1f}%（全国平均 {JSHIS_NATIONAL_AVG:.0f}% と同程度）"
    else:
        eq_expl = f"30年確率 {p:.1f}%（全国平均 {JSHIS_NATIONAL_AVG:.0f}% 未満）"

    # ── 水害（ポリゴン優先、なければ地質推定）──────────────
    if flood_score is None or flood_score == 0:
        flood_score = geology_to_score(geology_label, GEOLOGY_FLOOD_MAP, default=5)
        flood_explanation = f"地質推定: {geology_label}（データ未整備エリア）"

    # 内水浸水情報を追加
    inland_info = "内水浸水想定区域内" if inland_flood_in_zone else "内水浸水想定区域外"
    if "国土数値情報A31" in flood_explanation:
        flood_explanation += f" / {inland_info}"
    else:
        flood_explanation += f" / {inland_info}"

    # ── 液状化（地質 → スコア + 説明）────────────────────
    liq_score = geology_to_score(geology_label, GEOLOGY_LIQUEFACTION_MAP, default=4)
    liq_risk  = LIQUEFACTION_RISK_DESC.get(liq_score, "不明")
    liq_expl  = f"地質: {geology_label}（液状化リスク: {liq_risk}）"

    # ── 土砂（傾斜角 + ポリゴン判定）───────────────────────
    sediment_score = slope_score
    if slope_deg >= 30:
        sed_level = "危険（急斜面）"
    elif slope_deg >= 15:
        sed_level = "警戒（中急斜面）"
    elif slope_deg >= 8:
        sed_level = "注意（中程度の斜面）"
    elif slope_deg >= 3:
        sed_level = "低い（緩斜面）"
    else:
        sed_level = "非常に低い（ほぼ平地）"

    zone_info = "土砂災害警戒区域内" if landslide_in_zone else "土砂災害警戒区域外"
    if slope_deg > 0:
        sed_expl = f"周囲200m内の最大傾斜 {slope_deg:.1f}°（{sed_level}）/ {zone_info}"
    else:
        sed_expl = f"傾斜データなし（{sed_level}）/ {zone_info}"

    # ── 台風（都道府県 + 頻度説明）──────────────────────
    freq_desc = TYPHOON_FREQ_DESC.get(typhoon_score, "不明")
    if pref_name:
        typhoon_expl = f"{pref_name}: {freq_desc}（気象庁 過去70年統計）"
    else:
        typhoon_expl = f"{freq_desc}（気象庁 過去70年統計）"

    # ── 津波・高潮（ポリゴン優先、ポリゴンなしは標高+地質）────
    if tsunami_explanation:
        coastal_expl = tsunami_explanation
    else:
        if elev_m is not None:
            elev_label = f"{elev_m:.1f}m" if elev_m >= 0 else f"{elev_m:.1f}m（海面下）"
            coastal_expl = f"標高 {elev_label}・地質: {geology_label}"
        else:
            coastal_expl = f"地質: {geology_label}（標高データなし）"

    # ── 総合スコア（重み付き平均）───────────────────────
    total = (
        eq_score       * 0.25 +
        liq_score      * 0.10 +
        flood_score    * 0.25 +
        sediment_score * 0.10 +
        typhoon_score  * 0.15 +
        coastal_score  * 0.15
    )
    total = max(1, min(10, round(total)))

    return {
        "total":        total,
        "earthquake":   eq_score,
        "liquefaction": liq_score,
        "flood":        flood_score,
        "sediment":     sediment_score,
        "typhoon":      typhoon_score,
        "coastal":      coastal_score,
        "explanation": {
            "earthquake":   eq_expl,
            "liquefaction": liq_expl,
            "flood":        flood_explanation,
            "sediment":     sed_expl,
            "typhoon":      typhoon_expl,
            "coastal":      coastal_expl,
        }
    }
