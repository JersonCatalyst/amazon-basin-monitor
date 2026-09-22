#!/usr/bin/env python3
"""
Reconstruye index.json, events.json, meta.json y los CSV trimestrales-reporte
a partir de los archivos individuales en Events/<EVENT_ID>.json (fuente de
verdad del monitor de desastres de la cuenca amazonica).

Uso: python scripts/rebuild_aggregates.py
Se corre desde la raiz del repo (donde vive la carpeta Events/).

Este script es 100% determinista: no llama a ningun modelo ni API externa.
Solo lee Events/*.json y escribe index.json, events.json, meta.json y los
archivos Amazon_Basin_Disaster_Events_<AAAA>-Q<N>.csv que correspondan.

Pensado para correr dentro de .github/workflows/rebuild-aggregates.yml,
disparado por push sobre Events/**.
"""

import csv
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

EVENTS_DIR = "Events"
ENDED_RETENTION_DAYS = 90

# Las 50 columnas del esquema, EN ESTE ORDEN EXACTO. Cualquier archivo de
# Events/ que no tenga exactamente estas 50 claves se rechaza (ver
# load_events) en vez de reconstruir agregados con datos incompletos.
SCHEMA_COLUMNS = [
    "EVENT_ID", "FIRST_DETECTED_DATE", "LAST_UPDATED_DATE", "UPDATE_COUNT",
    "COUNTRY", "TERRITORY", "ADMINISTRATIVE_LEVEL_1", "ADMINISTRATIVE_LEVEL_2",
    "MUNICIPALITY", "LOCALITY", "AMAZON_BASIN_STATUS", "HAZARD_TYPE",
    "HAZARD_SUBTYPE", "EVENT_STATUS", "EVENT_DATE", "EVENT_START_DATE",
    "EVENT_END_DATE", "LATITUDE", "LONGITUDE", "COORDINATE_ACCURACY",
    "GEOGRAPHIC_CONFIDENCE", "EVENT_TITLE_ES", "EVENT_TITLE_PT",
    "EVENT_TITLE_EN", "EVENT_DESCRIPTION_ES", "EVENT_DESCRIPTION_PT",
    "EVENT_DESCRIPTION_EN", "DEATHS", "INJURIES", "AFFECTED_POPULATION",
    "DISPLACED_POPULATION", "EVACUATED_POPULATION", "AGRICULTURAL_IMPACT",
    "LIVESTOCK_IMPACT", "FOREST_IMPACT", "WATER_IMPACT",
    "FOOD_SECURITY_IMPACT", "ALERT_PRIORITY", "AA_RELEVANCE",
    "VERIFICATION_STATUS", "CONFIDENCE", "PRIMARY_SOURCE",
    "PRIMARY_SOURCE_URL", "SECONDARY_SOURCE", "SECONDARY_SOURCE_URL",
    "GDACS_URL", "OTHER_SOURCE_URLS", "NOTES_ES", "NOTES_PT", "NOTES_EN",
]
assert len(SCHEMA_COLUMNS) == 50, "El esquema debe tener exactamente 50 columnas"


def load_events():
    """Lee y valida todos los Events/AMZ-*.json. Aborta con exit(1) si alguno
    no cumple el esquema de 50 columnas (mejor fallar el workflow y avisar,
    que publicar agregados corruptos)."""
    events = []
    paths = sorted(glob.glob(os.path.join(EVENTS_DIR, "AMZ-*.json")))
    if not paths:
        print(f"ADVERTENCIA: no se encontro ningun archivo en {EVENTS_DIR}/. "
              f"No hay nada que reconstruir.", file=sys.stderr)
        return events

    for p in paths:
        with open(p, encoding="utf-8") as fh:
            try:
                d = json.load(fh)
            except json.JSONDecodeError as e:
                print(f"ERROR: {p} no es JSON valido: {e}", file=sys.stderr)
                sys.exit(1)

        missing = set(SCHEMA_COLUMNS) - set(d.keys())
        extra = set(d.keys()) - set(SCHEMA_COLUMNS)
        if missing or extra:
            print(
                f"ERROR: {p} no cumple el esquema de 50 columnas.\n"
                f"  Faltan: {sorted(missing)}\n"
                f"  Sobran: {sorted(extra)}",
                file=sys.stderr,
            )
            sys.exit(1)

        events.append(d)

    return events


def quarter_of(event_id):
    """AMZ-YYYYMMDD-NNN -> (YYYY, trimestre 1-4), segun los 8 digitos de
    fecha del propio EVENT_ID (nunca cambia aunque el evento se seguir
    actualizando meses despues)."""
    datepart = event_id.split("-")[1]
    y = int(datepart[0:4])
    m = int(datepart[4:6])
    q = (m - 1) // 3 + 1
    return y, q


