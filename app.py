import streamlit as st
# --- Bitácora de memoria: lo que pasa ENTRE clics (Streamlit, fuera de este código) y en cada parte de la página ---
import bitacora as _bita
_bita_ini = _bita.rss_mb()
_bita_prev = st.session_state.get("_bita_fin_run")
st.session_state["_bita_fin_run"] = None
if _bita_prev is not None and _bita_ini is not None:
    _bita.anotar("Entre clics (Streamlit, fuera del código)", _bita_prev, _bita_ini)
from supabase import create_client
import json
import subprocess
import sys
import os
import re
import math
import hmac
from shopify_borrador import crear_borrador_shopify, actualizar_producto_shopify, marcar_disponibilidad_shopify
# Ventas en desarrollo: poner en True para mostrar la pestaña «Ventas» (y cargar ventas.py)
MOSTRAR_VENTAS = True
if MOSTRAR_VENTAS:
    from ventas import render_ventas
from shopify_descripciones import render_traer_descripciones
from shopify_ventas import sincronizar_en_segundo_plano, aviso_pendiente, render_ventas_shopify
from shopify_lista import render_lista_shopify
from etiquetas import render_etiquetas
from permisos import puede
from shopify_productos import (sincronizar_productos_en_segundo_plano, conflictos_pendientes, render_sync_productos,
                               registrar_envio, sincronizar_ficha)
import time
from postgrest.exceptions import APIError


# Configuración de la página
_DIR_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_app")
try:
   from PIL import Image as _PILImage
   # Ícono chico (128 px): el original de 2200×1700 ocupaba 14 MB en memoria EN CADA CLIC
   _icono = _PILImage.open(os.path.join(_DIR_ASSETS, "favicon_128.png"))
except Exception:
   _icono = None
st.set_page_config(
   page_title="VIRIATHUS - INVENTARIO",
   page_icon=_icono,
   layout="wide"
)

# Estilo Viriathus (tema oscuro): Times New Roman, fondo gris carbón #272625, acentos beige #C4BD97, esquinas casi rectas y títulos en mayúsculas.
# (Los colores base están en .streamlit/config.toml; aquí van solo los detalles que el tema no cubre.)
st.markdown("""
<style>
.stApp *:not([data-testid="stIconMaterial"]):not(.material-icons):not([class*="material-symbols"]):not(svg):not(path) {
    font-family: "Times New Roman", Times, serif;
}
h1, h2 { text-transform: uppercase; letter-spacing: .08em; font-weight: 400 !important; }
h3, h4 { font-weight: 500 !important; }
.stButton > button, .stDownloadButton > button, .stLinkButton > a, .stFormSubmitButton > button {
    border-radius: 2px; border: 1px solid #C4BD97; letter-spacing: .03em;
}
.stTextInput input, .stNumberInput input, .stTextArea textarea, [data-baseweb="select"] > div { border-radius: 2px; }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 2px; }
hr { border-color: #C4BD97 !important; }
[data-testid="stSidebar"] { border-right: 1px solid #C4BD97; }
</style>
""", unsafe_allow_html=True)


# ID principal de la carpeta 'master fotos' en Google Drive
ID_MASTER_FOTOS = "1E6tlqi-W95qupX2Psi6XtgoHPpJo1ZEv"


# Abre tu proyecto en ChatGPT y copia la URL de la barra del navegador
PROYECTO_CHATGPT_URL = "https://chatgpt.com/g/g-p-6acd222151288191b9b5d8deb97b6941/project"


PROMPTS_IA = {
   "Descripción": 'Redacta la DESCRIPCIÓN / FICHA TÉCNICA del producto "{nombre}".',
   "Cédula curatorial": 'Redacta la CÉDULA CURATORIAL del producto "{nombre}".',
}


# Cargar credenciales de Supabase
url = st.secrets["SUPABASE_URL"]
key = st.secrets["SUPABASE_KEY"]


@st.cache_resource
def init_supabase():
   return create_client(url, key)


supabase = init_supabase()


from caches import guardar_en_memoria as _guardar_en_memoria, limpiar as _limpiar_memoria


@_guardar_en_memoria(ttl=300)
def _modelos_numericos():
   """Lee SOLO la columna MODELO (rápido) y devuelve:
   - "puros": los modelos que son solo dígitos, de mayor a menor como NÚMERO (el MODELO es texto)
   - "maximo": el número de modelo más alto (también cuenta los «123.0»)
   Si la lectura falla, lanza el error: así no se guarda un resultado incompleto."""
   puros, maximo, offset = [], 0, 0
   while True:
       filas = (supabase.table("productos").select("MODELO").order("MODELO")
                .range(offset, offset + 999).execute().data)
       for f in filas:
           m = str(f.get("MODELO", "") or "").strip()
           if m.isdigit():
               puros.append((int(m), m))
           base = m.split(".")[0]
           if base.isdigit():
               maximo = max(maximo, int(base))
       if len(filas) < 1000:
           break
       offset += 1000
   puros.sort(reverse=True)
   return {"puros": [m for _, m in puros], "maximo": maximo}


def ultimos_productos(desde, n):
   """Productos más recientes primero (número de modelo más alto), paginados: devuelve (lista, total)."""
   modelos = _modelos_numericos()["puros"]
   pagina = modelos[desde:desde + n]
   if not pagina:
       return [], len(modelos)
   resp = supabase.table("productos").select("*").in_("MODELO", pagina).execute()
   orden = {m: i for i, m in enumerate(pagina)}
   datos = sorted(resp.data or [], key=lambda r: orden.get(str(r.get("MODELO", "")).strip(), len(orden)))
   return datos, len(modelos)



# --- AUTENTICACIÓN Y ROLES ---
# Los usuarios y contraseñas ya no están en el código: viven en los secretos de Streamlit
# (.streamlit/secrets.toml en tu computadora; Settings > Secrets en Streamlit Cloud), sección [usuarios.<nombre>].
def _cargar_usuarios():
   try:
       return {str(u): {"password": str(d["password"]), "role": str(d["role"]), "nombre": str(d["nombre"])}
               for u, d in st.secrets["usuarios"].items()}
   except Exception:
       return {}


USUARIOS = _cargar_usuarios()
import sesion as _sesion
import importlib.util as _ilu
_HAY_CAUDAL = _ilu.find_spec("caudal") is not None      # Caudal se activa solo si está subido caudal.py


def login():
   col1, col2, col3 = st.columns([1, 2, 1])
   with col2:
       _l1, _l2, _l3 = st.columns([1, 1, 1])
       _l2.image(os.path.join(_DIR_ASSETS, "logo_escudo.png"), use_container_width=True)
       if st.session_state.pop("_borrar_llave", False):
           _sesion.olvidar()                # se cerró sesión: este navegador ya no entra solo
       username = st.text_input("Usuario", key="login_user")
       password = st.text_input("Contraseña", type="password", key="login_pass")
       mantener = st.checkbox("Mantener la sesión iniciada en este equipo", value=True, key="login_mantener",
                              help="No se cierra al suspender la compu o el celular ni con el tiempo; solo con «Cerrar Sesión».")
       btn_login = st.button("Iniciar Sesión", use_container_width=True, key="login_btn")
      
       if btn_login:
           if not USUARIOS:
               st.error("No hay usuarios configurados. Agrega la sección [usuarios] en los secretos de la app.")
           elif username in USUARIOS and hmac.compare_digest(USUARIOS[username]["password"].encode(), password.encode()):
               st.session_state["logged_in"] = True
               st.session_state["user_info"] = USUARIOS[username]
               st.session_state["login_usuario"] = username
               st.session_state.pop("_sesion_cerrada", None)
               st.session_state["_recordar"] = bool(mantener)   # la llave se deja en el navegador al entrar
               st.success(f"Bienvenido, {USUARIOS[username]['nombre']}")
               st.rerun()
           else:
               st.error("Usuario o contraseña incorrectos")


_sesion.entrar_solo(USUARIOS)          # llave guardada en el navegador → entra sin pedir contraseña
if "logged_in" not in st.session_state or not st.session_state["logged_in"]:
   login()
   st.stop()


user = st.session_state["user_info"]


# --- FUNCIONES AUXILIARES DE CÁLCULO Y LIMPIEZA V2 ---
def hay(v):
   """True si el usuario escribió algo en el campo."""
   return str(v).strip() != ""


def ok(*vals):
   """True si todos los valores ya están calculados (no son None)."""
   return all(v is not None for v in vals)


def mostrar_calculado(label, valor, key, formula=None):
   """Campo de solo lectura que se actualiza solo. Vacío si aún no hay datos."""
   txt = f"{valor:,.2f}" if valor is not None else ""
   # La key incluye el valor: si cambia, Streamlit redibuja el campo con el nuevo número
   st.text_input(label, value=txt, disabled=True,
                 placeholder="— pendiente —", key=f"{key}_{txt}")
   if formula:
       st.caption(f"*{formula}*")


