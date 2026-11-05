"""
Comprobantes de pago de los pedidos (ticket de la terminal, transferencia…): uno por cobro, opcional.

  * Se guardan en Supabase Storage (bucket «imagenes», carpeta comprobantes/<pedido>/) y la ruta en
    pedido_pagos.comprobante. Se ven bajando el archivo (no hace falta que el bucket sea público).
  * Para que ocupen lo menos posible:
      - Foto / captura: se recorta el fondo oscuro (la mesa), se pasa a blanco y negro, se reduce a ~900 px de
        ancho y se guarda como JPEG comprimido → normalmente 20-60 KB (la foto del celular pesa 2-4 MB).
      - PDF del banco: se guarda tal cual si pesa hasta 400 KB (sigue siendo el documento original).
  * Cualquiera puede adjuntar, ver, cambiar o quitar (queda en el registro de cambios).

SQL (una vez):
    alter table pedido_pagos add column if not exists comprobante text;
"""
import io
import uuid

import streamlit as st

import codigos
from navegacion import descargar

BUCKET = "imagenes"
MAX_PDF = 400 * 1024
ANCHO = 900
SQL = "alter table pedido_pagos add column if not exists comprobante text;"
TIPOS = ["jpg", "jpeg", "png", "webp", "pdf"]


def _recortar_fondo(img):
    """Recorta el fondo (la mesa, los dedos) alrededor del papel cuando se fotografía el ticket: se queda con las
    columnas y renglones donde predomina lo blanco. Si no queda claro dónde está el papel, no toca nada."""
    from PIL import ImageFilter
    chica = img.copy()
    chica.thumbnail((300, 300))
    chica = chica.filter(ImageFilter.MedianFilter(5))
    w, h = chica.size
    px = chica.load()
    blanco = [[px[x, y] > 190 for x in range(w)] for y in range(h)]
    cols = [x for x in range(w) if sum(blanco[y][x] for y in range(h)) > 0.3 * h]
    if not cols:
        return img
    x0, x1 = cols[0], cols[-1]
    ancho = x1 - x0 + 1
    filas = [y for y in range(h) if sum(blanco[y][x0:x1 + 1]) > 0.5 * ancho]
    if not filas:
        return img
    y0, y1 = filas[0], filas[-1]
    area = ancho * (y1 - y0 + 1)
    if area < 0.10 * w * h or area > 0.90 * w * h:       # papel muy chico (dudoso) o ya ocupa toda la foto
        return img
    fx, fy = img.size[0] / w, img.size[1] / h
    m = 3
    return img.crop((max(0, int((x0 - m) * fx)), max(0, int((y0 - m) * fy)),
                     min(img.size[0], int((x1 + 1 + m) * fx)), min(img.size[1], int((y1 + 1 + m) * fy))))


def comprimir(datos):
    """(bytes, extensión, content-type) listos para guardar. Lanza ValueError si no se puede usar."""
    if datos[:5] == b"%PDF-":
        if len(datos) > MAX_PDF:
            raise ValueError(f"El PDF pesa {len(datos) / 1024:,.0f} KB (máximo {MAX_PDF // 1024} KB). "
                             "Sube mejor una captura de pantalla del comprobante.")
        return datos, "pdf", "application/pdf"
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(datos))
        img = ImageOps.exif_transpose(img)
    except Exception:
        raise ValueError("No se pudo leer el archivo. Sube una foto (JPG / PNG) o el PDF del banco.")
    if img.mode in ("RGBA", "LA", "P"):
        fondo = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        fondo.paste(rgba, mask=rgba.split()[3])
        img = fondo
    img = img.convert("L")
    img = _recortar_fondo(img)
    if img.size[0] > ANCHO:
        img = img.resize((ANCHO, max(1, round(img.size[1] * ANCHO / img.size[0]))), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=55, optimize=True, progressive=True)
    return out.getvalue(), "jpg", "image/jpeg"


