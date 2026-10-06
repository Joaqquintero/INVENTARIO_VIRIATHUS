import os
import glob
import pandas as pd
import sqlite3

def consolidar_inventarios():
    carpeta = "inventarios_excel"
    archivos = glob.glob(os.path.join(carpeta, "*.xlsx"))
    
    if not archivos:
        print(f"⚠️ No se encontraron archivos Excel en la carpeta '{carpeta}'. Copia tus archivos ahí e intenta de nuevo.")
        return

    print(f"📦 Procesando {len(archivos)} archivo(s) Excel...\n")
    
    lista_dfs = []

    for archivo in archivos:
        try:
            # Leer el archivo Excel (header=5 toma la fila 6 como encabezados)
            df = pd.read_excel(archivo, header=5)
            
            # Limpiar filas vacías y sin MODELO
            df = df.dropna(how='all')
            if 'MODELO' in df.columns:
                df = df.dropna(subset=['MODELO'])
                # Formatear MODELO como texto limpio
                df['MODELO'] = df['MODELO'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
                lista_dfs.append(df)
                print(f"  ✓ Leído: {os.path.basename(archivo)} ({len(df)} filas)")
            
        except Exception as e:
            print(f"  ❌ Error al procesar {os.path.basename(archivo)}: {e}")

    if not lista_dfs:
        print("No se encontraron datos válidos.")
        return

    # 1. Unir todos los archivos en memoria
    df_completo = pd.concat(lista_dfs, ignore_index=True)
    
    # 2. ELIMINAR DUPLICADOS por la columna MODELO (conserva la última versión)
    filas_antes = len(df_completo)
    df_completo = df_completo.drop_duplicates(subset=['MODELO'], keep='last')
    filas_despues = len(df_completo)
    duplicados_eliminados = filas_antes - filas_despues

    # 3. Guardar en la base de datos SQLite (crea inventario.db automáticamente)
    conn = sqlite3.connect("inventario.db")
    df_completo.to_sql("productos", conn, if_exists="replace", index=False)
    
    # 4. Crear índice en la columna MODELO para búsquedas instantáneas
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_modelo ON productos(MODELO);")
    conn.commit()
    conn.close()

    print(f"\n🎉 ¡Proceso completado con éxito!")
    print(f"  • Total de productos únicos en la base de datos: {filas_despues}")
    if duplicados_eliminados > 0:
        print(f"  • Duplicados detectados y eliminados: {duplicados_eliminados}")

if _name_ == "_main_":
    consolidar_inventarios()