"""
Reportes (menú principal → «Reportes»). Los ven los mismos roles que el resumen mensual (permisos «resumen_mensual»).

  * Resumen mensual: ventas sin IVA, costos y utilidad por mes (el mismo que estaba en Ventas).
  * Resumen anual: lo mismo, una fila por año.
  * Descuentos: por pieza vendida (solo pedidos PAGADOS), el precio de lista contra lo que pagó el cliente.
      - Precio de lista = REDONDEO SHOWROOM de la ficha (con IVA). Pieza S/N o ficha sin precio: el precio
        unitario que se puso en el pedido.
      - Precio de venta final = lo que pagó el cliente por esa pieza (renglón del pedido, con IVA y con descuento).
      - Descuento = lista − final. OJO: el pedido no guarda cuánto valía la ficha el día de la venta; si después
        se le cambió el precio, se compara contra el precio de hoy.
    No cuenta el envío ni pedidos cancelados. Todo con IVA, como los precios.
"""
import json
import datetime as dt

import pandas as pd
import streamlit as st

from caches import guardar_en_memoria
from corte import pedido_desde
from navegacion import descargar, selector
from permisos import roles

ROLES_REPORTES = roles("resumen_mensual")


def _num(v, defecto=0.0):
    try:
        if v is None or v == "":
            return defecto
        return float(str(v).replace("$", "").replace(",", "").strip())
    except Exception:
        return defecto


def _paginado(consulta):
    """Todas las filas de una consulta (de a 1000)."""
    filas, desde = [], 0
    while True:
        r = consulta().range(desde, desde + 999).execute().data or []
        filas += r
        if len(r) < 1000:
            return filas
        desde += 1000


def _en_partes(lista, n=150):
    lista = list(lista)
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