def num(v):
   """Para guardar: None -> 0.0"""
   return float(v) if v is not None else 0.0


# --- FUNCIONES AUXILIARES DE CÁLCULO Y LIMPIEZA ---
def to_float(val, default=0.0):
   if val is None or val == "":
       return default
   if isinstance(val, (int, float)):
       return float(val)
   val_clean = re.sub(r"[^\d.-]", "", str(val).strip())
   try:
       return float(val_clean)
   except:
       return default


def aplicar_regla_redondeo(valor):
   if valor <= 0:
       return 0.0
   if valor < 500:
       return float(math.ceil(valor))
   elif valor <= 10000:
       return float(math.ceil(valor / 50.0) * 50.0)
   else:
       return float(math.ceil(valor / 100.0) * 100.0)


@st.cache_data(ttl=60, show_spinner=False)
def obtener_siguiente_modelo():
   """Siguiente número de modelo libre. Si no se puede leer la base, devuelve "" (no sugiere uno equivocado)."""
   try:
       maximo = _modelos_numericos()["maximo"]
   except Exception:
       return ""
   return str(maximo + 1) if maximo else "1"


def ejecutar_script_python(nombre_script):
   try:
       resultado = subprocess.run([sys.executable, nombre_script], capture_output=True, text=True)
       if resultado.returncode == 0:
           st.success(f"Se ejecutó '{nombre_script}' con éxito.")
           if resultado.stdout:
               st.code(resultado.stdout)
       else:
           st.error(f"Error al ejecutar '{nombre_script}':")
           st.code(resultado.stderr)
   except Exception as e:
       st.error(f"Error al intentar llamar a {nombre_script}: {e}")


# --- FORMULARIO PRINCIPAL ESTRUCTURADO ---


def _sesiones_abiertas():
   """Cuántas pestañas/computadoras tienen la app abierta ahora (si Streamlit lo permite saber)."""
   try:
       from streamlit.runtime import get_instance
       return len(get_instance()._session_mgr.list_active_sessions())
   except Exception:
       return None


def comprimir_foto(datos, lado_max=1600, calidad=85):
   """Deja la foto liviana antes de subirla: gira según la orientación del celular, la reduce a máximo
   `lado_max` px por lado y la guarda como JPEG de buena calidad (convierte PNG/WEBP de verdad a JPG).
   Devuelve (bytes_jpeg, kb_antes, kb_despues)."""
   import io
   from PIL import Image, ImageOps
   im = Image.open(io.BytesIO(datos))
   im = ImageOps.exif_transpose(im)
   if im.mode in ("RGBA", "LA", "P"):
       im = im.convert("RGBA")
       fondo = Image.new("RGB", im.size, (255, 255, 255))     # lo transparente queda blanco
       fondo.paste(im, mask=im.split()[-1])
       im = fondo
   else:
       im = im.convert("RGB")
   im.thumbnail((lado_max, lado_max), Image.LANCZOS)
   out = io.BytesIO()
   im.save(out, "JPEG", quality=calidad, optimize=True, progressive=True)
   nuevo = out.getvalue()
   if len(nuevo) >= len(datos) and Image.open(io.BytesIO(datos)).format == "JPEG" and max(Image.open(io.BytesIO(datos)).size) <= lado_max:
       nuevo = datos            # ya venía liviana: se deja como estaba
   return nuevo, round(len(datos) / 1024), round(len(nuevo) / 1024)


def guardar_producto_db(modelo_key, datos_dict):
   """Guarda o actualiza el producto correctamente en Supabase."""
   # Obtener la URL de Supabase de manera segura desde secrets
   sb_url = st.secrets.get("SUPABASE_URL", "")
   url_img_default = f"{sb_url}/storage/v1/object/public/imagenes/{modelo_key}.jpg"
  
   payload_json = json.dumps(datos_dict, ensure_ascii=False)
  
   registro_db = {
       "MODELO": str(modelo_key).strip(),
       "RUTA_IMAGEN": url_img_default,
       "datos_json": payload_json
   }
  
   # Si Supabase tarda demasiado (statement timeout, código 57014) se reintenta: guardar la misma ficha
   # otra vez no duplica nada (upsert por MODELO).
   for intento in range(3):
       try:
           return supabase.table("productos").upsert(registro_db, on_conflict="MODELO").execute()
       except Exception as ex:
           if "57014" not in str(ex) and "statement timeout" not in str(ex) or intento == 2:
               raise
           time.sleep(1.5 * (intento + 1))






# Al cambiar un campo solo se vuelve a ejecutar el formulario, no toda la página (más rápido).
_fragmento = getattr(st, "fragment", None) or (lambda f: f)


