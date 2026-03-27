import streamlit as st
import pandas as pd
from datetime import date
import os
import yfinance as yf # NEW: Yahoo Finance for real-time prices

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Wheel Strategy Log", layout="centered")

FILE_NAME = "trades_data.csv" 

# --- INITIALIZE DATA & LOAD FROM FILE ---
if 'trades' not in st.session_state:
    if os.path.exists(FILE_NAME): 
        loaded_df = pd.read_csv(FILE_NAME)
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

# --- NEW: AUTO-UPDATE EXPIRATIONS ---
# This block checks all "Open" trades. If today is past the expiration, it fetches the stock price
# and automatically marks it as Expired or Assigned based on the strategy rules.
changed = False
today = pd.to_datetime(date.today())

for idx, row in st.session_state.trades.iterrows():
    if row['Status'] == 'Open' and pd.to_datetime(row['Expiration Date']) <= today:
        try:
            # Fetch current stock price
            ticker_data = yf.Ticker(row['Ticker'])
            current_price = ticker_data.history(period="1d")['Close'].iloc[-1]
            
            # Cash-Secured Put Logic
            if row['Strategy'] == 'Cash-Secured Put':
                if current_price < row['Strike Price']:
                    st.session_state.trades.at[idx, 'Status'] = 'Assigned'
                else:
                    st.session_state.trades.at[idx, 'Status'] = 'Expired'
            
            # Covered Call Logic
            elif row['Strategy'] == 'Covered Call':
                if current_price > row['Strike Price']:
                    st.session_state.trades.at[idx, 'Status'] = 'Assigned' # Shares called away
                else:
                    st.session_state.trades.at[idx, 'Status'] = 'Expired'
            
            # Either way, if it reached expiration, you keep the full premium
            st.session_state.trades.at[idx, 'P&L'] = row['Premium Collected']
            st.session_state.trades.at[idx, 'Close Date'] = row['Expiration Date']
            changed = True
            
        except Exception as e:
            pass # If internet fails or ticker is wrong, skip and try again next time

if changed:
    st.session_state.trades.to_csv(FILE_NAME, index=False)

df = st.session_state.trades

# --- NEW: CALCULATE STOCK HOLDINGS & REALIZED P&L ---
holdings = []
realized_stock_pl = 0.0

if not df.empty:
    tickers = df['Ticker'].unique()
    for t in tickers:
        t_df = df[df['Ticker'] == t]
        
        # Calculate shares bought from Assigned Puts
        csp_assigned = t_df[(t_df['Strategy'] == 'Cash-Secured Put') & (t_df['Status'] == 'Assigned')]
        shares_bought = csp_assigned['# Contracts'].sum() * 100
        total_cost = (csp_assigned['Strike Price'] * csp_assigned['# Contracts'] * 100).sum()
        avg_cost = total_cost / shares_bought if shares_bought > 0 else 0
        
        # Calculate shares sold from Assigned Covered Calls
        cc_assigned = t_df[(t_df['Strategy'] == 'Covered Call') & (t_df['Status'] == 'Assigned')]
        shares_sold = cc_assigned['# Contracts'].sum() * 100
        total_sale = (cc_assigned['Strike Price'] * cc_assigned['# Contracts'] * 100).sum()
        
        current_shares = shares_bought - shares_sold
        
        # 3. Add realized stock P&L (Capital gains without premiums)
        if shares_sold > 0:
            realized_stock_pl += total_sale - (shares_sold * avg_cost)
            
        # 2. Build the Assigned Table if we still hold shares
        if current_shares > 0:
            try:
                curr_price = yf.Ticker(t).history(period="1d")['Close'].iloc[-1]
            except:
                curr_price = avg_cost # Fallback if offline
                
            stock_pl_dollars = (curr_price - avg_cost) * current_shares
            stock_pl_pct = (curr_price - avg_cost) / avg_cost if avg_cost > 0 else 0
            
            # Sum all premiums collected for this wheel
            total_premiums = t_df['Premium Collected'].sum() 
            
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
        st.session_state.show_form = True
        st.rerun()

