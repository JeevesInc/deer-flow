#!/usr/bin/env python3
"""
diligence_registry_refresh.py
==============================
Monthly job: crawl Capital Markets Drive folders, surface new/changed items,
and output a dated Refresh Summary for Brian to review.

Usage:
    python diligence_registry_refresh.py [--dry-run] [--no-upload]

What it does:
    1. Checks the last saved registry for known Drive IDs
    2. Crawls all active counterparty folders for new files
    3. Prints + saves a summary of NEW items and RECENT activity
    4. Rebuilds the dated registry Excel and uploads to Drive Debt/ root
    5. Logs a self-improvement episode

Folder IDs (from jeeves-capital-markets skill):
    DEBT_ROOT         = 1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU
    BBVA_DD           = 1pA5_GOqtHMTatJE5vIIYCwm-p742d5yT
    NB_DILIGENCE      = 19fmtr7f3714EGe9j8fYFBUHmZ7_aWRz0
    FP_DILIGENCE      = 1Z82iHprfIyXKdxNeuvwMUSiYXeOCH67X
    VISTA_ROOT        = 1ah1x2cD_wIBQrRku7xuLelS52-D0L3I8
    CIM_DILIGENCE     = 1bmZJORaHbvxqYeWAE-KCx4_cy4hZdtsE
    CIM_LEGAL         = 1bdqcBmngeKXBkUf5x5QR6zcggTA5Abuc
    COVALTO_ROOT      = 11v7G67k_XSGVXn7igUTRJlVNeojmcpZO
    GRAMERCY_ROOT     = 1k-R1fldUnw90kZpJCS7VR5Yu7SNu0TXn
    FASANARA_ROOT     = 125_p3cKygzuyh-dbcarZjMP9HI74ohhx
    FP_ROOT           = 1LdmMpCmQQ5Y1UUDoxNnAZ1toWIrytJp4
"""

import os, sys, json, subprocess, datetime, argparse, shutil

# Resolve sibling skills relative to this script so the job runs both inside
# the agent sandbox and from the gateway cron (where /mnt/ does not exist).
_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_SKILLS_DIR   = os.path.normpath(os.path.join(_SCRIPT_DIR, '..'))
DRIVE_SCRIPT  = os.path.join(_SKILLS_DIR, 'google-drive', 'list_drive_folder.py')
UPLOAD_SCRIPT = os.path.join(_SKILLS_DIR, 'google-drive', 'upload_to_drive.py')
DEBT_ROOT     = "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU"
# Stable state dir (not thread-specific): backend/.deer-flow/diligence
_DEFAULT_OUT  = os.path.normpath(os.path.join(_SKILLS_DIR, '..', '..', 'backend', '.deer-flow', 'diligence'))
OUTPUTS       = os.environ.get('DILIGENCE_OUTPUTS_PATH') or os.environ.get('OUTPUTS_PATH', _DEFAULT_OUT)
TODAY         = datetime.date.today()
DATE_STR      = TODAY.strftime('%Y%m%d')
DATE_ISO      = TODAY.isoformat()

# Folders to crawl: (label, folder_id, recursive?)
CRAWL_TARGETS = [
    ("BBVA",              "1pA5_GOqtHMTatJE5vIIYCwm-p742d5yT",  True),
    ("BBVA",              "12ns4FGnFiA6K3jH3h6cECJ2S8TD8irEf",  False),
    ("Neuberger Berman",  "19fmtr7f3714EGe9j8fYFBUHmZ7_aWRz0",  False),
    ("Neuberger Berman",  "18uJghRNqHmPLklxrRcMFl3as_JOB4Ss3",  False),
    ("Francisco Partners","1Z82iHprfIyXKdxNeuvwMUSiYXeOCH67X",  False),
    ("Francisco Partners","1LdmMpCmQQ5Y1UUDoxNnAZ1toWIrytJp4",  False),
    ("Vista Credit",      "1ah1x2cD_wIBQrRku7xuLelS52-D0L3I8",  False),
    ("CIM",               "1bmZJORaHbvxqYeWAE-KCx4_cy4hZdtsE",  False),
    ("CIM",               "1bdqcBmngeKXBkUf5x5QR6zcggTA5Abuc",   False),
    ("Covalto",           "11v7G67k_XSGVXn7igUTRJlVNeojmcpZO",  False),
    ("Gramercy",          "1k-R1fldUnw90kZpJCS7VR5Yu7SNu0TXn",  False),
    ("Fasanara",          "125_p3cKygzuyh-dbcarZjMP9HI74ohhx",   False),
    ("[Debt Root]",       DEBT_ROOT,                              False),
]


