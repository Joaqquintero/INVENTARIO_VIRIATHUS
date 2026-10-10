import os
import glob
import re
import io
import warnings
import pandas as pd
import toml
from openpyxl import load_workbook
from PIL import Image
from supabase import create_client

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

def cargar_secrets():
    path_secrets = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path_secrets):
        secrets = toml.load(path_secrets)
        return secrets.get("SUPABASE_URL"), secrets.get("SUPABASE_KEY")
    else:
        print("⚠️ No se encontró .streamlit/secrets.toml.")
        return None, None

def encontrar_encabezado_y_leer(archivo):
    palabras_clave = ["MODELO", "MODEL", "MOD", "CODIGO", "COD", "N°", "NO.", "ARTICULO"]
    for h in range(100):
        try:
            df = pd.read_excel(archivo, header=h)
            for col in df.columns:
                col_clean = re.sub(r"[^\w\s]", "", str(col)).replace("\n", " ").strip().upper()
                for pk in palabras_clave:
                    if pk in col_clean.split() or col_clean.startswith(pk):
                        return df, col, h
        except Exception:
            continue
    return None, None, None

def extraer_y_vincular_imagenes():
    url, key = cargar_secrets()
    if not url or not key:
        print("❌ Error: Faltan SUPABASE_URL o SUPABASE_KEY en secrets.toml")
        return

    supabase = create_client(url, key)
    carpeta_excel = "inventarios_excel"
    carpeta_img = "imagenes"
    
    os.makedirs(carpeta_img, exist_ok=True)
    
    archivos = glob.glob(os.path.join(carpeta_excel, "*.xlsx"))
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en '{carpeta_excel}'.")
        return

    print(f"🖼️ Extrayendo imágenes de {len(archivos)} archivo(s) Excel...\n")
    
    mapa_imagenes = {}
    total_imagenes = 0

    for i, archivo in enumerate(archivos, 1):
        nombre_excel = os.path.basename(archivo)
        print(f"[{i}/{len(archivos)}] Procesando: {nombre_excel}...", end=" ", flush=True)
        
        try:
            df, col_modelo, header_row = encontrar_encabezado_y_leer(archivo)
            
            if df is None or not col_modelo or header_row is None:
                print("-> ⚠️ Omitido (no se halló la columna MODELO)")
                continue

            df[col_modelo] = df[col_modelo].ffill()

            wb = load_workbook(archivo, data_only=True)
            sheet = wb.active
            
            count_archivo = 0
            
            if hasattr(sheet, '_images') and sheet._images:
                for img in sheet._images:
                    try:
                        row_excel = img.anchor._from.row + 1
                        idx_df = row_excel - (header_row + 2)
                        
                        if 0 <= idx_df < len(df):
                            raw_mod = str(df.iloc[idx_df][col_modelo]).strip()
                            if raw_mod and raw_mod.lower() not in ['none', 'nan', '', 'modelo']:
                                modelo_val = raw_mod.split(".")[0].strip() if raw_mod.replace(".", "").isdigit() else raw_mod
                                
                                image_data = img._data()
                                image = Image.open(io.BytesIO(image_data))
                                
                                if image.mode in ("RGBA", "P"):
                                    image = image.convert("RGB")
                                    
                                nombre_foto = f"{modelo_val}.jpg"
                                ruta_guardado = os.path.join(carpeta_img, nombre_foto)
                                
                                # Si ya existe la foto guardada, evitamos reescribirla en disco
                                if not os.path.exists(ruta_guardado):
                                    image.save(ruta_guardado, "JPEG", quality=85)
                                
                                ruta_normalizada = ruta_guardado.replace("\\", "/")
                                mapa_imagenes[modelo_val] = ruta_normalizada
                                count_archivo += 1
                                total_imagenes += 1
                    except Exception:
                        continue
                        
            print(f"-> ✅ ({count_archivo} fotos listas)")

        except Exception as e:
            print(f"-> ❌ Error al leer imágenes: {e}")

    print(f"\n📸 Total de imágenes detectadas: {len(mapa_imagenes)} foto(s).")

    # ACTUALIZACIÓN RÁPIDA POR LOTES (BATCH UPSERT)
    if mapa_imagenes:
        print("\n🚀 Sincronizando rutas de fotos con Supabase en lotes de alto rendimiento...")
        
        # Convertir mapa de imágenes a lista de payloads
        items_a_actualizar = []
        for modelo, ruta in mapa_imagenes.items():
            items_a_actualizar.append({
                "MODELO": modelo,
                "RUTA_IMAGEN": ruta
            })

        lote_size = 200
        total_sincronizados = 0
        
        for i in range(0, len(items_a_actualizar), lote_size):
            lote = items_a_actualizar[i:i + lote_size]
            try:
                # Usar upsert para actualizar RUTA_IMAGEN en bloque
                supabase.table("productos").upsert(lote, on_conflict="MODELO").execute()
                total_sincronizados += len(lote)
                print(f"  • Sincronizadas {total_sincronizados} / {len(items_a_actualizar)} fotos...", end="\r")
            except Exception as ex_db:
                print(f"\n❌ Error en lote: {ex_db}")

        print(f"\n🎉 ¡Sincronización masiva de fotos completada con éxito ({total_sincronizados} productos actualizados)!")

if __name__ == "__main__":
    extraer_y_vincular_imagenes()