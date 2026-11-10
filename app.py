import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import streamlit.components.v1 as components
from plotly.subplots import make_subplots
import json, os, re, secrets, hmac, tempfile, requests, math, hashlib, time, logging
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from datetime import date, timedelta

st.set_page_config(page_title="FCN 管理與客戶回報系統", page_icon="📈", layout="wide")

st.markdown("""<style>
.block-container{padding-top:2.2rem;padding-bottom:3rem;max-width:1280px}
h1{font-weight:700;letter-spacing:.5px}
h3,h4{font-weight:650;margin-top:.4rem}
[data-testid="stMetric"]{background:#f7f9fc;border:1px solid #e3e9f2;border-radius:12px;padding:12px 16px}
[data-testid="stMetricLabel"]{color:#5a6678}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:12px;border-color:#e3e9f2;background:#f7f9fc}
.stTabs [data-baseweb="tab-list"]{gap:6px;border-bottom:1px solid #e3e9f2}
.stTabs [data-baseweb="tab"]{padding:8px 18px;font-weight:600;border-radius:8px 8px 0 0}
[data-testid="stDataFrame"]{border:1px solid #e3e9f2;border-radius:10px;overflow:hidden}
div[role="radiogroup"] label{background:#eef3fa;border:1px solid #d7e1ef;border-radius:20px;padding:2px 14px;margin-right:6px}
hr{margin:1.4rem 0}
</style>""", unsafe_allow_html=True)

DATA_FILE = os.environ.get("FCN_DATA_FILE", "fcn_clients.json")
LIGHT = {"g": "🟢 正常", "y": "🟡 警戒", "r": "🔴 危機", "n": "⚪ 無資料"}
TODAY = pd.Timestamp.today().normalize()


# ---------- 資料存取 ----------
EXP_OPTS = {"1 天": 1, "1 週": 7, "1 個月": 30}
LP_MIN, LP_MAX = 6, 12
_MIG = {"added": False}


def new_exp(days):
    return (pd.Timestamp.now("UTC") + pd.Timedelta(days=days)).isoformat()


def link_expired(c):
    if not c.get("token_exp"):
        return True  # 沒有期限一律視為已過期（不存在永久連結）
    try:
        return pd.Timestamp.now("UTC") >= pd.Timestamp(c.get("token_exp"))
    except Exception:
        return True


def link_left(c):
    sec = (pd.Timestamp(c["token_exp"]) - pd.Timestamp.now("UTC")).total_seconds()
    if sec <= 0:
        return "已過期"
    d, h = int(sec // 86400), int(sec % 86400 // 3600)
    return f"剩 {d} 天 {h} 小時" if d else f"剩 {h} 小時 {int(sec % 3600 // 60)} 分"


def migrate(db):
    """舊版資料自動升級：補 token / ISIN / KO / 狀態，target_1~5 轉為 targets 清單"""
    for acct, c in db.items():
        c["name"] = _clean(c.get("name")) or f"客戶 {acct}"
        c.setdefault("token", secrets.token_urlsafe(12))
        if not c.get("token_exp"):  # 舊連結沒有期限 → 一律改為自現在起 24 小時，不再有永久連結
            c["token_exp"] = new_exp(1)
            _MIG["added"] = True
        c.setdefault("positions", [])
        for p in c["positions"]:
            for k_ in ("pos_name", "case_no", "isin"):
                if k_ in p:
                    p[k_] = _clean(p[k_])
            p.setdefault("id", secrets.token_hex(4))
            p.setdefault("isin", "")
            p.setdefault("closed", "")
            p.setdefault("records", [])
            p.setdefault("nc", 1)
            for k0, v0 in (("exit_date", ""), ("exit_note", ""), ("assign_symbol", ""), ("assign_shares", 0), ("assign_cost", 0.0)):
                p.setdefault(k0, v0)
            if "targets" not in p:
                p["targets"] = [p[f"target_{i}"] for i in range(1, 6) if p.get(f"target_{i}", {}).get("symbol")]
                for i in range(1, 6):
                    p.pop(f"target_{i}", None)
            for t in p["targets"]:
                t.setdefault("ko_price", 0)
                t["symbol"] = _sym(t.get("symbol"))
    return db


def _why(e):
    """內部錯誤只寫進後台日誌，畫面上只顯示通用訊息（避免洩漏資料庫網址等內部資訊）"""
    logging.getLogger("fcn").error("%s: %s", type(e).__name__, str(e)[:300])
    return "系統暫時無法處理，請稍後再試；若持續發生請聯絡系統管理員。"


def _clean(x, n=80):
    """去掉可能被當成 Markdown／HTML 的符號（客戶稱呼、商品名稱、案件編號等會顯示在客戶頁面）"""
    return re.sub(r"[<>\[\]`\\]", "", str(x or "")).strip()[:n]


def _sym(x):
    return re.sub(r"[^A-Za-z0-9.\-^=]", "", str(x or "")).upper()[:20]


def _sb():
    """若有設定 SUPABASE_URL / SUPABASE_KEY，資料改存雲端資料庫（重啟、更新都不會消失）"""
    try:
        u, k = st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"]
    except Exception:
        u, k = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
    u, k = str(u).strip().rstrip("/"), str(k).strip()
    return (u, k) if u and k else None


def _sb_headers(k):
    h = {"apikey": k, "Content-Type": "application/json"}
    if k.startswith("eyJ"):  # 舊式 JWT 金鑰（service_role）才需要 Authorization；新式 sb_secret_ 金鑰只用 apikey
        h["Authorization"] = f"Bearer {k}"
    return h


def multi_mode():
    """有設定雲端資料庫 → 多營業員模式（註冊／登入，各自的資料分開存放）"""
    return _sb() is not None


def _key(owner=None):
    if not multi_mode():
        return "main"
    o = owner if owner is not None else st.session_state.get("rep")
    return f"rep:{o}" if o else None


def sb_get(key):
    u, k = _sb()
    r = requests.get(f"{u}/rest/v1/fcn_store", headers=_sb_headers(k), timeout=15, params={"id": f"eq.{key}", "select": "data"})
    r.raise_for_status()
    rows = r.json()
    return rows[0]["data"] if rows else None


def sb_put(key, data):
    u, k = _sb()
    h = _sb_headers(k)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    r = requests.post(f"{u}/rest/v1/fcn_store", headers=h, timeout=15,
                      json={"id": key, "data": data, "updated_at": pd.Timestamp.now("UTC").isoformat()})
    r.raise_for_status()


def sb_del(key):
    u, k = _sb()
    requests.delete(f"{u}/rest/v1/fcn_store", headers=_sb_headers(k), timeout=15, params={"id": f"eq.{key}"}).raise_for_status()


def sb_list(prefix, with_data=False):
    u, k = _sb()
    r = requests.get(f"{u}/rest/v1/fcn_store", headers=_sb_headers(k), timeout=20,
                     params={"id": f"like.{prefix}*", "select": "id,updated_at" + (",data" if with_data else "")})
    r.raise_for_status()
    return r.json()


def _load_raw(owner=None):
    sb = _sb()
    if sb:
        key = _key(owner)
        if key is None:
            return {}
        try:
            data = sb_get(key)
            return migrate(data) if data else {}
        except Exception as e:
            # 不退回本機檔案，避免在連線異常時用空資料覆蓋雲端
            st.error("雲端資料庫讀取失敗。" + _why(e))
            st.stop()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return migrate(json.load(f))
        except Exception:
            st.error("資料檔讀取失敗，請檢查 JSON 格式或從備份還原。")
            st.stop()
    return {}


def load_data(owner=None):
    db = _load_raw(owner)
    changed = auto_split(db)  # 偵測到標的拆股，條件價已自動換算
    if _MIG["added"]:  # 舊連結補上期限
        changed, _MIG["added"] = True, False
    if changed:
        save_data(db, quiet=True, owner=owner)
    return db


def save_data(db, quiet=False, owner=None):
    sb = _sb()
    if sb:
        key = _key(owner)
        if key is None:
            return  # 尚未登入，不寫入
        try:
            sb_put(key, db)
        except Exception as e:
            if quiet:
                return
            st.error("雲端資料庫儲存失敗，這次的變更沒有存下來。" + _why(e))
            st.stop()
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(DATA_FILE)))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)  # 原子寫入，避免寫到一半損毀
    except Exception:
        if not quiet:
            raise


# ---------- 行情與判斷 ----------
@st.cache_data(ttl=600)
def get_hist(sym, start):
    try:
        df = yf.Ticker(sym).history(start=start, auto_adjust=False)[["Close"]]
        df = df.dropna(subset=["Close"])  # Yahoo 偶爾回傳收盤價為空的最新一列，會讓畫面出現 nan
        if df.empty:
            return None
        df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return None