def guardar(sb, pago, datos, usuario):
    """Comprime, sube y anota el comprobante del cobro (reemplaza el anterior si había)."""
    contenido, ext, tipo = comprimir(datos)
    ruta = f"comprobantes/{pago['numero_pedido']}/{pago['id']}_{uuid.uuid4().hex[:8]}.{ext}"
    sb.storage.from_(BUCKET).upload(path=ruta, file=contenido, file_options={"content-type": tipo})
    try:
        sb.table("pedido_pagos").update({"comprobante": ruta}).eq("id", pago["id"]).execute()
    except Exception:
        _borrar_archivo(sb, ruta)
        raise
    if pago.get("comprobante"):
        _borrar_archivo(sb, pago["comprobante"])
    codigos.registrar(sb, usuario, "Adjuntó comprobante", "pedido", pago["numero_pedido"],
                      f"{pago.get('forma_pago')} · ${float(pago.get('monto') or 0):,.2f} · {len(contenido) / 1024:,.0f} KB")
    return len(contenido)


def quitar(sb, pago, usuario):
    sb.table("pedido_pagos").update({"comprobante": None}).eq("id", pago["id"]).execute()
    _borrar_archivo(sb, pago.get("comprobante"))
    codigos.registrar(sb, usuario, "Quitó comprobante", "pedido", pago["numero_pedido"],
                      f"{pago.get('forma_pago')} · ${float(pago.get('monto') or 0):,.2f}")


def _borrar_archivo(sb, ruta):
    if ruta:
        try:
            sb.storage.from_(BUCKET).remove([ruta])
        except Exception:
            pass


@st.cache_data(ttl=600, max_entries=20, show_spinner=False)
def _bajar(_sb, ruta):
    return _sb.storage.from_(BUCKET).download(ruta)


def _fecha(v):
    s = str(v or "")[:10]
    return f"{s[8:10]}/{s[5:7]}/{s[:4]}" if len(s) == 10 else "sin fecha"


def render(sb, user, pagos):
    """Debajo de la tabla de cobros del pedido: un renglón por cobro con 📎 Adjuntar / 🧾 Ver."""
    if not pagos:
        return
    if not all("comprobante" in p for p in pagos):
        st.caption("Para adjuntar comprobantes falta correr en Supabase (una vez): `" + SQL + "`")
        return
    quien = user.get("nombre") or user.get("role") or "?"
    st.markdown("**Comprobantes de pago**")
    for p in sorted(pagos, key=lambda x: (str(x.get("fecha_pago") or ""), x["id"])):
        c1, c2 = st.columns([5, 2])
        tiene = bool(p.get("comprobante"))
        c1.markdown(f"{'🧾' if tiene else '▫️'} {p.get('forma_pago') or '—'} · {p.get('banco_caja') or '—'} · "
                    f"**${float(p.get('monto') or 0):,.2f}** · {_fecha(p.get('fecha_pago'))}")
        with c2.popover("🧾 Ver" if tiene else "📎 Adjuntar", use_container_width=True):
            if tiene:
                try:
                    datos = _bajar(sb, p["comprobante"])
                    if p["comprobante"].endswith(".pdf"):
                        st.caption("Comprobante en PDF")
                    else:
                        st.image(datos, use_container_width=True)
                    descargar(st, "Descargar", datos, file_name=p["comprobante"].rsplit("/", 1)[-1],
                              key=f"cmp_dl_{p['id']}")
                except Exception as ex:
                    st.warning(f"No se pudo abrir el comprobante: {ex}")
                st.divider()
            arch = st.file_uploader("Cambiar comprobante" if tiene else "Foto del ticket / captura / PDF del banco",
                                    type=TIPOS, key=f"cmp_up_{p['id']}")
            st.caption("Se achica solo al guardarlo (blanco y negro). Fotografía el frente del ticket, donde está "
                       "el monto.")
            b1, b2 = st.columns(2)
            if b1.button("Guardar", type="primary", key=f"cmp_ok_{p['id']}", disabled=arch is None):
                try:
                    kb = guardar(sb, p, arch.getvalue(), quien) / 1024
                except ValueError as ex:
                    st.error(str(ex))
                except Exception as ex:
                    st.error(f"No se pudo guardar: {ex}")
                else:
                    st.session_state.pop(f"cmp_up_{p['id']}", None)
                    st.toast(f"Comprobante guardado ({kb:,.0f} KB).")
                    st.rerun()
            if tiene and b2.button("Quitar", key=f"cmp_del_{p['id']}"):
                try:
                    quitar(sb, p, quien)
                except Exception as ex:
                    st.error(f"No se pudo quitar: {ex}")
                else:
                    st.toast("Comprobante quitado.")
                    st.rerun()
