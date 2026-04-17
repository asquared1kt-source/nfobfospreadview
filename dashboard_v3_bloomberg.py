# ─────────────────────────────────────────────
# dashboard_v3_bloomberg.py — Bloomberg Dark Theme
# Workflow identical to original dashboard_v3.py
# Run with: streamlit run dashboard_v3_bloomberg.py
# ─────────────────────────────────────────────

import os
import base64
import pyotp
import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import time
import datetime as _dt
from datetime import date
from urllib.parse import parse_qs, urlparse
from plotly.subplots import make_subplots
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
from fyers_apiv3 import fyersModel

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "YOUR_APP_ID-100")
SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "YOUR_SECRET_KEY")
TOKEN_FILE  = "access_token.txt"
REFRESH_SECONDS = 10

# ─────────────────────────────────────────────
# AUTO TOKEN (TOTP)
# ─────────────────────────────────────────────

def get_secret(key):
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, "")

def b64(value):
    return base64.b64encode(str(value).encode()).decode()

def generate_token():
    client_id  = get_secret("FYERS_CLIENT_ID")
    secret_key = get_secret("FYERS_SECRET_KEY")
    username   = get_secret("FYERS_USERNAME")
    pin        = get_secret("FYERS_PIN")
    totp_key   = get_secret("FYERS_TOTP_KEY")
    redirect_uri = "http://127.0.0.1:8080/"

    missing = [k for k, v in {
        "FYERS_CLIENT_ID": client_id, "FYERS_SECRET_KEY": secret_key,
        "FYERS_USERNAME": username, "FYERS_PIN": pin, "FYERS_TOTP_KEY": totp_key,
    }.items() if not v]
    if missing:
        return None, f"Missing credentials: {', '.join(missing)}"

    try:
        s = requests.Session()
        r1 = s.post("https://api-t2.fyers.in/vagator/v2/send_login_otp_v2",
                    json={"fy_id": b64(username), "app_id": "2"}, timeout=10)
        try:
            r1d = r1.json()
        except Exception:
            return None, f"Step 1 bad response (status {r1.status_code}): {r1.text[:200]}"
        if r1d.get("s") != "ok":
            return None, f"Step 1 failed: {r1d}"

        totp_code = pyotp.TOTP(totp_key).now()
        r2  = s.post("https://api-t2.fyers.in/vagator/v2/verify_otp",
                     json={"request_key": r1d["request_key"], "otp": totp_code}, timeout=10)
        r2d = r2.json()
        if r2d.get("s") != "ok":
            return None, f"Step 2 failed: {r2d}"

        r3  = s.post("https://api-t2.fyers.in/vagator/v2/verify_pin_v2",
                     json={"request_key": r2d["request_key"], "identity_type": "pin",
                           "identifier": b64(pin)}, timeout=10)
        r3d = r3.json()
        if r3d.get("s") != "ok":
            return None, f"Step 3 failed: {r3d}"

        app_id = client_id.split("-")[0]
        r4  = s.post("https://api-t1.fyers.in/api/v3/token", json={
            "fyers_id": username, "app_id": app_id, "redirect_uri": redirect_uri,
            "appType": "100", "code_challenge": "", "state": "sample",
            "scope": "", "nonce": "", "response_type": "code", "create_cookie": True
        }, headers={"Authorization": f"Bearer {r3d['data']['access_token']}"}, timeout=10)
        r4d = r4.json()
        if r4d.get("s") != "ok":
            return None, f"Step 4 failed: {r4d}"

        auth_code = parse_qs(urlparse(r4d["Url"]).query).get("auth_code", [None])[0]
        if not auth_code:
            return None, f"No auth_code in: {r4d}"

        session = fyersModel.SessionModel(
            client_id=client_id, secret_key=secret_key,
            redirect_uri=redirect_uri, response_type="code", grant_type="authorization_code"
        )
        session.set_token(auth_code)
        r5d   = session.generate_token()
        token = r5d.get("access_token")
        if not token:
            return None, f"Step 5 failed: {r5d}"
        return token, None
    except Exception as e:
        return None, f"Exception: {str(e)}"

# ─────────────────────────────────────────────
# FYERS CLIENT
# ─────────────────────────────────────────────

def load_fyers_from_file():
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError("Token file not found")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    return fyersModel.FyersModel(client_id=get_secret("FYERS_CLIENT_ID") or CLIENT_ID,
                                 token=token, log_path="")

@st.cache_resource
def get_shared_token():
    """Generates token once — shared across ALL browser sessions on this server."""
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token, error = generate_token()
    if token:
        return token
    raise RuntimeError(f"Fyers login failed: {error}")

def get_fyers_client():
    try:
        token = get_shared_token()
        cid   = get_secret("FYERS_CLIENT_ID") or CLIENT_ID
        return fyersModel.FyersModel(client_id=cid, token=token, log_path="")
    except Exception as e:
        st.error(f"❌ Login failed: {e}")
        return None

# ─────────────────────────────────────────────
# EXPIRY FETCHER
# ─────────────────────────────────────────────

_UNDERLYING_SYM = {
    "SENSEX":     "BSE:SENSEX-INDEX",
    "BANKEX":     "BSE:BANKEX-INDEX",
    "NIFTY":      "NSE:NIFTY50-INDEX",
    "BANKNIFTY":  "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":   "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
}
_MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

@st.cache_resource
def fetch_expiries_for(token, fyers_sym):
    from collections import defaultdict
    try:
        cid   = get_secret("FYERS_CLIENT_ID") or CLIENT_ID
        fyers = fyersModel.FyersModel(client_id=cid, token=token, log_path="")
        resp  = fyers.optionchain(data={"symbol": fyers_sym, "strikecount": 1, "timestamp": ""})
        if not (resp and resp.get("s") == "ok"):
            return {}
        raw    = resp.get("data", {}).get("expiryData", [])
        parsed = []
        for entry in raw:
            if not isinstance(entry, dict): continue
            d = entry.get("date", "")
            try:
                dd, mm, yyyy = d.split("-")
                dd, mm, yyyy = int(dd), int(mm), int(yyyy)
            except Exception:
                continue
            yy  = yyyy % 100
            mon = _MONTHS[mm - 1]
            parsed.append((yy, mm, dd, mon))
        by_month     = defaultdict(list)
        for yy, mm, dd, mon in parsed:
            by_month[(yy, mm)].append(dd)
        last_of_month = {k: max(v) for k, v in by_month.items()}
        result = {}
        for yy, mm, dd, mon in parsed:
            is_monthly = (dd == last_of_month[(yy, mm)])
            if is_monthly:
                code  = f"{yy:02d}{mon}"
                label = f"{dd:02d} {mon} {yy:02d} (M)"
            else:
                code  = f"{yy:02d}{mm:02d}{dd:02d}"
                label = f"{dd:02d} {mon} {yy:02d} (W)"
            result[label] = code
        return result
    except Exception:
        return {}

def get_expiries_for(exchange, underlying):
    try:
        token = get_shared_token()
        sym   = _UNDERLYING_SYM.get(underlying.upper(), f"{exchange}:{underlying}-INDEX")
        return fetch_expiries_for(token, sym)
    except Exception:
        return {}

def expiry_selectbox(label, opts_dict, manual_key, select_key, default_manual):
    if opts_dict:
        codes        = list(opts_dict.values())
        code_to_label = {v: k for k, v in opts_dict.items()}
        return st.selectbox(label, codes,
                            format_func=lambda c: code_to_label.get(c, c),
                            key=select_key)
    return st.text_input(label, value=default_manual, key=manual_key)

# ─────────────────────────────────────────────
# SYMBOL BUILDER
# ─────────────────────────────────────────────

def build_symbol(exchange, underlying, expiry, option_type, strike):
    ot     = "CE" if option_type.upper() in ("C", "CE") else "PE"
    expiry = expiry.strip().upper()
    if any(c.isalpha() for c in expiry):
        return f"{exchange}:{underlying}{expiry}{strike}{ot}"
    yy, mm, dd = expiry[0:2], expiry[2:4], expiry[4:6]
    return f"{exchange}:{underlying}{yy}{int(mm)}{dd}{strike}{ot}"

# ─────────────────────────────────────────────
# FETCH CANDLES
# ─────────────────────────────────────────────

