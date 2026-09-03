"""Acceso a Google Sheets y Drive con service account, via HTTP directo.
Mismo enfoque que el bot de produccion: sin gspread, sin conflictos de httpx."""
import json
import re
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


class Google:
    def __init__(self, credentials_json: str, sheet_id: str, folder_id: str):
        info = json.loads(credentials_json)
        self.creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        self.sheet_id = sheet_id
        self.folder_id = folder_id
        self.sa_email = info.get("client_email", "")

    # ---------- auth ----------
    def _headers(self):
        if not self.creds.valid:
            self.creds.refresh(Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    # ---------- lectura ----------
    def get(self, rango: str):
        url = f"{SHEETS}/{self.sheet_id}/values/{rango}"
        r = requests.get(url, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("values", [])

    def columna(self, hoja: str, letra: str, desde: int, hasta: int):
        """Devuelve la columna como lista de strings, rellenando vacios hasta `hasta`."""
        vals = self.get(f"{hoja}!{letra}{desde}:{letra}{hasta}")
        out = [(v[0] if v else "") for v in vals]
        out += [""] * ((hasta - desde + 1) - len(out))
        return out

    def primera_fila_vacia(self, hoja: str, letra: str, desde: int, hasta: int):
        col = self.columna(hoja, letra, desde, hasta)
        for i, v in enumerate(col):
            if str(v).strip() == "":
                return desde + i
        return None

    # ---------- escritura ----------
    def batch_update(self, data: list):
        """data = [{"range": "Compras!B10", "values": [[...]]}, ...]"""
        url = f"{SHEETS}/{self.sheet_id}/values:batchUpdate"
        body = {"valueInputOption": "USER_ENTERED", "data": data}
        r = requests.post(url, headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def batch_clear(self, rangos: list):
        url = f"{SHEETS}/{self.sheet_id}/values:batchClear"
        r = requests.post(url, headers=self._headers(), json={"ranges": rangos}, timeout=30)
        r.raise_for_status()

    def append(self, hoja: str, fila: list):
        url = f"{SHEETS}/{self.sheet_id}/values/{hoja}!A1:append"
        r = requests.post(url, headers=self._headers(),
                          params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                          json={"values": [fila]}, timeout=30)
        r.raise_for_status()

    def asegurar_hoja(self, nombre: str, encabezados: list):
        """Crea la hoja si no existe y le pone encabezados."""
        r = requests.get(f"{SHEETS}/{self.sheet_id}", headers=self._headers(),
                         params={"fields": "sheets.properties.title"}, timeout=30)
        r.raise_for_status()
        titulos = [s["properties"]["title"] for s in r.json().get("sheets", [])]
        if nombre in titulos:
            return
        body = {"requests": [{"addSheet": {"properties": {"title": nombre}}}]}
        requests.post(f"{SHEETS}/{self.sheet_id}:batchUpdate", headers=self._headers(),
                      json=body, timeout=30).raise_for_status()
        self.batch_update([{"range": f"{nombre}!A1", "values": [encabezados]}])

    # ---------- drive ----------
    def subir_archivo(self, nombre: str, contenido: bytes, mime: str) -> dict:
        meta = {"name": nombre, "parents": [self.folder_id]}
        files = {
            "metadata": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
            "file": (nombre, contenido, mime),
        }
        r = requests.post(DRIVE_UPLOAD, headers=self._headers(),
                          params={"uploadType": "multipart", "fields": "id,webViewLink",
                                  "supportsAllDrives": "true"},
                          files=files, timeout=120)
        r.raise_for_status()
        return r.json()


def solo_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def cuit_con_guiones(c: str) -> str:
    c = solo_digitos(c)
    return f"{c[:2]}-{c[2:10]}-{c[10:]}" if len(c) == 11 else c
