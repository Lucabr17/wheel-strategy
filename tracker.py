import streamlit as st
import pandas as pd
from datetime import date
import numpy as np
import yfinance as yf
import gspread
import json
import altair as alt

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Wheel Strategy Log", layout="wide")

# --- BRAND STYLING (dark + #A9CDE7) ---
st.markdown("""
<style>
/* ---------- layout ---------- */
.block-container {padding-top: 1.6rem; padding-bottom: 5rem; max-width: 1100px;}
@media (max-width: 640px){
  .block-container {padding-left: 0.9rem; padding-right: 0.9rem;}
}
hr {border-color: rgba(169,205,231,.10) !important;}

/* ---------- KPI cards ---------- */
.kpi-grid{display:grid; grid-template-columns:repeat(auto-fit, minmax(155px, 1fr)); gap:12px; margin:4px 0 10px 0;}
.kpi{background:linear-gradient(180deg,#151C25 0%,#10161E 100%); border:1px solid rgba(169,205,231,.15);
     border-radius:14px; padding:14px 16px;}
.kpi-label{font-size:.70rem; letter-spacing:.08em; text-transform:uppercase; color:#8CA3B8;
           margin-bottom:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.kpi-value{font-size:1.55rem; font-weight:700; line-height:1.1;}
.pos{color:#4ADE80;} .neg{color:#F87171;} .accent{color:#A9CDE7;} .neutral{color:#E8EDF2;}

/* ---------- holdings cards ---------- */
.hold-grid{display:grid; grid-template-columns:repeat(auto-fit, minmax(250px, 1fr)); gap:12px; margin-bottom:4px;}
.hold-card{background:#12181F; border:1px solid rgba(169,205,231,.15); border-radius:14px; padding:14px 16px;}
.hold-top{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px;}
.hold-ticker{font-size:1.15rem; font-weight:800; color:#A9CDE7; letter-spacing:.02em;}
.hold-shares{font-size:.8rem; color:#8CA3B8;}
.hold-row{display:flex; justify-content:space-between; font-size:.86rem; padding:3px 0; color:#C7D3DE;}
.hold-row b{font-weight:650; color:#E8EDF2;}

/* ---------- trade detail grid (inside expanders) ---------- */
.tgrid{display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px 14px; margin:4px 0 12px 0;}
.tcell .tl{font-size:.66rem; text-transform:uppercase; letter-spacing:.07em; color:#8CA3B8; margin-bottom:2px;}
.tcell .tv{font-size:.95rem; font-weight:650; color:#E8EDF2;}
.tnotes{font-size:.85rem; color:#AFC0CF; background:rgba(169,205,231,.06); border-left:3px solid #A9CDE7;
        padding:8px 12px; border-radius:0 8px 8px 0; margin-bottom:12px;}

/* ---------- expanders as trade rows ---------- */
div[data-testid="stExpander"]{border:1px solid rgba(169,205,231,.13); border-radius:12px;
                              background:#10161E; margin-bottom:7px;}
div[data-testid="stExpander"] summary{padding:.55rem .9rem;}
div[data-testid="stExpander"] summary p{font-size:.92rem !important;}

/* ---------- ROC chips (dialog) ---------- */
.chips{display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 4px 0;}
.chip{background:rgba(169,205,231,.10); border:1px solid rgba(169,205,231,.28); color:#A9CDE7;
      border-radius:12px; padding:7px 16px; font-size:.95rem; font-weight:700; text-align:center;}
.chip small{display:block; font-size:.60rem; font-weight:600; letter-spacing:.07em;
            color:#8CA3B8; text-transform:uppercase; margin-bottom:2px;}
</style>
""", unsafe_allow_html=True)