@_fragmento
def render_formulario_producto(datos_previos={}, modelo_existente=None):
   is_edit = modelo_existente is not None
   # En el alta, el prefijo lleva un número de versión: al guardar bien se sube la versión
   # y todos los campos se crean de nuevo (vacíos). Si hay error, la versión no cambia
   # y el formulario conserva lo que se capturó.
   version_alta = st.session_state.get("alta_version", 0)
   prefix = f"edit_{modelo_existente}" if is_edit else f"nuevo_prod_{version_alta}"
   if not is_edit:
       aviso_ok = st.session_state.pop("alta_aviso_ok", None)
       if aviso_ok:
           st.success(aviso_ok)
   siguiente_modelo = obtener_siguiente_modelo() if not is_edit and 'obtener_siguiente_modelo' in globals() else ""
  
   # --- SECCIÓN FOTO DE PORTADA EN ALTA / EDICIÓN ---
   st.header("FOTO DE PORTADA")
   nueva_foto_manual = st.file_uploader(
       "Subir / Asignar foto de portada (.jpg, .png)",
       type=["jpg", "jpeg", "png", "webp"],
       key=f"{prefix}_uploader_foto"
   )
   if nueva_foto_manual:
       st.image(nueva_foto_manual, caption="Vista previa de la portada a subir", width=250)
  
   st.divider()
   st.header("1. SECCIÓN PRODUCTO")
   col1, col2 = st.columns(2)
   with col1:
       if is_edit:
           modelo = st.text_input("MODELO", value=str(modelo_existente), disabled=True, key=f"{prefix}_modelo")
       else:
           modelo = st.text_input("MODELO *", value=str(datos_previos.get("MODELO", siguiente_modelo)), key=f"{prefix}_modelo").strip()
          
       nombre = st.text_input("NOMBRE", value=str(datos_previos.get("NOMBRE", "")), key=f"{prefix}_nombre")
       st.caption(f"CARACTERES DEL NOMBRE: **{len(nombre)}**")
      
       descripcion = st.text_area("DESCRIPCIÓN / FICHA TÉCNICA", value=str(datos_previos.get("DESCRIPCION", "")), height=100, key=f"{prefix}_desc")
       cedula = st.text_area("CÉDULA CURATORIAL", value=str(datos_previos.get("CEDULA_CURATORIAL", "")), height=100, key=f"{prefix}_cedula")
       st.caption(f"CARACTERES CÉDULA CURATORIAL: **{len(cedula)}**")
       with st.expander("Redactar con ChatGPT"):
           if not nombre.strip():
               st.info("Escribe primero el NOMBRE del producto.")
           else:
               tipo = st.radio("¿Qué quieres redactar?", list(PROMPTS_IA.keys()),
                               horizontal=True, key=f"{prefix}_ia_tipo")
               prompt_ia = PROMPTS_IA[tipo].format(nombre=nombre.strip())


               st.caption("1️⃣ Copia el prompt (icono arriba a la derecha del recuadro)")
               st.code(prompt_ia, language=None)


               st.caption("2️⃣ Abre el proyecto, pega el prompt y agrega la foto")
               st.link_button("Abrir mi proyecto en ChatGPT", PROYECTO_CHATGPT_URL,
                              use_container_width=True, key=f"{prefix}_ia_proj")


               st.caption("3️⃣ Copia la respuesta y pégala en el campo de abajo")


   with col2:
       c_dim1, c_dim2, c_dim3, c_dim4 = st.columns(4)
       with c_dim1:
           alto = st.text_input("ALTO (cm)", value=str(datos_previos.get("ALTO", "")), key=f"{prefix}_alto")
       with c_dim2:
           largo = st.text_input("LARGO (cm)", value=str(datos_previos.get("LARGO", "")), key=f"{prefix}_largo")
       with c_dim3:
           ancho = st.text_input("ANCHO/PROF (cm)", value=str(datos_previos.get("ANCHO_PROF", "")), key=f"{prefix}_ancho")
       with c_dim4:
           peso = st.text_input("PESO (kg)", value=str(datos_previos.get("PESO", "")), key=f"{prefix}_peso")


       disp_opts = ["Disponible (vacío)", "v - VENDIDO", "r - RESERVADO (cotización)"]
       _disp_prev = str(datos_previos.get("DISP", datos_previos.get("DISPONIBILIDAD", "")) or "").strip().lower()
       disp_idx = {"v": 1, "r": 2}.get(_disp_prev, 0)
       disp_sel = st.selectbox("DISPONIBILIDAD", disp_opts, index=disp_idx, key=f"{prefix}_disp",
                               help="RESERVADO lo pone solo una cotización vigente; se libera al vencer o cancelarse.")
       disponibilidad = "v" if "VENDIDO" in disp_sel else ("r" if "RESERVADO" in disp_sel else "")


       ubicacion_opts = ["1 - Showroom", "2 - Santa Fe", "3 - Antara", "4 - Lilly", "5 - Bodega"]
       ubic_idx = 0
       val_u_prev = str(datos_previos.get("UBICACION", ""))
       for i, u in enumerate(ubicacion_opts):
           if val_u_prev == u or val_u_prev.startswith(u[0]):
               ubic_idx = i
       ubicacion = st.selectbox("UBICACIÓN", ubicacion_opts, index=ubic_idx, key=f"{prefix}_ubic")


       categoria_opts = ["1 - Muebles", "2 - Objetos", "3 - Libros", "4 - Arte", "5 - Grafica"]
       cat_idx = 0
       val_c_prev = str(datos_previos.get("CATEGORIA", ""))
       for i, c in enumerate(categoria_opts):
           if val_c_prev == c or val_c_prev.startswith(c[0]):
               cat_idx = i
       categoria = st.selectbox("CATEGORÍA", categoria_opts, index=cat_idx, key=f"{prefix}_cat")


       subcategoria = st.text_input("SUBCATEGORÍA", value=str(datos_previos.get("SUBCATEGORÍA", "")), key=f"{prefix}_subcat")
       etiquetas = st.text_input("ETIQUETAS", value=str(datos_previos.get("ETIQUETAS", "")), key=f"{prefix}_etiq")


       observaciones = st.text_area(
           "OBSERVACIONES (uso interno)",
           value=str(datos_previos.get("OBSERVACIONES", "")),
           height=100,
           placeholder="Notas internas, recordatorios, pendientes…",
           help="Solo para uso interno. No se publica ni se envía a Shopify.",
           key=f"{prefix}_observaciones"
   )


   st.divider()
   st.header("2. SECCIÓN VENTA")
   cv1, cv2, cv3 = st.columns(3)
   with cv1:
       num_pedido = st.text_input("NÚMERO DE PEDIDO", value=str(datos_previos.get("NUMERO_PEDIDO", "")), key=f"{prefix}_npedido")
       fecha_pedido = st.text_input("FECHA DEL PEDIDO", value=str(datos_previos.get("FECHA_PEDIDO", "")), placeholder="10/oct/26", key=f"{prefix}_fpedido")
   with cv2:
       vendedor = st.text_input("VENDEDOR", value=str(datos_previos.get("VENDEDOR", "")), key=f"{prefix}_vendedor")
       factura = st.text_input("FACTURA", value=str(datos_previos.get("FACTURA", "")), key=f"{prefix}_factura")
   with cv3:
       fecha_factura = st.text_input("FECHA FACTURA", value=str(datos_previos.get("FECHA_FACTURA", "")), placeholder="10/oct/26", key=f"{prefix}_ffactura")
       status_pago_entrega = st.text_input("STATUS DE PAGO Y/O ENTREGA", value=str(datos_previos.get("STATUS_PAGO_ENTREGA", "")), key=f"{prefix}_status")


   st.divider()
   st.header("3. SECCIÓN COMPRA")
   pago_opts = ["1 - EFECTIVO", "2 - TRANSFERENCIA"]
   pago_quien_opts = ["1 - CAJA", "2 - ERNESTO", "3 - VIRIATO", "4 - CLAUDIA", "5 - MARY"]


   def _idx(opciones, valor):
       """Posición de la opción guardada (por el número «1 - …» o el texto); 0 si no se encuentra."""
       v = str(valor or "").strip().upper()
       for i, o in enumerate(opciones):
           if v and (v == o.upper() or v.split(" ")[0] == o.split(" ")[0] or v in o.upper()):
               return i
       return 0

   def _num_bloques(clave, n_inicial):
       if clave not in st.session_state:
           st.session_state[clave] = max(1, n_inicial)
       return st.session_state[clave]

   def _botones_bloques(clave, etiqueta):
       b1, b2, _ = st.columns([2, 2, 4])
       if b1.button(f"Añadir otra {etiqueta}" if etiqueta == "compra" else f"Añadir otro {etiqueta}",
                    key=f"{prefix}_btn_add_{etiqueta}"):
           st.session_state[clave] += 1
           st.rerun()
       if st.session_state[clave] > 1 and b2.button(f"Quitar la última" if etiqueta == "compra" else "Quitar el último",
                                                     key=f"{prefix}_btn_del_{etiqueta}"):
           st.session_state[clave] -= 1
           st.rerun()

   # ---------- Datos de Compra (puede haber varias; cada una con su costo) ----------
   st.subheader("Datos de Compra")
   lista_compras = datos_previos.get("COMPRAS") or []
   if not lista_compras:      # producto anterior: una sola compra con los campos sueltos
       lista_compras = [{
           "prov": str(datos_previos.get("PROVEEDOR_COMPRA", "")),
           "fp": str(datos_previos.get("FORMA_PAGO_COMPRA", "")),
           "qp": str(datos_previos.get("QUIEN_PAGO_COMPRA", "")),
           "fadq": str(datos_previos.get("FECHA_ADQUISICION", "")),
           "costo": datos_previos.get("COSTO_ADQUISICION", ""),
           "fpago": str(datos_previos.get("FECHA_PAGO_COMPRA", "")),
           "notas": str(datos_previos.get("NOTAS_FORMA_PAGO_COMPRA", "")),
       }]
   num_comp_key = f"num_comp_{prefix}"
   compras_capturadas = []
   for i_c in range(_num_bloques(num_comp_key, len(lista_compras))):
       d_c = lista_compras[i_c] if i_c < len(lista_compras) else {}
       n = i_c + 1
       st.markdown(f"**Compra #{n}**")
       k1, k2, k3 = st.columns(3)
       c_prov = k1.text_input(f"PROVEEDOR DE COMPRA #{n}", value=str(d_c.get("prov", "") or ""), key=f"{prefix}_provcompra_{i_c}")
       c_fp = k2.selectbox(f"FORMA DE PAGO A COMPRA #{n}", pago_opts, index=_idx(pago_opts, d_c.get("fp")), key=f"{prefix}_fp_compra_{i_c}")
       c_qp = k3.selectbox(f"QUIÉN PAGÓ (COMPRA) #{n}", pago_quien_opts, index=_idx(pago_quien_opts, d_c.get("qp")), key=f"{prefix}_qp_compra_{i_c}")
       k4, k5, k6 = st.columns(3)
       c_fadq = k4.text_input(f"FECHA ADQUISICIÓN #{n}", value=str(d_c.get("fadq", "") or ""), placeholder="10/oct/26", key=f"{prefix}_fadq_compra_{i_c}")
       costo_prev = d_c.get("costo", "")
       c_costo = k5.text_input(f"COSTO ADQUISICIÓN #{n} ($)", value="" if costo_prev in (None, "", 0, 0.0) else str(costo_prev), key=f"{prefix}_c_adq_{i_c}")
       c_fpago = k6.text_input(f"FECHA DE PAGO COMPRA #{n}", value=str(d_c.get("fpago", "") or ""), placeholder="10/oct/26", key=f"{prefix}_fpagocompra_{i_c}")
       c_notas = st.text_input(f"NOTAS FORMA PAGO COMPRA #{n}", value=str(d_c.get("notas", "") or ""), key=f"{prefix}_notaspagocompra_{i_c}")
       compras_capturadas.append({"prov": c_prov, "fp": c_fp, "qp": c_qp, "fadq": c_fadq, "costo": to_float(c_costo),
                                  "fpago": c_fpago, "notas": c_notas, "_txt": c_costo})
   _botones_bloques(num_comp_key, "compra")
   requerimientos = st.text_input("REQUERIMIENTOS", value=str(datos_previos.get("REQUERIMIENTOS", "")), key=f"{prefix}_req")
   # Campos sueltos de siempre = la primera compra (los usan la búsqueda y las cargas de Excel)
   _c0 = compras_capturadas[0]
   prov_compra, forma_pago_compra, quien_pago_compra = _c0["prov"], _c0["fp"], _c0["qp"]
   fecha_adquisicion_compra, fecha_pago_compra, notas_pago_compra = _c0["fadq"], _c0["fpago"], _c0["notas"]

   # ---------- Datos de Restauro (varios; cada uno con su costo) ----------
   st.subheader("Datos de Restauro")
   lista_restauradores = datos_previos.get("RESTAURADORES", [])
   if not lista_restauradores:
       lista_restauradores = [{
           "prov": str(datos_previos.get("PROVEEDOR_RESTAURO", "")),
           "fp": str(datos_previos.get("FORMA_PAGO_RESTAURO", "1 - EFECTIVO")),
           "qp": str(datos_previos.get("QUIEN_PAGO_RESTAURO", "1 - CAJA")),
           "fecha": str(datos_previos.get("FECHA_PAGO_RESTAURO", "")),
           "notas": str(datos_previos.get("NOTAS_FORMA_PAGO_RESTAURO", ""))
       }]
   # Productos anteriores: el costo de restauro estaba solo en Costos; pasa al primer restaurador
   if not any("costo" in r for r in lista_restauradores) and lista_restauradores:
       lista_restauradores = [dict(r) for r in lista_restauradores]
       lista_restauradores[0]["costo"] = datos_previos.get("COSTO_RESTAURO", "")

   num_rest_key = f"num_rest_{prefix}"
   restauradores_capturados = []
   for idx_r in range(_num_bloques(num_rest_key, len(lista_restauradores))):
       d_r = lista_restauradores[idx_r] if idx_r < len(lista_restauradores) else {}
       n = idx_r + 1
       st.markdown(f"**Restaurador #{n}**")
       cr1, cr2, cr3 = st.columns(3)
       p_rest = cr1.text_input(f"PROVEEDOR DE RESTAURO #{n}", value=str(d_r.get("prov", "") or ""), key=f"{prefix}_pr_{idx_r}")
       fp_rest = cr2.selectbox(f"FORMA DE PAGO RESTAURO #{n}", pago_opts, index=_idx(pago_opts, d_r.get("fp")), key=f"{prefix}_fpr_{idx_r}")
       qp_rest = cr3.selectbox(f"QUIÉN PAGÓ (RESTAURO) #{n}", pago_quien_opts, index=_idx(pago_quien_opts, d_r.get("qp")), key=f"{prefix}_qpr_{idx_r}")
       cr4, cr5, cr6 = st.columns(3)
       f_rest = cr4.text_input(f"FECHA DE PAGO RESTAURO #{n}", value=str(d_r.get("fecha", "") or ""), placeholder="10/oct/26", key=f"{prefix}_fpr_fecha_{idx_r}")
       costo_prev = d_r.get("costo", "")
       c_rest_txt = cr5.text_input(f"COSTO RESTAURO #{n} ($)", value="" if costo_prev in (None, "", 0, 0.0) else str(costo_prev), key=f"{prefix}_c_rest_{idx_r}")
       n_rest = cr6.text_input(f"NOTAS FORMA PAGO RESTAURO #{n}", value=str(d_r.get("notas", "") or ""), key=f"{prefix}_npr_{idx_r}")
       restauradores_capturados.append({
           "prov": p_rest, "fp": fp_rest, "qp": qp_rest, "fecha": f_rest, "notas": n_rest,
           "costo": to_float(c_rest_txt), "_txt": c_rest_txt,
       })
   _botones_bloques(num_rest_key, "restaurador")


   st.divider()
   st.header("4. SECCIÓN COSTOS, SHOWROOM Y CASA PALACIO")
   # ---------- COSTOS BASE (manuales) ----------
   st.subheader("Registro de Costos Base")
   col_c1, col_c2, col_c3 = st.columns(3)
   # Totales automáticos: suma de los costos de cada compra / cada restaurador (sección 3)
   v_costo_adq = "x" if any(hay(c["_txt"]) for c in compras_capturadas) else ""
   v_costo_rest = "x" if any(hay(r["_txt"]) for r in restauradores_capturados) else ""
   tot_adq = round(sum(c["costo"] for c in compras_capturadas), 2)
   tot_rest = round(sum(r["costo"] for r in restauradores_capturados), 2)
   with col_c1:
       mostrar_calculado("COSTO ADQUISICIÓN TOTAL ($)", tot_adq if v_costo_adq else None, f"{prefix}_c_adq_tot",
                         "Suma de los costos de adquisición de cada compra")
       v_costo_fin = st.text_input("COSTO FINANCIERO ($)", value=str(datos_previos.get("COSTO_FINANCIERO", "")), key=f"{prefix}_c_fin")
   with col_c2:
       mostrar_calculado("COSTO RESTAURO TOTAL ($)", tot_rest if v_costo_rest else None, f"{prefix}_c_rest_tot",
                         "Suma de los costos de cada restaurador")
       v_costo_fletes = st.text_input("COSTO FLETES ($)", value=str(datos_previos.get("COSTO_FLETES", "")), key=f"{prefix}_c_fletes")
   with col_c3:
       v_otros_costos = st.text_input("OTROS COSTOS ($)", value=str(datos_previos.get("OTROS_COSTOS", "")), key=f"{prefix}_c_otros")
   c_adq, c_fin, c_rest = tot_adq, to_float(v_costo_fin), tot_rest
   c_fletes, c_otros = to_float(v_costo_fletes), to_float(v_otros_costos)
   # TOTAL COSTO. Si el producto viejo no tenía desglose pero sí un total (o costo registrado), se respeta.
   hay_costos = any(hay(x) for x in [v_costo_adq, v_costo_fin, v_costo_rest, v_costo_fletes, v_otros_costos])
   suma_costos = c_adq + c_fin + c_rest + c_fletes + c_otros
   total_previo = to_float(datos_previos.get("TOTAL_COSTO", 0)) or to_float(datos_previos.get("COSTO_REGISTRADO", 0))
   if hay_costos and suma_costos > 0:
       total_costo = suma_costos
   elif total_previo > 0:
       total_costo = total_previo
   elif hay_costos:
       total_costo = suma_costos
   else:
       total_costo = None
   mostrar_calculado("TOTAL COSTO ($)", total_costo, f"{prefix}_tot_costo",
                     "COSTO ADQUISICION + COSTO FINANCIERO + COSTO RESTAURO + COSTO FLETES + OTROS COSTOS")
   st.divider()
   col_show, col_cp = st.columns(2)
   # ---------- SHOWROOM ----------
   with col_show:
       st.subheader("SHOWROOM")
       # Los espacios se crean en el orden en que se VEN y se llenan en el orden en que se CALCULAN,
       # así UTILIDAD BRUTA (manual) queda abajo aunque los campos de arriba dependan de ella.
       (c_sub, c_pci, c_iva, c_ub, c_psi,
        c_red, c_cisi, c_cuv, c_uvsi, c_uvc) = [st.container() for _ in range(10)]
       with c_ub:
           v_ub = st.text_input("UTILIDAD BRUTA ($)",
                                value=str(datos_previos.get("UTILIDAD_BRUTA", datos_previos.get("UTILIDAD_SHOWROOM", ""))),
                                key=f"{prefix}_ub")
       utilidad_bruta = to_float(v_ub) if hay(v_ub) else None
       subtotal = (total_costo + utilidad_bruta) if ok(total_costo, utilidad_bruta) else None
       with c_sub:
           mostrar_calculado("SUBTOTAL ($)", subtotal, f"{prefix}_subtotal", "TOTAL COSTO + UTILIDAD BRUTA")
       precio_com_int = (subtotal / 0.9) if ok(subtotal) else None
       with c_pci:
           mostrar_calculado("PRECIO COMISION INTERIORISTA ($)", precio_com_int, f"{prefix}_pci", "SUBTOTAL / 0.9")
       iva_sh = (precio_com_int * 0.16) if ok(precio_com_int) else None
       with c_iva:
           mostrar_calculado("IVA SHOWROOM ($)", iva_sh, f"{prefix}_iva_sh", "PRECIO COMISION INTERIORISTA * 16%")
       precio_sh_con_iva = (precio_com_int + iva_sh) if ok(precio_com_int, iva_sh) else None
       with c_psi:
           mostrar_calculado("PRECIO SHOWROOM CON IVA ($)", precio_sh_con_iva, f"{prefix}_ps_iva",
                             "PRECIO COMISION INTERIORISTA + IVA")
       with c_red:
           v_red_sh = st.text_input("REDONDEO SHOWROOM ($)", value=str(datos_previos.get("REDONDEO_SHOWROOM", "")), key=f"{prefix}_red_sh")
           if ok(precio_sh_con_iva):
               st.caption(f"Sugerido por regla de redondeo: **${aplicar_regla_redondeo(precio_sh_con_iva):,.2f}**")
       redondeo_sh = to_float(v_red_sh) if hay(v_red_sh) else None
       com_int_sin_iva = (precio_com_int - subtotal) if ok(precio_com_int, subtotal) else None
       with c_cisi:
           mostrar_calculado("COMISION INTERIORISTA SIN IVA ($)", com_int_sin_iva, f"{prefix}_cisi",
                             "PRECIO COMISION INTERIORISTA - SUBTOTAL")
       costo_mas_uv = (precio_com_int - com_int_sin_iva) if ok(precio_com_int, com_int_sin_iva) else None
       with c_cuv:
           mostrar_calculado("COSTO + UTILIDAD VIRIATHUS ($)", costo_mas_uv, f"{prefix}_cuv",
                             "PRECIO COMISION INTERIORISTA - COMISION INTERIORISTA SIN IVA")
       util_vir_sin_iva = (precio_com_int - com_int_sin_iva - total_costo) if ok(precio_com_int, com_int_sin_iva, total_costo) else None
       with c_uvsi:
           mostrar_calculado("UTILIDAD VIRIATHUS SIN IVA ($)", util_vir_sin_iva, f"{prefix}_uvsi",
                             "PRECIO COMISION INTERIORISTA - COMISION INTERIORISTA SIN IVA - TOTAL COSTO")
       util_vir_con_com = (com_int_sin_iva + util_vir_sin_iva) if ok(com_int_sin_iva, util_vir_sin_iva) else None
       with c_uvc:
           mostrar_calculado("UTILIDAD VIRIATHUS CON COMISION DE INTERIORISTA ($)", util_vir_con_com, f"{prefix}_uvc",
                             "COMISION INTERIORISTA SIN IVA + UTILIDAD VIRIATHUS SIN IVA")
   # ---------- CASA PALACIO ----------
   with col_cp:
       st.subheader("CASA PALACIO / C/P")
       factor_viriathus = st.number_input("FACTOR VIRIATHUS", value=float(to_float(datos_previos.get("FACTOR_VIRIATHUS", 1.4), 1.4)),
                                          min_value=0.0, step=0.05, format="%.2f", key=f"{prefix}_factor_vir")
       ptv_sin_iva = ((utilidad_bruta * factor_viriathus) + total_costo) if ok(utilidad_bruta, total_costo) else None
       mostrar_calculado("PRECIO TOTAL VIRIATHUS SIN IVA ($)", ptv_sin_iva, f"{prefix}_ptv",
                        "(UTILIDAD BRUTA * FACTOR VIRIATHUS) + TOTAL COSTO")






       pvcp_iva = (ptv_sin_iva / 0.65 * 1.16) if ok(ptv_sin_iva) else None
       mostrar_calculado("PRECIO DE VENTA CASA PALACIO CON IVA (I/0.65/1.16) ($)", pvcp_iva, f"{prefix}_pvcp_iva",
                         "PRECIO TOTAL VIRIATHUS SIN IVA / 0.65 * 1.16")
       v_pvcp_red = st.text_input("PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO ($)",
                                  value=str(datos_previos.get("CP_PRECIO_REDONDEADO", "")), key=f"{prefix}_pvcp_red")
       if ok(pvcp_iva):
           st.caption(f"Sugerido por regla de redondeo: **${aplicar_regla_redondeo(pvcp_iva):,.2f}**")
       pvcp_red = to_float(v_pvcp_red) if hay(v_pvcp_red) and to_float(v_pvcp_red) > 0 else None   # 0 = sin precio de C/P
       cp_sin_iva = (pvcp_red / 1.16) if ok(pvcp_red) else None
       mostrar_calculado("C/P PRECIO SIN IVA ($)", cp_sin_iva, f"{prefix}_cp_sin_iva",
                         "PRECIO CASA PALACIO CON IVA REDONDEADO / 1.16")
       cp_utilidad = (cp_sin_iva - ptv_sin_iva) if ok(cp_sin_iva, ptv_sin_iva) else None
       mostrar_calculado("C/P UTILIDAD ($)", cp_utilidad, f"{prefix}_cp_util",
                         "C/P PRECIO SIN IVA - PRECIO TOTAL VIRIATHUS SIN IVA")
       cp_pct = (cp_utilidad / cp_sin_iva * 100.0) if ok(cp_utilidad, cp_sin_iva) and cp_sin_iva > 0 else None
       mostrar_calculado("C/P % DE UTILIDAD (%)", cp_pct, f"{prefix}_cp_pct",
                         "(C/P UTILIDAD / C/P PRECIO SIN IVA) * 100")
       v_cantidad = st.text_input("CANTIDAD", value=str(datos_previos.get("CANTIDAD", "1")), key=f"{prefix}_cantidad")
       cantidad = int(to_float(v_cantidad, 1.0))
       total_cp = (pvcp_red * cantidad) if ok(pvcp_red) else None
       mostrar_calculado("TOTAL ($)", total_cp, f"{prefix}_total_cp",
                         "PRECIO CASA PALACIO CON IVA REDONDEADO * CANTIDAD")


   st.divider()
   if st.button("Guardar Producto Completo", type="primary", use_container_width=True, key=f"{prefix}_guardar_btn"):
       if not modelo:
           st.error("El campo MODELO es obligatorio.")
           return

       # En alta, no permitir sobrescribir un modelo que ya existe
       if not is_edit:
           try:
               existe = supabase.table("productos").select("MODELO").eq("MODELO", modelo).limit(1).execute()
               if existe.data:
                   st.error(f"El modelo '{modelo}' ya existe en el inventario. Cambia el número de MODELO; el formulario conserva tus datos.")
                   return
           except Exception as ex_chk:
               st.error(f"No se pudo verificar si el modelo ya existe: {ex_chk}")
               return


       # SI SE SUBIÓ UNA FOTO NUEVA EN EL FORMULARIO, SUBIRLA PRIMERO A STORAGE
       info_foto = ""
       if nueva_foto_manual is not None:
           with st.spinner("Subiendo foto de portada a Supabase Storage..."):
               try:
                   bytes_img, kb_antes, kb_despues = comprimir_foto(nueva_foto_manual.read())
                   info_foto = f" Foto comprimida: {kb_antes:,} KB → {kb_despues:,} KB."
                   nombre_remoto = f"{modelo}.jpg"
                   supabase.storage.from_("imagenes").upload(
                       path=nombre_remoto,
                       file=bytes_img,
                       file_options={"content-type": "image/jpeg", "upsert": "true"}
                   )
               except Exception as ex_foto:
                   st.error(f"No se pudo subir la foto: {ex_foto}. El producto NO se guardó; revisa y vuelve a intentar.")
                   return


       datos_guardar = {


           "MODELO": modelo, "NOMBRE": " ".join(str(nombre or "").split()), "DESCRIPCION": descripcion, "CEDULA_CURATORIAL": cedula,
           "OBSERVACIONES": observaciones,
           "ALTO": alto, "LARGO": largo, "ANCHO_PROF": ancho, "PESO": peso,
           "DISPONIBILIDAD": disponibilidad, "DISP": disponibilidad, "UBICACION": ubicacion,
           "CATEGORIA": categoria, "SUBCATEGORÍA": subcategoria, "ETIQUETAS": etiquetas,
           "NUMERO_PEDIDO": num_pedido, "FECHA_PEDIDO": fecha_pedido, "VENDEDOR": vendedor,
           "FACTURA": factura, "FECHA_FACTURA": fecha_factura, "STATUS_PAGO_ENTREGA": status_pago_entrega,
           "PROVEEDOR_COMPRA": prov_compra, "FORMA_PAGO_COMPRA": forma_pago_compra,
           "QUIEN_PAGO_COMPRA": quien_pago_compra, "FECHA_PAGO_COMPRA": fecha_pago_compra,
           "NOTAS_FORMA_PAGO_COMPRA": notas_pago_compra, "FECHA_ADQUISICION": fecha_adquisicion_compra,
           "REQUERIMIENTOS": requerimientos,
           "COMPRAS": [{k: v for k, v in c.items() if k != "_txt"} for c in compras_capturadas],
           "RESTAURADORES": [{k: v for k, v in r.items() if k != "_txt"} for r in restauradores_capturados],
           "COSTO_REGISTRADO": to_float(datos_previos.get("COSTO_REGISTRADO", 0)),  # campo ya no visible; se conserva el dato
           "COSTO_ADQUISICION": c_adq, "COSTO_FINANCIERO": c_fin, "COSTO_RESTAURO": c_rest,
           "COSTO_FLETES": c_fletes, "OTROS_COSTOS": c_otros,
           "TOTAL_COSTO": num(total_costo), "FACTOR_VIRIATHUS": factor_viriathus,
           "UTILIDAD_BRUTA": num(utilidad_bruta),
           "UTILIDAD_SHOWROOM": num(utilidad_bruta),                 # compatibilidad con datos/scripts anteriores
           "PRECIO_TOTAL_VIRIATHUS_SIN_IVA": num(ptv_sin_iva),
           "SUBTOTAL": num(subtotal),
           "PRECIO_SIN_COMISION_INTERIORISTA": num(subtotal),        # compatibilidad con datos/scripts anteriores
           "PRECIO_COMISION_INTERIORISTA": num(precio_com_int),
           "IVA_SHOWROOM": num(iva_sh), "PRECIO_SHOWROOM_CON_IVA": num(precio_sh_con_iva),
           "REDONDEO_SHOWROOM": num(redondeo_sh),
           "COMISION_INTERIORISTA_SIN_IVA": num(com_int_sin_iva),
           "COSTO_MAS_UTILIDAD_VIRIATHUS": num(costo_mas_uv),
           "UTILIDAD_VIRIATHUS_SIN_IVA": num(util_vir_sin_iva),
           "UTILIDAD_VIRIATHUS_CON_COMISION": num(util_vir_con_com),
           "PRECIO_CP_CON_IVA_CALCULADO": num(pvcp_iva), "CP_PRECIO_REDONDEADO": num(pvcp_red),
           "CP_PRECIO_SIN_IVA": num(cp_sin_iva), "CP_UTILIDAD": num(cp_utilidad),
           "CP_PORCENTAJE_UTILIDAD": num(cp_pct), "CANTIDAD": cantidad, "TOTAL_CP": num(total_cp)
       }


       with st.spinner("Guardando en la base de datos..."):
           try:
               res = guardar_producto_db(modelo, datos_guardar)
               guardado_ok = bool(res.data)
               error_guardado = None if guardado_ok else "Supabase no confirmó el guardado (respuesta vacía)."
           except Exception as ex:
               guardado_ok, error_guardado = False, str(ex)

       if guardado_ok:
           try:
               obtener_siguiente_modelo.clear()
               _limpiar_memoria()          # que el producto nuevo aparezca ya en «últimos productos»
           except Exception:
               pass
           # Caudal: los costos de compra / restauro que se cargaron o cambiaron restan a quien pagó
           if _HAY_CAUDAL:
               try:
                   import caudal
                   caudal.sincronizar_ficha(supabase, modelo, datos_previos, datos_guardar,
                                            user.get("nombre") or user.get("role"))
               except Exception as ex_caudal:
                   st.warning(f"El producto se guardó, pero no se pudo registrar en Caudal: {ex_caudal}")
           if is_edit:
               st.success(f"¡Producto '{modelo}' actualizado correctamente!{info_foto}")
               # Si el producto ya está en Shopify (SKU = modelo), se mandan los cambios
               vendido_antes = str(datos_previos.get("DISP", datos_previos.get("DISPONIBILIDAD", ""))).strip().lower() in ("v", "r")
               with st.spinner("Actualizando Shopify..."):
                   # El nombre y la descripción solo se mandan a Shopify si los cambiaste en esta ficha (y no están vacíos)
                   desc_cambio = (descripcion.strip() != "" and
                                  " ".join(descripcion.split()) != " ".join(str(datos_previos.get("DESCRIPCION", "") or "").split()))
                   nombre_cambio = (nombre.strip() != "" and
                                    " ".join(nombre.split()) != " ".join(str(datos_previos.get("NOMBRE", "") or "").split()))
                   precio_cambio = (num(redondeo_sh) > 0 and
                                    round(num(redondeo_sh), 2) != round(to_float(datos_previos.get("REDONDEO_SHOWROOM", 0)), 2))
                   est_sh, msg_sh, _url_sh = actualizar_producto_shopify(
                       modelo, datos_guardar, enviar_descripcion=desc_cambio, enviar_nombre=nombre_cambio,
                       enviar_precio=precio_cambio)
                   if est_sh == "ok":
                       registrar_envio(supabase, modelo, datos_guardar,
                                       [c for c, f in (("nombre", nombre_cambio), ("desc", desc_cambio), ("precio", precio_cambio)) if f])
                   if est_sh == "ok" and (disponibilidad in ("v", "r")) != vendido_antes:
                       est_sh2, msg_sh2 = marcar_disponibilidad_shopify(modelo, disponibilidad in ("v", "r"))
                       if est_sh2 == "error":
                           est_sh, msg_sh = "error", msg_sh2
                       else:
                           msg_sh = msg_sh + " " + msg_sh2
               if est_sh == "ok":
                   st.info("" + msg_sh)
               elif est_sh == "error":
                   st.warning("Se guardó en el inventario, pero NO en Shopify: " + msg_sh +
                              " Puedes reintentarlo con Sincronizar con Shopify en la ficha.")
           else:
               # Éxito: nueva versión del formulario = campos vacíos para el siguiente producto
               st.session_state["alta_version"] = version_alta + 1
               st.session_state["alta_aviso_ok"] = f"¡Producto '{modelo}' guardado correctamente!{info_foto} Ya puedes capturar el siguiente."
               st.rerun()
       else:
           st.error(f"El producto '{modelo}' NO se guardó: {error_guardado} "
                    "Corrige el problema y vuelve a tocar Guardar; el formulario conserva tus datos.")


   st.divider()
  
   # Renderizar el formulario con los datos cargados




