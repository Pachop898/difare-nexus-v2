"""
agente/analitica.py — Capa de cálculo pura para DIFARE NEXUS v2

Reutiliza las funciones ya validadas en generar_pdfs.py (proyecto agente-excel),
pero las expone como una API limpia que el backend Flask puede consumir desde
los endpoints /api/* sin tocar matplotlib ni reportlab.

Toda la lógica numérica vive en generar_pdfs.py (fuente de verdad). Aquí solo
re-exportamos y añadimos los wrappers nuevos que pide el dashboard gerencial:

  - tendencia_marca(unidad_negocio=None, comparar_yoy=False)
  - ranking_pdv(canal="FARMACIAS", top_n=50)
  - pareto_pdv()
  - dias_inventario(producto=None)
  - proyeccion_venta(horizonte_dias=30)
  - oportunidad_vectorizacion(producto)
  - sugerido_stock(grupo_farmacia=None, productos_top=20,
                   lead_time=2, buffer=8)
  - exportar_vectorizacion_excel(producto, ruta_salida)

Parámetros de negocio por defecto (Fase 1, confirmados con Pacho 2026-04-06):
  - LEAD_TIME_DIAS = 2
  - BUFFER_DIAS    = 8
  - DIAS_INV_SEGURIDAD = LEAD_TIME_DIAS + BUFFER_DIAS = 10
"""

from __future__ import annotations

import os
import sys
import pandas as pd

# generar_pdfs.py vive en la misma carpeta agente/. Lo importamos como
# módulo de cálculo. La carga de matplotlib en él es perezosa al renderizar,
# así que importarlo NO renderiza nada.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Stubs perezosos: generar_pdfs.py importa matplotlib y reportlab al cargar,
# pero analitica.py NUNCA renderiza gráficos ni PDFs. En entornos donde
# matplotlib/reportlab no están instalados (p. ej. tests rápidos del backend
# o un Railway sin matplotlib) inyectamos stubs vacíos para que el import
# no falle. La generación real de PDFs se hace en otra ruta y allí sí
# tendrá las dependencias instaladas.
import importlib, types
def _stub(name, attrs=None):
    if name in sys.modules:
        return
    m = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(m, k, v)
    sys.modules[name] = m

try:
    importlib.import_module("matplotlib")
except ImportError:
    _stub("matplotlib")
    _stub("matplotlib.pyplot")
try:
    importlib.import_module("reportlab")
except ImportError:
    for n in ("reportlab", "reportlab.lib", "reportlab.lib.pagesizes",
              "reportlab.lib.colors", "reportlab.lib.units",
              "reportlab.platypus", "reportlab.lib.styles",
              "reportlab.lib.enums"):
        _stub(n)
    # atributos mínimos usados a nivel de módulo en generar_pdfs
    sys.modules["reportlab.lib.pagesizes"].A4 = None
    sys.modules["reportlab.lib.pagesizes"].landscape = lambda x: x
    class _ColorsStub:
        @staticmethod
        def HexColor(s): return s
        white = "#FFFFFF"
    sys.modules["reportlab.lib"].colors = _ColorsStub
    sys.modules["reportlab.lib.colors"] = _ColorsStub  # type: ignore
    sys.modules["reportlab.lib.units"].cm = 1
    for cls in ("SimpleDocTemplate", "Paragraph", "Spacer", "Table", "TableStyle", "Image"):
        setattr(sys.modules["reportlab.platypus"], cls, type(cls, (), {}))
    sys.modules["reportlab.lib.styles"].getSampleStyleSheet = lambda: {}
    sys.modules["reportlab.lib.styles"].ParagraphStyle = type("ParagraphStyle", (), {})
    sys.modules["reportlab.lib.enums"].TA_CENTER = 0
    sys.modules["reportlab.lib.enums"].TA_LEFT = 1

import generar_pdfs as gp  # noqa: E402

# ── Parámetros de negocio (configurables por panel admin en Fase 1) ──
LEAD_TIME_DIAS = 2
BUFFER_DIAS = 8
DIAS_INV_SEGURIDAD = LEAD_TIME_DIAS + BUFFER_DIAS  # 10