# --- GOOGLE SHEETS SETUP ---
@st.cache_resource
def init_gsheets():
    creds_dict = json.loads(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open("Wheel Trades Database")  # Ensure your sheet is named exactly this
    return sh.sheet1

worksheet = init_gsheets()

# --- CACHED LIVE PRICES (one fetch per ticker per 5 min, shared everywhere) ---
@st.cache_data(ttl=300, show_spinner=False)
def get_live_price(ticker: str) -> float:
    try:
        hist = yf.Ticker(ticker).history(period="5d")['Close'].dropna()
        if len(hist) > 0:
            return float(hist.iloc[-1])
        return 0.0
    except Exception:
        return 0.0

# --- CLOSING PRICE ON A GIVEN DATE (for settling expirations correctly) ---
@st.cache_data(ttl=21600, show_spinner=False)
def get_price_on_date(ticker: str, iso_date: str) -> float:
    """Close on (or the last trading day before) the given date, so an expiration
    settles at the expiration-day price rather than today's price. Returns 0.0 if
    unavailable."""
    try:
        d = pd.Timestamp(iso_date).normalize()
        hist = yf.Ticker(ticker).history(
            start=(d - pd.Timedelta(days=6)).strftime('%Y-%m-%d'),
            end=(d + pd.Timedelta(days=2)).strftime('%Y-%m-%d')
        )['Close'].dropna()
        if hist.empty:
            return 0.0
        norm = []
        for x in hist.index:
            ts = pd.Timestamp(x)
            ts = ts.tz_localize(None) if ts.tzinfo is not None else ts
            norm.append(ts.normalize())
        hist.index = norm
        on_or_before = hist[hist.index <= d]
        if not on_or_before.empty:
            return float(on_or_before.iloc[-1])
        return float(hist.iloc[0])
    except Exception:
        return 0.0

# --- NEXT EARNINGS DATE (cached 6h; used for assignment-risk alerts) ---
@st.cache_data(ttl=21600, show_spinner=False)
def get_next_earnings(ticker: str):
    """Return the next upcoming earnings date (datetime.date) or None.

    Tries .calendar first, falls back to .get_earnings_dates(). Wrapped
    defensively because Yahoo's earnings feed is flaky.
    """
    try:
        t = yf.Ticker(ticker)
        candidates = []

        cal = getattr(t, "calendar", None)
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                candidates.extend(ed if isinstance(ed, (list, tuple)) else [ed])

        if not candidates:
            try:
                edf = t.get_earnings_dates(limit=12)
                if edf is not None and not edf.empty:
                    candidates.extend(list(edf.index))
            except Exception:
                pass

        today_ts = pd.Timestamp(date.today())
        future = []
        for d in candidates:
            try:
                ts = pd.Timestamp(d)
                if ts.tzinfo is not None:
                    ts = ts.tz_localize(None)
                ts = ts.normalize()
                if ts >= today_ts:
                    future.append(ts)
            except Exception:
                continue
        if future:
            return min(future).date()
    except Exception:
        return None
    return None

# --- DISTANCE OF SPOT FROM STRIKE (moneyness) ---
def strike_distance(current_price, strike, strategy):
    """Return (signed_pct, label, css_class) for how far spot is from the strike.

    signed_pct is (spot - strike) / strike. A Cash-Secured Put is OTM (safe)
    when spot is ABOVE strike; a Covered Call is OTM when spot is BELOW strike.
    Returns None if inputs are missing.
    """
    if not current_price or current_price <= 0 or not strike or strike <= 0:
        return None
    pct = (current_price - strike) / strike
    if strategy == 'Covered Call':
        otm = current_price < strike
    else:  # Cash-Secured Put (default)
        otm = current_price > strike
    dist = abs(pct) * 100
    if otm:
        return pct, f"{dist:.1f}% OTM", "pos"
    return pct, f"{dist:.1f}% ITM", "neg"

# --- STRICT DATA TYPE ENFORCER ---
def enforce_dtypes(df):
    """Forces Pandas to use the correct data types to prevent crash errors."""
    for col in ['Open Date', 'Expiration Date', 'Close Date']:
        df[col] = pd.to_datetime(df[col], errors='coerce')
    for col in ['Strike Price', 'Premium Collected', 'Cost Basis', 'P&L']:
        df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
    if '# Contracts' in df.columns:
        df['# Contracts'] = pd.to_numeric(df['# Contracts'], errors='coerce').fillna(1).astype(int)
    return df

def save_to_cloud(df):
    """Write the full trade log to Google Sheets SAFELY.

    Key fix vs. the old version: we write the new data FIRST, then clear only
    the leftover trailing rows. The old code called worksheet.clear() *before*
    writing, so any failure in between (network blip, rate limit, expired auth)
    left the sheet empty and unrecoverable. Now a failed write leaves the
    existing sheet untouched. Returns True on success, False on failure.
    """
    try:
        df_save = df.copy()

        # 1. Format dates FIRST (before filling NaNs)
        for col in ['Open Date', 'Expiration Date', 'Close Date']:
            df_save[col] = df_save[col].dt.strftime('%Y-%m-%d').replace('NaT', '')

        # 2. Replace any weird infinities or standard NaNs
        df_save = df_save.replace([np.inf, -np.inf], "")
        df_save = df_save.fillna("")

        # 3. Create the headers and data rows
        headers = df_save.columns.values.tolist()
        data_rows = df_save.values.tolist()

        # 4. Final aggressive scrub
        clean_data = [headers]
        for row in data_rows:
            clean_row = ["" if pd.isna(x) else x for x in row]
            clean_data.append(clean_row)

        # 5. Write EVERYTHING first (non-destructive if this raises)
        worksheet.update(values=clean_data, range_name='A1')

        # 6. Clear only stale rows *below* the freshly written data
        n_rows = len(clean_data)
        try:
            grid_rows = worksheet.row_count
        except Exception:
            grid_rows = n_rows
        if grid_rows > n_rows:
            worksheet.batch_clear([f"A{n_rows + 1}:Z{grid_rows}"])
        return True
    except Exception as e:
        st.error(f"⚠️ Could not save to Google Sheets: {e}\n\n"
                 f"Your changes are still in this session — press the button again to retry.")
        return False

# --- INITIALIZE DATA FROM CLOUD ---
if 'trades' not in st.session_state:
    try:
        records = worksheet.get_all_records()
        if records:
            loaded_df = pd.DataFrame(records)
            loaded_df.replace("", np.nan, inplace=True)
            loaded_df = enforce_dtypes(loaded_df)
            st.session_state.trades = loaded_df
        else:
            raise ValueError("Empty Sheet")
    except:
        empty_df = pd.DataFrame(columns=[
            'Open Date', 'Ticker', 'Strategy', 'Strike Price', '# Contracts',
            'Premium Collected', 'Cost Basis', 'Expiration Date', 'Status',
            'Close Date', 'P&L', 'Notes'
        ])
        st.session_state.trades = enforce_dtypes(empty_df)

if 'show_form' not in st.session_state:
    st.session_state.show_form = False
if 'edit_idx' not in st.session_state:
    st.session_state.edit_idx = None
if 'sort_by' not in st.session_state:
    st.session_state.sort_by = 'expiry'   # default: closest expiration first
if 'roll_parent_idx' not in st.session_state:
    st.session_state.roll_parent_idx = None   # set when rolling an open trade
if 'close_idx' not in st.session_state:
    st.session_state.close_idx = None         # set when buying-to-close an open trade
if 'show_close' not in st.session_state:
    st.session_state.show_close = False

# --- AUTO-UPDATE EXPIRATIONS ---
changed = False
today = pd.to_datetime(date.today())

for idx, row in st.session_state.trades.iterrows():
    if row['Status'] == 'Open' and pd.notna(row['Expiration Date']) and row['Expiration Date'] <= today:
        # Settle at the EXPIRATION-DAY close (fall back to the latest price).
        exp_iso = pd.to_datetime(row['Expiration Date']).strftime('%Y-%m-%d')
        settle_price = get_price_on_date(row['Ticker'], exp_iso)
        if settle_price <= 0:
            settle_price = get_live_price(row['Ticker'])
        if settle_price <= 0:
            continue  # no reliable price -> leave Open, don't guess

        strike = float(row['Strike Price']) if pd.notna(row['Strike Price']) else 0.0
        premium = float(row['Premium Collected']) if pd.notna(row['Premium Collected']) else 0.0

        if row['Strategy'] == 'Cash-Secured Put':
            worthless = settle_price >= strike     # put finished OTM
        elif row['Strategy'] == 'Covered Call':
            worthless = settle_price <= strike     # call finished OTM
        else:
            worthless = False

        # Expired worthless -> auto-closed, keep the FULL premium.
        # In-the-money -> assigned; premium is still kept and the share leg is tracked separately.
        st.session_state.trades.loc[idx, 'Status'] = 'Expired' if worthless else 'Assigned'
        st.session_state.trades.loc[idx, 'P&L'] = premium
        st.session_state.trades.loc[idx, 'Close Date'] = row['Expiration Date']
        changed = True

if changed:
    save_to_cloud(st.session_state.trades)

df = st.session_state.trades

# --- CALCULATE STOCK HOLDINGS & REALIZED P&L ---
holdings = []
realized_stock_pl = 0.0

if not df.empty:
    tickers = df['Ticker'].unique()
    for t in tickers:
        t_df = df[df['Ticker'] == t]

        csp_assigned = t_df[(t_df['Strategy'] == 'Cash-Secured Put') & (t_df['Status'] == 'Assigned')]
        shares_bought = csp_assigned['# Contracts'].sum() * 100
        total_cost = (csp_assigned['Strike Price'] * csp_assigned['# Contracts'] * 100).sum()
        avg_cost = total_cost / shares_bought if shares_bought > 0 else 0

        cc_assigned = t_df[(t_df['Strategy'] == 'Covered Call') & (t_df['Status'] == 'Assigned')]
        shares_sold = cc_assigned['# Contracts'].sum() * 100
        total_sale = (cc_assigned['Strike Price'] * cc_assigned['# Contracts'] * 100).sum()

        current_shares = shares_bought - shares_sold

        if shares_sold > 0:
            realized_stock_pl += total_sale - (shares_sold * avg_cost)

        if current_shares > 0:
            p = get_live_price(t)
            curr_price = p if p > 0 else avg_cost

            stock_pl_dollars = (curr_price - avg_cost) * current_shares
            stock_pl_pct = (curr_price - avg_cost) / avg_cost if avg_cost > 0 else 0

            all_ccs = t_df[t_df['Strategy'] == 'Covered Call']

            def get_net_prem(sub_df):
                if sub_df.empty: return 0.0
                open_p = sub_df[sub_df['Status'] == 'Open']['Premium Collected'].sum()
                closed_p = sub_df[sub_df['Status'] != 'Open']['P&L'].sum()
                return open_p + closed_p

            total_premiums = get_net_prem(csp_assigned) + get_net_prem(all_ccs)

            pl_with_premiums_dollars = stock_pl_dollars + total_premiums
            total_cost_basis = avg_cost * current_shares
            pl_with_premiums_pct = (pl_with_premiums_dollars / total_cost_basis) if total_cost_basis > 0 else 0

            holdings.append({
                'Ticker': t,
                'Shares': int(current_shares),
                'Assigned Price': avg_cost,
                'Current Price': curr_price,
                '$ P&L': stock_pl_dollars,
                '% P&L': stock_pl_pct,
                'Total Premiums': total_premiums,
                '% P&L (w/ Prem)': pl_with_premiums_pct
            })

holdings_df = pd.DataFrame(holdings)

# --- CALCULATE NET TOTAL PREMIUM & WIN RATE ---
# Win rate rule: a LOSS is only a buyback with negative P&L.
# Assignments and expirations (premium kept) count as WINS.
total_premium = 0.0
options_pl = 0.0
win_rate = 0.0
closed_trades = pd.DataFrame()

if not df.empty:
    open_prem = df[df['Status'] == 'Open']['Premium Collected'].sum()
    closed_pl = df[df['Status'] != 'Open']['P&L'].sum()
    total_premium = open_prem + closed_pl
    options_pl = closed_pl

    closed_trades = df[df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])]
    if len(closed_trades) > 0:
        losses = closed_trades[closed_trades['P&L'] < 0]
        win_rate = (1 - len(losses) / len(closed_trades)) * 100

