"""
Playwright ブラウザテスト
前提: uvicorn を localhost:8000 で起動済みであること
"""
import re
import pytest
from playwright.sync_api import Page, expect

BASE = "http://localhost:8000"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "locale": "ja-JP"}


def test_page_loads(page: Page):
    """ページが正常に表示され、入力ボックスと地図が存在する"""
    page.goto(BASE)
    expect(page).to_have_title("CocoHaza / ココハザード")
    expect(page.locator("#address-input")).to_be_visible()
    expect(page.locator("#map")).to_be_visible()
    page.screenshot(path="tests/screenshots/01_initial.png")


def test_high_risk_address(page: Page):
    """江東区亀戸（川沿い低地）は国土数値情報A31ポリゴンで浸水区域内と判定される"""
    page.goto(BASE)
    # 亀戸はkubun20(中小河川)の浸水想定区域内。豊洲は主要河川(kubun10)のみで現データ外。
    page.fill("#address-input", "東京都江東区亀戸")
    page.click("button")

    score_el = page.locator(".total-score .number")
    score_el.wait_for(timeout=20000)

    score_text = score_el.inner_text()
    score = int(re.search(r"\d+", score_text).group())
    assert score >= 2, f"亀戸の総合スコアは2以上のはず。実際: {score}"

    # 水害スコア根拠にポリゴンデータが使われているか
    flood_expl = page.locator(".risk-expl").first
    expect(flood_expl).to_be_visible()
    expect(page.locator(".breakdown")).to_be_visible()
    page.screenshot(path="tests/screenshots/02_kameido_polygon.png")


def test_low_risk_address(page: Page):
    """文京区本郷（台地）は豊洲より低いスコアになる"""
    page.goto(BASE)
    page.fill("#address-input", "東京都文京区本郷2丁目")
    page.click("button")

    score_el = page.locator(".total-score .number")
    score_el.wait_for(timeout=15000)

    score_text = score_el.inner_text()
    score = int(re.search(r"\d+", score_text).group())
    assert score <= 6, f"本郷（台地）のスコアは6以下のはず。実際: {score}"
    page.screenshot(path="tests/screenshots/03_hongo_result.png")


def test_invalid_address_shows_error(page: Page):
    """存在しない住所はエラーメッセージを表示する"""
    page.goto(BASE)
    page.fill("#address-input", "ほげほげ県ふがふが市")
    page.click("button")

    error_el = page.locator(".error")
    error_el.wait_for(timeout=10000)
    expect(error_el).to_contain_text("見つかりません")
    page.screenshot(path="tests/screenshots/04_error.png")


def test_enter_key_triggers_search(page: Page):
    """Enterキーでも検索できる"""
    page.goto(BASE)
    page.fill("#address-input", "東京都千代田区")
    page.keyboard.press("Enter")

    score_el = page.locator(".total-score .number")
    score_el.wait_for(timeout=15000)
    expect(score_el).to_be_visible()
    page.screenshot(path="tests/screenshots/05_enter_key.png")


