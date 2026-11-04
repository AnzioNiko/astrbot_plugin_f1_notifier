from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from src.astrbot_plugin_f1_notifier.formatter import (
    format_table,
    schedule_table,
    weekend_table,
)
from src.astrbot_plugin_f1_notifier.image_renderer import ImageRenderer
from src.astrbot_plugin_f1_notifier.models import Race, Session, Table


def _sprint_race() -> Race:
    """Return a sprint weekend with deliberately unordered sessions."""
    utc = timezone.utc
    return Race(
        season=2025,
        round=3,
        name="中国大奖赛",
        circuit_id="shanghai",
        circuit_name="上海国际赛车场",
        country="中国",
        sessions=(
            Session("race", datetime(2025, 3, 16, 4, 0, tzinfo=utc)),
            Session("sprint", datetime(2025, 3, 15, 4, 0, tzinfo=utc)),
            Session("practice1", datetime(2025, 3, 14, 1, 30, tzinfo=utc)),
            Session("qualifying", datetime(2025, 3, 15, 7, 0, tzinfo=utc)),
            Session("practice2", datetime(2025, 3, 14, 5, 30, tzinfo=utc)),
            Session("sprint_qualifying", datetime(2025, 3, 14, 7, 30, tzinfo=utc)),
            Session("practice3", datetime(2025, 3, 15, 2, 30, tzinfo=utc)),
        ),
    )


def test_format_table_preserves_card_content_in_reading_order() -> None:
    table = Table(
        title="F1 车手积分榜",
        subtitle="2025 赛季",
        columns=("名次", "车手", "积分"),
        rows=(("1", "奥斯卡·皮亚斯特里", "228"), ("2", "兰多·诺里斯", "201")),
        footer="更新时间：2025-07-01 12:00",
    )

    rendered = format_table(table)

    expected_parts = [
        "F1 车手积分榜",
        "2025 赛季",
        "名次  |  车手  |  积分",
        "1  |  奥斯卡·皮亚斯特里  |  228",
        "2  |  兰多·诺里斯  |  201",
        "更新时间：2025-07-01 12:00",
    ]
    positions = [rendered.index(part) for part in expected_parts]
    assert positions == sorted(positions)


def test_schedule_table_converts_every_session_to_requested_timezone() -> None:
    table = schedule_table([_sprint_race()], timezone="Asia/Shanghai")

    assert table.subtitle == "时区：Asia/Shanghai"
    assert table.columns == ("开始时间", "分站", "赛段", "UTC 偏移")
    assert len(table.rows) == 7
    assert [row[2] for row in table.rows] == [
        "练习赛 FP1",
        "练习赛 FP2",
        "冲刺排位赛",
        "练习赛 FP3",
        "冲刺赛",
        "排位赛",
        "正赛",
    ]
    assert [row[0] for row in table.rows] == [
        "2025-03-14 09:30",
        "2025-03-14 13:30",
        "2025-03-14 15:30",
        "2025-03-15 10:30",
        "2025-03-15 12:00",
        "2025-03-15 15:00",
        "2025-03-16 12:00",
    ]
    assert {row[3] for row in table.rows} == {"UTC+08:00"}


def test_weekend_table_labels_dst_offset_and_preserves_circuit() -> None:
    utc = timezone.utc
    race = Race(
        season=2025,
        round=2,
        name="迈阿密大奖赛",
        circuit_id="miami",
        circuit_name="迈阿密国际赛车场",
        country="美国",
        sessions=(
            Session(
                "race",
                datetime(2025, 5, 4, 20, 0, tzinfo=utc),
                datetime(2025, 5, 4, 22, 0, tzinfo=utc),
            ),
        ),
    )

    table = weekend_table(race, timezone="America/New_York")

    assert table.title == "F1 第2站 · 迈阿密大奖赛"
    assert "America/New_York" in table.subtitle
    assert table.circuit_id == "miami"
    assert table.rows == (
        (
            "2025-05-04 16:00",
            "正赛",
            "2025-05-04 18:00",
            "UTC-04:00",
        ),
    )


def test_renderer_uses_personal_defaults(tmp_path: Path) -> None:
    renderer = ImageRenderer(tmp_path)
    assert renderer.card_width == 1120
    assert renderer.body_font_size == 21
    assert renderer.subtitle_font_size == 22
    assert renderer.title_font_size == 41


def test_title_font_size_only_changes_title_font_size(tmp_path: Path) -> None:
    small = ImageRenderer(tmp_path, title_font_size=24)
    default = ImageRenderer(tmp_path, title_font_size=41)
    large = ImageRenderer(tmp_path, title_font_size=72)

    assert small._title_font_size(44) < default._title_font_size(44)
    assert default._title_font_size(44) < large._title_font_size(44)
    assert small._font_size(18) == default._font_size(18) == large._font_size(18)
    table = Table(title="标题缓存")
    assert (
        len(
            {
                small._cache_path(table),
                default._cache_path(table),
                large._cache_path(table),
            }
        )
        == 3
    )


