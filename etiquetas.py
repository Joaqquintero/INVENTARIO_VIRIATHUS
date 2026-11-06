"""
Etiquetas con CÓDIGO DE BARRAS (Code 128) del modelo, para imprimir en la Brother QL-800 y cobrar con lector.
Cada etiqueta: código de barras + el número de modelo en texto grande debajo (por si el lector falla).

Uso en app.py (pestaña Herramientas):
    from etiquetas import render_etiquetas
    render_etiquetas(supabase)
"""
import io
import json
import re
from urllib.parse import quote, urlparse

import streamlit as st
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128, qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas
from navegacion import descargar

# Rollos de la QL-800: (ancho del papel, largo) en mm. El PDF sale del tamaño exacto del rollo.
ROLLOS = {
    "DK-11201 · 29 × 90 mm (recomendado)": (29.0, 90.0),
    "DK-11209 · 29 × 62 mm": (29.0, 62.0),
    "DK-11204 · 17 × 54 mm": (17.0, 54.0),
    "DK-11202 · 62 × 100 mm": (62.0, 100.0),
}
LIMITE_ETIQUETAS = 500
TAM_PRECIO = 10.0      # tamaño del precio en la etiqueta (pt)
MARGEN_MM = 5.0        # margen que la impresora no imprime, por lado (ajustable en pantalla)


def modelo_valido(m):
    """Code 128 solo admite ASCII imprimible (sin acentos ni ñ)."""
    return bool(m) and all(32 <= ord(ch) <= 126 for ch in m)


def _url_qr(url_base, modelo, modo="modelo"):
    """Dirección del QR según el modo:
      "inicio" → solo la página principal del sitio (scheme://dominio/)
      "exacta" → la dirección tal como se escribió, sin tocarla
      "modelo" → la base + el modelo (si la base trae {modelo} se sustituye; si no, se agrega al final)"""
    if modo == "inicio":
        u = urlparse(url_base)
        return f"{u.scheme or 'https'}://{u.netloc}/" if u.netloc else url_base
    if modo == "exacta":
        return url_base
    seguro = quote(modelo, safe="")
    return url_base.replace("{modelo}", seguro) if "{modelo}" in url_base else url_base + seguro


