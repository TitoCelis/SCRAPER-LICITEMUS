#!/usr/bin/env python3
"""
Scraper de procesos del Estado peruano (SEACE) vía la API OCDS del OECE.
Licita Perú

Baja las publicaciones más recientes, filtra procesos grandes
(Licitación Pública, Concurso Público, Contratación Directa, etc.)
por monto mínimo y guarda procesos.json + procesos.csv.
El JSON se carga directo en el explorador web.

Uso:
  pip install requests
  python scraper_oece.py --dias 7 --monto-min 400000
  python scraper_oece.py --dias 3 --tipos "contratacion directa,licitacion publica" --keyword "medic"
  python scraper_oece.py --supabase   # además hace upsert a Supabase (ver variables abajo)

Variables para Supabase (opcional):
  SUPABASE_URL, SUPABASE_KEY, SUPABASE_TABLE (default: licita_procesos, PK = ocid). SUPABASE_KEY = service_role

Cron sugerido en tu VPS (cada 2 h):
  0 */2 * * * cd /opt/radar && /usr/bin/python3 scraper_oece.py --dias 2 --supabase >> radar.log 2>&1
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests


def cargar_env(ruta=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Lee SUPABASE_URL / SUPABASE_KEY / SUPABASE_TABLE desde .env (también en Windows)."""
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


cargar_env()

API = "https://contratacionesabiertas.oece.gob.pe/api/v1"
# La API ordena por un índice interno y está particionada por año: con first=<prefijo>-999999-<año>
# arranca en lo más nuevo de ese año y va bajando.
OCID_PREFIJO = "ocds-dgv273-seacev3"


def start_url(anio):
    return f"{API}/releasesAfter?format=json&order=desc&first={OCID_PREFIJO}-999999-{anio}"
# La API rechaza (403) clientes que no parecen navegador
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9",
    "Referer": "https://contratacionesabiertas.oece.gob.pe/",
}

# Tipos de procedimiento considerados "grandes"
TIPOS_DEFAULT = [
    "licitacion publica",
    "concurso publico",
    "contratacion directa",
    "adjudicacion simplificada",
    "subasta inversa",
    "seleccion de consultores",
    "comparacion de precios",
    "dialogo competitivo",
]

CATEGORIAS = {"goods": "Bien", "services": "Servicio", "works": "Obra",
              "consultingServices": "Consultoría"}

NOMENCLATURA_RE = re.compile(r"^[A-Z]{2,10}(-[A-Z]{1,8})*-\d+-\d{4}")


def norm(txt):
    """minúsculas sin tildes, para comparar."""
    txt = unicodedata.normalize("NFKD", str(txt or ""))
    return "".join(c for c in txt if not unicodedata.combining(c)).lower().strip()


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def aplanar(rel):
    """Convierte un release OCDS en una fila plana."""
    t = rel.get("tender") or {}
    buyer = rel.get("buyer") or {}
    parties = rel.get("parties") or []

    region = ""
    buyer_id = buyer.get("id")
    for p in parties:
        if p.get("id") == buyer_id or "buyer" in (p.get("roles") or []):
            region = ((p.get("address") or {}).get("region") or "").strip()
            if region:
                break

    titulo = (t.get("title") or "").strip()
    desc = (t.get("description") or "").strip()
    if NOMENCLATURA_RE.match(titulo):
        nomenclatura, objeto = titulo, desc or titulo
    else:
        nomenclatura, objeto = (t.get("id") or ""), titulo or desc

    val = t.get("value") or {}
    periodo = t.get("tenderPeriod") or {}
    rid = rel.get("id") or ""
    return {
        "ocid": rel.get("ocid"),
        "release_id": rid,
        "nomenclatura": nomenclatura,
        "objeto": objeto,
        "descripcion": desc,
        "tipo": (t.get("procurementMethodDetails") or "").strip(),
        "categoria": CATEGORIAS.get(t.get("mainProcurementCategory"), t.get("mainProcurementCategory") or ""),
        "entidad": (buyer.get("name") or "").strip(),
        "departamento": region.upper(),
        "monto": val.get("amount"),
        "moneda": val.get("currency") or "PEN",
        "fecha_publicacion": rel.get("date"),
        "fecha_cierre": periodo.get("endDate"),
        "estado": t.get("status") or "",
        "url": f"{API}/release/{rid}" if rid else "",
    }


def pasa_filtro(row, args, tipos):
    if not row["tipo"] or not any(tp in norm(row["tipo"]) for tp in tipos):
        return False
    if args.monto_min and (row["monto"] or 0) < args.monto_min:
        # Contrataciones directas a veces vienen sin monto: se dejan pasar si se pide
        if not (args.incluir_sin_monto and row["monto"] in (None, 0)):
            return False
    if args.keyword:
        blob = norm(" ".join([row["objeto"], row["descripcion"], row["entidad"]]))
        if not all(k in blob for k in (norm(x) for x in args.keyword.split(","))):
            return False
    if args.departamento and norm(args.departamento) not in norm(row["departamento"]):
        return False
    return True