def build_index(events):
    index = []
    for d in events:
        preview = (d.get("EVENT_DESCRIPTION_EN") or "")[:150]
        index.append({
            "id": d["EVENT_ID"],
            "country": d["COUNTRY"],
            "hazard": d["HAZARD_TYPE"],
            "lat": float(d["LATITUDE"]),
            "lon": float(d["LONGITUDE"]),
            "status": d["EVENT_STATUS"],
            "firstDetected": d["FIRST_DETECTED_DATE"],
            "lastUpdated": d["LAST_UPDATED_DATE"],
            "descEnPreview": preview,
        })
    return index


def sources_of(d):
    out = []
    if (d.get("PRIMARY_SOURCE") not in (None, "UNAVAILABLE")
            and d.get("PRIMARY_SOURCE_URL") not in (None, "UNAVAILABLE")):
        out.append({"label": d["PRIMARY_SOURCE"], "url": d["PRIMARY_SOURCE_URL"]})
    if (d.get("SECONDARY_SOURCE") not in (None, "UNAVAILABLE")
            and d.get("SECONDARY_SOURCE_URL") not in (None, "UNAVAILABLE")):
        out.append({"label": d["SECONDARY_SOURCE"], "url": d["SECONDARY_SOURCE_URL"]})
    return out


def build_events_json(events, today):
    """Un objeto por evento vigente: todos los que no esten ENDED, mas los
    ENDED cuyo LAST_UPDATED_DATE este dentro de los ultimos 90 dias. Un
    ENDED con mas de 90 dias sin actualizar se excluye de events.json (su
    archivo en Events/ sigue existiendo siempre, solo deja de mostrarse en
    el mapa en vivo)."""
    cutoff = today - timedelta(days=ENDED_RETENTION_DAYS)
    out = []
    for d in events:
        if d["EVENT_STATUS"] == "ENDED":
            try:
                lu = date.fromisoformat(d["LAST_UPDATED_DATE"])
            except ValueError:
                lu = today  # fecha mal formada -> no lo excluimos por error silencioso
            if lu < cutoff:
                continue

        out.append({
            "id": d["EVENT_ID"],
            "country": d["COUNTRY"],
            "hazard": d["HAZARD_TYPE"],
            "priority": d["ALERT_PRIORITY"],
            "status": d["EVENT_STATUS"],
            "basinStatus": d["AMAZON_BASIN_STATUS"],
            "verification": d["VERIFICATION_STATUS"],
            "lat": float(d["LATITUDE"]),
            "lon": float(d["LONGITUDE"]),
            "coordAccuracy": d["COORDINATE_ACCURACY"],
            "firstDetected": d["FIRST_DETECTED_DATE"],
            "lastUpdated": d["LAST_UPDATED_DATE"],
            "updateCount": int(d["UPDATE_COUNT"]),
            "eventDate": d["EVENT_DATE"],
            "adminLevel1": d["ADMINISTRATIVE_LEVEL_1"],
            "adminLevel2": d["ADMINISTRATIVE_LEVEL_2"],
            "municipality": d["MUNICIPALITY"],
            "locality": d["LOCALITY"],
            "title": {
                "es": d["EVENT_TITLE_ES"],
                "pt": d["EVENT_TITLE_PT"],
                "en": d["EVENT_TITLE_EN"],
            },
            "desc": {
                "es": d["EVENT_DESCRIPTION_ES"],
                "pt": d["EVENT_DESCRIPTION_PT"],
                "en": d["EVENT_DESCRIPTION_EN"],
            },
            "notes": {
                "es": d["NOTES_ES"],
                "pt": d["NOTES_PT"],
                "en": d["NOTES_EN"],
            },
            "sources": sources_of(d),
        })
    return out


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def write_quarterly_csvs(events):
    """Regenera TODOS los CSV trimestrales que tengan al menos un evento.
    A diferencia del flujo manual anterior, aqui no hace falta optimizar
    'solo subir si cambio': esto corre gratis y en segundos dentro de
    Actions, y el paso de git en el workflow ya se encarga de no hacer
    commit si el contenido no cambio."""
    by_quarter = {}
    for d in events:
        by_quarter.setdefault(quarter_of(d["EVENT_ID"]), []).append(d)

    for (y, q), rows in sorted(by_quarter.items()):
        fname = f"Amazon_Basin_Disaster_Events_{y}-Q{q}.csv"
        with open(fname, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SCHEMA_COLUMNS)
            w.writeheader()
            for d in sorted(rows, key=lambda x: x["EVENT_ID"]):
                w.writerow({k: d[k] for k in SCHEMA_COLUMNS})
        print(f"  {fname}: {len(rows)} eventos")


def main():
    events = load_events()
    today = datetime.now(timezone.utc).date()

    index = build_index(events)
    write_json("index.json", index)
    print(f"index.json: {len(index)} eventos")

    events_json = build_events_json(events, today)
    write_json("events.json", events_json)
    print(f"events.json: {len(events_json)} eventos vigentes de {len(events)} historicos")

    write_quarterly_csvs(events)

    meta = {"lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    write_json("meta.json", meta)
    print(f"meta.json: {meta['lastUpdated']}")


if __name__ == "__main__":
    main()
