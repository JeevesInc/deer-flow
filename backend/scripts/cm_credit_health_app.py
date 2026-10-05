#!/usr/bin/env python3
"""cm_credit_health_app.py - Capital Markets Mission Control (Streamlit, :8100).

Replaces the 2026-06 credit-health app (US bridge BB, since retired). Every chart is a live Redshift
query whose SQL lives in ONE place: cfo-org-kb scripts/mission_control/mc_cards.py. Facility, waterfall,
test and loan data come from analytics_sandbox.cm_mc_*, written daily by
backend/scripts/mission_control_snapshot.py (NB workbook + nb_covenant_check, CIM MX master,
servicer-report Exhibit B, receivable balance forecast).

Run:  streamlit run scripts/cm_credit_health_app.py --server.port 8100 --server.headless true
Service: deerflow-dashboard (deploy/winsw/deerflow-dashboard.xml).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = Path(os.environ.get("SKILLS_PATH", str(BACKEND_DIR.parent / "skills")))
MC_DIR = SKILLS_DIR / "custom" / "cfo-org-kb" / "scripts" / "mission_control"
for p in (SKILLS_DIR / "custom" / "jeeves-borrowing-base", MC_DIR, Path(__file__).resolve().parent):
    sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------------------------
# Supervisor hooks (kept for the 2026-06-16 import-safety test - must not query at import)
# ---------------------------------------------------------------------------------------------
def _kill_stale_dashboards(log) -> None:
    """Kill leftover dashboard processes from previous lifetimes (port 8100 bind-retry wedge)."""
    import subprocess

    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'streamlit' -and $_.CommandLine -match 'cm_credit_health_app' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        pids = [p for p in out.stdout.split() if p.strip().isdigit()]
        if pids:
            log.warning("killed stale dashboard process(es): %s", ", ".join(pids))
    except Exception:
        log.warning("stale dashboard sweep failed (continuing)", exc_info=True)


def run_loop():
    """Launch the dashboard as a subprocess on :8100 (legacy supervisor entry point)."""
    import logging
    import subprocess
    import time

    cmd = [sys.executable, "-m", "streamlit", "run", str(Path(__file__)), "--server.port", "8100",
           "--server.headless", "true", "--server.address", "localhost",
           "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    log = logging.getLogger("cm-dashboard")
    _kill_stale_dashboards(log)
    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR), env=env)
    log.info("Mission Control dashboard started (PID %d) on :8100", proc.pid)
    proc.wait()
    time.sleep(5)
    raise RuntimeError(f"Streamlit exited with code {proc.returncode}")


# ---------------------------------------------------------------------------------------------
# Shared by the app and the hosted-data exporter (mc_export.py)
# ---------------------------------------------------------------------------------------------
FWD_BASE_SQL = """select as_of, eligible, excess_concentration, excess_pct_stepdown, advance_rate, cash_bb, commitment,
       drawn, fx_exposed_share
from analytics_sandbox.cm_mc_facility_daily
where facility = 'NB' and as_of = (select max(as_of) from analytics_sandbox.cm_mc_facility_daily where facility = 'NB')"""
FWD_FC_SQL = "select dt, nb_pool, total, total_p10 from analytics_sandbox.cm_mc_receivable_forecast order by dt"
STEPDOWN_DATE = "2027-02-10"


def mc_export_name(card, key=None):
    return card + ("" if key is None else "__" + "".join(ch if ch.isalnum() else "_" for ch in str(key))) + ".parquet"


def forward_path(base, fc, weeks=13, funding_need_mm=None, dpd_shock_pct=0.0, conc_shock_pct=0.0, fx_move_pct=0.0,
                 growth_wk_pct=None):
    """NB forward view. Book path = receivable balance forecast (US+CO+BR series) indexed to its first day and
    applied to today's NB eligible; P10 path scales by TOTAL P10 / forecast. Growth override replaces the model."""
    import datetime as dt
    import pandas as pd
    b = base.iloc[0]
    fc = fc.copy()
    fc["dt"] = pd.to_datetime(fc["dt"]).dt.date
    fc = fc.set_index("dt")
    nb0 = float(fc["nb_pool"].iloc[0])
    as_of = pd.to_datetime(b["as_of"]).date()
    funding = (funding_need_mm * 1e6) if funding_need_mm is not None else float(b["drawn"])
    rows = []
    for w in range(1, int(weeks) + 1):
        d = as_of + dt.timedelta(weeks=w)
        if d not in fc.index:
            continue
        r = fc.loc[d]
        factor = (1 + w * growth_wk_pct / 100.0) if growth_wk_pct not in (None, "") else float(r["nb_pool"]) / nb0
        elig = float(b["eligible"]) * factor * (1 - (dpd_shock_pct or 0) / 100.0)             * (1 - (fx_move_pct or 0) / 100.0 * float(b["fx_exposed_share"] or 0))
        exc = (float(b["excess_pct_stepdown"]) if d > dt.date.fromisoformat(STEPDOWN_DATE)
               else float(b["excess_concentration"]) / float(b["eligible"])) + (conc_shock_pct or 0) / 100.0
        p10 = float(r["total_p10"]) / float(r["total"])
        bb = elig * (1 - exc) * float(b["advance_rate"]) + float(b["cash_bb"])
        bb10 = elig * p10 * (1 - exc) * float(b["advance_rate"]) + float(b["cash_bb"])
        cm = float(b["commitment"])
        rows.append(dict(week_ending=d, eligible=elig, borrowing_base=bb, borrowing_base_p10=bb10, commitment=cm,
                         funding_need=funding, availability=min(bb, cm) - funding, availability_p10=min(bb10, cm) - funding))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------------------------