def descargar(args):
    corte = datetime.now(timezone.utc) - timedelta(days=args.dias)
    tipos = [norm(x) for x in (args.tipos.split(",") if args.tipos else TIPOS_DEFAULT)]
    url = start_url(args.anio or datetime.now().year)
    paginas_viejas = 0
    filas, vistos = {}, 0
    s = requests.Session()
    s.headers.update(HEADERS)

    for pagina in range(1, args.max_paginas + 1):
        for intento in range(4):
            try:
                r = s.get(url, timeout=60)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                espera = 5 * (intento + 1)
                print(f"  ! pág {pagina}: {e} — reintento en {espera}s", file=sys.stderr)
                time.sleep(espera)
        else:
            print("  ! Demasiados errores, corto aquí.", file=sys.stderr)
            break

        releases = data.get("releases") or []
        if not releases:
            break
        mas_antiguo = mas_nuevo = None
        for rel in releases:
            vistos += 1
            t = rel.get("tender") or {}
            # fecha de convocatoria (inicio del periodo) o, si no hay, fecha del release
            d = parse_date((t.get("tenderPeriod") or {}).get("startDate")) or parse_date(rel.get("date"))
            if d:
                mas_antiguo = d if mas_antiguo is None or d < mas_antiguo else mas_antiguo
                mas_nuevo = d if mas_nuevo is None or d > mas_nuevo else mas_nuevo
            if not t or (d and d < corte):
                continue
            row = aplanar(rel)
            if pasa_filtro(row, args, tipos):
                prev = filas.get(row["ocid"])
                # nos quedamos con la versión más reciente de cada proceso
                if not prev or (row["fecha_publicacion"] or "") > (prev["fecha_publicacion"] or ""):
                    filas[row["ocid"]] = row

        rango = f"{mas_antiguo:%Y-%m-%d}..{mas_nuevo:%Y-%m-%d}" if mas_antiguo else "sin fechas"
        print(f"  pág {pagina}: {len(releases)} releases · {rango} · {len(filas)} procesos filtrados")
        # cortamos cuando dos páginas seguidas son enteramente más viejas que el corte
        paginas_viejas = paginas_viejas + 1 if (mas_nuevo and mas_nuevo < corte) else 0
        if paginas_viejas >= 2:
            break
        nxt = (data.get("links") or {}).get("next")
        if not nxt:
            break
        url = nxt
        time.sleep(args.pausa)

    print(f"Revisados {vistos} releases → {len(filas)} procesos.")
    return sorted(filas.values(), key=lambda x: -(x["monto"] or 0))


def guardar(filas, out):
    with open(f"{out}.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "procesos": filas},
                  f, ensure_ascii=False, indent=1)
    if filas:
        with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
            w.writeheader()
            w.writerows(filas)
    print(f"Guardado: {out}.json / {out}.csv")


def upsert_supabase(filas):
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    tabla = os.getenv("SUPABASE_TABLE", "licita_procesos")
    if not (url and key):
        print("  ! Falta SUPABASE_URL / SUPABASE_KEY, salto el upsert.", file=sys.stderr)
        return
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates"}
    for i in range(0, len(filas), 500):
        r = requests.post(f"{url}/rest/v1/{tabla}?on_conflict=ocid", headers=h,
                          json=filas[i:i + 500], timeout=60)
        if r.status_code >= 300:
            print(f"  ! Supabase {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return
    print(f"Supabase: {len(filas)} filas en {tabla}.")


def main():
    ap = argparse.ArgumentParser(description="Radar de procesos SEACE (API OCDS OECE)")
    ap.add_argument("--dias", type=int, default=7, help="cuántos días hacia atrás revisar")
    ap.add_argument("--monto-min", type=float, default=400000, help="valor referencial mínimo en S/")
    ap.add_argument("--tipos", default="", help="lista separada por comas (default: procesos grandes)")
    ap.add_argument("--keyword", default="", help="palabras clave (todas deben aparecer), separadas por coma")
    ap.add_argument("--departamento", default="")
    ap.add_argument("--incluir-sin-monto", action="store_true",
                    help="no descartar procesos sin valor referencial publicado")
    ap.add_argument("--max-paginas", type=int, default=400)
    ap.add_argument("--anio", type=int, default=0, help="año a recorrer (default: el actual)")
    ap.add_argument("--pausa", type=float, default=0.5, help="segundos entre páginas")
    ap.add_argument("--out", default="procesos")
    ap.add_argument("--supabase", action="store_true")
    args = ap.parse_args()

    filas = descargar(args)
    guardar(filas, args.out)
    if args.supabase:
        upsert_supabase(filas)


if __name__ == "__main__":
    main()