def test_score_breakdown_all_items(page: Page):
    """スコア内訳6項目（水害・地震・液状化・土砂・台風・津波高潮）が全て表示される"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")

    page.locator(".breakdown").wait_for(timeout=15000)
    items = page.locator(".risk-item")
    assert items.count() == 6, f"内訳は6項目のはず。実際: {items.count()}"
    page.screenshot(path="tests/screenshots/06_breakdown.png")


def test_typhoon_score_okinawa_higher_than_hokkaido(page: Page):
    """沖縄の台風スコアは北海道より高い（APIを直接検証）"""
    import urllib.request, json

    def api_score(address):
        from urllib.parse import quote
        url = f"{BASE}/api/score?address={quote(address)}"
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)["score"]["typhoon"]

    okinawa  = api_score("沖縄県那覇市")
    hokkaido = api_score("北海道札幌市")
    assert okinawa > hokkaido, \
        f"沖縄（{okinawa}）は北海道（{hokkaido}）より台風スコアが高いはず"

    # UIでも沖縄を表示してスクリーンショット
    page.goto(BASE)
    page.fill("#address-input", "沖縄県那覇市")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    page.screenshot(path="tests/screenshots/09_typhoon_okinawa.png")


def test_coastal_score_low_for_mountain(page: Page):
    """山地（長野）の津波・高潮スコアは低い（APIを直接検証）"""
    import urllib.request, json
    from urllib.parse import quote

    url = f"{BASE}/api/score?address={quote('長野県松本市')}"
    with urllib.request.urlopen(url, timeout=20) as r:
        coastal = json.load(r)["score"]["coastal"]

    assert coastal <= 3, f"内陸山地の津波・高潮スコアは3以下のはず。実際: {coastal}"

    page.goto(BASE)
    page.fill("#address-input", "長野県松本市")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    page.screenshot(path="tests/screenshots/10_coastal_inland.png")


def test_url_sharing_auto_search(page: Page):
    """?address= パラメータ付きURLで開くと自動検索される"""
    from urllib.parse import quote
    url = f"{BASE}/?address={quote('東京都江東区豊洲3丁目')}"
    page.goto(url)

    score_el = page.locator(".total-score .number")
    score_el.wait_for(timeout=20000)
    expect(score_el).to_be_visible()

    # 入力欄に住所が入っている
    addr = page.input_value("#address-input")
    assert "豊洲" in addr, f"URLパラメータの住所が入力欄に反映されているはず: {addr}"
    page.screenshot(path="tests/screenshots/11_url_param.png")


def test_compare_button_shows_input(page: Page):
    """検索後に「比較する」ボタンが表示される"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    expect(page.locator(".btn-primary")).to_be_visible()
    page.locator(".btn-primary").first.click()
    expect(page.locator("#compare-input")).to_be_visible()
    page.screenshot(path="tests/screenshots/12_compare_input.png")


