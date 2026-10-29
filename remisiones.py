"""
Remisiones: documento para entregar piezas al cliente (sin precios).

  * Cliente, fecha y productos (por modelo: foto, nombre y descripción del inventario), cada uno con sus
    observaciones.
  * Se descarga en PDF con el logo de Viriathus grande arriba.
  * No se guarda nada en la base de datos ni cambia el inventario: es solo el documento.
"""
import datetime as dt
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

import streamlit as st

from permisos import roles as _roles

ROLES_REMISIONES = _roles("remisiones")
MAX_DESC = 700          # caracteres de descripción en el PDF (las muy largas se recortan)


# ----------------------------------------------------------------------
# Datos del inventario
# ----------------------------------------------------------------------
def _producto(sb, modelo):
    from ventas import _parse
    from cotizaciones import _url_foto_inventario, _foto_existe
    r = sb.table("productos").select("MODELO,datos_json").eq("MODELO", modelo).limit(1).execute()
    if not r.data:
        return None
    d = _parse(r.data[0].get("datos_json"))
    m = str(r.data[0]["MODELO"])
    url = _url_foto_inventario(sb, m)
    return {"id": uuid.uuid4().hex[:8], "modelo": m,
            "nombre": " ".join(str(d.get("NOMBRE", "") or "").split()) or "Sin nombre",
            "descripcion": str(d.get("DESCRIPCION", "") or "").strip(),
            "foto_url": url if _foto_existe(url) else None}


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def generar_pdf_remision(rem, items):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from pedido_pdf import ASSETS, DIRECCION, WEB, CORREO, WHATSAPP, INSTAGRAM, FACEBOOK, _imagen
    from cotizacion_pdf import _cargar_foto

    def est(n, **kw):
        base = dict(fontName="Times-Roman", fontSize=10, leading=12)
        base.update(kw)
        return ParagraphStyle(n, **base)

    ST_LBL = est("r_lbl", fontName="Times-Bold", fontSize=10.5)
    ST_VAL = est("r_val", fontSize=10.5)
    ST_TIT = est("r_tit", fontName="Times-Bold", fontSize=18, leading=22, alignment=TA_RIGHT)
    ST_TH = est("r_th", fontName="Times-Bold", fontSize=9.5, textColor=colors.white, alignment=TA_CENTER)
    ST_NUM = est("r_num", fontName="Times-Bold", fontSize=11, alignment=TA_CENTER)
    ST_NOM = est("r_nom", fontName="Times-Bold", fontSize=11, leading=13)
    ST_MOD = est("r_mod", fontSize=9, leading=11, textColor=colors.HexColor("#555555"))
    ST_DESC = est("r_desc", fontSize=9, leading=11)
    ST_NOF = est("r_nof", fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor("#888888"))
    ST_OBS = est("r_obs", fontSize=10, leading=13)
    MARGEN = 36
    ANCHO = letter[0] - 2 * MARGEN
    FOTO_W, FOTO_H = 96, 110
    NEGRO, BEIGE = colors.black, colors.HexColor("#C4BD97")

    def campo(etiqueta, valor, w_et, w_val):
        t = Table([[Paragraph(escape(etiqueta), ST_LBL), Paragraph(escape(str(valor or "")), ST_VAL)]],
                  colWidths=[w_et, w_val])
        t.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, NEGRO), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        return t

    with ThreadPoolExecutor(max_workers=6) as ex:
        fotos = list(ex.map(_cargar_foto, items))

    story = []
    logo = _imagen("logo_viriathus.png", ancho=300)          # logo grande
    if logo is not None:
        logo.hAlign = "CENTER"
        story += [logo, Spacer(1, 10)]
    fecha = rem.get("fecha")
    fecha_txt = fecha.strftime("%d/%m/%Y") if hasattr(fecha, "strftime") else str(fecha or "")
    izq = campo("Cliente:", rem.get("cliente"), 52, 270)
    der = Table([[Paragraph("REMISIÓN", ST_TIT)], [campo("Fecha:", fecha_txt, 40, 130)]], colWidths=[ANCHO - 340])
    der.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    cab = Table([[izq, "", der]], colWidths=[322, 18, ANCHO - 340])
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [cab, Spacer(1, 14)]

    # Tabla de productos: # | foto | modelo, nombre y descripción
    filas = [[Paragraph("#", ST_TH), Paragraph("FOTO", ST_TH), Paragraph("PRODUCTO", ST_TH)]]
    for i, it in enumerate(items):
        if fotos[i]:
            w, h = ImageReader(io.BytesIO(fotos[i])).getSize()
            e = min(FOTO_W / w, FOTO_H / h)
            celda_foto = Image(io.BytesIO(fotos[i]), width=w * e, height=h * e)
        else:
            celda_foto = Paragraph("Sin foto", ST_NOF)
        desc = it.get("descripcion") or ""
        if len(desc) > MAX_DESC:
            desc = desc[:MAX_DESC].rsplit(" ", 1)[0] + "…"
        texto = [Paragraph(escape(it.get("nombre") or ""), ST_NOM),
                 Paragraph(escape(f"Modelo {it.get('modelo')}"), ST_MOD), Spacer(1, 4)]
        if desc:
            texto.append(Paragraph(escape(desc).replace("\n", "<br/>"), ST_DESC))
        obs_it = (it.get("observaciones") or "").strip()
        if obs_it:
            texto += [Spacer(1, 5), Paragraph("<b>Observaciones:</b> " + escape(obs_it).replace("\n", "<br/>"), ST_OBS)]
        filas.append([Paragraph(str(i + 1), ST_NUM), celda_foto, texto])
    tabla = Table(filas, colWidths=[28, FOTO_W + 12, ANCHO - 28 - FOTO_W - 12], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NEGRO), ("GRID", (0, 0), (-1, -1), 0.5, NEGRO),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("VALIGN", (2, 1), (2, -1), "TOP"),
        ("ALIGN", (1, 1), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 1), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("LEFTPADDING", (2, 1), (2, -1), 8),
    ]))
    story.append(tabla)

    # Firma
    final = [Spacer(1, 30)]
    # Izquierda: Atentamente / firma / VIRIATHUS · Derecha: Recibió / firma / Nombre y firma
    ST_FIR = est("r_fir", fontName="Times-Bold", fontSize=11, alignment=TA_CENTER)
    ST_FIR2 = est("r_fir2", fontSize=10, alignment=TA_CENTER)
    W = 220
    hueco = ANCHO - 2 * W
    firma = Table([[Paragraph("Atentamente", ST_FIR), "", Paragraph("Recibió", ST_FIR)],
                   ["", "", ""],
                   [Paragraph("VIRIATHUS", ST_FIR2), "", Paragraph("Nombre y firma", ST_FIR2)]],
                  colWidths=[W, hueco, W], rowHeights=[None, 46, None])
    firma.setStyle(TableStyle([("LINEBELOW", (0, 1), (0, 1), 0.6, NEGRO), ("LINEBELOW", (2, 1), (2, 1), 0.6, NEGRO),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    final.append(firma)
    story.append(KeepTogether(final))

    def pie(c, doc):
        c.saveState()
        if doc.page > 1:
            c.setFont("Times-Roman", 8)
            c.drawRightString(letter[0] - MARGEN, letter[1] - 24, f"Remisión — {rem.get('cliente') or ''} — hoja {doc.page}")
        c.setFont("Times-Roman", 10)
        c.drawCentredString(letter[0] / 2, 58, DIRECCION)
        c.setFont("Times-Roman", 9.5)
        y = 36
        c.drawString(MARGEN, y, WEB)
        c.drawString(MARGEN + 95, y, CORREO)
        x = MARGEN + 235
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

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=letter, leftMargin=MARGEN, rightMargin=MARGEN, topMargin=30,
                          bottomMargin=78, title="Remisión", author="Viriathus")
    marco = Frame(MARGEN, 78, ANCHO, letter[1] - 30 - 78, leftPadding=0, rightPadding=0, topPadding=0,
                  bottomPadding=0, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[marco], onPage=pie)])
    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------
