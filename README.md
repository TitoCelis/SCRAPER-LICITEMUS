# SCRAPER LICITEMUS

Radar de contrataciones del Estado peruano.

| Script | Fuente | Qué baja |
|---|---|---|
| `scraper_oece.py` | API OCDS del OECE (`contratacionesabiertas.oece.gob.pe`) | Procesos grandes: Licitación Pública, Concurso Público, Contratación Directa, etc. |
| `scraper_8uit.py` | Buscador público de contrataciones ≤ 8 UIT (`prep6.seace.gob.pe`) | Contratos menores |
| `index.html` | — | Buscador unificado: pestañas *Procesos de selección* (`procesos.json`) y *Contrataciones ≤ 8 UIT* (`contratos_8uit.json`), con filtros por objeto, procedimiento, estado, departamento, entidad, fechas y monto |
| `licita-peru.html` | Supabase `licita_procesos` | **Licita Perú**: login con correo y contraseña, perfil de empresa por rubro y reporte diario en la app (nuevos en 24 h + todos los vigentes, descarga CSV/HTML) |
| `actualizar.sh` | — | Corre ambos scrapers de una vez |

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
python3 scraper_oece.py --dias 7 --monto-min 400000
python3 scraper_8uit.py --dias 3 --vigentes --objeto bien,servicio --departamento lima
```

Ambos a la vez:

```bash
./actualizar.sh            # o ./actualizar.sh --supabase
```

Ver el buscador:

```bash
python3 -m http.server 8000   # abrir http://localhost:8000
```

Licita Perú: `http://localhost:8000/licita-peru.html` (necesita internet: usa Supabase).

## Supabase (Licita Perú)

Proyecto **Titos App** (`ahcepuufqcatzgocqjkm`). Las tablas de Licita Perú llevan prefijo `licita_` para convivir con otras apps del mismo proyecto. Esquema en `supabase/migrations/`.

| Tabla | Qué guarda | Acceso |
|---|---|---|
| `licita_procesos` | Procesos del scraper (PK `ocid`) | Lectura: usuarios con sesión. Escritura: solo `service_role` (scraper) |
| `licita_empresas` | Perfil de cada empresa (1 por usuario) | Cada usuario solo la suya |

Llenar la base desde el VPS:

```bash
cp .env.example .env   # pega la service_role key (Project Settings > API Keys)
./actualizar.sh --supabase
```

`SUPABASE_TABLE_8UIT` (PK `id_contrato`) sigue siendo opcional para el scraper 8 UIT.

### Reporte diario

No hay correos: al entrar, *Mi reporte* muestra los procesos **nuevos en las últimas 24 h** que calzan con la empresa (con contador en la pestaña) y, aparte, todos los vigentes. "Nuevo" = la primera vez que el scraper lo guardó (`creado`).

### Auth

- Supabase > Authentication > URL Configuration: poner la URL donde publiques `licita-peru.html` en *Site URL* y *Redirect URLs* (para confirmar cuenta y recuperar contraseña).
- El SMTP por defecto de Supabase manda pocos correos por hora (confirmación y recuperación de contraseña); para muchos usuarios configura un SMTP propio.

## Cron sugerido (VPS)

```
0 */2 * * * cd /opt/radar && ./actualizar.sh --supabase >> radar.log 2>&1
```

## Notas

- La API del OECE y `prod2.seace.gob.pe` bloquean IPs de nube (403): correr desde una IP en Perú.
- `prep6.seace.gob.pe` parece el ambiente de pruebas; si el de producción está en otro dominio, usar `--base https://<dominio>`.
- La API 8 UIT exige `anio` pero no lo aplica; el script filtra el año localmente (`--anio 0` = todos).