def main():
    import datetime as dt
    import warnings

    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    import streamlit as st

    warnings.filterwarnings("ignore")
    try:
        from dotenv import load_dotenv
        load_dotenv(BACKEND_DIR / ".env")
    except Exception:
        pass
    import mc_cards
    import mc_sql

    st.set_page_config(page_title="Jeeves - Capital Markets Mission Control", page_icon="📊", layout="wide")
    st.markdown("<style>.block-container{padding-top:1rem}</style>", unsafe_allow_html=True)

    HERE = Path(__file__).resolve().parent
    DATA = HERE / "data"
    MODE = "files" if (DATA / "meta.json").exists() else "live"
    FILE_KEYS = {  # card -> the parameter its exported file is split by (None = single file)
        "facility_table": None, "kpi_avail": None, "heatmap": None, "obligors": None, "recycling": None,
        "waterfall_steps": "facility_req", "reasons": "facility_req", "bb_trend": "facility_req",
        "loans": "facility_req", "test_trend": "facility_req",
        "dq_mix": "pool", "roll_rates": "pool", "epd": "pool", "vintage": "pool", "risk_migration": "pool",
        "collections": "pool"}

    @st.cache_data(ttl=900, show_spinner=False)
    def q(card: str, params: tuple) -> pd.DataFrame:
        p = dict(params)
        if MODE == "files":
            key = FILE_KEYS[card]
            df = pd.read_parquet(DATA / mc_export_name(card, p.get(key) if key else None))
            if card == "loans":
                if p.get("reason"):
                    df = df[df["reason"] == p["reason"]]
                if p.get("company"):
                    df = df[df["name"].str.contains(p["company"], case=False, na=False, regex=False)]
                if p.get("min_dpd"):
                    df = df[df["dpd"] >= p["min_dpd"]]
            if card == "test_trend" and p.get("test"):
                df = df[df["test"].str.contains(p["test"], case=False, na=False, regex=False)]
            return df.reset_index(drop=True)
        import redshift_util as ru
        sql = mc_sql.render(mc_cards.CARDS[card]["sql"], p) if card in mc_cards.CARDS else p["_sql"]
        conn = ru.connect()
        try:
            return pd.read_sql(sql, conn)
        finally:
            conn.close()

    def run(card, **params):
        if MODE == "files":
            params.pop("as_of", None)
        try:
            return q(card, tuple(sorted(params.items())))
        except Exception as e:  # show the failure on the card, never a blank page
            st.error(f"{mc_cards.CARDS[card]['name'] if card in mc_cards.CARDS else card}: query failed - {str(e)[:300]}")
            return pd.DataFrame()

    def forward_inputs():
        if MODE == "files":
            return pd.read_parquet(DATA / "forward_base.parquet"), pd.read_parquet(DATA / "forecast.parquet")
        b = run("_fwd_base", _sql=FWD_BASE_SQL)
        f = run("_fwd_fc", _sql=FWD_FC_SQL)
        return b, f

    def title(card):
        st.markdown(f"#### {mc_cards.CARDS[card]['name'].split(' - ', 1)[-1]}")

    def chart(card, df, pct=False, height=360):
        if df.empty:
            st.info("No rows.")
            return
        viz = mc_cards.CARDS[card].get("viz", {})
        dims = viz.get("graph.dimensions") or [df.columns[0]]
        mets = viz.get("graph.metrics") or [c for c in df.columns if c not in dims and pd.api.types.is_numeric_dtype(df[c])]
        if len(dims) > 1:
            fig = px.line(df, x=dims[0], y=mets[0], color=dims[1], markers=False)
        else:
            long = df.melt(id_vars=dims[0], value_vars=mets, var_name="series", value_name="value")
            fig = px.line(long, x=dims[0], y="value", color="series", markers=True)
        fig.update_layout(height=height, margin=dict(l=10, r=10, t=10, b=10), legend_title_text="")
        if pct:
            fig.update_yaxes(tickformat=".1%")
        st.plotly_chart(fig, width="stretch")

    usd = lambda v: "-" if v is None or pd.isna(v) else ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"
    usd_mm = lambda v: "-" if v is None or pd.isna(v) else ("-$" if v < 0 else "$") + f"{abs(v) / 1e6:,.1f}MM"

    # ---- sidebar ----
    with st.sidebar:
        st.header("Mission Control")
        as_of = st.date_input("Snapshot as of (blank = latest)", value=None) if MODE == "live" else None
        pool = st.radio("Credit-quality pool", ["NB", "MX", "All"], horizontal=True,
                        help="NB = US/CO/BR receivables; MX = Mexico; All = whole book")
        if st.button("Refresh data now"):
            st.cache_data.clear()
        st.caption("Live Redshift, cached 15 min." if MODE == "live" else
                   "Hosted copy: data published daily at about 11:00 PT from the Redshift snapshot.")
        st.caption("Snapshot sources: NB workbook, CIM MX master, servicer-report Exhibit B, receivable forecast.")
    base = {"as_of": as_of} if as_of else {}

    fac = run("facility_table", **base)
    hm = run("heatmap", **base)
    snap = fac["data_as_of"].iloc[0] if not fac.empty else "n/a"
    st.title("Capital Markets Mission Control")
    st.caption(f"Snapshot: {snap}")

    # ---- headline ----
    if not fac.empty:
        cols = st.columns(len(fac))
        for c, (_, r) in zip(cols, fac.iterrows()):
            with c:
                st.subheader(r["facility"])
                a, b, d = st.columns(3)
                a.metric("Borrowing base", usd_mm(r["borrowing_base"]))
                b.metric("Drawn", usd_mm(r["drawn"]))
                d.metric("Availability", usd_mm(r["availability"]))
                st.caption(f"Binding: **{r['binding_constraint'] or '-'}**  |  {r['status']}")
    if not hm.empty:
        bad = hm[hm["status"].isin(["BREACH", "EXCESS", "WATCH", "NO DATA"])]
        if (bad["status"] == "BREACH").any():
            for _, r in bad[bad["status"] == "BREACH"].iterrows():
                st.error(f"**{r['facility']} - {r['test']}**: {r['current']} vs {r['threshold']} ({r['distance']}). {r['consequence']}")

    t1, t2, t3, t4, t5 = st.tabs(["Facilities", "Waterfall & drill", "Triggers & covenants", "Credit quality", "Forward view"])

    # ---- 1. facilities ----
    with t1:
        title("facility_table")
        if not fac.empty:
            show = fac.copy()
            for c in ["commitment", "drawn", "borrowing_base", "availability", "undrawn_commitment", "bb_excess_deficit",
                      "gross_receivables", "eligible", "excess_concentration", "cash_bb"]:
                show[c] = show[c].map(usd)
            show["advance_rate"] = show["advance_rate"].map(lambda v: "-" if pd.isna(v) else f"{v:.0%}")
            st.dataframe(show, hide_index=True, width="stretch")
        f = st.selectbox("Facility", [x for x in fac["facility"]] if not fac.empty else ["NB"], key="bbf")
        title("bb_trend")
        chart("bb_trend", run("bb_trend", facility_req=f))
        title("recycling")
        rec = run("recycling")
        st.dataframe(rec, hide_index=True, width="stretch")

    # ---- 2. waterfall ----
    with t2:
        f = st.selectbox("Facility", [x for x in fac["facility"]] if not fac.empty else ["NB"], key="wff")
        steps = run("waterfall_steps", facility_req=f, **base)
        title("waterfall")
        if not steps.empty:
            measure = ["absolute" if k == "total" and i == 0 else ("total" if k == "total" else "relative")
                       for i, k in enumerate(steps["kind"])]
            fig = go.Figure(go.Waterfall(x=steps["step"], y=steps["amount_usd"], measure=measure,
                                         text=[usd_mm(v) for v in steps["amount_usd"]], textposition="outside"))
            fig.update_layout(height=460, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
            st.plotly_chart(fig, width="stretch")
        c1, c2 = st.columns([2, 3])
        with c1:
            title("reasons")
            rs = run("reasons", facility_req=f, **base)
            st.dataframe(rs.assign(balance_usd=rs.get("balance_usd", pd.Series(dtype=float)).map(usd)),
                         hide_index=True, width="stretch")
        with c2:
            title("loans")
            a, b, d = st.columns(3)
            reason = a.selectbox("Reason", ["(all)"] + (rs["reason"].tolist() if not rs.empty else []))
            company = b.text_input("Company contains")
            min_dpd = d.number_input("Min DPD", 0, 999, 0)
            ln = run("loans", facility_req=f, reason=None if reason == "(all)" else reason,
                     company=company or None, min_dpd=min_dpd or None, **base)
            st.dataframe(ln, hide_index=True, width="stretch", height=380)
            st.download_button("Download loans (CSV)", ln.to_csv(index=False), file_name=f"Loans - {f} - {snap}.csv")
        if f == "NB":
            title("obligors")
            st.dataframe(run("obligors", **base), hide_index=True, width="stretch")

    # ---- 3. triggers ----
    with t3:
        title("heatmap")
        colors = {"BREACH": "#f8d7da", "EXCESS": "#fde2c8", "WATCH": "#fff3cd", "NO DATA": "#e2e3e5", "OK": "#d4edda", "INFO": "#e7f1ff"}
        if not hm.empty:
            ff = st.multiselect("Facility", sorted(hm["facility"].unique()), default=sorted(hm["facility"].unique()))
            view = hm[hm["facility"].isin(ff)]
            st.dataframe(view.style.apply(lambda r: [f"background-color:{colors.get(r['status'], '')}"] * len(r), axis=1),
                         hide_index=True, width="stretch", height=600)
        a, b = st.columns(2)
        f = a.selectbox("Trend: facility", sorted(hm["facility"].unique()) if not hm.empty else ["NB"], key="ttf")
        tt = b.text_input("Trend: test contains", "DQ30")
        title("test_trend")
        chart("test_trend", run("test_trend", facility_req=f, test=tt or None), pct=True)

    # ---- 4. credit quality ----
    with t4:
        st.caption(f"Pool: **{pool}** (change in the sidebar). Live from capital_markets_dm.loc_tape.")
        c1, c2 = st.columns(2)
        with c1:
            title("dq_mix"); chart("dq_mix", run("dq_mix", pool=pool), pct=True)
            title("epd"); chart("epd", run("epd", pool=pool))
            title("collections"); chart("collections", run("collections", pool=pool))
        with c2:
            title("roll_rates"); chart("roll_rates", run("roll_rates", pool=pool), pct=True)
            title("vintage"); chart("vintage", run("vintage", pool=pool), pct=True)
            st.caption("Vintage = quarter of first positive balance. Cumulative charge-offs (balance on charge_off_dt) "
                       "/ the vintage's peak month-end balance. Benchmark = all vintages pooled.")
        title("risk_migration")
        st.dataframe(run("risk_migration", pool=pool), hide_index=True, width="stretch")

    # ---- 5. forward ----
    with t5:
        st.caption("Book path = receivable balance forecast by country x product (pooled XGBoost, retrained Mondays), "
                   "US/CO/BR summed for the NB pool; dotted = P10. Advance rate and controlled cash held flat; concentration "
                   "limits tighten after 2027-02-10. Availability = min(BB, $100MM commitment) - funding need.")
        a, b, c = st.columns(3)
        weeks = a.slider("Weeks", 4, 26, 13)
        funding = a.number_input("Funding need ($MM)", 0.0, 100.0, 79.5, 0.5, help="Default $77.5MM drawn + $2MM 10/9 draw")
        dpd = b.slider("DPD shock: % of eligible lost", 0.0, 30.0, 0.0, 0.5)
        conc = b.slider("Concentration shock (pp of eligible)", 0.0, 20.0, 0.0, 0.5)
        fx = c.slider("FX move vs USD (%)", 0.0, 40.0, 0.0, 1.0)
        growth = c.number_input("Growth override (%/wk, blank = forecast)", value=None, step=0.25)
        fb, ffc = forward_inputs()
        fw = forward_path(fb, ffc, weeks, funding, dpd, conc, fx, growth) if not (fb.empty or ffc.empty) else pd.DataFrame()
        title("forward")
        if not fw.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=fw["week_ending"], y=fw["borrowing_base_p10"], name="Borrowing base (P10)", line=dict(dash="dot")))
            fig.add_trace(go.Scatter(x=fw["week_ending"], y=fw["borrowing_base"], name="Borrowing base (forecast)", fill="tonexty"))
            fig.add_trace(go.Scatter(x=fw["week_ending"], y=fw["commitment"], name="Commitment", line=dict(dash="dash")))
            fig.add_trace(go.Scatter(x=fw["week_ending"], y=fw["funding_need"], name="Funding need", line=dict(color="red")))
            fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickprefix="$")
            st.plotly_chart(fig, width="stretch")
            low = fw["availability_p10"].min()
            st.metric("Lowest availability in window (P10 path)", usd_mm(low),
                      delta="SHORTFALL" if low < 0 else None, delta_color="inverse")
            show = fw.copy()
            for col in show.columns[1:]:
                show[col] = show[col].map(usd)
            st.dataframe(show, hide_index=True, width="stretch")


if __name__ == "__main__":
    main()
