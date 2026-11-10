#!/usr/bin/env bash
# Corre ambos scrapers y deja procesos.json + contratos_8uit.json para el explorador (radar.html).
# Uso: ./actualizar.sh [--supabase]
# Cron sugerido: 0 */2 * * * cd /opt/radar && ./actualizar.sh --supabase >> radar.log 2>&1
cd "$(dirname "$0")" || exit 1
# Carga SUPABASE_URL / SUPABASE_KEY / SUPABASE_TABLE desde .env si existe
if [ -f .env ]; then set -a; . ./.env; set +a; fi
echo "== $(date '+%F %T') Procesos de selección (OECE)"
python3 scraper_oece.py --dias "${DIAS_OECE:-7}" --monto-min "${MONTO_MIN:-0}" "$@" || echo "!! scraper_oece falló"
echo "== $(date '+%F %T') Contrataciones ≤ 8 UIT"
python3 scraper_8uit.py "$@" || echo "!! scraper_8uit falló"
