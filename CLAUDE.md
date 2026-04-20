# FFLogs 繁中服 Savage 排名 — 專案說明

## 專案概述
自動抓取 FFLogs TC（繁中）伺服器 Savage 排名資料，產生排行榜網站。
- 網站：https://whatthefoxx.github.io/fflogs-tc-ranking/
- GitHub Repo：https://github.com/WhatTheFoxx/fflogs-tc-ranking

## 主要檔案
| 檔案 | 說明 |
|------|------|
| `fflogs_playwright.py` | 主抓取程式（Playwright） |
| `index.html` | 排名網站前端 |
| `data_best.json` | 網站資料來源（累積最佳排名，由程式自動產生） |
| `RankingBest.xlsx` | 累積最佳排名 Excel（本地存放，不上 git） |
| `report_cache.json` | 抓取快取（本地存放，不上 git） |

## 執行方式

### 一般執行（每日例行）
```bash
cd D:/FF_LOG排名
python fflogs_playwright.py
```
流程：
1. 從 FFLogs zone 頁面收集所有 Report 清單
2. 比對快取：已處理過的 Report 直接跳過
3. 只抓新 Report 的 Kill 場次與傷害資料
4. 與 RankingBest.xlsx 比對更新（新紀錄或更高 rDPS 才更新）
5. 輸出 data_best.json → git push → GitHub Pages 自動更新

### 單一 Report 模式（手動補抓指定報告）
```bash
python fflogs_playwright.py https://www.fflogs.com/reports/XXXXXXXXXXXXXXXX
# 或只貼 code
python fflogs_playwright.py XXXXXXXXXXXXXXXX
```
- 不跑 Step 1，不檢查快取，直接強制處理指定 Report
- 處理完一樣更新 RankingBest.xlsx 並 push 到 GitHub

## 快取機制
- `report_cache.json` 以 Report code 為 key，記錄 `upload_time` 與 `kills`
- 若 upload_time 未變 → 該 Report 資料已在 RankingBest 中，完全跳過
- 每次執行會自動清除不再出現在 zone 頁面的舊快取

## TC 服判斷邏輯
同一場次（8人）中，只要有任一玩家觸發以下條件，整場排除：
1. 玩家名稱去空格後長度 > 6 字符
2. rDPS 超過職業上限（DPS: 31000 / Tank: 19000 / Healer: 17000）

## 網站結構
- Boss Tabs：Black Cat / Honey B. Lovely / Brute Bomber / Wicked Thunder
- Role Tabs（上層）：Tank / Healer / Melee / Range / Cast — 合併同 Role 職業、依 rDPS 統一排名
- Job Pills（下層）：個別職業，A-Z 排序，依 rDPS 同職業排名
