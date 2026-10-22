"""
PDF de COTIZACIÓN (hoja carta con membrete): datos del cliente y, en tarjetas de 2 columnas, cada producto con su
foto, nombre, medidas, cantidad y total. 6 productos en la primera hoja y 8 en las siguientes.
No muestra IVA. El descuento solo aparece en los productos que lo tienen.

    generar_pdf_cotizacion(cot, items) -> bytes
      cot   = fila de la tabla cotizaciones (codigo, cliente, vendedor, telefono, email, fecha, vence, estado, numero_pedido)
      items = filas de cotizacion_items (modelo, descripcion, medidas, cantidad, precio_unitario, descuento_pct,
              total_linea, foto_url) — "foto_bytes" opcional (bytes) tiene prioridad sobre foto_url.
"""
import io
import os
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

from pedido_pdf import (ASSETS, DIRECCION, WEB, CORREO, WHATSAPP, INSTAGRAM, FACEBOOK, _imagen)

NEGRO = colors.black
BEIGE = colors.HexColor("#C4BD97")
GRIS = colors.HexColor("#D9D9D9")
MARGEN = 36
ANCHO = letter[0] - 2 * MARGEN            # 540
HUECO = 12
ANCHO_TARJETA = (ANCHO - HUECO) / 2      # 264
ALTO_TARJETA = 166
FOTO_W, FOTO_H = 108, 150
POR_FILA = 2
FILAS_PRIMERA, FILAS_RESTO = 3, 4         # 6 en la primera hoja, 8 en las demás


def _st(nombre, **kw):
    base = dict(fontName="Times-Roman", fontSize=10, leading=12)
    base.update(kw)
    return ParagraphStyle(nombre, **base)


ST_LBL = _st("c_lbl", fontName="Times-Bold", fontSize=9.5)
ST_VAL = _st("c_val", fontSize=9.5, textColor=colors.HexColor("#1a1a1a"))
ST_TIT = _st("c_tit", fontName="Times-Bold", fontSize=15, leading=18, alignment=TA_RIGHT)
ST_NOM = _st("c_nom", fontName="Times-Bold", fontSize=10.5, leading=12)
ST_MOD = _st("c_mod", fontSize=8.5, leading=10, textColor=colors.HexColor("#555555"))
ST_DET = _st("c_det", fontSize=9.5, leading=11.5)
ST_DESC = _st("c_desc", fontName="Times-Italic", fontSize=9.5, leading=11.5, textColor=colors.HexColor("#8a1f1f"))
ST_TOT = _st("c_tot", fontName="Times-Bold", fontSize=12, leading=14)
ST_NOTA = _st("c_nota", fontSize=8, leading=10)
ST_TOT_L = _st("c_totl", alignment=TA_RIGHT)
ST_TOT_LB = _st("c_totlb", fontName="Times-Bold", fontSize=12, alignment=TA_RIGHT)
ST_NOFOTO = _st("c_nof", fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#888888"))


def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def _dinero(x):
    return f"${_num(x):,.2f}"


def _fecha(v):
    if not v:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%d/%m/%Y")
    try:
        return dt.date.fromisoformat(str(v)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(v)


# ----------------------------------------------------------------------
# Fotos
# ----------------------------------------------------------------------
def _cargar_foto(it):
    """Devuelve bytes JPEG reducidos de la foto del renglón, o None si no hay / no se pudo."""
    try:
        from PIL import Image as PILImage, ImageOps
        crudo = it.get("foto_bytes")
        if not crudo:
            url = it.get("foto_url")
            if not url:
                return None
            if str(url).startswith("http"):
                import requests
                r = requests.get(url, timeout=8)
                if r.status_code != 200 or not r.content:
                    return None
                crudo = r.content
            elif os.path.exists(url):
                crudo = open(url, "rb").read()
            else:
                return None
        im = PILImage.open(io.BytesIO(crudo))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((500, 650))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return None


def _celda_foto(datos):
    if datos:
        from reportlab.lib.utils import ImageReader
        w, h = ImageReader(io.BytesIO(datos)).getSize()
        esc = min(FOTO_W / w, FOTO_H / h)
        return Image(io.BytesIO(datos), width=w * esc, height=h * esc)
    return Paragraph("Sin foto", ST_NOFOTO)


# ----------------------------------------------------------------------
# Tarjeta de producto
# ----------------------------------------------------------------------
def _tarjeta(it, foto):
    cant = int(_num(it.get("cantidad"), 1))
    precio = _num(it.get("precio_unitario"))
    dsc = _num(it.get("descuento_pct"))
    total = _num(it.get("total_linea"), round(precio * cant * (1 - dsc / 100.0)))
    modelo = "" if (it.get("es_sn") or not it.get("modelo")) else f"Modelo {it.get('modelo')}"
    medidas = str(it.get("medidas") or "").strip()

    txt = [Paragraph(escape(str(it.get("descripcion") or "Sin nombre")), ST_NOM)]
    if modelo:
        txt.append(Paragraph(escape(modelo), ST_MOD))
    txt.append(Spacer(1, 3))
    if medidas:
        txt.append(Paragraph(f"<b>Medidas:</b> {escape(medidas)}", ST_DET))
    txt.append(Paragraph(f"<b>Cantidad:</b> {cant}", ST_DET))
    if dsc > 0:
        txt.append(Paragraph(f"<b>Precio:</b> {_dinero(precio)}" + (" c/u" if cant > 1 else ""), ST_DET))
        txt.append(Paragraph(f"Descuento -{dsc:g}%", ST_DESC))
    elif cant > 1:
        txt.append(Paragraph(f"<b>Precio:</b> {_dinero(precio)} c/u", ST_DET))
    txt.append(Spacer(1, 4))
    txt.append(Paragraph(f"Total: {_dinero(total)}", ST_TOT))

    t = Table([[_celda_foto(foto), txt]], colWidths=[FOTO_W + 8, ANCHO_TARJETA - FOTO_W - 8 - 4],
              rowHeights=[ALTO_TARJETA - 8])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BOX", (0, 0), (-1, -1), 0.6, NEGRO),
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F4F2E8")),
    ]))
    return t


