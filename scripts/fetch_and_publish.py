#!/usr/bin/env python3
"""
fetch_and_publish.py

Corre DENTRO de GitHub Actions (no en Cowork/Claude). Este script es el unico
punto del sistema que escribe en el repositorio de GitHub, y por eso nunca
pasa por la restriccion de acceso a la API de GitHub desde Cowork.

Hace dos cosas, cada una independiente (si una falla, la otra igual se intenta):

  1) FIRMS: llama directamente a la API de NASA FIRMS, agrega/recorta la
     ventana movil de 90 dias, y actualiza firms_hotspots.json.

  2) SHAREPOINT SYNC: descarga events.json y meta.json desde los enlaces
     "cualquiera con el enlace puede ver" que se configuraron en SharePoint,
     y los deja listos para el commit.

Variables de entorno requeridas (se configuran como GitHub Actions secrets):
  FIRMS_MAP_KEY           -> la MAP_KEY privada de FIRMS
  SHAREPOINT_EVENTS_URL   -> enlace de descarga directa de events.json
  SHAREPOINT_META_URL     -> enlace de descarga directa de meta.json

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
RETENTION_DAYS = 90
GRID_SIZE_DEG = 0.25
GRID_AGGREGATION_TRIGGER = 1500  # solo agrega si un solo dia supera este umbral

REPO_ROOT = os.environ.get("GITHUB_WORKSPACE", ".")
FIRMS_JSON_PATH = os.path.join(REPO_ROOT, "firms_hotspots.json")
EVENTS_JSON_PATH = os.path.join(REPO_ROOT, "events.json")
META_JSON_PATH = os.path.join(REPO_ROOT, "meta.json")


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

    print(f"FIRMS: {len(new_points)} puntos nuevos de hoy, {len(combined)} puntos totales en la ventana de 90 dias")


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


def main():
    update_firms()
    sync_from_sharepoint()


if __name__ == "__main__":
    sys.exit(main())
