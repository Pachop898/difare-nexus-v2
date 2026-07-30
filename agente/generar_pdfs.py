import pandas as pd
import matplotlib.pyplot as plt
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
import os
import glob
import tempfile
from datetime import datetime
import calendar

AZUL = colors.HexColor("#1B3A6B")
AZUL_CLARO = colors.HexColor("#2E75B6")
VERDE = colors.HexColor("#059669")
ROJO = colors.HexColor("#DC2626")
AMARILLO = colors.HexColor("#D97706")
GRIS = colors.HexColor("#F3F4F6")
BLANCO = colors.white

def fmt_num(n):
    try: return f"{float(n):,.0f}"
    except: return str(n)

def fmt_money(n):
    try: return f"${float(n):,.2f}"
    except: return str(n)

def fmt_pct(n):
    try: return f"{float(n):.1f}%"
    except: return str(n)

def parsear_mes(x):
    x = str(x).strip()
    if "/" in x:
        r = pd.to_datetime(x, format="%Y/%m/%d", errors="coerce")
    else:
        r = pd.to_datetime(x, format="%Y%m%d", errors="coerce")
    if pd.isna(r):
        return "desconocido"
    return r.to_period("M").strftime("%Y-%m")

def parsear_fecha_completa(x):
    x = str(x).strip()
    if "/" in x:
        return pd.to_datetime(x, format="%Y/%m/%d", errors="coerce")
    else:
        return pd.to_datetime(x, format="%Y%m%d", errors="coerce")


# ══════════════════════════════════════════════════════════════
# Filas de INVENTARIO vs filas de VENTA en el SAP
# ══════════════════════════════════════════════════════════════
# El SAP de Difare mezcla dos tipos de registro en la misma tabla y se
# distinguen por la columna CANAL:
#
#   • Filas de INVENTARIO → CANAL = "<SIN>".  Solo existen para el día de
#     corte.  STOCK > 0 siempre; UNIDADES_ROTADAS = 0.  Difare NO emite fila
#     cuando el inventario es cero: la AUSENCIA de la fila ES el cero.
#
#   • Filas de VENTA → CANAL = "Mostrador", "CallCenter", "Pedidos Ya", etc.
#     Existen todos los días.  STOCK = 0 SIEMPRE, por construcción: son
#     registros de rotación, no de inventario.
#
# Leer la columna STOCK sobre las filas de venta produce falsos quiebres: un
# PDV que VENDIÓ el día del corte aparece como "sin stock" aunque tenga
# inventario. (Ver auditoría 2026-07: Nikzon reportaba 296 PDV en Stock=0
# cuando los reales eran 5; los otros 291 eran PDV que vendieron ese día.)
CANAL_INVENTARIO = "<SIN>"

def solo_filas_inventario(df):
    """Devuelve solo las filas de INVENTARIO del SAP, descartando las de venta.

    Sin este filtro, cualquier conteo sobre la columna STOCK cuenta como
    quiebre a los PDV que registraron una venta ese día."""
    if df is None or getattr(df, "empty", True):
        return df
    if "CANAL" in df.columns:
        canal = df["CANAL"].astype(str).str.strip().str.upper()
        inv = df[canal == CANAL_INVENTARIO]
        if not inv.empty:
            return inv.copy()
    # Fallback defensivo por si Difare deja de usar "<SIN>" como etiqueta:
    # descartar las filas que son claramente de rotación (venta > 0, stock 0).
    if "UNIDADES_ROTADAS" in df.columns and "STOCK" in df.columns:
        return df[~((df["UNIDADES_ROTADAS"].fillna(0) > 0)
                    & (df["STOCK"].fillna(0) == 0))].copy()
    return df


def stock_por_pdv(df_stock, idneptuno=None):
    """Serie STOCK total por POS a partir de las filas de inventario.

    Suma por POS porque un mismo PDV puede traer más de una fila de
    inventario para el mismo SKU."""
    if df_stock is None or getattr(df_stock, "empty", True):
        return pd.Series(dtype=float)
    d = df_stock
    if idneptuno is not None and "IDNEPTUNO" in d.columns:
        d = d[d["IDNEPTUNO"] == idneptuno]
    if d.empty or "POS" not in d.columns or "STOCK" not in d.columns:
        return pd.Series(dtype=float)
    return d.dropna(subset=["POS"]).groupby("POS")["STOCK"].sum()

def detectar_archivo_sap(carpeta="excels"):
    archivos = glob.glob(f"{carpeta}/*.xlsx") + glob.glob(f"{carpeta}/*.xls")
    meses_nombres = ["ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
                     "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE"]
    for a in archivos:
        nombre = os.path.basename(a).upper()
        if "EJEMPLO" in nombre or "PLAN" in nombre or "VISIBILIDAD" in nombre:
            continue
        es_mensual = any(m in nombre for m in meses_nombres) and "SAP" not in nombre
        if not es_mensual or "SAP" in nombre:
            return a
    return None

def detectar_archivo_mes_anterior(carpeta="excels"):
    archivos = glob.glob(f"{carpeta}/*.xlsx") + glob.glob(f"{carpeta}/*.xls")
    meses_nombres = ["ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
                     "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE"]
    historicos = []
    for a in archivos:
        nombre = os.path.basename(a).upper()
        if "EJEMPLO" in nombre or "PLAN" in nombre or "VISIBILIDAD" in nombre:
            continue
        es_mensual = any(m in nombre for m in meses_nombres) and "SAP" not in nombre
        if es_mensual:
            historicos.append(a)
    return historicos[-1] if historicos else None

def detectar_ultimo_dia_stock_y_venta(carpeta="excels"):
    sap = detectar_archivo_sap(carpeta)
    if not sap:
        return 29, 29, 31, False
    try:
        df = _leer_excel_hoja_correcta(sap)
        if df is None:
            return 29, 29, 31, False
        farm = df[df["UNIDAD"] == "FARMACIAS"]
        if "DIA" not in farm.columns:
            return 29, 29, 31, False
        fechas = farm["DIA"].apply(parsear_fecha_completa)
        fechas_validas = fechas.dropna()
        # Filtrar días con al menos 100 filas (excluir madrugada residual)
        farm_con_fecha = farm.copy()
        farm_con_fecha["_fecha"] = fechas
        farm_con_fecha = farm_con_fecha.dropna(subset=["_fecha"])
        farm_con_fecha["_dia"] = farm_con_fecha["_fecha"].dt.day
        filas_por_dia = farm_con_fecha.groupby("_dia").size()
        dias_validos = filas_por_dia[filas_por_dia >= 100]
        if dias_validos.empty:
            ultimo_dia_venta = int(fechas_validas.dt.day.max())
        else:
            ultimo_dia_venta = int(dias_validos.index.max())
        mes = int(fechas_validas.dt.month.mode()[0])
        anio = int(fechas_validas.dt.year.mode()[0])
        dias_en_mes = calendar.monthrange(anio, mes)[1]
        stock_por_dia = farm.groupby("DIA")["STOCK"].sum()
        dias_con_stock = stock_por_dia[stock_por_dia > 0]
        if not dias_con_stock.empty:
            ultimo_dia_stock_str = dias_con_stock.index.max()
            fecha_stock = parsear_fecha_completa(ultimo_dia_stock_str)
            ultimo_dia_stock = int(fecha_stock.day) if not pd.isna(fecha_stock) else ultimo_dia_venta
        else:
            ultimo_dia_stock = ultimo_dia_venta
        es_mes_completo = (ultimo_dia_venta == dias_en_mes)
        return ultimo_dia_venta, ultimo_dia_stock, dias_en_mes, es_mes_completo
    except:
        pass
    return 29, 29, 31, False

def detectar_ultimo_dia_y_proyeccion(carpeta="excels"):
    ultimo_dia_venta, _, dias_en_mes, es_completo = detectar_ultimo_dia_stock_y_venta(carpeta)
    return ultimo_dia_venta, dias_en_mes, es_completo

