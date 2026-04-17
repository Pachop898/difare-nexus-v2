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
_cache_ts = 0  # timestamp de última carga
_CACHE_TTL = 3600  # 1 hora — se recarga automáticamente tras este tiempo
import time as _time

def _carpeta():
    return EXCELS_DIR if os.path.isdir(EXCELS_DIR) else "excels"

def _excels_mtime() -> float:
    """Retorna el mtime más reciente de cualquier .xlsx en la carpeta."""
    import glob
    carpeta = _carpeta()
    archivos = glob.glob(os.path.join(carpeta, "*.xlsx"))
    if not archivos:
        return 0
    return max(os.path.getmtime(f) for f in archivos)

def cargar_data(force: bool = False) -> dict:
    """
    Devuelve un dict con todos los DataFrames base ya calculados.
    Cachea en memoria con TTL de 1 hora.
    Se auto-invalida si algún Excel fue modificado después del último cache.
    Usa force=True para forzar recarga.
    """
    global _cache_ts
    now = _time.time()
    # Auto-invalidar si: forzado, TTL expirado, o excels más nuevos
    if _cache and not force:
        if (now - _cache_ts) < _CACHE_TTL:
            # Verificar si excels fueron actualizados
            try:
                if _excels_mtime() <= _cache_ts:
                    return _cache
            except Exception:
                return _cache
        print("[analitica] Cache expirado o excels actualizados, recargando…")

    carpeta = _carpeta()
    t0 = _time.time()
    df_todos = gp.cargar_todos_excels(carpeta)
    bodega, farm_stock_ult, farm_todo = gp.cargar_sap_completo(carpeta)
    universo = gp.calcular_universo_pdv(carpeta)
    stock_por_mes = gp.cargar_stock_por_mes(carpeta)
    ultimo_dia, dias_mes, mes_completo = gp.detectar_ultimo_dia_y_proyeccion(carpeta)
    elapsed = round(_time.time() - t0, 1)
    print(f"[analitica] Data cargada en {elapsed}s — {len(df_todos)} filas")

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
    _cache_ts = _time.time()
    return _cache


def invalidar_cache():
    global _cache_ts
    _cache.clear()
    _cache_ts = 0


# ══════════════════════════════════════════════════════════════
# Mapeo GRUPOPDV → nombre Genomma (agrupado)
# ══════════════════════════════════════════════════════════════

_GRUPO_DISPLAY = {
    "Cafa Mostrador":     "Cruz Azul Mostrador",
    "Cafi Mostrador":     "Cruz Azul Mostrador",
    "Cofa Mostrador":     "Cruz Azul Mostrador",
    "Cafa Autoservicio":  "Cruz Azul Autoservicios",
    "Cafi Autoservicio":  "Cruz Azul Autoservicios",
    "Cofa Autoservicio":  "Cruz Azul Autoservicios",
    # Los demás se muestran tal cual: Pharmacys, Dromayor, etc.
}

def _grupo_display(raw: str) -> str:
    """Convierte nombre interno DIFARE → nombre Genomma."""
    return _GRUPO_DISPLAY.get(raw, raw)

def _grupo_raw_values(display_names: list[str]) -> list[str]:
    """Dado nombres Genomma, devuelve TODOS los valores raw que matchean."""
    # Build reverse map: display_name → [raw1, raw2, ...]
    rev: dict[str, list[str]] = {}
    for raw, disp in _GRUPO_DISPLAY.items():
        rev.setdefault(disp, []).append(raw)
    result = []
    for name in display_names:
        if name in rev:
            result.extend(rev[name])
        else:
            result.append(name)  # Pharmacys, Dromayor, etc. → sin mapeo
    return result


# ══════════════════════════════════════════════════════════════
# KPIs principales (alimenta /api/kpis)
# ══════════════════════════════════════════════════════════════

def _aplicar_filtros_df(df, marca=None, canal=None, grupos=None, productos=None):
    """Aplica filtros comunes a un DataFrame de ventas."""
    if marca:
        df = df[df["MARCA"].astype(str).str.contains(marca, case=False, na=False)]
    if canal:
        df = df[df["UNIDAD"] == canal]
    if grupos and "GRUPOPDV" in df.columns:
        # Expandir nombres Genomma → valores raw DIFARE
        raw_vals = _grupo_raw_values(grupos)
        df = df[df["GRUPOPDV"].isin(raw_vals)]
    if productos:
        df = df[df["PRODUCTO"].isin(productos)]
    return df


