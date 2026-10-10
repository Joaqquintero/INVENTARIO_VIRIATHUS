import os
import glob
import json
import re
import math
import pandas as pd
import toml
from supabase import create_client

def cargar_secrets():
    path_secrets = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path_secrets):
        secrets = toml.load(path_secrets)
        return secrets.get("SUPABASE_URL"), secrets.get("SUPABASE_KEY")
    else:
        print("⚠️ No se encontró .streamlit/secrets.toml.")
        return None, None

def limpiar_numero(val, default=0.0):
    if pd.isna(val) or val is None:
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

def obtener_valor_flexible(row_dict, nombres_posibles, default="", es_texto=True):
    for clave, valor in row_dict.items():
        clave_norm = re.sub(r"[\s_]+", "", str(clave).upper())
        for np in nombres_posibles:
            np_norm = re.sub(r"[\s_]+", "", str(np).upper())
            if clave_norm == np_norm:
                if pd.isna(valor) or valor is None:
                    return "" if es_texto else default
                val_str = str(valor).strip()
                if val_str.lower() in ["nan", "none", "null"]:
                    return "" if es_texto else default
                return val_str if es_texto else limpiar_numero(valor, default)
    return "" if es_texto else default

def encontrar_encabezado_y_leer(archivo):
    for h in range(100):
        try:
            try:
                df = pd.read_excel(archivo, header=h, engine="calamine")
            except Exception:
                df = pd.read_excel(archivo, header=h, engine="openpyxl")
            
            for col in df.columns:
                col_clean = str(col).replace("\n", " ").strip().upper()
                if any(k in col_clean for k in ["MODELO", "CODIGO", "N° MODELO", "MODEL"]):
                    return df, col
        except Exception:
            continue
    return None, None

