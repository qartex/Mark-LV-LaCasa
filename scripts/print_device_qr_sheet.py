"""Render a print-ready sheet of labeled device QR cards for lamination.

Reads the private QR artifacts on the server and produces:
  - one A4 300-dpi PNG sheet (four cards, each with label + device caption)
  - one individual card PNG per label

The URL payload is never printed to stdout, never embedded as text, and the
output directory must be a private real directory (0700); artifacts are new
files (no overwrite), modes 0600.

Usage:
    python scripts/print_device_qr_sheet.py \
        --labels 'LaCasa iPhone 1' 'LaCasa iPhone 2' 'LaCasa iPhone 3' 'LaCasa iPhone 4' \
        --out-dir ~/cannanas-print
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.device_credentials import (  # noqa: E402
    DeviceCredentialConfigError, DeviceCredentialStore, write_device_artifacts,
)


def _private_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o700:
        raise SystemExit('output directory must be mode 0700 (private); refusing to write')
    return path


def _open_private_new_file(path: Path, overwrite_ok: bool = False) -> Path:
    if not overwrite_ok and (path.exists() or path.is_symlink()):
        flag = f'.{datetime.now(timezone.utc).strftime("%H%M%S")}.'
        path = path.with_name(path.stem + flag + path.suffix)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    return path


def build_sheet(store_path: Path, qr_dir: Path, labels, out_dir: Path,
                public_url: str) -> Path:
    from PIL import Image, ImageDraw

    store = DeviceCredentialStore(store_path)
    out_dir = _private_output_dir(out_dir)
    width, height = 2480, 3508              # A4 300 dpi
    sheet = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(sheet)
    try:
        from PIL import ImageFont
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 96)
        small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 56)
    except OSError:                          # fall back wherever fonts differ
        font = small = ImageFont.load_default()

    card_h = height // 4
    for index, label in enumerate(labels):
        matches = [item for item in store.list_devices()
                   if item.label == label and item.revoked_at is None]
        if len(matches) != 1:
            raise SystemExit(f'label {label!r} is not a unique active device; refusing')
        device_id = matches[0].device_id
        url_path = qr_dir / f'{device_id}.url'
        if not url_path.is_file():
            raise SystemExit(f'QR artifact missing for {label}; re-create with device-create')
        import qrcode
        qr = qrcode.QRCode(border=3, box_size=22)
        qr.add_data(url_path.read_text(encoding='utf-8'))
        qr.make(fit=True)
        image = qr.make_image(fill_color='black', back_color='white')
        # qrcode[pil] returns a PIL image; render it RGB for the sheet.
        if not hasattr(image, 'convert'):
            import io as _io
            buffer = _io.BytesIO()
            image.save(buffer, format='PNG')
            import PIL.Image
            image = PIL.Image.open(buffer)
        qr_image = image.convert('RGB')
        size = 1100
        qr_image = qr_image.resize((size, size))
        top = index * card_h
        left = (width - size) // 2
        sheet.paste(qr_image, (left, top + 120))
        text = label
        draw.text(((width - draw.textlength(text, font=font)) // 2, top),
                  text, fill='black', font=font)
        caption = 'Scan in Safari — Zugang entsteht, ohne dass etwas auf dem Papier lesbar ist.'
        draw.text(((width - draw.textlength(caption, font=small)) // 2, top + size + 160),
                  caption, fill='#444444', font=small)
    out_path = _open_private_new_file(
        out_dir / f'device-qrs-{datetime.now(timezone.utc).strftime("%Y%m%d")}.png')
    sheet.save(out_path, format='PNG')
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--labels', nargs='+', required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--store', type=Path,
                        default=None, help='defaults to JARVIS_DEVICE_CREDENTIALS')
    parser.add_argument('--qr-dir', type=Path,
                        default=None, help='defaults to JARVIS_DEVICE_QR_DIR')
    args = parser.parse_args()
    import os
    store_path = Path(args.store or os.environ.get(
        'JARVIS_DEVICE_CREDENTIALS', BASE_DIR / 'config' / 'device_credentials.json'))
    qr_dir = Path(args.qr_dir or os.environ.get(
        'JARVIS_DEVICE_QR_DIR', BASE_DIR / 'config' / 'device_qr'))
    path = build_sheet(store_path, qr_dir, args.labels, args.out_dir, '')
    print(f'print sheet written to {path} (mode 0600, private directory)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())