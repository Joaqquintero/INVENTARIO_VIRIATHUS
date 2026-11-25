"""
PDF de la COTIZACIÓN DE RENTA: copia del Excel «Renta LOLA Films» (mismas oraciones, rojos, verdes y recuadros).

    generar_pdf_renta(renta, items, firma=None) -> bytes
      renta = fila de la tabla rentas (id, cliente, retiro, devolucion, dias, garantia_pct, creado)
      items = filas de renta_items (modelo, nombre, precio, pct, cantidad) + "foto_bytes" (opcional)
              En la columna DESCRIPCIÓN va solo el NOMBRE de la pieza (no la descripción larga).
      firma = firmas.para_pdf(...) si el cliente ya firmó: {"png", "nombre", "fecha"}

Como el Excel («ajustar a 1 página»): se intenta que todo entre en UNA hoja, achicando un poco si hace falta.
Si con muchas piezas no entra, la hoja 2 lleva los totales, los datos de pago, las condiciones y las firmas
juntos (nunca una hoja con la firma sola).

    pdf_a_imagenes(pdf_bytes) -> [bytes JPG]   (para mostrar el contrato en el link de firma)
"""
import datetime as dt
import io
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer,
                                Table, TableStyle)

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_pedido")
ROJO = colors.HexColor("#FF0000")
VERDE = colors.HexColor("#CCFFCC")
NEGRO = colors.black
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre",
         "Noviembre", "Diciembre"]
# Márgenes del Excel (izq. 0.35", der. 0.2", arriba 0.79", abajo 0.59") algo más parejos
M_IZQ, M_DER, M_ARR, M_ABA = 26, 18, 30, 24
ANCHO = letter[0] - M_IZQ - M_DER
# Anchos de columna del Excel (la E, «PRECIO CON IVA», está oculta también aquí)
_COLS = {"A": 3.5, "B": 6.6, "C": 17.3, "D": 17.0, "F": 11.4, "G": 11.4, "H": 12.4, "I": 1.2, "J": 4.6, "K": 8.2,
         "L": 6.5, "M": 3.9, "N": 12.4}
ANCHOS = [ANCHO * v / sum(_COLS.values()) for v in _COLS.values()]

# ---------- Datos fijos del Excel ----------
CUENTA_RENTA = ["ENSALADA DE IDEAS, S.A. DE C.V.", "BBVA BANCOMER", "0196717012", "012180001967170126",
                "marisol@viriathus.com.mx"]
CUENTA_GARANTIA = ["ERNESTO VIRIATO CUENCA MADRIGAL", "BANCOMER", "111 3243 567", "0121 800 111 3243 5673",
                   "ernesto@viriathus.com.mx"]
CONDICIONES = [
    ('Si las piezas regresan en buenas condiciones y completas, se les regresa el dinero "Complemento de la '
     'Garantía" tal cual.', False),
    ("Si algo se daña o pierde, se cobra la restauración o reposición del valor total más IVA y se facturará al "
     "cliente si lo requiere.", False),
    ("Si los objetos no son devueltos en la fecha convenida, se cobrarán los días de renta que se tarden en "
     "regresar.", False),
    ("El cliente deberá recoger y regresar las piezas en la tienda de Mérida # 22 Col. Roma Norte, en los "
     "siguientes horarios:", False),
    ("Lunes a Viernes de 10 a 19 horas.", False),
    ("Sábados de 11:30 a 18:30 horas.", False),
    ("", False),
    ("Las piezas se pueden recoger un día antes y entregar un día después de los días de la renta siempre", False),
    ("y cuando esté realizado el pago de la Renta y el Depósito para Garantía.", False),
    ("NO SE ENTREGAN LAS PIEZAS SIN DICHOS REQUISITOS, SIN EXCEPCIÓN.", True),
    ("No incluye flete. No incluye empaque ni embalaje.", False),
]