def fetch_candles(fyers, symbol, interval, date_str=None):
    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")
    response = fyers.history(data={
        "symbol": symbol, "resolution": str(interval),
        "date_format": "1", "range_from": date_str,
        "range_to":   date_str, "cont_flag": "1"
    })
    if response.get("s") != "ok":
        return pd.DataFrame()
    df = pd.DataFrame(response["candles"],
                      columns=["timestamp","open","high","low","close","volume"])
    df["datetime"] = (pd.to_datetime(df["timestamp"], unit="s")
                      .dt.tz_localize("UTC")
                      .dt.tz_convert("Asia/Kolkata")
                      .dt.tz_localize(None))
    return df.drop(columns=["timestamp"]).set_index("datetime")

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
def load_css():
    st.markdown("""
    <style>

    :root {
        --bg: #0a0a0a;
        --bg2: #111111;
        --sidebar: #0d0d0d;
        --card: #161616;
        --card2: #1c1c1c;
        --card-bdr: #2a2a2a;
        --text: #e8e0d0;
        --text2: #a89880;
        --text3: #5a5248;
        --ce: #ff6633;
        --pe: #00cc66;
        --diff: #ffaa00;
        --blue: #3399ff;
        --purple: #9966ff;
        --divider: #222222;
        --orange: #ff6600;
        --yellow: #ffcc00;
    }

    html, body {
        background: var(--bg) !important;
    }

    .stApp {
        background: var(--bg) !important;
        color: var(--text) !important;
    }

    /* Kill streamlit chrome */
    header[data-testid="stHeader"] { display: none !important; }
    div[data-testid="stDecoration"] { display: none !important; }
    button[data-testid="collapsedControl"] { display: none !important; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: var(--sidebar) !important;
        border-right: 1px solid #1a1a1a !important;
        min-width: 380px !important;
        max-width: 380px !important;
    }

    /* Buttons — FIXED COMMENT */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="baseButton-primary"] {
        background: var(--orange) !important;
        color: #000 !important;
        border: none !important;
        border-radius: 2px !important;
        font-weight: 700 !important;
        font-size: 11px !important;
        letter-spacing: 1.5px !important;
        text-transform: uppercase !important;
    }

    .stButton > button[kind="primary"]:hover {
        background: #cc5200 !important;
    }

    /* Inputs */
    .stTextInput input,
    .stNumberInput input,
    .stSelectbox div[data-baseweb="select"] > div {
        background: var(--card) !important;
        border-color: #2c2c2c !important;
        color: var(--text) !important;
        font-size: 12px !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab"] {
        color: var(--text3) !important;
    }

    .stTabs [aria-selected="true"] {
        color: var(--orange) !important;
        border-bottom: 2px solid var(--orange) !important;
    }

    </style>
    """, unsafe_allow_html=True)

st.set_page_config(
    page_title="NFO/BFO Spread Terminal",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ─────────────────────────────────────────────
# BLOOMBERG DARK THEME
# ─────────────────────────────────────────────

BBG = {
    "bg":         "#0a0a0a",
    "bg2":        "#111111",
    "sidebar":    "#0d0d0d",
    "card":       "#161616",
    "card2":      "#1c1c1c",
    "card_bdr":   "#2a2a2a",
    "text":       "#e8e0d0",
    "text2":      "#a89880",
    "text3":      "#5a5248",
    "ce":         "#ff6633",   # Bloomberg orange-red
    "pe":         "#00cc66",   # Bloomberg green
    "diff":       "#ffaa00",   # Bloomberg amber
    "accent":     "#ff6600",   # Bloomberg orange
    "accent2":    "#cc3300",
    "blue":       "#3399ff",
    "purple":     "#9966ff",
    "divider":    "#222222",
    "plot_bg":    "#0e0e0e",
    "grid":       "#1e1e1e",
    "header_bg":  "#0d0d0d",
    "orange":     "#ff6600",
    "yellow":     "#ffcc00",
}

st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
    --bg:        {BBG["bg"]};
    --bg2:       {BBG["bg2"]};
    --sidebar:   {BBG["sidebar"]};
    --card:      {BBG["card"]};
    --card2:     {BBG["card2"]};
    --card-bdr:  {BBG["card_bdr"]};
    --text:      {BBG["text"]};
    --text2:     {BBG["text2"]};
    --text3:     {BBG["text3"]};
    --ce:        {BBG["ce"]};
    --pe:        {BBG["pe"]};
    --diff:      {BBG["diff"]};
    --accent:    {BBG["accent"]};
    --blue:      {BBG["blue"]};
    --purple:    {BBG["purple"]};
    --divider:   {BBG["divider"]};
    --orange:    {BBG["orange"]};
    --yellow:    {BBG["yellow"]};
}}

* {{ font-family: 'IBM Plex Sans', 'Courier New', monospace !important; }}
code, .mono, .metric-value, .val-ce, .val-pe, .val-diff, .val-time, .val-blue, .val-purple {{
    font-family: 'IBM Plex Mono', 'Courier New', monospace !important;
}}

html, body {{ background: var(--bg) !important; }}

.stApp {{
    background: var(--bg) !important;
    color: var(--text) !important;
}}

/* Kill streamlit chrome */
header[data-testid="stHeader"] {{ background: transparent !important; height: 0 !important; min-height: 0 !important; }}
header[data-testid="stHeader"] > * {{ display: none !important; }}
div[data-testid="stDecoration"] {{ display: none !important; }}
button[data-testid="collapsedControl"] {{ display: none !important; }}
.block-container {{ padding-top: 0.4rem !important; padding-bottom: 1rem !important; }}

/* Sidebar */
section[data-testid="stSidebar"] {{
    background: var(--sidebar) !important;
    border-right: 1px solid #1a1a1a !important;
    min-width: 380px !important;
    max-width: 380px !important;
}}
section[data-testid="stSidebar"] * {{ color: var(--text) !important; }}
section[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div,
section[data-testid="stSidebar"] .stTextInput input,
section[data-testid="stSidebar"] .stNumberInput input {{
    background: var(--card) !important;
    border-color: var(--card-bdr) !important;
    color: var(--text) !important;
    border-radius: 3px !important;
}}

/* ─── Bloomberg Top Bar ─── */
.bbg-topbar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    height: 48px;
    background: var(--bg2);
    border-bottom: 2px solid var(--orange);
    margin-bottom: 16px;
}}
.bbg-logo {{
    display: flex;
    align-items: center;
    gap: 12px;
}}
.bbg-logo-box {{
    background: var(--orange);
    color: #000 !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-weight: 700;
    font-size: 13px;
    padding: 4px 10px;
    letter-spacing: 1px;
}}
.bbg-title {{
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 13px;
    font-weight: 600;
    color: var(--text) !important;
    letter-spacing: 1px;
    text-transform: uppercase;
}}
.bbg-subtitle {{
    font-size: 10px;
    color: var(--text3) !important;
    font-family: 'IBM Plex Mono', monospace !important;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}}
.bbg-pills {{
    display: flex;
    gap: 12px;
    align-items: center;
}}
.bbg-pill {{
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
    padding: 3px 10px;
    border: 1px solid;
    text-transform: uppercase;
}}
.pill-live {{
    color: var(--pe) !important;
    border-color: var(--pe) !important;
    background: rgba(0,204,102,0.08);
    animation: blink 1.8s infinite;
}}
.pill-time {{
    color: var(--yellow) !important;
    border-color: #3a3020 !important;
    background: rgba(255,204,0,0.05);
}}
@keyframes blink {{
    0%, 100% {{ opacity: 1; }}
    50%  {{ opacity: 0.4; }}
}}

/* ─── Metric Cards — Bloomberg style ─── */
.metrics-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin-bottom: 16px;
}}
@media (max-width: 900px) {{
    .metrics-grid {{ grid-template-columns: repeat(2, 1fr); }}
}}
.metric-card {{
    background: var(--card);
    border: 1px solid var(--card-bdr);
    border-top: 2px solid;
    padding: 14px 16px 12px;
    position: relative;
    overflow: hidden;
}}
.card-ce   {{ border-top-color: var(--ce); }}
.card-pe   {{ border-top-color: var(--pe); }}
.card-diff {{ border-top-color: var(--diff); }}
.card-time {{ border-top-color: var(--blue); }}
.card-purple {{ border-top-color: var(--purple); }}

.metric-label {{
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--text3) !important;
    margin-bottom: 8px;
    font-family: 'IBM Plex Mono', monospace !important;
}}
.metric-value {{
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 26px;
    font-weight: 600;
    line-height: 1;
    margin-bottom: 6px;
}}
.val-ce     {{ color: var(--ce) !important; }}
.val-pe     {{ color: var(--pe) !important; }}
.val-diff   {{ color: var(--diff) !important; }}
.val-time   {{ color: var(--blue) !important; font-size: 20px; }}
.val-blue   {{ color: var(--blue) !important; }}
.val-purple {{ color: var(--purple) !important; }}

