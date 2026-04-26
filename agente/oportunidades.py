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

    Heurística primaria: mira el último DIA disponible del SAP en el último
    mes. Si el día (DD) es menor que (dias_del_mes - 2), el mes es parcial.
    Esto es más robusto que comparar row counts, porque el SAP semanal
    acumulado puede traer muchos registros incluso de un mes con pocos días.
    """
    meses = _meses_ordenados(df_farm)
    if not meses:
        return None, None, {}
    if len(meses) < 2:
        return meses[-1], None, {}
    last = meses[-1]
    # Vía DIA del SAP — más confiable
    if "DIA" in df_farm.columns:
        dias_last = df_farm[df_farm["MES"].astype(str) == last]["DIA"].dropna().astype(str)
        dias_norm = [re.sub(r"\D", "", str(d))[:8] for d in dias_last]
        dias_norm = [d for d in dias_norm if len(d) == 8]
        if dias_norm:
            try:
                max_dia = max(dias_norm)
                day_num = int(max_dia[6:8])
                dias_total = _dias_mes(last)
                if day_num < dias_total - 2:
                    return meses[-2], last, {"dia_corte": day_num, "dias_total_mes": dias_total}
            except Exception:
                pass
    # Fallback: row count (heurística antigua, solo si no hay DIA usable)
    rows_last = int((df_farm["MES"].astype(str) == meses[-1]).sum())
    rows_prev = int((df_farm["MES"].astype(str) == meses[-2]).sum())
    if rows_prev > 0 and rows_last < rows_prev * 0.5:
        return meses[-2], meses[-1], {"rows_actual": rows_last, "rows_completo": rows_prev}
    return last, None, {}


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

        # PDVs con venta acumulados en el mes EN CURSO (sin proyectar — un PDV
        # puede comprar varios días, así que extrapolar PDVs únicos linealmente
        # da números mayores al universo). Mostramos el conteo real y un % de
        # avance contra el mes anterior completo.
        idn = r.get("IDNEPTUNO", "")
        pdv_actual_acum = int(pdv_venta_actual.get(idn, 0))
        if pdv_v_ult > 0:
            avance_vs_mes_ant_pct = round(pdv_actual_acum / pdv_v_ult * 100, 1)
        else:
            avance_vs_mes_ant_pct = None

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
            "avance_vs_mes_ant_pct": avance_vs_mes_ant_pct,
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


# ── 2) Aceleradores Sell-Out (zona %Pon 60-79%) ──────────────────
# Lógica de negocio (input KAM):
#   ≥ 80%  → cliente aprueba ampliar vectorización (sección B)
#   60-79% → "casi ahí" — push sell-out para cruzar al 80%
#   < 60%  → ALERTA: producto no rota, riesgo de inventario muerto

def _enriquecer(r: dict) -> dict:
    """Añade ponderada_pct, cobertura_pct, doi_distrib%, presencia stock."""
    venta = float(r.get("VENTA", 0) or 0)
    presencia = int(r.get("PDV_PRESENCIA", 0) or 0)
    universo = int(r.get("UNIVERSO_PDV", 0) or 0) or 1
    pdv_v_ult = int(r.get("PDV_VENTA_ULT_MES", 0) or 0)
    cob = round(presencia / universo * 100, 1)
    pon = round(pdv_v_ult / presencia * 100, 1) if presencia > 0 else 0.0
    # DOI distribution as % of presencia
    le20 = int(r.get("DOI_LE20", 0) or 0)
    d20_30 = int(r.get("DOI_20_30", 0) or 0)
    d30_60 = int(r.get("DOI_30_60", 0) or 0)
    gt60 = int(r.get("DOI_GT60", 0) or 0)
    stock_0 = int(r.get("STOCK_0", 0) or 0)
    return {
        **r,
        "venta": venta,
        "presencia": presencia,
        "universo": universo,
        "cobertura_pct": cob,
        "ponderada_pct": pon,
        "pdv_venta_ult_mes": pdv_v_ult,
        "doi_le20": le20,
        "doi_20_30": d20_30,
        "doi_30_60": d30_60,
        "doi_gt60": gt60,
        "stock_eq0": stock_0,
    }


def _regla_acelerador(item: dict) -> dict:
    """Regla determinista para sugerir acción de sell-out (zona 60-79%)."""
    presencia = max(item["presencia"], 1)
    pct_gt60 = item["doi_gt60"] / presencia * 100
    pct_30_60 = item["doi_30_60"] / presencia * 100
    pct_le20 = item["doi_le20"] / presencia * 100
    pct_stock0 = item["stock_eq0"] / presencia * 100

    if pct_stock0 >= 25:
        return {
            "tipo": "REPOSICIÓN",
            "color": "#ef4444",
            "icono": "📦",
            "accion": f"Reposición urgente — {item['stock_eq0']} PDVs en quiebre ({round(pct_stock0)}% de la presencia).",
        }
    if pct_gt60 >= 30:
        return {
            "tipo": "COMBO/DESCUENTO",
            "color": "#f59e0b",
            "icono": "🎁",
            "accion": f"Activar combo cruzado o descuento factura 10-15% — {item['doi_gt60']} PDVs con DOI >60d ({round(pct_gt60)}%).",
        }
    if pct_30_60 >= 30:
        return {
            "tipo": "PUSH PROMOTOR",
            "color": "#3b82f6",
            "icono": "👥",
            "accion": f"Push promotor en PDVs sin venta últimos 15 días — {item['doi_30_60']} PDVs con DOI 30-60d.",
        }
    if pct_le20 >= 50:
        return {
            "tipo": "MATERIAL POP",
            "color": "#10b981",
            "icono": "📌",
            "accion": f"Producto rotando bien — material POP + capacitación en top PDVs (DOI ≤20 en {round(pct_le20)}% de presencia).",
        }
    return {
        "tipo": "ACTIVAR PDVs",
        "color": "#8b5cf6",
        "icono": "🎯",
        "accion": "Activar mecánicas en PDVs sin rotación reciente para llevar %Pon a 80%.",
    }


def _regla_alerta(item: dict) -> dict:
    """Regla determinista para zona <60% Pon (riesgo inventario)."""
    presencia = max(item["presencia"], 1)
    pct_gt60 = item["doi_gt60"] / presencia * 100
    pct_30_60 = item["doi_30_60"] / presencia * 100
    if pct_gt60 >= 40:
        return {
            "tipo": "STOCK MUERTO",
            "color": "#dc2626",
            "icono": "🚨",
            "accion": f"Liquidación o reasignación urgente — {item['doi_gt60']} PDVs con DOI >60d ({round(pct_gt60)}%). Considerar mover stock a PDVs de marcas con %Pon alto.",
        }
    if presencia > item["pdv_venta_ult_mes"] * 2 and presencia >= 50:
        return {
            "tipo": "REDUCIR PRESENCIA",
            "color": "#f59e0b",
            "icono": "📉",
            "accion": f"Reducir presencia: solo {item['pdv_venta_ult_mes']}/{presencia} PDVs lo venden. El producto no acepta mercado en muchos PDVs.",
        }
    if pct_30_60 + pct_gt60 >= 50:
        return {
            "tipo": "REVISAR ROTACIÓN",
            "color": "#f59e0b",
            "icono": "⚠️",
            "accion": "Revisar rotación: más del 50% de PDVs tiene DOI >30d. Validar visibilidad y precio vs competencia.",
        }
    return {
        "tipo": "ANALIZAR CAUSA",
        "color": "#6b7280",
        "icono": "🔍",
        "accion": "Analizar causa: producto con baja %Pon pero sin patrón claro de stock. Revisar competencia, exhibición, lifecycle.",
    }


def aceleradores_sellout(min_pon: float = 60.0,
                          max_pon: float = 79.9,
                          top_n: int = 50) -> dict:
    """SKUs en zona %Pon `min_pon`-`max_pon` con sugerencia de acción de sell-out."""
    rows_raw = analitica.oportunidad_vectorizacion()
    if not rows_raw:
        return {"items": [], "resumen": {}}
    items = []
    for r in rows_raw:
        e = _enriquecer(r)
        if e["presencia"] <= 0:
            continue
        if not (min_pon <= e["ponderada_pct"] <= max_pon):
            continue
        regla = _regla_acelerador(e)
        items.append({
            "IDNEPTUNO": e.get("IDNEPTUNO", ""),
            "MARCA": e.get("MARCA", ""),
            "PRODUCTO": e.get("PRODUCTO", ""),
            "VENTA": round(e["venta"], 0),
            "UNIVERSO_PDV": e["universo"],
            "PDV_PRESENCIA": e["presencia"],
            "PDV_VENTA_ULT_MES": e["pdv_venta_ult_mes"],
            "cobertura_pct": e["cobertura_pct"],
            "ponderada_pct": e["ponderada_pct"],
            "DOI_GT60": e["doi_gt60"],
            "DOI_30_60": e["doi_30_60"],
            "STOCK_0": e["stock_eq0"],
            "regla": regla,
        })
    items.sort(key=lambda x: x["VENTA"], reverse=True)
    return {
        "items": items[:top_n],
        "resumen": {
            "total_skus": len(items),
            "rango_pon": [min_pon, max_pon],
        },
    }


def alerta_critica(max_pon: float = 60.0, top_n: int = 50) -> dict:
    """SKUs con %Pon < `max_pon` — riesgo de inventario muerto."""
    rows_raw = analitica.oportunidad_vectorizacion()
    if not rows_raw:
        return {"items": [], "resumen": {}}
    items = []
    for r in rows_raw:
        e = _enriquecer(r)
        if e["presencia"] <= 0:
            continue
        if e["ponderada_pct"] >= max_pon:
            continue
        regla = _regla_alerta(e)
        items.append({
            "IDNEPTUNO": e.get("IDNEPTUNO", ""),
            "MARCA": e.get("MARCA", ""),
            "PRODUCTO": e.get("PRODUCTO", ""),
            "VENTA": round(e["venta"], 0),
            "UNIVERSO_PDV": e["universo"],
            "PDV_PRESENCIA": e["presencia"],
            "PDV_VENTA_ULT_MES": e["pdv_venta_ult_mes"],
            "cobertura_pct": e["cobertura_pct"],
            "ponderada_pct": e["ponderada_pct"],
            "DOI_GT60": e["doi_gt60"],
            "regla": regla,
        })
    # Ordenar por venta (mayor venta = mayor exposición de inventario en riesgo)
    items.sort(key=lambda x: x["VENTA"], reverse=True)
    return {
        "items": items[:top_n],
        "resumen": {
            "total_skus": len(items),
            "umbral_pon": max_pon,
        },
    }


# ── 3) Foco de la Semana — síntesis priorizada ───────────────────

def foco_semana(top_n: int = 7) -> dict:
    """Top N acciones priorizadas — mezcla los 3 buckets en una vista única.

    Algoritmo de priorización (por categoría):
      AMPLIAR  (%Pon ≥ 80%) → ordenar por $ uplift potencial
      PUSH     (%Pon 60-79%) → ordenar por venta (mayor exposición)
      ALERTA   (%Pon < 60%)  → ordenar por venta × pct_doi_gt60 (mayor stock muerto)

    Mezcla intercalada: 3 ampliar + 2 push + 2 alerta (configurable por top_n).
    """
    vec = ampliar_vectorizacion(min_pon_pct=80.0, top_n=20)
    ace = aceleradores_sellout(min_pon=60.0, max_pon=79.9, top_n=20)
    ale = alerta_critica(max_pon=60.0, top_n=20)

    foco = []
    # 3 ampliar (verde)
    for i in (vec.get("items") or [])[:3]:
        foco.append({
            "categoria": "AMPLIAR",
            "color": "#10b981",
            "icono": "🚀",
            "MARCA": i["MARCA"],
            "PRODUCTO": i["PRODUCTO"],
            "metrica_clave": f"%Pon {i['ponderada_pct']}% · presencia {i['PDV_PRESENCIA']}/{i['UNIVERSO_PDV']}",
            "accion": f"Pedir ampliación a {i['UNIVERSO_PDV'] - i['PDV_PRESENCIA']} PDVs nuevos",
            "impacto_usd": i.get("UPLIFT", 0),
            "IDNEPTUNO": i.get("IDNEPTUNO", ""),
        })
    # 2 push sell-out (ámbar)
    for i in (ace.get("items") or [])[:2]:
        foco.append({
            "categoria": "PUSH SELL-OUT",
            "color": "#f59e0b",
            "icono": "⚡",
            "MARCA": i["MARCA"],
            "PRODUCTO": i["PRODUCTO"],
            "metrica_clave": f"%Pon {i['ponderada_pct']}% · faltan {round(80 - i['ponderada_pct'], 1)}pp para gatillar ampliación",
            "accion": i["regla"]["accion"],
            "impacto_usd": i.get("VENTA", 0),  # venta como proxy de relevancia
            "IDNEPTUNO": i.get("IDNEPTUNO", ""),
        })
    # 2 alertas (rojo)
    for i in (ale.get("items") or [])[:2]:
        foco.append({
            "categoria": "ALERTA",
            "color": "#ef4444",
            "icono": "🚨",
            "MARCA": i["MARCA"],
            "PRODUCTO": i["PRODUCTO"],
            "metrica_clave": f"%Pon {i['ponderada_pct']}% · {i['DOI_GT60']} PDVs con DOI >60d",
            "accion": i["regla"]["accion"],
            "impacto_usd": i.get("VENTA", 0),
            "IDNEPTUNO": i.get("IDNEPTUNO", ""),
        })

    return {
        "items": foco[:top_n],
        "resumen": {
            "total_ampliar": len(vec.get("items") or []),
            "total_push": len(ace.get("items") or []),
            "total_alerta": len(ale.get("items") or []),
        },
    }


# ── 4) Insight con IA (Claude) ───────────────────────────────────

def construir_prompt_insight(idneptuno) -> str:
    """Genera el prompt para Claude basado en el contexto del SKU."""
    rows = analitica.oportunidad_vectorizacion()
    sku = next((r for r in rows if str(r.get("IDNEPTUNO", "")) == str(idneptuno)), None)
    if not sku:
        return ""
    e = _enriquecer(sku)
    presencia = max(e["presencia"], 1)
    pct_gt60 = round(e["doi_gt60"] / presencia * 100, 1)
    pct_30_60 = round(e["doi_30_60"] / presencia * 100, 1)
    pct_le20 = round(e["doi_le20"] / presencia * 100, 1)
    pct_stock0 = round(e["stock_eq0"] / presencia * 100, 1)
    return f"""Eres consultor KAM senior para Genomma Lab Ecuador (cliente farmacéutico Difare).