grand_total_pl = options_pl + realized_stock_pl
open_positions = len(df[df['Status'] == 'Open']) if not df.empty else 0

# --- CALCULATE AVERAGE ANNUAL ROC ---
def trade_roc_metrics(row):
    """Returns (roc, annual_roc, days_held) for a single trade, or None."""
    cost_basis = row['Cost Basis']
    if pd.isna(cost_basis) or cost_basis <= 0:
        return None
    start_date = pd.to_datetime(row['Open Date'])
    if pd.isna(start_date):
        return None

    if row['Status'] == 'Open':
        end_date = pd.to_datetime(today)
        net_profit = row['Premium Collected']
    else:
        end_date = pd.to_datetime(row['Close Date']) if pd.notna(row['Close Date']) else pd.to_datetime(row['Expiration Date'])
        net_profit = row['P&L']

    if pd.isna(end_date) or pd.isna(net_profit):
        return None

    days_held = (end_date - start_date).days
    if days_held <= 0:
        days_held = 1

    roc = net_profit / cost_basis
    annual_roc = roc * (365 / days_held)
    return roc, annual_roc, days_held

roc_list = []
if not df.empty:
    for _, row in df.iterrows():
        m = trade_roc_metrics(row)
        if m is not None:
            roc_list.append(m[1])

avg_annual_roc = sum(roc_list) / len(roc_list) if roc_list else 0.0

# =====================================================================
#  CAPITAL AT RISK  &  CAPITAL-WEIGHTED RETURNS
#  - Capital at risk = collateral committed on OPEN contracts
#    (strike x 100 x contracts). For a CSP this is cash secured; for a CC
#    it's the notional of the shares tied up.
#  - Return on capital = premium yield on that open capital, annualized by
#    the capital-weighted average days-to-expiration of the open book.
#  - Cap-weighted ROC = realized annualized ROC, weighting each closed
#    trade by its cost basis so a tiny 1-day trade can't distort the mean.
# =====================================================================
open_capital_at_risk = 0.0
open_premium_at_risk = 0.0
wdte_num = 0.0          # capital-weighted DTE numerator
wdte_den = 0.0
cw_num = 0.0            # Σ annual_roc_i * capital_i (realized)
cw_den = 0.0           # Σ capital_i (realized)
realized_np = 0.0      # Σ realized net P&L
realized_cap = 0.0     # Σ realized capital

if not df.empty:
    for _, row in df.iterrows():
        if row['Status'] == 'Open':
            n_c = int(row['# Contracts']) if pd.notna(row['# Contracts']) else 1
            strike = float(row['Strike Price']) if pd.notna(row['Strike Price']) else 0.0
            cap = strike * 100 * n_c
            open_capital_at_risk += cap
            open_premium_at_risk += float(row['Premium Collected']) if pd.notna(row['Premium Collected']) else 0.0
            if pd.notna(row['Expiration Date']) and cap > 0:
                dte = max((row['Expiration Date'] - today).days, 1)
                wdte_num += dte * cap
                wdte_den += cap
        elif row['Status'] in ('Closed', 'Expired', 'Assigned', 'Rolled'):
            m = trade_roc_metrics(row)
            cb = float(row['Cost Basis']) if pd.notna(row['Cost Basis']) else 0.0
            if m is not None and cb > 0:
                _, aroc, _ = m
                cw_num += aroc * cb
                cw_den += cb
                realized_np += float(row['P&L']) if pd.notna(row['P&L']) else 0.0
                realized_cap += cb

cap_wtd_annual_roc = (cw_num / cw_den) if cw_den > 0 else 0.0
realized_return_on_cap = (realized_np / realized_cap) if realized_cap > 0 else 0.0
avg_open_dte = (wdte_num / wdte_den) if wdte_den > 0 else 0.0
roc_period_open = (open_premium_at_risk / open_capital_at_risk) if open_capital_at_risk > 0 else 0.0
return_on_capital_ann = roc_period_open * (365 / avg_open_dte) if avg_open_dte > 0 else 0.0