# --- NEW TRADE / EDIT FORM ---
if st.session_state.show_form:
    st.markdown("### Log or Edit Trade")
    
    with st.form("new_trade_form"):
        c1, c2 = st.columns(2)
        with c1:
            ticker = st.text_input("Ticker (e.g., AAPL)")
            strike = st.number_input("Strike Price", min_value=0.0, format="%.2f")
            premium = st.number_input("Premium Collected ($)", min_value=0.0, format="%.2f")
            open_date = st.date_input("Open Date", value=date.today()) 
            status = st.selectbox("Status", ["Open", "Closed", "Assigned", "Rolled", "Expired"]) 
            pnl = st.number_input("P&L ($)", format="%.2f")
            
        with c2:
            strategy = st.selectbox("Strategy", ["Cash-Secured Put", "Covered Call"])
            contracts = st.number_input("# Contracts", min_value=1, step=1)
            cost_basis = st.number_input("Cost Basis ($)", min_value=0.0, format="%.2f")
            exp_date = st.date_input("Expiration Date", value=date.today()) 
            close_date = st.date_input("Close Date", value=None)
            
        notes = st.text_area("Notes", placeholder="Trade notes...")
        
        submit_col1, submit_col2 = st.columns(2)
        with submit_col1:
            cancel = st.form_submit_button("Cancel", use_container_width=True)
        with submit_col2:
            submit = st.form_submit_button("Save Trade", type="primary", use_container_width=True)

        if cancel:
            st.session_state.show_form = False
            st.rerun()
            
        if submit:
            if ticker != "": 
                open_dt = pd.to_datetime(open_date)
                ticker_upper = ticker.upper()
                
                mask = (
                    (st.session_state.trades['Ticker'] == ticker_upper) &
                    (st.session_state.trades['Strategy'] == strategy) &
                    (st.session_state.trades['Strike Price'] == strike) &
                    (st.session_state.trades['Open Date'] == open_dt)
                )
                
                if mask.any() and not st.session_state.trades.empty:
                    idx = st.session_state.trades[mask].index[0]
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
                
                st.session_state.trades.to_csv(FILE_NAME, index=False)
                st.session_state.show_form = False
                st.rerun()
            else:
                st.error("Please enter a Ticker symbol.")

# --- CALCULATE METRICS ---
total_premium = df['Premium Collected'].sum() if not df.empty else 0.0
options_pl = df['P&L'].sum() if not df.empty else 0.0
# Add the realized stock capital gains from assigned CCs to the Dashboard P&L
grand_total_pl = options_pl + realized_stock_pl 

open_positions = len(df[df['Status'] == 'Open']) if not df.empty else 0

win_rate = 0.0
if not df.empty:
    closed_trades = df[df['Status'].isin(['Closed', 'Expired', 'Assigned', 'Rolled'])]
    if len(closed_trades) > 0:
        wins = len(closed_trades[closed_trades['P&L'] > 0])
        win_rate = (wins / len(closed_trades)) * 100

# --- METRICS CARDS ---
m1, m2 = st.columns(2)
with m1:
    st.metric(label="💲 TOTAL PREMIUM", value=f"${total_premium:,.2f}")
    st.metric(label="📊 OPEN OPTIONS", value=open_positions)
with m2:
    st.metric(label="📈 TOTAL P&L (Options + Shares)", value=f"${grand_total_pl:,.2f}", help=f"Options P&L: ${options_pl:.2f} | Realized Stock P&L: ${realized_stock_pl:.2f}")
    st.metric(label="📉 WIN RATE", value=f"{win_rate:.1f}%" if not df.empty and len(closed_trades) > 0 else "—")

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

# --- FILTER TABS ---
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

def display_dataframe(filtered_df):
    if filtered_df.empty:
        st.info("No trades found.")
    else:
        display_df = filtered_df.copy()
        display_df['Open Date'] = display_df['Open Date'].dt.strftime('%Y-%m-%d')
        st.dataframe(
            display_df[['Open Date', 'Ticker', 'Strategy', 'Premium Collected', 'P&L', 'Status']], 
            use_container_width=True,
            hide_index=True
        )

with tabs[0]: display_dataframe(df)
with tabs[1]: display_dataframe(df[df['Status'] == 'Open'])
with tabs[2]: display_dataframe(df[df['Status'] == 'Assigned'])
with tabs[3]: display_dataframe(df[df['Status'] == 'Expired'])
with tabs[4]: display_dataframe(df[df['Status'] == 'Closed'])
with tabs[5]: display_dataframe(df[df['Status'] == 'Rolled'])

st.divider()

# --- MONTHLY GAINS BY YEAR TABLE ---
st.subheader("Monthly Gains (by Year)")

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