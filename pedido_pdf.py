"""
Genera el PDF del pedido (mismo diseño que la hoja '1 Pedido Digital').

Uso:  pdf_bytes = generar_pdf_pedido(pedido, items, vendedores)

  pedido     -> fila de la tabla `pedidos` (dict)
  items      -> filas de `pedido_items` (lista de dict)
  vendedores -> lista de nombres

Requiere:  pip install reportlab
Las imágenes (logo e íconos) se leen de la carpeta  assets_pedido/  junto a este archivo.
"""
import io
import os
import datetime as dt
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageBreak, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

# ----------------------------------------------------------------------
# Datos fijos del formato (cámbialos aquí si algo cambia)
# ----------------------------------------------------------------------
CORREO_CONSTANCIA = "marisol@viriathus.com.mx"   # "Enviar Constancia de Situación Fiscal a:"
DIRECCION = "SHOWROOM Mérida 22, Col. Roma Norte (esq. Puebla) C.P.:06700, CDMX 55 2624.3552 Y 53 ext. 101"
WEB = "viriathus.com.mx"
CORREO = "info@viriathus.com.mx"
WHATSAPP = "56 2694 8583"
INSTAGRAM = "ViriathusAntigüedades"
FACEBOOK = "ViriathusAntigüedades"
ENTREGA_CAMPOS = ("domicilio", "fecha_entrega", "horario_entrega", "atencion",
                  "telefono_entrega", "email_entrega", "notas_entrega")
FACTURA_CAMPOS = ("uso_cfdi", "concepto", "telefono_facturacion", "email_facturacion", "notas_facturacion")


def _mostrar_seccion(pedido, bandera, campos):
    """Entrega / facturación solo salen si se marcó el check. En pedidos viejos (sin check) salen si traen algún dato."""
    marca = pedido.get(bandera)
    if marca is not None:
        return bool(marca)
    return any(str(pedido.get(c) or "").strip() for c in campos)


FILAS_MINIMAS = 9          # el formato muestra al menos estas filas de productos (aunque estén vacías)

NOTAS = [
    "Nota: Por ser objetos recuperados pueden tener detalles de conservación que, más que defectos, son parte "
    "de su carácter y patina; por lo que el Cliente deberá revisar a detalle el objeto y firmar este pedido de conformidad.",
    "Favor de revisar el objeto antes de pagar y al recibirlo, ya que no hay cambios ni devoluciones.",
    "No incluye entrega.   Se cotiza por separado.  Los objetos que funcionan, no tienen garantía.",
]

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_pedido")
NEGRO = colors.black
BEIGE = colors.HexColor("#C4BD97")
GRIS = colors.HexColor("#D9D9D9")

MARGEN = 36
ANCHO = letter[0] - 2 * MARGEN        # 540 pt

# Anchos de columnas de la tabla de productos (proporcionales a la hoja de Excel)
COL_W = [50, 296, 64, 56, 74]


# ----------------------------------------------------------------------
# Estilos y utilidades
# ----------------------------------------------------------------------
def _st(nombre, **kw):
    base = dict(fontName="Times-Roman", fontSize=10, leading=12)
    base.update(kw)
    return ParagraphStyle(nombre, **base)


