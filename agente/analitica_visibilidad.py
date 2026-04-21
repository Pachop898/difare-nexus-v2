"""
ORION — Módulo de Análisis: Plan de Visibilidad 2026
Compara venta/stock en PDVs CON visibilidad vs SIN visibilidad.
"""
import os
import glob
import time as _time
import pandas as pd
from agente import generar_pdfs as gp

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLAN_FILE = "Plan_Visibilidad_2026.xlsx"
_cache_vis: dict = {}
_cache_vis_ts: float = 0
_CACHE_TTL = 3600  # 1 hora

# ══════════════════════════════════════════════════════════════
# Carga del plan de visibilidad
# ══════════════════════════════════════════════════════════════

def _find_plan_file():
    """Busca el archivo del plan en la carpeta excels/."""
    candidates = [
        os.path.join(os.path.dirname(_HERE), "excels"),
        os.path.join(os.path.dirname(os.path.dirname(_HERE)), "excels"),
        "excels",
    ]
    for c in candidates:
        path = os.path.join(c, _PLAN_FILE)
        if os.path.isfile(path):
            return path
    return None


def _cargar_plan() -> dict:
    """
    Carga todas las pestañas del plan de visibilidad.
    Retorna dict con DataFrames normalizados.
    """
    path = _find_plan_file()
    if not path:
        raise FileNotFoundError(f"No se encontró {_PLAN_FILE} en excels/")

    productos = pd.read_excel(path, sheet_name="Productos")
    instore_ca = pd.read_excel(path, sheet_name="Instore Cruz Azul")
    instore_dr = pd.read_excel(path, sheet_name="Instore Dromayor")
    instore_sx = pd.read_excel(path, sheet_name="Instore Suerox")
    instore_fc = pd.read_excel(path, sheet_name="Instore Facial")

    # Normalizar columnas de Dromayor/Suerox/Facial para unificar
    for df, source in [(instore_dr, "Dromayor"), (instore_sx, "Suerox"), (instore_fc, "Facial")]:
        if "FormatoH" in df.columns:
            df.rename(columns={"FormatoH": "GrupoPDV", "CiudadH": "Ciudad", "ProvinciaH": "Provincia"}, inplace=True)

    # Unificar todos los PDV del plan en un solo DataFrame
    frames = []
    for df, acuerdo in [
        (instore_ca, "Cruz Azul"),
        (instore_dr, "Dromayor"),
        (instore_sx, "Suerox Frigos"),
        (instore_fc, "Facial"),
    ]:
        tmp = df.copy()
        tmp["Acuerdo"] = acuerdo
        frames.append(tmp)

    pdv_plan = pd.concat(frames, ignore_index=True)
    # Asegurar tipos string
    pdv_plan["CódLocal"] = pdv_plan["CódLocal"].astype(str).str.strip()

    # Mapeo: Elemento → lista de NEPTUNO DIFARE (SKUs negociados)
    productos["NEPTUNO DIFARE"] = productos["NEPTUNO DIFARE"].astype(str).str.strip()
    elemento_skus = {}
    for _, row in productos.iterrows():
        elem = row["Elemento"]
        nept = row["NEPTUNO DIFARE"]
        elemento_skus.setdefault(elem, []).append(nept)

    return {
        "productos": productos,
        "pdv_plan": pdv_plan,
        "elemento_skus": elemento_skus,
        "instore_ca": instore_ca,
        "instore_dr": instore_dr,
        "instore_sx": instore_sx,
        "instore_fc": instore_fc,
    }


# ══════════════════════════════════════════════════════════════
# Carga de data SAP para cruzar con plan
# ══════════════════════════════════════════════════════════════

