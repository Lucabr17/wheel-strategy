import streamlit as st
import pandas as pd
from datetime import date
import numpy as np
import yfinance as yf 
import gspread # NEW: Google Sheets Library
import json # NEW: To read the secrets

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Wheel Strategy Log", layout="wide") 

# --- NEW: GOOGLE SHEETS SETUP ---
# Connect to Google Sheets using the Streamlit Secrets
@st.cache_resource
def init_gsheets():
    creds_dict = json.loads(st.secrets["gcp_service_account"])
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open("Wheel Trades Database")
    return sh.sheet1

worksheet = init_gsheets()

# Function to save data back to Google Sheets
def save_to_cloud(df):
    df_save = df.copy()
    # Google Sheets doesn't like NaN/NaT, so we replace them with empty strings
    df_save = df_save.fillna("")
    for col in ['Open Date', 'Expiration Date', 'Close Date']:
        df_save[col] = df_save[col].astype(str).replace('NaT', '')
    
    worksheet.clear()
    # Update the sheet with headers + data
    worksheet.update(values=[df_save.columns.values.tolist()] + df_save.values.tolist(), range_name='A1')

# --- INITIALIZE DATA FROM CLOUD ---
if 'trades' not in st.session_state:
    records = worksheet.get_all_records()
    if records: 
        loaded_df = pd.DataFrame(records)
        # Convert empty strings back to Pandas empty values
        loaded_df.replace("", np.nan, inplace=True)
        for col in ['Open Date', 'Expiration Date', 'Close Date']:
            loaded_df[col] = pd.to_datetime(loaded_df[col])
        st.session_state.trades = loaded_df
    else: 
        st.session_state.trades = pd.DataFrame(columns=[
            'Open Date', 'Ticker', 'Strategy', 'Strike Price', '# Contracts', 
            'Premium Collected', 'Cost Basis', 'Expiration Date', 'Status', 
            'Close Date', 'P&L', 'Notes'
        ])

if 'show_form' not in st.session_state:
    st.session_state.show_form = False

if 'edit_idx' not in st.session_state:
    st.session_state.edit_idx = None

# --- AUTO-UPDATE EXPIRATIONS ---
changed = False
today = pd.to_datetime(date.today())

for idx, row in st.session_state.trades.iterrows():
    if row['Status'] == 'Open' and pd.to_datetime(row['Expiration Date']) <= today:
        try:
            ticker_data = yf.Ticker(row['Ticker'])
            current_price = ticker_data.history(period="1d")['Close'].iloc[-1]
            
            if row['Strategy'] == 'Cash-Secured Put':
                if current_price < row['Strike Price']:
                    st.session_state.trades.at[idx, 'Status'] = 'Assigned'
                else:
                    st.session_state.trades.at[idx, 'Status'] = 'Expired'
            
            elif row['Strategy'] == 'Covered Call':
                if current_price > row['Strike Price']:
                    st.session_state.trades.at[idx, 'Status'] = 'Assigned' 
                else:
                    st.session_state.trades.at[idx, 'Status'] = 'Expired'
            
            st.session_state.trades.at[idx, 'P&L'] = row['Premium Collected']
            st.session_state.trades.at[idx, 'Close Date'] = row['Expiration Date']
            changed = True
            
        except Exception as e:
            pass 

if changed:
    save_to_cloud(st.session_state.trades) # NEW: Saves to Cloud

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
            try:
                curr_price = yf.Ticker(t).history(period="1d")['Close'].iloc[-1]
            except:
                curr_price = avg_cost 
                
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

# --- TOP HEADER ---
col1, col2 = st.columns([3, 1])
with col1:
    st.title("Wheel Strategy Log")
    st.caption("Track your CSP & CC trades")
with col2:
    st.write("") 
    if st.button("➕ New Trade", use_container_width=True):
        st.session_state.edit_idx = None
        st.session_state.show_form = True
        st.rerun()

