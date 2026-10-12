import os
import sys
import glob
import json
import re
import math
import unicodedata
import pandas as pd


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
# Los modelos con número menor a este valor se ignoran (archivos muy antiguos).
# Pon 0 si quieres cargarlos todos.
MODELO_MINIMO = 10000

# Cuántos productos se suben por solicitud a Supabase (más alto = más rápido).
TAMANO_LOTE = 200


def cargar_secrets():
    import toml
    path_secrets = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path_secrets):
        secrets = toml.load(path_secrets)
        return secrets.get("SUPABASE_URL"), secrets.get("SUPABASE_KEY")
    print("⚠️ No se encontró .streamlit/secrets.toml.")
    return None, None


def normalizar(texto):
    """MAYÚSCULAS, sin acentos, sin espacios/saltos de línea/guiones bajos.
    'COSTO ADQUISICIÓN' y 'COSTO ADQUISICION' quedan iguales."""
    t = unicodedata.normalize("NFD", str(texto))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[\s_]+", "", t.upper())


def a_numero(val):
    """Convierte a float. Devuelve None si está vacío o es un error (#DIV/0!, texto sin números)."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, (int, float)):
        return float(val)
    limpio = re.sub(r"[^\d.-]", "", str(val).strip())
    try:
        return float(limpio)
    except ValueError:
        return None


def buscar(row_dict, nombres):
    """Devuelve el valor crudo de la primera columna cuyo encabezado coincida con alguno de los nombres."""
    normas = [normalizar(n) for n in nombres]
    for clave, valor in row_dict.items():
        if normalizar(clave) in normas:
            return valor
    return None


def texto(row_dict, nombres, default=""):
    v = buscar(row_dict, nombres)
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp) or hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    s = str(v).strip()
    return default if s.lower() in ("nan", "none", "null", "nat") else s


def numero(row_dict, nombres):
    """Número de la columna o None si está vacío."""
    return a_numero(buscar(row_dict, nombres))


def encontrar_encabezado_y_leer(archivo):
    """Lee el Excel una sola vez, ubica la fila de encabezados (la que dice MODELO) y devuelve (df, col_modelo)."""
    try:
        raw = pd.read_excel(archivo, header=None, engine="calamine")
    except Exception:
        raw = pd.read_excel(archivo, header=None, engine="openpyxl")
    validos = ("MODELO", "CODIGO", "N°MODELO", "MODEL")
    for h in range(min(100, len(raw))):
        fila = raw.iloc[h].tolist()
        if not any(isinstance(v, str) and normalizar(v) in validos for v in fila):
            continue
        # nombres de columna: vacíos -> "Unnamed: i", repetidos -> "NOMBRE.1" (igual que pandas)
        nombres, vistos = [], {}
        for i, v in enumerate(fila):
            nom = f"Unnamed: {i}" if (v is None or (not isinstance(v, str) and pd.isna(v))) else str(v)
            if nom in vistos:
                vistos[nom] += 1
                nom = f"{nom}.{vistos[nom]}"
            else:
                vistos[nom] = 0
            nombres.append(nom)
        df = raw.iloc[h + 1:].copy()
        df.columns = nombres
        col = next(n for n, v in zip(nombres, fila) if isinstance(v, str) and normalizar(v) in validos)
        return df.reset_index(drop=True), col
    return None, None


# Columnas importantes y los nombres con que pueden aparecer en cada Excel.
# Si un archivo trae otro nombre, agrégalo aquí a la lista de esa columna.
COLUMNAS_CLAVE = {
    "NOMBRE": ["NOMBRE", "PRODUCTO"],
    "COSTO ADQUISICION": ["COSTO ADQUISICION", "COSTO ASQUISICION"],
    "OTROS COSTOS 1": ["OTROS COSTOS", "OTROS COSTOS 1", "IOTROS COSTOS 1"],
    "UTILIDAD SHOWROOM": ["UTILIDAD SHOWROOM"],
    "UTILIDAD VIRIATHUS": ["UTILIDAD VIRIATHUS", "UTILIDAD VISITHUS"],
    "TOTAL COSTO": ["TOTAL COSTO"],
    "REDONDEO SHOWROOM": ["REDONDEO SHOWROOM", "REDONDEO SR"],
    "REDONDEO CASA PALACIO": ["PRECIO DE VENTA CASA PLACIO CON IVA REDONDEADO",
                              "PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO"],
    "CATEGORIA": ["CATEGORIA"],
    "CANTIDAD": ["CANT", "CANTIDAD"],
}


def columnas_faltantes(df):
    """Devuelve las columnas clave que NO se encontraron en el archivo."""
    encabezados = {normalizar(c) for c in df.columns}
    faltan = []
    for etiqueta, alias in COLUMNAS_CLAVE.items():
        if not any(normalizar(a) in encabezados for a in alias):
            faltan.append(etiqueta)
    return faltan


# ----------------------------------------------------------------------
# Construcción del registro de un producto a partir de una fila del Excel
# ----------------------------------------------------------------------
def construir_datos(row_dict, modelo):
    # ---- Producto ----
    disp_str = texto(row_dict, ["DISPONIBILIDAD", "DISP"]).lower()
    disponibilidad = "v" if disp_str.startswith("v") else ""

    # ---- Costos (todo viene del Excel) ----
    costo_registrado = numero(row_dict, ["COSTO REGISTRADO"]) or 0.0
    c_adq = numero(row_dict, ["COSTO ADQUISICION", "COSTO ASQUISICION"]) or 0.0
    c_rest = numero(row_dict, ["COSTO RESTAURO"]) or 0.0
    c_otros = numero(row_dict, ["OTROS COSTOS", "OTROS COSTOS 1", "IOTROS COSTOS 1"]) or 0.0
    aportacion = numero(row_dict, ["APORTACION GASTOS"]) or 0.0

    utilidad_sh = numero(row_dict, ["UTILIDAD SHOWROOM"]) or 0.0
    util_vir_xl = numero(row_dict, ["UTILIDAD VIRIATHUS", "UTILIDAD VISITHUS"])

    # En los Excel no hay columna "COSTO FLETES". "OTROS COSTOS 2" a veces actúa como flete
    # (UTILIDAD VIRIATHUS = (OC2 + UTIL_SR) * factor - OC2) y a veces no
    # (UTILIDAD VIRIATHUS = UTIL_SR * factor). Se decide fila por fila cuál reproduce
    # un factor "redondo" (1.4, 1.3...). Si no es flete, ese monto se guarda como costo financiero.
    otros2 = numero(row_dict, ["OTROS COSTOS 2"]) or 0.0
    flete_col = numero(row_dict, ["COSTO FLETES"])
    c_fin = (numero(row_dict, ["COSTO FINANCIERO"]) or 0.0) + (numero(row_dict, ["INTERESES Y/O COMISIONES"]) or 0.0)

    def factor_para(flete):
        base = flete + utilidad_sh
        if util_vir_xl is None or base <= 0:
            return None
        f = (util_vir_xl + flete) / base
        if 1.0 <= f <= 3.0 and abs(f - round(f, 2)) < 0.005:
            return round(f, 2)
        return None

    factor = numero(row_dict, ["FACTOR VIRIATHUS"])
    if flete_col is not None:
        c_fletes = flete_col
        c_fin += otros2
    else:
        c_fletes = 0.0
        candidatos = []
        f_con = factor_para(otros2) if otros2 > 0 else None
        f_sin = factor_para(0.0)
        if f_con is not None:
            candidatos.append((abs(f_con - 1.4), otros2, f_con))
        if f_sin is not None:
            candidatos.append((abs(f_sin - 1.4), 0.0, f_sin))
        if candidatos:
            _, c_fletes, f_elegido = min(candidatos)
            if factor is None:
                factor = f_elegido
        c_fin += otros2 - c_fletes
    if factor is None:
        factor = 1.4

    total_costo = numero(row_dict, ["TOTAL COSTO"])
    if total_costo is None:
        total_costo = c_adq + c_fin + c_rest + c_fletes + c_otros
    if total_costo == 0 and costo_registrado > 0:
        total_costo = costo_registrado

    # ---- Showroom ----
    utilidad_viriathus = util_vir_xl if util_vir_xl is not None else (c_fletes + utilidad_sh) * factor - c_fletes
    ptv_sin_iva = numero(row_dict, ["PRECIO TOTAL VIRIATHUS SIN IVA"])
    if ptv_sin_iva is None:
        ptv_sin_iva = total_costo + utilidad_viriathus

    psci = numero(row_dict, ["PRECIO SIN COMIS INTERIORISTAS", "PRECIO SIN COMISION INTERIORISTA", "PRECIO SIN COMISION DE INTERIORISTA"])
    if psci is None:
        psci = total_costo + utilidad_sh
    pci = numero(row_dict, ["PRECIO COMISION INTERIORISTA 10%", "PRECIO COMISION INTERIORISTA 15%", "PRECIO CON COMISION INTERIORISTA 15%", "PRECIO COMISION INTERIORISTA 15% Y GRIS CLARO E 10%", "PRECIO COMISION INTERIORISTA"])
    if pci is None:
        # Si el Excel no trae el valor, se estima con la comisión que indique el encabezado (15% o 10%)
        div = 0.85 if any("15%" in str(k) for k in row_dict) else 0.9
        pci = psci / div if psci > 0 else 0.0
    iva_sh = numero(row_dict, ["IVA"])
    if iva_sh is None:
        iva_sh = pci * 0.16
    ps_con_iva = numero(row_dict, ["PRECIO SHOWROOM CON IVA", "PRECIO SHOW ROOM CON IVA"])
    if ps_con_iva is None:
        ps_con_iva = pci + iva_sh

    # REDONDEO SHOWROOM: es manual en el Excel. Se lee tal cual; si está vacío queda en 0.
    redondeo_sh = numero(row_dict, ["REDONDEO SHOWROOM", "REDONDEO SR"]) or 0.0

    cisi = numero(row_dict, ["COMISION INTERIORISTA A SIN IVA", "COMISION INTERIORISTA SIN IVA"])
    if cisi is None:
        cisi = pci - psci
    costo_mas_uv = numero(row_dict, ["COSTO + UTILIDAD VIRIATHUS", "COSTO + UTILIDAD VIEIATHUS", "VIRIATHUS (costo + utilidad)", "VIRIATHUS (costo y utilidad)"])
    if costo_mas_uv is None:
        costo_mas_uv = pci - cisi
    uv_sin_iva = numero(row_dict, ["UTILIDAD VIRIATHUS SIN IVA"])
    if uv_sin_iva is None:
        uv_sin_iva = pci - cisi - total_costo
    uv_con_com = numero(row_dict, ["UTILIDAD VIRIATHUS CON COMISION DE INTERIORISTA"])
    if uv_con_com is None:
        uv_con_com = cisi + uv_sin_iva

    # ---- Casa Palacio ----
    pvcp_iva = numero(row_dict, [
        "PRECIO DE VENTA CASA PALACIO CON IVA (I/0.65/1.16)",
        "PRECIO DE VENTA CASA PLACIO CON IVA (I/0.65/1.16)",
        "PRECIO DE VENTA CASA PALACIO CON IVA (I/0.65*1.16)",
        "PRECIO DE VENTA CASA PLACIO CON IVA (I/0.65*1.16)",
    ])
    if pvcp_iva is None:
        pvcp_iva = (ptv_sin_iva / 0.65) * 1.16 if ptv_sin_iva > 0 else 0.0

    # OJO: en tu Excel el encabezado dice "PLACIO" (con el error de dedo). Se aceptan ambos.
    pvcp_red = numero(row_dict, [
        "PRECIO DE VENTA CASA PLACIO CON IVA REDONDEADO",
        "PRECIO DE VENTA CASA PALACIO CON IVA REDONDEADO",
    ]) or 0.0

    if pvcp_red > 0:
        cp_sin_iva = pvcp_red / 1.16
        cp_utilidad = cp_sin_iva - ptv_sin_iva
        cp_pct = cp_utilidad / cp_sin_iva * 100.0
    else:
        cp_sin_iva = cp_utilidad = cp_pct = 0.0

    cantidad = int(numero(row_dict, ["CANT", "CANTIDAD"]) or 1)
    total_cp = pvcp_red * cantidad

    return {
        "MODELO": modelo,
        "NOMBRE": texto(row_dict, ["NOMBRE", "PRODUCTO"]),
        "DESCRIPCION": texto(row_dict, ["DESCRIPCION", "FICHA TECNICA"]),
        "CEDULA_CURATORIAL": texto(row_dict, ["CEDULA CURATORIAL", "CEDULA"]),
        "ALTO": texto(row_dict, ["ALTO", "ALTO METROS", "ALTO CM"]),
        "LARGO": texto(row_dict, ["LARGO", "LARGO METROS", "LARGO CM"]),
        "ANCHO_PROF": texto(row_dict, ["ANCHO PROF", "PROF", "PROF METROS", "PROF CM", "ANCHO"]),
        "PESO": texto(row_dict, ["PESO KG", "PESO"]),
        "DISPONIBILIDAD": disponibilidad,
        "DISP": disponibilidad,
        "UBICACION": texto(row_dict, ["UBICACION"], "1 - Showroom"),
        "CATEGORIA": texto(row_dict, ["CATEGORIA"], "1 - Muebles"),
        "SUBCATEGORÍA": texto(row_dict, ["SUBCATEGORIA"]),
        "ETIQUETAS": texto(row_dict, ["ETIQUETAS"]),
        "NUMERO_PEDIDO": texto(row_dict, ["NUMERO PEDIDO", "PEDIDO"]),
        "FECHA_PEDIDO": texto(row_dict, ["FECHA PEDIDO"]),
        "VENDEDOR": texto(row_dict, ["VENDEDOR"]),
        "FACTURA": texto(row_dict, ["FACTURA"]),
        "FECHA_FACTURA": texto(row_dict, ["FECHA FACTURA"]),
        "STATUS_PAGO_ENTREGA": texto(row_dict, ["STATUS PAGO ENTREGA", "STATUS"]),
        "PROVEEDOR_COMPRA": texto(row_dict, ["PROVEEDOR COMPRA", "PROVEEDOR"]),
        "FORMA_PAGO_COMPRA": texto(row_dict, ["FORMA PAGO COMPRA"], "1 - EFECTIVO"),
        "QUIEN_PAGO_COMPRA": texto(row_dict, ["QUIEN PAGO COMPRA"], "1 - CAJA"),
        "FECHA_PAGO_COMPRA": texto(row_dict, ["FECHA PAGO COMPRA"]),
        "NOTAS_FORMA_PAGO_COMPRA": texto(row_dict, ["NOTAS FORMA PAGO COMPRA"]),
        "FECHA_ADQUISICION": texto(row_dict, ["FECHA ADQUISICION", "FECHA DE ADQUISION", "FECHA DE ADQUISICION"]),
        "REQUERIMIENTOS": texto(row_dict, ["REQUERIMIENTOS"]),
        "RESTAURADORES": [{
            "prov": texto(row_dict, ["PROVEEDOR RESTAURO"]),
            "fp": texto(row_dict, ["FORMA PAGO RESTAURO"], "1 - EFECTIVO"),
            "qp": texto(row_dict, ["QUIEN PAGO RESTAURO"], "1 - CAJA"),
            "fecha": texto(row_dict, ["FECHA PAGO RESTAURO"]),
            "notas": texto(row_dict, ["NOTAS FORMA PAGO RESTAURO"]),
        }],
        "COSTO_REGISTRADO": costo_registrado,
        "COSTO_ADQUISICION": c_adq,
        "COSTO_FINANCIERO": c_fin,
        "COSTO_RESTAURO": c_rest,
        "COSTO_FLETES": c_fletes,
        "OTROS_COSTOS": c_otros,
        "APORTACION_GASTOS": aportacion,
        "TOTAL_COSTO": total_costo,
        "FACTOR_VIRIATHUS": factor,
        "UTILIDAD_SHOWROOM": utilidad_sh,
        "UTILIDAD_VIRIATHUS": utilidad_viriathus,
        "PRECIO_TOTAL_VIRIATHUS_SIN_IVA": ptv_sin_iva,
        "PRECIO_SIN_COMISION_INTERIORISTA": psci,
        "PRECIO_COMISION_INTERIORISTA": pci,
        "IVA_SHOWROOM": iva_sh,
        "PRECIO_SHOWROOM_CON_IVA": ps_con_iva,
        "REDONDEO_SHOWROOM": redondeo_sh,
        "COMISION_INTERIORISTA_SIN_IVA": cisi,
        "COSTO_MAS_UTILIDAD_VIRIATHUS": costo_mas_uv,
        "UTILIDAD_VIRIATHUS_SIN_IVA": uv_sin_iva,
        "UTILIDAD_VIRIATHUS_CON_COMISION": uv_con_com,
        "PRECIO_CP_CON_IVA_CALCULADO": pvcp_iva,
        "CP_PRECIO_REDONDEADO": pvcp_red,
        "CP_PRECIO_SIN_IVA": cp_sin_iva,
        "CP_UTILIDAD": cp_utilidad,
        "CP_PORCENTAJE_UTILIDAD": cp_pct,
        "CANTIDAD": cantidad,
        "TOTAL_CP": total_cp,
    }


def filas_del_excel(archivo):
    """Genera (modelo, datos_dict) por cada producto del archivo."""
    df, col_modelo = encontrar_encabezado_y_leer(archivo)
    if df is None or not col_modelo:
        return
    df = df.dropna(subset=[col_modelo])
    faltan = columnas_faltantes(df)
    if faltan:
        print(f"\n   ⚠️ COLUMNAS NO ENCONTRADAS en este archivo (se guardarán vacías o en 0): {', '.join(faltan)}")
        print(f"   Encabezados que sí trae: {[str(c) for c in df.columns if not str(c).startswith('Unnamed')]}")
    for _, row in df.iterrows():
        raw_mod = str(row[col_modelo]).strip()
        if not raw_mod or raw_mod.lower() in ("nan", "none", "null", "modelo"):
            continue
        # Filas que no son un producto (rangos de libros, notas largas con saltos de línea) se omiten
        if "\n" in raw_mod or len(raw_mod) > 15:
            print(f"\n   ⏭️ Fila omitida (modelo no válido): {raw_mod[:40]!r}", end="")
            continue
        modelo = raw_mod[:-2] if raw_mod.endswith(".0") else raw_mod
        try:
            if float(modelo) < MODELO_MINIMO:
                continue
        except ValueError:
            pass
        yield modelo, construir_datos(row.to_dict(), modelo)


# ----------------------------------------------------------------------
# Carga a Supabase
# ----------------------------------------------------------------------
def consolidar_e_insertar_supabase(dry_run=False):
    carpeta = "inventarios_excel"
    archivos = [a for a in glob.glob(os.path.join(carpeta, "*.xlsx")) if not os.path.basename(a).startswith("~$")]
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en '{carpeta}'.")
        return

    supabase = None
    if not dry_run:
        from supabase import create_client
        url, key = cargar_secrets()
        if not url or not key:
            print("❌ Error: Faltan SUPABASE_URL o SUPABASE_KEY")
            return
        supabase = create_client(url, key)

    print(f"📦 Procesando {len(archivos)} archivo(s)" + (" [PRUEBA, no se sube nada]" if dry_run else "") + "...")
    total = 0
    for i, archivo in enumerate(archivos, 1):
        nombre = os.path.basename(archivo)
        print(f"[{i}/{len(archivos)}] {nombre}...", end="", flush=True)
        try:
            n = 0
            lote = {}   # MODELO -> payload (si un modelo se repite en el lote, queda el último)

            def subir_lote():
                nonlocal n
                if not lote:
                    return
                filas = list(lote.values())
                try:
                    supabase.table("productos").upsert(filas, on_conflict="MODELO").execute()
                    n += len(filas)
                except Exception as ex_lote:
                    # Si falla el lote, se reintenta uno por uno para saber cuál es el problema
                    print(f"\n   ⚠️ Falló un lote ({ex_lote}); reintentando uno por uno...")
                    for fila in filas:
                        try:
                            supabase.table("productos").upsert(fila, on_conflict="MODELO").execute()
                            n += 1
                        except Exception as ex_db:
                            print(f"   ❌ Error insertando modelo {fila['MODELO']}: {ex_db}")
                lote.clear()

            for modelo, datos in filas_del_excel(archivo):
                if dry_run:
                    if n < 3:
                        print(f"\n   {modelo}: total_costo={datos['TOTAL_COSTO']:.2f} factor={datos['FACTOR_VIRIATHUS']} "
                              f"redondeo_sh={datos['REDONDEO_SHOWROOM']:.2f} cp_redondeado={datos['CP_PRECIO_REDONDEADO']:.2f}")
                    n += 1
                    continue
                # Solo se envía MODELO + datos_json. RUTA_IMAGEN no se toca.
                lote[modelo] = {"MODELO": modelo, "datos_json": json.dumps(datos, ensure_ascii=False)}
                if len(lote) >= TAMANO_LOTE:
                    subir_lote()
            subir_lote()
            print(f" -> ✅ ({n} productos)")
            total += n
        except Exception as e:
            print(f" -> ❌ Error leyendo archivo: {e}")
    print(f"\n🎉 Proceso completado. Total: {total}")



def diagnostico():
    """Resume los encabezados de TODOS los Excel en una sola lista corta."""
    import collections
    carpeta = "inventarios_excel"
    archivos = [a for a in glob.glob(os.path.join(carpeta, "*.xlsx")) if not os.path.basename(a).startswith("~$")]
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en '{carpeta}'.")
        return
    palabras = ["COSTO", "UTILIDAD", "REDONDEO", "PRECIO", "TOTAL", "FLETE", "ADQUIS",
                "CATEG", "CANT", "IVA", "COMIS", "OTROS", "FINANC", "RESTAUR", "PROF", "PESO"]
    encabezados = collections.Counter()
    ejemplo = {}
    faltan_por_col = collections.Counter()
    sin_leer = []
    for archivo in archivos:
        try:
            df, col_modelo = encontrar_encabezado_y_leer(archivo)
        except Exception:
            df = None
        if df is None:
            sin_leer.append(os.path.basename(archivo))
            continue
        for c in df.columns:
            if str(c).startswith("Unnamed"):
                continue
            nombre = re.sub(r"\s+", " ", str(c)).strip()
            encabezados[nombre] += 1
            ejemplo.setdefault(nombre, os.path.basename(archivo))
        for f in columnas_faltantes(df):
            faltan_por_col[f] += 1

    print(f"\n=== {len(archivos)} archivos revisados ===")
    if sin_leer:
        print(f"No se pudo leer (sin columna MODELO): {sin_leer}")
    print("\n--- Columnas clave que NO se encontraron (en cuántos archivos) ---")
    for k, v in faltan_por_col.most_common():
        print(f"  {k}: {v} archivos")
    print("\n--- Encabezados relacionados con costos/precios que existen (nombre: nº de archivos) ---")
    for nombre, n in sorted(encabezados.items(), key=lambda x: -x[1]):
        if any(p in normalizar(nombre) for p in palabras):
            print(f"  {nombre}: {n}   (ej. {ejemplo[nombre]})")


if __name__ == "__main__":
    # Para probar sin subir nada:  python cargar_excel.py --prueba
    if "--diagnostico" in sys.argv:
        diagnostico()
    else:
        consolidar_e_insertar_supabase(dry_run="--prueba" in sys.argv)