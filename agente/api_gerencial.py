"""
agente/api_gerencial.py — Blueprint Flask con los endpoints del Dashboard Gerencial
====================================================================================
Endpoints expuestos (todos requieren JWT con rol 'admin' o 'gerencial'):

  GET  /api/kpis                → KPIs cabecera del dashboard
  GET  /api/tendencia-marca     → ?un=FARMACIAS|DIFARE&yoy=0|1
  GET  /api/venta-canal-mes     → Barras agrupadas por canal/mes
  GET  /api/ranking-pdv         → ?canal=FARMACIAS|DIFARE S.A.&top=50
  GET  /api/pareto-pdv          → Top farmacias que acumulan 80% de venta
  POST /api/chat-gerencial      → Chat con function calling (3 tools)

Diseñado para integrarse con app.py SIN modificarlo más allá del
register_blueprint en una línea. La verificación JWT reusa la función
`verificar_jwt` del módulo padre vía late-binding (set_jwt_verifier).
"""

from __future__ import annotations
import os, json, traceback
from flask import Blueprint, request, jsonify, send_file
from . import analitica

# ── Cliente Anthropic (singleton propio del blueprint) ──
_anthropic_client = None
_anthropic_client_fn = None  # fallback: función inyectada desde app.py

def set_anthropic_client(fn):
    """app.py pasa get_anthropic_client() como fallback."""
    global _anthropic_client_fn
    _anthropic_client_fn = fn