# Buscar carpeta excels/ en varias ubicaciones candidatas
_EXCELS_CANDIDATES = [
    os.path.join(os.path.dirname(_HERE), "excels"),              # /app/excels (Railway)
    os.path.join(os.path.dirname(os.path.dirname(_HERE)), "excels"),  # legacy
    os.path.join(_HERE, "excels"),                               # /app/agente/excels
    "excels",                                                     # relativo al CWD
]
EXCELS_DIR = next((p for p in _EXCELS_CANDIDATES if os.path.isdir(p)), _EXCELS_CANDIDATES[0])
print(f"[analitica] EXCELS_DIR = {EXCELS_DIR} (existe={os.path.isdir(EXCELS_DIR)})")


# ══════════════════════════════════════════════════════════════
# Carga base (cacheada en memoria por proceso)
# ══════════════════════════════════════════════════════════════

_cache = {}

def _carpeta():
    return EXCELS_DIR if os.path.isdir(EXCELS_DIR) else "excels"

def cargar_data(force: bool = False) -> dict:
    """
    Devuelve un dict con todos los DataFrames base ya calculados.
    Cachea en memoria; usa force=True tras subir un Excel nuevo.
    """
    if _cache and not force:
        return _cache

    carpeta = _carpeta()
    df_todos = gp.cargar_todos_excels(carpeta)
    bodega, farm_stock_ult, farm_todo = gp.cargar_sap_completo(carpeta)
    universo = gp.calcular_universo_pdv(carpeta)
    stock_por_mes = gp.cargar_stock_por_mes(carpeta)
    ultimo_dia, dias_mes, mes_completo = gp.detectar_ultimo_dia_y_proyeccion(carpeta)

    _cache.update({
        "df_todos": df_todos,
        "bodega": bodega,
        "farm_stock_ult": farm_stock_ult,
        "farm_todo": farm_todo,
        "universo_pdv": universo,
        "stock_por_mes": stock_por_mes,
        "ultimo_dia_venta": ultimo_dia,
        "dias_mes": dias_mes,
        "mes_completo": mes_completo,
    })
    return _cache


def invalidar_cache():
    _cache.clear()


# ══════════════════════════════════════════════════════════════
# KPIs principales (alimenta /api/kpis)
# ══════════════════════════════════════════════════════════════

def kpis_dashboard() -> dict:
    d = cargar_data()
    df = d["df_todos"]
    if df.empty:
        return {"error": "no hay data"}

    farm = df[df["UNIDAD"] == "FARMACIAS"]
    # 'DIFARE S.A.' = bodega · 'DISTRIBUCION DIFARE' = canal distributivo
    dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"]
    bodega = df[df["UNIDAD"] == "DIFARE S.A."]

    venta_farm = float(farm["VENTA NETA RECUPERO"].sum())
    venta_dist = float(dist["VENTA NETA RECUPERO"].sum())
    venta_total = venta_farm + venta_dist

    return {
        "venta_total": venta_total,
        "venta_farmacias": venta_farm,
        "venta_distribucion": venta_dist,
        "universo_pdv": d["universo_pdv"],
        "ultimo_dia_venta": d["ultimo_dia_venta"],
        "dias_mes": d["dias_mes"],
        "mes_completo": d["mes_completo"],
        "stock_por_mes": d["stock_por_mes"],
    }


# ══════════════════════════════════════════════════════════════
# 1) Tendencia por marca (pregunta KAM #1)
# ══════════════════════════════════════════════════════════════

def tendencia_marca(unidad_negocio: str | None = None,
                    comparar_yoy: bool = False) -> list[dict]:
    """
    unidad_negocio: 'FARMACIAS' | 'DIFARE S.A.' | None (total)
    comparar_yoy: en Fase 1 siempre False (no hay 2025 cargado todavía).
    Devuelve [{marca, mes, venta}, ...]
    """
    d = cargar_data()
    df = d["df_todos"]
    if df.empty:
        return []
    if unidad_negocio:
        df = df[df["UNIDAD"] == unidad_negocio]
    g = df.groupby(["MARCA", "MES"], dropna=True)["VENTA NETA RECUPERO"].sum().reset_index()
    g = g.rename(columns={"VENTA NETA RECUPERO": "venta"})
    return g.to_dict(orient="records")


