import streamlit as st
from supabase import create_client
import json
import subprocess
import sys
import os
import re
import math

# Configuración de la página
st.set_page_config(
    page_title="Gestión de Inventario y Catálogo",
    page_icon="📦",
    layout="wide"
)

# ID principal de la carpeta 'master fotos' en Google Drive
ID_MASTER_FOTOS = "1E6tlqi-W95qupX2Psi6XtgoHPpJo1ZEv"

# Cargar credenciales de Supabase
url = st.secrets["SUPABASE_URL"]
key = st.secrets["SUPABASE_KEY"]

@st.cache_resource
def init_supabase():
    return create_client(url, key)

supabase = init_supabase()

# --- AUTENTICACIÓN Y ROLES ---
USUARIOS = {
    "admin": {"password": "admin123", "role": "Administrador", "nombre": "Admin Principal"},
    "almacen": {"password": "almacen123", "role": "Inventario", "nombre": "Encargado de Almacén"},
    "vendedor": {"password": "vendedor123", "role": "Vendedor", "nombre": "Ejecutivo de Ventas"}
}

def login():
    st.title("🔒 Acceso al Sistema de Inventario")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        username = st.text_input("Usuario", key="login_user")
        password = st.text_input("Contraseña", type="password", key="login_pass")
        btn_login = st.button("Iniciar Sesión", use_container_width=True, key="login_btn")
        
        if btn_login:
            if username in USUARIOS and USUARIOS[username]["password"] == password:
                st.session_state["logged_in"] = True
                st.session_state["user_info"] = USUARIOS[username]
                st.success(f"Bienvenido, {USUARIOS[username]['nombre']}")
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos")

if "logged_in" not in st.session_state or not st.session_state["logged_in"]:
    login()
    st.stop()

user = st.session_state["user_info"]

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

def guardar_producto_db(modelo, datos_dict):
    payload = {
        "MODELO": str(modelo).strip(),
        "datos_json": json.dumps(datos_dict)
    }
    supabase.table("productos").upsert(payload, on_conflict="MODELO").execute()

def obtener_siguiente_modelo():
    try:
        offset = 0
        todos_numeros = []
        while True:
            resp = supabase.table("productos").select("MODELO").range(offset, offset + 999).execute()
            filas = resp.data
            if not filas:
                break
            for f in filas:
                m_str = str(f.get("MODELO", "")).strip()
                if m_str:
                    clean_str = m_str.split(".")[0]
                    if clean_str.isdigit():
                        todos_numeros.append(int(clean_str))
            offset += 1000

        if todos_numeros:
            return str(max(todos_numeros) + 1)
        return "1"
    except Exception:
        return "1"

def ejecutar_script_python(nombre_script):
    try:
        resultado = subprocess.run([sys.executable, nombre_script], capture_output=True, text=True)
        if resultado.returncode == 0:
            st.success(f"✅ Se ejecutó '{nombre_script}' con éxito.")
            if resultado.stdout:
                st.code(resultado.stdout)
        else:
            st.error(f"❌ Error al ejecutar '{nombre_script}':")
            st.code(resultado.stderr)
    except Exception as e:
        st.error(f"Error al intentar llamar a {nombre_script}: {e}")

# --- FORMULARIO PRINCIPAL ESTRUCTURADO ---

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
    
    res = supabase.table("productos").upsert(registro_db, on_conflict="MODELO").execute()
    return res