def _get_client():
    """Obtiene el cliente Anthropic, probando múltiples vías."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    # 1) Intentar via función inyectada desde app.py
    if _anthropic_client_fn:
        try:
            c = _anthropic_client_fn()
            if c:
                _anthropic_client = c
                return c
        except Exception:
            pass
    # 2) Crear directamente (SDK auto-detecta ANTHROPIC_API_KEY del env)
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        _anthropic_client = anthropic.Anthropic(api_key=key)
    else:
        _anthropic_client = anthropic.Anthropic()  # intenta default
    return _anthropic_client

bp = Blueprint("gerencial", __name__, url_prefix="/api")

# Late-binding del verificador JWT y del mapa de roles ────────────────────
_jwt_verifier = None
_roles = {}  # { "francisco": "admin", "Gerente": "gerencial", "Campo": "campo" }

def set_jwt_verifier(fn, roles_map: dict):
    """app.py llama esto al registrar el blueprint."""
    global _jwt_verifier, _roles
    _jwt_verifier = fn
    _roles = roles_map


def _autorizar(roles_permitidos=("admin", "gerencial")):
    """Devuelve (usuario, rol) si OK; o (None, error_response) si no."""
    if _jwt_verifier is None:
        return None, (jsonify({"error": "JWT no configurado"}), 500)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.args.get("token", "")
    usuario = _jwt_verifier(token)
    if not usuario:
        return None, (jsonify({"error": "No autorizado"}), 401)
    rol = _roles.get(usuario, "campo")
    if rol not in roles_permitidos:
        return None, (jsonify({"error": f"Rol '{rol}' no autorizado"}), 403)
    return (usuario, rol), None


# ══════════════════════════════════════════════════════════════
# 1) KPIs cabecera del dashboard
# ══════════════════════════════════════════════════════════════

@bp.route("/kpis", methods=["GET"])
def kpis():
    auth, err = _autorizar()
    if err: return err
    try:
        return jsonify(analitica.kpis_dashboard()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 2) Tendencia por marca (pregunta KAM #1)
# ══════════════════════════════════════════════════════════════

@bp.route("/tendencia-marca", methods=["GET"])
def tendencia_marca():
    auth, err = _autorizar()
    if err: return err
    un_param = request.args.get("un", "").strip().upper()
    un_map = {"FARMACIAS": "FARMACIAS",
              "DIFARE": "DISTRIBUCION DIFARE",
              "DISTRIBUCION": "DISTRIBUCION DIFARE",
              "": None, "TOTAL": None}
    unidad = un_map.get(un_param, None)
    yoy = request.args.get("yoy", "0") in ("1", "true", "True")
    try:
        data = analitica.tendencia_marca(unidad_negocio=unidad, comparar_yoy=yoy)
        return jsonify({
            "unidad_negocio": unidad or "TOTAL",
            "yoy": yoy,
            "filas": data,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 2b) Venta por canal por mes (barras agrupadas dashboard)
# ══════════════════════════════════════════════════════════════

@bp.route("/venta-canal-mes", methods=["GET"])
def venta_canal_mes():
    auth, err = _autorizar()
    if err: return err
    try:
        return jsonify({"filas": analitica.venta_por_canal_mes()}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 3) Ranking PDV (pregunta KAM #4)
# ══════════════════════════════════════════════════════════════

@bp.route("/ranking-pdv", methods=["GET"])
def ranking_pdv():
    auth, err = _autorizar()
    if err: return err
    canal = request.args.get("canal", "FARMACIAS").strip().upper()
    canal_map = {"FARMACIAS": "FARMACIAS",
                 "DIFARE": "DISTRIBUCION DIFARE",
                 "DISTRIBUCION": "DISTRIBUCION DIFARE"}
    canal_real = canal_map.get(canal, "FARMACIAS")
    try:
        top = int(request.args.get("top", "50"))
    except ValueError:
        top = 50
    try:
        data = analitica.ranking_pdv(canal=canal_real, top_n=top)
        return jsonify({
            "canal": canal_real,
            "top_n": top,
            "filas": data,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 4) Pareto PDV (preview)
# ══════════════════════════════════════════════════════════════

@bp.route("/pareto-pdv", methods=["GET"])
def pareto_pdv():
    auth, err = _autorizar()
    if err: return err
    try:
        data = analitica.pareto_pdv()
        return jsonify({
            "total_pdv_pareto": len(data),
            "filas": data,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# 5) Tienda Perfecta — Pareto con buckets exclusivos de stock
# ══════════════════════════════════════════════════════════════

@bp.route("/tienda-perfecta", methods=["GET"])
def tienda_perfecta():
    auth, err = _autorizar()
    if err: return err
    try:
        rows = analitica.oportunidad_vectorizacion(top_n=50)
        # Calcular buckets EXCLUSIVOS:
        # stock_only_0 = PDV con stock exactamente 0 (no aparecen en último día)
        # stock_only_1 = PDV con stock=1 (están en <=1 pero NO en =0)
        # stock_only_2 = PDV con stock=2 (están en <=2 pero NO en <=1)
        # stock_only_3 = PDV con stock=3 (están en <=3 pero NO en <=2)
        for r in rows:
            s0 = r.get("STOCK_0", 0) or 0
            s1 = r.get("STOCK_1", 0) or 0
            s2 = r.get("STOCK_2", 0) or 0
            s3 = r.get("STOCK_3", 0) or 0
            r["stock_solo_0"] = s0
            r["stock_solo_1"] = max(s1 - s0, 0)
            r["stock_solo_2"] = max(s2 - s1, 0)
            r["stock_solo_3"] = max(s3 - s2, 0)
            uni = r.get("UNIVERSO_PDV", 0) or 1
            pres = r.get("PDV_PRESENCIA", 0) or 0
            r["cobertura_pct"] = round(pres / uni * 100, 1)
        return jsonify({"filas": rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Tienda Perfecta — descarga directa Excel vectorización ──

@bp.route("/tienda-perfecta-excel", methods=["GET"])
def tienda_perfecta_excel():
    auth, err = _autorizar()
    if err: return err
    try:
        import tempfile
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        fname = f"vectorizacion_completo_{ts}.xlsx"
        ruta = os.path.join(tempfile.gettempdir(), fname)
        analitica.exportar_vectorizacion_excel(producto="", ruta_salida=ruta)
        if not os.path.exists(ruta):
            return jsonify({"error": "No se pudo generar el archivo"}), 500
        return send_file(ruta, as_attachment=True, download_name=fname)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 500


# ══════════════════════════════════════════════════════════════
# 6) Distribución Numérica — clientes por mes con filtro marca
# ══════════════════════════════════════════════════════════════

@bp.route("/dist-numerica-chart", methods=["GET"])
def dist_numerica_chart():
    auth, err = _autorizar()
    if err: return err
    marca = request.args.get("marca", "").strip() or None
    try:
        data = analitica.distribucion_numerica(marca=marca, top_n=0)
        # También devolver lista de marcas disponibles para el filtro
        d = analitica.cargar_data()
        df = d["df_todos"]
        dist = df[df["UNIDAD"] == "DISTRIBUCION DIFARE"]
        marcas = sorted(dist["MARCA"].dropna().unique().tolist()) if not dist.empty else []
        return jsonify({
            "resumen_meses": data.get("resumen_meses", []),
            "total_clientes": data.get("total_clientes_historico", 0),
            "marcas_disponibles": marcas,
            "marca_filtro": marca,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
# Util: invalidar cache (admin only)
# ══════════════════════════════════════════════════════════════

@bp.route("/recargar-data", methods=["POST"])
def recargar_data():
    auth, err = _autorizar(roles_permitidos=("admin",))
    if err: return err
    analitica.invalidar_cache()
    return jsonify({"ok": True, "mensaje": "Cache invalidado, próxima consulta recargará desde excels/"}), 200


# ══════════════════════════════════════════════════════════════
# CHAT GERENCIAL — Claude con function calling
# ══════════════════════════════════════════════════════════════

_SYSTEM_GERENCIAL = """Eres el asistente analítico Difare Nexus para el equipo gerencial de Genommalab Ecuador.
Tu rol es responder preguntas estratégicas sobre ventas, inventario, tendencias de marca y
rendimiento de puntos de venta (PDV) usando las herramientas disponibles.

