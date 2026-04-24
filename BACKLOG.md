# ORION — Backlog técnico

Backlog de deuda técnica y mejoras identificadas. No es roadmap de features de negocio; es hygiene de infraestructura y código.

## Fase 2 — Infraestructura de datos

### Migrar fuente de Excel fuera del repo

**Estado:** pendiente
**Prioridad:** media-alta
**Impacto:** reduce ~25 MB del repo, acelera deploys en Railway, desacopla data de código.

Los reportes mensuales (`excels/Reporte_Mensual_*.xlsx`, `excels/SAP-REP SEMANAL *.xlsx`) están hoy dentro del repo de git. Cada actualización de data dispara un push de ~25 MB y un rebuild de la imagen Docker en Railway.

**Opciones a evaluar:**

1. **Volumen persistente de Railway** — montar un volume en `/excels`, subir archivos vía SFTP o vía un endpoint protegido. Más simple, misma estructura de carpetas.
2. **S3 / R2 / GCS** — mover a object storage, leer con `s3fs` o `boto3`. Más escalable para multi-cliente (Fase 2 del handoff).
3. **Uploader UI en el dashboard** — ya está listado como pendiente en el handoff. Una vez que exista, los Excel entran por ahí y no por git.
4. **PostgreSQL (Neon/Supabase)** — el objetivo final del handoff. Requiere ETL que ingeste los Excel a tablas normalizadas.

**Bloqueador corto plazo:** `cargar_data()` en `agente/analitica.py` hace `pd.read_excel()` directo sobre `excels/`. Hay que abstraer esa capa detrás de una función tipo `obtener_fuente_datos()` antes de cambiar el backend.

### Migrar `api/data.db` a servicio gestionado

**Estado:** ya removido de git tracking (abril 2026)
**Siguiente paso:** cuando `data.db` crezca más o se necesite concurrencia, migrar a Postgres.

## Fase 3 — Refactor de frontend

El frontend vive dentro de `app.py` (~3,000 líneas de HTML/CSS/JS embebidos). Separar a `static/` y `templates/` Jinja facilitaría iteración, mantenibilidad y code review.

## Otros

- Caché `_cache_pareto_grupo` hoy tiene TTL fijo de 5 min y máx 10 grupos. Evaluar LRU con hit-rate metrics si hay uso intensivo.
- Pre-warm de top 5 grupos al arrancar: revisar si los grupos elegidos siguen siendo los más consultados.
