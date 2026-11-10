-- Licita Perú en el proyecto Supabase "Titos App" (ref ahcepuufqcatzgocqjkm).
-- Ya aplicado. Se deja como registro de lo que existe en la base.

create table public.licita_procesos (
  ocid text primary key,
  release_id text, nomenclatura text, objeto text, descripcion text, tipo text, categoria text,
  entidad text, departamento text, monto numeric, moneda text default 'PEN',
  fecha_publicacion timestamptz, fecha_cierre timestamptz, estado text, url text,
  creado timestamptz not null default now(),
  actualizado timestamptz not null default now()
);
create index licita_procesos_creado_idx on public.licita_procesos (creado desc);
create index licita_procesos_cierre_idx on public.licita_procesos (fecha_cierre);

create table public.licita_empresas (
  user_id uuid primary key references auth.users(id) on delete cascade,
  razon text not null default '', ruc text default '',
  rubros text[] not null default '{}', kw text default '', excl text default '',
  monto_min numeric, monto_max numeric,
  deps text[] not null default '{}',
  tipos text[] not null default '{"Licitación Pública","Concurso Público","Contratación Directa","Adjudicación Simplificada","Subasta Inversa"}',
  rnp boolean not null default false,
  email_diario boolean not null default true,
  email_reporte text,
  creado timestamptz not null default now(),
  actualizado timestamptz not null default now()
);

create table public.licita_envios (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  enviado timestamptz not null default now(),
  n_procesos int not null default 0,
  estado text not null,
  detalle text
);

create table public.licita_config (clave text primary key, valor text not null);

create or replace function public.licita_touch() returns trigger language plpgsql set search_path = '' as $$
begin new.actualizado := now(); return new; end $$;
create trigger licita_procesos_touch before update on public.licita_procesos for each row execute function public.licita_touch();
create trigger licita_empresas_touch before update on public.licita_empresas for each row execute function public.licita_touch();

alter table public.licita_procesos enable row level security;
alter table public.licita_empresas enable row level security;
alter table public.licita_envios  enable row level security;
alter table public.licita_config  enable row level security;

create policy "procesos: usuarios con sesion leen" on public.licita_procesos for select to authenticated using (true);
create policy "empresas: cada uno ve la suya"   on public.licita_empresas for select to authenticated using ((select auth.uid()) = user_id);
create policy "empresas: cada uno crea la suya" on public.licita_empresas for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "empresas: cada uno edita la suya" on public.licita_empresas for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "empresas: cada uno borra la suya" on public.licita_empresas for delete to authenticated using ((select auth.uid()) = user_id);
create policy "envios: cada uno ve los suyos"   on public.licita_envios for select to authenticated using ((select auth.uid()) = user_id);

revoke all on public.licita_procesos, public.licita_empresas, public.licita_envios, public.licita_config from anon;
revoke all on public.licita_config from authenticated;
revoke insert, update, delete on public.licita_procesos, public.licita_envios from authenticated;

insert into public.licita_config (clave, valor) values ('cron_secret', encode(extensions.gen_random_bytes(24), 'hex'));

-- Correo diario 7:00 a. m. Lima (12:00 UTC)
create extension if not exists pg_cron;
create extension if not exists pg_net;
create or replace function public.licita_disparar_reporte() returns bigint language plpgsql security definer set search_path = '' as $$
declare secreto text; rid bigint;
begin
  select valor into secreto from public.licita_config where clave = 'cron_secret';
  select net.http_post(
    url := 'https://ahcepuufqcatzgocqjkm.supabase.co/functions/v1/reporte-diario',
    headers := jsonb_build_object('Content-Type','application/json','x-cron-secret', secreto),
    body := '{}'::jsonb, timeout_milliseconds := 60000) into rid;
  return rid;
end $$;
revoke execute on function public.licita_disparar_reporte() from anon, authenticated, public;
select cron.schedule('licita-reporte-diario', '0 12 * * *', $$select public.licita_disparar_reporte()$$);

-- Los registros con metadata app=licita no crean usuario en la app "0 papeles" (public.handle_new_user).