REGLAS:
- Siempre usa las herramientas (tools) para obtener datos REALES antes de responder.
- Responde en español ecuatoriano, tono profesional y conciso.
- Formatea montos en USD con separador de miles y 2 decimales: $1.234,56
- Cuando muestres tablas, usa formato markdown con | columna | columna |.
- Si el usuario pide exportar a Excel, usa la herramienta exportar_excel.
- Incluye insights accionables: no solo datos, también recomendaciones.
- Máximo 400 palabras por respuesta.

CONTEXTO DE NEGOCIO:
- Canales: FARMACIAS (sell-out en PDV propios de DIFARE), DISTRIBUCION DIFARE (sell-in a clientes externos)
- DIFARE S.A. = bodega central (solo stock, no venta directa)
- Parámetros de inventario: lead_time=2 días, buffer=8 días, seguridad=10 días
- Meses disponibles: Ene-Mar 2026 (cerrados) + Abr 2026 (parcial, data semanal SAP)

VOCABULARIO CLAVE DEL USUARIO:
- "Vectorización" u "Oportunidades de vectorización" = se refiere SIEMPRE a FARMACIAS PROPIAS.
  Significa identificar qué productos Pareto (80% de la venta) faltan en qué PDV (farmacias) y
  deberían estar presentes. Usa la herramienta oportunidad_vectorizacion para obtener el análisis
  Pareto con cobertura, presencia y stock por PDV. Muestra: producto, venta, cobertura actual,
  PDVs sin stock, y recomienda cuáles priorizar.

