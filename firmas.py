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

Firma A DISTANCIA (solo pedidos): un link /f/<token> de la app (viriathusapp.vercel.app) que vence a los 3 días.
El cliente ve el pedido (productos, precios, descuentos, pagos y saldo), firma con el dedo y la firma cae en esta
misma tabla. Lo que vio y aceptó queda en `firma_enlaces` (correr una vez en Supabase):
    create table if not exists firma_enlaces (
      id bigserial primary key,
      token text not null unique,
      tipo text not null default 'pedido',
      referencia text not null,
      creado timestamptz not null default now(),
      vence timestamptz not null,
      creado_por text,
      total numeric,
      firmado timestamptz,
      ip text, agente text,
      total_firmado numeric, saldo_firmado numeric,
      detalle jsonb
    );
"""
import base64
import datetime as dt
import io
import os
import secrets
import urllib.parse

import streamlit as st
import streamlit.components.v1 as components

import codigos

TABLA = "firmas"
ENLACES = "firma_enlaces"
DIAS_ENLACE = 3
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


# ----------------------------------------------------------------------
# Firma a distancia (link para el cliente)
# ----------------------------------------------------------------------
def _ahora():
    return dt.datetime.now(dt.timezone.utc)


def _base_url():
    try:
        return str(st.secrets.get("FICHA_PUBLICA_URL", "https://viriathusapp.vercel.app")).strip().rstrip("/")
    except Exception:
        return "https://viriathusapp.vercel.app"


def _hora_mx(v):
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t.astimezone(dt.timezone(dt.timedelta(hours=-6))).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""


def enlace_vigente(sb, numero):
    r = (sb.table(ENLACES).select("*").eq("tipo", "pedido").eq("referencia", str(numero)).is_("firmado", "null")
         .gt("vence", _ahora().isoformat()).order("id", desc=True).limit(1).execute().data)
    return r[0] if r else None


def enlace_firmado(sb, numero):
    try:
        r = (sb.table(ENLACES).select("*").eq("tipo", "pedido").eq("referencia", str(numero))
             .not_.is_("firmado", "null").order("id", desc=True).limit(1).execute().data)
        return r[0] if r else None
    except Exception:
        return None


def crear_enlace(sb, numero, usuario, total):
    ya = enlace_vigente(sb, numero)
    if ya:
        return ya
    fila = {"token": secrets.token_urlsafe(12), "tipo": "pedido", "referencia": str(numero),
            "vence": (_ahora() + dt.timedelta(days=DIAS_ENLACE)).isoformat(), "creado_por": usuario, "total": total}
    r = sb.table(ENLACES).insert(fila).execute().data
    codigos.registrar(sb, usuario, "Link para firmar a distancia", "pedido", numero, f"vence {fila['vence'][:16]}")
    return r[0] if r else fila


def _pedido(sb, numero):
    try:
        r = sb.table("pedidos").select("cliente,telefono,total").eq("numero_pedido", numero).limit(1).execute().data
        return r[0] if r else {}
    except Exception:
        return {}


def _distancia(sb, numero):
    """Dentro del expander de la firma del pedido: crear / mostrar el link para firmar a distancia."""
    st.markdown("**📲 Firmar a distancia** — el cliente revisa el pedido (precios, descuentos y pagos) y lo firma "
                f"desde su teléfono. El link vale {DIAS_ENLACE} días.")
    try:
        e = enlace_vigente(sb, numero)
    except Exception:
        st.caption("Falta activar los links de firma: corre en Supabase el SQL de `firma_enlaces` (está en firmas.py).")
        return
    if not e:
        if st.button("Crear link para el cliente", key=f"fz_link_{numero}"):
            u = st.session_state.get("user_info") or {}
            try:
                crear_enlace(sb, numero, u.get("nombre") or st.session_state.get("login_usuario") or "página",
                             _pedido(sb, numero).get("total"))
            except Exception as ex:
                st.error(f"No se pudo crear el link: {ex}")
                return
            st.rerun()
        return
    url = f"{_base_url()}/f/{e['token']}"
    st.code(url, language=None)
    p = _pedido(sb, numero)
    nombre = str(p.get("cliente") or "").strip().split(" ")[0]
    msg = (f"Hola{' ' + nombre if nombre else ''}, este es tu pedido N° {numero} de Viriathus. Por favor revísalo y "
           f"fírmalo aquí (el link vale hasta el {_hora_mx(e['vence'])}):\n{url}")
    try:
        from ventas import _telefono_whatsapp
        tel = _telefono_whatsapp(p.get("telefono"))
    except Exception:
        tel = None
    destino = f"https://wa.me/{tel}" if tel else "https://wa.me/"
    st.markdown(f"[💬 Mandar por WhatsApp{' al cliente' if tel else ''}]({destino}?text={urllib.parse.quote(msg)}) · "
                f"vence el {_hora_mx(e['vence'])}")


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
            if tipo == "pedido" and str(firma.get("vendedor") or "").startswith("A distancia"):
                e = enlace_firmado(sb, referencia)
                if e:
                    st.caption(f"Firmó desde el link: total aceptado ${float(e.get('total_firmado') or 0):,.2f}, "
                               f"saldo ${float(e.get('saldo_firmado') or 0):,.2f} · IP {e.get('ip') or '—'}.")
                    actual = _pedido(sb, referencia).get("total")
                    try:
                        if actual is not None and abs(float(actual) - float(e.get("total_firmado") or 0)) > 0.5:
                            st.warning(f"⚠️ El total del pedido cambió después de la firma (firmó por "
                                       f"${float(e['total_firmado']):,.2f}, ahora es ${float(actual):,.2f}). "
                                       "Si hace falta, borra la firma y mándale un link nuevo.")
                    except (TypeError, ValueError):
                        pass
            if st.checkbox("Borrar la firma para volver a firmar", key=f"{clave}_borrar_ok"):
                q_v, q_c = codigos.pedir_identidad(sb, f"{clave}_idb")
                if st.button("Borrar firma", key=f"{clave}_borrar") and (quien := codigos.confirmar(sb, q_v, q_c)):
                    borrar(sb, tipo, referencia, quien)
                    ss.pop(f"{clave}_borrar_ok", None)
                    st.rerun()
            return firma
        if tipo == "pedido":
            _distancia(sb, referencia)
            st.divider()
            st.markdown("**✍️ Firmar aquí en la tienda**")
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
