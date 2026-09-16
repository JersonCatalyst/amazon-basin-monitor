#!/usr/bin/env python3
"""
setup_enso_sst_climatology.py

Script de UNA SOLA EJECUCION (no corre en GitHub Actions, no corre a diario).
Corre esto tu, una vez, desde tu maquina o desde Colab -- con tu login
interactivo normal de Earth Engine (ee.Authenticate()), igual que en tu
notebook original.

Que hace: calcula el promedio historico (climatologia) de temperatura
superficial del mar 1991-2020, dia por dia del anio (365 bandas), sobre la
region ampliada que cubre las 4 cajas ENSO (Nino 1+2, 3, 3.4 y la mitad
oriental de Nino 4), y lo exporta como un Earth Engine Asset permanente.

Por que como Asset y no recalculado cada dia: la climatologia NUNCA cambia
(es un promedio fijo de 30 anos ya cerrados). Recalcularla cada dia -- que es
lo que hacia el notebook original -- desperdicia cuota de Earth Engine
(EECU-horas/mes) en algo cuya respuesta siempre es identica. Con el Asset,
fetch_and_publish.py (que SI corre a diario en GitHub Actions) simplemente
lee el Asset ya calculado y solo computa la resta contra el dia mas reciente
-- eso si es barato y rapido.

Como correrlo:

  1. pip install earthengine-api
  2. python setup_enso_sst_climatology.py
     (te va a pedir autenticarte con tu cuenta de Google la primera vez,
     igual que en el notebook)
  3. El script lanza una tarea de exportacion asincrona en Earth Engine.
     Revisa su progreso en https://code.earthengine.google.com/tasks
     (o corriendo este mismo script con --status). Puede tardar varios
     minutos a bastantes minutos, dependiendo de la carga de Earth Engine
     ese dia -- es normal, es un calculo pesado que por eso se hace una
     sola vez.
  4. Cuando la tarea termine en estado COMPLETED, el Asset queda disponible
     en la ruta de abajo (ASSET_ID) para que fetch_and_publish.py lo use
     todos los dias.

Si mas adelante quieres actualizar la climatologia (por ejemplo cuando la
OMM publique un nuevo periodo base, algo que pasa cada 10 anios, no a diario),
vuelves a correr este mismo script -- sobrescribe el Asset existente.
"""

import argparse
import sys

import ee

GCP_PROJECT_ID = "ee-jersoncatalyst"

# Debe coincidir exactamente con SST_ROI_BOUNDS en scripts/fetch_and_publish.py
# y con la cobertura documentada en ENSO_REGIONS dentro de index.html.
ROI_BOUNDS = [-180, -15, -70, 15]  # min_lon, min_lat, max_lon, max_lat

ASSET_ID = "projects/ee-jersoncatalyst/assets/enso_sst_climatology_1991_2020"
CLIMATOLOGY_START = "1991-01-01"
CLIMATOLOGY_END = "2020-12-31"  # periodo base estandar OMM 1991-2020


def initialize():
    try:
        ee.Initialize(project=GCP_PROJECT_ID)
    except Exception:
        # Cualquier falla al inicializar (credenciales ausentes, corruptas,
        # vencidas, o el clasico EEException de "necesitas autenticarte")
        # se resuelve igual: forzar un login nuevo. No se limita a
        # ee.EEException porque un archivo de credenciales vacio/corrupto
        # revienta antes con json.JSONDecodeError, no con EEException.
        print("Inicializando flujo de autenticacion OAuth2...")
        ee.Authenticate(auth_mode="localhost")
        ee.Initialize(project=GCP_PROJECT_ID)


def compute_climatology(roi: "ee.Geometry") -> "ee.Image":
    """Promedio por dia-del-anio (365 bandas) de SST, recortado a la region."""
    sst_col = ee.ImageCollection("NOAA/CDR/OISST/V2_1").select("sst")
    base_col = sst_col.filterDate(CLIMATOLOGY_START, CLIMATOLOGY_END)
    doys = ee.List.sequence(1, 365)

    def compute_doy_mean(doy):
        mean_img = base_col.filter(ee.Filter.calendarRange(doy, doy, "day_of_year")).mean()
        return mean_img.multiply(0.01).rename(ee.String("doy_").cat(ee.Number(doy).format("%03d")))

    clim_img = ee.ImageCollection.fromImages(doys.map(compute_doy_mean)).toBands()
    band_names = clim_img.bandNames().map(lambda b: ee.String(b).replace("^[0-9]+_", ""))
    return clim_img.rename(band_names).clip(roi)


def launch_export():
    initialize()
    roi = ee.Geometry.BBox(*ROI_BOUNDS)
    climatology = compute_climatology(roi)

    task = ee.batch.Export.image.toAsset(
        image=climatology,
        description="enso_sst_climatology_1991_2020",
        assetId=ASSET_ID,
        region=roi,
        scale=27750,  # resolucion nativa aprox. de NOAA OISST (0.25 grados)
        maxPixels=1e10,
    )
    task.start()
    print(f"Tarea de exportacion lanzada: {task.id}")
    print(f"Asset destino: {ASSET_ID}")
    print("Revisa el progreso en https://code.earthengine.google.com/tasks")
    print("(o vuelve a correr este script con --status para consultarlo desde aqui)")


def check_status():
    initialize()
    tasks = ee.batch.Task.list()
    matching = [t for t in tasks if "enso_sst_climatology" in (t.config.get("description") or "")]
    if not matching:
        print("No se encontro ninguna tarea de exportacion de la climatologia ENSO todavia.")
        return
    for t in matching[:5]:
        status = t.status()
        print(f"- {t.id}: {status.get('state')}")
        if status.get("state") == "FAILED":
            print(f"  error: {status.get('error_message')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="Consultar el estado de la tarea en vez de lanzarla")
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        launch_export()
    sys.exit(0)
