"""
agente/campo_pandas.py — Implementación pandas de los endpoints "Vista Campo".

Reemplaza la antigua implementación basada en SQLite (data.db) que se rompía
cuando data.db no estaba presente en el contenedor de Railway. Toda la data
proviene del caché de pandas que ya carga `analitica.cargar_data()`.

Endpoints servidos por estas funciones:
    /grupos              → obtener_grupos()
    /farmacias?grupo=X   → obtener_farmacias(grupo)
    /buscar_pos?q=X      → buscar_pos(q)
    /detalle_pos POST    → obtener_detalle_pos(pos)
    /productos_faltantes → obtener_faltantes(pos)
    /chat POST           → obtener_contexto_chat(pos)
"""
from __future__ import annotations

import calendar
import re
from typing import Optional

import pandas as pd

from . import analitica


# ── Mapeos de display ────────────────────────────────────────────
_GRUPO_DISPLAY = {
    "cafi mostrador": "Cruz Azul Mostrador",
    "cafa mostrador": "Cruz Azul Mostrador",
    "cofa mostrador": "Cruz Azul Mostrador",
    "cafi autoservicio": "Cruz Azul Autoservicio",
    "cafa autoservicio": "Cruz Azul Autoservicio",
}

_GRUPO_DISPLAY_INV = {
    "cruz azul mostrador": ("cafi mostrador", "cafa mostrador", "cofa mostrador"),
    "cruz azul autoservicio": ("cafi autoservicio", "cafa autoservicio"),
}

_MESES_ES = {"01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr",
             "05": "May", "06": "Jun", "07": "Jul", "08": "Ago",
             "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic"}


# ── Helpers internos ─────────────────────────────────────────────

def _df_farm() -> pd.DataFrame:
    """DataFrame de FARMACIAS solamente (alias estable + columna _pdv_key)."""
    d = analitica.cargar_data()
    df = d["df_todos"]
    if df is None or df.empty:
        return pd.DataFrame()
    farm = df[df["UNIDAD"] == "FARMACIAS"].copy()
    if farm.empty:
        return farm
    # Identificador estable de PDV: CODIGOPDV si existe, si no POS
    if "CODIGOPDV" in farm.columns:
        codigo = farm["CODIGOPDV"].fillna("").astype(str).str.strip()
        farm["_pdv_key"] = codigo.where(codigo != "", farm["POS"].astype(str))
    else:
        farm["_pdv_key"] = farm["POS"].astype(str)
    return farm


def _resolver_pdv_key(pos_nombre: str) -> tuple[str, str]:
    """Dado el nombre POS recibido del frontend, devuelve (col, valor) para
    filtrar el DF — usa CODIGOPDV si está disponible, sino POS."""
    farm = _df_farm()
    if farm.empty:
        return "POS", pos_nombre
    coincidencias = farm[farm["POS"].astype(str) == pos_nombre]
    if not coincidencias.empty:
        codigos = coincidencias["_pdv_key"].dropna().astype(str)
        codigos = codigos[codigos != ""].unique()
        if len(codigos) > 0:
            return "_pdv_key", codigos[0]
    return "POS", pos_nombre


def _filtrar_pos(df: pd.DataFrame, pos_nombre: str) -> pd.DataFrame:
    """Aplica el filtro PDV (vía CODIGOPDV cuando es posible)."""
    if df.empty:
        return df
    col, val = _resolver_pdv_key(pos_nombre)
    if col == "_pdv_key" and "_pdv_key" in df.columns:
        return df[df["_pdv_key"].astype(str) == val]
    return df[df["POS"].astype(str) == pos_nombre]


# ── 1) /grupos ───────────────────────────────────────────────────

