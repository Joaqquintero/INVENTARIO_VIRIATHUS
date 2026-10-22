"""
Pestaña «Shopify»: lista de los productos del inventario que están en Shopify (se emparejan por SKU = MODELO).
Uso en app.py:  from shopify_lista import render_lista_shopify  ->  render_lista_shopify(supabase)
"""
import time

import pandas as pd
import streamlit as st

from shopify_borrador import _gql, _secret
from sync_shopify import _num, _parse

Q = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    nodes { id title status variants(first: 3) { nodes { sku price inventoryQuantity } } }
    pageInfo { hasNextPage endCursor }
  }
}"""
ESTADOS = {"ACTIVE": "Activo", "DRAFT": "Borrador", "ARCHIVED": "Archivado"}


def _leer_shopify():
    res, cursor = [], None
    while True:
        for intento in range(4):
            try:
                d = _gql(Q, {"cursor": cursor})["products"]
                break
            except RuntimeError as ex:
                if "throttl" in str(ex).lower() and intento < 3:
                    time.sleep(2 + intento * 2)
                    continue
                raise
        for p in d["nodes"]:
            for v in p["variants"]["nodes"]:
                sku = str(v.get("sku") or "").strip()
                if sku:
                    res.append({"sku": sku, "titulo": p["title"], "estado": p["status"], "precio": _num(v.get("price")),
                                "stock": v.get("inventoryQuantity"), "id": p["id"]})
                    break
        if not d["pageInfo"]["hasNextPage"]:
            return res
        cursor = d["pageInfo"]["endCursor"]


@st.cache_data(ttl=600, show_spinner=False)
def _tabla(_sb):
    shopify = _leer_shopify()
    por_sku = {p["sku"]: p for p in shopify}
    skus = list(por_sku)
    inventario = {}
    for i in range(0, len(skus), 100):
        for f in (_sb.table("productos").select("MODELO,datos_json").in_("MODELO", skus[i:i + 100]).execute().data or []):
            inventario[str(f["MODELO"]).strip()] = _parse(f.get("datos_json"))
    shop = _secret("SHOPIFY_SHOP").replace(".myshopify.com", "")
    filas = []
    for sku, d in inventario.items():
        p = por_sku[sku]
        disp = str(d.get("DISP", d.get("DISPONIBILIDAD", "")) or "").strip().lower()
        filas.append({
            "MODELO": sku,
            "NOMBRE": str(d.get("NOMBRE", "") or p["titulo"]),
            "Estado en Shopify": ESTADOS.get(p["estado"], p["estado"]),
            "Precio inventario": _num(d.get("REDONDEO_SHOWROOM")),
            "Precio Shopify": p["precio"],
            "Stock Shopify": p["stock"],
            "Inventario": "Vendido" if disp == "v" else ("Reservado" if disp == "r" else "Disponible"),
            "Ver en Shopify": f"https://admin.shopify.com/store/{shop}/products/{p['id'].split('/')[-1]}",
        })
    filas.sort(key=lambda f: (len(f["MODELO"]), f["MODELO"]), reverse=True)
    return {"filas": filas, "solo_shopify": len(skus) - len(inventario), "hora": time.strftime("%H:%M")}


def render_lista_shopify(sb):
    st.subheader("Productos del inventario que están en Shopify")
    if st.button("Actualizar lista", key="lista_shop_act"):
        _tabla.clear()
    try:
        with st.spinner("Leyendo Shopify…"):
            r = _tabla(sb)
    except Exception as ex:
        st.error(f"No se pudo leer Shopify: {ex}")
        return
    filas = r["filas"]
    if not filas:
        st.info("No hay productos del inventario en Shopify (se emparejan por SKU = modelo).")
        return
    df = pd.DataFrame(filas)
    c1, c2, c3 = st.columns([2, 2, 1])
    texto = c1.text_input("Buscar por modelo o nombre", key="lista_shop_q").strip().lower()
    estados = c2.multiselect("Estado en Shopify", sorted(df["Estado en Shopify"].unique()), key="lista_shop_est")
    dif = c3.checkbox("Solo precio distinto", key="lista_shop_dif")
    if texto:
        df = df[df["MODELO"].str.lower().str.contains(texto, regex=False) |
                df["NOMBRE"].str.lower().str.contains(texto, regex=False)]
    if estados:
        df = df[df["Estado en Shopify"].isin(estados)]
    if dif:
        df = df[(df["Precio inventario"] - df["Precio Shopify"]).abs() > 0.009]
    st.caption(f"{len(df)} de {len(filas)} productos · actualizado a las {r['hora']}"
               + (f" · {r['solo_shopify']} productos de Shopify sin modelo en el inventario" if r["solo_shopify"] else ""))
    st.dataframe(df, use_container_width=True, hide_index=True, column_config={
        "Precio inventario": st.column_config.NumberColumn(format="$%.2f"),
        "Precio Shopify": st.column_config.NumberColumn(format="$%.2f"),
        "Ver en Shopify": st.column_config.LinkColumn(display_text="Abrir"),
    })