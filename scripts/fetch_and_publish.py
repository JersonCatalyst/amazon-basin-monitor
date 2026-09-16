#!/usr/bin/env python3
"""
fetch_and_publish.py

Corre DENTRO de GitHub Actions (no en Cowork/Claude). Este script es el unico
punto del sistema que escribe en el repositorio de GitHub, y por eso nunca
pasa por la restriccion de acceso a la API de GitHub desde Cowork.

Hace tres cosas, cada una independiente (si una falla, las otras igual se intentan):

  1) FIRMS: llama directamente a la API de NASA FIRMS, agrega/recorta la
     ventana movil de RETENTION_DAYS dias, y actualiza firms_hotspots.json.
     Esta ventana es solo para lo que se muestra en el mapa publico (lo
     operacionalmente accionable); el historico completo de focos de calor
     ya lo mantiene la propia NASA en su herramienta de descarga
     (https://firms.modaps.eosdis.nasa.gov/download/), asi que este script
     no intenta duplicar ese archivo.

  2) SHAREPOINT SYNC: descarga events.json y meta.json desde los enlaces
     "cualquiera con el enlace puede ver" que se configuraron en SharePoint,
     y los deja listos para el commit.

  3) SST/ENSO: calcula la anomalia de temperatura superficial del mar del
     Pacifico ecuatorial (NOAA OISST via Google Earth Engine) contra una
     climatologia 1991-2020 PRECALCULADA (ver scripts/setup_enso_sst_climatology.py,
     que se corre una sola vez, no aqui), y publica sst_layer.json con la URL
     del tile para que index.html la dibuje como capa raster. Esta capa nunca
     toca SharePoint -- se calcula y se publica enteramente por este script.

Variables de entorno requeridas (se configuran como GitHub Actions secrets):
  FIRMS_MAP_KEY              -> la MAP_KEY privada de FIRMS
  SHAREPOINT_EVENTS_URL      -> enlace de descarga directa de events.json
  SHAREPOINT_META_URL        -> enlace de descarga directa de meta.json
  GEE_SERVICE_ACCOUNT_EMAIL  -> email de la cuenta de servicio de Earth Engine
  GEE_SERVICE_ACCOUNT_KEY    -> contenido JSON de la clave privada de esa cuenta
  GEE_PROJECT_ID             -> opcional, por defecto "ee-jersoncatalyst"
  GEE_CLIMATOLOGY_ASSET_ID   -> opcional, ID del Earth Engine Asset con la
                                climatologia ya precalculada

No imprime nunca el contenido completo de estos archivos en los logs -
solo conteos y mensajes de error, para mantener los logs de Actions livianos
y no filtrar datos por accidente.
"""

import csv
import http.cookiejar
import io
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import ee  # earthengine-api -- ver GEE_* mas abajo; opcional hasta que se instale
except ImportError:
    ee = None

# Algunos runners de GitHub Actions no tienen ruta de red IPv6 utilizable hacia
# ciertos hosts externos (por ejemplo la NASA), aunque el host sí resuelva una
# direccion IPv6 por DNS. Eso produce "Network is unreachable" incluso cuando
# IPv4 funciona perfectamente. Forzamos aqui que TODAS las conexiones salientes
# de este script usen solo IPv4, para no depender de que el runner tenga IPv6.
_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    if results:
        return results
    # si por lo que sea no hay resultados IPv4, no lo escondemos: se deja que
    # falle con el error real en vez de devolver una lista vacia silenciosa.
    return _original_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# --- Configuracion (ver seccion 9 de las instrucciones originales del proyecto) ---
AREA_COORDINATES = "-79,-21,-40,11"  # min_lon,min_lat,max_lon,max_lat (Pan-Amazonia)
SOURCES = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
DAY_RANGE = 1
RETENTION_DAYS = 7  # ventana del mapa publico; el historico completo vive en NASA FIRMS
GRID_SIZE_DEG = 0.25
GRID_AGGREGATION_TRIGGER = 1500  # solo agrega si un solo dia supera este umbral

REPO_ROOT = os.environ.get("GITHUB_WORKSPACE", ".")
FIRMS_JSON_PATH = os.path.join(REPO_ROOT, "firms_hotspots.json")
EVENTS_JSON_PATH = os.path.join(REPO_ROOT, "events.json")
META_JSON_PATH = os.path.join(REPO_ROOT, "meta.json")
SST_LAYER_JSON_PATH = os.path.join(REPO_ROOT, "sst_layer.json")