def obtener_grupos() -> list[dict]:
    """Lista grupos de farmacias con venta acumulada y conteo de PDVs."""
    farm = _df_farm()
    if farm.empty or "GRUPOPDV" not in farm.columns:
        return []
    g = farm.groupby("GRUPOPDV").agg(
        ventas=("VENTA NETA RECUPERO", "sum"),
        pos_count=("_pdv_key", "nunique"),
    ).reset_index()

    agrupados: dict[str, dict] = {}
    for _, row in g.iterrows():
        nombre_raw = str(row["GRUPOPDV"] or "").strip()
        nombre = _GRUPO_DISPLAY.get(nombre_raw.lower(), nombre_raw)
        if not nombre:
            continue
        bucket = agrupados.setdefault(nombre, {"ventas": 0.0, "pos_count": 0})
        bucket["ventas"] += float(row["ventas"] or 0)
        bucket["pos_count"] += int(row["pos_count"] or 0)

    resultado = [
        {"grupo": k, "ventas": round(v["ventas"], 2), "total_pos": v["pos_count"]}
        for k, v in agrupados.items()
    ]
    resultado.sort(key=lambda x: x["ventas"], reverse=True)
    return resultado


# ── 2) /farmacias?grupo=X ────────────────────────────────────────

def obtener_farmacias(grupo: str) -> list[dict]:
    """Lista farmacias dentro de un grupo (ordenadas por venta)."""
    farm = _df_farm()
    if farm.empty:
        return []
    grupo_clean = (grupo or "").strip()
    grupos_raw = _GRUPO_DISPLAY_INV.get(grupo_clean.lower(), (grupo_clean.lower(),))
    grupos_raw_lower = set(g.lower() for g in grupos_raw)

    mask = farm["GRUPOPDV"].astype(str).str.lower().isin(grupos_raw_lower)
    sub = farm[mask]
    if sub.empty:
        # Fallback más laxo: contains
        sub = farm[farm["GRUPOPDV"].astype(str).str.lower().str.contains(grupo_clean.lower(), na=False)]
    if sub.empty:
        return []

    g = sub.groupby("_pdv_key").agg(
        pos_nombre=("POS", "first"),
        ventas=("VENTA NETA RECUPERO", "sum"),
        unidades=("UNIDADES_ROTADAS", "sum"),
    ).reset_index()
    g = g.sort_values("ventas", ascending=False)
    return [
        {
            "pos": str(r["pos_nombre"]),
            "codigo": str(r["_pdv_key"]),
            "ventas": round(float(r["ventas"] or 0), 2),
            "unidades": int(r["unidades"] or 0),
        }
        for _, r in g.iterrows()
    ]


# ── 3) /buscar_pos?q=X ───────────────────────────────────────────

def buscar_pos(texto: str, limit: int = 30) -> list[dict]:
    farm = _df_farm()
    if farm.empty or not texto or len(texto) < 2:
        return []
    t = texto.lower()
    sub = farm[farm["POS"].astype(str).str.lower().str.contains(t, na=False)]
    if sub.empty:
        return []
    g = sub.groupby("POS").agg(ventas=("VENTA NETA RECUPERO", "sum")).reset_index()
    g = g.sort_values("ventas", ascending=False).head(limit)
    return [
        {"pos": str(r["POS"]), "ventas": round(float(r["ventas"] or 0), 2)}
        for _, r in g.iterrows()
    ]


# ── 4) /detalle_pos POST ─────────────────────────────────────────

def _label_mes(mes_key: str) -> str:
    if not mes_key:
        return mes_key
    if "-" in mes_key:
        mm = mes_key.split("-")[1]
    elif len(mes_key) >= 6:
        mm = mes_key[4:6]
    else:
        return mes_key
    return _MESES_ES.get(mm, mes_key)


def _dias_en_mes(mes_key: str) -> int:
    try:
        if "-" in mes_key:
            y, m = mes_key.split("-")
        else:
            y, m = mes_key[:4], mes_key[4:6]
        return calendar.monthrange(int(y), int(m))[1]
    except Exception:
        return 30


