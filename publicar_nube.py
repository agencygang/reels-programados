"""
Publicador en la nube (corre en GitHub Actions, con tu PC apagada).
Lo sube `python editor.py activar-nube`; no hace falta tocarlo.

Cada archivo cola/NN.json es un reel: cuando llega su hora lo baja del release "videos",
lo publica en Instagram con la API oficial y anota el resultado.
"""
import glob, json, os, random, subprocess, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

CFG = json.load(open("config.json", encoding="utf-8"))
TZ = timezone(timedelta(minutes=CFG["utc_offset_min"]))
FMT = "%Y-%m-%d %H:%M"
IG_API = "https://graph.instagram.com/v25.0"
TMP = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "videos")


def ahora():
    return datetime.now(TZ).replace(tzinfo=None)


def log(msg):
    print(f"{ahora():%Y-%m-%d %H:%M} {msg}", flush=True)


# ------------------------------------------------------------------ GIT (estado)
def guardar(d, msg):
    ruta = f"cola/{d['num']}.json"
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in d.items() if k != "_ruta"}, f, ensure_ascii=False, indent=2)
    subir_cambios([ruta], msg)


def subir_cambios(rutas, msg):
    subprocess.run(["git", "add", *rutas], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
    for _ in range(6):
        if subprocess.run(["git", "pull", "-q", "--rebase"]).returncode != 0:
            subprocess.run(["git", "rebase", "--abort"])
        elif subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        time.sleep(10)
    raise RuntimeError("No se pudo guardar el estado en GitHub")


# ------------------------------------------------------------------ INSTAGRAM
def abrir(req, timeout=120):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode(errors="replace")
        try:
            msg = json.loads(cuerpo)["error"]["message"]
        except Exception:
            msg = cuerpo[:300]
        raise RuntimeError(f"Instagram {e.code}: {msg}") from None


def ig(ruta, token, datos=None, metodo="POST"):
    url = ruta if ruta.startswith("http") else f"{IG_API}/{ruta}"
    q = urllib.parse.urlencode(dict(datos or {}, access_token=token))
    if metodo == "GET":
        return abrir(urllib.request.Request(f"{url}?{q}"))
    return abrir(urllib.request.Request(url, data=q.encode(), method="POST"))


def token_vigente():
    """El token de Instagram dura 60 dias: se renueva cada semana y se guarda en el secreto IG_TOKEN."""
    token = os.environ["IG_TOKEN"]
    renovado = date.fromisoformat(open("renovado.txt").read().strip())
    if (date.today() - renovado).days < 7:
        return token
    try:
        nuevo = ig("https://graph.instagram.com/refresh_access_token", token, {"grant_type": "ig_refresh_token"}, "GET")["access_token"]
        print(f"::add-mask::{nuevo}")
        subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--body", nuevo, "-R", os.environ["REPO"]], check=True,
                       env=dict(os.environ, GH_TOKEN=os.environ["GH_PAT"]), capture_output=True)
        with open("renovado.txt", "w") as f:
            f.write(date.today().isoformat())
        subir_cambios(["renovado.txt"], "token renovado")
        log("Token de Instagram renovado")
        return nuevo
    except Exception as e:
        log(f"AVISO: no se pudo renovar el token ({e})")
        return token


def subir_reel(d, token):
    os.makedirs(TMP, exist_ok=True)
    video = os.path.join(TMP, d["asset"])
    subprocess.run(["gh", "release", "download", "videos", "-p", d["asset"], "-D", TMP, "--clobber"], check=True)
    datos = {"media_type": "REELS", "upload_type": "resumable", "caption": d["caption"]}
    if CFG["reel_de_prueba"]:
        datos["trial_params"] = json.dumps({"graduation_strategy": CFG["paso_a_seguidores"]})
    ig_id = os.environ["IG_ID"]
    contenedor = ig(f"{ig_id}/media", token, datos)["id"]
    tam = os.path.getsize(video)
    with open(video, "rb") as f:
        abrir(urllib.request.Request(f"https://rupload.facebook.com/ig-api-upload/v25.0/{contenedor}", data=f, method="POST",
                                     headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(tam),
                                              "Content-Length": str(tam)}), timeout=1800)
    for _ in range(90):  # hasta 15 min procesando
        r = ig(contenedor, token, {"fields": "status_code,status"}, "GET")
        if r.get("status_code") == "FINISHED":
            break
        if r.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram no pudo procesar el video: {r.get('status') or r['status_code']}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram tardo demasiado procesando el video")
    return ig(f"{ig_id}/media_publish", token, {"creation_id": contenedor})["id"]


# ------------------------------------------------------------------ CALENDARIO
def franja(t):
    for i, (a, b) in enumerate(CFG["horarios"]):
        if a <= t.strftime("%H:%M") <= b:
            return i


def huecos_libres(cola, desde):
    ocupados = {(datetime.strptime(d["publicar_en"], FMT).date(), franja(datetime.strptime(d["publicar_en"], FMT)))
                for d in cola if d["estado"] == "programado"}
    dia = desde.date()
    while True:
        for i, (a, b) in enumerate(CFG["horarios"]):
            ini = max(datetime.combine(dia, datetime.strptime(a, "%H:%M").time()), desde + timedelta(minutes=10))
            fin = datetime.combine(dia, datetime.strptime(b, "%H:%M").time())
            if (dia, i) in ocupados or ini >= fin:
                continue
            ocupados.add((dia, i))
            yield ini + timedelta(minutes=random.randint(0, int((fin - ini).total_seconds() // 60)))
        dia += timedelta(days=1)


def main():
    cola = [json.load(open(p, encoding="utf-8")) for p in sorted(glob.glob("cola/*.json"))]
    t = ahora()
    turno = []
    for d in cola:
        if d["estado"] != "programado" or datetime.strptime(d["publicar_en"], FMT) > t:
            continue
        if t - datetime.strptime(d["publicar_en"], FMT) > timedelta(minutes=CFG["atraso_maximo"]):
            d["publicar_en"] = next(huecos_libres(cola, t)).strftime(FMT)
            log(f"{d['num']} se paso de hora. Nueva hora: {d['publicar_en']}")
            guardar(d, f"reprogramar {d['num']}")
        else:
            turno.append(d)
    if not turno:
        log("Nada que publicar ahora")
        return
    d = min(turno, key=lambda d: d["publicar_en"])  # uno por vez
    d["estado"] = "subiendo"  # se guarda ANTES de subir: asi nunca se publica dos veces
    guardar(d, f"subiendo {d['num']}")
    log(f"{d['num']} subiendo a Instagram...")
    try:
        d["ig_media"] = subir_reel(d, token_vigente())
        d["estado"], d["publicado_en"], d["nota"] = "publicado", ahora().strftime(FMT), ""
        log(f"{d['num']} publicado {'como reel de prueba' if CFG['reel_de_prueba'] else ''}")
        subprocess.run(["gh", "release", "delete-asset", "videos", d["asset"], "-y"])  # libera espacio
    except Exception as e:
        d["estado"], d["nota"] = "error subida", str(e)[:500]
        log(f"{d['num']} ERROR: {e}")
    guardar(d, f"{d['estado']} {d['num']}")


if __name__ == "__main__":
    main()