def test_subtitle_font_size_is_independent(tmp_path: Path) -> None:
    small = ImageRenderer(tmp_path, subtitle_font_size=14)
    default = ImageRenderer(tmp_path, subtitle_font_size=22)
    large = ImageRenderer(tmp_path, subtitle_font_size=44)

    assert small._subtitle_font_size(19) < default._subtitle_font_size(19)
    assert default._subtitle_font_size(19) < large._subtitle_font_size(19)
    assert small._font_size(18) == default._font_size(18) == large._font_size(18)
    assert small._title_font_size(44) == default._title_font_size(44)
    table = Table(title="副标题缓存")
    assert (
        len(
            {
                small._cache_path(table),
                default._cache_path(table),
                large._cache_path(table),
            }
        )
        == 3
    )


def test_subtitle_width_reserves_room_for_circuit_graphic(tmp_path: Path) -> None:
    renderer = ImageRenderer(tmp_path)
    title_width = 800
    subtitle_width = renderer._subtitle_width(title_width)

    assert subtitle_width >= renderer._s(220)
    assert subtitle_width <= title_width - 100


def test_rank_table_detection_is_available_for_podium_styles() -> None:
    assert ImageRenderer._is_rank_table((("1", "车手 A"), ("2", "车手 B")))
    assert not ImageRenderer._is_rank_table((("2026-11-06", "练习赛 FP1"),))


def test_driver_headshot_is_drawn_before_ranked_driver_name(
    tmp_path: Path,
) -> None:
    renderer = ImageRenderer(tmp_path)
    headshot = Image.new("RGBA", (40, 40), "red")
    url = "https://example.invalid/driver.png"
    table = Table(
        "正赛结果",
        "2026 第 19 站",
        ("名次", "车手", "车队", "积分"),
        (("1", "Max Verstappen", "Red Bull", "25"),),
        row_avatars=((None, url, None, None),),
    )

    without = renderer._draw_card(table, {})
    with_headshot = renderer._draw_card(table, {url: headshot})

    assert with_headshot.size == without.size
    assert with_headshot.tobytes() != without.tobytes()


async def test_renderer_honors_custom_width_and_caches_png(tmp_path: Path) -> None:
    data_dir = tmp_path / "cards"
    renderer = ImageRenderer(
        data_dir,
        max_age_minutes=30,
        max_count=10,
        card_width=1320,
        body_font_size=21,
        title_font_size=41,
    )
    table = Table(
        title="F1 冲刺周末时间表",
        subtitle="中国大奖赛 · Asia/Shanghai",
        columns=("开始时间", "赛段", "UTC 偏移"),
        rows=(
            ("03-14 09:30", "练习赛 FP1", "UTC+08:00"),
            ("03-14 13:30", "练习赛 FP2", "UTC+08:00"),
            ("03-14 15:30", "冲刺排位赛", "UTC+08:00"),
            ("03-15 12:00", "冲刺赛", "UTC+08:00"),
            ("03-16 12:00", "正赛", "UTC+08:00"),
        ),
        footer="赛程可能调整，请以赛事官方公告为准",
        circuit_id="shanghai",
    )

    try:
        first = await renderer.render(table)
        second = await renderer.render(table)
    finally:
        await renderer.close()

    assert first == second
    assert first.parent == data_dir
    assert first.suffix == ".png"
    with Image.open(first) as image:
        assert image.format == "PNG"
        assert image.size[0] == 1320
        assert 500 <= image.size[1] <= 900


async def test_cancelled_render_keeps_worker_until_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    renderer = ImageRenderer(tmp_path, max_age_minutes=30, max_count=10)
    original_render_sync = renderer._render_sync

    def blocked_render_sync(
        table: Table,
        headshots=None,
    ) -> Path:
        """Wait until the test explicitly releases the worker."""
        entered.set()
        release.wait(timeout=2)
        return original_render_sync(table, headshots)

    monkeypatch.setattr(renderer, "_render_sync", blocked_render_sync)
    table = Table(
        title="取消测试",
        columns=("项目", "状态"),
        rows=(("渲染器", "正在工作"),),
    )
    render_task = asyncio.create_task(renderer.render(table))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()

    try:
        render_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await render_task

        queued_task = asyncio.create_task(renderer.render(table))
        close_task = asyncio.create_task(renderer.close())
        await asyncio.sleep(0.05)
        assert not close_task.done()
        assert not queued_task.done()
    finally:
        release.set()

    for _ in range(200):
        if close_task.done():
            break
        await asyncio.sleep(0.01)
    assert close_task.done()
    close_task.result()
    with pytest.raises(RuntimeError, match="closed"):
        await queued_task
    assert list(tmp_path.glob("f1-card-*.png"))


