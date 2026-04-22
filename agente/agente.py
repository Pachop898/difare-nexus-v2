import anthropic
import pandas as pd
import os
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SISTEMA = """
Eres un analista de negocios experto para empresas en Ecuador.
Analizas datos de ventas e inventario de DIFARE, el cliente mas grande de Genommalab Ecuador.
DIFARE tiene 3 unidades en los datos:
- DIFARE S.A.: bodega central (solo tiene STOCK, no vende directamente)
- FARMACIAS: farmacias propias de DIFARE (segmentadas por GRUPOPDV y POS)
- DISTRIBUCION DIFARE: canal de distribucion a clientes externos (segmentado por GRUPOCLIENTE y PROPIETARIO)
Genera reportes ejecutivos en espanol, con numeros reales, insights claros y recomendaciones accionables.
"""

TOOLS = [
    {"name": "leer_datos", "description": "Lee y combina todos los Excel del periodo",
     "input_schema": {"type": "object", "properties": {"carpeta": {"type": "string"}}, "required": ["carpeta"]}},
    {"name": "resumen_general", "description": "Resumen ejecutivo de ventas e inventario de ambas unidades",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "analisis_farmacias_propias", "description": "Analisis de farmacias propias DIFARE por GRUPOPDV y POS",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "analisis_distribucion", "description": "Analisis del canal distribucion por GRUPOCLIENTE y PROPIETARIO",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "analisis_stock_bodega", "description": "Analisis del stock en bodega DIFARE S.A. vs rotacion",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "top_productos", "description": "Top productos por ventas y rotacion",
     "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": []}},
    {"name": "tendencia_mensual", "description": "Tendencia de ventas por mes y unidad de negocio",
     "input_schema": {"type": "object", "properties": {}, "required": []}}
]

_df = None

def parsear_mes(x):
    x = str(x).strip()
    if "/" in x:
        r = pd.to_datetime(x, format="%Y/%m/%d", errors="coerce")
    else:
        r = pd.to_datetime(x, format="%Y%m%d", errors="coerce")
    if pd.isna(r):
        return "desconocido"
    return r.to_period("M").strftime("%Y-%m")