.metric-sub {{
    font-size: 10px;
    color: var(--text3) !important;
    font-family: 'IBM Plex Mono', monospace !important;
    letter-spacing: 0.3px;
}}
.metric-badge {{
    position: absolute;
    top: 10px; right: 12px;
    font-size: 16px;
    opacity: 0.25;
}}
.metric-divider {{
    width: 100%;
    height: 1px;
    background: var(--divider);
    margin: 8px 0;
}}

/* ─── Controls / Settings row ─── */
.section-label {{
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--text3);
    margin-bottom: 6px;
    font-family: 'IBM Plex Mono', monospace !important;
    border-left: 2px solid var(--orange);
    padding-left: 8px;
}}

/* ─── Streamlit widgets — dark override ─── */
.stSelectbox div[data-baseweb="select"] > div,
.stTextInput input,
.stNumberInput input {{
    background: var(--card) !important;
    border-color: #2c2c2c !important;
    color: var(--text) !important;
    border-radius: 2px !important;
    font-size: 12px !important;
    font-family: 'IBM Plex Mono', monospace !important;
}}
.stSelectbox div[data-baseweb="select"] > div:hover,
.stTextInput input:focus,
.stNumberInput input:focus {{
    border-color: var(--orange) !important;
    box-shadow: 0 0 0 1px var(--orange) !important;
}}
.stSelectbox label, .stTextInput label, .stNumberInput label,
.stSelectbox [data-baseweb="select"] span {{
    font-size: 10px !important;
    color: var(--text3) !important;
    font-family: 'IBM Plex Mono', monospace !important;
    letter-spacing: 0.5px !important;
    text-transform: uppercase !important;
}}
.stDateInput input {{
    background: var(--card) !important;
    border-color: #2c2c2c !important;
    color: var(--text) !important;
    border-radius: 2px !important;
    font-size: 12px !important;
}}
.stCheckbox label {{ color: var(--text2) !important; font-size: 11px !important; }}
.stCheckbox span[data-testid="stWidgetLabel"] {{ color: var(--text2) !important; }}

/* Primary button — Bloomberg orange */
.stButton > button[kind="primary"],
.stButton > button[data-testid="baseButton-primary"] {{
    background: var(--orange) !important;
    color: #000 !important;
    border: none !important;
    border-radius: 2px !important;
    font-weight: 700 !important;
    font-size: 11px !important;
    letter-spacing: 1.5px !important;
    text-transform: uppercase !important;
    font-family: 'IBM Plex Mono', monospace !important;
    transition: all 0.15s !important;
}}
.stButton > button[kind="primary"]:hover {{
    background: #cc5200 !important;
    transform: none !important;
}}
/* Secondary button */
.stButton > button:not([kind="primary"]) {{
    background: #1a1a1a !important;
    color: var(--orange) !important;
    border: 1px solid #2a2a2a !important;
    border-radius: 2px !important;
    font-size: 10px !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
    font-family: 'IBM Plex Mono', monospace !important;
}}
.stButton > button:not([kind="primary"]):hover {{
    border-color: var(--orange) !important;
}}

/* Tabs */
.stTabs [data-baseweb="tab-list"] {{
    background: var(--bg2) !important;
    border-bottom: 1px solid #222 !important;
    gap: 0 !important;
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent !important;
    color: var(--text3) !important;
    border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    padding: 8px 20px !important;
    font-size: 11px !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-weight: 500 !important;
    transition: all 0.15s !important;
}}
.stTabs [data-baseweb="tab"]:hover {{
    color: var(--text) !important;
    background: #161616 !important;
}}
.stTabs [aria-selected="true"] {{
    color: var(--orange) !important;
    border-bottom: 2px solid var(--orange) !important;
    background: #141414 !important;
}}
.stTabs [data-baseweb="tab-panel"] {{
    background: var(--bg) !important;
    padding-top: 16px !important;
}}

