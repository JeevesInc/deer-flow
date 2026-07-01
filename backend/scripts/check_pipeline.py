"""Verify the full Capital Markets -> Prometheus -> Grafana pipeline is wired."""
import urllib.request, json

def check_metric(name):
    with urllib.request.urlopen('http://localhost:8001/metrics', timeout=15) as r:
        body = r.read().decode()
    lines = [l for l in body.split('\n') if l.startswith(name)]
    return lines

results = {
    'jeeves_cm_us_bridge_drawn_usd':        check_metric('jeeves_cm_us_bridge_drawn_usd'),
    'jeeves_cm_us_bridge_available_usd':     check_metric('jeeves_cm_us_bridge_available_usd'),
    'jeeves_cm_us_bridge_eligible_usd':      check_metric('jeeves_cm_us_bridge_eligible_usd'),
    'jeeves_cm_mx_sofom_collection_cash_mxn':check_metric('jeeves_cm_mx_sofom_collection_cash_mxn'),
    'jeeves_cm_portfolio_total_usd':         check_metric('jeeves_cm_portfolio_total_usd'),
    'jeeves_cm_portfolio_accounts':          check_metric('jeeves_cm_portfolio_accounts'),
    'jeeves_cm_cron_days_ago':               check_metric('jeeves_cm_cron_days_ago'),
}

print("Pipeline check:")
for k, v in results.items():
    status = "OK" if v else "MISSING"
    val = v[0].split(' ')[-1] if v else '(none)'
    print(f"  [{status}] {k}: {val}")
