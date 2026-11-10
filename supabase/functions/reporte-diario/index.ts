// Licita Perú — reporte diario por correo.
// Modos:
//  - Cron (header x-cron-secret = licita_config.cron_secret): envía a todas las empresas con email_diario
//    los procesos NUEVOS (últimas 26 h) que calzan con su perfil.
//  - Usuario con sesión (Authorization: Bearer <jwt>): le envía ahora su reporte con todos los vigentes.
// Secretos: RESEND_API_KEY (obligatorio para enviar), RESEND_FROM (opcional), APP_URL (opcional).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-cron-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const RUBROS: Record<string, string[]> = {
  "Insumos médicos": ["material medico", "descartable", "guante", "jeringa", "mascarilla", "gasa", "cateter", "sutura", "insumo medico", "quirurgic"],
  "Equipos médicos": ["equipo medico", "equipo biomedic", "biomedic", "oxigen", "monitor de funciones", "ventilador mecanico", "ecograf", "desfibrilador", "tensiometro", "oximetro"],
  "Medicamentos": ["medicamento", "farmac", "producto farmaceutico", "antibiotic", "vacuna"],
  "Laboratorio": ["reactivo", "laboratorio", "prueba rapida", "pcr", "bioseguridad"],
  "Obras y construcción": ["ejecucion de obra", "obra", "construccion", "mejoramiento", "pistas", "veredas", "saneamiento", "agua potable", "puente", "infraestructura"],
  "Consultoría y supervisión": ["consultoria", "supervision", "expediente tecnico", "estudio de preinversion", "elaboracion de estudio"],
  "Tecnología": ["computadora", "laptop", "software", "servidor", "impresora", "licencia", "equipos informaticos", "red de datos", "tecnologia"],
  "Limpieza y mantenimiento": ["limpieza", "mantenimiento", "fumigacion", "desinfeccion"],
  "Seguridad y vigilancia": ["vigilancia", "seguridad", "cctv", "videovigilancia"],
  "Alimentos": ["alimento", "viveres", "raciones", "desayuno", "alimentario"],
  "Vehículos y combustible": ["vehiculo", "camioneta", "ambulancia", "combustible", "neumatico", "maquinaria"],
  "Útiles y mobiliario": ["utiles", "papel", "toner", "mobiliario", "escritorio"],
};

type Proceso = {
  ocid: string; nomenclatura: string | null; objeto: string | null; descripcion: string | null; tipo: string | null;
  categoria: string | null; entidad: string | null; departamento: string | null; monto: number | null;
  fecha_cierre: string | null; creado: string; url: string | null;
};
type Empresa = {
  user_id: string; razon: string; ruc: string | null; rubros: string[]; kw: string | null; excl: string | null;
  monto_min: number | null; monto_max: number | null; deps: string[]; tipos: string[]; rnp: boolean;
  email_diario: boolean; email_reporte: string | null;
};