@st.cache_data(ttl=900)
def get_ohlc(sym, start):
    try:
        df = yf.Ticker(sym).history(start=start, auto_adjust=False)[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if df.empty:
            return None
        df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return None


@st.cache_data(ttl=21600)
def get_splits(sym):
    """回傳 [(日期字串, 比例)]；比例 3 代表 1 拆 3。失敗時丟出例外（不會被快取）"""
    s = yf.Ticker(sym).splits
    return [(str(pd.Timestamp(d).tz_localize(None).date()), float(r)) for d, r in s.items() if r and abs(float(r) - 1) > 1e-9]


def split_text(f):
    return f"1 拆 {f:g}" if f >= 1 else f"{round(1 / f):g} 併 1"


def auto_split(db):
    """偵測持有期間內標的拆股／反向拆股，自動把期初價與 Strike／KO／KI 換算為拆股後價格。
    Yahoo 歷史股價已還原拆股，若條件價仍是拆股前的數字，燈號與線圖會失真。
    以「期初價 ≈ 起息日還原後收盤價 × 拆股倍數」判斷條件價是否仍為拆股前價格，已換算過的不會重複換算。"""
    changed = False
    for c in db.values():
        for p in c["positions"]:
            if p.get("closed") or not p.get("start_date"):
                continue
            sd = pd.Timestamp(p["start_date"])
            end = min(pd.Timestamp(p["end_date"]), TODAY) if p.get("end_date") else TODAY
            for t in p["targets"]:
                init = float(t.get("init_price") or 0)
                if init <= 0:
                    continue
                try:
                    sp = [(d, r) for d, r in get_splits(t["symbol"]) if sd < pd.Timestamp(d) <= end]
                except Exception:
                    continue
                if not sp:
                    continue
                df = get_hist(t["symbol"], p["start_date"])
                if df is None or df.empty or float(df.Close.iloc[0]) <= 0:
                    continue
                f = 1.0
                for _, r in sp:
                    f *= r
                c0 = float(df.Close.iloc[0])
                if abs(math.log(init / (c0 * f))) >= abs(math.log(init / c0)):
                    continue  # 期初價已是拆股後價格，不需換算
                keys = ("init_price", "strike_price", "ko_price", "ki_price", "ra_price")
                old = {k: t.get(k, 0) for k in keys}
                for k in keys:
                    if t.get(k):
                        t[k] = round(float(t[k]) / f, 4)
                t.setdefault("split_log", []).append({
                    "date": "、".join(d for d, _ in sp), "ratio": f, "old": old,
                    "new": {k: t.get(k, 0) for k in keys}, "ack": False})
                changed = True
    return changed


def split_notes(db, only_unack=False, days=None):
    out = []
    for acct, c in db.items():
        for p in c["positions"]:
            for t in p["targets"]:
                for l in t.get("split_log", []):
                    if only_unack and l.get("ack"):
                        continue
                    if days is not None and (TODAY - pd.Timestamp(l["date"].split("、")[-1])).days > days:
                        continue
                    out.append((acct, c, p, t, l))
    return out


CN = {"NVDA": "輝達", "TSM": "台積電", "TSLA": "特斯拉", "AAPL": "蘋果", "MSFT": "微軟", "AMZN": "亞馬遜", "GOOGL": "谷歌",
      "GOOG": "谷歌", "META": "Meta", "AMD": "超微", "AVGO": "博通", "MU": "美光", "INTC": "英特爾", "NFLX": "網飛",
      "QCOM": "高通", "ORCL": "甲骨文", "PLTR": "Palantir", "9984.T": "軟銀集團", "6861.T": "基恩斯", "6981.T": "村田製作所",
      "6857.T": "愛德萬測試", "8035.T": "東京威力科創", "7203.T": "豐田汽車", "6758.T": "索尼集團", "9983.T": "迅銷", "6920.T": "雷泰光電"}
SITES = {"經濟日報": "money.udn.com", "壹蘋網": "tw.nextapple.com", "鉅亨網": "cnyes.com", "Yahoo股市": "tw.stock.yahoo.com"}


@st.cache_data(ttl=1800)
def get_news(sym, n=10):
    """以 Google News RSS 抓取指定媒體近 30 天、含該標的的新聞標題"""
    kw = f'("{sym}" OR "{CN[sym]}")' if sym in CN else f'"{sym}"'
    q = f'{kw} ({" OR ".join("site:" + d for d in SITES.values())}) when:30d'
    try:
        r = requests.get("https://news.google.com/rss/search", timeout=8, headers={"User-Agent": "Mozilla/5.0"},
                         params={"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
        r.raise_for_status()
        out = []
        if len(r.content) > 2_000_000:
            return []
        for it in ET.fromstring(r.content).iter("item"):
            title, src = it.findtext("title", ""), it.findtext("source", "")
            if src and title.endswith(" - " + src):
                title = title[:-len(src) - 3]
            lk = (it.findtext("link", "") or "").strip()
            if not re.match(r"^https?://", lk):
                continue
            out.append({"title": _clean(title, 200), "link": lk.replace(" ", "%20").replace(")", "%29"), "src": _clean(src, 40),
                        "date": parsedate_to_datetime(it.findtext("pubDate")).strftime("%m/%d")})
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


@st.cache_data(ttl=86400)
def get_name(sym):
    try:
        return yf.Ticker(sym).info.get("shortName") or CN.get(sym, sym)
    except Exception:
        return CN.get(sym, sym)


def last_px(t, p):
    df = get_hist(t["symbol"], p["start_date"])
    return (float(df.Close.iloc[-1]) if df is not None else 0.0), df


PTYPES = {"FCN": "一般 FCN", "STEP": "Stepdown（KO 價逐期遞減）", "DRA": "DRA 區間累計配息"}


def ptype(p):
    return p.get("ptype", "FCN")


def ki_type(p):
    return p.get("ki_type", "EKI")


KI_TXT = {"EKI": "EKI（只看期末評價日）", "AKI": "AKI（期間每日收盤皆觀察）"}


def ki_hit(p, t):
    """AKI：合約期間內是否曾有收盤價低於 KI（回傳 (是否觸及, 最低收盤, 日期)）"""
    df = get_hist(t["symbol"], p["start_date"])
    if df is None or not t.get("ki_price"):
        return False, None, None
    lo = float(df.Close.min())
    return lo < t["ki_price"], lo, df.Close.idxmin()


def t_status(t, px, hit=False):
    if not px:
        return "n"
    if hit or (t["ki_price"] and px < t["ki_price"]):
        return "r"
    if t["strike_price"] and px < t["strike_price"]:
        return "y"
    return "g"


def tstat(p, t, px):
    """EKI：現價低於 KI 只是警示（到期評價日才定案）；AKI：期間曾低於 KI 即永久觸及"""
    return t_status(t, px, hit=ki_type(p) == "AKI" and ki_hit(p, t)[0])


def pos_status(p):
    sts = [tstat(p, t, last_px(t, p)[0]) for t in p["targets"]]
    return next((s for s in "ryg" if s in sts), "n")


def is_active(p):
    return not p["closed"] and pd.Timestamp(p["end_date"]) >= TODAY


def obs_list(p):
    """每期觀察（期末）日：說明書有列就用，否則用每月日程"""
    if p.get("obs_dates"):
        return [pd.Timestamp(d) for d in p["obs_dates"]]
    return [d for d, _ in _base_schedule(p)[0]]


def ko_step_idx(p):
    ol = obs_list(p)
    return next((i for i, d in enumerate(ol) if d >= TODAY), len(ol) - 1)


def ko_level(p, t, idx=None):
    """KO 價：Stepdown 依第幾期遞減；其他型態為固定價"""
    stp = p.get("ko_steps")
    if not stp:
        return t["ko_price"]
    i = ko_step_idx(p) if idx is None else idx
    return round(t["init_price"] * stp[max(0, min(i, len(stp) - 1))] / 100, 4)


def ko_idx_at(p, d):
    return max(0, sum(1 for x in obs_list(p) if x <= d) - 1)


def all_above_ko(p):
    pxs = [(t, last_px(t, p)[0]) for t in p["targets"]]
    return bool(pxs) and all(t["ko_price"] and px >= ko_level(p, t) for t, px in pxs)


def _base_schedule(p):
    """每月配息日程；最後一期對齊到期日（說明書有列出實際給付日時用實際日期）"""
    mc = p["principal"] * p["coupon_rate"] / 1200
    if p.get("pay_dates"):
        return [(pd.Timestamp(d), mc) for d in p["pay_dates"]], mc
    s, e = pd.Timestamp(p["start_date"]), pd.Timestamp(p["end_date"])
    n = max(1, round((e - s).days / 30.4))
    rows = [(min(s + pd.DateOffset(months=k), e), mc) for k in range(1, n + 1)]
    rows[-1] = (e, mc)
    return rows, mc


def ra_calc(p):
    """DRA 區間累計：每期配息 = 最大利率 ×（所有標的收盤都在區間內的天數 ÷ 該期總天數）。前 N 期保證滿額。"""
    ends = obs_list(p)
    pays = [pd.Timestamp(d) for d in p.get("pay_dates", [])] or ends
    mc = p["principal"] * p["coupon_rate"] / 1200
    free = int(p.get("ra_free", 2))
    start = pd.Timestamp(p.get("ra_obs_start") or p["start_date"])
    cl = {}
    for t in p["targets"]:
        df = get_hist(t["symbol"], str(start.date()))
        if df is not None and t.get("ra_price"):
            cl[t["symbol"]] = df.Close
    ok = None
    if len(cl) == len(p["targets"]) and cl:
        dfc = pd.DataFrame(cl).dropna()
        ok = pd.Series(True, index=dfc.index)
        for t in p["targets"]:
            ok &= dfc[t["symbol"]] >= t["ra_price"]
            if t.get("ra_hi_price"):
                ok &= dfc[t["symbol"]] <= t["ra_hi_price"]
    rows, prev = [], None
    for i, e in enumerate(ends):
        lo = start if prev is None else prev + pd.Timedelta(days=1)
        full = len(pd.bdate_range(lo, e))
        acc = tot = None
        if i < free:
            amt, kind = mc, "保證滿額"
        elif ok is None:
            amt, kind = mc, "無行情（以上限計）"
        else:
            sub = ok[(ok.index >= lo) & (ok.index <= min(e, TODAY))]
            n = len(sub)
            acc = int(sub.sum())
            if e <= TODAY:
                tot = n or full
                amt, kind = mc * acc / tot, "已結算"
            elif n:
                tot = full
                amt, kind = mc * acc / n, "進行中（以迄今達標率推估）"
            else:
                amt, kind = mc, "尚未開始（上限）"
        rows.append({"期別": i + 1, "期末日": e, "給付日": pays[i] if i < len(pays) else e, "達標天數": acc, "總天數": tot,
                     "配息": round(amt, 2), "狀態": kind})
        prev = e
    return rows, mc


def schedule(p):
    if ptype(p) == "DRA":
        rows, mc = ra_calc(p)
        return [(r["給付日"], r["配息"]) for r in rows], mc
    return _base_schedule(p)


SYMS = {"USD": "$", "JPY": "¥", "EUR": "€", "AUD": "A$", "GBP": "£", "CNH": "CN¥", "NZD": "NZ$", "ZAR": "R", "CHF": "CHF "}
CCYS = ["USD", "JPY", "EUR", "AUD", "GBP", "CNH", "NZD", "ZAR", "CHF"]


def ccy(p):
    return p.get("ccy") or "USD"


def money(p, x, d=None):
    """依部位幣別顯示金額；日圓預設不顯示小數。價格請明確傳入 d=2"""
    c_ = ccy(p)
    return f"{SYMS.get(c_, c_ + ' ')}{x:,.{(0 if c_ == 'JPY' else 2) if d is None else d}f}"


def tot_text(items, d=None):
    """[(幣別, 金額), ...] → 依幣別加總後以「｜」連接，例如 $1,200.00｜¥75,000"""
    agg = {}
    for c_, a in items:
        agg[c_] = agg.get(c_, 0) + a
    return "｜".join(f"{SYMS.get(c_, c_ + ' ')}{v:,.{(0 if c_ == 'JPY' else 2) if d is None else d}f}" for c_, v in agg.items()) or "—"


def tot_dict(items, d=None):
    agg = {}
    for c_, a in items:
        agg[c_] = agg.get(c_, 0) + a
    return [f"{SYMS.get(c_, c_ + ' ')}{v:,.{(0 if c_ == 'JPY' else 2) if d is None else d}f}" for c_, v in agg.items()]


def stack_metric(col, label, items, d=None):
    """每個幣別各佔一行，避免多幣別時數字被截斷成「…」"""
    with col.container(border=True):
        st.caption(label)
        for t_ in tot_dict(items, d) or ["—"]:
            st.markdown(f"#### {t_}")


def ko_first(p):
    ol = obs_list(p)
    return ol[min(max(p.get("nc", 1), 1) - 1, len(ol) - 1)]


# ---------- 部位表單 (新增 / 編輯 / PDF 預填共用) ----------
def pos_form(key, d):
    d = d or {}
    kk = lambda n: f"{key}_{n}"
    c0 = st.columns(3)
    pts = list(PTYPES)
    pt_ = c0[0].selectbox("商品型態", pts, index=pts.index(d.get("ptype", "FCN")) if d.get("ptype", "FCN") in pts else 0,
                          format_func=PTYPES.get, key=kk("ptype"))
    kts = list(KI_TXT)
    kt_ = c0[1].selectbox("KI 類型", kts, index=kts.index(d.get("ki_type", "EKI")) if d.get("ki_type", "EKI") in kts else 0,
                          format_func=KI_TXT.get, key=kk("kit"))
    c0[2].caption("EKI：只在期末評價日比較收盤價與 KI。AKI：合約期間內任一交易日收盤低於 KI 就算觸及，之後到期若低於轉換價／履約價就接股票。")
    pct = st.checkbox("以「期初價 × 百分比」換算 Strike / KO / KI", key=kk("pct"))
    with st.form(key):
        c = st.columns(4)
        case_no = c[0].text_input("案件編號", d.get("case_no", ""), key=kk("case"))
        isin = c[1].text_input("ISIN Code", d.get("isin", ""), key=kk("isin")).strip().upper()
        name = c[2].text_input("部位名稱", d.get("pos_name", ""), key=kk("name"))
        principal = c[3].number_input("投資本金（依下方幣別）", 0.0, value=float(d.get("principal", 50000)), step=1000.0, key=kk("pr"))
        c = st.columns(4)
        nc = c[3].number_input("首次KO觀察月數", 1, 12, int(d.get("nc", 1)), key=kk("nc"))
        rate = c[0].number_input("年利率 (%)", 0.0, 100.0, float(d.get("coupon_rate", 10.0)), 0.1, key=kk("rate"))
        sd = c[1].date_input("合約起始日", date.fromisoformat(d.get("start_date", str(date.today()))), key=kk("sd"))
        ed = c[2].date_input("合約到期日", date.fromisoformat(d.get("end_date", str(date.today() + timedelta(days=365)))), key=kk("ed"))
        c = st.columns(4)
        cur = d.get("ccy", "USD")
        ccy_ = c[0].selectbox("計價幣別", CCYS, index=CCYS.index(cur) if cur in CCYS else 0, key=kk("ccy"))
        ko_obs = c[1].selectbox("KO 觀察頻率", ["monthly", "daily"], index=1 if d.get("ko_obs") == "daily" else 0, key=kk("koobs"),
                                format_func=lambda x: "每月（配息日）" if x == "monthly" else "每日（每個交易日）")
        c[2].caption("日股請填 Yahoo 代號，例如 9984.T")
        steps_txt, rap, rfree = "", 60.0, 2
        if pt_ == "STEP":
            steps_txt = st.text_input("KO 價逐期遞減（期初價 %，依觀察日順序、逗號分隔）",
                                      ",".join(f"{x:g}" for x in d.get("ko_steps", [100, 95, 90, 85, 80, 75])), key=kk("steps"))
        if pt_ == "DRA":
            c = st.columns(3)
            rap = c[0].number_input("區間下限（期初價 %）", 0.0, 200.0, float(d.get("ra_lower_pct", d.get("sp", 60))), key=kk("rap"))
            rfree = c[1].number_input("前幾期保證滿額配息", 0, 12, int(d.get("ra_free", 2)), key=kk("rfree"))
            c[2].caption("年利率請填「最大」年利率；每期配息＝最大利率 ×（所有標的收盤皆在區間內的天數 ÷ 該期總天數）")
        if pct:
            c = st.columns(3)
            sp = c[0].number_input("Strike %", 0.0, 200.0, float(d.get("sp", 80)), key=kk("sp"))
            kp = c[1].number_input("KO %", 0.0, 300.0, float(d.get("kp", 100)), key=kk("kp"))
            ip = c[2].number_input("KI %", 0.0, 200.0, float(d.get("ip", 65)), key=kk("ip"))
        old, ts = d.get("targets", []), []
        for i in range(5):
            o = old[i] if i < len(old) else {}
            c = st.columns(5)
            sym = c[0].text_input(f"標的 {i+1} 代號" + ("（必填）" if i == 0 else ""), o.get("symbol", ""), key=kk(f"s{i}")).upper().strip()
            init = c[1].number_input("期初價", 0.0, value=float(o.get("init_price", 0)), key=kk(f"i{i}"))
            if pct:
                s_, k_, i_ = init * sp / 100, init * kp / 100, init * ip / 100
                c[2].caption(f"Strike {s_:,.2f}")
                c[3].caption(f"KO {k_:,.2f}")
                c[4].caption(f"KI {i_:,.2f}")
            else:
                s_ = c[2].number_input("履約價 Strike", 0.0, value=float(o.get("strike_price", 0)), key=kk(f"st{i}"))
                k_ = c[3].number_input("提前出場價 KO", 0.0, value=float(o.get("ko_price", 0)), key=kk(f"ko{i}"))
                i_ = c[4].number_input("下限價 KI", 0.0, value=float(o.get("ki_price", 0)), key=kk(f"ki{i}"))
            if sym:
                t_ = {"symbol": _sym(sym), "init_price": init, "strike_price": round(s_, 4), "ko_price": round(k_, 4), "ki_price": round(i_, 4)}
                if pt_ == "DRA":
                    t_["ra_price"] = round(init * rap / 100, 4)
                    c[1].caption(f"區間下限 {t_['ra_price']:,.2f}")
                ts.append(t_)
        ok = st.form_submit_button("💾 儲存")
    if not ok:
        return None
    steps = []
    if pt_ == "STEP":
        try:
            steps = [float(x) for x in re.split(r"[,，\s]+", steps_txt.strip()) if x]
        except ValueError:
            steps = []
        if len(steps) < 2:
            st.error("Stepdown 需要至少 2 期的 KO 價百分比，例如 100,95,90,85,80,75")
            return None
        for t_ in ts:
            t_["ko_price"] = round(t_["init_price"] * steps[0] / 100, 4)
    if not case_no.strip() or principal <= 0 or not ts or ed <= sd:
        st.error("請確認：案件編號、本金 > 0、至少 1 檔標的、到期日晚於起始日。")
        return None
    if any(t["strike_price"] <= 0 or t["ki_price"] <= 0 or t["ki_price"] > t["strike_price"] for t in ts):
        st.error("每檔標的需填 Strike 與 KI，且 KI 不得高於 Strike。")
        return None
    out = {"case_no": _clean(case_no), "isin": _clean(isin), "pos_name": _clean(name), "principal": principal, "coupon_rate": rate,
           "start_date": str(sd), "end_date": str(ed), "nc": int(nc), "targets": ts, "ccy": ccy_, "ko_obs": ko_obs,
           "ptype": pt_, "ki_type": kt_}
    if pt_ == "STEP":
        out["ko_steps"] = steps
    if pt_ == "DRA":
        out.update(ra_lower_pct=rap, ra_free=int(rfree))
    for k_ in ("obs_dates", "pay_dates", "ra_obs_start"):  # 說明書列出的實際日期，表單不編輯但要保留
        if d.get(k_):
            out[k_] = d[k_]
    if pt_ != "STEP":
        out["ko_steps"] = []
    return out


# ---------- PDF 解析：表格優先 → 純文字列 → 關鍵字，並用價格比例驗證 ----------
KEYS = {
    "init": ["期初評價日之收盤價", "期初評價日收盤價", "初始參考價格", "最初價格", "期初價格", "期初股價", "期初參考價", "初始價格",
             "期初價", "最初價", "進場價"],
    "strike": ["履約價格", "履約價", "轉換價格", "轉換價", "執行價格", "執行價", "行使價"],
    "ko": ["自動提前到期贖回價格", "自動提前到期贖回價", "提前到期贖回價格", "提前到期贖回價", "自動提前出場觸發價格", "自動提前出場觸發價", "自動提前出場界限", "自動提前出場界線", "自動提前出場價格", "自動提前出場價",
           "提前出場觸發價格", "提前出場界限", "提前出場界線", "提前出場價格", "提前出場價", "提前贖回價格", "提前贖回價",
           "KO價格", "KO價", "KO", "敲出價", "敲出"],
    "ki": ["界限價格", "界限價", "界線價格", "界線價", "觸及生效價格", "觸及生效價", "下限觸發價格", "下限觸發價", "觸及下限價", "觸及價格", "觸及價", "觸碰價格", "觸碰價",
           "下限價格", "下限價", "下檔保護價", "保護價格", "障礙價格", "KI價格", "KI價", "KI", "敲入價", "敲入", "保護價", "下限"],
}
LABEL = {"init": "期初價", "strike": "履約價", "ko": "KO 提前出場價", "ki": "KI 下限價"}
NAMES = {"輝達": "NVDA", "甲骨文": "ORCL", "台灣積體電路": "TSM", "台積電": "TSM", "特斯拉": "TSLA", "蘋果": "AAPL", "微軟": "MSFT",
         "亞馬遜": "AMZN", "字母公司": "GOOGL", "字母控股": "GOOGL", "谷歌": "GOOGL", "超微": "AMD", "超威": "AMD", "博通": "AVGO",
         "美光": "MU", "英特爾": "INTC", "高通": "QCOM", "網飛": "NFLX", "臉書": "META", "帕蘭泰爾": "PLTR", "魯門特姆": "LITE",
         "晟碟": "SNDK", "邁威爾": "MRVL", "思科": "CSCO", "賽富時": "CRM", "Marvell": "MRVL", "Alphabet": "GOOGL", "NVIDIA": "NVDA",
         "Oracle": "ORCL", "Tesla": "TSLA", "Apple": "AAPL", "Microsoft": "MSFT", "Amazon": "AMZN", "Micron": "MU",
         "Broadcom": "AVGO", "Intel": "INTC", "Netflix": "NFLX", "Qualcomm": "QCOM", "Palantir": "PLTR"}
D_RE = r"(20\d{2})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})"
TK_RE = re.compile(r"\b([A-Z]{1,5})\s+(?:UW|UN|UQ|UA|UP|UR|UV|UF)\b")
PRICE_CELL = re.compile(r"^\d[\d,]*(?:\.\d+)?(?:美元|USD|元)?$")
NUM_RE = r"\d[\d,]*\.\d{2,}"


def _alt(words):
    return re.compile("|".join(map(re.escape, sorted(words, key=len, reverse=True))), re.I)


def role_of(cell):
    """依儲存格中「最早出現」的關鍵字判斷欄位（避免「為最初價格的60%」被誤判成期初價）"""
    best = None
    for role, ws in KEYS.items():
        m = _alt(ws).search(cell)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), role)
    return best[1] if best else None


def sym_from(name):
    return next((v for k, v in NAMES.items() if k.lower() in name.lower()), "")


def _num(x):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(x or ""))
    return float(m.group().replace(",", "")) if m else 0.0