def render_formulario_producto(datos_previos={}, modelo_existente=None):
    is_edit = modelo_existente is not None
    prefix = f"edit_{modelo_existente}" if is_edit else "nuevo_prod"
    siguiente_modelo = obtener_siguiente_modelo() if not is_edit and 'obtener_siguiente_modelo' in globals() else ""
    
    st.header("📦 1. SECCIÓN PRODUCTO")
    col1, col2 = st.columns(2)
    with col1:
        if is_edit:
            modelo = st.text_input("MODELO", value=str(modelo_existente), disabled=True, key=f"{prefix}_modelo")
        else:
            modelo = st.text_input("MODELO *", value=str(datos_previos.get("MODELO", siguiente_modelo)), key=f"{prefix}_modelo").strip()
            
        nombre = st.text_input("NOMBRE", value=str(datos_previos.get("NOMBRE", "")), key=f"{prefix}_nombre")
        st.caption(f"📏 CARACTERES DEL NOMBRE: **{len(nombre)}**")
        
        descripcion = st.text_area("DESCRIPCIÓN / FICHA TÉCNICA", value=str(datos_previos.get("DESCRIPCION", "")), height=100, key=f"{prefix}_desc")
        cedula = st.text_area("CÉDULA CURATORIAL", value=str(datos_previos.get("CEDULA_CURATORIAL", "")), height=100, key=f"{prefix}_cedula")
        st.caption(f"📏 CARACTERES CÉDULA CURATORIAL: **{len(cedula)}**")

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

        disp_opts = ["Disponible (vacío)", "v - VENDIDO"]
        disp_idx = 1 if str(datos_previos.get("DISPONIBILIDAD", "")).lower() == "v" or str(datos_previos.get("DISP", "")).lower() == "v" else 0
        disp_sel = st.selectbox("DISPONIBILIDAD", disp_opts, index=disp_idx, key=f"{prefix}_disp")
        disponibilidad = "v" if "VENDIDO" in disp_sel else ""

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

    st.divider()
    st.header("🛒 2. SECCIÓN VENTA")
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
    st.header("🏷️ 3. SECCIÓN COMPRA")
    pago_opts = ["1 - EFECTIVO", "2 - TRANSFERENCIA"]
    pago_quien_opts = ["1 - CAJA", "2 - ERNESTO", "3 - VIRIATO", "4 - CLAUDIA", "5 - MARY"]

    st.subheader("Datos de Compra")
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        prov_compra = st.text_input("PROVEEDOR DE COMPRA", value=str(datos_previos.get("PROVEEDOR_COMPRA", "")), key=f"{prefix}_provcompra")
        forma_pago_compra = st.selectbox("FORMA DE PAGO A COMPRA", pago_opts, key=f"{prefix}_fp_compra")
        fecha_adquisicion_compra = st.text_input("FECHA ADQUISICIÓN", value=str(datos_previos.get("FECHA_ADQUISICION", "")), placeholder="10/oct/26", key=f"{prefix}_fadq_compra")
    with cc2:
        quien_pago_compra = st.selectbox("QUIÉN PAGÓ (COMPRA)", pago_quien_opts, key=f"{prefix}_qp_compra")
        fecha_pago_compra = st.text_input("FECHA DE PAGO COMPRA", value=str(datos_previos.get("FECHA_PAGO_COMPRA", "")), placeholder="10/oct/26", key=f"{prefix}_fpagocompra")
    with cc3:
        notas_pago_compra = st.text_input("NOTAS FORMA PAGO COMPRA", value=str(datos_previos.get("NOTAS_FORMA_PAGO_COMPRA", "")), key=f"{prefix}_notaspagocompra")
        requerimientos = st.text_input("REQUERIMIENTOS", value=str(datos_previos.get("REQUERIMIENTOS", "")), key=f"{prefix}_req")

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

    num_rest_key = f"num_rest_{prefix}"
    if num_rest_key not in st.session_state:
        st.session_state[num_rest_key] = len(lista_restauradores)

    restauradores_capturados = []
    for idx_r in range(st.session_state[num_rest_key]):
        d_r = lista_restauradores[idx_r] if idx_r < len(lista_restauradores) else {}
        st.markdown(f"**Restaurador #{idx_r + 1}**")
        cr1, cr2, cr3 = st.columns(3)
        with cr1:
            p_rest = st.text_input(f"PROVEEDOR DE RESTAURO #{idx_r + 1}", value=str(d_r.get("prov", "")), key=f"{prefix}_pr_{idx_r}")
            fp_rest = st.selectbox(f"FORMA DE PAGO RESTAURO #{idx_r + 1}", pago_opts, index=0 if "EFECTIVO" in str(d_r.get("fp", "")) else 1, key=f"{prefix}_fpr_{idx_r}")
        with cr2:
            qp_rest = st.selectbox(f"QUIÉN PAGÓ (RESTAURO) #{idx_r + 1}", pago_quien_opts, key=f"{prefix}_qpr_{idx_r}")
            f_rest = st.text_input(f"FECHA DE PAGO RESTAURO #{idx_r + 1}", value=str(d_r.get("fecha", "")), placeholder="10/oct/26", key=f"{prefix}_fpr_fecha_{idx_r}")
        with cr3:
            n_rest = st.text_input(f"NOTAS FORMA PAGO RESTAURO #{idx_r + 1}", value=str(d_r.get("notas", "")), key=f"{prefix}_npr_{idx_r}")

        restauradores_capturados.append({
            "prov": p_rest, "fp": fp_rest, "qp": qp_rest, "fecha": f_rest, "notas": n_rest
        })

    if st.button("➕ Añadir otro restaurador", key=f"{prefix}_btn_add_rest"):
        st.session_state[num_rest_key] += 1
        st.rerun()

    st.divider()
    st.header("💵 4. SECCIÓN COSTOS, SHOWROOM Y CASA PALACIO")

    st.subheader("📥 Registro de Costos Base")
    col_c1, col_c2, col_c3, col_c4 = st.columns(4)
    with col_c1:
        v_costo_reg = st.text_input("COSTO REGISTRADO ($)", value=str(datos_previos.get("COSTO_REGISTRADO", "")), key=f"{prefix}_c_reg")
        v_costo_adq = st.text_input("COSTO ADQUISICION ($)", value=str(datos_previos.get("COSTO_ADQUISICION", "")), key=f"{prefix}_c_adq")
    with col_c2:
        v_costo_fin = st.text_input("COSTO FINANCIERO ($)", value=str(datos_previos.get("COSTO_FINANCIERO", "")), key=f"{prefix}_c_fin")
        v_costo_rest = st.text_input("COSTO RESTAURO ($)", value=str(datos_previos.get("COSTO_RESTAURO", "")), key=f"{prefix}_c_rest")
    with col_c3:
        v_costo_fletes = st.text_input("COSTO FLETES ($)", value=str(datos_previos.get("COSTO_FLETES", "")), key=f"{prefix}_c_fletes")
        v_otros_costos = st.text_input("OTROS COSTOS ($)", value=str(datos_previos.get("OTROS_COSTOS", "")), key=f"{prefix}_c_otros")
    with col_c4:
        v_aportacion_gastos = st.text_input("APORTACION GASTOS ($)", value=str(datos_previos.get("APORTACION_GASTOS", "")), key=f"{prefix}_c_aport")
        v_factor_vir = st.text_input("FACTOR VIRIATHUS", value=str(datos_previos.get("FACTOR_VIRIATHUS", "1.4")), key=f"{prefix}_factor_vir")

    c_adq = to_float(v_costo_adq)
    c_fin = to_float(v_costo_fin)
    c_rest = to_float(v_costo_rest)
    c_fletes = to_float(v_costo_fletes)
    c_otros = to_float(v_otros_costos)
    factor_viriathus = to_float(v_factor_vir, 1.4)
    c_reg = to_float(v_costo_reg)

    calc_total_costo = c_adq + c_fin + c_rest + c_fletes + c_otros
    if calc_total_costo == 0 and c_reg > 0:
        calc_total_costo = c_reg

    v_total_costo_input = st.text_input("TOTAL COSTO ($)", value=str(datos_previos.get("TOTAL_COSTO", f"{calc_total_costo:.2f}")), key=f"{prefix}_tot_costo")
    st.caption("ℹ️ *Automático: COSTO DE ADQUISICION + COSTO FINANCIERO + COSTO RESTAURO + COSTO FLETES + OTROS COSTOS*")
    total_costo = to_float(v_total_costo_input, calc_total_costo)

    st.divider()
    col_show, col_cp = st.columns(2)

    with col_show:
        st.subheader("🏛️ SHOWROOM / VIRIATHUS")
        v_utilidad_sh = st.text_input("UTILIDAD SHOWROOM ($)", value=str(datos_previos.get("UTILIDAD_SHOWROOM", "")), key=f"{prefix}_util_sh")
        utilidad_sh = to_float(v_utilidad_sh)

        calc_uv = ((c_fletes + utilidad_sh) * factor_viriathus) - c_fletes
        v_uv = st.text_input("UTILIDAD VIRIATHUS ($)", value=str(datos_previos.get("UTILIDAD_VIRIATHUS", f"{calc_uv:.2f}")), key=f"{prefix}_uv")
        st.caption("ℹ️ *Automático: ((COSTO FLETES + UTILIDAD SHOWROOM) * FACTOR VIRIATHUS) - COSTO FLETES*")
        utilidad_viriathus = to_float(v_uv, calc_uv)

        calc_ptv = total_costo + utilidad_viriathus
        v_ptv = st.text_input("PRECIO TOTAL VIRIATHUS SIN IVA ($)", value=str(datos_previos.get("PRECIO_TOTAL_VIRIATHUS_SIN_IVA", f"{calc_ptv:.2f}")), key=f"{prefix}_ptv")
        st.caption("ℹ️ *Automático: TOTAL COSTO + UTILIDAD VIRIATHUS*")
        ptv_sin_iva = to_float(v_ptv, calc_ptv)

        calc_psci = total_costo + utilidad_sh
        v_psci = st.text_input("PRECIO SIN COMISION INTERIORISTA ($)", value=str(datos_previos.get("PRECIO_SIN_COMISION_INTERIORISTA", f"{calc_psci:.2f}")), key=f"{prefix}_psci")
        st.caption("ℹ️ *Automático: TOTAL COSTO + UTILIDAD SHOWROOM*")
        precio_sin_com_int = to_float(v_psci, calc_psci)

        calc_pci = precio_sin_com_int / 0.9 if precio_sin_com_int > 0 else 0.0
        v_pci = st.text_input("PRECIO COMISION INTERIORISTA ($)", value=str(datos_previos.get("PRECIO_COMISION_INTERIORISTA", f"{calc_pci:.2f}")), key=f"{prefix}_pci")
        st.caption("ℹ️ *Automático: PRECIO SIN COMISION INTERIORISTA / 0.9*")
        precio_com_int = to_float(v_pci, calc_pci)

        calc_iva_sh = precio_com_int * 0.16
        v_iva_sh = st.text_input("IVA SHOWROOM ($)", value=str(datos_previos.get("IVA_SHOWROOM", f"{calc_iva_sh:.2f}")), key=f"{prefix}_iva_sh")
        st.caption("ℹ️ *Automático: PRECIO COMISION INTERIORISTA * 16%*")
        iva_sh = to_float(v_iva_sh, calc_iva_sh)

        calc_ps_con_iva = precio_com_int + iva_sh
        v_ps_con_iva = st.text_input("PRECIO SHOWROOM CON IVA ($)", value=str(datos_previos.get("PRECIO_SHOWROOM_CON_IVA", f"{calc_ps_con_iva:.2f}")), key=f"{prefix}_ps_iva")
        st.caption("ℹ️ *Automático: PRECIO COMISION INTERIORISTA + IVA*")
        precio_sh_con_iva = to_float(v_ps_con_iva, calc_ps_con_iva)

        sug_red_sh = aplicar_regla_redondeo(precio_sh_con_iva) if 'aplicar_regla_redondeo' in globals() else precio_sh_con_iva
        v_red_sh = st.text_input("REDONDEO SHOWROOM ($)", value=str(datos_previos.get("REDONDEO_SHOWROOM", f"{sug_red_sh:.2f}")), key=f"{prefix}_red_sh")
        st.caption("ℹ️ *Automático: Redondeado según regla (<500 entero, >500 en 50/00, >10000 en 00)*")
        redondeo_sh = to_float(v_red_sh, sug_red_sh)

        calc_cisi = precio_com_int - precio_sin_com_int
        v_cisi = st.text_input("COMISION INTERIORISTA SIN IVA ($)", value=str(datos_previos.get("COMISION_INTERIORISTA_SIN_IVA", f"{calc_cisi:.2f}")), key=f"{prefix}_cisi")
        st.caption("ℹ️ *Automático: PRECIO COMISION INTERIORISTA - PRECIO SIN COMISION INTERIORISTA*")

        calc_cuv = precio_com_int - calc_cisi
        v_cuv = st.text_input("COSTO + UTILIDAD VIRIATHUS ($)", value=str(datos_previos.get("COSTO_MAS_UTILIDAD_VIRIATHUS", f"{calc_cuv:.2f}")), key=f"{prefix}_cuv")
        st.caption("ℹ️ *Automático: PRECIO COMISION INTERIORISTA - COMISION INTERIORISTA SIN IVA*")

        calc_uvsi = precio_com_int - calc_cisi - total_costo
        v_uvsi = st.text_input("UTILIDAD VIRIATHUS SIN IVA ($)", value=str(datos_previos.get("UTILIDAD_VIRIATHUS_SIN_IVA", f"{calc_uvsi:.2f}")), key=f"{prefix}_uvsi")
        st.caption("ℹ️ *Automático: PRECIO COMISION INTERIORISTA - COMISION INTERIORISTA SIN IVA - TOTAL COSTO*")

        calc_uvc = calc_cisi + calc_uvsi
        v_uvc = st.text_input("UTILIDAD VIRIATHUS CON COMISION DE INTERIORISTA ($)", value=str(datos_previos.get("UTILIDAD_VIRIATHUS_CON_COMISION", f"{calc_uvc:.2f}")), key=f"{prefix}_uvc")
        st.caption("ℹ️ *Automático: COMISION INTERIORISTA SIN IVA + UTILIDAD VIRIATHUS SIN IVA*")

    with col_cp:
        st.subheader("🏰 CASA PALACIO / C/P")
        calc_pvcp_iva = (ptv_sin_iva / 0.65) * 1.16 if ptv_sin_iva > 0 else 0.0
        v_pvcp_iva = st.text_input("PRECIO DE VENTA CASA PALACIO CON IVA (I/0.65/1.16) ($)", value=str(datos_previos.get("PRECIO_CP_CON_IVA_CALCULADO", f"{calc_pvcp_iva:.2f}")), key=f"{prefix}_pvcp_iva")
        st.caption("ℹ️ *Automático: PRECIO TOTAL VIRIATHUS SIN IVA / 0.65 * 1.16*")
        pvcp_iva = to_float(v_pvcp_iva, calc_pvcp_iva)

        sug_pvcp_red = aplicar_regla_redondeo(pvcp_iva) if 'aplicar_regla_redondeo' in globals() else pvcp_iva
        v_pvcp_red = st.text_input("PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO ($)", value=str(datos_previos.get("CP_PRECIO_REDONDEADO", f"{sug_pvcp_red:.2f}")), key=f"{prefix}_pvcp_red")
        st.caption("ℹ️ *Automático: Redondeado según regla (<500 entero, >500 en 50/00, >10000 en 00)*")
        pvcp_red = to_float(v_pvcp_red, sug_pvcp_red)

        calc_cp_sin_iva = pvcp_red / 1.16 if pvcp_red > 0 else 0.0
        v_cp_sin_iva = st.text_input("C/P PRECIO SIN IVA ($)", value=str(datos_previos.get("CP_PRECIO_SIN_IVA", f"{calc_cp_sin_iva:.2f}")), key=f"{prefix}_cp_sin_iva")
        st.caption("ℹ️ *Automático: PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO / 1.16*")
        cp_sin_iva = to_float(v_cp_sin_iva, calc_cp_sin_iva)

        calc_cp_utilidad = cp_sin_iva - ptv_sin_iva if pvcp_red > 0 else 0.0
        v_cp_utilidad = st.text_input("C/P UTILIDAD ($)", value=str(datos_previos.get("CP_UTILIDAD", f"{calc_cp_utilidad:.2f}")), key=f"{prefix}_cp_util")
        st.caption("ℹ️ *Automático: C/P PRECIO SIN IVA - PRECIO TOTAL VIRIATHUS SIN IVA*")
        cp_utilidad = to_float(v_cp_utilidad, calc_cp_utilidad)

        calc_cp_pct = (cp_utilidad / cp_sin_iva * 100.0) if cp_sin_iva > 0 else 0.0
        v_cp_pct = st.text_input("C/P % DE UTILIDAD (%)", value=str(datos_previos.get("CP_PORCENTAJE_UTILIDAD", f"{calc_cp_pct:.2f}")), key=f"{prefix}_cp_pct")
        st.caption("ℹ️ *Automático: (C/P UTILIDAD / C/P PRECIO SIN IVA) * 100*")

        v_cantidad = st.text_input("CANTIDAD", value=str(datos_previos.get("CANTIDAD", "1")), key=f"{prefix}_cantidad")
        cantidad = int(to_float(v_cantidad, 1.0))

        calc_total_cp = pvcp_red * cantidad
        v_total_cp = st.text_input("TOTAL ($)", value=str(datos_previos.get("TOTAL_CP", f"{calc_total_cp:.2f}")), key=f"{prefix}_total_cp")
        st.caption("ℹ️ *Automático: PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO * CANTIDAD*")

    st.divider()
    if st.button("🚀 Guardar Producto Completo", type="primary", use_container_width=True, key=f"{prefix}_guardar_btn"):
        if not modelo:
            st.error("❌ El campo MODELO es obligatorio.")
            return

        datos_guardar = {
            "MODELO": modelo, "NOMBRE": nombre, "DESCRIPCION": descripcion, "CEDULA_CURATORIAL": cedula,
            "ALTO": alto, "LARGO": largo, "ANCHO_PROF": ancho, "PESO": peso,
            "DISPONIBILIDAD": disponibilidad, "DISP": disponibilidad, "UBICACION": ubicacion,
            "CATEGORIA": categoria, "SUBCATEGORÍA": subcategoria, "ETIQUETAS": etiquetas,
            "NUMERO_PEDIDO": num_pedido, "FECHA_PEDIDO": fecha_pedido, "VENDEDOR": vendedor,
            "FACTURA": factura, "FECHA_FACTURA": fecha_factura, "STATUS_PAGO_ENTREGA": status_pago_entrega,
            "PROVEEDOR_COMPRA": prov_compra, "FORMA_PAGO_COMPRA": forma_pago_compra,
            "QUIEN_PAGO_COMPRA": quien_pago_compra, "FECHA_PAGO_COMPRA": fecha_pago_compra,
            "NOTAS_FORMA_PAGO_COMPRA": notas_pago_compra, "FECHA_ADQUISICION": fecha_adquisicion_compra,
            "REQUERIMIENTOS": requerimientos, "RESTAURADORES": restauradores_capturados,
            "COSTO_REGISTRADO": to_float(v_costo_reg), "COSTO_ADQUISICION": c_adq,
            "COSTO_FINANCIERO": c_fin, "COSTO_RESTAURO": c_rest, "COSTO_FLETES": c_fletes,
            "OTROS_COSTOS": c_otros, "APORTACION_GASTOS": to_float(v_aportacion_gastos),
            "TOTAL_COSTO": total_costo, "FACTOR_VIRIATHUS": factor_viriathus,
            "UTILIDAD_SHOWROOM": utilidad_sh, "UTILIDAD_VIRIATHUS": utilidad_viriathus,
            "PRECIO_TOTAL_VIRIATHUS_SIN_IVA": ptv_sin_iva,
            "PRECIO_SIN_COMISION_INTERIORISTA": to_float(v_psci), "PRECIO_COMISION_INTERIORISTA": to_float(v_pci),
            "IVA_SHOWROOM": to_float(v_iva_sh), "PRECIO_SHOWROOM_CON_IVA": to_float(v_ps_con_iva),
            "REDONDEO_SHOWROOM": to_float(v_red_sh), "COMISION_INTERIORISTA_SIN_IVA": to_float(v_cisi),
            "COSTO_MAS_UTILIDAD_VIRIATHUS": to_float(v_cuv), "UTILIDAD_VIRIATHUS_SIN_IVA": to_float(v_uvsi),
            "UTILIDAD_VIRIATHUS_CON_COMISION": to_float(v_uvc),
            "PRECIO_CP_CON_IVA_CALCULADO": to_float(v_pvcp_iva), "CP_PRECIO_REDONDEADO": to_float(v_pvcp_red),
            "CP_PRECIO_SIN_IVA": to_float(v_cp_sin_iva), "CP_UTILIDAD": to_float(v_cp_utilidad),
            "CP_PORCENTAJE_UTILIDAD": to_float(v_cp_pct), "CANTIDAD": cantidad, "TOTAL_CP": to_float(v_total_cp)
        }

        with st.spinner("Guardando en la base de datos..."):
            try:
                res = guardar_producto_db(modelo, datos_guardar)
                if res.data:
                    st.balloons()
                    st.success(f"🎉 ¡Producto '{modelo}' guardado correctamente en Supabase!")
                else:
                    st.warning("⚠️ Producto enviado, pero verifica si apareció en el listado.")
            except Exception as ex:
                st.error(f"❌ Error al guardar en Supabase: {ex}")


