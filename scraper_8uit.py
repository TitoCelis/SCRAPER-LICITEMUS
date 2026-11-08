#!/usr/bin/env python3
"""
Scraper de Contrataciones Menores o Iguales a 8 UIT (buscador público SEACE).
CMI General Solutions

Usa la API pública que consume la web
  https://prep6.seace.gob.pe/buscador-publico/contrataciones
y guarda contratos_8uit.json + contratos_8uit.csv (mismo estilo que scraper_oece.py).

Uso:
  pip install requests
  python scraper_8uit.py                         # año actual, todo
  python scraper_8uit.py --anio 2026 --keyword "medic" --vigentes
  python scraper_8uit.py --objeto bien,servicio --dias 3
  python scraper_8uit.py --supabase              # upsert (tabla contratos_8uit, PK = id_contrato)

Si OECE publica la misma app en otro dominio (producción), cámbialo con --base.
"""
import argparse
import csv
import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta

import requests

BASE_DEFAULT = "https://prep6.seace.gob.pe"
SERVICIO = "/v1/s8uit-services/buscadorpublico"
HEADERS = {"User-Agent": "CMI-Radar/1.0 (+scraper 8UIT)", "Accept": "application/json"}


def norm(txt):
    txt = unicodedata.normalize("NFKD", str(txt or ""))
    return "".join(c for c in txt if not unicodedata.combining(c)).lower().strip()


def fecha_iso(s):
    """'06/11/2026 15:07:28' -> '2026-11-06T15:07:28' (hora Perú)."""
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            pass
    return s


class Api:
    def __init__(self, base, pausa):
        self.root = base.rstrip("/") + SERVICIO
        self.pausa = pausa
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def get(self, path, **params):
        params = {k: v for k, v in params.items() if v not in (None, "", [])}
        for intento in range(4):
            try:
                r = self.s.get(self.root + path, params=params, timeout=60)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                espera = 5 * (intento + 1)
                print(f"  ! {path}: {e} — reintento en {espera}s", file=sys.stderr)
                time.sleep(espera)
        raise SystemExit(f"Demasiados errores en {path}")

    def maestra(self, nombre):
        d = self.get(f"/maestras/{nombre}")
        return d.get("data", d) if isinstance(d, dict) else d


def resolver_codigos(lista, valores, campo_id="id", campo_nom="nom"):
    """Traduce nombres ('bien,servicio') a ids usando una tabla maestra."""
    if not valores:
        return None
    pedidos = [norm(v) for v in valores.split(",") if v.strip()]
    ids = [str(x[campo_id]) for x in lista
           if any(p in norm(x.get(campo_nom)) for p in pedidos)]
    if not ids:
        opciones = ", ".join(str(x.get(campo_nom)) for x in lista)
        raise SystemExit(f"No reconozco '{valores}'. Opciones: {opciones}")
    return ",".join(ids)


def aplanar(c, base):
    return {
        "id_contrato": c.get("idContrato"),
        "nro_contratacion": c.get("nroContratacion"),
        "codigo": c.get("desContratacion"),
        "objeto": c.get("nomObjetoContrato"),
        "descripcion": (c.get("desObjetoContrato") or "").strip(),
        "entidad": (c.get("nomEntidad") or "").strip(),
        "estado": c.get("nomEstadoContrato"),
        "fecha_publicacion": fecha_iso(c.get("fecPublica")),
        "inicio_cotizacion": fecha_iso(c.get("fecIniCotizacion")),
        "fin_cotizacion": fecha_iso(c.get("fecFinCotizacion")),
        "se_puede_cotizar": c.get("cotizar"),
        "tipo_cotizacion": c.get("idTipoCotizacion"),
        "url": f"{base.rstrip('/')}/buscador-publico/contrataciones/{c.get('idContrato')}",
    }


