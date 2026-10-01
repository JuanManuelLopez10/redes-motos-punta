#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — Publicador automático de contenido a Instagram y Facebook.

Qué hace
--------
1. Recorre las carpetas  Feed/  y  Stories/  buscando archivos cuyo nombre
   codifica la fecha/hora de publicación con el formato  "DD-MM HH-MM".
2. Agrupa los archivos en "publicaciones":
      - Feed, varios archivos "DD-MM HH-MM N"  -> carrusel (ordenado por N).
      - Feed, un archivo imagen "DD-MM HH-MM"   -> foto simple.
      - Feed, un archivo video  "DD-MM HH-MM"   -> reel.
      - Stories, cada archivo                    -> una historia.
3. Para cada publicación cuya hora ya llegó (<= ahora, zona America/Montevideo)
   la publica en Instagram y Facebook vía la Graph API de Meta.
4. Al publicar con éxito mueve los archivos (media + caption .txt) a la
   subcarpeta  Subidas/  correspondiente, registra el estado y hace commit.

El caption/descripción de cada publicación se lee de un archivo de texto con el
mismo "nombre base" y extensión .txt  (ej: "25-06 18-00.txt").

Diseñado para correr en GitHub Actions (PC apagada). El propio repositorio
hace de hosting público: Meta descarga las imágenes/videos desde las URLs
raw.githubusercontent.com, por eso el repo debe ser PÚBLICO.

Uso
---
    python publish.py                 # corrida real
    python publish.py --dry-run       # muestra qué haría, sin publicar ni mover
    python publish.py --now "01-10 18-05"   # simula la hora actual (para probar)

Variables de entorno (se cargan desde los Secrets del repo en Actions)
----------------------------------------------------------------------
    META_ACCESS_TOKEN   (obligatoria)  Token de larga duración / System User.
    IG_USER_ID          (obligatoria para IG)  ID de la cuenta de Instagram.
    FB_PAGE_ID          (obligatoria para FB)  ID de la página de Facebook.
    GITHUB_REPOSITORY   (la pone Actions)  "owner/repo", para armar las URLs raw.
    GITHUB_REF_NAME     (la pone Actions)  rama actual (default "main").
    PUBLISH_TO_IG       "1"/"0"  (default 1)
    PUBLISH_TO_FB       "1"/"0"  (default 1)
    GIT_COMMIT          "1"/"0"  (default 1)  hacer commit+push de los cambios.
    GRAPH_VERSION       (default "v21.0")
    RAW_BASE_URL        (opcional) sobreescribe la base de las URLs públicas.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

TZ = ZoneInfo("America/Montevideo")

ROOT = Path(__file__).resolve().parent
FEED_DIR = ROOT / "Feed"
STORIES_DIR = ROOT / "Stories"
STATE_FILE = ROOT / "state" / "published.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Patrón de nombre:  DD-MM HH-MM  con un número de orden opcional al final.
NAME_RE = re.compile(r"^(\d{2})-(\d{2}) (\d{2})-(\d{2})(?: (\d+))?$")

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Cuánto esperar (segundos) a que Meta procese un video antes de rendirse.
VIDEO_POLL_TIMEOUT = 300
VIDEO_POLL_INTERVAL = 5


