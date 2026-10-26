"""
Lector de la Constancia de Situación Fiscal (CSF) del SAT, en PDF.

Saca RFC, nombre o razón social (como lo pide el CFDI 4.0), régimen fiscal (clave y nombre) y código postal.
Todo se hace en memoria: no se guarda el archivo ni los datos en ningún lado.

Requiere `pypdf` (está en requirements.txt).
"""
import io
import re
import unicodedata

# Catálogo c_RegimenFiscal del SAT (clave → nombre). Se busca por palabras clave del nombre.
REGIMENES = [
    ("601", "General de Ley Personas Morales", ["general de ley personas morales"]),
    ("603", "Personas Morales con Fines no Lucrativos", ["fines no lucrativos"]),
    ("605", "Sueldos y Salarios e Ingresos Asimilados a Salarios", ["sueldos y salarios"]),
    ("606", "Arrendamiento", ["arrendamiento"]),
    ("607", "Régimen de Enajenación o Adquisición de Bienes", ["enajenacion o adquisicion de bienes"]),
    ("608", "Demás ingresos", ["demas ingresos"]),
    ("610", "Residentes en el Extranjero sin Establecimiento Permanente en México", ["residentes en el extranjero"]),
    ("611", "Ingresos por Dividendos (socios y accionistas)", ["dividendos"]),
    ("612", "Personas Físicas con Actividades Empresariales y Profesionales",
     ["actividades empresariales y profesionales"]),
    ("614", "Ingresos por intereses", ["ingresos por intereses"]),
    ("615", "Régimen de los ingresos por obtención de premios", ["obtencion de premios"]),
    ("616", "Sin obligaciones fiscales", ["sin obligaciones fiscales"]),
    ("620", "Sociedades Cooperativas de Producción que optan por diferir sus ingresos", ["cooperativas de produccion"]),
    ("621", "Incorporación Fiscal", ["incorporacion fiscal"]),
    ("622", "Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras", ["agricolas, ganaderas", "agricolas ganaderas"]),
    ("623", "Opcional para Grupos de Sociedades", ["grupos de sociedades"]),
    ("624", "Coordinados", ["coordinados"]),
    ("625", "Actividades Empresariales con ingresos a través de Plataformas Tecnológicas", ["plataformas tecnologicas"]),
    ("626", "Régimen Simplificado de Confianza", ["simplificado de confianza"]),
]

_RFC_RX = r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}"


def _sin_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def texto_pdf(datos):
    """Texto de todas las páginas del PDF (bytes)."""
    from pypdf import PdfReader
    lector = PdfReader(io.BytesIO(datos))
    return "\n".join((p.extract_text() or "") for p in lector.pages)


def _campo(texto, etiqueta, hasta=None):
    """Valor que sigue a «etiqueta:» en la misma línea (o en la siguiente si quedó vacío)."""
    m = re.search(etiqueta + r"\s*:\s*([^\n]*)(?:\n([^\n]*))?", texto, flags=re.I)
    if not m:
        return ""
    valor = m.group(1).strip()
    if not valor and m.group(2):
        valor = m.group(2).strip()
    if hasta:   # cortar donde empieza la siguiente etiqueta en la misma línea
        valor = re.split(hasta, valor, flags=re.I)[0].strip()
    return re.sub(r"\s+", " ", valor).strip(" :")


def leer_constancia(datos):
    """Devuelve dict con rfc, nombre, tipo («moral»/«física»), regimenes [(clave, nombre)], cp, idcif y avisos."""
    texto = texto_pdf(datos)
    if not texto.strip():
        return {"avisos": ["El PDF no tiene texto (¿es una foto o un escaneo?). Pide la constancia descargada del SAT."]}
    t = texto.replace("\r", "\n")
    avisos = []

    rfc = _campo(t, r"\bRFC", hasta=r"\s{2,}|CURP|idCIF")
    m = re.search(_RFC_RX, rfc.upper()) or re.search(_RFC_RX, t.upper())
    rfc = m.group(0) if m else ""
    if not rfc:
        avisos.append("No encontré el RFC.")

    sig = r"\s{2,}|Primer Apellido|Segundo Apellido|Fecha|Estatus|R[eé]gimen Capital|Nombre Comercial|CURP"
    razon = _campo(t, r"Denominaci[oó]n\s*/\s*Raz[oó]n\s+Social", hasta=sig)
    if razon:
        tipo, nombre = "moral", razon
    else:
        nom = _campo(t, r"Nombre\s*\(s\)", hasta=sig)
        ap1 = _campo(t, r"Primer\s+Apellido", hasta=sig)
        ap2 = _campo(t, r"Segundo\s+Apellido", hasta=sig)
        nombre = " ".join(x for x in (nom, ap1, ap2) if x)
        tipo = "física" if nombre else ""
    if not nombre and rfc:
        # Encabezado de la cédula: «<RFC>\nRegistro Federal de Contribuyentes\n<NOMBRE>\nNombre, denominación o razón social»
        m = re.search(r"\n([^\n]+)\n\s*Nombre,\s*denominaci[oó]n\s+o\s+raz[oó]n\s+social", t, flags=re.I)
        if m:
            nombre = m.group(1).strip()
            tipo = "moral" if len(rfc) == 12 else "física"
    if not nombre:
        avisos.append("No encontré el nombre o la razón social.")
    nombre = re.sub(r"\s+", " ", nombre).strip().upper()

    cp = _campo(t, r"C[oó]digo\s+Postal", hasta=r"\D")
    m = re.match(r"\d{5}", cp)
    cp = m.group(0) if m else ""
    if not cp:
        m = re.search(r"C[oó]digo\s+Postal\s*:?\s*(\d{5})", t, flags=re.I)
        cp = m.group(1) if m else ""
    if not cp:
        avisos.append("No encontré el código postal.")

    # Regímenes: se buscan en la parte de «Regímenes» (si existe) y si no en todo el texto
    seccion = t
    m = re.search(r"Reg[ií]menes\s*:?(.*?)(Obligaciones|Sus datos personales|$)", t, flags=re.I | re.S)
    if m:
        seccion = m.group(1)
    base = _sin_acentos(seccion).lower()
    base = re.sub(r"\s+", " ", base)
    regimenes = []
    for clave, nom, claves in REGIMENES:
        if any(k in base for k in claves) and clave not in [c for c, _ in regimenes]:
            regimenes.append((clave, nom))
    # «Sueldos y salarios» solo casi nunca sirve para facturar una compra; se deja, pero al final
    regimenes.sort(key=lambda r: r[0] == "605")
    if not regimenes:
        avisos.append("No encontré el régimen fiscal.")
    elif len(regimenes) > 1:
        avisos.append("Tiene varios regímenes: confirma con el cliente con cuál quiere facturar.")

    if rfc and tipo == "moral" and len(rfc) != 12:
        avisos.append("El RFC no tiene 12 caracteres como el de una persona moral: revísalo.")
    if rfc and tipo == "física" and len(rfc) != 13:
        avisos.append("El RFC no tiene 13 caracteres como el de una persona física: revísalo.")

    idcif = _campo(t, r"idCIF", hasta=r"\D")
    return {"rfc": rfc, "nombre": nombre, "tipo": tipo, "regimenes": regimenes, "cp": cp,
            "idcif": re.sub(r"\D", "", idcif), "avisos": avisos}
