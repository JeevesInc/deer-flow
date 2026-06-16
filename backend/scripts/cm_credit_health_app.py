#!/usr/bin/env python3
"""cm_credit_health_app.py
===========================
Streamlit credit health dashboard for Jeeves Capital Markets.
Served at localhost:8100.

Sections:
  1. Borrowing Base snapshot (US Bridge + MX SOFOM)
  2. DQ rate trend — 24 months (Redshift)
  3. Roll rate matrix — last month-end (Redshift)
  4. Portfolio breakdown — by DPD bucket, country, product
  5. Covenant tracker

Run:
  streamlit run cm_credit_health_app.py --server.port 8100 --server.headless true
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
BACKEND_DIR = Path(__file__).resolve().parent.parent
STATE_FILE  = BACKEND_DIR / ".deer-flow" / "_cap_markets_state.json"
CICO_FILE   = BACKEND_DIR / ".deer-flow" / "_cico_state.json"

# Load Redshift/API creds from .env so the dashboard is self-sufficient. Under
# the old gateway cron it inherited the gateway's already-loaded environment;
# now that it runs as a standalone supervised service (start.sh), redshift_util
# (which reads os.environ['REDSHIFT_*'] directly) needs these populated here.
try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_DIR / ".env")
except Exception:
    pass

SKILLS_DIR  = Path(os.environ.get("SKILLS_PATH", str(BACKEND_DIR.parent / "deer-flow" / "skills")))
sys.path.insert(0, str(SKILLS_DIR / "custom" / "jeeves-borrowing-base"))
sys.path.insert(0, str(SKILLS_DIR / "custom" / "jeeves-redshift"))

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Jeeves · Capital Markets",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  .block-container { padding-top: 1rem; padding-bottom: 1rem; }
  .metric-card { background: #1e1e2e; border-radius: 8px; padding: 12px 16px; margin: 4px 0; }
  .red-val { color: #f38ba8; font-weight: 700; }
  .yellow-val { color: #f9e2af; font-weight: 700; }
  .green-val { color: #a6e3a1; font-weight: 700; }
  h3 { margin-top: 0.5rem !important; }
  div[data-testid="metric-container"] { background: #1e1e2e; border-radius: 8px; padding: 8px 12px; }
</style>
""", unsafe_allow_html=True)


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


@st.cache_data(ttl=300)
def load_cico():
    if CICO_FILE.exists():
        return json.loads(CICO_FILE.read_text(encoding="utf-8"))
    return {}


