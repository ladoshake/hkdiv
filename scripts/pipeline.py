#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股 股息率排名 —— 每日自动更新流水线（单文件、可独立运行）
流程：取代码宇宙 -> 批量 quote(真实市值/股息率TTM) -> 批量分红历史 -> 分市值档 Top30 -> 写 HTML
数据来源：腾讯自选股（westock-data skill 的行情/分红接口）
"""
import subprocess, json, re, os, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor

# ---------------- 环境常量（绝对路径，避免依赖环境变量） ----------------
NODE = "/Users/green/.workbuddy/binaries/node/versions/22.22.2/bin/node"
DATA_JS = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
TOOL_JS = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-tool/scripts/index.js"

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
HTML_PATH = os.path.abspath(os.path.join(BASE, "..", "index.html"))

# ---------------- 动态日期 ----------------
TODAY = datetime.date.today()
TTM_START = TODAY - datetime.timedelta(days=365)
GEN = TODAY.isoformat()
TTM_START_STR = TTM_START.isoformat()
LATEST_YEAR = TODAY.year - 1   # 最近一个完整自然年（用于港股历史列）

FX = 7.8  # HKD per USD（联系汇率，仅说明用）
TOP = 30

def log(*a):
    print("[pipeline]", *a, flush=True)

# =====================================================================
# 1) 代码宇宙
# =====================================================================
def run_filter(market, limit):
    out = subprocess.run(
        [NODE, TOOL_JS, "filter", "intersect([TotalMV > 0])",
         "--market", market, "--orderby", "TotalMV", "--desc", "--limit", str(limit)],
        capture_output=True, text=True, timeout=180)
    return out.stdout

def get_hk_universe():
    txt = run_filter("hk", 2500)
    rows = []
    for l in txt.splitlines():
        if "| hk" not in l:
            continue
        cols = [c.strip() for c in l.strip().strip("|").split("|")]
        if len(cols) < 4:
            continue
        try:
            mv = float(cols[2])
        except:
            continue
        rows.append((cols[0], cols[1], mv))
    # 仅保留 >500亿港元（覆盖两档：>1000 / 500-1000）
    THRESH = 500
    rows = [r for r in rows if r[2] > THRESH]
    # 去重：按归一化名（去掉 -R/-WR/-W 等柜台后缀），保留市值最大者
    def base(n):
        return re.sub(r'-[A-Z]{1,3}$', '', n).strip()
    best = {}
    for code, name, mv in rows:
        b = base(name)
        if b not in best or mv > best[b][2]:
            best[b] = (code, name, mv)
    uniq = sorted(best.values(), key=lambda x: -x[2])
    json.dump(uniq, open(os.path.join(DATA_DIR, "hk_universe.json"), "w"), ensure_ascii=False, indent=1)
    log(f"HK universe (>500亿HKD): raw={len(rows)} after dedupe={len(uniq)}")
    return uniq

# =====================================================================
# 2) 批量行情 quote
# =====================================================================
def run_quote(codes):
    out = subprocess.run([NODE, DATA_JS, "quote", ",".join(codes)],
                         capture_output=True, text=True, timeout=120)
    return out.stdout

def parse_quotes_generic(text, market):
    """按列名解析 quote 输出。港股 mv 单位亿港元。"""
    lines = text.splitlines()
    hidx = None
    for i, l in enumerate(lines):
        if "code" in l and "name" in l and "total_market_cap" in l and "|" in l:
            hidx = i
            break
    if hidx is None:
        return {}
    header = [c.strip() for c in lines[hidx].strip().strip("|").split("|")]
    if header and header[0] == "":
        header = header[1:]
    idx = {h: j for j, h in enumerate(header)}
    prefix = "hk"
    res = {}
    p_idx, d_idx, m_idx = idx["price"], idx["dividend_ratio_ttm"], idx["total_market_cap"]
    for l in lines[hidx + 1:]:
        s = l.strip().strip("|").strip()
        if not s or not s.startswith(prefix):
            continue
        cols = [c.strip() for c in s.split("|")]
        if len(cols) <= m_idx:
            continue
        code = cols[idx["code"]]
        if not code.startswith(prefix):
            continue
        def num(x):
            try:
                return float(x) if x not in ("", "-") else None
            except:
                return None
        res[code] = {
            "code": code, "name": cols[idx["name"]],
            "price": num(cols[p_idx]), "ttm_yield": num(cols[d_idx]), "mv": num(cols[m_idx]),
        }
    return res

def fetch_quotes(codes, market):
    out = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        try:
            txt = run_quote(batch)
            parsed = parse_quotes_generic(txt, market)
            out.update(parsed)
        except Exception as e:
            log(f"  quote batch {i} error: {e}")
        if i % 200 == 0:
            log(f"  quote {min(i+50,len(codes))}/{len(codes)} parsed={len(out)}")
    log(f"{market} quotes parsed: {len(out)}")
    return out

# =====================================================================
# 3) 分红历史
# =====================================================================
def parse_date(s):
    s = (s or "").strip()
    if len(s) < 8:
        return None
    try:
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except:
        return None

def run_div(code, force=False, retries=6):
    """取分红历史。
    force=True 表示行情已显示有股息率（几乎肯定派息）——此时若接口返回'暂无'，
    大概率是限流/偶发失败，按退避策略重试；空页/超时同样重试。
    force=False（行情无股息率）则首次见到'暂无'即判定为真的不派息，直接返回 None。
    """
    backoff = 0.4
    for _ in range(retries):
        try:
            out = subprocess.run([NODE, DATA_JS, "dividend", "list", code, "--years", "5"],
                                 capture_output=True, text=True, timeout=60).stdout
        except Exception:
            time.sleep(backoff); backoff = min(backoff * 2, 4); continue
        if "reportEndDate" in out or "exDivDate" in out:
            return out
        if "暂无分红数据" in out or "暂无" in out:
            if not force:
                return None
            time.sleep(backoff); backoff = min(backoff * 2, 4); continue
        # 既无分红行也无明确'暂无'：可能是空页/接口抖动，重试
        time.sleep(backoff); backoff = min(backoff * 2, 4)
    return None

def parse_hk_div(txt):
    rows = []
    for l in txt.splitlines():
        l = l.strip()
        if not l.startswith("|"):
            continue
        if "reportEndDate" in l or "---" in l or l == "|":
            continue
        cols = [c.strip() for c in l.strip("|").split("|")]
        if len(cols) < 5:
            continue
        try:
            cash = float(cols[3])
        except:
            cash = 0.0
        if cash <= 0:
            continue
        rows.append({
            "reportEndDate": parse_date(cols[0]),
            "exDiviDate": parse_date(cols[1]),
            "cash": cash,
        })
    return rows

def full_year_dps(rows):
    """港股：按 reportEndDate 年份汇总，跳过当前部分年度残值，取最近完整财年。"""
    by_year = {}
    for r in rows:
        if not r["reportEndDate"]:
            continue
        y = r["reportEndDate"].year
        by_year[y] = by_year.get(y, 0.0) + r["cash"]
    years = sorted(by_year.keys(), reverse=True)
    if not years:
        return None, None
    chosen = years[0]
    if len(years) > 1 and by_year[years[0]] < 0.6 * by_year[years[1]]:
        chosen = years[1]
    return chosen, by_year[chosen]

def fetch_hk_div(quotes):
    def work(q):
        code = q["code"]
        # 行情已显示股息率>0 的，几乎必然派息；对'暂无'强制重试，规避限流假阴性
        force = (q.get("ttm_yield") or 0) > 0
        txt = run_div(code, force=force)
        rec = {
            "code": code, "name": q["name"], "price": q.get("price"),
            "mv_hkd": q.get("mv"), "ttm_yield": q.get("ttm_yield"),
            "has_div": False, "ttm_dps": None, "ttm_count": None,
            "lfy_dps": None, "lfy_count": None, "lfy_yield": None,
            "prev_yield": None, "prev2_yield": None,
        }
        if not txt:
            return rec
        rows = parse_hk_div(txt)
        if not rows:
            return rec
        rec["has_div"] = True
        price = q.get("price")
        ttm_rows = [r for r in rows if r["exDiviDate"] and TTM_START <= r["exDiviDate"] <= TODAY]
        if ttm_rows:
            rec["ttm_dps"] = round(sum(r["cash"] for r in ttm_rows), 4)
            rec["ttm_count"] = len(ttm_rows)
        ly, ldps = full_year_dps(rows)
        if ly is not None and price:
            rec["lfy_dps"] = round(ldps, 4)
            rec["lfy_count"] = sum(1 for r in rows if r["reportEndDate"] and r["reportEndDate"].year == ly)
            rec["lfy_yield"] = round(ldps / price * 100, 3)
        for yr, key in [(LATEST_YEAR - 1, "prev_yield"), (LATEST_YEAR - 2, "prev2_yield")]:
            yr_rows = [r for r in rows if r["reportEndDate"] and r["reportEndDate"].year == yr]
            if yr_rows and price:
                s = round(sum(r["cash"] for r in yr_rows), 4)
                rec[key] = round(s / price * 100, 3)
        # 护栏：特殊分红/错误价格导致 LFY 畸高
        ttm = rec.get("ttm_yield")
        for k in ("lfy_yield", "prev_yield", "prev2_yield"):
            v = rec.get(k)
            if v is None or ttm is None:
                continue
            if v > ttm * 2.5 or v > 15:
                rec[k] = None
        return rec
    result = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, rec in enumerate(ex.map(work, quotes), 1):
            result.append(rec)
            if i % 60 == 0:
                log(f"  HK div progress {i}/{len(quotes)}")
    json.dump(result, open(os.path.join(DATA_DIR, "hk_enriched.json"), "w"), ensure_ascii=False, indent=1)
    log(f"HK enriched: {len(result)} (with div: {sum(1 for x in result if x['has_div'])})")
    return result

# =====================================================================
# 4) 构建 HTML
# =====================================================================
def sanitize(d):
    ttm = d.get("ttm_yield")
    for k in ("lfy_yield", "prev_yield", "prev2_yield"):
        v = d.get(k)
        if v is None or ttm is None:
            continue
        if v > ttm * 2.5 or v > 15:
            d[k] = None
    return d

def strip_mkt_prefix(code):
    """去掉行情接口返回的市场前缀 hk，仅用于展示。"""
    if code and code[:2].lower() in ("us", "hk"):
        return code[2:]
    return code

def build_html(hk):
    def classify_hk(d):
        mv = (d.get("mv_hkd") or 0)
        if mv > 1000: return "gt1000"
        if 500 < mv <= 1000: return "mid500"
        return None

    HK_CAPS = [{"key": "gt1000", "label": "市值 > 1000亿港元"},
               {"key": "mid500", "label": "500亿 < 市值 ≤ 1000亿港元"}]

    def build_market(records, classify, mv_key, cap_specs):
        groups = {"gt1000": [], "mid500": []}
        for d in records:
            k = classify(d)
            if k: groups[k].append(d)
        tiers = []
        for cs in cap_specs:
            key, label = cs["key"], cs["label"]
            sub = groups[key]
            pt = sorted([x for x in sub if (x.get("ttm_yield") or 0) > 0],
                        key=lambda x: -x["ttm_yield"])[:TOP]
            pl = sorted([x for x in sub if (x.get("lfy_yield") is not None) or (x.get("ttm_yield") or 0) > 0],
                        key=lambda x: (x.get("lfy_yield") is None, -(x.get("lfy_yield") or 0)))[:TOP]
            def make_rows(rows, yk, dk, ck):
                out = []
                for i, d in enumerate(rows, 1):
                    out.append({
                        "rank": i, "code": strip_mkt_prefix(d["code"]), "name": d["name"], "price": d.get("price"),
                        "total_mv_yi": d.get(mv_key), "dps": d.get(dk),
                        "div_count": d.get(ck), "yield": d.get(yk),
                        "prev_yield": d.get("prev_yield"), "prev2_yield": d.get("prev2_yield"),
                    })
                return out
            tiers.append({
                "key": key, "label": label, "count": len(sub),
                "ttm": make_rows(pt, "ttm_yield", "ttm_dps", "ttm_count"),
                "lfy": make_rows(pl, "lfy_yield", "lfy_dps", "lfy_count"),
            })
        return tiers

    hk_tiers = build_market(hk, classify_hk, "mv_hkd", HK_CAPS)
    markets = [
        {"key": "hk", "name": "港股", "cur": "港元", "price_unit": "港元", "dps_unit": "港元", "caps": HK_CAPS, "tiers": hk_tiers},
    ]
    EMBEDDED = {"generated_at": GEN, "ttm_start": TTM_START_STR, "markets": markets}
    json_str = json.dumps(EMBEDDED, ensure_ascii=False)

    HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>港股 股息率排名</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --ink:#1f2430; --sub:#6b7280;
    --line:#e5e7eb; --brand:#c0392b; --brand2:#2563eb; --gold:#b8860b;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--ink);line-height:1.5}
  .wrap{max-width:1100px;margin:0 auto;padding:16px 16px 56px}
  header h1{font-size:21px;margin:0 0 4px}
  header p{margin:1px 0;color:var(--sub);font-size:13px}
  .subhead{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:8px 0 2px}
  .desc{margin:0;color:var(--sub);font-size:clamp(12px,1.1vw,14px);line-height:1.5}
  .metabox{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .meta{margin:0;text-align:right;color:var(--sub);font-size:clamp(12px,1.1vw,14px);line-height:1.4;white-space:nowrap}
  .box{display:flex;align-items:baseline;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 12px;white-space:nowrap}
  .num{font-size:13px;font-weight:700;color:var(--brand2)}
  .lab{font-size:13px;color:var(--sub)}
  .tabs{display:flex;gap:8px;margin:14px 0 8px}
  .tab{padding:7px 20px;display:flex;flex-direction:column;align-items:center;gap:1px;border:1px solid var(--line);border-radius:999px;background:var(--card);cursor:pointer;font-size:14px;font-weight:600;color:var(--sub);line-height:1.2}
  .tab-main{white-space:nowrap}
  .tab-sub{font-size:11px;font-weight:400;opacity:.78}
  .tab.active .tab-sub{opacity:.92}
  .tab.active{background:var(--brand2);color:#fff;border-color:var(--brand2)}
  .capsel{display:flex;gap:8px;margin:14px 0 2px;flex-wrap:wrap}
  .cap{padding:6px 16px;border:1px solid var(--line);border-radius:999px;background:var(--card);cursor:pointer;font-size:13px;font-weight:600;color:var(--sub);line-height:1.2;white-space:nowrap}
  .cap.active{background:var(--brand);color:#fff;border-color:var(--brand)}
  .panel{display:none}
  .panel.active{display:block}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13.5px}
  .table-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{background:#f0f1f4;color:#374151;font-weight:600;cursor:pointer;user-select:none;position:relative}
  th.sort-asc::after{content:" ▲";font-size:10px;color:var(--brand)}
  th.sort-desc::after{content:" ▼";font-size:10px;color:var(--brand)}
  tbody tr:nth-child(even){background:#f7f8fa}
  tbody tr:hover{background:#e8f0fe}
  .rk{display:inline-block;min-width:22px;text-align:center;font-weight:700;color:var(--brand2)}
  .code{color:var(--sub);font-size:12px}
  .yld{font-weight:700;color:var(--brand)}
  .panel.alt .yld{color:var(--brand2)}
  .yld2{font-weight:600;color:#0f766e}
  .sub{font-size:10px;color:var(--sub);font-weight:400;line-height:1.15}
  .note{width:100%;margin-top:28px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:clamp(12px,1.6vw,18px) clamp(14px,1.8vw,20px);font-size:clamp(12px,1.1vw,14px);color:#374151}
  .note h3{margin:0 0 8px;font-size:clamp(14px,1.4vw,16px)}
  .note ul{margin:6px 0;padding-left:20px}
  .note li{margin:3px 0}
  .tag{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:6px;padding:1px 7px;font-size:11px;margin-left:6px}
  @media (max-width:560px){
    .subhead{flex-direction:column;align-items:flex-start;gap:8px}
    .metabox{flex-direction:column;align-items:flex-start;gap:8px}
    .meta{text-align:left;white-space:normal}
    .tabs{gap:6px}
    .tab{padding:8px 12px;font-size:clamp(12px,3.4vw,14px)}
    .wrap{padding:12px 12px 48px}
    /* 冻结首两列（排名、名称）：横向滑动时不动 */
    table{border-collapse:separate;border-spacing:0;overflow:visible}
    th:nth-child(1),td:nth-child(1),
    th:nth-child(2),td:nth-child(2){position:sticky}
    th:nth-child(1),td:nth-child(1){left:0;width:46px;min-width:46px;max-width:46px;background:#fff;z-index:2}
    th:nth-child(2),td:nth-child(2){
      left:46px;background:#fff;z-index:2;
      box-shadow:2px 0 4px -2px rgba(15,23,42,.22)  /* 冻结分隔阴影 */
    }
    th:nth-child(1),th:nth-child(2){background:#f0f1f4;z-index:3}
    tbody tr:nth-child(even) td:nth-child(1),
    tbody tr:nth-child(even) td:nth-child(2){background:#f7f8fa}
    tbody tr:hover td:nth-child(1),
    tbody tr:hover td:nth-child(2){background:#e8f0fe}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>港股 股息率排名</h1>
    <div class="subhead">
      <p class="desc" id="desc">港股 · 市值 &gt; 1000亿港元 · 股息率排名</p>
      <div class="metabox">
        <div class="meta">数据日期：<span id="gen"></span> ｜ TTM计算起点：<span id="ttmstart"></span></div>
        <div class="box"><div class="lab" id="cntlab">公司数</div><div class="num" id="cnt"></div></div>
      </div>
    </div>
  </header>

  <div class="capsel" id="capsel">
    <div class="cap active" data-cap="gt1000">市值 &gt; 1000亿港元</div>
    <div class="cap" data-cap="mid500">500亿 &lt; 市值 ≤ 1000亿港元</div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="ttm"><span class="tab-main">TTM 股息率</span><span class="tab-sub">最近12个月</span></div>
    <div class="tab alt" data-tab="lfy"><span class="tab-main">LFY 股息率</span><span class="tab-sub">最近财年</span></div>
  </div>

  <div class="panel active" id="panel-ttm">
    <div class="table-wrap">
    <table id="table-ttm">
      <thead><tr>
        <th data-k="rank">排名</th><th data-k="name">名称</th><th data-k="code">代码</th>
        <th data-k="price" id="th-price">现价(港元)</th><th data-k="total_mv_yi" id="th-mv">总市值(亿港元)</th>
        <th data-k="dps" id="th-dps">每股分红(港元)</th><th data-k="div_count">分红次数</th><th data-k="yield">TTM股息率</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="panel" id="panel-lfy">
    <div class="table-wrap">
    <table id="table-lfy">
      <thead><tr>
        <th data-k="rank">排名</th><th data-k="name">名称</th><th data-k="code">代码</th>
        <th data-k="price" id="th-price2">现价(港元)</th><th data-k="total_mv_yi" id="th-mv2">总市值(亿港元)</th>
        <th data-k="dps" id="th-dps2">每股分红(港元)</th><th data-k="div_count">分红次数</th><th data-k="yield">LFY股息率</th><th data-k="prev_yield">2024年股息率</th><th data-k="prev2_yield">2023年股息率</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="note">
    <h3>计算口径说明</h3>
    <ul>
      <li><b>市值筛选</b>：以港元计，提供两档——市值 &gt; 1000 亿港元、500 亿港元 &lt; 市值 ≤ 1000 亿港元，取各档内全部派息股按股息率排名（每档至多 Top 30）。</li>
      <li><b>港股市值单位</b>：总市值以港元计，表中「总市值」列以<b>亿港元</b>展示。</li>
      <li><b>股息率(TTM)</b>：行情接口「股息率TTM」= 近12个月每股股息 ÷ 现价 × 100%。</li>
      <li><b>每股分红 / 分红次数</b>：取自个股分红历史，统计除息日落在近 12 个月内的现金分红之和与次数；部分股票接口暂无分红历史，显示为「—」。</li>
      <li><b>LFY 股息率</b>：最近完整年度每股分红之和 ÷ 现价；历史列对应最近一年、前一年，口径同 LFY。</li>
      <li><b>货币单位</b>：港股为港元（现价、每股分红、总市值均按港元）。</li>
      <li><b>数据来源</b>：腾讯自选股（港股）实时行情与历史分红。榜单为计算快照，非投资建议。</li>
    </ul>
  </div>

</div>

<script>
const EMBEDDED = __JSON__;
let DATA = EMBEDDED;
let currentMkt = DATA.markets[0].key;
let currentCap = "gt1000";
let currentTab = "ttm";

const fmt = (n,d=2)=> (n==null||isNaN(n))?"-":Number(n).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});

const sortState = {};
const sortKey = (mkt,cap,kind)=> mkt+"_"+cap+"_"+kind;

function getMarket(k){ return DATA.markets.find(m=>m.key===k); }
function getTier(mkt,k){ const m=getMarket(mkt); return m.tiers.find(t=>t.key===k); }
function rowsFor(mkt, cap, kind){ const t=getTier(mkt,cap); return (kind==="ttm"?t.ttm:t.lfy).slice(); }

function renderTable(tableId, rows, withFy){
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  rows.forEach(r=>{
    const tr = document.createElement("tr");
    const lfyCell = `<td class="yld">${fmt(r.yield,2)}%</td>`;
    const prev = withFy ? `<td class="yld2">${fmt(r.prev_yield,2)}%</td><td class="yld2">${fmt(r.prev2_yield,2)}%</td>` : "";
    tr.innerHTML = `<td><span class="rk">${r.rank}</span></td>
      <td><b>${r.name}</b></td>
      <td class="code">${r.code}</td>
      <td>${fmt(r.price)}</td>
      <td>${fmt(r.total_mv_yi,0)}</td>
      <td>${fmt(r.dps,3)}</td>
      <td class="cnt">${fmt(r.div_count,0)}</td>
      ${lfyCell}${prev}`;
    tbody.appendChild(tr);
  });
}

function applyTable(tableId, withFy){
  const kind = withFy ? "lfy" : "ttm";
  const skey = sortKey(currentMkt, currentCap, kind);
  const sk = sortState[skey] || (sortState[skey] = {key:"yield", dir:-1});
  const rows = rowsFor(currentMkt, currentCap, kind);
  rows.sort((a,b)=>{
    let va=a[sk.key], vb=b[sk.key];
    if(typeof va==="string"){ return sk.dir*String(va).localeCompare(String(vb),"zh"); }
    if(va==null) va = sk.dir<0 ? -Infinity : Infinity;
    if(vb==null) vb = sk.dir<0 ? -Infinity : Infinity;
    return sk.dir*(va-vb);
  });
  renderTable(tableId, rows, withFy);
  const table = document.getElementById(tableId);
  table.querySelectorAll("th").forEach(x=>{
    x.classList.remove("sort-asc","sort-desc");
    if(x.dataset.k===sk.key) x.classList.add(sk.dir===1?"sort-asc":"sort-desc");
  });
}

function updateCaps(){
  const m = getMarket(currentMkt);
  const map = {};
  (m.caps || []).forEach(c=> map[c.key] = c.label);
  document.querySelectorAll(".cap").forEach(el=>{
    const k = el.dataset.cap;
    if(map[k] != null) el.textContent = map[k];
  });
}

function updateUnits(){
  const m = getMarket(currentMkt);
  const pu=m.price_unit, du=m.dps_unit, cu=m.cur;
  const set=(id,t)=>{const e=document.getElementById(id); if(e) e.textContent=t;};
  set("th-price",`现价(${pu})`); set("th-price2",`现价(${pu})`);
  set("th-mv",`总市值(亿${cu})`); set("th-mv2",`总市值(亿${cu})`);
  set("th-dps",`每股分红(${du})`); set("th-dps2",`每股分红(${du})`);
}

function renderAll(){
  applyTable("table-ttm", false);
  applyTable("table-lfy", true);
  const m = getMarket(currentMkt);
  const t = getTier(currentMkt, currentCap);
  document.getElementById("desc").textContent = `${m.name} · ${t.label} · 股息率排名`;
  document.getElementById("cntlab").textContent = `${m.name} ${t.label.replace(/\s+/g,"")}公司数`;
  document.getElementById("cnt").textContent = t.count;
  updateCaps();
  updateUnits();
}

function bindTable(tableId, withFy){
  const table = document.getElementById(tableId);
  table.querySelectorAll("th").forEach(th=>{
    th.addEventListener("click",()=>{
      const kind = withFy ? "lfy" : "ttm";
      const kp = th.dataset.k;
      const skey = sortKey(currentMkt, currentCap, kind);
      const sk = sortState[skey] || (sortState[skey] = {key:"yield", dir:-1});
      if(sk.key===kp){ sk.dir *= -1; }
      else { sk.key=kp; sk.dir = (kp==="rank"||kp==="name"||kp==="code")?1:-1; }
      applyTable(tableId, withFy);
    });
  });
}

function initData(d){
  DATA = d;
  document.getElementById("gen").textContent = DATA.generated_at;
  document.getElementById("ttmstart").textContent = DATA.ttm_start;
  renderAll();
}

bindTable("table-ttm", false);
bindTable("table-lfy", true);

document.querySelectorAll(".mkt").forEach(c=>{
  c.addEventListener("click",()=>{
    document.querySelectorAll(".mkt").forEach(x=>x.classList.remove("active"));
    c.classList.add("active");
    currentMkt = c.dataset.mkt;
    renderAll();
  });
});

document.querySelectorAll(".cap").forEach(c=>{
  c.addEventListener("click",()=>{
    document.querySelectorAll(".cap").forEach(x=>x.classList.remove("active"));
    c.classList.add("active");
    currentCap = c.dataset.cap;
    renderAll();
  });
});

document.querySelectorAll(".tab").forEach(t=>{
  t.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x=>x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("panel-"+t.dataset.tab).classList.add("active");
    currentTab = t.dataset.tab;
    renderAll();
  });
});

initData(EMBEDDED);
</script>
</body>
</html>
"""
    html_doc = HTML.replace("__JSON__", json_str)
    open(HTML_PATH, "w").write(html_doc)
    log(f"HTML written -> {HTML_PATH} ({len(html_doc)} bytes)")
    # 汇总输出，便于自动化验证
    summary = {}
    for m in markets:
        summary[m["name"]] = {}
        for t in m["tiers"]:
            top = t["ttm"][0] if t["ttm"] else None
            summary[m["name"]][t["label"]] = {
                "candidates": t["count"],
                "shown": len(t["ttm"]),
                "top1": (top["name"], top["yield"]) if top else None,
            }
    log("SUMMARY " + json.dumps(summary, ensure_ascii=False))

# =====================================================================
# 主流程
# =====================================================================
def main():
    log(f"=== start (TODAY={GEN}, TTM_START={TTM_START_STR}, LATEST_YEAR={LATEST_YEAR}) ===")
    t0 = time.time()

    log("[1/3] HK universe")
    hk_uni = get_hk_universe()
    hk_codes = [r[0] for r in hk_uni]

    log("[2/3] HK quotes")
    hk_quotes = fetch_quotes(hk_codes, "hk")
    hk_recs = [hk_quotes[c] for c in hk_codes if c in hk_quotes]

    log("[3/3] HK dividend history")
    hk_enriched = fetch_hk_div(hk_recs)

    build_html([sanitize(d) for d in hk_enriched])

    log(f"=== done in {time.time()-t0:.1f}s ===")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
