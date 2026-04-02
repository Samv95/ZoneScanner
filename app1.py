import pandas as pd
import numpy as np
import yfinance as yf
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import streamlit as st

# --- [ Page Configuration & CSS ] ---
st.set_page_config(page_title="Zone Scanner", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
    .stApp { background-color: #0B141E; color: #FFFFFF; }
    h1 { text-align: center; color: #FFFFFF; margin-bottom: 20px; }
    .stock-card {
        background-color: #122131; border: 1px solid #1E364F;
        border-radius: 8px; padding: 20px; margin-bottom: 20px;
        text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); transition: transform 0.2s;
    }
    .stock-card:hover { transform: scale(1.02); border-color: #3B82F6; }
    .card-symbol { font-size: 22px; font-weight: bold; margin-bottom: 5px; }
    .card-price { font-size: 18px; color: #E5E7EB; margin-bottom: 15px; }
    .card-tf { font-size: 12px; background-color: #1E3A8A; padding: 4px 8px; border-radius: 4px; }
    .zone-demand { color: #10B981; font-weight: bold; }
    .zone-supply { color: #EF4444; font-weight: bold; }
    div[role="radiogroup"] { justify-content: center; }
</style>
""", unsafe_allow_html=True)

# --- [ Session State Initialization ] ---
if 'raw_results' not in st.session_state:
    st.session_state.raw_results = []
if 'scan_complete' not in st.session_state:
    st.session_state.scan_complete = False

# --- [ Backend Logic ] ---
# --- [ Secure Google Sheets Integration ] ---
@st.cache_data(ttl=3600)
def get_tickers_from_sheet(sheet_input):
    try:
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        
        # Security Check: Cloud par hai ya Local PC par?
        if "gcp_service_account" in st.secrets:
            # Cloud Execution (Reads from encrypted secrets)
            creds = ServiceAccountCredentials.from_json_keyfile_dict(st.secrets["gcp_service_account"], scope)
        else:
            # Local Execution (Reads from your PC file)
            creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
            
        client = gspread.authorize(creds)
        sheet = client.open_by_url(sheet_input).sheet1 if "docs.google.com" in sheet_input else client.open(sheet_input).sheet1
        return [t.strip() for t in sheet.col_values(1)[1:] if t.strip()]
    except Exception as e:
        st.error(f"⚠️ Connection Error: {e}")
        return []

def fetch_data(symbol, tf_key):
    try:
        if tf_key in ['1d', '1wk', '1mo']:
            df = yf.download(symbol, period="5y", interval=tf_key, progress=False)
            return df if not df.empty else None
        raw_df = yf.download(symbol, period="10y", interval="1d", progress=False)
        if raw_df.empty: return None
        rule = '3ME' if tf_key == '3mo' else '6ME' if tf_key == '6mo' else '12ME'
        return raw_df.resample(rule).agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    except: return None

def calculate_zones(df):
    # FVG logic needs at least 3 candles (Base, Mom, Current)
    if df is None or len(df) < 3: return None 
    try:
        opens, closes = df['Open'].values, df['Close'].values
        highs, lows = df['High'].values, df['Low'].values
        active_demand, active_supply = [], []
        
        # Loop starts from 2 to check Base [i-2], Mom [i-1], and Conf/FVG [i]
        for i in range(2, len(df)):
            # 1. Base Candle [i-2]
            bOpen, bClose, bHigh, bLow = opens[i-2], closes[i-2], highs[i-2], lows[i-2]
            
            # 2. Momentum Candle [i-1]
            mOpen, mClose, mHigh, mLow = opens[i-1], closes[i-1], highs[i-1], lows[i-1]
            
            # 3. Confirmation Candle (Current Bar for FVG Check) [i]
            cHigh, cLow = highs[i], lows[i]
            
            # Calculations
            bBody, bRange = abs(bClose - bOpen), bHigh - bLow
            mBody, mRange = abs(mClose - mOpen), mHigh - mLow
            
            # Error Handling / Edge Cases
            validRanges = (bRange > 0) and (mRange > 0)
            if not validRanges: continue
            
            # Core Logic & Conditions
            isMomSizeValid  = (mBody > (bBody * 2))
            isMomRangeValid = (mBody > (mRange * 0.5))
            
            isGreenMom = (mClose > mOpen)
            isRedMom   = (mClose < mOpen)
            
            # FVG Logic
            hasBullishFVG = (cLow > bHigh)
            hasBearishFVG = (cHigh < bLow)
            
            # Pure price action & FVG breakout logic
            bullishValid = isGreenMom and (mClose > bHigh) and hasBullishFVG
            bearishValid = isRedMom   and (mClose < bLow)  and hasBearishFVG
            
            # Final Triggers
            triggerBullishZone = isMomSizeValid and isMomRangeValid and bullishValid
            triggerBearishZone = isMomSizeValid and isMomRangeValid and bearishValid
            
            # Zone Creation in Memory
            if triggerBullishZone:
                active_demand.append({'top': bHigh, 'bottom': min(bLow, mLow)})
            if triggerBearishZone:
                active_supply.append({'top': max(bHigh, mHigh), 'bottom': bLow})
                
            # Mitigation & Memory Overflow Safety (Matches Pine Script Logic)
            active_demand = [z for z in active_demand if closes[i] >= z['bottom']] # Remove if close < bottom
            active_supply = [z for z in active_supply if closes[i] <= z['top']]    # Remove if close > top
            
        latest_price = float(np.ravel(closes[-1])[0])
        
        # Proximity Logic (Unchanged as per your UI)
        for z in reversed(active_demand):
            if z['top'] > 0:
                dist = (latest_price - z['top']) / z['top']
                if latest_price <= z['top'] and latest_price >= z['bottom']: return "Demand", z['top'], "In Zone / Reacting"
                elif dist > 0 and dist <= 0.01: return "Demand", z['top'], "Approaching"
                    
        for z in reversed(active_supply):
            if z['bottom'] > 0:
                dist = (z['bottom'] - latest_price) / z['bottom']
                if latest_price >= z['bottom'] and latest_price <= z['top']: return "Supply", z['bottom'], "In Zone / Reacting"
                elif dist > 0 and dist <= 0.01: return "Supply", z['bottom'], "Approaching"
    except: pass
    return None

# --- [ UI Layout & Instant Filters ] ---
st.markdown("<h1>Zone Scanner</h1>", unsafe_allow_html=True)

# Main Dashboard Filters (Instantly update view without rescanning)
col1, col2, col3 = st.columns([1, 1, 1.5])
with col1:
    criteria_filter = st.radio("Status", ["Approaching", "In Zone / Reacting", "All"], horizontal=True, index=2)
with col2:
    zone_filter = st.radio("Zone Type", ["Demand", "Supply", "Both"], horizontal=True, index=2)
with col3:
    # 🆕 Change 2: Instant Timeframe UI Filter
    ui_tf_filter = st.multiselect("View Timeframes", ["Daily", "Weekly", "Monthly", "Quarterly", "Half Yearly", "Yearly"], default=["Daily", "Weekly", "Monthly"])

st.markdown("<br>", unsafe_allow_html=True)

# Sidebar Settings
st.sidebar.header("⚙️ Scan Settings")
# 🆕 Change 1: Hardcoded default name to "F&O Stock List"
sheet_url = st.sidebar.text_input("Google Sheet Name", "F&O Stock List")

# Yeh wo timeframes hain jo background mein ek hi baar scan honge
tf_options = {"Daily": "1d", "Weekly": "1wk", "Monthly": "1mo", "Quarterly": "3mo", "Half Yearly": "6mo", "Yearly": "12mo"}
selected_scan_tfs = st.sidebar.multiselect("Timeframes to Scan", list(tf_options.keys()), default=["Daily", "Weekly", "Monthly", "Quarterly", "Half Yearly", "Yearly"])

run_scan = st.sidebar.button("🚀 Start Scan", use_container_width=True)
st.sidebar.caption("Ek baar scan hone ke baad, main screen se instantly filters change karein.")

st.markdown("---")

# --- [ Execution Loop (Runs ONLY when Start Scan is clicked) ] ---
if run_scan:
    tickers = get_tickers_from_sheet(sheet_url)
    if not tickers:
        st.error("No stocks found in the Google Sheet.")
    else:
        # Purani memory clear karna
        st.session_state.raw_results = []
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Nested loops ko optimize karke fast data fetching
        for i, sym in enumerate(tickers):
            fetch_sym = sym if sym.endswith(('.NS', '.BO')) else f"{sym}.NS"
            status_text.text(f"Scanning: {sym}...")
            
            for tf_label in selected_scan_tfs:
                df = fetch_data(fetch_sym, tf_options[tf_label])
                res = calculate_zones(df)
                
                if res:
                    st.session_state.raw_results.append({
                        "Sym": sym, "TF": tf_label, "Type": res[0], 
                        "LTP": float(np.ravel(df['Close'].iloc[-1])[0]), 
                        "Level": float(np.ravel(res[1])[0]), "Status": res[2]
                    })
            progress_bar.progress((i + 1) / len(tickers))
            
        status_text.empty()
        progress_bar.empty()
        st.session_state.scan_complete = True

# --- [ Display Logic (Instant Application of Filters) ] ---
if st.session_state.scan_complete:
    filtered_results = []
    
    # Bina API calls kiye sidhe Virtual Memory se filter karna
    for res in st.session_state.raw_results:
        if criteria_filter != "All" and res["Status"] != criteria_filter: continue
        if zone_filter != "Both" and res["Type"] != zone_filter: continue
        if res["TF"] not in ui_tf_filter: continue # Instant TF filtering
        filtered_results.append(res)
    
    # Drawing Grid Cards
    if filtered_results:
        st.success(f"Showing {len(filtered_results)} matches based on filters")
        cols = st.columns(5)
        for index, res in enumerate(filtered_results):
            col = cols[index % 5]
            color_class = "zone-demand" if res["Type"] == "Demand" else "zone-supply"
            
            with col:
                st.markdown(f"""
                <div class="stock-card">
                    <div class="card-symbol">{res['Sym']}</div>
                    <div class="card-price">₹{res['LTP']:.2f}</div>
                    <div class="{color_class}">{res['Type']}</div>
                    <div style="font-size: 13px; color:#A0AEC0; margin-top:2px;">({res['Status']})</div>
                    <div style="font-size: 14px; margin-top: 10px;">Zone: ₹{res['Level']:.2f}</div>
                    <br>
                    <span class="card-tf">{res['TF']}</span>
                </div>
                """, unsafe_allow_html=True)
    else:
        st.info("No stocks matched your current filter criteria.")