ST_LBL = _st("lbl", fontSize=9.5)
ST_LBL_R = _st("lblr", fontSize=9.5, alignment=TA_RIGHT)
ST_LBL_B = _st("lblb", fontName="Times-Bold")
ST_VAL = _st("val", fontSize=9.5, textColor=colors.HexColor("#1a1a1a"))
ST_SEC = _st("sec", fontName="Times-Bold")
ST_TH = _st("th", fontName="Times-Bold", fontSize=8, leading=9.5, textColor=colors.white, alignment=TA_CENTER)
ST_BAND = _st("band", fontName="Times-Bold", alignment=TA_CENTER)
ST_CELL = _st("cell", fontSize=10.5, leading=12)
ST_CELL_C = _st("cellc", fontSize=10.5, leading=12, alignment=TA_CENTER)
ST_CELL_R = _st("cellr", splitLongWords=0, fontSize=10.5, leading=12, alignment=TA_RIGHT)
ST_TOT_L = _st("totl", alignment=TA_RIGHT)
ST_TOT_LB = _st("totlb", fontName="Times-Bold", alignment=TA_RIGHT)
ST_TOT_R = _st("totr", splitLongWords=0, alignment=TA_RIGHT)
ST_TOT_RB = _st("totrb", splitLongWords=0, fontName="Times-Bold", alignment=TA_RIGHT)
ST_BLANCO_R = _st("blancor", splitLongWords=0, textColor=colors.white, alignment=TA_RIGHT)
ST_BLANCO_RB = _st("blancorb", splitLongWords=0, fontName="Times-Bold", textColor=colors.white, alignment=TA_RIGHT)
ST_NOTA = _st("nota", fontSize=7.5, leading=9)
ST_OBS_T = _st("obst", fontName="Times-Bold", fontSize=8)
ST_OBS = _st("obs", fontSize=9.5, leading=12)
ST_FIRMA = _st("firma", fontSize=8.5, alignment=TA_CENTER)


def _p(texto, estilo):
    return Paragraph(escape(str(texto)) if texto is not None else "", estilo)


def _dinero(x):
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _fecha(v):
    if not v:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%d/%m/%Y")
    try:
        return dt.date.fromisoformat(str(v)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(v)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _fila_campos(campos, ancho_total=ANCHO, hueco=8):
    """campos = [(etiqueta, valor, ancho_etiqueta)], los valores se reparten el ancho restante."""
    n = len(campos)
    fijo = sum(c[2] for c in campos) + hueco * (n - 1)
    ancho_val = (ancho_total - fijo) / n
    fila, anchos, estilos, col = [], [], [], 0
    for i, (etiq, valor, w_et) in enumerate(campos):
        fila += [_p(etiq, ST_LBL), Paragraph(escape(str(valor or "")), ST_VAL)]
        anchos += [w_et, ancho_val]
        estilos.append(("LINEBELOW", (col + 1, 0), (col + 1, 0), 0.6, NEGRO))
        col += 2
        if i < n - 1:
            fila.append("")
            anchos.append(hueco)
            col += 1
    t = Table([fila], colWidths=anchos)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                           ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)] + estilos))
    t.hAlign = "LEFT"
    return t


def _imagen(nombre, ancho=None, alto=None):
    ruta = os.path.join(ASSETS, nombre)
    if not os.path.exists(ruta):
        return None
    from reportlab.lib.utils import ImageReader
    w, h = ImageReader(ruta).getSize()
    if ancho:
        alto = ancho * h / w
    else:
        ancho = alto * w / h
    return Image(ruta, width=ancho, height=alto)


# ----------------------------------------------------------------------
# Pie de página (en todas las hojas) y marca de agua
# ----------------------------------------------------------------------
def _cartel_pedido(pedido):
    """Texto y color del cartel diagonal: lo que tenga el pedido en OBSERVACIONES
    (estado_pedido); si está vacío, el status. CANCELADO va en rojo."""
    estado = str(pedido.get("estado_pedido") or "").strip()
    status = str(pedido.get("status") or "").strip()
    texto = estado or status
    if not texto:
        return None, None
    bajo = f"{estado} {status}".lower()
    if "cancelado" in bajo:
        return "CANCELADO", colors.red
    if "pendiente" in texto.lower():
        color = colors.HexColor("#D97706")      # naranja: falta pago y/o entrega
    elif "pagado" in texto.lower():
        color = colors.HexColor("#1E8E3E")      # verde
    elif "enviado" in texto.lower() or "entregado" in texto.lower():
        color = colors.HexColor("#1D5FA8")      # azul
    else:
        color = colors.HexColor("#555555")      # gris
    return texto.upper(), color