# --- MENÚ Y NAVEGACIÓN ---
with st.sidebar:
   st.image(os.path.join(_DIR_ASSETS, "logo_largo.png"), use_container_width=True)
   st.write(f"**Usuario:** {user['nombre']}")
   st.write(f"**Rol:** `{user['role']}`")
   st.divider()
  
   # La llave para no tener que volver a iniciar sesión (se deja en cada recarga; no ocupa lugar)
   if st.session_state.get("_recordar") and st.session_state.get("login_usuario") in USUARIOS:
       _u = st.session_state["login_usuario"]
       _sesion.recordar(_u, USUARIOS[_u]["password"])
   if st.button("Cerrar Sesión", use_container_width=True, key="btn_logout"):
       st.session_state["logged_in"] = False
       st.session_state["_sesion_cerrada"] = True     # que no vuelva a entrar solo en esta pestaña
       st.session_state["_borrar_llave"] = True       # y que el navegador olvide la llave
       st.session_state["_recordar"] = False
       st.rerun()
   if user["role"] == "Dios":
       import bitacora as _bit
       _mem, _tipo = _bit.rss_mb(), ("actual" if os.path.exists("/proc/self/status") else "pico")
       if _mem is not None:
           _sem = "🟢" if _mem < 450 else ("🟡" if _mem < 650 else "🔴")
           _ses = _sesiones_abiertas()
           st.caption(f"{_sem} Memoria de la app: **{_mem:,.0f} MB**" + (" (pico)" if _tipo == "pico" else "")
                      + (f" · {_ses} sesión(es) abierta(s)" if _ses is not None else ""),
                      help="Streamlit Cloud garantiza ~690 MB (a veces da más). Verde < 450 MB, amarillo < 650 MB, "
                           "rojo: cerca del límite. Si se queda en rojo, avisa qué se estaba haciendo.")
       try:
           import bitacora
           bitacora.render()
       except Exception as _ex_bit:
           st.caption(f"Bitácora no disponible: {_ex_bit}")
   st.divider()
   # Ventas de Shopify -> inventario (revisa Shopify como máximo cada 2 minutos)
   _sync = sincronizar_en_segundo_plano(supabase)
   if _sync.get("pendiente"):
       st.caption("Revisando Shopify…")
   elif _sync["ok"]:
       st.caption(f"Shopify revisado a las {_sync['hora']}")
   else:
       st.warning(f"No se pudo revisar Shopify: {_sync['error']}")
   # Cotizaciones vencidas -> los productos reservados vuelven a estar disponibles (en segundo plano, cada 5 min)
   try:
       from cotizaciones import revisar_vencimientos
       revisar_vencimientos(supabase)
   except Exception:
       pass          # si aún no están las tablas de cotizaciones, no pasa nada
   if puede(user, "sync_productos"):
       # Nombre, descripción y precio: se mantienen iguales en inventario y Shopify (revisa como máximo cada 5 min)
       _sync_p = sincronizar_productos_en_segundo_plano(supabase)
       if _sync_p.get("pendiente"):
           st.caption("Revisando productos…")
       elif _sync_p["ok"]:
           st.caption(f"Productos sincronizados a las {_sync_p['hora']}")
       else:
           st.warning(f"No se pudo sincronizar productos: {_sync_p['error']}")