def _carpeta_excels():
    candidates = [
        os.path.join(os.path.dirname(_HERE), "excels"),
        os.path.join(os.path.dirname(os.path.dirname(_HERE)), "excels"),
        "excels",
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return "excels"


def _cargar_sap_df() -> pd.DataFrame:
    """Carga el DataFrame SAP completo."""
    carpeta = _carpeta_excels()
    sap_path = gp.detectar_archivo_sap(carpeta)
    if not sap_path:
        raise FileNotFoundError("No se encontró archivo SAP")
    return pd.read_excel(sap_path)


# ══════════════════════════════════════════════════════════════
# Análisis principal
# ══════════════════════════════════════════════════════════════

def analisis_visibilidad(force: bool = False) -> dict:
    """
    Análisis completo del plan de visibilidad.
    Retorna dict con KPIs, tabla por elemento, detalle de stock.
    Cache de 1 hora.
    """
    global _cache_vis, _cache_vis_ts

    now = _time.time()
    if _cache_vis and not force and (now - _cache_vis_ts) < _CACHE_TTL:
        return _cache_vis

    t0 = _time.time()
    plan = _cargar_plan()
    sap = _cargar_sap_df()

    pdv_plan = plan["pdv_plan"]
    productos = plan["productos"]
    elemento_skus = plan["elemento_skus"]

    # Asegurar tipos
    sap["CODIGOPDV"] = sap["CODIGOPDV"].astype(str).str.strip()
    sap["IDNEPTUNO"] = sap["IDNEPTUNO"].astype(str).str.strip()

    # Set de códigos del plan
    codigos_plan = set(pdv_plan["CódLocal"].unique())

    # SKUs negociados (todos)
    skus_plan = set(productos["NEPTUNO DIFARE"].astype(str).str.strip().unique())

    # Filtrar SAP solo farmacias (excluir DIFARE S.A. = bodega)
    sap_farm = sap[sap["UNIDAD"] != "DIFARE S.A."].copy()

    # Detectar último día con stock
    dia_stock = sap_farm.groupby("DIA")["STOCK"].sum()
    dias_con_stock = dia_stock[dia_stock > 0]
    ultimo_dia_str = dias_con_stock.index.max() if not dias_con_stock.empty else sap_farm["DIA"].max()

    sap_ultimo = sap_farm[sap_farm["DIA"] == ultimo_dia_str].copy()

    # ── 1) Análisis POR ELEMENTO ──
    resultados_elementos = []

    for elemento, skus_elem in elemento_skus.items():
        skus_set = set(skus_elem)

        # PDVs que tienen ESTE elemento
        pdv_con_elem = set(pdv_plan[pdv_plan["Elemento"] == elemento]["CódLocal"].unique())
        n_pdv_plan = len(pdv_con_elem)

        if n_pdv_plan == 0:
            continue

        # Data del último día para estos SKUs
        sap_skus = sap_ultimo[sap_ultimo["IDNEPTUNO"].isin(skus_set)]

        # PDVs CON visibilidad
        sap_con = sap_skus[sap_skus["CODIGOPDV"].isin(pdv_con_elem)]
        # PDVs SIN visibilidad (mismo grupo de canales, mismos SKUs, pero NO en el plan)
        sap_sin = sap_skus[~sap_skus["CODIGOPDV"].isin(codigos_plan)]

        # Venta por PDV (sumar todos los SKUs del elemento)
        venta_con = sap_con.groupby("CODIGOPDV")["VENTA NETA RECUPERO"].sum()
        venta_sin = sap_sin.groupby("CODIGOPDV")["VENTA NETA RECUPERO"].sum()

        venta_prom_con = float(venta_con.mean()) if len(venta_con) > 0 else 0
        venta_prom_sin = float(venta_sin.mean()) if len(venta_sin) > 0 else 0
        venta_total_con = float(venta_con.sum())

        # Lift %
        lift = round((venta_prom_con / venta_prom_sin - 1) * 100, 1) if venta_prom_sin > 0 else 0

        # Stock en PDVs del plan (último día)
        stock_con = sap_con.groupby("CODIGOPDV")["STOCK"].sum()
        pdv_con_stock = set(stock_con[stock_con > 0].index)
        pdv_sin_stock = pdv_con_elem - pdv_con_stock  # PDVs del plan sin ningún stock

        # Cobertura: PDVs con presencia de AL MENOS 1 SKU del elemento
        pdv_con_presencia = set(sap_con[sap_con["STOCK"] > 0]["CODIGOPDV"].unique()) | \
                            set(sap_con[sap_con["VENTA NETA RECUPERO"] > 0]["CODIGOPDV"].unique())
        cobertura_pct = round(len(pdv_con_presencia) / n_pdv_plan * 100, 1) if n_pdv_plan > 0 else 0

        # Stock buckets (por PDV, total de SKUs del elemento)
        stock_0 = len(pdv_con_elem - set(stock_con[stock_con > 0].index))
        stock_1 = len(stock_con[(stock_con >= 1) & (stock_con <= 1)])
        stock_2 = len(stock_con[(stock_con >= 2) & (stock_con <= 2)])
        stock_3plus = len(stock_con[stock_con >= 3])

        # Determinar acuerdo(s) para este elemento
        acuerdos = pdv_plan[pdv_plan["Elemento"] == elemento]["Acuerdo"].unique()
        acuerdo_str = ", ".join(acuerdos)

        resultados_elementos.append({
            "elemento": elemento,
            "acuerdo": acuerdo_str,
            "n_pdv_plan": n_pdv_plan,
            "n_skus": len(skus_set),
            "venta_total": round(venta_total_con, 2),
            "venta_prom_con": round(venta_prom_con, 2),
            "venta_prom_sin": round(venta_prom_sin, 2),
            "lift_pct": lift,
            "cobertura_pct": cobertura_pct,
            "stock_0": stock_0,
            "stock_1": stock_1,
            "stock_2": stock_2,
            "stock_3plus": stock_3plus,
            "pdv_con_stock": len(pdv_con_stock),
            "pdv_sin_stock": len(pdv_sin_stock),
        })

    # Ordenar por venta total desc
    resultados_elementos.sort(key=lambda x: x["venta_total"], reverse=True)

    # ── 2) KPIs globales ──
    total_pdv_plan = len(codigos_plan)
    sap_skus_all = sap_ultimo[sap_ultimo["IDNEPTUNO"].isin(skus_plan)]

    sap_con_all = sap_skus_all[sap_skus_all["CODIGOPDV"].isin(codigos_plan)]
    sap_sin_all = sap_skus_all[~sap_skus_all["CODIGOPDV"].isin(codigos_plan)]

    venta_con_all = sap_con_all.groupby("CODIGOPDV")["VENTA NETA RECUPERO"].sum()
    venta_sin_all = sap_sin_all.groupby("CODIGOPDV")["VENTA NETA RECUPERO"].sum()

    venta_prom_con_global = float(venta_con_all.mean()) if len(venta_con_all) > 0 else 0
    venta_prom_sin_global = float(venta_sin_all.mean()) if len(venta_sin_all) > 0 else 0
    lift_global = round((venta_prom_con_global / venta_prom_sin_global - 1) * 100, 1) if venta_prom_sin_global > 0 else 0

    # Stock global
    stock_global = sap_con_all.groupby("CODIGOPDV")["STOCK"].sum()
    pdv_con_stock_global = len(stock_global[stock_global > 0])

    # Cobertura global
    pdv_con_presencia_global = set(sap_con_all[sap_con_all["STOCK"] > 0]["CODIGOPDV"].unique()) | \
                                set(sap_con_all[sap_con_all["VENTA NETA RECUPERO"] > 0]["CODIGOPDV"].unique())
    cobertura_global = round(len(pdv_con_presencia_global) / total_pdv_plan * 100, 1) if total_pdv_plan > 0 else 0

    # Parsear fecha del último día
    from agente.generar_pdfs import parsear_fecha_completa
    fecha_stock = parsear_fecha_completa(ultimo_dia_str)
    dia_num = int(fecha_stock.day) if not pd.isna(fecha_stock) else 0

    elapsed = round(_time.time() - t0, 1)
    print(f"[visibilidad] Análisis completado en {elapsed}s — {total_pdv_plan} PDVs del plan")

    result = {
        "kpis": {
            "total_pdv_plan": total_pdv_plan,
            "total_skus": len(skus_plan),
            "total_elementos": len(elemento_skus),
            "venta_prom_con": round(venta_prom_con_global, 2),
            "venta_prom_sin": round(venta_prom_sin_global, 2),
            "lift_pct": lift_global,
            "cobertura_pct": cobertura_global,
            "pdv_con_stock": pdv_con_stock_global,
            "pdv_sin_stock": total_pdv_plan - pdv_con_stock_global,
            "ultimo_dia_stock": dia_num,
        },
        "elementos": resultados_elementos,
    }

    _cache_vis = result
    _cache_vis_ts = _time.time()
    return result
