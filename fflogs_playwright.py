"""
FFLogs 繁中服 Savage 排名抓取器 v4 (Playwright)
用瀏覽器自動化抓取，繞過 Cloudflare 限制
"""

import sys
import time
import re
import json
import subprocess
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─── 設定 ─────────────────────────────────────────────────────────────────────

BASE_URL   = "https://www.fflogs.com"
ZONE_URL   = (
    f"{BASE_URL}/zone/reports"
    "?zone=62&boss=0&difficulty=101&class=Any&spec=Any"
    "&keystone=0&kills=2&duration=0&page={page}"
)

BEST_FILE   = "D:/FF_LOG排名/RankingBest.xlsx"
REPO_DIR    = "D:/FF_LOG排名"
JSON_BEST   = "D:/FF_LOG排名/data_best.json"
JSON_ALL    = "D:/FF_LOG排名/data_all.json"
CACHE_FILE  = "D:/FF_LOG排名/report_cache.json"

TARGET_BOSSES = {
    "Black Cat":       "Black Cat Savage",
    "Honey B. Lovely": "Honey B. Lovely Savage",
    "Brute Bomber":    "Brute Bomber Savage",
    "Wicked Thunder":  "Wicked Thunder Savage",
}

MAX_ZONE_PAGES = 50   # zone/reports 頁數上限
PAGE_DELAY     = 1.5  # 頁面操作間隔（秒）
FIGHT_DELAY    = 2.0  # 每場 Kill 間隔（秒）

# ─── TC服判斷規則 ─────────────────────────────────────────────────────────────

HEALERS = {"Astrologian", "Scholar", "Sage", "WhiteMage"}
TANKS   = {"Gunbreaker", "Paladin", "DarkKnight", "Warrior"}

TC_NAME_MAX = 6  # TC服名稱去空格後最多 6 字符

def rdps_limit(job: str) -> float:
    if job in HEALERS: return 17000
    if job in TANKS:   return 19000
    return 31000       # DPS

def is_tc_fight(rows: list[dict]) -> bool:
    """
    若同場任一玩家觸發以下條件，整場視為非TC服，回傳 False：
    1. 名稱去空格後長度 > TC_NAME_MAX
    2. rDPS 超過該職業上限
    """
    for r in rows:
        if len(r["玩家名稱"].replace(" ", "")) > TC_NAME_MAX:
            return False
        if r["rDPS"] > rdps_limit(r["職業"]):
            return False
    return True

# ─── 瀏覽器工具 ───────────────────────────────────────────────────────────────

def make_browser(playwright):
    browser = playwright.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="zh-TW",
        viewport={"width": 1280, "height": 900},
    )
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """)
    return browser, ctx


def load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def wait_cf(page, timeout=30):
    """等待 Cloudflare challenge 通過（頁面 title 不再是 'Just a moment...'）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = page.title()
        if "just a moment" not in title.lower():
            return
        time.sleep(1)
    raise RuntimeError("Cloudflare challenge 逾時，請手動操作瀏覽器。")


# ─── 第一階段：收集 Report codes + upload time ────────────────────────────────

def collect_report_codes(page) -> list[dict]:
    """
    回傳: [{"code": str, "upload_time": str}, ...]
    upload_time 為 zone 頁面上顯示的時間文字，用於快取比對
    """
    reports = []
    seen    = set()

    for pg_num in range(1, MAX_ZONE_PAGES + 1):
        url = ZONE_URL.format(page=pg_num)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        wait_cf(page)

        try:
            page.wait_for_selector("td.description-cell", timeout=10000)
        except PWTimeout:
            break

        # 逐行抓 code + 該行最後一欄（通常是日期）
        rows = page.query_selector_all("tr")
        page_count = 0
        for row in rows:
            link = row.query_selector("td.description-cell a[href^='/reports/']")
            if not link:
                continue
            href = link.get_attribute("href") or ""
            m = re.search(r"/reports/([A-Za-z0-9]+)", href)
            if not m:
                continue
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)

            # 取最後一個 td 的文字作為 upload_time
            tds = row.query_selector_all("td")
            upload_time = tds[-1].inner_text().strip() if tds else ""

            reports.append({"code": code, "upload_time": upload_time})
            page_count += 1

        if not page_count:
            break

        print(f"  第 {pg_num} 頁: +{page_count} 個 Report（累計 {len(reports)}）")

        next_btn = page.query_selector("a.next-page, li.next > a, a[rel='next'], a:has-text('Next')")
        if not next_btn:
            break

        time.sleep(PAGE_DELAY)

    return reports


