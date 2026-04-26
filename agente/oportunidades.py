"""
agente/oportunidades.py — Análisis accionable para el KAM.

Funciones que alimentan el módulo "Oportunidades" del dashboard:

  ampliar_vectorizacion()  → SKUs con %Pon ≥ 80% (candidatos para pedir
                             ampliación de presencia) + tendencia mes en curso
                             vs último mes completo + venta potencial.

  venta_perdida()          → estimación de venta perdida por baja cobertura,
                             usando venta_promedio_PDV × PDVs_faltantes.

Se apoya en `analitica.oportunidad_vectorizacion()` que ya entrega presencia,
PDVs con venta del último mes y stock buckets por producto.
"""
from __future__ import annotations

import re
from typing import Optional

import pandas as pd

from . import analitica


# ── Helpers compartidos ──────────────────────────────────────────

def _df_farm() -> pd.DataFrame:
    d = analitica.cargar_data()
    df = d["df_todos"]
    if df is None or df.empty:
        return pd.DataFrame()
    return df[df["UNIDAD"] == "FARMACIAS"]


def _meses_ordenados(df_farm: pd.DataFrame) -> list[str]:
    if df_farm.empty or "MES" not in df_farm.columns:
        return []
    return sorted(df_farm["MES"].dropna().astype(str).unique())


def _detectar_mes_completo_y_actual(df_farm: pd.DataFrame) -> tuple[Optional[str], Optional[str], dict]:
    """Devuelve (mes_completo, mes_actual_parcial, stats) para análisis de tendencia.

    Detección: si el último mes tiene <50% del row count del penúltimo, el
    último es parcial → mes_completo = penúltimo, mes_actual = último.
    Caso contrario (todo completo): mes_completo = último, mes_actual = None.
    """
    meses = _meses_ordenados(df_farm)
    if not meses:
        return None, None, {}
    if len(meses) < 2:
        return meses[-1], None, {}
    rows_last = int((df_farm["MES"].astype(str) == meses[-1]).sum())
    rows_prev = int((df_farm["MES"].astype(str) == meses[-2]).sum())
    if rows_prev > 0 and rows_last < rows_prev * 0.5:
        return meses[-2], meses[-1], {"rows_actual": rows_last, "rows_completo": rows_prev}
    return meses[-1], None, {"rows_actual": rows_last, "rows_completo": rows_last}


def _pdvs_con_venta_por_producto(df_farm: pd.DataFrame, mes: Optional[str]) -> dict:
    """Para un MES específico, devuelve {IDNEPTUNO: nº PDVs con venta > 0}."""
    if not mes or df_farm.empty:
        return {}
    sub = df_farm[(df_farm["MES"].astype(str) == mes)
                  & (df_farm["VENTA NETA RECUPERO"] > 0)]
    if sub.empty or "POS" not in sub.columns or "IDNEPTUNO" not in sub.columns:
        return {}
    return sub.groupby("IDNEPTUNO")["POS"].nunique().to_dict()


def _dias_unicos_por_mes(df_farm: pd.DataFrame, mes: str) -> int:
    """Cuenta días únicos del SAP dentro de `mes` (formato YYYY-MM).
    Útil para proyección de PDVs en el mes en curso."""
    if df_farm.empty or "DIA" not in df_farm.columns or not mes:
        return 0
    sub = df_farm[df_farm["MES"].astype(str) == mes]
    if sub.empty:
        return 0
    dias = sub["DIA"].dropna().astype(str).map(lambda x: re.sub(r"\D", "", x)[:8])
    dias = [d for d in dias if len(d) == 8]
    return len(set(dias)) if dias else 0


def _dias_mes(mes_yyyy_mm: str) -> int:
    """Días totales del mes (28-31)."""
    import calendar
    try:
        y, m = mes_yyyy_mm.split("-")
        return calendar.monthrange(int(y), int(m))[1]
    except Exception:
        return 30


# ── 1) Ampliar Vectorización ─────────────────────────────────────