def test_circuit_mapping_is_allowlisted_and_numeric(tmp_path: Path) -> None:
    renderer = ImageRenderer(tmp_path)
    assert renderer._circuit_svg_path("albert_park").name == "melbourne-2.svg"
    assert renderer._circuit_svg_path("villeneuve").name == "montreal-6.svg"
    assert renderer._circuit_svg_path("americas").name == "austin-1.svg"
    assert renderer._circuit_svg_path("../melbourne") is None
    assert renderer._circuit_svg_path("unknown") is None

    circuits_dir = tmp_path / "circuits"
    circuits_dir.mkdir()
    (circuits_dir / "shanghai-2.svg").write_text("<svg/>", encoding="utf-8")
    (circuits_dir / "shanghai-10.svg").write_text("<svg/>", encoding="utf-8")
    selected = renderer._circuit_svg_path("shanghai", circuits_dir=circuits_dir)
    assert selected.name == "shanghai-10.svg"


async def test_renderer_sizes_many_long_rows_without_truncating_table(
    tmp_path: Path,
) -> None:
    rows = tuple(
        (
            str(index),
            f"车手{index} · Alexander Very-Long-Family-Name-{index}",
            "车队名称同样很长 Very Long Constructor Name",
            str(300 - index),
        )
        for index in range(1, 31)
    )
    table = Table(
        title="F1 车手积分榜",
        subtitle="30 位车手完整排名",
        columns=("名次", "车手", "车队", "积分"),
        rows=rows,
        footer="完整榜单",
        circuit_id="unknown",
    )
    renderer = ImageRenderer(tmp_path, max_count=5)
    try:
        path = await renderer.render(table)
    finally:
        await renderer.close()

    with Image.open(path) as image:
        assert image.width == 1120
        assert image.height >= 1450


async def test_larger_body_font_size_increases_card_height(tmp_path: Path) -> None:
    table = Table(
        title="F1 车手积分榜",
        columns=("名次", "车手", "车队", "积分"),
        rows=tuple(
            (
                str(index),
                f"车手 {index}",
                f"车队名称 {index}",
                str(300 - index),
            )
            for index in range(1, 11)
        ),
    )
    regular = ImageRenderer(
        tmp_path / "regular",
        card_width=1120,
        body_font_size=16,
    )
    enlarged = ImageRenderer(
        tmp_path / "enlarged",
        card_width=1120,
        body_font_size=32,
    )
    try:
        regular_path = await regular.render(table)
        enlarged_path = await enlarged.render(table)
    finally:
        await regular.close()
        await enlarged.close()

    with Image.open(regular_path) as regular_image:
        regular_height = regular_image.height
    with Image.open(enlarged_path) as enlarged_image:
        assert enlarged_image.width == 1120
        assert enlarged_image.height > regular_height


async def test_cache_cleanup_removes_old_and_excess_cards(tmp_path: Path) -> None:
    stale_a = tmp_path / "f1-card-aaaaaaaa.png"
    stale_b = tmp_path / "f1-card-bbbbbbbb.png"
    strict_retained = tmp_path / "f1-card-cccccccc.png"
    unrelated = tmp_path / "keep-me.png"
    for path in (stale_a, stale_b, strict_retained, unrelated):
        Image.new("RGB", (10, 10), "red").save(path)
    old_time = time.time() - 3600
    for path in (stale_a, stale_b, strict_retained):
        path.touch()
        os.utime(path, (old_time, old_time))

    renderer = ImageRenderer(
        tmp_path,
        max_age_minutes=1,
        max_count=2,
    )
    try:
        current = await renderer.render(
            Table(title="缓存清理", rows=(("状态", "正常"),))
        )
    finally:
        await renderer.close()

    generated = list(tmp_path.glob("f1-card-*.png"))
    assert current in generated
    assert len(generated) <= 2
    assert stale_a not in generated
    assert stale_b not in generated
    assert strict_retained not in generated
    assert unrelated.exists()


async def test_pruning_counts_reused_cache_in_new_limit(tmp_path: Path) -> None:
    table = Table(title="复用缓存", rows=(("状态", "有效"),))
    initial = ImageRenderer(tmp_path, max_count=10)
    try:
        current = await initial.render(table)
    finally:
        await initial.close()

    newer: list[Path] = []
    for index in range(3):
        path = tmp_path / f"f1-card-newer{index}.png"
        Image.new("RGB", (10, 10), "red").save(path)
        newer.append(path)

    lowered = ImageRenderer(tmp_path, max_count=2)
    try:
        reused = await lowered.render(table)
    finally:
        await lowered.close()

    generated = list(tmp_path.glob("f1-card-*.png"))
    assert reused == current
    assert current in generated
    assert len(generated) == 2
    assert sum(path in generated for path in newer) == 1
