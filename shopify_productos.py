"""
Sincronización automática de NOMBRE, DESCRIPCIÓN y PRECIO entre el inventario y Shopify, dentro de la app.
La lógica está en sync_productos.py.

  * sincronizar_productos_en_segundo_plano(sb): trae/manda los cambios (como máximo cada 5 minutos mientras se usa la app).
  * conflictos_pendientes(sb): productos donde los dos lados cambiaron distinto y hay que elegir.
  * registrar_envio(sb, modelo, pdata, campos): anota lo que se acaba de mandar, para no confundirlo con un cambio.
  * sincronizar_ficha(sb, modelo, vendido): botón de una ficha.
  * render_sync_productos(sb): panel en Herramientas.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from shopify_borrador import _gql, actualizar_producto_shopify, marcar_disponibilidad_shopify
from sync_productos import (registrar_snapshot, resolver_conflicto, sincronizar_productos,
                            sincronizar_un_producto, valores_inventario)

INTERVALO_SEG = 300


def empujar(modelo, pdata, campos):
    """Manda a Shopify solo los campos indicados ('nombre', 'desc', 'precio'). Devuelve (ok, mensaje)."""
    est, msg, _ = actualizar_producto_shopify(
        modelo, pdata, enviar_nombre="nombre" in campos, enviar_descripcion="desc" in campos,
        enviar_precio="precio" in campos)
    return est == "ok", msg


def _mensaje_error(ex):
    msg = str(ex)
    if "shopify_producto_sync" in msg or "shopify_sync_estado" in msg:
        if "does not exist" in msg or "PGRST205" in msg or "schema cache" in msg:
            return "Falta crear las tablas de sincronización: corre shopify_productos.sql en Supabase."
    return msg


@st.cache_data(ttl=INTERVALO_SEG, show_spinner=False)
def _sync_cacheado(_sb):
    hora = dt.datetime.now().strftime("%H:%M")
    try:
        return {"ok": True, "resumen": sincronizar_productos(_sb, _gql, empujar), "hora": hora}
    except Exception as ex:
        return {"ok": False, "error": _mensaje_error(ex), "hora": hora}


def sincronizar_productos_en_segundo_plano(sb):
    return _sync_cacheado(sb)


@st.cache_data(ttl=60, show_spinner=False)
def conflictos_pendientes(_sb):
    try:
        filas = (_sb.table("shopify_producto_sync").select("modelo,conflicto")
                 .not_.is_("conflicto", "null").execute().data)
        return [f for f in filas if f.get("conflicto")]
    except Exception:
        return []


def registrar_envio(sb, modelo, pdata, campos):
    """Anota como 'igual en los dos lados' los campos que se acaban de mandar a Shopify."""
    try:
        vals = valores_inventario(pdata)
        registrar_snapshot(sb, modelo, {c: vals[c] for c in campos})
    except Exception:
        pass     # si falla, la próxima revisión lo reconcilia


def sincronizar_ficha(sb, modelo, vendido):
    """Revisa un producto contra Shopify (nombre, descripción, precio) y su disponibilidad.
    Devuelve (estado, mensaje): 'ok' | 'no_existe' | 'error'."""
    try:
        r = sincronizar_un_producto(sb, _gql, empujar, modelo)
    except Exception as ex:
        return "error", f"Error al revisar el producto: {_mensaje_error(ex)}"
    if r is None:
        return "no_existe", "Este producto no está en Shopify."
    est, msg = marcar_disponibilidad_shopify(modelo, vendido)
    if est == "error":
        return "error", msg
    conflictos_pendientes.clear()
    partes = [f"{r['traidos']} campo(s) traído(s) de Shopify", f"{r['enviados']} enviado(s)"]
    if r["conflictos"]:
        partes.append(f"{r['conflictos']} en conflicto (resuélvelos en Herramientas)")
    texto = "Revisado: " + ", ".join(partes) + ". " + msg
    if r["errores"]:
        return "error", texto + " Errores: " + "; ".join(r["errores"])
    return "ok", texto


def _fmt(campo, v):
    if campo == "precio":
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return str(v)
    t = " ".join(str(v or "").split())
    return t if len(t) <= 160 else t[:160] + "…"


def render_sync_productos(sb):
    st.subheader("🔁 Sincronización de productos con Shopify",
                 help="Nombre, descripción y precio se mantienen iguales en el inventario y en Shopify: lo que cambias en un "
               "lado se copia al otro. Si cambias el mismo dato en los dos lados con valores distintos, no se toca "
               "nada y te pide elegir. Nunca se manda un dato vacío ni un precio en 0.")
    estado = sincronizar_productos_en_segundo_plano(sb)
    if estado["ok"]:
        r = estado["resumen"]
        if r.get("inicial"):
            st.info("Primera vez: se marcó el punto de partida. Pulsa «Revisar TODOS los productos» para igualar lo que ya existe.")
        else:
            st.caption(f"Última revisión: {estado['hora']} — {r['revisados']} productos cambiados en Shopify revisados, "
                       f"{r['traidos']} campos traídos, {r['enviados']} enviados, {r['conflictos']} conflictos.")
        if r["errores"]:
            st.warning("Errores: " + "; ".join(r["errores"][:5]))
    else:
        st.error(f"No se pudo sincronizar: {estado['error']}")

    c1, c2 = st.columns(2)
    if c1.button("🔄 Revisar cambios ahora", key="sp_ahora"):
        _sync_cacheado.clear()
        conflictos_pendientes.clear()
        st.rerun()
    if c2.button("🔍 Revisar TODOS los productos (más lento)", key="sp_todos",
                 help="Compara todos los productos de Shopify con el inventario. Úsalo la primera vez y de vez en cuando."):
        barra = st.progress(0.0, text="Revisando productos…")
        try:
            r = sincronizar_productos(sb, _gql, empujar, completo=True,
                                      progreso=lambda n, t: barra.progress(n / max(t, 1), text=f"{n} de {t}"))
            st.success(f"Listo: {r['revisados']} productos revisados, {r['traidos']} campos traídos de Shopify, "
                       f"{r['enviados']} enviados, {r['conflictos']} conflictos.")
            if r["errores"]:
                st.warning("Errores: " + "; ".join(r["errores"][:5]))
        except Exception as ex:
            st.error(f"❌ {_mensaje_error(ex)}")
        _sync_cacheado.clear()
        conflictos_pendientes.clear()

    try:
        conflictos = (sb.table("shopify_producto_sync").select("modelo,conflicto")
                      .not_.is_("conflicto", "null").execute().data)
        conflictos = [c for c in conflictos if c.get("conflicto")]
    except Exception:
        conflictos = []
    if conflictos:
        st.markdown("#### ⚠️ Conflictos por resolver")
        st.caption("Estos datos cambiaron distinto en el inventario y en Shopify. Elige cuál se queda; el otro lado se actualiza.")
        for fila in conflictos[:50]:
            for campo, c in fila["conflicto"].items():
                with st.container(border=True):
                    st.write(f"**Modelo {fila['modelo']}** — {campo if campo != 'desc' else 'descripción'}")
                    a, b = st.columns(2)
                    a.markdown(f"**Inventario:** {_fmt(campo, c['inv'])}")
                    b.markdown(f"**Shopify:** {_fmt(campo, c['shopify'])}")
                    k1, k2 = st.columns(2)
                    for texto, usar, col in (("Usar el del inventario", "inventario", k1), ("Usar el de Shopify", "shopify", k2)):
                        if col.button(texto, key=f"sp_res_{fila['modelo']}_{campo}_{usar}", use_container_width=True):
                            ok, msg = resolver_conflicto(sb, empujar, fila["modelo"], campo, usar)
                            conflictos_pendientes.clear()
                            if ok:
                                st.rerun()
                            else:
                                st.error(msg)

    try:
        ult = (sb.table("shopify_producto_sync").select("modelo,ultimo_cambio,actualizado")
               .not_.is_("ultimo_cambio", "null").order("actualizado", desc=True).limit(20).execute().data)
    except Exception:
        ult = []
    if ult:
        with st.expander("Últimos cambios sincronizados"):
            st.dataframe(pd.DataFrame(ult)[["modelo", "ultimo_cambio"]].rename(
                columns={"modelo": "MODELO", "ultimo_cambio": "CAMBIO"}), use_container_width=True, hide_index=True)