def ampliar_vectorizacion(min_pon_pct: float = 80.0,
                           top_n: int = 100,
                           solo_farmacias: bool = True) -> dict:
    """Lista SKUs con %Pon ≥ `min_pon_pct` — productos donde donde están
    rotando bien y vale la pena pedir ampliación de presencia.

    Cada item incluye:
      - venta_total, presencia, universo
      - %Cob (presencia / universo)
      - %Pon (PDVs con venta último mes completo / presencia)
      - PDVs con venta proyectados del mes EN CURSO + tendencia vs mes anterior
      - venta_potencial estimada si presencia llegara al universo
    """
    rows_raw = analitica.oportunidad_vectorizacion()
    if not rows_raw:
        return {"items": [], "resumen": {}}

    df_farm = _df_farm()
    mes_completo, mes_actual_parcial, _stats = _detectar_mes_completo_y_actual(df_farm)
    pdv_venta_actual = _pdvs_con_venta_por_producto(df_farm, mes_actual_parcial) if mes_actual_parcial else {}

    # Días corridos del mes parcial (para proyección)
    dias_corridos = _dias_unicos_por_mes(df_farm, mes_actual_parcial) if mes_actual_parcial else 0
    dias_total_mes = _dias_mes(mes_actual_parcial) if mes_actual_parcial else 30

    items = []
    for r in rows_raw:
        venta = float(r.get("VENTA", 0) or 0)
        presencia = int(r.get("PDV_PRESENCIA", 0) or 0)
        universo = int(r.get("UNIVERSO_PDV", 0) or 0) or 1
        pdv_v_ult = int(r.get("PDV_VENTA_ULT_MES", 0) or 0)

        if presencia <= 0:
            continue

        cob_pct = round(presencia / universo * 100, 1)
        pon_pct = round(pdv_v_ult / presencia * 100, 1)

        if pon_pct < min_pon_pct:
            continue

        # Tendencia mes en curso vs último mes completo
        idn = r.get("IDNEPTUNO", "")
        pdv_actual_acum = int(pdv_venta_actual.get(idn, 0))
        if dias_corridos > 0 and dias_total_mes > 0:
            pdv_actual_proy = round(pdv_actual_acum / dias_corridos * dias_total_mes)
        else:
            pdv_actual_proy = pdv_actual_acum
        if pdv_v_ult > 0:
            tendencia_pct = round((pdv_actual_proy / pdv_v_ult - 1) * 100, 1)
        else:
            tendencia_pct = None

        # Venta potencial si presencia llegara al universo (proporcional al
        # ratio universo/presencia, aplicado a venta_total).
        if presencia > 0:
            venta_potencial = round(venta * (universo / presencia), 0)
            uplift = round(venta_potencial - venta, 0)
        else:
            venta_potencial = venta
            uplift = 0

        items.append({
            "IDNEPTUNO": idn,
            "MARCA": r.get("MARCA", ""),
            "PRODUCTO": r.get("PRODUCTO", ""),
            "VENTA": round(venta, 0),
            "UNIVERSO_PDV": universo,
            "PDV_PRESENCIA": presencia,
            "cobertura_pct": cob_pct,
            "PDV_VENTA_ULT_MES": pdv_v_ult,
            "ponderada_pct": pon_pct,
            "PDV_VENTA_ACTUAL_ACUM": pdv_actual_acum,
            "PDV_VENTA_ACTUAL_PROY": pdv_actual_proy,
            "tendencia_pct": tendencia_pct,
            "VENTA_POTENCIAL": venta_potencial,
            "UPLIFT": uplift,
        })

    items.sort(key=lambda x: x["UPLIFT"], reverse=True)
    items = items[:top_n]

    total_uplift = sum(i["UPLIFT"] for i in items)
    return {
        "items": items,
        "resumen": {
            "total_skus": len(items),
            "total_uplift": round(total_uplift, 0),
            "mes_completo": mes_completo,
            "mes_actual_parcial": mes_actual_parcial,
            "dias_corridos_mes_actual": dias_corridos,
            "dias_total_mes_actual": dias_total_mes,
            "umbral_pon_pct": min_pon_pct,
        },
    }


# ── 2) Venta Perdida por baja cobertura ──────────────────────────

def venta_perdida(top_n: int = 50) -> dict:
    """Estimación de venta perdida por NO estar en más PDVs.

    Para cada SKU:
      venta_prom_PDV    = venta_total / pdv_presencia
      pdvs_faltantes    = universo_pdv - pdv_presencia
      venta_perdida_$   = venta_prom_PDV × pdvs_faltantes  (escenario optimista)

    Filtros: solo SKUs con presencia > 0 y pdvs_faltantes > 0.
    """
    rows_raw = analitica.oportunidad_vectorizacion()
    if not rows_raw:
        return {"items": [], "resumen": {}}

    items = []
    for r in rows_raw:
        venta = float(r.get("VENTA", 0) or 0)
        presencia = int(r.get("PDV_PRESENCIA", 0) or 0)
        universo = int(r.get("UNIVERSO_PDV", 0) or 0)
        if presencia <= 0 or universo <= 0:
            continue
        faltantes = universo - presencia
        if faltantes <= 0:
            continue
        venta_prom_pdv = venta / presencia
        venta_perdida_est = venta_prom_pdv * faltantes
        cob_pct = round(presencia / universo * 100, 1)
        items.append({
            "IDNEPTUNO": r.get("IDNEPTUNO", ""),
            "MARCA": r.get("MARCA", ""),
            "PRODUCTO": r.get("PRODUCTO", ""),
            "VENTA": round(venta, 0),
            "UNIVERSO_PDV": universo,
            "PDV_PRESENCIA": presencia,
            "PDV_FALTANTES": faltantes,
            "cobertura_pct": cob_pct,
            "venta_prom_pdv": round(venta_prom_pdv, 2),
            "VENTA_PERDIDA_EST": round(venta_perdida_est, 0),
        })

    items.sort(key=lambda x: x["VENTA_PERDIDA_EST"], reverse=True)
    items = items[:top_n]
    total_perdida = sum(i["VENTA_PERDIDA_EST"] for i in items)
    total_venta = sum(i["VENTA"] for i in items)
    pct_perdida_vs_venta = round((total_perdida / total_venta * 100), 1) if total_venta > 0 else 0
    return {
        "items": items,
        "resumen": {
            "total_skus": len(items),
            "total_venta_perdida": round(total_perdida, 0),
            "total_venta_actual": round(total_venta, 0),
            "pct_perdida_vs_actual": pct_perdida_vs_venta,
        },
    }
