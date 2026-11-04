"""Generate polished F1 presentation cards with Pillow."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import aiohttp
from PIL import Image, ImageDraw, ImageFont

try:
    import cairosvg
except (ImportError, OSError):
    cairosvg = None

from .models import Table

_LAYOUT_VERSION = "f1-card-v2-ranking-legacy-1"
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_ASSETS_DIR = _PLUGIN_ROOT / "assets"
_FONTS_DIR = _ASSETS_DIR / "fonts"
_CARD_WIDTH = 1200
_MARGIN = 56
_DEFAULT_CARD_WIDTH = 1120
_MIN_CARD_WIDTH = 1000
_MAX_CARD_WIDTH = 1600
_DEFAULT_BODY_FONT_SIZE = 21
_MIN_BODY_FONT_SIZE = 14
_MAX_BODY_FONT_SIZE = 40
_DEFAULT_SUBTITLE_FONT_SIZE = 22
_MIN_SUBTITLE_FONT_SIZE = 12
_MAX_SUBTITLE_FONT_SIZE = 48
_DEFAULT_TITLE_FONT_SIZE = 41
_MIN_TITLE_FONT_SIZE = 18
_MAX_TITLE_FONT_SIZE = 80
_CACHE_PREFIX = "f1-card-"
_TEAM_COLOURS = MappingProxyType(
    {
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
)
_RANKING_TITLES = MappingProxyType(
    {
        "race": "正赛成绩",
        "qualifying": "排位赛成绩",
        "sprint": "冲刺赛成绩",
        "sprint_qualifying": "冲刺排位赛成绩",
        "practice1": "练习赛 FP1",
        "practice2": "练习赛 FP2",
        "practice3": "练习赛 FP3",
        "grid": "发车顺序",
        "standings_drivers": "车手积分榜",
        "standings_teams": "车队积分榜",
    }
)
_CIRCUIT_SVG_MAP = MappingProxyType(
    {
        "albert_park": "melbourne",
        "shanghai": "shanghai",
        "suzuka": "suzuka",
        "bahrain": "bahrain",
        "jeddah": "jeddah",
        "miami": "miami",
        "imola": "imola",
        "monaco": "monaco",
        "catalunya": "catalunya",
        "villeneuve": "montreal",
        "red_bull_ring": "spielberg",
        "silverstone": "silverstone",
        "spa": "spa-francorchamps",
        "hungaroring": "hungaroring",
        "zandvoort": "zandvoort",
        "monza": "monza",
        "baku": "baku",
        "marina_bay": "marina-bay",
        "americas": "austin",
        "rodriguez": "mexico-city",
        "interlagos": "interlagos",
        "vegas": "las-vegas",
        "losail": "lusail",
        "yas_marina": "yas-marina",
        "madring": "madring",
        "portimao": "portimao",
        "istanbul": "istanbul",
        "mugello": "mugello",
        "sochi": "sochi",
        "nurburgring": "nurburgring",
        "hockenheimring": "hockenheimring",
        "sepang": "sepang",
        "yeongam": "yeongam",
        "buddh": "buddh",
    }
)
_CJK_FONTS = {
    False: (
        Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/noto/NotoSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ),
    True: (
        Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/noto/NotoSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ),
}


class ImageRenderer:
    """Render structured tables as cached F1 cards.

    Args:
        data_dir: Directory for generated PNG files.
        max_age_minutes: Maximum cache age in minutes.
        max_count: Maximum number of generated cards to retain.
        card_width: Output card width in pixels.
        body_font_size: Body and auxiliary text size in pixels.
        subtitle_font_size: Subtitle size in pixels.
        title_font_size: Main title size in pixels.
    """

    def __init__(
        self,
        data_dir: Path,
        max_age_minutes: int = 30,
        max_count: int = 50,
        card_width: int = _DEFAULT_CARD_WIDTH,
        body_font_size: int = _DEFAULT_BODY_FONT_SIZE,
        subtitle_font_size: int = _DEFAULT_SUBTITLE_FONT_SIZE,
        title_font_size: int = _DEFAULT_TITLE_FONT_SIZE,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.max_age_minutes = max(0, int(max_age_minutes))
        self.max_count = max(1, int(max_count))
        self.card_width = min(max(int(card_width), _MIN_CARD_WIDTH), _MAX_CARD_WIDTH)
        self.body_font_size = min(
            max(int(body_font_size), _MIN_BODY_FONT_SIZE),
            _MAX_BODY_FONT_SIZE,
        )
        self.subtitle_font_size = min(
            max(int(subtitle_font_size), _MIN_SUBTITLE_FONT_SIZE),
            _MAX_SUBTITLE_FONT_SIZE,
        )
        self.title_font_size = min(
            max(int(title_font_size), _MIN_TITLE_FONT_SIZE),
            _MAX_TITLE_FONT_SIZE,
        )
        self._layout_scale = self.card_width / _CARD_WIDTH
        self._body_font_scale = self.body_font_size / 18
        self._semaphore = asyncio.Semaphore(1)
        self._state_lock = asyncio.Lock()
        self._workers: dict[asyncio.Task[Path], asyncio.Event] = {}
        self._font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self._latin_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self._headshot_cache: dict[str, Image.Image | None] = {}
        self._headshot_dir = self.data_dir.parent / "headshots"
        self._closed = False

    def _s(self, value: float) -> int:
        """Scale a layout dimension to the configured card width."""
        return max(1, round(value * self._layout_scale))

    def _t(self, value: float) -> int:
        """Scale text-related geometry from the configured body font size."""
        return max(1, round(value * self._body_font_scale))

    def _font_size(self, value: int) -> int:
        """Return a configured pixel size for a text element."""
        return self._t(value)

    def _title_font_size(self, value: int) -> int:
        """Return the independently configured title font size."""
        return self.title_font_size

    def _subtitle_font_size(self, value: int) -> int:
        """Return the independently configured subtitle font size."""
        return self.subtitle_font_size

    def _subtitle_width(self, title_width: int) -> int:
        """Reserve space for the circuit graphic at the header's right edge."""
        return max(self._s(220), title_width - self._s(110))

    def _headshot_cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._headshot_dir / f"{digest}.img"

    def _make_circular(self, source: Image.Image, size: int) -> Image.Image:
        resized = source.resize((size, size), Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        result.paste(resized, (0, 0), mask)
        return result

    def _read_persisted_headshot(self, url: str) -> Image.Image | None:
        path = self._headshot_cache_path(url)
        if not path.is_file():
            return None
        try:
            with Image.open(path) as source:
                return source.convert("RGBA").copy()
        except (OSError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _persist_headshot(self, url: str, data: bytes) -> None:
        path = self._headshot_cache_path(url)
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f"{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(data)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            self._prune_headshots()
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune_headshots(self) -> None:
        try:
            entries = sorted(
                self._headshot_dir.glob("*.img"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in entries[100:]:
                path.unlink(missing_ok=True)
        except OSError:
            return

    def _decode_headshot(self, data: bytes) -> Image.Image:
        with Image.open(io.BytesIO(data)) as source:
            return source.convert("RGBA").copy()

    async def _load_headshot(
        self,
        url: str,
        session: aiohttp.ClientSession,
    ) -> Image.Image | None:
        if url in self._headshot_cache:
            return self._headshot_cache[url]
        cached = await asyncio.to_thread(self._read_persisted_headshot, url)
        if cached is not None:
            self._headshot_cache[url] = cached
            return cached
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                declared = response.content_length
                if declared is not None and declared > 1024 * 1024:
                    raise ValueError("headshot exceeds size limit")
                data = await response.read()
                if len(data) > 1024 * 1024:
                    raise ValueError("headshot exceeds size limit")
            image = await asyncio.to_thread(self._decode_headshot, data)
            self._headshot_cache[url] = image
            await asyncio.to_thread(self._persist_headshot, url, data)
            return image
        except (aiohttp.ClientError, OSError, ValueError, asyncio.TimeoutError):
            self._headshot_cache[url] = None
            return None

    async def _prepare_headshots(self, table: Table) -> dict[str, Image.Image]:
        urls = sorted(
            {
                url
                for row in table.row_avatars
                for url in row
                if isinstance(url, str) and url.startswith(("http://", "https://"))
            }
        )
        if not urls:
            return {}
        semaphore = asyncio.Semaphore(4)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "AstrBot-F1-Notifier/2.1"},
        ) as session:

            async def fetch(url: str) -> tuple[str, Image.Image | None]:
                async with semaphore:
                    return url, await self._load_headshot(url, session)

            loaded = await asyncio.gather(*(fetch(url) for url in urls))
        return {url: image for url, image in loaded if image is not None}

    def _cache_is_fresh(self, path: Path) -> bool:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age <= self.max_age_minutes * 60

    @staticmethod
    def _is_rank_table(rows: tuple[tuple[str, ...], ...]) -> bool:
        return bool(rows) and all(row[0].strip().isdigit() for row in rows)

    @staticmethod
    def _is_ranking_table(table: Table) -> bool:
        """Return whether a table requests the legacy ranking-card layout."""
        return table.layout == "ranking" and bool(table.rows)

    @staticmethod
    def _time_segments(value: str) -> tuple[str, str, str] | None:
        """Split ``YYYY-MM-DD HH:MM`` into date, hour, and minute parts."""
        prefix, separator, clock = value.rpartition(" ")
        if not separator or ":" not in clock:
            return None
        hour, colon, minute = clock.partition(":")
        if (
            not hour.isdigit()
            or not minute.isdigit()
            or len(hour) not in (1, 2)
            or len(minute) != 2
        ):
            return None
        return f"{prefix} ", hour, f"{colon}{minute}"

    async def render(self, table: Table) -> Path:
        """Render a table to a PNG card.

        Args:
            table: Structured content to draw.

        Returns:
            Path to the generated or cached PNG file.

        Raises:
            RuntimeError: If the renderer has already been closed.
        """
        await self._semaphore.acquire()
        task: asyncio.Task[Path] | None = None
        try:
            output_path = self._cache_path(table)
            headshots = (
                {}
                if self._cache_is_fresh(output_path)
                else await self._prepare_headshots(table)
            )
            async with self._state_lock:
                if self._closed:
                    raise RuntimeError("ImageRenderer is closed")
                task = asyncio.create_task(
                    asyncio.to_thread(self._render_sync, table, headshots)
                )
                event = asyncio.Event()
                self._workers[task] = event

                def worker_finished(done: asyncio.Task[Path]) -> None:
                    try:
                        done.result()
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._workers.pop(done, None)
                    event.set()
                    self._semaphore.release()

                task.add_done_callback(worker_finished)
        except BaseException:
            if task is None:
                self._semaphore.release()
            raise

        await asyncio.shield(event.wait())
        return task.result()

    async def close(self) -> None:
        """Close the renderer after all in-flight card work finishes."""
        async with self._state_lock:
            self._closed = True
            events = tuple(self._workers.values())
        if events:
            await asyncio.gather(*(event.wait() for event in events))
        for image in self._headshot_cache.values():
            if image is not None:
                image.close()
        self._headshot_cache.clear()

    def _load_font(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        """Load the first available CJK-capable font.

        Args:
            size: Requested font size in pixels.
            bold: Whether to prefer a bold face.

        Returns:
            A Pillow font object. A bundled font or Pillow's default is used
            when no system CJK font is installed.
        """
        key = (size, bold)
        if key in self._font_cache:
            return self._font_cache[key]

        bundled = _FONTS_DIR / ("Orbitron-Bold.ttf" if bold else "Orbitron-Regular.ttf")
        for path in (*_CJK_FONTS[bold], bundled):
            try:
                font = ImageFont.truetype(str(path), size)
            except (OSError, ValueError):
                continue
            self._font_cache[key] = font
            return font

        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def _load_latin_font(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        """Load the bundled racing font for Latin text and timing values.

        Args:
            size: Requested font size in pixels.
            bold: Whether to load the bold face.

        Returns:
            The bundled Orbitron font, falling back to the CJK font loader.
        """
        key = (size, bold)
        if key in self._latin_font_cache:
            return self._latin_font_cache[key]

        filename = "Orbitron-Bold.ttf" if bold else "Orbitron-Regular.ttf"
        try:
            font = ImageFont.truetype(str(_FONTS_DIR / filename), size)
        except (OSError, ValueError):
            return self._load_font(size, bold=bold)
        self._latin_font_cache[key] = font
        return font

    def _ellipsize(
        self,
        text: str,
        font: ImageFont.ImageFont,
        max_width: float,
    ) -> str:
        """Trim text to a measured width with a trailing ellipsis.

        Args:
            text: Text to measure.
            font: Font used for drawing.
            max_width: Available horizontal space.

        Returns:
            Text that fits the requested width.
        """
        if not text:
            return ""
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength
        if measure(text, font=font) <= max_width:
            return text
        suffix = "…"
        while text and measure(text + suffix, font=font) > max_width:
            text = text[:-1]
        return text + suffix

    def _wrap(
        self,
        text: str,
        font: ImageFont.ImageFont,
        max_width: float,
        max_lines: int = 3,
    ) -> list[str]:
        """Wrap text by measured glyph width without exceeding line limits.

        Args:
            text: Text to wrap.
            font: Font used for drawing.
            max_width: Available width for each line.
            max_lines: Maximum number of lines to return.

        Returns:
            Wrapped lines. Overflow is replaced by an ellipsis on the last line.
        """
        text = " ".join(str(text).splitlines()).strip()
        if not text:
            return [""]

        measure = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength
        lines: list[str] = []
        remaining = text
        while remaining and len(lines) < max_lines:
            candidate = ""
            split_at = 0
            for index, character in enumerate(remaining):
                trial = candidate + character
                if candidate and measure(trial, font=font) > max_width:
                    break
                candidate = trial
                if character in (" ", "/", "-", "·"):
                    split_at = index + 1
            if candidate == remaining:
                lines.append(candidate)
                return lines
            if split_at and split_at < len(candidate):
                lines.append(candidate[:split_at].rstrip())
                remaining = remaining[split_at:].lstrip()
            else:
                lines.append(candidate.rstrip())
                remaining = remaining[len(candidate) :].lstrip()

        if remaining:
            lines[-1] = self._ellipsize(f"{lines[-1]}…{remaining}", font, max_width)
        return lines

    def _circuit_svg_path(
        self, circuit_id: str, circuits_dir: Path | None = None
    ) -> Path | None:
        """Resolve an allowlisted circuit ID to its newest layout SVG.

        Args:
            circuit_id: External circuit identifier from the source API.
            circuits_dir: Optional circuit directory override for testing.

        Returns:
            The highest-numbered matching SVG, or ``None`` when the identifier
            is unknown or has no bundled layout.
        """
        stem = _CIRCUIT_SVG_MAP.get(str(circuit_id).strip().lower())
        if stem is None:
            return None
        directory = circuits_dir or (_ASSETS_DIR / "circuits")
        variants: list[tuple[int, Path]] = []
        for path in directory.glob(f"{stem}-*.svg"):
            suffix = path.stem.removeprefix(f"{stem}-")
            if suffix.isdigit():
                variants.append((int(suffix), path))
        return max(variants, default=(0, None))[1]

    def _circuit_graphic(
        self, circuit_id: str, size: tuple[int, int]
    ) -> Image.Image | None:
        """Render a bundled circuit SVG when CairoSVG is available.

        Args:
            circuit_id: Circuit identifier from the table.
            size: Requested image size.

        Returns:
            An RGBA circuit image, or ``None`` when no genuine asset can be
            rendered.
        """
        width, height = size
        if circuit_id and cairosvg is not None:
            svg_path = self._circuit_svg_path(circuit_id)
            if svg_path is not None:
                output = io.BytesIO()
                try:
                    cairosvg.svg2png(
                        url=str(svg_path),
                        write_to=output,
                        output_width=width,
                        output_height=height,
                    )
                    output.seek(0)
                    with Image.open(output) as source:
                        return source.convert("RGBA")
                except Exception:
                    # CairoSVG can surface backend-specific parse exceptions.
                    pass
        return None

    def _cache_path(self, table: Table) -> Path:
        """Compute a deterministic cache path for a table.

        Args:
            table: Structured table content.

        Returns:
            Stable PNG path derived from every rendered field.
        """
        parts = [
            _LAYOUT_VERSION,
            table.title,
            table.subtitle,
            "\x1e".join(table.columns),
            "\x1f".join("\x1e".join(row) for row in table.rows),
            table.footer,
            table.circuit_id,
            "\x1c".join(
                "\x1f".join(url or "" for url in row) for row in table.row_avatars
            ),
            table.layout,
            table.rank_kind,
            str(self.card_width),
            str(self.body_font_size),
            str(self.subtitle_font_size),
            str(self.title_font_size),
        ]
        digest = hashlib.sha256("\x1d".join(parts).encode("utf-8")).hexdigest()[:24]
        return self.data_dir / f"{_CACHE_PREFIX}{digest}.png"

    def _prune_cache(self, keep: Path) -> None:
        """Remove stale and excess generated cards.

        Args:
            keep: The card produced by the current render. It is never removed
                by this cleanup pass.
        """
        try:
            entries = sorted(
                self.data_dir.glob(f"{_CACHE_PREFIX}*.png"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return

        cutoff = time.time() - self.max_age_minutes * 60
        retained_others = 0
        for path in entries:
            if path == keep:
                continue
            try:
                too_old = path.stat().st_mtime < cutoff
                too_many = retained_others >= self.max_count - 1
                if too_old or too_many:
                    path.unlink(missing_ok=True)
                else:
                    retained_others += 1
            except OSError:
                continue

    def _render_sync(
        self,
        table: Table,
        headshots: Mapping[str, Image.Image] | None = None,
    ) -> Path:
        """Render one table in a worker thread.

        Args:
            table: Structured content to draw.

        Returns:
            Path to the generated or cached PNG file.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._cache_path(table)
        cache_fresh = False
        try:
            age = time.time() - output_path.stat().st_mtime
            cache_fresh = age <= self.max_age_minutes * 60
        except OSError:
            pass

        if not cache_fresh:
            image = self._draw_card(table, headshots or {})
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.data_dir,
                    prefix=".f1-card-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                image.save(temporary_path, format="PNG", optimize=True)
                os.replace(temporary_path, output_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

        self._prune_cache(output_path)
        return output_path

    def _ranking_scale(self) -> float:
        """Return the scale from the original 1120px ranking artwork."""
        return self.card_width / 1120

    def _r(self, value: float) -> int:
        """Scale one legacy ranking-card dimension."""
        return max(1, round(value * self._ranking_scale()))

    @staticmethod
    def _team_colour(constructor_name: str) -> tuple[int, int, int]:
        """Resolve a constructor name to the 1.x team accent colour."""
        name = constructor_name.casefold()
        for key, colour in _TEAM_COLOURS.items():
            if key in name:
                return colour
        return (255, 255, 255)

    @staticmethod
    def _driver_acronym(name: str) -> str:
        """Derive the three-letter fallback used when no portrait is available."""
        words = str(name).split()
        source = words[-1] if words else str(name)
        return source[:3].upper() or "F1"

    def _ranking_stat_specs(self, table: Table) -> list[tuple[str, int]]:
        """Map the new table's Chinese columns to the 1.x English stat labels."""
        indexes = {name: index for index, name in enumerate(table.columns)}
        preferred = (
            ("胜场", "WINS"),
            ("积分", "POINTS"),
        ) if table.rank_kind.startswith("standings") else (
            ("用时 / 状态", "TIME"),
            ("圈数", "LAPS"),
            ("积分", "PTS"),
            ("圈速 / 状态", "BEST LAP"),
            ("差距", "GAP"),
            ("Q1", "Q1"),
            ("Q2", "Q2"),
            ("Q3", "Q3"),
        )
        return [
            (label, indexes[column])
            for column, label in preferred
            if column in indexes
        ]

    def _draw_time_value(
        self,
        draw: ImageDraw.ImageDraw,
        position: tuple[float, float],
        value: str,
        font: ImageFont.ImageFont,
        hour_font: ImageFont.ImageFont,
        max_width: float,
        base_fill: str,
        hour_fill: str = "#FF3B30",
    ) -> None:
        """Draw a timestamp with its hour and minute visually emphasized."""
        parts = self._time_segments(value)
        if parts is None:
            draw.text(
                position,
                self._ellipsize(value, font, max_width),
                font=font,
                fill=base_fill,
            )
            return

        prefix, hour, suffix = parts
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength
        total_width = (
            measure(prefix, font=font)
            + measure(hour, font=hour_font)
            + measure(suffix, font=font)
        )
        if total_width > max_width:
            draw.text(
                position,
                self._ellipsize(value, font, max_width),
                font=font,
                fill=base_fill,
            )
            return

        x, y = position
        draw.text((x, y), prefix, font=font, fill=base_fill)
        x += measure(prefix, font=font)
        draw.text((x, y), hour, font=hour_font, fill=hour_fill)
        x += measure(hour, font=hour_font)
        draw.text((x, y), suffix, font=font, fill=hour_fill)

    def _f1_logo(self, size: tuple[int, int]) -> Image.Image | None:
        """Render the bundled F1 logo at the requested size."""
        if cairosvg is None:
            return None
        output = io.BytesIO()
        try:
            cairosvg.svg2png(
                url=str(_ASSETS_DIR / "f1.svg"),
                write_to=output,
                output_width=size[0],
                output_height=size[1],
            )
            output.seek(0)
            with Image.open(output) as source:
                return source.convert("RGBA")
        except Exception:
            return None

    def _draw_ranking_card(
        self,
        table: Table,
        headshots: Mapping[str, Image.Image],
    ) -> Image.Image:
        """Draw the legacy 1.x ranked-driver and standings card style."""
        width = self.card_width
        column_count = max(
            len(table.columns),
            max((len(row) for row in table.rows), default=0),
            1,
        )
        columns = tuple(table.columns) + ("",) * (column_count - len(table.columns))
        rows = tuple(
            tuple(row) + ("",) * (column_count - len(row)) for row in table.rows
        )
        avatar_rows = [
            list(row) + [None] * (column_count - len(row)) for row in table.row_avatars
        ]
        while len(avatar_rows) < len(rows):
            avatar_rows.append([None] * column_count)

        header_height = self._r(184)
        row_height = self._r(132)
        row_gap = self._r(10)
        footer_height = self._r(84)
        margin_x = self._r(20)
        card_height = (
            header_height
            + self._r(16)
            + len(rows) * (row_height + row_gap)
            + footer_height
        )
        image = Image.new("RGBA", (width, card_height), (21, 21, 30, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, header_height, width, card_height), fill=(26, 26, 46, 255))
        draw.rectangle((0, 0, width, header_height), fill=(225, 6, 0, 255))

        circuit = self._circuit_graphic(
            table.circuit_id, (self._r(112), self._r(112))
        )
        if circuit is not None:
            alpha = circuit.getchannel("A").point(lambda value: int(value * 0.3))
            circuit.putalpha(alpha)
            image.paste(
                circuit,
                (width - self._r(470), (header_height - circuit.height) // 2),
                circuit,
            )
        logo = self._f1_logo((self._r(180), self._r(112)))
        if logo is not None:
            image.paste(
                logo,
                (
                    width - self._r(156) - logo.width,
                    (header_height - logo.height) // 2,
                ),
                logo,
            )

        subtitle_font = self._load_font(self._r(28))
        title = _RANKING_TITLES.get(table.rank_kind, table.title.upper())
        title_font = (
            self._load_latin_font(self._r(45), bold=True)
            if title.isascii()
            else self._load_font(self._r(45), bold=True)
        )
        title = self._ellipsize(title, title_font, width - self._r(400))
        draw.text((self._r(56), self._r(34)), title, font=title_font, fill="white")
        if table.subtitle:
            subtitle = self._ellipsize(
                table.subtitle.upper(),
                subtitle_font,
                width - self._r(400),
            )
            draw.text(
                (self._r(56), self._r(96)),
                subtitle,
                font=subtitle_font,
                fill=(255, 255, 255, 220),
            )

        position_font = self._load_latin_font(self._r(44), bold=True)
        latin_name_font = self._load_latin_font(self._r(32), bold=True)
        team_font = self._load_font(self._r(25))
        latin_team_font = self._load_latin_font(self._r(25))
        acronym_font = self._load_latin_font(self._r(27), bold=True)
        stat_label_font = self._load_latin_font(self._r(20), bold=True)
        stat_value_font = self._load_latin_font(self._r(30), bold=True)
        stat_value_cjk_font = self._load_font(self._r(30), bold=True)
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength

        driver_column = columns.index("车手") if "车手" in columns else None
        team_column = columns.index("车队") if "车队" in columns else None
        is_team_table = table.rank_kind == "standings_teams"
        stat_specs = self._ranking_stat_specs(table)
        stat_widths = [
            min(
                max(
                    measure(label, font=stat_label_font),
                    max(
                        measure(
                            row[column],
                            font=(
                                stat_value_font
                                if row[column].isascii()
                                else stat_value_cjk_font
                            ),
                        )
                        for row in rows
                    ),
                )
                + self._r(18),
                self._r(250 if label in ("TIME", "BEST LAP") else 180),
            )
            for label, column in stat_specs
        ]
        stats_right = width - margin_x - self._r(32)
        stats_left = stats_right - sum(stat_widths) - self._r(16) * max(
            0, len(stat_specs) - 1
        )

        y = header_height + self._r(16)
        for row_index, row in enumerate(rows):
            try:
                position = int(row[0])
            except ValueError:
                position = 99
            if position == 1:
                row_fill = (255, 215, 0, 25)
                position_fill = (255, 215, 0)
            elif position == 2:
                row_fill = (192, 192, 192, 20)
                position_fill = (192, 192, 192)
            elif position == 3:
                row_fill = (205, 127, 50, 20)
                position_fill = (205, 127, 50)
            else:
                row_fill = (255, 255, 255, 15 if position % 2 == 0 else 10)
                position_fill = (255, 255, 255)

            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            ImageDraw.Draw(overlay).rounded_rectangle(
                (margin_x, y, width - margin_x, y + row_height),
                radius=self._r(20),
                fill=row_fill,
            )
            image.alpha_composite(overlay)

            if is_team_table and team_column is not None:
                display_name = row[team_column]
                team_name = ""
            elif driver_column is not None:
                display_name = row[driver_column]
                team_name = row[team_column] if team_column is not None else ""
            else:
                display_name = row[1] if column_count > 1 else row[0]
                team_name = row[2] if column_count > 2 else ""
            team_colour = self._team_colour(team_name or display_name)
            draw.rectangle(
                (
                    margin_x,
                    y + self._r(12),
                    margin_x + self._r(8),
                    y + row_height - self._r(12),
                ),
                fill=team_colour,
            )
            draw.text(
                (margin_x + self._r(32), y + self._r(40)),
                str(position) if position != 99 else row[0],
                font=position_font,
                fill=position_fill,
            )

            if is_team_table:
                name_x = margin_x + self._r(112)
            else:
                avatar_url = (
                    avatar_rows[row_index][driver_column]
                    if driver_column is not None
                    else None
                )
                avatar_size = self._r(64)
                circle_x = margin_x + self._r(120)
                circle_y = y + (row_height - avatar_size) // 2
                draw.ellipse(
                    (
                        circle_x,
                        circle_y,
                        circle_x + avatar_size,
                        circle_y + avatar_size,
                    ),
                    outline=team_colour,
                    width=self._r(4),
                )
                raw_headshot = headshots.get(avatar_url) if avatar_url else None
                if raw_headshot is not None:
                    headshot = self._make_circular(
                        raw_headshot, avatar_size - self._r(8)
                    )
                    image.paste(
                        headshot,
                        (circle_x + self._r(4), circle_y + self._r(4)),
                        headshot,
                    )
                    headshot.close()
                else:
                    acronym = self._driver_acronym(display_name)
                    acronym_width = measure(acronym, font=acronym_font)
                    draw.text(
                        (
                            circle_x + (avatar_size - acronym_width) / 2,
                            y + (row_height - self._r(27)) / 2,
                        ),
                        acronym,
                        font=acronym_font,
                        fill="white",
                    )
                name_x = circle_x + avatar_size + self._r(24)

            name_max_width = stats_left - name_x - self._r(16)
            draw.text(
                (name_x, y + self._r(16)),
                self._ellipsize(display_name, latin_name_font, name_max_width),
                font=latin_name_font,
                fill="white",
            )
            if team_name:
                team_font_for_row = latin_team_font if team_name.isascii() else team_font
                draw.text(
                    (name_x, y + self._r(56)),
                    self._ellipsize(team_name, team_font_for_row, name_max_width),
                    font=team_font_for_row,
                    fill=(150, 150, 155),
                )

            stat_x = stats_right
            for stat_index in range(len(stat_specs) - 1, -1, -1):
                label, column = stat_specs[stat_index]
                column_width = stat_widths[stat_index]
                value = row[column]
                value_font = (
                    stat_value_font if value.isascii() else stat_value_cjk_font
                )
                draw.text(
                    (stat_x - column_width, y + self._r(16)),
                    label,
                    font=stat_label_font,
                    fill=(150, 150, 155),
                )
                value_fill = (
                    (225, 6, 0)
                    if label in ("PTS", "POINTS") and position <= 3
                    else (255, 255, 255)
                )
                draw.text(
                    (stat_x - column_width, y + self._r(48)),
                    self._ellipsize(value, value_font, column_width),
                    font=value_font,
                    fill=value_fill,
                )
                stat_x -= column_width + self._r(16)
            y += row_height + row_gap

        footer_font = self._load_font(self._r(20))
        footer = self._ellipsize(
            table.footer or "F1 NOTIFIER · POWERED BY ASTRBOT",
            footer_font,
            width - self._r(48),
        )
        footer_width = measure(footer, font=footer_font)
        draw.text(
            ((width - footer_width) / 2, card_height - footer_height + self._r(22)),
            footer,
            font=footer_font,
            fill=(255, 255, 255, 80),
        )
        return image.convert("RGB")

    def _draw_card(
        self,
        table: Table,
        headshots: Mapping[str, Image.Image],
    ) -> Image.Image:
        """Draw the complete card image.

        Args:
            table: Structured content to draw.

        Returns:
            A RGB card image using the configured width and font sizes.
        """
        if self._is_ranking_table(table):
            return self._draw_ranking_card(table, headshots)

        width = self.card_width
        margin = self._s(_MARGIN)
        table_width = width - margin * 2
        regular = self._load_font(self._font_size(18))
        bold = self._load_font(self._font_size(19), bold=True)
        latin_regular = self._load_latin_font(self._font_size(17))
        latin_bold = self._load_latin_font(self._font_size(18), bold=True)
        header_font = self._load_font(self._font_size(16), bold=True)
        title_font = self._load_font(self._title_font_size(44), bold=True)
        subtitle_font = self._load_font(self._subtitle_font_size(19))
        footer_font = self._load_font(self._font_size(16))
        micro_font = self._load_latin_font(self._font_size(12), bold=True)
        title_y = self._t(34)
        subtitle_y = title_y + self.title_font_size + self._t(20)
        micro_y = subtitle_y + self.subtitle_font_size + self._t(14)

        column_count = max(
            len(table.columns),
            max((len(row) for row in table.rows), default=0),
            1,
        )
        columns = tuple(table.columns) + ("",) * (column_count - len(table.columns))
        normalized_rows = tuple(
            tuple(row) + ("",) * (column_count - len(row)) for row in table.rows
        )
        avatar_rows = [
            list(row) + [None] * (column_count - len(row)) for row in table.row_avatars
        ]
        while len(avatar_rows) < len(normalized_rows):
            avatar_rows.append([None] * column_count)
        rank_table = self._is_rank_table(normalized_rows)
        driver_column = columns.index("车手") if "车手" in columns else None
        avatar_size = self._t(34)
        avatar_gap = self._s(8)
        avatar_extra = avatar_size + avatar_gap
        driver_has_avatars = driver_column is not None and any(
            row[driver_column] for row in avatar_rows
        )

        measure = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength
        usable_width = table_width
        natural_widths = []
        for index, heading in enumerate(columns):
            cells = [heading, *(row[index] for row in normalized_rows)]
            widest = max(
                (
                    measure(
                        cell,
                        font=latin_regular if cell.isascii() else regular,
                    )
                    for cell in cells
                ),
                default=0,
            )
            if index == driver_column and driver_has_avatars:
                widest += avatar_extra
            natural_widths.append(
                min(
                    max(float(self._s(82)), widest + self._s(28)),
                    float(self._s(380)),
                )
            )
        width_total = sum(natural_widths)
        widths = [
            max(self._s(82), int(usable_width * width_value / width_total))
            for width_value in natural_widths
        ]
        widths[-1] += usable_width - sum(widths)

        wrapped_rows: list[list[list[str]]] = []
        wrapped_fonts: list[list[ImageFont.ImageFont]] = []
        row_heights: list[int] = []
        line_height = self._t(24)
        for row_index, row in enumerate(normalized_rows):
            wrapped_row: list[list[str]] = []
            row_fonts: list[ImageFont.ImageFont] = []
            for index in range(column_count):
                cell_font = latin_regular if row[index].isascii() else regular
                row_fonts.append(cell_font)
                available_width = widths[index] - self._s(20)
                if (
                    index == driver_column
                    and avatar_rows[row_index][index] in headshots
                ):
                    available_width -= avatar_extra
                wrapped_row.append(self._wrap(row[index], cell_font, available_width))
            wrapped_rows.append(wrapped_row)
            wrapped_fonts.append(row_fonts)
            row_heights.append(
                max(
                    self._t(60),
                    self._t(20)
                    + max(len(lines) for lines in wrapped_row) * line_height,
                )
            )

        header_height = max(
            self._t(180),
            micro_y + self._font_size(12) + self._t(24),
        )
        column_header_height = self._t(52)
        footer_height = self._t(82)
        table_height = sum(row_heights)
        card_height = (
            header_height + column_header_height + table_height + footer_height
        )

        red = "#E10600"
        white = "#F7F7F4"
        ink = "#080808"
        muted = "#A6A6A6"
        rule = "#2A2A2A"
        image = Image.new("RGB", (width, card_height), ink)
        draw = ImageDraw.Draw(image)

        # White broadcast header with an angular F1-red identity block.
        draw.rectangle((0, 0, width, header_height), fill=white)
        draw.rectangle((0, 0, width, self._s(6)), fill=red)
        draw.polygon(
            (
                (0, 0),
                (self._s(218), 0),
                (self._s(188), header_height),
                (0, header_height),
            ),
            fill=red,
        )
        draw.polygon(
            (
                (width - self._s(300), 0),
                (width, 0),
                (width, header_height),
                (width - self._s(330), header_height),
            ),
            fill=ink,
        )
        draw.rectangle((0, header_height, width, header_height + self._s(5)), fill=ink)

        logo = None
        if cairosvg is not None:
            output = io.BytesIO()
            try:
                cairosvg.svg2png(
                    url=str(_ASSETS_DIR / "f1.svg"),
                    write_to=output,
                    output_width=self._s(112),
                    output_height=self._s(80),
                )
                output.seek(0)
                with Image.open(output) as source:
                    logo = source.convert("RGBA")
            except Exception:
                logo = None
        if logo is not None:
            image.paste(logo, (self._s(42), self._s(42)), logo)
        else:
            draw.text(
                (self._s(52), self._s(49)),
                "F1",
                font=self._load_latin_font(self._font_size(50), bold=True),
                fill=white,
            )

        title_x = margin + self._s(196)
        title_width = max(self._s(220), width - title_x - self._s(190))
        subtitle_width = self._subtitle_width(title_width)
        title = self._ellipsize(
            (table.title or "F1 信息").upper(),
            title_font,
            title_width,
        )
        draw.text((title_x, title_y), title, font=title_font, fill=ink)
        subtitle = self._ellipsize(
            table.subtitle.upper(), subtitle_font, subtitle_width
        )
        if subtitle:
            draw.text(
                (title_x, subtitle_y),
                subtitle,
                font=subtitle_font,
                fill="#4B4B4B",
            )
        draw.text(
            (title_x, micro_y),
            "F1 // TIMING GRAPHIC",
            font=micro_font,
            fill=red,
        )

        graphic = self._circuit_graphic(table.circuit_id, (self._s(132), self._s(132)))
        graphic_x = width - self._s(158)
        if graphic is not None:
            image.paste(graphic, (graphic_x, self._s(20)), graphic)
        else:
            draw.line(
                (
                    graphic_x + self._s(8),
                    self._s(52),
                    graphic_x + self._s(116),
                    self._s(52),
                ),
                fill="#434343",
                width=self._s(18),
            )
            draw.line(
                (
                    graphic_x + self._s(116),
                    self._s(52),
                    graphic_x + self._s(116),
                    self._s(126),
                ),
                fill="#434343",
                width=self._s(18),
            )
            draw.line(
                (
                    graphic_x + self._s(116),
                    self._s(126),
                    graphic_x + self._s(18),
                    self._s(126),
                ),
                fill="#434343",
                width=self._s(18),
            )
        draw.text(
            (width - self._s(284), self._s(143)),
            "CIRCUIT",
            font=micro_font,
            fill=muted,
        )

        header_top = header_height + self._s(5)
        draw.rectangle(
            (0, header_top, width, header_top + column_header_height),
            fill=red,
        )
        x = margin
        for index, heading in enumerate(columns):
            label = self._ellipsize(
                heading.upper(), header_font, widths[index] - self._s(20)
            )
            draw.text(
                (x + self._s(12), header_top + self._s(15)),
                label,
                font=header_font,
                fill=white,
            )
            x += widths[index]

        session_labels = {"正赛", "RACE", "R"}

        y = header_top + column_header_height
        for row_index, (wrapped_row, row_fonts, row_height) in enumerate(
            zip(wrapped_rows, wrapped_fonts, row_heights, strict=True)
        ):
            cells = normalized_rows[row_index]
            is_race = any(cell.strip().upper() in session_labels for cell in cells)
            is_podium = rank_table and cells[0].strip().isdigit() and int(cells[0]) <= 3
            accent = red if is_race or is_podium else "#202020"
            background = "#080808" if row_index % 2 == 0 else "#101010"
            draw.rectangle((0, y, width, y + row_height), fill=background)
            draw.rectangle((0, y, self._s(6), y + row_height), fill=accent)
            x = margin
            for column_index, lines in enumerate(wrapped_row):
                text_color = white
                if cells[column_index].strip().upper() in session_labels:
                    text_color = red
                elif column_index == 0 and is_podium:
                    text_color = red
                elif column_index == column_count - 1:
                    text_color = "#CFCFCF"

                cell_font = row_fonts[column_index]
                cell_bold = latin_bold if cells[column_index].isascii() else bold
                draw_font = cell_bold if column_index == 0 or is_race else cell_font
                align = "left"
                if rank_table and column_index == 0:
                    align = "center"
                elif rank_table and column_index == column_count - 1:
                    align = "right"

                headshot = None
                if column_index == driver_column:
                    avatar_url = avatar_rows[row_index][column_index]
                    raw_headshot = headshots.get(avatar_url) if avatar_url else None
                    if raw_headshot is not None:
                        headshot = self._make_circular(raw_headshot, avatar_size)
                        headshot_x = x + self._s(12)
                        headshot_y = y + (row_height - avatar_size) // 2
                        draw.ellipse(
                            (
                                headshot_x - self._s(1),
                                headshot_y - self._s(1),
                                headshot_x + avatar_size + self._s(1),
                                headshot_y + avatar_size + self._s(1),
                            ),
                            outline="#565656",
                            width=self._s(1),
                        )
                        image.paste(headshot, (headshot_x, headshot_y), headshot)
                        headshot.close()

                line_x_offset = self._s(12)
                if headshot is not None:
                    line_x_offset += avatar_size + avatar_gap
                for line_index, line in enumerate(lines):
                    line_x = x + line_x_offset
                    line_width = measure(line, font=draw_font)
                    if align == "center":
                        line_x = x + (widths[column_index] - line_width) / 2
                    elif align == "right":
                        line_x = x + widths[column_index] - line_width - self._s(18)
                    line_y = y + self._t(16) + line_index * line_height
                    if (
                        align == "left"
                        and "时间" in columns[column_index]
                        and len(lines) == 1
                    ):
                        self._draw_time_value(
                            draw,
                            (line_x, line_y),
                            line,
                            draw_font,
                            latin_bold,
                            widths[column_index] - line_x_offset - self._s(18),
                            text_color,
                        )
                    else:
                        draw.text(
                            (line_x, line_y),
                            line,
                            font=draw_font,
                            fill=text_color,
                        )
                x += widths[column_index]
            draw.line(
                (
                    margin,
                    y + row_height - 1,
                    width - margin,
                    y + row_height - 1,
                ),
                fill=rule,
                width=1,
            )
            y += row_height

        footer_top = card_height - footer_height
        draw.rectangle((0, footer_top, width, card_height), fill=ink)
        draw.rectangle((0, footer_top, width, footer_top + self._s(3)), fill=white)
        draw.rectangle(
            (
                margin,
                footer_top + self._t(28),
                margin + self._s(10),
                footer_top + self._t(38),
            ),
            fill=red,
        )
        footer = self._ellipsize(
            table.footer or "F1 TIMING DATA",
            footer_font,
            max(self._s(240), table_width - self._s(260)),
        )
        draw.text(
            (margin + self._s(22), footer_top + self._t(23)),
            footer,
            font=footer_font,
            fill="#D6D6D6",
        )
        draw.text(
            (width - margin - self._s(132), footer_top + self._t(27)),
            "F1 // DATA",
            font=micro_font,
            fill=muted,
        )
        return image