def _leer_excel_hoja_correcta(path):
    """Lee un Excel buscando la hoja que contenga FECHA o DIA (la data raw),
    no la primera hoja por defecto. Útil cuando el archivo viene con hojas
    extra (resúmenes, tablas pivote, etc.).
    También normaliza COSTOVENTANETO → VENTA NETA RECUPERO porque a partir de
    mayo 2026 Difare cambió el nombre de esa columna (mismo metric, etiqueta
    distinta — confirmado con el equipo de cuenta)."""
    nombre = os.path.basename(path)
    try:
        xl = pd.ExcelFile(path)
        sheets = xl.sheet_names
    except Exception as e:
        print(f"[cargar_excel] {nombre}: no se pudo abrir ({e})")
        return None

    df = None
    for sheet in sheets:
        try:
            tmp = pd.read_excel(path, sheet_name=sheet)
            if "FECHA" in tmp.columns or "DIA" in tmp.columns:
                df = tmp
                if sheet != sheets[0]:
                    print(f"[cargar_excel] {nombre}: usando hoja '{sheet}' (no la primera)")
                break
        except Exception as e:
            print(f"[cargar_excel] {nombre} hoja '{sheet}': {e}")
            continue

    if df is None:
        return None

    # Normalizar nombre de columna de venta neta (Difare cambió desde mayo 2026)
    if "VENTA NETA RECUPERO" not in df.columns and "COSTOVENTANETO" in df.columns:
        df = df.rename(columns={"COSTOVENTANETO": "VENTA NETA RECUPERO"})
        print(f"[cargar_excel] {nombre}: renombrado COSTOVENTANETO → VENTA NETA RECUPERO")

    return df


def cargar_todos_excels(carpeta="excels"):
    archivos = glob.glob(f"{carpeta}/*.xlsx") + glob.glob(f"{carpeta}/*.xls")
    dfs = []
    for a in archivos:
        nombre_upper = os.path.basename(a).upper()
        if "EJEMPLO" in nombre_upper:
            continue
        # Saltar archivos que no son de ventas/SAP (e.g. Plan_Visibilidad)
        if "PLAN" in nombre_upper or "VISIBILIDAD" in nombre_upper:
            continue
        try:
            df = _leer_excel_hoja_correcta(a)
            if df is None:
                continue
            # Solo procesar archivos con estructura de ventas (FECHA o DIA como columna)
            if "FECHA" not in df.columns and "DIA" not in df.columns:
                continue
            if "FECHA" in df.columns and "DIA" not in df.columns:
                df["MES"] = df["FECHA"].astype(str).apply(
                    lambda x: x[:4] + "-" + x[4:6] if len(str(x)) == 6 else "desconocido")
            elif "DIA" in df.columns:
                df["MES"] = df["DIA"].apply(parsear_mes)
            dfs.append(df)
        except Exception as e:
            print(f"[cargar_todos_excels] error con {a}: {e}")
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

def cargar_stock_por_mes(carpeta="excels"):
    archivos = glob.glob(f"{carpeta}/*.xlsx") + glob.glob(f"{carpeta}/*.xls")
    stock_por_mes = {}
    archivo_sap = detectar_archivo_sap(carpeta)
    meses_nombres = ["ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
                     "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE"]
    for a in archivos:
        nombre_upper = os.path.basename(a).upper()
        if "EJEMPLO" in nombre_upper or "PLAN" in nombre_upper or "VISIBILIDAD" in nombre_upper:
            continue
        try:
            df = _leer_excel_hoja_correcta(a)
            if df is None:
                continue
            if "FECHA" not in df.columns and "DIA" not in df.columns:
                continue
            nombre = os.path.basename(a).upper()
            es_sap = "SAP" in nombre or not any(m in nombre for m in meses_nombres)
            if "FECHA" in df.columns and "DIA" not in df.columns:
                df["MES"] = df["FECHA"].astype(str).apply(
                    lambda x: x[:4] + "-" + x[4:6] if len(str(x)) == 6 else "desconocido")
            elif "DIA" in df.columns:
                df["MES"] = df["DIA"].apply(parsear_mes)
            if es_sap and "DIA" in df.columns:
                farm_stock = df[df["UNIDAD"]=="FARMACIAS"]
                dia_stock = farm_stock.groupby("DIA")["STOCK"].sum()
                dias_con_stock = dia_stock[dia_stock > 0]
                if not dias_con_stock.empty:
                    ultimo_dia_str = dias_con_stock.index.max()
                    df_stock_dia = df[df["DIA"] == ultimo_dia_str]
                else:
                    df_stock_dia = df
            else:
                df_stock_dia = df
            bodega = df_stock_dia[df_stock_dia["UNIDAD"] == "DIFARE S.A."]
            farmacias = df_stock_dia[df_stock_dia["UNIDAD"] == "FARMACIAS"]
            if "MES" in df.columns:
                mes_vals = df[df["UNIDAD"].isin(["DIFARE S.A.","FARMACIAS"])]["MES"].mode()
                mes = mes_vals[0] if not mes_vals.empty else "desconocido"
            else:
                mes = "desconocido"
            stock_por_mes[mes] = {
                "stock_bodega_val": float(bodega["STOCK_VALORIZADO"].sum()),
                "stock_bodega_uni": float(bodega["STOCK"].sum()),
                "stock_pdv_val": float(farmacias["STOCK_VALORIZADO"].sum()),
                "stock_pdv_uni": float(farmacias["STOCK"].sum()),
                "stock_total_val": float(bodega["STOCK_VALORIZADO"].sum()) + float(farmacias["STOCK_VALORIZADO"].sum()),
                "archivo": a
            }
        except:
            pass
    return stock_por_mes

def cargar_sap_completo(carpeta="excels"):
    """Carga el SAP completo con toda la data de farmacias"""
    sap = detectar_archivo_sap(carpeta)
    if not sap:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    df = _leer_excel_hoja_correcta(sap)
    if df is None:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    farm_todo = df[df["UNIDAD"] == "FARMACIAS"].copy()
    bodega = df[df["UNIDAD"] == "DIFARE S.A."].copy()
    dia_stock = farm_todo.groupby("DIA")["STOCK"].sum()
    dias_con_stock = dia_stock[dia_stock > 0]
    if not dias_con_stock.empty:
        ultimo_dia_str = dias_con_stock.index.max()
        farm_stock_ultimo = farm_todo[farm_todo["DIA"] == ultimo_dia_str].copy()
    else:
        farm_stock_ultimo = farm_todo.copy()
    # Quedarse SOLO con las filas de inventario. Las filas de venta del mismo
    # día traen STOCK = 0 por construcción y, si no se descartan, se cuentan
    # como quiebres de stock inexistentes. Ver solo_filas_inventario().
    farm_stock_ultimo = solo_filas_inventario(farm_stock_ultimo)
    # bodega no se filtra: sus filas de venta traen STOCK/STOCK_VALORIZADO = 0
    # y solo se usa para sumar valorizado, así que no distorsionan nada.
    return bodega, farm_stock_ultimo, farm_todo

def calcular_universo_pdv(carpeta="excels"):
    """Universo = PDV del SAP VIGENTE con venta>0 O stock>0.

    Se cuenta por CODIGOPDV (código único) y NO por POS, porque el nombre del
    POS varía entre registros y duplica tiendas.

    Importante: el universo debe salir SOLO del SAP vigente. Si se acumulan
    los Excel históricos, las tiendas cerradas siguen sumando para siempre
    (auditoría 2026-07: 1149 acumulado ene–jul vs 1128 reales en julio)."""
    sap = detectar_archivo_sap(carpeta)
    if not sap:
        return 0
    try:
        df = _leer_excel_hoja_correcta(sap)
        if df is None:
            return 0
        farm = df[df["UNIDAD"] == "FARMACIAS"]
        col = "CODIGOPDV" if "CODIGOPDV" in farm.columns else "POS"
        universo = farm[
            (farm["VENTA NETA RECUPERO"] > 0) | (farm["STOCK"] > 0)
        ][col].dropna().nunique()
        return universo
    except:
        return 0