# Cobertura de la capa SST/ENSO: Pacifico ecuatorial desde el antimeridiano
# hasta la costa de Sudamerica (min_lon, min_lat, max_lon, max_lat). Cubre
# completas las regiones Nino 1+2, 3 y 3.4, y la mitad oriental de Nino 4
# (ver ENSO_REGIONS en index.html para el detalle de cada caja de referencia;
# la mitad occidental de Nino 4, 160E-180E, se deja fuera para no tener que
# manejar el cruce del antimeridiano en la geometria).
SST_ROI_BOUNDS = [-180, -15, -70, 15]
SST_VIS_PARAMS = {
    "min": -3.0,
    "max": 3.0,
    "palette": ["0000ff", "4db8ff", "ffffff", "ffb84d", "ff0000", "800000"],
}
# ID del Earth Engine Asset con la climatologia diaria 1991-2020 YA
# PRECALCULADA -- se genera una sola vez con scripts/setup_enso_sst_climatology.py,
# nunca aqui, para no recalcular a diario un promedio de 30 anos que no cambia
# (eso gastaria cuota de Earth Engine sin necesidad).
GEE_CLIMATOLOGY_ASSET_ID = os.environ.get(
    "GEE_CLIMATOLOGY_ASSET_ID",
    "projects/ee-jersoncatalyst/assets/enso_sst_climatology_1991_2020",
)


def satellite_name(source: str) -> str:
    return "NOAA-20" if "NOAA20" in source else "NOAA-21"


def fetch_firms_points(map_key: str):
    """Llama la API de FIRMS para cada satelite y devuelve la lista de puntos crudos."""
    points = []
    for source in SOURCES:
        url = (
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{map_key}/{source}/{AREA_COORDINATES}/{DAY_RANGE}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "amazon-basin-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8", errors="replace")

        if not text.strip():
            continue

        reader = csv.DictReader(io.StringIO(text))
        sat = satellite_name(source)
        for row in reader:
            try:
                points.append(
                    {
                        "lat": round(float(row["latitude"]), 4),
                        "lon": round(float(row["longitude"]), 4),
                        "date": row.get("acq_date", "").strip(),
                        "confidence": (row.get("confidence") or "nominal").strip().lower(),
                        "satellite": sat,
                    }
                )
            except (KeyError, ValueError):
                # fila con formato inesperado -> se ignora, no se inventa nada
                continue
    return points


def aggregate_if_needed(points):
    """Si un solo dia trae mas puntos que el umbral, agrega por celda de rejilla."""
    if len(points) <= GRID_AGGREGATION_TRIGGER:
        return points

    cells = {}
    for p in points:
        key = (
            round(p["lat"] / GRID_SIZE_DEG) * GRID_SIZE_DEG,
            round(p["lon"] / GRID_SIZE_DEG) * GRID_SIZE_DEG,
            p["date"],
            p["satellite"],
        )
        cells.setdefault(key, []).append(p)

    aggregated = []
    confidence_rank = {"low": 0, "nominal": 1, "high": 2}
    for (lat, lon, date, sat), group in cells.items():
        best_confidence = max(group, key=lambda g: confidence_rank.get(g["confidence"], 0))["confidence"]
        aggregated.append(
            {"lat": lat, "lon": lon, "date": date, "confidence": best_confidence, "satellite": sat}
        )
    return aggregated