# =====================================================================
#  TRADE DIALOG (add / edit) — with live %ROC preview
# =====================================================================
@st.dialog("Log / Edit Trade", width="large")
def trade_dialog():
    idx = st.session_state.edit_idx
    roll_parent = st.session_state.roll_parent_idx
    is_roll = roll_parent is not None and idx is None
    token = "roll" if is_roll else ("new" if idx is None else f"e{idx}")

    def_ticker, def_strike, def_premium, def_open_date = "", 0.0, 0.0, date.today()
    def_status, def_pnl, def_strategy = "Open", 0.0, "Cash-Secured Put"
    def_contracts, def_cost, def_exp_date = 1, 0.0, date.today()
    def_close_date, def_notes = None, ""
    roll_old_premium = 0.0   # premium already collected on the trade being rolled

    if is_roll:
        p = st.session_state.trades.loc[roll_parent]
        def_ticker = p['Ticker']
        def_strategy = p['Strategy'] if p['Strategy'] in ("Cash-Secured Put", "Covered Call") else "Cash-Secured Put"
        def_contracts = int(p['# Contracts']) if pd.notna(p['# Contracts']) else 1
        def_strike = float(p['Strike Price']) if pd.notna(p['Strike Price']) else 0.0
        def_cost = float(p['Cost Basis']) if pd.notna(p['Cost Basis']) else 0.0
        def_open_date = date.today()
        roll_old_premium = float(p['Premium Collected']) if pd.notna(p['Premium Collected']) else 0.0
        st.info(f"🔄 Rolling **{def_ticker} · {def_strategy}** — enter the NEW contract below. "
                f"The old one is marked **Rolled** automatically when you save.")

    if idx is not None:
        edit_row = st.session_state.trades.loc[idx]
        def_ticker = edit_row['Ticker']
        def_strike = float(edit_row['Strike Price']) if pd.notna(edit_row['Strike Price']) else 0.0
        def_premium = float(edit_row['Premium Collected']) if pd.notna(edit_row['Premium Collected']) else 0.0
        def_open_date = pd.to_datetime(edit_row['Open Date']).date() if pd.notna(edit_row['Open Date']) else date.today()
        def_status = edit_row['Status']
        def_pnl = float(edit_row['P&L']) if pd.notna(edit_row['P&L']) else 0.0
        def_strategy = edit_row['Strategy']
        def_contracts = int(edit_row['# Contracts']) if pd.notna(edit_row['# Contracts']) else 1
        def_cost = float(edit_row['Cost Basis']) if pd.notna(edit_row['Cost Basis']) else 0.0
        def_exp_date = pd.to_datetime(edit_row['Expiration Date']).date() if pd.notna(edit_row['Expiration Date']) else date.today()
        if pd.notna(edit_row['Close Date']):
            def_close_date = pd.to_datetime(edit_row['Close Date']).date()
        def_notes = str(edit_row['Notes']) if pd.notna(edit_row['Notes']) else ""

    status_options = ["Open", "Closed", "Assigned", "Rolled", "Expired"]
    strategy_options = ["Cash-Secured Put", "Covered Call"]

    c1, c2 = st.columns(2)
    with c1:
        ticker = st.text_input("Ticker (e.g., AAPL)", value=def_ticker, key=f"f_ticker_{token}")
        strike = st.number_input("Strike Price", min_value=0.0, format="%.2f", value=def_strike, key=f"f_strike_{token}")
        premium = st.number_input(
            "Premium / share (×100 × contracts applied)" if idx is None else "Premium Collected ($ total)",
            min_value=0.0, format="%.2f", value=def_premium, key=f"f_prem_{token}")
        open_date = st.date_input("Open Date", value=def_open_date, key=f"f_open_{token}")
        status = st.selectbox("Status", status_options,
                              index=status_options.index(def_status) if def_status in status_options else 0,
                              key=f"f_status_{token}")
        pnl = st.number_input("P&L ($)", format="%.2f", value=def_pnl, key=f"f_pnl_{token}")
    with c2:
        strategy = st.selectbox("Strategy", strategy_options,
                                index=strategy_options.index(def_strategy) if def_strategy in strategy_options else 0,
                                key=f"f_strat_{token}")
        contracts = st.number_input("# Contracts", min_value=1, step=1, value=def_contracts, key=f"f_con_{token}")
        cost_basis = st.number_input("Cost Basis ($)", min_value=0.0, format="%.2f", value=def_cost, key=f"f_cost_{token}")
        exp_date = st.date_input("Expiration Date", value=def_exp_date, key=f"f_exp_{token}")
        close_date = st.date_input("Close Date", value=def_close_date, key=f"f_close_{token}")

    # --- ROLL: cost to close the OLD contract (entered per share) ---
    buyback_cost = 0.0
    if is_roll:
        buyback_cost = st.number_input(
            "Cost to Close Old / share (×100 × contracts applied)",
            min_value=0.0, format="%.2f", value=0.0, key=f"f_buyback_{token}",
            help="Per-share debit paid to buy back the old contract. "
                 "Old trade P&L = old premium − (this × 100 × contracts)."
        )

    # --- LIVE PREVIEW CHIPS (ROC + live price + distance + roll math) ---
    new_entry = idx is None
    days_to_exp = max((exp_date - open_date).days, 1)
    eff_premium = premium * 100 * int(contracts) if new_entry else float(premium)
    eff_buyback = buyback_cost * 100 * int(contracts)

    chips = []
    if cost_basis > 0 and eff_premium != 0:
        roc_pct = (eff_premium / cost_basis) * 100
        annual_roc_pct = roc_pct * (365 / days_to_exp)
        chips.append(("%ROC", f"{roc_pct:.2f}%"))
        chips.append(("Annualized ROC", f"{annual_roc_pct:.1f}%"))
    chips.append(("Days to Exp", f"{days_to_exp}"))
    if new_entry:
        chips.append(("Premium stored", f"${eff_premium:,.2f}"))

    live_px = get_live_price(ticker.upper()) if ticker.strip() != "" else 0.0
    if live_px > 0:
        chips.append(("Live Price", f"${live_px:,.2f}"))
    sd = strike_distance(live_px, strike, strategy)
    if sd is not None:
        _, sd_label, _ = sd
        chips.append(("Price vs Strike", sd_label))

    chips_html = "<div class='chips'>"
    for lab, v in chips:
        chips_html += f"<div class='chip'><small>{lab}</small>{v}</div>"
    chips_html += "</div>"
    st.markdown(chips_html, unsafe_allow_html=True)
    if cost_basis <= 0 or eff_premium <= 0:
        st.caption("Enter Premium and Cost Basis to preview %ROC and Annualized ROC.")

    if is_roll:
        roll_pnl_old = roll_old_premium - eff_buyback
        chain_total = roll_pnl_old + eff_premium
        st.markdown(
            "<div class='chips'>"
            f"<div class='chip'><small>Old Premium</small>${roll_old_premium:,.2f}</div>"
            f"<div class='chip'><small>− Buyback</small>${eff_buyback:,.2f}</div>"
            f"<div class='chip'><small>+ New Premium</small>${eff_premium:,.2f}</div>"
            f"<div class='chip'><small>Roll Chain P&L</small>${chain_total:,.2f}</div>"
            "</div>", unsafe_allow_html=True)
        st.caption("Old (rolled) trade books **old premium − buyback**; the new open trade carries "
                   "**new premium**. Total across the chain = old − buyback + new.")

    notes = st.text_area("Notes", value=def_notes, placeholder="Trade notes...", key=f"f_notes_{token}")

    b1, b2 = st.columns(2)
    cancel = b1.button("Cancel", use_container_width=True, key=f"f_cancel_{token}")
    submit = b2.button("Save Trade", type="primary", use_container_width=True, key=f"f_save_{token}")

    if cancel:
        st.session_state.edit_idx = None
        st.session_state.roll_parent_idx = None
        st.session_state.show_form = False
        st.rerun()

    if submit:
        if ticker != "":
            open_dt = pd.to_datetime(open_date)
            ticker_upper = ticker.upper()

            if idx is not None:
                st.session_state.trades.loc[idx, 'Open Date'] = open_dt
                st.session_state.trades.loc[idx, 'Ticker'] = ticker_upper
                st.session_state.trades.loc[idx, 'Strategy'] = strategy
                st.session_state.trades.loc[idx, 'Strike Price'] = float(strike)
                st.session_state.trades.loc[idx, '# Contracts'] = int(contracts)
                st.session_state.trades.loc[idx, 'Premium Collected'] = float(premium)
                st.session_state.trades.loc[idx, 'Cost Basis'] = float(cost_basis)
                st.session_state.trades.loc[idx, 'Expiration Date'] = pd.to_datetime(exp_date)
                st.session_state.trades.loc[idx, 'Status'] = status
                st.session_state.trades.loc[idx, 'Close Date'] = pd.to_datetime(close_date) if close_date else pd.NaT
                st.session_state.trades.loc[idx, 'P&L'] = float(pnl)
                st.session_state.trades.loc[idx, 'Notes'] = notes
            else:
                # If this is a ROLL, close out the parent contract FIRST while its
                # index is still stable (concat(ignore_index=True) below would
                # otherwise renumber the rows and break the reference).
                if is_roll and roll_parent in st.session_state.trades.index:
                    parent_prem = st.session_state.trades.loc[roll_parent, 'Premium Collected']
                    parent_prem = float(parent_prem) if pd.notna(parent_prem) else 0.0
                    st.session_state.trades.loc[roll_parent, 'Status'] = 'Rolled'
                    st.session_state.trades.loc[roll_parent, 'Close Date'] = pd.to_datetime(date.today())
                    st.session_state.trades.loc[roll_parent, 'P&L'] = parent_prem - eff_buyback

                new_data = {
                    'Open Date': open_dt,
                    'Ticker': ticker_upper,
                    'Strategy': strategy,
                    'Strike Price': float(strike),
                    '# Contracts': int(contracts),
                    'Premium Collected': eff_premium,
                    'Cost Basis': float(cost_basis),
                    'Expiration Date': pd.to_datetime(exp_date),
                    'Status': status,
                    'Close Date': pd.to_datetime(close_date) if close_date else pd.NaT,
                    'P&L': float(pnl),
                    'Notes': notes
                }
                new_df = pd.DataFrame([new_data])
                st.session_state.trades = pd.concat([st.session_state.trades, new_df], ignore_index=True)

            st.session_state.trades = enforce_dtypes(st.session_state.trades)
            save_to_cloud(st.session_state.trades)
            st.session_state.edit_idx = None
            st.session_state.roll_parent_idx = None
            st.session_state.show_form = False
            st.rerun()
        else:
            st.error("Please enter a Ticker symbol.")