# Pantalla
# ----------------------------------------------------------------------
def _reset():
    ss = st.session_state
    ss["rm_ver"] = ss.get("rm_ver", 0) + 1
    ss["rm_items"] = []
    ss.pop("rm_pdf", None)


def tab_remisiones(sb, user):
    ss = st.session_state
    ss.setdefault("rm_ver", 0)
    ss.setdefault("rm_items", [])
    ver, items = ss["rm_ver"], ss["rm_items"]

    def k(n):
        return f"rm{ver}_{n}"

    st.caption("Documento de entrega sin precios. No se guarda ni cambia el inventario: solo se descarga el PDF.")
    c1, c2 = st.columns([3, 1])
    cliente = c1.text_input("CLIENTE *", key=k("cliente"))
    fecha = c2.date_input("FECHA", value=dt.date.today(), key=k("fecha"), format="DD/MM/YYYY")

    st.subheader("Productos")
    with st.form(f"rm_form_{ver}", clear_on_submit=True):
        m1, m2 = st.columns([4, 1])
        modelo_txt = m1.text_input("MODELO")
        m2.write("")
        m2.write("")
        agregar = m2.form_submit_button("Agregar", use_container_width=True)
    if agregar:
        m = modelo_txt.strip()
        if m.endswith(".0"):
            m = m[:-2]
        if not m:
            st.warning("Escribe un número de modelo.")
        elif any(i["modelo"] == m for i in items):
            st.warning(f"El modelo {m} ya está en la remisión.")
        else:
            try:
                p = _producto(sb, m)
            except Exception as ex:
                p = None
                st.error(f"Error consultando el inventario: {ex}")
            if p is None:
                st.error(f"El modelo {m} no existe en el inventario.")
            else:
                items.append(p)
                st.rerun()

    quitar = None
    for n, it in enumerate(items, 1):
        with st.container(border=True):
            top1, top2 = st.columns([6, 1])
            top1.markdown(f"**#{n} · Modelo {it['modelo']}**")
            if top2.button("Quitar", key=k(f"q_{it['id']}")):
                quitar = it["id"]
            a, b = st.columns([1, 5])
            subida = b.file_uploader(
                "Foto para esta remisión" + (" (opcional: ya hay una en el inventario)" if it.get("foto_url")
                                              else " — ⚠️ este modelo no tiene foto en el inventario"),
                type=["jpg", "jpeg", "png", "webp"], key=k(f"foto_{it['id']}"),
                help="Solo se usa en esta remisión; no cambia la foto del inventario.")
            if subida is not None:
                it["foto_bytes"] = subida.getvalue()
                it["_foto_id"] = f"{subida.name}:{subida.size}"
            else:
                it.pop("foto_bytes", None)
                it.pop("_foto_id", None)
            if it.get("foto_bytes"):
                a.image(it["foto_bytes"], width=90)
            elif it.get("foto_url"):
                a.image(it["foto_url"], width=90)
            else:
                a.warning("Sin foto")
            it["nombre"] = b.text_input("Nombre", value=it.get("nombre", ""), key=k(f"nom_{it['id']}"))
            it["descripcion"] = b.text_area("Descripción", value=it.get("descripcion", ""), key=k(f"desc_{it['id']}"),
                                            height=90)
            it["observaciones"] = b.text_input("Observaciones", value=it.get("observaciones", ""),
                                               key=k(f"obs_{it['id']}"), placeholder="Opcional")
    if quitar:
        ss["rm_items"] = [i for i in items if i["id"] != quitar]
        st.rerun()

    # El PDF se descarta si cambió algo
    firma = (cliente.strip(), str(fecha), tuple((i["modelo"], i.get("nombre", ""), i.get("descripcion", ""),
                                                  i.get("observaciones", ""), i.get("_foto_id", "")) for i in items))
    if ss.get("rm_pdf") and ss["rm_pdf"]["firma"] != firma:
        ss.pop("rm_pdf", None)
    b1, b2, b3 = st.columns([2, 2, 1])
    if b1.button("Generar PDF", type="primary", use_container_width=True, key=k("gen")):
        if not cliente.strip():
            st.error("Falta el nombre del cliente.")
        elif not items:
            st.error("Agrega al menos un producto.")
        else:
            with st.spinner("Armando el PDF (descarga las fotos)…"):
                try:
                    pdf = generar_pdf_remision({"cliente": cliente.strip(), "fecha": fecha}, items)
                    ss["rm_pdf"] = {"firma": firma, "pdf": pdf}
                except Exception as ex:
                    st.error(f"No se pudo generar el PDF: {ex}")
    if ss.get("rm_pdf"):
        nombre = "remision_" + "".join(ch for ch in cliente.strip().replace(" ", "_") if ch.isalnum() or ch == "_")[:40]
        b2.download_button("📥 Descargar PDF", ss["rm_pdf"]["pdf"], file_name=f"{nombre}_{fecha:%Y%m%d}.pdf",
                           mime="application/pdf", use_container_width=True, key=k("dl"))
    if b3.button("Nueva", use_container_width=True, key=k("nueva"), help="Empezar otra remisión desde cero"):
        _reset()
        st.rerun()