def kpis_dashboard(marca: str | None = None, canal: str | None = None,
                   grupos: list | None = None, productos: list | None = None) -> dict:
    d = cargar_data()
    df = d["df_todos"]
    if df.empty:
        return {"error": "no hay data"}

    df = _aplicar_filtros_df(df, marca=marca, canal=canal, grupos=grupos, productos=productos)

    farm = df[df["UNIDAD"] == "FARMACIAS"]
    dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"]

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


def filtros_disponibles(marca: str | None = None) -> dict:
    """Retorna las opciones de filtros disponibles: marcas, grupos, productos (cascadeados por marca)."""
    d = cargar_data()
    df = d["df_todos"]
    farm_todo = d.get("farm_todo")

    # Marcas de todos los datos
    marcas = sorted(df["MARCA"].dropna().unique().tolist()) if "MARCA" in df.columns else []

    # Canales (unidades de negocio)
    canales = []
    if "UNIDAD" in df.columns:
        canales = [u for u in ["FARMACIAS", "DISTRIBUCION DIFARE"]
                   if u in df["UNIDAD"].unique()]

    # Grupos PDV desde farm_todo (farmacias) — mapeados a nombres Genomma
    grupos = []
    if farm_todo is not None and not farm_todo.empty:
        if "GRUPOPDV" in farm_todo.columns:
            raw_grupos = farm_todo["GRUPOPDV"].dropna().unique().tolist()
            grupos = sorted(set(_grupo_display(g) for g in raw_grupos))

    # Productos — cascadeados por marca si se pasa
    df_prod = df
    if marca:
        df_prod = df_prod[df_prod["MARCA"].astype(str).str.contains(marca, case=False, na=False)]
    productos = sorted(df_prod["PRODUCTO"].dropna().unique().tolist()) if "PRODUCTO" in df_prod.columns else []

    return {
        "marcas": marcas,
        "canales": canales,
        "grupos": grupos,
        "productos": productos,
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


def venta_por_canal_mes(marca: str | None = None, canal: str | None = None,
                        grupos: list | None = None, productos: list | None = None) -> list[dict]:
    """
    Devuelve la venta mensual desglosada por canal.
    Para el mes en curso (incompleto) añade campos de proyección.
    """
    d = cargar_data()
    df = d["df_todos"]
    if df.empty:
        return []
    # Solo canales de venta real (excluye 'DIFARE S.A.' que es bodega)
    df = df[df["UNIDAD"].isin(["FARMACIAS", "DISTRIBUCION DIFARE"])]
    df = _aplicar_filtros_df(df, marca=marca, canal=canal, grupos=grupos, productos=productos)

    g = (df.groupby(["MES", "UNIDAD"], dropna=True)["VENTA NETA RECUPERO"]
           .sum().reset_index())
    piv = g.pivot(index="MES", columns="UNIDAD", values="VENTA NETA RECUPERO").fillna(0)
    piv = piv.reset_index()

    ultimo_dia = int(d.get("ultimo_dia_venta") or 0)
    dias_mes = int(d.get("dias_mes") or 30)
    mes_completo = bool(d.get("mes_completo"))
    # El mes actual (incompleto) es el más alto presente si mes_completo=False
    meses_raw = [_mes_num(r["MES"]) for _, r in piv.iterrows()]
    meses_validos = [m for m in meses_raw if m is not None]
    mes_actual = max(meses_validos) if meses_validos and not mes_completo else None
    factor = (dias_mes / ultimo_dia) if (ultimo_dia and dias_mes) else 1.0

    out = []
    for _, r in piv.iterrows():
        n = _mes_num(r["MES"])
        if n is None:
            continue
        f = float(r.get("FARMACIAS", 0) or 0)
        dist = float(r.get("DISTRIBUCION DIFARE", 0) or 0)
        fila = {
            "mes": n,
            "farmacias": f,
            "distribucion": dist,
            "total": f + dist,
            "proyectado": False,
        }
        if n == mes_actual and factor > 1.0:
            f_proy = f * factor
            dist_proy = dist * factor
            fila.update({
                "proyectado": True,
                "ultimo_dia": ultimo_dia,
                "dias_mes": dias_mes,
                "farmacias_proy": f_proy,
                "distribucion_proy": dist_proy,
                "total_proy": f_proy + dist_proy,
                # delta = lo que falta por vender para llegar a la proyección
                "farmacias_delta": max(f_proy - f, 0),
                "distribucion_delta": max(dist_proy - dist, 0),
                "total_delta": max((f_proy + dist_proy) - (f + dist), 0),
            })
        out.append(fila)
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
    DOIS (Días de Inventario) — fórmula idéntica al reporte PDF de agente-excel.

    DOIS = Stock_Valorizado / Venta_Diaria_SAP
    Donde Venta_Diaria_SAP = Venta del archivo SAP (mes actual) / dias transcurridos.

    Importante:
    - Bodega DOIS: stock bodega / venta diaria (FARMACIAS + DISTRIBUCIÓN)
    - PDV DOIS:    stock PDV   / venta diaria (solo FARMACIAS)
    - Total DOIS:  stock total / venta diaria (FARMACIAS + DISTRIBUCIÓN)
    - Usa SOLO venta del SAP (mes actual), NO la venta acumulada de todos los meses
    """
    d = cargar_data()
    bodega = d["bodega"]
    farm_stock = d["farm_stock_ult"]
    ultimo_dia = max(d["ultimo_dia_venta"], 1)
    dias_mes = d.get("dias_mes", 30) or 30

    # ── Venta del SAP (mes actual) ──
    # Cargar SAP directamente para obtener venta del mes en curso
    # (df_todos mezcla Ene-Mar + Abr, daría DOIS artificialmente bajo)
    carpeta = _carpeta()
    sap_path = gp.detectar_archivo_sap(carpeta)
    if sap_path:
        df_sap = pd.read_excel(sap_path)
        if producto:
            df_sap = df_sap[df_sap["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
        venta_sap_farm_dist = float(df_sap[df_sap["UNIDAD"].isin(
            ["FARMACIAS", "DISTRIBUCION DIFARE"])]["VENTA NETA RECUPERO"].sum())
        venta_sap_farm = float(df_sap[df_sap["UNIDAD"] == "FARMACIAS"]["VENTA NETA RECUPERO"].sum())
    else:
        # Fallback: usar df_todos (menos preciso)
        df_todos = d["df_todos"]
        if producto:
            df_todos = df_todos[df_todos["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
        venta_sap_farm_dist = float(df_todos[df_todos["UNIDAD"].isin(
            ["FARMACIAS", "DISTRIBUCION DIFARE"])]["VENTA NETA RECUPERO"].sum())
        venta_sap_farm = float(df_todos[df_todos["UNIDAD"] == "FARMACIAS"]["VENTA NETA RECUPERO"].sum())

    if producto:
        bodega = bodega[bodega["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
        farm_stock = farm_stock[farm_stock["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]

    # Stock VALORIZADO (USD)
    stock_bodega_val = float(bodega["STOCK_VALORIZADO"].sum()) if "STOCK_VALORIZADO" in bodega.columns else 0
    stock_pdv_val = float(farm_stock["STOCK_VALORIZADO"].sum()) if "STOCK_VALORIZADO" in farm_stock.columns else 0
    stock_total_val = stock_bodega_val + stock_pdv_val

    # Venta diaria del SAP (mes actual)
    venta_diaria_farm_dist = venta_sap_farm_dist / ultimo_dia if ultimo_dia else 0
    venta_diaria_farm = venta_sap_farm / ultimo_dia if ultimo_dia else 0

    # DOIS = Stock Valorizado / Venta Diaria
    # Bodega y Total usan venta Farm+Dist; PDV usa solo Farm
    dois_bodega = round(stock_bodega_val / venta_diaria_farm_dist, 1) if venta_diaria_farm_dist > 0 else None
    dois_pdv = round(stock_pdv_val / venta_diaria_farm, 1) if venta_diaria_farm > 0 else None
    dois_total = round(stock_total_val / venta_diaria_farm_dist, 1) if venta_diaria_farm_dist > 0 else None

    # Clasificación: >30 días = OK, 15-30 = bajo, <15 = crítico
    if dois_total is not None:
        if dois_total > 30:
            estado = "STOCK OK — inventario saludable"
        elif dois_total >= 15:
            estado = "STOCK BAJO — monitorear reabastecimiento"
        else:
            estado = "STOCK CRÍTICO — riesgo de desabasto"
    else:
        estado = "Sin datos suficientes"

    # Fórmula legible para que Claude la muestre
    fmt = lambda v: f"${v:,.2f}"
    formula_bodega = f"DOIS Bodega = {fmt(stock_bodega_val)} / ({fmt(venta_sap_farm_dist)}/{ultimo_dia}) = {fmt(stock_bodega_val)} / {fmt(venta_diaria_farm_dist)} = {dois_bodega} días"
    formula_pdv = f"DOIS PDV = {fmt(stock_pdv_val)} / ({fmt(venta_sap_farm)}/{ultimo_dia}) = {fmt(stock_pdv_val)} / {fmt(venta_diaria_farm)} = {dois_pdv} días"
    formula_total = f"DOIS Total = {fmt(stock_total_val)} / ({fmt(venta_sap_farm_dist)}/{ultimo_dia}) = {fmt(stock_total_val)} / {fmt(venta_diaria_farm_dist)} = {dois_total} días"

    return {
        "stock_bodega_valorizado": round(stock_bodega_val, 2),
        "stock_pdv_valorizado": round(stock_pdv_val, 2),
        "stock_total_valorizado": round(stock_total_val, 2),
        "venta_sap_farm_dist": round(venta_sap_farm_dist, 2),
        "venta_sap_farm": round(venta_sap_farm, 2),
        "venta_diaria_farm_dist": round(venta_diaria_farm_dist, 2),
        "venta_diaria_farm": round(venta_diaria_farm, 2),
        "dias_transcurridos": ultimo_dia,
        "dias_mes": dias_mes,
        "dois_bodega": dois_bodega,
        "dois_pdv": dois_pdv,
        "dois_total": dois_total,
        "estado_inventario": estado,
        "formula_bodega": formula_bodega,
        "formula_pdv": formula_pdv,
        "formula_total": formula_total,
    }


def dois_por_producto(umbral_min: float = 0, umbral_max: float = 9999,
                      top_n: int = 30) -> list[dict]:
    """
    Calcula DOIS individual por cada producto Pareto (80% de la venta).
    Permite filtrar por rango de DOIS: umbral_min <= dois <= umbral_max.
    Retorna lista ordenada por DOIS descendente.
    """
    d = cargar_data()
    bodega = d["bodega"]
    farm_stock = d["farm_stock_ult"]
    ultimo_dia = max(d["ultimo_dia_venta"], 1)

    # Venta SAP del mes actual
    carpeta = _carpeta()
    sap_path = gp.detectar_archivo_sap(carpeta)
    if sap_path:
        df_sap = pd.read_excel(sap_path)
    else:
        df_sap = d["df_todos"]

    # Productos Pareto (80% de la venta en farmacias)
    farm_sap = df_sap[df_sap["UNIDAD"] == "FARMACIAS"]
    venta_prod = farm_sap.groupby(["IDNEPTUNO", "MARCA", "PRODUCTO"]).agg(
        venta=("VENTA NETA RECUPERO", "sum")
    ).reset_index().sort_values("venta", ascending=False)
    total = venta_prod["venta"].sum()
    if total <= 0:
        return []
    venta_prod["pct"] = venta_prod["venta"] / total * 100
    venta_prod["pct_acum"] = venta_prod["pct"].cumsum()
    pareto = venta_prod[venta_prod["pct_acum"] <= 80].copy()
    if pareto.empty:
        pareto = venta_prod.head(20).copy()

    resultados = []
    for _, row in pareto.iterrows():
        idnep = row["IDNEPTUNO"]
        # Stock valorizado en bodega
        bod_prod = bodega[bodega["IDNEPTUNO"] == idnep]
        stk_bod = float(bod_prod["STOCK_VALORIZADO"].sum()) if "STOCK_VALORIZADO" in bodega.columns and not bod_prod.empty else 0
        # Stock valorizado en PDV
        pdv_prod = farm_stock[farm_stock["IDNEPTUNO"] == idnep]
        stk_pdv = float(pdv_prod["STOCK_VALORIZADO"].sum()) if "STOCK_VALORIZADO" in farm_stock.columns and not pdv_prod.empty else 0
        stk_total = stk_bod + stk_pdv
        # Venta diaria del producto
        venta_diaria = row["venta"] / ultimo_dia if ultimo_dia else 0
        # DOIS
        dois = round(stk_total / venta_diaria, 1) if venta_diaria > 0 else 0
        if umbral_min <= dois <= umbral_max:
            resultados.append({
                "IDNEPTUNO": idnep,
                "MARCA": row["MARCA"],
                "PRODUCTO": row["PRODUCTO"],
                "stock_bodega_val": round(stk_bod, 2),
                "stock_pdv_val": round(stk_pdv, 2),
                "stock_total_val": round(stk_total, 2),
                "venta_mes_sap": round(row["venta"], 2),
                "venta_diaria": round(venta_diaria, 2),
                "dois": dois,
            })

    resultados.sort(key=lambda x: x["dois"], reverse=True)
    return resultados[:top_n]


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
        p = producto.lower()
        rows = [r for r in rows
                if p in str(r.get("PRODUCTO", "")).lower()
                or p in str(r.get("MARCA", "")).lower()]

    marcas_en_pareto = set(r.get("MARCA", "?") for r in rows)
    print(f"[vectorización] Pareto: {len(rows)} productos, marcas: {sorted(marcas_en_pareto)}")
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

def exportar_vectorizacion_excel(producto: str = "", ruta_salida: str = "") -> str:
    """
    Genera el Informe de Vectorización semanal COMPLETO.
    Una pestaña por MARCA con los productos Pareto (80% de venta).
    Cada fila es un PDV que necesita acción: sin vectorizar, stock=0, stock<=1, stock<=2.

    Regla de SUGERIDO (todas las marcas excepto Suerox):
      - SIN VECTORIZAR → 2 unidades
      - Stock=0 → 2 unidades
      - Stock<=1 → 2 unidades
      - Stock<=2 → 1 unidad

    Regla de SUGERIDO para SUEROX:
      Grupo A (Mostrador + Bodegas): SIN VECT→6, Stock=0→6, <=1→5, <=2→4, <=3→3
      Grupo B (Autoservicio + Pharmacys + Dromayor): SIN VECT→12, Stock=0→12, <=1→11, <=2→10, <=3→9
    """
    d = cargar_data()
    farm_todo = d["farm_todo"]
    farm_stock = d["farm_stock_ult"]

    # Obtener Pareto (80% de venta)
    pareto_rows = oportunidad_vectorizacion(
        producto=producto if producto else None,
        top_n=100
    )
    if not pareto_rows:
        raise ValueError("No se encontraron productos Pareto para generar el informe")

    # Universo de PDV activos
    universo_pdv = set(farm_todo["POS"].dropna().unique())

    # Info de cada PDV: GRUPOPDV, CODIGOPDV, etc.
    pdv_info = (farm_todo.groupby("POS")
                         .agg(GRUPOPDV=("GRUPOPDV", "first"),
                              CODIGOPDV=("CODIGOPDV", "first"))
                         .reset_index())
    pdv_info_map = {r["POS"]: r for _, r in pdv_info.iterrows()}

    # Clasificar GRUPOPDV para Suerox
    SUEROX_GRUPO_A = {"Cafa Mostrador", "Cafi Mostrador", "Cofa Mostrador",
                      "Bodegas Internas Privadas", "Bodegas Administrativas"}
    SUEROX_GRUPO_B = {"Cafa Autoservicio", "Cafi Autoservicio", "Pharmacys", "Dromayor"}

    def sugerido_normal(stock_status):
        if stock_status == "SIN VECTORIZAR": return 2
        if stock_status == 0: return 2
        if stock_status <= 1: return 2
        if stock_status <= 2: return 1
        return 0

    def sugerido_suerox_a(stock_status):
        if stock_status == "SIN VECTORIZAR": return 6
        if stock_status == 0: return 6
        if stock_status <= 1: return 5
        if stock_status <= 2: return 4
        if stock_status <= 3: return 3
        return 0

    def sugerido_suerox_b(stock_status):
        if stock_status == "SIN VECTORIZAR": return 12
        if stock_status == 0: return 12
        if stock_status <= 1: return 11
        if stock_status <= 2: return 10
        if stock_status <= 3: return 9
        return 0

    # Agrupar Pareto por marca
    marcas = {}
    for r in pareto_rows:
        m = r.get("MARCA", "Otros")
        marcas.setdefault(m, []).append(r)

    if not ruta_salida:
        import tempfile
        ruta_salida = os.path.join(tempfile.gettempdir(), "vectorizacion_semanal.xlsx")
    os.makedirs(os.path.dirname(ruta_salida) or ".", exist_ok=True)

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1B3A6B")
    data_font = Font(name="Arial", size=9)
    gold_font = Font(name="Arial", size=9, bold=True, color="C9A84C")
    red_font = Font(name="Arial", size=9, bold=True, color="FF0000")
    border = Border(
        bottom=Side(style="thin", color="D0D0D0")
    )
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")

    resumen_rows = []

    for marca, prods in marcas.items():
        sheet_name = marca[:31]  # Excel 31 char limit
        ws = wb.create_sheet(title=sheet_name)

        headers = ["GRUPOPDV", "CODIGOPDV", "POS", "IDNEPTUNO", "IDDIFARE",
                    "PRODUCTO", "STOCK UNIDADES", "SUGERIDO"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center

        row_idx = 2
        total_sugerido = 0
        total_pdv_accionables = 0

        for prod_info in prods:
            idneptuno = prod_info.get("IDNEPTUNO", "")
            producto_nombre = prod_info.get("PRODUCTO", "")
            is_suerox = marca.upper() == "SUEROX"

            # Buscar PDVs y su stock del último día para este producto
            prod_stock_df = farm_stock[farm_stock["IDNEPTUNO"] == idneptuno] if farm_stock is not None and not farm_stock.empty else pd.DataFrame()
            prod_presencia_df = farm_todo[farm_todo["IDNEPTUNO"] == idneptuno] if farm_todo is not None and not farm_todo.empty else pd.DataFrame()

            pdv_con_presencia = set(prod_presencia_df["POS"].dropna().unique())
            pdv_stock_map = {}
            if not prod_stock_df.empty:
                for _, sr in prod_stock_df.iterrows():
                    pos = sr.get("POS")
                    if pd.notna(pos):
                        pdv_stock_map[pos] = sr.get("STOCK", 0) or 0

            # PDVs sin vectorizar: en universo pero sin ninguna presencia histórica
            pdv_sin_vectorizar = universo_pdv - pdv_con_presencia

            # Construir filas para PDVs que necesitan acción
            filas_producto = []

            # 1) Sin vectorizar
            for pos in sorted(pdv_sin_vectorizar):
                info = pdv_info_map.get(pos, {})
                grupo = info.get("GRUPOPDV", "") if isinstance(info, dict) else (info["GRUPOPDV"] if "GRUPOPDV" in info else "")
                if is_suerox:
                    if grupo in SUEROX_GRUPO_B:
                        sug = sugerido_suerox_b("SIN VECTORIZAR")
                    else:
                        sug = sugerido_suerox_a("SIN VECTORIZAR")
                else:
                    sug = sugerido_normal("SIN VECTORIZAR")
                filas_producto.append({
                    "GRUPOPDV": grupo,
                    "CODIGOPDV": info.get("CODIGOPDV", "") if isinstance(info, dict) else (info["CODIGOPDV"] if "CODIGOPDV" in info else ""),
                    "POS": pos,
                    "IDNEPTUNO": idneptuno,
                    "IDDIFARE": prod_info.get("IDDIFARE", ""),
                    "PRODUCTO": producto_nombre,
                    "STOCK UNIDADES": "SIN VECTORIZAR",
                    "SUGERIDO": sug
                })

            # 2) PDVs con presencia pero stock bajo
            for pos in sorted(pdv_con_presencia):
                stock = pdv_stock_map.get(pos)
                if stock is None:
                    stock = 0  # no apareció en último día = stock 0

                info = pdv_info_map.get(pos, {})
                grupo = info.get("GRUPOPDV", "") if isinstance(info, dict) else (info["GRUPOPDV"] if "GRUPOPDV" in info else "")

                if is_suerox:
                    max_threshold = 3
                    if grupo in SUEROX_GRUPO_B:
                        sug = sugerido_suerox_b(stock)
                    else:
                        sug = sugerido_suerox_a(stock)
                else:
                    max_threshold = 2
                    sug = sugerido_normal(stock)

                if sug > 0:  # Solo incluir si necesita acción
                    filas_producto.append({
                        "GRUPOPDV": grupo,
                        "CODIGOPDV": info.get("CODIGOPDV", "") if isinstance(info, dict) else (info["CODIGOPDV"] if "CODIGOPDV" in info else ""),
                        "POS": pos,
                        "IDNEPTUNO": idneptuno,
                        "IDDIFARE": prod_info.get("IDDIFARE", ""),
                        "PRODUCTO": producto_nombre,
                        "STOCK UNIDADES": stock,
                        "SUGERIDO": sug
                    })

            # Escribir filas al Excel
            for fila in filas_producto:
                for c, h in enumerate(headers, 1):
                    cell = ws.cell(row=row_idx, column=c, value=fila[h])
                    cell.font = data_font
                    cell.border = border
                    cell.alignment = left if c <= 6 else center
                    # Resaltar SIN VECTORIZAR en rojo
                    if h == "STOCK UNIDADES" and fila[h] == "SIN VECTORIZAR":
                        cell.font = red_font
                    elif h == "SUGERIDO":
                        cell.font = gold_font
                row_idx += 1
                total_sugerido += fila["SUGERIDO"]
                total_pdv_accionables += 1

        # Ajustar anchos de columna
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 12
        ws.column_dimensions["C"].width = 45
        ws.column_dimensions["D"].width = 12
        ws.column_dimensions["E"].width = 12
        ws.column_dimensions["F"].width = 35
        ws.column_dimensions["G"].width = 18
        ws.column_dimensions["H"].width = 12

        resumen_rows.append({
            "Marca": marca,
            "Productos Pareto": len(prods),
            "PDVs accionables": total_pdv_accionables,
            "Total unidades sugeridas": total_sugerido,
        })

    # Hoja Resumen al inicio
    ws_res = wb.create_sheet(title="Resumen", index=0)
    res_headers = ["Marca", "Productos Pareto", "PDVs accionables", "Total unidades sugeridas"]
    for c, h in enumerate(res_headers, 1):
        cell = ws_res.cell(row=1, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
    for i, r in enumerate(resumen_rows, 2):
        for c, h in enumerate(res_headers, 1):
            cell = ws_res.cell(row=i, column=c, value=r[h])
            cell.font = data_font
            cell.alignment = center if c >= 2 else left
    ws_res.column_dimensions["A"].width = 18
    ws_res.column_dimensions["B"].width = 18
    ws_res.column_dimensions["C"].width = 18
    ws_res.column_dimensions["D"].width = 24

    wb.save(ruta_salida)
    return ruta_salida


# ══════════════════════════════════════════════════════════════
# 7) Distribución numérica — clientes atendidos por RUC (canal distributivo)
# ══════════════════════════════════════════════════════════════

def venta_por_canal_farmacia(marca: str | None = None,
                             producto: str | None = None,
                             mes: int | None = None) -> dict:
    """
    Analiza la venta en FARMACIAS desglosada por CANAL (columna 'CANAL'):
    APP, Call Center, Mostrador, Tienda Virtual, etc.

    Clasificación de negocio:
    - Mostrador = Canal PRESENCIAL
    - Todo lo demás (APP, Call Center, Tienda Virtual, etc.) = Canal NO PRESENCIAL

    Devuelve:
    - Desglose por canal con venta, % participación
    - Resumen presencial vs no presencial
    - Evolución mensual si hay varios meses
    """
    d = cargar_data()
    df = d["df_todos"]

    # Solo farmacias
    farm = df[df["UNIDAD"] == "FARMACIAS"].copy()
    if farm.empty:
        return {"error": "No hay datos de farmacias"}

    # Verificar que exista columna CANAL
    if "CANAL" not in farm.columns:
        return {"error": "No se encontró la columna CANAL en los datos"}

    # Filtros opcionales
    if marca:
        farm = farm[farm["MARCA"].astype(str).str.contains(marca, case=False, na=False)]
    if producto:
        farm = farm[farm["PRODUCTO"].astype(str).str.contains(producto, case=False, na=False)]
    if mes:
        farm["_mes_num"] = farm["MES"].apply(_mes_num)
        farm = farm[farm["_mes_num"] == mes]

    if farm.empty:
        return {"error": "No hay datos con los filtros aplicados"}

    venta_col = "VENTA NETA RECUPERO"
    total_venta = float(farm[venta_col].sum())

    # ── Desglose por canal ──
    por_canal = (farm.groupby("CANAL", dropna=False)[venta_col]
                     .sum().reset_index()
                     .rename(columns={venta_col: "venta"})
                     .sort_values("venta", ascending=False))
    por_canal["pct"] = (por_canal["venta"] / total_venta * 100).round(1)
    canales = por_canal.to_dict(orient="records")

    # ── Presencial vs No presencial ──
    venta_presencial = float(farm[farm["CANAL"].astype(str).str.upper().str.strip() == "MOSTRADOR"][venta_col].sum())
    venta_no_presencial = total_venta - venta_presencial
    pct_presencial = round(venta_presencial / total_venta * 100, 1) if total_venta > 0 else 0
    pct_no_presencial = round(100 - pct_presencial, 1)

    resumen_tipo = {
        "presencial_mostrador": round(venta_presencial, 2),
        "no_presencial_otros": round(venta_no_presencial, 2),
        "pct_presencial": pct_presencial,
        "pct_no_presencial": pct_no_presencial,
        "total": round(total_venta, 2),
    }

    # ── Evolución mensual (presencial vs no presencial) ──
    farm["_tipo_canal"] = farm["CANAL"].astype(str).str.upper().str.strip().apply(
        lambda x: "Presencial" if x == "MOSTRADOR" else "No presencial"
    )
    farm["_mes_n"] = farm["MES"].apply(_mes_num)
    evol = (farm.groupby(["_mes_n", "_tipo_canal"], dropna=True)[venta_col]
                .sum().reset_index()
                .rename(columns={venta_col: "venta", "_mes_n": "mes", "_tipo_canal": "tipo"}))
    evol = evol.sort_values(["mes", "tipo"])
    evolucion = evol.to_dict(orient="records")

    return {
        "marca_filtro": marca,
        "producto_filtro": producto,
        "mes_filtro": mes,
        "total_venta_farmacias": round(total_venta, 2),
        "canales": canales,
        "resumen_presencial_vs_no": resumen_tipo,
        "evolucion_mensual": evolucion,
    }


def distribucion_numerica(marca: str | None = None,
                          top_n: int = 20) -> dict:
    """
    Analiza la distribución numérica del canal DISTRIBUCION DIFARE:
    - Clientes únicos atendidos (por RUC) en cada mes
    - Comparativa mensual: ¿cuántos clientes nuevos? ¿cuántos se perdieron?
    - Penetración del portafolio TOP: ¿cuántos productos distintos compra cada cliente?
    Si se pasa 'marca', filtra solo esa marca.
    """
    d = cargar_data()
    df = d["df_todos"]
    dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"].copy()

    if dist.empty:
        return {"error": "No hay datos de distribución"}

    # Filtrar por marca si aplica
    if marca:
        dist = dist[dist["MARCA"].astype(str).str.contains(marca, case=False, na=False)]
        if dist.empty:
            return {"error": f"Marca no encontrada en distribución: {marca}"}

    # Identificar columna RUC
    ruc_col = "RUC" if "RUC" in dist.columns else "PROPIETARIO"

    # Asegurar columna MES
    if "MES" not in dist.columns:
        return {"error": "No hay columna MES en los datos"}

    meses = sorted(dist["MES"].dropna().unique().tolist())

    # Clientes por mes
    clientes_por_mes = {}
    for mes in meses:
        mes_data = dist[dist["MES"] == mes]
        rucs = set(mes_data[ruc_col].dropna().unique())
        clientes_por_mes[mes] = rucs

    # Construir resumen mensual
    resumen_meses = []
    prev_rucs = set()
    for mes in meses:
        rucs = clientes_por_mes[mes]
        nuevos = rucs - prev_rucs if prev_rucs else set()
        perdidos = prev_rucs - rucs if prev_rucs else set()
        resumen_meses.append({
            "mes": mes,
            "clientes_atendidos": len(rucs),
            "clientes_nuevos": len(nuevos),
            "clientes_perdidos": len(perdidos),
            "variacion_neta": len(nuevos) - len(perdidos),
        })
        prev_rucs = rucs

    # Portafolio TOP: productos Pareto (80% de venta)
    venta_prod = (dist.groupby("PRODUCTO")["VENTA NETA RECUPERO"].sum()
                      .sort_values(ascending=False))
    total = venta_prod.sum()
    if total > 0:
        venta_prod_pct = (venta_prod / total * 100).cumsum()
        productos_top = venta_prod_pct[venta_prod_pct <= 80].index.tolist()
        if not productos_top:
            productos_top = venta_prod.head(10).index.tolist()
    else:
        productos_top = []

    # Penetración del portafolio TOP por cliente (último mes)
    ultimo_mes = meses[-1] if meses else None
    penetracion = []
    if ultimo_mes and productos_top:
        ult = dist[(dist["MES"] == ultimo_mes)]
        por_cliente = (ult.groupby([ruc_col, "PROPIETARIO"])
                         .agg(
                             productos_comprados=("PRODUCTO", "nunique"),
                             productos_top_comprados=("PRODUCTO", lambda x: len(set(x) & set(productos_top))),
                             venta_total=("VENTA NETA RECUPERO", "sum"),
                         )
                         .reset_index()
                         .sort_values("venta_total", ascending=False))
        por_cliente["pct_portafolio_top"] = (
            por_cliente["productos_top_comprados"] / max(len(productos_top), 1) * 100
        ).round(1)
        penetracion = por_cliente.head(top_n).to_dict(orient="records")

    # Universo total de clientes (histórico)
    todos_rucs = set()
    for rucs in clientes_por_mes.values():
        todos_rucs |= rucs

    return {
        "marca_filtro": marca,
        "total_clientes_historico": len(todos_rucs),
        "productos_top_count": len(productos_top),
        "productos_top": productos_top[:15],  # primeros 15 para contexto
        "resumen_meses": resumen_meses,
        "penetracion_portafolio_ultimo_mes": penetracion,
    }
