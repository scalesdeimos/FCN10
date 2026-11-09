import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import json, os, re, secrets, hmac, tempfile, requests, math, hashlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from datetime import date, timedelta

st.set_page_config(page_title="FCN 管理與客戶回報系統", page_icon="📈", layout="wide")

DATA_FILE = os.environ.get("FCN_DATA_FILE", "fcn_clients.json")
LIGHT = {"g": "🟢 正常", "y": "🟡 警戒", "r": "🔴 危機", "n": "⚪ 無資料"}
TODAY = pd.Timestamp.today().normalize()


# ---------- 資料存取 ----------
def migrate(db):
    """舊版資料自動升級：補 token / ISIN / KO / 狀態，target_1~5 轉為 targets 清單"""
    for acct, c in db.items():
        c.setdefault("name", f"客戶 {acct}")
        c.setdefault("token", secrets.token_urlsafe(12))
        c.setdefault("positions", [])
        for p in c["positions"]:
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
    return db


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
            st.error(f"雲端資料庫讀取失敗：{e}")
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
    if auto_split(db):  # 偵測到標的拆股，條件價已自動換算，順手存回
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
            st.error(f"雲端資料庫儲存失敗，這次的變更沒有存下來：{e}")
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
                keys = ("init_price", "strike_price", "ko_price", "ki_price")
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
        for it in ET.fromstring(r.content).iter("item"):
            title, src = it.findtext("title", ""), it.findtext("source", "")
            if src and title.endswith(" - " + src):
                title = title[:-len(src) - 3]
            out.append({"title": title.replace("[", "［").replace("]", "］"), "link": it.findtext("link", ""), "src": src,
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


def t_status(t, px):
    if not px:
        return "n"
    if t["ki_price"] and px < t["ki_price"]:
        return "r"
    if t["strike_price"] and px < t["strike_price"]:
        return "y"
    return "g"


def pos_status(p):
    sts = [t_status(t, last_px(t, p)[0]) for t in p["targets"]]
    return next((s for s in "ryg" if s in sts), "n")


def is_active(p):
    return not p["closed"] and pd.Timestamp(p["end_date"]) >= TODAY


def all_above_ko(p):
    pxs = [(t, last_px(t, p)[0]) for t in p["targets"]]
    return bool(pxs) and all(t["ko_price"] and px >= t["ko_price"] for t, px in pxs)


def schedule(p):
    """每月配息日程；最後一期對齊到期日"""
    s, e = pd.Timestamp(p["start_date"]), pd.Timestamp(p["end_date"])
    n = max(1, round((e - s).days / 30.4))
    mc = p["principal"] * p["coupon_rate"] / 1200
    rows = [(min(s + pd.DateOffset(months=k), e), mc) for k in range(1, n + 1)]
    rows[-1] = (e, mc)
    return rows, mc


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


def ko_first(p):
    sch = schedule(p)[0]
    return sch[min(max(p.get("nc", 1), 1) - 1, len(sch) - 1)][0]


# ---------- 部位表單 (新增 / 編輯 / PDF 預填共用) ----------
def pos_form(key, d):
    d = d or {}
    kk = lambda n: f"{key}_{n}"
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
                ts.append({"symbol": sym, "init_price": init, "strike_price": round(s_, 4), "ko_price": round(k_, 4), "ki_price": round(i_, 4)})
        ok = st.form_submit_button("💾 儲存")
    if not ok:
        return None
    if not case_no.strip() or principal <= 0 or not ts or ed <= sd:
        st.error("請確認：案件編號、本金 > 0、至少 1 檔標的、到期日晚於起始日。")
        return None
    if any(t["strike_price"] <= 0 or t["ki_price"] <= 0 or t["ki_price"] > t["strike_price"] for t in ts):
        st.error("每檔標的需填 Strike 與 KI，且 KI 不得高於 Strike。")
        return None
    return {"case_no": case_no.strip(), "isin": isin, "pos_name": name, "principal": principal, "coupon_rate": rate,
            "start_date": str(sd), "end_date": str(ed), "nc": int(nc), "targets": ts, "ccy": ccy_, "ko_obs": ko_obs}


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

    inits = []
    for v in nums:
        if v not in inits and all(has(v * p / 100) for p in pcts):
            inits.append(v)
    if not inits:
        return [], {}
    gl = _tks(text)
    names = {}
    for nm, code in re.findall(r"(?m)^\s*(\S.{1,38}?)\s+(\d{4})\s+JT\b", text):
        names[code + ".T"] = nm.strip()
    byval = {}
    for nm, val in re.findall(r"([A-Za-z][A-Za-z .&,]{2,30}?|[一-鿿]{3,20})\s*(\d+\.\d{4})", region):
        nm = re.sub(r"^[日圓美元\s]+", "", nm).strip()
        if nm and not re.search(r"日圓|美元", nm):
            byval.setdefault(round(float(val), 2), nm)
    roles = ["ko", "strike", "ki"] if len(pcts) >= 3 else ["strike", "ki"]
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
            return run[:5], {}
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
    mk = re.search(r"[自於]第\s*([一二三四五六七八\d]+)\s*個(?:配息)?(?:觀察)?期(?:間)?結束日", text)
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
    if re.search(r"自動提前(?:出場|到期)觀察日[^。]{0,80}?每(?:一個|個)?(?:預定)?(?:交易日|營業日)", text):
        out["ko_obs"] = "daily"
    # 標的價格
    rows, hdr = parse_tables(tables or [])
    if not rows:
        rows, hdr = parse_pct_table(text)
    if not rows:
        rows, hdr = parse_grid(text)
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
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts, tables = [], []
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
    sch = schedule(p)[0]
    return next((d for d, _ in sch[max(p.get("nc", 1), 1) - 1:-1] if d >= TODAY), None)


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
    return {"ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "狀態": state_text(p),
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


def target_panel(p, t):
    px, df = last_px(t, p)
    sym, stt = t["symbol"], t_status(t, px)
    st.markdown(f"### {sym}　{LIGHT[stt]}")
    chg = 0.0
    if df is not None and len(df) > 1:
        chg = (df.Close.iloc[-1] / df.Close.iloc[-2] - 1) * 100
    d = lambda lv: f"{(px / lv - 1) * 100:+.2f}% vs 現價" if lv and px else None
    c = st.columns(4)
    c[0].metric("最新收盤價", money(p, px, 2), f"{chg:+.2f}%")
    c[1].metric("Strike", money(p, t["strike_price"], 2), d(t["strike_price"]))
    c[2].metric("KO", money(p, t["ko_price"], 2), d(t["ko_price"]))
    c[3].metric("KI", money(p, t["ki_price"], 2), d(t["ki_price"]))
    if df is None:
        st.warning("目前無法取得走勢資料。")
    else:
        if t["ki_price"] and df.Close.min() < t["ki_price"]:
            st.warning("合約期間內曾有收盤價低於 KI。")
        fig = go.Figure(go.Scatter(x=df.index, y=df.Close, name="收盤價", line=dict(color="#2b6cb0")))
        for n, v, col in (("Strike", t["strike_price"], "#b7791f"), ("KO", t["ko_price"], "#2f855a"), ("KI", t["ki_price"], "#c53030")):
            if v:
                fig.add_hline(y=v, line_dash="dash", line_color=col, annotation_text=f"{n} {v:,.2f}")
        fig.update_layout(template="plotly_white", height=420, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig)
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


def cashflow_charts(act):
    cys = list(dict.fromkeys(ccy(p) for p in act))
    for cy in cys:
        if len(cys) > 1:
            st.markdown(f"**幣別：{cy}**")
        _cashflow([p for p in act if ccy(p) == cy], cy)


def _cashflow(act, cy):
    f1, f2, tot, ret = go.Figure(), go.Figure(), {}, {}
    for i, p in enumerate(act):
        color, nm = COLORS[i % len(COLORS)], p["isin"] or p["case_no"]
        m = {}
        for d, a in schedule(p)[0]:
            if d > TODAY:
                k = d.strftime("%Y-%m")
                m[k] = m.get(k, 0) + a
                tot[k] = tot.get(k, 0) + a
        if m:
            f1.add_trace(go.Bar(x=list(m), y=list(m.values()), name=nm, marker_color=color))
        k = pd.Timestamp(p["end_date"]).strftime("%Y-%m")
        ret[k] = ret.get(k, 0) + p["principal"]
        f2.add_trace(go.Bar(x=[k], y=[p["principal"]], name=nm, marker_color=color,
                            text=[f"{p['principal']:,.0f}"], textposition="auto"))
    ks = sorted(tot)
    f1.add_trace(go.Scatter(x=ks, y=[tot[k] for k in ks], mode="text", text=[f"{tot[k]:,.0f}" for k in ks],
                            textposition="top center", showlegend=False, hoverinfo="skip"))
    cc = st.columns([3, 2])
    for col, f, title, top, key in ((cc[0], f1, f"每月配息 ({cy}，依部位分色)", max(tot.values(), default=0), f"cf1{cy}"),
                                    (cc[1], f2, f"到期還本 ({cy})", max(ret.values(), default=0), f"cf2{cy}")):
        f.update_layout(template="plotly_white", barmode="stack", title=title, height=380, margin=dict(l=10, r=10, t=50, b=10),
                        yaxis_range=[0, max(top, 1) * 1.25], xaxis_type="category", legend=dict(orientation="h", y=-0.25))
        col.plotly_chart(f, key=key)


def detail_view(p):
    sts = [t_status(t, last_px(t, p)[0]) for t in p["targets"]]
    s = next((x for x in "ryg" if x in sts), "n")
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
                     "資料日": str(df.index[-1].date()) if df is not None else "—", "KO價": t["ko_price"], "STRIKE": t["strike_price"],
                     "KI價": t["ki_price"], "相對KO(%)": rel(t["ko_price"]), "相對STRIKE(%)": rel(t["strike_price"]),
                     "相對KI(%)": rel(t["ki_price"])})
        if px and t["init_price"]:
            perf.append((t["symbol"], px / t["init_price"] - 1))
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("相對 KO／STRIKE／KI(%) ＝ 最新收盤相對於該價位的漲跌幅；相對 KO 為負代表尚未達 KO。")
    if perf:
        w = min(perf, key=lambda x: x[1])
        st.caption(f"最差表現標的（Worst-of）：{w[0]}，較期初 {w[1] * 100:+.2f}%")
    for tab, t in zip(st.tabs([t["symbol"] for t in p["targets"]]), p["targets"]):
        with tab:
            target_panel(p, t)


def history_view(allp):
    st.markdown("---")
    st.markdown("### 各 FCN 組合損益（含進行中與已出場）")
    st.dataframe(pd.DataFrame([pnl_row(p) for p in allp]), hide_index=True, width="stretch")
    st.markdown("### 已入帳明細（配息／還本／承接）")
    rows = [{"日期": r["日期"].date(), "ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "幣別": ccy(p), "項目": r["項目"], "金額": round(r["金額"], 2)}
            for p in allp for r in received(p)]
    divs = [r for r in rows if r["項目"] == "配息"]
    cys = list(dict.fromkeys(ccy(p) for p in allp))
    tdiv = {cy: sum(r["金額"] for r in divs if r["幣別"] == cy) for cy in cys}
    tcap = {cy: sum(p["principal"] for p in allp if ccy(p) == cy) for cy in cys}
    m = st.columns(3)
    m[0].metric("💰 累計實際已領配息（全部部位）", tot_text([(cy, tdiv[cy]) for cy in cys]))
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
    st.caption("已實現損益 ＝ 已入帳配息；本金還回、或轉為股票的成本不計入損益。報酬率 ＝ 已領配息 ÷ 名目本金，年化報酬率依持有天數推算（進行中者算到今天）。"
               "承接股票帳面損益 ＝ 股數 ×（最新收盤 − 承接價），未計匯率與交易成本。若無手動配息紀錄，配息依說明書日程推算，實際入帳以券商對帳單為準。")


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
    act = [p for p in allp if is_active(p)]
    if not act:
        st.info("目前沒有進行中的部位。")
    else:
        sts = [pos_status(p) for p in act]
        m = st.columns(5)
        m[0].metric("進行中部位", len(act))
        m[1].metric("總本金", tot_text([(ccy(p), p["principal"]) for p in act], 0))
        m[2].metric("每月配息", tot_text([(ccy(p), schedule(p)[1]) for p in act]))
        m[3].metric("累積已領配息", tot_text([(ccy(p), r["金額"]) for p in allp for r in received(p) if r["項目"] == "配息"]))
        m[4].metric("燈號", f"🟢{sts.count('g')} 🟡{sts.count('y')} 🔴{sts.count('r')}")
        st.markdown("#### 💰 未來現金流（依部位分色）")
        cashflow_charts(act)
        nm = lambda p: p["isin"] or p["case_no"]
        fl = [{"日期": d.date(), "部位": nm(p), "幣別": ccy(p), "類型": "配息", "金額": round(a, 2)} for p in act for d, a in schedule(p)[0] if d > TODAY]
        fl += [{"日期": pd.Timestamp(p["end_date"]).date(), "部位": nm(p), "幣別": ccy(p), "類型": "到期還本", "金額": p["principal"]} for p in act]
        with st.expander("現金流明細"):
            st.dataframe(pd.DataFrame(fl).sort_values("日期"), hide_index=True, width="stretch")
        st.markdown("---")
        st.markdown("### 🔎 FCN 組合明細")
        opts = {f"{LIGHT[pos_status(p)].split()[0]} {nm(p)}｜{p['pos_name']}": p for p in act}
        detail_view(opts[st.selectbox("選擇 ISIN CODE", list(opts))])
    history_view(allp)


# ---------- 營業員註冊 / 登入（多營業員模式）----------
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


def _hash_pw(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), 200_000).hex()


def _secret(name):
    try:
        return str(st.secrets[name])
    except Exception:
        return os.environ.get(name, "")


def rep_register(code, pw, pw2, invite):
    inv = _secret("INVITE_CODE")
    if not inv:
        return "目前尚未開放註冊，請聯絡系統管理員。"
    if not hmac.compare_digest(invite.strip().encode("utf-8"), inv.encode("utf-8")):
        return "邀請碼錯誤。"
    code = code.strip()
    if not CODE_RE.match(code):
        return "營業員代號請用 3～20 個英數字、底線或減號。"
    if len(pw) < 8:
        return "密碼至少 8 碼。"
    if pw != pw2:
        return "兩次輸入的密碼不一致。"
    key = f"acct:{code.lower()}"
    try:
        if sb_get(key) is not None:
            return "這個代號已被使用，請換一個。"
        salt = secrets.token_hex(16)
        sb_put(key, {"code": code, "salt": salt, "hash": _hash_pw(pw, salt), "created": str(date.today()),
                     "fails": 0, "locked_until": ""})
    except Exception as e:
        return f"註冊失敗：{e}"
    st.session_state.rep = code.lower()
    return None


def rep_login(code, pw):
    code = code.strip().lower()
    bad = "代號或密碼錯誤。"
    try:
        a = sb_get(f"acct:{code}") if CODE_RE.match(code) else None
        if a is None:
            _hash_pw(pw, "00" * 16)  # 帳號不存在時也做一次雜湊，避免用回應時間猜帳號
            return bad
        if a.get("locked_until") and pd.Timestamp.now("UTC") < pd.Timestamp(a["locked_until"]):
            return "嘗試次數過多，請 10 分鐘後再試。"
        if hmac.compare_digest(_hash_pw(pw, a["salt"]).encode("utf-8"), a["hash"].encode("utf-8")):
            if a.get("fails") or a.get("locked_until"):
                a["fails"], a["locked_until"] = 0, ""
                sb_put(f"acct:{code}", a)
            st.session_state.rep = code
            return None
        a["fails"] = a.get("fails", 0) + 1
        if a["fails"] >= 5:
            a["fails"] = 0
            a["locked_until"] = (pd.Timestamp.now("UTC") + pd.Timedelta(minutes=10)).isoformat()
        sb_put(f"acct:{code}", a)
        return bad
    except Exception as e:
        return f"登入失敗：{e}"


def rep_change_pw(code, old, new):
    if len(new) < 8:
        return "新密碼至少 8 碼。"
    err = rep_login(code, old)
    if err:
        return "目前密碼不正確。" if err.startswith("代號或密碼") else err
    a = sb_get(f"acct:{code}")
    a["salt"] = secrets.token_hex(16)
    a["hash"] = _hash_pw(new, a["salt"])
    sb_put(f"acct:{code}", a)
    return None


def master_panel():
    pw = admin_pw()
    if not pw:
        st.info("尚未設定 ADMIN_PASSWORD，無法使用總管理。")
        return
    if not st.session_state.get("master"):
        with st.form("mst"):
            inp = st.text_input("總管理密碼（ADMIN_PASSWORD）", type="password")
            if st.form_submit_button("進入總管理"):
                if st.session_state.get("mfail", 0) >= 5:
                    st.error("嘗試次數過多，請重新整理頁面後再試。")
                elif hmac.compare_digest(inp.encode("utf-8"), pw.encode("utf-8")):
                    st.session_state.master = True
                    st.rerun()
                else:
                    st.session_state.mfail = st.session_state.get("mfail", 0) + 1
                    st.error("密碼錯誤。")
        return
    st.success("總管理模式：可重設同事密碼或刪除帳號（看不到同事的客戶內容）。")
    seen = {r["id"][4:]: r.get("updated_at", "") for r in sb_list("rep:")}
    accts = sb_list("acct:", with_data=True)
    st.write(f"目前共 {len(accts)} 個營業員帳號")
    for r in accts:
        code = r["id"][5:]
        with st.expander(f"{r['data'].get('code', code)}｜註冊 {r['data'].get('created', '')}｜資料更新 {str(seen.get(code, ''))[:10] or '—'}"):
            npw = st.text_input("新密碼（至少 8 碼）", type="password", key=f"mnp{code}")
            if st.button("重設密碼", key=f"mrp{code}"):
                if len(npw) < 8:
                    st.error("新密碼至少 8 碼。")
                else:
                    a = r["data"]
                    a["salt"] = secrets.token_hex(16)
                    a["hash"] = _hash_pw(npw, a["salt"])
                    a["fails"], a["locked_until"] = 0, ""
                    sb_put(r["id"], a)
                    st.success("已重設，請把新密碼告訴該營業員，並請他登入後自行變更。")
            if st.checkbox("確認刪除此帳號與其全部客戶資料", key=f"mdc{code}") and st.button("🗑️ 刪除帳號", key=f"mdl{code}", type="primary"):
                sb_del(r["id"])
                sb_del(f"rep:{code}")
                st.rerun()
    if st.button("離開總管理"):
        st.session_state.master = False
        st.rerun()


def rep_gate():
    if st.session_state.get("rep"):
        return
    st.title("🔒 FCN 營業員系統")
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
            p2 = st.text_input("設定密碼（至少 8 碼）", type="password")
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


def find_token_owner(token):
    """舊式連結（沒有 u 參數）：在所有營業員的資料中找這個客戶代碼，回傳營業員代號"""
    try:
        for r in sb_list("rep:", with_data=True):
            for x in (r.get("data") or {}).values():
                if isinstance(x, dict) and hmac.compare_digest(str(x.get("token", "")).encode("utf-8"), str(token).encode("utf-8")):
                    return r["id"][4:]
    except Exception:
        pass
    return None


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
                obs = [d for d, _ in schedule(p)[0][max(p.get("nc", 1), 1) - 1:-1] if d <= TODAY]
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
                    if row[t["symbol"]] is not None and row[t["symbol"]] >= t["ko_price"]:
                        hit[t["symbol"]] = True
                strict = all(row[t["symbol"]] is not None and row[t["symbol"]] >= t["ko_price"] for t in p["targets"])
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
                                        "KO 價": t["ko_price"]} for t in p["targets"]]), hide_index=True, width="stretch")
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
                if hmac.compare_digest(inp.encode("utf-8"), pw.encode("utf-8")):
                    st.session_state.auth = True
                    st.rerun()
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
                n_ = st.text_input("新密碼（至少 8 碼）", type="password")
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
                db[acct] = {"name": nm.strip() or f"客戶 {acct}", "token": secrets.token_urlsafe(12), "positions": []}
                save_data(db)
                st.rerun()
            st.warning("帳號為空或已存在")
    with sb.expander("💾 備份 / 還原"):
        st.download_button("下載備份 JSON", json.dumps(db, ensure_ascii=False, indent=2), "fcn_backup.json")
        up = st.file_uploader("還原備份", type="json")
        if up and st.button("確認覆蓋還原"):
            save_data(migrate(json.load(up)))
            st.rerun()
    if sb.button("登出"):
        st.session_state.auth = False
        st.session_state.rep = None
        for _k in ("split_shown", "ko_shown"):
            st.session_state.pop(_k, None)
        st.rerun()

    st.title("🛠️ FCN 營業員總控管理系統")
    if not db:
        st.info("請先在左側新增客戶。")
        st.stop()
    acct = sb.selectbox("管理客戶", list(db), format_func=lambda a: f"{a}｜{db[a]['name']}")
    c = db[acct]
    url = f"{base_url()}/?k={c['token']}" + (f"&u={rep}" if rep else "")
    sb.markdown("**客戶專屬網址**（含不可猜測代碼）")
    sb.code(url)
    sb.link_button("🔍 預覽客戶畫面", url)
    if sb.button("🔄 重發網址（舊網址立即失效）"):
        c["token"] = secrets.token_urlsafe(12)
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
if k:
    owner = None
    if multi_mode():
        u_ = str(st.query_params.get("u") or "").lower()
        owner = u_ if CODE_RE.match(u_) else find_token_owner(k)
    db = load_data(owner) if (owner or not multi_mode()) else {}
    cl = next((x for x in db.values() if hmac.compare_digest(x["token"].encode("utf-8"), str(k).encode("utf-8"))), None)
    if cl:
        client_view(cl)
    else:
        st.error("連結無效或已失效，請向您的營業員索取新連結。")
else:
    if multi_mode():
        rep_gate()
    admin_view(load_data())
