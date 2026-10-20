"""
Trae el NOMBRE y la DESCRIPCIÓN de los productos de Shopify al inventario (se emparejan por SKU = MODELO).

Uso desde app.py (pestaña Herramientas):
    from shopify_descripciones import render_traer_descripciones
    render_traer_descripciones(supabase)
"""
import csv
import html
import io
import json
import re
import time
from html.parser import HTMLParser

import pandas as pd
import streamlit as st

from shopify_borrador import _gql

Q_PRODUCTOS = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    nodes { id title descriptionHtml variants(first: 3) { nodes { sku } } }
    pageInfo { hasNextPage endCursor }
  }
}"""

_BLOQUES = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr", "blockquote"}


class _HtmlATexto(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.partes.append("\n")
        elif tag == "li":
            self.partes.append("\n- ")
        elif tag in _BLOQUES:
            self.partes.append("\n\n")

    def handle_endtag(self, tag):
        if tag in _BLOQUES:
            self.partes.append("\n\n")

    def handle_data(self, data):
        self.partes.append(data)


def html_a_texto(valor):
    """HTML de Shopify -> texto plano. Párrafo = línea en blanco; <br> = salto de línea."""
    if not valor:
        return ""
    p = _HtmlATexto()
    p.feed(str(valor))
    texto = "".join(p.partes).replace("\xa0", " ")
    lineas = [re.sub(r"[ \t]+", " ", l).strip() for l in texto.split("\n")]
    texto = "\n".join(lineas)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


def _norm(t):
    return re.sub(r"\s+", " ", str(t or "")).strip()


def productos_de_shopify():
    """{sku: {"titulo":..., "desc":...}} de todos los productos de Shopify."""
    res, cursor = {}, None
    while True:
        for intento in range(4):
            try:
                d = _gql(Q_PRODUCTOS, {"cursor": cursor})["products"]
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
                    res[sku] = {"titulo": str(p["title"] or "").strip(), "desc": html_a_texto(p["descriptionHtml"])}
        if not d["pageInfo"]["hasNextPage"]:
            return res
        cursor = d["pageInfo"]["endCursor"]


def _datos(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _estado(inv, sh):
    if not _norm(sh):
        return "sin dato en Shopify"
    if _norm(inv) == _norm(sh):
        return "igual"
    if not _norm(inv):
        return "inventario vacío"
    return "distinto"


# Campos que se pueden traer: clave -> (campo en el inventario, clave en el dato de Shopify, etiqueta)
CAMPOS = {"nombre": ("NOMBRE", "titulo", "NOMBRE"), "desc": ("DESCRIPCION", "desc", "DESCRIPCIÓN")}
TRAER = ("inventario vacío", "distinto")


def armar_plan(supabase, desde_shopify):
    """Compara nombre y descripción de cada producto de Shopify con el del inventario.
    Devuelve (plan, sin_inventario)."""
    skus = list(desde_shopify)
    plan, encontrados = [], set()
    for i in range(0, len(skus), 100):
        trozo = skus[i:i + 100]
        filas = supabase.table("productos").select("MODELO,datos_json").in_("MODELO", trozo).execute().data
        for f in filas:
            modelo = str(f["MODELO"]).strip()
            encontrados.add(modelo)
            d = _datos(f.get("datos_json"))
            fila = {"modelo": modelo}
            for k, (campo_inv, clave_sh, _) in CAMPOS.items():
                inv = str(d.get(campo_inv, "") or "")
                sh = desde_shopify[modelo][clave_sh]
                fila[k] = {"estado": _estado(inv, sh), "inventario": inv, "shopify": sh}
            plan.append(fila)
    return plan, len(skus) - len(encontrados)


def aplicar_plan(supabase, filas, progreso=None):
    """filas: [{"modelo", "nombre": {...}|None, "desc": {...}|None}] con solo los campos a escribir.
    Escribe lo de Shopify en el inventario y guarda lo anterior en NOMBRE_ANTERIOR / DESCRIPCION_ANTERIOR.
    Si un campo cambió en el inventario desde la revisión, no se toca."""
    hechos, errores = 0, []
    for n, f in enumerate(filas, 1):
        try:
            r = supabase.table("productos").select("datos_json").eq("MODELO", f["modelo"]).limit(1).execute().data
            if not r:
                errores.append((f["modelo"], "no se encontró en el inventario"))
                continue
            d = _datos(r[0].get("datos_json"))
            cambios = 0
            for k, (campo_inv, _, etiqueta) in CAMPOS.items():
                c = f.get(k)
                if not c:
                    continue
                if _norm(d.get(campo_inv)) != _norm(c["inventario"]):
                    errores.append((f["modelo"], f"{etiqueta.lower()} cambió en el inventario mientras tanto; no se tocó"))
                    continue
                if not _norm(c["shopify"]):
                    continue
                if _norm(d.get(campo_inv)):
                    d[campo_inv + "_ANTERIOR"] = d.get(campo_inv)
                d[campo_inv] = c["shopify"]
                cambios += 1
            if cambios:
                supabase.table("productos").update(
                    {"datos_json": json.dumps(d, ensure_ascii=False)}).eq("MODELO", f["modelo"]).execute()
                hechos += 1
        except Exception as ex:
            errores.append((f["modelo"], str(ex)))
        if progreso:
            progreso(n, len(filas))
    return hechos, errores


def _csv(filas):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(["MODELO", "CAMPO", "ESTADO", "INVENTARIO_ANTES", "SHOPIFY"])
    for f in filas:
        for k, (_, _, etiqueta) in CAMPOS.items():
            c = f.get(k)
            if c:
                w.writerow([f["modelo"], etiqueta, c["estado"], c["inventario"], c["shopify"]])
    return b.getvalue().encode("utf-8-sig")


def render_traer_descripciones(supabase):
    st.subheader("🛍️ Traer nombre y descripción de Shopify al inventario",
                 help="Empareja cada producto de Shopify con el inventario por SKU = MODELO y copia el nombre y/o la "
               "descripción (texto plano: se pierden negritas y enlaces). Lo anterior del inventario se guarda en el "
               "mismo producto (NOMBRE_ANTERIOR / DESCRIPCION_ANTERIOR) y puedes descargar un respaldo.")
    if st.button("🔍 Revisar Shopify", key="desc_revisar"):
        with st.spinner("Leyendo productos de Shopify (puede tardar un poco)..."):
            try:
                sh = productos_de_shopify()
                plan, sin_inv = armar_plan(supabase, sh)
            except Exception as ex:
                st.error(f"❌ No se pudo revisar: {ex}")
                return
        st.session_state["desc_plan"] = {"plan": plan, "sin_inv": sin_inv, "total": len(sh)}

    datos = st.session_state.get("desc_plan")
    if not datos:
        return
    plan = datos["plan"]

    def cuenta(k, e):
        return sum(1 for f in plan if f[k]["estado"] == e)

    c1, c2, c3 = st.columns(3)
    c1.metric("Productos con SKU en Shopify", datos["total"])
    c2.metric("Nombres: vacíos / distintos", f"{cuenta('nombre', 'inventario vacío')} / {cuenta('nombre', 'distinto')}")
    c3.metric("Descripciones: vacías / distintas", f"{cuenta('desc', 'inventario vacío')} / {cuenta('desc', 'distinto')}")
    if datos["sin_inv"]:
        st.caption(f"{datos['sin_inv']} SKU de Shopify no existen como MODELO en el inventario (se ignoran).")

    base = [f for f in plan if any(f[k]["estado"] in TRAER for k in CAMPOS)]
    if not base:
        st.info("No hay nada por traer: nombre y descripción ya coinciden o no tienen dato en Shopify.")
        return
    st.caption("Cada producto tiene una casilla para el nombre y otra para la descripción. Por defecto solo se marcan "
               "los campos que el inventario tiene vacíos; los «distintos» vienen sin marcar para no pisar lo que ya "
               "escribiste. Marca uno por uno los que quieras reemplazar, o usa «Todos».")
    seleccion = st.radio("Selección rápida", ["Solo vacíos", "Todos", "Ninguno"], horizontal=True,
                         key="desc_seleccion",
                         help="Cambia las casillas de la tabla. Después puedes marcar o quitar filas sueltas.")
    if seleccion == "Todos":
        st.warning("«Todos» incluye los nombres y descripciones distintos: se reemplazarán por los de Shopify "
                   "(lo anterior queda guardado en el producto y en el respaldo CSV).")

    def _marcada(c):
        if c["estado"] not in TRAER:
            return False
        if seleccion == "Todos":
            return True
        if seleccion == "Ninguno":
            return False
        return c["estado"] == "inventario vacío"

    tabla = pd.DataFrame([{
        "NOMBRE": _marcada(f["nombre"]), "DESCRIPCIÓN": _marcada(f["desc"]), "MODELO": f["modelo"],
        "NOMBRE (inventario)": f["nombre"]["inventario"], "NOMBRE (Shopify)": f["nombre"]["shopify"],
        "DESCRIPCIÓN (inventario)": f["desc"]["inventario"], "DESCRIPCIÓN (Shopify)": f["desc"]["shopify"],
    } for f in base])
    editada = st.data_editor(
        tabla, hide_index=True, use_container_width=True, key=f"desc_tabla_{seleccion}",
        disabled=[c for c in tabla.columns if c not in ("NOMBRE", "DESCRIPCIÓN")],
        column_config={"NOMBRE": st.column_config.CheckboxColumn("Traer nombre"),
                       "DESCRIPCIÓN": st.column_config.CheckboxColumn("Traer descripción")})
    filas = []
    for i, f in enumerate(base):
        fila = {"modelo": f["modelo"], "nombre": None, "desc": None}
        if bool(editada["NOMBRE"].iloc[i]) and f["nombre"]["estado"] in TRAER:
            fila["nombre"] = f["nombre"]
        if bool(editada["DESCRIPCIÓN"].iloc[i]) and f["desc"]["estado"] in TRAER:
            fila["desc"] = f["desc"]
        if fila["nombre"] or fila["desc"]:
            filas.append(fila)
    n_nom = sum(1 for f in filas if f["nombre"])
    n_des = sum(1 for f in filas if f["desc"])
    st.write(f"Se actualizarán **{n_nom}** nombres y **{n_des}** descripciones ({len(filas)} productos).")
    if not filas:
        return
    st.download_button("⬇️ Descargar respaldo (CSV) de lo que se va a cambiar", _csv(filas),
                       file_name="respaldo_nombre_descripcion.csv", mime="text/csv", key="desc_csv")
    if st.button(f"✅ Aplicar a {len(filas)} productos", type="primary", key="desc_aplicar"):
        barra = st.progress(0.0)
        hechos, errores = aplicar_plan(supabase, filas, lambda n, t: barra.progress(n / t))
        st.success(f"Listo: {hechos} productos actualizados en el inventario.")
        if errores:
            st.warning(f"{len(errores)} sin actualizar: " + "; ".join(f"{m}: {e}" for m, e in errores[:10]))
        st.session_state.pop("desc_plan", None)
        st.session_state.pop("desc_seleccion", None)
        for k in [k for k in st.session_state if str(k).startswith("desc_tabla_")]:
            st.session_state.pop(k, None)