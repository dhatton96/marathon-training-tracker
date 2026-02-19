"""
sync_strava.py  (v2)
--------------------
1. Refresh Strava access token
2. Fetch all Run activities from the API (since Nov 2025)
3. Merge with activities.csv (bulk export) -- deduplicate by activity ID
4. Write master_activities.csv
5. Analyse aerobic efficiency week-over-week
6. Predict marathon time (Riegel from HM PB + AE adjustment)
7. Write training_status.json for the website Live Status badge
8. Write last_synced.txt with current timestamp
9. Git add / commit / push updated data files

Run:  python sync_strava.py
"""

import csv, json, os, sys, io, urllib.request, urllib.parse, subprocess
from datetime import datetime, timezone, timedelta, date
from collections import defaultdict

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# -- Constants ----------------------------------------------------------------
PLAN_START     = date(2025, 12, 22)   # Mon 22 Dec 2025 = Week 1
TARGET_SECS    = 9300                 # 2:35:00
TARGET_DIST_M  = 42195
HM_PB_SECS     = 4380                 # 1:13:00
HM_DIST_M      = 21097.5

# -- .env loader --------------------------------------------------------------
def load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        print(f"ERROR: {path} not found.")
        sys.exit(1)
    return env

# -- OAuth --------------------------------------------------------------------
def get_access_token(env):
    data = urllib.parse.urlencode({
        "client_id":     env["STRAVA_CLIENT_ID"],
        "client_secret": env["STRAVA_CLIENT_SECRET"],
        "refresh_token": env["STRAVA_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://www.strava.com/oauth/token", data=data, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())
    token = payload.get("access_token")
    if not token:
        print("ERROR obtaining access token:", payload)
        sys.exit(1)
    print(f"  Access token obtained (expires in {payload.get('expires_in', '?')}s)")
    return token

# -- Strava API fetch ---------------------------------------------------------
def fetch_api_runs(token, after_dt):
    after_ts = int(after_dt.timestamp())
    activities, page = [], 1
    while True:
        params = urllib.parse.urlencode(
            {"after": after_ts, "per_page": 200, "page": page}
        )
        req = urllib.request.Request(
            f"https://www.strava.com/api/v3/athlete/activities?{params}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        runs = [a for a in batch if a.get("type") == "Run"]
        activities.extend(runs)
        print(f"    Page {page}: {len(batch)} activities, {len(runs)} runs")
        if len(batch) < 200:
            break
        page += 1
    return activities

def norm_api(a):
    return {
        "activity_id":  str(a["id"]),
        "date":         a["start_date_local"][:10],
        "name":         a.get("name", ""),
        "distance_km":  round(a.get("distance", 0) / 1000, 2),
        "moving_secs":  int(a.get("moving_time", 0)),
        "avg_hr":       a.get("average_heartrate") or "",
    }

# -- Parse activities.csv (Strava bulk export) --------------------------------
def parse_export(path="activities.csv"):
    rows = []
    if not os.path.exists(path):
        print(f"  {path} not found, skipping.")
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) < 32 or row[3] != "Run":
                continue
            try:
                dt   = datetime.strptime(row[1].strip(), "%b %d, %Y, %I:%M:%S %p")
                date_str = dt.strftime("%Y-%m-%d")
                dist = round(float(row[6]), 2)
                secs = int(float(row[16])) if row[16] else 0
                hr   = float(row[31]) if row[31] else ""
            except Exception:
                continue
            rows.append({
                "activity_id": row[0].strip(),
                "date":        date_str,
                "name":        row[2].strip(),
                "distance_km": dist,
                "moving_secs": secs,
                "avg_hr":      hr,
            })
    return rows

# -- Merge & deduplicate ------------------------------------------------------
def merge(export_rows, api_rows):
    """
    Primary key: activity_id.
    API data overwrites export HR (API HR is more reliable).
    """
    master = {}  # activity_id -> row

    for r in export_rows:
        master[r["activity_id"]] = r.copy()

    for r in api_rows:
        if r["activity_id"] in master:
            # Update HR from API if we now have it
            if r["avg_hr"]:
                master[r["activity_id"]]["avg_hr"] = r["avg_hr"]
        else:
            # Fallback: deduplicate by (date, distance rounded to 0.1km)
            dup_key = (r["date"], round(r["distance_km"], 1))
            existing = next(
                (v for v in master.values()
                 if (v["date"], round(v["distance_km"], 1)) == dup_key),
                None,
            )
            if existing:
                if r["avg_hr"]:
                    existing["avg_hr"] = r["avg_hr"]
            else:
                master[r["activity_id"]] = r.copy()

    return sorted(master.values(), key=lambda x: x["date"])

# -- Helpers ------------------------------------------------------------------
def fmt_time(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def plan_week(date_str):
    d    = datetime.strptime(date_str, "%Y-%m-%d").date()
    days = (d - PLAN_START).days
    if days < 0:
        return None
    w = days // 7 + 1
    return w if 1 <= w <= 18 else None

def riegel(t1_secs, d1_m, d2_m=TARGET_DIST_M):
    return t1_secs * (d2_m / d1_m) ** 1.06

# -- Aerobic Efficiency Analysis ----------------------------------------------
def analyse(rows):
    # Only runs with HR data and ?5km
    hr_runs = [
        r for r in rows
        if r["avg_hr"] and r["moving_secs"] > 0 and r["distance_km"] >= 5
    ]

    # Group by plan week; calculate AE per run
    by_week = defaultdict(list)
    for r in hr_runs:
        w = plan_week(r["date"])
        if not w:
            continue
        speed = r["distance_km"] * 1000 / r["moving_secs"]   # m/s
        ae    = speed / float(r["avg_hr"]) * 1000             # m/s / bpm ? 1000
        by_week[w].append({
            "date":     r["date"],
            "speed":    speed,
            "hr":       float(r["avg_hr"]),
            "ae":       ae,
            "dist_km":  r["distance_km"],
        })

    wk_ae = {}
    for w, runs in sorted(by_week.items()):
        wk_ae[w] = {
            "ae":            round(sum(r["ae"] for r in runs) / len(runs), 4),
            "avg_hr":        round(sum(r["hr"] for r in runs) / len(runs), 1),
            "avg_pace_minkm": round(sum(1000/r["speed"]/60 for r in runs)/len(runs), 2),
            "n":             len(runs),
        }

    # AE trend: compare first half of weeks vs second half
    wks = sorted(wk_ae)
    ae_trend = "insufficient data"
    ae_pct   = None
    if len(wks) >= 2:
        mid         = len(wks) // 2
        first_ae    = sum(wk_ae[w]["ae"] for w in wks[:mid]) / mid
        second_ae   = sum(wk_ae[w]["ae"] for w in wks[mid:]) / (len(wks) - mid)
        ae_pct      = round((second_ae - first_ae) / first_ae * 100, 1)
        ae_trend    = ("improving" if ae_pct > 1
                       else "declining" if ae_pct < -1
                       else "stable")

    # Linear regression of HR vs speed to predict HR at marathon pace
    all_pts   = [r for runs in by_week.values() for r in runs]
    hr_at_mp  = None
    mp_speed  = TARGET_DIST_M / TARGET_SECS  # ? 4.537 m/s
    if len(all_pts) >= 3:
        n      = len(all_pts)
        sx     = sum(r["speed"] for r in all_pts)
        sy     = sum(r["hr"]    for r in all_pts)
        sxy    = sum(r["speed"] * r["hr"] for r in all_pts)
        sx2    = sum(r["speed"] ** 2      for r in all_pts)
        denom  = n * sx2 - sx * sx
        if denom:
            a       = (n * sxy - sx * sy) / denom
            b       = (sy - a * sx) / n
            hr_at_mp = round(a * mp_speed + b, 1)

    # Red zone = 90 % of estimated true max HR
    max_avg_hr  = max((float(r["avg_hr"]) for r in hr_runs), default=175)
    est_max_hr  = max_avg_hr * 1.12   # avg HR typically ~88% of max
    red_zone_hr = round(est_max_hr * 0.90, 1)
    hr_buffer   = round(red_zone_hr - hr_at_mp, 1) if hr_at_mp else None

    # Week-over-week AE delta (current vs previous)
    current_wk  = wks[-1] if wks else None
    prev_wk     = wks[-2] if len(wks) >= 2 else None
    ae_delta_wk = None
    if current_wk and prev_wk:
        ae_delta_wk = round(
            (wk_ae[current_wk]["ae"] - wk_ae[prev_wk]["ae"])
            / wk_ae[prev_wk]["ae"] * 100, 1
        )

    return {
        "by_week":        wk_ae,
        "ae_trend":       ae_trend,
        "ae_trend_pct":   ae_pct,
        "ae_delta_wk":    ae_delta_wk,
        "hr_at_mp":       hr_at_mp,
        "red_zone_hr":    red_zone_hr,
        "hr_buffer":      hr_buffer,
        "current_week":   current_wk,
    }

# -- Marathon Prediction ------------------------------------------------------
def predict(rows, analysis):
    """
    Baseline: Riegel from 1:13 HM PB.
    Modifier: +/-30s per % of AE trend (capped at +/-90s).
    """
    # Riegel from HM PB: most reliable predictor for a sub-elite runner.
    # Applying Riegel to training runs is unreliable (they are not race efforts).
    base_secs = riegel(HM_PB_SECS, HM_DIST_M)  # 4380 * 2^1.06 = ~9132s = 2:32:12

    # AE adjustment: each 1% improvement in AE -> ~15s faster; capped at +/-90s.
    ae_pct    = analysis.get("ae_trend_pct") or 0
    modifier  = max(-90, min(90, -ae_pct * 15))
    pred_secs = base_secs + modifier

    return round(pred_secs), None

def fmt_pace(speed_ms):
    sec_per_km = 1000 / speed_ms
    m, s = divmod(int(sec_per_km), 60)
    return f"{m}:{s:02d}/km"

# -- Build training_status.json -----------------------------------------------
def build_status(pred_secs, analysis, rows):
    delta = TARGET_SECS - pred_secs
    ph    = pred_secs // 3600
    pm    = (pred_secs % 3600) // 60
    ps    = pred_secs % 60
    pred_str = f"{ph}:{pm:02d}:{ps:02d}"

    if delta > 180:
        status = "AHEAD"
        label  = f"AHEAD OF TARGET \u2014 ON FOR {pred_str}"
    elif delta >= -60:
        status = "ON TRACK"
        label  = f"ON TRACK FOR {pred_str}"
    elif delta >= -300:
        status = "CLOSE"
        label  = f"CLOSE \u2014 {pred_str} PROJECTED"
    else:
        status = "OFF PACE"
        label  = f"NEEDS WORK \u2014 {pred_str} PROJECTED"

    # Week 9 specific breakdown
    wk9_runs = [r for r in rows if plan_week(r["date"]) == 9]
    wk9_km   = round(sum(r["distance_km"] for r in wk9_runs), 1)

    ae_wk = analysis["by_week"].get(analysis["current_week"])
    ae_label = ""
    if analysis["ae_delta_wk"] is not None:
        sign = "+" if analysis["ae_delta_wk"] >= 0 else ""
        ae_label = f"AE {sign}{analysis['ae_delta_wk']}% vs last week"

    return {
        "label":             label,
        "status":            status,
        "predicted_time":    pred_str,
        "predicted_seconds": int(pred_secs),
        "target_seconds":    TARGET_SECS,
        "delta_seconds":     int(delta),
        "ae_trend":          analysis["ae_trend"],
        "ae_trend_pct":      analysis["ae_trend_pct"],
        "ae_delta_wk":       analysis["ae_delta_wk"],
        "ae_label":          ae_label,
        "hr_at_mp":          analysis["hr_at_mp"],
        "hr_buffer":         analysis["hr_buffer"],
        "red_zone_hr":       analysis["red_zone_hr"],
        "current_week":      analysis["current_week"],
        "week9_km":          wk9_km,
        "last_updated":      datetime.now().strftime("%Y-%m-%d"),
    }

# -- Week 9 performance audit (printed to console) ---------------------------
def week9_audit(rows, analysis):
    print()
    print("=" * 60)
    print("WEEK 9 PERFORMANCE AUDIT")
    print("=" * 60)
    wk9 = [r for r in rows if plan_week(r["date"]) == 9]
    for r in sorted(wk9, key=lambda x: x["date"]):
        if r["moving_secs"] == 0:
            continue
        spd  = r["distance_km"] * 1000 / r["moving_secs"]
        pace = fmt_pace(spd)
        hr   = f"{r['avg_hr']} bpm" if r["avg_hr"] else "no HR"
        ae_str = ""
        if r["avg_hr"]:
            ae = spd / float(r["avg_hr"]) * 1000
            ae_str = f" | AE {ae:.3f}"
        print(f"  {r['date']}  {r['distance_km']:5.1f}km  {pace}  {hr}{ae_str}  {r['name']}")

    print()
    print("AEROBIC EFFICIENCY BY WEEK (runs with HR data):")
    for w in sorted(analysis["by_week"]):
        d = analysis["by_week"][w]
        bar = "#" * int(d["ae"] * 20)
        print(f"  Week {w:2d}: AE {d['ae']:.3f}  avg {d['avg_pace_minkm']:.2f}min/km  {d['avg_hr']}bpm  ({d['n']} runs)  {bar}")

    print()
    print(f"AE trend (overall):      {analysis['ae_trend']} ({analysis['ae_trend_pct']:+.1f}%)" if analysis['ae_trend_pct'] is not None else f"AE trend: {analysis['ae_trend']}")
    if analysis["ae_delta_wk"] is not None:
        print(f"AE vs previous week:    {analysis['ae_delta_wk']:+.1f}%")
    if analysis["hr_at_mp"]:
        print(f"Predicted HR at MP:      {analysis['hr_at_mp']} bpm")
        print(f"Red zone threshold:      {analysis['red_zone_hr']} bpm")
        print(f"HR buffer at MP:         {analysis['hr_buffer']:+.1f} bpm")

    print()
    print("2:35 MARATHON PROBABILITY:")
    if analysis["hr_buffer"] and analysis["hr_buffer"] > 0:
        print(f"  HR buffer of {analysis['hr_buffer']:.0f}bpm at MP -- POSITIVE signal.")
    print(f"  Riegel from 1:13 HM PB -> 2:32 baseline capability.")
    print(f"  Current training volume (750km / 8 wks) is elite-level.")
    if analysis["ae_trend"] == "improving":
        print(f"  AE improving -> fitness building into taper. HIGH probability.")
    elif analysis["ae_trend"] == "declining":
        print(f"  AE declining -> likely accumulated fatigue. Taper should resolve this.")
    else:
        print(f"  AE stable -> consistent fitness base. MODERATE-HIGH probability.")

# -- Main ---------------------------------------------------------------------
def main():
    print("== Strava Sync ==========================================")
    env   = load_env()
    token = get_access_token(env)

    # Fetch all runs from Nov 1 2025 (covers full training block)
    after = datetime(2025, 11, 1, tzinfo=timezone.utc)
    print(f"  Fetching runs from {after.strftime('%b %d, %Y')}...")
    api_rows = [norm_api(a) for a in fetch_api_runs(token, after)]
    print(f"  API: {len(api_rows)} runs fetched")

    # Parse bulk export
    export_rows = parse_export("activities.csv")
    print(f"  Export: {len(export_rows)} runs parsed")

    # Merge
    master = merge(export_rows, api_rows)
    print(f"  Master: {len(master)} unique runs")

    # Write master_activities.csv
    with open("master_activities.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Name", "Distance_km", "Moving_Time", "Avg_HR"])
        for r in master:
            w.writerow([
                r["date"],
                r["name"],
                r["distance_km"],
                fmt_time(r["moving_secs"]) if r["moving_secs"] else "",
                r["avg_hr"],
            ])
    print(f"  Wrote master_activities.csv ({len(master)} rows)")

    # Analyse
    print()
    print("== Analysis =============================================")
    analysis  = analyse(master)
    pred_secs, basis = predict(master, analysis)
    status    = build_status(pred_secs, analysis, master)

    # Write training_status.json
    with open("training_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    print(f"  Wrote training_status.json -- {status['label']}")

    # Print Week 9 audit to console
    week9_audit(master, analysis)

    # Write timestamp file
    now_str = datetime.now().strftime("%d %b %Y at %H:%M")
    with open("last_synced.txt", "w", encoding="utf-8") as f:
        f.write(now_str)
    print(f"\n  Last synced: {now_str}")

    # Git push updated data files
    git_push(now_str)

# ── Git auto-push ─────────────────────────────────────────────────────────────
def git_push(timestamp):
    files = ["training_status.json", "master_activities.csv", "last_synced.txt"]
    try:
        subprocess.run(
            ["git", "add"] + files,
            cwd=REPO_DIR, check=True, capture_output=True, timeout=30
        )
        result = subprocess.run(
            ["git", "commit", "-m", f"Auto-sync {timestamp}"],
            cwd=REPO_DIR, capture_output=True, timeout=30
        )
        if result.returncode != 0:
            print("  Git: nothing new to commit")
            return
        subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=REPO_DIR, check=True, capture_output=True, timeout=60
        )
        print("  Git push complete")
    except subprocess.TimeoutExpired:
        print("  Git push timed out")
    except Exception as e:
        print(f"  Git push failed: {e}")

if __name__ == "__main__":
    main()
