"""
agente/api_gerencial.py — Blueprint Flask con los endpoints del Dashboard Gerencial
====================================================================================
Endpoints expuestos (todos requieren JWT con rol 'admin' o 'gerencial'):

  GET /api/kpis                 → KPIs cabecera del dashboard
  GET /api/tendencia-marca      → ?un=FARMACIAS|DIFARE&yoy=0|1
  GET /api/ranking-pdv          → ?canal=FARMACIAS|DIFARE S.A.&top=50
  GET /api/pareto-pdv           → Top farmacias que acumulan 80% de venta

Diseñado para integrarse con app.py SIN modificarlo más allá del
register_blueprint en una línea. La verificación JWT reusa la función
`verificar_jwt` del módulo padre vía late-binding (set_jwt_verifier).
"""

from __future__ import annotations
from flask import Blueprint, request, jsonify
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