def _mes_num(mes_raw) -> int | None:
    """Convierte 'MES' (formatos: '2026-01', '01', 1, '2026-1') a int 1-12."""
    if mes_raw is None:
        return None
    s = str(mes_raw).strip()
    if s in ("", "desconocido", "nan"):
        return None
    # Formato "YYYY-MM" o "YYYY-M"
    if "-" in s:
        try:
            return int(s.split("-")[1])
        except (ValueError, IndexError):
            return None
    # Formato numérico directo "1", "01", etc.
    try:
        n = int(float(s))
        return n if 1 <= n <= 12 else None
    except ValueError:
        return None


def venta_por_canal_mes() -> list[dict]:
    """
    Devuelve la venta mensual desglosada por canal.
    [{mes, farmacias, distribucion, total}, ...] ordenado por mes ascendente.
    """
    d = cargar_data()
    df = d["df_todos"]
    if df.empty:
        return []
    # Solo canales de venta real (excluye 'DIFARE S.A.' que es bodega)
    df = df[df["UNIDAD"].isin(["FARMACIAS", "DISTRIBUCION DIFARE"])]
    g = (df.groupby(["MES", "UNIDAD"], dropna=True)["VENTA NETA RECUPERO"]
           .sum().reset_index())
    piv = g.pivot(index="MES", columns="UNIDAD", values="VENTA NETA RECUPERO").fillna(0)
    piv = piv.reset_index()
    out = []
    for _, r in piv.iterrows():
        n = _mes_num(r["MES"])
        if n is None:
            continue
        f = float(r.get("FARMACIAS", 0) or 0)
        dist = float(r.get("DISTRIBUCION DIFARE", 0) or 0)
        out.append({
            "mes": n,
            "farmacias": f,
            "distribucion": dist,
            "total": f + dist,
        })
    out.sort(key=lambda x: x["mes"])
    return out


# ══════════════════════════════════════════════════════════════
# 4) Rankings (pregunta KAM #4)
# ══════════════════════════════════════════════════════════════

def ranking_pdv(canal: str = "FARMACIAS", top_n: int = 50) -> list[dict]:
    d = cargar_data()
    df = d["df_todos"]
    df = df[df["UNIDAD"] == canal]
    if df.empty:
        return []
    # FARMACIAS → key = POS (nombre + código de PDV).
    # DISTRIBUCION DIFARE → key = PROPIETARIO (cliente del canal distributivo).
    if canal == "FARMACIAS":
        key, label_col = "POS", "PROVINCIA"
    else:
        key, label_col = "PROPIETARIO", "GRUPOCLIENTE"

    cols_extra = [c for c in (label_col, "PROVINCIA", "CIUDAD", "GRUPOPDV", "GRUPOCLIENTE")
                  if c in df.columns and c != key]
    g = (df.groupby(key, dropna=False)
           .agg(**{
               "venta": ("VENTA NETA RECUPERO", "sum"),
               **{c: (c, "first") for c in cols_extra}
           })
           .reset_index()
           .rename(columns={key: "cliente"})
           .sort_values("venta", ascending=False)
           .head(top_n))
    return g.to_dict(orient="records")


def pareto_pdv() -> list[dict]:
    """Top farmacias que acumulan el 80% de la venta."""
    d = cargar_data()
    df = d["df_todos"]
    df = df[df["UNIDAD"] == "FARMACIAS"]
    if df.empty:
        return []
    g = (df.groupby("POS")["VENTA NETA RECUPERO"].sum()
           .sort_values(ascending=False).reset_index())
    total = g["VENTA NETA RECUPERO"].sum()
    if total <= 0:
        return []
    g["pct"] = g["VENTA NETA RECUPERO"] / total * 100
    g["pct_acum"] = g["pct"].cumsum()
    return g[g["pct_acum"] <= 80].to_dict(orient="records")


# ══════════════════════════════════════════════════════════════
# 2) Días de inventario y proyección de venta (pregunta KAM #2)
# ══════════════════════════════════════════════════════════════

