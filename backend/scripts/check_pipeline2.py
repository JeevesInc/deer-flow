import urllib.request, time

# Fetch metrics once with long timeout
try:
    with urllib.request.urlopen('http://localhost:8001/metrics', timeout=20) as r:
        body = r.read().decode()
except Exception as e:
    print(f"Error: {e}")
    raise

cm_lines = [l for l in body.split('\n') if 'jeeves_cm' in l and not l.startswith('#')]
print(f"jeeves_cm metrics ({len(cm_lines)} total):")
for l in cm_lines:
    name = l.split(' ')[0]
    val_str = l.split(' ')[-1]
    try:
        val = float(val_str)
        if val > 1_000_000:
            disp = f"${val/1e6:.1f}M"
        elif val > 1000:
            disp = f"{val:,.0f}"
        elif val < 0:
            disp = f"${val/1e6:.1f}M"
        else:
            disp = f"{val:.3f}"
    except:
        disp = val_str
    print(f"  {name}: {disp}")
