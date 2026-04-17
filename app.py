"""
DIFARE NEXUS API v3 — Unificado
Backend + Frontend servido desde Flask
SQLite · JWT Auth · Vercel-ready
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import anthropic
import sqlite3
import os
import time
import hashlib
import hmac
import json
import base64
from urllib.parse import unquote
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"]}})

# Lazy-init del cliente Anthropic para evitar crash al importar en Vercel
_client = None
def get_anthropic_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client

# ── CONFIG ──
JWT_SECRET = os.getenv("JWT_SECRET", "difare-nexus-secret-cambiar-en-produccion")
JWT_EXPIRY = 86400
# data.db: buscar en varias ubicaciones (Railway, Vercel, local)
_BASE = os.path.dirname(os.path.abspath(__file__))
_DB_CANDIDATES = [
    os.path.join(_BASE, "data.db"),                # junto a app.py
    os.path.join(_BASE, "api", "data.db"),         # en api/ (estructura Vercel)
    os.path.join(os.path.dirname(_BASE), "data.db"),  # un nivel arriba
]
DB_PATH = next((p for p in _DB_CANDIDATES if os.path.exists(p)), _DB_CANDIDATES[0])
print(f"[v2] DB_PATH = {DB_PATH} (exists={os.path.exists(DB_PATH)})")


def _excels_mas_nuevos_que_db() -> bool:
    """True si algún .xlsx en excels/ es más nuevo que data.db (o data.db no existe)."""
    try:
        from glob import glob
        excels_dir = os.path.join(_BASE, "excels")
        if not os.path.isdir(excels_dir):
            return False
        xls = glob(os.path.join(excels_dir, "*.xlsx"))
        if not xls:
            return False
        if not os.path.exists(DB_PATH):
            return True
        db_mtime = os.path.getmtime(DB_PATH)
        return any(os.path.getmtime(f) > db_mtime for f in xls)
    except Exception as e:
        print(f"[v2] check excels mtime falló: {e}")
        return False


def _regenerar_data_db():
    """Corre el ETL actualizar_data.py para regenerar data.db desde excels/."""
    try:
        print("[v2] Excels más nuevos que data.db → regenerando…")
        # Importar como módulo para reusar el mismo proceso Python
        import importlib.util
        etl_path = os.path.join(_BASE, "actualizar_data.py")
        if not os.path.exists(etl_path):
            print(f"[v2] ETL no encontrado en {etl_path}")
            return
        spec = importlib.util.spec_from_file_location("actualizar_data", etl_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
        # Recalcular DB_PATH por si el ETL escribió en api/data.db
        global DB_PATH
        DB_PATH = next((p for p in _DB_CANDIDATES if os.path.exists(p)), _DB_CANDIDATES[0])
        print(f"[v2] data.db regenerado OK → {DB_PATH}")
    except SystemExit:
        # actualizar_data.py hace sys.exit en algunos paths; lo ignoramos
        pass
    except Exception as e:
        print(f"[v2] Regeneración de data.db falló: {e}")

# ── USUARIOS ──
def _hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

USUARIOS = {
    os.getenv("USER_1_NAME", "francisco"): _hash(os.getenv("USER_1_PASS", "admin123")),
    os.getenv("USER_2_NAME", "Campo"):     _hash(os.getenv("USER_2_PASS", "markup123")),
    os.getenv("USER_3_NAME", "Gerente"):   _hash(os.getenv("USER_3_PASS", "gerentes2026")),
    os.getenv("USER_4_NAME", "Viernes"):   _hash(os.getenv("USER_4_PASS", "Callejero")),
}

# ── ROLES (Fase 1) ──
ROLES = {
    os.getenv("USER_1_NAME", "francisco"): "admin",
    os.getenv("USER_2_NAME", "Campo"):     "campo",
    os.getenv("USER_3_NAME", "Gerente"):   "gerencial",
    os.getenv("USER_4_NAME", "Viernes"):   "campo",
}


# ══════════════════════════════════════════════════════════════
# JWT (sin dependencias externas)
# ══════════════════════════════════════════════════════════════

def _b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (4 - len(s) % 4))

def crear_jwt(usuario):
    h = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64e(json.dumps({"sub": usuario, "exp": int(time.time()) + JWT_EXPIRY, "iat": int(time.time())}).encode())
    s = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64e(s)}"

def verificar_jwt(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h, p, s = parts
        expected = hmac.new(JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if _b64e(expected) != s:
            return None
        datos = json.loads(_b64d(p))
        if datos.get("exp", 0) < time.time():
            return None
        return datos.get("sub")
    except Exception:
        return None

def auth_user():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return verificar_jwt(token)


# ══════════════════════════════════════════════════════════════
# BASE DE DATOS (SQLite puro, sin pandas)
# ══════════════════════════════════════════════════════════════

def get_db():
    """Retorna conexion SQLite (una por request)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def query(sql, params=(), one=False):
    """Ejecuta query y retorna resultados como lista de dicts"""
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows[0] if one and rows else rows if not one else None
    finally:
        conn.close()

def query_val(sql, params=()):
    """Retorna un solo valor"""
    conn = get_db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()

def parsear_mes(dia_str):
    s = str(dia_str).strip()
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3 and len(parts[0]) == 4:
            return f"{parts[0]}-{parts[1]}"
    if len(s) == 8 and s.isdigit():
        return s[:4] + "-" + s[4:6]
    if len(s) == 6 and s.isdigit():
        return s[:4] + "-" + s[4:6]
    if len(s) >= 7:
        return s[:7]
    return "desconocido"


# ══════════════════════════════════════════════════════════════
# ENDPOINTS AUTH
# ══════════════════════════════════════════════════════════════

@app.route("/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return "", 204
    data = request.json or {}
    usuario = data.get("usuario", "").strip()
    contra = data.get("contrasena", data.get("contraseña", "")).strip()
    if not usuario or not contra:
        return jsonify({"error": "Usuario y contrasena requeridos"}), 400
    if usuario not in USUARIOS or USUARIOS[usuario] != _hash(contra):
        return jsonify({"error": "Credenciales invalidas"}), 401
    return jsonify({"exito": True, "token": crear_jwt(usuario), "usuario": usuario, "rol": ROLES.get(usuario, "campo"), "mensaje": f"Bienvenido {usuario}"}), 200

@app.route("/verificar_token", methods=["POST", "OPTIONS"])
def verificar_token_endpoint():
    if request.method == "OPTIONS":
        return "", 204
    data = request.json or {}
    usuario = verificar_jwt(data.get("token", ""))
    if usuario:
        return jsonify({"valido": True, "usuario": usuario}), 200
    return jsonify({"valido": False}), 401

@app.route("/logout", methods=["POST"])
def logout():
    return jsonify({"exito": True, "mensaje": "Sesion cerrada"}), 200

@app.route("/debug_db", methods=["GET"])
def debug_db():
    import glob
    base = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(base)
    info = {
        "db_path": DB_PATH,
        "exists": os.path.exists(DB_PATH),
        "size": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "files_api": [os.path.basename(f) + ":" + str(os.path.getsize(f)) for f in glob.glob(base + "/*")],
        "files_root": [os.path.basename(f) + ":" + str(os.path.getsize(f)) for f in glob.glob(parent + "/*") if os.path.isfile(f)][:30],
    }
    try:
        tables = [r["name"] for r in query("SELECT name FROM sqlite_master WHERE type='table'")]
        info["tables"] = tables
    except Exception as e:
        info["tables_error"] = str(e)[:200]
    return jsonify(info), 200


@app.route("/health", methods=["GET"])
def health():
    # Liviano: sólo confirma que el worker responde. No toca la BD para no
    # reventar el healthcheck durante el cold start de Railway.
    return jsonify({"status": "ok"}), 200

@app.route("/health/db", methods=["GET"])
def health_db():
    try:
        ventas = query_val("SELECT COUNT(*) FROM ventas")
        sap = query_val("SELECT COUNT(*) FROM sap")
        return jsonify({"status": "ok", "ventas": ventas, "sap": sap}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)[:100]}), 500


# ══════════════════════════════════════════════════════════════
# ENDPOINTS DATOS
# ══════════════════════════════════════════════════════════════

@app.route("/grupos", methods=["GET"])
def get_grupos():
    if not auth_user():
        return jsonify({"error": "No autorizado"}), 401

    rows = query("""
        SELECT GRUPOPDV,
               SUM("VENTA NETA RECUPERO") as ventas,
               COUNT(DISTINCT COALESCE(CODIGOPDV, POS)) as pos_count
        FROM ventas WHERE UNIDAD='FARMACIAS'
        GROUP BY GRUPOPDV ORDER BY ventas DESC
    """)

    mapeo = {
        "cafi mostrador": "Cruz Azul Mostrador",
        "cafa mostrador": "Cruz Azul Mostrador",
        "cofa mostrador": "Cruz Azul Mostrador",
        "cafi autoservicio": "Cruz Azul Autoservicio",
        "cafa autoservicio": "Cruz Azul Autoservicio"
    }

    agrupados = {}
    for r in rows:
        nombre = mapeo.get(r["GRUPOPDV"].lower(), r["GRUPOPDV"])
        if nombre not in agrupados:
            agrupados[nombre] = {"ventas": 0, "pos_count": 0}
        agrupados[nombre]["ventas"] += r["ventas"]
        agrupados[nombre]["pos_count"] += r["pos_count"]

    resultado = [{"grupo": k, "ventas": round(v["ventas"], 2), "total_pos": v["pos_count"]}
                 for k, v in agrupados.items()]
    resultado.sort(key=lambda x: x["ventas"], reverse=True)
    return jsonify(resultado), 200


@app.route("/farmacias", methods=["GET"])
@app.route("/farmacias/<path:grupo>", methods=["GET"])
def get_farmacias_por_grupo(grupo=None):
    if not auth_user():
        return jsonify({"error": "No autorizado"}), 401

    # Aceptamos grupo via query param o path (mas robusto ante URL encoding)
    if not grupo:
        grupo = request.args.get("grupo", "")
    grupo_decoded = unquote(grupo).replace("_", " ").strip()

    mapeo_inv = {
        "cruz azul mostrador": ("cafi mostrador", "cafa mostrador", "cofa mostrador"),
        "cruz azul autoservicio": ("cafi autoservicio", "cafa autoservicio"),
    }

    grupos = mapeo_inv.get(grupo_decoded.lower(), (grupo_decoded.lower(),))
    placeholders = ",".join(["?" for _ in grupos])

    rows = query(f"""
        SELECT POS as pos_nombre,
               COALESCE(CODIGOPDV, POS) as codigo,
               SUM("VENTA NETA RECUPERO") as ventas,
               SUM(UNIDADES_ROTADAS) as unidades
        FROM ventas
        WHERE UNIDAD='FARMACIAS' AND LOWER(GRUPOPDV) IN ({placeholders})
        GROUP BY COALESCE(CODIGOPDV, POS)
        ORDER BY ventas DESC
    """, grupos)

    if not rows:
        rows = query("""
            SELECT POS as pos_nombre, POS as codigo,
                   SUM("VENTA NETA RECUPERO") as ventas, SUM(UNIDADES_ROTADAS) as unidades
            FROM ventas WHERE UNIDAD='FARMACIAS' AND LOWER(GRUPOPDV) LIKE ?
            GROUP BY POS ORDER BY ventas DESC
        """, (f"%{grupo_decoded.lower()}%",))

    return jsonify([{
        "pos": r["pos_nombre"], "codigo": r["codigo"],
        "ventas": round(r["ventas"], 2), "unidades": int(r["unidades"] or 0)
    } for r in rows]), 200


@app.route("/buscar_pos", methods=["GET"])
def buscar_pos():
    if not auth_user():
        return jsonify({"error": "No autorizado"}), 401
    texto = request.args.get("q", "").strip()
    if len(texto) < 2:
        return jsonify([]), 200
    rows = query("""
        SELECT POS as pos, SUM("VENTA NETA RECUPERO") as ventas
        FROM ventas WHERE UNIDAD='FARMACIAS' AND LOWER(POS) LIKE ?
        GROUP BY POS ORDER BY ventas DESC LIMIT 30
    """, (f"%{texto.lower()}%",))
    return jsonify([{"pos": r["pos"], "ventas": round(r["ventas"], 2)} for r in rows]), 200


def _resolver_filtro_pos(pos_nombre):
    """Devuelve (filtro_sql, param_tupla) para filtrar por farmacia.
    Prefiere CODIGOPDV (identificador estable del PDV); cae a POS por nombre
    como fallback. Esto evita perder data cuando el cliente envia el mismo PDV
    con variaciones menores en el nombre entre archivos mensuales/semanales."""
    r = query("SELECT CODIGOPDV FROM ventas WHERE POS=? AND CODIGOPDV IS NOT NULL AND CODIGOPDV!='' LIMIT 1", (pos_nombre,), one=True)
    if not (r and r["CODIGOPDV"]):
        r = query("SELECT CODIGOPDV FROM sap WHERE POS=? AND CODIGOPDV IS NOT NULL AND CODIGOPDV!='' LIMIT 1", (pos_nombre,), one=True)
    if r and r["CODIGOPDV"] not in (None, ""):
        return "CODIGOPDV=?", (str(r["CODIGOPDV"]),)
    return "POS=?", (pos_nombre,)