def calcular_pareto_farmacias(df_todos, df_sap_farm_stock, df_sap_farm_todo, universo_pdv,
                               solo_pareto: bool = False):
    """
    df_todos = data acumulada historica (para calcular venta total)
    df_sap_farm_stock = farmacias del SAP solo en el ultimo dia de stock
    df_sap_farm_todo = farmacias del SAP todos los dias (para presencia)
    solo_pareto = True → solo 80% venta (legacy), False → todos los ítems con flag
    """
    df_farm_todos = df_todos[df_todos["UNIDAD"] == "FARMACIAS"].copy()

    group_cols = ["IDNEPTUNO","MARCA","PRODUCTO"]
    if "IDDIFARE" in df_farm_todos.columns:
        group_cols = ["IDNEPTUNO","IDDIFARE","MARCA","PRODUCTO"]
    venta_prod = df_farm_todos.groupby(group_cols).agg(
        venta_total=("VENTA NETA RECUPERO","sum")
    ).reset_index().sort_values("venta_total", ascending=False)

    total_venta = venta_prod["venta_total"].sum()
    venta_prod["pct"] = venta_prod["venta_total"] / total_venta * 100
    venta_prod["pct_acum"] = venta_prod["pct"].cumsum()

    # Marcar cuáles son pareto (80% de venta)
    venta_prod["es_pareto"] = venta_prod["pct_acum"] <= 80

    if solo_pareto:
        items = venta_prod[venta_prod["es_pareto"]].copy()
        if items.empty:
            items = venta_prod.head(20).copy()
    else:
        # Todos los ítems con venta > 0
        items = venta_prod[venta_prod["venta_total"] > 0].copy()

    # ── PDVs con venta en el último mes completo — por producto ──
    # Se usa para el %Pon (ponderada) = PDV con venta último mes / PDV presencia.
    #
    # Detección parcialidad: miramos el último DIA disponible del SAP en el
    # último mes. Si el día es menor a (días_del_mes - 2), el mes es parcial
    # y usamos el penúltimo como "último mes completo".
    # (Antes usábamos conteo de filas, pero con SAP acumulando muchos días
    #  ese ratio se acercaba a 1 y daba falso "completo".)
    import calendar as _calendar
    import re as _re_mod
    mes_ult_completo = None
    venta_ult_mes_por_prod = {}
    if "MES" in df_farm_todos.columns and "POS" in df_farm_todos.columns:
        meses_ord = sorted(df_farm_todos["MES"].dropna().unique())
        if meses_ord:
            mes_ult_completo = meses_ord[-1]  # default
            if len(meses_ord) >= 2 and "DIA" in df_farm_todos.columns:
                last = meses_ord[-1]
                dias_last = df_farm_todos[df_farm_todos["MES"] == last]["DIA"].dropna().astype(str)
                dias_norm = dias_last.str.replace(r"\D", "", regex=True)
                dias_norm = dias_norm[dias_norm.str.len() >= 8]
                if not dias_norm.empty:
                    try:
                        max_dia = dias_norm.max()
                        day_num = int(max_dia[6:8])
                        # días totales del mes (last es "YYYY-MM")
                        try:
                            y, m = str(last).split("-")
                            dias_total = _calendar.monthrange(int(y), int(m))[1]
                        except Exception:
                            dias_total = 30
                        if day_num < dias_total - 2:
                            mes_ult_completo = meses_ord[-2]
                    except Exception:
                        pass

            df_ult_mes = df_farm_todos[
                (df_farm_todos["MES"] == mes_ult_completo)
                & (df_farm_todos["VENTA NETA RECUPERO"] > 0)
            ]
            if not df_ult_mes.empty:
                venta_ult_mes_por_prod = (
                    df_ult_mes.groupby("IDNEPTUNO")["POS"].nunique().to_dict()
                )

    resultado = []
    for _, row in items.iterrows():
        idneptuno = row["IDNEPTUNO"]
        marca = row["MARCA"]
        producto = row["PRODUCTO"]

        presencia = 0
        stock_eq0 = 0
        stock_leq1 = 0
        stock_lt2 = 0
        stock_leq2 = 0
        stock_leq3 = 0

        # Presencia = PDV con cualquier registro del producto en SAP completo
        if df_sap_farm_todo is not None and not df_sap_farm_todo.empty:
            prod_todo = df_sap_farm_todo[df_sap_farm_todo["IDNEPTUNO"] == idneptuno]
            pdv_presencia = set(prod_todo["POS"].dropna().unique())
            presencia = len(pdv_presencia)
        else:
            pdv_presencia = set()

        # ── Stock del último día de corte ──
        # df_sap_farm_stock ya viene filtrado a filas de INVENTARIO
        # (cargar_sap_completo → solo_filas_inventario). Se suma el STOCK por
        # POS antes de comparar, porque un PDV puede traer varias filas.
        if df_sap_farm_stock is not None and not df_sap_farm_stock.empty:
            stock_pos = stock_por_pdv(df_sap_farm_stock, idneptuno)
            pdv_con_stock = set(stock_pos.index)

            # Difare no emite fila de inventario cuando el stock es cero:
            # la ausencia de fila ES el cero. Se suman también los PDV que
            # traen fila explícita en 0 por si el formato cambia.
            pdv_ausentes = pdv_presencia - pdv_con_stock
            pdv_cero_explicito = set(stock_pos[stock_pos <= 0].index)
            pdv_stock0 = pdv_ausentes | pdv_cero_explicito

            # Buckets ACUMULATIVOS: cada uno incluye a los PDV en cero.
            stock_eq0 = len(pdv_stock0)
            stock_leq1 = len(pdv_stock0 | set(stock_pos[stock_pos <= 1].index))
            stock_lt2 = len(pdv_stock0 | set(stock_pos[stock_pos < 2].index))
            stock_leq2 = len(pdv_stock0 | set(stock_pos[stock_pos <= 2].index))
            stock_leq3 = len(pdv_stock0 | set(stock_pos[stock_pos <= 3].index))

        iddifare = row.get("IDDIFARE", "")
        # Convertir IDDIFARE a numérico si es posible (viene como texto)
        if iddifare != "":
            try:
                iddifare = int(float(str(iddifare)))
            except (ValueError, TypeError):
                pass

        pdv_venta_ult_mes = int(venta_ult_mes_por_prod.get(idneptuno, 0))

        resultado.append({
            "IDNEPTUNO": idneptuno,
            "IDDIFARE": iddifare,
            "MARCA": marca,
            "PRODUCTO": producto,
            "VENTA": row["venta_total"],
            "PCT": row["pct"],
            "PCT_ACUM": row["pct_acum"],
            "es_pareto": bool(row["es_pareto"]),
            "UNIVERSO_PDV": universo_pdv,
            "PDV_PRESENCIA": presencia,
            "PDV_VENTA_ULT_MES": pdv_venta_ult_mes,
            # MES viene como string "YYYY-MM" (p.ej. "2026-03"), no entero
            "MES_ULT_COMPLETO": str(mes_ult_completo) if mes_ult_completo is not None else None,
            # STOCK_0    = PDV con stock exactamente 0
            # STOCK_1    = PDV con 1 unidad o menos  (incluye los de 0)
            # STOCK_LT2  = PDV con menos de 2 unidades — exactamente 2 NO cuenta
            # STOCK_2/3  = <=2 y <=3, se mantienen para el PDF gerencial
            "STOCK_0": stock_eq0,
            "STOCK_1": stock_leq1,
            "STOCK_LT2": stock_lt2,
            "STOCK_2": stock_leq2,
            "STOCK_3": stock_leq3,
            "PDV_CON_STOCK": presencia - stock_eq0,
        })
    return pd.DataFrame(resultado)

