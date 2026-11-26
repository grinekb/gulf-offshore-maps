#!/usr/bin/env python3
"""Fetch NDBC buoy observations, NWS marine forecasts/alerts, and an Open-Meteo
day-by-day wave/wind table. Writes waves.json. Stdlib only.

This is independent of the daily map pipeline (run_daily.sh / publish_live.sh).
"""
from __future__ import annotations

import json, math, os, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

UA = "MaKsCastWavePanel/1.0 (https://github.com/grinekb/gulf-offshore-maps; fishing forecast wave panel)"
CT = ZoneInfo("America/Chicago")
PP = (30.2583, -87.5567)
NM_R = 3440.065
COMP = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]

BUOYS = [
    {"id": "42012", "name": "Orange Beach", "lat": 30.061, "lon": -87.547,
     "detail": "NDBC 42012 · 44 nm SE of Mobile, ~12 nm S of Perdido Pass"},
    {"id": "42039", "name": "Pensacola", "lat": 28.768, "lon": -86.024,
     "detail": "NDBC 42039 · 115 nm SSE of Pensacola, deep-water buoy"},
    {"id": "42040", "name": "Luke Offshore", "lat": 29.207, "lon": -88.237,
     "detail": "NDBC 42040 · 63 nm S of Dauphin Island",
     "expected_offline": "Buoy recovered June 26, 2026. Not reporting."},
]

ZONES = [
    {"id": "GMZ655", "group": "nearshore",
     "name": "Coastal waters, Okaloosa–Walton line to Pensacola, out 20 nm",
     "url": "https://forecast.weather.gov/shmrn.php?mz=gmz655"},
    {"id": "GMZ650", "group": "nearshore",
     "name": "Coastal waters, Pensacola to Pascagoula, out 20 nm",
     "url": "https://forecast.weather.gov/shmrn.php?mz=gmz650"},
    {"id": "GMZ675", "group": "offshore",
     "name": "Waters, Okaloosa–Walton line to Pensacola, 20 to 60 nm",
     "url": "https://forecast.weather.gov/shmrn.php?mz=gmz675"},
    {"id": "GMZ670", "group": "offshore",
     "name": "Waters, Pensacola to Pascagoula, 20 to 60 nm",
     "url": "https://forecast.weather.gov/shmrn.php?mz=gmz670"},
]

POINTS = [
    {"id": "nearshore", "name": "Nearshore", "lat": 30.06, "lon": -87.55,
     "note": "~12 nm S of Perdido Pass, near buoy 42012"},
    {"id": "offshore", "name": "Offshore", "lat": 29.75, "lon": -87.5567,
     "note": "~30 nm S of Perdido Pass (in the 20–60 nm zone)"},
]


def compass(deg):
    if deg is None: return None
    return COMP[int((deg + 11.25) / 22.5) % 16]


def rng_brg(lat, lon):
    r = math.pi / 180
    p1, p2 = PP[0] * r, lat * r
    dl = (lon - PP[1]) * r
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * NM_R * math.asin(math.sqrt(h))
    b = (math.atan2(math.sin(dl) * math.cos(p2),
                    math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)) / r + 360) % 360
    return round(d, 1), int(round(b))


def fmt_ct(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CT)
    return local.strftime("%a %b %-d, %-I:%M %p CT")


def iso(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, timeout=20, accept=None):
    headers = {"User-Agent": UA}
    if accept: headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b"", {}
    except Exception as e:
        return None, str(e).encode(), {}


def get_text(url, timeout=20):
    status, body, _ = get(url, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"{url} -> {status} {body[:120]!r}")
    return body.decode("latin-1", "replace")


def get_json(url, timeout=20):
    status, body, _ = get(url, timeout=timeout, accept="application/geo+json, application/json")
    if status != 200:
        raise RuntimeError(f"{url} -> {status} {body[:120]!r}")
    return json.loads(body.decode("utf-8"))


def _f(raw):
    if raw in (None, "", "MM", "mm", "N/A"): return None
    try: return float(raw)
    except ValueError: return None


def parse_table(text, fields):
    """NDBC realtime2: newest-first. Return first non-missing value per field with its UTC time."""
    latest = {}
    for line in text.splitlines():
        if not line or line.startswith("#"): continue
        parts = line.split()
        if len(parts) < len(fields): continue
        try:
            t = datetime(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), tzinfo=timezone.utc)
        except Exception:
            continue
        for name, raw in zip(fields, parts):
            if name in ("YY", "MM", "DD", "hh", "mm"): continue
            if name in latest: continue
            v = raw if name in ("SwD", "WWD", "STEEPNESS") else _f(raw)
            if v is None: continue
            latest[name] = (v, t)
    return latest