# DIÁLOGO DE EDICIÓN EN MODAL@st.dialog("✏️ Editar Producto Completo", width="large")
def modal_editar_completo(mod_p, pdata):
    st.caption(f"Gestión y edición del modelo: **{mod_p}**")
    
    # --- SECCIÓN DE ACCIONES RÁPIDAS (FOTO Y ELIMINACIÓN) ---
    col_foto, col_del = st.columns(2)
    
    with col_foto:
        with st.expander("🖼️ Cambiar / Subir Foto de Portada"):
            nueva_foto = st.file_uploader("Selecciona una imagen (.jpg, .png)", type=["jpg", "jpeg", "png", "webp"], key=f"uploader_{mod_p}")
            if nueva_foto is not None:
                if st.button("🚀 Subir Imagen a Supabase", use_container_width=True, key=f"btn_upload_img_{mod_p}"):
                    with st.spinner("Subiendo imagen a Supabase Storage..."):
                        try:
                            bytes_img = nueva_foto.read()
                            nombre_remoto = f"{mod_p}.jpg"
                            
                            # Subir al bucket 'imagenes'
                            supabase.storage.from_("imagenes").upload(
                                path=nombre_remoto,
                                file=bytes_img,
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                            
                            # Actualizar URL pública en la tabla de Supabase
                            sb_url = st.secrets.get("SUPABASE_URL", "")
                            url_publica = f"{sb_url}/storage/v1/object/public/imagenes/{nombre_remoto}"
                            supabase.table("productos").update({"RUTA_IMAGEN": url_publica}).eq("MODELO", mod_p).execute()
                            
                            st.success("✅ ¡Foto de portada actualizada!")
                            st.rerun()
                        except Exception as ex_foto:
                            st.error(f"❌ Error al subir la imagen: {ex_foto}")

    with col_del:
        with st.expander("⚠️ Zona de Peligro: Eliminar Producto"):
            st.warning("Esta acción borrará el producto permanentemente de Supabase.")
            confirm_del = st.checkbox("Confirmo que deseo eliminarlo", key=f"check_del_{mod_p}")
            if confirm_del:
                if st.button("🗑️ Eliminar Producto Definitivamente", type="primary", use_container_width=True, key=f"btn_confirm_del_{mod_p}"):
                    with st.spinner("Eliminando producto..."):
                        try:
                            supabase.table("productos").delete().eq("MODELO", mod_p).execute()
                            st.success(f"Producto {mod_p} eliminado.")
                            st.rerun()
                        except Exception as ex_del:
                            st.error(f"❌ Error al eliminar: {ex_del}")

    st.divider()
    
    # Renderizar el formulario con los datos cargados
    render_formulario_producto(pdata, modelo_existente=mod_p)


# --- MENÚ Y NAVEGACIÓN ---
with st.sidebar:
    st.title("📦 Menú Principal")
    st.write(f"**Usuario:** {user['nombre']}")
    st.write(f"**Rol:** `{user['role']}`")
    st.divider()
    
    if st.button("Cerrar Sesión", use_container_width=True, key="btn_logout"):
        st.session_state["logged_in"] = False
        st.rerun()

st.title("🔍 Buscador e Inventario de Productos")
tab1, tab2, tab3 = st.tabs(["🔎 Consultar / Buscar", "➕ Alta Manual", "⚡ Herramientas & Scripts"])

# TAB 1: BÚSQUEDA
with tab1:
    busqueda_raw = st.text_input("Buscar por Modelo:", key="search_bar")
    busqueda = busqueda_raw.strip()
    
    if busqueda:
        if "busqueda_anterior" not in st.session_state or st.session_state["busqueda_anterior"] != busqueda:
            st.session_state["busqueda_anterior"] = busqueda
            st.session_state["pagina_actual"] = 0

        tamano_pagina = 50
        pagina = st.session_state.get("pagina_actual", 0)
        desde = pagina * tamano_pagina
        hasta = desde + tamano_pagina - 1

        with st.spinner("Consultando inventario..."):
            # Sanitizar búsqueda (quitar caracteres que rompan PostgREST)
            busqueda_clean = re.sub(r'[%"\']', '', busqueda)
            
            # Consulta limpia directamente a la columna MODELO
            query = supabase.table("productos").select("*", count="exact")
            query = query.ilike("MODELO", f"%{busqueda_clean}%")
            
            resp = query.range(desde, hasta).execute()
            productos = resp.data
            total_coincidencias = resp.count if resp.count is not None else len(productos)

        if productos:
            total_paginas = max(1, ((total_coincidencias - 1) // tamano_pagina) + 1)
            
            col_info, col_pag = st.columns([2, 1])
            with col_info:
                st.subheader(f"Resultados para \"{busqueda}\" ({total_coincidencias} encontrados):")
                st.caption(f"Mostrando productos *{desde + 1}* a *{min(hasta + 1, total_coincidencias)}* (Página {pagina + 1} de {total_paginas})")
            
            with col_pag:
                c_prev, c_next = st.columns(2)
                with c_prev:
                    if st.button("⬅️ Anterior", disabled=(pagina == 0), key="btn_prev_top", use_container_width=True):
                        st.session_state["pagina_actual"] -= 1
                        st.rerun()
                with c_next:
                    if st.button("Siguiente ➡️", disabled=(pagina >= total_paginas - 1), key="btn_next_top", use_container_width=True):
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
                is_vendido = (disp_val == "v")
                
                col_actual = cols[idx % cols_por_fila]
                with col_actual:
                    with st.container(border=True):
                        if img_url and (img_url.startswith("http") or os.path.exists(img_url)):
                            try:
                                st.image(img_url, use_container_width=True)
                            except Exception:
                                st.caption("🖼️ Sin imagen asignada")
                        else:
                            st.caption("🖼️ Sin imagen asignada")
                        
                        st.markdown(f"### *{mod_p}*")
                        st.write(f"*{pdata.get('NOMBRE', 'Sin Nombre')}*")
                        
                        if is_vendido:
                            st.error("🔴 Estado: VENDIDO")
                        else:
                            st.success("🟢 Estado: DISPONIBLE")
                            
                        st.write(f"📍 *Ubicación:* {pdata.get('UBICACION', 'N/A')}")
                        st.write(f"🏷️ *Categoría:* {pdata.get('CATEGORIA', 'N/A')}")
                        st.write(f"💵 *Redondeo Showroom:* ${to_float(pdata.get('REDONDEO_SHOWROOM', 0)):,.2f}")
                        
                        url_drive = f"https://drive.google.com/drive/folders/{ID_MASTER_FOTOS}?q={mod_p}"
                        st.link_button("📂 Fotos en Google Drive", url_drive, use_container_width=True, key=f"drive_btn_{mod_p}_{idx}")
                        
                        if user["role"] in ["Administrador", "Inventario"]:
                            if st.button("✏️ Editar Ficha Completa", use_container_width=True, key=f"btn_edit_{mod_p}_{idx}"):
                                modal_editar_completo(mod_p, pdata)

            st.divider()
            c_bot_info, c_bot_pag = st.columns([2, 1])
            with c_bot_info:
                st.caption(f"Página {pagina + 1} de {total_paginas}")
            with c_bot_pag:
                cp1, cp2 = st.columns(2)
                with cp1:
                    if st.button("⬅️ Anterior", disabled=(pagina == 0), key="btn_prev_bot", use_container_width=True):
                        st.session_state["pagina_actual"] -= 1
                        st.rerun()
                with cp2:
                    if st.button("Siguiente ➡️", disabled=(pagina >= total_paginas - 1), key="btn_next_bot", use_container_width=True):
                        st.session_state["pagina_actual"] += 1
                        st.rerun()
        else:
            st.warning(f"No se encontraron coincidencias para '{busqueda}'.")
    else:
        st.info("💡 Escribe un modelo o palabra clave para buscar productos.")
        
# TAB 2: ALTA MANUAL
with tab2:
    if user["role"] in ["Administrador", "Inventario"]:
        render_formulario_producto()
    else:
        st.info("Tu rol no tiene permisos para dar de alta productos.")

# TAB 3: SCRIPTS
with tab3:
    if user["role"] in ["Administrador", "Inventario"]:
        st.header("⚡ Ejecución de Scripts y Carga Automática")
        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            with st.container(border=True):
                st.subheader("📊 Cargar Excel")
                if st.button("🚀 Ejecutar cargar_excel.py", use_container_width=True, type="primary"):
                    with st.spinner("Procesando Excel..."):
                        ejecutar_script_python("cargar_excel.py")
        with col_s2:
            with st.container(border=True):
                st.subheader("🖼️ Extraer Imágenes")
                if st.button("🚀 Ejecutar extraer_imagenes.py", use_container_width=True):
                    with st.spinner("Procesando fotos..."):
                        ejecutar_script_python("extraer_imagenes.py")
        with col_s3:
            with st.container(border=True):
                st.subheader("🔄 Actualizar Stock")
                if st.button("🚀 Ejecutar actualizar_stock.py", use_container_width=True):
                    with st.spinner("Sincronizando stock..."):
                        ejecutar_script_python("actualizar_stock.py")


    #Para iniciar, en terminal : streamlit run app.py