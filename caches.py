"""
Memoria de corto plazo (st.cache_data) para las consultas pesadas, con limpieza central.

  * Las funciones con @guardar_en_memoria(ttl=...) guardan su resultado unos segundos.
  * limpiar() borra todas esas memorias; se llama sola cada vez que alguien guarda algo
    (codigos.registrar), así quien guarda ve su cambio al instante.
"""
import streamlit as st

_FUNCIONES = []


def guardar_en_memoria(ttl):
    def deco(f):
        g = st.cache_data(ttl=ttl, show_spinner=False)(f)
        _FUNCIONES.append(g)
        return g
    return deco


def limpiar():
    for f in _FUNCIONES:
        try:
            f.clear()
        except Exception:
            pass