def descargar(args):
    api = Api(args.base, args.pausa)

    objetos = estados = None
    if args.objeto:
        objetos = resolver_codigos(api.maestra("listar-objeto-contratacion"), args.objeto)
    if args.estado or args.vigentes:
        estados = resolver_codigos(api.maestra("listar-estados-contrato-cotizacion"),
                                   args.estado or "vigente")
    depto = None
    if args.departamento:
        depto = (args.departamento if args.departamento.isdigit() else
                 resolver_codigos(api.maestra("listar-departamento"), args.departamento).split(",")[0])

    corte = (datetime.now() - timedelta(days=args.dias)) if args.dias else None
    filas, pagina, total = {}, 1, None
    while pagina <= args.max_paginas:
        d = api.get("/contrataciones/buscador",
                    anio=args.anio or datetime.now().year, palabra_clave=args.keyword or None,
                    lista_codigo_objeto=objetos, lista_estado_contrato=estados,
                    codigo_departamento=depto,
                    page=pagina, page_size=args.page_size)
        data = d.get("data") or []
        total = (d.get("pageable") or {}).get("totalElements", total)
        if not data:
            break
        mas_antiguo = None
        for c in data:
            row = aplanar(c, args.base)
            fp = row["fecha_publicacion"]
            if fp and (mas_antiguo is None or fp < mas_antiguo):
                mas_antiguo = fp
            if corte and fp and fp < corte.isoformat():
                continue
            # la API exige 'anio' pero no lo aplica: filtramos aquí
            if args.anio and fp and not fp.startswith(str(args.anio)):
                continue
            filas[row["id_contrato"]] = row
        print(f"  pág {pagina}: {len(data)} · hasta {mas_antiguo} · {len(filas)} guardados (API: {total})")
        if ((corte and mas_antiguo and mas_antiguo < corte.isoformat()) or
                (args.anio and mas_antiguo and mas_antiguo[:4] < str(args.anio))):
            break  # vienen ordenados del más nuevo al más antiguo
        if len(data) < args.page_size or (total and pagina * args.page_size >= total):
            break
        pagina += 1
        time.sleep(args.pausa)

    print(f"Total: {len(filas)} contrataciones ≤ 8 UIT.")
    return sorted(filas.values(), key=lambda x: x["fecha_publicacion"] or "", reverse=True)


def guardar(filas, out):
    with open(f"{out}.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now().isoformat(), "contratos": filas},
                  f, ensure_ascii=False, indent=1)
    if filas:
        with open(f"{out}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
            w.writeheader()
            w.writerows(filas)
    print(f"Guardado: {out}.json / {out}.csv")


def upsert_supabase(filas):
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    tabla = os.getenv("SUPABASE_TABLE_8UIT", "contratos_8uit")
    if not (url and key):
        print("  ! Falta SUPABASE_URL / SUPABASE_KEY, salto el upsert.", file=sys.stderr)
        return
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates"}
    for i in range(0, len(filas), 500):
        r = requests.post(f"{url}/rest/v1/{tabla}?on_conflict=id_contrato", headers=h,
                          json=filas[i:i + 500], timeout=60)
        if r.status_code >= 300:
            print(f"  ! Supabase {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return
    print(f"Supabase: {len(filas)} filas en {tabla}.")


def main():
    ap = argparse.ArgumentParser(description="Radar de contrataciones ≤ 8 UIT (SEACE)")
    ap.add_argument("--anio", type=int, default=datetime.now().year, help="0 = todos los años")
    ap.add_argument("--dias", type=int, default=0, help="solo los publicados en los últimos N días")
    ap.add_argument("--keyword", default="", help="palabra clave (búsqueda del propio SEACE)")
    ap.add_argument("--objeto", default="", help="bien, servicio, obra, consultoria (separados por coma)")
    ap.add_argument("--estado", default="", help="vigente, culminado, ... (separados por coma)")
    ap.add_argument("--vigentes", action="store_true", help="atajo para --estado vigente")
    ap.add_argument("--departamento", default="", help="nombre (LIMA, CUSCO...) o código")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-paginas", type=int, default=500)
    ap.add_argument("--pausa", type=float, default=0.5)
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--out", default="contratos_8uit")
    ap.add_argument("--supabase", action="store_true")
    args = ap.parse_args()

    filas = descargar(args)
    guardar(filas, args.out)
    if args.supabase:
        upsert_supabase(filas)


if __name__ == "__main__":
    main()
