from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image, ExifTags, ImageOps
except ImportError:
    print("Missing dependency: Pillow. Install it with: python -m pip install Pillow", file=sys.stderr)
    raise SystemExit(2)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
APP_VERSION = "1.0.0"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
LORA_TAG_RE = re.compile(r"<lora:([^:>]+)(?::([-+]?\d*\.?\d+))?(?::([-+]?\d*\.?\d+))?>", re.I)
LORA_FILE_RE = re.compile(r"([^\s,;<>\"']+\.(?:safetensors|ckpt|pt|pth|bin))", re.I)


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _serializable(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        return "<max depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        return {str(k): _serializable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v, depth + 1) for v in value]
    return str(value)


def _display_lora_name(raw: str) -> str:
    raw = raw.replace("\\", "/").strip()
    base = raw.rsplit("/", 1)[-1]
    for ext in (".safetensors", ".ckpt", ".pth", ".pt", ".bin"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base or raw


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _looks_like_lora_filename(value: str) -> bool:
    low = value.lower().strip()
    return any(low.endswith(ext) for ext in (".safetensors", ".ckpt", ".pt", ".pth", ".bin"))


def _lora_key(key: str) -> bool:
    k = key.lower().replace(" ", "_")
    return "lora" in k or "lycoris" in k or "locon" in k


def _explicitly_disabled(mapping: dict[str, Any]) -> bool:
    """Return True when a custom-node entry explicitly marks itself disabled.

    Several ComfyUI LoRA stack/loaders keep configured LoRAs in metadata even
    when they are switched off, commonly as {"on": false, "lora": ...}. Those
    entries should not count as LoRAs that were actually used for the image.
    """
    for key in ("on", "enabled", "active"):
        if key not in mapping:
            continue
        value = mapping[key]
        if value is False or value == 0:
            return True
        if isinstance(value, str) and value.strip().lower() in {"false", "off", "no", "0", "disabled"}:
            return True
    return False


def _strengths_from_mapping(mapping: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    model = clip = weight = None
    for key, value in mapping.items():
        k = str(key).lower().replace(" ", "_")
        num = _float_or_none(value)
        if num is None:
            continue
        if "strength_model" in k or ("model" in k and "strength" in k):
            model = num
        elif "strength_clip" in k or ("clip" in k and "strength" in k):
            clip = num
        elif k in {"strength", "weight", "lora_strength", "lora_weight"} or ("strength" in k and "lora" in k):
            weight = num
    return model, clip, weight


def extract_loras(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def add(raw_name: str, source: str, model=None, clip=None, weight=None):
        if not raw_name or not isinstance(raw_name, str):
            return
        raw_name = raw_name.strip()
        if raw_name.lower() in {"none", "null", "undefined", "bypass", "off"}:
            return
        found.append({
            "name": _display_lora_name(raw_name),
            "file": raw_name,
            "strength_model": _float_or_none(model),
            "strength_clip": _float_or_none(clip),
            "weight": _float_or_none(weight),
            "source": source,
        })

    def inspect_mapping(mapping: dict[str, Any], source: str, node_is_lora: bool = False):
        if _explicitly_disabled(mapping):
            return
        model_s, clip_s, weight_s = _strengths_from_mapping(mapping)
        for key, value in mapping.items():
            key_s = str(key)
            if isinstance(value, str):
                if _lora_key(key_s) and (value.strip() and not value.strip().startswith("[")):
                    # Standard lora_name, dynamic lora_1, etc.
                    add(value, source, model_s, clip_s, weight_s)
                elif node_is_lora and _looks_like_lora_filename(value):
                    add(value, source, model_s, clip_s, weight_s)
                for match in LORA_TAG_RE.finditer(value):
                    add(match.group(1), source + ":tag", weight=match.group(2))
            elif isinstance(value, dict):
                if _explicitly_disabled(value):
                    continue
                if _lora_key(key_s) or node_is_lora:
                    inner_model, inner_clip, inner_weight = _strengths_from_mapping(value)
                    for ik, iv in value.items():
                        if isinstance(iv, str) and (_lora_key(str(ik)) or _looks_like_lora_filename(iv)):
                            add(iv, source, inner_model or model_s, inner_clip or clip_s, inner_weight or weight_s)
                inspect_mapping(value, source, node_is_lora or _lora_key(key_s))
            elif isinstance(value, list):
                inspect_any(value, source, node_is_lora or _lora_key(key_s))

    def inspect_workflow_node(node: dict[str, Any], source: str):
        if _explicitly_disabled(node):
            return
        node_type = str(node.get("type") or node.get("class_type") or node.get("title") or "")
        node_is_lora = any(token in node_type.lower() for token in ("lora", "lycoris", "locon"))
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            inspect_mapping(inputs, source, node_is_lora)
        if node_is_lora:
            widgets = node.get("widgets_values")
            if isinstance(widgets, list):
                string_candidates = [v for v in widgets if isinstance(v, str) and _looks_like_lora_filename(v)]
                numeric_candidates = [float(v) for v in widgets if isinstance(v, (int, float)) and not isinstance(v, bool)]
                for candidate in string_candidates:
                    model = numeric_candidates[0] if numeric_candidates else None
                    clip = numeric_candidates[1] if len(numeric_candidates) > 1 else None
                    add(candidate, source + ":workflow", model=model, clip=clip)
            elif isinstance(widgets, dict):
                inspect_mapping(widgets, source + ":workflow", True)
        # Some custom nodes keep LoRA lists under properties/values.
        for extra_key in ("properties", "values", "widgets"):
            extra = node.get(extra_key)
            if isinstance(extra, (dict, list)):
                inspect_any(extra, source + f":{extra_key}", node_is_lora)

    def inspect_any(value: Any, source: str, node_is_lora: bool = False):
        value = _jsonish(value)
        if isinstance(value, dict):
            if _explicitly_disabled(value):
                return
            # API prompt form: node id -> {class_type, inputs}
            if "class_type" in value or "type" in value:
                inspect_workflow_node(value, source)
            # Workflow form often has a nodes array.
            if isinstance(value.get("nodes"), list):
                for idx, node in enumerate(value["nodes"]):
                    if isinstance(node, dict):
                        inspect_workflow_node(node, f"{source}.nodes[{idx}]")
            for key, child in value.items():
                if key == "nodes" and isinstance(child, list):
                    continue
                if isinstance(child, dict) and ("class_type" in child or "type" in child):
                    inspect_workflow_node(child, f"{source}.{key}")
                elif isinstance(child, (dict, list)):
                    inspect_any(child, f"{source}.{key}", node_is_lora or _lora_key(str(key)))
                elif isinstance(child, str):
                    if (_lora_key(str(key)) or node_is_lora) and (_looks_like_lora_filename(child) or _lora_key(str(key))):
                        model_s, clip_s, weight_s = _strengths_from_mapping(value)
                        add(child, f"{source}.{key}", model_s, clip_s, weight_s)
                    for match in LORA_TAG_RE.finditer(child):
                        add(match.group(1), f"{source}.{key}:tag", weight=match.group(2))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                if isinstance(child, dict):
                    if "class_type" in child or "type" in child:
                        inspect_workflow_node(child, f"{source}[{idx}]")
                    inspect_any(child, f"{source}[{idx}]", node_is_lora)
                elif isinstance(child, list):
                    inspect_any(child, f"{source}[{idx}]", node_is_lora)
                elif node_is_lora and isinstance(child, str) and _looks_like_lora_filename(child):
                    add(child, f"{source}[{idx}]")
        elif isinstance(value, str):
            for match in LORA_TAG_RE.finditer(value):
                add(match.group(1), source + ":tag", weight=match.group(2))

    for key, value in metadata.items():
        inspect_any(value, str(key))
        if isinstance(value, str):
            for match in LORA_TAG_RE.finditer(value):
                add(match.group(1), f"{key}:tag", weight=match.group(2))

    # De-duplicate by file/name + strengths. Prefer records with more strength information.
    dedup: dict[str, dict[str, Any]] = {}
    for item in found:
        key = item["file"].replace("\\", "/").lower()
        old = dedup.get(key)
        if old is None:
            dedup[key] = item
            continue
        old_score = sum(old.get(k) is not None for k in ("strength_model", "strength_clip", "weight"))
        new_score = sum(item.get(k) is not None for k in ("strength_model", "strength_clip", "weight"))
        if new_score > old_score:
            dedup[key] = item

    return sorted(dedup.values(), key=lambda x: x["name"].lower())


def read_image_metadata(path: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    metadata: dict[str, Any] = {}
    with Image.open(path) as img:
        size = img.size
        for key, value in img.info.items():
            if key.lower() in {"icc_profile", "exif", "xmp"} and isinstance(value, bytes):
                continue
            metadata[str(key)] = _jsonish(_serializable(value))

        # JPEG/WebP metadata often lives in EXIF/UserComment/ImageDescription.
        try:
            exif = img.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if isinstance(value, bytes):
                        # Common EXIF UserComment prefixes.
                        decoded = None
                        for enc, prefix in (("utf-8", b"ASCII\x00\x00\x00"), ("utf-16", b"UNICODE\x00")):
                            try:
                                body = value[len(prefix):] if value.startswith(prefix) else value
                                decoded = body.decode(enc, errors="replace").strip("\x00")
                                if decoded:
                                    break
                            except Exception:
                                pass
                        value = decoded or f"<bytes:{len(value)}>"
                    metadata[f"EXIF:{tag_name}"] = _jsonish(_serializable(value))
        except Exception:
            pass
    return metadata, size


@dataclass
class ImageRecord:
    id: str
    relpath: str
    filename: str
    width: int
    height: int
    bytes: int
    mtime: float
    mtime_ns: int
    loras: list[dict[str, Any]]
    metadata_keys: list[str]


class GalleryIndex:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.records: list[ImageRecord] = []
        self.by_id: dict[str, ImageRecord] = {}
        self.scan_errors: list[dict[str, str]] = []
        self.last_scan = 0.0
        self.lock = threading.RLock()
        self.scan_state: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "processed": 0,
            "total": 0,
            "current": "",
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": None,
        }
        self.scan_thread: threading.Thread | None = None
        cache_key = hashlib.sha1(str(self.root).encode("utf-8", "replace")).hexdigest()[:12]
        self.thumb_dir = Path(tempfile.gettempdir()) / "comfy-lora-gallery" / cache_key
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    def _id_for(self, relpath: str) -> str:
        return hashlib.sha1(relpath.encode("utf-8", "replace")).hexdigest()[:20]

    def scan_status(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.scan_state)

    def start_scan(self) -> bool:
        with self.lock:
            if self.scan_state["running"]:
                return False
            self.scan_state = {
                "running": True,
                "phase": "discovering",
                "processed": 0,
                "total": 0,
                "current": "",
                "started_at": time.time(),
                "finished_at": 0.0,
                "error": None,
            }
            self.scan_thread = threading.Thread(target=self._scan_worker, name="gallery-scan", daemon=True)
            self.scan_thread.start()
            return True

    def _scan_worker(self) -> None:
        records: list[ImageRecord] = []
        errors: list[dict[str, str]] = []
        try:
            paths = sorted(
                path for path in self.root.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            )
            with self.lock:
                self.scan_state.update({"phase": "scanning", "total": len(paths), "processed": 0})

            last_terminal_update = 0.0
            for i, path in enumerate(paths, 1):
                try:
                    rel = path.relative_to(self.root).as_posix()
                except Exception:
                    rel = str(path)
                with self.lock:
                    self.scan_state["current"] = rel

                try:
                    stat = path.stat()
                    metadata, (width, height) = read_image_metadata(path)
                    loras = extract_loras(metadata)
                    records.append(ImageRecord(
                        id=self._id_for(rel),
                        relpath=rel,
                        filename=path.name,
                        width=width,
                        height=height,
                        bytes=stat.st_size,
                        mtime=stat.st_mtime,
                        mtime_ns=stat.st_mtime_ns,
                        loras=loras,
                        metadata_keys=sorted(metadata.keys()),
                    ))
                except Exception as exc:
                    errors.append({"path": rel, "error": str(exc)})

                with self.lock:
                    self.scan_state["processed"] = i

                now = time.time()
                if now - last_terminal_update >= 1.0 or i == len(paths):
                    print(f"\rScanning images: {i}/{len(paths)}", end="", flush=True)
                    last_terminal_update = now

            with self.lock:
                self.records = records
                self.by_id = {r.id: r for r in records}
                self.scan_errors = errors
                self.last_scan = time.time()
                self.scan_state.update({
                    "running": False,
                    "phase": "complete",
                    "processed": len(paths),
                    "total": len(paths),
                    "current": "",
                    "finished_at": self.last_scan,
                })
            print(f"\rIndexed {len(records)} images.{' ' * 24}")
        except Exception as exc:
            with self.lock:
                self.scan_state.update({
                    "running": False,
                    "phase": "error",
                    "current": "",
                    "finished_at": time.time(),
                    "error": str(exc),
                })
            print(f"\nScan failed: {exc}", file=sys.stderr)

    def summary(self) -> dict[str, Any]:
        with self.lock:
            counts: dict[str, dict[str, Any]] = {}
            for rec in self.records:
                seen = set()
                for lora in rec.loras:
                    key = lora["file"].replace("\\", "/").lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    if key not in counts:
                        counts[key] = {"key": key, "name": lora["name"], "file": lora["file"], "count": 0}
                    counts[key]["count"] += 1
            loras = sorted(counts.values(), key=lambda x: (-x["count"], x["name"].lower()))
            return {
                "root": str(self.root),
                "image_count": len(self.records),
                "lora_count": len(loras),
                "loras": loras,
                "errors": list(self.scan_errors),
                "last_scan": self.last_scan,
            }

    def list_images(self) -> list[dict[str, Any]]:
        with self.lock:
            return [asdict(r) for r in self.records]

    def get(self, image_id: str) -> ImageRecord | None:
        with self.lock:
            return self.by_id.get(image_id)

    def path_for(self, rec: ImageRecord) -> Path:
        path = (self.root / rec.relpath).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("Invalid image path")
        return path

    def detail(self, image_id: str) -> dict[str, Any] | None:
        rec = self.get(image_id)
        if rec is None:
            return None
        path = self.path_for(rec)
        metadata, _ = read_image_metadata(path)
        result = asdict(rec)
        result["metadata"] = _serializable(metadata)
        return result

    def thumb(self, image_id: str, px: int = 420) -> Path | None:
        rec = self.get(image_id)
        if rec is None:
            return None
        px = max(128, min(int(px), 1024))
        out = self.thumb_dir / f"{rec.id}-{rec.mtime_ns}-{rec.bytes}-{px}.jpg"
        if out.exists():
            return out
        src = self.path_for(rec)
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((px, px), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, "white")
                alpha = img.getchannel("A") if "A" in img.getbands() else None
                bg.paste(img.convert("RGB"), mask=alpha)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            tmp = out.with_suffix(".tmp")
            img.save(tmp, format="JPEG", quality=84, optimize=True)
            tmp.replace(out)
        return out


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = f"ComfyLoraGallery/{APP_VERSION}"

    @property
    def index(self) -> GalleryIndex:
        return self.server.index  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, data: Any, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _file(self, path: Path, content_type: str | None = None, cache=False):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/":
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
            target = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in target.parents:
                return self.send_error(403)
            return self._file(target)
        if path == "/api/summary":
            return self._json(self.index.summary())
        if path == "/api/scan-status":
            return self._json(self.index.scan_status())
        if path == "/api/images":
            return self._json({"images": self.index.list_images()})
        if path == "/api/detail":
            image_id = (qs.get("id") or [""])[0]
            detail = self.index.detail(image_id)
            return self._json(detail if detail is not None else {"error": "Not found"}, 200 if detail else 404)
        if path == "/api/thumb":
            image_id = (qs.get("id") or [""])[0]
            px = int((qs.get("px") or ["420"])[0])
            thumb = self.index.thumb(image_id, px)
            if thumb is None:
                return self.send_error(404)
            return self._file(thumb, "image/jpeg", cache=True)
        if path == "/api/media":
            image_id = (qs.get("id") or [""])[0]
            rec = self.index.get(image_id)
            if rec is None:
                return self.send_error(404)
            file_path = self.index.path_for(rec)
            return self._file(file_path, cache=True)
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/rescan":
            started = self.index.start_scan()
            return self._json({"started": started, "scan": self.index.scan_status()}, 202 if started else 200)
        self.send_error(404)


class GalleryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, index: GalleryIndex):
        super().__init__(addr, handler)
        self.index = index


def parse_args():
    parser = argparse.ArgumentParser(description="Local ComfyUI LoRA image gallery")
    parser.add_argument("directory", nargs="?", default=".", help="Image directory to scan recursively")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Port (default: 8787)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--version", action="version", version=f"ComfyUI LoRA Gallery {APP_VERSION}")
    return parser.parse_args()


def main():
    args = parse_args()
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"Directory not found: {root}", file=sys.stderr)
        raise SystemExit(2)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: binding beyond localhost exposes files in the selected directory to your network.", file=sys.stderr)

    index = GalleryIndex(root)
    server = GalleryServer((args.host, args.port), GalleryHandler, index)
    print(f"Scanning {root} ...")
    index.start_scan()
    url = f"http://{args.host}:{args.port}/"
    print(f"Gallery: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping gallery.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
