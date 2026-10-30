import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import json, os, re, secrets, hmac, tempfile, requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from datetime import date, timedelta

st.set_page_config(page_title="FCN 管理與客戶回報系統", page_icon="📈", layout="wide")
st.markdown("<style>.stApp{background:#0e1117;color:#fafafa}</style>", unsafe_allow_html=True)

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


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return migrate(json.load(f))
        except Exception:
            st.error("資料檔讀取失敗，請檢查 JSON 格式或從備份還原。")
            st.stop()
    return {}


def save_data(db):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(DATA_FILE)))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)  # 原子寫入，避免寫到一半損毀


# ---------- 行情與判斷 ----------
@st.cache_data(ttl=600)
def get_hist(sym, start):
    try:
        df = yf.Ticker(sym).history(start=start, auto_adjust=False)[["Close"]]
        if df.empty:
            return None
        df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return None


CN = {"NVDA": "輝達", "TSM": "台積電", "TSLA": "特斯拉", "AAPL": "蘋果", "MSFT": "微軟", "AMZN": "亞馬遜", "GOOGL": "谷歌",
      "GOOG": "谷歌", "META": "Meta", "AMD": "超微", "AVGO": "博通", "MU": "美光", "INTC": "英特爾", "NFLX": "網飛",
      "QCOM": "高通", "ORCL": "甲骨文", "PLTR": "Palantir"}
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
        principal = c[3].number_input("投資本金 (USD)", 0.0, value=float(d.get("principal", 50000)), step=1000.0, key=kk("pr"))
        c = st.columns(4)
        nc = c[3].number_input("首次KO觀察月數", 1, 12, int(d.get("nc", 1)), key=kk("nc"))
        rate = c[0].number_input("年利率 (%)", 0.0, 100.0, float(d.get("coupon_rate", 10.0)), 0.1, key=kk("rate"))
        sd = c[1].date_input("合約起始日", date.fromisoformat(d.get("start_date", str(date.today()))), key=kk("sd"))
        ed = c[2].date_input("合約到期日", date.fromisoformat(d.get("end_date", str(date.today() + timedelta(days=365)))), key=kk("ed"))
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
            "start_date": str(sd), "end_date": str(ed), "nc": int(nc), "targets": ts}


