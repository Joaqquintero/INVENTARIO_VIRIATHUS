"""
Listas con botones por renglón (Pedidos, Cotizaciones guardadas, Remisiones, Por facturar).

Las tablas de Streamlit (st.dataframe) no admiten botones adentro, así que la lista se arma renglón por renglón con
aspecto de tabla (bordes, renglones alternados) y una columna «Acciones» al principio:
  * PDF (rojo): arma el PDF y lo descarga en un solo toque. Si el navegador no deja la descarga automática, en ese
    mismo renglón queda «Bajar» (verde) para descargarlo.
  * Ver (azul): abre el detalle (lo decide cada pantalla con `al_ver`).
Los colores salen de las clases «st-key-…» que Streamlit pone a cada elemento con key (Streamlit 1.39+).
"""
import base64
import html
import json

import streamlit as st
import streamlit.components.v1 as components

ICONO_PDF = ":material/picture_as_pdf:"
ICONO_VER = ":material/visibility:"
ICONO_BAJAR = ":material/download:"
CLAVE_PDF = "_lista_pdf"          # el PDF recién armado (uno a la vez; se borra al cambiar de sección)

_CSS = """<style>
div[class*="st-key-tabla_"] { border: 1px solid rgba(196,189,151,.40); border-radius: 4px; gap: 0 !important;
  overflow: hidden; }
div[class*="st-key-cabeza_"] { background: #1E1D1C; padding: 7px 10px; border-bottom: 1px solid rgba(196,189,151,.40); }
div[class*="st-key-fila_"] { padding: 3px 10px; border-top: 1px solid rgba(196,189,151,.14); }
div[class*="st-key-fila_"][class*="_par_"] { background: rgba(255,255,255,.035); }
div[class*="st-key-fila_"]:hover { background: rgba(196,189,151,.08); }
/* Streamlit compensa el espacio de los párrafos con un margen negativo: aquí no hay espacio, así que tampoco margen
   (si no, el texto de dos líneas se sale del renglón y pisa el siguiente) */
div[class*="st-key-fila_"] [data-testid="stMarkdownContainer"],
div[class*="st-key-cabeza_"] [data-testid="stMarkdownContainer"],
div[class*="st-key-fila_"] .stMarkdown, div[class*="st-key-cabeza_"] .stMarkdown { margin: 0 !important; }
div[class*="st-key-cabeza_"] p { margin: 0 !important; font-weight: 700; font-size: .85rem; letter-spacing: .03em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Cada celda en una sola línea; lo largo se corta con «…» (el texto completo aparece al pasar el mouse) */
div[class*="st-key-fila_"] p { margin: 0 !important; font-size: .9rem; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
div[class*="st-key-fila_"] [data-testid="stColumn"] { min-width: 0; }
div[class*="st-key-fila_"] [data-testid="stHorizontalBlock"],
div[class*="st-key-cabeza_"] [data-testid="stHorizontalBlock"] { gap: .45rem; }
div[class*="st-key-cabeza_"] p:has(.acc-cab) { overflow: visible; }
div[class*="__acc"] button { min-height: 0 !important; height: 30px; width: 34px; padding: 0 !important;
  border: 0 !important; border-radius: 4px !important; color: #fff !important; display: flex;
  align-items: center; justify-content: center; }
div[class*="__acc"] button [data-testid="stIconMaterial"], div[class*="__acc"] button span { font-size: 18px !important; }
div[class*="__acc"] button p { font-size: .82rem !important; }
div[class*="__accpdf"] button { background: #c9534f !important; }
div[class*="__accpdf"] button:hover { background: #b0433f !important; }
div[class*="__accver"] button { background: #3d9cc0 !important; }
div[class*="__accver"] button:hover { background: #2f86a8 !important; }
div[class*="__accbajar"] button { background: #4f9d5b !important; }
</style>"""


def _css():
    st.markdown(_CSS, unsafe_allow_html=True)


def _caja(key):
    try:
        return st.container(key=key)
    except TypeError:                                    # Streamlit viejo: sin estilos, pero funciona
        return st.container()


def _columnas(pesos):
    try:
        return st.columns(pesos, gap="small", vertical_alignment="center")
    except TypeError:
        return st.columns(pesos)


def _descarga_automatica(nombre, datos):
    """Descarga el PDF sin otro clic (desde la página principal; si no se puede, desde el recuadro)."""
    b64 = base64.b64encode(datos).decode()
    components.html(f"""<script>
const nombre = {json.dumps(nombre)};
const bin = atob("{b64}"), arr = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
function bajar(w) {{
  const blob = new w.Blob([arr], {{type: "application/pdf"}});
  const a = w.document.createElement("a");
  a.href = w.URL.createObjectURL(blob); a.download = nombre;
  w.document.body.appendChild(a); a.click(); a.remove();
}}
try {{ bajar(window.parent); }} catch (e) {{ try {{ bajar(window); }} catch (e2) {{}} }}
</script>""", height=0)


_EMOJI = {ICONO_PDF: "📄", ICONO_VER: "👁"}


