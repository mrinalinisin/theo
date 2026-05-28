"""Centralised image-to-disk helpers used by routes and migration."""

import base64
import glob
import logging
import os
import re
import time

import numpy as np
import requests
from PIL import Image

log = logging.getLogger(__name__)

# Maximum Hamming distance (out of 64 bits) to consider two images as duplicates.
HASH_DISTANCE_THRESHOLD = 10

_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}

_URL_EXT_MAP = {
    ".jpeg": "jpg",
    ".jpg": "jpg",
    ".png": "png",
    ".webp": "webp",
    ".gif": "gif",
    ".svg": "svg",
    ".avif": "avif",
}


def _images_dir(app):
    return os.path.join(app.instance_path, "images")


def ensure_image_dir(app):
    os.makedirs(_images_dir(app), exist_ok=True)


def _is_existing_filename(value, app):
    """Return True if *value* is a bare filename already on disk."""
    if not value or "/" in value or ":" in value:
        return False
    return os.path.isfile(os.path.join(_images_dir(app), value))


def _ext_from_content_type(ct):
    if not ct:
        return "jpg"
    mime = ct.split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(mime, "jpg")


def _ext_from_url(url):
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    for suffix, ext in _URL_EXT_MAP.items():
        if path.endswith(suffix):
            return ext
    return None


def compute_image_hash(filepath):
    """Compute an average-hash (aHash) for the image at *filepath*.

    Returns a 16-character hex string (64-bit fingerprint), or None if the
    image cannot be processed (SVG, corrupt, etc.).
    """
    if filepath.lower().endswith(".svg"):
        return None
    try:
        with Image.open(filepath) as img:
            small = img.convert("L").resize((8, 8), Image.LANCZOS)
            pixels = list(small.getdata())
            avg = sum(pixels) / len(pixels)
            bits = 0
            for px in pixels:
                bits = (bits << 1) | (1 if px >= avg else 0)
            return f"{bits:016x}"
    except Exception as exc:
        log.warning("Could not hash %s: %s", filepath, exc)
        return None


def _hamming_distance(h1, h2):
    """Number of differing bits between two 64-bit hex-string hashes."""
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def find_duplicate_by_image(phash, exclude_product_id=None):
    """Return the first Product whose images perceptually match *phash*, or None."""
    from models import db, ImageHash

    query = db.session.query(ImageHash)
    if exclude_product_id is not None:
        query = query.filter(ImageHash.product_id != exclude_product_id)

    for row in query.all():
        if _hamming_distance(phash, row.phash) <= HASH_DISTANCE_THRESHOLD:
            return row.product
    return None