def dias_inventario(producto: str | None = None) -> dict:
    """
    Días de inventario = stock_actual / venta_diaria_promedio.
    Si se pasa producto, filtra a ese IDNEPTUNO o nombre.
    """
    d = cargar_data()
    bodega = d["bodega"]
    farm_stock = d["farm_stock_ult"]
    df_todos = d["df_todos"]
    ultimo_dia = max(d["ultimo_dia_venta"], 1)

    if producto:
        bodega = bodega[bodega["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
        farm_stock = farm_stock[farm_stock["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
        df_todos = df_todos[df_todos["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]

    stock_bodega = float(bodega["STOCK"].sum())
    stock_pdv = float(farm_stock["STOCK"].sum())
    venta_mes = float(df_todos[df_todos["UNIDAD"] == "FARMACIAS"]["VENTA NETA RECUPERO"].sum())
    venta_diaria = venta_mes / ultimo_dia if ultimo_dia else 0

    return {
        "stock_bodega_unidades": stock_bodega,
        "stock_pdv_unidades": stock_pdv,
        "stock_total_unidades": stock_bodega + stock_pdv,
        "venta_diaria_promedio": venta_diaria,
        "dias_inventario_total": (stock_bodega + stock_pdv) / venta_diaria if venta_diaria else None,
        "dias_seguridad_minimo": DIAS_INV_SEGURIDAD,
        "lead_time_dias": LEAD_TIME_DIAS,
        "buffer_dias": BUFFER_DIAS,
    }


def proyeccion_venta(horizonte_dias: int = 30) -> dict:
    d = cargar_data()
    df = d["df_todos"]
    farm = df[df["UNIDAD"] == "FARMACIAS"]
    venta = float(farm["VENTA NETA RECUPERO"].sum())
    dias = max(d["ultimo_dia_venta"], 1)
    venta_diaria = venta / dias
    return {
        "venta_actual_mes": venta,
        "dias_transcurridos": dias,
        "venta_diaria_promedio": venta_diaria,
        "proyeccion_horizonte": venta_diaria * horizonte_dias,
        "horizonte_dias": horizonte_dias,
    }


# ══════════════════════════════════════════════════════════════
# 3) Vectorización y venta perdida (pregunta KAM #3)
# ══════════════════════════════════════════════════════════════

def oportunidad_vectorizacion(producto: str | None = None,
                              top_n: int = 20) -> list[dict]:
    """
    Para cada producto Pareto, calcula:
      - PDV con presencia (cualquier registro)
      - PDV con stock 0 hoy
      - venta perdida estimada = velocidad_promedio_cluster × #PDV faltantes
    Si se pasa 'producto', filtra a ese.
    Reusa calcular_pareto_farmacias del módulo legacy.
    """
    d = cargar_data()
    pareto = gp.calcular_pareto_farmacias(
        d["df_todos"], d["farm_stock_ult"], d["farm_todo"], d["universo_pdv"]
    )
    # gp.calcular_pareto_farmacias devuelve estructura — la normalizamos
    if isinstance(pareto, pd.DataFrame):
        rows = pareto.to_dict(orient="records")
    elif isinstance(pareto, list):
        rows = pareto
    else:
        rows = []

    if producto:
        rows = [r for r in rows
                if producto.lower() in str(r.get("PRODUCTO", r.get("producto", ""))).lower()]
    return rows[:top_n]


# ══════════════════════════════════════════════════════════════
# 5) Sugerido min/max de stock por grupo de farmacias (pregunta KAM #5)
# ══════════════════════════════════════════════════════════════

def sugerido_stock(grupo_farmacia: str | None = None,
                   productos_top: int = 20,
                   lead_time: int = LEAD_TIME_DIAS,
                   buffer: int = BUFFER_DIAS) -> list[dict]:
    """
    Sugerido por (grupo_farmacia, producto):
      - velocidad = venta_unidades_mes / dias_transcurridos
      - min = velocidad × (lead_time + buffer)
      - max = min × 1.5
    grupo_farmacia: nombre o None (todos).
    """
    d = cargar_data()
    df = d["df_todos"]
    farm = df[df["UNIDAD"] == "FARMACIAS"].copy()
    dias = max(d["ultimo_dia_venta"], 1)

    if grupo_farmacia and "GRUPO" in farm.columns:
        farm = farm[farm["GRUPO"].astype(str).str.contains(grupo_farmacia, case=False, na=False)]

    # Identificar productos top por venta
    top_prods = (farm.groupby("PRODUCTO")["VENTA NETA RECUPERO"].sum()
                     .sort_values(ascending=False).head(productos_top).index.tolist())
    farm_top = farm[farm["PRODUCTO"].isin(top_prods)]

    grupo_col = "GRUPO" if "GRUPO" in farm_top.columns else "ESTABLECIMIENTO"
    g = (farm_top.groupby([grupo_col, "PRODUCTO"])
                 .agg(venta_unidades=("CANTIDAD", "sum") if "CANTIDAD" in farm_top.columns
                                     else ("VENTA NETA RECUPERO", "sum"))
                 .reset_index())
    g["velocidad_diaria"] = g["venta_unidades"] / dias
    g["min_sugerido"] = (g["velocidad_diaria"] * (lead_time + buffer)).round().astype(int)
    g["max_sugerido"] = (g["min_sugerido"] * 1.5).round().astype(int)
    g["lead_time"] = lead_time
    g["buffer"] = buffer
    return g.to_dict(orient="records")


# ══════════════════════════════════════════════════════════════
# 6) Export Excel de vectorización (pregunta KAM #6)
# ══════════════════════════════════════════════════════════════

def exportar_vectorizacion_excel(producto: str, ruta_salida: str) -> str:
    """
    Genera un .xlsx con los PDV que NO tienen stock del producto, su
    velocidad histórica y el mínimo sugerido a enviar.
    Devuelve la ruta del archivo creado.
    """
    d = cargar_data()
    farm_todo = d["farm_todo"]
    farm_stock = d["farm_stock_ult"]
    dias = max(d["ultimo_dia_venta"], 1)

    prod_hist = farm_todo[farm_todo["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
    if prod_hist.empty:
        raise ValueError(f"Producto no encontrado: {producto}")

    pdv_con_presencia = set(prod_hist["POS"].dropna().unique())
    prod_stock = farm_stock[farm_stock["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
    pdv_con_stock = set(prod_stock[prod_stock["STOCK"] > 0]["POS"].dropna().unique())

    # Universo: todos los PDV activos del SAP (no solo los que ya venden el producto)
    universo_pdv = set(farm_todo["POS"].dropna().unique())
    pdv_sin_stock = universo_pdv - pdv_con_stock

    # Velocidad promedio del cluster (PDV que sí lo venden)
    venta_cluster = prod_hist.groupby("POS")["VENTA NETA RECUPERO"].sum()
    velocidad_cluster_diaria = (venta_cluster.mean() / dias) if not venta_cluster.empty else 0
    minimo_sugerido = max(int(round(velocidad_cluster_diaria * DIAS_INV_SEGURIDAD)), 1)

    # Construir tabla de salida
    info_pdv = (farm_todo.groupby("POS")
                         .agg(razon_social=("ESTABLECIMIENTO", "first"),
                              provincia=("PROVINCIA", "first") if "PROVINCIA" in farm_todo.columns
                                                              else ("ESTABLECIMIENTO", "first"))
                         .reset_index())
    out = info_pdv[info_pdv["POS"].isin(pdv_sin_stock)].copy()
    out["producto"] = producto
    out["velocidad_cluster_diaria"] = round(velocidad_cluster_diaria, 2)
    out["dias_inventario_seguridad"] = DIAS_INV_SEGURIDAD
    out["minimo_sugerido_unidades"] = minimo_sugerido
    out["ya_lo_vendia"] = out["POS"].isin(pdv_con_presencia)

    os.makedirs(os.path.dirname(ruta_salida) or ".", exist_ok=True)
    with pd.ExcelWriter(ruta_salida, engine="openpyxl") as w:
        out.to_excel(w, sheet_name="Vectorización sugerida", index=False)
        resumen = pd.DataFrame([{
            "producto": producto,
            "universo_pdv": len(universo_pdv),
            "pdv_con_stock": len(pdv_con_stock),
            "pdv_sin_stock": len(pdv_sin_stock),
            "cobertura_actual_pct": round(len(pdv_con_stock) / max(len(universo_pdv), 1) * 100, 1),
            "velocidad_cluster_diaria": round(velocidad_cluster_diaria, 2),
            "minimo_sugerido_unidades_por_pdv": minimo_sugerido,
            "total_unidades_a_enviar": minimo_sugerido * len(pdv_sin_stock),
        }])
        resumen.to_excel(w, sheet_name="Resumen", index=False)
    return ruta_salida