def list_folder(folder_id, recursive=False):
    cmd = [sys.executable, DRIVE_SCRIPT, folder_id]
    if recursive:
        cmd.append("--recursive")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return r.stdout
    except Exception as e:
        return f"ERROR: {e}"


def parse_files(raw):
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[folder]") or not line:
            continue
        if "(id:" in line and "modified:" in line:
            try:
                name = line.split("(id:")[0].strip()
                rest = line.split("(id:")[1]
                fid  = rest.split(",")[0].strip()
                mods = [p for p in rest.split(",") if "modified:" in p]
                mod  = mods[0].replace("modified:", "").strip().rstrip(")") if mods else ""
                items.append({"name": name, "id": fid, "modified": mod})
            except Exception:
                continue
    return items


def _download_latest_registry_from_drive():
    """Fetch the latest canonical registry Excel from the Drive Debt/ root.

    The Drive copy is the canonical diligence library — always prefer it over
    any local copy, which may be stale or thread-specific.
    """
    try:
        sys.path.insert(0, os.path.join(_SKILLS_DIR, '_shared'))
        from env_loader import load_env
        load_env()
        from google_auth import get_credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        import io

        service = build('drive', 'v3', credentials=get_credentials())
        res = service.files().list(
            q=f"'{DEBT_ROOT}' in parents and trashed=false and name contains 'Diligence Registry'",
            fields='files(id,name,modifiedTime)',
            orderBy='modifiedTime desc',
            pageSize=5,
        ).execute()
        files = res.get('files', [])
        if not files:
            return None
        latest = files[0]
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, service.files().get_media(fileId=latest['id']))
        done = False
        while not done:
            _, done = dl.next_chunk()
        dest = os.path.join(OUTPUTS, latest['name'])
        with open(dest, 'wb') as f:
            f.write(buf.getvalue())
        print(f"Downloaded canonical registry from Drive: {latest['name']}")
        return dest
    except Exception as e:
        print(f"  (could not download registry from Drive: {e})")
        return None


def load_known_ids():
    known = set()
    try:
        import openpyxl
        latest = _download_latest_registry_from_drive()
        if latest is None:
            cands = [f for f in os.listdir(OUTPUTS)
                     if f.startswith("Diligence Registry") and f.endswith(".xlsx")]
            if not cands:
                return known
            latest = os.path.join(OUTPUTS, sorted(cands)[-1])
        wb = openpyxl.load_workbook(latest, read_only=True)
        ws = wb["Master Registry"]
        for row in ws.iter_rows(min_row=3, values_only=True):
            if row and row[5]:
                known.add(str(row[5]).strip())
        wb.close()
    except Exception:
        pass
    return known


