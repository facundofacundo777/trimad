"""Configuracion del bot. Todo sale de variables de entorno (Railway > Variables)."""
import os


def _ids(name):
    raw = os.environ.get(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

SHEET_ID = os.environ["SHEET_ID"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

# CUIT de la empresa (solo digitos). Si el emisor es este CUIT, el comprobante es una VENTA.
MI_CUIT = os.environ.get("MI_CUIT", "20380792983")

ALLOWED_USER_IDS = _ids("ALLOWED_USER_IDS")        # quienes pueden cargar
ALLOWED_CHAT_IDS = _ids("ALLOWED_CHAT_IDS")        # grupo(s) habilitados
ADMIN_USER_IDS = _ids("ADMIN_USER_IDS") or ALLOWED_USER_IDS  # quienes pueden /deshacer

MODEL = os.environ.get("MODEL", "claude-sonnet-5")

# Nombres de hojas y rangos (coinciden con la planilla que armamos)
HOJA_COMPRAS = "Compras"
HOJA_VENTAS = "Ventas"
HOJA_ENTIDADES = "Entidades"
HOJA_CONFIG = "Config"
HOJA_LOG = "Log"
FILA_INICIO = 4          # primera fila de datos
FILA_FIN = 903           # ultima fila con formulas
ENT_FILA_FIN = 303

TOLERANCIA = 1.0         # pesos: diferencia aceptada al cuadrar neto+iva+otros vs total
UNDO_MINUTOS = 10