def _sane(r):
    i = r.get("init", 0)
    if i <= 0:
        return False
    s, k, l = r.get("strike", 0), r.get("ko", 0), r.get("ki", 0)
    return (not s or 0.2 <= s / i <= 1.3) and (not k or 0.5 <= k / i <= 1.5) and (not l or 0.1 <= l / i <= 1.2)


def parse_tables(tables):
    """第一層：用標題判斷欄位；數值依出現順序對應（相容合併儲存格造成的欄位位移）"""
    for tb in tables:
        for hi, row in enumerate(tb):
            cells = [re.sub(r"\s+", "", str(c or "")) for c in row]
            roles = {i: role_of(c) for i, c in enumerate(cells) if c and role_of(c)}
            order = [roles[i] for i in sorted(roles)]
            if "init" not in order or len(set(order)) < 3 or len(set(order)) != len(order):
                continue
            pcts = {}
            for i, r_ in roles.items():
                m = re.search(r"(\d+(?:\.\d+)?)%", cells[i])
                if m:
                    pcts[r_] = float(m.group(1))
            lo, rows = min(roles) - 1, []
            for r2 in tb[hi + 1:]:
                nums = [_num(c) for i, c in enumerate(r2) if i >= lo and c and PRICE_CELL.match(re.sub(r"\s+", "", str(c)))]
                if len(nums) < len(order):
                    continue
                texts = [re.sub(r"\s+", " ", str(c or "")).strip() for c in r2]
                name = next((t for t in texts if re.search(r"[一-鿿A-Za-z]", t) and not TK_RE.search(t)), "")
                tk = next((m.group(1) for t in texts for m in [TK_RE.search(t)] if m), "")
                v = dict(zip(order, nums[:len(order)]))
                if _sane(v):
                    rows.append((name, tk, v))
            if rows:
                return rows[:5], pcts
    return [], {}


TK_ALL = re.compile(r"\b([A-Z]{1,5})\s+(?:UW|UN|UQ|UA|UP|UR|UV|UF)\b|(?<![\d.])(\d{4})\s+JT\b")
CYMAP = {"美元": "USD", "日圓": "JPY", "日幣": "JPY", "歐元": "EUR", "澳幣": "AUD", "人民幣": "CNH", "英鎊": "GBP", "紐幣": "NZD",
         "紐西蘭幣": "NZD", "南非幣": "ZAR", "瑞士法郎": "CHF"}


def _tks(text):
    """彭博代碼 → Yahoo 代號：美股 'NVDA UW' → NVDA；東證 '9984 JT' → 9984.T"""
    return list(dict.fromkeys((a or b + ".T") for a, b in TK_ALL.findall(re.sub(r"\s+", " ", text))))