def log(msg: str) -> None:
    ts = dt.datetime.now(TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Modelo de datos
# --------------------------------------------------------------------------- #

@dataclass
class MediaItem:
    path: Path
    order: Optional[int]  # N del carrusel, o None

    @property
    def is_video(self) -> bool:
        return self.path.suffix.lower() in VIDEO_EXTS

    @property
    def is_image(self) -> bool:
        return self.path.suffix.lower() in IMAGE_EXTS


@dataclass
class Post:
    key: str                 # "DD-MM HH-MM"  (identificador de la publicación)
    kind: str                # "photo" | "carousel" | "reel" | "story"
    scheduled: dt.datetime   # fecha/hora de publicación (con tz)
    items: list[MediaItem] = field(default_factory=list)
    caption: str = ""
    source: str = "feed"     # "feed" | "stories"

    @property
    def files(self) -> list[Path]:
        return [it.path for it in self.items]


# --------------------------------------------------------------------------- #
# Parsing de nombres y descubrimiento de publicaciones
# --------------------------------------------------------------------------- #

def parse_name(stem: str) -> Optional[tuple[int, int, int, int, Optional[int]]]:
    m = NAME_RE.match(stem.strip())
    if not m:
        return None
    day, month, hour, minute = (int(m.group(i)) for i in range(1, 5))
    order = int(m.group(5)) if m.group(5) else None
    return day, month, hour, minute, order


def resolve_datetime(day: int, month: int, hour: int, minute: int,
                     now: dt.datetime) -> dt.datetime:
    """
    El nombre no trae año. Se elige el año que deja la fecha más cercana a
    'now' (dentro de +-6 meses), para manejar bien el cambio de diciembre a
    enero.
    """
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(dt.datetime(year, month, day, hour, minute, tzinfo=TZ))
        except ValueError:
            continue  # ej. 29-02 en año no bisiesto
    if not candidates:
        raise ValueError(f"Fecha inválida: {day:02d}-{month:02d} {hour:02d}:{minute:02d}")
    return min(candidates, key=lambda c: abs((c - now).total_seconds()))


def read_caption(directory: Path, key: str) -> str:
    txt = directory / f"{key}.txt"
    if txt.exists():
        return txt.read_text(encoding="utf-8").strip()
    return ""


def discover_feed_posts(now: dt.datetime) -> list[Post]:
    """Agrupa los archivos de Feed/ (no recursivo, ignora Subidas/) en Posts."""
    groups: dict[str, list[MediaItem]] = {}
    for p in sorted(FEED_DIR.iterdir()):
        if p.is_dir() or p.suffix.lower() not in MEDIA_EXTS:
            continue
        parsed = parse_name(p.stem)
        if not parsed:
            log(f"  ⚠ Nombre no reconocido, se ignora: {p.name}")
            continue
        day, month, hour, minute, order = parsed
        key = f"{day:02d}-{month:02d} {hour:02d}-{minute:02d}"
        groups.setdefault(key, []).append(MediaItem(p, order))

    posts: list[Post] = []
    for key, items in groups.items():
        day, month, hour, minute, _ = parse_name(items[0].path.stem)  # type: ignore
        scheduled = resolve_datetime(day, month, hour, minute, now)
        caption = read_caption(FEED_DIR, key)

        numbered = [it for it in items if it.order is not None]
        plain = [it for it in items if it.order is None]

        if numbered and plain:
            log(f"  ⚠ {key}: mezcla de archivos con y sin número; uso los "
                f"numerados como carrusel e ignoro el resto.")

        if numbered:
            numbered.sort(key=lambda it: it.order)  # type: ignore
            if len(numbered) == 1:
                it = numbered[0]
                kind = "reel" if it.is_video else "photo"
                posts.append(Post(key, kind, scheduled, [it], caption, "feed"))
            else:
                posts.append(Post(key, "carousel", scheduled, numbered, caption, "feed"))
        else:
            if len(plain) == 1:
                it = plain[0]
                kind = "reel" if it.is_video else "photo"
                posts.append(Post(key, kind, scheduled, [it], caption, "feed"))
            else:
                # Varios sin número y mismo horario: lo tomamos como carrusel
                # en orden alfabético, avisando.
                log(f"  ⚠ {key}: {len(plain)} archivos sin número con el mismo "
                    f"horario; los publico como carrusel en orden de nombre.")
                plain.sort(key=lambda it: it.path.name)
                posts.append(Post(key, "carousel", scheduled, plain, caption, "feed"))
    return posts


def discover_story_posts(now: dt.datetime) -> list[Post]:
    """Cada archivo de Stories/ es una historia independiente."""
    posts: list[Post] = []
    for p in sorted(STORIES_DIR.iterdir()):
        if p.is_dir() or p.suffix.lower() not in MEDIA_EXTS:
            continue
        parsed = parse_name(p.stem)
        if not parsed:
            log(f"  ⚠ Nombre no reconocido, se ignora: {p.name}")
            continue
        day, month, hour, minute, _ = parsed
        scheduled = resolve_datetime(day, month, hour, minute, now)
        # La key de una historia incluye el nombre completo para no colisionar
        # si hay varias en el mismo minuto.
        key = p.stem
        posts.append(Post(key, "story", scheduled, [MediaItem(p, None)], "", "stories"))
    return posts


# --------------------------------------------------------------------------- #
# URLs públicas (hosting vía raw.githubusercontent.com)
# --------------------------------------------------------------------------- #

def public_url(path: Path) -> str:
    base = os.environ.get("RAW_BASE_URL")
    if base:
        rel = path.relative_to(ROOT).as_posix()
        return f"{base.rstrip('/')}/{urllib.parse.quote(rel)}"
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo:
        raise RuntimeError(
            "No puedo armar la URL pública: falta GITHUB_REPOSITORY o RAW_BASE_URL. "
            "En GitHub Actions esto se setea solo."
        )
    rel = path.relative_to(ROOT).as_posix()
    return (f"https://raw.githubusercontent.com/{repo}/{ref}/"
            f"{urllib.parse.quote(rel)}")


# --------------------------------------------------------------------------- #
# Cliente Graph API (helpers)
# --------------------------------------------------------------------------- #

class GraphError(RuntimeError):
    pass


def _check(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        return {}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise GraphError(
            f"{err.get('type', 'Error')}: {err.get('message', resp.text)} "
            f"(code {err.get('code')}, subcode {err.get('error_subcode')})"
        )
    return data


def graph_post(path: str, token: str, **params) -> dict:
    params["access_token"] = token
    return _check(requests.post(f"{GRAPH}/{path}", data=params, timeout=120))


def graph_get(path: str, token: str, **params) -> dict:
    params["access_token"] = token
    return _check(requests.get(f"{GRAPH}/{path}", params=params, timeout=60))


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #

def ig_wait_container(container_id: str, token: str) -> None:
    """Espera a que un contenedor (sobre todo de video) quede FINISHED."""
    waited = 0
    while waited < VIDEO_POLL_TIMEOUT:
        data = graph_get(container_id, token, fields="status_code,status")
        code = data.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise GraphError(f"IG: el contenedor {container_id} falló: {data.get('status')}")
        time.sleep(VIDEO_POLL_INTERVAL)
        waited += VIDEO_POLL_INTERVAL
    raise GraphError(f"IG: timeout esperando el procesamiento del contenedor {container_id}")


def ig_publish_container(ig_user_id: str, container_id: str, token: str) -> str:
    data = graph_post(f"{ig_user_id}/media_publish", token, creation_id=container_id)
    return data.get("id", "")


def ig_photo(ig_user_id: str, token: str, image_url: str, caption: str) -> str:
    c = graph_post(f"{ig_user_id}/media", token, image_url=image_url, caption=caption)
    return ig_publish_container(ig_user_id, c["id"], token)


def ig_reel(ig_user_id: str, token: str, video_url: str, caption: str) -> str:
    c = graph_post(f"{ig_user_id}/media", token,
                   media_type="REELS", video_url=video_url, caption=caption,
                   share_to_feed="true")
    ig_wait_container(c["id"], token)
    return ig_publish_container(ig_user_id, c["id"], token)


def ig_carousel(ig_user_id: str, token: str, items: list[MediaItem], caption: str) -> str:
    children = []
    for it in items:
        if it.is_video:
            child = graph_post(f"{ig_user_id}/media", token,
                               media_type="VIDEO", video_url=public_url(it.path),
                               is_carousel_item="true")
            ig_wait_container(child["id"], token)
        else:
            child = graph_post(f"{ig_user_id}/media", token,
                               image_url=public_url(it.path),
                               is_carousel_item="true")
        children.append(child["id"])
    parent = graph_post(f"{ig_user_id}/media", token,
                        media_type="CAROUSEL",
                        children=",".join(children), caption=caption)
    ig_wait_container(parent["id"], token)
    return ig_publish_container(ig_user_id, parent["id"], token)


def ig_story(ig_user_id: str, token: str, item: MediaItem) -> str:
    url = public_url(item.path)
    if item.is_video:
        c = graph_post(f"{ig_user_id}/media", token,
                       media_type="STORIES", video_url=url)
        ig_wait_container(c["id"], token)
    else:
        c = graph_post(f"{ig_user_id}/media", token,
                       media_type="STORIES", image_url=url)
    return ig_publish_container(ig_user_id, c["id"], token)


def publish_instagram(post: Post, ig_user_id: str, token: str) -> str:
    if post.kind == "photo":
        return ig_photo(ig_user_id, token, public_url(post.items[0].path), post.caption)
    if post.kind == "reel":
        return ig_reel(ig_user_id, token, public_url(post.items[0].path), post.caption)
    if post.kind == "carousel":
        return ig_carousel(ig_user_id, token, post.items, post.caption)
    if post.kind == "story":
        return ig_story(ig_user_id, token, post.items[0])
    raise GraphError(f"Tipo de post desconocido para IG: {post.kind}")


# --------------------------------------------------------------------------- #
# Facebook (página)
# --------------------------------------------------------------------------- #

def fb_resumable_upload(upload_url: str, file_url: str, token: str) -> None:
    """Sube un video a rupload.facebook.com usando la URL pública del archivo."""
    headers = {"Authorization": f"OAuth {token}", "file_url": file_url}
    r = requests.post(upload_url, headers=headers, timeout=300)
    _check(r)


def fb_wait_video(video_id: str, token: str) -> None:
    waited = 0
    while waited < VIDEO_POLL_TIMEOUT:
        data = graph_get(video_id, token, fields="status")
        status = (data.get("status") or {})
        phase = status.get("video_status") or status.get("processing_phase", {}).get("status")
        if phase in ("ready", "complete", "FINISHED", "published"):
            return
        if phase in ("error", "ERROR"):
            raise GraphError(f"FB: el video {video_id} falló al procesar: {status}")
        time.sleep(VIDEO_POLL_INTERVAL)
        waited += VIDEO_POLL_INTERVAL
    # No abortamos duro: algunos videos quedan "in_progress" pero igual se publican.
    log(f"  ⚠ FB: no confirmé el fin de procesado del video {video_id} (sigo igual).")


def fb_photo(page_id: str, token: str, image_url: str, caption: str,
             published: bool = True) -> str:
    data = graph_post(f"{page_id}/photos", token, url=image_url,
                      message=caption, published="true" if published else "false")
    return data.get("post_id") or data.get("id", "")


def fb_multiphoto(page_id: str, token: str, image_urls: list[str], caption: str) -> str:
    media_fbids = []
    for url in image_urls:
        data = graph_post(f"{page_id}/photos", token, url=url, published="false")
        media_fbids.append(data["id"])
    attached = [{"media_fbid": mid} for mid in media_fbids]
    post = graph_post(f"{page_id}/feed", token,
                      message=caption, attached_media=json.dumps(attached))
    return post.get("id", "")


def fb_reel(page_id: str, token: str, video_url: str, caption: str) -> str:
    start = graph_post(f"{page_id}/video_reels", token, upload_phase="start")
    video_id = start["video_id"]
    upload_url = start["upload_url"]
    fb_resumable_upload(upload_url, video_url, token)
    graph_post(f"{page_id}/video_reels", token,
               upload_phase="finish", video_id=video_id,
               video_state="PUBLISHED", description=caption)
    fb_wait_video(video_id, token)
    return video_id


def fb_photo_story(page_id: str, token: str, image_url: str) -> str:
    photo = graph_post(f"{page_id}/photos", token, url=image_url, published="false")
    data = graph_post(f"{page_id}/photo_stories", token, photo_id=photo["id"])
    return data.get("post_id") or data.get("id", "")


def fb_video_story(page_id: str, token: str, video_url: str) -> str:
    start = graph_post(f"{page_id}/video_stories", token, upload_phase="start")
    video_id = start["video_id"]
    upload_url = start["upload_url"]
    fb_resumable_upload(upload_url, video_url, token)
    data = graph_post(f"{page_id}/video_stories", token,
                      upload_phase="finish", video_id=video_id)
    return data.get("post_id") or video_id


def publish_facebook(post: Post, page_id: str, token: str) -> str:
    if post.kind == "photo":
        return fb_photo(page_id, token, public_url(post.items[0].path), post.caption)
    if post.kind == "reel":
        return fb_reel(page_id, token, public_url(post.items[0].path), post.caption)
    if post.kind == "carousel":
        images = [it for it in post.items if it.is_image]
        videos = [it for it in post.items if it.is_video]
        if videos:
            log(f"  ⚠ FB: el carrusel {post.key} tiene {len(videos)} video(s); "
                f"Facebook no admite carrusel mixto, publico solo las imágenes en FB.")
        if not images:
            raise GraphError("FB: el carrusel no tiene imágenes publicables en Facebook.")
        if len(images) == 1:
            return fb_photo(page_id, token, public_url(images[0].path), post.caption)
        return fb_multiphoto(page_id, token, [public_url(it.path) for it in images], post.caption)
    if post.kind == "story":
        it = post.items[0]
        if it.is_video:
            return fb_video_story(page_id, token, public_url(it.path))
        return fb_photo_story(page_id, token, public_url(it.path))
    raise GraphError(f"Tipo de post desconocido para FB: {post.kind}")


# --------------------------------------------------------------------------- #
# Estado (idempotencia) y movimiento de archivos
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def state_key(post: Post) -> str:
    return f"{post.source}/{post.key}"


def move_to_subidas(post: Post) -> list[Path]:
    """Mueve media + caption .txt a la subcarpeta Subidas/. Devuelve destinos."""
    src_dir = FEED_DIR if post.source == "feed" else STORIES_DIR
    dest_dir = src_dir / "Subidas"
    dest_dir.mkdir(parents=True, exist_ok=True)

    to_move = list(post.files)
    caption_txt = src_dir / f"{post.key}.txt"
    if caption_txt.exists():
        to_move.append(caption_txt)

    moved = []
    for f in to_move:
        dest = dest_dir / f.name
        _git_or_os_move(f, dest)
        moved.append(dest)
    return moved


def _git_or_os_move(src: Path, dest: Path) -> None:
    if os.environ.get("GIT_COMMIT", "1") == "1":
        try:
            subprocess.run(["git", "mv", "-f", str(src), str(dest)],
                           cwd=ROOT, check=True, capture_output=True)
            return
        except subprocess.CalledProcessError:
            pass  # cae a mover normal (archivo aún no trackeado, etc.)
    os.replace(src, dest)


def git_commit(message: str) -> None:
    if os.environ.get("GIT_COMMIT", "1") != "1":
        return
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "motos-punta-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "bot@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", "motos-punta-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "bot@users.noreply.github.com")
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if r.returncode == 0:
        return  # nada que commitear
    subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True, env=env)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def process(dry_run: bool, now: dt.datetime) -> None:
    token = os.environ.get("META_ACCESS_TOKEN", "")
    ig_user_id = os.environ.get("IG_USER_ID", "")
    fb_page_id = os.environ.get("FB_PAGE_ID", "")
    do_ig = os.environ.get("PUBLISH_TO_IG", "1") == "1" and bool(ig_user_id)
    do_fb = os.environ.get("PUBLISH_TO_FB", "1") == "1" and bool(fb_page_id)

    if not dry_run and not token:
        log("✖ Falta META_ACCESS_TOKEN. Nada para hacer.")
        sys.exit(1)

    state = load_state()
    posts = discover_feed_posts(now) + discover_story_posts(now)
    posts.sort(key=lambda p: p.scheduled)

    due = [p for p in posts if p.scheduled <= now]
    pending = [p for p in posts if p.scheduled > now]

    log(f"Ahora: {now:%d-%m %H:%M} (Montevideo). "
        f"{len(posts)} publicación(es) detectada(s): {len(due)} lista(s), "
        f"{len(pending)} a futuro.")
    for p in pending:
        log(f"  · programada {p.scheduled:%d-%m %H:%M}  [{p.kind}]  {p.key}")

    for post in due:
        sk = state_key(post)
        st = state.get(sk, {})
        targets = []
        if do_ig and not st.get("ig"):
            targets.append("ig")
        if do_fb and not st.get("fb"):
            targets.append("fb")

        label = f"{post.kind} · {post.source} · {post.key}"
        if not targets:
            log(f"  ✓ Ya publicado (o sin destinos): {label}")
            continue

        log(f"▶ Publicando [{label}] en: {', '.join(t.upper() for t in targets)}")
        for f in post.files:
            log(f"    - {f.relative_to(ROOT)}")
        if post.caption:
            log(f"    caption: {post.caption[:60]}{'…' if len(post.caption) > 60 else ''}")

        if dry_run:
            log("    (dry-run: no publico ni muevo)")
            continue

        ok_all = True
        for platform in targets:
            try:
                if platform == "ig":
                    pid = publish_instagram(post, ig_user_id, token)
                else:
                    pid = publish_facebook(post, fb_page_id, token)
                st[platform] = True
                st[f"{platform}_id"] = pid
                st["published_at"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
                log(f"    ✓ {platform.upper()} OK (id {pid})")
            except Exception as e:  # noqa: BLE001
                ok_all = False
                log(f"    ✖ {platform.upper()} falló: {e}")

        state[sk] = st
        save_state(state)

        if ok_all:
            moved = move_to_subidas(post)
            log(f"    → movido a Subidas/ ({len(moved)} archivo(s))")
            git_commit(f"Publicado {label}")
        else:
            log("    ⚠ No muevo a Subidas porque algún destino falló; "
                "se reintentará en la próxima corrida sin duplicar lo ya publicado.")
            git_commit(f"Estado parcial {label}")

    log("Listo.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Publicador a Instagram/Facebook.")
    ap.add_argument("--dry-run", action="store_true",
                    help="No publica ni mueve; solo informa.")
    ap.add_argument("--now", help="Hora simulada 'DD-MM HH-MM' (para pruebas).")
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    if args.now:
        parsed = parse_name(args.now)
        if not parsed:
            log(f"✖ --now inválido: {args.now!r} (esperado 'DD-MM HH-MM').")
            sys.exit(2)
        d, mo, h, mi, _ = parsed
        now = resolve_datetime(d, mo, h, mi, dt.datetime.now(TZ))

    FEED_DIR.mkdir(parents=True, exist_ok=True)
    STORIES_DIR.mkdir(parents=True, exist_ok=True)
    process(args.dry_run, now)


if __name__ == "__main__":
    main()
