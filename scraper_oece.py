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
# /releases devuelve lo más reciente primero (por fecha de actualización), paginado con page=N.
# (/releasesAfter ordena por un índice interno y arranca en 2024: no sirve para lo nuevo.)
def start_url():
    return f"{API}/releases?format=json&paginateBy=100"
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
    url = start_url()
    ahora = datetime.now(timezone.utc)
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
            d = parse_date(rel.get("date"))  # fecha de actualización: define el orden
            if d:
                mas_antiguo = d if mas_antiguo is None or d < mas_antiguo else mas_antiguo
                mas_nuevo = d if mas_nuevo is None or d > mas_nuevo else mas_nuevo
            if not t:
                continue
            periodo = t.get("tenderPeriod") or {}
            inicio, cierre = parse_date(periodo.get("startDate")), parse_date(periodo.get("endDate"))
            # nos quedamos con lo vigente: plazo abierto, o sin plazo pero convocado dentro de la ventana
            if cierre and cierre < ahora:
                continue
            if not cierre and (inicio or d) and (inicio or d) < corte:
                continue
            row = aplanar(rel)
            if pasa_filtro(row, args, tipos):
                prev = filas.get(row["ocid"])
                # nos quedamos con la versión más reciente de cada proceso
                if not prev or (row["fecha_publicacion"] or "") > (prev["fecha_publicacion"] or ""):
                    filas[row["ocid"]] = row

        rango = f"{mas_antiguo:%Y-%m-%d}..{mas_nuevo:%Y-%m-%d}" if mas_antiguo else "sin fechas"
        print(f"  pág {pagina}: {len(releases)} releases · {rango} · {len(filas)} procesos filtrados")
        # cortamos cuando dos páginas seguidas solo traen actualizaciones anteriores a la ventana
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


def diagnostico():
    """Imprime cómo responde la API para ajustar el scraper. No escribe nada."""
    s = requests.Session()
    s.headers.update(HEADERS)
    pruebas = [
        ("raiz", f"{API}/?format=json"),
        ("releasesAfter", f"{API}/releasesAfter?format=json&order=desc"),
        ("releases", f"{API}/releases?format=json"),
        ("files", f"{API}/files?page=1&paginateBy=5&format=json"),
    ]
    for nombre, url in pruebas:
        print(f"\n===== {nombre}: {url}")
        try:
            r = s.get(url, timeout=60)
            print("HTTP", r.status_code, "|", r.headers.get("content-type"))
            try:
                d = r.json()
            except ValueError:
                print(r.text[:600]); continue
            if isinstance(d, dict):
                print("claves:", list(d.keys())[:15])
                for k in ("links", "pagination", "count", "next"):
                    if k in d:
                        print(f"{k}:", json.dumps(d[k], ensure_ascii=False)[:500])
                rels = d.get("releases") or d.get("records") or d.get("results") or []
                for x in rels[:4]:
                    if isinstance(x, dict):
                        t = x.get("tender") or {}
                        print("  -", x.get("ocid") or x.get("id"), "|", x.get("date"), "|",
                              (t.get("tenderPeriod") or {}).get("startDate"), "|",
                              json.dumps(x, ensure_ascii=False)[:300] if not t else (t.get("title") or "")[:60])
            else:
                print(json.dumps(d, ensure_ascii=False)[:600])
        except requests.RequestException as e:
            print("ERROR", e)


def main():
    ap = argparse.ArgumentParser(description="Radar de procesos SEACE (API OCDS OECE)")
    ap.add_argument("--dias", type=int, default=7, help="cuántos días hacia atrás revisar")
    ap.add_argument("--monto-min", type=float, default=400000, help="valor referencial mínimo en S/")
    ap.add_argument("--tipos", default="", help="lista separada por comas (default: procesos grandes)")
    ap.add_argument("--keyword", default="", help="palabras clave (todas deben aparecer), separadas por coma")
    ap.add_argument("--departamento", default="")
    ap.add_argument("--incluir-sin-monto", action="store_true",
                    help="no descartar procesos sin valor referencial publicado")
    ap.add_argument("--max-paginas", type=int, default=3000)
    ap.add_argument("--pausa", type=float, default=0.3, help="segundos entre páginas")
    ap.add_argument("--out", default="procesos")
    ap.add_argument("--supabase", action="store_true")
    ap.add_argument("--diagnostico", action="store_true", help="muestra cómo responde la API y sale")
    args = ap.parse_args()
    if args.diagnostico:
        diagnostico()
        return

    filas = descargar(args)
    guardar(filas, args.out)
    if args.supabase:
        upsert_supabase(filas)


if __name__ == "__main__":
    main()