def grafico_tendencia_unidades(df, tmp_dir, ultimo_dia, dias_mes, es_completo):
    df_ventas = df[df["UNIDAD"] != "DIFARE S.A."].copy()
    if "MES" not in df_ventas.columns:
        return None
    tend = df_ventas.groupby("MES")["VENTA NETA RECUPERO"].sum().reset_index().sort_values("MES")
    mes_actual = tend["MES"].max()
    venta_actual_real = df_ventas[df_ventas["MES"] == mes_actual]["VENTA NETA RECUPERO"].sum()
    factor = dias_mes / ultimo_dia
    venta_proy = venta_actual_real * factor
    meses = tend["MES"].tolist()
    ventas = tend["VENTA NETA RECUPERO"].tolist()
    fig, ax = plt.subplots(figsize=(11, 5))
    colores_barras = ["#1B3A6B" if m != mes_actual else "#2E75B6" for m in meses]
    bars = ax.bar(meses, ventas, color=colores_barras, edgecolor="white", linewidth=0.5, label="Venta Real", zorder=3)
    if not es_completo:
        idx = meses.index(mes_actual)
        ax.bar(meses[idx], venta_proy, color="#60A5FA", alpha=0.35, edgecolor="#2E75B6",
               linewidth=1.5, label=f"Proyeccion (x{factor:.2f})", zorder=2)
        ax.text(idx, venta_proy * 1.02, f"Proy: ${venta_proy:,.0f}",
                ha="center", va="bottom", fontsize=8.5, fontweight="bold", color="#2563EB",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#DBEAFE", edgecolor="#2563EB", alpha=0.8))
    for i, bar in enumerate(bars):
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()*1.02,
                f"${bar.get_height():,.0f}", ha="center", va="bottom",
                fontsize=9, fontweight="bold", color="#1B3A6B")
    titulo = f"Tendencia de Ventas - {'Mes completo' if es_completo else f'Proyeccion al cierre (dia {ultimo_dia} de {dias_mes})'}"
    ax.set_title(titulo, fontsize=12, fontweight="bold", pad=14)
    ax.set_ylabel("Venta Neta ($)", fontsize=10)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.legend(fontsize=9, loc="upper left")
    ax.set_facecolor("#F8FAFC")
    fig.patch.set_facecolor("white")
    ax.spines[["top","right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.3, zorder=0)
    path = os.path.join(tmp_dir, "tendencia.png")
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path

def grafico_top_marcas(df, tmp_dir):
    df_ventas = df[df["UNIDAD"] != "DIFARE S.A."]
    top = df_ventas.groupby("MARCA")["VENTA NETA RECUPERO"].sum().nlargest(8).reset_index()
    fig, ax = plt.subplots(figsize=(11, 4))
    colores = ["#1B3A6B","#2E75B6","#3B82F6","#60A5FA","#93C5FD","#BFDBFE","#DBEAFE","#EFF6FF"]
    bars = ax.barh(top["MARCA"], top["VENTA NETA RECUPERO"], color=colores[:len(top)])
    ax.set_title("Top 8 Marcas por Venta Neta", fontsize=13, fontweight="bold", pad=12)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    for bar in bars:
        ax.text(bar.get_width()*1.01, bar.get_y()+bar.get_height()/2,
                f"${bar.get_width():,.0f}", va="center", fontsize=8, fontweight="bold")
    ax.set_facecolor("#F8FAFC")
    fig.patch.set_facecolor("white")
    ax.spines[["top","right"]].set_visible(False)
    ax.invert_yaxis()
    path = os.path.join(tmp_dir, "marcas.png")
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path

def grafico_grupos_pdv(df, tmp_dir):
    df_farm = df[df["UNIDAD"] == "FARMACIAS"]
    por_grupo = df_farm.groupby("GRUPOPDV")["VENTA NETA RECUPERO"].sum().sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(10, 4))
    bars = ax.bar(por_grupo.index, por_grupo.values, color="#1B3A6B", edgecolor="white")
    ax.set_title("Ventas por Grupo PDV - Farmacias Propias", fontsize=12, fontweight="bold")
    ax.set_ylabel("Venta Neta ($)", fontsize=10)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    plt.xticks(rotation=30, ha="right", fontsize=8)
    for bar in bars:
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()*1.01,
                f"${bar.get_height():,.0f}", ha="center", va="bottom", fontsize=7.5, fontweight="bold")
    ax.set_facecolor("#F8FAFC")
    fig.patch.set_facecolor("white")
    ax.spines[["top","right"]].set_visible(False)
    path = os.path.join(tmp_dir, "grupos_pdv.png")
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path

def grafico_grupos_cliente(df, tmp_dir):
    df_dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"]
    por_grupo = df_dist.groupby("GRUPOCLIENTE")["VENTA NETA RECUPERO"].sum().sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(10, 4))
    colores = ["#1B3A6B","#2E75B6","#3B82F6","#60A5FA","#93C5FD","#BFDBFE","#DBEAFE","#EFF6FF","#F0F9FF","#E0F2FE"]
    ax.barh(por_grupo.index, por_grupo.values, color=colores[:len(por_grupo)])
    ax.set_title("Ventas por Grupo Cliente - Canal Distribucion", fontsize=12, fontweight="bold")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.set_facecolor("#F8FAFC")
    fig.patch.set_facecolor("white")
    ax.spines[["top","right"]].set_visible(False)
    ax.invert_yaxis()
    path = os.path.join(tmp_dir, "grupos_cliente.png")
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path

def estilo_tabla(tabla):
    return TableStyle([
        ("BACKGROUND", (0,0), (-1,0), AZUL),
        ("TEXTCOLOR", (0,0), (-1,0), BLANCO),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 7.5),
        ("ALIGN", (1,0), (-1,-1), "CENTER"),
        ("ALIGN", (0,0), (0,-1), "LEFT"),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#E5E7EB")),
        ("ROWBACKGROUND", (0,1), (-1,-1), [BLANCO, GRIS]),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ])

