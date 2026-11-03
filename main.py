"""AstrBot F1 赛事推送与查询插件。中文子指令为主，保留原英文别名。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .src.astrbot_plugin_f1_notifier import api
from .src.astrbot_plugin_f1_notifier import formatter as fmt
from .src.astrbot_plugin_f1_notifier import image_renderer as img
from .src.astrbot_plugin_f1_notifier.models import Failure, Success
from .src.astrbot_plugin_f1_notifier.scheduler import F1Scheduler

HELP_TEXT = """🏎 F1 赛事查询与推送指令

/f1 帮助（/f1 help）— 显示此帮助。
/f1 赛程 [刷新]（/f1 schedule [refresh]）— 查看近期赛程（最近 5 站）。
/f1 下一站 [刷新]（/f1 next [refresh]）— 查看下一站完整时间表。
/f1 正赛成绩 [站次] [刷新]（/f1 result [round] [refresh]）— 查看正赛成绩，默认最近一站。
/f1 排位成绩 [站次] [刷新]（/f1 qualifying [round] [refresh]）— 查看排位赛成绩。
/f1 冲刺成绩 [站次] [刷新]（/f1 sprint [round] [refresh]）— 查看冲刺赛成绩。
/f1 练习成绩 [1|2|3] [刷新]（/f1 practice [1|2|3] [refresh]）— 查看练习赛最快圈速，默认 FP1。
/f1 积分榜 [车手|车队] [数量] [刷新]（/f1 standings [drivers|teams] [limit] [refresh]）— 查看车手或车队积分榜。
/f1 订阅（/f1 subscribe）— 订阅自动推送。
/f1 取消订阅（/f1 unsubscribe）— 取消自动推送。
/f1 测试 [赛季] [刷新]（/f1 test [season] [refresh]）— 测试插件查询功能。