st.title("Buscador e Inventario de Productos")
try:
   from segundo_plano import liberar_memoria_cada
   liberar_memoria_cada(60)          # devuelve al servidor la memoria libre (máx. una vez por minuto)
except Exception:
   pass
if st.session_state.get("aviso_foto"):
   st.success(st.session_state.pop("aviso_foto"))
if puede(user, "herramientas"):
   _conf = conflictos_pendientes(supabase)
   if _conf:
       st.warning(f"{len(_conf)} producto(s) con cambios distintos en el inventario y en Shopify. "
                  "Ve a «Herramientas & Scripts» → «Sincronización de productos» para elegir cuál se queda.")
   _avisos_shop = aviso_pendiente(supabase)
   if _avisos_shop:
       st.warning(f"{len(_avisos_shop)} venta(s) de Shopify con avisos por revisar (posible doble venta o SKU sin inventario). "
                  "Ve a «Herramientas & Scripts» → «Ventas de Shopify».")
# Disponibilidad distinta entre inventario y Shopify (con aviso; se resuelve en la pestaña «Shopify»)
if puede(user, "shopify_botones"):
   try:
       from disponibilidad import pendientes as _disp_pendientes
       _difs_disp = _disp_pendientes(supabase)
   except Exception:
       _difs_disp = []
   if _difs_disp:
       st.warning(f"{len(_difs_disp)} pieza(s) con disponibilidad distinta entre el inventario y Shopify. "
                  "Ve a la pestaña «Shopify» para revisarlas.")