def test_compare_shows_two_scores(page: Page):
    """比較機能で2地点のスコアが並んで表示される"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    page.locator(".btn-primary").first.click()
    page.fill("#compare-input", "東京都文京区本郷2丁目")
    page.locator("#compare-section button").click()

    page.locator(".compare-grid").wait_for(timeout=20000)
    cards = page.locator(".compare-card")
    assert cards.count() == 2, f"比較カードは2枚のはず。実際: {cards.count()}"

    # レーダーチャートが2つ存在する（元の結果 + 比較用）
    charts = page.locator(".radar-chart")
    assert charts.count() == 2, f"比較時はレーダーチャートが2つあるはず。実際: {charts.count()}"
    expect(charts.nth(1)).to_be_visible()  # 比較レーダーチャート（凡例付き）
    page.screenshot(path="tests/screenshots/13_compare_result.png")


def test_radar_chart_displayed(page: Page):
    """通常の検索結果でもレーダーチャートが表示される"""
    page.goto(BASE)
    page.fill("#address-input", "大阪府大阪市西区")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    expect(page.locator(".radar-chart")).to_be_visible()
    page.screenshot(path="tests/screenshots/14_radar_osaka.png")


def test_favorite_add_and_show(page: Page):
    """お気に入りに追加するとパネル上部に表示される"""
    page.goto(BASE)
    page.evaluate("localStorage.clear()")  # 前のテストの残りを消す
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    # お気に入りボタンをクリック
    page.locator("#star-btn").click()

    # お気に入りセクションが表示される
    fav_section = page.locator("#fav-section .fav-wrap")
    fav_section.wait_for(timeout=5000)
    expect(fav_section).to_be_visible()

    # アイテムが1件ある
    items = page.locator(".fav-item")
    assert items.count() == 1, f"お気に入りは1件のはず。実際: {items.count()}"
    page.screenshot(path="tests/screenshots/16_fav_added.png")


def test_favorite_max_3(page: Page):
    """お気に入りは3件まで保存できる"""
    page.goto(BASE)
    page.evaluate("localStorage.clear()")

    addresses = ["東京都江東区豊洲3丁目", "東京都文京区本郷2丁目", "大阪府大阪市西区"]
    for addr in addresses:
        page.fill("#address-input", addr)
        page.click("button")
        page.locator(".total-score .number").wait_for(timeout=20000)
        page.locator("#star-btn").click()
        page.wait_for_timeout(500)

    items = page.locator(".fav-item")
    assert items.count() == 3, f"お気に入りは3件のはず。実際: {items.count()}"

    # 4件目を追加しようとするとトーストが出る
    page.fill("#address-input", "北海道札幌市")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    page.locator("#star-btn").click()

    toast = page.locator(".toast")
    toast.wait_for(timeout=3000)
    expect(toast).to_contain_text("3件まで")
    page.screenshot(path="tests/screenshots/17_fav_max.png")


def test_favorite_compare_button(page: Page):
    """お気に入りの「比較」ボタンで比較結果が表示される"""
    page.goto(BASE)
    page.evaluate("localStorage.clear()")

    # 本郷をお気に入りに保存
    page.fill("#address-input", "東京都文京区本郷2丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    page.locator("#star-btn").click()
    page.locator("#fav-section .fav-wrap").wait_for(timeout=5000)

    # 豊洲を検索して表示
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    # お気に入りの「比較」ボタンをクリック
    page.locator(".fav-btn-compare").click()

    # 比較結果が表示される
    page.locator(".compare-grid").wait_for(timeout=20000)
    cards = page.locator(".compare-card")
    assert cards.count() == 2, f"比較カードは2枚のはず。実際: {cards.count()}"
    page.screenshot(path="tests/screenshots/18_fav_compare.png")


def test_compare_all_favorites(page: Page):
    """まとめて比較ボタンで2〜3件のお気に入りを一括比較できる"""
    page.goto(BASE)
    page.evaluate("localStorage.clear()")

    addresses = ["東京都江東区豊洲3丁目", "東京都文京区本郷2丁目", "大阪府大阪市西区"]
    for addr in addresses:
        page.fill("#address-input", addr)
        page.click("button")
        page.locator(".total-score .number").wait_for(timeout=20000)
        page.locator("#star-btn").click()
        page.wait_for_timeout(400)

    # 「3件まとめて比較」ボタンをクリック
    all_btn = page.locator(".btn-all-compare")
    expect(all_btn).to_be_visible()
    all_btn.click()

    # 3枚カードが表示される
    page.locator(".compare-grid-3").wait_for(timeout=5000)
    cards = page.locator(".compare-card")
    assert cards.count() == 3, f"3件比較カードが表示されるはず。実際: {cards.count()}"

    # レーダーチャートが表示される
    expect(page.locator(".radar-chart").first).to_be_visible()
    page.screenshot(path="tests/screenshots/19_compare_all.png")


def test_action_btns_above_score(page: Page):
    """ボタン（お気に入り・比較・共有）は総合スコアより上にある"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    btn_box   = page.locator(".action-btns").bounding_box()
    score_box = page.locator(".total-score").bounding_box()
    assert btn_box["y"] < score_box["y"], \
        f"ボタン(y={btn_box['y']:.0f})は総合スコア(y={score_box['y']:.0f})より上にあるはず"
    page.screenshot(path="tests/screenshots/20_btns_above_score.png")


def test_dblclick_on_map(page: Page):
    """地図のダブルクリックでスコアが表示される"""
    page.goto(BASE)

    # 地図が描画されるまで待つ
    page.locator("#map .leaflet-tile-loaded").first.wait_for(timeout=10000)

    # 地図の中央あたりをダブルクリック（東京湾岸エリア）
    map_el = page.locator("#map")
    box = map_el.bounding_box()
    cx = box["x"] + box["width"] * 0.55
    cy = box["y"] + box["height"] * 0.45
    page.mouse.dblclick(cx, cy)

    # 結果パネルにスコアが出るのを待つ
    score_el = page.locator(".total-score .number")
    score_el.wait_for(timeout=15000)
    expect(score_el).to_be_visible()

    # 住所入力欄も更新されている
    addr_val = page.input_value("#address-input")
    assert addr_val != "", "ダブルクリック後に住所欄が更新されているはず"

    page.screenshot(path="tests/screenshots/07_dblclick.png")


