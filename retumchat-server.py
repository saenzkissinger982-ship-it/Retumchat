#!/usr/bin/env python3
# RetumChat Server · servidor de chat propio para Termux
# Uso: python3 retumchat-server.py [puerto]
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8090"))
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retumchat-data")
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "chat.db")
FILES = os.path.join(DATA, "files")
os.makedirs(FILES, exist_ok=True)

lock = threading.Lock()
cond = threading.Condition(lock)

# --- control de reenvios duplicados ---
FP_LOCK = threading.Lock()
FP = {}          # huella -> (evento, momento)
FP_VENTANA = 25  # segundos


def huella(ruta, nombre, cuerpo):
    h = hashlib.sha1()
    h.update((ruta + "|" + (nombre or "") + "|" + str(len(cuerpo))).encode("utf-8"))
    h.update(cuerpo)
    return h.hexdigest()


def duplicado_reciente(fp):
    ahora = time.time()
    with FP_LOCK:
        for k in [k for k, v in FP.items() if ahora - v[1] > FP_VENTANA]:
            FP.pop(k, None)
        prev = FP.get(fp)
        if prev and ahora - prev[1] <= FP_VENTANA:
            return prev[0]
    return None


# --- quienes estan usando la app ---
PRES = {}
PRES_LOCK = threading.Lock()
PRES_VENTANA = 70


def presencia_actualizar(nick):
    if not nick:
        return 0
    with PRES_LOCK:
        PRES[nick] = time.time()
        ahora = time.time()
        for k in [k for k, v in PRES.items() if ahora - v > PRES_VENTANA]:
            PRES.pop(k, None)
        return len(PRES)


def presencia_contar():
    with PRES_LOCK:
        ahora = time.time()
        for k in [k for k, v in PRES.items() if ahora - v > PRES_VENTANA]:
            PRES.pop(k, None)
        return len(PRES), sorted(PRES.keys())