def ejecutar_tool(nombre, params):
    global _df

    if nombre == "leer_datos":
        import glob
        carpeta = params.get("carpeta", "excels")
        archivos = glob.glob(f"{carpeta}/*.xlsx") + glob.glob(f"{carpeta}/*.xls")
        dfs = []
        for a in archivos:
            try:
                df = pd.read_excel(a)
                if "FECHA" in df.columns and "DIA" not in df.columns:
                    df["MES"] = df["FECHA"].astype(str).apply(
                        lambda x: x[:4] + "-" + x[4:6] if len(str(x)) == 6 else "desconocido"
                    )
                elif "DIA" in df.columns:
                    df["MES"] = df["DIA"].apply(parsear_mes)
                dfs.append(df)
                print(f"    Cargado: {a} ({len(df)} filas)")
            except Exception as e:
                print(f"    Error: {a}: {e}")
        _df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
        return {
            "archivos_cargados": len(dfs),
            "total_filas": len(_df),
            "unidades": _df["UNIDAD"].value_counts().to_dict(),
            "meses": sorted(_df["MES"].dropna().unique().tolist()) if "MES" in _df.columns else []
        }

    elif nombre == "resumen_general":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_ventas = _df[_df["UNIDAD"] != "DIFARE S.A."]
        df_bodega = _df[_df["UNIDAD"] == "DIFARE S.A."]
        return {
            "venta_total": float(df_ventas["VENTA NETA RECUPERO"].sum()),
            "unidades_rotadas": float(df_ventas["UNIDADES_ROTADAS"].sum()),
            "stock_bodega": float(df_bodega["STOCK"].sum()),
            "ventas_por_unidad": df_ventas.groupby("UNIDAD")["VENTA NETA RECUPERO"].sum().to_dict(),
            "top_marcas": df_ventas.groupby("MARCA")["VENTA NETA RECUPERO"].sum().nlargest(10).to_dict(),
            "top_provincias": df_ventas.groupby("PROVINCIA")["VENTA NETA RECUPERO"].sum().nlargest(8).to_dict()
        }

    elif nombre == "analisis_farmacias_propias":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_farm = _df[_df["UNIDAD"] == "FARMACIAS"].copy()
        por_grupo = df_farm.groupby("GRUPOPDV").agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum"),
            pos_count=("POS", "nunique")
        ).reset_index().sort_values("ventas", ascending=False)
        por_pos = df_farm.groupby("POS").agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum"),
            grupo=("GRUPOPDV", "first")
        ).reset_index().nlargest(20, "ventas")
        bottom_pos = df_farm.groupby("POS").agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum"),
            grupo=("GRUPOPDV", "first")
        ).reset_index().nsmallest(15, "ventas")
        return {
            "total_pos": df_farm["POS"].nunique(),
            "venta_total_farmacias": float(df_farm["VENTA NETA RECUPERO"].sum()),
            "por_grupo_pdv": por_grupo.to_dict("records"),
            "top_20_farmacias": por_pos.to_dict("records"),
            "farmacias_menor_venta": bottom_pos.to_dict("records")
        }

    elif nombre == "analisis_distribucion":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_dist = _df[_df["UNIDAD"] == "DISTRIBUCION DIFARE"].copy()
        por_grupo = df_dist.groupby("GRUPOCLIENTE").agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum"),
            clientes=("PROPIETARIO", "nunique")
        ).reset_index().sort_values("ventas", ascending=False)
        top_clientes = df_dist.groupby(["PROPIETARIO", "GRUPOCLIENTE"]).agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum")
        ).reset_index().nlargest(20, "ventas")
        return {
            "total_clientes": df_dist["PROPIETARIO"].nunique(),
            "venta_total_distribucion": float(df_dist["VENTA NETA RECUPERO"].sum()),
            "por_grupo_cliente": por_grupo.to_dict("records"),
            "top_20_clientes": top_clientes.to_dict("records")
        }

    elif nombre == "analisis_stock_bodega":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_bodega = _df[_df["UNIDAD"] == "DIFARE S.A."].copy()
        df_ventas = _df[_df["UNIDAD"] != "DIFARE S.A."].copy()
        stock_prod = df_bodega.groupby(["MARCA", "PRODUCTO"]).agg(
            stock=("STOCK", "sum"),
            stock_val=("STOCK_VALORIZADO", "sum")
        ).reset_index()
        rotacion = df_ventas.groupby(["MARCA", "PRODUCTO"]).agg(
            unidades_vendidas=("UNIDADES_ROTADAS", "sum"),
            venta_neta=("VENTA NETA RECUPERO", "sum")
        ).reset_index()
        analisis = stock_prod.merge(rotacion, on=["MARCA", "PRODUCTO"], how="left")
        analisis["rotacion_diaria"] = analisis["unidades_vendidas"].fillna(0) / 90
        analisis["dias_cobertura"] = analisis.apply(
            lambda r: round(r["stock"] / r["rotacion_diaria"]) if r["rotacion_diaria"] > 0 else 999, axis=1
        )
        analisis["alerta"] = analisis["dias_cobertura"].apply(
            lambda d: "CRITICO" if d < 15 else ("BAJO" if d < 30 else "OK")
        )
        criticos = analisis[analisis["alerta"] == "CRITICO"].nlargest(15, "venta_neta")
        bajos = analisis[analisis["alerta"] == "BAJO"].nlargest(15, "venta_neta")
        return {
            "stock_total_valorizado": float(df_bodega["STOCK_VALORIZADO"].sum()),
            "productos_criticos": criticos[["MARCA", "PRODUCTO", "stock", "rotacion_diaria", "dias_cobertura", "venta_neta"]].to_dict("records"),
            "productos_bajo_stock": bajos[["MARCA", "PRODUCTO", "stock", "rotacion_diaria", "dias_cobertura", "venta_neta"]].to_dict("records"),
            "resumen_alertas": analisis["alerta"].value_counts().to_dict()
        }

    elif nombre == "top_productos":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_ventas = _df[_df["UNIDAD"] != "DIFARE S.A."]
        n = params.get("n", 15)
        top = df_ventas.groupby(["MARCA", "PRODUCTO"]).agg(
            unidades=("UNIDADES_ROTADAS", "sum"),
            venta=("VENTA NETA RECUPERO", "sum")
        ).reset_index().nlargest(n, "venta").to_dict("records")
        return {"top_productos": top}

    elif nombre == "tendencia_mensual":
        if _df is None or _df.empty:
            return {"error": "No hay datos"}
        df_ventas = _df[_df["UNIDAD"] != "DIFARE S.A."]
        tend = df_ventas.groupby(["MES", "UNIDAD"]).agg(
            ventas=("VENTA NETA RECUPERO", "sum"),
            unidades=("UNIDADES_ROTADAS", "sum")
        ).reset_index().sort_values(["MES", "UNIDAD"])
        marzo_real = df_ventas[df_ventas["MES"] == "2026-03"]["VENTA NETA RECUPERO"].sum()
        return {
            "tendencia": tend.to_dict("records"),
            "proyeccion_marzo": round(marzo_real * (31/22), 2),
            "marzo_real": round(marzo_real, 2)
        }

def analizar(carpeta="excels"):
    print(f"Analizando datos de: {carpeta}")
    mensajes = [{"role": "user", "content": f"""
Analiza todos los archivos Excel en '{carpeta}' del cliente DIFARE Ecuador.
Recuerda la estructura:
- DIFARE S.A. = bodega central (stock unicamente)
- FARMACIAS = farmacias propias (analizar por GRUPOPDV y POS)
- DISTRIBUCION DIFARE = canal distribucion (analizar por GRUPOCLIENTE y PROPIETARIO)
Ejecuta todos los analisis disponibles y genera un reporte ejecutivo completo.
"""}]

    while True:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=SISTEMA,
            tools=TOOLS,
            messages=mensajes
        )
        if resp.stop_reason == "end_turn":
            return "".join(b.text for b in resp.content if b.type == "text")
        mensajes.append({"role": "assistant", "content": resp.content})
        resultados = []
        for bloque in resp.content:
            if bloque.type == "tool_use":
                print(f"  Usando: {bloque.name}...")
                r = ejecutar_tool(bloque.name, bloque.input)
                resultados.append({"type": "tool_result", "tool_use_id": bloque.id, "content": str(r)})
        if resultados:
            mensajes.append({"role": "user", "content": resultados})
        else:
            break
