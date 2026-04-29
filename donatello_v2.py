import streamlit as st
import sqlite3
from datetime import datetime
from pathlib import Path
import shutil
import uuid
import qrcode
from io import BytesIO
import cv2
import numpy as np
from PIL import Image
import pandas as pd

# =========================================================
# DONATELLO CONTROL CENTER V2
# Inventario con imágenes + costeo básico mejorado
# =========================================================

st.set_page_config(
    page_title="Ventas Donatello POS",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# =========================
# FOLDERS
# =========================
BASE_DIR = Path(__file__).parent
IMG_DIR = BASE_DIR / "imagenes_productos"
IMG_DIR.mkdir(exist_ok=True)

ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
LOGO_PATHS = [
    ASSETS_DIR / "logo_donatello.png",
    ASSETS_DIR / "logo_donatello.jpg",
    BASE_DIR / "logo_donatello.png",
    BASE_DIR / "logo_donatello.jpg"
]

# =========================
# DATABASE SETUP
# =========================
conn = sqlite3.connect(BASE_DIR / "donatello_v2.db", check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS productos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT UNIQUE,
    nombre TEXT NOT NULL,
    categoria TEXT,
    proveedor TEXT,
    moneda TEXT,
    costo_base REAL,
    tipo_cambio REAL,
    comision_pct REAL,
    impuestos_pct REAL,
    flete_unitario REAL,
    costo_real REAL,
    precio_venta REAL,
    margen_pct REAL,
    stock INTEGER,
    stock_minimo INTEGER,
    imagen_local TEXT,
    imagen_url TEXT,
    fecha_alta TEXT
)''')

c.execute('''CREATE TABLE IF NOT EXISTS ventas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    producto_id INTEGER,
    cantidad INTEGER,
    precio_unitario REAL,
    costo_unitario REAL,
    total_venta REAL,
    utilidad REAL,
    fecha TEXT,
    FOREIGN KEY(producto_id) REFERENCES productos(id)
)''')

conn.commit()

# Migración segura para bases existentes
# Si vienes de una versión anterior, la tabla productos puede no tener columna codigo.
def columna_existe(nombre_tabla, nombre_columna):
    c.execute(f"PRAGMA table_info({nombre_tabla})")
    columnas = [row[1] for row in c.fetchall()]
    return nombre_columna in columnas

if not columna_existe("productos", "codigo"):
    c.execute("ALTER TABLE productos ADD COLUMN codigo TEXT")
    conn.commit()

# Asigna código a productos antiguos que no lo tengan
c.execute("SELECT id FROM productos WHERE codigo IS NULL OR codigo='' ORDER BY id")
productos_sin_codigo = c.fetchall()
for row in productos_sin_codigo:
    producto_id = row[0]
    codigo = f"DON-{producto_id:06d}"
    c.execute("UPDATE productos SET codigo=? WHERE id=?", (codigo, producto_id))
conn.commit()

# Intenta crear índice único para evitar códigos duplicados
try:
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_productos_codigo ON productos(codigo)")
    conn.commit()
except sqlite3.OperationalError:
    pass

# =========================
# HELPERS
# =========================

def calcular_costo_real(moneda, costo_base, tipo_cambio, comision_pct, impuestos_pct, flete_unitario):
    """Calcula costo real unitario en MXN."""
    costo_mxn = costo_base * tipo_cambio if moneda == "USD" else costo_base
    comision = costo_mxn * (comision_pct / 100)
    impuestos = costo_mxn * (impuestos_pct / 100)
    return costo_mxn + comision + impuestos + flete_unitario


def calcular_margen(precio_venta, costo_real):
    if precio_venta <= 0:
        return 0
    return ((precio_venta - costo_real) / precio_venta) * 100


def guardar_imagen(archivo, prefijo="producto"):
    """Guarda imagen subida o tomada desde cámara y devuelve ruta local."""
    if archivo is None:
        return None

    extension = Path(archivo.name).suffix if hasattr(archivo, "name") else ".jpg"
    if extension.lower() not in [".jpg", ".jpeg", ".png", ".webp"]:
        extension = ".jpg"

    nombre_archivo = f"{prefijo}_{uuid.uuid4().hex}{extension}"
    ruta_destino = IMG_DIR / nombre_archivo

    with open(ruta_destino, "wb") as f:
        f.write(archivo.getbuffer())

    return str(ruta_destino)


def obtener_imagen_preferida(producto):
    """Prioridad: imagen local / foto cámara / imagen subida, después URL."""
    imagen_local = producto[16]
    imagen_url = producto[17]

    if imagen_local and Path(imagen_local).exists():
        return imagen_local
    if imagen_url:
        return imagen_url
    return None


def generar_codigo_producto():
    c.execute("SELECT MAX(id) FROM productos")
    ultimo_id = c.fetchone()[0]
    siguiente_id = 1 if ultimo_id is None else ultimo_id + 1
    return f"DON-{siguiente_id:06d}"


def generar_qr_bytes(codigo):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(codigo)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def leer_qr_desde_imagen(archivo_imagen):
    """Lee un QR desde una imagen tomada con cámara o subida."""
    if archivo_imagen is None:
        return None

    try:
        imagen = Image.open(archivo_imagen).convert("RGB")
        imagen_np = np.array(imagen)
        detector = cv2.QRCodeDetector()
        data, points, _ = detector.detectAndDecode(imagen_np)

        if data:
            return data.strip()
        return None
    except Exception:
        return None


def agregar_producto(data):
    c.execute('''INSERT INTO productos (
        codigo, nombre, categoria, proveedor, moneda, costo_base, tipo_cambio,
        comision_pct, impuestos_pct, flete_unitario, costo_real,
        precio_venta, margen_pct, stock, stock_minimo,
        imagen_local, imagen_url, fecha_alta
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', data)
    conn.commit()


def codigo_existe(codigo):
    c.execute("SELECT COUNT(*) FROM productos WHERE UPPER(codigo)=UPPER(?)", (str(codigo).strip(),))
    return c.fetchone()[0] > 0


def importar_productos_csv(df):
    """Importa productos desde CSV exportado de la base local."""
    importados = 0
    omitidos = 0

    for _, row in df.iterrows():
        try:
            codigo = str(row.get("codigo", "")).strip()
            if not codigo or codigo.lower() == "nan":
                codigo = generar_codigo_producto()

            if codigo_existe(codigo):
                omitidos += 1
                continue

            nombre = str(row.get("nombre", "")).strip()
            if not nombre or nombre.lower() == "nan":
                omitidos += 1
                continue

            categoria = str(row.get("categoria", "")).strip()
            proveedor = str(row.get("proveedor", "")).strip()
            moneda = str(row.get("moneda", "MXN")).strip()
            if moneda not in ["MXN", "USD"]:
                moneda = "MXN"

            costo_base = float(row.get("costo_base", 0) or 0)
            tipo_cambio = float(row.get("tipo_cambio", 17) or 17)
            comision_pct = float(row.get("comision_pct", 0) or 0)
            impuestos_pct = float(row.get("impuestos_pct", 0) or 0)
            flete_unitario = float(row.get("flete_unitario", 0) or 0)

            costo_real = row.get("costo_real", None)
            if pd.isna(costo_real) or costo_real == "":
                costo_real = calcular_costo_real(moneda, costo_base, tipo_cambio, comision_pct, impuestos_pct, flete_unitario)
            else:
                costo_real = float(costo_real)

            precio_venta = float(row.get("precio_venta", 0) or 0)
            margen_pct = row.get("margen_pct", None)
            if pd.isna(margen_pct) or margen_pct == "":
                margen_pct = calcular_margen(precio_venta, costo_real)
            else:
                margen_pct = float(margen_pct)

            stock = int(float(row.get("stock", 0) or 0))
            stock_minimo = int(float(row.get("stock_minimo", 1) or 1))

            imagen_local = ""  # En Streamlit Cloud no sirve la ruta local de tu PC
            imagen_url = str(row.get("imagen_url", "")).strip()
            if imagen_url.lower() == "nan":
                imagen_url = ""

            fecha_alta = str(row.get("fecha_alta", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            if fecha_alta.lower() == "nan":
                fecha_alta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            data = (
                codigo, nombre, categoria, proveedor, moneda,
                costo_base, tipo_cambio, comision_pct, impuestos_pct,
                flete_unitario, costo_real, precio_venta, margen_pct,
                stock, stock_minimo, imagen_local, imagen_url, fecha_alta
            )

            agregar_producto(data)
            importados += 1
        except Exception:
            omitidos += 1

    return importados, omitidos


def obtener_productos():
    c.execute('''SELECT id, codigo, nombre, categoria, proveedor, moneda, costo_base,
                        tipo_cambio, comision_pct, impuestos_pct, flete_unitario,
                        costo_real, precio_venta, margen_pct, stock, stock_minimo,
                        imagen_local, imagen_url, fecha_alta
                 FROM productos
                 ORDER BY id DESC''')
    return c.fetchall()


def obtener_producto_por_id(producto_id):
    c.execute('''SELECT id, codigo, nombre, categoria, proveedor, moneda, costo_base,
                        tipo_cambio, comision_pct, impuestos_pct, flete_unitario,
                        costo_real, precio_venta, margen_pct, stock, stock_minimo,
                        imagen_local, imagen_url, fecha_alta
                 FROM productos
                 WHERE id=?''', (producto_id,))
    return c.fetchone()


def obtener_producto_por_codigo(codigo):
    c.execute('''SELECT id, codigo, nombre, categoria, proveedor, moneda, costo_base,
                        tipo_cambio, comision_pct, impuestos_pct, flete_unitario,
                        costo_real, precio_venta, margen_pct, stock, stock_minimo,
                        imagen_local, imagen_url, fecha_alta
                 FROM productos
                 WHERE UPPER(codigo)=UPPER(?)''', (codigo.strip(),))
    return c.fetchone()


def actualizar_stock(producto_id, nuevo_stock):
    c.execute("UPDATE productos SET stock=? WHERE id=?", (nuevo_stock, producto_id))
    conn.commit()


def eliminar_producto(producto_id):
    producto = obtener_producto_por_id(producto_id)
    if producto and producto[16] and Path(producto[16]).exists():
        try:
            Path(producto[16]).unlink()
        except Exception:
            pass
    c.execute("DELETE FROM productos WHERE id=?", (producto_id,))
    conn.commit()


def actualizar_imagen_producto(producto_id, imagen_local=None, imagen_url=None):
    producto = obtener_producto_por_id(producto_id)

    # Si se sube o toma una nueva imagen local, elimina la anterior para no acumular archivos basura
    if imagen_local and producto and producto[16] and Path(producto[16]).exists():
        try:
            Path(producto[16]).unlink()
        except Exception:
            pass

    if imagen_local is not None and imagen_url is not None:
        c.execute(
            "UPDATE productos SET imagen_local=?, imagen_url=? WHERE id=?",
            (imagen_local, imagen_url, producto_id)
        )
    elif imagen_local is not None:
        c.execute(
            "UPDATE productos SET imagen_local=? WHERE id=?",
            (imagen_local, producto_id)
        )
    elif imagen_url is not None:
        c.execute(
            "UPDATE productos SET imagen_url=? WHERE id=?",
            (imagen_url, producto_id)
        )

    conn.commit()


def limpiar_formulario_producto():
    keys = [
        "nombre_producto", "categoria_producto", "proveedor_producto",
        "stock_producto", "stock_minimo_producto", "moneda_producto",
        "costo_base_producto", "tipo_cambio_producto", "comision_producto",
        "impuestos_producto", "flete_producto", "precio_venta_producto",
        "imagen_url_producto"
    ]
    for key in keys:
        if key in st.session_state:
            del st.session_state[key]


def registrar_venta(producto_id, cantidad):
    producto = obtener_producto_por_id(producto_id)

    if not producto:
        return False, "Producto no encontrado"

    stock_actual = producto[14]
    precio_unitario = producto[12]
    costo_unitario = producto[11]

    if stock_actual < cantidad:
        return False, "Stock insuficiente"

    total_venta = precio_unitario * cantidad
    utilidad = (precio_unitario - costo_unitario) * cantidad
    nuevo_stock = stock_actual - cantidad

    c.execute("UPDATE productos SET stock=? WHERE id=?", (nuevo_stock, producto_id))
    c.execute('''INSERT INTO ventas (
        producto_id, cantidad, precio_unitario, costo_unitario,
        total_venta, utilidad, fecha
    ) VALUES (?, ?, ?, ?, ?, ?, ?)''', (
        producto_id, cantidad, precio_unitario, costo_unitario,
        total_venta, utilidad, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    conn.commit()
    return True, total_venta


def registrar_venta_carrito(carrito):
    """Registra una venta con varios productos y descuenta inventario."""
    if not carrito:
        return False, "El carrito está vacío"

    # Validación previa para evitar ventas parciales
    for item in carrito:
        producto = obtener_producto_por_id(item["producto_id"])
        if not producto:
            return False, f"Producto no encontrado: {item['nombre']}"
        if producto[14] < item["cantidad"]:
            return False, f"Stock insuficiente para {item['nombre']}"

    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_general = 0
    utilidad_general = 0

    for item in carrito:
        producto = obtener_producto_por_id(item["producto_id"])
        stock_actual = producto[14]
        precio_unitario = producto[12]
        costo_unitario = producto[11]
        cantidad = item["cantidad"]

        total_venta = precio_unitario * cantidad
        utilidad = (precio_unitario - costo_unitario) * cantidad
        nuevo_stock = stock_actual - cantidad

        c.execute("UPDATE productos SET stock=? WHERE id=?", (nuevo_stock, item["producto_id"]))
        c.execute('''INSERT INTO ventas (
            producto_id, cantidad, precio_unitario, costo_unitario,
            total_venta, utilidad, fecha
        ) VALUES (?, ?, ?, ?, ?, ?, ?)''', (
            item["producto_id"], cantidad, precio_unitario, costo_unitario,
            total_venta, utilidad, fecha
        ))

        total_general += total_venta
        utilidad_general += utilidad

    conn.commit()
    return True, {"total": total_general, "utilidad": utilidad_general}


def obtener_ventas():
    c.execute('''SELECT ventas.id, productos.nombre, ventas.cantidad, ventas.precio_unitario,
                        ventas.costo_unitario, ventas.total_venta, ventas.utilidad, ventas.fecha
                 FROM ventas
                 LEFT JOIN productos ON ventas.producto_id = productos.id
                 ORDER BY ventas.id DESC''')
    return c.fetchall()


def formato_moneda(valor):
    return f"${valor:,.2f}"

# =========================
# UI
# =========================

st.markdown(
    """
    <style>
        :root {
            --don-orange: #fc4a1a;
            --don-gold: #f7b733;
            --don-dark: #1f1f1f;
            --don-brown: #4b2f14;
            --don-green: #6b8e23;
            --don-cream: #fff7e8;
            --don-card: #fffdf8;
            --don-border: #ead6ad;
            --don-text: #24180d;
            --don-muted: #6d604d;
        }

        .block-container {
            padding-top: 1rem;
            padding-left: 0.8rem;
            padding-right: 0.8rem;
            max-width: 1180px;
        }

        h1, h2, h3, label, p, span {
            letter-spacing: -0.01em;
        }

        div[data-testid="stMetric"] {
            background: var(--don-card) !important;
            border: 1px solid var(--don-border) !important;
            padding: 0.65rem !important;
            border-radius: 16px !important;
            box-shadow: 0 4px 14px rgba(60,35,10,0.08) !important;
        }

        div[data-testid="stMetric"] label,
        div[data-testid="stMetric"] div {
            color: var(--don-text) !important;
        }

        .stButton > button {
            border-radius: 16px !important;
            padding: 0.62rem 0.9rem !important;
            font-weight: 800 !important;
            border: 1px solid var(--don-border) !important;
            background: var(--don-card) !important;
            color: var(--don-text) !important;
            box-shadow: 0 2px 8px rgba(60,35,10,0.06) !important;
        }

        .stButton > button:hover {
            border-color: var(--don-orange) !important;
            color: var(--don-orange) !important;
            background: #fff4dc !important;
        }

        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, var(--don-gold) 0%, var(--don-orange) 100%) !important;
            color: #ffffff !important;
            border: none !important;
        }

        div[data-testid="stContainer"] {
            border-radius: 18px !important;
        }

        .donatello-shell {
            background: linear-gradient(135deg, #251f17 0%, #5a3a16 52%, #f7b733 100%);
            padding: 12px 16px;
            border-radius: 22px;
            margin-bottom: 12px;
            color: white;
            box-shadow: 0 8px 24px rgba(0,0,0,0.14);
            display: flex;
            align-items: center;
            gap: 14px;
            min-height: 82px;
            overflow: visible;
        }

        .donatello-logo-box {
            width: 62px;
            min-width: 62px;
            height: 62px;
            border-radius: 18px;
            background: rgba(255,255,255,0.16);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            font-size: 2rem;
        }

        .donatello-logo-box img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .donatello-title {
            font-size: 1.45rem;
            font-weight: 900;
            margin: 0;
            line-height: 1.1;
            color: #ffffff !important;
        }

        .donatello-subtitle {
            font-size: 0.86rem;
            opacity: 0.95;
            margin-top: 4px;
            color: #ffffff !important;
        }

        .quick-card {
            background: var(--don-card);
            border: 1px solid var(--don-border);
            border-radius: 18px;
            padding: 12px;
            margin-bottom: 10px;
            box-shadow: 0 4px 14px rgba(60,35,10,0.06);
            color: var(--don-text);
        }

        .quick-title {
            font-weight: 900;
            font-size: 1.05rem;
            margin-bottom: 4px;
            color: var(--don-text);
        }

        .quick-muted {
            color: var(--don-muted);
            font-size: 0.85rem;
        }

        div[role="radiogroup"] {
            gap: 6px;
        }

        div[role="radiogroup"] label {
            background: var(--don-card) !important;
            border: 1px solid var(--don-border) !important;
            padding: 6px 10px !important;
            border-radius: 14px !important;
            margin-right: 4px !important;
            color: var(--don-text) !important;
            box-shadow: 0 2px 8px rgba(60,35,10,0.05);
        }

        div[role="radiogroup"] label p,
        div[role="radiogroup"] label span {
            color: var(--don-text) !important;
            font-weight: 800 !important;
        }

        input, textarea {
            border-radius: 14px !important;
        }

        @media (max-width: 768px) {
            .block-container {
                padding-left: 0.45rem;
                padding-right: 0.45rem;
                padding-top: 0.45rem;
            }

            .donatello-shell {
                padding: 10px;
                border-radius: 18px;
                margin-bottom: 8px;
                min-height: 70px;
                gap: 10px;
            }

            .donatello-logo-box {
                width: 52px;
                min-width: 52px;
                height: 52px;
                border-radius: 15px;
            }

            .donatello-title {
                font-size: 1.15rem;
            }

            .donatello-subtitle {
                font-size: 0.75rem;
            }

            div[data-testid="column"] {
                width: 100% !important;
                flex: 1 1 100% !important;
            }

            .stButton > button {
                width: 100% !important;
                min-height: 44px !important;
                font-size: 0.95rem !important;
            }

            div[data-testid="stMetric"] {
                padding: 0.55rem !important;
            }
        }
    </style>
    """,
    unsafe_allow_html=True
)

logo_encontrado = None
for logo_path in LOGO_PATHS:
    if logo_path.exists():
        logo_encontrado = logo_path
        break

if logo_encontrado:
    import base64
    with open(logo_encontrado, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    logo_html = f"<img src='data:image/png;base64,{logo_b64}' />"
else:
    logo_html = "🛒"

st.markdown(
    f"""
    <div class="donatello-shell">
        <div class="donatello-logo-box">{logo_html}</div>
        <div>
            <p class="donatello-title">Ventas Donatello</p>
            <div class="donatello-subtitle">Inventario, ventas, QR y control de utilidad</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True
)

menu = st.radio(
    "Navegación",
    ["🚀 Venta rápida", "📦 Inventario", "➕ Agregar", "🏷️ QR", "🔁 Stock", "📊 Dashboard", "⬆️ Importar"],
    horizontal=True,
    label_visibility="collapsed"
)

menu_map = {
    "🚀 Venta rápida": "Registrar venta",
    "📦 Inventario": "Inventario visual",
    "➕ Agregar": "Agregar producto",
    "🏷️ QR": "Etiquetas QR",
    "🔁 Stock": "Ajustar inventario",
    "📊 Dashboard": "Dashboard",
    "⬆️ Importar": "Importar CSV"
}
menu = menu_map[menu]

# =========================
# AGREGAR PRODUCTO
# =========================

if menu == "Agregar producto":
    st.header("Agregar producto con imagen")
    st.caption("En los campos de porcentaje escribe 15 para 15%, 8.25 para 8.25%, etc.")

    # Este ID fuerza a Streamlit a reconstruir todos los campos después de guardar
    if "producto_form_version" not in st.session_state:
        st.session_state.producto_form_version = 0

    form_version = st.session_state.producto_form_version

    col1, col2 = st.columns(2)

    with col1:
        nombre = st.text_input(
            "Nombre del producto *",
            placeholder="Ej. Espejo redondo LED 60 cm",
            key=f"nombre_producto_v{form_version}"
        )
        categoria = st.text_input(
            "Categoría",
            placeholder="Ej. Espejos, muebles, decoración",
            key=f"categoria_producto_v{form_version}"
        )
        proveedor = st.text_input(
            "Proveedor",
            placeholder="Ej. Amazon, Costco, proveedor local",
            key=f"proveedor_producto_v{form_version}"
        )
        stock = st.number_input(
            "Stock inicial",
            min_value=0,
            step=1,
            value=0,
            key=f"stock_producto_v{form_version}"
        )
        stock_minimo = st.number_input(
            "Stock mínimo para alerta",
            min_value=0,
            step=1,
            value=1,
            key=f"stock_minimo_producto_v{form_version}"
        )

    with col2:
        moneda = st.selectbox(
            "Moneda de compra",
            ["MXN", "USD"],
            key=f"moneda_producto_v{form_version}"
        )
        costo_base = st.number_input(
            "Costo base unitario",
            min_value=0.0,
            step=1.0,
            value=0.0,
            key=f"costo_base_producto_v{form_version}"
        )
        tipo_cambio = st.number_input(
            "Tipo de cambio",
            min_value=1.0,
            step=0.10,
            value=17.00,
            key=f"tipo_cambio_producto_v{form_version}"
        )
        comision_pct = st.number_input(
            "Comisión proveedor / plataforma (%)",
            min_value=0.0,
            step=0.5,
            value=0.0,
            key=f"comision_producto_v{form_version}"
        )
        impuestos_pct = st.number_input(
            "Impuestos / tasas (%)",
            min_value=0.0,
            step=0.25,
            value=0.0,
            key=f"impuestos_producto_v{form_version}"
        )
        flete_unitario = st.number_input(
            "Flete / gasto unitario",
            min_value=0.0,
            step=1.0,
            value=0.0,
            key=f"flete_producto_v{form_version}"
        )
        precio_venta = st.number_input(
            "Precio de venta",
            min_value=0.0,
            step=1.0,
            value=0.0,
            key=f"precio_venta_producto_v{form_version}"
        )

    # Cálculo visible en tiempo real
    costo_mxn = costo_base * tipo_cambio if moneda == "USD" else costo_base
    comision_monto = costo_mxn * (comision_pct / 100)
    impuestos_monto = costo_mxn * (impuestos_pct / 100)
    costo_real = costo_mxn + comision_monto + impuestos_monto + flete_unitario
    margen_pct = calcular_margen(precio_venta, costo_real)

    st.divider()
    st.subheader("Resumen de costo calculado")

    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Costo convertido", formato_moneda(costo_mxn))
    r2.metric("Comisión", formato_moneda(comision_monto))
    r3.metric("Impuestos / tasas", formato_moneda(impuestos_monto))
    r4.metric("Flete", formato_moneda(flete_unitario))
    r5.metric("Costo real final", formato_moneda(costo_real))

    if precio_venta > 0:
        st.metric("Margen estimado", f"{margen_pct:.2f}%")
    else:
        st.warning("Aún no has definido precio de venta. El costo real ya está calculado, pero el margen aparecerá cuando agregues precio de venta.")

    st.divider()
    st.subheader("Imagen del producto")
    st.caption("Prioridad: foto tomada desde cámara > imagen subida > URL de proveedor")

    img_col1, img_col2, img_col3 = st.columns(3)
    with img_col1:
        foto_camara = st.camera_input(
            "Tomar foto desde cámara",
            key=f"foto_camara_producto_v{form_version}"
        )
    with img_col2:
        imagen_subida = st.file_uploader(
            "Subir imagen",
            type=["jpg", "jpeg", "png", "webp"],
            key=f"imagen_subida_producto_v{form_version}"
        )
    with img_col3:
        imagen_url = st.text_input(
            "URL imagen proveedor / internet",
            key=f"imagen_url_producto_v{form_version}"
        )

    guardar = st.button("Guardar producto", type="primary", key=f"guardar_producto_v{form_version}")

    if guardar:
        if not nombre.strip():
            st.error("El nombre del producto es obligatorio.")
        else:
            imagen_local = None

            if foto_camara is not None:
                imagen_local = guardar_imagen(foto_camara, "camara")
            elif imagen_subida is not None:
                imagen_local = guardar_imagen(imagen_subida, "upload")

            data = (
                generar_codigo_producto(), nombre.strip(), categoria.strip(), proveedor.strip(), moneda,
                costo_base, tipo_cambio, comision_pct, impuestos_pct,
                flete_unitario, costo_real, precio_venta, margen_pct,
                stock, stock_minimo, imagen_local, imagen_url.strip(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )

            agregar_producto(data)

            # Forzar reconstrucción total de campos en el siguiente render
            st.session_state.producto_form_version += 1
            st.success("Producto guardado correctamente. El formulario se limpió.")
            st.rerun()

# =========================
# INVENTARIO VISUAL
# =========================

elif menu == "Inventario visual":
    st.header("Inventario visual")

    productos = obtener_productos()

    if not productos:
        st.warning("Todavía no hay productos registrados.")
    else:
        busqueda = st.text_input("Buscar producto", placeholder="Buscar por nombre, categoría o proveedor")
        productos_filtrados = []

        for p in productos:
            texto = f"{p[1]} {p[2]} {p[3]} {p[4]}".lower()
            if busqueda.lower() in texto:
                productos_filtrados.append(p)

        st.caption(f"Productos encontrados: {len(productos_filtrados)}")

        for p in productos_filtrados:
            imagen = obtener_imagen_preferida(p)
            stock = p[14]
            stock_minimo = p[15]
            alerta = stock <= stock_minimo

            with st.container(border=True):
                col_img, col_info, col_nums = st.columns([1, 2, 1])

                with col_img:
                    if imagen:
                        st.image(imagen, use_container_width=True)
                    else:
                        st.write("Sin imagen")

                with col_info:
                    st.subheader(p[2])
                    st.write(f"**Código:** {p[1]}")
                    st.write(f"**Categoría:** {p[3] or 'Sin categoría'}")
                    st.write(f"**Proveedor:** {p[4] or 'Sin proveedor'}")
                    st.write(f"**Moneda compra:** {p[5]}")
                    st.write(f"**Fecha alta:** {p[18]}")

                with col_nums:
                    st.metric("Precio venta", formato_moneda(p[12]))
                    st.metric("Costo real", formato_moneda(p[11]))
                    st.metric("Margen", f"{p[13]:.2f}%")
                    if alerta:
                        st.error(f"Stock bajo: {stock}")
                    else:
                        st.success(f"Stock: {stock}")

                    confirmar_key = f"confirmar_eliminar_{p[0]}"
                    if st.session_state.get(confirmar_key, False):
                        st.warning("Confirma eliminación")
                        if st.button("Sí, eliminar", key=f"eliminar_si_{p[0]}"):
                            eliminar_producto(p[0])
                            st.success("Producto eliminado.")
                            st.rerun()
                        if st.button("Cancelar", key=f"eliminar_no_{p[0]}"):
                            st.session_state[confirmar_key] = False
                            st.rerun()
                    else:
                        if st.button("Eliminar producto", key=f"eliminar_{p[0]}"):
                            st.session_state[confirmar_key] = True
                            st.rerun()

                    editar_img_key = f"editar_imagen_{p[0]}"
                    if st.button("Agregar / cambiar imagen", key=f"btn_img_{p[0]}"):
                        st.session_state[editar_img_key] = not st.session_state.get(editar_img_key, False)
                        st.rerun()

                    if st.session_state.get(editar_img_key, False):
                        st.divider()
                        st.write("**Actualizar imagen**")
                        st.caption("Prioridad al guardar: foto cámara > imagen subida > URL")

                        nueva_foto = st.camera_input("Tomar foto", key=f"nueva_foto_{p[0]}")
                        nueva_imagen = st.file_uploader("Subir imagen", type=["jpg", "jpeg", "png", "webp"], key=f"nueva_img_{p[0]}")
                        nueva_url = st.text_input("URL imagen", value=p[17] or "", key=f"nueva_url_{p[0]}")

                        col_guardar_img, col_cancelar_img = st.columns(2)
                        with col_guardar_img:
                            if st.button("Guardar imagen", key=f"guardar_img_{p[0]}"):
                                imagen_local_nueva = None
                                if nueva_foto is not None:
                                    imagen_local_nueva = guardar_imagen(nueva_foto, "camara")
                                elif nueva_imagen is not None:
                                    imagen_local_nueva = guardar_imagen(nueva_imagen, "upload")

                                if imagen_local_nueva:
                                    actualizar_imagen_producto(p[0], imagen_local=imagen_local_nueva, imagen_url=nueva_url.strip())
                                else:
                                    actualizar_imagen_producto(p[0], imagen_url=nueva_url.strip())

                                st.success("Imagen actualizada correctamente.")
                                st.session_state[editar_img_key] = False
                                st.rerun()

                        with col_cancelar_img:
                            if st.button("Cancelar imagen", key=f"cancelar_img_{p[0]}"):
                                st.session_state[editar_img_key] = False
                                st.rerun()

# =========================
# IMPORTAR CSV
# =========================

elif menu == "Importar CSV":
    st.header("Importar productos desde CSV")
    st.caption("Usa este módulo para cargar productos exportados desde tu base local.")

    archivo_csv = st.file_uploader("Sube productos_exportados.csv", type=["csv"])

    if archivo_csv is not None:
        try:
            df = pd.read_csv(archivo_csv)
            st.success("CSV leído correctamente.")
            st.write(f"Productos detectados en archivo: {len(df)}")
            st.dataframe(df.head(10), use_container_width=True)

            st.warning("Las imágenes locales de tu computadora no se migran a Streamlit Cloud. Se conservarán solo las URLs de imagen si existen.")

            if st.button("Importar productos", type="primary"):
                importados, omitidos = importar_productos_csv(df)
                st.success(f"Importación finalizada. Importados: {importados} | Omitidos: {omitidos}")
                st.info("Los omitidos normalmente son productos duplicados por código o registros incompletos.")
        except Exception as e:
            st.error(f"No pude leer el CSV: {e}")

# =========================
# ETIQUETAS QR
# =========================

elif menu == "Etiquetas QR":
    st.header("Etiquetas QR")
    st.caption("Genera el QR interno del producto para imprimirlo y pegarlo como sticker.")

    productos = obtener_productos()

    if not productos:
        st.warning("Todavía no hay productos registrados.")
    else:
        opciones = {f"{p[1]} | {p[2]}": p[0] for p in productos}
        seleccion = st.selectbox("Selecciona producto", list(opciones.keys()))
        producto_id = opciones[seleccion]
        producto = obtener_producto_por_id(producto_id)

        col1, col2, col3 = st.columns([1, 1, 2])

        with col1:
            imagen = obtener_imagen_preferida(producto)
            if imagen:
                st.image(imagen, use_container_width=True)
            else:
                st.write("Sin imagen")

        with col2:
            qr_bytes = generar_qr_bytes(producto[1])
            st.image(qr_bytes, caption=producto[1], use_container_width=True)
            st.download_button(
                "Descargar QR",
                data=qr_bytes,
                file_name=f"QR_{producto[1]}.png",
                mime="image/png"
            )

        with col3:
            st.subheader(producto[2])
            st.write(f"**Código:** {producto[1]}")
            st.write(f"**Precio:** {formato_moneda(producto[12])}")
            st.write(f"**Costo real:** {formato_moneda(producto[11])}")
            st.write(f"**Margen:** {producto[13]:.2f}%")
            st.write(f"**Stock:** {producto[14]}")
            st.info("El QR contiene solo el código interno. Así, si cambias precio, costo o stock, el QR sigue funcionando.")

# =========================
# AJUSTAR INVENTARIO
# =========================

elif menu == "Ajustar inventario":
    st.header("Ajustar inventario")
    st.caption("Usa este módulo para reponer, restar o corregir stock de productos existentes.")

    productos = obtener_productos()

    if not productos:
        st.warning("Todavía no hay productos registrados.")
    else:
        opciones = {f"{p[1]} | {p[2]} | Stock actual: {p[14]}": p[0] for p in productos}
        seleccion = st.selectbox("Selecciona producto", list(opciones.keys()))
        producto_id = opciones[seleccion]
        producto = obtener_producto_por_id(producto_id)

        col_img, col_info = st.columns([1, 2])

        with col_img:
            imagen = obtener_imagen_preferida(producto)
            if imagen:
                st.image(imagen, use_container_width=True)
            else:
                st.write("Sin imagen")

        with col_info:
            st.subheader(producto[2])
            st.write(f"**Código:** {producto[1]}")
            st.write(f"**Categoría:** {producto[3] or 'Sin categoría'}")
            st.write(f"**Proveedor:** {producto[4] or 'Sin proveedor'}")
            st.metric("Stock actual", producto[14])
            st.metric("Stock mínimo", producto[15])

        st.divider()

        tipo_ajuste = st.radio(
            "Tipo de ajuste",
            ["Agregar stock", "Restar stock", "Corregir stock manualmente"],
            horizontal=True
        )

        stock_actual = producto[14]

        if tipo_ajuste == "Agregar stock":
            cantidad = st.number_input("Cantidad a agregar", min_value=1, step=1)
            nuevo_stock = stock_actual + cantidad
            st.info(f"Nuevo stock será: {nuevo_stock}")

        elif tipo_ajuste == "Restar stock":
            cantidad = st.number_input("Cantidad a restar", min_value=1, max_value=stock_actual if stock_actual > 0 else 1, step=1)
            nuevo_stock = max(stock_actual - cantidad, 0)
            st.info(f"Nuevo stock será: {nuevo_stock}")

        else:
            nuevo_stock = st.number_input("Nuevo stock correcto", min_value=0, step=1, value=stock_actual)
            st.info(f"El stock quedará en: {nuevo_stock}")

        if st.button("Guardar ajuste", type="primary"):
            actualizar_stock(producto_id, nuevo_stock)
            st.success("Inventario actualizado correctamente.")
            st.rerun()

# =========================
# REGISTRAR VENTA
# =========================

elif menu == "Registrar venta":
    st.header("🚀 Venta rápida")

    if "carrito" not in st.session_state:
        st.session_state.carrito = []

    productos = obtener_productos()

    if not productos:
        st.warning("Primero registra productos.")
    else:
        productos_disponibles = [p for p in productos if p[14] > 0]

        if not productos_disponibles:
            st.error("No hay productos con stock disponible.")
        else:
            total_carrito = sum(item["total"] for item in st.session_state.carrito)
            utilidad_carrito = sum(item["utilidad"] for item in st.session_state.carrito)
            piezas_carrito = sum(item["cantidad"] for item in st.session_state.carrito)

            k1, k2, k3 = st.columns(3)
            k1.metric("Total", formato_moneda(total_carrito))
            k2.metric("Piezas", piezas_carrito)
            k3.metric("Utilidad", formato_moneda(utilidad_carrito))

            st.markdown("<div class='quick-card'><div class='quick-title'>Escanear / capturar producto</div><div class='quick-muted'>Escanea el QR, escribe el código o selecciona manualmente.</div></div>", unsafe_allow_html=True)

            modo_qr = st.radio(
                "Método",
                ["Código / lector", "Cámara", "Manual"],
                horizontal=True,
                label_visibility="collapsed"
            )

            producto_codigo = None
            cantidad_codigo = 1

            if modo_qr == "Código / lector":
                col_scan1, col_scan2 = st.columns([2, 1])
                with col_scan1:
                    codigo_final = st.text_input(
                        "Código",
                        placeholder="Escanea o escribe DON-000001",
                        key="codigo_escaneado_venta"
                    )
                with col_scan2:
                    cantidad_codigo = st.number_input("Cantidad", min_value=1, step=1, value=1, key="cantidad_codigo_venta")

                producto_codigo = obtener_producto_por_codigo(codigo_final) if codigo_final.strip() else None

                if st.button("Agregar al carrito", type="primary", key="btn_agregar_codigo"):
                    if not codigo_final.strip():
                        st.error("Primero captura o escanea un código.")
                    elif not producto_codigo:
                        st.error("No encontré un producto con ese código.")
                    elif producto_codigo[14] <= 0:
                        st.error("Ese producto no tiene stock disponible.")
                    else:
                        producto_existente = None
                        for item in st.session_state.carrito:
                            if item["producto_id"] == producto_codigo[0]:
                                producto_existente = item
                                break

                        cantidad_en_carrito = producto_existente["cantidad"] if producto_existente else 0
                        if cantidad_en_carrito + cantidad_codigo > producto_codigo[14]:
                            st.error("No puedes agregar más piezas que el stock disponible.")
                        else:
                            if producto_existente:
                                producto_existente["cantidad"] += cantidad_codigo
                                producto_existente["total"] = producto_existente["cantidad"] * producto_existente["precio_unitario"]
                                producto_existente["utilidad"] = (producto_existente["precio_unitario"] - producto_existente["costo_unitario"]) * producto_existente["cantidad"]
                            else:
                                st.session_state.carrito.append({
                                    "producto_id": producto_codigo[0],
                                    "nombre": producto_codigo[2],
                                    "codigo": producto_codigo[1],
                                    "cantidad": cantidad_codigo,
                                    "precio_unitario": producto_codigo[12],
                                    "costo_unitario": producto_codigo[11],
                                    "total": producto_codigo[12] * cantidad_codigo,
                                    "utilidad": (producto_codigo[12] - producto_codigo[11]) * cantidad_codigo
                                })
                            st.success(f"Agregado: {producto_codigo[2]}")
                            if "codigo_escaneado_venta" in st.session_state:
                                del st.session_state["codigo_escaneado_venta"]
                            st.rerun()

            elif modo_qr == "Cámara":
                st.info("Toma foto del QR. Procura buena luz y que el QR ocupe buena parte de la imagen.")
                foto_qr = st.camera_input("Escanear QR", key="foto_qr_venta")
                codigo_detectado = leer_qr_desde_imagen(foto_qr)

                col_cam1, col_cam2 = st.columns([1, 1])
                with col_cam1:
                    cantidad_codigo = st.number_input("Cantidad", min_value=1, step=1, value=1, key="cantidad_codigo_camara")
                with col_cam2:
                    if codigo_detectado:
                        st.success(f"Detectado: {codigo_detectado}")
                    elif foto_qr is not None:
                        st.error("No pude leer el QR.")

                if st.button("Agregar QR detectado", type="primary", disabled=not bool(codigo_detectado), key="btn_agregar_camara"):
                    producto_codigo = obtener_producto_por_codigo(codigo_detectado)
                    if not producto_codigo:
                        st.error("No encontré un producto con ese código.")
                    elif producto_codigo[14] <= 0:
                        st.error("Ese producto no tiene stock disponible.")
                    else:
                        producto_existente = None
                        for item in st.session_state.carrito:
                            if item["producto_id"] == producto_codigo[0]:
                                producto_existente = item
                                break
                        cantidad_en_carrito = producto_existente["cantidad"] if producto_existente else 0
                        if cantidad_en_carrito + cantidad_codigo > producto_codigo[14]:
                            st.error("No puedes agregar más piezas que el stock disponible.")
                        else:
                            if producto_existente:
                                producto_existente["cantidad"] += cantidad_codigo
                                producto_existente["total"] = producto_existente["cantidad"] * producto_existente["precio_unitario"]
                                producto_existente["utilidad"] = (producto_existente["precio_unitario"] - producto_existente["costo_unitario"]) * producto_existente["cantidad"]
                            else:
                                st.session_state.carrito.append({
                                    "producto_id": producto_codigo[0],
                                    "nombre": producto_codigo[2],
                                    "codigo": producto_codigo[1],
                                    "cantidad": cantidad_codigo,
                                    "precio_unitario": producto_codigo[12],
                                    "costo_unitario": producto_codigo[11],
                                    "total": producto_codigo[12] * cantidad_codigo,
                                    "utilidad": (producto_codigo[12] - producto_codigo[11]) * cantidad_codigo
                                })
                            st.success(f"Agregado: {producto_codigo[2]}")
                            st.rerun()

            else:
                opciones = {f"{p[1]} | {p[2]} | Stock: {p[14]} | {formato_moneda(p[12])}": p[0] for p in productos_disponibles}
                seleccion = st.selectbox("Producto", list(opciones.keys()))
                producto_id = opciones[seleccion]
                producto = obtener_producto_por_id(producto_id)

                col_m1, col_m2 = st.columns([1, 2])
                with col_m1:
                    imagen = obtener_imagen_preferida(producto)
                    if imagen:
                        st.image(imagen, use_container_width=True)
                with col_m2:
                    st.write(f"**{producto[2]}**")
                    st.caption(producto[1])
                    st.write(f"Precio: {formato_moneda(producto[12])}")
                    st.write(f"Stock: {producto[14]}")
                    cantidad = st.number_input("Cantidad", min_value=1, max_value=producto[14], step=1, key="cantidad_manual_venta")

                if st.button("Agregar producto", type="primary", key="btn_agregar_manual"):
                    producto_existente = None
                    for item in st.session_state.carrito:
                        if item["producto_id"] == producto_id:
                            producto_existente = item
                            break
                    cantidad_en_carrito = producto_existente["cantidad"] if producto_existente else 0
                    if cantidad_en_carrito + cantidad > producto[14]:
                        st.error("No puedes agregar más piezas que el stock disponible.")
                    else:
                        if producto_existente:
                            producto_existente["cantidad"] += cantidad
                            producto_existente["total"] = producto_existente["cantidad"] * producto_existente["precio_unitario"]
                            producto_existente["utilidad"] = (producto_existente["precio_unitario"] - producto_existente["costo_unitario"]) * producto_existente["cantidad"]
                        else:
                            st.session_state.carrito.append({
                                "producto_id": producto_id,
                                "nombre": producto[2],
                                "codigo": producto[1],
                                "cantidad": cantidad,
                                "precio_unitario": producto[12],
                                "costo_unitario": producto[11],
                                "total": producto[12] * cantidad,
                                "utilidad": (producto[12] - producto[11]) * cantidad
                            })
                        st.success("Producto agregado.")
                        st.rerun()

            st.divider()
            st.subheader("Carrito")

            if not st.session_state.carrito:
                st.info("Carrito vacío.")
            else:
                for idx, item in enumerate(st.session_state.carrito):
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([3, 1, 1])
                        c1.write(f"**{item['nombre']}**")
                        c1.caption(item.get("codigo", ""))
                        c2.write(f"x{item['cantidad']}")
                        c2.caption(formato_moneda(item["precio_unitario"]))
                        c3.write(f"**{formato_moneda(item['total'])}**")
                        if c3.button("Quitar", key=f"quitar_{idx}"):
                            st.session_state.carrito.pop(idx)
                            st.rerun()

                total_carrito = sum(item["total"] for item in st.session_state.carrito)
                utilidad_carrito = sum(item["utilidad"] for item in st.session_state.carrito)

                st.divider()
                st.subheader("Cobro")
                pago1, pago2 = st.columns(2)
                with pago1:
                    monto_recibido = st.number_input("Recibido", min_value=0.0, step=1.0, value=0.0, key="monto_recibido_venta")
                cambio = monto_recibido - total_carrito
                falta = total_carrito - monto_recibido
                with pago2:
                    if monto_recibido >= total_carrito:
                        st.metric("Cambio", formato_moneda(cambio))
                    else:
                        st.metric("Falta", formato_moneda(falta))

                b1, b2 = st.columns(2)
                with b1:
                    if st.button("✅ Cobrar venta", type="primary", disabled=monto_recibido < total_carrito):
                        success, result = registrar_venta_carrito(st.session_state.carrito)
                        if success:
                            st.success(f"Venta registrada: {formato_moneda(result['total'])} | Cambio: {formato_moneda(cambio)}")
                            st.session_state.carrito = []
                            if "monto_recibido_venta" in st.session_state:
                                del st.session_state["monto_recibido_venta"]
                            st.rerun()
                        else:
                            st.error(result)
                with b2:
                    if st.button("🧹 Vaciar carrito"):
                        st.session_state.carrito = []
                        if "monto_recibido_venta" in st.session_state:
                            del st.session_state["monto_recibido_venta"]
                        st.rerun()

# =========================
# DASHBOARD
# =========================

elif menu == "Dashboard":
    st.header("Dashboard")

    productos = obtener_productos()
    ventas = obtener_ventas()

    total_ventas = sum(v[5] for v in ventas)
    utilidad_total = sum(v[6] for v in ventas)
    total_productos = len(productos)
    total_stock = sum(p[14] for p in productos)
    productos_bajo_stock = sum(1 for p in productos if p[14] <= p[15])

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Ventas totales", formato_moneda(total_ventas))
    col2.metric("Utilidad estimada", formato_moneda(utilidad_total))
    col3.metric("Productos", total_productos)
    col4.metric("Stock total", total_stock)
    col5.metric("Alertas stock", productos_bajo_stock)

    st.divider()
    st.subheader("Últimas ventas")

    if not ventas:
        st.info("Todavía no hay ventas registradas.")
    else:
        for v in ventas[:20]:
            with st.container(border=True):
                st.write(f"**{v[1]}**")
                st.write(f"Cantidad: {v[2]} | Precio: {formato_moneda(v[3])} | Total: {formato_moneda(v[5])} | Utilidad: {formato_moneda(v[6])}")
                st.caption(v[7])