/* Divider */
hr {{ border-color: #1e1e1e !important; margin: 10px 0 !important; }}

/* Expander */
.stExpander {{
    background: var(--card) !important;
    border: 1px solid var(--card-bdr) !important;
    border-radius: 2px !important;
}}
.stExpander summary {{ color: var(--text2) !important; font-size: 11px !important; }}

/* Plotly hover / tooltip */
.stMarkdown p {{ color: var(--text2) !important; }}

/* Slider */
.stSlider [data-testid="stSlider"] {{ accent-color: var(--orange); }}
.stSlider label {{ color: var(--text3) !important; font-size: 10px !important; font-family: 'IBM Plex Mono', monospace !important; text-transform: uppercase !important; }}

/* Info / warning boxes */
.stInfo {{ background: #0d1a2a !important; border-color: var(--blue) !important; color: var(--blue) !important; }}
.stWarning {{ background: #1a1200 !important; border-color: var(--yellow) !important; }}
.stError {{ background: #1a0800 !important; border-color: var(--ce) !important; }}

/* Spacing tweaks */
.stSelectbox, .stTextInput, .stNumberInput {{ margin-bottom: -18px !important; }}
div[data-baseweb="popover"] {{ left: 0 !important; right: auto !important; }}
div[data-baseweb="calendar"] {{ left: 0 !important; right: auto !important; }}

/* Leg separator */
.leg-sep {{
    padding-top: 28px;
    font-size: 11px;
    color: #2a2a2a;
    text-align: center;
    font-family: 'IBM Plex Mono', monospace;
}}

/* Refresh token button — fixed top-right */
div[data-testid="stMainBlockContainer"] > div > div > div:nth-child(1) button {{
    position: fixed !important;
    top: 10px !important;
    right: 200px !important;
    z-index: 9999 !important;
    background: rgba(255,102,0,0.1) !important;
    border: 1px solid rgba(255,102,0,0.35) !important;
    color: var(--orange) !important;
    border-radius: 2px !important;
    padding: 3px 12px !important;
    font-size: 10px !important;
    font-weight: 600 !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
}}

/* Table/dataframe */
.stDataFrame {{ border-radius: 2px; border: 1px solid var(--card-bdr); }}
</style>
""", unsafe_allow_html=True)

load_css()

# ─────────────────────────────────────────────
# DATE LOGIC
# ─────────────────────────────────────────────

today = date.today()
if today.weekday() == 5:
    default_date = today - pd.Timedelta(days=1)
elif today.weekday() == 6:
    default_date = today - pd.Timedelta(days=2)
else:
    default_date = today

# ─────────────────────────────────────────────
# REFRESH TOKEN BUTTON + TOP BAR
# ─────────────────────────────────────────────

if st.button("↺ Refresh Token", key="refresh_token_nav", help="Clear cached token and re-authenticate"):
    get_shared_token.clear()
    st.rerun()

_now = _dt.datetime.now().strftime("%H:%M:%S")

st.markdown(f"""
<div class="bbg-topbar">
    <div class="bbg-logo">
        <div class="bbg-logo-box">BBG</div>
        <div>
            <div class="bbg-title">NFO / BFO Spread Terminal</div>
            <div class="bbg-subtitle">Options Spread &nbsp;·&nbsp; NFO / BFO &nbsp;·&nbsp; Fyers API v3</div>
        </div>
    </div>
    <div class="bbg-pills">
        <span class="bbg-pill pill-live">● LIVE</span>
        <span class="bbg-pill pill-time">{_now} IST</span>
    </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# FETCH LIVE DATA  (defined after symbol vars below)
# ─────────────────────────────────────────────

def fetch_live_data(sym_sx_ce, sym_sx_pe, sym_nf_ce, sym_nf_pe,
                    candle_interval, date_str, multiplier,
                    sensex_underlying, nifty_underlying):
    fyers = get_fyers_client()
    if fyers is None:
        return pd.DataFrame()
    with st.spinner("Fetching option & spot prices from Fyers..."):
        df_sx_ce  = fetch_candles(fyers, sym_sx_ce,  candle_interval, date_str)
        df_sx_pe  = fetch_candles(fyers, sym_sx_pe,  candle_interval, date_str)
        df_nf_ce  = fetch_candles(fyers, sym_nf_ce,  candle_interval, date_str)
        df_nf_pe  = fetch_candles(fyers, sym_nf_pe,  candle_interval, date_str)
        df_sx_spot = fetch_candles(fyers, "BSE:SENSEX-INDEX", candle_interval, date_str)
        if df_sx_spot.empty:
            df_sx_spot = fetch_candles(fyers, "BSE:SENSEX", candle_interval, date_str)
        df_nf_spot = fetch_candles(fyers, "NSE:NIFTY50-INDEX", candle_interval, date_str)
        if df_nf_spot.empty:
            df_nf_spot = fetch_candles(fyers, "NSE:NIFTY50", candle_interval, date_str)

    if any(df.empty for df in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe]):
        st.warning(f"⚠️ One or more symbols returned no data.\nBuilt: `{sym_sx_ce}` | `{sym_sx_pe}` | `{sym_nf_ce}` | `{sym_nf_pe}`")
        return pd.DataFrame()

    for dff in [df_sx_ce, df_sx_pe, df_nf_ce, df_nf_pe, df_sx_spot, df_nf_spot]:
        dff.drop(index=dff.index[dff.index.duplicated(keep="last")], inplace=True)

    common_idx = (df_sx_ce.index
                  .intersection(df_sx_pe.index)
                  .intersection(df_nf_ce.index)
                  .intersection(df_nf_pe.index))
    df = pd.DataFrame({
        "sensex_ce": df_sx_ce["close"].reindex(common_idx),
        "sensex_pe": df_sx_pe["close"].reindex(common_idx),
        "nifty_ce":  df_nf_ce["close"].reindex(common_idx),
        "nifty_pe":  df_nf_pe["close"].reindex(common_idx),
    }).dropna()

    if not df_sx_spot.empty and not df_nf_spot.empty:
        df["sensex_spot"] = df_sx_spot["close"].reindex(df.index, method="ffill")
        df["nifty_spot"]  = df_nf_spot["close"].reindex(df.index, method="ffill")
        df["synth_sensex"] = df["sensex_spot"] + df["sensex_ce"] - df["sensex_pe"]
        df["synth_nifty"]  = df["nifty_spot"]  + df["nifty_ce"]  - df["nifty_pe"]
        df["synth_ratio"]  = df["synth_sensex"] / df["synth_nifty"]

    df["ce_spread"] = df["sensex_ce"] - (df["nifty_ce"] * multiplier)
    df["pe_spread"] = df["sensex_pe"] - (df["nifty_pe"] * multiplier)
    df["diff"]      = df["ce_spread"] + df["pe_spread"]
    return df

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "df"        not in st.session_state: st.session_state.df        = pd.DataFrame()
if "df_custom" not in st.session_state: st.session_state.df_custom = pd.DataFrame()

# ─────────────────────────────────────────────
# PLOTLY THEME HELPERS
# ─────────────────────────────────────────────

def chart_layout(fig, title, height=380):
    fig.update_layout(
        title=dict(text=title,
                   font=dict(size=11, color=BBG["text3"],
                              family="IBM Plex Mono"),
                   x=0),
        height=height,
        plot_bgcolor=BBG["plot_bg"],
        paper_bgcolor=BBG["plot_bg"],
        font=dict(color=BBG["text2"], family="IBM Plex Mono"),
        hovermode="x unified",
        margin=dict(l=10, r=10, t=44, b=10),
        legend=dict(bgcolor="#111", bordercolor="#2a2a2a", borderwidth=1,
                    orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1,
                    font=dict(size=10, color=BBG["text2"])),
        xaxis=dict(gridcolor=BBG["grid"], tickfont=dict(size=10),
                   showspikes=True, spikemode="across",
                   spikecolor=BBG["text3"], spikethickness=1, spikedash="dot",
                   tickcolor=BBG["text3"], linecolor="#2a2a2a"),
        yaxis=dict(gridcolor=BBG["grid"], title="Value (₹)",
                   tickfont=dict(size=10), showspikes=True,
                   spikemode="across", spikecolor=BBG["text3"],
                   spikethickness=1, spikedash="dot",
                   tickcolor=BBG["text3"], linecolor="#2a2a2a"),
        hoverlabel=dict(bgcolor="#1c1c1c", bordercolor="#3a3a3a",
                        font=dict(color=BBG["text"], size=11,
                                  family="IBM Plex Mono")),
    )

def make_hlines(fig, series, hi_col, lo_col, row=None, col=None):
    h = series.max()
    l = series.min()
    kw = {"row": row, "col": col} if row else {}
    fig.add_hline(y=0,  line_dash="dash", line_color="#333", line_width=1, **kw)
    fig.add_hline(y=h,  line_dash="dot",  line_color=hi_col, line_width=1,
                  annotation_text=f"H: {h:.0f}",
                  annotation_position="right",
                  annotation_font=dict(color=hi_col, size=9,
                                       family="IBM Plex Mono"), **kw)
    fig.add_hline(y=l,  line_dash="dot",  line_color=lo_col, line_width=1,
                  annotation_text=f"L: {l:.0f}",
                  annotation_position="right",
                  annotation_font=dict(color=lo_col, size=9,
                                       family="IBM Plex Mono"), **kw)

def delta_html(v, pos_color=None, neg_color=None):
    pc = pos_color or BBG["ce"]
    nc = neg_color or BBG["pe"]
    arrow = "▲" if v >= 0 else "▼"
    color = pc if v >= 0 else nc
    return (f"<span style='color:{color};font-size:11px;"
            f"font-family:IBM Plex Mono'>{arrow} {abs(v):.2f}</span>")

# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs(["📊  Spread Dashboard", "🧮  Butterfly", "📐  IV Analysis"])

# ══════════════════════════════════════════════
# TAB 2 — 4-LEG BUTTERFLY BUILDER
# ══════════════════════════════════════════════

with tab2:
    st.markdown("<div class='section-label'>Configure 4 Legs</div>", unsafe_allow_html=True)

    UNDERLYINGS = ["SENSEX","BANKEX","NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"]
    leg_colors  = [BBG["ce"], BBG["pe"], BBG["blue"], BBG["diff"]]
    leg_labels  = ["Leg 1", "Leg 2", "Leg 3", "Leg 4"]

    def next_weekday_str(weekday):
        d = date.today()
        days_ahead = weekday - d.weekday()
        if days_ahead <= 0: days_ahead += 7
        return (d + pd.Timedelta(days=days_ahead)).strftime("%y%m%d")

    nse_exp = next_weekday_str(1)   # Tuesday
    bse_exp = next_weekday_str(3)   # Thursday

    LEG_DEFAULTS = [
        ("BSE", "SENSEX",  nse_exp, 80000, "CE", 1.0),
        ("NSE", "NIFTY",   bse_exp, 24200, "CE", 3.3),
        ("BSE", "SENSEX",  bse_exp, 80000, "CE", 1.0),
        ("NSE", "NIFTY",   nse_exp, 24200, "CE", 3.3),
    ]

    if "c_defaults_set" not in st.session_state:
        for i, (ex, un, ep, st_, ot, mu) in enumerate(LEG_DEFAULTS):
            st.session_state[f"c_exch_{i}"]  = ex
            st.session_state[f"c_under_{i}"] = un
            st.session_state[f"c_exp_{i}"]   = ep
            st.session_state[f"c_str_{i}"]   = float(st_)
            st.session_state[f"c_opt_{i}"]   = ot
            st.session_state[f"c_lots_{i}"]  = mu
        st.session_state["c_defaults_set"] = True

    c_row = st.columns([1.2, 1, 1, 1, 1.5])
    with c_row[0]: custom_date     = st.date_input("Date", value=default_date, key="c_date")
    with c_row[1]: custom_interval = st.selectbox("Interval", [1,3,5,10,15,30,60], index=2, key="c_interval")
    with c_row[2]: c_auto          = st.checkbox("Auto Refresh", value=False, key="c_auto_refresh")
    with c_row[3]: c_secs          = st.slider("Sec", 5, 60, 10, key="c_refresh_secs")
    with c_row[4]: custom_fetch    = st.button("⟳  FETCH 4-LEG DATA", type="primary",
                                               use_container_width=True, key="c_fetch")

    leg_configs = []

    for row_idx in range(2):
        legs_in_row = [row_idx * 2, row_idx * 2 + 1]
        cols = st.columns([0.25, 0.5, 0.8, 0.8, 0.65, 0.65, 0.6, 0.15,
                           0.25, 0.5, 0.8, 0.8, 0.65, 0.65, 0.6])
        for j, i in enumerate(legs_in_row):
            d_exch, d_under, d_exp, d_str, d_opt, d_mult = LEG_DEFAULTS[i]
            exch_opts = ["NSE","BSE"] if d_exch == "NSE" else ["BSE","NSE"]
            und_opts  = [d_under] + [u for u in UNDERLYINGS if u != d_under]
            off = j * 8

            cols[off].markdown(
                f"<div style='padding-top:28px;font-size:10px;font-weight:700;"
                f"letter-spacing:1.5px;text-transform:uppercase;"
                f"color:{leg_colors[i]};font-family:IBM Plex Mono'>{leg_labels[i]}</div>",
                unsafe_allow_html=True)

            with cols[off+1]: exch  = st.selectbox("Exchange",   exch_opts, key=f"c_exch_{i}")
            with cols[off+2]: under = st.selectbox("Underlying",  und_opts,  key=f"c_under_{i}")
            _exp_opts = get_expiries_for(exch, under)
            with cols[off+3]: expiry   = expiry_selectbox("Expiry", _exp_opts,
                                                          f"c_exp_man_{i}", f"c_exp_sel_{i}", d_exp)
            with cols[off+4]: strike   = st.number_input("Strike", step=100, key=f"c_str_{i}")
            with cols[off+5]: opt_type = st.selectbox("CE/PE", ["CE","PE"], key=f"c_opt_{i}")
            with cols[off+6]: mult     = st.number_input("Mult", min_value=0.1, step=0.1,
                                                          key=f"c_lots_{i}")
            if j == 0:
                cols[7].markdown("<div class='leg-sep' style='padding-top:28px'>│</div>",
                                 unsafe_allow_html=True)
            leg_configs.append({"exchange": exch, "underlying": under, "expiry": expiry,
                                 "strike": int(strike), "opt_type": opt_type, "lots": mult})

    custom_date_str = custom_date.strftime("%Y-%m-%d")
    L = leg_configs

    def leg_name(i):
        return f"{L[i]['lots']}×{L[i]['underlying']} {L[i]['opt_type']}"

    st.markdown(f"""
    <div style='font-size:10px;color:{BBG["text3"]};margin:4px 0 8px 0;
                font-family:IBM Plex Mono;letter-spacing:0.5px;'>
        CHART 1: &nbsp;
        <span style='color:{BBG["ce"]}'>{leg_name(0)}</span>
        &nbsp;−&nbsp;
        <span style='color:{BBG["pe"]}'>{leg_name(1)}</span>
        &nbsp;&nbsp;|&nbsp;&nbsp;
        <span style='color:{BBG["blue"]}'>{leg_name(2)}</span>
        &nbsp;−&nbsp;
        <span style='color:{BBG["diff"]}'>{leg_name(3)}</span>
        &nbsp;&nbsp;&nbsp;&nbsp;
        CHART 2: (Leg1−Leg2) + (Leg3−Leg4)
    </div>
    """, unsafe_allow_html=True)

    if custom_fetch:
        fyers = get_fyers_client()
        if fyers is None:
            st.error("Not connected to Fyers.")
        else:
            raw_series = []
            ok = True
            with st.spinner("Fetching 4-leg data..."):
                for i, leg in enumerate(leg_configs):
                    sym    = build_symbol(leg["exchange"], leg["underlying"],
                                          leg["expiry"], leg["opt_type"][0], leg["strike"])
                    df_leg = fetch_candles(fyers, sym, custom_interval, custom_date_str)
                    if df_leg.empty:
                        st.warning(f"⚠️ {leg_labels[i]}: No data for `{sym}`")
                        ok = False; break
                    df_leg = df_leg[~df_leg.index.duplicated(keep="last")]
                    raw_series.append(df_leg["close"] * leg["lots"])

            if ok and len(raw_series) == 4:
                base_idx  = raw_series[0].index
                s         = [sr.reindex(base_idx, method="ffill").fillna(0) for sr in raw_series]
                spread12  = s[0] - s[1]
                spread34  = s[2] - s[3]
                combined  = spread12 + spread34
                st.session_state.df_custom = pd.DataFrame({
                    "spread12": spread12,
                    "spread34": spread34,
                    "combined": combined,
                })

    df_custom = st.session_state.df_custom

    if not df_custom.empty:
        _s12  = df_custom["spread12"].iloc[-1]
        _s34  = df_custom["spread34"].iloc[-1]
        _comb = df_custom["combined"].iloc[-1]
        _s12_d  = _s12  - df_custom["spread12"].iloc[-2] if len(df_custom) > 1 else 0
        _s34_d  = _s34  - df_custom["spread34"].iloc[-2] if len(df_custom) > 1 else 0
        _comb_d = _comb - df_custom["combined"].iloc[-2] if len(df_custom) > 1 else 0
        _updated = df_custom.index[-1].strftime("%H:%M:%S")

        st.markdown(f"""
        <div class="metrics-grid">
            <div class="metric-card card-ce">
                <div class="metric-badge">📊</div>
                <div class="metric-label">LEG 1 − LEG 2</div>
                <div class="metric-value val-ce">{_s12:+.1f}</div>
                <div class="metric-sub">Spread &nbsp; {delta_html(_s12_d)}</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-badge">📊</div>
                <div class="metric-label">LEG 3 − LEG 4</div>
                <div class="metric-value val-pe">{_s34:+.1f}</div>
                <div class="metric-sub">Spread &nbsp; {delta_html(_s34_d)}</div>
            </div>
            <div class="metric-card card-diff">
                <div class="metric-badge">⚖️</div>
                <div class="metric-label">4 LEG TOTAL</div>
                <div class="metric-value val-diff">{_comb:+.1f}</div>
                <div class="metric-sub">(Leg1−Leg2) + (Leg3−Leg4) &nbsp; {delta_html(_comb_d)}</div>
            </div>
            <div class="metric-card card-time">
                <div class="metric-badge">🕐</div>
                <div class="metric-label">LAST UPDATE</div>
                <div class="metric-value val-time">{_updated}</div>
                <div class="metric-sub">{len(df_custom)} candles</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Chart 1 — spread12 & spread34
        fig1 = go.Figure()
        fig1.add_trace(go.Scatter(
            x=df_custom.index, y=df_custom["spread12"],
            name="Leg1 − Leg2",
            line=dict(color=BBG["ce"], width=1.5),
            hovertemplate="%{x|%H:%M}<br>Leg1−Leg2: %{y:.2f}<extra></extra>"))
        fig1.add_trace(go.Scatter(
            x=df_custom.index, y=df_custom["spread34"],
            name="Leg3 − Leg4",
            line=dict(color=BBG["blue"], width=1.5),
            hovertemplate="%{x|%H:%M}<br>Leg3−Leg4: %{y:.2f}<extra></extra>"))
        make_hlines(fig1, df_custom["spread12"], BBG["ce"], BBG["pe"])
        chart_layout(fig1, "SPREAD CHART — LEG1−LEG2 & LEG3−LEG4")
        st.plotly_chart(fig1, use_container_width=True)

        # Chart 2 — combined
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=df_custom.index, y=df_custom["combined"],
            name="Combined (Leg1−Leg2)+(Leg3−Leg4)",
            line=dict(color=BBG["purple"], width=2),
            fill="tozeroy", fillcolor=f"rgba(153,102,255,0.07)",
            hovertemplate="%{x|%H:%M}<br>Combined: %{y:.2f}<extra></extra>"))
        make_hlines(fig2, df_custom["combined"], BBG["pe"], BBG["ce"])
        chart_layout(fig2, "COMBINED CHART — (LEG1−LEG2) + (LEG3−LEG4)")
        st.plotly_chart(fig2, use_container_width=True)

    else:
        st.info("👆 Configure your 4 legs above and click **FETCH 4-LEG DATA**.")

    # Auto-refresh
    if c_auto:
        time.sleep(c_secs)
        st.rerun()

# ══════════════════════════════════════════════
# TAB 1 — SPREAD DASHBOARD
# ══════════════════════════════════════════════

with tab1:
    st.markdown("<div class='section-label'>Settings</div>", unsafe_allow_html=True)

    r0 = st.columns([1.2, 1, 1, 1, 1, 1, 1, 1.5])
    with r0[0]: selected_date   = st.date_input("Date", value=default_date, key="date_inp")
    with r0[1]: multiplier      = st.number_input("Ratio", value=3.3, step=0.1, min_value=0.1, key="mult")
    with r0[2]: candle_interval = st.selectbox("Interval (min)", [1,3,5,10,15,30,60], index=2, key="interval")
    with r0[3]: show_diff       = st.checkbox("4-Leg Chart",  value=True,  key="show_diff")
    with r0[4]: auto_refresh    = st.checkbox("Auto Refresh", value=True,  key="auto_ref")
    with r0[5]: refresh_secs    = st.slider("Refresh (sec)", 5, 60, REFRESH_SECONDS, key="ref_sec")
    with r0[6]: st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    with r0[7]: fetch_btn       = st.button("⟳  FETCH DATA", use_container_width=True,
                                            type="primary", key="fetch_btn")

    date_str = selected_date.strftime("%Y-%m-%d")

    # ── Both legs on one row ──
    legs_row = st.columns([0.25, 0.5, 0.8, 0.8, 0.8, 0.65, 0.65, 0.15,
                            0.25, 0.5, 0.8, 0.8, 0.8, 0.65, 0.65])

    legs_row[0].markdown(
        f"<div style='padding-top:28px;font-size:10px;font-weight:700;"
        f"letter-spacing:1.5px;text-transform:uppercase;color:{BBG['ce']};"
        f"font-family:IBM Plex Mono'>LEG 1</div>",
        unsafe_allow_html=True)

    with legs_row[1]:  sensex_exchange    = st.selectbox("Exchange",   ["BSE","NSE"], index=0, key="sx_exch")
    with legs_row[2]:  sensex_underlying  = st.selectbox("Underlying",
                            ["SENSEX","BANKEX","NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"],
                            index=0, key="sx_under")
    _sx_opts = get_expiries_for(sensex_exchange, sensex_underlying)
    with legs_row[3]:  sensex_ce_expiry   = expiry_selectbox("CE Expiry", _sx_opts,
                                                              "sx_ce_man", "sx_ce_sel", "260312")
    with legs_row[4]:  sensex_pe_expiry   = expiry_selectbox("PE Expiry", _sx_opts,
                                                              "sx_pe_man", "sx_pe_sel", "260312")
    with legs_row[5]:  sensex_ce_strike   = st.number_input("CE Strike", value=80000, step=100, key="sx_ce_str")
    with legs_row[6]:  sensex_pe_strike   = st.number_input("PE Strike", value=80000, step=100, key="sx_pe_str")

    legs_row[7].markdown(
        "<div class='leg-sep' style='padding-top:28px'>│</div>", unsafe_allow_html=True)

    legs_row[8].markdown(
        f"<div style='padding-top:28px;font-size:10px;font-weight:700;"
        f"letter-spacing:1.5px;text-transform:uppercase;color:{BBG['blue']};"
        f"font-family:IBM Plex Mono'>LEG 2</div>",
        unsafe_allow_html=True)

    with legs_row[9]:  nifty_exchange     = st.selectbox("Exchange",   ["NSE","BSE"], index=0, key="nf_exch")
    with legs_row[10]: nifty_underlying   = st.selectbox("Underlying",
                            ["NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","SENSEX","BANKEX"],
                            index=0, key="nf_under")
    _nf_opts = get_expiries_for(nifty_exchange, nifty_underlying)
    with legs_row[11]: nifty_ce_expiry    = expiry_selectbox("CE Expiry", _nf_opts,
                                                              "nf_ce_man", "nf_ce_sel", "260310")
    with legs_row[12]: nifty_pe_expiry    = expiry_selectbox("PE Expiry", _nf_opts,
                                                              "nf_pe_man", "nf_pe_sel", "260310")
    with legs_row[13]: nifty_ce_strike    = st.number_input("CE Strike", value=24800, step=50, key="nf_ce_str")
    with legs_row[14]: nifty_pe_strike    = st.number_input("PE Strike", value=24800, step=50, key="nf_pe_str")

    st.divider()

    # ── Build symbols ──
    sym_sx_ce = build_symbol(sensex_exchange, sensex_underlying, sensex_ce_expiry, "C", int(sensex_ce_strike))
    sym_sx_pe = build_symbol(sensex_exchange, sensex_underlying, sensex_pe_expiry, "P", int(sensex_pe_strike))
    sym_nf_ce = build_symbol(nifty_exchange,  nifty_underlying,  nifty_ce_expiry,  "C", int(nifty_ce_strike))
    sym_nf_pe = build_symbol(nifty_exchange,  nifty_underlying,  nifty_pe_expiry,  "P", int(nifty_pe_strike))

    if fetch_btn or st.session_state.df.empty:
        st.session_state.df = fetch_live_data(
            sym_sx_ce, sym_sx_pe, sym_nf_ce, sym_nf_pe,
            candle_interval, date_str, multiplier,
            sensex_underlying, nifty_underlying)

    df = st.session_state.df

    if df.empty:
        st.info("👆 Set your options above and click **FETCH DATA**.")
    else:
        latest    = df.iloc[-1]
        ce_val    = latest["ce_spread"]
        pe_val    = latest["pe_spread"]
        diff_val  = latest["diff"]
        updated   = df.index[-1].strftime("%H:%M:%S")
        is_today  = date_str == date.today().strftime("%Y-%m-%d")

        ce_delta   = ce_val   - df["ce_spread"].iloc[-2] if len(df) > 1 else 0
        pe_delta   = pe_val   - df["pe_spread"].iloc[-2] if len(df) > 1 else 0
        diff_delta = diff_val - df["diff"].iloc[-2]       if len(df) > 1 else 0

        synth_val = (f"{latest['synth_ratio']:.4f}"
                     if "synth_ratio" in df.columns and pd.notna(latest.get("synth_ratio"))
                     else "N/A")

        st.markdown(f"""
        <div class="metrics-grid">
            <div class="metric-card card-ce">
                <div class="metric-badge">📈</div>
                <div class="metric-label">CE SPREAD</div>
                <div class="metric-value val-ce">{ce_val:+.1f}</div>
                <div class="metric-sub">{sensex_underlying} CE − {nifty_underlying} CE ×{multiplier}
                    &nbsp; {delta_html(ce_delta)}</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-badge">📉</div>
                <div class="metric-label">PE SPREAD</div>
                <div class="metric-value val-pe">{pe_val:+.1f}</div>
                <div class="metric-sub">{sensex_underlying} PE − {nifty_underlying} PE ×{multiplier}
                    &nbsp; {delta_html(pe_delta)}</div>
            </div>
            <div class="metric-card card-diff">
                <div class="metric-badge">⚖️</div>
                <div class="metric-label">4 LEG TOTAL</div>
                <div class="metric-value val-diff">{diff_val:+.1f}</div>
                <div class="metric-sub">CE + PE combined &nbsp; {delta_html(diff_delta)}</div>
            </div>
            <div class="metric-card card-time">
                <div class="metric-badge">🔢</div>
                <div class="metric-label">SYNTHETIC FUT · MULT</div>
                <div class="metric-value val-time">{synth_val}</div>
                <div class="metric-sub">{'LIVE' if is_today else 'HISTORICAL'} · {updated}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # ── Subplot layout ──
        has_synth = "synth_ratio" in df.columns and df["synth_ratio"].notna().any()
        n_rows    = 1 + int(show_diff) + int(has_synth)

        if n_rows == 3:
            row_heights = [0.55, 0.25, 0.20]
            subplot_titles = (
                f"CE SPREAD — {sensex_underlying} CE − {nifty_underlying} CE ×{multiplier}  |  "
                f"PE SPREAD — {sensex_underlying} PE − {nifty_underlying} PE ×{multiplier}",
                "4-LEG TOTAL (CE Spread + PE Spread)",
                "SYNTHETIC FUTURES RATIO"
            )
        elif n_rows == 2:
            row_heights = [0.70, 0.30]
            subplot_titles = (
                f"CE SPREAD — {sensex_underlying} CE − {nifty_underlying} CE ×{multiplier}  |  "
                f"PE SPREAD — {sensex_underlying} PE − {nifty_underlying} PE ×{multiplier}",
                "4-LEG TOTAL (CE Spread + PE Spread)"
            )
        else:
            row_heights = [1.0]
            subplot_titles = (
                f"CE SPREAD — {sensex_underlying} CE − {nifty_underlying} CE ×{multiplier}  |  "
                f"PE SPREAD — {sensex_underlying} PE − {nifty_underlying} PE ×{multiplier}",
            )

        fig = make_subplots(
            rows=n_rows, cols=1,
            shared_xaxes=True,
            row_heights=row_heights,
            subplot_titles=subplot_titles,
            vertical_spacing=0.04,
        )

        # Row 1 — CE spread & PE spread
        fig.add_trace(go.Scatter(
            x=df.index, y=df["ce_spread"],
            name="CE Spread",
            line=dict(color=BBG["ce"], width=1.5),
            hovertemplate="%{x|%H:%M}<br>CE Spread: %{y:.2f}<extra></extra>"),
            row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=df["pe_spread"],
            name="PE Spread",
            line=dict(color=BBG["pe"], width=1.5),
            hovertemplate="%{x|%H:%M}<br>PE Spread: %{y:.2f}<extra></extra>"),
            row=1, col=1)
        make_hlines(fig, df["ce_spread"], BBG["ce"], BBG["pe"], row=1, col=1)

        if show_diff:
            fig.add_trace(go.Scatter(
                x=df.index, y=df["diff"],
                name="4-Leg Total",
                line=dict(color=BBG["diff"], width=1.5),
                fill="tozeroy", fillcolor=f"rgba(255,170,0,0.07)",
                hovertemplate="%{x|%H:%M}<br>4-Leg Total: %{y:.2f}<extra></extra>"),
                row=2, col=1)
            make_hlines(fig, df["diff"], BBG["diff"], BBG["diff"], row=2, col=1)

        if has_synth:
            synth_row = n_rows
            fig.add_trace(go.Scatter(
                x=df.index, y=df["synth_ratio"],
                name="Synth Ratio",
                line=dict(color=BBG["purple"], width=1.2),
                hovertemplate="%{x|%H:%M}<br>Ratio: %{y:.4f}<extra></extra>"),
                row=synth_row, col=1)

        # Global chart styling
        total_h = 420 + (120 if show_diff else 0) + (100 if has_synth else 0)
        fig.update_layout(
            height=total_h,
            plot_bgcolor=BBG["plot_bg"],
            paper_bgcolor=BBG["plot_bg"],
            font=dict(color=BBG["text2"], family="IBM Plex Mono"),
            hovermode="x unified",
            margin=dict(l=10, r=10, t=40, b=10),
            legend=dict(bgcolor="#111", bordercolor="#2a2a2a", borderwidth=1,
                        orientation="h", yanchor="bottom", y=1.01,
                        xanchor="right", x=1,
                        font=dict(size=10, color=BBG["text2"])),
            hoverlabel=dict(bgcolor="#1c1c1c", bordercolor="#3a3a3a",
                            font=dict(color=BBG["text"], size=11, family="IBM Plex Mono")),
        )
        for i in range(1, n_rows + 1):
            fig.update_xaxes(gridcolor=BBG["grid"], tickfont=dict(size=10),
                             showspikes=True, spikemode="across",
                             spikecolor=BBG["text3"], spikethickness=1, spikedash="dot",
                             tickcolor=BBG["text3"], linecolor="#2a2a2a",
                             row=i, col=1)
            fig.update_yaxes(gridcolor=BBG["grid"], tickfont=dict(size=10),
                             showspikes=True, spikemode="across",
                             spikecolor=BBG["text3"], spikethickness=1, spikedash="dot",
                             tickcolor=BBG["text3"], linecolor="#2a2a2a",
                             row=i, col=1)
        for ann in fig.layout.annotations:
            ann.font.color  = BBG["text3"]
            ann.font.size   = 10
            ann.font.family = "IBM Plex Mono"

        st.plotly_chart(fig, use_container_width=True)

        # ── Raw data expander ──
        with st.expander("RAW DATA TABLE", expanded=False):
            disp = df.copy()
            disp.index = disp.index.strftime("%H:%M:%S")
            st.dataframe(disp.style.format("{:.2f}"), use_container_width=True)

    # Auto-refresh
    if auto_refresh:
        time.sleep(refresh_secs)
        st.session_state.df = fetch_live_data(
            sym_sx_ce, sym_sx_pe, sym_nf_ce, sym_nf_pe,
            candle_interval, date_str, multiplier,
            sensex_underlying, nifty_underlying)
        st.rerun()

# ══════════════════════════════════════════════
# TAB 3 — IV ANALYSIS
# ══════════════════════════════════════════════

with tab3:
    st.markdown("<div class='section-label'>IV Analysis — Black-Scholes Calculator</div>",
                unsafe_allow_html=True)

    # ── Black-Scholes helpers ──
    def bs_price(S, K, T, r, sigma, opt_type="CE"):
        if T <= 0 or sigma <= 0:
            intrinsic = max(S - K, 0) if opt_type == "CE" else max(K - S, 0)
            return intrinsic
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if opt_type == "CE":
            return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:
            return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def implied_vol(market_price, S, K, T, r, opt_type="CE"):
        if T <= 0 or market_price <= 0:
            return np.nan
        intrinsic = max(S - K, 0) if opt_type == "CE" else max(K - S, 0)
        if market_price < intrinsic:
            return np.nan
        try:
            iv = brentq(lambda s: bs_price(S, K, T, r, s, opt_type) - market_price,
                        1e-6, 10.0, xtol=1e-6)
            return iv
        except Exception:
            return np.nan

    def bs_greeks(S, K, T, r, sigma, opt_type="CE"):
        if T <= 0 or sigma <= 0:
            return {"delta": np.nan, "gamma": np.nan, "theta": np.nan,
                    "vega": np.nan, "rho": np.nan}
        d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        gamma = norm.pdf(d1) / (S * sigma * np.sqrt(T))
        vega  = S * norm.pdf(d1) * np.sqrt(T) / 100
        if opt_type == "CE":
            delta = norm.cdf(d1)
            theta = (-(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
                     - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
            rho   = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
        else:
            delta = norm.cdf(d1) - 1
            theta = (-(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
                     + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365
            rho   = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100
        return {"delta": delta, "gamma": gamma, "theta": theta,
                "vega": vega, "rho": rho}

    # ── IV Calculator inputs ──
    iv_cols = st.columns([1, 1, 1, 1, 1, 1, 1])
    with iv_cols[0]: iv_spot    = st.number_input("Spot Price",    value=80000.0,  step=100.0, key="iv_spot")
    with iv_cols[1]: iv_strike  = st.number_input("Strike",        value=80000.0,  step=100.0, key="iv_strike")
    with iv_cols[2]: iv_price   = st.number_input("Option Price",  value=200.0,    step=1.0,   key="iv_price")
    with iv_cols[3]: iv_days    = st.number_input("Days to Expiry",value=7,        step=1,     min_value=0, key="iv_days")
    with iv_cols[4]: iv_rate    = st.number_input("Risk-Free Rate %", value=7.0,   step=0.1,   key="iv_rate")
    with iv_cols[5]: iv_type    = st.selectbox("Option Type",      ["CE","PE"],    key="iv_type")
    with iv_cols[6]: iv_calc    = st.button("▶  CALCULATE IV", type="primary",     key="iv_calc_btn")

    if iv_calc or True:   # always show
        T  = iv_days / 365.0
        r  = iv_rate / 100.0
        iv = implied_vol(iv_price, iv_spot, iv_strike, T, r, iv_type)
        iv_pct = iv * 100 if not np.isnan(iv) else 0.0
        bs_val = bs_price(iv_spot, iv_strike, T, r, iv if not np.isnan(iv) else 0.2, iv_type)
        greeks = bs_greeks(iv_spot, iv_strike, T, r, iv if not np.isnan(iv) else 0.2, iv_type)

        iv_display = f"{iv_pct:.2f}%" if not np.isnan(iv) else "N/A"

        st.markdown(f"""
        <div class="metrics-grid" style="grid-template-columns: repeat(6,1fr);">
            <div class="metric-card card-ce">
                <div class="metric-label">IMPLIED VOL</div>
                <div class="metric-value val-ce">{iv_display}</div>
                <div class="metric-sub">Annualised IV</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-label">BS PRICE</div>
                <div class="metric-value val-pe">{bs_val:.2f}</div>
                <div class="metric-sub">Theoretical Price</div>
            </div>
            <div class="metric-card card-diff">
                <div class="metric-label">DELTA</div>
                <div class="metric-value val-diff">{greeks['delta']:.4f}</div>
                <div class="metric-sub">Δ</div>
            </div>
            <div class="metric-card card-time">
                <div class="metric-label">GAMMA</div>
                <div class="metric-value val-time">{greeks['gamma']:.6f}</div>
                <div class="metric-sub">Γ</div>
            </div>
            <div class="metric-card card-purple">
                <div class="metric-label">THETA / DAY</div>
                <div class="metric-value val-purple">{greeks['theta']:.4f}</div>
                <div class="metric-sub">Θ</div>
            </div>
            <div class="metric-card card-pe">
                <div class="metric-label">VEGA / 1%</div>
                <div class="metric-value val-pe">{greeks['vega']:.4f}</div>
                <div class="metric-sub">𝓥</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.divider()

    # ── IV Smile / Skew chart ──
    st.markdown("<div class='section-label'>IV Skew Builder</div>", unsafe_allow_html=True)

    skew_cols = st.columns([1, 1, 1, 1, 1, 1.5])
    with skew_cols[0]: skew_spot    = st.number_input("Spot",        value=80000.0, step=100.0, key="skew_spot")
    with skew_cols[1]: skew_days    = st.number_input("DTE",         value=7,       step=1, min_value=1, key="skew_dte")
    with skew_cols[2]: skew_rate    = st.number_input("Rate %",      value=7.0,     step=0.1, key="skew_rate")
    with skew_cols[3]: skew_range   = st.number_input("Strike Range %", value=5.0,  step=0.5, key="skew_range")
    with skew_cols[4]: skew_steps   = st.number_input("Steps",       value=20,      step=1, min_value=5, max_value=50, key="skew_steps")
    with skew_cols[5]: skew_btn     = st.button("▶  GENERATE SKEW", type="primary", key="skew_btn")

    if skew_btn:
        lo   = skew_spot * (1 - skew_range / 100)
        hi   = skew_spot * (1 + skew_range / 100)
        strikes = np.linspace(lo, hi, int(skew_steps))
        T_s  = skew_days / 365.0
        r_s  = skew_rate / 100.0

        # Simulate a simple skew: ATM IV ≈ 0.15, with put skew
        atm_iv   = 0.15
        skew_data = []
        for K in strikes:
            moneyness = np.log(K / skew_spot)
            # Heuristic smile with put skew
            iv_ce = atm_iv + 0.3 * moneyness**2 - 0.05 * moneyness
            iv_pe = atm_iv + 0.3 * moneyness**2 + 0.08 * moneyness
            iv_ce = max(iv_ce, 0.01)
            iv_pe = max(iv_pe, 0.01)
            p_ce  = bs_price(skew_spot, K, T_s, r_s, iv_ce, "CE")
            p_pe  = bs_price(skew_spot, K, T_s, r_s, iv_pe, "PE")
            skew_data.append({"strike": K, "iv_ce": iv_ce*100, "iv_pe": iv_pe*100,
                               "price_ce": p_ce, "price_pe": p_pe})

        df_skew = pd.DataFrame(skew_data)

        fig_skew = go.Figure()
        fig_skew.add_trace(go.Scatter(
            x=df_skew["strike"], y=df_skew["iv_ce"],
            name="CE IV",
            line=dict(color=BBG["ce"], width=2),
            mode="lines+markers",
            marker=dict(size=5, color=BBG["ce"]),
            hovertemplate="Strike: %{x:.0f}<br>CE IV: %{y:.2f}%<extra></extra>"))
        fig_skew.add_trace(go.Scatter(
            x=df_skew["strike"], y=df_skew["iv_pe"],
            name="PE IV",
            line=dict(color=BBG["pe"], width=2),
            mode="lines+markers",
            marker=dict(size=5, color=BBG["pe"]),
            hovertemplate="Strike: %{x:.0f}<br>PE IV: %{y:.2f}%<extra></extra>"))
        fig_skew.add_vline(x=skew_spot, line_dash="dash", line_color=BBG["yellow"],
                            annotation_text="ATM", annotation_font_color=BBG["yellow"],
                            annotation_font_size=10)
        chart_layout(fig_skew, "IV SKEW — CE vs PE (SIMULATED SMILE)", height=360)
        fig_skew.update_yaxes(title="Implied Volatility (%)")
        fig_skew.update_xaxes(title="Strike Price")
        st.plotly_chart(fig_skew, use_container_width=True)

        # Price chart
        fig_price = make_subplots(rows=1, cols=2,
                                  subplot_titles=("CE PRICES BY STRIKE", "PE PRICES BY STRIKE"))
        fig_price.add_trace(go.Bar(
            x=df_skew["strike"], y=df_skew["price_ce"],
            name="CE Price",
            marker_color=BBG["ce"], opacity=0.8,
            hovertemplate="Strike: %{x:.0f}<br>CE: ₹%{y:.2f}<extra></extra>"),
            row=1, col=1)
        fig_price.add_trace(go.Bar(
            x=df_skew["strike"], y=df_skew["price_pe"],
            name="PE Price",
            marker_color=BBG["pe"], opacity=0.8,
            hovertemplate="Strike: %{x:.0f}<br>PE: ₹%{y:.2f}<extra></extra>"),
            row=1, col=2)
        fig_price.update_layout(
            height=280, plot_bgcolor=BBG["plot_bg"], paper_bgcolor=BBG["plot_bg"],
            font=dict(color=BBG["text2"], family="IBM Plex Mono"),
            showlegend=False, margin=dict(l=10, r=10, t=36, b=10),
            hoverlabel=dict(bgcolor="#1c1c1c", bordercolor="#3a3a3a",
                            font=dict(color=BBG["text"], size=11, family="IBM Plex Mono")),
        )
        for i in [1, 2]:
            fig_price.update_xaxes(gridcolor=BBG["grid"], tickfont=dict(size=9),
                                   tickcolor=BBG["text3"], linecolor="#2a2a2a", row=1, col=i)
            fig_price.update_yaxes(gridcolor=BBG["grid"], tickfont=dict(size=9),
                                   tickcolor=BBG["text3"], linecolor="#2a2a2a", row=1, col=i)
        for ann in fig_price.layout.annotations:
            ann.font.color  = BBG["text3"]
            ann.font.size   = 10
            ann.font.family = "IBM Plex Mono"
        st.plotly_chart(fig_price, use_container_width=True)

    else:
        st.info("👆 Configure the skew parameters above and click **GENERATE SKEW**.")

    # ── P&L Payoff Diagram ──
    st.divider()
    st.markdown("<div class='section-label'>Strategy Payoff at Expiry</div>", unsafe_allow_html=True)

    pay_cols = st.columns([1, 1, 1, 1, 1, 1, 1.5])
    with pay_cols[0]: pay_spot    = st.number_input("Current Spot", value=80000.0, step=100.0, key="pay_spot")
    with pay_cols[1]: pay_strike  = st.number_input("Strike",       value=80000.0, step=100.0, key="pay_strike")
    with pay_cols[2]: pay_prem    = st.number_input("Premium Paid", value=200.0,   step=1.0,   key="pay_prem")
    with pay_cols[3]: pay_lots    = st.number_input("Lots",         value=1,       step=1,     min_value=1, key="pay_lots")
    with pay_cols[4]: pay_type    = st.selectbox("Buy/Sell",        ["Buy","Sell"], key="pay_bs")
    with pay_cols[5]: pay_opt     = st.selectbox("CE/PE",           ["CE","PE"],   key="pay_opt")
    with pay_cols[6]: pay_btn     = st.button("▶  DRAW PAYOFF", type="primary", key="pay_btn")

    if pay_btn:
        lo_p  = pay_spot * 0.90
        hi_p  = pay_spot * 1.10
        S_arr = np.linspace(lo_p, hi_p, 300)

        if pay_opt == "CE":
            intrinsic = np.maximum(S_arr - pay_strike, 0)
        else:
            intrinsic = np.maximum(pay_strike - S_arr, 0)

        if pay_type == "Buy":
            pnl = (intrinsic - pay_prem) * pay_lots
        else:
            pnl = (pay_prem - intrinsic) * pay_lots

        fig_pay = go.Figure()
        pos_mask = pnl >= 0
        neg_mask = pnl <  0

        fig_pay.add_trace(go.Scatter(
            x=S_arr[pos_mask], y=pnl[pos_mask],
            fill="tozeroy", fillcolor=f"rgba(0,204,102,0.15)",
            line=dict(color=BBG["pe"], width=0), name="Profit",
            hoverinfo="skip"))
        fig_pay.add_trace(go.Scatter(
            x=S_arr[neg_mask], y=pnl[neg_mask],
            fill="tozeroy", fillcolor=f"rgba(255,102,51,0.15)",
            line=dict(color=BBG["ce"], width=0), name="Loss",
            hoverinfo="skip"))
        fig_pay.add_trace(go.Scatter(
            x=S_arr, y=pnl,
            name=f"{pay_type} {pay_opt}",
            line=dict(color=BBG["yellow"], width=2),
            hovertemplate="Spot: %{x:.0f}<br>P&L: ₹%{y:.2f}<extra></extra>"))
        fig_pay.add_vline(x=pay_spot,   line_dash="dot",  line_color=BBG["blue"],
                           annotation_text="Current Spot",
                           annotation_font_color=BBG["blue"],
                           annotation_font_size=9)
        fig_pay.add_vline(x=pay_strike, line_dash="dash", line_color=BBG["text3"],
                           annotation_text="Strike",
                           annotation_font_color=BBG["text3"],
                           annotation_font_size=9)
        fig_pay.add_hline(y=0, line_dash="solid", line_color="#333", line_width=1)

        chart_layout(fig_pay, f"PAYOFF AT EXPIRY — {pay_type.upper()} {pay_opt} | "
                               f"Strike {pay_strike:.0f} | Premium {pay_prem:.2f} | Lots {pay_lots}",
                     height=360)
        fig_pay.update_yaxes(title="P&L (₹)")
        fig_pay.update_xaxes(title="Spot at Expiry")
        st.plotly_chart(fig_pay, use_container_width=True)
    else:
        st.info("👆 Enter your trade parameters and click **DRAW PAYOFF**.")