@st.dialog("Close Position", width="large")
def close_dialog():
    idx = st.session_state.close_idx
    if idx is None or idx not in st.session_state.trades.index:
        return
    row = st.session_state.trades.loc[idx]

    ticker = row['Ticker']
    strategy = row['Strategy']
    strike = float(row['Strike Price']) if pd.notna(row['Strike Price']) else 0.0
    contracts = int(row['# Contracts']) if pd.notna(row['# Contracts']) else 1
    premium = float(row['Premium Collected']) if pd.notna(row['Premium Collected']) else 0.0
    cost_basis = float(row['Cost Basis']) if pd.notna(row['Cost Basis']) else 0.0
    exp_str = pd.to_datetime(row['Expiration Date']).strftime('%Y-%m-%d') if pd.notna(row['Expiration Date']) else "—"

    st.info(f"✅ Closing **{ticker} · {strategy}** — {contracts} contract(s) @ ${strike:,.2f}, exp {exp_str}. "
            f"Enter the buyback price; realized P&L = premium − buyback.")

    buyback = st.number_input(
        "Buyback price / share (×100 × contracts applied)",
        min_value=0.0, format="%.2f", value=0.0, key=f"close_bb_{idx}",
        help="Per-share debit to buy the option back. "
             "Realized P&L = premium collected − (this × 100 × contracts)."
    )
    close_date = st.date_input("Close Date", value=date.today(), key=f"close_date_{idx}")

    eff_buyback = buyback * 100 * int(contracts)
    realized = premium - eff_buyback
    roc_pct = (realized / cost_basis * 100) if cost_basis > 0 else None

    preview = (
        "<div class='chips'>"
        f"<div class='chip'><small>Premium</small>${premium:,.2f}</div>"
        f"<div class='chip'><small>− Buyback</small>${eff_buyback:,.2f}</div>"
        f"<div class='chip'><small>Realized P&L</small>${realized:,.2f}</div>"
    )
    if roc_pct is not None:
        preview += f"<div class='chip'><small>ROC</small>{roc_pct:.2f}%</div>"
    preview += "</div>"
    st.markdown(preview, unsafe_allow_html=True)

    b1, b2 = st.columns(2)
    cancel = b1.button("Cancel", use_container_width=True, key=f"close_cancel_{idx}")
    confirm = b2.button("Confirm Close", type="primary", use_container_width=True, key=f"close_confirm_{idx}")

    if cancel:
        st.session_state.close_idx = None
        st.rerun()
    if confirm:
        st.session_state.trades.loc[idx, 'Status'] = 'Closed'
        st.session_state.trades.loc[idx, 'Close Date'] = pd.to_datetime(close_date)
        st.session_state.trades.loc[idx, 'P&L'] = realized
        st.session_state.trades = enforce_dtypes(st.session_state.trades)
        save_to_cloud(st.session_state.trades)
        st.session_state.close_idx = None
        st.rerun()

# =====================================================================
#  HEADER
# =====================================================================
col1, col2 = st.columns([3, 1])
with col1:
    st.title("Wheel Strategy Log")
    st.caption("Track your CSP & CC trades")
with col2:
    st.write("")
    if st.button("➕ New Trade", type="primary", use_container_width=True):
        st.session_state.edit_idx = None
        st.session_state.roll_parent_idx = None
        st.session_state.show_form = True
        st.rerun()

if st.session_state.show_form:
    st.session_state.show_form = False   # so an X-dismiss doesn't reopen it
    trade_dialog()

if st.session_state.show_close:
    st.session_state.show_close = False   # so an X-dismiss doesn't reopen it
    close_dialog()

# =====================================================================
#  KPI CARDS (responsive grid: 2 per row on phone, 5 on desktop)
# =====================================================================
def kpi_class(val):
    return "pos" if val >= 0 else "neg"

wr_display = f"{win_rate:.1f}%" if (not df.empty and len(closed_trades) > 0) else "—"
cwr_display = f"{cap_wtd_annual_roc * 100:.1f}%" if cw_den > 0 else "—"
cwr_class = kpi_class(cap_wtd_annual_roc) if cw_den > 0 else "neutral"
car_display = f"${open_capital_at_risk:,.0f}" if open_capital_at_risk > 0 else "—"
roc_ann_display = f"{return_on_capital_ann * 100:.1f}%" if (open_capital_at_risk > 0 and avg_open_dte > 0) else "—"
roc_period_display = f"{roc_period_open * 100:.2f}%" if open_capital_at_risk > 0 else "—"
roc_ann_class = "pos" if return_on_capital_ann >= 0 else "neg"

st.markdown(f"""
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-label">💲 Net Premium</div>
    <div class="kpi-value {kpi_class(total_premium)}">${total_premium:,.2f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">📈 Total P&amp;L</div>
    <div class="kpi-value {kpi_class(grand_total_pl)}">${grand_total_pl:,.2f}</div>
  </div>
  <div class="kpi" title="Collateral committed across open contracts (strike x 100 x contracts).">
    <div class="kpi-label">🔒 Capital at Risk</div>
    <div class="kpi-value accent">{car_display}</div>
  </div>
  <div class="kpi" title="Annualized premium yield on open capital at risk — {roc_period_display} over an avg {avg_open_dte:.0f} DTE.">
    <div class="kpi-label">⚡ Return on Capital</div>
    <div class="kpi-value {roc_ann_class}">{roc_ann_display}</div>
  </div>
  <div class="kpi" title="Realized annualized ROC, weighting each closed trade by its cost basis (so tiny trades don't distort it).">
    <div class="kpi-label">🚀 Cap-Wtd ROC (ann.)</div>
    <div class="kpi-value {cwr_class}">{cwr_display}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">🎯 Win Rate</div>
    <div class="kpi-value neutral">{wr_display}</div>
  </div>
  <div class="kpi">
    <div class="kpi-label">📊 Open Options</div>
    <div class="kpi-value accent">{open_positions}</div>
  </div>
</div>
""", unsafe_allow_html=True)