@app.route("/detalle_pos", methods=["POST", "OPTIONS"])
def detalle_pos():
    if request.method == "OPTIONS":
        return "", 204
    if not auth_user():
        return jsonify({"error": "No autorizado"}), 401

    pos = (request.json or {}).get("pos", "")
    if not pos:
        return jsonify({"error": "POS requerido"}), 400

    flt, p = _resolver_filtro_pos(pos)

    info = query(f"SELECT GRUPOPDV, SUM(\"VENTA NETA RECUPERO\") as vt, SUM(UNIDADES_ROTADAS) as ur FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY GRUPOPDV", p, one=True)
    if not info:
        return jsonify({"error": f"No se encontro {pos}"}), 404

    # Meses de ventas (mensual oficial) — tienen precedencia sobre sap (semanal)
    meses_ventas_pos = set(str(r["m"]) for r in query(
        f"SELECT DISTINCT substr(DIA,1,6) as m FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt}", p))

    # Sumar ventas adicionales desde sap excluyendo meses que ya estan en ventas
    extra_vt_sum = 0.0
    extra_ur_sum = 0.0
    for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as vt, SUM(UNIDADES_ROTADAS) as ur FROM sap WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY DIA", p):
        dia = str(r["DIA"])
        mes6 = dia[:4] + dia[5:7] if "/" in dia else dia[:6]
        if mes6 in meses_ventas_pos:
            continue
        extra_vt_sum += (r["vt"] or 0)
        extra_ur_sum += (r["ur"] or 0)
    vt_extra = extra_vt_sum
    ur_extra = extra_ur_sum

    venta_total = (info["vt"] or 0) + vt_extra
    total_farm = (query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM ventas WHERE UNIDAD='FARMACIAS'") or 0) \
               + (query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM sap WHERE UNIDAD='FARMACIAS'") or 0)
    pct = (venta_total / total_farm * 100) if total_farm > 0 else 0

    # Tendencia mensual (ventas Ene/Feb + sap Mar)
    tend = {}
    dias_con_data = {}  # mes -> set(dias YYYYMMDD) — GLOBAL (todos los PDV), no solo este
    meses_en_ventas = set()
    for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY DIA", p):
        mes = parsear_mes(r["DIA"])
        tend[mes] = round(tend.get(mes, 0) + (r["v"] or 0), 2)
        meses_en_ventas.add(mes)
    for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM sap WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY DIA", p):
        mes = parsear_mes(r["DIA"])
        # Precedencia: si el mes ya existe en ventas (mensual oficial), ignorar el semanal
        if mes in meses_en_ventas:
            continue
        tend[mes] = round(tend.get(mes, 0) + (r["v"] or 0), 2)
    # Dias con data GLOBALES por mes: cuantos dias distintos tiene el SAP
    # (no depende del PDV; así proyectamos abril sobre 12 días, no 5).
    # Normalizamos DIA a 'YYYYMMDD' para deduplicar formatos '2026/04/01'
    # y '20260401', y excluimos días con <100 filas (ventas de madrugada
    # del día siguiente al corte del snapshot semanal — p.ej. 13/04 con 2 filas).
    import re as _re
    _conteo = {}
    for r in query("SELECT DIA, COUNT(*) as n FROM sap WHERE UNIDAD='FARMACIAS' AND DIA IS NOT NULL GROUP BY DIA"):
        mes = parsear_mes(r["DIA"])
        if mes in meses_en_ventas:
            continue
        d_norm = _re.sub(r"\D", "", str(r["DIA"]))[:8]
        if len(d_norm) != 8:
            continue
        _conteo.setdefault(mes, {})
        _conteo[mes][d_norm] = _conteo[mes].get(d_norm, 0) + (r["n"] or 0)
    for mes, dias_n in _conteo.items():
        for d, n in dias_n.items():
            if n >= 100:  # umbral: filas residuales de madrugada (≈2) quedan fuera
                dias_con_data.setdefault(mes, set()).add(d)

    import calendar
    def _dias_en_mes(mes_key):
        try:
            y, m = mes_key.split("-") if "-" in mes_key else (mes_key[:4], mes_key[4:6])
            return calendar.monthrange(int(y), int(m))[1]
        except Exception:
            return 30

    # Ordenar por mes y agregar etiqueta corta + prorrateo si el mes esta incompleto
    tend_ord = []
    for mes_key in sorted(tend.keys()):
        mm = mes_key[-2:] if "-" in mes_key else mes_key[4:6] if len(mes_key) >= 6 else ""
        label = MESES_ES.get(mm, mes_key)
        valor = tend[mes_key]
        dias_data = len(dias_con_data.get(mes_key, set()))
        dias_tot = _dias_en_mes(mes_key)
        entry = {"mes": mes_key, "label": label, "valor": valor,
                 "dias_con_data": dias_data, "dias_mes": dias_tot, "parcial": False}
        # Prorratear si tenemos datos diarios y el mes esta incompleto
        if 0 < dias_data < dias_tot:
            entry["valor_real"] = valor
            entry["valor_prorrateado"] = round(valor / dias_data * dias_tot, 2)
            entry["parcial"] = True
        tend_ord.append(entry)

    proyeccion = _calc_proyeccion(tend_ord)

    # Top productos (ventas + sap excluyendo meses con precedencia)
    top_map = {}
    for r in query(f"SELECT PRODUCTO, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY PRODUCTO", p):
        top_map[r["PRODUCTO"]] = top_map.get(r["PRODUCTO"], 0) + (r["v"] or 0)
    for r in query(f"SELECT PRODUCTO, DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM sap WHERE UNIDAD='FARMACIAS' AND {flt} GROUP BY PRODUCTO, DIA", p):
        dia = str(r["DIA"])
        mes6 = dia[:4] + dia[5:7] if "/" in dia else dia[:6]
        if mes6 in meses_ventas_pos:
            continue
        top_map[r["PRODUCTO"]] = top_map.get(r["PRODUCTO"], 0) + (r["v"] or 0)
    top_prods = {k: round(v, 2) for k, v in sorted(top_map.items(), key=lambda x: x[1], reverse=True)[:5]}

    # Stock SAP
    stock_info = _get_stock_pos(pos, flt=flt, p=p)

    return jsonify({
        "pos": pos,
        "grupo_pdv": info["GRUPOPDV"],
        "venta_total": round(venta_total, 2),
        "unidades_rotadas": int((info["ur"] or 0) + ur_extra),
        "pct_del_total": round(pct, 2),
        "tendencia_mensual": tend,
        "tendencia_ordenada": tend_ord,
        "proyeccion_proximo_mes": proyeccion,
        "top_5_productos": top_prods,
        "stock_info": stock_info
    }), 200


def _get_stock_pos(pos, flt=None, p=None):
    """Stock desde tabla SAP — devuelve tabla completa de items codificados"""
    if flt is None or p is None:
        flt, p = _resolver_filtro_pos(pos)
    # Adaptamos el filtro para el JOIN (usa alias s.)
    flt_s = flt.replace("CODIGOPDV=?", "s.CODIGOPDV=?").replace("POS=?", "s.POS=?")
    flt_inner = flt  # dentro del subquery sin alias
    try:
        # Ultimo DIA por producto (tomar la foto mas reciente disponible por cada item)
        rows = query(f"""
            SELECT s.PRODUCTO, s.IDNEPTUNO, s.STOCK, s.STOCK_VALORIZADO, s.DIA
            FROM sap s
            INNER JOIN (
                SELECT IDNEPTUNO, MAX(DIA) as max_dia
                FROM sap WHERE UNIDAD='FARMACIAS' AND {flt_inner}
                GROUP BY IDNEPTUNO
            ) ult ON ult.IDNEPTUNO = s.IDNEPTUNO AND ult.max_dia = s.DIA
            WHERE s.UNIDAD='FARMACIAS' AND {flt_s}
            ORDER BY s.STOCK_VALORIZADO DESC, s.STOCK DESC
        """, p + p)

        if not rows:
            return {"mensaje": "Sin registros en SAP", "detalle_completo": []}

        ultimo_dia = max((r["DIA"] for r in rows), default="")
        total_unid = sum((r["STOCK"] or 0) for r in rows)
        total_val = sum((r["STOCK_VALORIZADO"] or 0) for r in rows)

        detalle = [{
            "producto": r["PRODUCTO"],
            "id_neptuno": r["IDNEPTUNO"],
            "stock_unid": float(r["STOCK"] or 0),
            "stock_val": round(float(r["STOCK_VALORIZADO"] or 0), 2),
            "dia": str(r["DIA"])
        } for r in rows]

        con_stock = [d for d in detalle if d["stock_unid"] > 0]
        sin_stock = [d for d in detalle if d["stock_unid"] == 0]
        bajo = [d for d in con_stock if 0 < d["stock_unid"] <= 3]

        return {
            "fecha": str(ultimo_dia),
            "total_productos": len(detalle),
            "total_con_stock": len(con_stock),
            "total_sin_stock": len(sin_stock),
            "total_unidades": round(total_unid, 0),
            "total_valorizado": round(total_val, 2),
            "detalle_completo": detalle,
            "sin_stock": [d["producto"] for d in sin_stock][:8],
            "bajo_stock": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in bajo][:8],
            # Retrocompat
            "detalle_stock": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in con_stock[:15]],
            "con_stock_ok": [{"PRODUCTO": d["producto"], "STOCK": d["stock_unid"]} for d in con_stock if d["stock_unid"] > 3][:5]
        }
    except Exception as e:
        return {"error": f"Error: {str(e)[:80]}", "detalle_completo": []}


def _calc_proyeccion(tend_ord):
    """Proyeccion del MES EN CURSO (cierre). Si el ultimo mes es parcial,
    proyecta su cierre con formula lineal: valor / dias_data * dias_mes.
    Si el ultimo mes ya esta completo, proyecta el proximo mes con crecimiento promedio."""
    if not tend_ord:
        return None
    last = tend_ord[-1]
    # Caso principal: mes en curso parcial -> proyeccion lineal del cierre
    if last.get("parcial"):
        valor_real = last["valor"]
        dias_data = last.get("dias_con_data", 0)
        dias_mes = last.get("dias_mes", 30)
        if dias_data > 0:
            proy = valor_real / dias_data * dias_mes
        else:
            proy = valor_real
        # % vs mes anterior (para ver si cierra mejor o peor)
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
            "metodo": f"lineal {round(valor_real,2)}/{dias_data}*{dias_mes}"
        }
    # Fallback: todos los meses completos -> proyectar proximo mes con crecimiento
    def _vef(e):
        return e.get("valor_prorrateado", e["valor"])
    if len(tend_ord) == 1:
        return {"valor": round(_vef(tend_ord[0]), 2), "label": "Proy.", "metodo": "ultimo mes"}
    crec = []
    for i in range(1, len(tend_ord)):
        prev = _vef(tend_ord[i-1])
        cur = _vef(tend_ord[i])
        if prev > 0:
            crec.append((cur - prev) / prev)
    base = _vef(tend_ord[-1])
    if not crec:
        return {"valor": round(base, 2), "label": "Proy.", "metodo": "ultimo mes"}
    avg = sum(crec) / len(crec)
    proy = base * (1 + avg)
    return {
        "valor": round(proy, 2),
        "label": "Proy.",
        "crecimiento_pct": round(avg * 100, 1),
        "metodo": f"crecimiento promedio {round(avg*100,1)}%"
    }


MESES_ES = {"01":"Ene","02":"Feb","03":"Mar","04":"Abr","05":"May","06":"Jun",
            "07":"Jul","08":"Ago","09":"Sep","10":"Oct","11":"Nov","12":"Dic"}


@app.route("/productos_faltantes", methods=["POST", "OPTIONS"])
def productos_faltantes():
    if request.method == "OPTIONS":
        return "", 204
    if not auth_user():
        return jsonify({"error": "No autorizado"}), 401

    pos = (request.json or {}).get("pos", "")
    if not pos:
        return jsonify({"error": "POS requerido"}), 400

    resultado = _calc_faltantes(pos)
    return jsonify(resultado), 200


def _calc_faltantes(pos):
    """Top 5 productos faltantes con oportunidad"""
    flt, p = _resolver_filtro_pos(pos)
    prods_farm = query(f"SELECT DISTINCT PRODUCTO FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt}", p)
    if not prods_farm:
        return {"pos": pos, "error": f"No se encontro {pos}"}

    productos_en = set(r["PRODUCTO"] for r in prods_farm)

    ranking = query("""
        SELECT PRODUCTO, MARCA,
               SUM("VENTA NETA RECUPERO") as venta_total,
               COUNT(DISTINCT POS) as num_farmacias,
               SUM(UNIDADES_ROTADAS) as unidades_totales
        FROM ventas WHERE UNIDAD='FARMACIAS'
        GROUP BY PRODUCTO ORDER BY venta_total DESC
    """)

    total_farmacias = query_val("SELECT COUNT(DISTINCT POS) FROM ventas WHERE UNIDAD='FARMACIAS'")

    faltantes = [r for r in ranking if r["PRODUCTO"] not in productos_en][:20]

    resultado = []
    for r in faltantes[:5]:
        vta_prom = r["venta_total"] / r["num_farmacias"] if r["num_farmacias"] > 0 else 0
        pen = (r["num_farmacias"] / total_farmacias * 100) if total_farmacias > 0 else 0
        score = vta_prom * (r["num_farmacias"] / total_farmacias) if total_farmacias > 0 else 0
        resultado.append({
            "marca": r["MARCA"],
            "producto": r["PRODUCTO"],
            "venta_global_total": round(r["venta_total"], 2),
            "venta_promedio_por_farmacia": round(vta_prom, 2),
            "disponible_en_farmacias": r["num_farmacias"],
            "penetracion_mercado": round(pen, 1),
            "unidades_totales_vendidas": int(r["unidades_totales"] or 0),
            "score_oportunidad": round(score, 2)
        })

    return {
        "pos": pos,
        "total_productos_faltantes": len(faltantes),
        "top_5_productos_faltantes": resultado,
        "productos_en_farmacia": len(productos_en),
        "productos_globales": len(ranking),
        "total_farmacias_red": total_farmacias
    }