def consolidar_e_insertar_supabase():
    url, key = cargar_secrets()
    if not url or not key:
        print("❌ Error: Faltan SUPABASE_URL o SUPABASE_KEY")
        return

    supabase = create_client(url, key)
    carpeta = "inventarios_excel"
    archivos = glob.glob(os.path.join(carpeta, "*.xlsx"))
    
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en '{carpeta}'.")
        return

    print(f"📦 Procesando {len(archivos)} archivo(s) Excel con lógica de redondeo y fórmulas...")
    total_insertados = 0

    for i, archivo in enumerate(archivos, 1):
        nombre = os.path.basename(archivo)
        print(f"[{i}/{len(archivos)}] Leyendo {nombre}...", end="", flush=True)
        
        try:
            df, col_modelo = encontrar_encabezado_y_leer(archivo)
            if df is None or not col_modelo:
                print(" -> ⚠️ Omitido: No se encontró la columna 'MODELO'.", flush=True)
                continue

            df[col_modelo] = df[col_modelo].ffill()
            df = df.dropna(subset=[col_modelo])
            insertados_archivo = 0

            for _, row in df.iterrows():
                row_dict = row.to_dict()
                raw_mod = str(row[col_modelo]).strip()
                if not raw_mod or raw_mod.lower() in ["nan", "none", "null", "modelo"]:
                    continue

                modelo = raw_mod.split(".")[0].strip() if raw_mod.replace(".", "").isdigit() else raw_mod

                # --- CAMPOS EXTRAÍDOS ---
                nombre_p = obtener_valor_flexible(row_dict, ["NOMBRE", "PRODUCTO"], "", es_texto=True)
                descripcion = obtener_valor_flexible(row_dict, ["DESCRIPCION", "FICHA TECNICA"], "", es_texto=True)
                cedula = obtener_valor_flexible(row_dict, ["CEDULA CURATORIAL", "CEDULA"], "", es_texto=True)
                alto = obtener_valor_flexible(row_dict, ["ALTO"], "", es_texto=True)
                largo = obtener_valor_flexible(row_dict, ["LARGO"], "", es_texto=True)
                ancho = obtener_valor_flexible(row_dict, ["ANCHO PROF", "ANCHO"], "", es_texto=True)
                peso = obtener_valor_flexible(row_dict, ["PESO"], "", es_texto=True)
                
                disp_str = obtener_valor_flexible(row_dict, ["DISPONIBILIDAD", "DISP"], "", es_texto=True).lower()
                disponibilidad = "v" if "v" in disp_str or "vendido" in disp_str else ""
                
                ubicacion = obtener_valor_flexible(row_dict, ["UBICACION"], "1 - Showroom", es_texto=True)
                categoria = obtener_valor_flexible(row_dict, ["CATEGORIA"], "1 - Muebles", es_texto=True)
                subcategoria = obtener_valor_flexible(row_dict, ["SUBCATEGORIA"], "", es_texto=True)
                etiquetas = obtener_valor_flexible(row_dict, ["ETIQUETAS"], "", es_texto=True)

                num_pedido = obtener_valor_flexible(row_dict, ["NUMERO PEDIDO", "PEDIDO"], "", es_texto=True)
                fecha_pedido = obtener_valor_flexible(row_dict, ["FECHA PEDIDO"], "", es_texto=True)
                vendedor = obtener_valor_flexible(row_dict, ["VENDEDOR"], "", es_texto=True)
                factura = obtener_valor_flexible(row_dict, ["FACTURA"], "", es_texto=True)
                fecha_factura = obtener_valor_flexible(row_dict, ["FECHA FACTURA"], "", es_texto=True)
                status_pago_entrega = obtener_valor_flexible(row_dict, ["STATUS PAGO ENTREGA", "STATUS"], "", es_texto=True)

                prov_compra = obtener_valor_flexible(row_dict, ["PROVEEDOR COMPRA", "PROVEEDOR"], "", es_texto=True)
                forma_pago_compra = obtener_valor_flexible(row_dict, ["FORMA PAGO COMPRA"], "1 - EFECTIVO", es_texto=True)
                quien_pago_compra = obtener_valor_flexible(row_dict, ["QUIEN PAGO COMPRA"], "1 - CAJA", es_texto=True)
                fecha_pago_compra = obtener_valor_flexible(row_dict, ["FECHA PAGO COMPRA"], "", es_texto=True)
                fecha_adquisicion = obtener_valor_flexible(row_dict, ["FECHA ADQUISICION", "FECHA ADQUISICIÓN"], "", es_texto=True)
                notas_pago_compra = obtener_valor_flexible(row_dict, ["NOTAS FORMA PAGO COMPRA"], "", es_texto=True)
                requerimientos = obtener_valor_flexible(row_dict, ["REQUERIMIENTOS"], "", es_texto=True)

                prov_restauro = obtener_valor_flexible(row_dict, ["PROVEEDOR RESTAURO"], "", es_texto=True)
                forma_pago_restauro = obtener_valor_flexible(row_dict, ["FORMA PAGO RESTAURO"], "1 - EFECTIVO", es_texto=True)
                quien_pago_restauro = obtener_valor_flexible(row_dict, ["QUIEN PAGO RESTAURO"], "1 - CAJA", es_texto=True)
                fecha_pago_restauro = obtener_valor_flexible(row_dict, ["FECHA PAGO RESTAURO"], "", es_texto=True)
                notas_pago_restauro = obtener_valor_flexible(row_dict, ["NOTAS FORMA PAGO RESTAURO"], "", es_texto=True)

                restauradores = [{
                    "prov": prov_restauro, "fp": forma_pago_restauro,
                    "qp": quien_pago_restauro, "fecha": fecha_pago_restauro,
                    "notas": notas_pago_restauro
                }]

                # --- COSTOS & FÓRMULAS ---
                costo_registrado = obtener_valor_flexible(row_dict, ["COSTO REGISTRADO"], 0.0, es_texto=False)
                c_adq = obtener_valor_flexible(row_dict, ["COSTO ADQUISICION"], 0.0, es_texto=False)
                c_fin_1 = obtener_valor_flexible(row_dict, ["COSTO FINANCIERO"], 0.0, es_texto=False)
                c_fin_2 = obtener_valor_flexible(row_dict, ["INTERESES Y/O COMISIONES", "OTROS COSTOS 2"], 0.0, es_texto=False)
                c_fin = c_fin_1 + c_fin_2

                c_rest = obtener_valor_flexible(row_dict, ["COSTO RESTAURO"], 0.0, es_texto=False)
                c_fletes = obtener_valor_flexible(row_dict, ["COSTO FLETES"], 0.0, es_texto=False)
                c_otros = obtener_valor_flexible(row_dict, ["OTROS COSTOS", "OTROS COSTOS 1"], 0.0, es_texto=False)
                aportacion_gastos = obtener_valor_flexible(row_dict, ["APORTACION GASTOS"], 0.0, es_texto=False)

                total_costo = c_adq + c_fin + c_rest + c_fletes + c_otros
                if total_costo == 0 and costo_registrado > 0:
                    total_costo = costo_registrado

                factor_viriathus = obtener_valor_flexible(row_dict, ["FACTOR VIRIATHUS"], 1.4, es_texto=False)
                utilidad_sh = obtener_valor_flexible(row_dict, ["UTILIDAD SHOWROOM"], 0.0, es_texto=False)

                utilidad_viriathus = ((c_fletes + utilidad_sh) * factor_viriathus) - c_fletes
                ptv_sin_iva = total_costo + utilidad_viriathus

                precio_sin_com_int = total_costo + utilidad_sh
                precio_com_int = precio_sin_com_int / 0.9 if precio_sin_com_int > 0 else 0.0
                iva_sh = precio_com_int * 0.16
                precio_sh_con_iva = precio_com_int + iva_sh
                redondeo_sh = aplicar_regla_redondeo(precio_sh_con_iva)

                cisi = precio_com_int - precio_sin_com_int
                costo_mas_uv = precio_com_int - cisi
                uv_sin_iva = precio_com_int - cisi - total_costo
                uv_con_com = cisi + uv_sin_iva

                pvcp_iva = (ptv_sin_iva / 0.65) * 1.16 if ptv_sin_iva > 0 else 0.0
                pvcp_red = aplicar_regla_redondeo(pvcp_iva)
                cp_sin_iva = pvcp_red / 1.16 if pvcp_red > 0 else 0.0
                cp_utilidad = cp_sin_iva - ptv_sin_iva if pvcp_red > 0 else 0.0
                cp_pct = (cp_utilidad / cp_sin_iva * 100.0) if cp_sin_iva > 0 else 0.0
                cantidad = int(obtener_valor_flexible(row_dict, ["CANTIDAD"], 1, es_texto=False))
                total_cp = pvcp_red * cantidad

                datos_dict = {
                    "MODELO": modelo, "NOMBRE": nombre_p, "DESCRIPCION": descripcion, "CEDULA_CURATORIAL": cedula,
                    "ALTO": alto, "LARGO": largo, "ANCHO_PROF": ancho, "PESO": peso,
                    "DISPONIBILIDAD": disponibilidad, "DISP": disponibilidad, "UBICACION": ubicacion,
                    "CATEGORIA": categoria, "SUBCATEGORÍA": subcategoria, "ETIQUETAS": etiquetas,
                    "NUMERO_PEDIDO": num_pedido, "FECHA_PEDIDO": fecha_pedido, "VENDEDOR": vendedor,
                    "FACTURA": factura, "FECHA_FACTURA": fecha_factura, "STATUS_PAGO_ENTREGA": status_pago_entrega,
                    "PROVEEDOR_COMPRA": prov_compra, "FORMA_PAGO_COMPRA": forma_pago_compra,
                    "QUIEN_PAGO_COMPRA": quien_pago_compra, "FECHA_PAGO_COMPRA": fecha_pago_compra,
                    "NOTAS_FORMA_PAGO_COMPRA": notas_pago_compra, "FECHA_ADQUISICION": fecha_adquisicion,
                    "REQUERIMIENTOS": requerimientos, "RESTAURADORES": restauradores,
                    "COSTO_REGISTRADO": costo_registrado, "COSTO_ADQUISICION": c_adq,
                    "COSTO_FINANCIERO": c_fin, "COSTO_RESTAURO": c_rest, "COSTO_FLETES": c_fletes,
                    "OTROS_COSTOS": c_otros, "APORTACION_GASTOS": aportacion_gastos,
                    "TOTAL_COSTO": total_costo, "FACTOR_VIRIATHUS": factor_viriathus,
                    "UTILIDAD_SHOWROOM": utilidad_sh, "UTILIDAD_VIRIATHUS": utilidad_viriathus,
                    "PRECIO_TOTAL_VIRIATHUS_SIN_IVA": ptv_sin_iva,
                    "PRECIO_SIN_COMISION_INTERIORISTA": precio_sin_com_int, "PRECIO_COMISION_INTERIORISTA": precio_com_int,
                    "IVA_SHOWROOM": iva_sh, "PRECIO_SHOWROOM_CON_IVA": precio_sh_con_iva,
                    "REDONDEO_SHOWROOM": redondeo_sh, "COMISION_INTERIORISTA_SIN_IVA": cisi,
                    "COSTO_MAS_UTILIDAD_VIRIATHUS": costo_mas_uv, "UTILIDAD_VIRIATHUS_SIN_IVA": uv_sin_iva,
                    "UTILIDAD_VIRIATHUS_CON_COMISION": uv_con_com,
                    "PRECIO_CP_CON_IVA_CALCULADO": pvcp_iva, "CP_PRECIO_REDONDEADO": pvcp_red,
                    "CP_PRECIO_SIN_IVA": cp_sin_iva, "CP_UTILIDAD": cp_utilidad,
                    "CP_PORCENTAJE_UTILIDAD": cp_pct, "CANTIDAD": cantidad, "TOTAL_CP": total_cp
                }

                payload = {"MODELO": modelo, "datos_json": json.dumps(datos_dict)}
                try:
                    supabase.table("productos").upsert(payload, on_conflict="MODELO").execute()
                    insertados_archivo += 1
                except Exception as ex_db:
                    print(f"\n   ❌ Error insertando modelo {modelo}: {ex_db}")

            print(f" -> ✅ ({insertados_archivo} productos procesados)", flush=True)
            total_insertados += insertados_archivo

        except Exception as e:
            print(f" -> ❌ Error leyendo archivo: {e}", flush=True)

    print(f"\n🎉 ¡Proceso completado con éxito! Total: {total_insertados}")

if __name__ == "__main__":
    consolidar_e_insertar_supabase()