st.divider()

# =====================================================================
#  CURRENT HOLDINGS (ASSIGNED) — responsive cards
# =====================================================================
if not holdings_df.empty:
    st.subheader("Current Stock Holdings")
    cards_html = "<div class='hold-grid'>"
    for _, h in holdings_df.iterrows():
        pl_cls = "pos" if h['$ P&L'] >= 0 else "neg"
        plp_cls = "pos" if h['% P&L (w/ Prem)'] >= 0 else "neg"
        cards_html += f"""
        <div class="hold-card">
          <div class="hold-top">
            <span class="hold-ticker">{h['Ticker']}</span>
            <span class="hold-shares">{h['Shares']} sh @ ${h['Assigned Price']:,.2f}</span>
          </div>
          <div class="hold-row"><span>Current Price</span><b>${h['Current Price']:,.2f}</b></div>
          <div class="hold-row"><span>Stock P&amp;L</span><b class="{pl_cls}">${h['$ P&L']:,.2f} ({h['% P&L']:.1%})</b></div>
          <div class="hold-row"><span>Premiums Collected</span><b class="accent">${h['Total Premiums']:,.2f}</b></div>
          <div class="hold-row"><span>P&amp;L w/ Premiums</span><b class="{plp_cls}">{h['% P&L (w/ Prem)']:.1%}</b></div>
        </div>"""
    cards_html += "</div>"
    st.markdown(cards_html, unsafe_allow_html=True)
    st.divider()

# =====================================================================
#  EARNINGS-BEFORE-EXPIRY ALERTS (open positions)
# =====================================================================
if not df.empty:
    _alerts = {}
    for _, r in df[df['Status'] == 'Open'].iterrows():
        if pd.notna(r['Expiration Date']) and pd.notna(r['Ticker']):
            ed = get_next_earnings(r['Ticker'])
            if ed is not None and pd.Timestamp(ed) <= r['Expiration Date']:
                _alerts[(r['Ticker'], ed)] = pd.Timestamp(ed).strftime('%b %d')
    if _alerts:
        _line = " · ".join(f"**{tk}** (ER {d})" for (tk, _e), d in _alerts.items())
        st.warning(f"📢 Earnings land before expiration on open positions: {_line}")

# =====================================================================
#  TRADES — tap-to-expand rows, sortable by Expiry or Ticker
# =====================================================================
count_open = len(df[df['Status'] == 'Open']) if not df.empty else 0
count_assigned = len(df[df['Status'] == 'Assigned']) if not df.empty else 0
count_expired = len(df[df['Status'] == 'Expired']) if not df.empty else 0
count_closed = len(df[df['Status'] == 'Closed']) if not df.empty else 0
count_rolled = len(df[df['Status'] == 'Rolled']) if not df.empty else 0

th_col, s1, s2 = st.columns([2.4, 1, 1])
with th_col:
    st.subheader("Trades")
with s1:
    st.write("")
    if st.button("⏳ Expiry", type="primary" if st.session_state.sort_by == 'expiry' else "secondary",
                 use_container_width=True, help="Sort by expiration date (closest first)"):
        st.session_state.sort_by = 'expiry'
        st.rerun()
with s2:
    st.write("")
    if st.button("🔤 Ticker", type="primary" if st.session_state.sort_by == 'ticker' else "secondary",
                 use_container_width=True, help="Group trades by ticker (A→Z)"):
        st.session_state.sort_by = 'ticker'
        st.rerun()

tabs = st.tabs([
    f"All ({len(df)})",
    f"Open ({count_open})",
    f"Assigned ({count_assigned})",
    f"Expired ({count_expired})",
    f"Closed ({count_closed})",
    f"Rolled ({count_rolled})"
])

def money(v):
    """Dollar string safe for Streamlit markdown (escaped so $...$ isn't parsed as LaTeX)."""
    return f"\\${v:,.2f}"