def recordar(fp, evento):
    with FP_LOCK:
        FP[fp] = (evento, time.time())

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS mensajes (
        id TEXT PRIMARY KEY, topic TEXT, author TEXT, texto TEXT,
        attach_name TEXT, attach_type TEXT, attach_path TEXT, attach_size INTEGER,
        ts INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_topic_ts ON mensajes(topic, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS perfiles (
        nombre TEXT PRIMARY KEY, archivo TEXT, admin INTEGER, ts INTEGER)""")
    c.commit()
    c.close()

init()

def nuevo_id():
    return uuid.uuid4().hex[:12]

def evento(row):
    ev = {
        "id": row["id"],
        "time": row["ts"],
        "expires": row["ts"] + 86400 * 365,
        "event": "message",
        "topic": row["topic"],
        "message": row["texto"] if row["texto"] is not None else "",
    }
    if row["attach_path"]:
        ev["attachment"] = {
            "name": row["attach_name"],
            "type": row["attach_type"],
            "size": row["attach_size"],
            "expires": row["ts"] + 86400 * 365,
            "url": "/file/" + os.path.basename(row["attach_path"]),
        }
    return ev

def guardar(topic, texto, att):
    ts = int(time.time())
    mid = nuevo_id()
    c = db()
    with cond:
        c.execute("INSERT INTO mensajes VALUES (?,?,?,?,?,?,?,?,?)", (
            mid, topic, "", texto,
            att["name"] if att else None,
            att["type"] if att else None,
            att["path"] if att else None,
            att["size"] if att else 0,
            ts))
        c.commit()
        cond.notify_all()
    c.close()
    return mid, ts

def desde(item):
    if not item or item == "all":
        return None
    m = re.match(r"^(\d+)([smhd]?)$", str(item))
    if not m:
        return None
    val = int(m.group(1))
    unidad = m.group(2)
    if unidad == "m": val *= 60
    elif unidad == "h": val *= 3600
    elif unidad == "d": val *= 86400
    elif unidad == "s": pass
    else:  # timestamp unix absoluto
        return val
    return int(time.time()) - val

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RetumChat/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    # ---------- subir mensaje o archivo ----------
    def do_POST(self):
        ruta = urlparse(self.path).path.strip("/")
        if not ruta:
            return self._json({"error": "falta el canal"}, 400)
        if ruta == "perfil":
            largo = int(self.headers.get("Content-Length") or 0)
            cuerpo = self.rfile.read(largo) if largo else b"{}"
            try:
                datos = json.loads(cuerpo.decode("utf-8", "replace"))
                nombre = (datos.get("n") or "").strip()[:40]
                foto = datos.get("foto") or ""
            except Exception:
                return self._json({"error": "datos invalidos"}, 400)
            if not nombre:
                return self._json({"error": "falta el nombre"}, 400)
            if not foto:
                return self._json({"error": "falta la foto"}, 400)
            try:
                crudo = base64.b64decode(foto.split(",")[-1])
            except Exception:
                return self._json({"error": "foto invalida"}, 400)
            if len(crudo) > 3 * 1024 * 1024:
                return self._json({"error": "foto muy grande"}, 413)
            archivo = "perfil_" + hashlib.md5(nombre.lower().encode("utf-8")).hexdigest() + ".jpg"
            ruta_perfil = os.path.join(FILES, archivo)
            with open(ruta_perfil, "wb") as f:
                f.write(crudo)
            nl = nombre.lower()
            admin = 1 if ("retumbero" in nl and "agente" not in nl) else 0
            c = db()
            c.execute("INSERT INTO perfiles (nombre, archivo, admin, ts) VALUES (?,?,?,?) "
                      "ON CONFLICT(nombre) DO UPDATE SET archivo=excluded.archivo, admin=excluded.admin, ts=excluded.ts",
                      (nombre, archivo, admin, int(time.time())))
            c.commit()
            c.close()
            return self._json({"ok": True, "n": nombre, "url": "/file/" + archivo, "admin": admin})

        if ruta == "presencia":
            largo = int(self.headers.get("Content-Length") or 0)
            cuerpo = self.rfile.read(largo) if largo else b"{}"
            try:
                nick = json.loads(cuerpo.decode("utf-8", "replace")).get("n", "")
            except Exception:
                nick = ""
            total = presencia_actualizar(nick.strip()[:40])
            return self._json({"online": total})

        largo = int(self.headers.get("Content-Length") or 0)
        cuerpo = self.rfile.read(largo) if largo else b""
        att = None
        nombre = self.headers.get("Filename")
        fp = huella(ruta, nombre, cuerpo)
        previo = duplicado_reciente(fp)
        if previo is not None:
            return self._json(previo)
        if nombre:
            tipo = self.headers.get("Content-Type") or "application/octet-stream"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", unquote(nombre))
            midp = nuevo_id()
            ruta_arch = os.path.join(FILES, midp + "_" + safe)
            with open(ruta_arch, "wb") as f:
                f.write(cuerpo)
            att = {"name": safe, "type": tipo, "path": ruta_arch, "size": len(cuerpo)}
            texto = self.headers.get("Message") or ""
        else:
            texto = cuerpo.decode("utf-8", "replace")
        mid, ts = guardar(ruta, texto, att)
        ev = {"id": mid, "time": ts, "event": "message", "topic": ruta,
              "message": texto}
        if att:
            ev["attachment"] = {"name": att["name"], "type": att["type"],
                                "size": att["size"], "url": "/file/" + os.path.basename(att["path"])}
        recordar(fp, ev)
        return self._json(ev)

    # ---------- leer ----------
    def do_GET(self):
        u = urlparse(self.path)
        ruta = u.path.strip("/")
        q = parse_qs(u.query)

        if ruta in ("", "health"):
            return self._json({"ok": True, "server": "RetumChat", "hora": int(time.time())})

        if ruta == "perfiles":
            lista = []
            c = db()
            try:
                for f in c.execute("SELECT nombre, archivo, admin, ts FROM perfiles ORDER BY ts DESC LIMIT 200").fetchall():
                    if f["archivo"] and os.path.isfile(os.path.join(FILES, f["archivo"])):
                        lista.append({"n": f["nombre"], "url": "/file/" + f["archivo"],
                                      "admin": int(f["admin"] or 0), "ts": f["ts"]})
                # tambien la gente que ya escribio alguna vez
                nombres = set(x["n"] for x in lista)
                for f in c.execute("SELECT DISTINCT author FROM mensajes WHERE author <> '' LIMIT 300").fetchall():
                    if f["author"] not in nombres:
                        lista.append({"n": f["author"], "url": "", "admin": 0, "ts": 0})
            finally:
                c.close()
            return self._json({"perfiles": lista})

        if ruta == "presencia":
            total, nombres = presencia_contar()
            return self._json({"online": total, "nombres": nombres})

        if ruta.startswith("file/"):
            nombre = os.path.basename(ruta[5:])
            ruta_arch = os.path.join(FILES, nombre)
            if not os.path.isfile(ruta_arch):
                return self._json({"error": "no existe"}, 404)
            with open(ruta_arch, "rb") as f:
                datos = f.read()
            self.send_response(200)
            tipos = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".3gp": "audio/3gpp",
                     ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
                     ".opus": "audio/ogg", ".aac": "audio/aac", ".flac": "audio/flac",
                     ".apk": "application/vnd.android.package-archive", ".pdf": "application/pdf",
                     ".zip": "application/zip", ".txt": "text/plain; charset=utf-8"}
            ext = os.path.splitext(nombre.lower())[1]
            tipo = tipos.get(ext, "application/octet-stream")
            self.send_header("Content-Type", tipo)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
            return

        if ruta.endswith("/sse"):
            topic = ruta[:-4].strip("/")
            return self._sse(topic, q)

        if ruta.endswith("/json"):
            topic = ruta[:-5].strip("/")
            return self._poll(topic, q)

        topic = ruta
        return self._poll(topic, q)

    def _traer(self, topic, since, limite=200):
        c = db()
        if since is None:
            filas = c.execute("SELECT * FROM mensajes WHERE topic=? ORDER BY ts DESC LIMIT ?",
                              (topic, limite)).fetchall()
        else:
            filas = c.execute("SELECT * FROM mensajes WHERE topic=? AND ts>=? ORDER BY ts ASC LIMIT ?",
                              (topic, int(since), limite)).fetchall()
        c.close()
        return [evento(f) for f in filas]

    def _poll(self, topic, q):
        since = desde((q.get("since") or ["all"])[0])
        eventos = self._traer(topic, since)
        cuerpo = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eventos).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _sse(self, topic, q):
        since = desde((q.get("since") or ["all"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(b"event: open\ndata: {\"event\":\"open\"}\n\n")
        self.wfile.flush()
        ultimo = int(time.time()) if since is None else int(since)
        enviados = set()
        try:
            while True:
                # primero entrega lo que ya hay, despues se queda esperando
                filas = self._traer(topic, ultimo)
                for e in filas:
                    if e["id"] in enviados:
                        continue
                    enviados.add(e["id"])
                    self.wfile.write(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    if int(e["time"]) > ultimo:
                        ultimo = int(e["time"])
                self.wfile.write(b": latido\n\n")
                self.wfile.flush()
                with cond:
                    cond.wait(timeout=15)
        except Exception:
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("RetumChat Server escuchando en el puerto", PORT)
    srv.serve_forever()
