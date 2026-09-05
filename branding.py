"""Logo processing for operator-uploaded branding.

Two problems to solve on upload:

1. The logos operators actually have are usually flat PNG or JPEG on a
   solid white background, not transparent cut-outs. Dropped straight
   onto the portal's tinted background they appear as a white rectangle.

2. They are also enormous -- a 2000px, 1 MB export is normal. The portal
   is a CAPTIVE page: customers load it with no internet, over the
   gateway's own WiFi, often on a cheap phone. Every kilobyte is served
   by the Orange Pi to every customer, so shipping a megabyte of logo is
   a real cost.

Both are fixed once, at upload time, so the request path stays a plain
byte-for-byte read of a small PNG.
"""
import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)

# Anything at or above this on every channel is treated as background.
WHITE_CUTOFF = 250
# Below this, the pixel is definitely part of the artwork.
OPAQUE_BELOW = 228

# Rendered sizes. The header band shows the wide logo at roughly 320 CSS
# pixels and the mobile badge at about 40, so these give better than 2x
# headroom for high-density screens while keeping each file to tens of
# kilobytes. Going larger cost 2-4x the bytes for detail nobody can see:
# at 1100px wide the seeded logo was 297 KB, at 640px it is 124 KB.
MAX_WIDTH = {"large": 640, "small": 256}
MAX_HEIGHT = {"large": 240, "small": 256}

MAX_UPLOAD_BYTES = 12 * 1024 * 1024


class LogoError(ValueError):
    """Raised with a message meant to be shown to the operator."""


def _strip_white(image):
    """Makes the white backdrop transparent, with a soft edge.

    Only removes white that is CONNECTED TO THE BORDER, by flooding
    inwards from the edges. Deleting every white pixel instead would
    punch holes through the artwork itself -- white lettering on a
    coloured badge, a white highlight on a bottle, the white ring inside
    a roundel. Those are not background and must survive.

    The alpha ramp matters too: a hard threshold leaves a pale halo,
    because the pixels blending logo into background are near-white but
    not white. Fading them instead is what keeps curved edges smooth
    against a coloured background."""
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    span = WHITE_CUTOFF - OPAQUE_BELOW

    # Precompute lightness once; the flood below reads it repeatedly.
    lightest = [0] * (width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            r, g, b, _ = pixels[x, y]
            lightest[row + x] = r if r < g else g
            if b < lightest[row + x]:
                lightest[row + x] = b

    background = bytearray(width * height)
    stack = []

    def consider(x, y):
        idx = y * width + x
        if not background[idx] and lightest[idx] > OPAQUE_BELOW:
            background[idx] = 1
            stack.append(idx)

    for x in range(width):
        consider(x, 0)
        consider(x, height - 1)
    for y in range(height):
        consider(0, y)
        consider(width - 1, y)

    while stack:
        idx = stack.pop()
        x, y = idx % width, idx // width
        if x > 0:
            consider(x - 1, y)
        if x < width - 1:
            consider(x + 1, y)
        if y > 0:
            consider(x, y - 1)
        if y < height - 1:
            consider(x, y + 1)

    for y in range(height):
        row = y * width
        for x in range(width):
            idx = row + x
            if not background[idx]:
                continue
            r, g, b, a = pixels[x, y]
            light = lightest[idx]
            if light >= WHITE_CUTOFF:
                pixels[x, y] = (r, g, b, 0)
            else:
                ramp = int(255 * (WHITE_CUTOFF - light) / span)
                pixels[x, y] = (r, g, b, min(a, ramp))

    return image


def _trim(image):
    """Crops away fully transparent margins.

    Exports are usually padded with whitespace. Left in, that padding
    becomes invisible but still occupies layout space, so the logo looks
    mysteriously small and off-centre."""
    box = image.getbbox()
    return image.crop(box) if box else image


def process_logo(raw: bytes, variant: str, strip_background: bool = True) -> bytes:
    """Returns optimised PNG bytes ready to serve.

    Raises LogoError with an operator-readable message on bad input."""
    if not raw:
        raise LogoError("No image was uploaded.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise LogoError(
            f"That image is {len(raw) // (1024 * 1024)} MB. Keep it under "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise LogoError("That file is not an image the server can read. Use PNG or JPEG.")

    if image.mode == "P":
        image = image.convert("RGBA")

    had_alpha = image.mode in ("RGBA", "LA")
    image = image.convert("RGBA")

    # Resize first. The flood fill below is pure Python, so running it on
    # a 2000px upload costs seconds of CPU on an SBC to compute detail
    # that is immediately discarded.
    max_w = MAX_WIDTH.get(variant, 640)
    max_h = MAX_HEIGHT.get(variant, 240)
    if image.width > max_w or image.height > max_h:
        image.thumbnail((max_w, max_h), Image.LANCZOS)

    # An image that already has real transparency has been cut out by
    # someone who knew what they wanted -- do not second-guess it.
    if strip_background and not _has_meaningful_alpha(image, had_alpha):
        image = _strip_white(image)

    image = _trim(image)
    if image.width == 0 or image.height == 0:
        raise LogoError("That image came out empty once the background was removed.")

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _has_meaningful_alpha(image, had_alpha):
    """True when the source already carried real transparency."""
    if not had_alpha:
        return False
    alpha = image.getchannel("A")
    return alpha.getextrema()[0] < 250


def describe(raw: bytes):
    """Size and dimensions, for showing the operator what is stored."""
    try:
        image = Image.open(io.BytesIO(raw))
        return {"width": image.width, "height": image.height, "bytes": len(raw)}
    except Exception:
        return {"width": None, "height": None, "bytes": len(raw)}
