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


def _dibujar_etiqueta(c, W, H, modelo, nombre=None, precio=None, qr_texto="", margen_mm=MARGEN_MM,
                      barra_mm=0.339, alto_barra_mm=7.0, tam_nombre=6.5):
    """Etiqueta horizontal de W × H (puntos), origen abajo a la izquierda.
    Con qr_texto: QR a la izquierda y, a la derecha, información + código de barras + número.
    Sin qr_texto: lo mismo pero ocupando todo el ancho."""
    m = margen_mm * mm
    x0, y0 = m, m
    iw, ih = W - 2 * m, H - 2 * m                       # zona imprimible
    gap = 3 * mm
    if qr_texto:
        lado = min(ih, iw * 0.4)
        _dibujar_qr(c, qr_texto, x0, y0 + (ih - lado) / 2.0, lado)
        rx, rw = x0 + lado + gap, iw - lado - gap       # columna derecha
    else:
        rx, rw = x0, iw
    cx = rx + rw / 2.0

    # Número de modelo (abajo): discreto, ajustado al ancho
    fuente = "Helvetica-Bold"
    tam_num = min(8.5, ih * 0.22)
    while tam_num > 5 and c.stringWidth(modelo, fuente, tam_num) > rw:
        tam_num -= 0.5
    c.setFont(fuente, tam_num)
    c.drawCentredString(cx, y0 + 0.3 * mm, modelo)
    y = y0 + tam_num * 0.95 + 1.0 * mm

    # Precio: se reserva su espacio antes de calcular el alto del código de barras
    tam_p = tam_num                                   # el precio, del mismo tamaño que el modelo
    alto_precio = tam_p * 1.15 if precio else 0.0
    alto_b = min(alto_barra_mm * mm, max(ih - (y - y0) - alto_precio - 1 * mm, 4 * mm))
    bw = barra_mm
    while True:
        b = code128.Code128(modelo, barHeight=alto_b, barWidth=bw * mm, quiet=False)
        if b.width <= rw - 2 * 4 * bw * mm or bw <= 0.2:
            break
        bw = round(bw - 0.042, 3)
    b.drawOn(c, cx - b.width / 2.0, y)
    y += alto_b + 1.0 * mm

    # Arriba: nombre (letra chica, hasta donde quepa) y precio (más grande, en negrita)
    libre = (y0 + ih) - y
    tam = tam_nombre
    paso = tam * 1.15
    if precio:
        txt_p = f"${precio:,.0f}"
        c.setFont("Helvetica-Bold", tam_p)
        c.drawCentredString(cx, y + 0.3 * mm, txt_p)          # justo encima del código de barras
        libre_nombre = libre - alto_precio
    else:
        libre_nombre = libre
    n_lin = int(libre_nombre // paso)
    if nombre and n_lin >= 1:
        lineas = _lineas(c, nombre, "Helvetica", tam, rw, n_lin)
        ytxt = (y0 + ih) - tam
        c.setFont("Helvetica", tam)
        for t in lineas:
            c.drawCentredString(cx, ytxt, t)
            ytxt -= paso
    return bw


def generar_pdf_etiquetas(modelos, rollo, copias=1, girar_180=False, datos=None, margen_mm=MARGEN_MM,
                          url_base="", barra_mm=0.339, alto_barra_mm=7.0, mostrar_nombre=True, mostrar_precio=False,
                          qr_modo="modelo", tam_nombre=6.5, urls=None):
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
    return {"nombre": str(d.get("NOMBRE", "") or "").strip(), "precio": precio}


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
        candidatos = [t for t in re.split(r"[,\\s;]+", txt.strip()) if t]

    url_base = st.text_input(
        "Dirección del QR (déjala vacía para etiquetas sin QR)",
        value=str(st.secrets.get("TIENDA_QR_URL", "https://viriathus.com.mx/")), key="et_url",
        placeholder="https://viriathus.com.mx/",
        help="La dirección que se codifica en el QR. Pégala completa, por ejemplo la de un producto: "
             "https://viriathus.com.mx/collections/objetos/products/escultura-rodo-padilla. "
             "Abajo eliges si se usa tal cual, solo la página principal, o con el modelo agregado al final.").strip()

    modo_qr = st.radio("¿A dónde lleva el QR?",
                       ["El enlace de cada producto (desde Shopify)", "La dirección exacta que escribí arriba",
                        "Página principal de la tienda", "La dirección + el modelo"],
                       index=0, horizontal=True, key="et_modo_qr",
                       help="«Enlace de cada producto»: cada QR abre la publicación de su producto en tu tienda (se busca en Shopify por "
                            "el modelo; si un producto no está publicado, su QR lleva a la página principal). «Exacta»: todos los QR "
                            "abren lo que escribiste. «Dirección + el modelo»: agrega el modelo al final.")
    qr_modo = {"El enlace de cada producto (desde Shopify)": "shopify", "La dirección exacta que escribí arriba": "exacta", "Página principal de la tienda": "inicio",
               "La dirección + el modelo": "modelo"}[modo_qr]
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
    tam_nombre = p3.number_input("Tamaño del nombre (pt)", min_value=5.0, max_value=12.0, value=6.5, step=0.5, key="et_tam_nombre",
                                 help="Más grande = menos palabras caben. Si el nombre no cabe, se corta con «…».")
    con_nombre = p4.checkbox("Incluir el nombre", value=True, key="et_nombre")
    con_precio = p5.checkbox("Incluir el precio", value=False, key="et_precio",
                             help="Ojo: el precio impreso no se actualiza si luego lo cambias en el inventario.")

    if st.button("Generar PDF de etiquetas", type="primary", key="et_generar", disabled=not candidatos):
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
        if len(modelos) * int(copias) > LIMITE_ETIQUETAS:
            st.error(f"Son demasiadas etiquetas ({len(modelos) * int(copias)}). El máximo por PDF es {LIMITE_ETIQUETAS}.")
            return
        if not modelos:
            st.warning("Ninguno de esos modelos existe en el inventario.")
        else:
            urls, sin_enlace = {}, []
            if url_base and qr_modo == "shopify":
                try:
                    with st.spinner("Buscando los enlaces en Shopify…"):
                        urls = urls_de_shopify(modelos)
                    sin_enlace = [m for m in modelos if m not in urls]
                except Exception as ex:
                    st.warning(f"No se pudieron leer los enlaces de Shopify ({ex}). Los QR llevarán a la página principal.")
                    sin_enlace = list(modelos)
            pdf = generar_pdf_etiquetas(modelos, rollo, copias, girar, hallados, margen, url_base, barra, alto,
                                        con_nombre, con_precio, qr_modo, tam_nombre, urls)
            if sin_enlace:
                vista = ", ".join(sin_enlace[:30]) + ("…" if len(sin_enlace) > 30 else "")
                st.info(f"{len(sin_enlace)} modelo(s) no están publicados en la tienda; su QR lleva a la página principal: {vista}")
            st.session_state["et_pdf"] = (pdf, len(modelos) * int(copias))
        if invalidos:
            st.warning("Estos modelos tienen caracteres que el código de barras no admite (acentos, ñ…), se omitieron: "
                       + ", ".join(invalidos[:20]))
        if faltan:
            vista = ", ".join(faltan[:30]) + ("…" if len(faltan) > 30 else "")
            st.info(f"{len(faltan)} modelo(s) no existen en el inventario y se omitieron: {vista}")

    if st.session_state.get("et_pdf"):
        pdf, n = st.session_state["et_pdf"]
        st.success(f"Listo: {n} etiqueta(s).")
        descargar(st, "Descargar PDF de etiquetas", pdf, file_name="etiquetas.pdf", mime="application/pdf", key="et_desc")
        st.caption("Al imprimir, elige la Brother QL-800, el papel que coincide con tu rollo (p. ej. 29 mm × 90 mm), "
                   "tamaño real 100% (sin «ajustar a la página») y sin márgenes.")