def _bloque_filas(tarjetas):
    """Arma una tabla con las tarjetas de 2 en 2."""
    filas = []
    for i in range(0, len(tarjetas), POR_FILA):
        fila = tarjetas[i:i + POR_FILA]
        while len(fila) < POR_FILA:
            fila.append("")
        filas.append([fila[0], "", fila[1]])
    t = Table(filas, colWidths=[ANCHO_TARJETA, HUECO, ANCHO_TARJETA], rowHeights=[ALTO_TARJETA] * len(filas))
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    t.hAlign = "LEFT"
    return t


# ----------------------------------------------------------------------
# Encabezado, pie y marca de agua
# ----------------------------------------------------------------------
def _campo(etiqueta, valor, ancho_et, ancho_val):
    t = Table([[Paragraph(escape(etiqueta), ST_LBL), Paragraph(escape(str(valor or "")), ST_VAL)]],
              colWidths=[ancho_et, ancho_val])
    t.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return t


def _encabezado(cot):
    story = []
    logo = _imagen("logo_viriathus.png", ancho=130)
    if logo is not None:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 4)]
    izq = Table([[_campo("Cliente:", cot.get("cliente"), 50, 205)],
                 [_campo("Vendedor:", cot.get("vendedor"), 50, 205)],
                 [_campo("Teléfono:", cot.get("telefono"), 50, 205)],
                 [_campo("E-mail:", cot.get("email"), 50, 205)]], colWidths=[260])
    pendiente = "PENDIENTE" if not cot.get("numero_pedido") else str(cot.get("numero_pedido"))
    der = Table([[Paragraph(f"COTIZACIÓN {escape(str(cot.get('codigo') or ''))}", ST_TIT)],
                 [_campo("No. Pedido:", pendiente, 62, 128)],
                 [_campo("Fecha:", _fecha(cot.get("fecha")), 62, 128)],
                 [_campo("Válida hasta:", _fecha(cot.get("vence")), 62, 128)]], colWidths=[ANCHO - 280])
    for t in (izq, der):
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    cab = Table([[izq, "", der]], colWidths=[260, 20, ANCHO - 280])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    cab.hAlign = "LEFT"
    story += [cab, Spacer(1, 8)]
    return story


def _cartel(cot):
    est = cot.get("estado")
    if est == "Vencida":
        return "VENCIDA", colors.HexColor("#b36b00")
    if est == "Cancelada":
        return "CANCELADA", colors.HexColor("#b00020")
    if est == "Convertida":
        return f"PEDIDO {cot.get('numero_pedido') or ''}".strip(), colors.HexColor("#2e7d32")
    return None, None


