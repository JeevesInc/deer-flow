# VENDORED COPY -- canonical source is JeevesInc/airflow-dags
# aig-tex/scripts/tex_file_builder.py (PR #542, merged 2026-09-09).
# Copied here 2026-09-16 so report_scheduler_cron can build the weekly AIG/TEX
# file from this machine while the Airflow DAG waits on its two prerequisites
# (aronova_tex_sftp connection + apache-airflow-providers-sftp in the image).
# If the contract changes, change it in airflow-dags FIRST, then re-copy.
"""
AIG TEX invoice-history file builder.

Turns the raw result of aig-tex/queries/aig_invoice_data_v8_tex_validated.sql into the
pipe-delimited flat file Aronova's TEX platform ingests for AIG trade-credit reporting.

File contract agreed with Aronova (Annette McGinley) and validated by their UAT run on
2026-08-26/27:
  * 21 fields, pipe delimited, no quoting, no escape character
  * header row present, CRLF line terminators, UTF-8
  * amounts fixed 2dp text (TEX rejects binary float noise such as 250249.640000001)
  * dates MM/DD/YYYY, due date never earlier than issue date
  * alpha2code always a 2-letter ISO alpha-2 code
  * legalname and businessaddress capped at 80 characters
  * zipcode populated wherever known - it drives TEX's D&B DUNS auto-link, which gates
    the credit limit AIG assigns to each buyer
  * amounts are reported in the statement's own local currency (no FX conversion);
    the ISO currency code travels in the `currency` field, so no cross-row total is
    meaningful unless grouped by currency

The validation in validate_tex_file() is a hard gate: this file leaves Jeeves and lands in
a counterparty's platform, so the DAG must fail rather than upload a file that does not
satisfy the contract above.
"""

import csv
import os
import re
from datetime import datetime

import pandas as pd

# Exact field order Aronova expects. Do not reorder - TEX maps by position as well as name.
COLUMNS = [
    "legalname",
    "id",
    "id_number",
    "alpha2code",
    "statement_id",
    "currency",
    "invoicesentdatetime",
    "invoicedamount",
    "duedate",
    "settlementdatetime",
    "days_past_due",
    "unpaidamount",
    "cashpaidamount",
    "businessaddress",
    "zip",
    "city",
    "state",
    "country",
    "charge_off_flag",
    "is_in_repayment",
    "charge_off_dt",
]

TEXT_COLUMNS = [
    "legalname",
    "id_number",
    "alpha2code",
    "currency",
    "businessaddress",
    "zip",
    "city",
    "state",
    "country",
    "charge_off_dt",
]

AMOUNT_COLUMNS = ["invoicedamount", "unpaidamount", "cashpaidamount"]
BOOL_COLUMNS = ["charge_off_flag", "is_in_repayment"]
CAPPED_80 = ["legalname", "businessaddress"]

FILE_NAME_TEMPLATE = "Jeeves-Invoice-{stamp}.txt"


def build_file_name(when=None):
    """Aronova's expected name: Jeeves-Invoice-YYYYMMDD-HHMM.txt"""
    when = when or datetime.utcnow()
    return FILE_NAME_TEMPLATE.format(stamp=when.strftime("%Y%m%d-%H%M"))


