import streamlit as st
import sqlite3
import pandas as pd
import os

st.set_page_config(
    page_title="Buscador de Inventario",
    page_icon="📦",
    layout="wide"
)

st.title("📦 Buscador de Inventario Unificado")
st.write("Busca por *MODELO, **NOMBRE, **DESCRIPCIÓN* o cualquier palabra clave.")

# Entrada de búsqueda
criterio = st.text_input("🔍 Ingresa tu búsqueda:", placeholder="Ej. 30587, Filtro, Manguera...").strip()

def buscar_en_todo(texto_busqueda):
    if not os.path.exists("inventario.db"):
        return None
    
    conn = sqlite3.connect("inventario.db")
    df = pd.read_sql_query("SELECT * FROM productos", conn)
    conn.close()
    
    if df.empty:
        return df

    # Filtra en TODAS las columnas sin importar mayúsculas/minúsculas
    mascara = df.astype(str).apply(
        lambda fila: fila.str.contains(texto_busqueda, case=False, na=False)
    ).any(axis=1)
    
    return df[mascara]

if criterio:
    df_res = buscar_en_todo(criterio)
    
    if df_res is None:
        st.error("⚠️ La base de datos 'inventario.db' no existe.")
    elif df_res.empty:
        st.warning(f"❌ No se encontraron resultados para: '{criterio}'")
    else:
        st.success(f"Se encontraron *{len(df_res)}* resultado(s):")
        st.markdown("---")
        
        for idx, fila in df_res.iterrows():
            col_img, col_datos = st.columns([1, 2])
            
            # Muestra la Imagen si existe
            with col_img:
                ruta_img = str(fila.get('RUTA_IMAGEN', '')).strip()
                if ruta_img and os.path.exists(ruta_img):
                    st.image(ruta_img, use_container_width=True, caption=f"Modelo: {fila.get('MODELO')}")
                else:
                    st.info("🖼️ Sin imagen disponible")

            # Muestra todos los campos con datos
            with col_datos:
                st.subheader(f"📌 Modelo: {fila.get('MODELO', 'S/M')}")
                
                c1, c2 = st.columns(2)
                items = list(fila.items())
                mitad = len(items) // 2 + 1
                
                def mostrar_datos(lista):
                    for col, val in lista:
                        val_str = str(val).strip()
                        if col not in ['RUTA_IMAGEN', 'index'] and val_str and val_str.lower() not in ['none', 'nan', 'nat', '']:
                            st.write(f"*{col}:* {val_str}")

                with c1:
                    mostrar_datos(items[:mitad])
                with c2:
                    mostrar_datos(items[mitad:])

            st.markdown("---")

    #Para iniciar, en terminal : streamlit run app.py