MET_FIELDS = "YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS PTDY TIDE".split()
SPEC_FIELDS = "YY MM DD hh mm WVHT SwH SwP WWH WWP SwD WWD STEEPNESS APD MWD".split()


def ms_to_kt(v):
    return None if v is None else round(v * 1.94384, 1)


def m_to_ft(v):
    return None if v is None else round(v * 3.28084, 1)


def c_to_f(v):
    return None if v is None else round(v * 9 / 5 + 32, 1)


def hpa_to_inhg(v):
    return None if v is None else round(v * 0.029529983, 2)


def fetch_buoy(meta):
    sid = meta["id"]
    out = {
        "id": sid, "name": meta["name"], "lat": meta["lat"], "lon": meta["lon"],
        "detail": meta["detail"], "url": f"https://www.ndbc.noaa.gov/station_page.php?station={sid}",
    }
    nm, brg = rng_brg(meta["lat"], meta["lon"])
    out["nm"], out["bearing"] = nm, brg
    if meta.get("expected_offline"):
        # still try, in case it is back
        pass
    try:
        met = parse_table(get_text(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt"), MET_FIELDS)
    except Exception as e:
        if meta.get("expected_offline"):
            out["status"] = "offline"
            out["note"] = meta["expected_offline"]
            return out
        out["status"] = "unavailable"
        out["note"] = f"No recent reports ({e})"
        return out
    if not met:
        out["status"] = "offline"
        out["note"] = meta.get("expected_offline") or "No recent reports"
        return out
    try:
        spec = parse_table(get_text(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.spec"), SPEC_FIELDS)
    except Exception:
        spec = {}

    def val(src, name, conv=None):
        if name not in src: return None, None
        v, t = src[name]
        return (conv(v) if conv else v), t

    wdir, wt = val(met, "WDIR")
    wspd, _ = val(met, "WSPD", ms_to_kt)
    gst, _ = val(met, "GST", ms_to_kt)
    wvht, wvt = val(met, "WVHT", m_to_ft)
    if wvht is None:
        wvht, wvt = val(spec, "WVHT", m_to_ft)
    dpd, _ = val(met, "DPD") or val(spec, "WVHT")  # period from met
    dpd, dpdt = val(met, "DPD")
    if dpd is None:
        # wave period sometimes only on the MM-wind wave row; already handled by parse_table
        pass
    mwd, _ = val(met, "MWD")
    if mwd is None:
        mwd, _ = val(spec, "MWD")
    apd, _ = val(met, "APD")
    if apd is None:
        apd, _ = val(spec, "APD")
    out.update({
        "status": "reporting",
        "wind_dir": None if wdir is None else int(round(wdir)),
        "wind_dir_txt": compass(wdir),
        "wind_kt": wspd, "gust_kt": gst,
        "wind_time": iso(wt), "wind_time_ct": fmt_ct(wt),
        "wvht_ft": wvht, "dpd_s": None if dpd is None else (int(dpd) if float(dpd).is_integer() else round(dpd, 1)),
        "mwd": None if mwd is None else int(round(mwd)),
        "mwd_txt": compass(mwd),
        "apd_s": None if apd is None else round(apd, 1),
        "wave_time": iso(wvt), "wave_time_ct": fmt_ct(wvt),
        "wtmp_f": val(met, "WTMP", c_to_f)[0],
        "atmp_f": val(met, "ATMP", c_to_f)[0],
        "pres_inhg": val(met, "PRES", hpa_to_inhg)[0],
    })
    swh, swt = val(spec, "SwH", m_to_ft)
    swp, _ = val(spec, "SwP")
    wwh, _ = val(spec, "WWH", m_to_ft)
    wwp, _ = val(spec, "WWP")
    swd = spec["SwD"][0] if "SwD" in spec else None
    wwd = spec["WWD"][0] if "WWD" in spec else None
    if swh is not None or wwh is not None:
        out.update({
            "swell_ft": swh, "swell_s": None if swp is None else round(swp, 1), "swell_dir": swd,
            "wwave_ft": wwh, "wwave_s": None if wwp is None else round(wwp, 1), "wwave_dir": wwd,
        })
    if not out.get("wind_time") and not out.get("wave_time"):
        out["status"] = "unavailable"
        out["note"] = "No usable observations in the latest file"
    return out


def fetch_zones():
    out = []
    errors = []
    for z in ZONES:
        try:
            d = get_json(f"https://api.weather.gov/zones/forecast/{z['id']}/forecast")
            p = d.get("properties") or {}
            periods = []
            for x in p.get("periods") or []:
                periods.append({
                    "name": x.get("name"),
                    "text": (x.get("detailedForecast") or x.get("shortForecast") or "").strip(),
                })
            upd = p.get("updated")
            upd_dt = None
            if upd:
                try: upd_dt = datetime.fromisoformat(upd)
                except Exception: pass
            out.append({
                "id": z["id"], "group": z["group"], "name": z["name"], "url": z["url"],
                "updated": upd, "updated_ct": fmt_ct(upd_dt),
                "periods": periods,
            })
        except Exception as e:
            errors.append(f"{z['id']}: {e}")
            out.append({"id": z["id"], "group": z["group"], "name": z["name"], "url": z["url"],
                        "error": str(e), "periods": []})
    return out, errors


def fetch_alerts():
    url = "https://api.weather.gov/alerts/active?zone=" + ",".join(z["id"] for z in ZONES)
    d = get_json(url)
    alerts = []
    for f in d.get("features") or []:
        p = f.get("properties") or {}
        ends = p.get("ends") or p.get("expires")
        ends_dt = None
        if ends:
            try: ends_dt = datetime.fromisoformat(ends)
            except Exception: pass
        zones = []
        for zurl in p.get("affectedZones") or []:
            zid = zurl.rstrip("/").split("/")[-1]
            if zid.startswith("GMZ"): zones.append(zid)
        alerts.append({
            "event": p.get("event"),
            "headline": p.get("headline"),
            "severity": p.get("severity"),
            "ends": ends,
            "ends_ct": fmt_ct(ends_dt),
            "instruction": (p.get("instruction") or "").strip() or None,
            "description": ((p.get("description") or "").strip()[:600] or None),
            "sender": p.get("senderName"),
            "url": f.get("id") or p.get("@id") or "https://www.weather.gov/mob/",
            "zones": zones,
        })
    return alerts


def fetch_open_meteo(point):
    lat, lon = point["lat"], point["lon"]
    marine_q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": "wave_height,wave_period,wave_direction,wind_wave_height,swell_wave_height",
        "forecast_days": 7, "timezone": "America/Chicago",
    })
    wind_q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        "forecast_days": 7, "timezone": "America/Chicago", "wind_speed_unit": "kn",
    })
    marine = get_json("https://marine-api.open-meteo.com/v1/marine?" + marine_q)
    wind = get_json("https://api.open-meteo.com/v1/forecast?" + wind_q)
    mh, wh = marine["hourly"], wind["hourly"]
    # index wind by time
    wmap = {t: i for i, t in enumerate(wh["time"])}
    by_day = {}
    for i, t in enumerate(mh["time"]):
        dt = datetime.fromisoformat(t)  # naive CT
        hour = dt.hour
        # daytime 6–18 CT for the table range; still record all hours for max
        day = dt.date().isoformat()
        hs = mh["wave_height"][i]
        tp = mh["wave_period"][i]
        wd = mh["wave_direction"][i]
        wi = wmap.get(t)
        wspd = wh["wind_speed_10m"][wi] if wi is not None else None
        gst = wh["wind_gusts_10m"][wi] if wi is not None else None
        wdir = wh["wind_direction_10m"][wi] if wi is not None else None
        rec = by_day.setdefault(day, {"date": day, "dow": dt.strftime("%a"),
                                      "seas": [], "period_at": [], "winds": [], "gusts": [], "dirs": []})
        if 6 <= hour <= 18:
            if hs is not None: rec["seas"].append(hs)
            if hs is not None and tp is not None: rec["period_at"].append((hs, tp, wd))
            if wspd is not None: rec["winds"].append(wspd)
            if gst is not None: rec["gusts"].append(gst)
            if wdir is not None and wspd is not None: rec["dirs"].append((wspd, wdir))
    days = []
    for day in sorted(by_day):
        rec = by_day[day]
        if not rec["seas"] and not rec["winds"]:
            continue
        period = None
        if rec["period_at"]:
            # period / dir at the hour of max seas
            hs, tp, wd = max(rec["period_at"], key=lambda x: x[0])
            period = round(tp, 1) if tp is not None else None
            wave_dir = compass(wd)
        else:
            wave_dir = None
        wdir_txt = None
        if rec["dirs"]:
            # dir at max wind
            _, d = max(rec["dirs"], key=lambda x: x[0])
            wdir_txt = compass(d)
        days.append({
            "date": rec["date"], "dow": rec["dow"],
            "seas_ft_min": m_to_ft(min(rec["seas"])) if rec["seas"] else None,
            "seas_ft_max": m_to_ft(max(rec["seas"])) if rec["seas"] else None,
            "period_s": period,
            "wave_dir_txt": wave_dir,
            "wind_kt_min": round(min(rec["winds"]), 0) if rec["winds"] else None,
            "wind_kt_max": round(max(rec["winds"]), 0) if rec["winds"] else None,
            "gust_kt_max": round(max(rec["gusts"]), 0) if rec["gusts"] else None,
            "wind_dir_txt": wdir_txt,
        })
    return {
        "id": point["id"], "name": point["name"], "lat": point["lat"], "lon": point["lon"],
        "note": point["note"], "days": days,
        "model_lat": marine.get("latitude"), "model_lon": marine.get("longitude"),
    }