def generar_pdf_ejecutivo(df, reporte_ia, ruta_salida, carpeta="excels"):
    tmp_dir = tempfile.mkdtemp()
    ultimo_dia, dias_mes, es_completo = detectar_ultimo_dia_y_proyeccion(carpeta)
    factor = dias_mes / ultimo_dia if not es_completo else 1.0
    doc = SimpleDocTemplate(ruta_salida, pagesize=A4,
                             leftMargin=1.5*cm, rightMargin=1.5*cm,
                             topMargin=1.5*cm, bottomMargin=1.5*cm)
    elementos = []
    T = ParagraphStyle("T", fontSize=20, fontName="Helvetica-Bold", textColor=AZUL, alignment=TA_CENTER, spaceAfter=4)
    S = ParagraphStyle("S", fontSize=10, textColor=colors.HexColor("#6B7280"), alignment=TA_CENTER, spaceAfter=14)
    SEC = ParagraphStyle("SEC", fontSize=12, fontName="Helvetica-Bold", textColor=AZUL, spaceBefore=14, spaceAfter=8)
    NOR = ParagraphStyle("NOR", fontSize=9.5, textColor=colors.HexColor("#374151"), spaceAfter=5, leading=15)
    SUB = ParagraphStyle("SUB", fontSize=10, fontName="Helvetica-Bold", textColor=AZUL_CLARO, spaceBefore=8, spaceAfter=4)

    df_ventas = df[df["UNIDAD"] != "DIFARE S.A."].copy()
    df_farm = df[df["UNIDAD"] == "FARMACIAS"].copy()
    df_dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"].copy()

    MESES_NOM_ES = ["ENERO","FEBRERO","MARZO","ABRIL","MAYO","JUNIO",
                    "JULIO","AGOSTO","SEPTIEMBRE","OCTUBRE","NOVIEMBRE","DICIEMBRE"]
    meses_disp = sorted(df_ventas["MES"].dropna().unique().tolist()) if "MES" in df_ventas.columns else []
    mes_parcial = meses_disp[-1] if meses_disp else None
    meses_completos = meses_disp[:-1] if not es_completo else meses_disp
    if es_completo:
        mes_parcial = None

    def label_mes(m):
        try:
            n = int(m.split("-")[1])
            return f"{MESES_NOM_ES[n-1]} {m.split('-')[0]}"
        except:
            return m

    sum_v = lambda d, m: float(d[d["MES"] == m]["VENTA NETA RECUPERO"].sum()) if m else 0.0
    venta_completos = {m: sum_v(df_ventas, m) for m in meses_completos}
    farm_completos  = {m: sum_v(df_farm,  m) for m in meses_completos}
    dist_completos  = {m: sum_v(df_dist,  m) for m in meses_completos}

    venta_parc_real = sum_v(df_ventas, mes_parcial) if mes_parcial else 0.0
    farm_parc_real  = sum_v(df_farm,  mes_parcial) if mes_parcial else 0.0
    dist_parc_real  = sum_v(df_dist,  mes_parcial) if mes_parcial else 0.0
    venta_parc_proy = venta_parc_real * factor if mes_parcial else 0.0
    farm_parc_proy  = farm_parc_real  * factor if mes_parcial else 0.0
    dist_parc_proy  = dist_parc_real  * factor if mes_parcial else 0.0

    venta_total = sum(venta_completos.values()) + venta_parc_real
    farm_total  = sum(farm_completos.values())  + farm_parc_real
    dist_total  = sum(dist_completos.values())  + dist_parc_real

    stock_mes = cargar_stock_por_mes(carpeta)
    stock_completos = {m: stock_mes.get(m, {}).get("stock_total_val", 0) for m in meses_completos}
    stock_parc = stock_mes.get(mes_parcial, {}).get("stock_total_val", 0) if mes_parcial else 0

    elementos.append(Paragraph("REPORTE EJECUTIVO - DIFARE ECUADOR", T))
    if meses_disp:
        periodo_txt = f"{label_mes(meses_disp[0])} - {label_mes(meses_disp[-1])}"
    else:
        periodo_txt = ""
    elementos.append(Paragraph(f"Genommalab Ecuador - Periodo {periodo_txt} - {datetime.now().strftime('%d/%m/%Y')}", S))

    headers = [""] + [label_mes(m) for m in meses_completos]
    if mes_parcial:
        nm_p = label_mes(mes_parcial).split()[0]
        headers += [f"{nm_p}\n(al {ultimo_dia})", f"PROY.\nCIERRE {nm_p}"]
    headers += ["ACUMULADO"]

    fila_vt = ["Venta Total"] + [fmt_money(venta_completos[m]) for m in meses_completos]
    fila_fp = ["Farmacias Propias"] + [fmt_money(farm_completos[m]) for m in meses_completos]
    fila_cd = ["Canal Distribucion"] + [fmt_money(dist_completos[m]) for m in meses_completos]
    fila_st = ["Stock Total (Val.)"] + [fmt_money(stock_completos[m]) for m in meses_completos]
    if mes_parcial:
        fila_vt += [fmt_money(venta_parc_real), fmt_money(venta_parc_proy)]
        fila_fp += [fmt_money(farm_parc_real),  fmt_money(farm_parc_proy)]
        fila_cd += [fmt_money(dist_parc_real),  fmt_money(dist_parc_proy)]
        fila_st += [fmt_money(stock_parc), "---"]
    fila_vt += [fmt_money(venta_total)]
    fila_fp += [fmt_money(farm_total)]
    fila_cd += [fmt_money(dist_total)]
    fila_st += ["---"]

    elementos.append(Paragraph("RESUMEN MES A MES", SEC))
    kpi = [headers, fila_vt, fila_fp, fila_cd, fila_st]
    n_cols = len(headers)
    ancho_disp = 18.0
    col_w = [3.2*cm] + [((ancho_disp - 3.2)/(n_cols-1))*cm] * (n_cols-1)
    t_kpi = Table(kpi, colWidths=col_w)
    t_kpi.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), AZUL), ("TEXTCOLOR", (0,0), (-1,0), BLANCO),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("BACKGROUND", (0,1), (0,-1), AZUL), ("TEXTCOLOR", (0,1), (0,-1), BLANCO),
        ("FONTNAME", (0,1), (0,-1), "Helvetica-Bold"),
        ("BACKGROUND", (-1,1), (-1,-2), colors.HexColor("#DBEAFE")),
        ("BACKGROUND", (-2,1), (-2,-2), colors.HexColor("#FEF3C7")),
        ("TEXTCOLOR", (-2,1), (-2,-2), colors.HexColor("#92400e")),
        ("FONTNAME", (-1,1), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("ALIGN", (1,0), (-1,-1), "CENTER"), ("ALIGN", (0,0), (0,-1), "LEFT"),
        ("ROWBACKGROUND", (1,1), (-1,-1), [colors.HexColor("#EFF6FF"), BLANCO]),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 7), ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
    ]))
    elementos.append(t_kpi)
    elementos.append(Spacer(1, 0.3*cm))

    g1 = grafico_tendencia_unidades(df, tmp_dir, ultimo_dia, dias_mes, es_completo)
    if g1:
        elementos.append(Paragraph("TENDENCIA MENSUAL DE VENTAS", SEC))
        elementos.append(Image(g1, width=17*cm, height=7*cm))

    g2 = grafico_top_marcas(df, tmp_dir)
    if g2:
        elementos.append(Paragraph("TOP MARCAS POR VENTA NETA", SEC))
        elementos.append(Image(g2, width=17*cm, height=5.5*cm))

    def serie_mes_a_mes(dic_completos, parc_real, parc_proy):
        partes = [f"{label_mes(m).split()[0].title()}: {fmt_money(v)}" for m, v in dic_completos.items()]
        linea1 = "  ".join(partes)
        if len(dic_completos) >= 2:
            ms = list(dic_completos.keys())
            v_ult = dic_completos[ms[-1]]
            v_pen = dic_completos[ms[-2]]
            var = ((v_ult - v_pen)/v_pen*100) if v_pen > 0 else 0
            linea1 += f"  ({label_mes(ms[-1]).split()[0].title()} {'subio' if var>=0 else 'bajo'} {abs(var):.1f}% vs {label_mes(ms[-2]).split()[0].title()})"
        return linea1

    elementos.append(Paragraph("ANALISIS E INSIGHTS DEL PERIODO", SEC))
    elementos.append(Paragraph("Farmacias Propias", SUB))
    top_grupo_farm = df_farm.groupby("GRUPOPDV")["VENTA NETA RECUPERO"].sum().idxmax() if not df_farm.empty else "N/A"
    top_pos = df_farm.groupby("POS")["VENTA NETA RECUPERO"].sum().idxmax() if not df_farm.empty else "N/A"
    elementos.append(Paragraph(serie_mes_a_mes(farm_completos, farm_parc_real, farm_parc_proy), NOR))
    elementos.append(Paragraph(f"Grupo PDV lider: {top_grupo_farm}  |  Farmacia top: {top_pos}", NOR))
    if mes_parcial:
        nm = label_mes(mes_parcial).split()[0].title()
        elementos.append(Paragraph(f"{nm} real (al dia {ultimo_dia} de {dias_mes}): {fmt_money(farm_parc_real)}  |  Proyeccion cierre {nm}: {fmt_money(farm_parc_proy)}", NOR))

    elementos.append(Paragraph("Canal Distribucion", SUB))
    top_grupo_dist = df_dist.groupby("GRUPOCLIENTE")["VENTA NETA RECUPERO"].sum().idxmax() if not df_dist.empty else "N/A"
    top_cliente = df_dist.groupby("PROPIETARIO")["VENTA NETA RECUPERO"].sum().idxmax() if not df_dist.empty else "N/A"
    elementos.append(Paragraph(serie_mes_a_mes(dist_completos, dist_parc_real, dist_parc_proy), NOR))
    elementos.append(Paragraph(f"Grupo cliente lider: {top_grupo_dist}  |  Cliente top: {top_cliente}", NOR))
    if mes_parcial:
        nm = label_mes(mes_parcial).split()[0].title()
        elementos.append(Paragraph(f"{nm} real (al dia {ultimo_dia} de {dias_mes}): {fmt_money(dist_parc_real)}  |  Proyeccion cierre {nm}: {fmt_money(dist_parc_proy)}", NOR))

    elementos.append(Paragraph("Analisis Inteligente (Claude AI)", SUB))
    for linea in reporte_ia.split("\n"):
        if linea.strip():
            elementos.append(Paragraph(linea.strip(), NOR))

    elementos.append(Paragraph("VENTAS POR PROVINCIA", SEC))
    prov = df_ventas.groupby("PROVINCIA")["VENTA NETA RECUPERO"].sum().nlargest(10).reset_index()
    prov_data = [["PROVINCIA", "VENTA NETA", "% DEL TOTAL"]]
    for _, row in prov.iterrows():
        pct = row["VENTA NETA RECUPERO"]/venta_total*100 if venta_total > 0 else 0
        prov_data.append([row["PROVINCIA"], fmt_money(row["VENTA NETA RECUPERO"]), f"{pct:.1f}%"])
    t_prov = Table(prov_data, colWidths=[8*cm, 5*cm, 4*cm])
    t_prov.setStyle(estilo_tabla(t_prov))
    elementos.append(t_prov)

    doc.build(elementos)
    print(f"PDF Ejecutivo generado: {ruta_salida}")

