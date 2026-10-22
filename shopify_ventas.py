"""
Ventas de Shopify -> inventario, dentro de la app.

  * sincronizar_en_segundo_plano(sb): revisa Shopify como máximo cada 2 minutos mientras alguien usa la app.
  * aviso_pendiente(sb): cuántas ventas de Shopify necesitan revisión (posible doble venta, SKU sin inventario...).
  * render_ventas_shopify(sb): panel en Herramientas con las últimas ventas, avisos y botón "Sincronizar ahora".
La lógica está en sync_shopify.py.
"""
import datetime as dt

import pandas as pd
import streamlit as st

from shopify_borrador import _gql
from segundo_plano import TareaPeriodica
from sync_shopify import sincronizar

INTERVALO_SEG = 120


def _revisar(_sb):
    hora = dt.datetime.now().strftime("%H:%M")
    try:
        return {"ok": True, "resumen": sincronizar(_sb, _gql), "hora": hora}
    except Exception as ex:
        msg = str(ex)
        if "shopify_ordenes" in msg and ("does not exist" in msg or "PGRST205" in msg or "schema cache" in msg):
            msg = "Falta crear la tabla shopify_ordenes: corre ventas_shopify.sql en Supabase."
        elif "access" in msg.lower() and "denied" in msg.lower() or "read_orders" in msg:
            msg = "Shopify no deja leer pedidos: agrega el permiso read_orders a tu app de Shopify."
        return {"ok": False, "error": msg, "hora": hora}


_TAREA = TareaPeriodica(_revisar, INTERVALO_SEG)


def sincronizar_en_segundo_plano(sb):
    """Devuelve al instante el resultado de la última revisión; cada INTERVALO_SEG segundos lanza otra
    en segundo plano sin hacer esperar la página."""
    return _TAREA.obtener(sb)


@st.cache_data(ttl=60, show_spinner=False)
def aviso_pendiente(_sb):
    try:
        r = (_sb.table("shopify_ordenes").select("nombre,avisos").eq("revisado", False)
             .not_.is_("avisos", "null").execute().data)
        return r
    except Exception:
        return []


def render_ventas_shopify(sb):
    st.subheader("Ventas de Shopify",
                 help="Cuando un pedido de Shopify se paga, sus modelos se marcan como vendidos en el inventario; "
               "si el pedido se cancela o reembolsa, vuelven a disponible. La app lo revisa sola cada pocos minutos.")
    estado = sincronizar_en_segundo_plano(sb)
    if estado.get("pendiente"):
        st.caption("Revisando Shopify… recarga en unos segundos.")
    elif estado["ok"]:
        r = estado["resumen"]
        st.caption(f"Última revisión: {estado['hora']} — {r['revisados']} pedidos revisados, {r['nuevos']} nuevos, "
                   f"{r['marcados']} modelos marcados, {r['liberados']} devueltos a disponible.")
    else:
        st.error(f"No se pudo revisar Shopify: {estado['error']}")
    if st.button("Sincronizar ahora", key="sv_ahora"):
        with st.spinner("Revisando Shopify…"):
            _TAREA.ejecutar_ahora(sb)
        aviso_pendiente.clear()
        st.rerun()

    try:
        filas = (sb.table("shopify_ordenes").select("*").order("creado", desc=True).limit(50).execute().data)
    except Exception:
        return
    if not filas:
        st.info("Todavía no hay ventas de Shopify registradas.")
        return
    df = pd.DataFrame(filas)
    df["modelos"] = df["modelos"].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)
    df["estado"] = ["Anulada" if c else "Vendida" for c in df["cancelada"]]
    tabla = df[["nombre", "creado", "cliente", "total", "modelos", "estado", "avisos", "nota", "revisado"]].rename(columns={
        "nombre": "PEDIDO", "creado": "FECHA", "cliente": "CLIENTE", "total": "TOTAL", "modelos": "MODELOS",
        "estado": "ESTADO", "avisos": "AVISOS", "nota": "NOTA", "revisado": "REVISADO"})
    st.dataframe(tabla, use_container_width=True, hide_index=True)

    pendientes = df[df["avisos"].notna() & (~df["revisado"])]
    if len(pendientes):
        st.warning(f"{len(pendientes)} pedido(s) con avisos sin revisar.")
        if st.button("Marcar todos los avisos como revisados", key="sv_revisados"):
            for oid in pendientes["order_id"]:
                sb.table("shopify_ordenes").update({"revisado": True}).eq("order_id", oid).execute()
            aviso_pendiente.clear()
            st.rerun()