def _clean(value):
    """Strip anything that would break a pipe-delimited, unquoted flat file."""
    if pd.isna(value):
        return ""
    text = str(value)
    text = (
        text.replace("|", " ")
        .replace('"', "")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


def build_tex_dataframe(df):
    """Apply the TEX formatting contract to the raw query result. Returns a new DataFrame."""
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError("query result is missing required TEX columns: {}".format(missing))

    out = df.loc[:, COLUMNS].copy()

    for col in TEXT_COLUMNS:
        out[col] = out[col].map(_clean)

    # Re-assert the 80-char caps AFTER sanitising (whitespace collapse can only shorten).
    for col in CAPPED_80:
        out[col] = out[col].str.slice(0, 80)

    for col in BOOL_COLUMNS:
        out[col] = out[col].map(
            lambda v: 1 if str(v).strip().lower() in ("true", "1", "1.0") else 0
        )

    # Amounts as fixed 2dp text, never a float repr.
    for col in AMOUNT_COLUMNS:
        out[col] = out[col].map(
            lambda v: "" if pd.isna(v) else "{:.2f}".format(round(float(v) + 0.0, 2))
        )

    return out.fillna("")


def write_tex_file(df, path):
    """Write the formatted DataFrame as the TEX flat file. Returns the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(
        path,
        sep="|",
        index=False,
        lineterminator="\r\n",
        quoting=csv.QUOTE_NONE,
        quotechar="",
        escapechar=None,
    )
    return path


def validate_tex_file(path, expected_fields=len(COLUMNS)):
    """Re-read the written file off disk and check every clause of the Aronova contract.

    Returns (errors, stats). An empty errors list means the file is safe to upload.
    """
    raw = open(path, "rb").read()
    text = raw.decode("utf-8")
    lines = text.split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    errors = []
    if not lines:
        return ["file is empty"], {"rows": 0, "bytes": len(raw), "fields": 0}

    header = lines[0].split("|")
    fields = len(header)
    if fields != expected_fields:
        errors.append("header has {} fields, expected {}".format(fields, expected_fields))
    if header != COLUMNS:
        errors.append("header does not match the agreed field order")

    bad_rows = [
        i for i, line in enumerate(lines[1:], start=2) if line.count("|") + 1 != fields
    ]
    if bad_rows:
        errors.append(
            "{} rows with wrong field count (first: line {})".format(len(bad_rows), bad_rows[0])
        )
    if b"\n" in raw.replace(b"\r\n", b""):
        errors.append("bare LF inside a field")
    if "\t" in text:
        errors.append("tab character present")
    if not raw.endswith(b"\r\n"):
        errors.append("missing trailing CRLF")
    if len(lines) < 2:
        errors.append("no data rows")

    index = {name: pos for pos, name in enumerate(header)}
    amount_bad, duedate_bad, country_bad, length_bad, zip_populated = [], [], [], [], 0

    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) != fields:
            continue
        for col in AMOUNT_COLUMNS:
            value = parts[index[col]]
            if value and not re.fullmatch(r"-?\d+\.\d{2}", value):
                amount_bad.append((parts[index["statement_id"]], col, value))
        issued, due = parts[index["invoicesentdatetime"]], parts[index["duedate"]]
        if issued and due:
            try:
                if datetime.strptime(due, "%m/%d/%Y") < datetime.strptime(issued, "%m/%d/%Y"):
                    duedate_bad.append((parts[index["statement_id"]], issued, due))
            except ValueError:
                errors.append(
                    "unparseable date on statement {}: {} / {}".format(
                        parts[index["statement_id"]], issued, due
                    )
                )
        if not re.fullmatch(r"[A-Z]{2}", parts[index["alpha2code"]] or ""):
            country_bad.append(parts[index["alpha2code"]])
        for col in CAPPED_80:
            if len(parts[index[col]]) > 80:
                length_bad.append((parts[index["statement_id"]], col, len(parts[index[col]])))
        if parts[index["zip"]].strip():
            zip_populated += 1

    if amount_bad:
        errors.append("{} amounts not exactly 2dp (first: {})".format(len(amount_bad), amount_bad[0]))
    if duedate_bad:
        errors.append(
            "{} rows with due date before issue date (first: {})".format(
                len(duedate_bad), duedate_bad[0]
            )
        )
    if country_bad:
        errors.append(
            "{} rows with a non alpha-2 country code (values: {})".format(
                len(country_bad), sorted(set(country_bad))[:5]
            )
        )
    if length_bad:
        errors.append("{} rows over the 80-char cap (first: {})".format(len(length_bad), length_bad[0]))

    rows = len(lines) - 1
    stats = {
        "rows": rows,
        "bytes": len(raw),
        "fields": fields,
        "zip_populated": zip_populated,
        "zip_populated_pct": round(100.0 * zip_populated / rows, 1) if rows else 0.0,
    }
    return errors, stats
