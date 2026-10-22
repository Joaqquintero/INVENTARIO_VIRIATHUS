"""
CÓDIGOS DE VENDEDOR: para que todo lo que se crea o se edita quede registrado por quien realmente lo hizo.

  * Cada vendedor tiene un código de 4 dígitos (lo asigna el Administrador en la pestaña «Códigos»).
  * Al crear / editar / cobrar / cancelar, la persona elige su nombre y escribe SU código. Si no coincide, no se guarda.
  * No importa de quién sea el pedido: cualquiera puede editarlo, pero queda registrado quién fue.
  * 5 intentos fallidos bloquean a esa persona 10 minutos.

Requiere vendedor_codigos.sql (tablas vendedor_codigos y registro_cambios).
"""
import datetime as dt
import random

import pandas as pd
import streamlit as st

MAX_INTENTOS = 5
BLOQUEO_MIN = 10
LARGO = 4


def _ahora():
    return dt.datetime.now(dt.timezone.utc)


def _vendedores(sb):
    from ventas import catalogo
    return catalogo(sb, "vendedor")


def _fila(sb, vendedor):
    r = sb.table("vendedor_codigos").select("*").eq("vendedor", vendedor).limit(1).execute().data
    return r[0] if r else None


def verificar(sb, vendedor, codigo):
    """Devuelve (ok, mensaje). Cuenta intentos fallidos y bloquea tras MAX_INTENTOS."""
    if not vendedor:
        return False, "Elige quién eres."
    codigo = str(codigo or "").strip()
    if not codigo:
        return False, "Escribe tu código."
    fila = _fila(sb, vendedor)
    if not fila:
        return False, f"{vendedor} aún no tiene código asignado. Pídeselo al administrador."
    bloq = fila.get("bloqueado_hasta")
    if bloq:
        try:
            hasta = dt.datetime.fromisoformat(str(bloq).replace("Z", "+00:00"))
            if hasta > _ahora():
                mins = int((hasta - _ahora()).total_seconds() // 60) + 1
                return False, f"Demasiados intentos. {vendedor} queda bloqueado unos {mins} min."
        except ValueError:
            pass
    if codigo == str(fila.get("codigo") or "").strip():
        if fila.get("fallos") or fila.get("bloqueado_hasta"):
            sb.table("vendedor_codigos").update({"fallos": 0, "bloqueado_hasta": None}).eq("vendedor", vendedor).execute()
        return True, "ok"
    fallos = int(fila.get("fallos") or 0) + 1
    cambios = {"fallos": fallos}
    if fallos >= MAX_INTENTOS:
        cambios = {"fallos": 0, "bloqueado_hasta": (_ahora() + dt.timedelta(minutes=BLOQUEO_MIN)).isoformat()}
        sb.table("vendedor_codigos").update(cambios).eq("vendedor", vendedor).execute()
        return False, f"Código incorrecto. {vendedor} queda bloqueado {BLOQUEO_MIN} minutos."
    sb.table("vendedor_codigos").update(cambios).eq("vendedor", vendedor).execute()
    return False, f"Código incorrecto ({MAX_INTENTOS - fallos} intento(s) antes del bloqueo)."


def registrar(sb, vendedor, accion, entidad, referencia, detalle=""):
    """Anota quién hizo qué. Si falla (p. ej. falta la tabla) no interrumpe el guardado."""
    try:
        sb.table("registro_cambios").insert({
            "vendedor": vendedor, "accion": accion, "entidad": entidad,
            "referencia": str(referencia), "detalle": str(detalle)[:500]}).execute()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Widgets
# ----------------------------------------------------------------------
def pedir_identidad(sb, clave, fijo=None):
    """Muestra «¿Quién eres?» + código. Con `fijo` (nombre) solo pide el código de esa persona (creación: primer vendedor).
    Devuelve (vendedor, codigo)."""
    ss = st.session_state
    c1, c2 = st.columns([2, 1])
    if fijo:
        c1.markdown(f"**Responsable: {fijo}** — escribe su código")
        vend = fijo
    else:
        lista = _vendedores(sb)
        ultimo = ss.get("cod_ultimo")
        vend = c1.selectbox("¿Quién eres?", lista, index=lista.index(ultimo) if ultimo in lista else None,
                            key=f"{clave}_quien", placeholder="Elige tu nombre…")
    cod = c2.text_input("Código", type="password", max_chars=LARGO, key=f"{clave}_cod",
                        help=f"Tu código personal de {LARGO} dígitos.")
    return vend, cod


def confirmar(sb, vendedor, codigo):
    """Verifica y muestra el error si no coincide. Devuelve el nombre verificado o None."""
    try:
        ok, msg = verificar(sb, vendedor, codigo)
    except Exception as ex:
        st.error(f"No se pudo verificar el código: {ex}. ¿Ya corriste vendedor_codigos.sql en Supabase?")
        return None
    if not ok:
        st.error(f"{msg}")
        return None
    st.session_state["cod_ultimo"] = vendedor
    return vendedor


# ----------------------------------------------------------------------
# Pantalla del administrador
# ----------------------------------------------------------------------
def _nuevo_codigo(usados):
    while True:
        c = f"{random.randint(0, 10 ** LARGO - 1):0{LARGO}d}"
        if c not in usados:
            return c


def tab_codigos(sb, user):
    st.subheader("Códigos de vendedor")
    st.caption(f"Cada vendedor necesita un código de {LARGO} dígitos para crear o editar pedidos y cotizaciones. "
               "Solo el Administrador ve esta pantalla.")
    try:
        vendedores = _vendedores(sb)
        filas = {f["vendedor"]: f for f in sb.table("vendedor_codigos").select("*").execute().data}
    except Exception as ex:
        st.error(f"No se pudo leer. ¿Ya corriste vendedor_codigos.sql en Supabase? Detalle: {ex}")
        return
    usados = {str(f.get("codigo")) for f in filas.values()}

    sin_codigo = [v for v in vendedores if v not in filas]
    if sin_codigo:
        st.warning("Sin código: " + ", ".join(sin_codigo))
        if st.button("Asignar un código aleatorio a los que no tienen", key="cod_auto"):
            for v in sin_codigo:
                c = _nuevo_codigo(usados)
                usados.add(c)
                sb.table("vendedor_codigos").insert({"vendedor": v, "codigo": c}).execute()
            st.rerun()

    for v in vendedores:
        f = filas.get(v)
        c1, c2, c3, c4 = st.columns([2, 1.2, 1, 1])
        c1.markdown(f"**{v}**" + (" (bloqueado)" if f and f.get("bloqueado_hasta") and str(f["bloqueado_hasta"]) > _ahora().isoformat() else ""))
        nuevo = c2.text_input("Código", value=(f or {}).get("codigo", ""), max_chars=LARGO,
                              key=f"cod_ed_{v}", label_visibility="collapsed", placeholder="----")
        if c3.button("Guardar", key=f"cod_g_{v}"):
            n = nuevo.strip()
            if not (n.isdigit() and len(n) == LARGO):
                st.error(f"El código debe tener {LARGO} dígitos.")
            elif any(o != v and str(filas[o].get("codigo")) == n for o in filas):
                st.error("Ese código ya lo tiene otra persona.")
            else:
                sb.table("vendedor_codigos").upsert({"vendedor": v, "codigo": n, "fallos": 0,
                                                     "bloqueado_hasta": None}).execute()
                registrar(sb, user.get("nombre", "admin"), "Código cambiado", "vendedor", v)
                st.rerun()
        if c4.button("Aleatorio", key=f"cod_r_{v}", help="Generar uno aleatorio"):
            sb.table("vendedor_codigos").upsert({"vendedor": v, "codigo": _nuevo_codigo(usados), "fallos": 0,
                                                 "bloqueado_hasta": None}).execute()
            st.rerun()

    with st.expander("Registro de cambios (quién hizo qué)"):
        try:
            datos = sb.table("registro_cambios").select("*").order("id", desc=True).limit(300).execute().data
        except Exception as ex:
            st.caption(f"No se pudo leer el registro: {ex}")
            datos = []
        if datos:
            df = pd.DataFrame(datos)[["fecha", "vendedor", "accion", "entidad", "referencia", "detalle"]]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("Todavía no hay movimientos registrados.")