def _lineas_cartel(c, texto, ancho_max=430, fuente="Helvetica-Bold", tam_max=90, tam_min=30):
    """Reparte el texto en 1-3 líneas y elige el tamaño de letra mayor que quepa."""
    palabras = texto.split()

    def repartir(n):
        objetivo = c.stringWidth(texto, fuente, 100) / n
        lineas, actual = [], []
        for w in palabras:
            prueba = " ".join(actual + [w])
            if actual and len(lineas) < n - 1 and c.stringWidth(prueba, fuente, 100) > objetivo:
                lineas.append(" ".join(actual))
                actual = [w]
            else:
                actual.append(w)
        lineas.append(" ".join(actual))
        return lineas

    mejor = None
    for n in (1, 2, 3):
        lineas = repartir(n)
        ancho100 = max(c.stringWidth(l, fuente, 100) for l in lineas)
        tam = min(tam_max, int(ancho_max / ancho100 * 100))
        if mejor is None or tam > mejor[1]:
            mejor = (lineas, tam)
        if tam >= 60:          # ya se lee bien; no hace falta partir más
            break
    lineas, tam = mejor
    return lineas, max(tam, tam_min)


def _hacer_pie(pedido):
    cartel_texto, cartel_color = _cartel_pedido(pedido)
    numero = pedido.get("numero_pedido", "")

    def pie(c, doc):
        c.saveState()
        if doc.page > 1 and not pedido.get("_sin_hoja"):
            c.setFont("Times-Roman", 8)
            c.drawRightString(letter[0] - MARGEN, letter[1] - 24, f"Pedido N° {numero} — hoja {doc.page}")
        c.setFont("Times-Roman", 10)
        c.drawCentredString(letter[0] / 2, 58, DIRECCION)
        c.setFont("Times-Roman", 9.5)
        y = 36
        c.drawString(MARGEN, y, WEB)
        c.drawString(MARGEN + 95, y, CORREO)
        x = MARGEN + 235
        for icono, texto, alto in (("ico_whatsapp.png", WHATSAPP, 12), ("ico_instagram.png", INSTAGRAM, 12),
                                   ("ico_facebook.png", FACEBOOK, 12)):
            ruta = os.path.join(ASSETS, icono)
            if os.path.exists(ruta):
                from reportlab.lib.utils import ImageReader
                w, h = ImageReader(ruta).getSize()
                c.drawImage(ruta, x, y - 2.5, width=alto * w / h, height=alto, mask="auto")
                x += alto * w / h + 4
            c.drawString(x, y, texto)
            x += c.stringWidth(texto, "Times-Roman", 9.5) + 18
        c.restoreState()

    def marca(c, doc):
        """Se dibuja al final de cada hoja, encima del contenido."""
        if not cartel_texto:
            return
        c.saveState()
        c.setFillColor(cartel_color)
        c.setFillAlpha(0.16)
        lineas, tam = _lineas_cartel(c, cartel_texto)
        c.setFont("Helvetica-Bold", tam)
        c.translate(letter[0] / 2, letter[1] / 2)
        c.rotate(35)
        alto = tam * 1.1
        y = (len(lineas) - 1) * alto / 2 - tam * 0.3
        for linea in lineas:
            c.drawCentredString(0, y, linea)
            y -= alto
        c.restoreState()
    return pie, marca


# ----------------------------------------------------------------------
# Pedidos importados del Excel: solo traen el texto "MOD. 28308 PARCHE..., SN POSTAL..."
# ----------------------------------------------------------------------
_MODELO_RX = r"(?:MOD\.?\s*)?(?:\d{4,6}(?:\.\d+)?|SN|S/N|TTEN\d+)\b"


