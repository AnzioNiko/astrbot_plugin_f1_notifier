# astrbot_plugin_f1_notifier

🏎 **F1 Notifier** — AstrBot 的 F1 赛事推送与查询插件

自动推送 F1 比赛周通知（赛程提醒、练习赛结果、排位赛结果、冲刺赛结果、发车顺序、正赛结果），同时支持用户通过指令主动查询赛程、积分榜等信息。

---

## 功能特性

### 自动推送（订阅后生效）

| 触发时机 | 推送内容 |
|---|---|
| 距第一个练习赛开始 ≤ 24 小时 | 比赛周开始提醒 + 完整周末赛程表 |
| 每场练习赛结束后 | FP1 / FP2 / FP3 最快圈速排行 |
| 排位赛结束后 | 排位赛完整结果 |
| 冲刺赛结束后（冲刺周末） | 冲刺赛完整结果 |
| 正赛开始前 30 分钟 | 正赛发车顺序 |
| 正赛结束后 | 正赛完整结果 |

### 指令查询

/f1 帮助（/f1 help）— 显示帮助信息。  
/f1 赛程 [刷新]（/f1 schedule [refresh]）— 近期赛程（最近 5 站）。  
/f1 下一站 [刷新]（/f1 next [refresh]）— 下一站完整时间表。  
/f1 正赛成绩 [站次] [刷新]（/f1 result [round] [refresh]）— 正赛成绩，默认最近一站。  
/f1 排位成绩 [站次] [刷新]（/f1 qualifying [round] [refresh]）— 排位赛成绩。  
/f1 冲刺成绩 [站次] [刷新]（/f1 sprint [round] [refresh]）— 冲刺赛成绩。  
/f1 练习成绩 [1|2|3] [刷新]（/f1 practice [1|2|3] [refresh]）— 练习赛最快圈速，默认 FP1。  
/f1 积分榜 [车手|车队] [数量] [刷新]（/f1 standings [drivers|teams] [limit] [refresh]）— 默认显示全部车手。  
/f1 订阅（/f1 subscribe）— 订阅当前会话的自动推送。  
/f1 取消订阅（/f1 unsubscribe）— 取消当前会话的自动推送。  
/f1 测试 [赛季] [刷新]（/f1 test [season] [refresh]）— 测试插件查询功能。

插件设置新增“图片卡片宽度（像素）”和“图片文字字号比例（%）”两项。默认输出宽度为 1120 像素、字号比例为 125%；可在设置界面继续调整，保存后重新加载插件生效。图片清晰度倍率保持不变。

在查询指令末尾添加“刷新”，会跳过现有缓存，尝试重新获取赛程或成绩、重新下载头像并生成图片。若头像下载失败，会使用本地已有头像作为备用。

API 响应与车手头像保存在 AstrBot 插件数据目录的 `cache` 子目录中，重启后仍可复用；API 缓存仍遵循各接口原有的有效期。

---

## 安装

在 AstrBot 插件管理页面中搜索 `astrbot_plugin_f1_notifier` 并安装，或手动克隆本仓库到插件目录：

```bash
git clone https://github.com/MareDevi/astrbot_plugin_f1_notifier
```

插件依赖 `aiohttp`，AstrBot 会在安装时自动处理。

---

## 使用方法

1. 安装并启用插件后，在任意支持的会话中发送 `/f1 订阅` 即可订阅自动推送。
2. 发送 `/f1 帮助` 查看完整指令列表。
3. 发送 `/f1 取消订阅` 可随时取消订阅。

订阅信息与推送状态会通过 AstrBot KV 存储持久化，重启后自动恢复。

---

## 数据来源

- **[Jolpica-F1](https://api.jolpi.ca/ergast/f1/)** (Ergast API 镜像)：赛程、比赛结果、积分榜
- **[OpenF1](https://api.openf1.org/v1/)**：练习赛实时圈速数据

---

## 依赖

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) >= 适配当前 API 版本
- `aiohttp`

---

## 许可证

[AGPL-3.0](LICENSE)