# --- NEW TRADE / EDIT FORM ---
if st.session_state.show_form:
    st.markdown("### Log or Edit Trade")
    
    def_ticker, def_strike, def_premium, def_open_date = "", 0.0, 0.0, date.today()
    def_status, def_pnl, def_strategy = "Open", 0.0, "Cash-Secured Put"
    def_contracts, def_cost, def_exp_date = 1, 0.0, date.today()
    def_close_date, def_notes = None, ""
    
    if st.session_state.edit_idx is not None:
        edit_row = st.session_state.trades.loc[st.session_state.edit_idx]
        def_ticker = edit_row['Ticker']
        def_strike = float(edit_row['Strike Price'])
        def_premium = float(edit_row['Premium Collected'])
        def_open_date = pd.to_datetime(edit_row['Open Date']).date()
        def_status = edit_row['Status']
        def_pnl = float(edit_row['P&L']) if pd.notna(edit_row['P&L']) else 0.0
        def_strategy = edit_row['Strategy']
        def_contracts = int(edit_row['# Contracts'])
        def_cost = float(edit_row['Cost Basis']) if pd.notna(edit_row['Cost Basis']) else 0.0
        def_exp_date = pd.to_datetime(edit_row['Expiration Date']).date()
        if pd.notna(edit_row['Close Date']):
            def_close_date = pd.to_datetime(edit_row['Close Date']).date()
        def_notes = edit_row['Notes'] if pd.notna(edit_row['Notes']) else ""

    with st.form("new_trade_form"):
        c1, c2 = st.columns(2)
        
        status_options = ["Open", "Closed", "Assigned", "Rolled", "Expired"]
        strategy_options = ["Cash-Secured Put", "Covered Call"]
        
        with c1:
            ticker = st.text_input("Ticker (e.g., AAPL)", value=def_ticker)
            strike = st.number_input("Strike Price", min_value=0.0, format="%.2f", value=def_strike)
            premium = st.number_input("Premium Collected ($)", min_value=0.0, format="%.2f", value=def_premium)
            open_date = st.date_input("Open Date", value=def_open_date) 
            status = st.selectbox("Status", status_options, index=status_options.index(def_status) if def_status in status_options else 0) 
            pnl = st.number_input("P&L ($)", format="%.2f", value=def_pnl)
            
        with c2:
            strategy = st.selectbox("Strategy", strategy_options, index=strategy_options.index(def_strategy) if def_strategy in strategy_options else 0)
            contracts = st.number_input("# Contracts", min_value=1, step=1, value=def_contracts)
            cost_basis = st.number_input("Cost Basis ($)", min_value=0.0, format="%.2f", value=def_cost)
            exp_date = st.date_input("Expiration Date", value=def_exp_date) 
            close_date = st.date_input("Close Date", value=def_close_date)
            
        notes = st.text_area("Notes", value=def_notes, placeholder="Trade notes...")
        
        submit_col1, submit_col2 = st.columns(2)
        with submit_col1:
            cancel = st.form_submit_button("Cancel", use_container_width=True)
        with submit_col2:
            submit = st.form_submit_button("Save Trade", type="primary", use_container_width=True)

        if cancel:
            st.session_state.edit_idx = None
            st.session_state.show_form = False
            st.rerun()
            
        if submit:
            if ticker != "": 
                open_dt = pd.to_datetime(open_date)
                ticker_upper = ticker.upper()
                
                if st.session_state.edit_idx is not None:
                    idx = st.session_state.edit_idx
                    st.session_state.trades.at[idx, 'Open Date'] = open_dt
                    st.session_state.trades.at[idx, 'Ticker'] = ticker_upper
                    st.session_state.trades.at[idx, 'Strategy'] = strategy
                    st.session_state.trades.at[idx, 'Strike Price'] = strike
                    st.session_state.trades.at[idx, '# Contracts'] = contracts
                    st.session_state.trades.at[idx, 'Premium Collected'] = premium
                    st.session_state.trades.at[idx, 'Cost Basis'] = cost_basis
                    st.session_state.trades.at[idx, 'Expiration Date'] = pd.to_datetime(exp_date)
                    st.session_state.trades.at[idx, 'Status'] = status
                    st.session_state.trades.at[idx, 'Close Date'] = pd.to_datetime(close_date) if close_date else pd.NaT
                    st.session_state.trades.at[idx, 'P&L'] = pnl
                    st.session_state.trades.at[idx, 'Notes'] = notes
                else:
                    new_data = {
                        'Open Date': open_dt,
                        'Ticker': ticker_upper,
                        'Strategy': strategy,
                        'Strike Price': strike,
                        '# Contracts': contracts,
                        'Premium Collected': premium,
                        'Cost Basis': cost_basis,
                        'Expiration Date': pd.to_datetime(exp_date),
                        'Status': status,
                        'Close Date': pd.to_datetime(close_date) if close_date else pd.NaT,
                        'P&L': pnl,
                        'Notes': notes
                    }
                    st.session_state.trades = pd.concat([st.session_state.trades, pd.DataFrame([new_data])], ignore_index=True)
                
                save_to_cloud(st.session_state.trades) # NEW: Saves to Cloud
                st.session_state.edit_idx = None
                st.session_state.show_form = False
                st.rerun()
            else:
                st.error("Please enter a Ticker symbol.")

