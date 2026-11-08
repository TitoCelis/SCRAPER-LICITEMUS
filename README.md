# SCRAPER LICITEMUS

Radar de contrataciones del Estado peruano. CMI General Solutions.

| Script | Fuente | Qué baja |
|---|---|---|
| `scraper_oece.py` | API OCDS del OECE (`contratacionesabiertas.oece.gob.pe`) | Procesos grandes: Licitación Pública, Concurso Público, Contratación Directa, etc. |
| `scraper_8uit.py` | Buscador público de contrataciones ≤ 8 UIT (`prep6.seace.gob.pe`) | Contratos menores |
| `index.html` | — | Explorador web para `contratos_8uit.json` |

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
python3 scraper_oece.py --dias 7 --monto-min 400000
python3 scraper_8uit.py --dias 3 --vigentes --objeto bien,servicio --departamento lima
```

Ver el explorador:

```bash
python3 -m http.server 8000   # abrir http://localhost:8000
```

## Supabase (opcional)

Variables: `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_TABLE` (OECE, PK `ocid`), `SUPABASE_TABLE_8UIT` (PK `id_contrato`). Añadir `--supabase`.

## Cron sugerido (VPS)

```
0 */2 * * * cd /opt/radar && python3 scraper_oece.py --dias 2 --supabase >> radar.log 2>&1
30 */2 * * * cd /opt/radar && python3 scraper_8uit.py --dias 2 --supabase >> radar.log 2>&1
```

## Notas

- La API del OECE y `prod2.seace.gob.pe` bloquean IPs de nube (403): correr desde una IP en Perú.
- `prep6.seace.gob.pe` parece el ambiente de pruebas; si el de producción está en otro dominio, usar `--base https://<dominio>`.
- La API 8 UIT exige `anio` pero no lo aplica; el script filtra el año localmente (`--anio 0` = todos).