Producto: {e.get('MARCA', '')} - {e.get('PRODUCTO', '')}
Venta acumulada Q1: ${round(e['venta']):,}
Universo PDV farmacias: {e['universo']}
Presencia actual: {e['presencia']} PDVs ({e['cobertura_pct']}% del universo)
%Ponderada (PDVs con venta último mes / presencia): {e['ponderada_pct']}%
PDVs con venta último mes completo: {e['pdv_venta_ult_mes']}

Distribución DOI sobre presencia:
  - Stock=0 (quiebre):     {e['stock_eq0']} PDVs ({pct_stock0}%)
  - DOI ≤20d (rotando):    {e['doi_le20']} PDVs ({pct_le20}%)
  - DOI 30-60d (lento):    {e['doi_30_60']} PDVs ({pct_30_60}%)
  - DOI >60d (stock alto): {e['doi_gt60']} PDVs ({pct_gt60}%)

Reglas de negocio:
- %Pon ≥ 80% → cliente aprueba ampliación de vectorización inmediata
- %Pon 60-79% → "casi ahí", debes empujar sell-out para cruzar al 80%
- %Pon < 60% → riesgo de inventario muerto, requiere aceleradores fuertes

Genera un plan de acción accionable de 3-5 puntos para esta semana, en español, conciso y ejecutivo. Cada punto debe ser concreto (ej: "activar promotor en X PDVs", "negociar combo Y con buyer Z"), con orden de prioridad y, si aplica, impacto estimado. NO uses jerga genérica. NO uses bullets > 2 niveles. Máximo 200 palabras."""


def insight_ia(idneptuno, anthropic_client) -> dict:
    """Llama a Claude con el contexto del SKU y devuelve el plan."""
    prompt = construir_prompt_insight(idneptuno)
    if not prompt:
        return {"error": "SKU no encontrado"}
    if not anthropic_client:
        return {"error": "Cliente Anthropic no configurado"}
    try:
        resp = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"recomendacion": resp.content[0].text.strip()}
    except Exception as e:
        return {"error": f"Error IA: {str(e)[:200]}"}
