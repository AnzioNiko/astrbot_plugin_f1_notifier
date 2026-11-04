"""Load and command tests against AstrBot's actual plugin API."""

import importlib.util
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.astrbot_plugin_f1_notifier.models import Race, Session, Table


@pytest.fixture
def module(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "astrbot"))
    name = "f1_notifier_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parents[1] / "__init__.py",
        submodule_search_locations=[str(Path(__file__).resolve().parents[1])],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location(f"{name}.main", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
async def plugin(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module.StarTools, "get_data_dir", lambda *args: tmp_path)
    context = AsyncMock()
    instance = module.F1NotifierPlugin(context, {})
    store = {}
    instance.get_kv_data = AsyncMock(
        side_effect=lambda key, default: deepcopy(store.get(key, default))
    )

    async def save(key, value):
        store[key] = deepcopy(value)

    instance.put_kv_data = save
    await instance.initialize()
    await instance.scheduler.stop()
    yield instance
    await instance.terminate()


class Event:
    unified_msg_origin = "test:GroupMessage:1"

    def is_admin(self):
        return False

    def plain_result(self, text):
        return text

    def image_result(self, path):
        return ("image", path)


class AdminEvent(Event):
    def is_admin(self):
        return True


async def test_push_denies_non_admin_before_querying(plugin):
    plugin.client.schedule = AsyncMock()
    replies = [r async for r in plugin.f1_push(Event(), "test:GroupMessage:9", "next")]
    assert "管理员" in replies[0]
    plugin.client.schedule.assert_not_called()
    plugin.context.send_message.assert_not_called()


@pytest.mark.parametrize(
    "target", ["12345", "test:FriendMessage:9", ":GroupMessage:9", "test:GroupMessage:"]
)
async def test_push_rejects_invalid_or_private_target(plugin, target):
    plugin.client.schedule = AsyncMock()
    replies = [r async for r in plugin.f1_push(AdminEvent(), target, "next")]
    assert "群" in replies[0] and "UMO" in replies[0]
    plugin.client.schedule.assert_not_called()
    plugin.context.send_message.assert_not_called()


async def test_push_sends_to_target_without_changing_subscriptions(plugin):
    now = datetime.now(timezone.utc)
    race = Race(
        now.year,
        2,
        "Target weekend",
        "",
        "",
        "",
        (Session("race", now + timedelta(days=1)),),
    )
    plugin.client.schedule = AsyncMock(return_value=[race])
    target = "other_bot:GroupMessage:999"
    replies = [r async for r in plugin.f1_push(AdminEvent(), target, "next")]
    assert "已向" in replies[0] and target in replies[0]
    sent_target, chain = plugin.context.send_message.call_args.args
    assert sent_target == target
    assert "Target weekend" in str(chain)
    assert plugin.scheduler.subscriber_count == 0
    assert not plugin.scheduler._state["delivered"]


async def test_push_practice_reuses_result_selection(plugin):
    now = datetime.now(timezone.utc)
    race = Race(
        now.year,
        2,
        "GP",
        "",
        "",
        "",
        (
            Session("practice2", now - timedelta(days=1)),
            Session("race", now + timedelta(days=1)),
        ),
    )
    plugin.client.schedule = AsyncMock(return_value=[race])
    plugin.client.results = AsyncMock(
        return_value=Table("FP2", rows=(("1", "Winner"),))
    )
    replies = [
        r
        async for r in plugin.f1_push(
            AdminEvent(), "test:GroupMessage:9", "practice", "2"
        )
    ]
    assert "已向" in replies[0]
    plugin.client.results.assert_awaited_once_with(race, "practice2")


async def test_push_missing_data_does_not_send(plugin):
    plugin.client.standings = AsyncMock(return_value=None)
    replies = [
        r
        async for r in plugin.f1_push(AdminEvent(), "test:GroupMessage:9", "standings")
    ]
    assert "暂无" in replies[0]
    plugin.context.send_message.assert_not_called()


@pytest.mark.parametrize("outcome", [False, RuntimeError("offline")])
async def test_push_reports_delivery_failure(plugin, outcome):
    plugin.client.standings = AsyncMock(return_value=Table("Standings"))
    if isinstance(outcome, Exception):
        plugin.context.send_message.side_effect = outcome
    else:
        plugin.context.send_message.return_value = outcome
    replies = [
        r
        async for r in plugin.f1_push(AdminEvent(), "test:GroupMessage:9", "standings")
    ]
    assert "失败" in replies[0]
    assert "已向" not in replies[0]


async def test_push_image_failure_falls_back_to_target_text(plugin):
    plugin.config["enable_image_render"] = True
    plugin.renderer.render = AsyncMock(side_effect=RuntimeError("render failed"))
    plugin.client.standings = AsyncMock(return_value=Table("Standings"))
    target = "test:GroupMessage:9"
    replies = [r async for r in plugin.f1_push(AdminEvent(), target, "standings")]
    assert "已向" in replies[0]
    assert plugin.context.send_message.call_args.args[0] == target


@pytest.mark.parametrize(
    "query,argument",
    [
        ("subscribe", ""),
        ("invalid", ""),
        ("next", "2"),
        ("practice", "4"),
        ("result", "0"),
    ],
)
async def test_push_rejects_invalid_queries_before_send(plugin, query, argument):
    plugin.client.schedule = AsyncMock()
    replies = [
        r
        async for r in plugin.f1_push(
            AdminEvent(), "test:GroupMessage:9", query, argument
        )
    ]
    assert replies
    plugin.client.schedule.assert_not_called()
    plugin.context.send_message.assert_not_called()


async def test_subs_denies_non_admin_without_changing_state(plugin):
    plugin.scheduler.subscribe = AsyncMock()
    plugin.scheduler.unsubscribe = AsyncMock()
    for action, target in (
        ("list", ""),
        ("add", "x:GroupMessage:1"),
        ("remove", "x:GroupMessage:1"),
    ):
        replies = [r async for r in plugin.f1_subs(Event(), action, target)]
        assert "管理员" in replies[0]
    plugin.scheduler.subscribe.assert_not_called()
    plugin.scheduler.unsubscribe.assert_not_called()
    assert plugin.scheduler.subscriber_count == 0


async def test_subs_lists_local_subscription_times(plugin):
    await plugin.scheduler.subscribe(
        "bot:GroupMessage:1", datetime(2026, 3, 15, 10, tzinfo=timezone.utc)
    )
    await plugin.scheduler.subscribe(
        "bot:FriendMessage:2", datetime(2026, 3, 15, 11, tzinfo=timezone.utc)
    )
    replies = [r async for r in plugin.f1_subs(AdminEvent())]
    text = replies[0]
    assert "bot:GroupMessage:1" in text and "[群聊]" in text
    assert "bot:FriendMessage:2" in text and "[私聊]" in text
    assert "2026-03-15 18:00" in text
    assert "2026-03-15 19:00" in text


async def test_subs_reports_empty_inventory(plugin):
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "list")]
    assert "没有订阅" in replies[0]