在查询指令末尾添加“刷新”，会跳过现有缓存，尝试重新获取赛程或成绩、重新下载头像并生成图片。例如：
/f1 正赛成绩 刷新
/f1 积分榜 车手 10 刷新
如果头像下载失败，会继续使用本地已有头像作为备用。"""


@register(
    "astrbot_plugin_f1_notifier",
    "MareDevi",
    "F1赛事推送与查询插件",
    "1.1.4",
    "https://github.com/MareDevi/astrbot_plugin_f1_notifier",
)
class F1NotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = Path(
            StarTools.get_data_dir("astrbot_plugin_f1_notifier")
        )
        self.cache_dir = self.plugin_data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        api.configure_openf1_api_token(
            self.config.get("openf1_api_token", "")
        )
        api.configure_persistent_cache(self.cache_dir / "api_responses.json")
        img.configure(config, cache_dir=self.cache_dir / "headshots")
        self.scheduler = F1Scheduler(self, context, config)

    @property
    def _image_mode(self) -> bool:
        return bool(self.config.get("enable_image_render", False))

    async def _render_or_text(
        self, event: AstrMessageEvent, text: str, image_path: str
    ):
        """Yield image result if image mode is on, otherwise plain text."""
        if self._image_mode:
            try:
                return event.image_result(image_path)
            except Exception as e:
                logger.warning(
                    f"[F1Notifier] Image render failed, fallback to text: {e}"
                )
        return event.plain_result(text)

    async def initialize(self) -> None:
        """Start the background notification scheduler."""
        self.scheduler.start()
        logger.info("[F1Notifier] Plugin initialized.")

    async def terminate(self) -> None:
        """Stop the scheduler and close HTTP session."""
        await self.scheduler.stop()
        await api.close_session()
        logger.info("[F1Notifier] Plugin terminated.")

    # ──────────────────── command_group ────────────────────────────
    @filter.command_group("f1")
    def f1(self) -> None:
        """F1赛事查询与推送"""

    # ──────────────────── sub-commands ─────────────────────────────

    @staticmethod
    def _refresh_requested(*values: str) -> bool:
        markers = {"刷新", "强制刷新", "refresh", "force-refresh"}
        return any(value.strip().casefold() in markers for value in values if value)

    @staticmethod
    def _is_refresh_marker(value: str) -> bool:
        return F1NotifierPlugin._refresh_requested(value)

    async def _with_refresh(self, force_refresh: bool, action):
        """在一次查询或渲染期间启用缓存绕过上下文。"""
        if not force_refresh:
            return await action()
        with api.force_refresh_cache(), img.force_refresh_headshots():
            return await action()

    @f1.command("帮助", alias={"help"})
    async def f1_help(self, event: AstrMessageEvent):
        """显示中文指令帮助。"""
        yield event.plain_result(HELP_TEXT)

    @f1.command("赛程", alias={"schedule"})
    async def f1_schedule(self, event: AstrMessageEvent, refresh_arg: str = ""):
        """查看近期赛程；末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(refresh_arg)
        result = await self._with_refresh(
            force_refresh, lambda: api.get_current_schedule()
        )
        match result:
            case Success(value=races):
                text = fmt.format_schedule(races)
                image_path = await self._with_refresh(
                    force_refresh, lambda: img.render_schedule(races)
                )
                yield await self._render_or_text(event, text, image_path)
            case Failure(error=err):
                logger.error(f"[F1Notifier] 赛程查询失败：{err}")
                yield event.plain_result("❌ 获取赛程失败，请稍后重试。")

    @f1.command("下一站", alias={"next"})
    async def f1_next(self, event: AstrMessageEvent, refresh_arg: str = ""):
        """查看下一站完整时间表；末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(refresh_arg)
        result = await self._with_refresh(
            force_refresh, lambda: api.get_current_schedule()
        )
        match result:
            case Success(value=races):
                now = datetime.now(timezone.utc)
                next_race = None
                for race in races:
                    dt = fmt.race_utc(race)
                    if dt is not None and dt >= now:
                        next_race = race
                        break
                if next_race is None:
                    yield event.plain_result(
                        "📅 本赛季剩余赛程已全部完成，期待下赛季！"
                    )
                else:
                    text = fmt.format_next_race(next_race)
                    image_path = await self._with_refresh(
                        force_refresh, lambda: img.render_next_race(next_race)
                    )
                    yield await self._render_or_text(event, text, image_path)
            case Failure(error=err):
                logger.error(f"[F1Notifier] 下一站查询失败：{err}")
                yield event.plain_result("❌ 获取下一站信息失败，请稍后重试。")

    @f1.command("正赛成绩", alias={"result"})
    async def f1_result(
        self,
        event: AstrMessageEvent,
        round_num: str = "last",
        refresh_arg: str = "",
    ):
        """查看正赛成绩，可指定轮次，末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(round_num, refresh_arg)
        if self._is_refresh_marker(round_num):
            round_num = "last"
        round_arg: int | str = int(round_num) if round_num.isdigit() else "last"
        result = await self._with_refresh(
            force_refresh, lambda: api.get_race_result(round_arg)
        )
        match result:
            case Success(value=race) if race.race_results:
                text = fmt.format_race_result(race)
                image_path = await self._with_refresh(
                    force_refresh, lambda: img.render_race_result(race)
                )
                yield await self._render_or_text(event, text, image_path)
            case Success():
                yield event.plain_result("⏳ 正赛成绩暂未公布，请比赛结束后再试。")
            case Failure(error=err):
                logger.error(f"[F1Notifier] 正赛成绩查询失败：{err}")
                yield event.plain_result("❌ 获取正赛成绩失败，请稍后重试。")

    @f1.command("排位成绩", alias={"qualifying"})
    async def f1_qualifying(
        self,
        event: AstrMessageEvent,
        round_num: str = "last",
        refresh_arg: str = "",
    ):
        """查看排位赛成绩，可指定轮次，末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(round_num, refresh_arg)
        if self._is_refresh_marker(round_num):
            round_num = "last"
        round_arg: int | str = int(round_num) if round_num.isdigit() else "last"
        result = await self._with_refresh(
            force_refresh, lambda: api.get_qualifying_result(round_arg)
        )
        match result:
            case Success(value=race) if race.qualifying_results:
                text = fmt.format_qualifying_result(race)
                image_path = await self._with_refresh(
                    force_refresh, lambda: img.render_qualifying_result(race)
                )
                yield await self._render_or_text(event, text, image_path)
            case Success():
                yield event.plain_result("⏳ 排位赛成绩暂未公布，请稍后再试。")
            case Failure(error=err):
                logger.error(f"[F1Notifier] 排位成绩查询失败：{err}")
                yield event.plain_result("❌ 获取排位赛成绩失败，请稍后重试。")

    @f1.command("冲刺成绩", alias={"sprint"})
    async def f1_sprint(
        self,
        event: AstrMessageEvent,
        round_num: str = "last",
        refresh_arg: str = "",
    ):
        """查看冲刺赛成绩，可指定轮次，末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(round_num, refresh_arg)
        if self._is_refresh_marker(round_num):
            round_num = "last"
        round_arg: int | str = int(round_num) if round_num.isdigit() else "last"
        result = await self._with_refresh(
            force_refresh, lambda: api.get_sprint_result(round_arg)
        )
        match result:
            case Success(value=race) if race.sprint_results:
                text = fmt.format_sprint_result(race)
                image_path = await self._with_refresh(
                    force_refresh, lambda: img.render_sprint_result(race)
                )
                yield await self._render_or_text(event, text, image_path)
            case Success():
                yield event.plain_result("⏳ 冲刺赛成绩暂未公布，或该站没有冲刺赛。")
            case Failure(error=err):
                logger.error(f"[F1Notifier] 冲刺成绩查询失败：{err}")
                yield event.plain_result("❌ 获取冲刺赛成绩失败，请稍后重试。")

    @f1.command("练习成绩", alias={"practice", "练习赛", "练习赛成绩"})
    async def f1_practice(
        self,
        event: AstrMessageEvent,
        session: str = "1",
        refresh_arg: str = "",
    ):
        """查看练习赛最快圈速，末尾可加“刷新”。"""
        force_refresh = self._refresh_requested(session, refresh_arg)
        if self._is_refresh_marker(session):
            session = "1"
        normalized = session.lower().removeprefix("fp") or "1"
        if normalized not in ("1", "2", "3"):
            yield event.plain_result(
                "❌ 练习赛场次请输入 1、2 或 3（例如 /f1 练习成绩 2）。"
            )
            return

        session_result = await self._with_refresh(
            force_refresh, lambda: api.get_practice_session(normalized)
        )
        match session_result:
            case Failure(error=err):
                logger.warning(f"[F1Notifier] 练习赛场次查询失败：{err}")
                yield event.plain_result(
                    f"⏳ 暂未找到 FP{normalized} 数据，请练习赛结束后再试。"
                )
                return
            case Success(value=of1_session):
                sk = of1_session.session_key
                results_result, drivers_result = await self._with_refresh(
                    force_refresh,
                    lambda: asyncio.gather(
                        api.get_session_result(sk),
                        api.get_drivers_for_session(sk),
                    ),
                )
                match (results_result, drivers_result):
                    case (Success(value=results), Success(value=drivers_list)) if results:
                        drivers_by_num = {d.driver_number: d for d in drivers_list}
                        text = fmt.format_practice_result(
                            of1_session, results, drivers_by_num, normalized
                        )
                        image_path = await self._with_refresh(
                            force_refresh,
                            lambda: img.render_practice_result(
                                of1_session, results, drivers_by_num, normalized
                            ),
                        )
                        yield await self._render_or_text(event, text, image_path)
                    case (Success(), Failure(error=err)):
                        logger.error(f"[F1Notifier] 练习赛车手数据查询失败：{err}")
                        yield event.plain_result("❌ 获取练习赛车手数据失败，请稍后重试。")
                    case (Success(), _):
                        yield event.plain_result(
                            f"⏳ FP{normalized} 成绩暂未就绪，请练习赛结束后再试。"
                        )
                    case (Failure(error=err), _):
                        logger.error(f"[F1Notifier] 练习赛成绩查询失败：{err}")
                        yield event.plain_result("❌ 获取练习赛成绩失败，请稍后重试。")

    @f1.command("积分榜", alias={"standings"})
    async def f1_standings(
        self,
        event: AstrMessageEvent,
        category: str = "车手",
        limit_arg: str = "",
        refresh_arg: str = "",
    ):
        """查看车手或车队积分榜；可指定数量并在末尾加“刷新”。"""
        force_refresh = self._refresh_requested(category, limit_arg, refresh_arg)
        if self._is_refresh_marker(category):
            category = "车手"
        if self._is_refresh_marker(limit_arg):
            limit_arg = ""

        if category.strip().isdigit():
            limit_arg = category.strip()
            category = "车手"

        limit: int | None = None
        if limit_arg.strip():
            if not limit_arg.strip().isdigit() or int(limit_arg.strip()) <= 0:
                yield event.plain_result("❌ 数量必须是大于 0 的整数。")
                return
            limit = int(limit_arg.strip())

        normalized_category = category.strip().casefold()
        if normalized_category in {"teams", "constructors", "team", "车队", "车队积分"}:
            result = await self._with_refresh(
                force_refresh, lambda: api.get_constructor_standings()
            )
            match result:
                case Success(value=standings):
                    text = fmt.format_constructor_standings(standings)
                    image_path = await self._with_refresh(
                        force_refresh,
                        lambda: img.render_constructor_standings(standings),
                    )
                    yield await self._render_or_text(event, text, image_path)
                case Failure(error=err):
                    logger.error(f"[F1Notifier] 车队积分榜查询失败：{err}")
                    yield event.plain_result("❌ 获取车队积分榜失败，请稍后重试。")
        else:
            result = await self._with_refresh(
                force_refresh, lambda: api.get_driver_standings()
            )
            match result:
                case Success(value=standings):
                    text = fmt.format_driver_standings(standings, limit=limit)
                    image_path = await self._with_refresh(
                        force_refresh,
                        lambda: img.render_driver_standings(standings, limit=limit),
                    )
                    yield await self._render_or_text(event, text, image_path)
                case Failure(error=err):
                    logger.error(f"[F1Notifier] 车手积分榜查询失败：{err}")
                    yield event.plain_result("❌ 获取车手积分榜失败，请稍后重试。")
    @f1.command("订阅", alias={"subscribe"})
    async def f1_subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的自动推送"""
        session = event.unified_msg_origin
        added = await self.scheduler.add_subscriber(session)
        if added:
            yield event.plain_result(
                f"✅ 已订阅 F1 自动推送！\n"
                f"当前共 {self.scheduler.subscriber_count()} 个会话已订阅。\n"
                "将自动推送：赛程提醒、排位/冲刺赛结果、发车顺序、正赛结果。"
            )
        else:
            yield event.plain_result(
                "ℹ️ 当前会话已经订阅过了。发送 /f1 取消订阅 可以取消。"
            )

    @f1.command("取消订阅", alias={"unsubscribe"})
    async def f1_unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的自动推送"""
        session = event.unified_msg_origin
        removed = await self.scheduler.remove_subscriber(session)
        if removed:
            yield event.plain_result("✅ 已取消订阅 F1 自动推送。")
        else:
            yield event.plain_result(
                "ℹ️ 当前会话尚未订阅。发送 /f1 订阅 可以订阅。"
            )

    @f1.command("测试", alias={"test"})
    async def f1_test(
        self,
        event: AstrMessageEvent,
        season: str = "2025",
        refresh_arg: str = "",
    ):
        """测试插件各项查询功能，可指定赛季和末尾刷新标记。"""
        force_refresh = self._refresh_requested(season, refresh_arg)
        if self._is_refresh_marker(season):
            season = "2025"
        yr = int(season) if season.isdigit() else 2025
        yield event.plain_result(f"🔧 正在测试 {yr} 赛季数据，请稍候...")

        async def run(name: str, coro) -> tuple[str, bool, str]:
            r = await coro
            match r:
                case Success(value=val):
                    match val:
                        case [*items]:
                            return (name, True, f"✅ {len(items)} 条记录")
                        case obj:
                            label = getattr(obj, "race_name", None) or getattr(
                                obj, "driver", None
                            )
                            detail = f"✅ {label}" if label else "✅"
                            return (name, True, detail)
                case Failure(error=err):
                    return (name, False, f"❌ {err}")
            return (name, False, "❌ 未知结果")

        async def _fp_test():
            session_result = await api.get_practice_session("1", yr)
            match session_result:
                case Success(value=session):
                    return await api.get_session_result(session.session_key)
                case Failure() as failure:
                    return failure

        results: list[tuple[str, bool, str]] = list(
            await self._with_refresh(
                force_refresh,
                lambda: asyncio.gather(
                    run("赛程", api.get_current_schedule(yr)),
                    run("正赛成绩（第 1 轮）", api.get_race_result(1, yr)),
                    run("排位成绩（第 1 轮）", api.get_qualifying_result(1, yr)),
                    run("冲刺成绩（第 5 轮）", api.get_sprint_result(5, yr)),
                    run("车手积分榜", api.get_driver_standings(yr)),
                    run("车队积分榜", api.get_constructor_standings(yr)),
                    run("练习赛成绩（FP1）", _fp_test()),
                ),
            )
        )

        lines = [f"📋 F1 插件测试报告（{yr} 赛季）\n"]
        passed = sum(1 for _, ok, _ in results if ok)
        for name, ok, detail in results:
            lines.append(f"  {'✅' if ok else '❌'} {name}：{detail}")
        lines.append(f"\n共 {passed}/{len(results)} 项通过")
        yield event.plain_result("\n".join(lines))