def load_json_array(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def trim_to_window(points, days=RETENTION_DAYS):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [p for p in points if p.get("date", "") >= cutoff]


def dedupe(points):
    """Evita duplicados exactos si el Action corre mas de una vez en el mismo dia."""
    seen = set()
    unique = []
    for p in points:
        key = (p["lat"], p["lon"], p["date"], p["satellite"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def update_firms():
    map_key = os.environ.get("FIRMS_MAP_KEY", "")
    if not map_key:
        print("FIRMS: BLOQUEADO - falta la variable de entorno FIRMS_MAP_KEY")
        return

    try:
        new_points = fetch_firms_points(map_key)
    except urllib.error.URLError as e:
        print(f"FIRMS: BLOQUEADO - error de red al llamar la API ({e})")
        return
    except Exception as e:
        print(f"FIRMS: BLOQUEADO - {e}")
        return

    new_points = aggregate_if_needed(new_points)
    existing = load_json_array(FIRMS_JSON_PATH)
    combined = dedupe(trim_to_window(existing + new_points))

    with open(FIRMS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, separators=(",", ":"))

    print(f"FIRMS: {len(new_points)} puntos nuevos de hoy, {len(combined)} puntos totales en la ventana de {RETENTION_DAYS} dias")


BROWSER_HEADERS = {
    # SharePoint (y la capa de proteccion contra bots delante de Office 365) suele
    # rechazar con 403 cualquier peticion que no "parezca" un navegador real, y
    # los enlaces de "cualquiera con el enlace" a veces pasan por 1-2 redirecciones
    # que fijan una cookie de sesion antes de servir el archivo. Por eso usamos un
    # User-Agent de navegador real y un opener con manejo de cookies, en vez de una
    # peticion urllib "desnuda".
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
}

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))


def download_file(url: str, dest_path: str):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with _opener.open(req, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        # Muestra un fragmento del cuerpo de la respuesta de error (sin exponer la
        # URL completa, que contiene el token de enlace compartido) para poder
        # diagnosticar si el bloqueo viene de SharePoint, de un WAF, etc.
        snippet = ""
        try:
            snippet = e.read(300).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise urllib.error.URLError(f"HTTP {e.code}: {e.reason} | cuerpo: {snippet!r}") from e
    with open(dest_path, "wb") as f:
        f.write(data)


def sync_from_sharepoint():
    events_url = os.environ.get("SHAREPOINT_EVENTS_URL", "")
    meta_url = os.environ.get("SHAREPOINT_META_URL", "")

    if not events_url or not meta_url:
        print("SHAREPOINT SYNC: BLOQUEADO - faltan SHAREPOINT_EVENTS_URL y/o SHAREPOINT_META_URL")
        return

    try:
        download_file(events_url, EVENTS_JSON_PATH)
        download_file(meta_url, META_JSON_PATH)
        # validacion minima: que el archivo descargado sea JSON valido
        with open(EVENTS_JSON_PATH, "r", encoding="utf-8") as f:
            events = json.load(f)
        print(f"SHAREPOINT SYNC: events.json y meta.json actualizados ({len(events)} eventos)")
    except urllib.error.URLError as e:
        print(f"SHAREPOINT SYNC: BLOQUEADO - error de red al descargar ({e})")
    except json.JSONDecodeError:
        print("SHAREPOINT SYNC: BLOQUEADO - el archivo descargado no es JSON valido (revisar el enlace compartido)")
    except Exception as e:
        print(f"SHAREPOINT SYNC: BLOQUEADO - {e}")


def _gee_initialize():
    email = os.environ.get("GEE_SERVICE_ACCOUNT_EMAIL", "")
    key_data = os.environ.get("GEE_SERVICE_ACCOUNT_KEY", "")
    project_id = os.environ.get("GEE_PROJECT_ID", "ee-jersoncatalyst")
    if not email or not key_data:
        raise RuntimeError("faltan GEE_SERVICE_ACCOUNT_EMAIL y/o GEE_SERVICE_ACCOUNT_KEY")
    credentials = ee.ServiceAccountCredentials(email, key_data=key_data)
    ee.Initialize(credentials, project=project_id)


def update_sst_layer():
    """Capa ENSO (anomalia de temperatura superficial del mar). Se calcula
    directamente contra Google Earth Engine -- nunca pasa por SharePoint.

    Si algo falla (falta earthengine-api, faltan credenciales, la API no
    responde, no existe todavia el Asset de climatologia), se deja
    sst_layer.json tal como estaba de la corrida anterior: nunca se inventa
    un tile ni se borra el ultimo que si funciono.
    """
    if ee is None:
        print("SST/ENSO: BLOQUEADO - falta instalar el paquete earthengine-api")
        return

    try:
        _gee_initialize()

        climatology = ee.Image(GEE_CLIMATOLOGY_ASSET_ID)
        sst_col = ee.ImageCollection("NOAA/CDR/OISST/V2_1").select("sst")
        latest = sst_col.sort("system:time_start", False).first()
        latest_date = latest.date()
        doy = latest_date.getRelative("day", "year").add(1).min(365)
        doy_band = ee.String("doy_").cat(ee.Number(doy).format("%03d"))

        roi = ee.Geometry.BBox(*SST_ROI_BOUNDS)
        current_sst = latest.multiply(0.01)
        hist_sst = climatology.select(doy_band)
        anomaly = current_sst.subtract(hist_sst).rename("sst_anomaly").clip(roi)

        map_id = anomaly.getMapId(SST_VIS_PARAMS)
        tile_url = map_id["tile_fetcher"].url_format
        image_date = latest_date.format("YYYY-MM-dd").getInfo()

        payload = {
            "tileUrl": tile_url,
            "date": image_date,
            "vis": SST_VIS_PARAMS,
            "bounds": SST_ROI_BOUNDS,
        }
        with open(SST_LAYER_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

        print(f"SST/ENSO: capa actualizada (imagen del {image_date})")
    except Exception as e:
        print(f"SST/ENSO: BLOQUEADO - {e}")


def main():
    update_firms()
    sync_from_sharepoint()
    update_sst_layer()


if __name__ == "__main__":
    sys.exit(main())