async def test_subs_adds_and_removes_explicit_umo(plugin):
    target = "other_bot:GroupMessage:999"
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "add", target)]
    assert "已添加" in replies[0] and target in replies[0]
    assert plugin.scheduler.subscriber_count == 1
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "add", target)]
    assert "已订阅" in replies[0]
    assert plugin.scheduler.subscriber_count == 1
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "remove", target)]
    assert "已取消" in replies[0] and target in replies[0]
    assert plugin.scheduler.subscriber_count == 0
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "remove", target)]
    assert "尚未订阅" in replies[0]


async def test_subs_manages_private_conversations(plugin):
    target = "bot:FriendMessage:42"
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "add", target)]
    assert "已添加" in replies[0]
    assert await plugin.scheduler.subscriptions()


@pytest.mark.parametrize(
    "target",
    [
        "",
        "12345",
        ":GroupMessage:9",
        "bot:GroupMessage:",
        "bot:OtherMessage:9",
        "bot:GroupMessage:9 9",
    ],
)
async def test_subs_rejects_invalid_umo(plugin, target):
    plugin.scheduler.subscribe = AsyncMock()
    plugin.scheduler.unsubscribe = AsyncMock()
    for action in ("add", "remove"):
        replies = [r async for r in plugin.f1_subs(AdminEvent(), action, target)]
        assert "用法" in replies[0]
    plugin.scheduler.subscribe.assert_not_called()
    plugin.scheduler.unsubscribe.assert_not_called()


