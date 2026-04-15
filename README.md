# DIFARE NEXUS v2

Proyecto unificado que fusiona `agente-excel/` (analítica + PDFs) y
`difare-nexus-api/` (chat de farmacias en Vercel) en un único backend Flask
desplegado en **Railway**.

> ⚠️ Las dos carpetas originales (`agente-excel/`, `difare-nexus-api/`) siguen
> intactas y operando en paralelo hasta que v2 esté validado en producción.

## Roles de usuario

| Usuario     | Rol         | Pantalla inicial                                  |
|-------------|-------------|---------------------------------------------------|
| `francisco` | admin       | Dashboard gerencial + acceso total + panel admin  |
| `Gerente`   | gerencial   | Dashboard gerencial (con link al chat farmacias)  |
| `Campo`     | campo       | Chat farmacias (igual que hoy)                    |

## Parámetros de negocio (Fase 1)

- `LEAD_TIME_DIAS = 2`
- `BUFFER_DIAS = 8`
- `DIAS_INV_SEGURIDAD = 10`

Definidos en `agente/analitica.py`. En el panel admin de `francisco` se podrán
modificar en caliente.

## Estructura

```
difare-nexus-v2/
├── app.py                  # Flask, rutas, JWT, login (heredado de v1)
├── actualizar_data.py      # Re-hidratación de api/data.db desde excels/
├── requirements.txt
├── api/
│   └── data.db             # SQLite (Fase 1). Migra a Postgres en Fase 2.
├── agente/
│   ├── __init__.py
│   ├── analitica.py        # ✨ NUEVO — capa de cálculo pura para los endpoints
│   ├── generar_pdfs.py     # legacy, fuente de verdad de los cálculos
│   ├── agente.py           # análisis IA legacy
│   └── main.py             # orquestador local (Outlook)
├── excels/                 # única carpeta de Excels (antes duplicada)
├── templates/              # dashboard.html, login.html, chat.html
└── static/                 # css/js/charts
```

## Plan de 8 días → live miércoles 15 abril 2026

| Día | Fecha    | Entregable                                                                 |
|-----|----------|----------------------------------------------------------------------------|
| 1   | Lun 6/4  | ✅ Carpeta v2 creada, archivos copiados, `analitica.py` listo              |
| 2   | Mar 7/4  | Cuenta Railway + primer deploy + JWT con rol                               |
| 3   | Mié 8/4  | Endpoints `/api/kpis`, `/api/tendencia-marca`, `/api/ranking-pdv`, `/api/pareto-pdv` |
| 4   | Jue 9/4  | `dashboard.html` con Tailwind + Chart.js + routing por rol                 |
| 5   | Vie 10/4 | Chat gerencial embebido + tool use (3 herramientas críticas)               |
| 6   | Sáb 11/4 | 3 herramientas restantes + botones rápidos + export Excel vectorización    |
| 7   | Dom 12/4 | Reporte gerencial PDF + panel admin                                         |
| 8   | Lun 13/4 | QA con 3 usuarios, fix bugs, pulido                                         |
|     | Mar 14/4 | Buffer                                                                     |
| 🚀  | Mié 15/4 | **LIVE** en `nexus-v2.up.railway.app`                                     |

## Catálogo de herramientas del chat gerencial

| # | Pregunta KAM                                          | Tool                              |
|---|--------------------------------------------------------|-----------------------------------|
| 1 | Tendencia por marca / UN, vs año anterior              | `tendencia_marca`                 |
| 2 | Días de inventario + proyección                        | `dias_inventario`, `proyeccion_venta` |
| 3 | Vectorización Pareto + venta perdida                   | `oportunidad_vectorizacion`       |
| 4 | Ranking top 50 farmacias / top 20 distribución         | `ranking_pdv`                     |
| 5 | Mínimos/máximos sugeridos por grupo de farmacias       | `sugerido_stock`                  |
| 6 | Reporte Excel de vectorización de un producto          | `exportar_vectorizacion_excel`    |

Si el Gerente pregunta por una farmacia puntual, el chat responde un resumen
breve y muestra un botón **→ Ver detalle en chat de farmacias** que abre el
chat actual con la consulta pre-cargada.

## Fase 2 (mayo–junio)

- Migrar `data.db` a Postgres (Neon/Supabase/Railway Postgres).
- Cargar histórico 2025 (12 Excel) → comparativos YoY reales.
- Snapshots automáticos de cierre de mes (cron de Railway).
- Auditoría de cambios.
- Uploader de Excels por UI.
- Multi-cliente (Genommalab + otros laboratorios).

---

**Próximo paso (Día 2 — Mar 7):** abrir cuenta en Railway juntos y desplegar
el v2 actual (que aún es 1:1 con `difare-nexus-api/` + el módulo `agente/`).
