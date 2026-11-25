"""
Memoria de corto plazo (st.cache_data) para las consultas pesadas, con limpieza central.

  * Las funciones con @guardar_en_memoria(ttl=...) guardan su resultado unos segundos.
  * limpiar() borra todas esas memorias; se llama sola cada vez que alguien guarda algo
    (codigos.registrar), así quien guarda ve su cambio al instante.
"""
import streamlit as st

_FUNCIONES = []
_VERSION = [0]          # sube cada vez que alguien guarda algo: las memorias de cada sesión saben que quedaron viejas


def version():
    """Número que cambia cada vez que se llama a limpiar() (o sea, cada vez que alguien guarda algo)."""
    return _VERSION[0]


def guardar_en_memoria(ttl):
    def deco(f):
        g = st.cache_data(ttl=ttl, max_entries=30, show_spinner=False)(f)
        _FUNCIONES.append(g)
        return g
    return deco


def limpiar():
    _VERSION[0] += 1
    for f in _FUNCIONES:
        try:
            f.clear()
        except Exception:
            pass