def strip_solid_background(filepath, color_tolerance=35, uniformity_threshold=40):
    """If the image has a plain solid-colour background, make it transparent.

    Detection: samples the four corners of the image. If every corner
    is within *uniformity_threshold* Euclidean distance of the others,
    the background is considered solid and removal proceeds.

    Removal: every pixel whose colour distance from the background
    colour is ≤ *color_tolerance* has its alpha set to 0.

    The result is always saved as PNG (the only common raster format
    that supports an alpha channel). The original file is deleted when
    the extension changes. Returns the (possibly new) basename.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".svg", ".gif", ".avif"):
        return os.path.basename(filepath)

    try:
        with Image.open(filepath) as img:
            img = img.convert("RGBA")
            arr = np.array(img, dtype=np.float32)   # (H, W, 4)
            h, w = arr.shape[:2]

            inset = max(1, min(4, w // 10, h // 10))
            corners = np.array([
                arr[inset,         inset,         :3],
                arr[inset,         w - 1 - inset, :3],
                arr[h - 1 - inset, inset,         :3],
                arr[h - 1 - inset, w - 1 - inset, :3],
            ])  # (4, 3)

            # Reject images whose corners are not all similar
            for i in range(4):
                for j in range(i + 1, 4):
                    if np.linalg.norm(corners[i] - corners[j]) > uniformity_threshold:
                        return os.path.basename(filepath)

            bg = corners.mean(axis=0)               # (3,) mean background colour

            # Euclidean distance from bg for every pixel
            dist = np.sqrt(np.sum((arr[:, :, :3] - bg) ** 2, axis=2))  # (H, W)

            # Zero out alpha where pixel is within tolerance
            arr[:, :, 3] = np.where(dist <= color_tolerance, 0, arr[:, :, 3])

            result = Image.fromarray(arr.astype(np.uint8), "RGBA")
            png_path = os.path.splitext(filepath)[0] + ".png"
            result.save(png_path, "PNG")

            if png_path != filepath:
                try:
                    os.remove(filepath)
                except OSError:
                    pass

            return os.path.basename(png_path)

    except Exception as exc:
        log.warning("Background strip failed for %s: %s", filepath, exc)
        return os.path.basename(filepath)


def save_image(image_value, product_id, index, app):
    """Save a single image (URL, base64, or filename) to disk.

    Returns the filename on success, or None on failure.
    """
    if not image_value or not isinstance(image_value, str):
        return None

    value = image_value.strip()
    if not value:
        return None

    # Already a saved filename
    if _is_existing_filename(value, app):
        return value

    dest_dir = _images_dir(app)

    # --- base64 data URL ---
    if value.startswith("data:image/"):
        m = re.match(r"data:image/([^;]+);base64,", value)
        if not m:
            log.warning("Product %s image %s: unparseable data URL", product_id, index)
            return None
        mime_sub = m.group(1).lower()
        ext = _MIME_TO_EXT.get(f"image/{mime_sub}", "jpg")
        payload = value[m.end():]
        try:
            data = base64.b64decode(payload)
        except Exception:
            log.warning("Product %s image %s: base64 decode failed", product_id, index)
            return None
        filename = f"product_{product_id}_{index}.{ext}"
        path = os.path.join(dest_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        filename = strip_solid_background(path)
        return filename

    # --- remote URL ---
    if value.startswith(("http://", "https://")):
        try:
            resp = requests.get(value, timeout=15, stream=True, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36",
            })
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Product %s image %s: download failed (%s): %s",
                        product_id, index, value[:80], exc)
            return None
        ext = _ext_from_content_type(resp.headers.get("Content-Type")) or "jpg"
        url_ext = _ext_from_url(value)
        if url_ext:
            ext = url_ext
        filename = f"product_{product_id}_{index}.{ext}"
        path = os.path.join(dest_dir, filename)
        with open(path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        filename = strip_solid_background(path)
        return filename

    # Unknown format — skip
    log.warning("Product %s image %s: unrecognised image value", product_id, index)
    return None


def _store_hash(filename, product_id, app):
    """Compute and persist the perceptual hash for a saved image file."""
    from models import db, ImageHash

    filepath = os.path.join(_images_dir(app), filename)
    phash = compute_image_hash(filepath)
    if phash:
        db.session.add(ImageHash(product_id=product_id, filename=filename, phash=phash))
    return phash


def save_images_for_product(image_list, product_id, app, start_index=0):
    """Save every image in *image_list* to disk and store perceptual hashes.

    Returns a list of filenames (None entries filtered out).
    """
    if not image_list:
        return []
    filenames = []
    for i, img in enumerate(image_list):
        fname = save_image(img, product_id, start_index + i, app)
        if fname:
            filenames.append(fname)
            _store_hash(fname, product_id, app)
    return filenames


def save_new_images_for_product(image_list, product_id, app):
    """Save images using timestamp-based filenames to avoid collisions with
    existing files (used by edit routes adding new images).

    Also stores perceptual hashes for newly saved images.
    """
    if not image_list:
        return []
    ts = int(time.time() * 1000)
    filenames = []
    for i, img in enumerate(image_list):
        if not img or not isinstance(img, str):
            continue
        v = img.strip()
        if _is_existing_filename(v, app):
            filenames.append(v)
            continue
        # Need to save — use timestamped filename
        idx = f"{ts}_{i}"
        fname = save_image(v, product_id, idx, app)
        if fname:
            filenames.append(fname)
            _store_hash(fname, product_id, app)
    return filenames


def delete_product_images(product_id, app):
    """Remove all image files for a given product from disk."""
    pattern = os.path.join(_images_dir(app), f"product_{product_id}_*")
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError:
            pass
