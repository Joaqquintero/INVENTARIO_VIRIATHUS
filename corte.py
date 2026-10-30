"""
Desde qué número de pedido se muestran los pedidos en la app.

Los pedidos anteriores siguen en la base de datos, pero no aparecen en Pedidos, Por facturar, Resumen mensual,
Comisiones, Costos pendientes, Caudal ni en las sugerencias de clientes.

Para cambiarlo: en los secretos de la app (Streamlit Cloud → Settings → Secrets, o .streamlit/secrets.toml)
    PEDIDO_DESDE = 8999      # 0 = mostrar todos
"""
import streamlit as st

PEDIDO_DESDE_DEFECTO = 8999


def pedido_desde():
    try:
        return int(st.secrets.get("PEDIDO_DESDE", PEDIDO_DESDE_DEFECTO))
    except Exception:
        return PEDIDO_DESDE_DEFECTO
