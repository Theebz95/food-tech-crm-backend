"""
Server-side QR code generation — replaces the old dependency on an
external service (QRServer.com) for gift card QR codes. Generated on
demand (GET .../gift-cards/<id>/qr-code/ in views.py), not stored as a
Document: a QR code is a deterministic encoding of data already on the
GiftCard row (its `code`), so storing a rendered copy would just be
cache-invalidation risk (what if it gets out of sync?) for an asset with
no independent value of its own — regenerating it is cheap and always
correct, unlike a business logo (Settings domain), which *is* the
canonical asset.
"""

import io

import qrcode


def generate_qr_png(data: str) -> bytes:
    image = qrcode.make(data)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