async def test_subs_rejects_unknown_action(plugin):
    replies = [r async for r in plugin.f1_subs(AdminEvent(), "rename", "x")]
    assert "用法" in replies[0]


async def test_initialize_uses_real_astrbot_api_and_restores_state(plugin):
    assert plugin.scheduler.subscriber_count == 0
    assert plugin.client._session is not None
    assert plugin.renderer.card_width == 1120
    assert plugin.renderer.body_font_size == 21
    assert plugin.renderer.title_font_size == 41


def test_chinese_subcommand_aliases_are_registered(module):
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(module.__name__)
    command_names = {
        name
        for handler in handlers
        for event_filter in handler.event_filters
        if isinstance(event_filter, CommandFilter)
        for name in event_filter.get_complete_command_names()
    }

    assert "f1 积分榜" in command_names
    assert "f1 正赛成绩" in command_names
    assert "f1 订阅" in command_names


def test_help_menu_uses_chinese_as_default_with_english_in_parentheses(module):
    assert "/f1 帮助（help）" in module.HELP_TEXT
    assert "/f1 积分榜（standings）" in module.HELP_TEXT
    assert "/f1 订阅列表（subs）" in module.HELP_TEXT
    assert "/f1 help（帮助）" not in module.HELP_TEXT


async def test_invalid_round_does_not_call_provider(plugin):
    plugin.client.schedule = AsyncMock()
    replies = [r async for r in plugin.f1_result(Event(), "0")]
    assert "正整数" in replies[0]
    plugin.client.schedule.assert_not_called()


async def test_invalid_standings_category_has_usage(plugin):
    replies = [r async for r in plugin.f1_standings(Event(), "wrong")]
    assert "drivers" in replies[0] and "车手" in replies[0]


@pytest.mark.parametrize(
    "category,expected_category,expected_limit",
    [
        ("drivers", "drivers", 20),
        ("teams", "teams", 10),
        ("车手", "drivers", 20),
        ("车队", "teams", 10),
    ],
)
async def test_standings_uses_configured_default_limit(
    plugin, category, expected_category, expected_limit
):
    plugin.client.standings = AsyncMock(return_value=Table("积分榜"))
    replies = [r async for r in plugin.f1_standings(Event(), category)]
    assert replies[0] == "🏎 积分榜"
    args = plugin.client.standings.await_args.args
    assert args[1:] == (expected_category, expected_limit)


async def test_standings_accepts_explicit_limit_for_teams(plugin):
    plugin.client.standings = AsyncMock(return_value=Table("积分榜"))
    replies = [r async for r in plugin.f1_standings(Event(), "车队", "5")]
    assert replies[0] == "🏎 积分榜"
    assert plugin.client.standings.await_args.args[1:] == ("teams", 5)


async def test_standings_accepts_numeric_only_driver_limit(plugin):
    plugin.client.standings = AsyncMock(return_value=Table("积分榜"))
    replies = [r async for r in plugin.f1_standings(Event(), "5")]
    assert replies[0] == "🏎 积分榜"
    assert plugin.client.standings.await_args.args[1:] == ("drivers", 5)


@pytest.mark.parametrize("limit", ["0", "21", "abc", "１"])
async def test_standings_rejects_invalid_team_limit(plugin, limit):
    plugin.client.standings = AsyncMock()
    replies = [r async for r in plugin.f1_standings(Event(), "teams", limit)]
    assert "1 到 20" in replies[0]
    plugin.client.standings.assert_not_called()


