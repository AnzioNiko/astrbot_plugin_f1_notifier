"""Pillow-based image renderer for F1 plugin.


Generates broadcast-style F1 graphics using Pillow + CairoSVG.
Uses:
  - Local Orbitron TTF fonts for the racing aesthetic
  - Local circuit SVG files rendered via CairoSVG
  - Team colours mapped from constructor names


Each public ``render_*`` function returns a **file path** (str) pointing
to a temporary PNG. The caller passes the path directly to
``event.image_result(path)`` or ``Image.fromFileSystem(path)``.
"""


from __future__ import annotations


import hashlib
import io
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path


import aiohttp
import cairosvg
from PIL import Image, ImageDraw, ImageFont


from .models import (
    F1RaceWeekend,
    F1SessionSlot,
    JolpicaConstructorStanding,
    JolpicaDriverStanding,
    OpenF1Driver,
    OpenF1Position,
    OpenF1Session,
    OpenF1SessionResult,
)


# ── Plugin config (injected at runtime via configure()) ───────────────────────


_renderer_config: dict = {}
_HEADSHOT_CACHE_DIR: Path | None = None
_FORCE_REFRESH_HEADSHOTS: ContextVar[bool] = ContextVar(
    "f1_force_refresh_headshots", default=False
)




def configure(config, cache_dir: str | Path | None = None) -> None:
    """接收插件配置并更新卡片宽度、字号与持久头像目录。"""
    global _renderer_config, _HEADSHOT_CACHE_DIR
    global CARD_W, FONT_SCALE, LAYOUT_SCALE, HEADER_H, ROW_H, FOOTER_H
    global ROW_MARGIN_X, ROW_MARGIN_Y
    _renderer_config = config or {}
    try:
        CARD_W = int(_renderer_config.get("image_card_width", DEFAULT_CARD_WIDTH))
    except (TypeError, ValueError):
        CARD_W = DEFAULT_CARD_WIDTH
    CARD_W = min(max(CARD_W, MIN_CARD_WIDTH), MAX_CARD_WIDTH)

    try:
        scale_percent = int(
            _renderer_config.get("image_font_scale_percent", DEFAULT_FONT_SCALE_PERCENT)
        )
    except (TypeError, ValueError):
        scale_percent = DEFAULT_FONT_SCALE_PERCENT
    FONT_SCALE = min(max(scale_percent, MIN_FONT_SCALE_PERCENT), MAX_FONT_SCALE_PERCENT) / 100
    LAYOUT_SCALE = FONT_SCALE / (DEFAULT_FONT_SCALE_PERCENT / 100)
    HEADER_H = _s(92 * LAYOUT_SCALE)
    ROW_H = _s(66 * LAYOUT_SCALE)
    FOOTER_H = _s(42 * LAYOUT_SCALE)
    ROW_MARGIN_X = _s(10)
    ROW_MARGIN_Y = _s(5 * LAYOUT_SCALE)

    if cache_dir is not None:
        _HEADSHOT_CACHE_DIR = Path(cache_dir)
        try:
            _HEADSHOT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            _HEADSHOT_CACHE_DIR = None


@contextmanager
def force_refresh_headshots():
    """强制本次渲染重新下载头像，下载失败时仍回退到旧本地缓存。"""
    token = _FORCE_REFRESH_HEADSHOTS.set(True)
    try:
        yield
    finally:
        _FORCE_REFRESH_HEADSHOTS.reset(token)


# ── Paths ──────────────────────────────────────────────────────────────────────


_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"
_CIRCUITS_DIR = _ASSETS_DIR / "circuits"


# Flags stored under the AstrBot data directory for persistence across updates
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path


    _FLAGS_DIR = (
        Path(get_astrbot_data_path())
        / "plugin_data"
        / "astrbot_plugin_f1_notifier"
        / "flags"
    )
except Exception:
    _FLAGS_DIR = _ASSETS_DIR / "flags"


# ── Constants ──────────────────────────────────────────────────────────────────


SCALE = 2  # 仍以 2 倍分辨率渲染
DEFAULT_CARD_WIDTH = 1120  # 输出像素宽度，约比旧版缩窄五分之一
MIN_CARD_WIDTH = 1000
MAX_CARD_WIDTH = 1600
DEFAULT_FONT_SCALE_PERCENT = 125
MIN_FONT_SCALE_PERCENT = 100
MAX_FONT_SCALE_PERCENT = 140
FONT_SCALE = DEFAULT_FONT_SCALE_PERCENT / 100
LAYOUT_SCALE = 1.0




def _s(v: int | float) -> int:
    """Scale a pixel value by the global SCALE factor."""
    return int(v * SCALE)




CARD_W = DEFAULT_CARD_WIDTH
HEADER_H = _s(92)
ROW_H = _s(66)
FOOTER_H = _s(42)
ROW_MARGIN_X = _s(10)
ROW_MARGIN_Y = _s(5)
ROW_RADIUS = _s(10)
TEAM_BAR_W = _s(4)


# Colours
BG_TOP = (21, 21, 30)  # #15151E
BG_BOT = (26, 26, 46)  # #1A1A2E
HEADER_RED = (225, 6, 0)  # #E10600
WHITE = (255, 255, 255)
GOLD = (255, 215, 0)
SILVER = (192, 192, 192)
BRONZE = (205, 127, 50)
RED_ACCENT = (225, 6, 0)


CST = timezone(timedelta(hours=8))


# ── Team colours ───────────────────────────────────────────────────────────────


TEAM_COLOURS: dict[str, tuple[int, int, int]] = {
    "red bull": (54, 113, 198),
    "ferrari": (232, 0, 45),
    "mclaren": (255, 128, 0),
    "mercedes": (39, 244, 210),
    "aston martin": (34, 153, 113),
    "alpine": (0, 147, 204),
    "williams": (100, 196, 255),
    "racing bulls": (102, 146, 255),
    "rb": (102, 146, 255),
    "haas": (182, 186, 189),
    "sauber": (82, 226, 82),
    "audi": (255, 0, 0),
    "cadillac": (144, 144, 144),
}


# Jolpica circuit_id → SVG filename stem (julesr0y/f1-circuits-svg)
CIRCUIT_SVG_MAP: dict[str, str] = {
    "albert_park": "melbourne-2",
    "shanghai": "shanghai-1",
    "suzuka": "suzuka-2",
    "bahrain": "bahrain-1",
    "jeddah": "jeddah-1",
    "miami": "miami-1",
    "imola": "imola-3",
    "monaco": "monaco-6",
    "catalunya": "catalunya-6",
    "villeneuve": "montreal-6",
    "red_bull_ring": "spielberg-3",
    "silverstone": "silverstone-8",
    "spa": "spa-francorchamps-4",
    "hungaroring": "hungaroring-3",
    "zandvoort": "zandvoort-5",
    "monza": "monza-7",
    "baku": "baku-1",
    "marina_bay": "marina-bay-4",
    "americas": "austin-1",
    "rodriguez": "mexico-city-3",
    "interlagos": "interlagos-2",
    "vegas": "las-vegas-1",
    "losail": "lusail-1",
    "yas_marina": "yas-marina-2",
    "madring": "madring-1",
    "portimao": "portimao-1",
    "istanbul": "istanbul-1",
    "mugello": "mugello-1",
    "sochi": "sochi-1",
    "nurburgring": "nurburgring-4",
    "hockenheimring": "hockenheimring-4",
    "sepang": "sepang-1",
    "yeongam": "yeongam-1",
    "buddh": "buddh-1",
}