def _dibujar_qr(c, texto, x, y, lado):
    w = qr.QrCodeWidget(texto, barLevel="M", barBorder=0)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(lado, lado, transform=[lado / (x1 - x0), 0, 0, lado / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def _lineas(c, texto, fuente, tam, ancho, max_lineas):
    """Parte el texto en hasta max_lineas líneas que quepan en `ancho`; recorta con «…» si sobra."""
    lineas = simpleSplit(texto, fuente, tam, ancho)
    if len(lineas) > max_lineas:
        lineas = lineas[:max_lineas]
        ult = lineas[-1]
        while ult and c.stringWidth(ult + "…", fuente, tam) > ancho:
            ult = ult[:-1]
        lineas[-1] = ult.rstrip() + "…"
    return lineas


def _texto_que_quepa(c, texto, fuente, tam_max, tam_min, ancho, alto, interlineado=1.15):
    """El tamaño de letra más grande (entre tam_max y tam_min) con el que `texto` cabe en ancho × alto.
    Si ni con tam_min cabe, recorta con «…». Devuelve (tamaño, líneas)."""
    tam = tam_max
    while True:
        lineas = simpleSplit(texto, fuente, tam, ancho)
        if len(lineas) * tam * interlineado <= alto or tam <= tam_min:
            break
        tam = round(tam - 0.25, 2)
    max_lin = max(1, int(alto // (tam * interlineado)))
    if len(lineas) > max_lin:
        lineas = _lineas(c, texto, fuente, tam, ancho, max_lin)
    return tam, lineas


def _dibujar_etiqueta(c, W, H, modelo, nombre=None, precio=None, qr_texto="", margen_mm=MARGEN_MM,
                      barra_mm=0.339, alto_barra_mm=7.0, tam_nombre=7.0):
    """Etiqueta horizontal de W × H (puntos), origen abajo a la izquierda:

        ┌──────────┬───────────────────────────────┐
        │          │   Nombre del producto, hasta   │
        │    QR    │   donde quepa (se achica solo) │
        │          │   ║║│║║│║│║║║│║║│║║│║║│║║│║    │
        │          │   24189          Precio: $3,490│
        └──────────┴───────────────────────────────┘
    Sin QR, la columna de la derecha ocupa todo el ancho."""
    m = margen_mm * mm
    x0, y0 = m, m
    iw, ih = W - 2 * m, H - 2 * m                       # zona imprimible
    if qr_texto:
        lado = min(ih, iw * 0.30)
        _dibujar_qr(c, qr_texto, x0, y0 + (ih - lado) / 2.0, lado)
        gap = max(3 * mm, iw * 0.06)
        rx, rw = x0 + lado + gap, iw - lado - gap       # columna derecha
    else:
        rx, rw = x0, iw
    cx = rx + rw / 2.0

    # Abajo: modelo a la izquierda y precio a la derecha (misma línea)
    # El precio va en negritas a 11 pt; si no cabe junto al modelo, primero se achica el modelo
    # y solo en etiquetas muy chicas se achica también el precio.
    tam_mod = min(7.5, ih * 0.20)
    txt_p = f"Precio: ${precio:,.2f}" if precio else ""
    tam_pre = min(TAM_PRECIO, ih * 0.32) if txt_p else 0.0     # en rollos chicos, más chico

    def ancho_pie():
        return (c.stringWidth(modelo, "Helvetica", tam_mod)
                + (c.stringWidth(txt_p, "Helvetica-Bold", tam_pre) if txt_p else 0) + 2 * mm)
    while tam_mod > 4.5 and ancho_pie() > rw:
        tam_mod -= 0.25
    while txt_p and tam_pre > 4.5 and ancho_pie() > rw:
        tam_pre -= 0.25
    c.setFont("Helvetica", tam_mod)
    c.drawString(rx, y0 + 0.2 * mm, modelo)
    if txt_p:
        c.setFont("Helvetica-Bold", tam_pre)               # el precio, en negritas
        c.drawRightString(rx + rw, y0 + 0.2 * mm, txt_p)
    tam_pie = max(tam_mod, tam_pre)
    y = y0 + tam_pie + 0.9 * mm                         # base del código de barras

    # Código de barras: lo más alto que permita dejar lugar para 2 renglones de texto arriba
    dos_renglones = 2 * 5.5 * 1.15 + 0.8 * mm if nombre else 0.0
    alto_b = min(alto_barra_mm * mm, max((y0 + ih) - y - dos_renglones - 0.8 * mm, 5 * mm))
    bw = barra_mm
    while True:
        b = code128.Code128(modelo, barHeight=alto_b, barWidth=bw * mm, quiet=False)
        if b.width <= rw or bw <= 0.2:
            break
        bw = round(bw - 0.021, 3)
    b.drawOn(c, cx - b.width / 2.0, y)
    y += alto_b + 0.8 * mm

    # Arriba: el texto, centrado, del tamaño más grande con el que entra todo
    if nombre:
        alto_txt = (y0 + ih) - y
        tam, lineas = _texto_que_quepa(c, nombre, "Helvetica", tam_nombre, 4.5, rw, alto_txt)
        paso = tam * 1.15
        bloque = len(lineas) * paso
        ytxt = y + (alto_txt - bloque) / 2.0 + bloque - tam      # centrado en el espacio libre
        c.setFont("Helvetica", tam)
        for t in lineas:
            c.drawCentredString(cx, ytxt, t)
            ytxt -= paso
    return bw


def generar_pdf_etiquetas(modelos, rollo, copias=1, girar_180=False, datos=None, margen_mm=MARGEN_MM,
                          url_base="", barra_mm=0.339, alto_barra_mm=7.0, mostrar_nombre=True, mostrar_precio=False,
                          qr_modo="modelo", tam_nombre=7.0, urls=None):
    """PDF con una página por etiqueta, del tamaño exacto del rollo (ancho × largo, vertical).
    El diseño es horizontal (se lee con el rollo largo en horizontal) y se gira 90° para el papel.
    datos = {modelo: {"nombre": ..., "precio": ...}}. Devuelve bytes."""
    ancho, largo = ROLLOS[rollo]
    pw, ph = ancho * mm, largo * mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(pw, ph))
    c.setTitle("Etiquetas")
    for m in modelos:
        d = (datos or {}).get(m, {})
        nombre = d.get("nombre") if mostrar_nombre else None
        precio = d.get("precio") if (mostrar_precio and d.get("precio")) else None
        if not url_base:
            qr_texto = ""
        elif qr_modo == "shopify":
            qr_texto = (urls or {}).get(m) or _url_qr(url_base, m, "inicio")   # sin publicación: página principal
        else:
            qr_texto = _url_qr(url_base, m, qr_modo)
        for _ in range(max(1, int(copias))):
            c.saveState()
            if girar_180:
                c.translate(0, ph)
                c.rotate(-90)
            else:
                c.translate(pw, 0)
                c.rotate(90)
            _dibujar_etiqueta(c, ph, pw, m, nombre, precio, qr_texto, margen_mm, barra_mm, alto_barra_mm, tam_nombre)
            c.restoreState()
            c.showPage()
    c.save()
    return buf.getvalue()


# ----------------------------------------------------------------------
# Enlaces de las publicaciones en Shopify (SKU = modelo)
# ----------------------------------------------------------------------
Q_URLS = """
query($q: String) {
  products(first: 50, query: $q) {
    nodes { onlineStoreUrl variants(first: 5) { nodes { sku } } }
  }
}"""


def urls_de_shopify(modelos, gql=None):
    """{modelo: enlace de la publicación} para los modelos que están publicados en la tienda."""
    if gql is None:
        from shopify_borrador import _gql as gql
    res = {}
    for i in range(0, len(modelos), 20):
        trozo = [m for m in modelos[i:i + 20] if '"' not in m and "\\" not in m]
        if not trozo:
            continue
        q = " OR ".join(f'sku:"{m}"' for m in trozo)
        for p in gql(Q_URLS, {"q": q})["products"]["nodes"]:
            url = p.get("onlineStoreUrl")
            if not url:
                continue
            for v in p["variants"]["nodes"]:
                sku = str(v.get("sku") or "").strip()
                if sku in trozo:
                    res[sku] = url
    return res


# ----------------------------------------------------------------------
# Pantalla (pestaña Herramientas)
# ----------------------------------------------------------------------
def medidas_texto(d):
    """«Medidas: Alto 29 x 20 x 24 cm» desde ALTO, LARGO y ANCHO/PROF de la ficha ("" si no hay)."""
    vals = []
    for clave, etq in (("ALTO", "Alto"), ("LARGO", "Largo"), ("ANCHO_PROF", "Prof.")):
        v = re.sub(r"\s*cm\.?$", "", str(d.get(clave, "") or "").strip(), flags=re.I)
        if v and v not in ("0", "0.0"):
            vals.append((etq, v))
    if not vals:
        return ""
    if len(vals) == 3:
        return f"Medidas: Alto {vals[0][1]} x {vals[1][1]} x {vals[2][1]} cm"
    return "Medidas: " + " x ".join(f"{e} {v}" for e, v in vals) + " cm"


def texto_etiqueta(nombre, medidas, con_medidas=True):
    """Nombre + medidas (las medidas no se agregan si el nombre ya las trae)."""
    nombre = " ".join(str(nombre or "").split())
    if con_medidas and medidas and "medida" not in nombre.lower():
        return f"{nombre} {medidas}".strip()
    return nombre


def _datos_de(fila):
    try:
        d = fila.get("datos_json")
        d = d if isinstance(d, dict) else json.loads(d or "{}")
    except Exception:
        d = {}
    try:
        precio = float(re.sub(r"[^\d.\-]", "", str(d.get("REDONDEO_SHOWROOM", "") or "")) or 0)
    except ValueError:
        precio = 0.0
    return {"nombre": str(d.get("NOMBRE", "") or "").strip(), "precio": precio, "medidas": medidas_texto(d)}


def _buscar_en_inventario(sb, candidatos):
    """{modelo: {"nombre","precio"}} de los candidatos que existen en el inventario."""
    hallados = {}
    for i in range(0, len(candidatos), 100):
        for f in (sb.table("productos").select("MODELO,datos_json")
                  .in_("MODELO", candidatos[i:i + 100]).execute().data or []):
            hallados[str(f["MODELO"]).strip()] = _datos_de(f)
    return hallados


def render_etiquetas(sb):
    st.subheader("Etiquetas con código de barras",
                 help="Genera un PDF con el código de barras (Code 128) del modelo y el número escrito debajo, "
                      "del tamaño exacto del rollo de la Brother QL-800. Solo se hacen etiquetas de modelos que "
                      "existan en el inventario.")
    c1, c2 = st.columns([2, 2])
    modo = c1.radio("¿Qué etiquetas?", ["Un modelo", "Rango de modelos", "Lista de modelos"], horizontal=True, key="et_modo")
    rollo = c2.selectbox("Rollo de la impresora", list(ROLLOS), key="et_rollo")

    candidatos = []
    if modo == "Un modelo":
        m = st.text_input("Modelo", key="et_uno").strip()
        candidatos = [m] if m else []
    elif modo == "Rango de modelos":
        r1, r2 = st.columns(2)
        d = r1.text_input("Desde", key="et_desde").strip()
        h = r2.text_input("Hasta", key="et_hasta").strip()
        if d.isdigit() and h.isdigit():
            if int(h) - int(d) + 1 > LIMITE_ETIQUETAS * 4:
                st.warning("El rango es demasiado grande. Usa rangos más chicos.")
            elif int(h) >= int(d):
                candidatos = [str(n) for n in range(int(d), int(h) + 1)]
    else:
        txt = st.text_area("Modelos (separados por coma, espacio o salto de línea)", key="et_lista", height=90)
        candidatos = [t for t in re.split(r"[,\s;]+", txt.strip()) if t]

    FICHA = "La ficha del inventario (página pública)"
    modo_qr = st.radio("¿A dónde lleva el QR?",
                       [FICHA, "El enlace de cada producto (desde Shopify)", "La dirección exacta que escribí abajo",
                        "Página principal de la tienda", "La dirección + el modelo", "Sin QR"],
                       index=0, horizontal=True, key="et_modo_qr",
                       help="«Ficha del inventario»: cada QR abre la página pública de su pieza con los datos en vivo "
                            "(foto, precio, medidas, disponible / vendida), esté o no en la tienda en línea. "
                            "«Enlace de cada producto»: la publicación en Shopify (si no está publicada, la página principal). "
                            "«Exacta»: todos los QR abren lo que escribas. «Dirección + el modelo»: agrega el modelo al final.")
    if modo_qr == FICHA:
        # La dirección sale de los secrets (FICHA_PUBLICA_URL) para poder cambiar de dominio sin tocar código
        base_ficha = str(st.secrets.get("FICHA_PUBLICA_URL", "https://viriathusapp.vercel.app")).strip().rstrip("/")
        url_base, qr_modo = f"{base_ficha}/p/", "modelo"
        st.caption(f"Cada QR abre **{url_base}<modelo>** (por ejemplo {url_base}32654). "
                   "Revisa que esa dirección abra bien antes de imprimir muchas.")
    elif modo_qr == "Sin QR":
        url_base, qr_modo = "", "exacta"
    else:
        url_base = st.text_input(
            "Dirección del QR",
            value=str(st.secrets.get("TIENDA_QR_URL", "https://viriathus.com.mx/")), key="et_url",
            placeholder="https://viriathus.com.mx/",
            help="La dirección que se codifica en el QR. Pégala completa, por ejemplo la de un producto: "
                 "https://viriathus.com.mx/collections/objetos/products/escultura-rodo-padilla.").strip()
        qr_modo = {"El enlace de cada producto (desde Shopify)": "shopify", "La dirección exacta que escribí abajo": "exacta",
                   "Página principal de la tienda": "inicio", "La dirección + el modelo": "modelo"}[modo_qr]
    o1, o2, o3 = st.columns(3)
    copias = o1.number_input("Copias de cada una", min_value=1, max_value=20, value=1, step=1, key="et_copias")
    margen = o2.number_input("Margen sin imprimir (mm por lado)", min_value=0.0, max_value=10.0, value=MARGEN_MM, step=0.5,
                             key="et_margen", help="Lo que la impresora no imprime en cada borde. El diseño se acomoda dentro del resto.")
    girar = o3.checkbox("Girar 180° (si sale al revés)", value=False, key="et_girar")
    p1, p2, p3, p4, p5 = st.columns(5)
    barra = p1.number_input("Grosor de barras (mm)", min_value=0.254, max_value=0.5, value=0.339, step=0.042, format="%.3f",
                            key="et_barra", help="Más fino = código más corto. Por debajo de 0.254 mm los lectores empiezan a fallar.")
    alto = p2.number_input("Alto del código (mm)", min_value=4.0, max_value=20.0, value=7.0, step=0.5, key="et_alto",
                           help="Los lectores leen bien desde unos 6 mm de alto.")
    tam_nombre = p3.number_input("Tamaño máximo del texto (pt)", min_value=5.0, max_value=12.0, value=7.0, step=0.5,
                                 key="et_tam_nombre",
                                 help="El texto empieza con este tamaño y se achica solo hasta que entra todo "
                                      "(mínimo 4.5 pt; si ni así entra, se corta con «…»).")
    con_nombre = p4.checkbox("Incluir el texto", value=True, key="et_nombre",
                             help="Nombre del producto (y las medidas, si las agregas).")
    con_precio = p5.checkbox("Incluir el precio", value=True, key="et_precio",
                             help="Ojo: el precio impreso no se actualiza si luego lo cambias en el inventario.")
    con_medidas = st.checkbox("Agregar las medidas de la ficha al texto (Alto × Largo × Prof.)", value=True,
                              key="et_medidas", help="No se agregan si el nombre ya trae las medidas.")
    ss = st.session_state

    # Paso 1: buscar los modelos y armar la tabla para revisar
    if st.button("1 · Preparar etiquetas", type="primary", key="et_preparar", disabled=not candidatos):
        candidatos = list(dict.fromkeys(candidatos))          # sin repetidos, en orden
        invalidos = [m for m in candidatos if not modelo_valido(m)]
        validos = [m for m in candidatos if modelo_valido(m)]
        try:
            hallados = _buscar_en_inventario(sb, validos)
        except Exception as ex:
            st.error(f"No se pudo consultar el inventario: {ex}")
            return
        modelos = [m for m in validos if m in hallados]
        faltan = [m for m in validos if m not in hallados]
        avisos = []
        if invalidos:
            avisos.append("Estos modelos tienen caracteres que el código de barras no admite (acentos, ñ…), se omitieron: "
                          + ", ".join(invalidos[:20]))
        if faltan:
            avisos.append(f"{len(faltan)} modelo(s) no existen en el inventario y se omitieron: "
                          + ", ".join(faltan[:30]) + ("…" if len(faltan) > 30 else ""))
        if not modelos:
            avisos.append("Ninguno de esos modelos existe en el inventario.")
        ss["et_filas"] = [{"modelo": m, "texto": texto_etiqueta(hallados[m]["nombre"], hallados[m]["medidas"], con_medidas),
                           "precio": float(hallados[m]["precio"] or 0)} for m in modelos]
        ss["et_avisos"] = avisos
        ss["et_ver"] = ss.get("et_ver", 0) + 1
        ss.pop("et_pdf", None)

    for a in ss.get("et_avisos", []):
        st.info(a)
    filas = ss.get("et_filas") or []
    if not filas:
        return

    # Paso 2: revisar / corregir (solo para la etiqueta; el inventario no cambia)
    import pandas as pd
    st.markdown(f"**2 · Revisa las {len(filas)} etiqueta(s)** — puedes corregir el texto y el precio; "
                "el inventario no cambia.")
    editado = st.data_editor(
        pd.DataFrame(filas), hide_index=True, use_container_width=True, num_rows="fixed",
        key=f"et_tabla_{ss.get('et_ver', 0)}", disabled=["modelo"],
        column_config={"modelo": st.column_config.TextColumn("MODELO", width="small"),
                       "texto": st.column_config.TextColumn("TEXTO DE LA ETIQUETA", width="large"),
                       "precio": st.column_config.NumberColumn("PRECIO", min_value=0.0, format="$%.2f",
                                                               width="small")})
    sin_precio = [r["modelo"] for r in editado.to_dict("records") if con_precio and not (r.get("precio") or 0)]
    if sin_precio:
        st.caption("Sin precio (la etiqueta sale sin «Precio»): " + ", ".join(sin_precio[:20]))

    # Paso 3: generar el PDF con lo que quedó en la tabla
    if st.button("3 · Generar PDF de etiquetas", type="primary", key="et_generar"):
        registros = editado.to_dict("records")
        modelos = [r["modelo"] for r in registros]
        if len(modelos) * int(copias) > LIMITE_ETIQUETAS:
            st.error(f"Son demasiadas etiquetas ({len(modelos) * int(copias)}). El máximo por PDF es {LIMITE_ETIQUETAS}.")
            return
        datos = {r["modelo"]: {"nombre": str(r.get("texto") or "").strip(),
                               "precio": float(r.get("precio") or 0) if not pd.isna(r.get("precio")) else 0.0}
                 for r in registros}
        urls, sin_enlace = {}, []
        if url_base and qr_modo == "shopify":
            try:
                with st.spinner("Buscando los enlaces en Shopify…"):
                    urls = urls_de_shopify(modelos)
                sin_enlace = [m for m in modelos if m not in urls]
            except Exception as ex:
                st.warning(f"No se pudieron leer los enlaces de Shopify ({ex}). Los QR llevarán a la página principal.")
                sin_enlace = list(modelos)
        pdf = generar_pdf_etiquetas(modelos, rollo, copias, girar, datos, margen, url_base, barra, alto,
                                    con_nombre, con_precio, qr_modo, tam_nombre, urls)
        if sin_enlace:
            vista = ", ".join(sin_enlace[:30]) + ("…" if len(sin_enlace) > 30 else "")
            st.info(f"{len(sin_enlace)} modelo(s) no están publicados en la tienda; su QR lleva a la página principal: {vista}")
        ss["et_pdf"] = (pdf, len(modelos) * int(copias))

    if ss.get("et_pdf"):
        pdf, n = ss["et_pdf"]
        st.success(f"Listo: {n} etiqueta(s).")
        descargar(st, "Descargar PDF de etiquetas", pdf, file_name="etiquetas.pdf", mime="application/pdf", key="et_desc")
        st.caption("Al imprimir, elige la Brother QL-800, el papel que coincide con tu rollo (p. ej. 29 mm × 90 mm), "
                   "tamaño real 100% (sin «ajustar a la página») y sin márgenes.")