def display_trades(filtered_df, tab_key=""):
    if filtered_df.empty:
        st.info("No trades found in this category.")
        return

    # --- SORTING: default closest expiration first; or grouped by ticker ---
    if st.session_state.sort_by == 'ticker':
        fdf = filtered_df.sort_values(['Ticker', 'Expiration Date'], na_position='last')
    else:
        fdf = filtered_df.sort_values(['Expiration Date', 'Ticker'], na_position='last')

    for i, row in fdf.iterrows():
        ticker = row['Ticker']
        strat = "CSP" if row['Strategy'] == "Cash-Secured Put" else "CC"
        n_con = int(row['# Contracts']) if pd.notna(row['# Contracts']) else 1
        qty = f" ×{n_con}" if n_con > 1 else ""
        strike_str = money(row['Strike Price']) if pd.notna(row['Strike Price']) else "—"
        is_open = row['Status'] == 'Open'

        # --- summary line: value + urgency + moneyness + earnings ---
        dist_seg = ""
        er_flag = ""
        if is_open:
            val = row['Premium Collected'] if pd.notna(row['Premium Collected']) else 0.0
            val_str = f":green[+{money(val)}]" if val >= 0 else f":red[{money(val)}]"
            if pd.notna(row['Expiration Date']):
                d = (row['Expiration Date'] - today).days
                if d < 0:
                    tail = ":red[⚠️ expired]"
                elif d <= 3:
                    tail = f":red[⏳ {d}d]"
                elif d <= 10:
                    tail = f":orange[⏳ {d}d]"
                else:
                    tail = f":gray[⏳ {d}d]"
            else:
                tail = ":gray[no expiry]"

            cp_sum = get_live_price(ticker)
            sd = strike_distance(cp_sum, row['Strike Price'], row['Strategy'])
            if sd is not None:
                _, sd_label, sd_cls = sd
                dist_seg = f" · :{'green' if sd_cls == 'pos' else 'red'}[{sd_label}]"

            if pd.notna(row['Expiration Date']):
                ed = get_next_earnings(ticker)
                if ed is not None and pd.Timestamp(ed) <= row['Expiration Date']:
                    er_flag = f" · :red[📢 ER {pd.Timestamp(ed).strftime('%m/%d')}]"
        else:
            val = row['P&L'] if pd.notna(row['P&L']) else 0.0
            val_str = f":green[+{money(val)}]" if val >= 0 else f":red[{money(val)}]"
            tail = f":gray[{row['Status']}]"

        label = f"**{ticker}** · {strat} {strike_str}{qty} · {val_str}{dist_seg} · {tail}{er_flag}"

        with st.expander(label):
            open_d = row['Open Date'].strftime('%Y-%m-%d') if pd.notna(row['Open Date']) else "—"
            exp_d = row['Expiration Date'].strftime('%Y-%m-%d') if pd.notna(row['Expiration Date']) else "—"
            close_d = row['Close Date'].strftime('%Y-%m-%d') if pd.notna(row['Close Date']) else "—"
            prem_str = f"${row['Premium Collected']:,.2f}" if pd.notna(row['Premium Collected']) else "—"
            cost_str = f"${row['Cost Basis']:,.2f}" if pd.notna(row['Cost Basis']) and row['Cost Basis'] > 0 else "—"
            pnl_val = row['P&L'] if pd.notna(row['P&L']) else 0.0
            pnl_cls = "pos" if pnl_val >= 0 else "neg"

            cells = [
                ("Status", row['Status'], "neutral"),
                ("Open Date", open_d, "neutral"),
                ("Expiration", exp_d, "neutral"),
            ]
            if not is_open:
                cells.append(("Close Date", close_d, "neutral"))
            cells += [
                ("Strike", f"${row['Strike Price']:,.2f}" if pd.notna(row['Strike Price']) else "—", "neutral"),
                ("Contracts", str(n_con), "neutral"),
                ("Premium", prem_str, "accent"),
                ("Cost Basis", cost_str, "neutral"),
                ("P&L", f"${pnl_val:,.2f}", pnl_cls),
            ]

            if is_open:
                cp = get_live_price(ticker)
                cells.append(("Cur. Price", f"${cp:,.2f}" if cp > 0 else "—", "neutral"))
                sd = strike_distance(cp, row['Strike Price'], row['Strategy'])
                if sd is not None:
                    _, sd_label, sd_cls = sd
                    cells.append(("Dist. to Strike", sd_label, sd_cls))
                ed = get_next_earnings(ticker)
                if ed is not None:
                    _before = pd.notna(row['Expiration Date']) and pd.Timestamp(ed) <= row['Expiration Date']
                    cells.append(("Next Earnings",
                                  pd.Timestamp(ed).strftime('%Y-%m-%d') + (" ⚠️" if _before else ""),
                                  "neg" if _before else "neutral"))

            m = trade_roc_metrics(row)
            if m is not None:
                roc, aroc, dh = m
                cells.append(("%ROC", f"{roc * 100:.2f}%", "pos" if roc >= 0 else "neg"))
                cells.append(("Ann. ROC", f"{aroc * 100:.1f}%", "pos" if aroc >= 0 else "neg"))

            grid = "<div class='tgrid'>"
            for lab, v, cls in cells:
                grid += f"<div class='tcell'><div class='tl'>{lab}</div><div class='tv {cls}'>{v}</div></div>"
            grid += "</div>"
            st.markdown(grid, unsafe_allow_html=True)

            notes_val = str(row['Notes']) if pd.notna(row['Notes']) and str(row['Notes']).strip() != "" else ""
            if notes_val:
                st.markdown(f"<div class='tnotes'>{notes_val}</div>", unsafe_allow_html=True)

            if is_open:
                a0, a1, a2, a3 = st.columns(4)
                close_clicked = a0.button("✅ Close", key=f"close_{tab_key}_{i}", use_container_width=True)
                roll_clicked = a1.button("🔄 Roll", key=f"roll_{tab_key}_{i}", use_container_width=True)
                edit_clicked = a2.button("✏️ Edit", key=f"edit_{tab_key}_{i}", use_container_width=True)
                del_clicked = a3.button("🗑️ Delete", key=f"del_{tab_key}_{i}", use_container_width=True)
            else:
                a1, a2, _ = st.columns([1, 1, 1])
                close_clicked = False
                roll_clicked = False
                edit_clicked = a1.button("✏️ Edit", key=f"edit_{tab_key}_{i}", use_container_width=True)
                del_clicked = a2.button("🗑️ Delete", key=f"del_{tab_key}_{i}", use_container_width=True)

            if close_clicked:
                st.session_state.close_idx = i
                st.session_state.edit_idx = None
                st.session_state.roll_parent_idx = None
                st.session_state.show_close = True
                st.rerun()
            if roll_clicked:
                st.session_state.roll_parent_idx = i
                st.session_state.edit_idx = None
                st.session_state.show_form = True
                st.rerun()
            if edit_clicked:
                st.session_state.edit_idx = i
                st.session_state.roll_parent_idx = None
                st.session_state.show_form = True
                st.rerun()
            if del_clicked:
                st.session_state.trades = st.session_state.trades.drop(i)
                save_to_cloud(st.session_state.trades)
                st.rerun()

with tabs[0]: display_trades(df, "all")
with tabs[1]: display_trades(df[df['Status'] == 'Open'], "open")
with tabs[2]: display_trades(df[df['Status'] == 'Assigned'], "assigned")
with tabs[3]: display_trades(df[df['Status'] == 'Expired'], "expired")
with tabs[4]: display_trades(df[df['Status'] == 'Closed'], "closed")
with tabs[5]: display_trades(df[df['Status'] == 'Rolled'], "rolled")

st.divider()

# =====================================================================
#  TICKER PERFORMANCE — per-symbol summary
# =====================================================================
st.subheader("Ticker Performance")

def ticker_summary(tk):
    """Aggregate premium, realized P&L, win rate, holdings and breakeven for one symbol."""
    t_df = df[df['Ticker'] == tk]

    open_p = t_df[t_df['Status'] == 'Open']['Premium Collected'].sum()
    closed_p = t_df[t_df['Status'] != 'Open']['P&L'].sum()
    net_prem = float(open_p + closed_p)
    realized_opt = float(closed_p)

    csp_assigned = t_df[(t_df['Strategy'] == 'Cash-Secured Put') & (t_df['Status'] == 'Assigned')]
    shares_bought = csp_assigned['# Contracts'].sum() * 100
    total_cost = (csp_assigned['Strike Price'] * csp_assigned['# Contracts'] * 100).sum()
    avg_cost = total_cost / shares_bought if shares_bought > 0 else 0.0

    cc_assigned = t_df[(t_df['Strategy'] == 'Covered Call') & (t_df['Status'] == 'Assigned')]
    shares_sold = cc_assigned['# Contracts'].sum() * 100
    total_sale = (cc_assigned['Strike Price'] * cc_assigned['# Contracts'] * 100).sum()
    stock_realized = float(total_sale - shares_sold * avg_cost) if shares_sold > 0 else 0.0
    current_shares = int(shares_bought - shares_sold)

    realized = t_df[t_df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])]
    n_realized = len(realized)
    losses = realized[realized['P&L'] < 0]
    wr = (1 - len(losses) / n_realized) * 100 if n_realized > 0 else None

    breakeven = None
    if current_shares > 0 and avg_cost > 0:
        breakeven = avg_cost - (net_prem / current_shares)

    return {
        'net_prem': net_prem, 'realized_opt': realized_opt, 'stock_realized': stock_realized,
        'total_pl': realized_opt + stock_realized, 'wr': wr,
        'n_open': len(t_df[t_df['Status'] == 'Open']), 'n_trades': len(t_df),
        'current_shares': current_shares, 'avg_cost': avg_cost, 'breakeven': breakeven,
    }

