#!/bin/bash
set -e

# flood_5339.json が存在しない場合のみ Cloudflare R2 からダウンロード
FLOOD_FILE="data/flood_5339.json"

if [ ! -f "$FLOOD_FILE" ]; then
  echo "[start] flood_5339.json が見つかりません。R2 からダウンロードします..."
  if [ -z "$FLOOD_DATA_URL" ]; then
    echo "[start] FLOOD_DATA_URL が未設定です。洪水ポリゴンなしで起動します。"
  else
    mkdir -p data
    curl -fSL "$FLOOD_DATA_URL" -o "$FLOOD_FILE"
    echo "[start] ダウンロード完了: $(du -sh $FLOOD_FILE | cut -f1)"
  fi
else
  echo "[start] flood_5339.json 確認済み: $(du -sh $FLOOD_FILE | cut -f1)"
fi

# FastAPI 起動
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
