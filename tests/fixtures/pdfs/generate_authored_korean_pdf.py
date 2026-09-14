"""Generate the repository-authored Korean text extraction fixture."""

from pathlib import Path

TEXT = "연차 휴가는 사전에 신청합니다."


def _stream(data: bytes) -> bytes:
    header = b"<< /Length " + str(len(data)).encode("ascii") + b" >>\nstream\n"
    return header + data + b"\nendstream"


def build_pdf() -> bytes:
    characters = sorted(set(TEXT))
    mappings = "\n".join(
        f"<{ord(character):04X}> <{ord(character):04X}>" for character in characters
    )
    to_unicode = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n"
        "/CMapName /AuthoredKorean def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<0000> <FFFF>\n"
        "endcodespacerange\n"
        f"{len(characters)} beginbfchar\n"
        f"{mappings}\n"
        "endbfchar\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end"
    ).encode("ascii")
    encoded_text = TEXT.encode("utf-16-be").hex().upper().encode("ascii")
    content = b"BT\n/F1 16 Tf\n72 720 Td\n<" + encoded_text + b"> Tj\nET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 7 0 R >>"
        ),
        (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /HYSMyeongJo-Medium "
            b"/Encoding /Identity-H /DescendantFonts [5 0 R] /ToUnicode 6 0 R >>"
        ),
        (
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYSMyeongJo-Medium "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>"
        ),
        _stream(to_unicode),
        _stream(content),
    ]

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{number} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


if __name__ == "__main__":
    Path(__file__).with_name("authored_korean.pdf").write_bytes(build_pdf())
