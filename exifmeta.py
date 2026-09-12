# -*- coding: utf-8 -*-
"""Minimal EXIF reader that works on a JPEG head buffer (no PIL needed).

Returns the fields this project cares about:
    dt      DateTimeOriginal (else DateTime) as 'YYYYMMDDHHMMSS'
    make    camera maker
    model   camera model
    width   / height  (EXIF PixelXDimension / PixelYDimension)
    gps     True when a GPS IFD is present
"""

import re
import struct

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENT = 0x0112
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DTORIG = 0x9003
TAG_PIXX = 0xA002
TAG_PIXY = 0xA003


def _find_app1(head):
    if not head.startswith(b'\xff\xd8'):
        return None
    i, n = 2, len(head)
    while i + 4 <= n:
        if head[i] != 0xFF:
            return None
        marker = head[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:
            return None
        seglen = struct.unpack_from('>H', head, i + 2)[0]
        if marker == 0xE1 and head[i + 4:i + 10] == b'Exif\x00\x00':
            return head[i + 10:i + 2 + seglen]
        i += 2 + seglen
    return None


def read(head):
    out = {'dt': None, 'make': None, 'model': None,
           'width': None, 'height': None, 'gps': False, 'orient': None}
    try:
        app1 = _find_app1(head)
        if not app1 or len(app1) < 8:
            return out
        bo = '<' if app1[:2] == b'II' else ('>' if app1[:2] == b'MM' else None)
        if bo is None:
            return out
        ifd0 = struct.unpack_from(bo + 'I', app1, 4)[0]

        def read_ifd(off, want):
            found = {}
            if off <= 0 or off + 2 > len(app1):
                return found
            cnt = struct.unpack_from(bo + 'H', app1, off)[0]
            if cnt > 512:
                return found
            for k in range(cnt):
                e = off + 2 + k * 12
                if e + 12 > len(app1):
                    break
                tag, typ, num = struct.unpack_from(bo + 'HHI', app1, e)
                if tag not in want:
                    continue
                try:
                    if typ in (3, 4) and num == 1:
                        if typ == 3:
                            found[tag] = struct.unpack_from(bo + 'H', app1, e + 8)[0]
                        else:
                            found[tag] = struct.unpack_from(bo + 'I', app1, e + 8)[0]
                    elif typ == 2:
                        if num <= 4:
                            raw = app1[e + 8:e + 8 + num]
                        else:
                            p = struct.unpack_from(bo + 'I', app1, e + 8)[0]
                            raw = app1[p:p + num]
                        found[tag] = raw.split(b'\x00')[0].decode('ascii', 'ignore').strip()
                except Exception:
                    continue
            return found

        top = read_ifd(ifd0, {TAG_MAKE, TAG_MODEL, TAG_DATETIME, TAG_EXIF_IFD,
                              TAG_GPS_IFD, TAG_ORIENT})
        out['make'] = top.get(TAG_MAKE) or None
        out['model'] = top.get(TAG_MODEL) or None
        out['gps'] = TAG_GPS_IFD in top
        o = top.get(TAG_ORIENT)
        if isinstance(o, int) and 1 <= o <= 8:
            out['orient'] = o

        dt = None
        if TAG_EXIF_IFD in top:
            sub = read_ifd(top[TAG_EXIF_IFD], {TAG_DTORIG, TAG_PIXX, TAG_PIXY})
            dt = sub.get(TAG_DTORIG)
            if isinstance(sub.get(TAG_PIXX), int):
                out['width'] = sub[TAG_PIXX]
            if isinstance(sub.get(TAG_PIXY), int):
                out['height'] = sub[TAG_PIXY]
        if not dt:
            dt = top.get(TAG_DATETIME)
        if dt:
            s = re.sub(r'\D', '', str(dt))
            if len(s) >= 14:
                out['dt'] = s[:14]
    except Exception:
        pass
    return out