async def test_text_mode_does_not_invoke_renderer(plugin):
    plugin.client.standings = AsyncMock(
        return_value=Table(
            "车手积分榜", columns=("名次", "车手"), rows=(("1", "Winner"),)
        )
    )
    plugin.renderer.render = AsyncMock(side_effect=AssertionError("must not render"))
    replies = [r async for r in plugin.f1_standings(Event(), "drivers")]
    assert "Winner" in replies[0]
    plugin.renderer.render.assert_not_called()


async def test_image_failure_falls_back_to_text(plugin):
    plugin.config["enable_image_render"] = True
    plugin.client.standings = AsyncMock(
        return_value=Table("积分榜", rows=(("1", "Winner"),))
    )
    plugin.renderer.render = AsyncMock(side_effect=OSError("font unavailable"))
    replies = [r async for r in plugin.f1_standings(Event(), "drivers")]
    assert "Winner" in replies[0]


async def test_subscription_command_is_persistent(plugin):
    replies = [r async for r in plugin.f1_subscribe(Event())]
    assert "订阅成功" in replies[0]
    assert plugin.scheduler.subscriber_count == 1
    replies = [r async for r in plugin.f1_unsubscribe(Event())]
    assert "取消" in replies[0]
    assert plugin.scheduler.subscriber_count == 0


async def test_next_selects_upcoming_weekend(plugin):
    now = datetime.now(timezone.utc)
    old = Race(
        now.year, 1, "Old", "", "", "", (Session("race", now - timedelta(days=7)),)
    )
    new = Race(
        now.year, 2, "New", "", "", "", (Session("race", now + timedelta(days=7)),)
    )
    plugin.client.schedule = AsyncMock(return_value=[old, new])
    replies = [r async for r in plugin.f1_next(Event())]
    assert "New" in replies[0] and "Old" not in replies[0]


async def test_unsupported_platform_does_not_mark_delivery_success(plugin):
    plugin.context.send_message.return_value = False
    assert not await plugin._send("test:GroupMessage:1", Table("Test"))


async def test_schedule_queries_next_year_during_offseason(plugin):
    now = datetime.now(timezone.utc)
    future = Race(
        now.year + 1,
        1,
        "Next season",
        "",
        "",
        "",
        (Session("race", now + timedelta(days=365)),),
    )
    plugin.client.schedule = AsyncMock(side_effect=[[], [future]])
    replies = [r async for r in plugin.f1_next(Event())]
    assert "Next season" in replies[0]


@pytest.mark.parametrize(
    "settings",
    [
        {"weekend_push_day": "周四"},
        {"weekend_push_day": 1},
        {"weekend_push_time": "24:00"},
        {"weekend_push_time": "09:60"},
        {"weekend_push_time": "9:00"},
        {"weekend_push_time": "09:00:00"},
        {"weekend_push_time": "０９:００"},
        {"weekend_push_time": None},
    ],
)
def test_invalid_weekend_reminder_settings_rejected(module, settings):
    with pytest.raises(ValueError, match="weekend_push_"):
        module.F1NotifierPlugin(AsyncMock(), settings)


@pytest.mark.parametrize(
    "settings,key",
    [
        ({"image_card_width": 999}, "image_card_width"),
        ({"image_card_width": 1601}, "image_card_width"),
        ({"image_body_font_size": 13}, "image_body_font_size"),
        ({"image_body_font_size": True}, "image_body_font_size"),
        ({"image_title_font_size": 17}, "image_title_font_size"),
        ({"image_title_font_size": 81}, "image_title_font_size"),
        ({"standings_driver_limit": 0}, "standings_driver_limit"),
        ({"standings_team_limit": 21}, "standings_team_limit"),
    ],
)
def test_invalid_personal_settings_rejected(module, settings, key):
    with pytest.raises(ValueError, match=key):
        module.F1NotifierPlugin(AsyncMock(), settings)
