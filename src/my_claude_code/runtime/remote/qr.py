"""Pure-Python ISO/IEC 18004 QR Code SVG generator (zero dependencies)."""

from html import escape

# Reed-Solomon GF(256) with primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D).
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        factor = [1, _GF_EXP[i]]
        nxt = [0] * (len(poly) + 1)
        for idx_a, coeff_a in enumerate(poly):
            for idx_b, coeff_b in enumerate(factor):
                nxt[idx_a + idx_b] ^= _gf_mul(coeff_a, coeff_b)
        poly = nxt
    return poly


def _rs_remainder(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator_poly(ec_len)
    buf = [*data, *([0] * ec_len)]
    for i in range(len(data)):
        coef = buf[i]
        if coef != 0:
            for j in range(len(gen)):
                buf[i + j] ^= _gf_mul(gen[j], coef)
    return buf[len(data) :]


# Versions 1..10 Error Correction Level L: (total_codewords, ec_per_block, blocks_g1, data_g1, blocks_g2, data_g2)
_VERSION_L_PARAMS: dict[int, tuple[int, int, int, int, int, int]] = {
    1: (26, 7, 1, 19, 0, 0),
    2: (44, 10, 1, 34, 0, 0),
    3: (70, 15, 1, 55, 0, 0),
    4: (100, 20, 1, 80, 0, 0),
    5: (134, 26, 1, 108, 0, 0),
    6: (172, 18, 2, 68, 0, 0),
    7: (196, 20, 2, 78, 0, 0),
    8: (242, 24, 2, 97, 0, 0),
    9: (292, 30, 2, 116, 0, 0),
    10: (346, 18, 2, 68, 2, 69),
}

_ALIGNMENT_POSITIONS: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}


def _choose_version(payload_len: int) -> int:
    for version in range(1, 11):
        _total, _ec, b1, d1, b2, d2 = _VERSION_L_PARAMS[version]
        total_data = b1 * d1 + b2 * d2
        cc_bits = 8 if version <= 9 else 16
        header_bits = 4 + cc_bits
        capacity_bytes = (total_data * 8 - header_bits) // 8
        if payload_len <= capacity_bytes:
            return version
    raise ValueError("URL is too long for QR Version 1..10 byte encoding")


def _encode_data_codewords(payload: bytes, version: int) -> list[int]:
    _total, _ec, b1, d1, b2, d2 = _VERSION_L_PARAMS[version]
    total_data = b1 * d1 + b2 * d2
    bits: list[int] = []

    def push(val: int, width: int) -> None:
        bits.extend((val >> shift) & 1 for shift in range(width - 1, -1, -1))

    push(0b0100, 4)  # Byte mode
    push(len(payload), 8 if version <= 9 else 16)
    for byte in payload:
        push(byte, 8)

    max_bits = total_data * 8
    terminator = min(4, max_bits - len(bits))
    bits.extend([0] * terminator)
    while len(bits) % 8 != 0:
        bits.append(0)

    codewords = [
        sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8)
    ]
    pad_bytes = (0xEC, 0x11)
    pad_idx = 0
    while len(codewords) < total_data:
        codewords.append(pad_bytes[pad_idx % 2])
        pad_idx += 1
    return codewords


def _interleave_blocks(data_cw: list[int], version: int) -> list[int]:
    _total, ec_len, b1, d1, b2, d2 = _VERSION_L_PARAMS[version]
    blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for count, size in ((b1, d1), (b2, d2)):
        for _ in range(count):
            blk = data_cw[pos : pos + size]
            pos += size
            blocks.append(blk)
            ec_blocks.append(_rs_remainder(blk, ec_len))

    result: list[int] = []
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        result.extend(blk[i] for blk in blocks if i < len(blk))
    for i in range(ec_len):
        result.extend(ec[i] for ec in ec_blocks)
    return result


def _bch_format_bits(mask_id: int) -> int:
    # Level L indicator = 01
    data = (0b01 << 3) | mask_id
    rem = data << 10
    gen = 0b10100110111
    for bit in range(14, 9, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 10)
    return ((data << 10) | rem) ^ 0b101010000010010


def _bch_version_bits(version: int) -> int:
    rem = version << 12
    gen = 0b1111100100101
    for bit in range(17, 11, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 12)
    return (version << 12) | rem


