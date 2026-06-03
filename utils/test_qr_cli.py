"""CLI test for QR-decoder.

Generates a test QR, decodes it back, then offers to decode any image
file you point at — handy for testing real screenshots from FunPay.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

import qrcode

from utils.qr_decoder import decode_all_from_bytes, decode_qr_from_file
from utils.logging_config import setup_logging

setup_logging()


def make_test_qr(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    print()
    print("=" * 60)
    print("  QR-decoder тест")
    print("=" * 60)
    print()

    # Round-trip self-test
    sample = "rso:qr:abc123XYZ?login=test"
    print(f"1. Round-trip: generate → decode")
    print(f"   Original:  {sample!r}")
    img_bytes = make_test_qr(sample)
    decoded = decode_all_from_bytes(img_bytes)
    print(f"   Decoded:   {decoded[0] if decoded else '— FAIL —'!r}")
    print(f"   Status:    {'✅ OK' if decoded and decoded[0] == sample else '❌ FAIL'}")

    # Optional: try a user-supplied image
    print()
    print("2. Декодирование реального файла (опционально)")
    print()
    while True:
        path = input("Путь к изображению (Enter — выход): ").strip().strip('"')
        if not path:
            break
        p = Path(path)
        if not p.exists():
            print(f"   [!] Файл не найден: {p}")
            continue
        with p.open("rb") as f:
            data = f.read()
        results = decode_all_from_bytes(data)
        if results:
            print(f"   ✅ Найдено {len(results)} QR-код(ов):")
            for i, r in enumerate(results, 1):
                snippet = r[:200] + ("..." if len(r) > 200 else "")
                print(f"      [{i}] {snippet}")
        else:
            print("   [!] QR не обнаружен (возможно слишком мелкий/размытый)")
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(1)
