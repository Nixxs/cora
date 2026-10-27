"""A QR code drawn with half-block characters, two rows of modules per line of text."""

import qrcode
from rich.text import Text

# Dark modules on a light background, as phone scanners expect, whatever the terminal's theme.
STYLE = "#000000 on #ffffff"


def qr_text(data: str) -> Text:
    code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(data)
    code.make(fit=True)
    rows = code.get_matrix()
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    lines = [
        "".join(
            "█" if upper and lower else "▀" if upper else "▄" if lower else " "
            for upper, lower in zip(top, bottom)
        )
        for top, bottom in zip(rows[0::2], rows[1::2])
    ]
    # Spaces are the light border scanners need, so nothing is trimmed.
    return Text("\n".join(lines), style=STYLE)