# Cambios de precio autorizados (precios.py)
try:
   import precios as _precios
   _precios.render_aviso(supabase, user)
except Exception:
   pass
_bita.anotar("Encabezado y barra lateral (en cada clic)", _bita_ini, _bita.rss_mb())
# Las pestañas que el rol no puede usar ni se muestran (ver permisos.py)
_nombres_tabs = ["Consultar / Buscar"]
if puede(user, "alta_manual"):
    _nombres_tabs.append("Alta Manual")
if puede(user, "herramientas"):
    _nombres_tabs.append("Herramientas & Scripts")
if puede(user, "shopify_lista"):
    _nombres_tabs.append("Shopify")
if MOSTRAR_VENTAS:
    _nombres_tabs.append("Ventas")
if _HAY_CAUDAL and puede(user, "caudal"):
    _nombres_tabs.append("Caudal")
# Solo se carga la sección elegida (con st.tabs se cargaban todas en cada clic)
from navegacion import selector as _selector
_seccion = _selector(_nombres_tabs, "nav_principal")
def _nombre_actividad(nombre):
    """«Ventas › Pedidos», «Ventas › Cotizaciones › Guardadas», «Consultar / Buscar»…"""
    def f():
        ss = st.session_state
        partes = [nombre]
        if nombre == "Ventas" and ss.get("nav_ventas__ultimo"):
            partes.append(ss["nav_ventas__ultimo"])
            if ss["nav_ventas__ultimo"] == "Cotizaciones" and ss.get("nav_cot__ultimo"):
                partes.append(ss["nav_cot__ultimo"])
        return "Página: " + " › ".join(partes)
    return f