# ══════════════════════════════════════════════════════════════
# CHAT CON CLAUDE AI
# ══════════════════════════════════════════════════════════════

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 204
    if not auth_user():
        return jsonify({"error": "No autorizado. Inicia sesion primero."}), 401

    data = request.json or {}
    pregunta = data.get("pregunta", "").strip()
    contexto_pos = data.get("contexto_pos", None)
    if not pregunta:
        return jsonify({"error": "Pregunta vacia"}), 400

    if contexto_pos:
        flt_c, p_c = _resolver_filtro_pos(contexto_pos)
        info = query(f"SELECT GRUPOPDV, SUM(\"VENTA NETA RECUPERO\") as vt FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY GRUPOPDV", p_c, one=True)
        if not info:
            return jsonify({"error": f"Farmacia {contexto_pos} no encontrada"}), 404

        meses_v_ctx = set(str(r["m"]) for r in query(
            f"SELECT DISTINCT substr(DIA,1,6) as m FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt_c}", p_c))
        def _mes6(d):
            d = str(d)
            return d[:4] + d[5:7] if "/" in d else d[:6]

        extra_vt = 0.0
        for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as vt FROM sap WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY DIA", p_c):
            if _mes6(r["DIA"]) in meses_v_ctx: continue
            extra_vt += (r["vt"] or 0)
        venta_total_ctx = (info["vt"] or 0) + extra_vt
        total_farm = (query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM ventas WHERE UNIDAD='FARMACIAS'") or 0) \
                   + (query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM sap WHERE UNIDAD='FARMACIAS'") or 0)
        pct = (venta_total_ctx / total_farm * 100) if total_farm else 0

        tend = {}
        for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY DIA", p_c):
            mes = parsear_mes(r["DIA"])
            tend[mes] = round(tend.get(mes, 0) + (r["v"] or 0), 2)
        for r in query(f"SELECT DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM sap WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY DIA", p_c):
            if _mes6(r["DIA"]) in meses_v_ctx: continue
            mes = parsear_mes(r["DIA"])
            tend[mes] = round(tend.get(mes, 0) + (r["v"] or 0), 2)

        top_map_c = {}
        for r in query(f"SELECT PRODUCTO, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY PRODUCTO", p_c):
            top_map_c[r["PRODUCTO"]] = top_map_c.get(r["PRODUCTO"], 0) + (r["v"] or 0)
        for r in query(f"SELECT PRODUCTO, DIA, SUM(\"VENTA NETA RECUPERO\") as v FROM sap WHERE UNIDAD='FARMACIAS' AND {flt_c} GROUP BY PRODUCTO, DIA", p_c):
            if _mes6(r["DIA"]) in meses_v_ctx: continue
            top_map_c[r["PRODUCTO"]] = top_map_c.get(r["PRODUCTO"], 0) + (r["v"] or 0)
        top = [{"PRODUCTO": k, "v": v} for k, v in sorted(top_map_c.items(), key=lambda x: x[1], reverse=True)[:5]]
        stock = _get_stock_pos(contexto_pos)
        faltantes = _calc_faltantes(contexto_pos).get("top_5_productos_faltantes", [])

        contexto = {
            "pos": contexto_pos, "grupo": info["GRUPOPDV"],
            "venta_total": round(venta_total_ctx, 2), "pct_total": round(pct, 2),
            "tendencia": tend,
            "top_productos": {r["PRODUCTO"]: round(r["v"], 2) for r in top},
            "stock": stock,
            "productos_faltantes_oportunidad": faltantes
        }
    else:
        vf = query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM ventas WHERE UNIDAD='FARMACIAS'")
        vd = query_val("SELECT SUM(\"VENTA NETA RECUPERO\") FROM ventas WHERE UNIDAD='DISTRIBUCION DIFARE'")
        top_f = query("SELECT POS, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD='FARMACIAS' GROUP BY POS ORDER BY v DESC LIMIT 5")
        top_m = query("SELECT MARCA, SUM(\"VENTA NETA RECUPERO\") as v FROM ventas WHERE UNIDAD!='DIFARE S.A.' GROUP BY MARCA ORDER BY v DESC LIMIT 5")
        contexto = {
            "venta_farmacias": round(vf or 0, 2),
            "venta_distribucion": round(vd or 0, 2),
            "top_farmacias": {r["POS"]: round(r["v"], 2) for r in top_f},
            "top_marcas": {r["MARCA"]: round(r["v"], 2) for r in top_m}
        }

    prompt = f"""Eres el asistente comercial Difare Nexus de Genommalab Ecuador.
Datos reales DIFARE Ecuador enero-marzo 2026.
Responde conciso, ejecutivo, maximo 5 lineas. Usa emojis. Destaca numeros con **negrita**.

IMPORTANTE: Solo puedes responder sobre la farmacia actualmente seleccionada.
Si el usuario pregunta por OTRA farmacia diferente, responde:
"Para consultar otra farmacia, usa el boton Inicio para seleccionarla."

Productos faltantes con oportunidad de venta (DATOS REALES):
{contexto.get('productos_faltantes_oportunidad', 'No hay datos')}

Farmacia seleccionada: {contexto.get('pos', 'GENERAL')}
Datos: {contexto}
Pregunta: {pregunta}

Responde en espanol, practico para vendedor en campo."""

    try:
        resp = get_anthropic_client().messages.create(
            model="claude-sonnet-4-5", max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return jsonify({"respuesta": resp.content[0].text, "contexto_tipo": "farmacia" if contexto_pos else "general"}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:100], "respuesta": "Disculpa, hubo un error. Intenta de nuevo."}), 500


# ══════════════════════════════════════════════════════════════
# FRONTEND
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return Response(FRONTEND_HTML, mimetype="text/html")


