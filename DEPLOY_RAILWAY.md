# Deploy en Railway — Guía paso a paso

Esta guía es para mañana (Día 2). Ya tienes cuenta de Railway y GitHub.

## 1) Subir el repo a GitHub (5 min)

```bash
cd "C:\Users\favilac\OneDrive - genommalabinternacional\Documentos\difare-nexus-v2"
git init
git add .
git commit -m "v2 inicial: unificación + analitica + endpoints gerenciales"
git branch -M main
git remote add origin https://github.com/<tu-usuario>/difare-nexus-v2.git
git push -u origin main
```

> El repo crea como **privado** desde la web de GitHub antes del push.

## 2) Crear proyecto en Railway (5 min)

1. railway.app → **New Project** → **Deploy from GitHub repo** → seleccionar `difare-nexus-v2`.
2. Railway detecta el `Dockerfile` automáticamente.
3. Plan **Hobby ($5/mes)** activado para evitar suspensión por inactividad.

## 3) Variables de entorno (10 min)

En Railway → Project → Variables, copiar las del Vercel actual:

| Variable | Valor (copiar del Vercel) |
|----------|---------------------------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `JWT_SECRET` | (el mismo del Vercel) |
| `USER_1_NAME` / `USER_1_PASS` | `francisco` / `<pass actual>` |
| `USER_2_NAME` / `USER_2_PASS` | `Campo` / `<pass actual>` |
| `USER_3_NAME` / `USER_3_PASS` | `Gerente` / `<pass actual>` |

> No setear `PORT` — Railway lo inyecta solo.

## 4) Primer deploy (3 min)

Railway despliega automáticamente al detectar el push. Esperar logs verdes.

URL temporal asignada: `https://difare-nexus-v2-production.up.railway.app` (o similar).

## 5) Validación post-deploy (10 min)

```bash
# Healthcheck
curl https://<tu-url-railway>/health

# Login con francisco
curl -X POST https://<tu-url-railway>/login \
  -H "Content-Type: application/json" \
  -d '{"usuario":"francisco","contrasena":"<pass>"}'
# → debe devolver token, usuario, rol="admin"

# KPIs (con el token del paso anterior)
curl https://<tu-url-railway>/api/kpis \
  -H "Authorization: Bearer <token>"

# Ranking top 10 farmacias
curl "https://<tu-url-railway>/api/ranking-pdv?canal=FARMACIAS&top=10" \
  -H "Authorization: Bearer <token>"
```

## 6) Endpoints disponibles tras el deploy

### Públicos
- `POST /login` → ahora devuelve `rol` además de token
- `POST /verificar_token`
- `GET /health`
- `GET /` → frontend del chat de farmacias (sigue igual)

### Chat de farmacias (heredados, requieren JWT)
- `GET /grupos`, `GET /farmacias[/<grupo>]`, `GET /buscar_pos`
- `POST /detalle_pos`, `POST /productos_faltantes`, `POST /chat`

### ✨ Nuevos del Dashboard Gerencial (rol `admin` o `gerencial`)
- `GET /api/kpis`
- `GET /api/tendencia-marca?un=FARMACIAS|DIFARE|TOTAL&yoy=0`
- `GET /api/ranking-pdv?canal=FARMACIAS|DISTRIBUCION&top=50`
- `GET /api/pareto-pdv`
- `POST /api/recargar-data` (solo `admin`, invalida cache tras subir nuevo Excel)

## 7) Apuntar dominio propio (opcional, día 8)

Railway → Settings → Domains → Custom Domain → añadir `nexus.tudominio.com` y
crear el CNAME en tu DNS.

---

## Smoke test local (opcional, antes del push)

Si quieres probar antes de subir:

```bash
cd difare-nexus-v2
python -m venv .venv
source .venv/bin/activate     # o .venv\Scripts\activate en Windows
pip install -r requirements.txt
cp .env.example .env           # editar con credenciales reales
python app.py
# abrir http://localhost:5000
```
