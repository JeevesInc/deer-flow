"""Render Mission Control card SQL (cfo-org-kb scripts/mission_control/mc_cards.py) for direct Redshift execution."""
import datetime as dt
import decimal
import re

OPT = re.compile(r"\[\[(.*?)\]\]", re.S)
VAR = re.compile(r"\{\{(\w+)\}\}")


def lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, decimal.Decimal)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return "'{}'".format(v.isoformat()[:10])
    return "'{}'".format(str(v).replace("'", "''"))


def render(sql, params):
    p = {k: v for k, v in (params or {}).items() if v not in (None, "")}

    def opt(m):
        body = m.group(1)
        names = VAR.findall(body)
        return VAR.sub(lambda x: lit(p[x.group(1)]), body) if all(n in p for n in names) else ""

    sql = OPT.sub(opt, sql)
    missing = [n for n in VAR.findall(sql) if n not in p]
    if missing:
        raise ValueError("required parameter(s) missing: " + ", ".join(sorted(set(missing))))
    return VAR.sub(lambda x: lit(p[x.group(1)]), sql)


if __name__ == "__main__":
    import os, sys, time
    import pandas as pd
    sys.path.insert(0, os.path.join(os.environ["SKILLS_PATH"], "custom", "jeeves-borrowing-base"))
    sys.path.insert(0, os.path.join(os.environ["SKILLS_PATH"], "custom", "cfo-org-kb", "scripts", "mission_control"))
    import redshift_util as ru
    import mc_cards
    conn = ru.connect()
    defaults = dict(facility_req="NB", pool="NB")
    only = sys.argv[1:]
    for k, c in mc_cards.CARDS.items():
        if only and k not in only:
            continue
        t = time.time()
        try:
            df = pd.read_sql(render(c["sql"], defaults), conn)
            print("OK  ", k, len(df), "rows", round(time.time() - t, 1), "s", list(df.columns))
            print("      ", df.head(2).values.tolist())
        except Exception as e:
            conn.rollback()
            print("FAIL", k, str(e)[:300])