def _hacer_paginas(cot):
    texto, color = _cartel(cot)

    def pie(c, doc):
        c.saveState()
        if doc.page > 1:
            c.setFont("Times-Roman", 8)
            c.drawRightString(letter[0] - MARGEN, letter[1] - 24, f"Cotización {cot.get('codigo', '')} — hoja {doc.page}")
        c.setFont("Times-Roman", 10)
        c.drawCentredString(letter[0] / 2, 58, DIRECCION)
        c.setFont("Times-Roman", 9.5)
        y = 36
        c.drawString(MARGEN, y, WEB)
        c.drawString(MARGEN + 95, y, CORREO)
        x = MARGEN + 235
        from reportlab.lib.utils import ImageReader
        for icono, txt in (("ico_whatsapp.png", WHATSAPP), ("ico_instagram.png", INSTAGRAM),
                           ("ico_facebook.png", FACEBOOK)):
            ruta = os.path.join(ASSETS, icono)
            if os.path.exists(ruta):
                w, h = ImageReader(ruta).getSize()
                c.drawImage(ruta, x, y - 2.5, width=12 * w / h, height=12, mask="auto")
                x += 12 * w / h + 4
            c.drawString(x, y, txt)
            x += c.stringWidth(txt, "Times-Roman", 9.5) + 18
        c.restoreState()

    def marca(c, doc):
        if not texto:
            return
        c.saveState()
        c.setFillColor(color)
        c.setFillAlpha(0.16)
        c.setFont("Helvetica-Bold", 80)
        c.translate(letter[0] / 2, letter[1] / 2)
        c.rotate(35)
        c.drawCentredString(0, 0, texto)
        c.restoreState()

    return pie, marca


# ----------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------
def generar_pdf_cotizacion(cot, items):
    with ThreadPoolExecutor(max_workers=6) as ex:
        fotos = list(ex.map(_cargar_foto, items))

    tarjetas = [_tarjeta(it, fotos[i]) for i, it in enumerate(items)]
    story = _encabezado(cot)

    pos = 0
    primera = True
    while pos < len(tarjetas) or primera:
        n = (FILAS_PRIMERA if primera else FILAS_RESTO) * POR_FILA
        trozo = tarjetas[pos:pos + n]
        if trozo:
            story.append(_bloque_filas(trozo))
        pos += n
        if pos < len(tarjetas):
            story.append(PageBreak())
        primera = False

    # ---- Totales (sin IVA) ----
    bruto = sum(_num(it.get("precio_unitario")) * int(_num(it.get("cantidad"), 1)) for it in items)
    total = sum(_num(it.get("total_linea"),
                     round(_num(it.get("precio_unitario")) * int(_num(it.get("cantidad"), 1))
                           * (1 - _num(it.get("descuento_pct")) / 100.0))) for it in items)
    piezas = sum(int(_num(it.get("cantidad"), 1)) for it in items)
    filas = []
    if round(bruto - total, 2) > 0:
        filas.append([Paragraph("Importe", ST_TOT_L), Paragraph(_dinero(bruto), ST_TOT_L)])
        filas.append([Paragraph("Descuento", ST_TOT_L), Paragraph("-" + _dinero(bruto - total), ST_TOT_L)])
    filas.append([Paragraph(f"TOTAL ({piezas} pieza{'s' if piezas != 1 else ''})", ST_TOT_LB),
                  Paragraph(_dinero(total), ST_TOT_LB)])
    tt = Table(filas, colWidths=[150, 100])
    tt.setStyle(TableStyle([("LINEABOVE", (0, len(filas) - 1), (-1, len(filas) - 1), 0.8, NEGRO),
                            ("BACKGROUND", (0, len(filas) - 1), (-1, len(filas) - 1), BEIGE),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    tt.hAlign = "RIGHT"

    notas = [Paragraph("<b>" + escape(f"Los objetos de esta cotización quedan apartados hasta el {_fecha(cot.get('vence'))}. "
                                      "Pasada esa fecha sin confirmar, vuelven a estar disponibles.") + "</b>", ST_NOTA),
             Paragraph("Precios en pesos mexicanos. No incluye entrega; se cotiza por separado.", ST_NOTA),
             Paragraph("Por ser objetos recuperados pueden tener detalles de conservación que, más que defectos, "
                       "son parte de su carácter y patina. No hay cambios ni devoluciones.", ST_NOTA)]
    story.append(KeepTogether([Spacer(1, 8), tt, Spacer(1, 6)] + notas))

    pie, marca = _hacer_paginas(cot)
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter, leftMargin=MARGEN, rightMargin=MARGEN, topMargin=30,
                          bottomMargin=78, title=f"Cotización {cot.get('codigo', '')}", author="Viriathus")
    marco = Frame(MARGEN, 78, ANCHO, letter[1] - 30 - 78, leftPadding=0, rightPadding=0, topPadding=0,
                  bottomPadding=0, id="f")

    def al_final(c, d):
        pie(c, d)
        marca(c, d)

    doc.addPageTemplates([PageTemplate(id="p", frames=[marco], onPage=al_final)])
    doc.build(story)
    return buf.getvalue()