def _seccion_si(nombre):
    if _seccion != nombre:
        return None
    try:
        from bitacora import medir
        return medir(_nombre_actividad(nombre), st.container(),
                     al_salir=lambda mb: st.session_state.__setitem__("_bita_fin_run", mb))
    except Exception:
        return st.container()
tab1 = _seccion_si("Consultar / Buscar")
tab2 = _seccion_si("Alta Manual")
tab3 = _seccion_si("Herramientas & Scripts")
tab5 = _seccion_si("Shopify")
tab4 = _seccion_si("Ventas")
tab_caudal = _seccion_si("Caudal")


# DIÁLOGO DE EDICIÓN EN MODAL
@st.dialog("Editar Producto Completo", width="large")
def modal_editar_completo(mod_p, pdata):
   st.caption(f"Gestión y edición del modelo: **{mod_p}**")
  
   # Opción para cambiar foto
   with st.expander("Cambiar / Subir Foto de Portada"):
       nueva_foto = st.file_uploader("Selecciona una imagen (.jpg, .png)", type=["jpg", "jpeg", "png", "webp"], key=f"uploader_{mod_p}")
       if nueva_foto is not None:
           if st.button("Subir Imagen a Supabase", use_container_width=True, key=f"btn_upload_img_{mod_p}"):
               with st.spinner("Subiendo imagen..."):
                   try:
                       bytes_img, kb_antes, kb_despues = comprimir_foto(nueva_foto.read())
                       nombre_remoto = f"{mod_p}.jpg"
                       supabase.storage.from_("imagenes").upload(
                           path=nombre_remoto,
                           file=bytes_img,
                           file_options={"content-type": "image/jpeg", "upsert": "true"}
                       )
                       sb_url = st.secrets.get("SUPABASE_URL", "")
                       url_publica = f"{sb_url}/storage/v1/object/public/imagenes/{nombre_remoto}"
                       supabase.table("productos").update({"RUTA_IMAGEN": url_publica}).eq("MODELO", mod_p).execute()
                       st.session_state["aviso_foto"] = f"Foto del modelo {mod_p} actualizada: {kb_antes:,} KB → {kb_despues:,} KB."
                       st.rerun()
                   except Exception as ex_foto:
                       st.error(f"Error: {ex_foto}")


   # Opción para eliminar
   with st.expander("Zona de Peligro: Eliminar Producto"):
       st.warning("Esta acción borrará el producto permanentemente de Supabase.")
       confirm_del = st.checkbox("Confirmo que deseo eliminarlo", key=f"check_del_{mod_p}")
       if confirm_del:
           if st.button("Eliminar Producto Definitivamente", type="primary", use_container_width=True, key=f"btn_confirm_del_{mod_p}"):
               with st.spinner("Eliminando..."):
                   try:
                       supabase.table("productos").delete().eq("MODELO", mod_p).execute()
                       _limpiar_memoria()
                       st.success(f"Producto {mod_p} eliminado.")
                       st.rerun()
                   except Exception as ex_del:
                       st.error(f"Error al eliminar: {ex_del}")


   st.divider()
   render_formulario_producto(pdata, modelo_existente=mod_p)