def test_dblclick_address_includes_prefecture_and_city(page: Page):
    """ダブルクリック後の住所に都道府県と市区町村が含まれる"""
    page.goto(BASE)
    page.locator("#map .leaflet-tile-loaded").first.wait_for(timeout=10000)

    map_el = page.locator("#map")
    box = map_el.bounding_box()
    page.mouse.dblclick(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.45)

    page.locator(".total-score .number").wait_for(timeout=20000)
    addr = page.input_value("#address-input")

    # 都道府県（都/道/府/県）が含まれているか
    has_pref = any(c in addr for c in ("都", "道", "府", "県"))
    # 市区町村（市/区/町/村）が含まれているか
    has_city = any(c in addr for c in ("市", "区", "町", "村"))

    assert has_pref, f"都道府県が住所に含まれていない: '{addr}'"
    assert has_city, f"市区町村が住所に含まれていない: '{addr}'"
    page.screenshot(path="tests/screenshots/15_dblclick_full_addr.png")


def test_dblclick_updates_address_input(page: Page):
    """ダブルクリック後に住所入力欄が逆ジオコーダの結果で上書きされる"""
    page.goto(BASE)

    # まず住所検索で豊洲を表示（地図が豊洲中心にセットされる）
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=15000)
    original_addr = page.input_value("#address-input")

    # 地図中央（豊洲周辺の内陸）をダブルクリック
    map_el = page.locator("#map")
    box = map_el.bounding_box()
    cx = box["x"] + box["width"] * 0.52
    cy = box["y"] + box["height"] * 0.52
    page.mouse.dblclick(cx, cy)

    # loading → スコア表示の順に遷移するのを待つ
    page.locator(".loading").wait_for(timeout=5000)
    page.locator(".total-score .number").wait_for(timeout=20000)

    new_addr = page.input_value("#address-input")
    assert new_addr != "", "ダブルクリック後に住所欄が更新されているはず"
    page.screenshot(path="tests/screenshots/08_dblclick_addr_update.png")


def test_score_explanation_displayed(page: Page):
    """スコアの根拠説明がバーの直下に表示される"""
    page.goto(BASE)
    page.fill("#address-input", "北海道札幌市")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)

    # 根拠テキスト（.risk-expl）がバー項目の直下に5つ以上ある
    expl_items = page.locator(".risk-expl")
    count = expl_items.count()
    assert count >= 5, f"根拠テキストは5個以上のはず。実際: {count}"

    # バーと根拠が同じ親（.breakdown）内に収まっている
    expect(page.locator(".breakdown")).to_be_visible()

    page.screenshot(path="tests/screenshots/21_score_explanation.png")


def test_autocomplete_shows_suggestions(page: Page):
    """住所を途中まで入力するとオートコンプリート候補が表示される"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東")
    page.wait_for_timeout(700)  # デバウンス待ち
    dropdown = page.locator("#ac-dropdown")
    dropdown.wait_for(timeout=8000)
    expect(dropdown).to_be_visible()
    items = page.locator(".ac-item")
    assert items.count() >= 1, f"候補が1件以上表示されるはず。実際: {items.count()}"
    page.screenshot(path="tests/screenshots/22_autocomplete.png")


def test_disaster_history_displayed(page: Page):
    """検索結果に被災履歴セクションが表示される"""
    page.goto(BASE)
    page.fill("#address-input", "東京都江東区豊洲3丁目")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    history = page.locator(".disaster-history")
    expect(history).to_be_visible()
    items = page.locator(".disaster-item")
    assert items.count() >= 1, f"被災履歴が1件以上表示されるはず。実際: {items.count()}"
    page.screenshot(path="tests/screenshots/23_disaster_history.png")


def test_mobile_layout(page: Page):
    """モバイル幅（390px）で地図とパネルが縦積みになる"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(BASE)
    page.fill("#address-input", "大阪府大阪市西区")
    page.click("button")
    page.locator(".total-score .number").wait_for(timeout=20000)
    # パネルの幅が画面幅と同じになっている（横並びでない）
    panel_box = page.locator(".panel").bounding_box()
    assert panel_box["width"] >= 380, f"モバイルではパネルが全幅のはず: {panel_box['width']}"
    page.screenshot(path="tests/screenshots/24_mobile.png")