# ══════════════════════════════════════════════════════════════
# Dashboard Gerencial — Día 4
# ══════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Difare Nexus · Dashboard Gerencial</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--navy:#0a1628;--navy2:#111f38;--blue:#1B3A6B;--azure:#2E75B6;--gold:#C9A84C;--gold2:#F0C97A;--white:#F8FAFF;--muted:#7a8fbb;--border:rgba(46,117,182,0.2)}
  body{font-family:'DM Sans',system-ui,sans-serif;background:var(--navy);color:var(--white)}
  .card{background:var(--navy2);border:1px solid var(--border);border-radius:14px}
  .kpi-val{font-variant-numeric:tabular-nums;color:var(--gold)}
  .kpi-label{color:var(--muted);text-transform:uppercase;letter-spacing:0.05em}
  .kpi-sub{color:var(--muted)}
  h2.section-title{color:var(--gold);font-family:'Playfair Display',serif}
  .seg{background:rgba(27,58,107,0.3);border:1px solid var(--border);border-radius:10px;padding:4px;display:inline-flex;gap:4px}
  .seg button{padding:6px 14px;font-size:13px;border-radius:7px;color:var(--muted);font-weight:500;background:transparent;border:none;cursor:pointer}
  .seg button.active{background:var(--blue);color:var(--gold);box-shadow:0 1px 4px rgba(0,0,0,.2)}
  .seg button:hover{color:var(--gold)}
  .row:hover{background:rgba(46,117,182,0.08)}
  .spinner{border:2px solid var(--border);border-top-color:var(--gold);border-radius:50%;width:18px;height:18px;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .fab{position:fixed;bottom:26px;right:26px;background:var(--navy2);color:var(--gold);padding:14px 22px;border-radius:999px;border:1px solid var(--border);box-shadow:0 10px 24px rgba(0,0,0,.4);display:flex;align-items:center;gap:10px;font-weight:600;transition:transform .15s;z-index:50;touch-action:none}
  .fab:hover{transform:translateY(-2px);border-color:var(--gold)}
  @media(max-width:768px){.fab{padding:10px 14px;font-size:12px;gap:6px;bottom:80px;right:14px}.fab svg{width:14px;height:14px}}
  table{font-variant-numeric:tabular-nums;color:var(--white)}
  /* Dashboard table rows */
  .dash-table th{color:var(--gold);background:var(--blue);border-bottom:1px solid var(--border);font-weight:500;font-size:12px}
  .dash-table td{border-bottom:1px solid var(--border);color:var(--white)}
  .dash-table .text-slate-400{color:var(--muted)!important}
  .dash-table .text-slate-500{color:var(--muted)!important}
  .dash-table .font-medium{color:var(--white)!important}
  .qbtn{font-size:12px;padding:6px 14px;border-radius:9px;border:1px solid rgba(201,168,76,0.3);color:#C9A84C;background:rgba(201,168,76,0.08);cursor:pointer;transition:all .15s;white-space:nowrap}
  .qbtn:hover{background:rgba(201,168,76,0.18);color:#F0C97A;border-color:rgba(201,168,76,0.5)}
  .chat-section{background:#0a1628;border:1px solid rgba(46,117,182,0.2);border-radius:14px;color:#F8FAFF}
  .chat-section h2{color:#C9A84C;font-family:'Playfair Display',serif}
  .chat-section p{color:#7a8fbb}
  .chat-bubble-user{background:linear-gradient(135deg,#1B3A6B,#2E75B6);color:#F8FAFF;border-radius:16px 16px 4px 16px;padding:12px 16px;font-size:13px;max-width:80%;margin-left:auto}
  .chat-bubble-bot{background:#111f38;border:1px solid rgba(46,117,182,0.2);color:#F8FAFF;border-radius:16px 16px 16px 4px;padding:12px 16px;font-size:13px;max-width:90%}
  .chat-bubble-bot strong{color:#C9A84C;font-weight:600}
  .chat-bubble-bot em{color:#7a8fbb;font-style:italic}
  .chat-bubble-bot table{border-collapse:collapse;width:100%;font-size:11px;margin:8px 0}
  .chat-bubble-bot th{background:#1B3A6B;color:#C9A84C;border:1px solid rgba(46,117,182,0.3);padding:6px 8px;text-align:left;font-weight:500;font-size:11px}
  .chat-bubble-bot td{border:1px solid rgba(46,117,182,0.2);padding:5px 8px;color:#F8FAFF}
  .chat-bubble-bot tr:hover td{background:rgba(46,117,182,0.1)}
  .chat-bubble-bot code{background:rgba(46,117,182,0.2);padding:1px 5px;border-radius:4px;font-size:11px;color:#60A5FA}
  .chat-bubble-bot .download-link{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;background:linear-gradient(135deg,#C9A84C,#F0C97A);color:#0a1628;font-size:12px;font-weight:700;border-radius:10px;text-decoration:none;margin-top:8px}
  .chat-bubble-bot .download-link:hover{opacity:0.9}
  .chat-input-area{display:flex;gap:10px;align-items:end}
  .chat-input-area textarea{flex:1;resize:none;background:#111f38;border:1px solid rgba(46,117,182,0.3);border-radius:12px;padding:12px 16px;color:#F8FAFF;font-size:13px;font-family:'DM Sans',system-ui,sans-serif;outline:none}
  .chat-input-area textarea::placeholder{color:#7a8fbb}
  .chat-input-area textarea:focus{border-color:#2E75B6}
  .chat-send-btn{width:42px;height:42px;background:linear-gradient(135deg,#C9A84C,#F0C97A);border:none;border-radius:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:transform .15s}
  .chat-send-btn:hover{transform:scale(1.05)}
  .chat-send-btn:disabled{opacity:0.3}
  .chat-clear-btn{font-size:11px;color:#7a8fbb;border:1px solid rgba(46,117,182,0.2);background:transparent;padding:4px 12px;border-radius:8px;cursor:pointer}
  .chat-clear-btn:hover{color:#C9A84C;border-color:rgba(201,168,76,0.3)}
</style>
</head>
<body class="min-h-screen">

<header class="sticky top-0 z-20" style="background:var(--navy2);border-bottom:1px solid var(--border)">
  <div class="max-w-[1400px] mx-auto px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="w-9 h-9 rounded-lg flex items-center justify-center font-bold" style="background:linear-gradient(135deg,var(--gold),var(--gold2));color:var(--navy)">N</div>
      <div>
        <div class="text-[15px] font-semibold" style="font-family:'Playfair Display',serif;color:var(--gold)">Difare Nexus</div>
        <div class="text-xs" style="color:var(--muted)">Dashboard Gerencial</div>
      </div>
    </div>
    <div class="flex items-center gap-3 text-sm">
      <span style="color:var(--muted)">Hola, <b id="userLabel" style="color:var(--white)">—</b></span>
      <span id="rolBadge" class="px-2 py-1 rounded-md text-xs font-medium" style="background:rgba(201,168,76,0.15);color:var(--gold)">—</span>
      <button onclick="exportarPDFScreenshot()" title="Captura de pantalla" class="text-sm" style="color:var(--gold);border:1px solid rgba(201,168,76,0.3);padding:4px 10px;border-radius:8px;cursor:pointer;background:rgba(201,168,76,0.08)">📸 Screenshot</button>
      <button onclick="exportarPDFReporte()" title="Reporte PDF completo" class="text-sm" style="color:var(--gold);border:1px solid rgba(201,168,76,0.3);padding:4px 10px;border-radius:8px;cursor:pointer;background:rgba(201,168,76,0.08)">📄 Reporte PDF</button>
      <button onclick="logout()" class="text-sm" style="color:var(--muted);border:1px solid var(--border);padding:4px 10px;border-radius:8px;cursor:pointer;background:transparent">Salir</button>
    </div>
  </div>
</header>

<main class="max-w-[1400px] mx-auto px-6 py-6 space-y-6">

  <!-- Loading overlay -->
  <div id="loading-overlay" style="display:none;position:fixed;inset:0;z-index:9999;background:var(--navy);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;">
    <div style="border:3px solid var(--border);border-top-color:var(--gold);border-radius:50%;width:48px;height:48px;animation:spin 1s linear infinite"></div>
    <div style="color:var(--gold);font-size:16px;font-weight:500">Cargando datos...</div>
    <div id="loading-sub" style="color:var(--muted);font-size:13px">Esto toma ~30 segundos después de cada deploy</div>
  </div>

  <!-- Filtros globales -->
  <style>
    .ms-wrap{position:relative;display:inline-block;min-width:170px}
    .ms-btn{background:var(--navy);color:var(--white);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;width:100%;text-align:left;display:flex;align-items:center;justify-content:space-between;gap:6px}
    .ms-btn:hover{border-color:var(--gold)}
    .ms-btn .ms-arrow{font-size:10px;opacity:.6}
    .ms-drop{display:none;position:absolute;top:100%;left:0;right:0;z-index:50;background:var(--card);border:1px solid var(--border);border-radius:8px;margin-top:4px;max-height:240px;overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.4)}
    .ms-drop.open{display:block}
    .ms-drop label{display:flex;align-items:center;gap:8px;padding:7px 12px;font-size:13px;color:var(--white);cursor:pointer;transition:background .15s}
    .ms-drop label:hover{background:rgba(201,168,76,.1)}
    .ms-drop input[type=checkbox]{accent-color:var(--gold);width:15px;height:15px}
    .ms-badge{display:inline-block;background:var(--gold);color:var(--navy);font-size:11px;font-weight:600;padding:1px 7px;border-radius:10px;margin-left:4px}
    /* ── Mobile responsive ── */
    @media(max-width:820px){
      /* Header */
      header .flex{flex-wrap:wrap;gap:6px}
      header .flex .flex.items-center.gap-3:last-child{flex-wrap:wrap;justify-content:flex-end}
      /* Main content: usar todo el ancho */
      main{padding-left:12px!important;padding-right:12px!important}
      /* KPIs: 2 columnas en móvil */
      #kpis{display:grid!important;grid-template-columns:1fr 1fr!important;gap:8px!important}
      #kpis .card{padding:12px!important}
      .kpi-val{font-size:1.3rem!important}
      .kpi-label{font-size:10px!important}
      .kpi-sub{font-size:10px!important}
      /* Filtros: apilados vertical */
      #filtros-bar{padding:10px!important}
      #filtros-bar .flex{flex-direction:column;align-items:stretch;gap:8px}
      #filtros-bar select, .ms-btn{width:100%;min-width:0;font-size:14px;padding:10px 12px}
      .ms-wrap{width:100%}
      #filtro-reset{width:100%;text-align:center;padding:10px!important}
      #filtro-label{text-align:center;margin-left:0!important}
      /* Chart height */
      #chartCanalMes{min-height:200px}
      /* Tabla TP: scroll horizontal */
      .dash-table{font-size:11px!important}
    }
    /* ── Sticky columns for TP table ── */
    .dash-table th.sticky-col, .dash-table td.sticky-col{position:sticky;z-index:2;background:inherit}
    .dash-table th.sticky-col-1, .dash-table td.sticky-col-1{left:0;min-width:70px;max-width:90px}
    .dash-table th.sticky-col-2, .dash-table td.sticky-col-2{left:70px;min-width:110px;max-width:160px;border-right:2px solid var(--border)}
    .dash-table thead th.sticky-col{background:var(--blue)!important;z-index:3}
    .dash-table tbody td.sticky-col{background:var(--navy2)}
  </style>
  <section class="card p-4" id="filtros-bar">
    <div class="flex flex-wrap items-center gap-3">
      <span class="text-xs font-semibold" style="color:var(--gold);text-transform:uppercase;letter-spacing:0.05em">Filtros</span>

      <!-- Marca (single select) -->
      <select id="filtro-marca" style="background:var(--navy);color:var(--white);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;min-width:160px">
        <option value="">Todas las marcas</option>
      </select>

      <!-- Canal (single select: Farmacias / Distribución) -->
      <select id="filtro-canal" style="background:var(--navy);color:var(--white);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;min-width:160px">
        <option value="">Todos los canales</option>
      </select>

      <!-- Grupo PDV (multi-select checkboxes) -->
      <div class="ms-wrap" id="ms-grupo-wrap">
        <button type="button" class="ms-btn" id="ms-grupo-btn" onclick="toggleMS('grupo')">
          <span id="ms-grupo-label">Todos los grupos</span><span class="ms-arrow">▼</span>
        </button>
        <div class="ms-drop" id="ms-grupo-drop"></div>
      </div>

      <!-- Producto (multi-select checkboxes, cascaded by marca) -->
      <div class="ms-wrap" id="ms-producto-wrap">
        <button type="button" class="ms-btn" id="ms-producto-btn" onclick="toggleMS('producto')">
          <span id="ms-producto-label">Todos los productos</span><span class="ms-arrow">▼</span>
        </button>
        <div class="ms-drop" id="ms-producto-drop"></div>
      </div>

      <button id="filtro-reset" onclick="resetFiltros()" style="display:none;font-size:12px;color:var(--gold);border:1px solid rgba(201,168,76,0.3);background:transparent;padding:5px 14px;border-radius:8px;cursor:pointer">✕ Limpiar filtros</button>
      <span id="filtro-label" class="text-xs" style="color:var(--muted);margin-left:auto"></span>
    </div>
  </section>

  <!-- KPI cards -->
  <section id="kpis" class="grid gap-4" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <div class="card p-5">
      <div class="text-xs kpi-label">Venta Total</div>
      <div class="kpi-val text-2xl font-semibold mt-1" id="kpi-total">—</div>
      <div class="text-xs kpi-sub mt-1">Acumulado 2026</div>
      <div class="text-xs mt-0.5" style="color:var(--muted);font-size:10px" id="kpi-total-sub">—</div>
    </div>
    <div class="card p-5">
      <div class="text-xs kpi-label">Farmacias</div>
      <div class="flex items-baseline gap-2 mt-1">
        <div class="kpi-val text-2xl font-semibold" id="kpi-farm">—</div>
        <span class="text-xs font-medium" style="color:var(--gold);opacity:0.7" id="kpi-farm-pct"></span>
      </div>
      <div class="text-xs kpi-sub mt-1">Canal directo</div>
    </div>
    <div class="card p-5">
      <div class="text-xs kpi-label">Distribución</div>
      <div class="flex items-baseline gap-2 mt-1">
        <div class="kpi-val text-2xl font-semibold" id="kpi-dist">—</div>
        <span class="text-xs font-medium" style="color:var(--gold);opacity:0.7" id="kpi-dist-pct"></span>
      </div>
      <div class="text-xs kpi-sub mt-1">Distribución Difare</div>
    </div>
    <div class="card p-5">
      <div class="text-xs kpi-label">Universo PDV</div>
      <div class="kpi-val text-2xl font-semibold mt-1" id="kpi-univ">—</div>
      <div class="text-xs kpi-sub mt-1" id="kpi-periodo">—</div>
    </div>
  </section>

  <!-- Venta por canal por mes -->
  <section class="card p-6">
    <div class="flex items-center justify-between mb-4">
      <div>
        <h2 class="text-lg font-semibold section-title">Venta mensual por canal</h2>
        <p class="text-sm" style="color:var(--muted)">Farmacias, Distribución y Total por mes · mes actual incluye proyección (banda punteada)</p>
      </div>
      <div id="chart-canal-sub" class="text-xs text-slate-500 text-right"></div>
    </div>
    <div class="relative" style="height:360px"><canvas id="chartCanalMes"></canvas></div>
  </section>

  <!-- Tienda Perfecta + Distribución Numérica -->
  <div class="grid grid-cols-1 xl:grid-cols-5 gap-6">
    <!-- Oportunidades Tienda Perfecta Farmacias -->
    <section class="card p-6 xl:col-span-3">
      <div class="mb-4">
        <h2 class="text-lg font-semibold section-title">Oportunidades Tienda Perfecta Farmacias</h2>
        <p class="text-sm" style="color:var(--muted)">Productos Pareto (80% venta) · Stock del último día SAP · Buckets exclusivos</p>
      </div>
      <div class="mb-3" style="display:flex;gap:10px;flex-wrap:wrap">
        <a id="btn-tp-excel" href="#" onclick="descargarTPExcel(event)"
           style="display:inline-flex;align-items:center;gap:6px;background:var(--gold);color:var(--navy);padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;text-decoration:none;cursor:pointer;transition:opacity .2s"
           onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
          Descargar Excel
        </a>
        <button onclick="abrirTPFullscreen()"
           style="display:inline-flex;align-items:center;gap:6px;background:transparent;color:var(--gold);padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;border:1px solid var(--gold);cursor:pointer;transition:opacity .2s"
           onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/></svg>
          Ampliar tabla
        </button>
      </div>
      <div class="overflow-auto max-h-[480px]">
        <table class="w-full text-xs dash-table" id="tp-table">
          <thead class="sticky top-0" style="background:var(--blue);color:var(--gold)">
            <tr>
              <th class="text-left px-2 py-2 sticky-col sticky-col-1">Marca</th>
              <th class="text-left px-2 py-2 sticky-col sticky-col-2">Producto</th>
              <th class="text-right px-2 py-2">Venta</th>
              <th class="text-right px-2 py-2">Peso%</th>
              <th class="text-right px-2 py-2">Acum%</th>
              <th class="text-center px-2 py-2">PDV</th>
              <th class="text-center px-2 py-2">Presencia</th>
              <th class="text-center px-2 py-2">%Cob</th>
              <th class="text-center px-2 py-2" style="color:#ef4444">#PDV Stock=0</th>
              <th class="text-center px-2 py-2" style="color:#f59e0b">#PDV Stock=1</th>
              <th class="text-center px-2 py-2" style="color:#3b82f6">#PDV Stock=2</th>
              <th class="text-center px-2 py-2" style="color:#8b5cf6">#PDV Stock=3</th>
            </tr>
          </thead>
          <tbody id="tp-body"><tr><td colspan="12" class="text-center py-6" style="color:var(--muted)">Cargando…</td></tr></tbody>
        </table>
      </div>
    </section>

    <!-- Distribución Numérica Canal Distribución -->
    <section class="card p-6 xl:col-span-2">
      <div class="flex items-center justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold section-title">Distribución Numérica</h2>
          <p class="text-sm" style="color:var(--muted)">Canal Distribución · Clientes impactados por mes</p>
        </div>
      </div>
      <div class="mb-3">
        <select id="dist-marca-filter" style="background:var(--navy);color:var(--white);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;width:100%">
          <option value="">Todas las marcas</option>
        </select>
      </div>
      <div class="text-2xl font-semibold kpi-val mb-1" id="dist-total">—</div>
      <div class="text-xs mb-3" style="color:var(--muted)" id="dist-sub">Clientes únicos (RUC) histórico</div>
      <div class="relative" style="height:280px"><canvas id="chartDistNumerica"></canvas></div>
    </section>
  </div>
</main>

<!-- Fullscreen overlay Tienda Perfecta -->
<div id="tp-fullscreen" style="display:none;position:fixed;inset:0;z-index:10000;background:var(--navy);overflow:auto;padding:20px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <div>
      <h2 style="color:var(--gold);font-size:18px;font-weight:700;margin:0">Oportunidades Tienda Perfecta Farmacias</h2>
      <p style="color:var(--muted);font-size:12px;margin:4px 0 0">Productos Pareto (80% venta) · Stock del último día SAP · Buckets exclusivos</p>
    </div>
    <button onclick="cerrarTPFullscreen()" style="background:var(--gold);color:var(--navy);border:none;border-radius:8px;padding:8px 16px;font-weight:600;font-size:13px;cursor:pointer">✕ Cerrar</button>
  </div>
  <div id="tp-full-table" style="overflow:auto"></div>
</div>

<!-- Chat Gerencial integrado -->
<section class="chat-section p-6 mt-6" id="chat-section">
  <div class="flex items-center justify-between mb-4">
    <div>
      <h2 class="text-lg font-semibold">Asistente Gerencial</h2>
      <p class="text-sm" style="color:#7a8fbb">Pregunta sobre tendencias, inventario, rankings, Pareto o pide exportar a Excel</p>
    </div>
    <button onclick="chatClear()" class="chat-clear-btn">Limpiar chat</button>
  </div>
  <!-- Quick actions -->
  <div class="flex flex-wrap gap-2 mb-4" id="chat-quick">
    <button class="qbtn" onclick="chatSend('Dame un resumen general de KPIs y cómo vamos este mes')">Resumen general</button>
    <button class="qbtn" onclick="chatSend('Muéstrame la tendencia de ventas por marca en farmacias')">Tendencia marcas</button>
    <button class="qbtn" onclick="chatSend('¿Cuántos días de inventario tenemos? ¿Hay riesgo de desabasto?')">Días inventario</button>
    <button class="qbtn" onclick="chatSend('¿Cuáles son las farmacias Pareto que concentran el 80% de la venta?')">Pareto 80/20</button>
    <button class="qbtn" onclick="chatSend('Top 20 farmacias por venta')">Top farmacias</button>
    <button class="qbtn" onclick="chatSend('Top 10 clientes de distribución')">Top distribución</button>
    <button class="qbtn" onclick="chatSend('Genera el informe de vectorización semanal en Excel para todas las marcas')">Vectorización Excel</button>
    <button class="qbtn" onclick="chatSend('¿Qué oportunidades de vectorización tengo?')">Oportunidades</button>
  </div>
  <!-- Messages -->
  <div id="chat-msgs" class="space-y-3 overflow-y-auto mb-4 scroll-smooth" style="max-height:600px;min-height:80px"></div>
  <!-- Input -->
  <div class="chat-input-area">
    <textarea id="chat-input" rows="1" placeholder="Escribe tu pregunta..."></textarea>
    <button id="chat-send" onclick="chatSendInput()" class="chat-send-btn" disabled>
      <svg width="16" height="16" fill="none" stroke="#0a1628" stroke-width="2.5" viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
    </button>
  </div>
</section>

<a href="/?modo=campo" target="_blank" class="fab" id="fab-btn">
  <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
  <span class="fab-text">Consultar farmacia</span>
</a>

<script>
const S=window.location.origin;
let TK=localStorage.getItem("nx_tk");
let US=localStorage.getItem("nx_us");
let RL=localStorage.getItem("nx_rl");

if(!TK||!US||(RL!=="admin"&&RL!=="gerencial")){window.location.href="/";}

document.getElementById("userLabel").textContent=US||"—";
document.getElementById("rolBadge").textContent=RL==="admin"?"Admin":"Gerencial";

const AH={"Content-Type":"application/json","Authorization":"Bearer "+TK};
// Dark theme global para Chart.js
Chart.defaults.color="#7a8fbb";
Chart.defaults.borderColor="rgba(46,117,182,0.15)";
const fmtUSD = v => "$"+Math.round(v||0).toLocaleString("es-EC");
const fmtShort = v => {v=v||0; if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M"; if(v>=1e3)return "$"+(v/1e3).toFixed(0)+"K"; return "$"+Math.round(v)}

function logout(){
  fetch(S+"/logout",{method:"POST",headers:AH,body:"{}"}).catch(()=>{});
  localStorage.clear();
  window.location.href="/";
}

async function api(path){
  const r=await fetch(S+path,{headers:AH});
  if(r.status===401){logout();return null;}
  return r.json();
}

// Descarga directa Excel Tienda Perfecta
async function descargarTPExcel(e){
  e.preventDefault();
  const btn=document.getElementById("btn-tp-excel");
  const orig=btn.innerHTML;
  btn.innerHTML='<span style="display:inline-flex;align-items:center;gap:6px">Generando…<span style="border:2px solid var(--navy);border-top-color:transparent;border-radius:50%;width:14px;height:14px;display:inline-block;animation:spin 1s linear infinite"></span></span>';
  btn.style.pointerEvents="none";btn.style.opacity="0.7";
  try{
    const r=await fetch(S+"/api/tienda-perfecta-excel?token="+encodeURIComponent(TK));
    if(!r.ok){const j=await r.json().catch(()=>({}));alert(j.error||"Error al generar");return;}
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download=r.headers.get("content-disposition")?.match(/filename="?(.+?)"?$/)?.[1]||"vectorizacion.xlsx";
    document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
  }catch(err){alert("Error: "+err.message);}
  finally{btn.innerHTML=orig;btn.style.pointerEvents="";btn.style.opacity="";}
}

// Fullscreen Tienda Perfecta
function abrirTPFullscreen(){
  const src=document.getElementById("tp-table");
  const dst=document.getElementById("tp-full-table");
  dst.innerHTML=src.outerHTML;
  // Remove max height constraint and make table bigger
  const tbl=dst.querySelector("table");
  if(tbl){tbl.style.fontSize="13px";}
  const wrapper=dst.querySelector(".overflow-auto");
  if(wrapper){wrapper.style.maxHeight="none";}
  document.getElementById("tp-fullscreen").style.display="block";
  document.body.style.overflow="hidden";
}
function cerrarTPFullscreen(){
  document.getElementById("tp-fullscreen").style.display="none";
  document.body.style.overflow="";
}

// Banner de error global
function mostrarError(msg){
  let b=document.getElementById("err-banner");
  if(!b){b=document.createElement("div");b.id="err-banner";b.className="card p-4 border-l-4 border-red-500 bg-red-50 text-red-700 text-sm";document.querySelector("main").insertBefore(b,document.querySelector("main").firstChild);}
  b.innerHTML="<b>Error al cargar datos:</b> "+msg;
}

// ── Esperar a que el servidor termine de cargar data ──
async function waitForReady(maxWait=120){
  const overlay=document.getElementById("loading-overlay");
  const sub=document.getElementById("loading-sub");
  overlay.style.display="flex";
  const t0=Date.now();
  while((Date.now()-t0)<maxWait*1000){
    try{
      const r=await fetch(S+"/api/ready");
      const d=await r.json();
      if(d.ready){return true;} // overlay se oculta después de cargar todo
    }catch(e){}
    const elapsed=Math.round((Date.now()-t0)/1000);
    if(sub)sub.textContent=`Procesando datos... ${elapsed}s`;
    await new Promise(r=>setTimeout(r,2000));
  }
  return true; // proceder de todas formas — overlay se oculta después
}

// ── Filtro global (marca, canal, grupo multi, producto multi) ──
let _filtroMarca="";
let _filtroCanal="";
let _filtroGrupos=[];   // array de strings
let _filtroProductos=[]; // array de strings

// Multi-select helpers
function toggleMS(name){
  const drop=document.getElementById("ms-"+name+"-drop");
  // Close other dropdowns first
  document.querySelectorAll(".ms-drop.open").forEach(d=>{if(d!==drop)d.classList.remove("open");});
  drop.classList.toggle("open");
}
// Close dropdowns when clicking outside
document.addEventListener("click",e=>{
  if(!e.target.closest(".ms-wrap")){
    document.querySelectorAll(".ms-drop.open").forEach(d=>d.classList.remove("open"));
  }
});

function _poblarMS(containerId, items, labelId, defaultLabel, onChange){
  const drop=document.getElementById(containerId);
  drop.innerHTML="";
  items.forEach(item=>{
    const lbl=document.createElement("label");
    const cb=document.createElement("input");
    cb.type="checkbox";cb.value=item;
    cb.addEventListener("change",onChange);
    const txt=document.createTextNode(item);
    lbl.appendChild(cb);lbl.appendChild(txt);
    drop.appendChild(lbl);
  });
}

function _getChecked(containerId){
  return Array.from(document.querySelectorAll("#"+containerId+" input:checked")).map(cb=>cb.value);
}

function _updateMSLabel(containerId, labelId, defaultLabel){
  const checked=_getChecked(containerId);
  const lbl=document.getElementById(labelId);
  if(!checked.length){lbl.textContent=defaultLabel;lbl.innerHTML=defaultLabel;}
  else if(checked.length===1){lbl.textContent=checked[0].substring(0,20);lbl.innerHTML=checked[0].substring(0,20);}
  else{lbl.innerHTML=checked[0].substring(0,14)+'<span class="ms-badge">+'+( checked.length-1)+'</span>';}
}

async function cargarFiltros(){
  try{
    const d=await api("/api/filtros");if(!d)return;
    // Marca (single select)
    const selMarca=document.getElementById("filtro-marca");
    (d.marcas||[]).forEach(m=>{const o=document.createElement("option");o.value=m;o.textContent=m;selMarca.appendChild(o);});
    selMarca.addEventListener("change",async()=>{
      _filtroMarca=selMarca.value;
      // Cascading: reload productos filtered by marca
      await _recargarProductos();
      aplicarFiltros();
    });
    // Canal (single select)
    const selCanal=document.getElementById("filtro-canal");
    (d.canales||[]).forEach(c=>{const o=document.createElement("option");o.value=c;o.textContent=c==="DISTRIBUCION DIFARE"?"Distribución":c==="FARMACIAS"?"Farmacias":c;selCanal.appendChild(o);});
    selCanal.addEventListener("change",()=>{_filtroCanal=selCanal.value;aplicarFiltros();});
    // Grupo PDV (multi-select) — auto-selecciona canal Farmacias
    _poblarMS("ms-grupo-drop",d.grupos||[],"ms-grupo-label","Todos los grupos",()=>{
      _filtroGrupos=_getChecked("ms-grupo-drop");
      _updateMSLabel("ms-grupo-drop","ms-grupo-label","Todos los grupos");
      // Grupos son exclusivos de Farmacias → forzar canal
      if(_filtroGrupos.length&&_filtroCanal!=="FARMACIAS"){
        _filtroCanal="FARMACIAS";
        document.getElementById("filtro-canal").value="FARMACIAS";
      }
      // Si deseleccionan todos los grupos, liberar canal
      if(!_filtroGrupos.length&&_filtroCanal==="FARMACIAS"){
        _filtroCanal="";
        document.getElementById("filtro-canal").value="";
      }
      aplicarFiltros();
    });
    // Producto (multi-select)
    _poblarMS("ms-producto-drop",d.productos||[],"ms-producto-label","Todos los productos",()=>{
      _filtroProductos=_getChecked("ms-producto-drop");
      _updateMSLabel("ms-producto-drop","ms-producto-label","Todos los productos");
      aplicarFiltros();
    });
  }catch(e){console.warn("filtros:",e);}
}

async function _recargarProductos(){
  try{
    const url="/api/filtros"+(_filtroMarca?"?marca="+encodeURIComponent(_filtroMarca):"");
    const d=await api(url);if(!d)return;
    _filtroProductos=[];
    _poblarMS("ms-producto-drop",d.productos||[],"ms-producto-label","Todos los productos",()=>{
      _filtroProductos=_getChecked("ms-producto-drop");
      _updateMSLabel("ms-producto-drop","ms-producto-label","Todos los productos");
      aplicarFiltros();
    });
    _updateMSLabel("ms-producto-drop","ms-producto-label","Todos los productos");
  }catch(e){console.warn("recargar productos:",e);}
}

function resetFiltros(){
  _filtroMarca="";_filtroCanal="";_filtroGrupos=[];_filtroProductos=[];
  document.getElementById("filtro-marca").value="";
  document.getElementById("filtro-canal").value="";
  // Uncheck all multi-selects
  document.querySelectorAll("#ms-grupo-drop input, #ms-producto-drop input").forEach(cb=>cb.checked=false);
  _updateMSLabel("ms-grupo-drop","ms-grupo-label","Todos los grupos");
  _updateMSLabel("ms-producto-drop","ms-producto-label","Todos los productos");
  document.getElementById("filtro-reset").style.display="none";
  document.getElementById("filtro-label").textContent="";
  // Reload full product list (remove marca cascade)
  _recargarProductos();
  aplicarFiltros();
}

function _qs(){
  const params=[];
  if(_filtroMarca)params.push("marca="+encodeURIComponent(_filtroMarca));
  if(_filtroCanal)params.push("canal="+encodeURIComponent(_filtroCanal));
  _filtroGrupos.forEach(g=>params.push("grupo="+encodeURIComponent(g)));
  _filtroProductos.forEach(p=>params.push("producto="+encodeURIComponent(p)));
  return params.length?"?"+params.join("&"):"";
}

// Debounce: espera 300ms después del último cambio de filtro antes de llamar APIs
let _filtroTimer=null;
let _filtroLoading=false;
function aplicarFiltros(){
  // Actualizar UI inmediatamente (labels, botón limpiar)
  const btn=document.getElementById("filtro-reset");
  const lbl=document.getElementById("filtro-label");
  const hayFiltro=_filtroMarca||_filtroCanal||_filtroGrupos.length||_filtroProductos.length;
  if(hayFiltro){
    btn.style.display="inline-block";
    const parts=[];
    if(_filtroMarca)parts.push(_filtroMarca);
    if(_filtroCanal)parts.push(_filtroCanal==="DISTRIBUCION DIFARE"?"Distribución":_filtroCanal);
    if(_filtroGrupos.length)parts.push(_filtroGrupos.length+" grupo(s)");
    if(_filtroProductos.length)parts.push(_filtroProductos.length+" producto(s)");
    lbl.textContent="Mostrando: "+parts.join(" · ");
  }else{btn.style.display="none";lbl.textContent="";}
  // Sync dist filter too
  const distSel=document.getElementById("dist-marca-filter");
  if(distSel){distSel.value=_filtroMarca||"";}
  // Debounce: si hay un timer pendiente, cancelarlo
  if(_filtroTimer)clearTimeout(_filtroTimer);
  _filtroTimer=setTimeout(()=>_ejecutarFiltros(),300);
}
async function _ejecutarFiltros(){
  if(_filtroLoading)return; // evitar requests simultáneos
  _filtroLoading=true;
  try{
    await Promise.all([cargarKPIs(),cargarChart(),cargarTP(),cargarDist()]);
  }finally{_filtroLoading=false;}
}

// ── Funciones de carga individuales (reutilizables con filtros) ──
async function cargarKPIs(){
  try{
    const d=await api("/api/kpis"+_qs()); if(!d) return;
    if(d.error){mostrarError(d.error);return;}
    document.getElementById("kpi-total").textContent=fmtUSD(d.venta_total);
    const vtot=d.venta_total||1;
    const pFarm=Math.round((d.venta_farmacias||0)/vtot*100);
    const pDist=Math.round((d.venta_distribucion||0)/vtot*100);
    document.getElementById("kpi-farm").textContent=fmtUSD(d.venta_farmacias);
    document.getElementById("kpi-farm-pct").textContent=`(${pFarm}%)`;
    document.getElementById("kpi-dist").textContent=fmtUSD(d.venta_distribucion);
    document.getElementById("kpi-dist-pct").textContent=`(${pDist}%)`;
    document.getElementById("kpi-univ").textContent=(d.universo_pdv||0).toLocaleString("es-EC");
    const periodo=d.mes_completo?`Mes completo · día ${d.ultimo_dia_venta}/${d.dias_mes}`:`Día ${d.ultimo_dia_venta}/${d.dias_mes}`;
    document.getElementById("kpi-periodo").textContent=periodo;
    document.getElementById("kpi-total-sub").textContent=`Data hasta día ${d.ultimo_dia_venta} de abril`;
  }catch(e){mostrarError(e.message||e);}
}

let _canalChart=null;
async function cargarChart(){
  try{
    const d=await api("/api/venta-canal-mes"+_qs()); if(!d) return;
    if(d.error){mostrarError(d.error);return;}
    const filas=d.filas||[];
    if(!filas.length) return;
    const nombresMes=["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"];
    const labels=filas.map(f=>{
      if(f.proyectado) return (nombresMes[f.mes-1]||("M"+f.mes))+" (proy)";
      return nombresMes[f.mes-1]||("M"+f.mes);
    });
    const actual=filas.find(f=>f.proyectado);
    if(actual){
      const sub=document.getElementById("chart-canal-sub");
      if(sub){
        const proyTot=fmtUSD(actual.total_proy||0);
        sub.textContent=`Proyección ${nombresMes[actual.mes-1]}: ${proyTot} (día ${actual.ultimo_dia}/${actual.dias_mes})`;
      }
    }
    // Determinar qué datasets mostrar según filtro de canal
    const soloFarm=_filtroCanal==="FARMACIAS";
    const soloDist=_filtroCanal==="DISTRIBUCION DIFARE";
    const ambos=!soloFarm&&!soloDist;
    const datasets=[];
    if(ambos||soloFarm){
      datasets.push({label:"Farmacias",data:filas.map(f=>f.farmacias),backgroundColor:"#2563eb",borderRadius:6,maxBarThickness:48,stack:"farm"});
      const dFarm=filas.map(f=>f.proyectado?(f.farmacias_delta||0):0);
      if(dFarm.some(v=>v>0))datasets.push({label:"Farm. proyectado",data:dFarm,backgroundColor:"rgba(37,99,235,0.35)",borderColor:"#2563eb",borderWidth:1,borderDash:[4,4],borderRadius:6,maxBarThickness:48,stack:"farm"});
    }
    if(ambos||soloDist){
      datasets.push({label:"Distribución",data:filas.map(f=>f.distribucion),backgroundColor:"#10b981",borderRadius:6,maxBarThickness:48,stack:"dist"});
      const dDist=filas.map(f=>f.proyectado?(f.distribucion_delta||0):0);
      if(dDist.some(v=>v>0))datasets.push({label:"Dist. proyectado",data:dDist,backgroundColor:"rgba(16,185,129,0.35)",borderColor:"#10b981",borderWidth:1,borderDash:[4,4],borderRadius:6,maxBarThickness:48,stack:"dist"});
    }
    // Solo mostrar Total cuando ambos canales están visibles (si no, es redundante)
    if(ambos){
      datasets.push({label:"Total",data:filas.map(f=>f.total),backgroundColor:"#f59e0b",borderRadius:6,maxBarThickness:48,stack:"tot"});
      const dTot=filas.map(f=>f.proyectado?(f.total_delta||0):0);
      if(dTot.some(v=>v>0))datasets.push({label:"Total proyectado",data:dTot,backgroundColor:"rgba(245,158,11,0.35)",borderColor:"#f59e0b",borderWidth:1,borderDash:[4,4],borderRadius:6,maxBarThickness:48,stack:"tot"});
    }
    if(_canalChart){_canalChart.destroy();}
    _canalChart=new Chart(document.getElementById("chartCanalMes"),{
      type:"bar",
      data:{labels,datasets},
      options:{responsive:true,maintainAspectRatio:false,
        plugins:{
          legend:{position:"bottom",labels:{boxWidth:12,font:{size:12},padding:12,filter:(it)=>!it.text.includes("proyectado")}},
          tooltip:{callbacks:{label:ctx=>ctx.dataset.label+": "+fmtUSD(ctx.parsed.y)}}
        },
        scales:{
          y:{stacked:true,ticks:{callback:v=>fmtShort(v)},grid:{color:"rgba(46,117,182,0.15)"},beginAtZero:true},
          x:{stacked:true,grid:{display:false}}
        }
      }
    });
  }catch(e){mostrarError(e.message||e);}
}

async function cargarTP(){
  try{
    document.getElementById("tp-body").innerHTML='<tr><td colspan="12" class="text-center py-6" style="color:var(--muted)">Cargando…</td></tr>';
    const d=await api("/api/tienda-perfecta"+_qs()); if(!d) return;
    if(d.error){mostrarError(d.error);return;}
    const filas=d.filas||[];
    const body=document.getElementById("tp-body");
    if(!filas.length){body.innerHTML='<tr><td colspan="12" class="text-center py-6" style="color:var(--muted)">Sin datos</td></tr>';return;}
    body.innerHTML=filas.map(f=>{
      const v=f.VENTA||f.venta_total||0;
      const pct=(f.PCT||0).toFixed(1);
      const acum=(f.PCT_ACUM||0).toFixed(1);
      const uni=f.UNIVERSO_PDV||0;
      const pres=f.PDV_PRESENCIA||0;
      const cob=f.cobertura_pct||0;
      const s0=f.stock_solo_0||0;
      const s1=f.stock_solo_1||0;
      const s2=f.stock_solo_2||0;
      const s3=f.stock_solo_3||0;
      return `<tr class="row" style="border-bottom:1px solid var(--border)">
        <td class="px-2 py-1.5 font-medium sticky-col sticky-col-1" style="color:var(--gold)">${f.MARCA||"—"}</td>
        <td class="px-2 py-1.5 sticky-col sticky-col-2" style="color:var(--white)">${(f.PRODUCTO||"—").substring(0,30)}</td>
        <td class="px-2 py-1.5 text-right font-medium" style="color:var(--gold)">${fmtUSD(v)}</td>
        <td class="px-2 py-1.5 text-right" style="color:var(--muted)">${pct}%</td>
        <td class="px-2 py-1.5 text-right" style="color:var(--muted)">${acum}%</td>
        <td class="px-2 py-1.5 text-center" style="color:var(--muted)">${uni}</td>
        <td class="px-2 py-1.5 text-center" style="color:var(--white)">${pres}</td>
        <td class="px-2 py-1.5 text-center" style="color:${cob>=90?'#10b981':cob>=70?'#f59e0b':'#ef4444'}">${cob}%</td>
        <td class="px-2 py-1.5 text-center font-bold" style="color:#ef4444">${s0||""}</td>
        <td class="px-2 py-1.5 text-center" style="color:#f59e0b">${s1||""}</td>
        <td class="px-2 py-1.5 text-center" style="color:#3b82f6">${s2||""}</td>
        <td class="px-2 py-1.5 text-center" style="color:#8b5cf6">${s3||""}</td>
      </tr>`;
    }).join("");
  }catch(e){mostrarError(e.message||e);}
}

// Distribución Numérica — carga reutilizable
let _distFiltrosInit=false;
async function cargarDist(){
  try{
    const marca=_filtroMarca||document.getElementById("dist-marca-filter").value||"";
    const d=await api("/api/dist-numerica-chart"+(marca?"?marca="+encodeURIComponent(marca):"")); if(!d) return;
    if(d.error){return;}
    // Poblar select de marcas solo la primera vez
    if(!_distFiltrosInit){
      const sel=document.getElementById("dist-marca-filter");
      (d.marcas_disponibles||[]).forEach(m=>{
        const o=document.createElement("option");o.value=m;o.textContent=m;sel.appendChild(o);
      });
      sel.addEventListener("change",async()=>{
        // Si el usuario cambia el filtro local de dist, usarlo
        const d2=await api("/api/dist-numerica-chart?marca="+encodeURIComponent(sel.value));
        if(d2) renderDistChart(d2);
      });
      _distFiltrosInit=true;
    }
    renderDistChart(d);
  }catch(e){console.warn("dist:",e);}
}

// ── Cargar todo el dashboard ──
(async()=>{
  await waitForReady();
  const overlay=document.getElementById("loading-overlay");
  const sub=document.getElementById("loading-sub");
  if(sub)sub.textContent="Cargando dashboard...";
  try{
    await cargarFiltros();
    await Promise.all([cargarKPIs(),cargarChart(),cargarTP(),cargarDist()]);
  }finally{
    if(overlay)overlay.style.display="none";
  }
})(); // fin de la función principal de carga

// Distribución Numérica chart renderer
let _distChart=null;
function renderDistChart(d){
  document.getElementById("dist-total").textContent=(d.total_clientes||0).toLocaleString("es-EC");
  const sub=d.marca_filtro?`Clientes que compraron ${d.marca_filtro}`:"Clientes únicos (RUC) histórico";
  document.getElementById("dist-sub").textContent=sub;
  const meses=d.resumen_meses||[];
  if(!meses.length)return;
  const nombresMes=["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"];
  const labels=meses.map(m=>{
    const parts=m.mes.split("-");
    return nombresMes[parseInt(parts[1])-1]||m.mes;
  });
  const vals=meses.map(m=>m.clientes_atendidos);
  if(_distChart){_distChart.destroy();}
  _distChart=new Chart(document.getElementById("chartDistNumerica"),{
    type:"bar",
    data:{labels,datasets:[
      {label:"Clientes impactados",data:vals,backgroundColor:"#2563eb",borderRadius:6,maxBarThickness:48}
    ]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{
          label:ctx=>`${ctx.parsed.y} clientes`,
          afterLabel:ctx=>{
            const m=meses[ctx.dataIndex];
            if(!m)return"";
            const parts=[];
            if(m.clientes_nuevos)parts.push(`+${m.clientes_nuevos} nuevos`);
            if(m.clientes_perdidos)parts.push(`-${m.clientes_perdidos} perdidos`);
            return parts.join(" · ");
          }
        }}
      },
      scales:{
        y:{beginAtZero:true,ticks:{callback:v=>v},grid:{color:"rgba(46,117,182,0.15)"}},
        x:{grid:{display:false}}
      }
    }
  });
}

// ════════════════════════════════════════════════
// CHAT GERENCIAL
// ════════════════════════════════════════════════
const chatMsgs=document.getElementById("chat-msgs");
const chatInput=document.getElementById("chat-input");
const chatSendBtn=document.getElementById("chat-send");
let chatHistorial=[];

chatInput.addEventListener("input",()=>{
  chatSendBtn.disabled=!chatInput.value.trim();
  chatInput.style.height="auto";
  chatInput.style.height=Math.min(chatInput.scrollHeight,120)+"px";
});
chatInput.addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();chatSendInput();}
});

function chatSendInput(){
  const t=chatInput.value.trim();
  if(!t)return;
  chatInput.value="";chatInput.style.height="auto";chatSendBtn.disabled=true;
  chatSend(t);
}

function chatClear(){
  chatMsgs.innerHTML="";chatHistorial=[];
}

function addChatBubble(html,role){
  const d=document.createElement("div");
  d.className=role==="user"?"chat-bubble-user":"chat-bubble-bot";
  d.innerHTML=html;
  chatMsgs.appendChild(d);
  chatMsgs.scrollTop=chatMsgs.scrollHeight;
  return d;
}

function renderMarkdown(txt){
  let h=txt.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const lines=h.split("\n");
  let inTable=false;
  const out=[];
  for(let i=0;i<lines.length;i++){
    const l=lines[i].trim();
    // Skip ALL empty lines — spacing is handled by CSS margins on blocks
    if(l==="") continue;
    // HR dividers
    if(/^-{3,}$/.test(l)){out.push('<hr style="border-color:rgba(46,117,182,0.3);margin:8px 0">');continue;}
    // Table rows
    if(l.startsWith("|")&&l.endsWith("|")){
      if(!inTable){out.push('<div class="overflow-x-auto" style="margin:6px 0"><table class="text-xs border-collapse w-full">');inTable=true;}
      if(/^\|[\s\-:|]+\|$/.test(l))continue;
      const cells=l.split("|").filter(c=>c!=="").map(c=>c.trim());
      if(i>0&&lines[i-1]&&/^\|[\s\-:|]+\|$/.test(lines[i-1].trim())){
        out.push("<tr>"+cells.map(c=>`<td>${c}</td>`).join("")+"</tr>");
      } else if(inTable && out[out.length-1].includes("<table")){
        out.push("<thead><tr>"+cells.map(c=>`<th>${c}</th>`).join("")+"</tr></thead><tbody>");
      } else {
        out.push("<tr>"+cells.map(c=>`<td>${c}</td>`).join("")+"</tr>");
      }
    } else {
      if(inTable){out.push("</tbody></table></div>");inTable=false;}
      // Headers ## and ###
      if(l.startsWith("## ")){out.push('<div style="font-size:14px;font-weight:700;color:#C9A84C;margin:10px 0 4px">'+l.slice(3)+'</div>');continue;}
      if(l.startsWith("### ")){out.push('<div style="font-size:13px;font-weight:600;color:#C9A84C;margin:8px 0 3px">'+l.slice(4)+'</div>');continue;}
      out.push(l);
    }
  }
  if(inTable) out.push("</tbody></table></div>");
  h=out.join("\n");
  // Bold, italic, code
  h=h.replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>");
  h=h.replace(/\*(.+?)\*/g,"<em>$1</em>");
  h=h.replace(/`(.+?)`/g,'<code>$1</code>');
  // Line breaks — but not adjacent to block elements
  h=h.replace(/\n/g,"<br>");
  h=h.replace(/<br>(<div|<hr|<table)/gi,"$1");
  h=h.replace(/(<\/div>|<\/table>)<br>/gi,"$1");
  h=h.replace(/(<br\s*\/?>){2,}/gi,"<br>");
  return h;
}

async function chatSend(pregunta){
  addChatBubble(pregunta.replace(/</g,"&lt;"),"user");
  chatHistorial.push({role:"user",content:pregunta});

  // Thinking indicator
  const thinking=addChatBubble('<div style="display:flex;align-items:center;gap:8px;color:#C9A84C"><svg style="animation:spin .8s linear infinite" width="16" height="16" viewBox="0 0 24 24"><circle opacity="0.25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path opacity="0.75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg> Analizando datos...</div>',"bot");

  try{
    const r=await fetch(S+"/api/chat-gerencial",{
      method:"POST",
      headers:AH,
      body:JSON.stringify({pregunta,historial:chatHistorial.slice(-10)})
    });
    if(r.status===401){logout();return;}
    const d=await r.json();
    thinking.remove();
    if(d.error){
      addChatBubble('<span class="text-red-500">Error: '+d.error+'</span>',"bot");
      return;
    }
    const html=renderMarkdown(d.respuesta||"Sin respuesta");
    // Si hay archivos para descargar
    let filesHtml="";
    if(d.archivos&&d.archivos.length){
      filesHtml='<div class="mt-3 flex flex-wrap gap-2">'+d.archivos.map(f=>
        `<a href="${S}/api/descargar/${encodeURIComponent(f)}?token=${encodeURIComponent(TK)}" class="download-link" target="_blank">
          <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
          ${f}</a>`
      ).join("")+"</div>";
    }
    addChatBubble(html+filesHtml,"bot");
    chatHistorial.push({role:"assistant",content:d.respuesta});
  }catch(e){
    thinking.remove();
    addChatBubble('<span class="text-red-500">Error de conexión: '+e.message+'</span>',"bot");
  }
}

// ════════════════════════════════════════════════
// EXPORTAR PDF
// ════════════════════════════════════════════════
async function exportarPDFScreenshot(){
  const main=document.querySelector("main");
  const chat=document.getElementById("chat-section");
  const fab=document.getElementById("fab-btn");
  // Ocultar chat y fab temporalmente
  if(chat)chat.style.display="none";
  if(fab)fab.style.display="none";
  try{
    const canvas=await html2canvas(main,{backgroundColor:"#0a1628",scale:2,useCORS:true,logging:false});
    const link=document.createElement("a");
    link.download="dashboard_nexus_"+new Date().toISOString().slice(0,10)+".png";
    link.href=canvas.toDataURL("image/png");
    link.click();
  }catch(e){alert("Error al capturar: "+e.message);}
  finally{if(chat)chat.style.display="";if(fab)fab.style.display="";}
}

async function exportarPDFReporte(){
  const btn=event.target;
  const orig=btn.innerHTML;
  btn.innerHTML="Generando…";btn.disabled=true;
  try{
    const r=await fetch(S+"/api/reporte-pdf?token="+encodeURIComponent(TK));
    if(!r.ok){const j=await r.json().catch(()=>({}));alert(j.error||"Error");return;}
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download=r.headers.get("content-disposition")?.match(/filename="?(.+?)"?$/)?.[1]||"reporte_nexus.pdf";
    document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
  }catch(e){alert("Error: "+e.message);}
  finally{btn.innerHTML=orig;btn.disabled=false;}
}

// Draggable FAB for mobile
(function(){
  const fab=document.getElementById("fab-btn");if(!fab)return;
  let sx,sy,ox,oy,dragging=false;
  fab.addEventListener("touchstart",e=>{
    const t=e.touches[0];
    sx=t.clientX;sy=t.clientY;
    const r=fab.getBoundingClientRect();
    ox=t.clientX-r.left;oy=t.clientY-r.top;
    dragging=false;
  },{passive:true});
  fab.addEventListener("touchmove",e=>{
    const t=e.touches[0];
    if(Math.abs(t.clientX-sx)>8||Math.abs(t.clientY-sy)>8) dragging=true;
    if(!dragging)return;
    e.preventDefault();
    fab.style.position="fixed";
    fab.style.left=(t.clientX-ox)+"px";
    fab.style.top=(t.clientY-oy)+"px";
    fab.style.right="auto";fab.style.bottom="auto";
  },{passive:false});
  fab.addEventListener("touchend",e=>{
    if(dragging){e.preventDefault();}
  });
  fab.addEventListener("click",e=>{
    if(dragging){e.preventDefault();dragging=false;}
  });
})();
</script>
</body>
</html>"""


@app.route("/dashboard")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ── Flag de pre-warm completado ──
_data_ready = False

@app.route("/api/ready")
def api_ready():
    """Health-check: ¿ya terminó el pre-warm de data?"""
    return jsonify({"ready": _data_ready})


# ══════════════════════════════════════════════════════════════
# Blueprint Gerencial v2 (dashboard) — Día 3
# ══════════════════════════════════════════════════════════════
try:
    from agente.api_gerencial import bp as bp_gerencial, set_jwt_verifier, set_anthropic_client
    set_jwt_verifier(verificar_jwt, ROLES)
    set_anthropic_client(get_anthropic_client)
    app.register_blueprint(bp_gerencial)
    print("[v2] Blueprint gerencial registrado en /api/*")

    # Pre-calentar cache de Excels en background para que el primer request al dashboard
    # no tenga que esperar 20-60s de pandas leyendo los archivos.
    import threading
    def _prewarm():
        global _data_ready
        try:
            # 1) Si los Excels son más nuevos que data.db, regenerar antes de cachear
            if _excels_mas_nuevos_que_db():
                _regenerar_data_db()
            else:
                print("[v2] data.db al día con excels/, no hace falta regenerar")
            # 2) Pre-cargar cache del dashboard (pandas sobre excels/)
            from agente import analitica
            print("[v2] Pre-cargando data de Excels en background…")
            analitica.cargar_data()
            _data_ready = True
            print("[v2] Data cacheada OK, dashboard listo para servir rápido")
        except Exception as e:
            _data_ready = True  # marcar como ready igualmente para no bloquear para siempre
            print(f"[v2] Pre-warm falló (se cargará en la primera petición): {e}")
    threading.Thread(target=_prewarm, daemon=True).start()
except Exception as e:
    _data_ready = True  # sin blueprint, no hay pre-warm que esperar
    print(f"[v2] Blueprint gerencial NO registrado: {e}")


if __name__ == "__main__":
    print("=" * 50)
    print("DIFARE NEXUS v3")
    print("=" * 50)
    try:
        v = query_val("SELECT COUNT(*) FROM ventas")
        s = query_val("SELECT COUNT(*) FROM sap")
        print(f"DB: {v:,} ventas + {s:,} SAP")
    except Exception as e:
        print(f"DB Error: {e}")
    print(f"Servidor: http://0.0.0.0:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


# ══════════════════════════════════════════════════════════════
# FRONTEND HTML
# ══════════════════════════════════════════════════════════════

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a1628">
<title>Difare Nexus</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --navy:#0a1628; --navy2:#111f38; --blue:#1B3A6B; --azure:#2E75B6;
  --sky:#60A5FA; --gold:#C9A84C; --gold2:#F0C97A; --white:#F8FAFF;
  --muted:#7a8fbb; --border:rgba(46,117,182,0.2); --green:#059669; --red:#DC2626;
}
*{margin:0;padding:0;box-sizing:border-box;}
html,body{height:100%;-webkit-tap-highlight-color:transparent;}
body{background:var(--navy);color:var(--white);font-family:'DM Sans',sans-serif;display:flex;flex-direction:column;}

.login-screen{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;padding:24px;text-align:center;}
.login-logo{font-family:'Playfair Display',serif;font-size:2rem;font-weight:900;color:var(--gold);margin-bottom:4px;}
.login-sub{font-size:12px;color:var(--muted);margin-bottom:32px;}
.login-form{width:100%;max-width:320px;display:flex;flex-direction:column;gap:12px;}
.login-input{width:100%;background:rgba(255,255,255,0.06);border:1px solid var(--border);border-radius:12px;padding:13px 16px;font-size:15px;color:var(--white);font-family:'DM Sans',sans-serif;outline:none;transition:border 0.2s;}
.login-input:focus{border-color:var(--azure);}
.login-input::placeholder{color:var(--muted);}
.login-btn{width:100%;padding:14px;background:linear-gradient(135deg,var(--gold),var(--gold2));border:none;border-radius:12px;font-size:15px;font-weight:700;color:var(--navy);cursor:pointer;font-family:'DM Sans',sans-serif;transition:transform 0.2s;}
.login-btn:hover{transform:scale(1.02);}
.login-error{font-size:12px;color:var(--red);min-height:18px;}

.header{background:var(--navy2);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
.logo-icon{width:40px;height:40px;background:linear-gradient(135deg,var(--gold),var(--gold2));border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:900;color:var(--navy);}
.logo-name{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:700;color:var(--gold);}
.logo-sub{font-size:11px;color:var(--muted);}
.header-left{display:flex;align-items:center;gap:12px;}
.header-right{display:flex;align-items:center;gap:10px;}
.status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--green);font-weight:500;}
.status-dot{width:7px;height:7px;background:var(--green);border-radius:50%;animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.btn-logout{background:none;border:1px solid var(--border);color:var(--muted);padding:5px 10px;border-radius:8px;font-size:11px;cursor:pointer;font-family:'DM Sans',sans-serif;}
.btn-logout:hover{border-color:var(--red);color:var(--red);}

.content{flex:1;overflow-y:auto;padding:16px;-webkit-overflow-scrolling:touch;}
.content::-webkit-scrollbar{width:4px;}
.content::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px;}

.panel{animation:slideUp 0.3s ease;}
@keyframes slideUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.panel-title{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:700;color:var(--gold);margin-bottom:6px;}
.panel-sub{font-size:12px;color:var(--muted);margin-bottom:16px;}
.btn-back{background:none;border:1px solid var(--border);color:var(--muted);padding:6px 14px;border-radius:8px;font-size:12px;cursor:pointer;margin-bottom:16px;font-family:'DM Sans',sans-serif;transition:all 0.2s;}
.btn-back:hover{border-color:var(--azure);color:var(--sky);}

.grupos-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;}
.grupo-card{background:var(--navy2);border:1px solid var(--border);border-radius:12px;padding:14px;cursor:pointer;transition:all 0.2s;text-align:left;}
.grupo-card:hover,.grupo-card:active{border-color:var(--azure);background:rgba(46,117,182,0.15);}
.grupo-nombre{font-weight:600;font-size:13px;margin-bottom:3px;}
.grupo-stats{font-size:11px;color:var(--muted);}

.search-wrap{position:relative;margin-bottom:12px;}
.search-input{width:100%;background:rgba(255,255,255,0.05);border:1px solid var(--border);border-radius:12px;padding:10px 16px 10px 38px;font-size:14px;color:var(--white);font-family:'DM Sans',sans-serif;outline:none;}
.search-input:focus{border-color:var(--azure);}
.search-input::placeholder{color:var(--muted);}
.search-icon{position:absolute;left:12px;top:50%;transform:translateY(-50%);font-size:16px;pointer-events:none;}

.farm-list{display:flex;flex-direction:column;gap:6px;}
.farm-item{background:var(--navy2);border:1px solid var(--border);border-radius:10px;padding:11px 14px;cursor:pointer;transition:all 0.2s;display:flex;align-items:center;justify-content:space-between;}
.farm-item:hover,.farm-item:active{border-color:var(--gold);background:rgba(201,168,76,0.08);}
.farm-nombre{font-size:13px;font-weight:500;}
.farm-venta{font-size:12px;color:var(--gold);font-weight:600;}

.chat-context{background:rgba(201,168,76,0.08);border:1px solid rgba(201,168,76,0.25);border-radius:12px;padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;}
.chat-context-name{font-size:13px;font-weight:600;color:var(--gold);}
.chat-context-sub{font-size:11px;color:var(--muted);}
.btn-cambiar{background:none;border:1px solid var(--border);color:var(--muted);padding:5px 12px;border-radius:8px;font-size:11px;cursor:pointer;font-family:'DM Sans',sans-serif;}
.btn-cambiar:hover{border-color:var(--azure);color:var(--sky);}

.messages{display:flex;flex-direction:column;gap:12px;}
.msg{display:flex;gap:10px;align-items:flex-start;animation:slideUp 0.3s ease;}
.msg.user{flex-direction:row-reverse;}
.msg-avatar{width:30px;height:30px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;font-weight:700;color:var(--navy);}
.msg.user .msg-avatar{background:var(--azure);color:white;}
.msg.bot .msg-avatar{background:linear-gradient(135deg,var(--gold),var(--gold2));}
.msg-bubble{max-width:85%;padding:10px 14px;border-radius:4px 14px 14px 14px;font-size:13px;line-height:1.65;}
.msg.user .msg-bubble{background:var(--azure);color:white;border-radius:14px 4px 14px 14px;}
.msg.bot .msg-bubble{background:var(--navy2);border:1px solid var(--border);color:var(--white);}
.msg.bot .msg-bubble strong{color:var(--gold);}
.msg-time{font-size:10px;color:var(--muted);margin-top:3px;text-align:right;}

.typing{display:flex;gap:5px;align-items:center;padding:10px 14px;}
.typing span{width:7px;height:7px;background:var(--muted);border-radius:50%;animation:typing 1.2s infinite;}
.typing span:nth-child(2){animation-delay:0.2s;}
.typing span:nth-child(3){animation-delay:0.4s;}
@keyframes typing{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-8px)}}

.quick-btns{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;}
.quick-btn{background:var(--navy2);border:1px solid var(--border);border-radius:100px;padding:5px 12px;font-size:11px;color:var(--muted);cursor:pointer;transition:all 0.2s;font-family:'DM Sans',sans-serif;}
.quick-btn:hover,.quick-btn:active{border-color:var(--azure);color:var(--sky);}

.input-area{background:var(--navy2);border-top:1px solid var(--border);padding:12px 16px;flex-shrink:0;}
.input-row{display:flex;gap:8px;align-items:flex-end;}
.input-box{flex:1;background:rgba(255,255,255,0.05);border:1px solid var(--border);border-radius:12px;padding:10px 14px;font-size:14px;color:var(--white);font-family:'DM Sans',sans-serif;resize:none;outline:none;max-height:80px;line-height:1.5;}
.input-box:focus{border-color:var(--azure);}
.input-box::placeholder{color:var(--muted);}
.send-btn{width:42px;height:42px;background:linear-gradient(135deg,var(--gold),var(--gold2));border:none;border-radius:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;}
.send-btn:disabled{opacity:0.4;cursor:not-allowed;}
.loading{text-align:center;padding:20px;color:var(--muted);font-size:13px;}
#appScreen{display:none;}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.8.2/jspdf.plugin.autotable.min.js"></script>
</head>
<body>

<div id="loginScreen" class="login-screen">
  <div class="login-logo">Difare Nexus</div>
  <div class="login-sub">Asistente Comercial IA</div>
  <div class="login-form">
    <input class="login-input" id="loginUser" placeholder="Usuario" autocomplete="username" autocapitalize="none">
    <input class="login-input" id="loginPass" type="password" placeholder="Contrasena" autocomplete="current-password">
    <button class="login-btn" onclick="hacerLogin()">Iniciar Sesion</button>
    <div class="login-error" id="loginError"></div>
  </div>
</div>

<div id="appScreen">
  <div class="header">
    <div class="header-left">
      <div class="logo-icon">N</div>
      <div><div class="logo-name">Difare Nexus</div><div class="logo-sub">Asistente Comercial</div></div>
    </div>
    <div class="header-right">
      <div class="status"><div class="status-dot"></div><span id="userLabel">-</span></div>
      <button class="btn-logout" onclick="cerrarSesion()">Salir</button>
    </div>
  </div>
  <div class="content" id="content"><div class="loading">Cargando...</div></div>
  <div class="input-area" id="inputArea" style="display:none;">
    <div class="input-row">
      <textarea class="input-box" id="inputBox" placeholder="Pregunta sobre esta farmacia..." rows="1"
        onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
      <button class="send-btn" id="sendBtn" onclick="enviarChat()">&#x27A4;</button>
    </div>
  </div>
</div>

<script>
const S=window.location.origin;
let TK=localStorage.getItem("nx_tk")||null, US=localStorage.getItem("nx_us")||null;
let RL=localStorage.getItem("nx_rl")||null;
let posActual=null, esperando=false;

function AH(){return{"Content-Type":"application/json","Authorization":"Bearer "+TK}}

function routePorRol(){
  // Si viene con ?modo=campo, no redirigir al dashboard — mostrar vista campo
  if(new URLSearchParams(window.location.search).get("modo")==="campo") return false;
  if(RL==="admin"||RL==="gerencial"){window.location.href="/dashboard";return true;}
  return false;
}

window.addEventListener("DOMContentLoaded",async()=>{
  if(TK){try{const r=await fetch(S+"/verificar_token",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:TK})});const d=await r.json();if(d.valido){if(routePorRol())return;entrarApp();return;}}catch(e){}localStorage.removeItem("nx_tk");localStorage.removeItem("nx_us");localStorage.removeItem("nx_rl");TK=null;RL=null;}
  document.getElementById("loginScreen").style.display="flex";
});

async function hacerLogin(){
  const u=document.getElementById("loginUser").value.trim();
  const p=document.getElementById("loginPass").value.trim();
  const err=document.getElementById("loginError");err.textContent="";
  if(!u||!p){err.textContent="Ingresa usuario y contrasena";return;}
  try{const r=await fetch(S+"/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({usuario:u,contrasena:p})});const d=await r.json();
    if(d.exito){TK=d.token;US=d.usuario;RL=d.rol||"campo";localStorage.setItem("nx_tk",TK);localStorage.setItem("nx_us",US);localStorage.setItem("nx_rl",RL);if(routePorRol())return;entrarApp();}
    else{err.textContent=d.error||"Error";}
  }catch(e){err.textContent="No se pudo conectar";}
}
document.addEventListener("keydown",e=>{if(e.key==="Enter"&&document.getElementById("loginScreen").style.display!=="none")hacerLogin();});

function entrarApp(){
  document.getElementById("loginScreen").style.display="none";
  const a=document.getElementById("appScreen");a.style.display="flex";a.style.flexDirection="column";a.style.height="100vh";
  document.getElementById("userLabel").textContent=US;mostrarGrupos();
}
function cerrarSesion(){
  fetch(S+"/logout",{method:"POST",headers:AH(),body:"{}"}).catch(()=>{});
  localStorage.removeItem("nx_tk");localStorage.removeItem("nx_us");localStorage.removeItem("nx_rl");TK=null;US=null;RL=null;
  document.getElementById("appScreen").style.display="none";
  document.getElementById("loginScreen").style.display="flex";
  document.getElementById("loginUser").value="";document.getElementById("loginPass").value="";
}

async function mostrarGrupos(){
  posActual=null;document.getElementById("inputArea").style.display="none";
  const c=document.getElementById("content");c.innerHTML='<div class="loading">Cargando grupos...</div>';
  try{const r=await fetch(S+"/grupos",{headers:AH()});if(r.status===401){cerrarSesion();return;}
    const g=await r.json();const ic={"Cruz Azul Mostrador":"&#x1F3EA;","Cruz Azul Autoservicio":"&#x1F6D2;","Pharmacys":"&#x1F48A;","Dromayor":"&#x1F3EC;","Bodegas Internas Privadas":"&#x1F4E6;"};
    c.innerHTML='<div class="panel"><div class="panel-title">Selecciona el grupo de farmacias</div><div class="panel-sub">Ordenados por venta Q1 2026</div><div class="grupos-grid">'+
      g.map(x=>'<div class="grupo-card" onclick="mostrarFarmacias(\''+encodeURIComponent(x.grupo)+"','"+x.grupo.replace(/'/g,"\\'")+"')\">"+'<div style="font-size:20px;margin-bottom:6px">'+(ic[x.grupo]||"&#x1F3EA;")+'</div><div class="grupo-nombre">'+x.grupo+'</div><div class="grupo-stats">'+x.total_pos+' farmacias &middot; $'+(x.ventas/1000).toFixed(0)+'K</div></div>').join("")+'</div></div>';
  }catch(e){c.innerHTML='<div class="loading">No se pudo conectar al servidor.</div>';}
}

async function mostrarFarmacias(ge,gn){
  const c=document.getElementById("content");c.innerHTML='<div class="loading">Cargando farmacias...</div>';
  try{const r=await fetch(S+"/farmacias?grupo="+ge,{headers:AH()});if(r.status===401){cerrarSesion();return;}
    const f=await r.json();window._f=f;
    c.innerHTML='<div class="panel"><button class="btn-back" onclick="mostrarGrupos()">&#8592; Cambiar grupo</button><div class="panel-title">Farmacias de '+gn+'</div><div class="panel-sub">'+f.length+' farmacias</div><div class="search-wrap"><span class="search-icon">&#128269;</span><input class="search-input" id="si" placeholder="Buscar farmacia..." oninput="filtF(this.value)"></div><div class="farm-list" id="fl">'+renF(f)+'</div></div>';
  }catch(e){c.innerHTML='<div class="loading">Error al cargar.</div>';}
}
function renF(f){return f.slice(0,40).map(x=>'<div class="farm-item" onclick="selPos(\''+x.pos.replace(/'/g,"\\'")+"')\">"+'<div class="farm-nombre">'+x.pos+'</div><div class="farm-venta">$'+Math.round(x.ventas).toLocaleString("es-EC")+'</div></div>').join("");}
function filtF(t){if(!window._f)return;document.getElementById("fl").innerHTML=renF(window._f.filter(x=>x.pos.toLowerCase().includes(t.toLowerCase())));}

async function selPos(pos){
  posActual=pos;const c=document.getElementById("content");c.innerHTML='<div class="loading">Cargando datos...</div>';
  try{const r=await fetch(S+"/detalle_pos",{method:"POST",headers:AH(),body:JSON.stringify({pos})});
    if(r.status===401){cerrarSesion();return;}const d=await r.json();window._detalle=d;
    const si=d.stock_info||{},ss=(si.sin_stock||[]).slice(0,3),bs=(si.bajo_stock||[]).slice(0,3);
    c.innerHTML='<div class="panel"><div id="sb" style="position:sticky;top:0;z-index:10;background:var(--navy);padding-bottom:10px;"><div class="chat-context"><div><div class="chat-context-name">'+pos+'</div><div class="chat-context-sub">'+d.grupo_pdv+' &middot; $'+d.venta_total.toLocaleString("es-EC")+' Q1 &middot; '+d.pct_del_total+'%</div></div><button class="btn-cambiar" onclick="mostrarGrupos()">Cambiar</button></div><div class="quick-btns"><button class="quick-btn" onclick="mostrarGrupos()" style="border-color:rgba(201,168,76,0.3);color:var(--gold)">Inicio</button><button class="quick-btn" onclick="showTendencia()">Tendencia</button><button class="quick-btn" onclick="qr(\'Que productos debo ofrecer hoy\')">Que ofrecer</button><button class="quick-btn" onclick="qr(\'Oportunidad de crecimiento\')">Oportunidad</button><button class="quick-btn" onclick="showStock()">Stock</button></div></div><div class="messages" id="msgs"><div class="msg bot"><div class="msg-avatar">N</div><div><div class="msg-bubble">Hola! Estoy listo para ayudarte con <strong>'+pos+'</strong>.<br><br>Venta Q1: <strong>$'+d.venta_total.toLocaleString("es-EC")+'</strong> ('+d.pct_del_total+'% del total)<br>'+(ss.length?'Sin stock: <strong>'+ss.join(", ")+'</strong><br>':'OK stock<br>')+(bs.length?'Stock bajo: <strong>'+bs.map(b=>(b.PRODUCTO||"?").split(" ").slice(0,3).join(" ")+": "+b.STOCK+"u").join(" | ")+'</strong><br>':'')+'<br>Que quieres saber?</div><div class="msg-time">'+gN()+'</div></div></div></div></div>';
    document.getElementById("inputArea").style.display="block";document.getElementById("inputBox").focus();
  }catch(e){c.innerHTML='<div class="loading">Error al cargar.</div>';}
}
function qr(t){document.getElementById("inputBox").value=t;enviarChat();}

function showTendencia(){
  const d=window._detalle;if(!d)return;
  const tend=d.tendencia_ordenada||[];const proy=d.proyeccion_proximo_mes;
  if(!tend.length){addMsg("Sin datos de tendencia disponibles.","bot");return;}
  // Construir barras (datos reales + proyeccion)
  const bars=tend.map(x=>({label:x.label,valor:x.valor,full:x.parcial?x.valor_prorrateado:x.valor,parcial:!!x.parcial,dias:x.dias_con_data,diasMes:x.dias_mes,proy:false}));
  if(proy&&proy.valor){bars.push({label:proy.label||"Proy.",valor:proy.valor,full:proy.valor,parcial:false,proy:true});}
  const maxV=Math.max(...bars.map(b=>b.full||b.valor))||1;
  const W=300,H=170,PAD=28,BW=Math.floor((W-PAD*2)/bars.length*0.7),GAP=Math.floor((W-PAD*2)/bars.length*0.3);
  let svg='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;max-width:340px;height:auto;display:block;">';
  svg+='<defs><linearGradient id="pg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#C9A84C" stop-opacity="0.6"/><stop offset="1" stop-color="#C9A84C" stop-opacity="0.2"/></linearGradient><pattern id="hatch" patternUnits="userSpaceOnUse" width="4" height="4" patternTransform="rotate(45)"><rect width="4" height="4" fill="#C9A84C" fill-opacity="0.25"/><line x1="0" y1="0" x2="0" y2="4" stroke="#C9A84C" stroke-width="1.2"/></pattern></defs>';
  svg+='<line x1="'+PAD+'" y1="'+(H-PAD)+'" x2="'+(W-PAD/2)+'" y2="'+(H-PAD)+'" stroke="#37516e" stroke-width="1"/>';
  bars.forEach((b,i)=>{
    const baseY=H-PAD;
    const bhFull=((b.full||b.valor)/maxV)*(H-PAD*2);
    const bhReal=(b.valor/maxV)*(H-PAD*2);
    const x=PAD+i*(BW+GAP);
    if(b.parcial){
      // Rectangulo completo con hatch (cierre proyectado) + rectangulo solido con valor real
      svg+='<rect x="'+x+'" y="'+(baseY-bhFull)+'" width="'+BW+'" height="'+bhFull+'" fill="url(#hatch)" stroke="#C9A84C" stroke-dasharray="2,2" stroke-width="0.8" rx="3"/>';
      svg+='<rect x="'+x+'" y="'+(baseY-bhReal)+'" width="'+BW+'" height="'+bhReal+'" fill="#C9A84C" rx="3"/>';
      svg+='<text x="'+(x+BW/2)+'" y="'+(baseY-bhFull-4)+'" fill="#C9A84C" font-size="9" text-anchor="middle">$'+Math.round(b.full)+'*</text>';
    }else{
      const fill=b.proy?"url(#pg)":"#C9A84C";
      svg+='<rect x="'+x+'" y="'+(baseY-bhFull)+'" width="'+BW+'" height="'+bhFull+'" fill="'+fill+'" rx="3"/>';
      svg+='<text x="'+(x+BW/2)+'" y="'+(baseY-bhFull-4)+'" fill="#e8edf3" font-size="9" text-anchor="middle">$'+Math.round(b.valor)+'</text>';
    }
    svg+='<text x="'+(x+BW/2)+'" y="'+(baseY+12)+'" fill="#8ea0b6" font-size="10" text-anchor="middle">'+b.label+'</text>';
  });
  svg+='</svg>';
  let txt='<strong>Tendencia '+d.pos+'</strong><br>';
  tend.forEach(x=>{
    if(x.parcial){
      txt+=x.label+' 2026: <strong>$'+x.valor.toLocaleString("es-EC")+'</strong> <span style="color:#8ea0b6;font-size:11px">('+x.dias_con_data+'/'+x.dias_mes+' dias &middot; cierre est. $'+x.valor_prorrateado.toLocaleString("es-EC")+')</span><br>';
    }else{
      txt+=x.label+' 2026: <strong>$'+x.valor.toLocaleString("es-EC")+'</strong><br>';
    }
  });
  if(proy&&proy.valor){
    const pctS=proy.crecimiento_pct!==undefined?(proy.crecimiento_pct>=0?"+":"")+proy.crecimiento_pct+"%":"";
    const proyTit=proy.mes_en_curso?('Proyeccion cierre '+(proy.label||'').replace('Proy. ','')):'Proyeccion proximo mes';
    txt+='<br>'+proyTit+': <strong>$'+proy.valor.toLocaleString("es-EC")+'</strong> '+pctS;
  }
  const hayParc=tend.some(x=>x.parcial);
  if(hayParc){txt+='<br><em style="color:#8ea0b6;font-size:11px">* Cierre estimado prorrateado por dias con data.</em>';}
  addMsg(svg+txt,"bot");
}

function showStock(){
  const d=window._detalle;if(!d)return;
  const si=d.stock_info||{};const det=si.detalle_completo||[];
  if(!det.length){addMsg("Sin registros de stock disponibles.","bot");return;}
  const conStock=det.filter(x=>x.stock_unid>0);
  let html='<strong>Stock '+d.pos+'</strong>';
  if(si.fecha){const f=si.fecha;html+='<br><span style="color:#8ea0b6;font-size:11px">Corte: '+f.slice(6,8)+'/'+f.slice(4,6)+'/'+f.slice(0,4)+'</span>';}
  html+='<br><br>Items codificados: <strong>'+si.total_productos+'</strong> &middot; Con stock: <strong>'+si.total_con_stock+'</strong>';
  html+='<br>Total unidades: <strong>'+Math.round(si.total_unidades||0).toLocaleString("es-EC")+'</strong>';
  html+='<br>Valorizado: <strong>$'+(si.total_valorizado||0).toLocaleString("es-EC")+'</strong><br><br>';
  html+='<button onclick="downloadStockPDF()" style="margin-bottom:10px;background:linear-gradient(135deg,var(--gold),var(--gold2));border:none;color:#0b1a2b;font-weight:700;padding:8px 14px;border-radius:10px;cursor:pointer;font-size:12px">Descargar PDF</button>';
  html+='<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table style="width:100%;border-collapse:collapse;font-size:11px;"><thead><tr style="background:rgba(46,117,182,0.15);color:#C9A84C"><th style="text-align:left;padding:6px 4px;border-bottom:1px solid #37516e">ID Neptuno</th><th style="text-align:left;padding:6px 4px;border-bottom:1px solid #37516e">Producto</th><th style="text-align:right;padding:6px 4px;border-bottom:1px solid #37516e">Unid.</th><th style="text-align:right;padding:6px 4px;border-bottom:1px solid #37516e">Valor $</th></tr></thead><tbody>';
  det.forEach(x=>{
    const zero=x.stock_unid===0;
    const color=zero?"color:#8ea0b6":(x.stock_unid<=3?"color:#f0a84c":"color:#e8edf3");
    html+='<tr style="'+color+'"><td style="padding:4px;border-bottom:1px solid rgba(55,81,110,0.4);font-family:monospace">'+(x.id_neptuno||"-")+'</td><td style="padding:4px;border-bottom:1px solid rgba(55,81,110,0.4)">'+x.producto+'</td><td style="text-align:right;padding:4px;border-bottom:1px solid rgba(55,81,110,0.4)">'+x.stock_unid+'</td><td style="text-align:right;padding:4px;border-bottom:1px solid rgba(55,81,110,0.4)">$'+x.stock_val.toLocaleString("es-EC")+'</td></tr>';
  });
  html+='</tbody></table></div>';
  addMsg(html,"bot");
}

function downloadStockPDF(){
  const d=window._detalle;if(!d)return;
  const si=d.stock_info||{};const det=si.detalle_completo||[];
  if(!det.length){alert("Sin datos");return;}
  try{
    const {jsPDF}=window.jspdf;const doc=new jsPDF({orientation:"portrait",unit:"mm",format:"a4"});
    doc.setFontSize(14);doc.setTextColor(201,168,76);doc.text("Stock - "+d.pos,14,15);
    doc.setFontSize(9);doc.setTextColor(60,60,60);
    let sub=d.grupo_pdv||"";
    if(si.fecha){const f=si.fecha;sub+="  |  Corte: "+f.slice(6,8)+"/"+f.slice(4,6)+"/"+f.slice(0,4);}
    doc.text(sub,14,21);
    doc.text("Items: "+si.total_productos+"   Con stock: "+si.total_con_stock+"   Unidades: "+Math.round(si.total_unidades||0).toLocaleString("es-EC")+"   Valorizado: $"+(si.total_valorizado||0).toLocaleString("es-EC"),14,27);
    const body=det.map(x=>[String(x.id_neptuno||"-"),x.producto,String(x.stock_unid),"$"+x.stock_val.toLocaleString("es-EC")]);
    doc.autoTable({startY:32,head:[["ID Neptuno","Producto","Unid.","Valor $"]],body:body,styles:{fontSize:8,cellPadding:1.5},headStyles:{fillColor:[46,117,182],textColor:[255,255,255]},columnStyles:{0:{cellWidth:22},2:{halign:"right",cellWidth:18},3:{halign:"right",cellWidth:26}},didParseCell:function(data){if(data.section==="body"){const u=parseFloat(data.row.raw[2]);if(u===0)data.cell.styles.textColor=[140,140,140];else if(u<=3)data.cell.styles.textColor=[220,120,30];}}});
    const safe=(d.pos||"stock").replace(/[^a-zA-Z0-9]+/g,"_");
    doc.save("Stock_"+safe+".pdf");
  }catch(e){alert("Error generando PDF: "+e.message);}
}

function gN(){return new Date().toLocaleTimeString("es-EC",{hour:"2-digit",minute:"2-digit"});}
function autoResize(el){el.style.height="auto";el.style.height=Math.min(el.scrollHeight,80)+"px";}
function handleKey(e){if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();enviarChat();}}

function addMsg(t,tp){const m=document.getElementById("msgs");if(!m)return;const d=document.createElement("div");d.className="msg "+tp;d.innerHTML='<div class="msg-avatar">'+(tp==="user"?"U":"N")+'</div><div><div class="msg-bubble">'+t.replace(/\*\*(.*?)\*\*/g,"<strong>$1</strong>").replace(/\n/g,"<br>")+'</div><div class="msg-time">'+gN()+'</div></div>';m.appendChild(d);document.getElementById("content").scrollTop=99999;}
function showTy(){const m=document.getElementById("msgs");if(!m)return;const d=document.createElement("div");d.className="msg bot";d.id="ty";d.innerHTML='<div class="msg-avatar">N</div><div class="msg-bubble"><div class="typing"><span></span><span></span><span></span></div></div>';m.appendChild(d);document.getElementById("content").scrollTop=99999;}
function hideTy(){const t=document.getElementById("ty");if(t)t.remove();}

async function enviarChat(){
  if(esperando||!posActual)return;const inp=document.getElementById("inputBox");const q=inp.value.trim();if(!q)return;
  addMsg(q,"user");inp.value="";inp.style.height="auto";esperando=true;document.getElementById("sendBtn").disabled=true;showTy();
  try{const r=await fetch(S+"/chat",{method:"POST",headers:AH(),body:JSON.stringify({pregunta:q,contexto_pos:posActual})});
    if(r.status===401){cerrarSesion();return;}const d=await r.json();hideTy();addMsg(d.respuesta||d.error,"bot");
  }catch(e){hideTy();addMsg("Error de conexion.","bot");}
  esperando=false;document.getElementById("sendBtn").disabled=false;document.getElementById("inputBox").focus();
}
</script>
</body>
</html>"""