@st.cache_data(ttl=600, show_spinner="Querying Redshift…")
def load_dq_history() -> pd.DataFrame:
    """24-month monthly DQ30+ rate from loc_tape."""
    sql = """
    with dq30 as (
        select
            date_trunc('month', dt) as month,
            avg(daily_balance) as avg_dq_balance
        from (
            select dt, sum(balance_usd) as daily_balance
            from capital_markets_dm.loc_tape
            where charge_off_flag is false
              and is_in_repayment is false
              and dt >= dateadd(month, -24, date_trunc('month', current_date))
              and dq_bucket = 2
            group by dt
        ) d
        group by 1
    ),
    adb as (
        select
            date_trunc('month', dt) as month,
            avg(daily_balance) as avg_total_balance
        from (
            select dt, sum(balance_usd) as daily_balance
            from capital_markets_dm.loc_tape
            where charge_off_flag is false
              and is_in_repayment is false
              and dt >= dateadd(month, -24, date_trunc('month', current_date))
            group by dt
        ) d
        group by 1
    )
    select
        a.month,
        a.avg_total_balance as total_adb_usd,
        coalesce(d.avg_dq_balance, 0) as dq30_adb_usd,
        case when a.avg_total_balance > 0
             then coalesce(d.avg_dq_balance, 0) / a.avg_total_balance * 100
             else 0 end as dq30_rate_pct
    from adb a
    left join dq30 d on a.month = d.month
    order by 1
    """
    try:
        from redshift_util import connect
        conn = connect()
        df = pd.read_sql(sql, conn)
        conn.close()
        df["month"] = pd.to_datetime(df["month"])
        return df
    except Exception as e:
        st.warning(f"Redshift unavailable: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner="Loading roll rates...")
def load_roll_rate() -> pd.DataFrame:
    """Last two month-ends roll rate matrix — count-weighted, live accounts only.

    Excludes:
    - charge_off_flag=true at BOM (already charged off, trivially stay in CO bucket)
    - is_in_repayment=true (not at risk of rolling)
    Percentages = % of BOM *accounts* (not balance) — consistent with NCO and BB.
    """
    sql = """
    with d as (
        select dt, company_id, balance_usd,
               coalesce(dq_bucket_daily, 0) as bucket
        from capital_markets_dm.loc_tape
        where dt = last_day(dt)
          and charge_off_flag is false
          and is_in_repayment is false
          and dt >= dateadd(month, -2, date_trunc('month', current_date))
    ),
    with_lead as (
        select dt, company_id, balance_usd as bom_balance,
               bucket as bom_bucket,
               lead(bucket) over (partition by company_id order by dt) as eom_bucket
        from d
    )
    select
        dt as bom_dt, bom_bucket, eom_bucket,
        count(*) as accounts,
        sum(bom_balance) as total_balance_usd
    from with_lead
    where eom_bucket is not null
    group by 1, 2, 3
    order by 1, 2, 3
    """
    try:
        from redshift_util import connect
        conn = connect()
        df = pd.read_sql(sql, conn)
        conn.close()
        df["bom_dt"] = pd.to_datetime(df["bom_dt"])
        return df
    except Exception as e:
        st.warning(f"Redshift unavailable for roll rates: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner="Loading NCO history…")
def load_nco_history() -> pd.DataFrame:
    """Monthly net charge-off rate for past 18 months."""
    # Flow NCO: new charge-offs in month / avg portfolio balance that month
    # charge_off_dt identifies when an account was first written off
    sql = """
    with new_cos as (
        select
            date_trunc('month', charge_off_dt::date) as month,
            sum(balance_usd) as co_flow_usd
        from capital_markets_dm.loc_tape
        where charge_off_flag is true
          and charge_off_dt is not null
          and dt = last_day(charge_off_dt::date)
          and charge_off_dt >= dateadd(month, -18, date_trunc('month', current_date))
        group by 1
    ),
    adb as (
        select
            date_trunc('month', dt) as month,
            avg(bal) as avg_balance
        from (
            select dt, sum(balance_usd) as bal
            from capital_markets_dm.loc_tape
            where charge_off_flag is false
              and is_in_repayment is false
              and dt >= dateadd(month, -18, date_trunc('month', current_date))
            group by dt
        ) d
        group by 1
    )
    select
        a.month,
        coalesce(c.co_flow_usd, 0) as co_flow_usd,
        a.avg_balance,
        case when a.avg_balance > 0
             then coalesce(c.co_flow_usd, 0) / a.avg_balance * 100
             else 0 end as nco_rate_pct
    from adb a
    left join new_cos c on a.month = c.month
    order by 1
    """
    try:
        from redshift_util import connect
        conn = connect()
        df = pd.read_sql(sql, conn)
        conn.close()
        df["month"] = pd.to_datetime(df["month"])
        return df
    except Exception as e:
        return pd.DataFrame()


# ── Helpers ───────────────────────────────────────────────────────────────────

BUCKET_LABELS = {0: "Current", 1: "1-30 DPD", 2: "31-60 DPD", 3: "61-90 DPD", 4: "90+ / CO"}


def _m(v, d=1) -> str:
    if v is None: return "--"
    if abs(v) >= 1_000_000: return f"${v/1e6:.{d}f}M"
    if abs(v) >= 1_000: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def _mxn(v, d=1) -> str:
    if v is None: return "--"
    return f"MXN {v/1e6:.{d}f}M"


def _pct(v) -> str:
    return f"{v:.2f}%" if v is not None else "—"


def avail_color(v):
    if v is None: return "normal"
    if v < 0: return "inverse"
    if v < 3_000_000: return "off"
    return "normal"


# ── Header ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    state = load_state()
    cico  = load_cico()
    us    = state.get("us_bridge", {})
    mx    = state.get("mx_sofom", {})
    port  = state.get("portfolio", {})

    updated = state.get("updated_at", "")[:16].replace("T", " ")

    st.markdown(f"## 📊 Jeeves Capital Markets")
    st.caption(f"State as of {updated} · Auto-refresh: state changes ≤1 min, full every 5 min · [localhost:8100](http://localhost:8100)")

    st.divider()

    # ── Section 1: Borrowing Base Snapshot ───────────────────────────────────────
    st.markdown("### 🏦 Borrowing Base")

    bb_col1, bb_col2 = st.columns(2)

    with bb_col1:
        st.markdown("**US Bridge**")
        us_recv   = us.get("total_receivables")
        us_elig_g = us.get("eligible_gross") or us.get("eligible")
        us_inelig = (us_recv - us_elig_g) if (us_recv and us_elig_g) else None
        us_elig   = us_elig_g
        us_bb     = us.get("borrowing_base")
        us_cash   = (us.get("us_cash") or 0) + (us.get("ex_us_cash") or 0)
        us_drawn  = us.get("total_drawn")
        us_avail  = us.get("availability")
        us_dt     = (us.get("tape_as_of") or "")[:10]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Recv", _m(us_recv))
        c2.metric("Ineligible", _m(us_inelig), delta=None)
        c3.metric("Eligible", _m(us_elig))
        c4.metric("Final BB", _m(us_bb), help=f"Incl. {_m(us_cash)} cash")

        c5, c6 = st.columns(2)
        c5.metric("Drawn", _m(us_drawn))
        avail_delta = f"{'⚠️ TIGHT' if us_avail and us_avail < 3e6 else ''}"
        c6.metric("Availability", _m(us_avail), delta=avail_delta,
                  delta_color=avail_color(us_avail))
        st.caption(f"Tape: {us_dt} · {us.get('source_file','')}")

    with bb_col2:
        st.markdown("**MX SOFOM**")
        mx_recv_mxn   = mx.get("total_receivables_mxn")
        mx_inelig_mxn = mx.get("ineligible_mxn")
        mx_elig_mxn   = mx.get("eligible_mxn")
        mx_bb_usd     = mx.get("borrowing_base")
        mx_drawn      = mx.get("total_drawn")
        mx_avail      = mx.get("availability")
        mx_dt         = (mx.get("tape_as_of") or "")[:10]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Recv", _mxn(mx_recv_mxn))
        c2.metric("Ineligible", _mxn(mx_inelig_mxn))
        c3.metric("Eligible", _mxn(mx_elig_mxn))
        c4.metric("Final BB", _m(mx_bb_usd))

        c5, c6 = st.columns(2)
        c5.metric("Drawn", _m(mx_drawn))
        avail_delta = "⚠️ NEGATIVE" if mx_avail and mx_avail < 0 else ""
        c6.metric("Availability", _m(mx_avail), delta=avail_delta,
                  delta_color=avail_color(mx_avail))
        st.caption(f"Tape: {mx_dt} · {mx.get('source_file','')}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Row 2: DQ Trend (left) | Roll Rate Matrix (right)
    # ═══════════════════════════════════════════════════════════════════════════
    row2_left, row2_right = st.columns([1, 1])

    with row2_left:
        st.markdown("**📉 DQ 30+ Rate**")
        dq_df = load_dq_history()
        if not dq_df.empty:
            fig_dq = px.line(
                dq_df, x="month", y="dq30_rate_pct",
                labels={"month": "", "dq30_rate_pct": "DQ 30+ (%)"},
                template="plotly_dark", height=200,
            )
            fig_dq.add_hline(y=3.0, line_dash="dot", line_color="#f9e2af",
                             annotation_text="3%", annotation_position="bottom right")
            fig_dq.add_hline(y=5.0, line_dash="dot", line_color="#f38ba8",
                             annotation_text="5%", annotation_position="bottom right")
            fig_dq.update_traces(line_color="#89b4fa", line_width=2)
            fig_dq.update_layout(margin=dict(l=0, r=0, t=4, b=0),
                                 plot_bgcolor="#1e1e2e", paper_bgcolor="#1e1e2e")
            st.plotly_chart(fig_dq, use_container_width=True)
            latest = dq_df.iloc[-1]
            prev   = dq_df.iloc[-2] if len(dq_df) > 1 else None
            dq_c1, dq_c2, dq_c3 = st.columns(3)
            dq_c1.metric("DQ 30+", _pct(latest["dq30_rate_pct"]),
                         delta=f"{latest['dq30_rate_pct'] - prev['dq30_rate_pct']:+.2f}pp" if prev is not None else None)
            dq_c2.metric("DQ ADB", _m(latest["dq30_adb_usd"]))
            dq_c3.metric("Total ADB", _m(latest["total_adb_usd"]))

    with row2_right:
        st.markdown("**🔄 Roll Rate Matrix**")
        roll_df = load_roll_rate()
        if not roll_df.empty:
            latest_dt  = roll_df["bom_dt"].max()
            roll_latest = roll_df[roll_df["bom_dt"] == latest_dt].copy()

            roll_latest["bom_label"] = roll_latest["bom_bucket"].map(BUCKET_LABELS)
            roll_latest["eom_label"] = roll_latest["eom_bucket"].map(BUCKET_LABELS)

            bom_totals = roll_latest.groupby("bom_bucket")["total_balance_usd"].sum().rename("bom_total")
            roll_latest = roll_latest.join(bom_totals, on="bom_bucket")
            roll_latest["pct"] = (roll_latest["total_balance_usd"] / roll_latest["bom_total"] * 100).round(1)

            pivot_pct = roll_latest.pivot_table(
                index="bom_label", columns="eom_label", values="pct", aggfunc="sum", fill_value=0
            )
            order     = [l for l in BUCKET_LABELS.values() if l in pivot_pct.index]
            col_order = [l for l in BUCKET_LABELS.values() if l in pivot_pct.columns]
            pivot_pct   = pivot_pct.reindex(index=order, columns=col_order, fill_value=0)
            pivot_accts = roll_latest.pivot_table(
                index="bom_label", columns="eom_label", values="accounts", aggfunc="sum", fill_value=0
            ).reindex(index=order, columns=col_order, fill_value=0)

            bucket_order = list(BUCKET_LABELS.values())
            rows_list    = list(pivot_pct.index)
            cols_list    = list(pivot_pct.columns)

            def _blend(h1, h2, t):
                def _p(h): return tuple(int(h.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
                t = max(0.0, min(1.0, t))
                r1,g1,b1 = _p(h1); r2,g2,b2 = _p(h2)
                return "#{:02x}{:02x}{:02x}".format(
                    max(0,min(255,int(r1+(r2-r1)*t))),
                    max(0,min(255,int(g1+(g2-g1)*t))),
                    max(0,min(255,int(b1+(b2-b1)*t))))

            BG="#1e1e2e"; GRN_LO="#1a3328"; GRN_HI="#27a060"
            RED_LO="#3a1a1a"; RED_HI="#c0392b"; NEU_LO="#313244"; NEU_HI="#5a5a8a"; CO_CLR="#7d0000"

            col_fill, col_text = [], []
            for ci, col_label in enumerate(cols_list):
                eom_idx = bucket_order.index(col_label) if col_label in bucket_order else 99
                fill_col, text_col = [], []
                for ri, row_label in enumerate(rows_list):
                    bom_idx = bucket_order.index(row_label) if row_label in bucket_order else 0
                    val = float(pivot_pct.iat[ri, ci])
                    t   = min(val / 100.0, 1.0)
                    if col_label == "90+ / CO":
                        color = _blend(BG, CO_CLR, max(t * 2.5, 0.3 if val > 0 else 0))
                    elif eom_idx < bom_idx:
                        color = _blend(GRN_LO, GRN_HI, t) if val > 0 else BG
                    elif eom_idx == bom_idx:
                        color = _blend(NEU_LO, NEU_HI, t) if val > 0 else BG
                    else:
                        color = _blend(RED_LO, RED_HI, min(t * 4, 1.0)) if val > 0 else BG
                    fill_col.append(color)
                    try:
                        n = int(pivot_accts.iat[ri, ci])
                    except Exception:
                        n = 0
                    text_col.append(f"{val:.1f}%<br><sub>{n}ac</sub>" if val >= 0.1 else "--")
                col_fill.append(fill_col)
                col_text.append(text_col)

            _row_h = 34
            _hdr_h = 32
            fig_heat = go.Figure(go.Table(
                columnwidth=[90] + [70] * len(cols_list),
                header=dict(
                    values=["<b>BOM/EOM</b>"] + [f"<b>{c}</b>" for c in cols_list],
                    fill_color="#2a2a3e", font=dict(color="#cdd6f4", size=11),
                    align="center", height=_hdr_h,
                ),
                cells=dict(
                    values=[[f"<b>{r}</b>" for r in rows_list]] + col_text,
                    fill_color=[["#252538"] * len(rows_list)] + col_fill,
                    font=dict(color="#cdd6f4", size=11),
                    align="center", height=_row_h,
                ),
            ))
            fig_heat.update_layout(
                template="plotly_dark",
                height=_hdr_h + len(rows_list) * _row_h + 48,
                margin=dict(l=0, r=0, t=4, b=0),
                paper_bgcolor="#1e1e2e",
            )
            st.plotly_chart(fig_heat, use_container_width=True)

            legend_html = (
                '<small style="opacity:0.7">'
                '<span style="background:#27a060;padding:1px 6px;border-radius:3px">&nbsp;</span> Cure &nbsp;'
                '<span style="background:#5a5a8a;padding:1px 6px;border-radius:3px">&nbsp;</span> Stable &nbsp;'
                '<span style="background:#c0392b;padding:1px 6px;border-radius:3px">&nbsp;</span> Roll &nbsp;'
                '<span style="background:#7d0000;padding:1px 6px;border-radius:3px">&nbsp;</span> CO'
                '</small>'
            )
            st.markdown(legend_html, unsafe_allow_html=True)

            kpi_defs = [
                ("1-30 DPD", "Current",   "1-30 Cure"),
                ("1-30 DPD", "31-60 DPD", "1-30 Roll"),
                ("31-60 DPD","Current",   "31-60 Cure"),
                ("31-60 DPD","61-90 DPD", "31-60 Roll"),
            ]
            kc = st.columns(4)
            for i, (rk, ck, lbl) in enumerate(kpi_defs):
                try:
                    kc[i].metric(lbl, f"{pivot_pct.loc[rk, ck]:.1f}%")
                except KeyError:
                    pass
            st.caption(f"{latest_dt.strftime('%b %Y')} -> {(latest_dt + pd.offsets.MonthEnd(1)).strftime('%b %Y')} · balance-weighted · excl. charged-off BOM")

    # ═══════════════════════════════════════════════════════════════════════════
    # Row 3: Portfolio DPD (left) | Country (center) | NCO (right)
    # ═══════════════════════════════════════════════════════════════════════════
    row3_l, row3_m, row3_r = st.columns(3)

    with row3_l:
        st.markdown("**By DPD Bucket**")
        dpd_data = port.get("by_dpd_bucket") or {}
        if dpd_data:
            dpd_df = pd.DataFrame([
                {"Bucket": k, "Balance ($M)": v.get("balance", 0) / 1e6}
                for k, v in dpd_data.items()
            ]).sort_values("Balance ($M)", ascending=False)
            colors = {"Current":"#a6e3a1","1-30 DPD":"#f9e2af",
                      "31-60 DPD":"#fab387","61-90 DPD":"#f38ba8","90+ DPD":"#cba6f7"}
            fig_dpd = px.bar(dpd_df, x="Bucket", y="Balance ($M)",
                             color="Bucket", color_discrete_map=colors,
                             template="plotly_dark", height=200, text="Balance ($M)")
            fig_dpd.update_traces(texttemplate="$%{text:.1f}M", textposition="outside")
            fig_dpd.update_layout(showlegend=False, margin=dict(l=0,r=0,t=4,b=0),
                                  plot_bgcolor="#1e1e2e", paper_bgcolor="#1e1e2e")
            st.plotly_chart(fig_dpd, use_container_width=True)

    with row3_m:
        st.markdown("**By Country**")
        country_data = port.get("by_country") or {}
        if country_data:
            cdf = pd.DataFrame([
                {"Country": k, "Balance ($M)": v.get("balance", 0) / 1e6}
                for k, v in country_data.items() if v.get("balance", 0) > 100_000
            ]).sort_values("Balance ($M)", ascending=True)
            fig_c = px.bar(cdf, y="Country", x="Balance ($M)", orientation="h",
                           template="plotly_dark", height=200,
                           color="Balance ($M)", color_continuous_scale="Blues",
                           text="Balance ($M)")
            fig_c.update_traces(texttemplate="$%{text:.1f}M", textposition="outside")
            fig_c.update_layout(showlegend=False, coloraxis_showscale=False,
                                margin=dict(l=0,r=0,t=4,b=0),
                                plot_bgcolor="#1e1e2e", paper_bgcolor="#1e1e2e")
            st.plotly_chart(fig_c, use_container_width=True)

    with row3_r:
        st.markdown("**Monthly Charge-Off Rate**")
        nco_df = load_nco_history()
        if not nco_df.empty:
            fig_nco = px.bar(nco_df, x="month", y="nco_rate_pct",
                             labels={"month":"","nco_rate_pct":"NCO %"},
                             template="plotly_dark", height=200)
            fig_nco.update_traces(marker_color="#cba6f7")
            fig_nco.update_layout(margin=dict(l=0,r=0,t=4,b=0),
                                  plot_bgcolor="#1e1e2e", paper_bgcolor="#1e1e2e")
            st.plotly_chart(fig_nco, use_container_width=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # Row 4: Covenants
    # ═══════════════════════════════════════════════════════════════════════════
    st.markdown("**📋 Covenants**")
    cov_col1, cov_col2 = st.columns(2)

    with cov_col1:
        st.caption("US Bridge")
        us_covs = us.get("covenants", [])
        if us_covs:
            cov_rows = []
            for c in us_covs:
                excess   = c.get("excess_usd", 0) or 0
                headroom = c.get("limit_pct", 0) - c.get("actual_pct", 0)
                status   = "🔴 BREACH" if excess > 0 else ("🟡 Close" if headroom < 2 else "🟢 OK")
                cov_rows.append({
                    "Test":   c.get("test","")[:50] + "…",
                    "Limit":  f"{c.get('limit_pct',0):.0f}%",
                    "Actual": f"{c.get('actual_pct',0):.1f}%",
                    "Excess": _m(excess) if excess else "—",
                    "Status": status,
                })
            st.dataframe(pd.DataFrame(cov_rows), use_container_width=True, hide_index=True)

    with cov_col2:
        st.caption("MX SOFOM")
        mx_covs = mx.get("covenants", [])
        if mx_covs:
            cov_rows = []
            for c in mx_covs:
                excess   = c.get("excess_mxn", 0) or 0
                headroom = c.get("limit_pct", 0) - c.get("actual_pct", 0)
                status   = "🔴 BREACH" if excess > 0 else ("🟡 Close" if headroom < 2 else "🟢 OK")
                cov_rows.append({
                    "Test":   c.get("test","")[:50] + "…",
                    "Limit":  f"{c.get('limit_pct',0):.0f}%",
                    "Actual": f"{c.get('actual_pct',0):.1f}%",
                    "Excess": _mxn(excess) if excess else "—",
                    "Status": status,
                })
            st.dataframe(pd.DataFrame(cov_rows), use_container_width=True, hide_index=True)

    # ── Footer / auto-refresh ──────────────────────────────────────────────────────
    st.caption(f"Jeeves Capital Markets Dashboard · {date.today():%B %d, %Y} · Data from BB state files + Redshift")


    # Auto-refresh: fragment polls every 60s server-side; full-page rerun when a
    # state file changes or 5 minutes have passed. (A plain time-check at module
    # level never fires — Streamlit only reruns the script on an event.)
    import time


    @st.fragment(run_every="60s")
    def _auto_refresh():
        mtimes = tuple(f.stat().st_mtime if f.exists() else 0 for f in (STATE_FILE, CICO_FILE))
        marker = st.session_state.setdefault("_refresh_marker", (mtimes, time.time()))
        if mtimes != marker[0] or time.time() - marker[1] > 300:
            st.session_state._refresh_marker = (mtimes, time.time())
            st.cache_data.clear()
            st.rerun(scope="app")


    _auto_refresh()


# ── Cron supervisor entry point ───────────────────────────────────────────────
def _kill_stale_dashboards(log) -> None:
    """Kill leftover dashboard processes from previous gateway lifetimes.

    A gateway restart orphans the Streamlit subprocess, which keeps port 8100
    bound; every relaunch then wedges in a bind-retry loop behind it (2026-06-10:
    four queued instances). Sweep by command line so the wedged non-listeners
    die too, not just the port holder.
    """
    import subprocess

    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'streamlit' -and $_.CommandLine -match 'cm_credit_health_app' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        pids = [p for p in out.stdout.split() if p.strip().isdigit()]
        if pids:
            log.warning("killed stale dashboard process(es): %s", ", ".join(pids))
    except Exception:
        log.warning("stale dashboard sweep failed (continuing)", exc_info=True)


def run_loop():
    """Launch the Streamlit dashboard as a subprocess on port 8100.
    Called by cron_supervisor — runs as a supervised daemon.
    If the process dies, the supervisor restarts it.
    """
    import subprocess, time, os, sys

    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(Path(__file__)),
        "--server.port", "8100",
        "--server.headless", "true",
        "--server.address", "localhost",
        "--browser.gatherUsageStats", "false",
        "--server.fileWatcherType", "none",
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["STREAMLIT_SERVER_ENABLE_CORS"] = "false"

    import logging
    _kill_stale_dashboards(logging.getLogger("cm-dashboard"))

    proc = subprocess.Popen(
        cmd,
        cwd=str(Path(__file__).parent.parent),  # backend/
        env=env,
    )
    import logging
    log = logging.getLogger("cm-dashboard")
    log.info("Streamlit credit health dashboard started (PID %d) on :8100", proc.pid)

    # Block until process exits (supervisor will restart if it crashes)
    proc.wait()
    log.warning("Streamlit dashboard exited with code %d", proc.returncode)
    # Small delay before supervisor restarts
    time.sleep(5)
    raise RuntimeError(f"Streamlit exited with code {proc.returncode}")
