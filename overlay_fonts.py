"""Bundled font families shared by the Windows overlay and its previews."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import sys
from tkinter import font
import winreg

from PIL import Image, ImageDraw, ImageFont

from app_paths import resource_dir


FILES = {
    'Gowun Dodum': 'GowunDodum.ttf',
    'NanumMyeongjo': 'NanumMyeongjo.ttf',
    'Noto Sans JP': 'NotoSansJP.ttf',
    'Noto Sans KR': 'NotoSansKR.ttf',
    'Noto Serif JP': 'NotoSerifJP.ttf',
}
LOCALIZED_NAMES = {'나눔명조': 'NanumMyeongjo'}


def font_path(family):
    filename = FILES.get(LOCALIZED_NAMES.get(family,family))
    if filename:
        path = resource_dir()/'resources'/'fonts'/filename
        if path.is_file():
            return path


@lru_cache(maxsize=1)
def register_bundled_fonts():
    if sys.platform != 'win32':
        return ()
    add = ctypes.windll.gdi32.AddFontResourceExW
    add.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p)
    add.restype = ctypes.c_int
    return tuple(family for family in FILES
                 if (path := font_path(family)) and add(str(path),0x10,None))


def font_families(root):
    installed = {LOCALIZED_NAMES.get(family,family) for family in font.families(root) if not family.startswith('@')}
    installed.update(register_bundled_fonts())
    return sorted(installed,key=str.casefold)


def bundled_face(family,size,bold):
    path = font_path(family)
    if not path:
        return None
    face = ImageFont.truetype(path,size)
    if family.startswith('Noto '):
        face.set_variation_by_axes([700 if bold else 400])
    return face


@lru_cache(maxsize=16)
def _credit_font(size,bold,family='Malgun Gothic'):
    if face := bundled_face(family,size,bold):
        return face
    directory=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    if family.casefold()=='yu gothic ui':
        try:return ImageFont.truetype(str(directory/('YuGothB.ttc' if bold else 'YuGothM.ttc')),size,index=1)
        except OSError:pass
    if family.casefold() != 'malgun gothic':
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                index=0
                while True:
                    try:name,filename,_=winreg.EnumValue(key,index)
                    except OSError:break
                    index+=1
                    if name.casefold().startswith(family.casefold()) and (('bold' in name.casefold())==bold):
                        return ImageFont.truetype(str(directory/filename),size)
        except OSError:pass
    path=directory/('malgunbd.ttf' if bold else 'malgun.ttf')
    return ImageFont.truetype(str(path),size)


@lru_cache(maxsize=16)
def _credit_fonts(size,bold,family='Malgun Gothic'):
    """Pillow does not apply Windows' font linking for missing glyphs."""
    fonts=[_credit_font(size,bold,family)]
    directory=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for filename in (('meiryob.ttc' if bold else 'meiryo.ttc'),
                     ('YuGothB.ttc' if bold else 'YuGothM.ttc'),
                     'msgothic.ttc','seguisym.ttf','seguiemj.ttf',
                     ('malgunbd.ttf' if bold else 'malgun.ttf')):
        try:fonts.append(ImageFont.truetype(str(directory/filename),size))
        except OSError:continue
    return tuple(fonts)


@lru_cache(maxsize=128)
def _missing_glyph(font):
    mask=font.getmask(chr(0x10ffff))
    return mask.size,bytes(mask)


@lru_cache(maxsize=8192)
def _has_glyph(font,char):
    mask=font.getmask(char)
    return (mask.size,bytes(mask))!=_missing_glyph(font)


def _credit_mask(value,size,bold,ascent,line_height,family='Malgun Gothic'):
    """Render one measured text run, substituting fonts only for absent glyphs."""
    primary=_credit_font(size,bold,family)
    fonts=None
    runs=[]
    for char in value:
        face=primary
        if not _has_glyph(primary,char):
            if fonts is None:fonts=_credit_fonts(size,bold,family)
            face=next((font for font in fonts if _has_glyph(font,char)),primary)
        if runs and runs[-1][1] is face:runs[-1]=(runs[-1][0]+char,face)
        else:runs.append((char,face))
    lengths=[face.getlength(text) for text,face in runs]
    mask=Image.new('L',(max(1,round(sum(lengths)))+8,line_height))
    draw=ImageDraw.Draw(mask);x=4
    for (text,face),width in zip(runs,lengths):
        draw.text((x,ascent),text,font=face,fill=255,anchor='ls')
        x+=width
    return mask


