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


# Receivable balance forecast by country x product (forward view). Actuals use the same basis as
# cfo-org-kb sql/receivable_balance_panel_daily.sql (ex charge-offs, ex in-repayment).
ACTUALS_SQL = """select t.dt,
       case t.country_code when 840 then 'US' when 484 then 'MX' when 170 then 'CO' when 76 then 'BR'
            when 124 then 'CA' else 'OTHER' end as country,
       sum(coalesce(t.card_balance_usd, 0)) as card, sum(coalesce(t.jp_balance_usd, 0)) as jp
from capital_markets_dm.loc_tape t
where t.dt >= current_date - 400 and t.charge_off_flag = false and t.is_in_repayment = false
group by 1, 2 order by 1, 2"""
FC_DIRS = [os.path.join(str(SKILLS_DIR.parent), "backend", ".deer-flow", d) for d in ("_cap_markets_scratch", "_mission_control")]


def actuals_long(df):
    import pandas as pd
    out = df.melt(id_vars=["dt", "country"], value_vars=["card", "jp"], var_name="product", value_name="balance")
    out["product"] = out["product"].map({"card": "Card", "jp": "Jeeves Pay"})
    out["dt"] = pd.to_datetime(out["dt"])
    day = out.groupby("dt")["balance"].sum()
    good = day[day > 0.5 * day.rolling(7, min_periods=1).median().shift(1).fillna(day)].index  # drop empty / partial days
    return out[out["dt"].isin(good)].reset_index(drop=True)


def latest_forecast_workbook():
    import glob
    fs = [f for d in FC_DIRS for f in glob.glob(os.path.join(d, "Model - Receivable Balance Forecast - *.xlsx"))]
    return max(fs, key=os.path.getmtime)


def forecast_bands(path=None):
    """Empirical P10/P90 (log actual/forecast) by horizon for each country-group x product selection,
    from the model's out-of-fold paths ('Band quantiles' sheet), plus the 'Band summary' coverage table."""
    import pandas as pd
    path = path or latest_forecast_workbook()
    x = pd.ExcelFile(path)
    if "Band quantiles" not in x.sheet_names:
        return pd.DataFrame(), pd.DataFrame()
    return x.parse("Band quantiles"), x.parse("Band summary")


def forecast_series(path=None):
    """Latest weekly forecast workbook -> long table: date, country, product, yhat, a, h (+ TOTAL rows with p10/p90).
    The model only publishes a band for TOTAL (sd = a*sqrt(h) from out-of-fold errors). Series bands use the same
    form, with a fitted per series from that run's hold-out errors (log actual/predicted over sqrt(days since
    train cut-off)); series without hold-out errors use the pooled value."""
    import glob, re as _re
    import numpy as np
    import pandas as pd
    path = path or latest_forecast_workbook()
    run_as_of = pd.Timestamp(_re.search(r"(\d{8})", os.path.basename(path)).group(1))
    w = pd.read_excel(path, sheet_name="Forecast daily")
    hd = pd.read_excel(path, sheet_name="Hold-out daily")
    hm = pd.read_excel(path, sheet_name="Hold-out metrics")
    cut = pd.Timestamp(hm["train_through"].iloc[0])
    hd = hd[(hd["Actual (USD)"] > 0) & (hd["Predicted (USD)"] > 0)].copy()
    hd["z"] = np.log(hd["Actual (USD)"] / hd["Predicted (USD)"]) / np.sqrt((pd.to_datetime(hd["Date"]) - cut).dt.days.clip(lower=1))
    a_s = hd.groupby("Series")["z"].apply(lambda z: float(np.sqrt(np.mean(z ** 2))))
    a_pool = float(np.sqrt(np.mean(hd["z"] ** 2)))
    w["Date"] = pd.to_datetime(w["Date"])
    h = (w["Date"] - run_as_of).dt.days.clip(lower=1)
    rows = []
    for c in w.columns:
        if " | " not in str(c):
            continue
        ctry, prod = str(c).split(" | ")
        rows.append(pd.DataFrame({"date": w["Date"], "country": ctry, "product": prod, "yhat": w[c],
                                  "a": a_s.get(c, a_pool), "h": h, "p10": np.nan, "p90": np.nan}))
    rows.append(pd.DataFrame({"date": w["Date"], "country": "TOTAL", "product": "TOTAL", "yhat": w["TOTAL"],
                              "a": np.nan, "h": h, "p10": w["TOTAL P10"], "p90": w["TOTAL P90"]}))
    out = pd.concat(rows, ignore_index=True)
    out["run_as_of"] = run_as_of
    wape = hm.set_index("series")["wape_model"]
    out["holdout_wape"] = [wape.get(f"{r.country} | {r.product}", wape.get("TOTAL PORTFOLIO")) for r in
                           out[["country", "product"]].itertuples()]
    return out


# ---------------------------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------------------------
LABELS = {
    "borrowing_base": "Borrowing base", "drawn": "Drawn", "commitment": "Commitment", "availability": "Availability",
    "eligible": "Eligible", "undrawn_commitment": "Undrawn commitment", "bb_excess_deficit": "BB excess / (deficit)",
    "advance_rate": "Advance rate", "binding_constraint": "Binding constraint", "draw_period_end": "Draw period end",
    "gross_receivables": "Gross receivables", "excess_concentration": "Excess concentration", "cash_bb": "Controlled cash in BB",
    "data_as_of": "Data as of", "dpd_1_30": "1-30 DPD", "dpd_31_60": "31-60 DPD", "dpd_61_90": "61-90 DPD",
    "dpd_90_plus": "90+ DPD", "dq30_plus": "DQ30+ (total)", "current_to_dq": "Current → DQ",
    "roll_1_30_to_31_60": "1-30 → 31-60", "roll_31_60_to_61_90": "31-60 → 61-90", "roll_61_90_to_90_plus": "61-90 → 90+",
    "cure_30_plus_to_current": "Cure: 30+ → current", "new_obligors": "New obligors", "epd_rate": "EPD rate",
    "collections_usd": "Collections", "collections_to_opening_balance": "Collections ÷ opening balance",
    "balance_usd": "Balance", "dpd": "DPD", "pct_of_eligible": "% of eligible", "cumulative_pct": "Cumulative %",
    "request_no": "Request #", "sent_at": "Sent", "requested_mxn": "Requested (MXN)", "current_mxn": "Current (MXN)",
    "directed_mxn": "Directed (MXN)", "recycling_2_mxn": "Recycling 2 (MXN)", "recycling_1_mxn": "Recycling 1 (MXN)",
    "reqd_mxn": "Required (MXN)", "avail_to_recycle_mxn": "Available to recycle (MXN)",
    "availability_lost_if_breached": "Availability lost if breached", "week_ending": "Week ending",
    "borrowing_base_p10": "Borrowing base (P10)", "funding_need": "Funding need", "availability_p10": "Availability (P10)",
    "grade_90d_ago": "Grade 90 days ago", "grade_now": "Grade now", "credit_support": "Credit support", "net": "Net",
}
C = dict(navy="#8ab4ff", blue="#4c9bff", teal="#2ec4a6", orange="#ffa04d", red="#ff6b6b", grey="#8b95a7",
         purple="#b39dff", ink="#e8ebf1", light="#5b6475")