# ---------- Letra: Carlito (las mismas medidas que Calibri, la del Excel) ----------
def _fuentes():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    base = os.path.join(ASSETS, "fuentes")
    try:
        for nombre, archivo in (("Carlito", "Carlito-Regular.ttf"), ("Carlito-Bold", "Carlito-Bold.ttf"),
                                ("Carlito-Italic", "Carlito-Italic.ttf")):
            if nombre not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(nombre, os.path.join(base, archivo)))
        pdfmetrics.registerFontFamily("Carlito", normal="Carlito", bold="Carlito-Bold", italic="Carlito-Italic",
                                      boldItalic="Carlito-Bold")
        return "Carlito", "Carlito-Bold", "Carlito-Italic"
    except Exception:
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


# ---------- Cálculos (los mismos que la página) ----------
def _num(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip()) if v not in (None, "") else d
    except (TypeError, ValueError):
        return d


def calcular(items, dias, garantia_pct=80):
    """Cada renglón: subtotal / IVA / total del valor de las piezas (precio con IVA × cantidad), unitario de renta
    (precio × %) y renta (unitario × cantidad × días que se cobran). Devuelve (renglones, totales)."""
    dias = max(int(_num(dias, 1)), 0)
    filas, t = [], {"subtotal": 0.0, "iva": 0.0, "total": 0.0, "cantidad": 0, "renta": 0.0}
    for it in items:
        precio, cant, pct = _num(it.get("precio")), max(int(_num(it.get("cantidad"), 1)), 1), _num(it.get("pct"), 20)
        total = round(precio * cant, 2)
        subtotal = round(total / 1.16, 2)
        unitario = round(precio * pct / 100, 2)
        renta = round(unitario * cant * dias, 2)
        filas.append({**it, "cantidad": cant, "pct": pct, "subtotal": subtotal, "iva": round(total - subtotal, 2),
                      "total": total, "unitario": unitario, "renta": renta})
        t["subtotal"] += subtotal
        t["iva"] += total - subtotal
        t["total"] += total
        t["cantidad"] += cant
        t["renta"] += renta
    t = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}
    t["dias"] = dias
    t["garantia_pct"] = _num(garantia_pct, 80)
    t["garantia"] = round(t["subtotal"] * t["garantia_pct"] / 100, 2)
    return filas, t


def _fecha(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def fecha_larga(v):
    d = _fecha(v)
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}" if d else ""


def _m(x):
    return f"{_num(x):,.2f}"


def _pct(x):
    x = _num(x)
    return f"{x:g}%"