def main():
    parser = argparse.ArgumentParser(description="Monthly Diligence Registry Refresh")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    os.makedirs(OUTPUTS, exist_ok=True)
    print(f"\n=== Diligence Registry Refresh - {DATE_ISO} ===\n")

    known_ids    = load_known_ids()
    print(f"Known registry items: {len(known_ids)}")

    cutoff       = TODAY - datetime.timedelta(days=45)
    new_items    = []
    recent_items = []
    all_items    = []

    for (cp, folder_id, recursive) in CRAWL_TARGETS:
        print(f"  Crawling {cp} ({folder_id[:14]}...)...", end=" ", flush=True)
        raw   = list_folder(folder_id, recursive=recursive)
        items = parse_files(raw)
        print(f"{len(items)} files")
        for item in items:
            item["counterparty"] = cp
            all_items.append(item)
            if item["id"] not in known_ids:
                new_items.append(item)
                print(f"    [NEW] {item['name']} ({item['modified']})")
            try:
                if datetime.date.fromisoformat(item["modified"][:10]) >= cutoff:
                    recent_items.append(item)
            except Exception:
                pass

    # Duplicate detection (Brian 2026-06-10: duplicative items must always
    # be deconflicted, never listed as independent triage rows)
    from collections import defaultdict
    name_map = defaultdict(list)
    for item in all_items:
        name_map[item["name"].strip().lower()].append(item)
    dup_groups = {k: v for k, v in name_map.items() if len(v) > 1}
    dup_ids = {i["id"] for v in dup_groups.values() for i in v}

    print(f"\n{'=' * 55}")
    print(f"NEW (not in registry): {len(new_items)}")
    print(f"Duplicate groups:      {len(dup_groups)}")
    print(f"Recent (last 45d):     {len(recent_items)}")

    # Write summary report
    summary_path = os.path.join(OUTPUTS, f"Diligence Refresh Summary - {DATE_STR}.txt")
    with open(summary_path, "w") as f:
        f.write(f"Diligence Registry Refresh - {DATE_ISO}\n{'=' * 55}\n\n")
        f.write(f"Known registry items:  {len(known_ids)}\n")
        f.write(f"NEW items found:       {len(new_items)}\n")
        f.write(f"Recent (last 45d):     {len(recent_items)}\n\n")

        f.write("--- NEW ITEMS (add to Master Registry) ---\n")
        if new_items:
            for item in new_items:
                f.write(f"\n  [{item['counterparty']}] {item['name']}\n")
                f.write(f"    Drive ID:  {item['id']}\n")
                f.write(f"    Modified:  {item['modified']}\n")
                if item["id"] in dup_ids:
                    f.write(f"    Action:    DUPLICATE - deconflict to one canonical copy before adding\n")
                else:
                    f.write(f"    Action:    Add to Master Registry with Status, Owner, Notes\n")
        else:
            f.write("  (none - registry is current)\n")

        f.write("\n--- DUPLICATES TO DECONFLICT (pick one canonical copy, remove the rest) ---\n")
        if dup_groups:
            for name, copies in sorted(dup_groups.items()):
                f.write(f"\n  {copies[0]['name']}  ({len(copies)} copies)\n")
                for c in copies:
                    f.write(f"    [{c['counterparty']}] Drive ID: {c['id']}  Modified: {c['modified']}\n")
        else:
            f.write("  (none)\n")

        f.write("\n--- RECENT ACTIVITY (last 45 days) ---\n")
        for item in recent_items:
            marker = "[NEW] " if item["id"] not in known_ids else "      "
            f.write(f"  {marker}[{item['counterparty']}] {item['name']} | {item['modified']}\n")

        f.write("\n--- NEXT STEPS ---\n")
        f.write("  1. Review NEW items above and add to Master Registry\n")
        f.write("  2. Update BBVA Outstanding - verify which of 9 open items closed\n")
        f.write("  3. Update Counterparty Summary for any stage changes\n")
        f.write(f"  4. Re-upload registry Excel to Drive folder {DEBT_ROOT}\n")
        f.write("  5. Patch jeeves-diligence skill if new lessons emerged\n")

    print(f"\nSummary: {summary_path}")

    if args.dry_run:
        print("[DRY RUN] Done.")
        return

    # Rebuild registry (builder writes a dated file directly)
    builder = os.path.join(_SCRIPT_DIR, "build_diligence_registry.py")
    registry_path = os.path.join(OUTPUTS, f"Diligence Registry - Capital Markets - {DATE_STR}.xlsx")

    if os.path.exists(builder):
        print(f"\nRebuilding registry Excel...")
        env = dict(os.environ, OUTPUTS_PATH=OUTPUTS)
        result = subprocess.run([sys.executable, builder], capture_output=True, text=True, env=env)
        if result.returncode == 0 and os.path.exists(registry_path):
            print(f"  Built: {registry_path}")
        else:
            print(f"  Build error: {result.stderr or 'output file missing'}")
            registry_path = None
    else:
        print("  build_diligence_registry.py not found - skipping Excel rebuild")
        registry_path = None

    # Upload
    if registry_path and not args.no_upload and os.path.exists(UPLOAD_SCRIPT):
        print(f"\nUploading to Drive...")
        r = subprocess.run(
            [sys.executable, UPLOAD_SCRIPT, registry_path, "--folder", DEBT_ROOT],
            capture_output=True, text=True, timeout=120
        )
        print(f"  {'OK: ' + r.stdout.strip() if r.returncode == 0 else 'FAILED: ' + r.stderr.strip()}")

    # Log episode
    if new_items:
        manage = os.path.normpath(os.path.join(
            _SKILLS_DIR, '..', 'public', 'self-improving-agent', 'scripts', 'skill_manage.py'))
        if os.path.exists(manage):
            episode = json.dumps({
                "situation": f"Monthly refresh {DATE_ISO} found {len(new_items)} new Drive items",
                "lesson": f"{len(new_items)} new items catalogued. Run refresh monthly.",
                "date": DATE_ISO
            })
            subprocess.run([sys.executable, manage, "log", "jeeves-diligence",
                            "--episode", episode], capture_output=True)

    print(f"\n=== Refresh Complete ===")
    print(f"  New items:  {len(new_items)}")
    print(f"  Summary:    {summary_path}")
    print(f"  Registry:   {registry_path or 'not rebuilt'}")


if __name__ == "__main__":
    main()
