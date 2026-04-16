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
- Canales: FARMACIAS (sell-out PDV), DISTRIBUCION DIFARE (sell-in distribuidores)
- DIFARE S.A. = bodega central (solo stock, no venta)
- Parámetros de inventario: lead_time=2 días, buffer=8 días, seguridad=10 días
- Meses disponibles: Ene-Mar 2026 (cerrados) + Abr 2026 (parcial, data semanal SAP)
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
        "description": "Genera un archivo Excel (.xlsx) con la vectorización de un producto específico: PDVs que NO lo tienen en stock y deberían tenerlo, con sugerido mínimo de unidades a enviar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {
                    "type": "string",
                    "description": "Nombre del producto a analizar (ej: 'CICATRICURE', 'TIO NACHO')."
                }
            },
            "required": ["producto"]
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
            if not producto:
                return json.dumps({"error": "Falta el nombre del producto"})
            import tempfile
            ruta = os.path.join(tempfile.gettempdir(), f"vectorizacion_{producto.replace(' ','_')}.xlsx")
            analitica.exportar_vectorizacion_excel(producto, ruta)
            return json.dumps({"ok": True, "ruta": ruta, "producto": producto})

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
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
                model="claude-sonnet-4-5-20241022",
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
        return jsonify({"error": f"Error en análisis: {str(e)[:200]}"}), 500


# Endpoint para descargar archivos generados por el chat
@bp.route("/descargar/<filename>", methods=["GET"])
def descargar_archivo(filename):
    auth, err = _autorizar()
    if err:
        return err
    import tempfile
    ruta = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.exists(ruta):
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_file(ruta, as_attachment=True, download_name=filename)