# ─── 第二階段：取得每個 Report 的 Savage Kill fights ─────────────────────────

def get_savage_kills(page, code: str, upload_time: str, cache: dict) -> tuple[list[dict], bool]:
    """
    攔截 fights-and-participants 回應，篩選 difficulty=101 的 kill fights
    若 upload_time 未變則直接回傳快取，不開啟頁面
    回傳: (kills, from_cache)
    """
    # ── 快取命中 ──
    cached = cache.get(code)
    if cached and upload_time and cached.get("upload_time") == upload_time:
        return cached["kills"], True

    # ── 需要抓取 ──
    fights_data = {}

    def handle_response(resp):
        if "fights-and-participants" in resp.url:
            try:
                body = resp.json()
                fights_data["fights"] = body.get("fights", [])
            except Exception:
                pass

    page.on("response", handle_response)
    try:
        page.goto(
            f"{BASE_URL}/reports/{code}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        wait_cf(page)
        # 等待網路請求
        page.wait_for_timeout(3000)
    finally:
        page.remove_listener("response", handle_response)

    kills = []
    for f in fights_data.get("fights", []):
        if (
            f.get("difficulty") == 101
            and f.get("kill") is True
            and f.get("name") in TARGET_BOSSES
        ):
            kills.append({
                "id":         f["id"],
                "name":       f["name"],
                "start_time": f.get("start_time", 0),
                "end_time":   f.get("end_time", 0),
            })

    # ── 更新快取 ──
    cache[code] = {"upload_time": upload_time, "kills": kills}
    return kills, False


# ─── 職業顏色對照表（FFLogs 標準配色） ──────────────────────────────────────
# 來源: FFLogs 網站 All Jobs 顏色示範
# 用於 actor-sprite 無法取得時的備用偵測

JOB_COLOR_MAP: dict[str, str] = {
    "#a8d2e6": "Paladin",
    "#cf2621": "Warrior",
    "#d126cc": "DarkKnight",
    "#796d30": "Gunbreaker",
    "#fff0dc": "WhiteMage",
    "#8657ff": "Scholar",
    "#ffe74a": "Astrologian",
    "#80a0f0": "Sage",
    "#d69c00": "Monk",
    "#4164cd": "Dragoon",
    "#af1964": "Ninja",
    "#e46d04": "Samurai",
    "#965a80": "Reaper",
    "#6f2b7f": "Viper",
    "#91ba5e": "Bard",
    "#6ee1d6": "Machinist",
    "#e2b0af": "Dancer",
    "#a579d6": "BlackMage",
    "#2d9b78": "Summoner",
    "#e87b7b": "RedMage",
    "#fd97c7": "Pictomancer",
    "#b38bff": "BlueMage",
}


def _extract_job(tr) -> str | None:
    """
    職業偵測（三段備援）:
    1. icon 元素的 actor-sprite-{Job} class
    2. tooltip span 文字（'\nJobName\n'）
    3. 玩家名稱 <a> 的 style color → JOB_COLOR_MAP
    """
    # 方法1: sprite class
    icon = tr.query_selector("[class*='actor-sprite-']")
    if icon:
        cls_str = icon.get_attribute("class") or ""
        m = re.search(r"actor-sprite-(\w+)", cls_str)
        if m:
            return m.group(1)

    # 方法2: tooltip span text
    for span in tr.query_selector_all("span.tooltip, span[class*='tooltip']"):
        txt = span.inner_text().strip()
        if txt and "\n" not in txt and len(txt) < 30:
            return txt.replace(" ", "")  # "Black Mage" → "BlackMage"

    # 方法3: 玩家名稱顏色
    name_a = tr.query_selector("a.main-table-link, a.tooltip.main-table-link")
    if name_a:
        style = name_a.get_attribute("style") or ""
        m = re.search(r"color\s*:\s*(#[0-9a-fA-F]{3,6})", style)
        if m:
            color = m.group(1).lower()
            if color in JOB_COLOR_MAP:
                return JOB_COLOR_MAP[color]
        # 也試 class 名稱（例如 class="... job-Paladin ..."）
        cls = name_a.get_attribute("class") or ""
        m2 = re.search(r"job-(\w+)", cls, re.IGNORECASE)
        if m2:
            return m2.group(1)

    return None


# ─── 第三階段：解析傷害表格 ──────────────────────────────────────────────────

def parse_damage_table(page, code: str, fight: dict) -> list[dict]:
    fid      = fight["id"]
    boss     = TARGET_BOSSES[fight["name"]]
    duration = (fight["end_time"] - fight["start_time"]) / 1000.0

    url = f"{BASE_URL}/reports/{code}#fight={fid}&type=damage-done"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    wait_cf(page)

    try:
        page.wait_for_selector("table.summary-table.report.dataTable", timeout=20000)
    except PWTimeout:
        print(f"    [警告] 傷害表格未出現 ({code} fight={fid})")
        return []

    rows = []
    trs = page.query_selector_all("table.summary-table.report.dataTable tr")

    for tr in trs:
        # 取得職業（三段備援）
        job = _extract_job(tr)
        if not job:
            continue
        if job.lower() == "limitbreak":
            continue

        # 玩家名稱
        name_el = tr.query_selector("a.main-table-link, a.tooltip.main-table-link")
        if not name_el:
            name_el = tr.query_selector("a")
        name = (name_el.inner_text().strip() if name_el else "").split("\n")[0].strip()
        if not name:
            continue

        # 數字欄位（DPS / rDPS / aDPS）
        # 表格欄位順序: 名稱 | % | 總傷害 | 活躍% | DPS | rDPS | aDPS
        tds = tr.query_selector_all("td")
        nums = []
        for td in tds:
            t = td.inner_text().strip().replace(",", "")
            try:
                nums.append(float(t))
            except ValueError:
                nums.append(None)

        # DPS / rDPS / aDPS 是最後三個純數字欄
        dps_vals = [v for v in nums if v is not None and v > 0]

        if len(dps_vals) >= 3:
            dps, rdps, adps = dps_vals[-3], dps_vals[-2], dps_vals[-1]
        elif len(dps_vals) == 2:
            dps, rdps, adps = dps_vals[-2], dps_vals[-1], dps_vals[-1]
        elif len(dps_vals) == 1:
            dps = rdps = adps = dps_vals[0]
        else:
            dps = rdps = adps = 0.0

        rows.append({
            "副本":         boss,
            "職業":         job,
            "玩家名稱":     name,
            "DPS":          round(dps,  2),
            "rDPS":         round(rdps, 2),
            "aDPS":         round(adps, 2),
            "戰鬥時長(秒)": round(duration, 1),
            "Report":       code,
            "FightID":      fid,
        })

    return rows


# ─── RankingBest 累積更新 ────────────────────────────────────────────────────

BEST_BOSS_SHEETS = {
    "Black Cat Savage":       "Black Cat(最佳)",
    "Honey B. Lovely Savage": "Honey B Lovely(最佳)",
    "Brute Bomber Savage":    "Brute Bomber(最佳)",
    "Wicked Thunder Savage":  "Wicked Thunder(最佳)",
}

BEST_COLS = ["同職業排名", "副本", "職業", "玩家名稱", "DPS", "rDPS", "aDPS", "戰鬥時長(秒)"]


def _rank_best(df: pd.DataFrame) -> pd.DataFrame:
    """同職業排名（按 rDPS 降序），回傳含「同職業排名」欄的 DataFrame"""
    df = df.sort_values(["職業", "rDPS"], ascending=[True, False]).copy()
    df["同職業排名"] = df.groupby("職業").cumcount() + 1
    df = df.sort_values(["職業", "同職業排名"]).reset_index(drop=True)
    return df


def update_best_file(df_today: pd.DataFrame) -> None:
    """
    以今日資料更新 RankingBest.xlsx：
    - 玩家有更高 rDPS → 更新紀錄
    - 新玩家 → 新增紀錄
    - 今日未出現的玩家 → 保留舊紀錄不刪除
    """
    # 載入現有最佳檔（若存在）
    existing: dict[str, pd.DataFrame] = {}
    try:
        xl = pd.read_excel(BEST_FILE, sheet_name=None)
        for sheet, sdf in xl.items():
            existing[sheet] = sdf
        print(f"  載入現有 RankingBest（{len(existing)} 個分頁）")
    except FileNotFoundError:
        print("  RankingBest.xlsx 不存在，將建立新檔")

    with pd.ExcelWriter(BEST_FILE, engine="openpyxl") as writer:
        for display_name, sheet_name in BEST_BOSS_SHEETS.items():
            # 今日該 Boss 的最佳（每人取最高 rDPS）
            today_boss = df_today[df_today["副本"] == display_name].copy()
            today_best = (
                today_boss
                .sort_values("rDPS", ascending=False)
                .drop_duplicates(subset="玩家名稱", keep="first")
                .copy()
            )

            if sheet_name in existing:
                old = existing[sheet_name].copy()
                # 合併：新舊資料疊加後，每人保留最高 rDPS
                merged = pd.concat([old, today_best], ignore_index=True)
                merged = (
                    merged
                    .sort_values("rDPS", ascending=False)
                    .drop_duplicates(subset="玩家名稱", keep="first")
                    .copy()
                )
            else:
                merged = today_best.copy()

            # 確保欄位完整
            for col in ["副本", "職業", "玩家名稱", "DPS", "rDPS", "aDPS", "戰鬥時長(秒)"]:
                if col not in merged.columns:
                    merged[col] = ""

            merged = _rank_best(merged)
            # 整理欄位順序，移除 Report/FightID
            merged = merged.drop(columns=["Report", "FightID", "同職業排名"], errors="ignore")
            merged = _rank_best(merged)
            out_cols = [c for c in BEST_COLS if c in merged.columns]
            merged[out_cols].to_excel(writer, sheet_name=sheet_name, index=False)

            updated = len(today_best)
            total   = len(merged)
            print(f"  {sheet_name}: 今日 {updated} 筆 → 累積 {total} 位玩家")


# ─── JSON 匯出 & Git 推送 ────────────────────────────────────────────────────

def export_json(df_today: pd.DataFrame) -> None:
    """
    輸出兩個 JSON 供網站讀取：
    data_best.json — 每人最高 rDPS，同職業排名
    data_all.json  — 全場次資料，依 DPS 排序
    """
    updated = datetime.now().strftime("%Y-%m-%d %H:%M")

    def df_to_records(d: pd.DataFrame) -> list[dict]:
        return json.loads(d.to_json(orient="records", force_ascii=False))

    best_data: dict[str, list] = {}
    all_data:  dict[str, list] = {}

    for display_name in TARGET_BOSSES.values():
        boss_df = df_today[df_today["副本"] == display_name].copy()
        if boss_df.empty:
            best_data[display_name] = []
            all_data[display_name]  = []
            continue

        # all: DPS 排序，移除 Report/FightID
        all_ranked = boss_df.sort_values("DPS", ascending=False).reset_index(drop=True)
        all_ranked.insert(0, "排名", range(1, len(all_ranked) + 1))
        all_ranked = all_ranked.drop(columns=["Report", "FightID"], errors="ignore")
        all_data[display_name] = df_to_records(all_ranked)

        # best: 每人最高 rDPS，同職業排名
        best = (
            boss_df
            .sort_values("rDPS", ascending=False)
            .drop_duplicates(subset="玩家名稱", keep="first")
            .copy()
        )
        best = best.sort_values(["職業", "rDPS"], ascending=[True, False])
        best["同職業排名"] = best.groupby("職業").cumcount() + 1
        best = best.sort_values(["職業", "同職業排名"]).reset_index(drop=True)
        best = best.drop(columns=["Report", "FightID"], errors="ignore")
        best_data[display_name] = df_to_records(best)

    with open(JSON_BEST, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "bosses": best_data}, f, ensure_ascii=False, indent=2)
    with open(JSON_ALL, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "bosses": all_data},  f, ensure_ascii=False, indent=2)

    print(f"  JSON 輸出: data_best.json / data_all.json")


