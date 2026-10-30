"""
Firma del cliente en pedidos y remisiones, dibujada con el dedo en el teléfono del frente.

  * Opcional: no todos los pedidos ni remisiones la llevan.
  * Se firma una vez; los cambios de estado después no la tocan. Se puede borrar para volver a firmar.
  * La imagen se guarda en la tabla `firmas` (no en el almacenamiento público de fotos), recortada y chica.
  * Sale en el PDF: pedido → recuadro «Firma y nombre del Cliente»; remisión → «Recibió» (en cada hoja).

Tabla (correr una vez en Supabase):
    create table if not exists firmas (
      id bigserial primary key,
      creado timestamptz not null default now(),
      tipo text not null,          -- 'pedido' | 'remision'
      referencia text not null,    -- N° de pedido / id de la remisión
      nombre text, imagen text not null, vendedor text,
      unique (tipo, referencia)
    );
"""
import base64
import datetime as dt
import io
import os

import streamlit as st
import streamlit.components.v1 as components

import codigos

TABLA = "firmas"
_componente = components.declare_component(
    "firma_lienzo", path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "firma_componente"))


def leer(sb, tipo, referencia):
    try:
        r = (sb.table(TABLA).select("*").eq("tipo", tipo).eq("referencia", str(referencia))
             .limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        return None


def imagen(firma):
    try:
        return base64.b64decode(firma["imagen"])
    except Exception:
        return None


def fecha_texto(firma):
    try:
        t = dt.datetime.fromisoformat(str(firma.get("creado")).replace("Z", "+00:00"))
        return t.astimezone(dt.timezone(dt.timedelta(hours=-6))).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""


def para_pdf(firma):
    """{"png", "nombre", "fecha"} para estampar en un PDF, o None."""
    if not firma:
        return None
    png = imagen(firma)
    return {"png": png, "nombre": firma.get("nombre") or "", "fecha": fecha_texto(firma), "id": firma.get("id")} \
        if png else None


def _limpiar_imagen(data_url):
    """dataURL del lienzo → PNG recortado al trazo, de máximo 600×220 px (unos KB)."""
    from PIL import Image
    crudo = base64.b64decode(data_url.split(",", 1)[1])
    im = Image.open(io.BytesIO(crudo)).convert("RGBA")
    caja = im.getchannel("A").getbbox()
    if not caja:
        raise ValueError("La firma está vacía.")
    im = im.crop((max(caja[0] - 8, 0), max(caja[1] - 8, 0), min(caja[2] + 8, im.width), min(caja[3] + 8, im.height)))
    im.thumbnail((600, 220))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def guardar(sb, tipo, referencia, nombre, data_url, vendedor):
    if not (nombre or "").strip():
        raise ValueError("Escribe el nombre de quien firma.")
    png = _limpiar_imagen(data_url)
    sb.table(TABLA).delete().eq("tipo", tipo).eq("referencia", str(referencia)).execute()
    sb.table(TABLA).insert({"tipo": tipo, "referencia": str(referencia), "nombre": nombre.strip(),
                            "imagen": base64.b64encode(png).decode(), "vendedor": vendedor}).execute()
    codigos.registrar(sb, vendedor, "Firma del cliente", tipo, referencia, nombre.strip())


def borrar(sb, tipo, referencia, usuario):
    sb.table(TABLA).delete().eq("tipo", tipo).eq("referencia", str(referencia)).execute()
    codigos.registrar(sb, usuario, "Borró firma del cliente", tipo, referencia, "")


def widget(sb, tipo, referencia, titulo="✍️ Firma del cliente"):
    """Expander para firmar (o ver / borrar la firma). Devuelve la firma guardada (dict) o None."""
    ss = st.session_state
    clave = f"fz_{tipo}_{referencia}"
    firma = leer(sb, tipo, referencia)
    with st.expander(titulo + (" · ✅ firmado" if firma else ""), expanded=False):
        if firma:
            png = imagen(firma)
            if png:
                st.image(png, width=260)
            st.caption(f"Firmó **{firma.get('nombre') or '—'}** el {fecha_texto(firma)} · capturó "
                       f"{firma.get('vendedor') or '—'}. La firma sale en el PDF.")
            if st.checkbox("Borrar la firma para volver a firmar", key=f"{clave}_borrar_ok"):
                q_v, q_c = codigos.pedir_identidad(sb, f"{clave}_idb")
                if st.button("Borrar firma", key=f"{clave}_borrar") and (quien := codigos.confirmar(sb, q_v, q_c)):
                    borrar(sb, tipo, referencia, quien)
                    ss.pop(f"{clave}_borrar_ok", None)
                    st.rerun()
            return firma
        st.caption("Opcional. Entrega el teléfono al cliente: firma con el dedo, toca «Listo» y luego tú guardas.")
        nombre = st.text_input("Nombre de quien firma *", key=f"{clave}_nombre")
        v = ss.get(f"{clave}_v", 0)
        trazo = _componente(key=f"{clave}_lienzo_{v}", default=None)
        q_v, q_c = codigos.pedir_identidad(sb, f"{clave}_id")
        if st.button("Guardar firma", type="primary", key=f"{clave}_guardar",
                     disabled=not (trazo and (nombre or "").strip()),
                     help=None if trazo else "Primero el cliente firma y toca «Listo».") \
                and (quien := codigos.confirmar(sb, q_v, q_c)):
            try:
                guardar(sb, tipo, referencia, nombre, trazo, quien)
            except Exception as ex:
                st.error(f"No se pudo guardar la firma: {ex}. ¿Ya corriste en Supabase el SQL de firmas?")
                return None
            ss[f"{clave}_v"] = v + 1               # lienzo nuevo (vacío) por si se borra después
            ss.pop(f"{clave}_nombre", None)
            st.rerun()
    return None