def _build_base_matrix(
    version: int,
) -> tuple[list[list[bool | None]], list[list[bool]]]:
    size = 17 + 4 * version
    grid: list[list[bool | None]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    def set_fn(r: int, c: int, val: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = val
            reserved[r][c] = True

    def place_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    set_fn(r, c, False)
                elif dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4):
                    set_fn(r, c, True)
                else:
                    set_fn(r, c, False)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    centers = _ALIGNMENT_POSITIONS[version]
    for r0 in centers:
        for c0 in centers:
            if reserved[r0][c0]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    set_fn(r0 + dr, c0 + dc, dark)

    for i in range(8, size - 8):
        val = i % 2 == 0
        if not reserved[6][i]:
            set_fn(6, i, val)
        if not reserved[i][6]:
            set_fn(i, 6, val)

    # Dark module
    set_fn(4 * version + 9, 8, True)

    # Reserve format info strips
    for i in range(9):
        if not reserved[8][i]:
            reserved[8][i] = True
            grid[8][i] = False
        if not reserved[i][8]:
            reserved[i][8] = True
            grid[i][8] = False
    for i in range(8):
        reserved[8][size - 1 - i] = True
        grid[8][size - 1 - i] = False
    for i in range(7):
        reserved[size - 1 - i][8] = True
        grid[size - 1 - i][8] = False

    if version >= 7:
        vbits = _bch_version_bits(version)
        for i in range(18):
            bit = bool((vbits >> i) & 1)
            r = i // 3
            c = size - 11 + (i % 3)
            set_fn(r, c, bit)
            set_fn(c, r, bit)

    return grid, reserved


def _place_codewords(
    grid: list[list[bool | None]], reserved: list[list[bool]], codewords: list[int]
) -> None:
    size = len(grid)
    bits: list[bool] = []
    for cw in codewords:
        bits.extend(bool((cw >> shift) & 1) for shift in range(7, -1, -1))

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not reserved[r][c]:
                    grid[r][c] = bits[idx] if idx < len(bits) else False
                    idx += 1
        col -= 2
        upward = not upward


def _mask_condition(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0:
        return (r + c) % 2 == 0
    if mask_id == 1:
        return r % 2 == 0
    if mask_id == 2:
        return c % 3 == 0
    if mask_id == 3:
        return (r + c) % 3 == 0
    if mask_id == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask_id == 5:
        return ((r * c) % 2) + ((r * c) % 3) == 0
    if mask_id == 6:
        return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _apply_format(matrix: list[list[bool]], mask_id: int) -> None:
    size = len(matrix)
    fmt = _bch_format_bits(mask_id)
    for i in range(6):
        bit = bool((fmt >> i) & 1)
        matrix[8][size - 1 - i] = bool((fmt >> i) & 1) if i < 8 else False
        matrix[i][8] = bit
    # Exact ISO placement: bit 0..14
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        # Vertical + corner
        if i < 6:
            matrix[i][8] = bit
        elif i < 8:
            matrix[i + 1][8] = bit
        else:
            matrix[size - 15 + i][8] = bit
        # Horizontal
        if i < 8:
            matrix[8][size - 1 - i] = bit
        elif i < 9:
            matrix[8][7] = bit
        else:
            matrix[8][14 - i] = bit


def _penalty_score(matrix: list[list[bool]]) -> int:
    size = len(matrix)
    score = 0

    # N1: runs of >= 5 same colour
    for line in matrix:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(size):
        run = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # N2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if (
                matrix[r][c + 1] == v
                and matrix[r + 1][c] == v
                and matrix[r + 1][c + 1] == v
            ):
                score += 3

    # N3: finder-like patterns
    p1 = (True, False, True, True, True, False, True, False, False, False, False)
    p2 = (False, False, False, False, True, False, True, True, True, False, True)
    for r in range(size):
        row = matrix[r]
        for c in range(size - 10):
            sl = tuple(row[c : c + 11])
            if sl in (p1, p2):
                score += 40
    for c in range(size):
        for r in range(size - 10):
            sl = tuple(matrix[r + k][c] for k in range(11))
            if sl in (p1, p2):
                score += 40

    # N4: dark ratio
    dark = sum(1 for row in matrix for cell in row if cell)
    pct = (dark * 100) // (size * size)
    prev5 = abs(pct - (pct % 5) - 50) // 5
    next5 = abs(pct + (5 - pct % 5) - 50) // 5
    score += min(prev5, next5) * 10
    return score


def encode_qr_matrix(text: str) -> list[list[bool]]:
    """Encode ``text`` into a square boolean QR module matrix (True = dark)."""

    payload = text.encode("utf-8")
    version = _choose_version(len(payload))
    data_cw = _encode_data_codewords(payload, version)
    stream = _interleave_blocks(data_cw, version)
    base, reserved = _build_base_matrix(version)
    _place_codewords(base, reserved, stream)

    best_score: int | None = None
    best_matrix: list[list[bool]] | None = None
    size = len(base)
    for mask_id in range(8):
        cand = [
            [
                bool(base[r][c])
                ^ (not reserved[r][c] and _mask_condition(mask_id, r, c))
                for c in range(size)
            ]
            for r in range(size)
        ]
        _apply_format(cand, mask_id)
        score = _penalty_score(cand)
        if best_score is None or score < best_score:
            best_score = score
            best_matrix = cand
    assert best_matrix is not None
    return best_matrix


def render_qr_svg(text: str, *, quiet_zone: int = 4, module_px: int = 6) -> str:
    """Render ``text`` as a self-contained SVG QR code string."""

    matrix = encode_qr_matrix(text)
    size = len(matrix)
    total = (size + quiet_zone * 2) * module_px
    paths: list[str] = []
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                x = (c + quiet_zone) * module_px
                y = (r + quiet_zone) * module_px
                paths.append(f"M{x},{y}h{module_px}v{module_px}h-{module_px}z")
    d = "".join(paths)
    label = escape(f"Pairing QR code for {text}", quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}" role="img" aria-label="{label}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>'
        f'<path d="{d}" fill="#0f172a"/>'
        f"</svg>"
    )