def git_push() -> None:
    """將 JSON 資料檔 commit 並 push 到 GitHub"""
    today = datetime.now().strftime("%Y-%m-%d")
    cmds = [
        ["git", "-C", REPO_DIR, "add", "data_best.json", "data_all.json"],
        ["git", "-C", REPO_DIR, "commit", "-m", f"data: update rankings {today}"],
        ["git", "-C", REPO_DIR, "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # commit 若沒有變動會失敗，屬正常情況
            if "nothing to commit" in result.stdout + result.stderr:
                print("  Git: 資料無變動，略過 push")
                return
            print(f"  [Git 警告] {' '.join(cmd[2:])}: {result.stderr.strip()}")
            return
    print("  Git push 完成 → GitHub Pages 將在約 30 秒後更新")


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("FFLogs 繁中服 Savage 排名抓取器 v4 (Playwright)")
    print(f"執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    with sync_playwright() as pw:
        browser, ctx = make_browser(pw)
        page = ctx.new_page()

        # ── 步驟1: 收集 Report 清單 ──
        print("\n[1/3] 收集 Report 清單...")
        reports = collect_report_codes(page)
        print(f"  共 {len(reports)} 個 Report\n")

        if not reports:
            print("未找到任何 Report，請確認 URL 參數或網路連線。")
            browser.close()
            return

        # ── 步驟2: 掃描 Savage Kill fights（含快取）──
        print("[2/3] 掃描各 Report 的 Savage Kill...")
        cache = load_cache()

        # 本次查到的 Report code 集合，用於清理失效快取
        current_codes = {r["code"] for r in reports}

        kill_list: list[tuple[str, dict]] = []
        cache_hits = 0
        for idx, report in enumerate(reports, 1):
            code        = report["code"]
            upload_time = report["upload_time"]
            print(f"  [{idx}/{len(reports)}] {code}", end=" ")
            from_cache = False          # 保證變數在 except 時仍已定義
            try:
                kills, from_cache = get_savage_kills(page, code, upload_time, cache)
                tag = "[快取]" if from_cache else f"→ {len(kills)} kill(s)"
                print(tag)
                if from_cache:
                    cache_hits += 1
                for k in kills:
                    kill_list.append((code, k))
            except Exception as e:
                print(f"→ [錯誤] {e}")
            if not from_cache:
                time.sleep(PAGE_DELAY)

        # 移除本次未查到的舊快取（Report 已從 zone 頁面消失）
        stale = [c for c in list(cache) if c not in current_codes]
        if stale:
            for c in stale:
                del cache[c]
            print(f"  清理失效快取: {len(stale)} 筆 ({', '.join(stale[:5])}{'...' if len(stale)>5 else ''})")

        save_cache(cache)
        print(f"\n  快取命中: {cache_hits}/{len(reports)}，共找到 {len(kill_list)} 場 Savage Kill\n")

        print(f"\n  共找到 {len(kill_list)} 場 Savage Kill\n")

        if not kill_list:
            print("  未找到任何 Savage Kill。")
            browser.close()
            return

        # ── 步驟3: 抓取傷害表格（含TC服場次過濾）──
        print("[3/3] 抓取各場 Kill 的傷害資料...")
        all_rows = []
        skipped_fights = 0
        for idx, (code, fight) in enumerate(kill_list, 1):
            boss = TARGET_BOSSES[fight["name"]]
            print(f"  [{idx}/{len(kill_list)}] {boss} — {code} Fight {fight['id']}", end=" ")
            try:
                rows = parse_damage_table(page, code, fight)
                # 移除 rDPS=0 的無效列
                rows = [r for r in rows if r["rDPS"] > 0]
                if not is_tc_fight(rows):
                    print(f"→ [略過] 疑似非TC服")
                    skipped_fights += 1
                else:
                    all_rows.extend(rows)
                    print(f"→ {len(rows)} 位玩家")
            except Exception as e:
                print(f"→ [錯誤] {e}")
            time.sleep(FIGHT_DELAY)

        browser.close()

    print(f"\n  略過非TC服場次: {skipped_fights} 場")
    if not all_rows:
        print("未成功取得任何玩家資料。")
        return

    df = pd.DataFrame(all_rows)

    # ── 輸出 Excel ──
    print(f"\n[輸出] 產生 Excel...")
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = f"D:/FF_LOG排名/rankings_pw_{timestamp}.xlsx"

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="全部資料", index=False)

        for display_name in TARGET_BOSSES.values():
            boss_df = df[df["副本"] == display_name].copy()
            if boss_df.empty:
                continue
            short = display_name.replace(" Savage", "").replace(".", "")
            ranked = boss_df.sort_values("DPS", ascending=False).reset_index(drop=True)
            ranked.insert(0, "排名", range(1, len(ranked) + 1))
            ranked.to_excel(writer, sheet_name=short[:31], index=False)

    print(f"  今日資料輸出: {output_path}")

    # ── 更新累積最佳排名 ──
    print("\n[更新] RankingBest.xlsx...")
    update_best_file(df)

    # ── 輸出 JSON 並推送到 GitHub ──
    print("\n[JSON] 匯出網站資料...")
    export_json(df)
    print("\n[Git] 推送至 GitHub Pages...")
    git_push()

    print(f"\n完成！")
    print(f"  今日資料: {len(df)} 筆 / {len(kill_list)} 場 Kill")
    print(f"  最佳排名: {BEST_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    run()