# TAB 1: BÚSQUEDA
if tab1 is not None:
   with tab1:
      col_b1, col_b2, col_b3 = st.columns([2, 1, 1])
      with col_b1:
          busqueda_raw = st.text_input("Buscar por modelo o palabra clave:", key="search_bar")
      with col_b2:
          rango_desde = st.text_input("Desde modelo", key="rango_desde").strip()
      with col_b3:
          rango_hasta = st.text_input("Hasta modelo", key="rango_hasta").strip()


      modo_rango = rango_desde.isdigit() and rango_hasta.isdigit()
      busqueda = f"{rango_desde} a {rango_hasta}" if modo_rango else busqueda_raw.strip()
  
      mostrar_ultimos = not busqueda
      if busqueda or mostrar_ultimos:
          if "busqueda_anterior" not in st.session_state or st.session_state["busqueda_anterior"] != busqueda:
              st.session_state["busqueda_anterior"] = busqueda
              st.session_state["pagina_actual"] = 0


          tamano_pagina = 30
          pagina = st.session_state.get("pagina_actual", 0)
          desde = pagina * tamano_pagina
          hasta = desde + tamano_pagina - 1


          with st.spinner("Consultando inventario..."):
              # Sanitizar búsqueda (quitar caracteres que rompan PostgREST)
              if mostrar_ultimos:
                  query = None
              elif modo_rango:
                  limite_sup = str(int(rango_hasta) + 1)
                  query = supabase.table("productos").select("*", count="exact")
                  query = query.gte("MODELO", rango_desde).lt("MODELO", limite_sup).order("MODELO")
              else:
                  busqueda_clean = re.sub(r'[%"\',()]', '', busqueda)
                  query = supabase.table("productos").select("*", count="exact")
                  query = query.or_(f"MODELO.ilike.%{busqueda_clean}%,datos_json.ilike.%{busqueda_clean}%")
          
              resp, ultimo_error = None, None
              if mostrar_ultimos:
                  productos, total_coincidencias = [], 0
                  for intento in range(2):
                      try:
                          productos, total_coincidencias = ultimos_productos(desde, tamano_pagina)
                          ultimo_error = None
                          break
                      except Exception as e:
                          ultimo_error = e
                          time.sleep(1)
                  if ultimo_error is not None:
                      st.warning(f"No se pudieron cargar los últimos productos (la base tardó demasiado). "
                                 f"Recarga en unos segundos o busca por modelo. Detalle: {ultimo_error}")
              else:
                  for intento in range(3):
                      try:
                          resp = query.range(desde, hasta).execute()
                          break
                      except APIError as e:
                          ultimo_error = e
                          time.sleep(1)
                  if resp is None:
                      st.error(f"No se pudo consultar la base de datos: {ultimo_error}")
                      productos, total_coincidencias = [], 0
                  else:
                      productos = resp.data
                      total_coincidencias = resp.count if resp.count is not None else len(productos)


          if productos:
              total_paginas = max(1, ((total_coincidencias - 1) // tamano_pagina) + 1)
          
              col_info, col_pag = st.columns([2, 1])
              with col_info:
                  if mostrar_ultimos:
                      st.subheader("🆕 Últimos productos agregados")
                      st.caption(f"Del más reciente al más antiguo · productos {desde + 1} a {min(hasta + 1, total_coincidencias)} "
                                 f"(Página {pagina + 1} de {total_paginas}). Escribe un modelo o palabra clave arriba para buscar.")
                  else:
                      st.subheader(f"Resultados para \"{busqueda}\" ({total_coincidencias} encontrados):")
                      st.caption(f"Mostrando productos *{desde + 1}* a *{min(hasta + 1, total_coincidencias)}* (Página {pagina + 1} de {total_paginas})")
          
              with col_pag:
                  c_prev, c_next = st.columns(2)
                  with c_prev:
                      if st.button("Anterior", disabled=(pagina == 0), key="btn_prev_top", use_container_width=True):
                          st.session_state["pagina_actual"] -= 1
                          st.rerun()
                  with c_next:
                      if st.button("Siguiente ", disabled=(pagina >= total_paginas - 1), key="btn_next_top", use_container_width=True):
                          st.session_state["pagina_actual"] += 1
                          st.rerun()


              st.divider()
              cols_por_fila = 3
              cols = st.columns(cols_por_fila)
          
              for idx, item in enumerate(productos):
                  mod_p = item.get("MODELO")
                  img_url = item.get("RUTA_IMAGEN")
              
                  raw_json = item.get("datos_json", "{}")
                  if isinstance(raw_json, str):
                      try:
                          pdata = json.loads(raw_json)
                      except:
                          pdata = {}
                  else:
                      pdata = raw_json if isinstance(raw_json, dict) else {}
              
                  disp_val = str(pdata.get("DISP", pdata.get("DISPONIBILIDAD", ""))).strip().lower()
                  is_reservado = (disp_val == "r")
                  is_vendido = (disp_val in ("v", "r"))      # para Shopify, reservado = agotado
              
                  col_actual = cols[idx % cols_por_fila]
                  with col_actual:
                      with st.container(border=True):
                          if img_url and (img_url.startswith("http") or os.path.exists(img_url)):
                              try:
                                  st.image(img_url, use_container_width=True)
                              except Exception:
                                  st.caption("Sin imagen asignada")
                          else:
                              st.caption("Sin imagen asignada")
                      
                          st.markdown(f"### *{mod_p}*")
                          st.caption(str(pdata.get('NOMBRE') or 'Sin Nombre').strip())
                      
                          if is_reservado:
                              try:
                                  from ventas import etiqueta_reserva as _etiqueta_reserva
                                  _quien_res = _etiqueta_reserva(supabase, mod_p)
                              except Exception:
                                  _quien_res = "por una cotización"
                              st.warning(f"Estado: RESERVADO {_quien_res}")
                          elif is_vendido:
                              st.error("Estado: VENDIDO")
                          else:
                              st.success("Estado: DISPONIBLE")
                          
                          st.write(f"*Ubicación:* {pdata.get('UBICACION', 'N/A')}")
                          st.write(f"*Categoría:* {pdata.get('CATEGORIA', 'N/A')}")
                          st.write(f"*Redondeo Showroom:* ${to_float(pdata.get('REDONDEO_SHOWROOM', 0)):,.2f}")
                      
                          # Descripción, cédula curatorial y medidas: las ve cualquier rol (sin costos)
                          with st.expander("Descripción, cédula y medidas"):
                              _desc = str(pdata.get("DESCRIPCION", "") or "").strip()
                              _ced = str(pdata.get("CEDULA_CURATORIAL", "") or "").strip()
                              st.markdown("**Descripción**")
                              st.write(_desc or "—")
                              st.markdown("**Cédula curatorial**")
                              st.write(_ced or "—")
                              _med = [f"{etq} {pdata.get(c)}" for c, etq in (("ALTO", "Alto"), ("LARGO", "Largo"), ("ANCHO_PROF", "Ancho/Prof.")) if str(pdata.get(c, "") or "").strip()]
                              st.markdown("**Medidas**")
                              st.write((" · ".join(_med) + " cm") if _med else "—")

                          url_drive = f"https://drive.google.com/drive/folders/{ID_MASTER_FOTOS}?q={mod_p}"
                          st.link_button("Fotos en Google Drive", url_drive, use_container_width=True, key=f"drive_btn_{mod_p}_{idx}")
                      
                          if puede(user, "editar_ficha"):
                              if st.button("Editar Ficha Completa", use_container_width=True, key=f"btn_edit_{mod_p}_{idx}"):
                                  modal_editar_completo(mod_p, pdata)
                          if puede(user, "shopify_botones"):
                              if st.button("Sincronizar con Shopify", use_container_width=True, key=f"btn_sync_{mod_p}_{idx}",
                                           help="Revisa este producto contra Shopify (nombre, descripción, precio) y su disponibilidad, y copia lo que haya cambiado hacia donde corresponda."):
                                  with st.spinner("Sincronizando con Shopify..."):
                                      est_s, msg_s = sincronizar_ficha(supabase, mod_p, is_vendido)
                                  if est_s == "ok":
                                      st.success(msg_s)
                                  elif est_s == "no_existe":
                                      st.info("Este producto aún no está en Shopify: usa «Crear borrador en Shopify».")
                                  else:
                                      st.warning(msg_s)
                              if st.button("Crear borrador en Shopify", use_container_width=True, key=f"btn_shop_{mod_p}_{idx}"):
                                  with st.spinner("Enviando a Shopify..."):
                                      ok, mensaje, url_shop = crear_borrador_shopify(mod_p, pdata)
                                  if ok:
                                      st.success(mensaje)
                                      registrar_envio(supabase, mod_p, pdata, ["nombre", "desc", "precio"])
                                  else:
                                      st.warning(mensaje)
                                  if url_shop:
                                      st.link_button("Abrir en Shopify", url_shop, use_container_width=True, key=f"lnk_shop_{mod_p}_{idx}")


              st.divider()
              c_bot_info, c_bot_pag = st.columns([2, 1])
              with c_bot_info:
                  st.caption(f"Página {pagina + 1} de {total_paginas}")
              with c_bot_pag:
                  cp1, cp2 = st.columns(2)
                  with cp1:
                      if st.button("Anterior", disabled=(pagina == 0), key="btn_prev_bot", use_container_width=True):
                          st.session_state["pagina_actual"] -= 1
                          st.rerun()
                  with cp2:
                      if st.button("Siguiente ", disabled=(pagina >= total_paginas - 1), key="btn_next_bot", use_container_width=True):
                          st.session_state["pagina_actual"] += 1
                          st.rerun()
          else:
              st.warning(f"No se encontraron coincidencias para '{busqueda}'.")
      else:
          st.info("Escribe un modelo o palabra clave para buscar productos.")
      
# TAB 2: ALTA MANUAL
if tab2 is not None:
   with tab2:
      if puede(user, "alta_manual"):
          render_formulario_producto()
      else:
          st.info("Tu rol no tiene permisos para dar de alta productos.")


# TAB 3: SCRIPTS
if tab3 is not None:
   with tab3:
      if puede(user, "herramientas"):
          st.header("Ejecución de Scripts y Carga Automática")
          col_s1, col_s2, col_s3 = st.columns(3)
          with col_s1:
              with st.container(border=True):
                  st.subheader("Cargar Excel")
                  if st.button("Ejecutar cargar_excel.py", use_container_width=True, type="primary"):
                      with st.spinner("Procesando Excel..."):
                          ejecutar_script_python("cargar_excel.py")
          with col_s2:
              with st.container(border=True):
                  st.subheader("Extraer Imágenes")
                  if st.button("Ejecutar extraer_imagenes.py", use_container_width=True):
                      with st.spinner("Procesando fotos..."):
                          ejecutar_script_python("extraer_imagenes.py")
          with col_s3:
              with st.container(border=True):
                  st.subheader("Actualizar Stock")
                  if st.button("Ejecutar actualizar_stock.py", use_container_width=True):
                      with st.spinner("Sincronizando stock..."):
                          ejecutar_script_python("actualizar_stock.py")
          st.divider()
          with st.container(border=True):
              render_traer_descripciones(supabase)
          st.divider()
          with st.container(border=True):
              render_sync_productos(supabase)
          st.divider()
          with st.container(border=True):
              render_ventas_shopify(supabase)
          st.divider()
          with st.container(border=True):
              render_etiquetas(supabase)


# TAB 4: VENTAS
if tab4 is not None:
   with tab4:
       render_ventas(supabase, user)

# CAUDAL: dinero por persona
if tab_caudal is not None:
   with tab_caudal:
       try:
           import caudal
           caudal.render(supabase, user)
       except Exception as ex:
           st.error(f"Caudal no disponible: {ex}")


# TAB 5: SHOPIFY
if tab5 is not None:
   with tab5:
      if puede(user, "shopify_lista"):
          if puede(user, "shopify_botones"):
              try:
                  from disponibilidad import render_diferencias
              except Exception as ex:
                  st.caption(f"Revisión de disponibilidad no disponible: {ex}")
              else:
                  with st.container(border=True):
                      render_diferencias(supabase, user)
              st.divider()
          render_lista_shopify(supabase)
      else:
          st.info("Tu rol no tiene acceso a esta sección.")

      #Para iniciar, en terminal : streamlit run app.py