def items_desde_texto(texto):
    """Convierte el texto libre de modelos en renglones [{modelo, es_sn, descripcion, cantidad}].
    No hay precios por pieza en el Excel, así que los renglones salen sin precio."""
    import re
    if not texto or not str(texto).strip():
        return []
    t = str(texto).replace("\r", "\n")
    t = re.split(r"\bTOTAL\s*:", t, flags=re.I)[0]                    # lo que sigue a "TOTAL: $..." son notas
    items = []
    for linea in t.split("\n"):
        linea = linea.strip()
        if not linea or linea.upper().startswith("CANCELADO"):
            continue
        for trozo in re.split(r",\s*(?=" + _MODELO_RX + r")", linea, flags=re.I):
            trozo = re.sub(r"\([^)]*\bCOSTO[^)]*\)", "", trozo, flags=re.I)   # "(COSTO REAL ... $1,337)" es interno
            trozo = trozo.strip(" .;")
            if not trozo or re.search(r"PROVEEDOR|\bDRIVE\b|\bCOSTOS?\b|\bUTILIDAD", trozo, flags=re.I):
                continue                                                      # notas internas: no se imprimen
            trozo = re.sub(r"^MOD\.?\s*", "", trozo, flags=re.I)
            m = re.match(r"^(\d{1,6}(?:\.\d+)?|SN|S/N|TTEN\d+)\s*(.*)$", trozo, flags=re.I | re.S)
            if m:
                modelo, desc = m.group(1).upper(), m.group(2).strip(" -.")
            else:
                modelo, desc = "", trozo
            es_sn = modelo in ("SN", "S/N")
            cant = 1
            q = re.search(r"\(\s*(\d+)\s*PZ[A-Z]*\s*\)", desc, flags=re.I)
            if q:
                cant = int(q.group(1))
                desc = (desc[:q.start()] + desc[q.end():]).strip(" -.")
            items.append({"modelo": None if es_sn else (modelo or None), "es_sn": es_sn or not modelo,
                          "descripcion": desc or "(sin descripción)", "cantidad": cant,
                          "precio_unitario": None})
    return items


