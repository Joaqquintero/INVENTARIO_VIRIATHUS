import os
import glob
import pandas as pd
import sqlite3
from openpyxl import load_workbook
from PIL import Image
import io

def extraer_y_vincular_imagenes():
    carpeta_excel = "inventarios_excel"
    carpeta_img = "imagenes"
    
    # Crear carpeta 'imagenes' si no existe
    os.makedirs(carpeta_img, exist_ok=True)
    
    archivos = glob.glob(os.path.join(carpeta_excel, "*.xlsx"))
    if not archivos:
        print("⚠️ No se encontraron archivos Excel en la carpeta.")
        return

    print("🖼️ Extrayendo imágenes de los archivos Excel...\n")
    
    mapa_imagenes = {}
    total_imagenes = 0

    for archivo in archivos:
        nombre_excel = os.path.basename(archivo)
        print(f"Procesando: {nombre_excel}...", end=" ", flush=True)
        
        try:
            # 1. Cargar el Excel con openpyxl para acceder a las imágenes flotantes
            wb = load_workbook(archivo, data_only=True)
            sheet = wb.active
            
            # 2. Leer los datos con pandas para mapear filas a la columna MODELO
            df = pd.read_excel(archivo, header=5)
            col_modelo = next((c for c in df.columns if str(c).strip().upper() == 'MODELO'), None)
            
            if not col_modelo:
                print("⚠️ Omitido (no se halló la columna MODELO)")
                continue
                
            count_archivo = 0
            
            # 3. Recorrer cada imagen encontrada en la hoja
            for img in sheet._images:
                # Fila de la celda donde está anclada la imagen
                row = img.anchor._from.row  
                
                # Ajuste de índice por los encabezados (header=5 toma la fila 6)
                idx_df = row - 6
                
                if 0 <= idx_df < len(df):
                    modelo_val = str(df.iloc[idx_df][col_modelo]).replace('.0', '').strip()
                    
                    if modelo_val and modelo_val.lower() not in ['none', 'nan', '']:
                        # Extraer los bytes de la imagen
                        image_data = img._data()
                        image = Image.open(io.BytesIO(image_data))
                        
                        # Convertir a RGB (por si viene en transparente PNG)
                        if image.mode in ("RGBA", "P"):
                            image = image.convert("RGB")
                            
                        # Guardar la imagen nombrada como el modelo
                        nombre_foto = f"{modelo_val}.jpg"
                        ruta_guardado = os.path.join(carpeta_img, nombre_foto)
                        
                        image.save(ruta_guardado, "JPEG", quality=85)
                        mapa_imagenes[modelo_val] = ruta_guardado
                        count_archivo += 1
                        total_imagenes += 1
                        
            print(f"-> ✅ ({count_archivo} fotos extraídas)")

        except Exception as e:
            print(f"-> ❌ Error al leer imágenes: {e}")

    print(f"\n📸 Proceso de imágenes finalizado: {total_imagenes} foto(s) guardada(s) en '{carpeta_img}/'.")

    # 4. Actualizar la base de datos SQLite vinculando las rutas de las fotos
    if mapa_imagenes and os.path.exists("inventario.db"):
        print("\n🔄 Vinculando fotos con la base de datos 'inventario.db'...")
        conn = sqlite3.connect("inventario.db")
        cursor = conn.cursor()
        
        # Crear la columna RUTA_IMAGEN si no existe
        try:
            cursor.execute("ALTER TABLE productos ADD COLUMN RUTA_IMAGEN TEXT;")
        except sqlite3.OperationalError:
            pass  # Ya existía la columna
            
        # Asignar la ruta a cada modelo
        registros_actualizados = 0
        for modelo, ruta in mapa_imagenes.items():
            cursor.execute("UPDATE productos SET RUTA_IMAGEN = ? WHERE MODELO = ?", (ruta, modelo))
            registros_actualizados += cursor.rowcount
            
        conn.commit()
        conn.close()
        print(f"🎉 Se vincularon {registros_actualizados} productos con su foto en la base de datos.")

if __name__ == "__main__":
    extraer_y_vincular_imagenes()