const nrm = (s: unknown) => String(s ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const lista = (s: string | null) => String(s ?? "").split(",").map(nrm).map((x) => x.trim()).filter(Boolean);
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
const soles = (n: number | null) => n == null ? "—" : "S/ " + Number(n).toLocaleString("es-PE", { maximumFractionDigits: 0 });
const fecha = (f: string | null) => f ? new Date(f).toLocaleString("es-PE", { timeZone: "America/Lima", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "Sin fecha";

function calce(r: Proceso, p: Empresa) {
  const blob = nrm(`${r.objeto ?? ""} ${r.descripcion ?? ""}`);
  for (const x of lista(p.excl)) if (blob.includes(x)) return null;
  const rub: string[] = [], por: string[] = [];
  for (const name of p.rubros ?? []) {
    const hits = (RUBROS[name] ?? []).filter((k) => blob.includes(k));
    if (hits.length) { rub.push(name); por.push(...hits); }
  }
  const propias = lista(p.kw).filter((k) => blob.includes(k));
  por.push(...propias);
  if (!por.length) return null;
  if (p.tipos?.length && !p.tipos.some((t) => nrm(r.tipo).includes(nrm(t)))) return null;
  if (p.monto_min && r.monto && r.monto < p.monto_min) return null;
  if (p.monto_max && r.monto && r.monto > p.monto_max) return null;
  if (p.deps?.length && !p.deps.some((x) => nrm(x) === nrm(r.departamento))) return null;
  let score = 55 + Math.min(30, (por.length - 1) * 10) + (propias.length ? 15 : 0) + (p.deps?.length ? 5 : 0);
  return { score: Math.min(100, score), rub: rub.length ? rub : ["Palabra clave propia"] };
}

function correoHTML(p: Empresa, ops: (Proceso & { _s: number; _r: string[] })[], nuevos: boolean, appUrl: string) {
  const total = ops.reduce((s, r) => s + (r.monto ?? 0), 0);
  const filas = ops.slice(0, 60).map((r) => `
    <tr>
      <td style="padding:10px 8px;border-bottom:1px solid #d6e2da;font:600 13px Arial;color:#0d6b44;white-space:nowrap">${r._s}%</td>
      <td style="padding:10px 8px;border-bottom:1px solid #d6e2da;font:14px Arial;color:#12261c">
        <b>${esc(r.objeto)}</b><br><span style="font-size:12px;color:#56695e">${esc(r.nomenclatura)} · ${esc(r.tipo)} · ${esc(r._r.join(", "))}</span><br>
        <span style="font-size:12px;color:#56695e">${esc(r.entidad)} · ${esc(r.departamento)}</span></td>
      <td style="padding:10px 8px;border-bottom:1px solid #d6e2da;font:13px Arial;text-align:right;white-space:nowrap">${soles(r.monto)}<br><span style="font-size:12px;color:#c2410c">Cierra ${fecha(r.fecha_cierre)}</span></td>
    </tr>`).join("");
  return `<!doctype html><html lang="es"><body style="margin:0;background:#f2f6f3;padding:24px 12px">
  <div style="max-width:680px;margin:0 auto;background:#fff;border:1px solid #d6e2da;border-radius:12px;padding:24px">
    <p style="margin:0;font:700 12px Arial;letter-spacing:.08em;color:#0d6b44">LICITA PERÚ</p>
    <h1 style="margin:6px 0 4px;font:700 22px Arial;color:#12261c">${nuevos ? "Procesos nuevos para" : "Tu reporte de"} ${esc(p.razon)}</h1>
    <p style="margin:0 0 16px;font:14px Arial;color:#56695e">${ops.length} proceso${ops.length === 1 ? "" : "s"} ${nuevos ? "publicados en las últimas 24 horas" : "vigentes"} que calzan con tus rubros · ${soles(total)} en valor referencial.</p>
    ${!p.rnp ? `<p style="background:#fdeee6;color:#7c2d12;padding:10px 12px;border-radius:8px;font:13px Arial">Tu perfil indica que no tienes RNP vigente. Sin RNP no puedes presentar ofertas.</p>` : ""}
    <table style="width:100%;border-collapse:collapse">${filas}</table>
    ${ops.length > 60 ? `<p style="font:13px Arial;color:#56695e">Y ${ops.length - 60} más en la plataforma.</p>` : ""}
    ${appUrl ? `<p style="margin:20px 0 0"><a href="${esc(appUrl)}" style="background:#0d6b44;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font:600 14px Arial">Ver en Licita Perú</a></p>` : ""}
    <p style="margin:20px 0 0;font:12px Arial;color:#56695e">Fuente: datos abiertos OCDS del OECE (SEACE). Verifica bases y cronograma en el SEACE antes de postular. Puedes desactivar este correo en tu perfil.</p>
  </div></body></html>`;
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "Usa POST" }, 405);

  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false } });
  const resendKey = Deno.env.get("RESEND_API_KEY") ?? "";
  const from = Deno.env.get("RESEND_FROM") ?? "Licita Perú <onboarding@resend.dev>";
  const appUrl = Deno.env.get("APP_URL") ?? "";

  // ¿Quién llama?
  let soloUser: string | null = null;
  const cron = req.headers.get("x-cron-secret");
  if (cron) {
    const { data } = await admin.from("licita_config").select("valor").eq("clave", "cron_secret").single();
    if (!data || data.valor !== cron) return json({ error: "Secreto inválido" }, 401);
  } else {
    const token = (req.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
    const { data, error } = await admin.auth.getUser(token);
    if (error || !data?.user) return json({ error: "Inicia sesión para pedir tu reporte" }, 401);
    soloUser = data.user.id;
    const desde = new Date(Date.now() - 864e5).toISOString();
    const { count } = await admin.from("licita_envios").select("id", { count: "exact", head: true }).eq("user_id", soloUser).gte("enviado", desde);
    if ((count ?? 0) >= 5) return json({ error: "Ya pediste 5 reportes hoy. Intenta mañana." }, 429);
  }
  const nuevos = !soloUser;

  // Empresas
  let q = admin.from("licita_empresas").select("*");
  q = soloUser ? q.eq("user_id", soloUser) : q.eq("email_diario", true);
  const { data: empresas, error: eErr } = await q;
  if (eErr) return json({ error: eErr.message }, 500);
  if (!empresas?.length) return json({ ok: true, enviados: 0, nota: soloUser ? "Primero guarda el perfil de tu empresa" : "Sin empresas suscritas" });

  // Procesos vigentes (y nuevos si es el envío diario)
  const procesos: Proceso[] = [];
  const ahora = new Date().toISOString();
  for (let from_ = 0; from_ < 20000; from_ += 1000) {
    let pq = admin.from("licita_procesos")
      .select("ocid,nomenclatura,objeto,descripcion,tipo,categoria,entidad,departamento,monto,fecha_cierre,creado,url")
      .or(`fecha_cierre.is.null,fecha_cierre.gte.${ahora}`)
      .order("ocid").range(from_, from_ + 999);
    if (nuevos) pq = pq.gte("creado", new Date(Date.now() - 26 * 3600e3).toISOString());
    const { data, error } = await pq;
    if (error) return json({ error: error.message }, 500);
    procesos.push(...(data as Proceso[]));
    if (!data || data.length < 1000) break;
  }

  const resultados: { user_id: string; estado: string; n: number }[] = [];
  for (const emp of empresas as Empresa[]) {
    const ops = procesos.map((r) => { const c = calce(r, emp); return c ? { ...r, _s: c.score, _r: c.rub } : null; })
      .filter(Boolean) as (Proceso & { _s: number; _r: string[] })[];
    ops.sort((a, b) => b._s - a._s || (a.fecha_cierre ? +new Date(a.fecha_cierre) : Infinity) - (b.fecha_cierre ? +new Date(b.fecha_cierre) : Infinity));

    let estado = "", detalle = "";
    if (!ops.length && nuevos) { estado = "sin_novedades"; }
    else {
      let to = emp.email_reporte?.trim() || "";
      if (!to) { const { data } = await admin.auth.admin.getUserById(emp.user_id); to = data?.user?.email ?? ""; }
      if (!to) { estado = "error"; detalle = "Sin correo"; }
      else if (!resendKey) { estado = "error"; detalle = "Falta configurar RESEND_API_KEY"; }
      else {
        const subject = ops.length
          ? `${ops.length} proceso${ops.length === 1 ? "" : "s"} ${nuevos ? "nuevo" + (ops.length === 1 ? "" : "s") : "vigente" + (ops.length === 1 ? "" : "s")} para ${emp.razon} · Licita Perú`
          : `Sin procesos vigentes para ${emp.razon} · Licita Perú`;
        const r = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: { Authorization: `Bearer ${resendKey}`, "Content-Type": "application/json" },
          body: JSON.stringify({ from, to: [to], subject, html: correoHTML(emp, ops, nuevos, appUrl) }),
        });
        if (r.ok) estado = "enviado"; else { estado = "error"; detalle = (await r.text()).slice(0, 300); }
      }
    }
    await admin.from("licita_envios").insert({ user_id: emp.user_id, n_procesos: ops.length, estado, detalle: detalle || null });
    resultados.push({ user_id: emp.user_id, estado, n: ops.length });
  }

  if (soloUser) {
    const r = resultados[0];
    if (r.estado === "enviado") return json({ ok: true, n: r.n });
    const { data } = await admin.from("licita_envios").select("detalle").eq("user_id", soloUser).order("enviado", { ascending: false }).limit(1).single();
    return json({ error: data?.detalle || "No se pudo enviar" }, 502);
  }
  return json({ ok: true, empresas: resultados.length, enviados: resultados.filter((r) => r.estado === "enviado").length, procesos_nuevos: procesos.length });
});
