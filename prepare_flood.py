"""
洪水浸水想定区域データの事前ダウンロード・変換スクリプト
国土数値情報A31（想定最大規模）を取得してローカルにキャッシュする。

使い方:
    python3 prepare_flood.py [メッシュコード]
    例) python3 prepare_flood.py 5339    # 東京
        python3 prepare_flood.py 5235    # 大阪
        python3 prepare_flood.py 5240    # 名古屋

メッシュコードの計算:
    mesh = int(lat * 1.5) * 100 + int(lng - 100)
"""
import sys
import zipfile
import json
import math
import requests
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

DEPTH_RANK_TO_SCORE = {1: 2, 2: 4, 3: 6, 4: 8, 5: 9, 6: 10}
DEPTH_RANK_TO_TEXT  = {
    1: "0～0.5m", 2: "0.5～3m", 3: "3～5m",
    4: "5～10m",  5: "10～20m", 6: "20m以上",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A31-v4_0.html",
}


def mesh_code(lat: float, lng: float) -> int:
    return int(lat * 1.5) * 100 + int(lng - 100)


def download_and_extract(mesh: int) -> list[dict]:
    """ZIPをダウンロードして想定最大規模GeoJSONを展開、ポリゴンリストを返す"""
    features = []

    for kubun in ["10", "20"]:
        url = f"https://nlftp.mlit.go.jp/ksj/gml/data/A31/A31-22/A31-22_{kubun}_{mesh}_GEOJSON.zip"
        out_path = DATA_DIR / f"A31_{kubun}_{mesh}.zip"

        if not out_path.exists():
            print(f"  ダウンロード中: {url}")
            r = requests.get(url, headers=HEADERS, timeout=300, stream=True)
            if r.status_code != 200:
                print(f"  スキップ（{r.status_code}）")
                continue
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r  {downloaded//1024//1024}MB / {total//1024//1024}MB ({pct:.0f}%)", end="", flush=True)
            print()
        else:
            print(f"  キャッシュ済み: {out_path.name}")

        print(f"  展開中: {out_path.name} ...")
        with zipfile.ZipFile(out_path) as z:
            for name in z.namelist():
                if "20_想定最大規模" in name and name.endswith(".geojson"):
                    data = json.loads(z.open(name).read().decode("utf-8"))
                    raw = data.get("features", [])
                    print(f"    {name.split('/')[-1]}: {len(raw)} ポリゴン")
                    features.extend(raw)

    return features


def simplify_features(features: list[dict]) -> list[dict]:
    """必要なフィールドだけ残してサイズを削減"""
    from shapely.geometry import shape
    result = []
    for feat in features:
        rank = feat.get("properties", {}).get("A31_201")
        if rank is None:
            continue
        try:
            geom = shape(feat["geometry"])
            # 精度を落として軽量化（0.0001度 ≒ 11m）
            geom_simple = geom.simplify(0.0001, preserve_topology=True)
            result.append({
                "rank": rank,
                "score": DEPTH_RANK_TO_SCORE.get(rank, 5),
                "depth": DEPTH_RANK_TO_TEXT.get(rank, "不明"),
                "geometry": geom_simple.__geo_interface__,
            })
        except Exception:
            continue
    return result


def main():
    meshes = [int(m) for m in sys.argv[1:]] if len(sys.argv) > 1 else [5339]

    for mesh in meshes:
        out_file = DATA_DIR / f"flood_{mesh}.json"
        if out_file.exists():
            print(f"✅ {out_file} は既存です。削除して再実行すると更新されます。")
            continue

        print(f"\n=== メッシュ {mesh} の洪水データを処理中 ===")
        features = download_and_extract(mesh)
        if not features:
            print(f"  データが取得できませんでした。")
            continue

        print(f"  簡略化中（{len(features)} ポリゴン）...")
        simplified = simplify_features(features)

        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(simplified, f, ensure_ascii=False, separators=(",", ":"))

        size_kb = out_file.stat().st_size // 1024
        print(f"✅ 保存完了: {out_file} ({size_kb} KB, {len(simplified)} ポリゴン)")

    print("\n完了。サーバーを再起動してください。")


if __name__ == "__main__":
    main()