def _boton(destino, key, texto, icono, ayuda, tipo="secondary", **kw):
    try:
        return destino.button(texto, key=key, icon=icono, help=ayuda, type=tipo, **kw)
    except TypeError:
        return destino.button(texto or _EMOJI.get(icono, "·"), key=key, help=ayuda, **kw)


def lista(filas, columnas, clave, armar_pdf=None, al_ver=None, ayuda_pdf="Descargar PDF", ayuda_ver="Ver detalle",
          antes=None, titulo_antes="✔", por_pagina=50):
    """Dibuja la tabla.
    filas: dicts con los datos (cada uno con "_id" único). columnas: [(titulo, peso, funcion(fila) -> texto)].
    armar_pdf(fila) -> (bytes, nombre_archivo). al_ver(fila): callback (corre al tocar «Ver», antes de recargar).
    antes(fila, columna): opcional, una primera columna angosta (p. ej. una casilla), con título `titulo_antes`."""
    ss = st.session_state
    _css()
    pdf = ss.get(CLAVE_PDF)
    if pdf and pdf.get("error"):
        st.error(f"No se pudo armar el PDF: {pdf['error']}")
        ss.pop(CLAVE_PDF, None)
        pdf = None
    if pdf and pdf.get("nuevo") and pdf.get("lista") == clave:
        _descarga_automatica(pdf["nombre"], pdf["datos"])
        pdf["nuevo"] = False

    # Páginas: muchos renglones con botones hacen lenta la página
    total = len(filas)
    if total > por_pagina:
        paginas = (total - 1) // por_pagina + 1
        pag = min(max(int(ss.get(f"{clave}_pag", 0)), 0), paginas - 1)
        p1, p2, p3 = st.columns([1, 2, 1])
        if p1.button("← Anteriores", key=f"{clave}_ant", disabled=pag == 0, use_container_width=True):
            ss[f"{clave}_pag"] = pag - 1
            st.rerun()
        p2.caption(f"Página {pag + 1} de {paginas} · renglones {pag * por_pagina + 1} a "
                   f"{min((pag + 1) * por_pagina, total)} de {total}")
        if p3.button("Siguientes →", key=f"{clave}_sig", disabled=pag >= paginas - 1, use_container_width=True):
            ss[f"{clave}_pag"] = pag + 1
            st.rerun()
        filas = filas[pag * por_pagina:(pag + 1) * por_pagina]

    n_acc = int(armar_pdf is not None) + int(al_ver is not None)
    pesos = ([0.32] if antes else []) + [0.4] * n_acc + [c[1] for c in columnas]
    with _caja(f"tabla_{clave}"):
        with _caja(f"cabeza_{clave}"):
            cab = _columnas(pesos)
            i0 = 0
            if antes:
                cab[0].markdown(titulo_antes)
                i0 = 1
            if n_acc:
                cab[i0].markdown('<span class="acc-cab">Acciones</span>', unsafe_allow_html=True)
            for col, (titulo, _, _) in zip(cab[i0 + n_acc:], columnas):
                col.markdown(titulo)
        for i, fila in enumerate(filas):
            fid = fila.get("_id", i)
            k = f"{clave}_{fid}"
            with _caja(f"fila_{clave}_{'par' if i % 2 else 'non'}_{fid}"):
                cols = _columnas(pesos)
                j = 0
                if antes:
                    antes(fila, cols[0])
                    j = 1
                if armar_pdf is not None:
                    if pdf and pdf.get("clave") == k:
                        try:
                            cols[j].download_button("", pdf["datos"], file_name=pdf["nombre"],
                                                    mime="application/pdf", key=f"{k}__accbajar", icon=ICONO_BAJAR,
                                                    help="El PDF está listo: tócalo si no se descargó solo.",
                                                    on_click="ignore")
                        except TypeError:
                            cols[j].download_button("⬇", pdf["datos"], file_name=pdf["nombre"],
                                                    mime="application/pdf", key=f"{k}__accbajar")
                    elif _boton(cols[j], f"{k}__accpdf", "", ICONO_PDF, ayuda_pdf):
                        with st.spinner("Armando el PDF…"):
                            try:
                                datos, nombre = armar_pdf(fila)
                                ss[CLAVE_PDF] = {"clave": k, "lista": clave, "datos": datos, "nombre": nombre,
                                                 "nuevo": True}
                            except Exception as ex:
                                ss[CLAVE_PDF] = {"error": str(ex)}
                        st.rerun()
                    j += 1
                if al_ver is not None:
                    _boton(cols[j], f"{k}__accver", "", ICONO_VER, ayuda_ver, on_click=al_ver, args=(fila,))
                    j += 1
                for col, (_, _, valor) in zip(cols[j:], columnas):
                    texto = str(valor(fila) or "—")
                    titulo = html.escape(texto.replace("**", ""), quote=True)
                    col.markdown(f'<span title="{titulo}">{_seguro(html.escape(texto, quote=False))}</span>',
                                 unsafe_allow_html=True)


def _seguro(texto):
    """Sin esto, el texto entre dos «$» se dibuja como fórmula matemática (números verdes y chiquitos)."""
    return str(texto).replace("$", "\\$")


def dinero(n):
    try:
        return f"${float(n or 0):,.2f}"
    except (TypeError, ValueError):
        return "—"