HEAT = [[0, "#151a24"], [0.35, "#1d4579"], [1, "#2f6fc4"]]  # dark-mode sequential scale


def lab(c):
    return LABELS.get(c, str(c).replace("_", " ").capitalize())


def main():
    import datetime as dt
    import json
    import warnings

    import numpy as np
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    import streamlit as st
    from plotly.subplots import make_subplots

    warnings.filterwarnings("ignore")
    try:
        from dotenv import load_dotenv
        load_dotenv(BACKEND_DIR / ".env")
    except Exception:
        pass
    import mc_cards
    import mc_sql
    import mc_assistant

    try:  # hosted copy: Streamlit Cloud secrets -> env (ANTHROPIC_API_KEY, MC_GITHUB_TOKEN)
        for k_ in ("ANTHROPIC_API_KEY", "MC_GITHUB_TOKEN", "MC_LAYOUT_REPO", "MC_ASSISTANT_MODEL"):
            if k_ in st.secrets and not os.environ.get(k_):
                os.environ[k_] = str(st.secrets[k_])
    except Exception:
        pass

    st.set_page_config(page_title="Capital Markets Mission Control", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.markdown("""<style>
    .block-container{padding-top:1.2rem;max-width:1500px}
    .kpi{border:1px solid #2b3242;border-radius:10px;padding:12px 14px;background:#171c27;height:100%}
    .kpi.bad{border-color:#e05d5d;box-shadow:0 0 0 1px #e05d5d inset}
    .kpi-h{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
    .kpi-h .nm{font-weight:700;font-size:1.15rem;color:#f1f3f7}
    .pill{font-size:.72rem;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
    .pill.ok{background:rgba(52,168,83,.18);color:#7fd99a}.pill.bad{background:rgba(224,93,93,.2);color:#ff9b9b}
    .pill.info{background:rgba(90,169,255,.18);color:#9cc8ff}
    .kpi-g{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;align-items:end}
    .kpi-g .l{font-size:.72rem;color:#9aa4b2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .kpi-g .v{font-size:clamp(.95rem,1.45vw,1.35rem);font-weight:650;color:#f1f3f7;
      font-variant-numeric:tabular-nums;white-space:nowrap}
    .kpi-g .v.neg{color:#ff7b7b}
    .kpi .sub{font-size:.78rem;color:#9aa4b2;margin-top:8px;line-height:1.3}
    .kpi .sub b{color:#d5dae3}
    .chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 4px}
    .chip{border-radius:8px;padding:6px 10px;font-size:.82rem;line-height:1.3;border:1px solid transparent;color:#e6e9ef}
    .chip b{font-variant-numeric:tabular-nums;color:#fff}
    .chip.BREACH{background:rgba(224,93,93,.16);border-color:rgba(224,93,93,.55)}
    .chip.EXCESS{background:rgba(240,140,46,.14);border-color:rgba(240,140,46,.5)}
    .chip.WATCH{background:rgba(242,201,76,.12);border-color:rgba(242,201,76,.45)}
    .chip.OK{background:rgba(52,168,83,.12);border-color:rgba(52,168,83,.4)}
    .chip .t{font-size:.7rem;font-weight:700;letter-spacing:.03em;margin-right:6px}
    .chip.BREACH .t{color:#ff9b9b}.chip.EXCESS .t{color:#ffb672}.chip.WATCH .t{color:#f2d27a}.chip.OK .t{color:#7fd99a}
    .alert{background:rgba(224,93,93,.16);border-left:4px solid #e05d5d;border-radius:6px;padding:10px 14px;
      margin:10px 0;color:#f3d6d6}
    .alert b{color:#ffb3b3}
    </style>""", unsafe_allow_html=True)

    HERE = Path(__file__).resolve().parent
    DATA = HERE / "data"
    MODE = "files" if (DATA / "meta.json").exists() else "live"
    META = json.loads((DATA / "meta.json").read_text()) if MODE == "files" else {}
    # one shared layout for local + hosted: mc_layout.json in the hosted repo (GitHub API); local file = cache
    LAYOUT = mc_assistant.Layout(HERE / "mc_layout.json" if MODE == "files" else
                                 BACKEND_DIR / ".deer-flow" / "mission_control_layout.json",
                                 gh_repo=os.environ.get("MC_LAYOUT_REPO", "brian-tryjeeves/cm-mission-control"),
                                 readonly_without_gh=(MODE == "files"))
    on, T = LAYOUT.on, LAYOUT.title

    # ---------- what-if sandbox: free-form pandas over the NB loan book + the certificate calc (mc_sandbox.py) ----------
    import mc_sandbox
    import nb_scenario
    SCN_DIR = DATA if MODE == "files" else BACKEND_DIR / ".deer-flow" / "mission_control"
    _sp = SCN_DIR / "nb_scenario_base.parquet"
    SCN_VER = f"{_sp.stat().st_mtime if _sp.exists() else 0}"
    SQL_PATHS = None if MODE == "files" else [SKILLS_DIR / "custom" / "_shared",
                                              SKILLS_DIR / "custom" / "jeeves-borrowing-base"]

    @st.cache_data(ttl=3600, show_spinner=False, max_entries=50)
    def whatif(code, ver):
        return mc_sandbox.run(code, SCN_DIR, SQL_PATHS)

    def scn_fmt(k, v):
        if isinstance(v, str) or v is None:
            return v or "-"
        return f"{v:.1%}" if k == "advance_rate" else mm(v)

    def scn_change(k, a, b):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or abs(b - a) < 0.5 and k != "advance_rate":
            return ""
        return f"{b - a:+.1%}" if k == "advance_rate" else mm(b - a)

    def _sb_run(code, purpose):
        if not _sp.exists():
            return "No scenario data published yet.", None
        out = whatif(code, SCN_VER)
        if not out.get("ok"):
            return "FAILED - fix the code and rerun:\n" + (out.get("error") or "")[-2500:] + \
                "\nstdout:\n" + (out.get("stdout") or "")[-1500:], None
        txt = []
        if (out.get("stdout") or "").strip():
            txt.append("stdout:\n" + out["stdout"][-3000:])
        if out.get("result") is not None:
            txt.append("result:\n" + json.dumps(out["result"], default=str)[:6000])
        txt.append(f"Base: NB tape {out['scal']['as_of']}; cash and borrowings: {out['scal']['cash_source']}.")
        label = None
        if out.get("scenario"):
            a_, s_ = out["actual"]["metrics"], out["scenario"]["metrics"]
            txt.append("line | actual | scenario | change")
            txt += [f"{lbl} | {scn_fmt(k, a_.get(k))} | {scn_fmt(k, s_.get(k))} | {scn_change(k, a_.get(k), s_.get(k))}"
                    for k, lbl in nb_scenario.COMPARE]
            txt.append("Scenario top obligors (net, % of eligible): " + "; ".join(
                f"{o['name'][:30]} {mm(o['net'])} {o['pct_of_eligible']:.1%}" for o in out["scenario"]["top_obligors"][:5]))
            label = (purpose or "what-if")[:80]
            st.session_state["mc_scn"] = {"name": "Assistant: " + label, "code": code, "question": purpose}
            st.session_state["scn_pick"] = "Assistant: " + label
        return "\n".join(txt), label

    scenario_tool = {"run": _sb_run, "catalog": lambda: mc_sandbox.catalog(SCN_DIR)}

    FILE_KEYS = {
        "facility_table": None, "kpi_avail": None, "heatmap": None, "obligors": None, "recycling": None,
        "waterfall_steps": "facility_req", "reasons": "facility_req", "bb_trend": "facility_req",
        "loans": "facility_req", "test_trend": "facility_req",
        "dq_mix": "pool", "roll_rates": "pool", "roll_matrix": "pool", "epd": "pool", "vintage": "pool", "risk_migration": "pool",
        "collections": "pool"}

    @st.cache_data(ttl=900, show_spinner=False)
    def q(card: str, params: tuple, data_version: str = "") -> pd.DataFrame:  # version busts cache on each publish
        p = dict(params)
        if MODE == "files":
            key = FILE_KEYS[card]
            path = DATA / mc_export_name(card, p.get(key) if key else None)
            if not path.exists():
                return pd.DataFrame()
            df = pd.read_parquet(path)
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
            return q(card, tuple(sorted(params.items())), META.get("published_at", ""))
        except Exception as e:
            st.error(esc(f"{lab(card)}: could not load data - {str(e)[:300]}. Use Refresh data in the sidebar."))
            return pd.DataFrame()

    def forward_inputs():
        if MODE == "files":
            return pd.read_parquet(DATA / "forward_base.parquet"), pd.read_parquet(DATA / "forecast.parquet")
        return run("_fwd_base", _sql=FWD_BASE_SQL), run("_fwd_fc", _sql=FWD_FC_SQL)

    # ---------- helpers ----------
    def esc(s):  # stop Streamlit reading "$...$" as LaTeX
        return str(s).replace("$", "\\$")

    def h(s):  # same, for raw HTML blocks
        return str(s).replace("$", "&#36;")

    def mm(v, dash="-"):
        if v is None or pd.isna(v):
            return dash
        return ("-$" if v < 0 else "$") + f"{abs(v) / 1e6:,.1f}MM"

    def short(v):
        if v is None or pd.isna(v):
            return ""
        a = abs(v)
        s_ = f"{a / 1e6:,.1f}MM" if a >= 1e6 else (f"{a / 1e3:,.0f}K" if a >= 1e3 else f"{a:,.0f}")
        return ("-$" if v < 0 else "$") + s_

    def title(text, help=None):
        st.markdown(f"#### {esc(text)}", help=help)

    def card_title(card):
        title(mc_cards.CARDS[card]["name"].split(" - ", 1)[-1])

    def table(df, money=(), pct=(), mxn=(), ints=(), hide=(), order=None, height="auto", **kw):
        cfg = {}
        for c in df.columns:
            if c in hide:
                cfg[c] = None
            elif c in money and df[c].isna().any():
                cfg[c] = st.column_config.Column(lab(c) + " ($)")
            elif c in money:
                cfg[c] = st.column_config.NumberColumn(lab(c) + " ($)", format="localized")
            elif c in pct:
                cfg[c] = st.column_config.NumberColumn(lab(c), format="%.2f%%")
            elif c in mxn:
                cfg[c] = st.column_config.NumberColumn(lab(c), format="%,d")
            elif c in ints:
                cfg[c] = st.column_config.NumberColumn(lab(c), format="%,d")
            else:
                cfg[c] = st.column_config.Column(lab(c))
        d = df.copy()
        for c in pct:
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce") * 100
        for c in list(money) + list(mxn):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce").round(0)
                if c in money and d[c].isna().any():
                    d[c] = d[c].map(lambda v: "-" if pd.isna(v) else f"{v:,.0f}")
        return st.dataframe(d, column_config=cfg, column_order=order, hide_index=True, width="stretch",
                            height=height, **kw)

    def style(fig, height=340, pct=False, money=False, legend=True, y0=False):
        fig.update_layout(height=height, margin=dict(l=8, r=8, t=30 if legend else 8, b=8), template="plotly_dark",
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          hovermode="x unified", showlegend=legend,
                          legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, title_text=""))
        fig.update_xaxes(title_text="")
        fig.update_yaxes(title_text="", tickformat=".1%" if pct else None, tickprefix="$" if money else None,
                         rangemode="tozero" if y0 else "normal")
        return fig

    def lines(df, x, ys, pct=False, money=False, colors=None, widths=None, dashes=None, height=340, y0=False):
        fig = go.Figure()
        for i, y in enumerate(ys):
            fig.add_trace(go.Scatter(x=df[x], y=df[y], name=lab(y), mode="lines+markers", marker=dict(size=5),
                                     line=dict(color=(colors or {}).get(y), width=(widths or {}).get(y, 2),
                                               dash=(dashes or {}).get(y))))
        return style(fig, height, pct, money, y0=y0)

    def show(fig):
        st.plotly_chart(fig, width="stretch", config={"displaylogo": False})

    # ---------- sidebar ----------
    with st.sidebar:
        st.header("Mission Control")
        as_of = st.date_input("Snapshot as of (blank = latest)", value=None) if MODE == "live" else None
        if st.button("Refresh data", width="stretch"):
            st.cache_data.clear()
        if MODE == "files":
            pub = META.get("published_at")
            try:
                p = dt.datetime.fromisoformat(pub)
                pub_txt = p.strftime("%a %b %d, %I:%M %p").replace(" 0", " ") + " PT"
            except Exception:
                pub_txt = pub or "unknown"
            st.caption(f"Hosted copy, published {pub_txt} from the Redshift snapshot of "
                       f"{META.get('snapshot_as_of', 'n/a')}.")
        else:
            st.caption("Live Redshift, cached 15 min.")
        st.caption("Sources: NB workbook, CIM MX master, servicer-report Exhibit B, receivable forecast.")
    base = {"as_of": as_of} if as_of else {}

    fac = run("facility_table", **base)
    hm = run("heatmap", **base)
    if not fac.empty:
        fac = fac[fac["borrowing_base"].notna()].reset_index(drop=True)
        if not hm.empty:
            hm = hm[hm["facility"].isin(fac["facility"])].reset_index(drop=True)
    snap = fac["data_as_of"].iloc[0] if not fac.empty else "n/a"
    bb_facs = fac.loc[fac["borrowing_base"].notna(), "facility"].tolist() if not fac.empty else ["NB"]

    # ---------- assistant (same pattern as the Cash Planner assistant: Claude + tools, audited layout writes) ----------
    with st.sidebar:
        st.divider()
        st.subheader("Assistant")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            st.caption("Assistant off: ANTHROPIC_API_KEY is not set for this copy (Streamlit app settings > Secrets).")
        else:
            if LAYOUT.readonly:
                st.caption("Layout changes are off here until MC_GITHUB_TOKEN is in the app secrets; questions work.")
            st.caption("Ask any what-if - e.g. \"NB BB without BR cards\", \"what if our top 3 Brazil obligors "
                       "go 30+ DPD\", \"cap every obligor at $3MM\" - or change the page. \"Save that\" shares it.")
            msgs = st.session_state.setdefault("mc_chat", [])
            for m_ in msgs[-20:]:
                with st.chat_message(m_["role"]):
                    st.markdown(esc(m_["content"]))
            ask = st.chat_input("Ask or change the dashboard", key="mc_ask")
            if ask:
                def _fetch(card, p):
                    if card not in mc_cards.CARDS or card.startswith("_"):
                        raise ValueError(f"unknown card {card}")
                    return q(card, tuple(sorted(p.items())), META.get("published_at", ""))
                cols_ = [c for c in ["facility", "status", "commitment", "drawn", "borrowing_base", "availability",
                                     "eligible", "binding_constraint"] if c in fac.columns]
                ctx_ = (f"Snapshot: {snap}\nFacility table:\n" + (fac[cols_].to_csv(index=False) if not fac.empty else "n/a"))
                _cur = st.session_state.get("mc_scn")
                if _cur:  # lets "save that" save the exact code already run, without re-running
                    ctx_ += f"\nScenario currently on the Scenarios tab: {_cur['name']}\nIts code:\n{_cur['code']}\n"
                with st.spinner("Working..."):
                    try:
                        who_ = getattr(getattr(st, "user", None), "email", None) or "brian"
                        reply, notes = mc_assistant.chat(msgs, ask, LAYOUT, _fetch, ctx_,
                                                         actor=f"{who_} via assistant ({'hosted' if MODE == 'files' else 'local'})",
                                                         scenario=scenario_tool)
                    except Exception as e_:
                        reply, notes = f"Assistant error: {str(e_)[:300]}", []
                if notes:
                    reply = (reply + "\n\n" if reply else "") + "\n".join("- " + n_ for n_ in notes)
                msgs += [{"role": "user", "content": ask}, {"role": "assistant", "content": reply or "Done."}]
                st.rerun()
            hidden_ = [T(k) for k in LAYOUT.d["hidden"]]
            if hidden_:
                st.caption("Hidden: " + ", ".join(hidden_))
            if hidden_ or LAYOUT.d["titles"]:
                if st.button("Reset layout", width="stretch"):
                    try:
                        LAYOUT.change("reset button", "reset_layout", reset=True)
                    except Exception as e_:
                        st.error(esc(f"Reset failed: {str(e_)[:200]}"))
                    else:
                        st.rerun()

    st.title("Capital Markets Mission Control")
    st.caption(esc(f"Snapshot: {snap}"))

    # ---------- headline cards ----------
    if on("kpis") and not fac.empty:
        cols = st.columns(len(fac))
        for c, (_, r) in zip(cols, fac.iterrows()):
            avail = r["availability"]
            deficit = (pd.notna(avail) and avail < 0) or "DEFICIT" in str(r["binding_constraint"]).upper()
            if pd.notna(r["borrowing_base"]):
                cells = [("Borrowing base", mm(r["borrowing_base"]), False), ("Drawn", mm(r["drawn"]), False),
                         ("Availability", mm(avail), pd.notna(avail) and avail < 0)]
            else:
                cells = [("Commitment", mm(r["commitment"]), False), ("Drawn", mm(r["drawn"], "n/a"), False),
                         ("Borrowing base", "n/a", False)]
            status = str(r["status"])
            short_status = status.split(" - ")[-1].capitalize() if " - " in status else status
            pill = ("bad", "Deficit") if deficit else (("ok", status) if status.startswith("Active")
                                                        else ("info", short_status))
            grid = "".join(f'<div><div class="l">{l}</div><div class="v{" neg" if n else ""}">{h(v)}</div></div>'
                           for l, v, n in cells)
            sub = f"Binding: <b>{h(r['binding_constraint'] or '-')}</b>"
            if deficit or pill[1] != status:
                sub += f" &middot; {h(status)}"
            with c:
                st.markdown(f'<div class="kpi{" bad" if deficit else ""}"><div class="kpi-h"><span class="nm">'
                            f'{h(r["facility"])}</span><span class="pill {pill[0]}">{h(pill[1])}</span></div>'
                            f'<div class="kpi-g">{grid}</div><div class="sub">{sub}</div></div>',
                            unsafe_allow_html=True)

    # ---------- alerts + headroom strip ----------
    if on("alerts") and not hm.empty:
        for _, r in hm[hm["status"] == "BREACH"].iterrows():
            st.markdown(f'<div class="alert"><b>BREACH - {h(r["facility"])}: {h(r["test"])}.</b> '
                        f'{h(r["current"])} vs {h(r["threshold"])} ({h(str(r["distance"]).replace("$-", "-$"))}). '
                        f'{h(r["consequence"])}</div>', unsafe_allow_html=True)
        key = hm[hm["status"].isin(["EXCESS", "WATCH"]) |
                 hm["test"].str.contains("DQ30 Rate, 3-mo", case=False, na=False)]
        if not key.empty:
            chips = "".join(
                f'<div class="chip {h(r["status"])}"><span class="t">{h(r["status"])}</span>{h(r["facility"])} - '
                f'{h(r["test"])}: <b>{h(r["current"])}</b> vs {h(r["threshold"])}</div>'
                for _, r in key.iterrows())
            st.markdown(f'<div class="chips">{chips}</div>', unsafe_allow_html=True)

    TAB_IDS = [k for k in ("tab_facilities", "tab_scenarios", "tab_waterfall", "tab_triggers", "tab_credit",
                           "tab_forward") if on(k)]
    TABS = dict(zip(TAB_IDS, st.tabs([T(k) for k in TAB_IDS])))

    # ---------- 1. facilities ----------
    if on("tab_facilities"):
        with TABS["tab_facilities"]:
            if on("facility_table"):
                title(T("facility_table"))
                if not fac.empty:
                    all_cols = st.toggle("Show all columns", value=False, key="fac_all")
                    core = ["facility", "lender", "status", "commitment", "drawn", "borrowing_base", "availability",
                            "bb_excess_deficit", "advance_rate", "binding_constraint", "cost", "draw_period_end", "maturity"]
                    table(fac, money=["commitment", "drawn", "borrowing_base", "availability", "undrawn_commitment",
                                      "bb_excess_deficit", "gross_receivables", "eligible", "excess_concentration", "cash_bb"],
                          pct=["advance_rate"], order=None if all_cols else core)
            if on("bb_trend"):
                title(T("bb_trend"))
                f = st.radio("Facility", bb_facs, horizontal=True, key="bbf", label_visibility="collapsed")
                bt = run("bb_trend", facility_req=f)
                if len(bt) < 2:
                    st.info(f"{f} history starts {bt['as_of'].iloc[0] if not bt.empty else 'n/a'} - the trend fills in as "
                            "daily snapshots accumulate.")
                else:
                    ys = ["borrowing_base", "eligible", "cash_bb", "commitment", "drawn", "availability"]
                    ys = [y for y in ys if y in bt.columns]
                    show(lines(bt, "as_of", ys, money=True,
                               colors=dict(borrowing_base=C["navy"], eligible=C["grey"], cash_bb=C["purple"],
                                           commitment=C["ink"], drawn=C["orange"], availability=C["teal"]),
                               dashes=dict(eligible="dot", commitment="dash", cash_bb="dot"), widths=dict(borrowing_base=3),
                               y0=True))
                    st.caption("Borrowing base = eligible receivables less excess concentration, times the advance rate, plus "
                               "controlled-account cash at 95%. Cash sits outside eligible receivables, so the base can exceed "
                               "eligible when cash in the controlled accounts is high.")
            if on("recycling"):
                title(T("recycling"))
                rec = run("recycling")
                table(rec, mxn=[c for c in rec.columns if c.endswith("_mxn")])

    # ---------- 2. waterfall ----------
    # ---------- 2. scenarios: any what-if, actual alongside ----------
    if on("tab_scenarios"):
        with TABS["tab_scenarios"]:
            saved = {k: v for k, v in LAYOUT.d.get("scenarios", {}).items() if (v.get("spec") or {}).get("code")}
            cur = st.session_state.get("mc_scn")
            opts = ([cur["name"]] if cur and cur["name"] not in saved else []) + list(saved)
            if not _sp.exists():
                st.info("Scenario data not published yet - it appears after the next daily snapshot.")
            elif not opts:
                st.info("No scenarios yet. Ask the assistant in the sidebar any what-if, e.g. \"NB BB without BR "
                        "cards\" or \"what if our top 3 Brazil obligors stop paying\".")
            else:
                if st.session_state.get("scn_pick_next") in opts:  # set by Save (widget can't be edited after render)
                    st.session_state["scn_pick"] = st.session_state.pop("scn_pick_next")
                if st.session_state.get("scn_pick") not in opts:
                    st.session_state["scn_pick"] = opts[0]
                pick = st.selectbox("Scenario", opts, key="scn_pick",
                                    help="Ask the assistant for any what-if; saved ones are shared with the hosted copy "
                                         "and re-run on each day's data.")
                spx = saved[pick]["spec"] if pick in saved else {"code": cur["code"], "question": cur.get("question", "")}
                with st.spinner("Running scenario..."):
                    out = whatif(spx["code"], SCN_VER)
                if not out.get("ok") or not out.get("scenario"):
                    st.error(esc("Scenario did not run on today's data: " + (out.get("error") or "no scenario set")[-400:]))
                else:
                    am, smt = out["actual"]["metrics"], out["scenario"]["metrics"]
                    if spx.get("question"):
                        st.caption(esc(f"Question: {spx['question']}"))
                    k = st.columns(4)
                    for col, (key, lbl) in zip(k[:3], [("borrowing_base", "Borrowing base"),
                                                      ("bb_excess", "BB excess / (deficit)"), ("availability", "Availability")]):
                        col.metric(lbl, mm(smt[key]), mm(smt[key] - am[key]) + " vs actual")
                    k[3].metric("Binding constraint", smt["binding_constraint"].replace(" (undrawn DDTL)", ""))
                    c1, c2 = st.columns([5, 4])
                    with c1:
                        title("Actual vs scenario")
                        tb = pd.DataFrame([{"line": lbl, "actual": scn_fmt(key, am.get(key)),
                                            "scenario": scn_fmt(key, smt.get(key)),
                                            "change": scn_change(key, am.get(key), smt.get(key))}
                                           for key, lbl in nb_scenario.COMPARE])
                        st.dataframe(tb, hide_index=True, width="stretch", height=38 + 35 * len(tb),
                                     column_config={c: st.column_config.Column(c.capitalize()) for c in tb.columns})
                    with c2:
                        title("Scenario: top 10 obligors (EIN groups)")
                        to = pd.DataFrame(out["scenario"]["top_obligors"])
                        if not to.empty:
                            table(to[["name", "country", "net", "pct_of_eligible"]], money=["net"],
                                  pct=["pct_of_eligible"], height=38 + 35 * len(to))
                    st.caption(esc(f"Receivables: NB tape {out['scal']['as_of']}; cash and borrowings: "
                                   f"{out['scal']['cash_source']}. Actual = the certificate calculation, ties to the "
                                   "Facilities tab."))
                    rs_ = out.get("result")
                    if isinstance(rs_, dict) and "_frame" in rs_:
                        fr = rs_["_frame"]
                        st.dataframe(pd.DataFrame(fr["data"], columns=fr["columns"]), hide_index=True, width="stretch")
                    with st.expander("How this was computed"):
                        st.code(spx["code"], language="python")
                    if not LAYOUT.readonly:
                        a1, b1, b2 = st.columns([3, 1, 1])
                        nm_ = a1.text_input("Save as", placeholder="Name to share this scenario", key="scn_name",
                                            label_visibility="collapsed")
                        if b1.button("Save", disabled=not nm_, width="stretch"):
                            LAYOUT.save_scenario("scenario tab", nm_.strip()[:60], spx)
                            st.session_state["scn_pick_next"] = nm_.strip()[:60]
                            st.rerun()
                        if pick in saved and b2.button("Delete", width="stretch"):
                            LAYOUT.delete_scenario("scenario tab", pick)
                            st.rerun()

    if on("tab_waterfall"):
        with TABS["tab_waterfall"]:
            f = st.radio("Facility", bb_facs, horizontal=True, key="wff")
            if on("waterfall"):
                steps = run("waterfall_steps", facility_req=f, **base)
                title(T("waterfall"))
                if not steps.empty:
                    s = steps[(steps["kind"] == "total") | (steps["amount_usd"].abs() > 0.5)].reset_index(drop=True)
                    measure = ["absolute" if k == "total" and i == 0 else ("total" if k == "total" else "relative")
                               for i, k in enumerate(s["kind"])]

                    def wrap(t, n=12):
                        t = t.lstrip("-+ ").strip()
                        out, line = [], ""
                        for w in t.split():
                            if len(line) + len(w) > n and line:
                                out.append(line)
                                line = w
                            else:
                                line = (line + " " + w).strip()
                        out.append(line)
                        return "<br>".join(out)

                    fig = go.Figure(go.Waterfall(
                        x=[wrap(t) for t in s["step"]], y=s["amount_usd"], measure=measure,
                        text=[mm(v) for v in s["amount_usd"]], textposition="outside",
                        totals=dict(marker=dict(color=C["navy"])), decreasing=dict(marker=dict(color=C["red"])),
                        increasing=dict(marker=dict(color=C["teal"])), connector=dict(line=dict(color="#4a5263"))))
                    style(fig, 500, money=True, legend=False)
                    fig.update_xaxes(tickangle=0, tickfont=dict(size=11))
                    fig.update_yaxes(range=[0, float(s["amount_usd"].clip(lower=0).max()) * 1.12])
                    show(fig)
                    st.caption("Blue = subtotal, red = deduction, green = addition. Zero-dollar steps are hidden.")
            reason = None
            c1, c2 = st.columns([5, 7])
            if on("reasons"):
                with c1:
                    title(T("reasons"))
                    rs = run("reasons", facility_req=f, **base)
                    st.caption("Click a row to filter the loan list.")
                    ev = table(rs, money=["balance_usd"], ints=["loans"], order=["reason", "balance_usd", "loans"],
                               on_select="rerun",
                               selection_mode="single-row", key=f"rs_{f}")
                    sel = ev.selection.rows if ev is not None and hasattr(ev, "selection") else []
                    reason = rs["reason"].iloc[sel[0]] if sel and not rs.empty else None
            if on("loans"):
                with c2:
                    title(T("loans") + (f": {reason}" if reason else ""))
                    a, b = st.columns([3, 1])
                    company = a.text_input("Company name contains", placeholder="e.g. Kalunga…")
                    min_dpd = b.number_input("Min DPD", 0, 999, 0)
                    ln = run("loans", facility_req=f, reason=reason, company=company or None, min_dpd=min_dpd or None, **base)
                    table(ln, money=["balance_usd"], hide=["company_id", "loan_id", "as_of"], height=380,
                          order=["name", "balance_usd", "dpd", "country", "reason"])
                    st.download_button("Download loans (CSV)", ln.to_csv(index=False),
                                       file_name=f"Loans - {f} - {str(snap)[:30]}.csv")
            if f == "NB" and on("obligors"):
                title(T("obligors"))
                ob = run("obligors", **base)
                if not ob.empty:
                    ob = ob.assign(pct_of_eligible=ob["pct_of_eligible"] / 100, cumulative_pct=ob["cumulative_pct"] / 100)
                table(ob, money=["balance", "credit_support", "net"], pct=["pct_of_eligible", "cumulative_pct"])

    # ---------- 3. triggers ----------
    if on("tab_triggers"):
        with TABS["tab_triggers"]:
            if on("heatmap_table"):
                title(T("heatmap_table"))
                colors = {"BREACH": "rgba(224,93,93,.28)", "EXCESS": "rgba(240,140,46,.24)", "WATCH": "rgba(242,201,76,.18)",
                          "NO DATA": "rgba(139,149,167,.18)", "OK": "rgba(52,168,83,.12)", "INFO": "rgba(76,155,255,.15)"}
                if not hm.empty:
                    ff = st.multiselect("Facility", sorted(hm["facility"].unique()), default=sorted(hm["facility"].unique()))
                    view = hm[hm["facility"].isin(ff)].copy()
                    for c in ["current", "threshold", "distance"]:
                        view[c] = view[c].fillna("-").astype(str).str.replace("$-", "-$", regex=False)
                    view["availability_lost_if_breached"] = view["availability_lost_if_breached"].round(0)
                    cfg = {c: st.column_config.Column(lab(c)) for c in view.columns}
                    cfg["availability_lost_if_breached"] = st.column_config.NumberColumn(
                        lab("availability_lost_if_breached") + " ($)", format="localized")
                    cfg["test"] = st.column_config.Column("Test", width="large")
                    cfg["consequence"] = st.column_config.Column("Consequence", width="large")
                    st.dataframe(view.style.apply(lambda r: [f"background-color:{colors.get(r['status'], '')}"] * len(r),
                                                  axis=1),
                                 column_config=cfg, hide_index=True, width="stretch", height=min(38 + 35 * len(view), 1100),
                                 column_order=["status", "facility", "test", "current", "threshold", "distance",
                                               "availability_lost_if_breached", "consequence", "source"])
            if on("test_trend"):
                title(T("test_trend"))
                fl = sorted(hm["facility"].unique()) if not hm.empty else ["NB"]
                a, b = st.columns(2)
                f = a.selectbox("Facility", fl, index=fl.index("NB") if "NB" in fl else 0, key="ttf")
                tt_all = run("test_trend", facility_req=f)
                tests = sorted(tt_all["test"].unique()) if not tt_all.empty else []
                if tests:
                    dq = [t for t in tests if "DQ30 Rate" in t]
                    tsel = b.selectbox("Test", tests, index=tests.index(dq[0]) if dq else 0, key="tts")
                    d = tt_all[tt_all["test"] == tsel]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=d["as_of"], y=d["actual"], name="Actual", mode="lines+markers",
                                             line=dict(color=C["navy"], width=3)))
                    fig.add_trace(go.Scatter(x=d["as_of"], y=d["threshold"], name="Threshold", mode="lines",
                                             line=dict(color=C["red"], dash="dash")))
                    show(style(fig, 340, pct=True, y0=True))
                else:
                    st.info(f"No percentage tests with history for {f}.")

    # ---------- 4. credit quality ----------
    if on("tab_credit"):
        with TABS["tab_credit"]:
            pool = st.radio("Pool", ["NB", "MX", "All"], horizontal=True, key="pool",
                            help="NB = US/CO/BR receivables; MX = Mexico; All = whole book")
            st.caption("Live from capital_markets_dm.loc_tape.")
            c1, c2 = st.columns(2)
            if on("dq_mix"):
                with c1:
                    title(T("dq_mix"))
                    d = run("dq_mix", pool=pool)
                    if not d.empty:
                        show(lines(d, "dt", ["dq30_plus", "dpd_1_30", "dpd_31_60", "dpd_61_90", "dpd_90_plus"], pct=True,
                                   colors=dict(dq30_plus=C["navy"], dpd_1_30=C["light"], dpd_31_60=C["orange"],
                                               dpd_61_90=C["red"], dpd_90_plus=C["purple"]),
                                   widths=dict(dq30_plus=3.5), y0=True).update_traces(
                                       visible="legendonly", selector=dict(name="1-30 DPD")))
                        st.caption("1-30 DPD is hidden so the 30+ buckets are readable - click it in the legend to add it.")
            if on("epd"):
                with c2:
                    title(T("epd"))
                    d = run("epd", pool=pool)
                    if not d.empty:
                        fig = make_subplots(specs=[[{"secondary_y": True}]])
                        fig.add_trace(go.Bar(x=d["cohort_month"], y=d["new_obligors"], name="New obligors (right axis)",
                                             marker_color="#323a4b"), secondary_y=False)
                        fig.add_trace(go.Scatter(x=d["cohort_month"], y=d["epd_rate"], name="EPD rate (left axis)",
                                                 mode="lines+markers", line=dict(color=C["red"], width=3)), secondary_y=True)
                        style(fig, 340, y0=True)
                        fig.update_yaxes(showgrid=False, rangemode="tozero", side="right", secondary_y=False)
                        fig.update_yaxes(tickformat=".0%", rangemode="tozero", side="left", showgrid=True, secondary_y=True)
                        show(fig)
            if on("roll_matrix"):
                title(T("roll_matrix"))
                rm = run("roll_matrix", pool=pool)
                if not rm.empty:
                    rm = rm.copy()
                    rm["month_end"] = pd.to_datetime(rm["month_end"])
                    months = sorted(rm["month_end"].unique(), reverse=True)
                    opts = ["Average, last 3 months", "Average, last 6 months", "Average, last 12 months"] + \
                        [f"{pd.Timestamp(m):%b %Y}" for m in months]
                    per = st.selectbox("Period (month the balance landed in)", opts, index=0, key="rm_per")
                    if per.startswith("Average"):
                        n = int(per.split("last ")[1].split()[0])
                        sel = rm[rm["month_end"].isin(months[:n])]
                    else:
                        sel = rm[rm["month_end"].dt.strftime("%b %Y") == per]
                    n_m = max(sel["month_end"].nunique(), 1)
                    names = {"C": "Current", "B1": "1-30", "B2": "31-60", "B3": "61-90", "B4": "90+", "CO": "Charged off"}
                    rows_, cols_ = ["C", "B1", "B2", "B3", "B4"], ["C", "B1", "B2", "B3", "B4", "CO"]
                    pv = sel.pivot_table(index="b0", columns="b1", values="balance_usd", aggfunc="sum").reindex(
                        index=rows_, columns=cols_).fillna(0)
                    share = pv.div(pv.sum(axis=1).replace(0, float("nan")), axis=0)
                    txt = share.map(lambda v: "" if pd.isna(v) else f"{v:.1%}")
                    fig = go.Figure(go.Heatmap(z=share.values, x=[names[c] for c in cols_], y=[names[r] for r in rows_],
                                               text=txt.values, texttemplate="%{text}", colorscale=HEAT, textfont=dict(color="#f1f3f7"), zmin=0, zmax=1,
                                               showscale=False, hovertemplate="%{y} → %{x}: %{text}<extra></extra>"))
                    style(fig, 330, legend=False)
                    fig.update_yaxes(autorange="reversed", title_text="From (start of month)")
                    fig.update_xaxes(side="top", title_text="To (end of month)")
                    fig.update_layout(hovermode="closest")
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        show(fig)
                    with c2:
                        st.markdown("**Opening balance by bucket**" + (" (monthly average)" if n_m > 1 else ""))
                        ob = (pv.sum(axis=1) / n_m).rename(index=names).reset_index()
                        ob.columns = ["bucket", "balance_usd"]
                        table(ob, money=["balance_usd"])
                    st.caption("Each row sums to 100% of that bucket's opening balance (balance-weighted, month-end to month-end). "
                               "Averages pool the balances across months. Late buckets hold few loans, so single names swing them.")
            c1, c2 = st.columns(2)
            if on("vintage"):
                with c1:
                    title(T("vintage"))
                    v = run("vintage", pool=pool)
                    if not v.empty:
                        bench = "All vintages (benchmark)"
                        allv = sorted(x for x in v["vintage"].unique() if x != bench)
                        vint = st.multiselect("Vintages", allv, default=allv[-6:], key=f"vint_{pool}")
                        v = v[v["vintage"].isin(vint + [bench])]
                        pal = px.colors.sample_colorscale("Viridis", [0.25 + 0.75 * i / max(len(vint) - 1, 1)
                                                                    for i in range(len(vint))])
                        fig = go.Figure()
                        for vv, col in zip(vint, pal):
                            d = v[v["vintage"] == vv]
                            fig.add_trace(go.Scatter(x=d["mob"], y=d["cum_co_pct_peak"], name=vv, mode="lines",
                                                     line=dict(color=col, width=1.6)))
                        d = v[v["vintage"] == bench]
                        fig.add_trace(go.Scatter(x=d["mob"], y=d["cum_co_pct_peak"], name="Benchmark (all vintages)",
                                                 mode="lines", line=dict(color=C["ink"], width=3.5)))
                        style(fig, 380, pct=True)
                        fig.update_layout(hovermode="closest", legend=dict(orientation="v", y=1, x=1.02, yanchor="top"))
                        fig.update_xaxes(title_text="Months on book")
                        peaks = v.groupby("vintage")["cum_co_pct_peak"].max().dropna()
                        if len(peaks) > 2:
                            srt = peaks.sort_values(ascending=False)
                            if srt.iloc[0] > 2 * srt.iloc[1]:
                                cap = float(srt.iloc[1]) * 1.3
                                fig.update_yaxes(range=[0, cap])
                                st.caption(f"Off scale: {srt.index[0]} peaks at {srt.iloc[0]:.1%} (axis capped at {cap:.1%} "
                                           "so the other vintages are readable; hover or zoom out to see it).")
                        show(fig)
            if on("collections"):
                with c2:
                    title(T("collections"))
                    d = run("collections", pool=pool)
                    if not d.empty:
                        d = d.copy()
                        d["month"] = pd.to_datetime(d["month"])
                        cur = pd.Timestamp(dt.date.today().replace(day=1))
                        partial = d[d["month"] >= cur]
                        d = d[d["month"] < cur]
                        fig = make_subplots(specs=[[{"secondary_y": True}]])
                        fig.add_trace(go.Bar(x=d["month"], y=d["collections_usd"], name="Collections",
                                             marker_color=C["blue"]), secondary_y=False)
                        fig.add_trace(go.Scatter(x=d["month"], y=d["collections_to_opening_balance"],
                                                 name="Collections ÷ opening balance (right axis)", mode="lines+markers",
                                                 line=dict(color=C["orange"], width=3)), secondary_y=True)
                        style(fig, 380)
                        fig.update_yaxes(tickprefix="$", secondary_y=False)
                        fig.update_yaxes(tickformat=".0%", showgrid=False, secondary_y=True)
                        show(fig)
                        if not partial.empty:
                            p = partial.iloc[-1]
                            st.caption(esc(f"{p['month']:%b %Y} month-to-date not shown: {mm(p['collections_usd'])} collected "
                                           f"so far ({p['collections_to_opening_balance']:.0%} of opening balance)."))
            if on("risk_migration"):
                title(T("risk_migration"))
                m = run("risk_migration", pool=pool)
                if not m.empty:
                    tot = m.groupby("direction")["balance_usd"].sum()
                    cnt = m.groupby("direction")["obligors"].sum()
                    k = st.columns(4)
                    for col, dname in zip(k, ["Downgrade", "Stable", "Upgrade", "n/a"]):
                        col.metric(dname if dname != "n/a" else "New / unscored", mm(tot.get(dname, 0)))
                        col.caption(f"{int(cnt.get(dname, 0)):,} obligors")
                    order_r = [g for g in ["New", "A", "B", "C", "D", "F", "No Score"] if g in set(m["grade_90d_ago"])]
                    order_c = [g for g in ["A", "B", "C", "D", "F", "No Score"] if g in set(m["grade_now"])]
                    pv = m.pivot_table(index="grade_90d_ago", columns="grade_now", values="balance_usd", aggfunc="sum")
                    pv = pv.reindex(index=order_r, columns=order_c)
                    txt = pv.map(short)
                    fig = go.Figure(go.Heatmap(z=pv.values, x=order_c, y=order_r, text=txt.values, texttemplate="%{text}",
                                               colorscale=HEAT, textfont=dict(color="#f1f3f7"), showscale=False, hoverongaps=False,
                                               hovertemplate="90d ago %{y} → now %{x}: %{text}<extra></extra>"))
                    style(fig, 360, legend=False)
                    fig.update_yaxes(autorange="reversed", title_text="Grade 90 days ago")
                    fig.update_xaxes(side="top", title_text="Grade now")
                    fig.update_layout(hovermode="closest")
                    show(fig)
                    st.caption("JURS loss-rate grade, A = best. Right of the diagonal = downgrade, left = upgrade.")

    # ---------- 5. forward ----------
    if on("tab_forward"):
        with TABS["tab_forward"]:
            if MODE == "files":
                _rp = lambda n: pd.read_parquet(DATA / n) if (DATA / n).exists() else pd.DataFrame()
                act, fcs = _rp("actuals_series.parquet"), _rp("forecast_series.parquet")
                bq, bsum = _rp("forecast_bands.parquet"), _rp("forecast_band_summary.parquet")
            else:
                act = actuals_long(run("_actuals", _sql=ACTUALS_SQL))
                try:
                    fcs = forecast_series()
                    bq, bsum = forecast_bands()
                except Exception as e:
                    st.error(esc(f"Forecast workbook could not be read - {str(e)[:200]}."))
                    fcs, bq, bsum = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
            if act.empty or fcs.empty:
                st.info("Forecast data not published yet - it appears after the next daily publish.")
            else:
                act["dt"] = pd.to_datetime(act["dt"])
                fcs["date"] = pd.to_datetime(fcs["date"])
                groups = {"All countries": None, "NB pool (US+CO+BR)": ["US", "CO", "BR"], "US": ["US"], "CO": ["CO"],
                          "BR": ["BR"], "MX": ["MX"], "Other (CA + rest)": ["CA", "OTHER"]}
                a, b, c = st.columns([3, 2, 2])
                g = a.radio("Country", list(groups), horizontal=True, key="fc_c")
                prod = b.radio("Product", ["All", "Card", "Jeeves Pay"], horizontal=True, key="fc_p")
                hist = c.select_slider("History shown", ["3 months", "6 months", "12 months"], value="6 months", key="fc_h")
                ctry = groups[g]
                run_as_of = pd.Timestamp(fcs["run_as_of"].iloc[0])
                ser = fcs[fcs["country"] != "TOTAL"]
                if ctry:
                    ser = ser[ser["country"].isin(ctry)]
                    act_s = act[act["country"].isin(ctry)]
                else:
                    act_s = act
                if prod != "All":
                    ser, act_s = ser[ser["product"] == prod], act_s[act_s["product"] == prod]
                months = int(hist.split()[0])
                act_d = act_s[act_s["dt"] >= run_as_of - pd.DateOffset(months=months)].groupby("dt")["balance"].sum()
                fc_d = ser.groupby("date").agg(yhat=("yhat", "sum"), h=("h", "first"))
                sel_q = bq[(bq["group_country"] == g) & (bq["product"] == prod)].set_index("h") if not bq.empty else pd.DataFrame()
                if not sel_q.empty:
                    hi = fc_d["h"].clip(upper=int(sel_q.index.max())).astype(int)
                    fc_d["p10"] = fc_d["yhat"] * np.exp(sel_q["q10"].reindex(hi).values)
                    fc_d["p90"] = fc_d["yhat"] * np.exp(sel_q["q90"].reindex(hi).values)
                    r = bsum[(bsum["group_country"] == g) & (bsum["product"] == prod)].iloc[0]
                    band_txt = (f"empirical P10-P90 of this selection's own out-of-sample forecast misses at each horizon "
                                f"({int(r['paths_short'])} 40-day + {int(r['paths_long'])} full 26-week back-test paths; "
                                f"held-out days inside the band: {r['loo_coverage_p10_p90']:.0%}, target 80%)")
                else:
                    fc_d["p10"] = fc_d["p90"] = np.nan
                    band_txt = "no band - these series are carried flat by the model, so there are no forecast misses to measure"
                k = st.columns(4)
                last_act = act_d.iloc[-1] if len(act_d) else float("nan")
                k[0].metric(f"Actual, {act_d.index[-1]:%b %d}" if len(act_d) else "Actual", mm(last_act))
                for col, wk in zip(k[1:], [4, 13, 26]):
                    d = run_as_of + pd.Timedelta(weeks=wk)
                    if d in fc_d.index:
                        r = fc_d.loc[d]
                        col.metric(f"Forecast, {d:%b %d} (+{wk} wks)", mm(r["yhat"]))
                        col.caption(esc(f"P10-P90: {mm(r['p10'])} to {mm(r['p90'])}"))
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=fc_d.index, y=fc_d["p90"], line=dict(width=0), hoverinfo="skip", showlegend=False))
                fig.add_trace(go.Scatter(x=fc_d.index, y=fc_d["p10"], line=dict(width=0), fill="tonexty",
                                         fillcolor="rgba(76,155,255,.22)", name="P10-P90 band", hoverinfo="skip"))
                fig.add_trace(go.Scatter(x=act_d.index, y=act_d.values, name="Actual", mode="lines",
                                         line=dict(color=C["ink"], width=2)))
                fig.add_trace(go.Scatter(x=fc_d.index, y=fc_d["yhat"], name="Forecast", mode="lines",
                                         line=dict(color=C["blue"], width=2.5)))
                fig.add_vline(x=run_as_of, line=dict(color=C["grey"], dash="dot"))
                fig.add_annotation(x=run_as_of, y=1, yref="paper", text=f"Forecast run {run_as_of:%b %d}", showarrow=False,
                                   xanchor="left", yanchor="bottom", font=dict(size=11, color=C["grey"]))
                style(fig, 460, money=True)
                show(fig)
                wape = ser["holdout_wape"].dropna()
                st.caption(esc(f"Receivable balance (ex charge-offs and repayment plans) for {g}, {prod.lower() if prod != 'All' else 'all products'}. "
                               f"Forecast = pooled XGBoost by country x product, run {run_as_of:%Y-%m-%d} (retrained Mondays). "
                               f"Shaded = {band_txt}."))


if __name__ == "__main__":
    main()