- "Distribución numérica" = se refiere al CANAL DISTRIBUTIVO (DISTRIBUCION DIFARE).
  Significa analizar cuántos clientes (por RUC) se han atendido, cuántos son nuevos vs perdidos
  mes a mes, y si cada cliente está comprando el portafolio TOP completo.
  Si el usuario pregunta por distribución numérica, primero PREGUNTA si se refiere al canal
  distributivo o a farmacias. Si confirma distributivo, usa la herramienta distribucion_numerica.
  Lo mínimo esperado es que cada cliente con RUC compre el portafolio TOP cada mes.
"""

_TOOLS_GERENCIAL = [
    {
        "name": "tendencia_marca",
        "description": "Obtiene la venta mensual desglosada por MARCA. Útil para ver evolución de cada marca mes a mes, detectar caídas o crecimientos. Puedes filtrar por canal (farmacias o distribución).",
        "input_schema": {
            "type": "object",
            "properties": {
                "canal": {
                    "type": "string",
                    "description": "Canal de venta: 'FARMACIAS', 'DISTRIBUCION DIFARE', o null para total.",
                    "enum": ["FARMACIAS", "DISTRIBUCION DIFARE", None]
                }
            },
            "required": []
        }
    },
    {
        "name": "dias_inventario",
        "description": "Calcula los días de inventario actuales: stock total (bodega + PDV) dividido entre venta diaria promedio. Indica si hay riesgo de desabasto (< 10 días = peligro). Puede filtrar por producto específico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre o código del producto a consultar. Dejar vacío para el total de todos los productos."
                }
            },
            "required": []
        }
    },
    {
        "name": "pareto_pdv",
        "description": "Lista las farmacias que concentran el 80% de la venta total (análisis Pareto/80-20). Muestra cada farmacia con su venta y porcentaje acumulado. Útil para priorizar visitas y negociaciones.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "ranking_pdv",
        "description": "Ranking de los mejores PDV o clientes por volumen de venta. Para farmacias muestra el Top N de PDV individuales. Para distribución muestra los clientes más grandes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "canal": {
                    "type": "string",
                    "description": "Canal: 'FARMACIAS' o 'DISTRIBUCION DIFARE'.",
                    "enum": ["FARMACIAS", "DISTRIBUCION DIFARE"]
                },
                "top_n": {
                    "type": "integer",
                    "description": "Cantidad de resultados. Default: 20.",
                    "default": 20
                }
            },
            "required": []
        }
    },
    {
        "name": "kpis_resumen",
        "description": "Obtiene los KPIs principales: venta total, venta farmacias, venta distribución, universo de PDV, día de data, y stock valorizado por mes. Útil para contexto general antes de profundizar.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "venta_canal_mes",
        "description": "Venta mensual desglosada por canal (farmacias vs distribución) con proyección del mes en curso. Muestra barras por mes con el total de cada canal.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "exportar_excel",
        "description": "Genera el Informe de Vectorización semanal en Excel (.xlsx) para FARMACIAS. Contiene una pestaña por marca con los productos Pareto (80% de la venta). Cada fila es un PDV que necesita stock: sin vectorizar, stock=0, stock bajo. Incluye columna SUGERIDO con unidades mínimas a enviar. Usa esta herramienta cuando el usuario pide 'reporte de vectorización', 'informe semanal', 'exportar vectorización', o 'generar Excel para el cliente'. Si se pasa un producto específico, solo incluye ese producto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Producto o marca específica para filtrar (ej: 'NIKZON', 'SUEROX'). Dejar vacío para generar el informe completo con TODAS las marcas Pareto."
                }
            },
            "required": []
        }
    },
    {
        "name": "oportunidad_vectorizacion",
        "description": "Análisis de oportunidades de VECTORIZACIÓN en FARMACIAS propias. Devuelve los productos Pareto (80% de la venta) con: cobertura actual (% de PDVs que lo tienen), PDVs con stock=0, y presencia. Útil para identificar dónde faltan productos estrella y priorizar envíos. Usa esta herramienta cuando el usuario pregunta por 'vectorización', 'oportunidades', 'cobertura de productos', o 'qué falta en las farmacias'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre del producto para filtrar. Dejar vacío para ver todos los productos Pareto."
                },
                "top_n": {
                    "type": "integer",
                    "description": "Cantidad de productos a mostrar. Default: 20.",
                    "default": 20
                }
            },
            "required": []
        }
    },
    {
        "name": "distribucion_numerica",
        "description": "Análisis de distribución numérica del canal DISTRIBUTIVO (DISTRIBUCION DIFARE): clientes atendidos por RUC mes a mes, clientes nuevos vs perdidos, y penetración del portafolio TOP. Usa esta herramienta cuando el usuario pregunta por 'distribución numérica', 'clientes atendidos', 'cuántos RUCs compraron', o 'penetración del portafolio'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "marca": {
                    "type": "string",
                    "description": "Marca para filtrar (ej: 'CICATRICURE'). Dejar vacío para analizar todas las marcas."
                },
                "top_n": {
                    "type": "integer",
                    "description": "Cantidad de clientes a mostrar en penetración. Default: 20.",
                    "default": 20
                }
            },
            "required": []
        }
    },
]


def _ejecutar_tool(name: str, inp: dict) -> str:
    """Ejecuta una tool y devuelve resultado como string JSON."""
    try:
        if name == "tendencia_marca":
            data = analitica.tendencia_marca(
                unidad_negocio=inp.get("canal"),
                comparar_yoy=False
            )
            # Agrupar por marca para que sea más legible
            marcas = {}
            for r in data:
                m = r.get("marca", "?")
                marcas.setdefault(m, []).append(r)
            return json.dumps({"total_registros": len(data), "por_marca": marcas}, default=str, ensure_ascii=False)

        elif name == "dias_inventario":
            data = analitica.dias_inventario(producto=inp.get("producto") or None)
            return json.dumps(data, default=str, ensure_ascii=False)

        elif name == "pareto_pdv":
            data = analitica.pareto_pdv()
            return json.dumps({"total_pdv_80pct": len(data), "top_20": data[:20]}, default=str, ensure_ascii=False)

        elif name == "ranking_pdv":
            canal = inp.get("canal", "FARMACIAS")
            top_n = inp.get("top_n", 20)
            data = analitica.ranking_pdv(canal=canal, top_n=top_n)
            return json.dumps({"canal": canal, "filas": data}, default=str, ensure_ascii=False)

        elif name == "kpis_resumen":
            data = analitica.kpis_dashboard()
            return json.dumps(data, default=str, ensure_ascii=False)

        elif name == "venta_canal_mes":
            data = analitica.venta_por_canal_mes()
            return json.dumps({"filas": data}, default=str, ensure_ascii=False)

        elif name == "exportar_excel":
            producto = inp.get("producto", "")
            import tempfile
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            fname = f"vectorizacion_{producto.replace(' ','_') if producto else 'completo'}_{ts}.xlsx"
            ruta = os.path.join(tempfile.gettempdir(), fname)
            analitica.exportar_vectorizacion_excel(producto=producto, ruta_salida=ruta)
            return json.dumps({"ok": True, "ruta": ruta, "producto": producto or "TODAS LAS MARCAS",
                               "archivo": fname})

        elif name == "oportunidad_vectorizacion":
            data = analitica.oportunidad_vectorizacion(
                producto=inp.get("producto") or None,
                top_n=inp.get("top_n", 20)
            )
            # Enriquecer con % cobertura para que Claude lo presente mejor
            for r in data:
                uni = r.get("UNIVERSO_PDV", 0) or 1
                pres = r.get("PDV_PRESENCIA", 0)
                r["cobertura_pct"] = round(pres / uni * 100, 1)
                r["pdv_sin_stock"] = r.get("STOCK_0", 0)
                r["pdv_stock_critico"] = r.get("STOCK_1", 0)
            return json.dumps({
                "total_productos_pareto": len(data),
                "productos": data
            }, default=str, ensure_ascii=False)

        elif name == "distribucion_numerica":
            data = analitica.distribucion_numerica(
                marca=inp.get("marca") or None,
                top_n=inp.get("top_n", 20)
            )
            return json.dumps(data, default=str, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Tool desconocida: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)[:200]})


@bp.route("/chat-gerencial", methods=["POST", "OPTIONS"])
def chat_gerencial():
    if request.method == "OPTIONS":
        return "", 204
    auth, err = _autorizar()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    pregunta = body.get("pregunta", "").strip()
    historial = body.get("historial", [])  # [{role, content}, ...]
    if not pregunta:
        return jsonify({"error": "Falta 'pregunta'"}), 400

    try:
        client = _get_client()
        # Verificar que la key tenga formato correcto
        key_val = os.getenv("ANTHROPIC_API_KEY", "")
        if not key_val:
            return jsonify({"error": "ANTHROPIC_API_KEY no está configurada en las variables de entorno del servidor."}), 500
        if not key_val.startswith("sk-ant-"):
            return jsonify({"error": f"ANTHROPIC_API_KEY tiene formato incorrecto (empieza con '{key_val[:8]}...'). Debe empezar con 'sk-ant-'."}), 500
    except Exception as e:
        return jsonify({"error": f"API key no configurada: {e}"}), 500

    # Construir mensajes
    messages = []
    for h in historial[-10:]:  # últimos 10 turnos de contexto
        messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
    messages.append({"role": "user", "content": pregunta})

    # Agentic loop: Claude puede llamar tools múltiples veces
    file_downloads = []  # archivos generados para descarga
    max_iterations = 5
    try:
        for _ in range(max_iterations):
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=_SYSTEM_GERENCIAL,
                tools=_TOOLS_GERENCIAL,
                messages=messages,
            )
            # Procesar respuesta
            if resp.stop_reason == "tool_use":
                # Claude quiere llamar una o más tools
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result_str = _ejecutar_tool(block.name, block.input)
                        # Si fue exportar_excel, guardar ruta del archivo
                        if block.name == "exportar_excel":
                            try:
                                r = json.loads(result_str)
                                if r.get("ok") and r.get("ruta"):
                                    file_downloads.append(r["ruta"])
                            except Exception:
                                pass
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })
                # Agregar turno del asistente + resultados
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                # Respuesta final
                text_parts = [b.text for b in resp.content if hasattr(b, "text")]
                respuesta = "\n".join(text_parts)
                result = {
                    "respuesta": respuesta,
                    "archivos": [os.path.basename(f) for f in file_downloads],
                }
                return jsonify(result), 200

        # Si agotamos iteraciones, devolver lo que haya
        text_parts = [b.text for b in resp.content if hasattr(b, "text")]
        return jsonify({"respuesta": "\n".join(text_parts) or "Análisis completado.", "archivos": []}), 200

    except Exception as e:
        traceback.print_exc()
        err_msg = str(e)
        if "authentication_error" in err_msg or "invalid x-api-key" in err_msg:
            return jsonify({"error": "La API key de Anthropic es inválida. Ve a console.anthropic.com > API Keys, genera una nueva key que empiece con 'sk-ant-api03-...' y actualízala en Railway."}), 500
        return jsonify({"error": f"Error en análisis: {err_msg[:200]}"}), 500


# Endpoint para descargar archivos generados por el chat
@bp.route("/descargar/<filename>", methods=["GET"])
def descargar_archivo(filename):
    # Aceptar token en header O en query param (para links <a href>)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.args.get("token", "")
    if _jwt_verifier and token:
        usuario = _jwt_verifier(token)
        if not usuario:
            return jsonify({"error": "No autorizado"}), 401
    elif _jwt_verifier:
        return jsonify({"error": "No autorizado"}), 401
    import tempfile
    ruta = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.exists(ruta):
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_file(ruta, as_attachment=True, download_name=filename)
