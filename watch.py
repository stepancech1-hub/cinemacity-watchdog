#!/usr/bin/env python3
"""Hlídá rozpis Cinema City Flora (Praha) a hlásí nově vypsaná představení Duny.

Upraveno z https://github.com/TarkDetrius/cinemacity-watchdog — místo
celostátního hledání IMAX sálů cílí přímo na jedno kino (Flora, id 1052),
takže je jednodušší a dělá míň HTTP dotazů.

Data bere z veřejného JSON API cinemacity.cz (bez klíče, bez přihlášení).
Stav (už viděná představení) drží v JSON souboru, takže při každém běhu
hlásí jen to, co přibylo od minule.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SITE_ID = "10101"  # cinemacity.cz
BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/{SITE_ID}"
LANG = "cs_CZ"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cinema City Flora, Praha. (Jiné kino: zjistěte ID z URL na cinemacity.cz,
# např. .../cinemas/flora/1052 -> 1052.)
CINEMA_ID = os.environ.get("CINEMA_ID", "1052")

# Podřetězec v názvu filmu (case-insensitive). "duna" chytí i případné
# reprízy starších dílů - pokud chcete jen "část třetí", přepište na "třetí".
FILM_PATTERN = os.environ.get("FILM_PATTERN", "duna").lower()

# Podřetězec v názvu sálu - necháno prázdné = hlídá se každý sál.
AUDITORIUM_PATTERN = os.environ.get("AUDITORIUM_PATTERN", "").lower()

HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS", "120"))
DELAY = float(os.environ.get("REQUEST_DELAY", "0.25"))

CZ_DAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]

# API vrací eventDateTime bez zóny, v místním čase kina. Runner v GitHub
# Actions jede v UTC, takže by se čas představení porovnával s časem o dvě
# hodiny pozadu.
CINEMA_TZ = ZoneInfo("Europe/Prague")


def now():
    return datetime.now(CINEMA_TZ).replace(tzinfo=None)


def api(path):
    url = f"{BASE}{path}"
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))["body"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise SystemExit(f"API selhalo po 4 pokusech: {url}\n{last}")


def horizon():
    return (date.today() + timedelta(days=HORIZON_DAYS)).isoformat()


def cinema_name():
    body = api(f"/cinemas/with-event/until/{horizon()}?attr=&lang={LANG}")
    for c in body["cinemas"]:
        if c["id"] == CINEMA_ID:
            return c["displayName"]
    return f"Cinema {CINEMA_ID}"


def fetch_dates():
    return api(f"/dates/in-cinema/{CINEMA_ID}/until/{horizon()}?attr=&lang={LANG}")["dates"]


def fetch_day(day):
    time.sleep(DELAY)
    body = api(f"/film-events/in-cinema/{CINEMA_ID}/at-date/{day}?attr=&lang={LANG}")
    films = {f["id"]: f for f in body.get("films", [])}
    return films, body.get("events", [])


def is_target_hall(event):
    if not AUDITORIUM_PATTERN:
        return True
    return AUDITORIUM_PATTERN in (event.get("auditorium") or "").lower()


def collect():
    cname = cinema_name()
    found = {}
    for day in fetch_dates():
        films, events = fetch_day(day)
        for e in events:
            film = films.get(e["filmId"], {})
            if FILM_PATTERN not in film.get("name", "").lower():
                continue
            if not is_target_hall(e):
                continue
            found[e["id"]] = {
                "id": e["id"],
                "film": film.get("name", e["filmId"]),
                "filmLink": film.get("link"),
                "cinema": cname,
                "datetime": e["eventDateTime"],
                "auditorium": e.get("auditorium"),
                "attrs": e.get("attributeIds", []),
                "booking": f"https://tickets.cinemacity.cz/order/{e.get('presentationCode') or e['id']}",
                "soldOut": bool(e.get("soldOut")),
            }
    return found


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"updated": None, "events": {}}


def save_state(path, events):
    if set(events) == set(load_state(path).get("events", {})):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": now().replace(microsecond=0).isoformat(),
        "events": dict(sorted(events.items(), key=lambda kv: kv[1]["datetime"])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    return True


def prune_past(events):
    cutoff = (now() - timedelta(days=1)).isoformat()
    return {k: v for k, v in events.items() if v["datetime"] >= cutoff}


def fmt_dt(iso):
    dt = datetime.fromisoformat(iso)
    return f"{CZ_DAYS[dt.weekday()]} {dt.day}. {dt.month}. {dt.year} v {dt:%H:%M}"


def fmt_short(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.day}. {dt.month}."


def render(new_events, gone_events):
    lines = []
    if new_events:
        lines.append(f"### Nově vypsáno ({len(new_events)})\n")
        for e in sorted(new_events, key=lambda x: x["datetime"]):
            flags = []
            if "subbed" in e["attrs"]:
                flags.append("titulky")
            if "dubbed" in e["attrs"]:
                flags.append("dabing")
            if e["soldOut"]:
                flags.append("**vyprodáno**")
            suffix = f" — {', '.join(flags)}" if flags else ""
            link = f" — [koupit]({e['booking']})" if e["booking"] else ""
            lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}{suffix}{link}")
        lines.append("")
    if gone_events:
        lines.append(f"### Zmizelo z rozpisu ({len(gone_events)})\n")
        for e in sorted(gone_events, key=lambda x: x["datetime"]):
            lines.append(f"- {fmt_dt(e['datetime'])} · {e['auditorium']}")
        lines.append("")
    film_link = next(
        (e["filmLink"] for e in list(new_events) + list(gone_events) if e.get("filmLink")),
        None,
    )
    if film_link:
        lines.append(f"[Stránka filmu na Cinema City]({film_link})")
    lines.append("")
    lines.append(
        f"<sub>Zkontrolováno {now():%d. %m. %Y %H:%M} · "
        f"kino `{CINEMA_ID}` · film ~ `{FILM_PATTERN}`</sub>"
    )
    return "\n".join(lines)


def title_for(new_events):
    film = new_events[0]["film"]
    days = sorted({e["datetime"][:10] for e in new_events})
    span = fmt_short(days[0])
    if len(days) > 1:
        span += f"–{fmt_short(days[-1])}"
    n = len(new_events)
    word = "nový termín" if n == 1 else ("nové termíny" if n < 5 else "nových termínů")
    return f"🎬 {film}: {n} {word} ({span})"


def gh_output(**kwargs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            fh.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--force-report", action="store_true")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--title", default="title.txt")
    args = ap.parse_args()

    current = collect()
    state = load_state(args.state)
    known = state.get("events", {})

    print(f"Nalezeno {len(current)} hlídaných představení, ve stavu {len(known)}.")

    if args.seed:
        save_state(args.state, prune_past(current))
        print(f"Stav zapsán do {args.state} (seed, nic se nehlásí).")
        gh_output(has_news="false")
        return

    if args.force_report:
        new_events = sorted(current.values(), key=lambda e: e["datetime"])
        gone = []
    else:
        new_events = sorted(
            (v for k, v in current.items() if k not in known),
            key=lambda e: e["datetime"],
        )
        future = now().isoformat()
        gone = sorted(
            (v for k, v in known.items() if k not in current and v["datetime"] > future),
            key=lambda e: e["datetime"],
        )

    save_state(args.state, prune_past(current))

    if not new_events and not gone:
        print("Nic nového.")
        gh_output(has_news="false")
        return

    body = render(new_events, gone)
    title = title_for(new_events) if new_events else "🎬 Duna: zrušené termíny"

    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    with open(args.title, "w", encoding="utf-8") as fh:
        fh.write(title + "\n")

    print(f"\n{title}\n")
    print(body)
    gh_output(has_news="true")


if __name__ == "__main__":
    sys.exit(main())