# ---------- Armado ----------
def _historia(renta, filas, t, firma, k, F):
    """Los bloques del PDF a escala k (1 = tamaño normal)."""
    reg, neg, cur = F

    def S(nombre, tam, fuente=None, color=NEGRO, alin=TA_LEFT, lead=1.18):
        return ParagraphStyle(nombre, fontName=fuente or reg, fontSize=tam * k, leading=tam * k * lead,
                              textColor=color, alignment=alin)

    s12, s12b = S("s12", 10), S("s12b", 10, neg)
    s12r, s12br = S("s12r", 10, color=ROJO), S("s12br", 10, neg, ROJO)
    s12i = S("s12i", 10, cur)
    s11, s11b = S("s11", 9.2), S("s11b", 9.2, neg)
    s11ir = S("s11ir", 9.2, cur, ROJO)
    s10, s10r = S("s10", 8.4), S("s10r", 8.4, color=ROJO)
    s_cab = S("cab", 6.6, neg, alin=TA_CENTER, lead=1.1)
    s_desc = S("desc", 8, lead=1.12)
    s_der = S("der", 8.4, alin=TA_RIGHT)
    s_cen = S("cen", 8.4, alin=TA_CENTER)
    s_tot = S("tot", 8.8, neg, alin=TA_RIGHT)
    s_fecha = S("fecha", 10, neg, alin=TA_RIGHT)
    P = lambda txt, st: Paragraph(escape(str(txt)), st)

    hoy = _fecha(renta.get("creado")) or dt.date.today()
    h = []
    logo = os.path.join(ASSETS, "logo_viriathus.png")
    if os.path.exists(logo):
        from reportlab.lib.utils import ImageReader
        w, hh = ImageReader(logo).getSize()
        img = Image(logo, width=44 * k * w / hh, height=44 * k)
        img.hAlign = "CENTER"
        h.append(img)
    h += [P(f"CDMX {hoy.day} {MESES[hoy.month - 1]}, {hoy.year}", s_fecha),
          P(renta.get("cliente") or "", s12br), P("Presente", s12), Spacer(1, 6 * k),
          P(f"COTIZACION DE RENTA {renta.get('id') or ''}", s12r),
          P("Después de saludarle, presentamos la Cotización de Renta de objetos de Viriathus.", s12),
          Spacer(1, 4 * k),
          P("NOTA: El cotizar no significa que se aparten los objetos.", s12i),
          P("Como están a la venta, están sujetos a disponibilidad al llegar la fecha de la renta.", s12i),
          P("Podemos buscar alternativas en caso de que algo se venda.", s12i),
          P(f"Periodo de renta: {t['dias']} {'día' if t['dias'] == 1 else 'días'}", s12br),
          P(f"Fechas de renta: recogiendo el {fecha_larga(renta.get('retiro'))} y regresando el "
            f"{fecha_larga(renta.get('devolucion'))}", s12br),
          Spacer(1, 6 * k)]

    # Tabla (como las columnas A–N del Excel, sin la E)
    cab = ["#", "MODELO", "DESCRIPCIÓN", "FOTO", "SUBTOTAL", "IVA", "TOTAL", "", "% RENTA POR DIA",
           "UNITARIO RENTA", "CANT OBJETOS", "DIAS", "PRECIO DE LA RENTA CON IVA"]
    datos = [[Paragraph(c, s_cab) for c in cab]]
    alto_foto = 70 * k
    for i, f in enumerate(filas, 1):
        foto = ""
        if f.get("foto_bytes"):
            try:
                from reportlab.lib.utils import ImageReader
                ir = ImageReader(io.BytesIO(f["foto_bytes"]))
                w, hh = ir.getSize()
                esc_ = min((ANCHOS[3] - 6) / w, alto_foto / hh)
                foto = Image(io.BytesIO(f["foto_bytes"]), width=w * esc_, height=hh * esc_)
            except Exception:
                foto = ""
        datos.append([P(i, s_cen), P(f.get("modelo") or "", s_cen), P(f.get("nombre") or "", s_desc), foto,
                      P(_m(f["subtotal"]), s_der), P(_m(f["iva"]), s_der), P(_m(f["total"]), s_der), "",
                      P(_pct(f["pct"]), s_cen), P(_m(f["unitario"]), s_der), P(f["cantidad"], s_cen),
                      P(t["dias"], s_cen), P(_m(f["renta"]), s_der)])
    datos.append(["", P("SUBTOTAL", s11), "", "", P(f"$ {_m(t['subtotal'])}", s_tot), P(f"$ {_m(t['iva'])}", s_tot),
                  P(f"$ {_m(t['total'])}", s_tot), "", "", "", P(t["cantidad"], s_cen), P(t["dias"], s_cen),
                  P(f"$ {_m(t['renta'])}", s_tot)])
    n = len(datos)
    estilo = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2 * k),
              ("RIGHTPADDING", (0, 0), (-1, -1), 2 * k), ("TOPPADDING", (0, 0), (-1, -1), 2 * k),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * k),
              ("GRID", (0, 0), (6, n - 2), 0.6, NEGRO), ("GRID", (8, 0), (12, n - 2), 0.6, NEGRO),
              ("ALIGN", (3, 1), (3, n - 2), "CENTER"),
              # SUBTOTAL: B–D unidas, F–H, L, M, N con bordes; N en verde
              ("SPAN", (1, n - 1), (3, n - 1)), ("BOX", (1, n - 1), (3, n - 1), 0.6, NEGRO),
              ("GRID", (4, n - 1), (6, n - 1), 0.6, NEGRO), ("GRID", (10, n - 1), (12, n - 1), 0.6, NEGRO),
              ("BACKGROUND", (12, n - 1), (12, n - 1), VERDE)]
    tabla = Table(datos, colWidths=ANCHOS, repeatRows=1)
    tabla.setStyle(TableStyle(estilo))
    h.append(tabla)

    # Totales (recuadros como D20/F20, D22/F22 en verde, D24/F24) con su texto al lado
    w_lbl = sum(ANCHOS[1:4]) - ANCHOS[1] * 0.3
    w_val = ANCHOS[4] * 1.2
    w_txt = ANCHO - ANCHOS[0] - w_lbl - w_val

    def fila_total(etq, valor, txt="", verde=False, chico=False):
        t_ = Table([[P(etq, S("tl", 9.2 if chico else 10, neg, alin=TA_RIGHT)), P(_m(valor), S("tv", 10, neg, alin=TA_RIGHT)),
                     P(txt, s11)]], colWidths=[w_lbl, w_val, w_txt])
        st_ = [("BOX", (0, 0), (1, 0), 0.6, NEGRO), ("LINEAFTER", (0, 0), (0, 0), 0.6, NEGRO),
               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.5 * k),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * k), ("LEFTPADDING", (2, 0), (2, 0), 8 * k)]
        if verde:
            st_.append(("BACKGROUND", (0, 0), (1, 0), VERDE))
        t_.setStyle(TableStyle(st_))
        t_.hAlign = "RIGHT"
        return t_

    def recuadro(txt, estilo_, verde=False):
        t_ = Table([[P(txt, estilo_)]], colWidths=[ANCHO - ANCHOS[0]])
        st_ = [("BOX", (0, 0), (-1, -1), 0.6, NEGRO), ("TOPPADDING", (0, 0), (-1, -1), 1.5 * k),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * k), ("LEFTPADDING", (0, 0), (-1, -1), 4 * k)]
        if verde:
            st_.append(("BACKGROUND", (0, 0), (-1, -1), VERDE))
        t_.setStyle(TableStyle(st_))
        t_.hAlign = "RIGHT"
        return t_

    def lineas(textos, estilo_):
        return [Paragraph(escape(x) if x else "&nbsp;", estilo_) for x in textos]

    sangria = ANCHOS[0]
    s11s = S("s11s", 9.2)
    s11s.leftIndent = sangria
    s11bs = S("s11bs", 9.2, neg)
    s11bs.leftIndent = sangria
    s11irs = S("s11irs", 9.2, cur, ROJO)
    s11irs.leftIndent = sangria
    s10s, s10rs = S("s10s", 8.4), S("s10rs", 8.4, color=ROJO)
    s10s.leftIndent = s10rs.leftIndent = sangria

    pie = [Spacer(1, 7 * k),
           fila_total("Valor total de los objetos sin IVA:", t["subtotal"]), Spacer(1, 5 * k),
           fila_total("Total de la Renta con IVA:", t["renta"], "Deposito / Transfer a Ensalada de Ideas", verde=True),
           Spacer(1, 5 * k),
           fila_total(f"Complemento de Garantía {t['garantia_pct']:g}% valor sin IVA:", t["garantia"],
                      "Deposito / Transfer a Ernesto V. Cuenca Madrigal o efectivo", chico=True),
           Spacer(1, 8 * k),
           recuadro("El monto de la RENTA CON IVA deberá depositarse o transferirse 24 horas antes de recoger las "
                    "piezas a: ", s11, verde=True), Spacer(1, 3 * k),
           *lineas(CUENTA_RENTA, s11s), Spacer(1, 6 * k),
           recuadro("El COMPLEMENTO DE GARANTIA deberá depositarse o transferirse 24 horas antes o llevarlo en "
                    "efectivo al recoger: ", s11), Spacer(1, 3 * k),
           *lineas(CUENTA_GARANTIA, s11s),
           Paragraph("La Garantía también puede darse en Efectivo al recoger los objetos.", s11bs),
           Paragraph("NOTA: NO SE ACEPTA TARJETA NI CHEQUE PARA LA GARANTIA SIN EXCEPCIÓN.", s11irs),
           Spacer(1, 6 * k),
           recuadro("CONDICIONES PARA LA RENTA:", s11b), Spacer(1, 3 * k)]
    for txt, rojo in CONDICIONES:
        pie.append(Paragraph(escape(txt) if txt else "&nbsp;", s10rs if rojo else s10s))
    pie.append(Spacer(1, 10 * k))

    # Firmas: Ernesto a la izquierda; el cliente a la derecha (Firma / Nombre / Fecha)
    firma_img = ""
    if firma and firma.get("png"):
        try:
            from reportlab.lib.utils import ImageReader
            ir = ImageReader(io.BytesIO(firma["png"]))
            w, hh = ir.getSize()
            e_ = min(150 * k / w, 34 * k / hh)
            firma_img = Image(io.BytesIO(firma["png"]), width=w * e_, height=hh * e_)
        except Exception:
            firma_img = ""
    w_izq = sum(ANCHOS[:6])
    w_nom = 46
    w_der = ANCHO - w_izq - w_nom
    fecha_firma = firma.get("fecha", "")[:10] if firma else ""
    izq = [P("Atentamente", s12), Spacer(1, 30 * k), P("Ernesto Cuenca Madrigal", s12b), P("Director General", s12)]
    der = Table([
        [P("Acepto cotización y condiciones:", s12), ""],
        ["", firma_img],
        ["", P("Firma", S("fr", 10, alin=TA_RIGHT))],
        [P("Nombre", s12), P((firma or {}).get("nombre") or renta.get("cliente") or "", s12b)],
        [P("Fecha", s12), P(fecha_firma, s12)],
    ], colWidths=[w_nom, w_der], rowHeights=[None, 36 * k, None, None, None])
    der.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("LINEBELOW", (1, 1), (1, 1), 0.6, NEGRO),
                             ("LINEBELOW", (1, 3), (1, 4), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                             ("ALIGN", (1, 1), (1, 1), "CENTER"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 1 * k), ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * k)]))
    firmas = Table([[izq, der]], colWidths=[w_izq, ANCHO - w_izq])
    firmas.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), sangria)]))
    pie.append(firmas)
    if firma:
        pie += [Spacer(1, 4 * k),
                P(f"Firmado electrónicamente por {firma.get('nombre') or ''} el {firma.get('fecha') or ''} desde el "
                  f"link de Viriathus, después de leer la cotización completa y aceptar las condiciones.",
                  S("fe", 7.5, color=colors.HexColor("#555555"), alin=TA_RIGHT))]
    return h, pie