# --- CALCULATE NET TOTAL PREMIUM & WIN RATE ---
total_premium = 0.0
options_pl = 0.0
win_rate = 0.0

if not df.empty:
    open_prem = df[df['Status'] == 'Open']['Premium Collected'].sum()
    closed_pl = df[df['Status'] != 'Open']['P&L'].sum()
    total_premium = open_prem + closed_pl
    options_pl = closed_pl
    
    closed_trades = df[df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])]
    if len(closed_trades) > 0:
        wins = closed_trades[(closed_trades['P&L'] > 0) & (closed_trades['Status'] != 'Assigned')]
        win_rate = (len(wins) / len(closed_trades)) * 100

grand_total_pl = options_pl + realized_stock_pl 
open_positions = len(df[df['Status'] == 'Open']) if not df.empty else 0

# --- CALCULATE AVERAGE ANNUAL ROC ---
roc_list = []
if not df.empty:
    for _, row in df.iterrows():
        cost_basis = row['Cost Basis']
        if pd.notna(cost_basis) and cost_basis > 0:
            start_date = pd.to_datetime(row['Open Date'])
            
            if row['Status'] == 'Open':
                end_date = pd.to_datetime(today)
                net_profit = row['Premium Collected'] 
            else:
                end_date = pd.to_datetime(row['Close Date']) if pd.notna(row['Close Date']) else pd.to_datetime(row['Expiration Date'])
                net_profit = row['P&L']
                
            days_held = (end_date - start_date).days
            if days_held <= 0: days_held = 1 
            
            trade_roc = net_profit / cost_basis
            annual_roc = trade_roc * (365 / days_held)
            roc_list.append(annual_roc)

avg_annual_roc = sum(roc_list) / len(roc_list) if roc_list else 0.0

# --- METRICS CARDS ---
def get_color(val):
    return "#28a745" if val >= 0 else "#dc3545"

m1, m2, m3 = st.columns(3)
with m1:
    st.markdown(f"**💲 TOTAL NET PREMIUM**<br><span style='color:{get_color(total_premium)}; font-size: 2.2rem; font-weight: bold;'>${total_premium:,.2f}</span>", unsafe_allow_html=True)
    st.write("") 
    st.metric(label="📊 OPEN OPTIONS", value=open_positions)
with m2:
    st.markdown(f"**📈 TOTAL P&L (Options + Shares)**<br><span style='color:{get_color(grand_total_pl)}; font-size: 2.2rem; font-weight: bold;'>${grand_total_pl:,.2f}</span>", unsafe_allow_html=True)
    st.write("") 
    st.metric(label="📉 WIN RATE", value=f"{win_rate:.1f}%" if not df.empty and len(closed_trades) > 0 else "—", help="Assignments and trades with P&L <= $0 count as losses.")
with m3:
    st.metric(label="🚀 AVG ANNUAL ROC", value=f"{avg_annual_roc * 100:.1f}%" if roc_list else "—", help="Average return on capital, annualized based on days held.")
    st.write("") 

st.divider()

# --- CURRENT HOLDINGS (ASSIGNED) TABLE ---
if not holdings_df.empty:
    st.subheader("Current Stock Holdings (Assigned)")
    st.dataframe(
        holdings_df.style.format({
            'Assigned Price': '${:.2f}',
            'Current Price': '${:.2f}',
            '$ P&L': '${:.2f}',
            '% P&L': '{:.2%}',
            'Total Premiums': '${:.2f}',
            '% P&L (w/ Prem)': '{:.2%}'
        }),
        use_container_width=True,
        hide_index=True
    )
    st.divider()

# --- CUSTOM INTERACTIVE TRADES TABLE WITH EDIT & DELETE ---
count_open = len(df[df['Status'] == 'Open']) if not df.empty else 0
count_assigned = len(df[df['Status'] == 'Assigned']) if not df.empty else 0
count_expired = len(df[df['Status'] == 'Expired']) if not df.empty else 0
count_closed = len(df[df['Status'] == 'Closed']) if not df.empty else 0
count_rolled = len(df[df['Status'] == 'Rolled']) if not df.empty else 0

tabs = st.tabs([
    f"All ({len(df)})", 
    f"Open ({count_open})", 
    f"Assigned ({count_assigned})", 
    f"Expired ({count_expired})", 
    f"Closed ({count_closed})",
    f"Rolled ({count_rolled})"
])

