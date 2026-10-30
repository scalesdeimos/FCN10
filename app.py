import streamlit as st
import pandas as pd
import yfinance as yf
import json
import os

# 設定網頁版面與 Dark Mode 專業金融風格
st.set_page_config(
    page_title="FCN 營業員總控與客戶即時回報系統",
    page_icon="📈",
    layout="wide"
)

# 自訂 CSS 樣式打造高質感金融 Dark Theme
st.markdown("""
    <style>
    .stApp { background-color: #0e1117; color: #fafafa; }
    .status-green { color: #48bb78; font-weight: bold; font-size: 1.1rem; }
    .status-yellow { color: #ecc94b; font-weight: bold; font-size: 1.1rem; }
    .status-red { color: #f56565; font-weight: bold; font-size: 1.1rem; }
    .badge-saved { background-color: #2f855a; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; }
    .badge-editing { background-color: #d69e2e; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; }
    </style>
""", unsafe_allow_html=True)

# 資料持久化檔案名稱
DATA_FILE = "fcn_clients.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 預設初始資料
    return {
        "12727-1": {
            "name": "預設大戶客戶",
            "positions": [
                {
                    "case_no": "FCN-2026-001",
                    "pos_name": "輝達台積電優利連動",
                    "principal": 150000,
                    "coupon_rate": 10.0,
                    "start_date": "2026-01-01",
                    "end_date": "2027-01-01",
                    "target_1": {"symbol": "NVDA", "init_price": 220.0, "strike_price": 173.12, "ki_price": 143.84},
                    "target_2": {"symbol": "TSM", "init_price": 150.0, "strike_price": 120.0, "ki_price": 95.0},
                    "target_3": {"symbol": "", "init_price": 0, "strike_price": 0, "ki_price": 0},
                    "target_4": {"symbol": "", "init_price": 0, "strike_price": 0, "ki_price": 0},
                    "target_5": {"symbol": "", "init_price": 0, "strike_price": 0, "ki_price": 0},
                }
            ]
        }
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# 初始化 Session State 並從檔案載入
if "clients_db" not in st.session_state:
    st.session_state.clients_db = load_data()

# 自動抓取真實歷史與最新股價函式
@st.cache_data(ttl=600)
def get_stock_data(symbol, start_date):
    if not symbol:
        return None, 0.0, 0.0
    try:
        ticker = yf.Ticker(symbol.strip())
        df = ticker.history(start=start_date)
        if df.empty:
            df = ticker.history(period="1y")
        
        latest_price = float(df['Close'].iloc[-1]) if not df.empty else 0.0
        prev_close = float(df['Close'].iloc[-2]) if len(df) > 1 else latest_price
        change_pct = ((latest_price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
        return df, latest_price, change_pct
    except Exception:
        return None, 0.0, 0.0

# 取得網址參數
query_params = st.query_params
client_mode = query_params.get("client", None)
if isinstance(client_mode, list):
    client_mode = client_mode[0] if client_mode else None

# ==================== 客戶端專屬畫面 ====================
if client_mode:
    target_client = client_mode.strip()
    
    # 每次讀取時重新從檔案載入最新資料，確保跨分頁同步
    st.session_state.clients_db = load_data()
    
    if target_client in st.session_state.clients_db:
        client_data = st.session_state.clients_db[target_client]
        
        st.title("📊 FCN 尊榮客戶專屬即時回報系統")
        st.markdown(f"**尊榮客戶**：客戶 {target_client} | 歡迎隨時掌握您的結構型商品部位與市場動態。")
        st.markdown("---")
        
        positions = client_data["positions"]
        total_principal = sum(p["principal"] for p in positions)
        total_monthly_coupon = sum(p["principal"] * (p["coupon_rate"] / 100.0 / 12.0) for p in positions)
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("當前受託總額 (USD)", f"${total_principal:,.2f} USD")
        with col2:
            st.metric("預計每月派息總額 (USD)", f"${total_monthly_coupon:,.2f} USD")
            
        st.markdown("---")
        
        if not positions:
            st.info("目前此客戶尚無任何投資部位資料。")
        else:
            pos_options = [f"{p['case_no']} - {p['pos_name']}" for p in positions]
            selected_pos_str = st.selectbox("📌 選擇您要檢視的投資部位專案：", pos_options)
            selected_idx = pos_options.index(selected_pos_str)
            curr_pos = positions[selected_idx]
            
            st.info(f"**目前檢視部位**：{curr_pos['pos_name']} (案件編號: {curr_pos['case_no']}) | 合約區間: {curr_pos['start_date']} ~ {curr_pos['end_date']} | 本金: ${curr_pos['principal']:,.2f} USD (年息 {curr_pos['coupon_rate']}%)")
            
            valid_targets = []
            for t_key in ["target_1", "target_2", "target_3", "target_4", "target_5"]:
                t = curr_pos[t_key]
                if t["symbol"]:
                    valid_targets.append(t)
                    
            if valid_targets:
                target_names = [t['symbol'].upper() for t in valid_targets]
                tabs = st.tabs([f"📈 標的：{name}" for name in target_names])
                
                for tab, t in zip(tabs, valid_targets):
                    with tab:
                        sym = t['symbol'].upper()
                        df, live_price, change_pct = get_stock_data(sym, curr_pos['start_date'])
                        
                        if live_price > 0 and live_price < t["ki_price"]:
                            status_html = '<span class="status-red">🔴 已跌破 KI 下限</span>'
                        elif live_price > 0 and live_price < t["strike_price"]:
                            status_html = '<span class="status-yellow">🟡 低於履約價 (警戒)</span>'
                        else:
                            status_html = '<span class="status-green">🟢 正常運行中</span>'
                            
                        st.markdown(f"### 🎯 標的：{sym}")
                        st.markdown(f"**目前狀態**： {status_html}", unsafe_allow_html=True)
                        
                        col_a, col_b, col_c = st.columns(3)
                        with col_a:
                            chg_color = "normal" if change_pct >= 0 else "inverse"
                            st.metric("最新收盤價 (即時)", f"${live_price:,.2f}", delta=f"{change_pct:.2f}%", delta_color=chg_color)
                        with col_b:
                            dist_strike = ((live_price - t['strike_price']) / t['strike_price']) * 100 if t['strike_price'] > 0 else 0
                            st.metric("履約價格 (固定)", f"${t['strike_price']:,.2f}", delta=f"{dist_strike:+.2f}% vs 現價")
                        with col_c:
                            dist_ki = ((live_price - t['ki_price']) / t['ki_price']) * 100 if t['ki_price'] > 0 else 0
                            st.metric("KI 下限價格 (固定)", f"${t['ki_price']:,.2f}", delta=f"{dist_ki:+.2f}% vs 現價")
                            
                        st.markdown("---")
                        
                        st.markdown("#### 📈 合約內期間真實股價走勢與固定履約/KI對照圖")
                        if df is not None and not df.empty:
                            chart_df = pd.DataFrame(index=df.index)
                            chart_df['實際股價走勢'] = df['Close']
                            chart_df['履約價 (固定)'] = t['strike_price']
                            chart_df['KI下限價 (固定)'] = t['ki_price']
                            st.line_chart(chart_df)
                        else:
                            st.warning("目前無法取得該標的歷史走勢資料。")
                            
                        st.markdown("---")
                        
                        st.markdown("#### 📰 最新相關新聞與市場資訊 (近一個月動態)")
                        st.markdown(f"- [{sym} 最新股價行情、近一個月新聞與財報表現 - Yahoo股市](https://tw.stock.yahoo.com/quote/{sym})")
                        st.markdown(f"- [{sym} 鉅亨網即時財經新聞與個股行情分析](https://www.cnyes.com/search/news?keyword={sym})")
            else:
                st.warning("此部位尚未設定任何美股標的。")
    else:
        st.error(f"找不到此客戶帳號 [{target_client}] 或網址參數錯誤。請確認後台是否已成功建立該客戶檔案。")

# ==================== 營業員總控管理後台 ====================
else:
    st.sidebar.markdown("🔒 **營業員總控管理後台**")
    st.sidebar.markdown('<p><span class="badge-saved">🟢 系統狀態：已儲存就緒</span></p>', unsafe_allow_html=True)
    
    st.title("🛠️ FCN 營業員總控管理與自動化部位維護系統")
    st.markdown("管理員專屬介面：期初價、履約價、KI價為合約固定值；每日收盤價與走勢自動連線抓取真實市場數據。")
    st.markdown("---")
    
    st.sidebar.markdown("### ➕ 快速新增客戶")
    new_client_id = st.sidebar.text_input("輸入客戶帳號 (例如: 11278-1)", placeholder="11278-1")
    if st.sidebar.button("新增客戶"):
        clean_id = new_client_id.strip()
        if clean_id and clean_id not in st.session_state.clients_db:
            st.session_state.clients_db[clean_id] = {"name": f"客戶 {clean_id}", "positions": []}
            save_data(st.session_state.clients_db)
            st.sidebar.success(f"成功新增客戶 {clean_id}！")
            st.rerun()
        elif clean_id in st.session_state.clients_db:
            st.sidebar.warning("此客戶帳號已存在！")
            
    st.sidebar.markdown("### ⚙️ 客戶與網址管理")
    client_list = list(st.session_state.clients_db.keys())
    selected_client = st.sidebar.selectbox("選擇要管理的客戶：", client_list)
    
    # 動態抓取網址根目錄
    try:
        base_url = st.context.headers.get("Host", "localhost:8501")
        if "http" not in base_url:
            base_url = f"https://{base_url}" if "localhost" not in base_url else f"http://{base_url}"
    except Exception:
        base_url = "http://localhost:8501"
        
    client_specific_url = f"{base_url}/?client={selected_client}"
    
    st.sidebar.markdown(f"**當前管理客戶**：{selected_client}")
    st.sidebar.code(client_specific_url)
    st.sidebar.link_button("🔍 點擊預覽此客戶端畫面", client_specific_url)
    
    if st.sidebar.button("🗑️ 刪除此客戶檔案", type="primary"):
        if len(st.session_state.clients_db) > 1:
            del st.session_state.clients_db[selected_client]
            save_data(st.session_state.clients_db)
            st.success("已刪除該客戶檔案！")
            st.rerun()
        else:
            st.error("至少需保留一個客戶檔案！")

    st.subheader(f"📋 客戶 [{selected_client}] 的 FCN 部位資料管理")
    current_client_obj = st.session_state.clients_db[selected_client]
    
    with st.expander("➕ 為此客戶新增一筆新的 FCN 投資部位", expanded=False):
        st.markdown('<p><span class="badge-editing">✏️ 編輯模式：填寫完畢後按下儲存</span></p>', unsafe_allow_html=True)
        with st.form("add_position_form"):
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                case_no = st.text_input("案件編號", value="FCN-2026-002")
                principal = st.number_input("投資本金 (USD)", value=50000.0)
            with col_b:
                pos_name = st.text_input("部位名稱", value="美股精選連動收益")
                coupon_rate = st.number_input("年利率 (%)", value=11.5)
            with col_c:
                start_date = st.text_input("合約期初日期 (YYYY-MM-DD)", value="2026-01-01")
                end_date = st.text_input("合約期末日期 (YYYY-MM-DD)", value="2027-01-01")
                
            st.markdown("#### 設定連結美股標的 (輸入美股代號，如 NVDA, TSM，期初/履約/KI價為合約固定值)")
            
            for i in range(1, 6):
                st.markdown(f"---  \n**標的 {i}** {'(必填)' if i==1 else '(選填)'}")
                c_1, c_2, c_3, c_4 = st.columns(4)
                sym = c_1.text_input(f"美股代號 {i}", value="NVDA" if i==1 else ("TSM" if i==2 else ""))
                init_p = c_2.number_input(f"期初價 {i} (固定)", value=220.0 if i==1 else (150.0 if i==2 else 0.0), key=f"init_{i}")
                strike_p = c_3.number_input(f"履約價 {i} (固定)", value=173.12 if i==1 else (120.0 if i==2 else 0.0), key=f"strike_{i}")
                ki_p = c_4.number_input(f"下限價(KI) {i} (固定)", value=143.84 if i==1 else (95.0 if i==2 else 0.0), key=f"ki_{i}")
                
                if i == 1:
                    t1_sym, t1_init, t1_strike, t1_ki = sym, init_p, strike_p, ki_p
                elif i == 2:
                    t2_sym, t2_init, t2_strike, t2_ki = sym, init_p, strike_p, ki_p
                elif i == 3:
                    t3_sym, t3_init, t3_strike, t3_ki = sym, init_p, strike_p, ki_p
                elif i == 4:
                    t4_sym, t4_init, t4_strike, t4_ki = sym, init_p, strike_p, ki_p
                elif i == 5:
                    t5_sym, t5_init, t5_strike, t5_ki = sym, init_p, strike_p, ki_p

            submitted = st.form_submit_button("確認儲存新部位")
            if submitted:
                new_pos = {
                    "case_no": case_no,
                    "pos_name": pos_name,
                    "principal": principal,
                    "coupon_rate": coupon_rate,
                    "start_date": start_date,
                    "end_date": end_date,
                    "target_1": {"symbol": t1_sym, "init_price": t1_init, "strike_price": t1_strike, "ki_price": t1_ki},
                    "target_2": {"symbol": t2_sym, "init_price": t2_init, "strike_price": t2_strike, "ki_price": t2_ki},
                    "target_3": {"symbol": t3_sym, "init_price": t3_init, "strike_price": t3_strike, "ki_price": t3_ki},
                    "target_4": {"symbol": t4_sym, "init_price": t4_init, "strike_price": t4_strike, "ki_price": t4_ki},
                    "target_5": {"symbol": t5_sym, "init_price": t5_init, "strike_price": t5_strike, "ki_price": t5_ki},
                }
                current_client_obj["positions"].append(new_pos)
                save_data(st.session_state.clients_db)
                st.success("成功新增部位！資料已寫入檔案並自動儲存。")
                st.rerun()

    st.markdown("### 📊 現有投資部位清單與管理")
    if not current_client_obj["positions"]:
        st.info("此客戶目前尚無任何 FCN 投資部位。")
    else:
        for idx, pos in enumerate(current_client_obj["positions"]):
            with st.expander(f"📁 [{pos['case_no']}] {pos['pos_name']} (本金: ${pos['principal']:,.2f})"):
                col_x, col_y = st.columns([3, 1])
                with col_x:
                    st.write(f"**合約期間**: {pos['start_date']} ~ {pos['end_date']} | **年利率**: {pos['coupon_rate']}%")
                with col_y:
                    if st.button(f"刪除此部位", key=f"del_pos_{idx}"):
                        current_client_obj["positions"].pop(idx)
                        save_data(st.session_state.clients_db)
                        st.success("部位已刪除！")
                        st.rerun()