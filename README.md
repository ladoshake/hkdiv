# 港股 股息率排名

港股（沪深以外）高股息股票排名工具，从腾讯自选股（WeStock）实时行情与历史分红自动计算，每日可定时刷新。

> 本项目由「美股/港股 股息率排名」拆分而来，仅保留港股部分；计算口径、UI 风格与原版港股完全一致。

## 功能

- **市值分档**：以港元计，两档——市值 > 1000 亿港元、500 亿港元 < 市值 ≤ 1000 亿港元。
- **两种股息率口径**：
  - **TTM 股息率**：近 12 个月每股股息 ÷ 现价（行情接口直接提供）。
  - **LFY 股息率**：最近完整财年每股分红之和 ÷ 现价；另附前两年历史列。
- **每档 Top 30**：各市值档内按股息率排名，至多展示前 30 名。
- **交互页面**：市场/市值档/口径（TTM·LFY）切换，表头点击排序，移动端自适应。

## 计算口径说明

- 港股市值以港元计，表中「总市值」列以**亿港元**展示。
- 每股分红 / 分红次数取自个股分红历史，统计除息日落在近 12 个月内的现金分红。
- 护栏：特殊分红或异常价格导致 LFY 畸高（> TTM×2.5 或 > 15%）时置空，避免误导。
- 数据来源：腾讯自选股（港股）实时行情与历史分红。榜单为计算快照，非投资建议。

## 本地运行

```bash
# 依赖：Node（调用 westock-data / westock-tool skill 脚本）
python3 scripts/pipeline.py
# 产物：index.html（自包含，可直接用浏览器打开）
```

## 部署到 Vercel

本项目为纯静态站点（`index.html` 在根目录，`vercel.json` 已配置 `outputDirectory: "."`）。

1. 将本仓库推送到 GitHub（见下方）。
2. 打开 [Vercel 控制台](https://vercel.com/dashboard) → **Add New → Project** → 导入该 GitHub 仓库。
3. Framework 选 **Other**，Build Command 留空，Output Directory 填 `.`（vercel.json 已指定）。
4. 点击 Deploy，几秒后即可获得独立网址。
5. 之后每次 `git push` 到 main，Vercel 自动重新部署。

## 定时自动刷新（可选）

可用 WorkBuddy 自动化 / crontab 每日执行 `python3 scripts/pipeline.py && git push` 实现榜单日更。
