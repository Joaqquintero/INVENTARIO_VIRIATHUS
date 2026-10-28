"""
Precios bloqueados.

  * El precio de un producto con modelo viene del inventario (o de la cotización) y no se puede cambiar, salvo que
    se marque la casilla «Cambiar precio / descuento» y se diga quién lo autorizó (Viriato o Ernesto).
  * Los S/N y los modelos sin precio en el inventario también necesitan autorización para tener precio.
  * El descuento es libre hasta DESC_LIBRE (10%); más que eso necesita autorización.
  * Cada cambio autorizado se guarda en la tabla `cambios_precio` y aparece como aviso arriba de la app
    (roles con permiso «avisos_precio») hasta que alguien lo marque como revisado.

Tabla (correr una vez en Supabase):
    create table if not exists cambios_precio (
      id bigserial primary key,
      creado timestamptz not null default now(),
      documento text, modelo text, descripcion text,
      precio_lista numeric, precio_nuevo numeric,
      desc_lista numeric default 0, desc_nuevo numeric default 0,
      autorizo text, usuario text,
      revisado boolean not null default false, revisado_por text, revisado_en timestamptz
    );
"""
import datetime as dt

import streamlit as st

from caches import guardar_en_memoria
from permisos import puede

AUTORIZAN = ("Viriato", "Ernesto")
DESC_LIBRE = 10.0          # descuento (%) que se puede dar sin autorización
TABLA = "cambios_precio"
MAX_AVISOS = 40


def _n(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def habilitado(clave):
    """¿Está marcada la casilla de esta pantalla? (se puede leer antes de dibujarla)."""
    return bool(st.session_state.get(f"{clave}_hab"))


def control(clave):
    """Casilla para habilitar el cambio + quién lo autorizó. Devuelve (habilitado, autorizo o None)."""
    hab = st.checkbox(f"Cambiar precio o dar más de {DESC_LIBRE:g}% de descuento (requiere autorización)",
                      key=f"{clave}_hab",
                      help="El precio de los modelos viene del inventario. Para cambiarlo, ponerle precio a un S/N o a "
                           f"un modelo sin precio, o dar más de {DESC_LIBRE:g}% de descuento, marca esta casilla y "
                           "elige quién lo autorizó; el cambio queda registrado y les llega un aviso.")
    autorizo = None
    if hab:
        autorizo = st.radio("¿Quién autorizó el cambio de precio? *", AUTORIZAN, index=None, horizontal=True,
                            key=f"{clave}_aut")
    return hab, autorizo


def autorizo_de(clave):
    """Quién autorizó en la casilla de esa pantalla (None si no está marcada o no eligieron)."""
    return st.session_state.get(f"{clave}_aut") if habilitado(clave) else None


def cambio(modelo, descripcion, precio_lista, desc_lista, precio_nuevo, desc_nuevo):
    """Dict del cambio que necesita autorización, o None.
    precio_lista = precio de referencia (0 para S/N o modelo sin precio: cualquier precio necesita autorización).
    El descuento solo cuenta si pasa de DESC_LIBRE y es distinto del que ya estaba autorizado."""
    pl, pn = _n(precio_lista), _n(precio_nuevo)
    dl, dn = _n(desc_lista), _n(desc_nuevo)
    cambia_precio = pn != pl
    cambia_desc = dn > DESC_LIBRE and dn != dl
    if not (cambia_precio or cambia_desc):
        return None
    return {"modelo": str(modelo or "S/N"), "descripcion": str(descripcion or "")[:200], "precio_lista": pl,
            "precio_nuevo": pn, "desc_lista": dl, "desc_nuevo": dn}


def cambios_de_items(items):
    """Para las listas de «Nuevo pedido» / «Nueva cotización» (precio_lista / desc_lista: la referencia;
    sin precio_lista, como un S/N recién capturado, la referencia es 0)."""
    out = []
    for it in items:
        c = cambio(None if it.get("es_sn") else it.get("modelo"), it.get("descripcion"), it.get("precio_lista"),
                   it.get("desc_lista"), it.get("precio_unitario"), it.get("descuento_pct"))
        if c:
            out.append(c)
    return out


def registrar(sb, documento, cambios, autorizo, usuario):
    """Guarda los cambios autorizados. Devuelve un aviso si no se pudo (el documento ya quedó guardado)."""
    if not cambios:
        return None
    filas = [{**c, "documento": documento, "autorizo": autorizo, "usuario": usuario} for c in cambios]
    try:
        sb.table(TABLA).insert(filas).execute()
    except Exception as ex:
        return (f"Se guardó, pero no se pudo registrar el aviso del cambio de precio ({ex}). "
                "¿Ya se creó la tabla «cambios_precio» en Supabase?")
    finally:
        try:
            _pendientes_mem.clear()
        except Exception:
            pass
    return None


@guardar_en_memoria(ttl=60)
def _pendientes_mem(_sb):
    return (_sb.table(TABLA).select("*").eq("revisado", False)
            .order("creado", desc=True).limit(200).execute().data or [])


def pendientes(sb):
    try:
        return _pendientes_mem(sb)
    except Exception:
        return []          # todavía no existe la tabla


def _texto(p):
    precio = f"${_n(p['precio_lista']):,.2f} → **${_n(p['precio_nuevo']):,.2f}**"
    if _n(p.get("desc_lista")) != _n(p.get("desc_nuevo")) or _n(p.get("desc_nuevo")):
        precio += f" · desc. {_n(p.get('desc_lista')):g}% → **{_n(p.get('desc_nuevo')):g}%**"
    try:
        cuando = dt.datetime.fromisoformat(str(p["creado"]).replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=-6))).strftime("%d/%m %H:%M")
    except Exception:
        cuando = ""
    return (f"**{p.get('documento') or ''}** · Modelo {p.get('modelo')} {p.get('descripcion') or ''} · {precio}  \n"
            f"Autorizó **{p.get('autorizo') or '—'}** · lo capturó {p.get('usuario') or '—'} · {cuando}")


def _marcar(sb, ids, user):
    sb.table(TABLA).update({"revisado": True, "revisado_por": user.get("nombre") or user.get("role"),
                            "revisado_en": dt.datetime.now(dt.timezone.utc).isoformat()}).in_("id", ids).execute()
    _pendientes_mem.clear()


def render_aviso(sb, user):
    """Aviso arriba de la app con los cambios de precio sin revisar."""
    if not puede(user, "avisos_precio"):
        return
    pend = pendientes(sb)
    if not pend:
        return
    st.warning(f"💲 {len(pend)} cambio(s) de precio autorizados por revisar.")
    with st.expander("Ver cambios de precio"):
        for p in pend[:MAX_AVISOS]:
            c1, c2 = st.columns([6, 1])
            c1.markdown(_texto(p))
            if c2.button("Enterado", key=f"cp_ok_{p['id']}", use_container_width=True):
                _marcar(sb, [p["id"]], user)
                st.rerun()
        if len(pend) > MAX_AVISOS:
            st.caption(f"Se muestran {MAX_AVISOS} de {len(pend)}.")
        if len(pend) > 1 and st.button("Marcar todos como revisados", key="cp_ok_todos"):
            _marcar(sb, [p["id"] for p in pend], user)
            st.rerun()