def parse_pct_table(text):
    """標題只寫「期初價之 X%」、欄位在文字中錯位時：用數值彼此的比例找出每檔標的的期初價，
    再依 KO ≥ Strike ≥ KI 對應百分比"""
    ms = list(re.finditer(r"期初\w{0,4}價\w{0,2}之\s*(\d+(?:\.\d+)?)\s*[%％]", text))
    if not ms:
        return [], {}
    # 標題群：第一個「期初價之 X%」起約 160 字內的所有百分比（換行時有些只剩數字）
    hs = ms[0].start()
    head = text[hs: hs + 160]
    pcts = sorted({float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", head)}, reverse=True)[:3]
    if len(pcts) < 2:
        return [], {}
    hend = hs + [m_.end() for m_ in re.finditer(r"\d+(?:\.\d+)?\s*[%％]", head)][-1]
    cut = re.search(r"註\s*[:：]", text[hend:])
    region = text[hend: hend + (cut.start() if cut else 1500)]
    nums = [float(x) for x in re.findall(r"(?<![\d.])\d+\.\d{2,4}(?![\d.]*\s*[%％])", region)]

    def has(x):
        return any(abs(n - x) <= max(0.0051, abs(x) * 2e-3) for n in nums)

    def find_inits(ps):
        out_ = []
        for v in nums:
            if v not in out_ and all(has(v * p / 100) for p in ps):
                out_.append(v)
        return out_

    # 列式表格（每個價位一整列、百分比寫在列尾）：百分比可能散在 160 字之外，擴大範圍並嘗試三個百分比的組合
    import itertools
    wide = text[hs: hs + 700]
    cand = sorted({float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", wide)}, reverse=True)[:8]
    inits = []
    for n_ in (3, 2):
        for combo in itertools.combinations(cand, n_):
            got = find_inits(combo)
            if got:
                pcts, inits = sorted(combo, reverse=True), got
                break
        if inits:
            break
    if not inits:
        return [], {}
    # 依百分比前最近的欄位名稱決定角色（列式表格：Strike 列、KO 列、KI 列各有自己的百分比）
    roles_by_pct = {}
    for p_ in pcts:
        for mm in re.finditer(r"(?<![\d.])" + re.escape(f"{p_:.2f}") + r"\s*[%％]", wide):
            pre = text[max(0, hs + mm.start() - 260): hs + mm.start()]
            last = None
            for role_, ws_ in KEYS.items():
                for m3 in _alt(ws_).finditer(pre):
                    if role_ != "init" and (last is None or m3.start() > last[0]):
                        last = (m3.start(), role_)
            if last:
                roles_by_pct[p_] = last[1]
                break
    gl = _tks(text)
    names = {}
    for nm, code in re.findall(r"(?m)^\s*(\S.{1,38}?)\s+(\d{4})\s+JT\b", text):
        names[code + ".T"] = nm.strip()
    byval = {}
    for nm, val in re.findall(r"([A-Za-z][A-Za-z .&,]{2,30}?|[一-鿿]{3,20})\s*(\d+\.\d{4})", region):
        nm = re.sub(r"^[日圓美元\s]+", "", nm).strip()
        if nm and not re.search(r"日圓|美元|Initial|Price|Strike|Knock", nm, re.I):
            byval.setdefault(round(float(val), 2), nm)
    roles = ["ko", "strike", "ki"] if len(pcts) >= 3 else ["strike", "ki"]
    if len(pcts) >= 2 and len(roles_by_pct) == len(pcts) and len(set(roles_by_pct.values())) == len(pcts):
        roles = [roles_by_pct[p_] for p_ in pcts]   # 與 pcts（由大到小）一一對應
    rows = []
    for i, init in enumerate(inits[:5]):
        v = {"init": init}
        for r_, p_ in zip(roles, pcts):
            v[r_] = round(init * p_ / 100, 4)
        tk = gl[i] if i < len(gl) else ""
        rows.append((byval.get(round(init, 2)) or names.get(tk, ""), tk, v))
    return rows, dict(zip(roles, pcts))


def _by_ratio(vals):
    """文字模式沒有欄位標題可用時：期初價在前；其餘依大小 KO ≥ Strike ≥ KI"""
    rest = sorted(vals[1:4], reverse=True)
    if len(rest) >= 3:
        return {"init": vals[0], "ko": rest[0], "strike": rest[1], "ki": rest[2]}
    return {"init": vals[0], "strike": rest[0], "ki": rest[1]}


def parse_grid(text):
    """第二層：純文字。同一行出現 ≥3 個小數價格，視為一檔標的的一列"""
    rows = []
    for line in text.split("\n"):
        vals = [float(v.replace(",", "")) for v in re.findall(NUM_RE + r"(?![\d%％])", line)]
        if len(vals) >= 3:
            name = re.sub(NUM_RE + r"|^\s*\d{1,2}\s+|\b(?:UW|UN|UQ)\b|美元|USD|Equity", " ", line)
            rows.append((re.sub(r"\s+", " ", name).strip(), vals))
    for i in range(len(rows)):
        k, run = len(rows[i][1]), []
        for nm, vals in rows[i:]:
            if len(vals) != k:
                break
            run.append((nm, "", _by_ratio(vals)))
        if run and all(_sane(r[2]) for r in run):
            uniq, seen = [], set()
            for r in run:
                if round(r[2]["init"], 2) not in seen:
                    seen.add(round(r[2]["init"], 2))
                    uniq.append(r)
            return uniq[:5], {}
    return [], {}


def grab(text, cat):
    """第三層：關鍵字後面緊接的數值，回傳 [(數值, 是否為百分比), ...]"""
    stop = _alt([w for ws in KEYS.values() for w in ws])
    for m in _alt(KEYS[cat]).finditer(text):
        seg = text[m.end(): m.end() + 120]
        nx = stop.search(seg)
        if nx:
            seg = seg[:nx.start()]
        vals = [(float(v.replace(",", "")), bool(p)) for v, p in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([%％]?)", seg)
                if p or "." in v or float(v.replace(",", "")) >= 10]
        if vals:
            return vals[:5]
    return []


def _pick_date(text, keys):
    for k in keys:
        for m in re.finditer(k + r"[^0-9]{0,45}" + D_RE, text):
            try:
                return date(*map(int, m.groups()))
            except ValueError:
                continue
    return None


def parse_text(text, tables=None):
    text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)  # 修正中文字之間被拆開的空白
    out = {}
    m = re.search(r"ISIN[^A-Z0-9]{0,20}([A-Z]{2}[A-Z0-9]{9}\d)", text) or re.search(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b", text)
    if m:
        out["isin"] = m.group(1)
    m = re.search(r"(?:商品中文名稱|商品名稱)\s*[：:]\s*([^\n]{4,80})", text)
    if m:
        nm = re.split(r"[（(](?:下稱|無擔保|不保本|含)", m.group(1))[0]
        out["pos_name"] = re.sub(r"^[\s‧·•\-]+", "", nm).strip()[:40]
    # 年利率：先找明寫「年利率／年化配息率」，其次「X% x 1/12」，最後月配息率 ×12
    ann = re.search(r"(?:年利率|年化配息率|年化利率|固定年利率|票面利率|年息)[^\d%]{0,12}(\d+(?:\.\d+)?)\s*[%％]", text)
    m12 = re.search(r"(\d+(?:\.\d+)?)\s*[%％]\s*[xX×*]\s*1\s*/\s*12", text)
    mon = re.search(r"每月之?配息率[^\d%]{0,12}(\d+(?:\.\d+)?)\s*[%％]", text)
    if ann:
        out["coupon_rate"] = float(ann.group(1))
    elif m12:
        out["coupon_rate"] = float(m12.group(1))
    elif mon:
        out["coupon_rate"] = round(float(mon.group(1)) * 12, 2)
    # 日期：先找有標籤的，找不到才用全文最早／最晚日期
    ds = []
    for a_, b_, c_ in re.findall(D_RE, text):
        try:
            ds.append(date(int(a_), int(b_), int(c_)))
        except ValueError:
            pass
    ds = sorted(set(ds))
    sd = _pick_date(text, ["起息日", "發行日", "成立日", "生效日", "交易日", "期初評價日"]) or (ds[0] if ds else None)
    ed = _pick_date(text, ["最終到期日", "到期日", "最終評價日", "最後評價日", "期末評價日"]) or (ds[-1] if ds else None)
    if sd and ed and sd < ed:
        out["start_date"], out["end_date"] = str(sd), str(ed)
    # 首次 KO 觀察月數
    CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
    na = [int(t) for t in re.findall(r"(?<!\d)(\d{1,2})\s*N\.?/?A\s*N\.?/?A\s*N\.?/?A", text)]
    na_rows = len(re.findall(r"(?m)^\s*\d{1,2}\s+" + D_RE + r"日?\s+" + D_RE + r"日?\s+N\.?A\s+N\.?A", text))  # 摩根式：觀察期欄位為 N.A 的列數
    mk = re.search(r"[自於從]第\s*([一二三四五六七八\d]+)\s*個(?:配息)?(?:觀察)?期(?:間)?結束日", text)
    if mk:
        v = mk.group(1)
        out["nc"] = int(v) if v.isdigit() else CN_NUM.get(v, 1)
    elif na:
        out["nc"] = max(na) + 1
    elif na_rows:
        out["nc"] = na_rows + 1
    # 計價幣別與 KO 觀察頻率
    mc = re.search(r"計價幣別[：:\s]*([^\n]{0,24})", text)
    if mc:
        iso = re.search(r"\b(USD|JPY|EUR|AUD|CNH|CNY|GBP|NZD|ZAR|CHF)\b", mc.group(1))
        out["ccy"] = ("CNH" if iso.group(1) == "CNY" else iso.group(1)) if iso else next((v for k, v in CYMAP.items() if k in mc.group(1)), "USD")
    if re.search(r"自動提前(?:出場|到期)觀察日[^。]{0,80}?每(?:一個|個)?(?:預定)?(?:交易日|營業日)", text) or \
            re.search(r"自動提前出場決定日[^。]{0,90}?每一個(?:觀察日|規劃交易日|預定交易日)", text):
        out["ko_obs"] = "daily"
    # 商品型態：DRA 區間累計／Stepdown 逐期遞減 KO；KI 類型：EKI／AKI
    if re.search(r"區間累計", text) and re.search(r"累積日數", text):
        out["ptype"] = "DRA"
        mx = re.search(r"即\s*(\d+(?:\.\d+)?)\s*%\s*/\s*12", text)
        if mx:
            out["coupon_rate"] = float(mx.group(1))
        mf = re.search(r"第\s*1\s*[~～\-–]\s*(\d+)\s*個觀察期間[^。\n]{0,12}?最大利率值", text)
        out["ra_free"] = int(mf.group(1)) if mf else 2
        mo = re.search(r"觀察起始日\s*[：:]\s*" + D_RE, text)
        if mo:
            out["ra_obs_start"] = str(date(*map(int, mo.groups())))
    steps = [float(x) for x in re.findall(r"第\s*\d+\s*個(?:配息)?(?:觀察日|月配息)[^\n\d]{0,12}?期初(?:價格|訂價|評價)\w{0,3}之\s*(\d+(?:\.\d+)?)\s*[%％]", text)]
    if len(steps) >= 3 and len(set(steps)) > 1 and "ptype" not in out:
        out["ptype"], out["ko_steps"] = "STEP", steps
    aki = re.search(r"任一(?:預定)?交易日[^。]{0,40}?收盤價低於[^。]{0,14}?(?:下限|界限|界線|KI|障礙|觸及)", text) or \
        re.search(r"(?:期初|訂價|評價)日（?含?）?[^。]{0,24}至[^。]{0,24}任一(?:預定)?交易日", text)
    out["ki_type"] = "AKI" if aki else "EKI"
    # 說明書列出的每期觀察日／給付日（兩個日期一行）
    prs = []
    for pat in (r"第\s*\d+\s*個月配息\s*" + D_RE + r"\s*日?\s*" + D_RE + r"\s*日",
                r"(?m)^\s*\d{1,2}\s+" + D_RE + r"\s*日?\s+" + D_RE + r"\s*日?\s*$"):
        prs = []
        for m in re.finditer(pat, text):
            try:
                prs.append((date(*map(int, m.groups()[:3])), date(*map(int, m.groups()[3:]))))
            except ValueError:
                pass
        if len(prs) >= 3:
            break
    if len(prs) >= 3 and all(prs[i][0] < prs[i + 1][0] for i in range(len(prs) - 1)) and all(a <= b for a, b in prs):
        out["obs_dates"], out["pay_dates"] = [str(a) for a, _ in prs], [str(b) for _, b in prs]
    # 標的價格
    rows, hdr = parse_tables(tables or [])
    if not rows:
        rows, hdr = parse_pct_table(text)
    if not rows or len(rows) < len(_tks(text)):  # 表格跨頁被切斷（後面幾檔沒有標題列）：改用逐列文字補齊
        g_rows, g_hdr = parse_grid(text)
        if rows and g_rows and any(abs(r[2]["init"] - rows[0][2]["init"]) < 0.005 for r in g_rows):
            seen = {round(r[2]["init"], 2) for r in rows}
            rows = rows + [r for r in g_rows if round(r[2]["init"], 2) not in seen]
        elif not rows:
            rows, hdr = g_rows, g_hdr
    if not rows:  # 只有行文敘述：關鍵字後取值
        g = {c: grab(text, c) for c in KEYS}
        ab = {c: [v for v, p in g[c] if not p] for c in KEYS}
        hdr = {c: next((v for v, p in g[c] if p), None) for c in KEYS}
        rows = [("", "", {c: (ab[c][i] if i < len(ab[c]) else 0.0) for c in KEYS}) for i in range(min(5, len(ab["init"])))]
    gl = _tks(text)
    targets = []
    for i, (name, tk, v) in enumerate(rows):
        init = v.get("init", 0.0)
        t = {"name": name[:40], "symbol": tk or sym_from(name) or (gl[i] if i < len(gl) else ""), "init_price": init}
        for c, nm in (("strike", "strike_price"), ("ko", "ko_price"), ("ki", "ki_price")):
            x = v.get(c, 0.0)
            if not x and init and hdr.get(c):  # 只給百分比 → 以期初價換算
                x = round(init * hdr[c] / 100, 4)
            t[nm] = x
        targets.append(t)
    if out.get("ko_steps"):
        for t in targets:
            if t["init_price"] and not t["ko_price"]:
                t["ko_price"] = round(t["init_price"] * out["ko_steps"][0] / 100, 4)
    if out.get("ptype") == "DRA":
        mt = re.search(r"表\s*1\s*彭博代碼[\s\S]{0,400}", text)
        ps = {float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", mt.group(0))} if mt else set()
        sp_ = [t["strike_price"] / t["init_price"] * 100 for t in targets if t["init_price"] and t["strike_price"]]
        sp_ = round(sp_[0], 2) if sp_ else 0
        kp_ = max([x for x in ps if x > sp_] or [0])
        out["ra_lower_pct"] = sp_
        for t in targets:
            if t["init_price"]:
                if not t["ko_price"] and kp_:
                    t["ko_price"] = round(t["init_price"] * kp_ / 100, 4)
                t["ra_price"] = round(t["init_price"] * sp_ / 100, 4)
    for nm, k in (("strike_price", "sp"), ("ko_price", "kp"), ("ki_price", "ip")):
        xs = sorted(t[nm] / t["init_price"] * 100 for t in targets if t["init_price"] and t[nm])
        if xs:
            out[k] = round(xs[len(xs) // 2], 2)
    out["targets"] = targets
    return out


def read_pdf(file):
    """優先用 pdfplumber（表格與中文較準）；沒安裝則退回 pypdf 純文字"""
    import io
    data = file.getvalue() if hasattr(file, "getvalue") else file.read()
    if len(data) > 15_000_000 or not data.startswith(b"%PDF"):
        raise ValueError("檔案不是有效的 PDF，或超過 15MB。")
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts, tables = [], []
            if len(pdf.pages) > 80:
                raise ValueError("PDF 頁數過多（上限 80 頁）。")
            for pg in pdf.pages:
                t = pg.extract_text() or ""
                texts.append(t)
                if re.search("期初|最初|初始|執行價|履約價|轉換價|觸及|觸碰|下限", t):  # 只對可能含價格表的頁面抽表格，加快速度
                    tables += pg.extract_tables()
        return "\n".join(texts).replace(",", ""), tables
    except ImportError:
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages).replace(",", ""), []


def parse_pdf(file):
    text, tables = read_pdf(file)
    return parse_text(text, tables), text


# ---------- 出場 / 入帳 / 損益輔助 ----------
COLORS = ["#2b6cb0", "#2f855a", "#dd6b20", "#6b46c1", "#c53030", "#b7791f", "#2c7a7b", "#b83280"]
METHODS = ["KO提前出場", "到期還本", "承接股票", "其他"]


def ref_date(p):
    """持有天數與配息的截止日：已出場取出場日，否則取今天（不超過到期日）"""
    if p["closed"] and p.get("exit_date"):
        return pd.Timestamp(p["exit_date"])
    return min(TODAY, pd.Timestamp(p["end_date"]))


def received(p):
    """已入帳項目：有手動記錄用記錄；沒有配息記錄時，依日程推算已到期配息"""
    rec = [{"日期": pd.Timestamp(r["date"]), "項目": r["type"], "金額": float(r["amount"])} for r in p["records"]]
    if not any(r["項目"] == "配息" for r in rec):
        rd = ref_date(p)
        rec += [{"日期": d, "項目": "配息", "金額": a} for d, a in schedule(p)[0] if d <= rd]
    return rec


def next_ko(p):
    if p.get("ko_obs") == "daily":
        return max(ko_first(p), TODAY) if TODAY <= pd.Timestamp(p["end_date"]) else None
    ol = obs_list(p)
    return next((d for d in ol[max(p.get("nc", 1), 1) - 1:-1] if d >= TODAY), None)


def nk_str(p):
    n = next_ko(p)
    if n is None:
        return "—"
    return f"每日（自 {ko_first(p).date()}）" if p.get("ko_obs") == "daily" else str(n.date())


def state_text(p):
    return "已出場" if p["closed"] else ("進行中" if pd.Timestamp(p["end_date"]) >= TODAY else "已到期")


def pnl_row(p):
    div = sum(r["金額"] for r in received(p) if r["項目"] == "配息")
    days = max((ref_date(p) - pd.Timestamp(p["start_date"])).days, 0)
    ret = div / p["principal"] * 100
    sym, sh, cost = p.get("assign_symbol", ""), p.get("assign_shares", 0), p.get("assign_cost", 0.0)
    mtm = 0.0
    if sym and sh:
        px = last_px({"symbol": sym}, p)[0]
        mtm = sh * (px - cost) if px else 0.0
    done = not is_active(p)
    return {"ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "型態": PTYPES[ptype(p)].split("（")[0], "KI": ki_type(p),
            "狀態": state_text(p),
            "出場方式": p["closed"] or ("到期還本" if done else "持有中"), "起息日": p["start_date"],
            "出場日": p.get("exit_date") or (p["end_date"] if done else ""), "持有天數": days, "幣別": ccy(p), "名目本金": p["principal"],
            "已領配息": round(div, 2), "已實現損益": round(div, 2), "報酬率(%)": round(ret, 2),
            "年化報酬率(%)": round(ret / days * 365, 2) if days else 0.0,
            "尚未入帳配息": 0.0 if done else round(sum(a for d, a in schedule(p)[0] if d > TODAY), 2),
            "承接標的": sym, "承接股數": sh, "承接股票帳面損益": round(mtm, 2)}


def exit_panel(db, p):
    if p["closed"]:
        st.success(f"已出場：{p['closed']}｜出場日 {p.get('exit_date', '')}｜{p.get('exit_note', '')}")
        if p.get("assign_symbol"):
            st.write(f"承接 {p['assign_symbol']} {p['assign_shares']:,.0f} 股，承接價 {p['assign_cost']:,.2f}")
        if st.button("↩️ 恢復為進行中", key=f"re{p['id']}"):
            p.update(closed="", exit_date="", exit_note="", assign_symbol="", assign_shares=0, assign_cost=0.0)
            p["records"] = [r for r in p["records"] if r.get("note") != "出場自動帶入"]
            save_data(db)
            st.rerun()
        return
    with st.form(f"exit{p['id']}"):
        c = st.columns(3)
        method = c[0].selectbox("出場方式", METHODS)
        xd = c[1].date_input("出場日", date.today())
        note = c[2].text_input("出場條件／備註", placeholder="例：第3次KO評價日，所有標的≥KO價")
        c = st.columns(3)
        sym = c[0].selectbox("承接標的（僅承接股票時需要）", [t["symbol"] for t in p["targets"]])
        sh = c[1].number_input("承接股數", 0.0, step=1.0)
        cost = c[2].number_input("承接價", 0.0)
        if st.form_submit_button("✅ 確認出場"):
            p.update(closed=method, exit_date=str(xd), exit_note=note)
            if method in ("KO提前出場", "到期還本"):
                p["records"].append({"date": str(xd), "type": "還本", "amount": p["principal"], "note": "出場自動帶入"})
            if method == "承接股票" and sh > 0:
                p.update(assign_symbol=sym, assign_shares=sh, assign_cost=cost)
                p["records"].append({"date": str(xd), "type": "承接", "amount": round(sh * cost, 2), "note": "出場自動帶入"})
            save_data(db)
            st.rerun()


def records_panel(db, p):
    st.caption("記錄實際入帳的配息／還本／承接。若沒有任何配息紀錄，客戶端會依日程推算。可在表格末列新增、或選取列後刪除。")
    df = pd.DataFrame(p["records"], columns=["date", "type", "amount", "note"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key=f"rec{p['id']}", column_config={
        "date": st.column_config.DateColumn("日期", required=True),
        "type": st.column_config.SelectboxColumn("項目", options=["配息", "還本", "承接", "其他"], required=True),
        "amount": st.column_config.NumberColumn(f"金額 ({ccy(p)})", min_value=0.0, format="%.2f", required=True),
        "note": st.column_config.TextColumn("備註")})
    b = st.columns(2)
    if b[0].button("💾 儲存紀錄", key=f"sr{p['id']}"):
        p["records"] = [{"date": str(r["date"]), "type": r["type"], "amount": float(r["amount"]),
                         "note": "" if pd.isna(r["note"]) else r["note"]}
                        for r in ed.to_dict("records") if pd.notna(r["date"]) and pd.notna(r["amount"]) and r["type"]]
        save_data(db)
        st.success("已儲存")
    if b[1].button("📥 帶入已到期的日程配息", key=f"ag{p['id']}"):
        rd = ref_date(p)
        p["records"] = [r for r in p["records"] if r["type"] != "配息"] + [
            {"date": str(d.date()), "type": "配息", "amount": round(a, 2), "note": "日程帶入"} for d, a in schedule(p)[0] if d <= rd]
        save_data(db)
        st.rerun()


def ko_events(p, t):
    """標的第一次在 KO 觀察日達標的點（⭐，記憶式鎖定）與整檔 KO 觸發日。"""
    if not p["targets"] or any(not x.get("ko_price") for x in p["targets"]):
        return [], None
    sers = {}
    for x in p["targets"]:
        df = get_hist(x["symbol"], p["start_date"])
        if df is None or df.empty:
            return [], None
        sers[x["symbol"]] = df.Close
    if p.get("ko_obs") == "daily":
        lim = min(TODAY, pd.Timestamp(p["end_date"]))
        obs = [d for d in sers[t["symbol"]].index if ko_first(p) <= d <= lim]
    else:
        obs = [d for i, d in enumerate(obs_list(p)[:-1]) if i >= max(p.get("nc", 1), 1) - 1 and d <= TODAY]
    mine, locked, ko_date, seen = [], {x["symbol"]: False for x in p["targets"]}, None, False
    for d in obs:
        for x in p["targets"]:
            sub = sers[x["symbol"]][sers[x["symbol"]].index <= d]
            if len(sub) and (d - sub.index[-1]).days <= 5 and float(sub.iloc[-1]) >= ko_level(p, x, ko_idx_at(p, d)):
                locked[x["symbol"]] = True
                if x["symbol"] == t["symbol"] and not seen:
                    mine.append((sub.index[-1], float(sub.iloc[-1]), ko_level(p, x, ko_idx_at(p, d)), d))
                    seen = True
        if ko_date is None and all(locked.values()):
            ko_date = d
    return mine, ko_date


def _ma_bb_rsi_macd(c):
    ind = {f"MA{n}": c.rolling(n).mean() for n in (5, 20, 60)}
    m, sd = c.rolling(20).mean(), c.rolling(20).std()
    ind["BBu"], ind["BBl"] = m + 2 * sd, m - 2 * sd
    dl = c.diff()
    up, dn = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(), (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    ind["RSI"] = (100 - 100 / (1 + up / dn.replace(0, float("nan")))).where(dn > 0, 100.0)
    ema = lambda n: c.ewm(span=n, adjust=False).mean()
    ind["MACD"] = ema(12) - ema(26)
    ind["SIG"] = ind["MACD"].ewm(span=9, adjust=False).mean()
    ind["HIST"] = ind["MACD"] - ind["SIG"]
    return ind


TV_EX = {".T": "TSE:", ".HK": "HKEX:", ".TW": "TWSE:", ".TWO": "TPEX:", ".KS": "KRX:", ".L": "LSE:", ".SS": "SSE:", ".SZ": "SZSE:", ".PA": "EURONEXT:", ".DE": "XETR:"}


def tv_symbol(sym):
    """Yahoo 代號 → TradingView 代號；美股直接用代號，TradingView 會自動對應交易所"""
    for suf, ex in TV_EX.items():
        if sym.upper().endswith(suf):
            code = sym[: -len(suf)]
            return ex + (str(int(code)) if suf == ".HK" and code.isdigit() else code)
    return _sym(sym).replace("-", ".")


def tradingview(sym, height):
    # no-referrer：避免瀏覽器把含客戶代碼的網址（?k=...）當作來源網址送給 TradingView
    html = f"""<meta name="referrer" content="no-referrer">
<div style="height:{height}px;width:100%"><div id="tv_c" style="height:{height}px;width:100%"></div></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>new TradingView.widget({{autosize:true,symbol:{json.dumps(tv_symbol(sym))},interval:"D",timezone:"Asia/Taipei",theme:"light",style:"1",locale:"zh_TW",
toolbar_bg:"#f1f3f6",enable_publishing:false,allow_symbol_change:false,withdateranges:true,hide_side_toolbar:false,save_image:true,
studies:["Volume@tv-basicstudies"],container_id:"tv_c"}});</script>"""
    components.html(html, height=height + 6)


def price_chart(p, t, df, kl):
    """走勢圖：收盤線／技術線圖（K 線＋指標）可切換；KO 達標點以 ⭐ 標示"""
    key = f"{p['id']}{t['symbol']}"
    mode = st.radio("圖表", ["📈 價格走勢（含 KO／Strike／KI、⭐）", "🕯️ K 線圖（TradingView）"], horizontal=True,
                    key=f"cm{key}", label_visibility="collapsed")
    if "TradingView" in mode:
        h = st.slider("K 線圖高度（像素）", 400, 900, 620, 20, key=f"tvh{key}")
        st.markdown(f"**{t.get('name') or t['symbol']}（{t['symbol']}）**　｜　期初價 {t['init_price']:,.2f}　｜　KO {kl:,.2f}　｜　"
                    f"Strike {t['strike_price']:,.2f}　｜　KI {t['ki_price']:,.2f}（{ki_type(p)}）")
        tradingview(t["symbol"], h)
        st.caption("TradingView 互動圖：可切換週期（分鐘／小時／日／週）、K 線樣式、畫線與加技術指標。此圖無法畫出 KO／Strike／KI 與 ⭐，"
                   "上方已列出價位；要看價位線與出場星號請切回「價格走勢」。")
        return
    mine, ko_date = ko_events(p, t)
    lines = [("Strike", t["strike_price"], "#b7791f"), ("KO", kl, "#2f855a"), ("KI", t["ki_price"], "#c53030")]
    if ptype(p) == "DRA" and t.get("ra_price") and abs(t["ra_price"] - t["strike_price"]) > 1e-6:
        lines.append(("區間下限", t["ra_price"], "#6b46c1"))
    x0 = pd.Timestamp(p["start_date"]) - pd.Timedelta(days=14)
    if mode.startswith("📈"):
        fig = go.Figure(go.Scatter(x=df.index, y=df.Close, name="收盤價", line=dict(color="#2b6cb0")))
        rows = 1
    else:
        ohlc = get_ohlc(t["symbol"], str((x0 - pd.Timedelta(days=150)).date()))
        if ohlc is None:
            st.warning("無法取得 K 線資料，改顯示價格走勢。")
            fig, rows = go.Figure(go.Scatter(x=df.index, y=df.Close, name="收盤價", line=dict(color="#2b6cb0"))), 1
            mode = "📈"
        else:
            opts = st.multiselect("技術指標", ["均線 MA5/20/60", "布林通道", "成交量", "RSI(14)", "MACD"], ["均線 MA5/20/60", "成交量"], key=f"ti{key}")
            ind = _ma_bb_rsi_macd(ohlc.Close)
            sub_n = [n for n in ("成交量", "RSI(14)", "MACD") if n in opts]
            rows = 1 + len(sub_n)
            fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                                row_heights=[0.62] + [0.38 / max(len(sub_n), 1)] * len(sub_n))
            fig.add_trace(go.Candlestick(x=ohlc.index, open=ohlc.Open, high=ohlc.High, low=ohlc.Low, close=ohlc.Close, name="K 線",
                                         increasing_line_color="#d6453d", decreasing_line_color="#2f9e6b"), row=1, col=1)
            if "均線 MA5/20/60" in opts:
                for n, col in ((5, "#e6a23c"), (20, "#2b6cb0"), (60, "#8e44ad")):
                    fig.add_trace(go.Scatter(x=ohlc.index, y=ind[f"MA{n}"], name=f"MA{n}", line=dict(width=1.2, color=col)), row=1, col=1)
            if "布林通道" in opts:
                for k_, nm in (("BBu", "布林上軌"), ("BBl", "布林下軌")):
                    fig.add_trace(go.Scatter(x=ohlc.index, y=ind[k_], name=nm, line=dict(width=1, dash="dot", color="#718096")), row=1, col=1)
            for i, n in enumerate(sub_n, start=2):
                if n == "成交量":
                    up = ohlc.Close >= ohlc.Open
                    fig.add_trace(go.Bar(x=ohlc.index, y=ohlc.Volume, name="成交量", marker_color=["#d6453d" if u else "#2f9e6b" for u in up]), row=i, col=1)
                elif n == "RSI(14)":
                    fig.add_trace(go.Scatter(x=ohlc.index, y=ind["RSI"], name="RSI", line=dict(color="#2b6cb0", width=1.2)), row=i, col=1)
                    for lv in (30, 70):
                        fig.add_hline(y=lv, line_dash="dot", line_color="#a0aec0", row=i, col=1)
                else:
                    fig.add_trace(go.Bar(x=ohlc.index, y=ind["HIST"], name="MACD 柱", marker_color="#a0aec0"), row=i, col=1)
                    fig.add_trace(go.Scatter(x=ohlc.index, y=ind["MACD"], name="MACD", line=dict(color="#2b6cb0", width=1.2)), row=i, col=1)
                    fig.add_trace(go.Scatter(x=ohlc.index, y=ind["SIG"], name="訊號線", line=dict(color="#e6a23c", width=1.2)), row=i, col=1)
            fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
            fig.update_layout(xaxis_rangeslider_visible=False)
    for n, v, col in lines:
        if v:
            fig.add_hline(y=v, line_dash="dash", line_color=col, annotation_text=f"{n} {v:,.2f}", row=1 if rows > 1 else None, col=1 if rows > 1 else None)
    if mine:
        fig.add_trace(go.Scatter(x=[m[0] for m in mine], y=[m[1] for m in mine], mode="markers", name="⭐ KO 達標",
                                 marker=dict(symbol="star", size=15, color="#f6c90e", line=dict(width=1.2, color="#7a5c00")),
                                 text=[f"觀察日 {m[3].date()}<br>收盤 {m[1]:,.2f} ≥ KO {m[2]:,.2f}" for m in mine],
                                 hovertemplate="%{text}<extra>⭐ KO 達標</extra>"), row=1 if rows > 1 else None, col=1 if rows > 1 else None)
    if ko_date is not None:
        sub = df[df.index <= ko_date]
        if len(sub):
            fig.add_trace(go.Scatter(x=[sub.index[-1]], y=[float(sub.Close.iloc[-1])], mode="markers+text", name="🚩 整檔 KO 出場日",
                                     marker=dict(symbol="star", size=26, color="#e53e3e", line=dict(width=1.5, color="#7b1c1c")),
                                     text=["KO 出場日"], textposition="top center", textfont=dict(color="#e53e3e", size=13),
                                     hovertemplate=f"所有標的達標（記憶式鎖定）<br>{ko_date.date()}<extra>🚩 KO 出場日</extra>"),
                          row=1 if rows > 1 else None, col=1 if rows > 1 else None)
    # 合約期間：觀察日（每月）、KO 觀察期底色、今天、到期日
    sd, ed = pd.Timestamp(p["start_date"]), pd.Timestamp(p["end_date"])
    shapes, notes = [], []
    mark = lambda x, txt, col, dash="dot", w=1: (
        shapes.append(dict(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1, line=dict(color=col, width=w, dash=dash), layer="below")),
        notes.append(dict(x=x, y=1, xref="x", yref="paper", text=txt, showarrow=False, yanchor="bottom", font=dict(size=11, color=col))))
    if p.get("ko_obs") == "daily":
        k0 = ko_first(p)
        shapes.append(dict(type="rect", xref="x", yref="paper", x0=k0, x1=ed, y0=0, y1=1, fillcolor="rgba(47,133,90,0.07)", line=dict(width=0), layer="below"))
        notes.append(dict(x=k0, y=1, xref="x", yref="paper", text="每日 KO 觀察期", showarrow=False, xanchor="left", yanchor="bottom",
                          yshift=16, font=dict(size=11, color="#2f855a")))
    ol = [d for d in obs_list(p) if sd <= d <= ed]
    for i, d in enumerate(ol):
        if abs((d - ed).days) <= 7 and i == len(ol) - 1:
            continue  # 最後一期＝到期日，下面另外標
        mark(d, f"{d.month}月", "#2f855a")
    mark(ed, f"到期 {ed.strftime('%Y-%m-%d')}", "#c53030", "dash", 1.5)
    if sd <= TODAY <= ed:
        mark(TODAY, "今天", "#dd8a00", "solid", 1.5)
    fig.update_layout(shapes=list(fig.layout.shapes) + shapes, annotations=list(fig.layout.annotations) + notes)
    fig.update_layout(template="plotly_white", height=440 if rows == 1 else 400 + 140 * (rows - 1), margin=dict(l=10, r=10, t=56, b=10),
                      legend=dict(orientation="h", y=-0.12, x=0), hovermode="x unified" if mode.startswith("📈") else "closest")
    fig.update_xaxes(range=[sd - pd.Timedelta(days=3), ed + pd.Timedelta(days=6)])
    st.plotly_chart(fig, key=f"pc{key}")
    if mine or ko_date is not None:
        st.caption("⭐ 該標的第一次在 KO 觀察日收盤 ≥ 當期 KO 價（記憶式鎖定，只標第一次）。🚩 紅色大星＝所有標的都已達標（記憶式鎖定）的日期，即可能的提前出場時點，實際以發行商通知為準。")


def target_panel(p, t):
    px, df = last_px(t, p)
    sym, stt = t["symbol"], tstat(p, t, px)
    st.markdown(f"### {sym}　{LIGHT[stt]}")
    chg = 0.0
    if df is not None and len(df) > 1:
        chg = (df.Close.iloc[-1] / df.Close.iloc[-2] - 1) * 100
    d = lambda lv: f"{(px / lv - 1) * 100:+.2f}% vs 現價" if lv and px else None
    kl = ko_level(p, t)
    has_ra = ptype(p) == "DRA" and t.get("ra_price")
    c = st.columns(5 if has_ra else 4)
    c[0].metric("最新收盤價", money(p, px, 2), f"{chg:+.2f}%")
    c[1].metric("Strike", money(p, t["strike_price"], 2), d(t["strike_price"]))
    c[2].metric("KO（本期）" if p.get("ko_steps") else "KO", money(p, kl, 2), d(kl))
    c[3].metric(f"KI（{ki_type(p)}）", money(p, t["ki_price"], 2), d(t["ki_price"]))
    if has_ra:
        c[4].metric("區間下限", money(p, t["ra_price"], 2), d(t["ra_price"]))
    if df is None:
        st.warning("目前無法取得走勢資料。")
    else:
        hit, lo, lod = ki_hit(p, t)
        if ki_type(p) == "AKI":
            if hit:
                st.error(f"🔴 已觸及 KI（AKI）：{lod.date()} 收盤 {lo:,.2f} 低於 KI {t['ki_price']:,.2f}。之後即使股價回升，"
                         f"到期若最差標的低於轉換價／履約價仍會承接股票。")
            else:
                st.caption(f"AKI：合約期間內任一交易日收盤低於 {t['ki_price']:,.2f} 即觸及（目前期間最低收盤 {lo:,.2f}，尚未觸及）。")
        elif px and t["ki_price"] and px < t["ki_price"]:
            st.warning("目前收盤低於 KI；此商品為 EKI，只看期末評價日收盤，之前的下跌不算觸及。")
        elif hit:
            st.caption(f"EKI：期間曾低於 KI（最低收盤 {lo:,.2f}），但只看期末評價日，目前不算觸及。")
        price_chart(p, t, df, kl)
    st.markdown("#### 📰 近一個月相關新聞")
    news = get_news(sym)
    if news:
        for n in news:
            st.markdown(f"- `{n['date']}`　[{n['title']}]({n['link']})　— {n['src']}")
    else:
        st.caption("暫時抓不到新聞清單，可使用下方各媒體連結查看。")
    kw = quote_plus(f"{sym} OR {CN[sym]}" if sym in CN else sym)
    links = [f"[{name}](https://www.google.com/search?q={kw}+site:{dom}&tbm=nws&tbs=qdr:m)" for name, dom in SITES.items()]
    st.markdown("🔎 依媒體搜尋近一個月新聞：" + "　|　".join(links))


PAL = {"JPY": ["#c0392b", "#e67e22", "#d4a017", "#a93f78", "#8e5a2b", "#e17055", "#b33771", "#cc8e35"],
       "USD": ["#2b6cb0", "#1f8a8a", "#5b8def", "#6b46c1", "#2f855a", "#3a9ad9", "#805ad5", "#4a7c59"]}


def cashflow_charts(act):
    cys = list(dict.fromkeys(ccy(p) for p in act))
    for cy in cys:
        if len(cys) > 1:
            st.markdown(f"**幣別：{cy}**")
        _cashflow([p for p in act if ccy(p) == cy], cy)


def _cashflow(act, cy):
    pal = PAL.get(cy, COLORS)
    f1, f2, tot, ret = go.Figure(), go.Figure(), {}, {}
    for i, p in enumerate(act):
        color, nm = pal[i % len(pal)], p["isin"] or p["case_no"]
        m = {}
        for d, a in schedule(p)[0]:
            if d > TODAY:
                k = d.strftime("%Y-%m")
                m[k] = m.get(k, 0) + a
                tot[k] = tot.get(k, 0) + a
        if m:
            f1.add_trace(go.Bar(x=list(m), y=list(m.values()), name=nm, marker_color=color,
                                hovertemplate="%{x}<br>%{y:,.2f}<extra>" + nm + "</extra>"))
        k = pd.Timestamp(p["end_date"]).strftime("%Y-%m")
        ret[k] = ret.get(k, 0) + p["principal"]
        f2.add_trace(go.Bar(x=[k], y=[p["principal"]], name=nm, marker_color=color,
                            text=[f"{p['principal']:,.0f}"], textposition="inside", insidetextanchor="middle",
                            textfont=dict(color="white", size=12),
                            hovertemplate="%{x}<br>%{y:,.0f}<extra>" + nm + "</extra>"))
    ks = sorted(tot)
    dd = 0 if cy == "JPY" else 2
    f1.add_trace(go.Scatter(x=ks, y=[tot[k] for k in ks], mode="text", text=[f"{tot[k]:,.{dd}f}" for k in ks],
                            textposition="top center", textfont=dict(size=12, color="#4a5568"), showlegend=False, hoverinfo="skip"))
    cc = st.columns([3, 2])
    for col, f, title, top, key, n in ((cc[0], f1, f"每月配息（{cy}）", max(tot.values(), default=0), f"cf1{cy}", len(ks)),
                                       (cc[1], f2, f"到期還本（{cy}）", max(ret.values(), default=0), f"cf2{cy}", len(ret))):
        pad = max(0.0, (7 - n) / 2)  # 柱子少時左右補白，避免單根柱子撐滿整個圖
        f.update_layout(template="plotly_white", barmode="stack", bargap=0.45, height=360,
                        title=dict(text=title, font=dict(size=16)), margin=dict(l=10, r=10, t=56, b=10),
                        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                        yaxis=dict(range=[0, max(top, 1) * 1.22], tickformat=",", gridcolor="#e8edf4", zeroline=False),
                        xaxis=dict(type="category", range=[-0.5 - pad, max(n, 1) - 0.5 + pad], showgrid=False),
                        legend=dict(orientation="h", y=1.02, x=1, xanchor="right", yanchor="bottom", font=dict(size=11)),
                        font=dict(family="sans-serif"))
        col.plotly_chart(f, key=key, config={"displayModeBar": False})


def type_panel(p):
    """依商品型態顯示：Stepdown 的 KO 價逐期表；DRA 的各期配息累計"""
    if p.get("ko_steps"):
        ol = obs_list(p)
        i0 = ko_step_idx(p)
        rows = []
        for i, d in enumerate(ol):
            r = {"期別": i + 1, "觀察日": d.date(), "KO 價（期初價 %）": f"{p['ko_steps'][min(i, len(p['ko_steps']) - 1)]:g}%"}
            for t in p["targets"]:
                r[t["symbol"]] = round(ko_level(p, t, i), 2)
            r["狀態"] = "已過" if d < TODAY else ("◀ 下一個觀察日" if i == i0 else "")
            rows.append(r)
        with st.expander("📉 Stepdown：每期 KO 價（逐期遞減）", expanded=True):
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("每個觀察日各標的只要收盤 ≥ 當期 KO 價就「鎖定」（記憶式，不必同一天）；所有標的都鎖定即提前出場，領回本金與當期配息。")
    if ptype(p) == "DRA":
        rows, mc = ra_calc(p)
        df_ = pd.DataFrame([{**r, "期末日": r["期末日"].date(), "給付日": r["給付日"].date()} for r in rows])
        with st.expander("📐 DRA 區間累計：各期配息", expanded=True):
            st.dataframe(df_, hide_index=True, width="stretch",
                         column_config={"配息": st.column_config.NumberColumn(f"配息（{ccy(p)}）", format="%.2f")})
            st.caption(f"每期最高配息 {money(p, mc, 2)}。第 {int(p.get('ra_free', 2))} 期內保證滿額；之後＝最高配息 ×（所有標的收盤都在區間內的天數 ÷ 該期總天數）。"
                       "進行中與未來期別為推估／上限，實際以發行商結算為準。")


def detail_view(p):
    sts = [tstat(p, t, last_px(t, p)[0]) for t in p["targets"]]
    s = next((x for x in "ryg" if x in sts), "n")
    st.caption(f"商品型態：**{PTYPES[ptype(p)]}**　｜　KI 類型：**{KI_TXT[ki_type(p)]}**　｜　"
               f"KO：{'每日觀察' if p.get('ko_obs') == 'daily' else '每期（配息日）觀察'}，記憶式／鎖定股票需依說明書")
    m = st.columns(5)
    m[0].metric("狀態", f"{LIGHT[s].split()[0]} 進行中")
    m[1].metric("到期日", p["end_date"])
    m[2].metric("距到期", f"{(pd.Timestamp(p['end_date']) - TODAY).days} 天")
    m[3].metric("下次KO評價日", nk_str(p))
    m[4].metric("年化配息率", f"{p['coupon_rate']:.2f}%")
    if sts.count("r"):
        st.error(f"🔴 危機：{sts.count('r')} 檔標的最新收盤價已低於 KI 下限價。")
    elif sts.count("y"):
        st.warning(f"🟡 警戒：{sts.count('y')} 檔標的最新收盤價低於履約價，到期有承接股票的風險。")
    elif s == "n":
        st.info("目前取不到行情資料。")
    else:
        st.success("🟢 正常：所有連結標的的最新收盤價都高於履約價。")
    if all_above_ko(p):
        st.info("🎯 目前所有標的都高於 KO 價；若在下次KO評價日仍維持，部位將提前出場。")
    rows, perf = [], []
    for t in p["targets"]:
        px, df = last_px(t, p)
        rel = lambda lv: round((px / lv - 1) * 100, 2) if px and lv else None
        rows.append({"標的": get_name(t["symbol"]), "代號": t["symbol"], "期初價": t["init_price"], "最新收盤": round(px, 2) if px else None,
                     "資料日": str(df.index[-1].date()) if df is not None else "—", "KO價": ko_level(p, t), "STRIKE": t["strike_price"],
                     "KI價": t["ki_price"], "相對KO(%)": rel(ko_level(p, t)), "相對STRIKE(%)": rel(t["strike_price"]),
                     "相對KI(%)": rel(t["ki_price"])})
        if px and t["init_price"]:
            perf.append((t["symbol"], px / t["init_price"] - 1))
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    type_panel(p)
    st.caption("相對 KO／STRIKE／KI(%) ＝ 最新收盤相對於該價位的漲跌幅；相對 KO 為負代表尚未達 KO。")
    if perf:
        w = min(perf, key=lambda x: x[1])
        st.caption(f"最差表現標的（Worst-of）：{w[0]}，較期初 {w[1] * 100:+.2f}%")
    for tab, t in zip(st.tabs([t["symbol"] for t in p["targets"]]), p["targets"]):
        with tab:
            target_panel(p, t)


def pnl_view(allp):
    st.markdown("### 各 FCN 組合損益（含進行中與已出場）")
    st.dataframe(pd.DataFrame([pnl_row(p) for p in allp]), hide_index=True, width="stretch")
    st.caption("已實現損益 ＝ 已入帳配息；本金還回、或轉為股票的成本不計入損益。報酬率 ＝ 已領配息 ÷ 名目本金，年化報酬率依持有天數推算（進行中者算到今天）。"
               "承接股票帳面損益 ＝ 股數 ×（最新收盤 − 承接價），未計匯率與交易成本。")


def dividend_view(allp):
    st.markdown("### 已入帳明細（配息／還本／承接）")
    rows = [{"日期": r["日期"].date(), "ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "幣別": ccy(p), "項目": r["項目"], "金額": round(r["金額"], 2)}
            for p in allp for r in received(p)]
    divs = [r for r in rows if r["項目"] == "配息"]
    cys = list(dict.fromkeys(ccy(p) for p in allp))
    tdiv = {cy: sum(r["金額"] for r in divs if r["幣別"] == cy) for cy in cys}
    tcap = {cy: sum(p["principal"] for p in allp if ccy(p) == cy) for cy in cys}
    m = st.columns(3)
    stack_metric(m[0], "💰 累計實際已領配息（全部部位）", [(cy, tdiv[cy]) for cy in cys])
    m[1].metric("配息筆數", len(divs))
    m[2].metric("最近一次配息", f"{max(r['日期'] for r in divs)}" if divs else "—")
    per = []
    for p in allp:
        d = sum(r["金額"] for r in received(p) if r["項目"] == "配息")
        per.append({"ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "狀態": state_text(p), "幣別": ccy(p), "名目本金": p["principal"],
                    "累計已領配息": round(d, 2), "占累計配息(%)": round(d / tdiv[ccy(p)] * 100, 1) if tdiv[ccy(p)] else 0.0,
                    "配息報酬率(%)": round(d / p["principal"] * 100, 2) if p["principal"] else 0.0})
    st.markdown("**各部位累計已領配息**")
    pdf_ = pd.DataFrame(per)
    tots = [{"ISIN": f"合計 ({cy})", "商品": "", "狀態": "", "幣別": cy, "名目本金": tcap[cy], "累計已領配息": round(tdiv[cy], 2),
             "占累計配息(%)": 100.0 if tdiv[cy] else 0.0, "配息報酬率(%)": round(tdiv[cy] / tcap[cy] * 100, 2) if tcap[cy] else 0.0}
            for cy in cys]
    st.dataframe(pd.concat([pdf_, pd.DataFrame(tots)], ignore_index=True), hide_index=True, width="stretch")
    if rows:
        df = pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)
        df["累計配息"] = df["金額"].where(df["項目"] == "配息", 0).groupby(df["幣別"]).cumsum().round(2)  # 逐筆累計（只計配息，依幣別分開）
        st.markdown("**入帳明細（新到舊；「累計配息」為截至該筆的全部部位配息累計）**")
        st.dataframe(df.sort_values("日期", ascending=False, kind="stable"), hide_index=True, width="stretch",
                     column_config={"金額": st.column_config.NumberColumn("金額", format="%.2f"),
                                    "累計配息": st.column_config.NumberColumn("累計配息", format="%.2f")})
    else:
        st.caption("尚無入帳紀錄。")
    st.caption("若無手動配息紀錄，配息依說明書日程推算，實際入帳以券商對帳單為準。")


def client_view(c):
    st.title("📊 FCN 專屬即時回報")
    st.caption(f"{c['name']} 您好，進行中的部位顯示於上方；已出場或到期的部位保留在下方損益與入帳明細。")
    allp = c["positions"]
    mine = [(p, t, l) for p in allp for t in p["targets"] for l in t.get("split_log", [])
            if (TODAY - pd.Timestamp(l["date"].split("、")[-1])).days <= 90]
    for p, t, l in mine:
        st.info(f"📌 {t['symbol']} 於 {l['date']} {split_text(l['ratio'])}，"
                f"系統已自動將 {p['isin'] or p['case_no']} 的期初價、Strike、KO、KI 依比例換算，股價與線圖皆為拆股後價格。")
    if not allp:
        st.info("目前沒有部位資料。")
        return
    cys_all = list(dict.fromkeys(ccy(p) for p in allp))
    if len(cys_all) > 1:
        sel = st.radio("幣別", cys_all, horizontal=True, key="cv_ccy")
        allp = [p for p in allp if ccy(p) == sel]
    act = [p for p in allp if is_active(p)]
    nm = lambda p: p["isin"] or p["case_no"]
    t_main, t_div = st.tabs(["📊 部位與現金流", "💵 配息與入帳"])
    with t_main:
        if not act:
            st.info("目前沒有進行中的部位。")
        else:
            sts = [pos_status(p) for p in act]
            m = st.columns([1, 2, 2, 1.4])
            m[0].metric("進行中部位", len(act))
            stack_metric(m[1], "總本金", [(ccy(p), p["principal"]) for p in act], 0)
            stack_metric(m[2], "每月配息", [(ccy(p), schedule(p)[1]) for p in act])
            m[3].metric("燈號", f"🟢{sts.count('g')} 🟡{sts.count('y')} 🔴{sts.count('r')}")
            st.markdown("#### 💰 未來現金流（依部位分色）")
            cashflow_charts(act)
            if any(ptype(p) == "DRA" for p in act):
                st.caption("DRA 區間累計配息：進行中與未來期別以最高配息或迄今達標率推估，實際金額依各期達標天數而定。")
            fl = [{"日期": d.date(), "部位": nm(p), "幣別": ccy(p), "類型": "配息", "金額": round(a, 2)} for p in act for d, a in schedule(p)[0] if d > TODAY]
            fl += [{"日期": pd.Timestamp(p["end_date"]).date(), "部位": nm(p), "幣別": ccy(p), "類型": "到期還本", "金額": p["principal"]} for p in act]
            with st.expander("現金流明細"):
                st.dataframe(pd.DataFrame(fl).sort_values("日期"), hide_index=True, width="stretch")
            st.markdown("---")
            st.markdown("### 🔎 FCN 組合明細")
            opts = {f"{LIGHT[pos_status(p)].split()[0]} {nm(p)}｜{p['pos_name']}": p for p in act}
            detail_view(opts[st.selectbox("選擇 ISIN CODE", list(opts))])
        st.markdown("---")
        pnl_view(allp)
    with t_div:
        dividend_view(allp)


# ---------- 營業員註冊 / 登入（多營業員模式）----------
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


PW_ITER = 600_000  # PBKDF2 迭代次數；舊帳號（200,000）登入成功時會自動升級
WEAK_PW = {"123456", "1234567", "12345678", "123456789", "654321", "123123", "000000", "111111", "666666", "888888", "999999", "qwerty",
           "password", "password1", "qwertyui", "abc123", "abc12345", "11111111", "00000000", "88888888", "iloveyou"}
PW_MIN, PW_MAX, MAX_FAILS, TEMP_HOURS = 6, 12, 3, 24


def _hash_pw(pw, salt_hex, it=None):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), it or PW_ITER).hex()


def pw_problem(pw, code=""):
    if not PW_MIN <= len(pw) <= PW_MAX:
        return f"密碼長度須為 {PW_MIN}～{PW_MAX} 碼。"
    if pw.lower() in WEAK_PW or len(set(pw)) < 3:
        return "這個密碼太容易被猜到，請換一個。"
    if code and len(code) >= 3 and (code.lower() in pw.lower()):
        return "密碼不要包含營業員代號。"
    return None


def thr_check(name, session_only=False):
    """全域節流（存在資料庫，換瀏覽器或重開頁面也無法重置）。回傳錯誤訊息或 None。"""
    try:
        if session_only or not _sb():
            ls = st.session_state.get(f"thr_{name}", {})
        else:
            ls = sb_get(f"sys:{name}") or {}
        if ls.get("locked_until") and pd.Timestamp.now("UTC") < pd.Timestamp(ls["locked_until"]):
            return "嘗試次數過多，已暫時鎖定，請 15 分鐘後再試。"
    except Exception as e:
        _why(e)
        return "暫時無法驗證，請稍後再試。"
    return None


def thr_fail(name, limit=5, minutes=15, session_only=False):
    time.sleep(1.0)  # 拖慢連續猜測
    try:
        ls = (st.session_state.get(f"thr_{name}", {}) if (session_only or not _sb()) else (sb_get(f"sys:{name}") or {}))
        ls["fails"] = ls.get("fails", 0) + 1
        if ls["fails"] >= limit:
            ls = {"fails": 0, "locked_until": (pd.Timestamp.now("UTC") + pd.Timedelta(minutes=minutes)).isoformat()}
        if session_only or not _sb():
            st.session_state[f"thr_{name}"] = ls
        else:
            sb_put(f"sys:{name}", ls)
    except Exception as e:
        _why(e)


def thr_ok(name, session_only=False):
    try:
        if session_only or not _sb():
            st.session_state.pop(f"thr_{name}", None)
        else:
            if (sb_get(f"sys:{name}") or {}).get("fails"):
                sb_put(f"sys:{name}", {"fails": 0, "locked_until": ""})
    except Exception as e:
        _why(e)


def _secret(name):
    try:
        return str(st.secrets[name])
    except Exception:
        return os.environ.get(name, "")


def rep_register(code, pw, pw2, invite):
    inv = _secret("INVITE_CODE")
    if not inv:
        return "目前尚未開放註冊，請聯絡系統管理員。"
    lk = thr_check("invite")
    if lk:
        return lk
    if not hmac.compare_digest(invite.strip().encode("utf-8"), inv.encode("utf-8")):
        thr_fail("invite", limit=8, minutes=30)
        return "邀請碼錯誤。"
    thr_ok("invite")
    code = code.strip()
    if not CODE_RE.match(code):
        return "營業員代號請用 3～20 個英數字、底線或減號。"
    if pw_problem(pw, code):
        return pw_problem(pw, code)
    if pw != pw2:
        return "兩次輸入的密碼不一致。"
    key = f"acct:{code.lower()}"
    try:
        if sb_get(key) is not None:
            return "這個代號已被使用，請換一個。"
        salt = secrets.token_hex(16)
        sb_put(key, {"code": code, "salt": salt, "iter": PW_ITER, "hash": _hash_pw(pw, salt), "created": str(date.today()),
                     "fails": 0, "locked_until": ""})
    except Exception as e:
        return "註冊失敗。" + _why(e)
    st.session_state.rep = code.lower()
    return None


def rep_login(code, pw):
    code = code.strip().lower()
    bad = "代號或密碼錯誤。"
    try:
        a = sb_get(f"acct:{code}") if CODE_RE.match(code) else None
        if a is None:
            _hash_pw(pw, "00" * 16)  # 帳號不存在時也做一次雜湊，避免用回應時間猜帳號
            time.sleep(1.0)
            return bad
        if a.get("locked") or (a.get("locked_until") and pd.Timestamp.now("UTC") < pd.Timestamp(a["locked_until"])):
            return "此帳號因連續輸入錯誤已鎖定，請聯絡管理員重設密碼。"
        if hmac.compare_digest(_hash_pw(pw, a["salt"], a.get("iter", 200_000)).encode("utf-8"), a["hash"].encode("utf-8")):
            if a.get("must_change") and a.get("temp_until") and pd.Timestamp.now("UTC") > pd.Timestamp(a["temp_until"]):
                return "臨時密碼已過期（有效 24 小時），請聯絡管理員重新重設。"
            if a.get("iter", 200_000) < PW_ITER:  # 舊雜湊強度自動升級
                a["salt"] = secrets.token_hex(16)
                a["iter"], a["hash"] = PW_ITER, _hash_pw(pw, a["salt"])
            a["fails"], a["locked_until"] = 0, ""
            a["last_login"] = pd.Timestamp.now("UTC").isoformat()
            sb_put(f"acct:{code}", a)
            st.session_state.rep = code
            st.session_state.force_pw = bool(a.get("must_change"))
            st.session_state.last_active = time.time()
            return None
        time.sleep(1.0)
        a["fails"] = a.get("fails", 0) + 1
        if a["fails"] >= MAX_FAILS:
            a["fails"], a["locked"] = 0, True
            sb_put(f"acct:{code}", a)
            return f"密碼連續錯誤 {MAX_FAILS} 次，帳號已鎖定，請聯絡管理員重設密碼。"
        sb_put(f"acct:{code}", a)
        return bad
    except Exception as e:
        return "登入失敗。" + _why(e)


def rep_change_pw(code, old, new):
    if pw_problem(new, code):
        return pw_problem(new, code)
    err = rep_login(code, old)
    if err:
        return "目前密碼不正確。" if err.startswith("代號或密碼") else err
    a = sb_get(f"acct:{code}")
    a["salt"] = secrets.token_hex(16)
    a["iter"], a["hash"] = PW_ITER, _hash_pw(new, a["salt"])
    a["must_change"], a["temp_until"], a["locked"] = False, "", False
    sb_put(f"acct:{code}", a)
    st.session_state.force_pw = False
    return None


def force_change_gate():
    """管理員重設後的臨時密碼（＝營業員代號）登入時，必須先設定自己的新密碼才能使用系統"""
    rep = st.session_state.get("rep")
    if not (multi_mode() and rep and st.session_state.get("force_pw")):
        return
    st.title("🔑 請先變更密碼")
    st.warning("您目前使用的是管理員重設的臨時密碼。為了帳號安全，請立刻設定只有您自己知道的新密碼，完成後才能使用系統。")
    with st.form("force_pw_form"):
        n1 = st.text_input("新密碼（6～12 碼，不可與營業員代號相同或包含代號）", type="password")
        n2 = st.text_input("再輸入一次新密碼", type="password")
        if st.form_submit_button("設定新密碼並進入系統", type="primary"):
            err = pw_problem(n1, rep) or (None if n1 == n2 else "兩次輸入的密碼不一致。")
            if err:
                st.error(err)
            else:
                try:
                    a = sb_get(f"acct:{rep}")
                    a["salt"] = secrets.token_hex(16)
                    a["iter"], a["hash"] = PW_ITER, _hash_pw(n1, a["salt"])
                    a["must_change"], a["temp_until"], a["locked"], a["fails"] = False, "", False, 0
                    sb_put(f"acct:{rep}", a)
                    st.session_state.force_pw = False
                    st.rerun()
                except Exception as e:
                    st.error("儲存失敗。" + _why(e))
    if st.button("登出"):
        for k_ in ("rep", "force_pw", "auth"):
            st.session_state.pop(k_, None)
        st.rerun()
    st.stop()


def master_panel():
    pw = admin_pw()
    if not pw:
        st.info("尚未設定 ADMIN_PASSWORD，無法使用總管理。")
        return
    if not st.session_state.get("master"):
        with st.form("mst"):
            inp = st.text_input("總管理密碼（ADMIN_PASSWORD）", type="password")
            if st.form_submit_button("進入總管理"):
                lk = thr_check("master")
                if lk:
                    st.error(lk)
                elif hmac.compare_digest(inp.encode("utf-8"), pw.encode("utf-8")):
                    thr_ok("master")
                    st.session_state.master = True
                    st.session_state.last_active = time.time()
                    st.rerun()
                else:
                    thr_fail("master", limit=5, minutes=15)
                    st.error("密碼錯誤。")
        return
    top = st.columns([4, 1])
    top[0].success("總管理模式：可查看使用概況、重設密碼（臨時密碼）、刪除帳號（看不到同事的客戶姓名與部位內容）。")
    if top[1].button("離開總管理", width="stretch"):
        st.session_state.master = False
        st.rerun()
    try:
        accts = sb_list("acct:", with_data=True)
        reps = {r["id"][4:]: r for r in sb_list("rep:", with_data=True)}
    except Exception as e:
        st.error("讀取雲端資料庫失敗。" + _why(e))
        return
    now = pd.Timestamp.now("UTC")
    rows = []
    for r in accts:
        code, a = r["id"][5:], r["data"]
        rp = reps.get(code)
        nc_ = npos = nact = 0
        if rp:
            for cl in (rp.get("data") or {}).values():
                if isinstance(cl, dict):
                    nc_ += 1
                    ps = cl.get("positions", [])
                    npos += len(ps)
                    nact += sum(1 for x in ps if is_active(x))
        upd = str(rp.get("updated_at", ""))[:16].replace("T", " ") if rp else ""
        locked = bool(a.get("locked")) or (bool(a.get("locked_until")) and now < pd.Timestamp(a["locked_until"]))
        rows.append({"營業員代號": a.get("code", code), "註冊日": str(a.get("created", ""))[:10],
                     "最後登入": str(a.get("last_login", ""))[:16].replace("T", " ") or "—", "最後資料更新": upd or "—",
                     "客戶數": nc_, "部位數": npos, "進行中部位": nact, "狀態": "🔒 已鎖定（待重設）" if locked else ("🔑 臨時密碼待變更" if a.get("must_change") else "✅ 正常"),
                     "_upd": upd, "_key": code})
    df = pd.DataFrame(rows)
    wk = (now - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    m = st.columns(5)
    m[0].metric("營業員帳號", len(df))
    m[1].metric("7 天內有活動", int((df["_upd"] >= wk).sum()) if len(df) else 0)
    m[2].metric("客戶總數", int(df["客戶數"].sum()) if len(df) else 0)
    m[3].metric("進行中部位", int(df["進行中部位"].sum()) if len(df) else 0)
    m[4].metric("🔒 待重設密碼", int(df["狀態"].str.startswith("🔒").sum()) if len(df) else 0)
    weak = [n_ for n_, mn in (("ADMIN_PASSWORD", 12), ("INVITE_CODE", 8)) if len(_secret(n_)) < mn]
    if weak:
        st.warning("⚠️ 安全提醒：" + "、".join(weak) + " 太短，建議改成更長的隨機字串（Streamlit Secrets 內修改）。")
    st.caption(f"邀請碼：{'已設定（要更換請到 Streamlit Secrets 修改 INVITE_CODE）' if _secret('INVITE_CODE') else '⚠️ 尚未設定，任何人都可註冊'}　｜　雲端資料庫：連線正常（共 {len(accts) + len(reps)} 筆）")
    if df.empty:
        st.info("目前還沒有人註冊。")
        return
    q = st.text_input("搜尋營業員代號", key="mq")
    v = df[df["營業員代號"].str.contains(q.strip(), case=False)] if q.strip() else df
    st.dataframe(v.drop(columns=["_upd", "_key"]).sort_values("最後資料更新", ascending=False), hide_index=True, width="stretch")
    st.markdown("#### 帳號管理")
    sel = st.selectbox("選擇營業員", list(df["_key"]), format_func=lambda k_: f"{k_}（{df.loc[df['_key'] == k_, '狀態'].iloc[0]}）", key="msel")
    r = next(x for x in accts if x["id"] == f"acct:{sel}")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**重設密碼（臨時密碼）**")
        st.caption(f"臨時密碼＝該營業員代號「{sel}」，{TEMP_HOURS} 小時內有效；對方用它登入後會被強制變更成自己的新密碼。同時會解除鎖定。")
        if st.button("重設為臨時密碼", key=f"mrp{sel}", type="primary"):
            a = r["data"]
            a["salt"] = secrets.token_hex(16)
            a["iter"], a["hash"] = PW_ITER, _hash_pw(str(a.get("code", sel)), a["salt"])
            a["fails"], a["locked_until"], a["locked"] = 0, "", False
            a["must_change"] = True
            a["temp_until"] = (pd.Timestamp.now("UTC") + pd.Timedelta(hours=TEMP_HOURS)).isoformat()
            sb_put(r["id"], a)
            st.success(f"已重設。請通知該營業員用營業員代號「{a.get('code', sel)}」當密碼登入，系統會要求他立刻設定新密碼。")
            st.rerun()
    with c2.container(border=True):
        st.markdown("**刪除帳號**（連同全部客戶資料，無法復原）")
        typed = st.text_input(f"請輸入「{sel}」確認刪除", key=f"mdc{sel}")
        if st.button("🗑️ 刪除帳號", key=f"mdl{sel}", type="primary", disabled=typed.strip().lower() != sel.lower()):
            sb_del(r["id"])
            sb_del(f"rep:{sel}")
            st.rerun()


def rep_gate():
    if st.session_state.get("rep"):
        return
    st.title("🔒 FCN 營業員系統")
    if st.session_state.pop("expired", False):
        st.warning("閒置時間過長，為了安全已自動登出，請重新登入。")
    tl, tr, tm = st.tabs(["登入", "註冊新帳號", "總管理"])
    with tl:
        with st.form("rep_login"):
            c_ = st.text_input("營業員代號")
            p_ = st.text_input("密碼", type="password")
            if st.form_submit_button("登入", type="primary"):
                err = rep_login(c_, p_)
                if err:
                    st.error(err)
                else:
                    for _k in ("split_shown", "ko_shown"):
                        st.session_state.pop(_k, None)
                    st.rerun()
    with tr:
        st.caption("需要邀請碼才能註冊。請向系統管理員索取。註冊後只能看到自己的客戶資料。")
        with st.form("rep_reg"):
            inv = st.text_input("邀請碼", type="password")
            c2 = st.text_input("設定營業員代號（3～20 個英數字）")
            p2 = st.text_input("設定密碼（6～12 碼）", type="password")
            p3 = st.text_input("再輸入一次密碼", type="password")
            if st.form_submit_button("註冊並登入"):
                err = rep_register(c2, p2, p3, inv)
                if err:
                    st.error(err)
                else:
                    for _k in ("split_shown", "ko_shown"):
                        st.session_state.pop(_k, None)
                    st.rerun()
    with tm:
        master_panel()
    st.stop()


@st.cache_data(ttl=60, show_spinner=False)
def _token_map():
    m = {}
    for r in sb_list("rep:", with_data=True):
        for x in (r.get("data") or {}).values():
            if isinstance(x, dict) and x.get("token"):
                m[str(x["token"])] = r["id"][4:]
    return m


def find_token_owner(token):
    """舊式連結（沒有 u 參數）：找出這個客戶代碼屬於哪位營業員（60 秒快取，避免被拿來狂刷資料庫）"""
    try:
        m = _token_map()
    except Exception as e:
        _why(e)
        return None
    hit = None
    for tk, o in m.items():
        if hmac.compare_digest(tk.encode("utf-8"), str(token).encode("utf-8")):
            hit = o
    return hit


def _idle_mins():
    try:
        return int(_secret("SESSION_MINUTES") or 30)
    except ValueError:
        return 30


def link_gate(cl, db, owner):
    """客戶連結若設了開啟密碼：輸入正確才顯示內容；錯 3 次鎖 30 分鐘；閒置超過時間需重新輸入"""
    ok_key = "cvok_" + hashlib.sha256(cl["token"].encode("utf-8")).hexdigest()[:12]
    t0 = st.session_state.get(ok_key)
    if t0 and time.time() - t0 < _idle_mins() * 60:
        st.session_state[ok_key] = time.time()
        return
    st.session_state.pop(ok_key, None)
    st.title("🔐 FCN 客戶專屬頁面")
    lp, now = cl["link_pw"], pd.Timestamp.now("UTC")
    if cl.get("lp_locked_until") and now < pd.Timestamp(cl["lp_locked_until"]):
        st.error("密碼輸入錯誤次數過多，已暫時鎖定 30 分鐘。請稍後再試，或聯絡您的營業員。")
        st.stop()
    with st.form("lp_form"):
        pw = st.text_input("請輸入連結密碼（由您的營業員提供）", type="password")
        if st.form_submit_button("進入", type="primary"):
            if hmac.compare_digest(_hash_pw(pw, lp["salt"], lp.get("iter", PW_ITER)).encode("utf-8"), lp["hash"].encode("utf-8")):
                if cl.get("lp_fails"):
                    cl["lp_fails"] = 0
                    save_data(db, quiet=True, owner=owner)
                st.session_state[ok_key] = time.time()
                st.rerun()
            else:
                time.sleep(1.0)
                cl["lp_fails"] = cl.get("lp_fails", 0) + 1
                if cl["lp_fails"] >= MAX_FAILS:
                    cl["lp_fails"], cl["lp_locked_until"] = 0, (now + pd.Timedelta(minutes=30)).isoformat()
                    st.error("密碼連續錯誤 3 次，已鎖定 30 分鐘。請聯絡您的營業員。")
                else:
                    st.error("密碼錯誤。")
                save_data(db, quiet=True, owner=owner)
    st.stop()


def idle_guard():
    """閒置超過 SESSION_MINUTES（預設 30 分鐘）自動登出"""
    mins = _idle_mins()
    now, last = time.time(), st.session_state.get("last_active")
    if last and now - last > mins * 60 and (st.session_state.get("rep") or st.session_state.get("auth") or st.session_state.get("master")):
        for k_ in ("rep", "auth", "master", "split_shown", "ko_shown"):
            st.session_state.pop(k_, None)
        st.session_state.expired = True
    st.session_state.last_active = now


# ---------- 管理後台 ----------
def admin_pw():
    try:
        return st.secrets["ADMIN_PASSWORD"]
    except Exception:
        return os.environ.get("ADMIN_PASSWORD", "")


def base_url():
    try:
        return st.secrets["BASE_URL"].rstrip("/")
    except Exception:
        pass
    try:
        h = st.context.headers.get("Host", "localhost:8501")
    except Exception:
        h = "localhost:8501"
    return ("http://" if "localhost" in h else "https://") + h


def ko_candidates(db):
    """找出進行中、已有 KO 評價日達標但尚未標記出場的部位。
    同日全部標的收盤 ≥ KO 價 → 「同日全部達標」；各標的曾在評價日各自達標過 → 「累計達標（記憶式）」。"""
    out = []
    for acct, c in db.items():
        for p in c["positions"]:
            if not is_active(p) or not p["targets"] or any(not t.get("ko_price") for t in p["targets"]):
                continue
            ser = {}
            for t in p["targets"]:
                df = get_hist(t["symbol"], p["start_date"])
                if df is not None and not df.empty:
                    ser[t["symbol"]] = df.Close
            if len(ser) < len(p["targets"]):
                continue
            if p.get("ko_obs") == "daily":
                lim = min(TODAY, pd.Timestamp(p["end_date"]))
                obs = [d for d in next(iter(ser.values())).index if ko_first(p) <= d <= lim]
            else:
                obs = [d for i, d in enumerate(obs_list(p)[:-1]) if i >= max(p.get("nc", 1), 1) - 1 and d <= TODAY]
            if not obs:
                continue
            dis = p.get("ko_dismissed", [])
            hit = {t["symbol"]: False for t in p["targets"]}
            for d in obs:
                row = {}
                for t in p["targets"]:
                    s = ser[t["symbol"]]
                    sub = s[s.index <= d]
                    row[t["symbol"]] = float(sub.iloc[-1]) if len(sub) and (d - sub.index[-1]).days <= 5 else None
                    if row[t["symbol"]] is not None and row[t["symbol"]] >= ko_level(p, t, ko_idx_at(p, d)):
                        hit[t["symbol"]] = True
                strict = all(row[t["symbol"]] is not None and row[t["symbol"]] >= ko_level(p, t, ko_idx_at(p, d)) for t in p["targets"])
                kind = "strict" if strict else ("memory" if all(hit.values()) else None)
                if not kind:
                    continue
                if any(x["date"] == str(d.date()) for x in dis) or (kind == "memory" and any(x["kind"] == "memory" for x in dis)):
                    continue
                out.append((acct, c, p, d, kind, row))
                break
    return out


@st.dialog("🎯 可能已觸發 KO 提前出場", width="large")
def ko_dialog(db):
    items = ko_candidates(db)
    if not items:
        st.success("目前沒有待確認的 KO 部位。")
        return
    st.caption("依 KO 評價日收盤價判斷，實際是否提前出場請以發行商通知為準。按「確認出場」會標記已出場並記錄還本；"
               "按「略過」代表先不處理，不會再為同一情況提醒。")
    for acct, c, p, d, kind, row in items:
        with st.container(border=True):
            st.markdown(f"**{acct} {c['name']}｜{p['isin'] or p['case_no']}｜{p['pos_name']}**")
            st.write(f"評價日 {d.date()}（{'同日全部標的達標' if kind == 'strict' else '各標的累計達標（記憶式）'}）")
            st.dataframe(pd.DataFrame([{"標的": t["symbol"], "評價日收盤": None if row[t["symbol"]] is None else round(row[t["symbol"]], 2),
                                        "KO 價": ko_level(p, t, ko_idx_at(p, d))} for t in p["targets"]]), hide_index=True, width="stretch")
            b = st.columns(2)
            if b[0].button("✅ 確認出場", key=f"koy{p['id']}", type="primary"):
                p.update(closed="KO提前出場", exit_date=str(d.date()),
                         exit_note=f"{d.date()} KO評價日（{'同日全部達標' if kind == 'strict' else '累計達標'}），系統偵測並由營業員確認")
                p["records"].append({"date": str(d.date()), "type": "還本", "amount": p["principal"], "note": "出場自動帶入"})
                save_data(db)
                st.rerun()
            if b[1].button("略過（先不處理）", key=f"kon{p['id']}"):
                p.setdefault("ko_dismissed", []).append({"date": str(d.date()), "kind": kind})
                save_data(db)
                st.rerun()


@st.dialog("⚠️ 偵測到標的拆股，已自動換算", width="large")
def split_dialog(db):
    notes = split_notes(db, only_unack=True)
    st.write("以下部位的標的在持有期間發生拆股（或反向拆股）。Yahoo 的股價已還原為拆股後價格，"
             "系統已自動把期初價、Strike、KO、KI 依拆股比例換算，燈號與線圖都以換算後為準。")
    rows = []
    for acct, c, p, t, l in notes:
        o, n = l["old"], l["new"]
        rows.append({"客戶": f"{acct} {c['name']}", "部位": p["isin"] or p["case_no"], "標的": t["symbol"],
                     "拆股日": l["date"], "比例": split_text(l["ratio"]),
                     "Strike": f"{o['strike_price']:g} → {n['strike_price']:g}",
                     "KO": f"{o['ko_price']:g} → {n['ko_price']:g}",
                     "KI": f"{o['ki_price']:g} → {n['ki_price']:g}"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("請與發行商的調整通知核對；若發行商的調整方式不同，到「✏️編輯條件」修改該部位即可。")
    if st.button("我知道了", type="primary"):
        for _, _, _, _, l in notes:
            l["ack"] = True
        save_data(db)
        st.rerun()


REFRESH_KEYS = ["ptype", "ki_type", "ko_steps", "obs_dates", "pay_dates", "ra_lower_pct", "ra_free", "ra_obs_start", "ko_obs", "nc"]


def pdf_refresh(db, p):
    """用原本的說明書 PDF 更新既有部位的型態／KI 類型／逐期日期，不動本金、標的價格與入帳紀錄"""
    with st.expander("📄 用說明書 PDF 重新判讀型態（DRA／Stepdown／EKI／AKI）"):
        f = st.file_uploader("上傳這個部位的說明書 PDF", type="pdf", key=f"rf{p['id']}")
        if not f:
            st.caption("只會更新：商品型態、KI 類型、KO 逐期價、每期觀察／給付日、首次 KO 觀察期等；本金、標的價格（含拆股調整）與入帳紀錄都不會動。")
            return
        try:
            new, _ = parse_pdf(f)
        except Exception as e:
            st.error(f"解析失敗：{e}")
            return
        if new.get("isin") and p.get("isin") and new["isin"] != p["isin"]:
            st.warning(f"PDF 的 ISIN（{new['isin']}）與此部位（{p['isin']}）不同，請確認上傳的是同一檔。")
        upd = {"ptype": new.get("ptype", "FCN"), "ki_type": new.get("ki_type", "EKI"), "ko_steps": new.get("ko_steps", []),
               "ko_obs": new.get("ko_obs", "monthly")}
        for k_ in ("obs_dates", "pay_dates", "ra_lower_pct", "ra_free", "ra_obs_start", "nc"):
            if k_ in new:
                upd[k_] = new[k_]
        show = lambda v: "—" if v in (None, "", []) else (f"{len(v)} 期" if isinstance(v, list) and len(v) > 6 else str(v))
        st.dataframe(pd.DataFrame([{"欄位": k_, "目前": show(p.get(k_)), "PDF 判讀": show(upd.get(k_))} for k_ in REFRESH_KEYS if k_ in upd]),
                     hide_index=True, width="stretch")
        if st.button("✅ 套用更新", key=f"rfy{p['id']}", type="primary"):
            p.update(upd)
            if upd["ptype"] == "DRA":
                nt = {t["symbol"]: t for t in new.get("targets", [])}
                for t in p["targets"]:
                    if t["symbol"] in nt and nt[t["symbol"]].get("ra_price") and t["init_price"]:
                        t["ra_price"] = round(t["init_price"] * new.get("ra_lower_pct", 0) / 100, 4)
            save_data(db)
            st.success("已更新")
            st.rerun()


def admin_view(db):
    if not multi_mode():
        pw = admin_pw()
        if not pw:
            st.error("尚未設定管理員密碼。請在 .streamlit/secrets.toml 加入 ADMIN_PASSWORD = \"你的密碼\"，或設定同名環境變數。")
            st.stop()
        if not st.session_state.get("auth"):
            st.title("🔒 營業員登入")
            inp = st.text_input("管理員密碼", type="password")
            if inp:
                lk = thr_check("admin_single", session_only=True)
                if lk:
                    st.error(lk)
                elif hmac.compare_digest(inp.encode("utf-8"), pw.encode("utf-8")):
                    thr_ok("admin_single", session_only=True)
                    st.session_state.auth = True
                    st.session_state.last_active = time.time()
                    st.rerun()
                else:
                    thr_fail("admin_single", session_only=True)
                    st.error("密碼錯誤")
            st.stop()

    notes = split_notes(db, only_unack=True)
    kos = ko_candidates(db)
    if notes and not st.session_state.get("split_shown"):
        st.session_state.split_shown = True
        split_dialog(db)
    elif kos and not st.session_state.get("ko_shown"):
        st.session_state.ko_shown = True
        ko_dialog(db)

    sb = st.sidebar
    sb.header("🔒 營業員總控後台")
    rep = st.session_state.get("rep") if multi_mode() else None
    if rep:
        sb.caption(f"營業員代號：**{rep}**")
        with sb.expander("🔑 變更我的密碼"):
            with st.form("chpw", clear_on_submit=True):
                o_ = st.text_input("目前密碼", type="password")
                n_ = st.text_input("新密碼（6～12 碼）", type="password")
                if st.form_submit_button("變更密碼"):
                    e_ = rep_change_pw(rep, o_, n_)
                    if e_:
                        st.error(e_)
                    else:
                        st.success("密碼已變更")
    if kos and sb.button(f"🎯 待確認 KO 部位（{len(kos)}）", type="primary"):
        ko_dialog(db)
    with sb.form("newc", clear_on_submit=True):
        acct = st.text_input("新增客戶帳號", placeholder="11278-1")
        nm = st.text_input("客戶稱呼（顯示於客戶頁，選填）")
        if st.form_submit_button("➕ 新增客戶"):
            acct = acct.strip()
            if acct and acct not in db:
                acct = _clean(acct, 40)
                db[acct] = {"name": _clean(nm) or f"客戶 {acct}", "token": secrets.token_urlsafe(12), "token_exp": new_exp(1), "positions": []}
                save_data(db)
                st.rerun()
            st.warning("帳號為空或已存在")
    with sb.expander("💾 備份 / 還原"):
        st.download_button("下載備份 JSON", json.dumps(db, ensure_ascii=False, indent=2), "fcn_backup.json")
        up = st.file_uploader("還原備份", type="json")
        if up and st.button("確認覆蓋還原"):
            try:
                raw = up.getvalue()
                if len(raw) > 5_000_000:
                    raise ValueError("檔案過大")
                obj = json.loads(raw.decode("utf-8"))
                if not isinstance(obj, dict) or not all(isinstance(k_, str) and isinstance(v_, dict) and isinstance(v_.get("positions", []), list)
                                                        and all(isinstance(x_, dict) and isinstance(x_.get("targets", []), list) for x_ in v_.get("positions", []))
                                                        for k_, v_ in obj.items()):
                    raise ValueError("格式不符")
                save_data(migrate(obj))
            except Exception:
                st.error("這不是有效的備份檔（格式不符、過大或已損毀），未做任何變更。")
            else:
                st.rerun()
    if sb.button("登出"):
        st.session_state.auth = False
        st.session_state.rep = None
        for _k in ("split_shown", "ko_shown", "force_pw"):
            st.session_state.pop(_k, None)
        st.rerun()

    st.title("🛠️ FCN 營業員總控管理系統")
    if not db:
        st.info("請先在左側新增客戶。")
        st.stop()
    acct = sb.selectbox("管理客戶", list(db), format_func=lambda a: f"{a}｜{db[a]['name']}")
    c = db[acct]
    url = f"{base_url()}/?k={c['token']}" + (f"&u={rep}" if rep else "")
    exp_ok = not link_expired(c)
    sb.markdown("**客戶專屬網址**")
    if exp_ok:
        sb.code(url)
        until = pd.Timestamp(c["token_exp"]).tz_convert("Asia/Taipei").strftime("%Y-%m-%d %H:%M")
        sb.caption(f"⏳ {link_left(c)}（至 {until}）｜" + ("🔐 已設定開啟密碼" if c.get("link_pw") else "🔓 未設定開啟密碼"))
        sb.link_button("🔍 預覽客戶畫面", url)
    else:
        sb.error("⛔ 這位客戶的連結已過期（客戶現在打不開），請在下方重新產生。")
    with sb.expander("🔗 產生／重發網址、有效期與開啟密碼", expanded=not exp_ok):
        d_lbl = st.selectbox("有效期限", list(EXP_OPTS), index=0, key=f"exp{acct}")
        lp1 = st.text_input("連結開啟密碼（選填，6～12 碼）", type="password", key=f"lp{acct}",
                            help="設定後，客戶打開連結要先輸入這組密碼。請用電話或另一個管道告訴客戶，不要和連結放在同一則訊息。留空＝沿用目前設定。")
        keep = st.checkbox("沿用目前網址（只更新有效期／密碼）", value=False, disabled=not exp_ok, key=f"keep{acct}")
        rm_pw = st.checkbox("移除目前的開啟密碼", key=f"rmlp{acct}") if c.get("link_pw") else False
        if st.button("✅ 套用", type="primary", key=f"gen{acct}"):
            err = None
            if lp1 and not LP_MIN <= len(lp1) <= LP_MAX:
                err = f"連結密碼須為 {LP_MIN}～{LP_MAX} 碼。"
            elif lp1 and pw_problem(lp1):
                err = pw_problem(lp1)
            if err:
                st.error(err)
            else:
                if not (keep and exp_ok):
                    c["token"] = secrets.token_urlsafe(12)  # 重發：舊網址立即失效
                c["token_exp"] = new_exp(EXP_OPTS[d_lbl])
                c["lp_fails"], c["lp_locked_until"] = 0, ""
                if lp1:
                    sl = secrets.token_hex(16)
                    c["link_pw"] = {"salt": sl, "iter": PW_ITER, "hash": _hash_pw(lp1, sl)}
                elif rm_pw:
                    c.pop("link_pw", None)
                save_data(db)
                st.rerun()
    if sb.checkbox("確認要刪除此客戶") and sb.button("🗑️ 刪除客戶", type="primary"):
        del db[acct]
        save_data(db)
        st.rerun()

    t_over, t_pos, t_pdf = st.tabs(["📊 全客戶燈號總覽", "📋 客戶部位管理", "📄 PDF 匯入"])

    with t_over:
        rows = []
        for a, cl in db.items():
            for p in cl["positions"]:
                if not is_active(p):
                    continue
                det = []
                for t in p["targets"]:
                    px = last_px(t, p)[0]
                    det.append(f"{t['symbol']} {px:,.2f}（距Strike {(px / t['strike_price'] - 1) * 100:+.1f}%）" if px else f"{t['symbol']} 無資料")
                rows.append({"燈號": LIGHT[pos_status(p)], "客戶": f"{a}｜{cl['name']}", "案件": p["case_no"], "ISIN": p["isin"],
                             "型態": PTYPES[ptype(p)].split("（")[0],
                             "KI": ("AKI・已觸及" if any(ki_hit(p, t)[0] for t in p["targets"]) else "AKI") if ki_type(p) == "AKI" else "EKI",
                             "到期日": p["end_date"], "下次KO評價日": nk_str(p), "幣別": ccy(p), "本金": p["principal"], "標的現況": "；".join(det),
                             "KO 提示": "⚠️ 所有標的 ≥ KO，請確認是否出場" if all_above_ko(p) else ""})
        if rows:
            df = pd.DataFrame(rows)
            m = st.columns(3)
            for col, k in zip(m, ("🟢 正常", "🟡 警戒", "🔴 危機")):
                col.metric(k, int((df["燈號"] == k).sum()))
            st.dataframe(df.sort_values("燈號", ascending=False), hide_index=True, width="stretch")
        else:
            st.info("目前沒有進行中的部位。")

    with t_pos:
        st.subheader(f"客戶 {acct}｜{c['name']}")
        nn = st.text_input("客戶稱呼", c["name"])
        if nn != c["name"]:
            c["name"] = nn
            save_data(db)
        with st.expander("➕ 新增部位"):
            ver = st.session_state.get("addver", 0)
            res = pos_form(f"add{ver}", None)
            if res:
                c["positions"].append({**res, "id": secrets.token_hex(4), "closed": ""})
                save_data(db)
                st.session_state.addver = ver + 1
                st.rerun()
        for closed_view in (False, True):
            ps = [p for p in c["positions"] if (not is_active(p)) == closed_view]
            if closed_view and ps:
                st.markdown("### 🗂️ 已結束部位（客戶端僅顯示於損益與入帳明細）")
            elif not closed_view:
                st.markdown("### 進行中部位")
            for p in ps:
                tag = (p["closed"] or "已到期") if closed_view else LIGHT[pos_status(p)]
                with st.expander(f"{tag}｜[{p['case_no']}] {p['pos_name']}｜{money(p, p['principal'], 0)}"):
                    tb = st.tabs(["✏️ 編輯條件", "🚪 出場記錄", "💵 配息／入帳紀錄"])
                    with tb[0]:
                        pdf_refresh(db, p)
                        res = pos_form(f"edit{p['id']}", p)
                        if res:
                            p.update(res)
                            save_data(db)
                            st.rerun()
                    with tb[1]:
                        exit_panel(db, p)
                    with tb[2]:
                        records_panel(db, p)
                    if st.button("🗑️ 刪除部位", key=f"del{p['id']}"):
                        c["positions"].remove(p)
                        save_data(db)
                        st.rerun()

    with t_pdf:
        st.caption("上傳各家外資的中文產品說明書 PDF，系統會自動判讀年利率、起訖日、標的、期初價、Strike／KO／KI。"
                   "判讀結果會預填到下方表單，請核對後再儲存。")
        f = st.file_uploader("產品說明書 PDF", type="pdf")
        if f and st.button("開始解析"):
            try:
                with st.spinner("解析中（頁數多的說明書約需十幾秒）…"):
                    pre, raw = parse_pdf(f)
                pre.setdefault("case_no", pre.get("isin", ""))
                st.session_state.update(prefill=pre, rawtext=raw, pdfver=st.session_state.get("pdfver", 0) + 1)
            except ImportError:
                st.error("請先安裝：pip install pdfplumber pypdf")
            except Exception as e:
                st.error(f"解析失敗：{e}")
        if st.session_state.get("prefill"):
            pre = st.session_state.prefill
            if len(st.session_state.rawtext.strip()) < 50:
                st.warning("讀不到文字，可能是掃描檔（圖片）或特殊字型，請改用可複製文字的 PDF，或手動輸入。")
            st.success(f"自動判讀結果（將存入客戶 {acct}，請核對）")
            miss = [lb for k_, lb in (("isin", "ISIN"), ("coupon_rate", "年利率"), ("start_date", "起始日"), ("end_date", "到期日")) if k_ not in pre]
            m = st.columns(5)
            m[0].metric("ISIN", pre.get("isin", "未偵測"))
            m[1].metric("年利率", f"{pre['coupon_rate']:g}%" if "coupon_rate" in pre else "未偵測")
            m[2].metric("起始日", pre.get("start_date", "未偵測"))
            m[3].metric("到期日", pre.get("end_date", "未偵測"))
            m[4].metric("首次KO觀察月數", pre.get("nc", 1))
            pct = lambda k_: f"{pre[k_]:g}%" if k_ in pre else "—"
            st.caption(f"條件比例（取各標的中位數）：Strike {pct('sp')}　KO {pct('kp')}　KI {pct('ip')}")
            ex = [f"商品型態：{PTYPES[pre.get('ptype', 'FCN')]}", f"KI 類型：{KI_TXT[pre.get('ki_type', 'EKI')]}"]
            if pre.get("ko_steps"):
                ex.append("KO 逐期：" + " → ".join(f"{x:g}%" for x in pre["ko_steps"]))
            if pre.get("ptype") == "DRA":
                ex.append(f"區間下限 {pre.get('ra_lower_pct', 0):g}%（前 {pre.get('ra_free', 2)} 期保證滿額）")
            if pre.get("pay_dates"):
                ex.append(f"已讀到 {len(pre['pay_dates'])} 期實際觀察／給付日")
            st.info("　｜　".join(ex) + "\n\n請核對，若判斷有誤可在下方表單修正「商品型態」「KI 類型」。")
            tg = pre.get("targets", [])
            if tg:
                st.dataframe(pd.DataFrame([{"代號": t["symbol"] or "（請手動填）", "名稱": t.get("name", ""), "期初價": t["init_price"],
                                            "Strike 履約價": t["strike_price"], "KO 提前出場價": t["ko_price"], "KI 下限價": t["ki_price"]}
                                           for t in tg]), hide_index=True, width="stretch")
            else:
                miss.append("標的與價格")
            if len(tg) > 5:
                st.warning("偵測到超過 5 檔標的，僅帶入前 5 檔。")
            if miss:
                st.warning("以下欄位沒有偵測到，請在表單手動補上：" + "、".join(miss))
            with st.expander("PDF 原始文字"):
                st.text(st.session_state.rawtext[:6000])
            res = pos_form(f"pdf{st.session_state.pdfver}", pre)
            if res:
                c["positions"].append({**res, "id": secrets.token_hex(4), "closed": ""})
                save_data(db)
                st.session_state.prefill = None
                st.success("已新增部位")
                st.rerun()


# ---------- 入口 ----------
k = st.query_params.get("k")
if k and len(str(k)) > 64:
    k = "x"  # 過長的代碼一定無效
if k:
    owner = None
    if multi_mode():
        u_ = str(st.query_params.get("u") or "").lower()
        owner = u_ if CODE_RE.match(u_) else find_token_owner(k)
    db = load_data(owner) if (owner or not multi_mode()) else {}
    cl = next((x for x in db.values() if hmac.compare_digest(x["token"].encode("utf-8"), str(k).encode("utf-8"))), None)
    if cl and link_expired(cl):
        st.error("這個連結已過期，請向您的營業員索取新連結。")
    elif cl:
        if cl.get("link_pw"):
            link_gate(cl, db, owner)
        client_view(cl)
    else:
        st.error("連結無效或已失效，請向您的營業員索取新連結。")
else:
    idle_guard()
    if multi_mode():
        rep_gate()
        force_change_gate()
    admin_view(load_data())