def _construir(renta, filas, t, firma, k, juntar):
    F = _fuentes()
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter, leftMargin=M_IZQ, rightMargin=M_DER, topMargin=M_ARR,
                          bottomMargin=M_ABA, title=f"Cotización de renta {renta.get('id') or ''}", author="Viriathus")
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(M_IZQ, M_ABA, ANCHO, letter[1] - M_ARR - M_ABA, id="f",
                                                             leftPadding=0, rightPadding=0, topPadding=0,
                                                             bottomPadding=0)])])
    cab, pie = _historia(renta, filas, t, firma, k, F)
    doc.build(cab + ([KeepTogether(pie)] if juntar else pie))
    return buf.getvalue(), doc.page


def generar_pdf_renta(renta, items, firma=None):
    filas, t = calcular(items, renta.get("dias"), renta.get("garantia_pct"))
    # 1) Como el Excel: todo en una hoja (achicando un poco si hace falta)
    for k in (1.0, 0.95, 0.9, 0.85, 0.8):
        pdf, paginas = _construir(renta, filas, t, firma, k, juntar=False)
        if paginas == 1:
            return pdf
    # 2) No entra: tamaño normal y la parte de abajo (totales, pagos, condiciones y firmas) junta en la hoja 2
    pdf, _ = _construir(renta, filas, t, firma, 1.0, juntar=True)
    return pdf


def pdf_a_imagenes(pdf_bytes, ancho=1240):
    """Cada hoja del PDF como JPG (para el link de firma: el cliente ve exactamente el contrato)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    out = []
    try:
        for i in range(len(doc)):
            pag = doc[i]
            escala = ancho / pag.get_width()
            img = pag.render(scale=escala).to_pil().convert("RGB")
            b = io.BytesIO()
            img.save(b, "JPEG", quality=82, optimize=True)
            out.append(b.getvalue())
    finally:
        doc.close()
    return out