def build():
    now = datetime.now(timezone.utc)
    errors = []
    buoys = []
    for b in BUOYS:
        try:
            buoys.append(fetch_buoy(b))
        except Exception as e:
            errors.append(f"buoy {b['id']}: {e}")
            buoys.append({"id": b["id"], "name": b["name"], "status": "unavailable",
                          "note": str(e), "url": f"https://www.ndbc.noaa.gov/station_page.php?station={b['id']}"})
    zones, zerr = fetch_zones()
    errors.extend(zerr)
    try:
        alerts = fetch_alerts()
    except Exception as e:
        errors.append(f"alerts: {e}")
        alerts = []
    model_points = []
    for p in POINTS:
        try:
            model_points.append(fetch_open_meteo(p))
        except Exception as e:
            errors.append(f"open-meteo {p['id']}: {e}")
    # Align days from first successful point as the spine
    days_out = []
    if model_points and model_points[0].get("days"):
        keys = [d["date"] for d in model_points[0]["days"]]
        by_id = {p["id"]: {d["date"]: d for d in p["days"]} for p in model_points}
        for date in keys:
            row = {"date": date, "dow": None}
            for p in model_points:
                cell = by_id.get(p["id"], {}).get(date)
                if cell:
                    row["dow"] = cell["dow"]
                    row[p["id"]] = {k: cell[k] for k in cell if k not in ("date", "dow")}
            days_out.append(row)

    doc = {
        "fetched_at": iso(now),
        "fetched_at_ct": fmt_ct(now),
        "perdido_pass": {"name": "Perdido Pass", "lat": PP[0], "lon": PP[1]},
        "buoys": buoys,
        "forecast": {
            "office": "NWS Mobile AL (MOB)",
            "office_url": "https://www.weather.gov/mob/",
            "marine_url": "https://www.weather.gov/marine/mobmz",
            "zones": zones,
        },
        "alerts": alerts,
        "model": {
            "source": "Open-Meteo Marine + GFS wind, 7-day hourly, daytime (6 AM–6 PM CT) range",
            "source_url": "https://open-meteo.com/",
            "points": [{"id": p["id"], "name": p["name"], "lat": p["lat"], "lon": p["lon"], "note": p["note"]} for p in POINTS],
            "days": days_out,
        },
        "links": [
            {"label": "NDBC 42012 Orange Beach", "url": "https://www.ndbc.noaa.gov/station_page.php?station=42012"},
            {"label": "NDBC 42039 Pensacola", "url": "https://www.ndbc.noaa.gov/station_page.php?station=42039"},
            {"label": "NWS nearshore (GMZ655)", "url": "https://forecast.weather.gov/shmrn.php?mz=gmz655"},
            {"label": "NWS 20–60 nm (GMZ675)", "url": "https://forecast.weather.gov/shmrn.php?mz=gmz675"},
            {"label": "NWS Mobile marine zones", "url": "https://www.weather.gov/marine/mobmz"},
        ],
        "errors": errors,
    }
    return doc


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site-repo", "waves.json")
    doc = build()
    path = os.path.abspath(out)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    n_rep = sum(1 for b in doc["buoys"] if b.get("status") == "reporting")
    n_al = len(doc["alerts"])
    n_day = len(doc["model"]["days"])
    print(f"wrote {path}  buoys_reporting={n_rep} alerts={n_al} model_days={n_day} errors={len(doc['errors'])}")
    for e in doc["errors"]:
        print("  ERR", e, file=sys.stderr)
    return 0 if n_rep or n_day or doc["forecast"]["zones"] else 1


if __name__ == "__main__":
    sys.exit(main())