# ----------------------------------------------------------------------
# Generador principal
# ----------------------------------------------------------------------
def _story_pedido(pedido, items, vendedores, vacias=None):
    story = []
    sin_precios = False
    if not items:                                  # pedido importado del Excel: solo hay texto
        items = items_desde_texto(pedido.get("modelos_texto"))
        sin_precios = bool(items)

    # ---- Logo ----
    logo = _imagen("logo_viriathus.png", ancho=150)
    if logo is not None:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 2)]

    # ---- Cliente (izquierda) y datos del pedido (derecha) ----
    def campo_der(etiqueta, valor):
        t = Table([[_p(etiqueta, ST_LBL_R), Paragraph(escape(str(valor or "")), ST_VAL)]], colWidths=[72, 138])
        t.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return t

    derecha = Table([[campo_der("No. Pedido:", pedido.get("numero_pedido"))],
                     [campo_der("Vendedor:", " / ".join(vendedores))],
                     [campo_der("Fecha Pedido:", _fecha(pedido.get("fecha")))],
                     [campo_der("Procedencia:", pedido.get("categoria_venta"))]], colWidths=[210])
    derecha.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    cliente = Table([[_p("CLIENTE:", ST_LBL_B), Paragraph(escape(str(pedido.get("cliente") or "")), ST_VAL)]],
                    colWidths=[52, 258])
    cliente.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                 ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    cab = Table([[cliente, "", derecha]], colWidths=[310, 20, 210])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    cab.hAlign = "LEFT"
    story += [cab, Spacer(1, 3)]

    # ---- Contacto del cliente (siempre) ----
    campos_c = [("Teléfono:", pedido.get("telefono"), 48), ("eMail:", pedido.get("email"), 80)]
    if str(pedido.get("celular") or "").strip():
        campos_c.insert(1, ("Celular:", pedido.get("celular"), 40))
    story.append(_fila_campos(campos_c))
    story.append(Spacer(1, 6))

    # ---- Datos de entrega (solo si se pidieron o traen datos) ----
    if _mostrar_seccion(pedido, "requiere_entrega", ENTREGA_CAMPOS):
        story.append(_p("DATOS DE ENTREGA", ST_SEC))
        dom = Table([[_p("Domicilio:", ST_LBL), Paragraph(escape(str(pedido.get("domicilio") or "")), ST_VAL)],
                     ["", ""]], colWidths=[52, ANCHO - 52 - 40], rowHeights=[None, 14])
        dom.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 1), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        dom.hAlign = "LEFT"
        story += [dom, Spacer(1, 2)]
        story.append(_fila_campos([("Fecha de Entrega:", _fecha(pedido.get("fecha_entrega")), 80),
                                   ("Horario de Entrega:", pedido.get("horario_entrega"), 88),
                                   ("Atención:", pedido.get("atencion"), 46)]))
        story.append(_fila_campos([("Teléfono:", pedido.get("telefono_entrega"), 48),
                                   ("eMail:", pedido.get("email_entrega"), 80)]))
        story.append(_fila_campos([("Notas:", pedido.get("notas_entrega"), 40)], ancho_total=ANCHO - 20))
        story.append(Spacer(1, 6))

    # ---- Datos de facturación (solo si se pidieron o traen datos) ----
    if _mostrar_seccion(pedido, "requiere_facturacion", FACTURA_CAMPOS):
        story.append(_p("DATOS DE FACTURACIÓN", ST_SEC))
        story.append(_fila_campos([("Enviar Constancia de Situación Fiscal a:", CORREO_CONSTANCIA, 168),
                                   ("Uso CFDI:", pedido.get("uso_cfdi"), 46)]))
        story.append(_fila_campos([("Concepto:", pedido.get("concepto"), 48)]))
        story.append(_fila_campos([("Teléfono:", pedido.get("telefono_facturacion"), 48),
                                   ("eMail:", pedido.get("email_facturacion"), 80)]))
        story.append(_fila_campos([("Notas:", pedido.get("notas_facturacion"), 40)], ancho_total=ANCHO - 20))
        story.append(Spacer(1, 6))

    # ---- Tabla de productos ----
    filas = [[_p("Modelo", ST_TH), _p("Descripción", ST_TH), _p("Precio Unitario", ST_TH),
              _p("Cantidad", ST_TH), _p("Total", ST_TH)],
             [_p("Lote OBJETOS", ST_BAND), "", "", "", ""]]
    bruto, piezas = 0.0, 0
    envio = round(_num(pedido.get("envio")), 2)
    if envio > 0:      # el envío va como un renglón más, abajo de los modelos (no cuenta como objeto)
        items = list(items) + [{"es_sn": True, "descripcion": "Servicio de envío", "cantidad": 1,
                                "precio_unitario": envio, "total_linea": envio, "_envio": True}]
    for it in items:
        cant = int(_num(it.get("cantidad"), 1))
        if not it.get("_envio"):
            piezas += cant
        modelo = "S/N" if (it.get("es_sn") or not it.get("modelo")) else str(it.get("modelo"))
        if it.get("_envio"):
            modelo = "ENVÍO"
        if sin_precios and not it.get("_envio"):
            filas.append([_p(modelo, ST_CELL_C), _p(it.get("descripcion") or "", ST_CELL),
                          "", _p(cant, ST_CELL_C), ""])
            continue
        precio = _num(it.get("precio_unitario"))
        linea = precio * cant
        dpc = _num(it.get("descuento_pct"))
        desc_txt = escape(str(it.get("descripcion") or ""))
        if dpc > 0:
            # descuento por producto: el total del renglón ya va con descuento
            linea = _num(it.get("total_linea"), round(linea * (1 - dpc / 100.0)))
            desc_txt += f"<br/><font size='7'><i>Descuento -{dpc:g}%</i></font>"
        bruto += linea
        filas.append([_p(modelo, ST_CELL_C), Paragraph(desc_txt, ST_CELL),
                      _p(_dinero(precio), ST_CELL_R), _p(cant, ST_CELL_C), _p(_dinero(linea), ST_CELL_R)])
    if sin_precios:
        bruto = _num(pedido.get("total"))          # sin precios por pieza: el lote vale lo que el pedido
    # Filas en blanco: menos cuando entran las secciones de entrega / facturación, para que todo quepa en una hoja
    n_sec = (1 if _mostrar_seccion(pedido, "requiere_entrega", ENTREGA_CAMPOS) else 0) + \
            (1 if _mostrar_seccion(pedido, "requiere_facturacion", FACTURA_CAMPOS) else 0)
    n_vacias = max(0, (FILAS_MINIMAS + 3 - 3 * n_sec) - len(items)) if vacias is None else max(0, int(vacias))
    for _ in range(n_vacias):
        filas.append(["", "", "", "", ""])
    alturas = [24, 17] + [None] * len(items) + [20] * n_vacias

    tabla = Table(filas, colWidths=COL_W, rowHeights=alturas, repeatRows=2)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NEGRO),
        ("BACKGROUND", (0, 1), (-1, 1), BEIGE), ("SPAN", (0, 1), (-1, 1)),
        ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, GRIS]),
        ("GRID", (0, 0), (-1, -1), 0.5, NEGRO),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 2), (-1, -1), 4), ("BOTTOMPADDING", (0, 2), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tabla)

    # ---- Totales ----
    total = _num(pedido.get("total"))
    desc_pct = _num(pedido.get("descuento_pct"))
    con_iva = bool(pedido.get("con_iva"))
    anticipo = _num(pedido.get("anticipo"))
    descuento_monto = round(bruto - total, 2) if desc_pct > 0 else 0.0
    # Con envío, el subtotal y el IVA del PDF son de todo el pedido (productos + envío), para que cuadre con el total
    subtotal = round(total / 1.16, 2) if envio > 0 else _num(pedido.get("subtotal"))
    iva = round(total - subtotal, 2) if envio > 0 else _num(pedido.get("iva"))

    w3 = [COL_W[2], COL_W[3], COL_W[4]]
    sup = Table([[_p("Total Lote OBJETOS", ST_TOT_LB), "", _p(_dinero(bruto), ST_TOT_RB)],
                 [_p("TOTAL OBJETOS:", ST_BLANCO_R), "", _p(piezas, ST_BLANCO_RB)]], colWidths=w3)
    sup.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 1), (1, 1)),
                             ("BACKGROUND", (0, 0), (-1, 0), BEIGE), ("BACKGROUND", (0, 1), (-1, 1), NEGRO),
                             ("BOX", (0, 0), (-1, -1), 0.5, NEGRO), ("LINEBELOW", (0, 0), (-1, 0), 0.5, NEGRO),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    sup.hAlign = "RIGHT"

    inf, negritas = [], []
    if desc_pct > 0:
        inf.append(("IMPORTE" if con_iva else "SUBTOTAL", _dinero(bruto), False))
    elif con_iva:
        inf.append(("SUBTOTAL", _dinero(subtotal), False))
    if desc_pct > 0:
        inf.append((f"Descuento -{desc_pct:g}%", "-" + _dinero(descuento_monto), False))
    if con_iva:
        if desc_pct > 0:
            inf.append(("SUBTOTAL", _dinero(subtotal), False))
        inf.append(("IVA 16%", _dinero(iva), False))
    inf.append(("TOTAL", _dinero(total), True))
    if anticipo > 0:
        inf.append(("ANTICIPO", _dinero(anticipo), False))
        inf.append(("SALDO", _dinero(total - anticipo), True))
    datos_inf = [[_p(a, ST_TOT_LB if b else ST_TOT_L), "", _p(v, ST_TOT_RB if b else ST_TOT_R)] for a, v, b in inf]
    estilo_inf = [("SPAN", (0, i), (1, i)) for i in range(len(inf))]
    estilo_inf += [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                   ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for i, (a, _, b) in enumerate(inf):
        if a == "TOTAL":
            estilo_inf.append(("LINEABOVE", (0, i), (-1, i), 0.8, NEGRO))
    tot_inf = Table(datos_inf, colWidths=w3)
    tot_inf.setStyle(TableStyle(estilo_inf))

    notas = [Paragraph(escape(n), ST_NOTA) for n in NOTAS]
    notas_tot = Table([[notas, tot_inf]], colWidths=[ANCHO - sum(w3) - 6, sum(w3)])
    notas_tot.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 14),
                                   ("RIGHTPADDING", (1, 0), (1, 0), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

    # ---- Observaciones y firma ----
    texto_obs = " · ".join(x for x in [pedido.get("estado_pedido"), pedido.get("observaciones")] if x)
    obs = Table([[_p("OBSERVACIONES", ST_OBS_T)],
                 [Paragraph(escape(texto_obs), ST_OBS)], [""], [""]], colWidths=[330], rowHeights=[12, None, 18, 18])
    obs.setStyle(TableStyle([("LINEBELOW", (0, 1), (0, 3), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                             ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    celda = ""
    fz = pedido.get("_firma")
    if fz and fz.get("png"):                 # firma dibujada en el teléfono del frente
        try:
            from reportlab.lib.utils import ImageReader
            w, h = ImageReader(io.BytesIO(fz["png"])).getSize()
            esc = min(158 / w, 32 / h)
            img = Image(io.BytesIO(fz["png"]), width=w * esc, height=h * esc)
            img.hAlign = "CENTER"
            celda = [img, Paragraph(f"<font size='6.5'>{escape(fz.get('nombre') or '')} · {escape(fz.get('fecha') or '')}</font>",
                                    ST_FIRMA)]
        except Exception:
            celda = ""
    firma = Table([[_p("Firma y nombre del Cliente", ST_FIRMA)], [celda]], colWidths=[170], rowHeights=[14, 56])
    firma.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, NEGRO), ("LINEBELOW", (0, 1), (-1, 1), 1.2, NEGRO),
                               ("ALIGN", (0, 1), (0, 1), "CENTER"),          # la firma, centrada en el recuadro
                               ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                               ("BOTTOMPADDING", (0, 1), (-1, 1), 10)]))
    abajo = Table([[obs, firma]], colWidths=[ANCHO - 170 - 10, 170])
    abajo.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), 10),
                               ("RIGHTPADDING", (1, 0), (1, 0), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))

    story.append(KeepTogether([sup, notas_tot, Spacer(1, 10), abajo]))
    return story