FLAG_MAP: dict[str, str] = {
    "Australia": "AU",
    "China": "CN",
    "Japan": "JP",
    "Bahrain": "BH",
    "Saudi Arabia": "SA",
    "USA": "US",
    "United States": "US",
    "Canada": "CA",
    "Monaco": "MC",
    "Spain": "ES",
    "Austria": "AT",
    "UK": "GB",
    "United Kingdom": "GB",
    "Belgium": "BE",
    "Hungary": "HU",
    "Netherlands": "NL",
    "Italy": "IT",
    "Azerbaijan": "AZ",
    "Singapore": "SG",
    "Mexico": "MX",
    "Brazil": "BR",
    "UAE": "AE",
    "United Arab Emirates": "AE",
    "Qatar": "QA",
}


# ── Font loading ───────────────────────────────────────────────────────────────


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_fallback_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | None] = {}
_missing_glyph_cache: dict[int, tuple[tuple[int, int], bytes]] = {}




# 保留内置 Orbitron 作为主字体，以维持 F1 视觉风格。
# 以下候选回退字体只用于处理 Orbitron 无法渲染的字符。
# 如果插件未来加入字体文件则优先使用，否则使用 Windows/Linux 系统字体。


_CJK_FONT_CANDIDATES = [
    _FONTS_DIR / "NotoSansSC-Regular.ttf",
    _FONTS_DIR / "NotoSansSC-VF.ttf",
    _FONTS_DIR / "SourceHanSansSC-Regular.otf",
    Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    Path(r"C:\Windows\Fonts\Deng.ttf"),
    Path(r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path(r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
]


_EMOJI_FONT_CANDIDATES = [
    _FONTS_DIR / "NotoColorEmoji.ttf",
    Path(r"C:\Windows\Fonts\seguiemj.ttf"),
    Path(r"/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
]




def _font(weight: str = "Bold", size: int = 14) -> ImageFont.FreeTypeFont:
    """Load an Orbitron font variant, cached. Font sizes are auto-scaled."""
    scaled = _s(size * FONT_SCALE)
    key = (weight, scaled)
    if key not in _font_cache:
        path = _FONTS_DIR / f"Orbitron-{weight}.ttf"
        if not path.exists():
            path = _FONTS_DIR / "Orbitron-Regular.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), scaled)
    return _font_cache[key]




def _load_first_font(
    candidates: list[Path], size: int, cache_key: str
) -> ImageFont.FreeTypeFont | None:
    """按候选顺序加载已经使用渲染尺寸的回退字体。"""
    key = (cache_key, size)
    if key in _fallback_font_cache:
        return _fallback_font_cache[key]
    for path in candidates:
        if not path.exists():
            continue
        try:
            loaded = ImageFont.truetype(str(path), size)
            _fallback_font_cache[key] = loaded
            return loaded
        except (OSError, ValueError):
            continue
    _fallback_font_cache[key] = None
    return None




def _fallback_font(primary: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont | None:
    return _load_first_font(_CJK_FONT_CANDIDATES, primary.size, "cjk")




def _emoji_font(primary: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont | None:
    return _load_first_font(_EMOJI_FONT_CANDIDATES, primary.size, "emoji")




def _is_emoji_character(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
        or codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299, 0xFE0F, 0x200D}
    )




def _font_has_glyph(font: ImageFont.FreeTypeFont, char: str) -> bool:
    """通过 Pillow 的缺字形掩码尽力判断字体是否支持字符。"""
    if not char or char.isspace() or char in {"\u200b", "\u200c", "\u200d", "\ufe0f"}:
        return True
    try:
        missing = _missing_glyph_cache.get(id(font))
        if missing is None:
            missing_mask = font.getmask("\ufffd")
            missing = (missing_mask.size, bytes(missing_mask))
            _missing_glyph_cache[id(font)] = missing
        current_mask = font.getmask(char)
        return (current_mask.size, bytes(current_mask)) != missing
    except (OSError, ValueError, UnicodeError):
        return False




def _font_runs(
    text: str, primary: ImageFont.FreeTypeFont
) -> list[tuple[str, ImageFont.FreeTypeFont, bool]]:
    """将文本拆分为可由同一字体渲染的连续片段。"""
    cjk = _fallback_font(primary)
    emoji = _emoji_font(primary)
    runs: list[tuple[str, ImageFont.FreeTypeFont, bool]] = []
    for char in str(text):
        if _font_has_glyph(primary, char):
            selected = primary
            is_emoji = False
        elif _is_emoji_character(char) and emoji is not None:
            selected = emoji
            is_emoji = True
        elif cjk is not None and _font_has_glyph(cjk, char):
            selected = cjk
            is_emoji = False
        else:
            # 最后保留原有行为作为兜底；如果仍不支持，字形可能显示为方框。


            selected = primary
            is_emoji = False


        if runs and runs[-1][1] is selected and runs[-1][2] == is_emoji:
            runs[-1] = (runs[-1][0] + char, selected, is_emoji)
        else:
            runs.append((char, selected, is_emoji))
    return runs




def _aligned_run_y(
    primary: ImageFont.FreeTypeFont,
    run_font: ImageFont.FreeTypeFont,
    text: str,
    y: int,
) -> int:
    """将回退字体的字形对齐到主字体的视觉行框。"""
    if run_font is primary:
        return y
    primary_box = primary.getbbox("Ag")
    run_box = run_font.getbbox(text)
    primary_center = (primary_box[1] + primary_box[3]) / 2
    run_center = (run_box[1] + run_box[3]) / 2
    return int(round(y + primary_center - run_center))




def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill,
    font: ImageFont.FreeTypeFont,
    max_width: int | None = None,
) -> None:
    """优先使用 Orbitron，必要时按最大宽度截短并执行字体回退。"""
    x, y = xy
    edge_width = max(0, CARD_W - x - _s(8))
    max_width = edge_width if max_width is None else min(max_width, edge_width)
    text = _fit_text_to_width(draw, str(text), font, max_width)
    for run_text, run_font, is_emoji in _font_runs(str(text), font):
        run_y = _aligned_run_y(font, run_font, run_text, y)
        options = {"embedded_color": True} if is_emoji else {}
        try:
            draw.text((x, run_y), run_text, fill=fill, font=run_font, **options)
        except TypeError:
            # 兼容不支持 embedded_color 参数的旧版 Pillow。
            draw.text((x, run_y), run_text, fill=fill, font=run_font)
        x += int(round(draw.textlength(run_text, font=run_font)))




def _textbbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int]:
    """返回与 _draw_text 回退布局一致的边界框。"""
    runs = _font_runs(str(text), font)
    if not runs:
        return (0, 0, 0, 0)
    x = 0
    top = 0
    bottom = 0
    for run_text, run_font, is_emoji in runs:
        box = draw.textbbox((0, 0), run_text, font=run_font)
        run_y = _aligned_run_y(font, run_font, run_text, 0)
        top = min(top, run_y + box[1])
        bottom = max(bottom, run_y + box[3])
        x += int(round(draw.textlength(run_text, font=run_font)))
    return (0, top, x, bottom)




def _fit_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    """按真实回退字体测量文本，超宽时以省略号截短。"""
    max_width = max(0, int(max_width))
    if max_width == 0:
        return ""
    if _textbbox(draw, text, font)[2] <= max_width:
        return text
    ellipsis = "…"
    if _textbbox(draw, ellipsis, font)[2] > max_width:
        ellipsis = "..."
        if _textbbox(draw, ellipsis, font)[2] > max_width:
            return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + ellipsis
        if _textbbox(draw, candidate, font)[2] <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + ellipsis




# ── Helpers ────────────────────────────────────────────────────────────────────




def _flag(country: str) -> str:
    return FLAG_MAP.get(country, "")




def _cc_to_twemoji_stem(code: str) -> str:
    """Convert 2-letter country code to twemoji filename stem, e.g. 'AU' → '1f1e6-1f1fa'."""
    return "-".join(f"{0x1F1E6 + ord(c) - ord('A'):x}" for c in code.upper())




async def _download_to_file(url: str, dest: Path, timeout: int = 5) -> bool:
    """Download a URL to a local file. Returns True on success."""
    if not url.startswith(("http://", "https://")):
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": "AstrBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(await resp.read())
        return True
    except Exception:
        return False




async def _load_flag_image(country: str, size: int = 20) -> Image.Image | None:
    """Load a flag image (twemoji SVG) for a country name."""
    code = FLAG_MAP.get(country)
    if not code:
        return None
    stem = _cc_to_twemoji_stem(code)
    svg_path = _FLAGS_DIR / f"{stem}.svg"
    if not svg_path.exists():
        url = (
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/svg/{stem}.svg"
        )
        if not await _download_to_file(url, svg_path):
            return None
    try:
        png_data = cairosvg.svg2png(
            url=str(svg_path), output_width=size, output_height=size
        )
        return Image.open(io.BytesIO(png_data)).convert("RGBA")
    except Exception:
        return None




# 内存中缓存圆形头像，数量受 headshot_cache_max 限制；原始图片另存到本地目录。
_HEADSHOTS_CACHE: dict[str, Image.Image | None] = {}




def _make_circular(source: Image.Image, size: int) -> Image.Image:
    """将头像缩放并裁剪为圆形。"""
    source = source.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(source, (0, 0), mask)
    return result




def _headshot_cache_path(url: str) -> Path | None:
    if _HEADSHOT_CACHE_DIR is None:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _HEADSHOT_CACHE_DIR / f"{digest}.img"




def _read_persisted_headshot(url: str, size: int) -> Image.Image | None:
    path = _headshot_cache_path(url)
    if path is None or not path.is_file():
        return None
    try:
        with Image.open(path) as source:
            return _make_circular(source.convert("RGBA"), size)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None




def _persist_headshot(url: str, data: bytes) -> None:
    path = _headshot_cache_path(url)
    if path is None:
        return
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f"{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(data)
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
        _prune_persisted_headshots()
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass




def _prune_persisted_headshots() -> None:
    if _HEADSHOT_CACHE_DIR is None:
        return
    max_count = max(0, int(_renderer_config.get("headshot_cache_max", 30)))
    try:
        files = sorted(
            _HEADSHOT_CACHE_DIR.glob("*.img"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[max_count:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return




async def _read_limited_response(response, max_bytes: int) -> bytes:
    """分块读取完整头像响应，并限制总大小，避免单次 read 得到截断数据。"""
    content_length = getattr(response, "content_length", None)
    if content_length is not None and content_length > max_bytes:
        raise ValueError(f"头像响应超过 {max_bytes} 字节限制")

    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > max_bytes:
            raise ValueError(f"头像响应超过 {max_bytes} 字节限制")
        body.extend(chunk)
    return bytes(body)


async def _load_headshot(url: str | None, size: int = 32) -> Image.Image | None:
    """优先读取本地头像；缺失时下载并同时写入内存和磁盘。"""
    if not url or not url.startswith(("http://", "https://")):
        return None


    force_refresh = _FORCE_REFRESH_HEADSHOTS.get()
    cached = _HEADSHOTS_CACHE.get(url)
    if not force_refresh and url in _HEADSHOTS_CACHE:
        return cached.copy() if cached else None


    fallback = cached.copy() if cached is not None else None
    if fallback is None:
        fallback = _read_persisted_headshot(url, size)
    if not force_refresh and fallback is not None:
        _headshot_cache_evict()
        _HEADSHOTS_CACHE[url] = fallback
        return fallback.copy()


    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "AstrBot/1.0"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    resp.raise_for_status()
                    data = await _read_limited_response(resp, 1024 * 1024)
            with Image.open(io.BytesIO(data)) as source:
                raw = source.convert("RGBA")
            result = _make_circular(raw, size)
            _persist_headshot(url, data)
            _headshot_cache_evict()
            _HEADSHOTS_CACHE[url] = result
            return result.copy()
        except Exception:
            if attempt == 0:
                continue


    # 刷新请求失败时保留旧头像；首次下载失败不写入失败缓存。
    return fallback.copy() if fallback is not None else None




def _headshot_cache_evict() -> None:
    """按配置移除最早加入内存缓存的头像。"""
    max_count = max(0, int(_renderer_config.get("headshot_cache_max", 30)))
    while len(_HEADSHOTS_CACHE) >= max_count and _HEADSHOTS_CACHE:
        _HEADSHOTS_CACHE.pop(next(iter(_HEADSHOTS_CACHE)), None)


async def _draw_flagged_text(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    country: str,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    flag_size: int = _s(20),
    max_width: int | None = None,
) -> None:
    """绘制可选国旗和文本，并按剩余空间截短文字。"""
    x, y_pos = xy
    flag_img = await _load_flag_image(country, flag_size)
    remaining_width = max_width
    if flag_img is not None:
        flag_y = y_pos + max(0, (font.size - flag_size) // 2)
        img.paste(flag_img, (x, int(flag_y)), flag_img)
        x += flag_size + _s(6)
        if remaining_width is not None:
            remaining_width = max(0, remaining_width - flag_size - _s(6))
    _draw_text(
        draw,
        (x, y_pos),
        text,
        fill=fill,
        font=font,
        max_width=remaining_width,
    )

def _team_colour(constructor_name: str) -> tuple[int, int, int]:
    name_lower = constructor_name.lower()
    for key, colour in TEAM_COLOURS.items():
        if key in name_lower:
            return colour
    return (255, 255, 255)




def _parse_openf1_team_colour(drv: OpenF1Driver) -> tuple[int, int, int] | None:
    """Parse hex team colour from OpenF1 driver data."""
    if not drv.team_colour:
        return None
    try:
        h = drv.team_colour.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return None




def _utc_to_cst(date_str: str, time_str: str) -> str:
    try:
        return datetime.fromisoformat(f"{date_str}T{time_str}").astimezone(CST).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return f"{date_str} {time_str}"




def _session_cst(s: F1SessionSlot | None) -> str | None:
    if s is None:
        return None
    return _utc_to_cst(s.date, s.time)




def _format_lap_duration(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"




def _load_circuit_image(circuit_id: str, size: int = 80) -> Image.Image | None:
    """Load circuit SVG as a Pillow RGBA image."""
    layout_id = CIRCUIT_SVG_MAP.get(circuit_id, "")
    if not layout_id:
        return None
    svg_path = _CIRCUITS_DIR / f"{layout_id}.svg"
    if not svg_path.exists():
        return None
    try:
        png_data = cairosvg.svg2png(
            url=str(svg_path), output_width=size, output_height=size
        )
        return Image.open(io.BytesIO(png_data)).convert("RGBA")
    except Exception:
        return None




def _load_f1_logo(size: int = 50) -> Image.Image | None:
    """Load the F1 logo SVG from assets."""
    svg_path = _ASSETS_DIR / "f1.svg"
    if not svg_path.exists():
        return None
    try:
        png_data = cairosvg.svg2png(
            url=str(svg_path), output_width=size, output_height=size
        )
        return Image.open(io.BytesIO(png_data)).convert("RGBA")
    except Exception:
        return None




def _pos_colour(pos: int) -> tuple[int, int, int]:
    if pos == 1:
        return GOLD
    if pos == 2:
        return SILVER
    if pos == 3:
        return BRONZE
    return WHITE




_generated_files: list[tuple[float, str]] = []
_save_counter = 0




def _get_cleanup_max_age() -> float:
    """Max age in seconds before cached images are deleted (from config)."""
    return int(_renderer_config.get("image_cache_max_age", 30)) * 60




def _get_cache_max_count() -> int:
    """Max number of generated image files to keep in the cache list."""
    return int(_renderer_config.get("image_cache_max_count", 50))




def _cleanup_old_images() -> None:
    """Remove generated temp images that are too old or exceed the max count."""
    import os
    import time


    max_age = _get_cleanup_max_age()
    max_count = _get_cache_max_count()
    now = time.time()
    remaining: list[tuple[float, str]] = []
    for ts, fpath in _generated_files:
        if now - ts > max_age:
            try:
                os.remove(fpath)
            except OSError:
                pass
        else:
            remaining.append((ts, fpath))
    # Also enforce max count: evict oldest entries first
    while len(remaining) > max_count:
        _, fpath = remaining.pop(0)
        try:
            os.remove(fpath)
        except OSError:
            pass
    _generated_files.clear()
    _generated_files.extend(remaining)




def _save_image(img: Image.Image) -> str:
    """Save image to a temp file and return the path. Old images are auto-cleaned."""
    global _save_counter
    import os
    import time


    fd, path = tempfile.mkstemp(suffix=".png")
    img.save(path, format="PNG", optimize=True)
    os.close(fd)


    _generated_files.append((time.time(), path))
    _save_counter += 1
    # Trigger cleanup every 20 saves, or whenever the cache exceeds the max count
    if _save_counter % 20 == 0 or len(_generated_files) > _get_cache_max_count():
        _cleanup_old_images()


    return path




# ── Drawing primitives ─────────────────────────────────────────────────────────




async def _create_card(
    n_rows: int,
    header_title: str,
    header_sub: str,
    circuit_id: str = "",
    extra_body_h: int = 0,
    country: str = "",
) -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    """Create the base card image with header. Returns (image, draw, y_cursor)."""
    body_h = n_rows * (ROW_H + ROW_MARGIN_Y) + extra_body_h + FOOTER_H + _s(16 * LAYOUT_SCALE)
    total_h = HEADER_H + body_h
    img = Image.new("RGBA", (CARD_W, total_h), BG_TOP)
    draw = ImageDraw.Draw(img)


    # Background bottom half
    draw.rectangle((0, HEADER_H, CARD_W, total_h), fill=BG_BOT)


    # Header bar
    draw.rectangle((0, 0, CARD_W, HEADER_H), fill=HEADER_RED)


    # F1 logo in header
    logo = _load_f1_logo(_s(128))
    if logo is not None:
        logo_x = CARD_W - _s(28) - logo.width
        logo_y = (HEADER_H - logo.height) // 2
        img.paste(logo, (logo_x, logo_y), logo)


    # Circuit overlay in header (semi-transparent)
    circuit_img = _load_circuit_image(circuit_id, _s(56))
    if circuit_img is not None:
        alpha = circuit_img.split()[3].point(lambda p: int(p * 0.3))
        circuit_img.putalpha(alpha)
        cx = CARD_W - _s(180) - circuit_img.width
        cy = (HEADER_H - circuit_img.height) // 2
        img.paste(circuit_img, (cx, cy), circuit_img)


    # Header text
    title_font = _font("ExtraBold", 18)
    sub_font = _font("Medium", 11)
    await _draw_flagged_text(
        img, draw, (_s(28), _s(18 * LAYOUT_SCALE)), country, header_title, title_font, WHITE, flag_size=_s(22), max_width=CARD_W - _s(190)
    )
    _draw_text(draw, (_s(28), _s(46 * LAYOUT_SCALE)), header_sub, fill=(255, 255, 255, 220), font=sub_font)


    y = HEADER_H + _s(8 * LAYOUT_SCALE)
    return img, draw, y




def _draw_footer(draw: ImageDraw.ImageDraw, img_h: int) -> None:
    """Draw the footer text."""
    footer_font = _font("Regular", 8)
    text = _fit_text_to_width(draw, "F1 NOTIFIER · POWERED BY ASTRBOT", footer_font, CARD_W - _s(24))
    bbox = _textbbox(draw, text, footer_font)
    tw = bbox[2] - bbox[0]
    _draw_text(draw, 
        ((CARD_W - tw) // 2, img_h - FOOTER_H + _s(8 * LAYOUT_SCALE)),
        text,
        fill=(255, 255, 255, 80),
        font=footer_font,
    )




async def _draw_driver_row(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    pos: int,
    driver_name: str,
    team_name: str,
    team_colour: tuple[int, int, int],
    acronym: str = "",
    stats: list[tuple[str, str]] | None = None,
    stat_col_widths: list[int] | None = None,
    headshot_url: str | None = None,
) -> int:
    """Draw a single driver/result row. Returns new y position."""
    x0 = ROW_MARGIN_X
    x1 = CARD_W - ROW_MARGIN_X
    row_y0 = y
    row_y1 = y + ROW_H


    # Row background with podium highlight
    if pos == 1:
        bg = (255, 215, 0, 25)
    elif pos == 2:
        bg = (192, 192, 192, 20)
    elif pos == 3:
        bg = (205, 127, 50, 20)
    else:
        bg = (255, 255, 255, 15 if (pos or 0) % 2 == 0 else 10)


    # Overlay for alpha blending
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ovr_draw = ImageDraw.Draw(overlay)
    ovr_draw.rounded_rectangle((x0, row_y0, x1, row_y1), radius=ROW_RADIUS, fill=bg)
    img.alpha_composite(overlay)


    # Team colour bar
    draw.rectangle((x0, row_y0 + _s(6 * LAYOUT_SCALE), x0 + TEAM_BAR_W, row_y1 - _s(6 * LAYOUT_SCALE)), fill=team_colour)


    # Position number
    pos_font = _font("ExtraBold", 20)
    pos_col = _pos_colour(pos)
    _draw_text(draw, 
        (x0 + _s(16), row_y0 + (ROW_H - _s(28 * LAYOUT_SCALE)) // 2), str(pos) if pos is not None else "-", fill=pos_col, font=pos_font
    )


    # Driver acronym circle or headshot
    circle_x = x0 + _s(56)
    circle_y = row_y0 + (ROW_H - _s(36)) // 2
    circle_r = _s(18)


    headshot = await _load_headshot(headshot_url, _s(32))
    if headshot is not None:
        # Team colour circle border
        draw.ellipse(
            (circle_x, circle_y, circle_x + circle_r * 2, circle_y + circle_r * 2),
            outline=team_colour,
            width=_s(2),
        )
        # Paste circular headshot inside border
        img.paste(headshot, (circle_x + _s(2), circle_y + _s(2)), headshot)
    else:
        draw.ellipse(
            (circle_x, circle_y, circle_x + circle_r * 2, circle_y + circle_r * 2),
            outline=team_colour,
            width=_s(2),
        )
        acr_font = _font("Bold", 11)
        acr_text = acronym[:3].upper() if acronym else driver_name[:3].upper()
        acr_bbox = _textbbox(draw, acr_text, acr_font)
        acr_w = acr_bbox[2] - acr_bbox[0]
        acr_h = acr_bbox[3] - acr_bbox[1]
        _draw_text(draw, 
            (circle_x + circle_r - acr_w // 2, circle_y + circle_r - acr_h // 2 - _s(1)),
            acr_text,
            fill=WHITE,
            font=acr_font,
        )


    # 先计算右侧统计栏占位，再将车手名和车队名限制在剩余宽度内。
    label_font = _font("Regular", 8)
    value_font = _font("SemiBold", 12)
    driver_stat_widths = (
        (stat_col_widths or _calc_stat_col_widths([stats])) if stats else []
    )
    if stats:
        stats_left = (
            x1
            - _s(16)
            - sum(driver_stat_widths)
            - _s(16) * max(0, len(stats) - 1)
        )
    else:
        stats_left = x1 - _s(8)
    name_x = circle_x + circle_r * 2 + _s(12)
    name_max_width = max(0, stats_left - name_x - _s(8))
    name_font = _font("Bold", 13)
    team_font = _font("Regular", 10)
    _draw_text(
        draw,
        (name_x, row_y0 + _s(8 * LAYOUT_SCALE)),
        driver_name,
        fill=WHITE,
        font=name_font,
        max_width=name_max_width,
    )
    _draw_text(
        draw,
        (name_x, row_y0 + _s(28 * LAYOUT_SCALE)),
        team_name,
        fill=(255, 255, 255, 150),
        font=team_font,
        max_width=name_max_width,
    )
    # 右侧统计栏保持右对齐，超长字段在自己的列内截短。
    if stats:
        stat_x = x1 - _s(16)
        for i, (label, value) in enumerate(reversed(stats)):
            col_w = driver_stat_widths[len(stats) - 1 - i]
            col_text_width = max(0, col_w - _s(8))
            _draw_text(
                draw,
                (stat_x - col_w, row_y0 + _s(8 * LAYOUT_SCALE)),
                label,
                fill=(255, 255, 255, 130),
                font=label_font,
                max_width=col_text_width,
            )
            val_col = RED_ACCENT if label == "PTS" and pos is not None and pos <= 3 else WHITE
            _draw_text(
                draw,
                (stat_x - col_w, row_y0 + _s(24 * LAYOUT_SCALE)),
                value,
                fill=val_col,
                font=value_font,
                max_width=col_text_width,
            )
            stat_x -= col_w + _s(16)
    return row_y1 + ROW_MARGIN_Y




def _draw_constructor_row(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    pos: int,
    constructor_name: str,
    team_colour: tuple[int, int, int],
    stats: list[tuple[str, str]] | None = None,
    stat_col_widths: list[int] | None = None,
) -> int:
    """Draw a single constructor row. Returns new y position."""
    x0 = ROW_MARGIN_X
    x1 = CARD_W - ROW_MARGIN_X
    row_y0 = y
    row_y1 = y + ROW_H


    if pos == 1:
        bg = (255, 215, 0, 25)
    elif pos == 2:
        bg = (192, 192, 192, 20)
    elif pos == 3:
        bg = (205, 127, 50, 20)
    else:
        bg = (255, 255, 255, 15 if (pos or 0) % 2 == 0 else 10)


    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ovr_draw = ImageDraw.Draw(overlay)
    ovr_draw.rounded_rectangle((x0, row_y0, x1, row_y1), radius=ROW_RADIUS, fill=bg)
    img.alpha_composite(overlay)


    draw.rectangle((x0, row_y0 + _s(6 * LAYOUT_SCALE), x0 + TEAM_BAR_W, row_y1 - _s(6 * LAYOUT_SCALE)), fill=team_colour)


    # Position
    pos_font = _font("ExtraBold", 20)
    _draw_text(draw, 
        (x0 + _s(16), row_y0 + (ROW_H - _s(28 * LAYOUT_SCALE)) // 2),
        str(pos),
        fill=_pos_colour(pos),
        font=pos_font,
    )


    # 统计栏宽度确定后，将车队名称限制在左侧可用空间内。
    label_font = _font("Regular", 8)
    value_font = _font("SemiBold", 12)
    pts_font = _font("ExtraBold", 18)
    constructor_stat_widths = (
        (stat_col_widths or _calc_constructor_stat_col_widths([stats]))
        if stats
        else []
    )
    name_x = x0 + _s(56)
    if stats:
        stats_left = (
            x1
            - _s(16)
            - sum(constructor_stat_widths)
            - _s(16) * max(0, len(stats) - 1)
        )
    else:
        stats_left = x1 - _s(8)
    name_font = _font("Bold", 14)
    _draw_text(
        draw,
        (name_x, row_y0 + (ROW_H - _s(18 * LAYOUT_SCALE)) // 2),
        constructor_name,
        fill=WHITE,
        font=name_font,
        max_width=max(0, stats_left - name_x - _s(8)),
    )
    if stats:
        stat_x = x1 - _s(16)
        for i, (label, value) in enumerate(reversed(stats)):
            is_pts = label == "POINTS"
            vf = pts_font if is_pts else value_font
            col_w = constructor_stat_widths[len(stats) - 1 - i]
            col_text_width = max(0, col_w - _s(8))
            _draw_text(
                draw,
                (stat_x - col_w, row_y0 + _s(8 * LAYOUT_SCALE)),
                label,
                fill=(255, 255, 255, 130),
                font=label_font,
                max_width=col_text_width,
            )
            col = RED_ACCENT if is_pts else WHITE
            vy = row_y0 + _s(20 * LAYOUT_SCALE) if is_pts else row_y0 + _s(24 * LAYOUT_SCALE)
            _draw_text(
                draw,
                (stat_x - col_w, vy),
                value,
                fill=col,
                font=vf,
                max_width=col_text_width,
            )
            stat_x -= col_w + _s(16)
    return row_y1 + ROW_MARGIN_Y




def _calc_stat_col_widths(all_stats: list[list[tuple[str, str]]]) -> list[int]:
    """Pre-calculate per-column max widths across all rows for aligned stat rendering."""
    if not all_stats:
        return []
    label_font = _font("Regular", 8)
    value_font = _font("SemiBold", 12)
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    n_cols = max(len(s) for s in all_stats)
    widths = [0] * n_cols
    for row_stats in all_stats:
        for i, (label, value) in enumerate(row_stats):
            vbbox = _textbbox(scratch, str(value), value_font)
            lbbox = _textbbox(scratch, label, label_font)
            col_w = max(vbbox[2] - vbbox[0], lbbox[2] - lbbox[0]) + _s(8)
            widths[i] = max(widths[i], col_w)
    return widths




def _calc_constructor_stat_col_widths(all_stats: list[list[tuple[str, str]]]) -> list[int]:
    """Pre-calculate per-column max widths for constructor rows (POINTS uses large font)."""
    if not all_stats:
        return []
    label_font = _font("Regular", 8)
    value_font = _font("SemiBold", 12)
    pts_font = _font("ExtraBold", 18)
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    n_cols = max(len(s) for s in all_stats)
    widths = [0] * n_cols
    for row_stats in all_stats:
        for i, (label, value) in enumerate(row_stats):
            vf = pts_font if label == "POINTS" else value_font
            vbbox = _textbbox(scratch, str(value), vf)
            lbbox = _textbbox(scratch, label, label_font)
            col_w = max(vbbox[2] - vbbox[0], lbbox[2] - lbbox[0]) + _s(8)
            widths[i] = max(widths[i], col_w)
    return widths




# ── Schedule / next race drawing ───────────────────────────────────────────────




async def _draw_schedule_item(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y: int,
    race: F1RaceWeekend,
) -> int:
    """Draw a single schedule item block. Returns new y position."""
    x0 = ROW_MARGIN_X
    x1 = CARD_W - ROW_MARGIN_X
    content_right = x1 - _s(8)


    # Collect session times
    session_slots: list[tuple[F1SessionSlot | None, str]] = [
        (race.first_practice, "FP1"),
        (race.sprint_qualifying, "Sprint Quali"),
        (race.second_practice, "FP2"),
        (race.sprint, "Sprint"),
        (race.third_practice, "FP3"),
        (race.qualifying, "Qualifying"),
    ]
    sessions: list[tuple[str, str]] = []
    for slot, label in session_slots:
        t = _session_cst(slot)
        if t:
            sessions.append((label, f"{t} CST"))
    race_time = _utc_to_cst(race.date, race.time)
    sessions.append(("Race", f"{race_time} CST"))


    item_h = _s(44 * LAYOUT_SCALE) + len(sessions) * _s(14 * LAYOUT_SCALE)
    row_y1 = y + item_h


    # Background
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ovr_draw = ImageDraw.Draw(overlay)
    ovr_draw.rounded_rectangle(
        (x0, y, x1, row_y1), radius=ROW_RADIUS, fill=(255, 255, 255, 12)
    )
    img.alpha_composite(overlay)


    # Circuit SVG on the right side
    circ_size = min(item_h - _s(16), _s(90))
    circuit_img = _load_circuit_image(race.circuit_id, circ_size)
    if circuit_img is not None:
        alpha = circuit_img.split()[3].point(lambda p: int(p * 0.45))
        circuit_img.putalpha(alpha)
        cx = x1 - _s(8) - circuit_img.width
        content_right = max(x0 + _s(72), cx - _s(10))
        cy = y + (item_h - circuit_img.height) // 2
        img.paste(circuit_img, (cx, cy), circuit_img)


    # Red accent bar
    draw.rectangle((x0, y + _s(6 * LAYOUT_SCALE), x0 + TEAM_BAR_W, row_y1 - _s(6 * LAYOUT_SCALE)), fill=RED_ACCENT)


    # Round label + sprint badge
    round_font = _font("Medium", 10)
    round_text = f"ROUND {race.round}"
    _draw_text(draw, (x0 + _s(16), y + _s(8 * LAYOUT_SCALE)), round_text, fill=(255, 255, 255, 130), font=round_font)


    if race.is_sprint_weekend:
        sprint_font = _font("Bold", 8)
        badge_text = "SPRINT"
        bb = _textbbox(draw, badge_text, sprint_font)
        bw = bb[2] - bb[0] + _s(12)
        bh = bb[3] - bb[1] + _s(6)
        rbbox = _textbbox(draw, round_text, round_font)
        badge_x = x0 + _s(16) + rbbox[2] - rbbox[0] + _s(8)
        draw.rounded_rectangle(
            (badge_x, y + _s(8 * LAYOUT_SCALE), badge_x + bw, y + _s(8 * LAYOUT_SCALE) + bh), radius=_s(3), fill=RED_ACCENT
        )
        _draw_text(draw, (badge_x + _s(6), y + _s(10 * LAYOUT_SCALE)), badge_text, fill=WHITE, font=sprint_font)


    # Race name with flag image
    title_font = _font("Bold", 14)
    await _draw_flagged_text(
        img,
        draw,
        (x0 + _s(16), y + _s(24 * LAYOUT_SCALE)),
        race.country,
        race.race_name,
        title_font,
        WHITE,
        flag_size=_s(18),
        max_width=content_right - (x0 + _s(16)),
    )


    # Sessions list
    session_font = _font("Regular", 10)
    race_font = _font("SemiBold", 10)
    sy = y + _s(44 * LAYOUT_SCALE)
    for label, time_str in sessions:
        is_race = label == "Race"
        f = race_font if is_race else session_font
        col = RED_ACCENT if is_race else (255, 255, 255, 180)
        _draw_text(
            draw,
            (x0 + _s(28), sy),
            f"{label}: {time_str}",
            fill=col,
            font=f,
            max_width=content_right - (x0 + _s(28)),
        )
        sy += _s(14 * LAYOUT_SCALE)


    return row_y1 + ROW_MARGIN_Y + _s(4 * LAYOUT_SCALE)




# ── Public render functions ────────────────────────────────────────────────────




async def render_race_result(race: F1RaceWeekend) -> str:
    """Render race result as a PNG image. Returns file path."""
    n = len(race.race_results)
    img, draw, y = await _create_card(
        n,
        race.race_name,
        f"ROUND {race.round} · RACE RESULT",
        circuit_id=race.circuit_id,
        country=race.country,
    )
    all_stats = [
        [("TIME", res.time if res.time else (res.status or "-")), ("LAPS", res.laps), ("PTS", res.points)]
        for res in race.race_results
    ]
    col_widths = _calc_stat_col_widths(all_stats)
    for res, stats in zip(race.race_results, all_stats):
        pos = res.position
        colour = _team_colour(res.team_name)
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            f"{res.driver_first_name} {res.driver_last_name}",
            res.team_name,
            colour,
            acronym=res.driver_last_name[:3],
            stats=stats,
            stat_col_widths=col_widths,
            headshot_url=res.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_qualifying_result(race: F1RaceWeekend) -> str:
    """Render qualifying result as a PNG image. Returns file path."""
    n = len(race.qualifying_results)
    img, draw, y = await _create_card(
        n,
        race.race_name,
        f"ROUND {race.round} · QUALIFYING",
        circuit_id=race.circuit_id,
        country=race.country,
    )
    all_stats = [
        [("Q1", res.q1), ("Q2", res.q2), ("Q3", res.q3)]
        for res in race.qualifying_results
    ]
    col_widths = _calc_stat_col_widths(all_stats)
    for res, stats in zip(race.qualifying_results, all_stats):
        pos = res.position
        colour = _team_colour(res.team_name)
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            f"{res.driver_first_name} {res.driver_last_name}",
            res.team_name,
            colour,
            acronym=res.driver_last_name[:3],
            stats=stats,
            stat_col_widths=col_widths,
            headshot_url=res.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_sprint_result(race: F1RaceWeekend) -> str:
    """Render sprint result as a PNG image. Returns file path."""
    n = len(race.sprint_results)
    img, draw, y = await _create_card(
        n,
        race.race_name,
        f"ROUND {race.round} · SPRINT",
        circuit_id=race.circuit_id,
        country=race.country,
    )
    all_stats = [
        [("TIME", res.time if res.time else (res.status or "-")), ("PTS", res.points)]
        for res in race.sprint_results
    ]
    col_widths = _calc_stat_col_widths(all_stats)
    for res, stats in zip(race.sprint_results, all_stats):
        pos = res.position
        colour = _team_colour(res.team_name)
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            f"{res.driver_first_name} {res.driver_last_name}",
            res.team_name,
            colour,
            acronym=res.driver_last_name[:3],
            stats=stats,
            stat_col_widths=col_widths,
            headshot_url=res.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_driver_standings(
    standings: list[JolpicaDriverStanding], limit: int | None = None
) -> str:
    """Render driver standings as a PNG image. Returns file path."""
    entries = standings if limit is None else standings[: max(0, limit)]
    img, draw, y = await _create_card(len(entries), "DRIVER STANDINGS", "CHAMPIONSHIP")
    all_stats = [
        [("WINS", entry.wins), ("POINTS", entry.points)]
        for entry in entries
    ]
    col_widths = _calc_stat_col_widths(all_stats)
    for entry, stats in zip(entries, all_stats):
        pos = entry.pos_int
        colour = _team_colour(entry.primary_team)
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            entry.driver.full_name,
            entry.primary_team,
            colour,
            acronym=entry.driver.family_name[:3],
            stats=stats,
            stat_col_widths=col_widths,
            headshot_url=entry.driver.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_constructor_standings(
    standings: list[JolpicaConstructorStanding],
) -> str:
    """Render constructor standings as a PNG image. Returns file path."""
    img, draw, y = await _create_card(len(standings), "CONSTRUCTOR STANDINGS", "CHAMPIONSHIP")
    all_stats = [
        [("WINS", entry.wins), ("POINTS", entry.points)]
        for entry in standings
    ]
    col_widths = _calc_constructor_stat_col_widths(all_stats)
    for entry, stats in zip(standings, all_stats):
        pos = entry.pos_int
        colour = _team_colour(entry.constructor.name)
        y = _draw_constructor_row(
            img, draw, y, pos, entry.constructor.name, colour,
            stats=stats, stat_col_widths=col_widths,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_schedule(races: list[F1RaceWeekend], limit: int = 5) -> str:
    """Render upcoming schedule as a PNG image. Returns file path."""
    now = datetime.now(timezone.utc)
    upcoming: list[F1RaceWeekend] = []
    for race in races:
        try:
            dt_utc = datetime.fromisoformat(f"{race.date}T{race.time}")
            if dt_utc >= now:
                upcoming.append(race)
        except (ValueError, TypeError, AttributeError):
            continue
    upcoming = upcoming[:limit]


    if not upcoming:
        img = Image.new("RGBA", (CARD_W, HEADER_H + _s(80 * LAYOUT_SCALE)), BG_TOP)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, CARD_W, HEADER_H), fill=HEADER_RED)
        tf = _font("ExtraBold", 18)
        _draw_text(draw, (_s(28), _s(28 * LAYOUT_SCALE)), "SCHEDULE", fill=WHITE, font=tf)
        msg_font = _font("Regular", 14)
        _draw_text(draw, 
            (CARD_W // 2 - _s(100), HEADER_H + _s(24 * LAYOUT_SCALE)),
            "No upcoming races",
            fill=(255, 255, 255, 130),
            font=msg_font,
        )
        return _save_image(img)


    # Pre-calculate total height
    total_items_h = 0
    for race in upcoming:
        session_slots: list[tuple[F1SessionSlot | None, str]] = [
            (race.first_practice, "FP1"),
            (race.sprint_qualifying, "Sprint Quali"),
            (race.second_practice, "FP2"),
            (race.sprint, "Sprint"),
            (race.third_practice, "FP3"),
            (race.qualifying, "Qualifying"),
        ]
        n_sessions = sum(1 for s, _ in session_slots if s is not None) + 1
        total_items_h += _s(44 * LAYOUT_SCALE) + n_sessions * _s(14 * LAYOUT_SCALE) + ROW_MARGIN_Y + _s(4 * LAYOUT_SCALE)


    total_h = HEADER_H + _s(8 * LAYOUT_SCALE) + total_items_h + FOOTER_H
    season = upcoming[0].season if upcoming else ""


    img = Image.new("RGBA", (CARD_W, total_h), BG_TOP)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, HEADER_H, CARD_W, total_h), fill=BG_BOT)
    draw.rectangle((0, 0, CARD_W, HEADER_H), fill=HEADER_RED)


    logo = _load_f1_logo(_s(128))
    if logo is not None:
        img.paste(logo, (CARD_W - _s(28) - logo.width, (HEADER_H - logo.height) // 2), logo)


    title_font = _font("ExtraBold", 18)
    sub_font = _font("Medium", 11)
    _draw_text(draw, (_s(28), _s(18 * LAYOUT_SCALE)), f"F1 {season}", fill=WHITE, font=title_font)
    _draw_text(draw, (_s(28), _s(46 * LAYOUT_SCALE)), "UPCOMING SCHEDULE", fill=(255, 255, 255, 220), font=sub_font)


    y = HEADER_H + _s(8 * LAYOUT_SCALE)
    for race in upcoming:
        y = await _draw_schedule_item(img, draw, y, race)


    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_next_race(race: F1RaceWeekend) -> str:
    """Render next race weekend timetable as a PNG image. Returns file path."""
    circuit_name = race.circuit_name
    locality = race.locality
    country = race.country


    session_slots: list[tuple[F1SessionSlot | None, str]] = [
        (race.first_practice, "FP1"),
        (race.sprint_qualifying, "Sprint Qualifying"),
        (race.second_practice, "FP2"),
        (race.sprint, "Sprint Race"),
        (race.third_practice, "FP3"),
        (race.qualifying, "Qualifying"),
    ]
    sessions: list[tuple[str, str]] = []
    for slot, label in session_slots:
        t = _session_cst(slot)
        if t:
            sessions.append((label, f"{t} CST"))
    race_time = _utc_to_cst(race.date, race.time)
    sessions.append(("Race", f"{race_time} CST"))


    body_h = _s(112 * LAYOUT_SCALE) + len(sessions) * _s(20 * LAYOUT_SCALE) + FOOTER_H
    total_h = HEADER_H + body_h


    img = Image.new("RGBA", (CARD_W, total_h), BG_TOP)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, HEADER_H, CARD_W, total_h), fill=BG_BOT)
    draw.rectangle((0, 0, CARD_W, HEADER_H), fill=HEADER_RED)


    # F1 logo + circuit overlay
    logo = _load_f1_logo(_s(128))
    if logo is not None:
        img.paste(logo, (CARD_W - _s(28) - logo.width, (HEADER_H - logo.height) // 2), logo)
    circuit_img = _load_circuit_image(race.circuit_id, _s(56))
    if circuit_img is not None:
        alpha = circuit_img.split()[3].point(lambda p: int(p * 0.3))
        circuit_img.putalpha(alpha)
        cx = CARD_W - _s(180) - circuit_img.width
        cy = (HEADER_H - circuit_img.height) // 2
        img.paste(circuit_img, (cx, cy), circuit_img)


    title_font = _font("ExtraBold", 18)
    sub_font = _font("Medium", 11)
    await _draw_flagged_text(
        img, draw, (_s(28), _s(18 * LAYOUT_SCALE)), country, race.race_name, title_font, WHITE, flag_size=_s(22), max_width=CARD_W - _s(190)
    )
    _draw_text(draw, 
        (_s(28), _s(46 * LAYOUT_SCALE)),
        f"ROUND {race.round} · RACE WEEKEND",
        fill=(255, 255, 255, 220),
        font=sub_font,
    )


    y = HEADER_H + _s(16 * LAYOUT_SCALE)


    # Round + sprint badge
    round_font = _font("Medium", 10)
    round_text = f"ROUND {race.round}"
    _draw_text(draw, (_s(24), y), round_text, fill=(255, 255, 255, 130), font=round_font)
    if race.is_sprint_weekend:
        sprint_font = _font("Bold", 8)
        bb = _textbbox(draw, "SPRINT WEEKEND", sprint_font)
        bw = bb[2] - bb[0] + _s(12)
        bh = bb[3] - bb[1] + _s(6)
        rbbox = _textbbox(draw, round_text, round_font)
        badge_x = _s(24) + rbbox[2] - rbbox[0] + _s(8)
        draw.rounded_rectangle(
            (badge_x, y, badge_x + bw, y + bh), radius=_s(3), fill=RED_ACCENT
        )
        _draw_text(draw, (badge_x + _s(6), y + _s(2 * LAYOUT_SCALE)), "SPRINT WEEKEND", fill=WHITE, font=sprint_font)
    y += _s(18 * LAYOUT_SCALE)


    # Race name big
    big_font = _font("ExtraBold", 20)
    await _draw_flagged_text(
        img, draw, (_s(24), y), country, race.race_name, big_font, WHITE, flag_size=_s(24), max_width=CARD_W - _s(48)
    )
    y += _s(28 * LAYOUT_SCALE)


    # Circuit info
    info_font = _font("Regular", 11)
    _draw_text(draw, 
        (_s(24), y),
        f"{circuit_name} · {locality}, {country}",
        fill=(255, 255, 255, 160),
        font=info_font,
        max_width=CARD_W - _s(48),
    )
    y += _s(24 * LAYOUT_SCALE)


    # Section title
    section_font = _font("SemiBold", 11)
    _draw_text(draw, (_s(24), y), "SCHEDULE (CST)", fill=RED_ACCENT, font=section_font)
    y += _s(20 * LAYOUT_SCALE)


    # Sessions
    session_font = _font("Regular", 11)
    race_session_font = _font("SemiBold", 11)
    for label, time_str in sessions:
        is_race = label == "Race"
        f = race_session_font if is_race else session_font
        col = RED_ACCENT if is_race else (255, 255, 255, 180)
        _draw_text(
            draw,
            (_s(36), y),
            f"{label}: {time_str}",
            fill=col,
            font=f,
            max_width=CARD_W - _s(60),
        )
        y += _s(20 * LAYOUT_SCALE)


    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_practice_result(
    session: OpenF1Session,
    results: list[OpenF1SessionResult],
    drivers_by_number: dict[int, OpenF1Driver],
    fp_number: str = "1",
    circuit_id: str = "",
) -> str:
    """Render practice result as a PNG image. Returns file path."""
    circuit = session.circuit_short_name or session.location
    n = len(results)
    img, draw, y = await _create_card(
        n,
        circuit,
        f"FREE PRACTICE {fp_number} · RESULTS",
        circuit_id=circuit_id,
        country=session.country_name,
    )
    rows: list[tuple] = []
    for entry in results:
        drv = drivers_by_number.get(
            entry.driver_number, OpenF1Driver(driver_number=entry.driver_number)
        )
        colour = _parse_openf1_team_colour(drv) or _team_colour(drv.team_name or "")
        lap_time = _format_lap_duration(entry.duration) if entry.duration else "-"
        gap_str = ""
        if isinstance(entry.gap_to_leader, (int, float)) and entry.gap_to_leader > 0:
            gap_str = f"+{entry.gap_to_leader:.3f}s"
        stats = [("BEST LAP", lap_time), ("GAP", gap_str or "LEADER")]
        rows.append((entry.position, drv, colour, stats))
    col_widths = _calc_stat_col_widths([r[3] for r in rows])
    for pos, drv, colour, stats in rows:
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            drv.display_name,
            drv.team_name or "",
            colour,
            acronym=drv.name_acronym or (drv.last_name or "?")[:3],
            stats=stats,
            stat_col_widths=col_widths,
            headshot_url=drv.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_starting_grid(
    drivers_by_number: dict[int, OpenF1Driver],
    grid: list[OpenF1Position],
    circuit_id: str = "",
) -> str:
    """Render starting grid as a PNG image. Returns file path."""
    n = len(grid)
    img, draw, y = await _create_card(n, "STARTING GRID", "RACE DAY", circuit_id=circuit_id)
    for entry in grid:
        pos = entry.position
        drv = drivers_by_number.get(
            entry.driver_number, OpenF1Driver(driver_number=entry.driver_number)
        )
        colour = _parse_openf1_team_colour(drv) or _team_colour(drv.team_name or "")
        y = await _draw_driver_row(
            img,
            draw,
            y,
            pos,
            drv.display_name,
            drv.team_name or "",
            colour,
            acronym=drv.name_acronym or (drv.last_name or "?")[:3],
            headshot_url=drv.headshot_url,
        )
    _draw_footer(draw, img.height)
    return _save_image(img)




async def render_weekend_start(race: F1RaceWeekend) -> str:
    """Render weekend start notification. Returns file path."""
    return await render_next_race(race)