def _calc_proyeccion(tend_ord: list[dict]) -> Optional[dict]:
    """Proyección del cierre del mes en curso (lineal) o del próximo mes."""
    if not tend_ord:
        return None
    last = tend_ord[-1]
    if last.get("parcial"):
        valor_real = last["valor"]
        dias_data = last.get("dias_con_data", 0)
        dias_mes = last.get("dias_mes", 30)
        proy = (valor_real / dias_data * dias_mes) if dias_data > 0 else valor_real
        pct_vs_prev = None
        if len(tend_ord) >= 2:
            prev = tend_ord[-2]["valor"]
            if prev > 0:
                pct_vs_prev = round((proy - prev) / prev * 100, 1)
        return {
            "valor": round(proy, 2),
            "label": "Proy. " + last["label"],
            "mes_en_curso": True,
            "crecimiento_pct": pct_vs_prev,
            "metodo": f"lineal {round(valor_real, 2)}/{dias_data}*{dias_mes}",
        }
    # Proyectar próximo mes con crecimiento promedio
    if len(tend_ord) == 1:
        return {"valor": round(tend_ord[0]["valor"], 2), "label": "Proy.", "metodo": "ultimo mes"}
    crec = []
    for i in range(1, len(tend_ord)):
        prev = tend_ord[i - 1].get("valor_prorrateado", tend_ord[i - 1]["valor"])
        cur = tend_ord[i].get("valor_prorrateado", tend_ord[i]["valor"])
        if prev > 0:
            crec.append((cur - prev) / prev)
    base = tend_ord[-1].get("valor_prorrateado", tend_ord[-1]["valor"])
    if not crec:
        return {"valor": round(base, 2), "label": "Proy.", "metodo": "ultimo mes"}
    avg = sum(crec) / len(crec)
    proy = base * (1 + avg)
    return {
        "valor": round(proy, 2),
        "label": "Proy.",
        "crecimiento_pct": round(avg * 100, 1),
        "metodo": f"crecimiento promedio {round(avg * 100, 1)}%",
    }


def obtener_detalle_pos(pos_nombre: str) -> dict:
    """Detalle completo de una farmacia: tendencia, top 5, stock, proyección."""
    farm = _df_farm()
    sub = _filtrar_pos(farm, pos_nombre)
    if sub.empty:
        return {"error": f"No se encontro {pos_nombre}"}

    grupo_pdv_raw = sub["GRUPOPDV"].dropna().astype(str).iloc[0] if "GRUPOPDV" in sub.columns and not sub["GRUPOPDV"].dropna().empty else ""
    grupo_pdv = _GRUPO_DISPLAY.get(grupo_pdv_raw.lower(), grupo_pdv_raw)

    venta_total = float(sub["VENTA NETA RECUPERO"].sum())
    unidades = int(sub["UNIDADES_ROTADAS"].sum())

    # % del total de farmacias
    total_farm = float(farm["VENTA NETA RECUPERO"].sum()) if not farm.empty else 0
    pct = (venta_total / total_farm * 100) if total_farm > 0 else 0

    # Tendencia mensual usando MES (formato YYYY-MM)
    tend: dict[str, float] = {}
    if "MES" in sub.columns:
        agrup = sub.groupby("MES")["VENTA NETA RECUPERO"].sum()
        for mes_key, val in agrup.items():
            tend[str(mes_key)] = round(float(val or 0), 2)

    # Días distintos por mes — para detectar mes parcial y prorratear
    dias_por_mes: dict[str, set] = {}
    if "DIA" in sub.columns:
        for mes_key in tend.keys():
            sub_mes = sub[sub["MES"].astype(str) == mes_key]
            dias = sub_mes["DIA"].dropna().astype(str).map(lambda x: re.sub(r"\D", "", x)[:8])
            dias = [d for d in dias if len(d) == 8]
            dias_por_mes[mes_key] = set(dias)

    tend_ord = []
    for mes_key in sorted(tend.keys()):
        mm = mes_key.split("-")[1] if "-" in mes_key else mes_key[4:6] if len(mes_key) >= 6 else ""
        label = _MESES_ES.get(mm, mes_key)
        valor = tend[mes_key]
        dias_data = len(dias_por_mes.get(mes_key, set()))
        dias_tot = _dias_en_mes(mes_key)
        entry = {
            "mes": mes_key, "label": label, "valor": valor,
            "dias_con_data": dias_data, "dias_mes": dias_tot, "parcial": False,
        }
        if 0 < dias_data < dias_tot:
            entry["valor_real"] = valor
            entry["valor_prorrateado"] = round(valor / dias_data * dias_tot, 2)
            entry["parcial"] = True
        tend_ord.append(entry)

    proyeccion = _calc_proyeccion(tend_ord)

    # Top 5 productos
    top_map = sub.groupby("PRODUCTO")["VENTA NETA RECUPERO"].sum().sort_values(ascending=False).head(5)
    top_5 = {str(k): round(float(v or 0), 2) for k, v in top_map.items()}

    # Stock
    stock_info = _stock_pos(pos_nombre)

    return {
        "pos": pos_nombre,
        "grupo_pdv": grupo_pdv,
        "venta_total": round(venta_total, 2),
        "unidades_rotadas": unidades,
        "pct_del_total": round(pct, 2),
        "tendencia_mensual": tend,
        "tendencia_ordenada": tend_ord,
        "proyeccion_proximo_mes": proyeccion,
        "top_5_productos": top_5,
        "stock_info": stock_info,
    }