# ---------- PDF 解析：表格優先 → 純文字列 → 關鍵字，並用價格比例驗證 ----------
KEYS = {
    "init": ["期初評價日之收盤價", "期初評價日收盤價", "初始參考價格", "最初價格", "期初價格", "期初股價", "期初參考價", "初始價格",
             "期初價", "最初價", "進場價"],
    "strike": ["履約價格", "履約價", "轉換價格", "轉換價", "執行價格", "執行價", "行使價"],
    "ko": ["自動提前出場觸發價格", "自動提前出場觸發價", "自動提前出場界限", "自動提前出場界線", "自動提前出場價格", "自動提前出場價",
           "提前出場觸發價格", "提前出場界限", "提前出場界線", "提前出場價格", "提前出場價", "提前贖回價格", "提前贖回價",
           "KO價格", "KO價", "KO", "敲出價", "敲出"],
    "ki": ["觸及生效價格", "觸及生效價", "下限觸發價格", "下限觸發價", "觸及下限價", "觸及價格", "觸及價", "觸碰價格", "觸碰價",
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
    mk = re.search(r"[自於]第\s*([一二三四五六七八\d]+)\s*個(?:配息)?觀察期間結束日", text)
    if mk:
        v = mk.group(1)
        out["nc"] = int(v) if v.isdigit() else CN_NUM.get(v, 1)
    elif na:
        out["nc"] = max(na) + 1
    elif na_rows:
        out["nc"] = na_rows + 1
    # 標的價格
    rows, hdr = parse_tables(tables or [])
    if not rows:
        rows, hdr = parse_grid(text)
    if not rows:  # 只有行文敘述：關鍵字後取值
        g = {c: grab(text, c) for c in KEYS}
        ab = {c: [v for v, p in g[c] if not p] for c in KEYS}
        hdr = {c: next((v for v, p in g[c] if p), None) for c in KEYS}
        rows = [("", "", {c: (ab[c][i] if i < len(ab[c]) else 0.0) for c in KEYS}) for i in range(min(5, len(ab["init"])))]
    gl = list(dict.fromkeys(TK_RE.findall(re.sub(r"\s+", " ", text))))
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
COLORS = ["#4299e1", "#48bb78", "#ed8936", "#9f7aea", "#f56565", "#ecc94b", "#38b2ac", "#ed64a6"]
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
    sch = schedule(p)[0]
    return next((d for d, _ in sch[max(p.get("nc", 1), 1) - 1:-1] if d >= TODAY), None)


def nk_str(p):
    n = next_ko(p)
    return str(n.date()) if n is not None else "—"


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
            "出場日": p.get("exit_date") or (p["end_date"] if done else ""), "持有天數": days, "名目本金": p["principal"],
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
        "amount": st.column_config.NumberColumn("金額 (USD)", min_value=0.0, format="%.2f", required=True),
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
    c[0].metric("最新收盤價", f"${px:,.2f}", f"{chg:+.2f}%")
    c[1].metric("Strike", f"${t['strike_price']:,.2f}", d(t["strike_price"]))
    c[2].metric("KO", f"${t['ko_price']:,.2f}", d(t["ko_price"]))
    c[3].metric("KI", f"${t['ki_price']:,.2f}", d(t["ki_price"]))
    if df is None:
        st.warning("目前無法取得走勢資料。")
    else:
        if t["ki_price"] and df.Close.min() < t["ki_price"]:
            st.warning("合約期間內曾有收盤價低於 KI。")
        fig = go.Figure(go.Scatter(x=df.index, y=df.Close, name="收盤價", line=dict(color="#4299e1")))
        for n, v, col in (("Strike", t["strike_price"], "#ecc94b"), ("KO", t["ko_price"], "#48bb78"), ("KI", t["ki_price"], "#f56565")):
            if v:
                fig.add_hline(y=v, line_dash="dash", line_color=col, annotation_text=f"{n} {v:,.2f}")
        fig.update_layout(template="plotly_dark", height=420, margin=dict(l=10, r=10, t=30, b=10))
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
    for col, f, title, top, key in ((cc[0], f1, "每月配息 (USD，依部位分色)", max(tot.values(), default=0), "cf1"),
                                    (cc[1], f2, "到期還本 (USD)", max(ret.values(), default=0), "cf2")):
        f.update_layout(template="plotly_dark", barmode="stack", title=title, height=380, margin=dict(l=10, r=10, t=50, b=10),
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
    rows = [{"日期": r["日期"].date(), "ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "項目": r["項目"], "金額": round(r["金額"], 2)}
            for p in allp for r in received(p)]
    divs = [r for r in rows if r["項目"] == "配息"]
    total_div = sum(r["金額"] for r in divs)
    total_cap = sum(p["principal"] for p in allp)
    m = st.columns(3)
    m[0].metric("💰 累計實際已領配息（全部部位）", f"${total_div:,.2f}")
    m[1].metric("配息筆數", len(divs))
    m[2].metric("最近一次配息", f"{max(r['日期'] for r in divs)}" if divs else "—")
    per = []
    for p in allp:
        d = sum(r["金額"] for r in received(p) if r["項目"] == "配息")
        per.append({"ISIN": p["isin"] or p["case_no"], "商品": p["pos_name"], "狀態": state_text(p), "名目本金": p["principal"],
                    "累計已領配息": round(d, 2), "占累計配息(%)": round(d / total_div * 100, 1) if total_div else 0.0,
                    "配息報酬率(%)": round(d / p["principal"] * 100, 2) if p["principal"] else 0.0})
    st.markdown("**各部位累計已領配息**")
    pdf_ = pd.DataFrame(per)
    tot = {"ISIN": "合計", "商品": "", "狀態": "", "名目本金": pdf_["名目本金"].sum(), "累計已領配息": round(total_div, 2),
           "占累計配息(%)": 100.0 if total_div else 0.0, "配息報酬率(%)": round(total_div / total_cap * 100, 2) if total_cap else 0.0}
    st.dataframe(pd.concat([pdf_, pd.DataFrame([tot])], ignore_index=True), hide_index=True, width="stretch")
    if rows:
        df = pd.DataFrame(rows).sort_values("日期").reset_index(drop=True)
        df["累計配息"] = df["金額"].where(df["項目"] == "配息", 0).cumsum().round(2)  # 逐筆累計（只計配息）
        st.markdown("**入帳明細（新到舊；「累計配息」為截至該筆的全部部位配息累計）**")
        st.dataframe(df.sort_values("日期", ascending=False, kind="stable"), hide_index=True, width="stretch",
                     column_config={"金額": st.column_config.NumberColumn("金額 (USD)", format="%.2f"),
                                    "累計配息": st.column_config.NumberColumn("累計配息 (USD)", format="%.2f")})
    else:
        st.caption("尚無入帳紀錄。")
    st.caption("已實現損益 ＝ 已入帳配息；本金還回、或轉為股票的成本不計入損益。報酬率 ＝ 已領配息 ÷ 名目本金，年化報酬率依持有天數推算（進行中者算到今天）。"
               "承接股票帳面損益 ＝ 股數 ×（最新收盤 − 承接價），未計匯率與交易成本。若無手動配息紀錄，配息依說明書日程推算，實際入帳以券商對帳單為準。")


def client_view(c):
    st.title("📊 FCN 專屬即時回報")
    st.caption(f"{c['name']} 您好，進行中的部位顯示於上方；已出場或到期的部位保留在下方損益與入帳明細。")
    allp = c["positions"]
    if not allp:
        st.info("目前沒有部位資料。")
        return
    act = [p for p in allp if is_active(p)]
    if not act:
        st.info("目前沒有進行中的部位。")
    else:
        sts = [pos_status(p) for p in act]
        recv = sum(r["金額"] for p in allp for r in received(p) if r["項目"] == "配息")
        m = st.columns(5)
        m[0].metric("進行中部位", len(act))
        m[1].metric("總本金 (USD)", f"${sum(p['principal'] for p in act):,.0f}")
        m[2].metric("每月配息 (USD)", f"${sum(schedule(p)[1] for p in act):,.2f}")
        m[3].metric("累積已領配息 (USD)", f"${recv:,.2f}")
        m[4].metric("燈號", f"🟢{sts.count('g')} 🟡{sts.count('y')} 🔴{sts.count('r')}")
        st.markdown("#### 💰 未來現金流（依部位分色）")
        cashflow_charts(act)
        nm = lambda p: p["isin"] or p["case_no"]
        fl = [{"日期": d.date(), "部位": nm(p), "類型": "配息", "金額": round(a, 2)} for p in act for d, a in schedule(p)[0] if d > TODAY]
        fl += [{"日期": pd.Timestamp(p["end_date"]).date(), "部位": nm(p), "類型": "到期還本", "金額": p["principal"]} for p in act]
        with st.expander("現金流明細"):
            st.dataframe(pd.DataFrame(fl).sort_values("日期"), hide_index=True, width="stretch")
        st.markdown("---")
        st.markdown("### 🔎 FCN 組合明細")
        opts = {f"{LIGHT[pos_status(p)].split()[0]} {nm(p)}｜{p['pos_name']}": p for p in act}
        detail_view(opts[st.selectbox("選擇 ISIN CODE", list(opts))])
    history_view(allp)


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


def admin_view(db):
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

    sb = st.sidebar
    sb.header("🔒 營業員總控後台")
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
        st.rerun()

    st.title("🛠️ FCN 營業員總控管理系統")
    if not db:
        st.info("請先在左側新增客戶。")
        st.stop()
    acct = sb.selectbox("管理客戶", list(db), format_func=lambda a: f"{a}｜{db[a]['name']}")
    c = db[acct]
    url = f"{base_url()}/?k={c['token']}"
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
                             "到期日": p["end_date"], "下次KO評價日": nk_str(p), "本金": p["principal"], "標的現況": "；".join(det),
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
                with st.expander(f"{tag}｜[{p['case_no']}] {p['pos_name']}｜${p['principal']:,.0f}"):
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
db = load_data()
k = st.query_params.get("k")
if k:
    cl = next((x for x in db.values() if hmac.compare_digest(x["token"].encode("utf-8"), str(k).encode("utf-8"))), None)
    if cl:
        client_view(cl)
    else:
        st.error("連結無效或已失效，請向您的營業員索取新連結。")
else:
    admin_view(db)