class _MarcaCopia(Flowable):
    """No dibuja nada: solo anota qué copia se está imprimiendo (ORIGINAL / COPIA CLIENTE) para el rótulo de la hoja."""
    def __init__(self, estado, texto):
        super().__init__()
        self.estado, self.texto = estado, texto

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        self.estado["copia"] = self.texto


def generar_pdf_pedido(pedido, items, vendedores, copias=None):
    """PDF del pedido. `copias` = lista de rótulos (p. ej. ["ORIGINAL", "COPIA CLIENTE"]): el pedido se repite
    una vez por rótulo, cada copia en hojas nuevas y con su rótulo en la esquina. Sin `copias`: una sola, sin rótulo."""
    # Las filas en blanco de relleno se quitan de a una hasta que el pedido entre en UNA hoja (si se puede)
    vacias = _filas_vacias_que_entran(pedido, items, vendedores)
    estado = {"copia": None}
    story = []
    for n, rotulo in enumerate(copias or [None]):
        if n:
            story.append(PageBreak())
        if rotulo:
            story.append(_MarcaCopia(estado, rotulo))
        story += _story_pedido(pedido, items, vendedores, vacias)
    return _armar(pedido, story, estado, copias)


def _filas_vacias_que_entran(pedido, items, vendedores):
    """Cuántas filas en blanco caben sin pasar a una segunda hoja (None = las de siempre)."""
    def hojas(vac):
        return _armar(pedido, _story_pedido(pedido, items, vendedores, vac), {"copia": None}, None, contar=True)
    if hojas(None) <= 1:
        return None
    n_sec = (1 if _mostrar_seccion(pedido, "requiere_entrega", ENTREGA_CAMPOS) else 0) + \
            (1 if _mostrar_seccion(pedido, "requiere_facturacion", FACTURA_CAMPOS) else 0)
    inicial = max(0, (FILAS_MINIMAS + 3 - 3 * n_sec) - len(items))
    for vac in range(inicial - 1, -1, -1):
        if hojas(vac) <= 1:
            return vac
    return 0                                     # ni sin relleno entra: van las hojas que hagan falta


def _armar(pedido, story, estado, copias, contar=False):
    buf = io.BytesIO()
    pie, marca = _hacer_pie(dict(pedido, _sin_hoja=True) if copias and len(copias) > 1 else pedido)

    def fin_hoja(c, doc):
        marca(c, doc)
        if estado["copia"]:
            c.saveState()
            c.setFont("Helvetica-Bold", 9)
            c.drawRightString(letter[0] - MARGEN, letter[1] - 16, estado["copia"])
            c.restoreState()

    doc = BaseDocTemplate(buf, pagesize=letter, title=f"Pedido {pedido.get('numero_pedido', '')}",
                          author="Viriathus", leftMargin=MARGEN, rightMargin=MARGEN)
    marco = Frame(MARGEN, 74, ANCHO, letter[1] - 74 - 28, id="marco",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="pagina", frames=[marco], onPage=pie, onPageEnd=fin_hoja)])
    doc.build(story)
    return doc.page if contar else buf.getvalue()