if not df.empty and len(df['Ticker'].dropna().unique()) > 0:
    all_tickers = sorted(df['Ticker'].dropna().unique().tolist())
    sel_tk = st.selectbox("Select a ticker", options=all_tickers, key="ticker_perf_select")
    s = ticker_summary(sel_tk)

    def _card(label, value, cls="neutral"):
        return (f"<div class='kpi'><div class='kpi-label'>{label}</div>"
                f"<div class='kpi-value {cls}'>{value}</div></div>")

    perf_html = "<div class='kpi-grid'>"
    perf_html += _card("Net Premium", f"${s['net_prem']:,.2f}", kpi_class(s['net_prem']))
    perf_html += _card("Realized Options P&amp;L", f"${s['realized_opt']:,.2f}", kpi_class(s['realized_opt']))
    if s['stock_realized'] != 0:
        perf_html += _card("Realized Stock P&amp;L", f"${s['stock_realized']:,.2f}", kpi_class(s['stock_realized']))
    perf_html += _card("Total Realized P&amp;L", f"${s['total_pl']:,.2f}", kpi_class(s['total_pl']))
    perf_html += _card("Win Rate", f"{s['wr']:.1f}%" if s['wr'] is not None else "—", "neutral")
    perf_html += _card("Trades (open / total)", f"{s['n_open']} / {s['n_trades']}", "accent")
    if s['current_shares'] > 0:
        perf_html += _card("Shares Held", f"{s['current_shares']} @ ${s['avg_cost']:,.2f}", "neutral")
        if s['breakeven'] is not None:
            perf_html += _card("Breakeven (w/ prem)", f"${s['breakeven']:,.2f}", "accent")
    perf_html += "</div>"
    st.markdown(perf_html, unsafe_allow_html=True)
else:
    st.caption("Add trades to see per-ticker performance.")

st.divider()

# =====================================================================
#  MONTHLY GAINS — chart first (brand-styled), table tucked away
# =====================================================================
st.subheader("Monthly Gains")

years_to_show = [date.today().year]
months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

if not df.empty:
    df['Open Date'] = pd.to_datetime(df['Open Date'], errors='coerce')
    trade_years = df['Open Date'].dt.year.dropna().unique().tolist()
    for y in trade_years:
        if int(y) not in years_to_show:
            years_to_show.append(int(y))

years_to_show.sort()
gains_data = pd.DataFrame(0.0, index=years_to_show, columns=months)

if not df.empty:
    realized_df = df[df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])].copy()
    if not realized_df.empty:
        realized_df['Year'] = realized_df['Open Date'].dt.year
        realized_df['Month'] = realized_df['Open Date'].dt.strftime('%b')
        for index, row in realized_df.iterrows():
            if pd.notna(row['Year']) and pd.notna(row['Month']):
                gains_data.at[row['Year'], row['Month']] += float(row['P&L'])

gains_data['Yearly Total'] = gains_data.sum(axis=1)

y1, y2 = st.columns([1, 2])
with y1:
    selected_year = st.selectbox("Year", options=years_to_show, index=len(years_to_show) - 1)
with y2:
    st.write("")
    st.write("")
    year_total = gains_data.loc[selected_year, 'Yearly Total']
    tot_cls = "pos" if year_total >= 0 else "neg"
    st.markdown(f"<span style='color:#8CA3B8; font-size:.8rem; text-transform:uppercase; letter-spacing:.07em;'>"
                f"{selected_year} Total&nbsp;&nbsp;</span>"
                f"<span class='{tot_cls}' style='font-size:1.3rem; font-weight:700;'>${year_total:,.2f}</span>",
                unsafe_allow_html=True)

chart_series = gains_data.loc[selected_year].drop('Yearly Total')
chart_df = pd.DataFrame({'Month': chart_series.index, 'Gain': chart_series.values.astype(float)})

base = alt.Chart(chart_df).encode(
    x=alt.X('Month:N', sort=months,
            axis=alt.Axis(title=None, labelAngle=0, labelColor='#8CA3B8', labelFontSize=11)),
    y=alt.Y('Gain:Q',
            axis=alt.Axis(title=None, format='$,.0f', labelColor='#8CA3B8',
                          gridColor='rgba(169,205,231,0.08)', labelFontSize=11)),
    tooltip=[alt.Tooltip('Month:N'), alt.Tooltip('Gain:Q', format='$,.2f', title='Gain')]
)

bars = base.mark_bar(cornerRadiusEnd=5).encode(
    color=alt.condition('datum.Gain >= 0', alt.value('#A9CDE7'), alt.value('#F87171'))
)
labels_pos = base.transform_filter('datum.Gain > 0').mark_text(
    dy=-9, color='#E8EDF2', fontSize=10.5, fontWeight='bold'
).encode(text=alt.Text('Gain:Q', format='$,.0f'))
labels_neg = base.transform_filter('datum.Gain < 0').mark_text(
    dy=12, color='#F87171', fontSize=10.5, fontWeight='bold'
).encode(text=alt.Text('Gain:Q', format='$,.0f'))

chart = alt.layer(bars, labels_pos, labels_neg).properties(height=330).configure_view(
    strokeWidth=0
).configure_axis(
    domainOpacity=0, tickOpacity=0
)

st.altair_chart(chart, use_container_width=True)

with st.expander("📋 Monthly table (all years)"):
    st.dataframe(gains_data.style.format("${:,.2f}"), use_container_width=True)

st.divider()

# =====================================================================
#  CUMULATIVE EQUITY CURVE — running realized P&L over time
# =====================================================================
st.subheader("Cumulative Equity Curve")

if not df.empty:
    eq = df[df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])].copy()
    if not eq.empty:
        # Realize P&L on the close date (fall back to expiration if missing)
        eq['Realized Date'] = eq['Close Date'].fillna(eq['Expiration Date'])
        eq = eq.dropna(subset=['Realized Date'])
        eq['P&L'] = pd.to_numeric(eq['P&L'], errors='coerce').fillna(0.0)
        eq = eq.sort_values('Realized Date')
        eq['Cumulative'] = eq['P&L'].cumsum()
        curve = eq.groupby('Realized Date', as_index=False)['Cumulative'].last()

        eq_base = alt.Chart(curve).encode(
            x=alt.X('Realized Date:T',
                    axis=alt.Axis(title=None, labelColor='#8CA3B8', format='%b %d',
                                  labelFontSize=11, gridOpacity=0)),
            y=alt.Y('Cumulative:Q',
                    axis=alt.Axis(title=None, format='$,.0f', labelColor='#8CA3B8',
                                  gridColor='rgba(169,205,231,0.08)', labelFontSize=11)),
            tooltip=[alt.Tooltip('Realized Date:T', title='Date'),
                     alt.Tooltip('Cumulative:Q', format='$,.2f', title='Cumulative P&L')]
        )
        eq_area = eq_base.mark_area(
            line={'color': '#A9CDE7'},
            color=alt.Gradient(
                gradient='linear',
                stops=[alt.GradientStop(color='rgba(169,205,231,0.28)', offset=0),
                       alt.GradientStop(color='rgba(169,205,231,0.02)', offset=1)],
                x1=1, x2=1, y1=1, y2=0
            )
        )
        eq_line = eq_base.mark_line(color='#A9CDE7', strokeWidth=2)
        eq_pts = eq_base.mark_point(color='#A9CDE7', filled=True, size=30)
        eq_chart = (eq_area + eq_line + eq_pts).properties(height=300).configure_view(
            strokeWidth=0
        ).configure_axis(domainOpacity=0, tickOpacity=0)
        st.altair_chart(eq_chart, use_container_width=True)
    else:
        st.caption("No realized trades yet — your equity curve appears once trades close.")
else:
    st.caption("No trades yet — your equity curve will appear here.")