# ----------------------------------------------------------------------
# Resumen anual
# ----------------------------------------------------------------------
@guardar_en_memoria(ttl=120)
def _resumen_anual_mem(_sb, solo_pagados):
    from ventas import es_pagado
    filas = _paginado(lambda: _sb.table("pedidos")
                      .select("numero_pedido,anio,subtotal,costo_total,utilidad,status,estado_pedido")
                      .gte("numero_pedido", pedido_desde()).order("numero_pedido"))
    df = pd.DataFrame(filas, columns=["numero_pedido", "anio", "subtotal", "costo_total", "utilidad",
                                      "status", "estado_pedido"])
    if df.empty:
        return df
    for c in ("subtotal", "costo_total", "utilidad"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df[(df["status"].fillna("") != "Cancelado") & (df["estado_pedido"].fillna("") != "Cancelado")]
    if solo_pagados:
        df = df[[es_pagado(a, b) for a, b in zip(df["status"], df["estado_pedido"])]]
    df = df[df["anio"].notna()]
    df = df.assign(anio=df["anio"].astype(int), sin_costo=(df["costo_total"] <= 0).astype(int))
    por = (df.groupby("anio").agg(pedidos=("numero_pedido", "count"), ventas=("subtotal", "sum"),
                                  costos=("costo_total", "sum"), utilidad=("utilidad", "sum"),
                                  sin_costo=("sin_costo", "sum")).reset_index().sort_values("anio"))
    por["margen"] = [(u / v * 100.0) if v else 0.0 for u, v in zip(por["utilidad"], por["ventas"])]
    return por


def tab_anual(sb, user):
    solo_pagados = st.checkbox("Solo pedidos pagados (status Pagado u observación \"Pagado…\")", key="rp_an_pagados")
    st.caption("Ventas = subtotal sin IVA de los pedidos del año (por fecha del pedido). "
               "No incluye pedidos cancelados. Utilidad = ventas − costos.")
    try:
        df = _resumen_anual_mem(sb, solo_pagados)
    except Exception as ex:
        st.error(f"No se pudo calcular el resumen anual: {ex}")
        return
    if df.empty:
        st.info("No hay pedidos.")
        return
    tv, tc, tu = df["ventas"].sum(), df["costos"].sum(), df["utilidad"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("VENTAS SIN IVA (todos los años)", f"${tv:,.2f}")
    m2.metric("COSTOS", f"${tc:,.2f}")
    m3.metric("UTILIDAD", f"${tu:,.2f}")
    m4.metric("MARGEN", f"{(tu / tv * 100.0) if tv else 0.0:,.1f}%")

    # Crecimiento contra el año anterior
    df = df.assign(crec=[None] + [((b - a) / a * 100.0) if a else None
                                  for a, b in zip(df["ventas"].iloc[:-1], df["ventas"].iloc[1:])])
    tabla = df.rename(columns={"anio": "AÑO", "pedidos": "PEDIDOS", "ventas": "VENTAS SIN IVA", "costos": "COSTOS",
                               "utilidad": "UTILIDAD", "margen": "MARGEN %", "crec": "VS. AÑO ANTERIOR %",
                               "sin_costo": "PEDIDOS SIN COSTO"})[
        ["AÑO", "PEDIDOS", "VENTAS SIN IVA", "VS. AÑO ANTERIOR %", "COSTOS", "UTILIDAD", "MARGEN %",
         "PEDIDOS SIN COSTO"]]
    tabla["AÑO"] = tabla["AÑO"].astype(str)
    st.dataframe(tabla, use_container_width=True, hide_index=True, column_config={
        "VENTAS SIN IVA": st.column_config.NumberColumn(format="$%.2f"),
        "COSTOS": st.column_config.NumberColumn(format="$%.2f"),
        "UTILIDAD": st.column_config.NumberColumn(format="$%.2f"),
        "MARGEN %": st.column_config.NumberColumn(format="%.1f%%"),
        "VS. AÑO ANTERIOR %": st.column_config.NumberColumn(format="%+.1f%%")})
    if pedido_desde():
        st.caption(f"Cuenta desde el pedido {pedido_desde()} (los anteriores no se muestran en la app): "
                   "el primer año puede estar incompleto.")
    st.bar_chart(tabla.set_index("AÑO")[["VENTAS SIN IVA", "COSTOS", "UTILIDAD"]])
    descargar(st, "Descargar resumen anual (CSV)", tabla.to_csv(index=False).encode("utf-8-sig"),
              file_name="resumen_anual.csv", mime="text/csv")


# ----------------------------------------------------------------------
# Descuentos
# ----------------------------------------------------------------------
@guardar_en_memoria(ttl=120)
def _descuentos_mem(_sb, anio):
    """Un renglón por pieza vendida en pedidos PAGADOS del año (sin cancelados)."""
    from ventas import es_pagado
    peds = _paginado(lambda: _sb.table("pedidos")
                     .select("numero_pedido,fecha,mes,cliente,status,estado_pedido")
                     .eq("anio", anio).gte("numero_pedido", pedido_desde()).order("numero_pedido"))
    peds = {p["numero_pedido"]: p for p in peds if es_pagado(p.get("status"), p.get("estado_pedido"))}
    if not peds:
        return pd.DataFrame()
    items, vends = [], {}
    for parte in _en_partes(peds):
        items += _paginado(lambda: _sb.table("pedido_items")
                           .select("numero_pedido,modelo,es_sn,descripcion,cantidad,precio_unitario,total_linea,"
                                   "descuento_pct").in_("numero_pedido", parte).order("id"))
        for v in _paginado(lambda: _sb.table("pedido_vendedores").select("numero_pedido,vendedor")
                           .in_("numero_pedido", parte)):
            vends.setdefault(v["numero_pedido"], []).append(str(v.get("vendedor") or "").strip())
    modelos = {str(i.get("modelo") or "").strip() for i in items} - {""}
    lista_ficha = {}
    for parte in _en_partes(modelos):
        for r in _sb.table("productos").select("MODELO,datos_json").in_("MODELO", parte).execute().data or []:
            d = r.get("datos_json")
            try:
                d = json.loads(d) if isinstance(d, str) else (d or {})
            except Exception:
                d = {}
            lista_ficha[str(r["MODELO"]).strip()] = _num(d.get("REDONDEO_SHOWROOM"))
    filas = []
    for it in items:
        p = peds.get(it["numero_pedido"])
        if not p:
            continue
        cant = max(_num(it.get("cantidad"), 1.0), 1.0)
        modelo = str(it.get("modelo") or "").strip()
        de_ficha = lista_ficha.get(modelo, 0.0) if modelo else 0.0
        unit = de_ficha if de_ficha > 0 else _num(it.get("precio_unitario"))
        lista = round(unit * cant, 2)
        final = round(_num(it.get("total_linea"), lista), 2)
        filas.append({
            "pedido": int(it["numero_pedido"]), "fecha": p.get("fecha"), "mes": int(_num(p.get("mes"), 0)),
            "cliente": p.get("cliente") or "", "vendedor": " / ".join(sorted(set(vends.get(it["numero_pedido"], []))))
            or "(sin vendedor)", "modelo": modelo or "S/N", "pieza": it.get("descripcion") or "",
            "cantidad": int(cant), "lista": lista, "final": final, "descuento": round(lista - final, 2),
            "origen_lista": "Ficha" if de_ficha > 0 else "Pedido"})
    return pd.DataFrame(filas)


def _resumir(df, por, etiqueta):
    g = df.groupby(por).agg(piezas=("cantidad", "sum"), con_desc=("descuento", lambda s: int((s > 0.5).sum())),
                            lista=("lista", "sum"), final=("final", "sum"), descuento=("descuento", "sum")).reset_index()
    g["pct"] = [(d / l * 100.0) if l else 0.0 for d, l in zip(g["descuento"], g["lista"])]
    return g.rename(columns={por: etiqueta, "piezas": "PIEZAS", "con_desc": "CON DESCUENTO", "lista": "PRECIO DE LISTA",
                             "final": "VENTA FINAL", "descuento": "DESCUENTO $", "pct": "DESCUENTO %"})


def _fmt():
    return {"PRECIO DE LISTA": st.column_config.NumberColumn(format="$%.2f"),
            "VENTA FINAL": st.column_config.NumberColumn(format="$%.2f"),
            "DESCUENTO $": st.column_config.NumberColumn(format="$%.2f"),
            "DESCUENTO %": st.column_config.NumberColumn(format="%.1f%%")}


def tab_descuentos(sb, user):
    from ventas import MESES
    hoy = dt.date.today()
    c1, c2, c3 = st.columns([1, 1, 2])
    anio = c1.selectbox("Año", list(range(hoy.year, hoy.year - 8, -1)), key="rp_ds_anio")
    mes = c2.selectbox("Mes", ["Todo el año"] + MESES, key="rp_ds_mes")
    st.caption("Solo pedidos **pagados**. Precio de lista = redondeo showroom de la ficha (con IVA); en piezas S/N, "
               "el precio que se puso en el pedido. Venta final = lo que pagó el cliente por la pieza (con IVA y "
               "descuento). Sin envío. Si a la ficha le cambiaron el precio después de la venta, se compara contra "
               "el precio de hoy.")
    try:
        df = _descuentos_mem(sb, anio)
    except Exception as ex:
        st.error(f"No se pudo calcular el reporte de descuentos: {ex}")
        return
    if not df.empty and mes != "Todo el año":
        df = df[df["mes"] == MESES.index(mes) + 1]
    if df.empty:
        st.info("No hay pedidos pagados en ese período.")
        return

    tl, tf, td = df["lista"].sum(), df["final"].sum(), df["descuento"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("PRECIO DE LISTA", f"${tl:,.2f}")
    m2.metric("VENTA FINAL", f"${tf:,.2f}")
    m3.metric("DESCUENTO DADO", f"${td:,.2f}")
    m4.metric("DESCUENTO PROMEDIO", f"{(td / tl * 100.0) if tl else 0.0:,.1f}%",
              help="Descuento dado / precio de lista.")
    con = int((df["descuento"] > 0.5).sum())
    st.caption(f"{con} de {len(df)} renglón(es) vendido(s) llevaron descuento.")

    if mes == "Todo el año":
        st.subheader("Por mes")
        pm = _resumir(df, "mes", "MES")
        pm["MES"] = [MESES[m - 1] if 1 <= m <= 12 else "?" for m in pm["MES"]]
        st.dataframe(pm, use_container_width=True, hide_index=True, column_config=_fmt())
        st.bar_chart(pm.set_index("MES")[["DESCUENTO $"]])

    st.subheader("Por vendedor")
    pv = _resumir(df, "vendedor", "VENDEDOR").sort_values("DESCUENTO $", ascending=False)
    st.dataframe(pv, use_container_width=True, hide_index=True, column_config=_fmt())

    st.subheader("Pieza por pieza")
    solo_con = st.checkbox("Solo las que llevaron descuento", value=True, key="rp_ds_solo")
    det = df[df["descuento"] > 0.5] if solo_con else df
    det = det.sort_values(["pedido"]).assign(pct=[(d / l * 100.0) if l else 0.0
                                                  for d, l in zip(det["descuento"], det["lista"])])
    det = det.rename(columns={"pedido": "PEDIDO", "fecha": "FECHA", "cliente": "CLIENTE", "vendedor": "VENDEDOR",
                              "modelo": "MODELO", "pieza": "PIEZA", "cantidad": "CANT.", "lista": "PRECIO DE LISTA",
                              "final": "VENTA FINAL", "descuento": "DESCUENTO $", "pct": "DESCUENTO %",
                              "origen_lista": "LISTA SEGÚN"})[
        ["PEDIDO", "FECHA", "CLIENTE", "VENDEDOR", "MODELO", "PIEZA", "CANT.", "PRECIO DE LISTA", "VENTA FINAL",
         "DESCUENTO $", "DESCUENTO %", "LISTA SEGÚN"]]
    st.dataframe(det, use_container_width=True, hide_index=True, column_config=_fmt())
    nombre = f"descuentos_{anio}" + ("" if mes == "Todo el año" else f"_{MESES.index(mes) + 1:02d}")
    descargar(st, "Descargar detalle (CSV)", det.to_csv(index=False).encode("utf-8-sig"),
              file_name=f"{nombre}.csv", mime="text/csv")


# ----------------------------------------------------------------------
# Punto de entrada
# ----------------------------------------------------------------------
def render(sb, user):
    st.header("Reportes")
    from ventas import tab_resumen
    secciones = {"Resumen mensual": tab_resumen, "Resumen anual": tab_anual, "Descuentos": tab_descuentos}
    elegida = selector(list(secciones), "nav_reportes", "Reporte")
    secciones[elegida](sb, user)