def display_custom_table(filtered_df, tab_key=""):
    if filtered_df.empty:
        st.info("No trades found in this category.")
        return

    open_tickers = filtered_df[filtered_df['Status'] == 'Open']['Ticker'].unique()
    current_prices = {}
    for t in open_tickers:
        try:
            current_prices[t] = yf.Ticker(t).history(period="1d")['Close'].iloc[-1]
        except:
            current_prices[t] = 0.0

    cols = st.columns([1.2, 1, 1.3, 1.2, 1.2, 1.2, 1.2, 1.5, 1, 1.2, 1.2])
    headers = ["Open Date", "Ticker", "Strategy", "Strike", "Cur. Price", "Premium", "P&L", "Exp/Close Date", "Days Left", "Status", "Actions"]
    for col, header in zip(cols, headers):
        col.markdown(f"**<span style='font-size: 0.85rem;'>{header}</span>**", unsafe_allow_html=True)
    
    st.divider()

    for i, row in filtered_df.iterrows():
        cols = st.columns([1.2, 1, 1.3, 1.2, 1.2, 1.2, 1.2, 1.5, 1, 1.2, 1.2])
        
        open_d = row['Open Date'].strftime('%Y-%m-%d')
        ticker = row['Ticker']
        strategy = "CSP" if row['Strategy'] == "Cash-Secured Put" else "CC"
        strike = f"${row['Strike Price']:.2f}"
        
        if row['Status'] == 'Open':
            curr_price = f"${current_prices.get(ticker, 0.0):.2f}"
        else:
            curr_price = "—"
            
        premium = f"${row['Premium Collected']:.2f}"
        pnl = f"${row['P&L']:.2f}" if pd.notna(row['P&L']) else "$0.00"
        
        if row['Status'] == 'Open':
            exp_close = row['Expiration Date'].strftime('%Y-%m-%d') if pd.notna(row['Expiration Date']) else "-"
            days_left = (row['Expiration Date'] - today).days if pd.notna(row['Expiration Date']) else 0
            days_str = str(days_left) if days_left >= 0 else "Exp"
        else:
            exp_close = row['Close Date'].strftime('%Y-%m-%d') if pd.notna(row['Close Date']) else row['Expiration Date'].strftime('%Y-%m-%d')
            days_str = "-"
            
        status = row['Status']

        cols[0].write(open_d)
        cols[1].write(ticker)
        cols[2].write(strategy)
        cols[3].write(strike)
        cols[4].write(curr_price)
        cols[5].write(premium)
        cols[6].write(pnl)
        cols[7].write(exp_close)
        cols[8].write(days_str)
        cols[9].write(status)
        
        with cols[10]:
            btn_col1, btn_col2 = st.columns(2)
            if btn_col1.button("⚙️", key=f"edit_{tab_key}_{i}", help="Edit Trade"):
                st.session_state.edit_idx = i
                st.session_state.show_form = True
                st.rerun()
            if btn_col2.button("🗑️", key=f"del_{tab_key}_{i}", help="Delete Trade"):
                st.session_state.trades = st.session_state.trades.drop(i)
                save_to_cloud(st.session_state.trades) # NEW: Saves to Cloud
                st.rerun()
        
        st.markdown("<hr style='margin: 0px; padding: 0px;'>", unsafe_allow_html=True)

with tabs[0]: display_custom_table(df, "all")
with tabs[1]: display_custom_table(df[df['Status'] == 'Open'], "open")
with tabs[2]: display_custom_table(df[df['Status'] == 'Assigned'], "assigned")
with tabs[3]: display_custom_table(df[df['Status'] == 'Expired'], "expired")
with tabs[4]: display_custom_table(df[df['Status'] == 'Closed'], "closed")
with tabs[5]: display_custom_table(df[df['Status'] == 'Rolled'], "rolled")

st.divider()

# --- MONTHLY GAINS BY YEAR TABLE & CHART ---
st.subheader("Monthly Gains")

years_to_show = [2026]
months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

if not df.empty:
    trade_years = df['Open Date'].dt.year.dropna().unique().tolist()
    for y in trade_years:
        if y not in years_to_show:
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
                gains_data.at[row['Year'], row['Month']] += row['P&L']

gains_data['Yearly Total'] = gains_data.sum(axis=1)

st.dataframe(gains_data.style.format("${:,.2f}"), use_container_width=True)

st.markdown("### Visualization")
if not df.empty and len(years_to_show) > 0:
    selected_year = st.selectbox("Select Year", options=years_to_show, index=len(years_to_show)-1)
    chart_data = gains_data.loc[selected_year].drop('Yearly Total')
    st.bar_chart(chart_data)