def _stock_pos(pos_nombre: str) -> dict:
    """Stock detallado del PDV — usa farm_todo (SAP completo) y filtra por último día."""
    d = analitica.cargar_data()
    farm_todo = d.get("farm_todo")
    if farm_todo is None or farm_todo.empty:
        return {"mensaje": "Sin registros en SAP", "detalle_completo": []}

    # Aplicar mismo filtro PDV
    col, val = _resolver_pdv_key(pos_nombre)
    if col == "_pdv_key" and "CODIGOPDV" in farm_todo.columns:
        codigo = farm_todo["CODIGOPDV"].fillna("").astype(str).str.strip()
        key = codigo.where(codigo != "", farm_todo["POS"].astype(str))
        sub_sap = farm_todo[key.astype(str) == val]
    else:
        sub_sap = farm_todo[farm_todo["POS"].astype(str) == pos_nombre]

    if sub_sap.empty:
        return {"mensaje": "Sin registros en SAP", "detalle_completo": []}

    # Por cada IDNEPTUNO, el día más reciente
    if "DIA" not in sub_sap.columns or "IDNEPTUNO" not in sub_sap.columns:
        return {"mensaje": "Datos SAP incompletos", "detalle_completo": []}

    sub_sap = sub_sap.copy()
    sub_sap["_dia_norm"] = sub_sap["DIA"].astype(str)
    idx_max = sub_sap.groupby("IDNEPTUNO")["_dia_norm"].transform("max")
    ult = sub_sap[sub_sap["_dia_norm"] == idx_max].drop_duplicates(subset=["IDNEPTUNO"])

    if ult.empty:
        return {"mensaje": "Sin registros en SAP", "detalle_completo": []}

    detalle = [
        {
            "producto": str(r.get("PRODUCTO", "")),
            "id_neptuno": int(r["IDNEPTUNO"]) if pd.notna(r.get("IDNEPTUNO")) else 0,
            "stock_unid": float(r.get("STOCK", 0) or 0),
            "stock_val": round(float(r.get("STOCK_VALORIZADO", 0) or 0), 2),
            "dia": str(r.get("DIA", "")),
        }
        for _, r in ult.iterrows()
    ]
    detalle.sort(key=lambda x: (x["stock_val"], x["stock_unid"]), reverse=True)

    ultimo_dia = max((d["dia"] for d in detalle), default="")
    total_unid = sum(d["stock_unid"] for d in detalle)
    total_val = sum(d["stock_val"] for d in detalle)
    con_stock = [d for d in detalle if d["stock_unid"] > 0]
    sin_stock = [d for d in detalle if d["stock_unid"] == 0]
    bajo = [d for d in con_stock if 0 < d["stock_unid"] <= 3]

    return {
        "fecha": ultimo_dia,
        "total_productos": len(detalle),
        "total_con_stock": len(con_stock),
        "total_sin_stock": len(sin_stock),
        "total_unidades": round(total_unid, 0),
        "total_valorizado": round(total_val, 2),
        "detalle_completo": detalle,
        "sin_stock": [d["producto"] for d in sin_stock][:8],
        "bajo_stock": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in bajo][:8],
        "detalle_stock": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in con_stock[:15]],
        "con_stock_ok": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in con_stock if d["stock_unid"] > 3][:5],
    }


# ── 5) /productos_faltantes POST ─────────────────────────────────