def generar_pdf_difare(df, ruta_salida, carpeta="excels"):
    tmp_dir = tempfile.mkdtemp()
    ultimo_dia, dias_mes, es_completo = detectar_ultimo_dia_y_proyeccion(carpeta)
    _, ultimo_dia_stock, _, _ = detectar_ultimo_dia_stock_y_venta(carpeta)

    doc = SimpleDocTemplate(ruta_salida, pagesize=landscape(A4),
                             leftMargin=1.5*cm, rightMargin=1.5*cm,
                             topMargin=1.5*cm, bottomMargin=1.5*cm)
    elementos = []
    T = ParagraphStyle("T", fontSize=18, fontName="Helvetica-Bold", textColor=AZUL, alignment=TA_CENTER, spaceAfter=4)
    S = ParagraphStyle("S", fontSize=9, textColor=colors.HexColor("#6B7280"), alignment=TA_CENTER, spaceAfter=14)
    SEC = ParagraphStyle("SEC", fontSize=11, fontName="Helvetica-Bold", textColor=AZUL, spaceBefore=12, spaceAfter=7)
    SEC2 = ParagraphStyle("SEC2", fontSize=10, fontName="Helvetica-Bold", textColor=AZUL_CLARO, spaceBefore=10, spaceAfter=6)
    NOR = ParagraphStyle("NOR", fontSize=9, textColor=colors.HexColor("#374151"), spaceAfter=4, leading=14)

    elementos.append(Paragraph("REPORTE DIFARE - FARMACIAS PROPIAS & CANAL DISTRIBUCION", T))
    elementos.append(Paragraph(f"Genommalab Ecuador - Periodo Enero-Marzo 2026 - {datetime.now().strftime('%d/%m/%Y')}", S))

    df_farm = df[df["UNIDAD"] == "FARMACIAS"].copy()
    df_dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"].copy()
    mes_actual = df_farm["MES"].max() if "MES" in df_farm.columns else "2026-03"

    # ── SECCION 1: OPORTUNIDADES ──
    elementos.append(Paragraph("SECCION 1: OPORTUNIDADES DISPONIBILIDAD Y STOCK - ALERTAS", SEC))

    stock_mes = cargar_stock_por_mes(carpeta)
    stock_actual = stock_mes.get(mes_actual, {})
    stock_bodega_val = stock_actual.get("stock_bodega_val", 0)
    stock_pdv_val = stock_actual.get("stock_pdv_val", 0)
    stock_total_val = stock_bodega_val + stock_pdv_val

    # DOIS con venta proyectada del SAP
    sap_path = detectar_archivo_sap(carpeta)
    df_sap_raw = _leer_excel_hoja_correcta(sap_path) if sap_path else None
    if df_sap_raw is not None:
        venta_sap_farm_dist = df_sap_raw[df_sap_raw["UNIDAD"].isin(["FARMACIAS","DISTRIBUCION DIFARE"])]["VENTA NETA RECUPERO"].sum()
        venta_sap_farm = df_sap_raw[df_sap_raw["UNIDAD"]=="FARMACIAS"]["VENTA NETA RECUPERO"].sum()
    else:
        venta_sap_farm_dist = df[df["UNIDAD"].isin(["FARMACIAS","DISTRIBUCION DIFARE"])]["VENTA NETA RECUPERO"].sum()
        venta_sap_farm = df_farm["VENTA NETA RECUPERO"].sum()

    # DOIS = Stock / (Venta_SAP / ultimo_dia * dias_mes) * dias_mes
    # Simplificado: Stock / Venta_SAP * ultimo_dia
    venta_proy_farm_dist = (venta_sap_farm_dist / ultimo_dia * dias_mes) if ultimo_dia > 0 else 1
    venta_proy_farm = (venta_sap_farm / ultimo_dia * dias_mes) if ultimo_dia > 0 else 1

    dois_bodega = (stock_bodega_val / venta_proy_farm_dist * dias_mes) if venta_proy_farm_dist > 0 else 0
    dois_pdv = (stock_pdv_val / venta_proy_farm * dias_mes) if venta_proy_farm > 0 else 0
    dois_total = (stock_total_val / venta_proy_farm_dist * dias_mes) if venta_proy_farm_dist > 0 else 0

    kpi_stock = [
        ["STOCK BODEGA (Val.)", f"DOIS BODEGA\n(dia {ultimo_dia})",
         "STOCK PDV (Val.)", f"DOIS PDV\n(dia {ultimo_dia})",
         "STOCK TOTAL (Val.)", f"DOIS TOTAL\n(dia {ultimo_dia})"],
        [fmt_money(stock_bodega_val), f"{dois_bodega:.1f} dias",
         fmt_money(stock_pdv_val), f"{dois_pdv:.1f} dias",
         fmt_money(stock_total_val), f"{dois_total:.1f} dias"]
    ]
    t_kstock = Table(kpi_stock, colWidths=[4.5*cm]*6)
    t_kstock.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (1,0), AZUL),
        ("BACKGROUND", (2,0), (3,0), AZUL_CLARO),
        ("BACKGROUND", (4,0), (5,0), VERDE),
        ("BACKGROUND", (0,1), (1,1), colors.HexColor("#DBEAFE")),
        ("BACKGROUND", (2,1), (3,1), colors.HexColor("#EDE9FE")),
        ("BACKGROUND", (4,1), (5,1), colors.HexColor("#D1FAE5")),
        ("TEXTCOLOR", (0,0), (-1,0), BLANCO),
        ("TEXTCOLOR", (0,1), (1,1), AZUL),
        ("TEXTCOLOR", (2,1), (3,1), colors.HexColor("#6D28D9")),
        ("TEXTCOLOR", (4,1), (5,1), VERDE),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 8), ("FONTSIZE", (0,1), (-1,1), 12),
        ("ALIGN", (0,0), (-1,-1), "CENTER"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 8), ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("GRID", (0,0), (-1,-1), 1, BLANCO),
    ]))
    elementos.append(t_kstock)
    elementos.append(Spacer(1, 0.2*cm))

    formula_dois = (f"DOIS Bodega = {fmt_money(stock_bodega_val)} / ({fmt_money(venta_sap_farm_dist)}/{ultimo_dia}*{dias_mes})*{dias_mes} = {dois_bodega:.1f} dias  |  "
                    f"DOIS PDV = {fmt_money(stock_pdv_val)} / ({fmt_money(venta_sap_farm)}/{ultimo_dia}*{dias_mes})*{dias_mes} = {dois_pdv:.1f} dias  |  "
                    f"Stock tomado del dia {ultimo_dia_stock}")
    elementos.append(Paragraph(formula_dois, NOR))
    elementos.append(Spacer(1, 0.3*cm))

    # Alertas bodega
    df_bodega_sap, df_sap_farm_stock, df_sap_farm_todo = cargar_sap_completo(carpeta)

    df_ventas_todos = df[df["UNIDAD"] != "DIFARE S.A."].copy()
    stock_prod_bod = df_bodega_sap.groupby(["IDNEPTUNO","MARCA","PRODUCTO"]).agg(
        stock_bodega=("STOCK","sum")).reset_index() if not df_bodega_sap.empty else pd.DataFrame()
    stock_prod_pdv = df_sap_farm_stock.groupby(["IDNEPTUNO","MARCA","PRODUCTO"]).agg(
        stock_pdv=("STOCK","sum")).reset_index() if not df_sap_farm_stock.empty else pd.DataFrame()
    rotacion = df_ventas_todos.groupby(["IDNEPTUNO","MARCA","PRODUCTO"]).agg(
        unidades_vendidas=("UNIDADES_ROTADAS","sum")).reset_index()

    if not stock_prod_bod.empty:
        analisis = stock_prod_bod.merge(rotacion, on=["IDNEPTUNO","MARCA","PRODUCTO"], how="left")
        if not stock_prod_pdv.empty:
            analisis = analisis.merge(stock_prod_pdv, on=["IDNEPTUNO","MARCA","PRODUCTO"], how="left")
            analisis["stock_pdv"] = analisis["stock_pdv"].fillna(0)
        else:
            analisis["stock_pdv"] = 0
        analisis["rotacion_diaria"] = analisis["unidades_vendidas"].fillna(0) / 90
        analisis["dias_cobertura_bodega"] = analisis.apply(
            lambda r: round(r["stock_bodega"]/r["rotacion_diaria"]) if r["rotacion_diaria"] > 0 else 999, axis=1)
        analisis["alerta"] = analisis["dias_cobertura_bodega"].apply(
            lambda d: "CRITICO" if d < 15 else ("BAJO" if d < 30 else "OK"))
        n_criticos = len(analisis[analisis["alerta"]=="CRITICO"])
        n_bajos = len(analisis[analisis["alerta"]=="BAJO"])
        n_ok = len(analisis[analisis["alerta"]=="OK"])
    else:
        analisis = pd.DataFrame()
        n_criticos = n_bajos = n_ok = 0

    kpi_alertas = [
        ["PRODUCTOS CRITICOS\n(Bodega < 15 dias)", "STOCK BAJO\n(Bodega 15-30 dias)", "STOCK OK\n(Bodega > 30 dias)"],
        [str(n_criticos), str(n_bajos), str(n_ok)]
    ]
    t_alertas = Table(kpi_alertas, colWidths=[9*cm, 9*cm, 9*cm])
    t_alertas.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (0,0), ROJO), ("BACKGROUND", (1,0), (1,0), AMARILLO),
        ("BACKGROUND", (2,0), (2,0), VERDE),
        ("BACKGROUND", (0,1), (0,1), colors.HexColor("#FEE2E2")),
        ("BACKGROUND", (1,1), (1,1), colors.HexColor("#FEF3C7")),
        ("BACKGROUND", (2,1), (2,1), colors.HexColor("#D1FAE5")),
        ("TEXTCOLOR", (0,0), (-1,0), BLANCO),
        ("TEXTCOLOR", (0,1), (0,1), ROJO), ("TEXTCOLOR", (1,1), (1,1), AMARILLO),
        ("TEXTCOLOR", (2,1), (2,1), VERDE),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTNAME", (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9), ("FONTSIZE", (0,1), (-1,1), 18),
        ("ALIGN", (0,0), (-1,-1), "CENTER"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 9), ("BOTTOMPADDING", (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 1, BLANCO),
    ]))
    elementos.append(t_alertas)
    elementos.append(Spacer(1, 0.3*cm))

    if not analisis.empty:
        criticos = analisis[analisis["alerta"]=="CRITICO"].nlargest(15, "unidades_vendidas")
        bajos = analisis[analisis["alerta"]=="BAJO"].nlargest(15, "unidades_vendidas")
        if not criticos.empty:
            elementos.append(Paragraph("PRODUCTOS CRITICOS BODEGA CENTRAL - MENOS DE 15 DIAS", SEC2))
            crit_data = [["MARCA", "PRODUCTO", "STOCK\nBODEGA", "STOCK\nPDV", "ROT.\nDIARIA", "DIAS COB.\nBODEGA"]]
            for _, r in criticos.iterrows():
                crit_data.append([str(r["MARCA"]), str(r["PRODUCTO"])[:38],
                                   fmt_num(r["stock_bodega"]), fmt_num(r.get("stock_pdv",0)),
                                   f"{r['rotacion_diaria']:.1f}", str(int(r["dias_cobertura_bodega"]))])
            t_crit = Table(crit_data, colWidths=[4*cm, 11*cm, 2.8*cm, 2.8*cm, 2.8*cm, 3.6*cm])
            t_crit.setStyle(estilo_tabla(t_crit))
            elementos.append(t_crit)
        if not bajos.empty:
            elementos.append(Paragraph("PRODUCTOS STOCK BAJO BODEGA CENTRAL - ENTRE 15 Y 30 DIAS", SEC2))
            bajo_data = [["MARCA", "PRODUCTO", "STOCK\nBODEGA", "STOCK\nPDV", "ROT.\nDIARIA", "DIAS COB.\nBODEGA"]]
            for _, r in bajos.iterrows():
                bajo_data.append([str(r["MARCA"]), str(r["PRODUCTO"])[:38],
                                   fmt_num(r["stock_bodega"]), fmt_num(r.get("stock_pdv",0)),
                                   f"{r['rotacion_diaria']:.1f}", str(int(r["dias_cobertura_bodega"]))])
            t_bajo = Table(bajo_data, colWidths=[4*cm, 11*cm, 2.8*cm, 2.8*cm, 2.8*cm, 3.6*cm])
            t_bajo.setStyle(estilo_tabla(t_bajo))
            elementos.append(t_bajo)

    # PARETO
    elementos.append(Paragraph("ANALISIS PARETO - PRODUCTOS QUE GENERAN EL 80% DE LA VENTA EN FARMACIAS", SEC))
    universo_pdv = calcular_universo_pdv(carpeta)
    elementos.append(Paragraph(
        f"Universo PDV = {universo_pdv} PDV activos en SAP marzo (venta o stock). "
        f"Presencia = PDV con cualquier registro del producto en SAP. "
        f"Stock tomado del dia {ultimo_dia_stock} (ultimo domingo).", NOR))
    elementos.append(Spacer(1, 0.2*cm))

    pareto_df = calcular_pareto_farmacias(df, df_sap_farm_stock, df_sap_farm_todo, universo_pdv)

    if not pareto_df.empty:
        pareto_data = [["ID NEP", "MARCA", "PRODUCTO", "VENTA", "PESO%", "ACUM%",
                        "UNIVERSO\nPDV", "PDV\nPRESENCIA", "%COBERTURA",
                        "PDV con\nStock=0", "PDV con\nStock<=1", "PDV con\nStock<2", "PDV con\nStock<=3"]]
        for _, r in pareto_df.iterrows():
            uni = int(r["UNIVERSO_PDV"]) if r["UNIVERSO_PDV"] else 0
            pres = int(r["PDV_PRESENCIA"])
            cobertura = (pres / uni * 100) if uni > 0 else 0
            pareto_data.append([
                str(int(r["IDNEPTUNO"])),
                str(r["MARCA"])[:10],
                str(r["PRODUCTO"])[:28],
                fmt_money(r["VENTA"]),
                fmt_pct(r["PCT"]),
                fmt_pct(r["PCT_ACUM"]),
                str(uni),
                str(pres),
                f"{cobertura:.1f}%",
                str(int(r["STOCK_0"])),
                str(int(r["STOCK_1"])),
                str(int(r.get("STOCK_LT2", r["STOCK_2"]))),
                str(int(r["STOCK_3"])),
            ])
        col_w = [1.5*cm, 2.2*cm, 6.0*cm, 2.4*cm, 1.3*cm, 1.3*cm, 1.6*cm, 1.7*cm, 1.7*cm, 1.5*cm, 1.5*cm, 1.5*cm, 1.5*cm]
        t_pareto = Table(pareto_data, colWidths=col_w, repeatRows=1)
        pareto_style = TableStyle([
            ("BACKGROUND", (0,0), (-1,0), AZUL),
            ("TEXTCOLOR", (0,0), (-1,0), BLANCO),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 7),
            ("ALIGN", (3,0), (-1,-1), "CENTER"),
            ("ALIGN", (0,0), (2,-1), "LEFT"),
            ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#E5E7EB")),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ])
        for i in range(1, len(pareto_data)):
            bg = colors.HexColor("#EFF6FF") if i % 2 == 0 else BLANCO
            pareto_style.add("BACKGROUND", (0,i), (-1,i), bg)
            stock0 = int(pareto_df.iloc[i-1]["STOCK_0"])
            if stock0 > 0:
                pareto_style.add("TEXTCOLOR", (9,i), (9,i), ROJO)
                pareto_style.add("FONTNAME", (9,i), (9,i), "Helvetica-Bold")
        t_pareto.setStyle(pareto_style)
        elementos.append(t_pareto)

    # ── SECCION 2: FARMACIAS ──
    elementos.append(Paragraph("SECCION 2: FARMACIAS PROPIAS", SEC))
    venta_farm = df_farm["VENTA NETA RECUPERO"].sum()
    total_pos = df_farm["POS"].nunique()
    top_grupo = df_farm.groupby("GRUPOPDV")["VENTA NETA RECUPERO"].sum().idxmax()

    kpi_f = [["VENTA FARMACIAS", "TOTAL PUNTOS DE VENTA", "GRUPO TOP"],
             [fmt_money(venta_farm), str(total_pos), str(top_grupo)]]
    t_kf = Table(kpi_f, colWidths=[9*cm, 9*cm, 9*cm])
    t_kf.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), AZUL),
        ("BACKGROUND", (0,1), (0,1), colors.HexColor("#1B3A6B")),
        ("BACKGROUND", (1,1), (1,1), colors.HexColor("#2E75B6")),
        ("BACKGROUND", (2,1), (2,1), colors.HexColor("#3B82F6")),
        ("TEXTCOLOR", (0,0), (-1,-1), BLANCO),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTNAME", (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9), ("FONTSIZE", (0,1), (-1,1), 13),
        ("ALIGN", (0,0), (-1,-1), "CENTER"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 9), ("BOTTOMPADDING", (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 1, BLANCO),
    ]))
    elementos.append(t_kf)
    elementos.append(Spacer(1, 0.2*cm))

    g_pdv = grafico_grupos_pdv(df, tmp_dir)
    if g_pdv:
        elementos.append(Image(g_pdv, width=26*cm, height=5.5*cm))

    elementos.append(Paragraph("TOP 20 FARMACIAS POR VENTA NETA", SEC2))
    por_pos = df_farm.groupby(["POS","GRUPOPDV"]).agg(
        ventas=("VENTA NETA RECUPERO","sum"), unidades=("UNIDADES_ROTADAS","sum")
    ).reset_index().nlargest(20, "ventas")
    top_pos_data = [["FARMACIA (POS)", "GRUPO PDV", "VENTA NETA", "UNIDADES"]]
    for _, r in por_pos.iterrows():
        top_pos_data.append([str(r["POS"])[:45], str(r["GRUPOPDV"]),
                              fmt_money(r["ventas"]), fmt_num(r["unidades"])])
    t_pos = Table(top_pos_data, colWidths=[13*cm, 6*cm, 4.5*cm, 3.5*cm])
    t_pos.setStyle(estilo_tabla(t_pos))
    elementos.append(t_pos)

    elementos.append(Paragraph("FARMACIAS CON MENOR VENTA - OPORTUNIDAD DE CRECIMIENTO", SEC2))
    bottom_pos = df_farm.groupby(["POS","GRUPOPDV"]).agg(
        ventas=("VENTA NETA RECUPERO","sum"), unidades=("UNIDADES_ROTADAS","sum")
    ).reset_index().nsmallest(15, "ventas")
    bot_data = [["FARMACIA (POS)", "GRUPO PDV", "VENTA NETA", "UNIDADES"]]
    for _, r in bottom_pos.iterrows():
        bot_data.append([str(r["POS"])[:45], str(r["GRUPOPDV"]),
                         fmt_money(r["ventas"]), fmt_num(r["unidades"])])
    t_bot = Table(bot_data, colWidths=[13*cm, 6*cm, 4.5*cm, 3.5*cm])
    t_bot.setStyle(estilo_tabla(t_bot))
    elementos.append(t_bot)

    # ── SECCION 3: DISTRIBUCION ──
    elementos.append(Paragraph("SECCION 3: CANAL DISTRIBUCION", SEC))
    venta_dist = df_dist["VENTA NETA RECUPERO"].sum()
    total_clientes = df_dist["PROPIETARIO"].nunique()
    top_grupo_dist = df_dist.groupby("GRUPOCLIENTE")["VENTA NETA RECUPERO"].sum().idxmax() if not df_dist.empty else "N/A"

    kpi_d = [["VENTA DISTRIBUCION", "TOTAL CLIENTES", "GRUPO TOP"],
             [fmt_money(venta_dist), str(total_clientes), str(top_grupo_dist)]]
    t_kd = Table(kpi_d, colWidths=[9*cm, 9*cm, 9*cm])
    t_kd.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), AZUL_CLARO),
        ("BACKGROUND", (0,1), (0,1), colors.HexColor("#1B3A6B")),
        ("BACKGROUND", (1,1), (1,1), colors.HexColor("#2E75B6")),
        ("BACKGROUND", (2,1), (2,1), colors.HexColor("#3B82F6")),
        ("TEXTCOLOR", (0,0), (-1,-1), BLANCO),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTNAME", (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9), ("FONTSIZE", (0,1), (-1,1), 13),
        ("ALIGN", (0,0), (-1,-1), "CENTER"), ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 9), ("BOTTOMPADDING", (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 1, BLANCO),
    ]))
    elementos.append(t_kd)
    elementos.append(Spacer(1, 0.2*cm))

    g_gc = grafico_grupos_cliente(df, tmp_dir)
    if g_gc:
        elementos.append(Image(g_gc, width=26*cm, height=5*cm))

    elementos.append(Paragraph("TOP 20 CLIENTES POR VENTA NETA", SEC2))
    top_cli = df_dist.groupby(["PROPIETARIO","GRUPOCLIENTE"]).agg(
        ventas=("VENTA NETA RECUPERO","sum"), unidades=("UNIDADES_ROTADAS","sum")
    ).reset_index().nlargest(20, "ventas")
    cli_data = [["CLIENTE (RAZON SOCIAL)", "GRUPO", "VENTA NETA", "UNIDADES"]]
    for _, r in top_cli.iterrows():
        cli_data.append([str(r["PROPIETARIO"])[:45], str(r["GRUPOCLIENTE"])[:20],
                         fmt_money(r["ventas"]), fmt_num(r["unidades"])])
    t_cli = Table(cli_data, colWidths=[13*cm, 6*cm, 4.5*cm, 3.5*cm])
    t_cli.setStyle(estilo_tabla(t_cli))
    elementos.append(t_cli)

    doc.build(elementos)
    print(f"PDF DIFARE generado: {ruta_salida}")