def obtener_faltantes(pos_nombre: str) -> dict:
    """Top 5 productos que vende la red pero no este PDV — score por oportunidad."""
    farm = _df_farm()
    if farm.empty:
        return {"pos": pos_nombre, "error": "No hay data"}

    sub = _filtrar_pos(farm, pos_nombre)
    if sub.empty:
        return {"pos": pos_nombre, "error": f"No se encontro {pos_nombre}"}

    productos_en = set(sub["PRODUCTO"].dropna().unique())

    ranking = farm.groupby(["PRODUCTO", "MARCA"]).agg(
        venta_total=("VENTA NETA RECUPERO", "sum"),
        num_farmacias=("_pdv_key", "nunique"),
        unidades_totales=("UNIDADES_ROTADAS", "sum"),
    ).reset_index().sort_values("venta_total", ascending=False)

    total_farmacias = int(farm["_pdv_key"].nunique())

    faltantes_df = ranking[~ranking["PRODUCTO"].isin(productos_en)].head(20)
    resultado = []
    for _, r in faltantes_df.head(5).iterrows():
        n_farm = int(r["num_farmacias"] or 0)
        venta = float(r["venta_total"] or 0)
        vta_prom = (venta / n_farm) if n_farm > 0 else 0
        pen = (n_farm / total_farmacias * 100) if total_farmacias > 0 else 0
        score = vta_prom * (n_farm / total_farmacias) if total_farmacias > 0 else 0
        resultado.append({
            "marca": str(r["MARCA"]),
            "producto": str(r["PRODUCTO"]),
            "venta_global_total": round(venta, 2),
            "venta_promedio_por_farmacia": round(vta_prom, 2),
            "disponible_en_farmacias": n_farm,
            "penetracion_mercado": round(pen, 1),
            "unidades_totales_vendidas": int(r["unidades_totales"] or 0),
            "score_oportunidad": round(score, 2),
        })

    return {
        "pos": pos_nombre,
        "total_productos_faltantes": int(len(faltantes_df)),
        "top_5_productos_faltantes": resultado,
        "productos_en_farmacia": len(productos_en),
        "productos_globales": int(len(ranking)),
        "total_farmacias_red": total_farmacias,
    }


# ── 6) Contexto para /chat ───────────────────────────────────────

def obtener_contexto_chat(pos_nombre: Optional[str]) -> dict:
    """Construye el contexto que se pasa al prompt de Claude."""
    if pos_nombre:
        detalle = obtener_detalle_pos(pos_nombre)
        if detalle.get("error"):
            return {"error": detalle["error"]}
        faltantes = obtener_faltantes(pos_nombre).get("top_5_productos_faltantes", [])
        return {
            "pos": pos_nombre,
            "grupo": detalle.get("grupo_pdv", ""),
            "venta_total": detalle.get("venta_total", 0),
            "pct_total": detalle.get("pct_del_total", 0),
            "tendencia": detalle.get("tendencia_mensual", {}),
            "top_productos": detalle.get("top_5_productos", {}),
            "stock": detalle.get("stock_info", {}),
            "productos_faltantes_oportunidad": faltantes,
        }
    # Sin POS: vista general
    farm = _df_farm()
    d = analitica.cargar_data()
    df = d["df_todos"]
    venta_farm = float(farm["VENTA NETA RECUPERO"].sum()) if not farm.empty else 0
    dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"] if not df.empty else pd.DataFrame()
    venta_dist = float(dist["VENTA NETA RECUPERO"].sum()) if not dist.empty else 0
    top_f = (farm.groupby("POS")["VENTA NETA RECUPERO"].sum().sort_values(ascending=False).head(5)
             if not farm.empty else pd.Series(dtype=float))
    no_difare = df[df["UNIDAD"] != "DIFARE S.A."] if not df.empty else pd.DataFrame()
    top_m = (no_difare.groupby("MARCA")["VENTA NETA RECUPERO"].sum().sort_values(ascending=False).head(5)
             if not no_difare.empty else pd.Series(dtype=float))
    return {
        "venta_farmacias": round(venta_farm, 2),
        "venta_distribucion": round(venta_dist, 2),
        "top_farmacias": {str(k): round(float(v or 0), 2) for k, v in top_f.items()},
        "top_marcas": {str(k): round(float(v or 0), 2) for k, v in top_m.items()},
    }
