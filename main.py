# -*- coding: utf-8 -*-
"""松果电子开机卡 · 飞牛 NAS 开关机控制

接口事实（按官方文档实现，勿改）：
- 控制：POST https://songguoyun.topwd.top/Esp_Api_new.php
        body = urlencode(整个 JSON)，字段 sgdz_account/sgdz_password/device_name/value
- 列表：POST https://songguoyun.topwd.top/Esp_Api_advance.php
        body = urlencode(整个 JSON)，字段 sgdz_account/sgdz_password/type=1
- value：0 关机 / 1 开机 / 2 强制重启 / 11 查询状态 / 14 强制关机 / 25 重启
- 查询状态返回：1=开机、0=关机（与"动作执行成功=0"语义不同，别混用）
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.parse
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

API_CONTROL = "https://songguoyun.topwd.top/Esp_Api_new.php"
API_LIST = "https://songguoyun.topwd.top/Esp_Api_advance.php"

# 动作返回码 → 人话
STATUS_TEXT = {
    "0": "成功",
    "-1": "失败",
    "2": "设备不在线",
    "-2": "账号或密码错误",
    "-3": "设备不存在",
    "-4": "数据范围错误",
}

# 设备列表里的状态
DEVICE_STATE = {"0": "关机", "1": "开机", "2": "离线", "3": "在线"}

CONFIRM_TTL = 90  # 二次确认有效期（秒）


@register(
    "astrbot_plugin_songguo_power",
    "彭语涵",
    "松果电子开机卡：控制飞牛 NAS 开关机与状态查询",
    "1.1.1",
)
class SongguoPower(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.cfg = config or {}
        self._pending: dict[str, dict] = {}      # uid -> {action, expire}
        self._last_call: dict[str, float] = {}   # uid -> 上次执行时间
        self._monitor_task: asyncio.Task | None = None
        self._last_state: str | None = None
        self._last_umo: str = ""

    # ================= 生命周期 =================
    # ================= 配置页接口 =================
    def _sessions(self) -> list:
        """从会话库里列出可选的推送目标。"""
        import sqlite3

        db = "/AstrBot/data/data_v4.db"
        rows = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select user_id, platform_id, max(updated_at) as t, count(*) as n "
                    "from conversations group by user_id, platform_id order by t desc limit 40"
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[songguo] 会话列表读取失败：%s", exc)
            return []

        plat_name = {
            "weixin_personal_ezgt": "微信",
            "webchat": "小智设备",
            "wecom": "企业微信",
        }
        out = []
        for umo, platform, t, n in rows:
            if not umo:
                continue
            label = plat_name.get(platform or "", platform or "未知")
            tail = str(umo).split(":")[-1]
            short = tail if len(tail) <= 20 else tail[:8] + "…" + tail[-6:]
            out.append({
                "umo": str(umo),
                "label": f"{label} · {short}",
                "last": str(t)[:19],
            })
        return out

    def _panel_cfg(self) -> dict:
        cp = "/AstrBot/data/config/astrbot_plugin_songguo_power_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}
        return {
            "monitor_enable": cur.get("monitor_enable", False),
            "monitor_interval_min": cur.get("monitor_interval_min", 10),
            "monitor_umo": cur.get("monitor_umo", ""),
            "device_name": cur.get("device_name", ""),
            "allowed_users": cur.get("allowed_users") or [],
        }

    def _panel_save(self, data: dict) -> list:
        cp = "/AstrBot/data/config/astrbot_plugin_songguo_power_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}
        allowed = {"monitor_enable", "monitor_interval_min", "monitor_umo", "device_name"}
        changed = []
        for k, v in (data or {}).items():
            if k in allowed and cur.get(k) != v:
                cur[k] = v
                changed.append(k)
        try:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[songguo] 保存失败：%s", exc)
            return []
        if changed:
            logger.info("[songguo] 页面保存：%s", ", ".join(changed))
        return changed

    def _register_page_api(self) -> None:
        try:
            import astrbot.api.web as webbridge

            async def _page_sessions(**kwargs):
                return webbridge.json_response({
                    "ok": True,
                    "sessions": self._sessions(),
                    "config": self._panel_cfg(),
                })

            async def _page_config(**kwargs):
                method = getattr(webbridge.request, "method", "GET").upper()
                if method == "POST":
                    try:
                        d = await webbridge.request.json(default={}) or {}
                    except Exception:  # noqa: BLE001
                        d = {}
                    changed = self._panel_save(d.get("save") or {})
                    return webbridge.json_response({"ok": True, "changed": changed})

                return webbridge.json_response({
                    "ok": True,
                    "config": self._panel_cfg(),
                    "sessions": self._sessions(),
                })

            async def _page_devices(**kwargs):
                data = await self._devices()
                devs = data.get("deviceslist")
                if not isinstance(devs, list):
                    return webbridge.json_response({
                        "ok": False, "error": self._friendly(data), "devices": [],
                    })
                out = []
                for d in devs:
                    name = str(d.get("deviceName") or "-")
                    st = DEVICE_STATE.get(str(d.get("status")), str(d.get("status")))
                    out.append({"name": name, "label": f"{name}（{st}）", "status": st})
                return webbridge.json_response({"ok": True, "devices": out})

            async def _page_status(**kwargs):
                data = await self._control(self._cfg("value_query", 11))
                code = self._code(data)
                text = {"1": "NAS 现在是【开机】状态", "0": "NAS 现在是【关机】状态"}.get(code)
                if not text:
                    text = f"查不到状态：{self._friendly(data)}"
                return webbridge.json_response({"ok": True, "code": code, "text": text})

            routes = [
                ("/page/sessions", _page_sessions, ["GET"], "松果：会话列表"),
                ("/page/config", _page_config, ["GET", "POST"], "松果：配置读写"),
                ("/astrbot_plugin_songguo_power/page/sessions", _page_sessions, ["GET"], "松果：会话列表"),
                ("/astrbot_plugin_songguo_power/page/config", _page_config, ["GET", "POST"], "松果：配置读写"),
                ("/page/devices", _page_devices, ["GET"], "松果：设备列表"),
                ("/page/status", _page_status, ["GET"], "松果：查询状态"),
                ("/astrbot_plugin_songguo_power/page/devices", _page_devices, ["GET"], "松果：设备列表"),
                ("/astrbot_plugin_songguo_power/page/status", _page_status, ["GET"], "松果：查询状态"),
            ]
            for path, handler, methods, desc in routes:
                try:
                    self.context.register_web_api(path, handler, methods, desc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[songguo] 注册 %s 失败：%s", path, exc)
            logger.info("[songguo] 配置页接口已注册")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[songguo] 配置页接口注册失败：%s", exc)

    async def initialize(self) -> None:
        if self._cfg("monitor_enable", False) and (self.cfg.get("monitor_umo") or "").strip():
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("[songguo] 巡检已启动，间隔 %s 分钟", self._cfg("monitor_interval_min", 10))
        logger.info("[songguo] 插件就绪")

    async def terminate(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()

    # ================= 基础工具 =================
    def _cfg(self, key: str, default=None):
        v = self.cfg.get(key)
        return default if v in (None, "") else v

    def _mask(self, account: str) -> str:
        s = str(account or "")
        if len(s) <= 4:
            return "***"
        return s[:2] + "***" + s[-2:]

    def _uid(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            return "unknown"

    def _admins(self) -> list[str]:
        try:
            conf = self.context.get_config()
            ids = conf.get("admins_id") or []
            if isinstance(ids, str):
                ids = [x.strip() for x in ids.replace("，", ",").split(",") if x.strip()]
            return [str(i) for i in ids]
        except Exception:  # noqa: BLE001
            return []

    def _allowed(self, event: AstrMessageEvent) -> bool:
        uid = self._uid(event)
        white = self.cfg.get("allowed_users") or []
        if isinstance(white, str):
            white = [x.strip() for x in white.replace("，", ",").split(",") if x.strip()]
        white = [str(x) for x in white]
        if uid in white:
            return True
        if not white and uid in self._admins():
            return True
        return False

    # ================= HTTP =================
    async def _post(self, url: str, payload: dict) -> dict:
        """整段 JSON 先 urlencode，再作为请求体发送。"""
        timeout = int(self._cfg("timeout", 15))
        mode = str(self._cfg("body_mode", "quote"))
        raw = json.dumps(payload, ensure_ascii=False)
        if mode == "data_field":
            body = urllib.parse.urlencode({"data": raw})
        else:
            body = urllib.parse.quote(raw, safe="")

        def _do() -> str:
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read(4096).decode("utf-8", "replace")

        try:
            text = await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[songguo] 请求失败 %s：%s", url.rsplit("/", 1)[-1], exc)
            return {"status": "net", "tips": f"网络异常：{exc}"}

        # 松果接口的响应本身也可能是 urlencode 过的 JSON，先原样试，再解码试
        data = None
        for candidate in (text, urllib.parse.unquote(text)):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            return {"status": "bad", "tips": f"返回无法解析：{text[:120]}"}
        return data if isinstance(data, dict) else {"status": "bad", "tips": str(data)[:120]}

    async def _control(self, value) -> dict:
        payload = {
            "sgdz_account": str(self._cfg("sgdz_account", "")),
            "sgdz_password": str(self._cfg("sgdz_password", "")),
            "device_name": str(self._cfg("device_name", "")),
            "value": value,
        }
        return await self._post(API_CONTROL, payload)

    async def _devices(self) -> dict:
        payload = {
            "sgdz_account": str(self._cfg("sgdz_account", "")),
            "sgdz_password": str(self._cfg("sgdz_password", "")),
            "type": 1,
        }
        return await self._post(API_LIST, payload)

    # ================= 结果翻译 =================
    @staticmethod
    def _code(data: dict) -> str:
        s = data.get("status")
        if s is None:
            s = data.get("tips")
        return str(s).strip()

    def _friendly(self, data: dict) -> str:
        code = self._code(data)
        tips = str(data.get("tips") or "").strip()
        if code in ("net", "bad"):
            return tips or "接口返回异常"
        human = STATUS_TEXT.get(code)
        if human:
            return f"{human}（{code}）" + (f"，{tips}" if tips and tips != human else "")
        return f"未知返回：{code}" + (f"，{tips}" if tips else "")

    # ================= 指令 =================
    @filter.command("nas")
    async def nas(self, event: AstrMessageEvent, arg: str = ""):
        # 记住最近的会话，巡检留空时自动用它
        try:
            umo = event.unified_msg_origin
            if umo:
                self._last_umo = str(umo)
        except Exception:  # noqa: BLE001
            pass

        if not self._allowed(event):
            yield event.plain_result("没有权限。这个指令只给白名单里的用户用。")
            return

        if not self._cfg("sgdz_account") or not self._cfg("sgdz_password") or not self._cfg("device_name"):
            yield event.plain_result("还没配置账号/密码/设备名，去插件配置页填一下。")
            return

        text = (arg or "").strip()
        if not text:
            text = self._rest_of_message(event)
        action = self._parse_action(text)

        if not action:
            yield event.plain_result(
                "用法：\n"
                "/nas 状态\n"
                "/nas 开机\n"
                "/nas 关机（需确认）\n"
                "/nas 重启（需确认）\n"
                "/nas 强制关机（需确认）\n"
                "/nas 设备\n"
                "/nas 确认"
            )
            return

        uid = self._uid(event)

        # 二次确认
        if action == "确认":
            pending = self._pending.get(uid)
            if not pending or pending["expire"] < time.time():
                self._pending.pop(uid, None)
                yield event.plain_result("没有待确认的操作，或者已经超时了，重新发一次吧。")
                return
            real = pending["action"]
            self._pending.pop(uid, None)
            async for r in self._do_action(real, event, uid):
                yield r
            return

        if action in ("关机", "重启", "强制关机"):
            self._pending[uid] = {"action": action, "expire": time.time() + CONFIRM_TTL}
            word = {"关机": "关机", "重启": "重启", "强制关机": "强制关机"}[action]
            extra = "（强制断电，可能损伤文件系统）" if action == "强制关机" else ""
            yield event.plain_result(f"确认{word}请回复：/nas 确认　（{CONFIRM_TTL} 秒内有效）{extra}")
            return

        async for r in self._do_action(action, event, uid):
            yield r

    async def _do_action(self, action: str, event: AstrMessageEvent, uid: str):
        # 频率限制
        gap = float(self._cfg("cooldown_sec", 8))
        last = self._last_call.get(uid, 0.0)
        if time.time() - last < gap:
            yield event.plain_result(f"别急，{int(gap - (time.time() - last)) + 1} 秒后再试。")
            return
        self._last_call[uid] = time.time()

        if action == "状态":
            value = self._cfg("value_query", 11)
            data = await self._control(value)
            code = self._code(data)
            if code == "1":
                yield event.plain_result("设备现在是【开机】状态。")
            elif code == "0":
                yield event.plain_result("设备现在是【关机】状态。")
            else:
                yield event.plain_result(f"查不到状态：{self._friendly(data)}")
            return

        if action == "设备":
            data = await self._devices()
            devices = data.get("deviceslist")
            if not isinstance(devices, list):
                yield event.plain_result(f"拉不到设备列表：{self._friendly(data)}")
                return
            if not devices:
                yield event.plain_result("账号下没有设备。")
                return
            lines = ["设备列表："]
            for d in devices:
                name = str(d.get("deviceName") or "-")
                st = DEVICE_STATE.get(str(d.get("status")), str(d.get("status")))
                mark = " ← 当前配置" if name == str(self._cfg("device_name", "")) else ""
                lines.append(f"· {name}　{st}{mark}")
            yield event.plain_result("\n".join(lines))
            return

        mapping = {
            "开机": (self._cfg("value_power_on", 1), "开机"),
            "关机": (self._cfg("value_power_off", 0), "关机"),
            "重启": (self._cfg("value_reboot", 25), "重启"),
            "强制关机": (self._cfg("value_force_off", 14), "强制关机"),
        }
        if action not in mapping:
            yield event.plain_result("看不懂这个动作。")
            return

        value, label = mapping[action]
        data = await self._control(value)
        code = self._code(data)
        logger.info("[songguo] %s 下发 %s -> %s（账号 %s）",
                    label, value, code, self._mask(str(self._cfg("sgdz_account", ""))))

        if code == "0":
            yield event.plain_result(f"{label}指令已下发，设备会执行。")
        else:
            yield event.plain_result(f"{label}失败：{self._friendly(data)}")

    # ================= 解析 =================
    @staticmethod
    def _rest_of_message(event: AstrMessageEvent) -> str:
        try:
            raw = str(getattr(event, "message_str", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""
        for prefix in ("/nas", "nas"):
            if raw.lower().startswith(prefix):
                return raw[len(prefix):].strip()
        return raw

    @staticmethod
    def _parse_action(text: str) -> str:
        t = (text or "").strip().replace(" ", "")
        if not t:
            return ""
        table = {
            "状态": "状态", "查询": "状态", "state": "状态", "status": "状态",
            "开机": "开机", "启动": "开机", "on": "开机",
            "关机": "关机", "关闭": "关机", "off": "关机",
            "重启": "重启", "reboot": "重启", "restart": "重启",
            "强制关机": "强制关机", "强制断电": "强制关机", "forcedown": "强制关机",
            "设备": "设备", "列表": "设备", "devices": "设备",
            "确认": "确认", "确定": "确认", "confirm": "确认",
        }
        if t in table:
            return table[t]
        for k, v in table.items():
            if k in t:
                return v
        return ""

    # ================= 巡检 =================
    async def _monitor_loop(self) -> None:
        interval = max(3, int(self._cfg("monitor_interval_min", 10))) * 60
        while True:
            try:
                await asyncio.sleep(interval)
                data = await self._control(self._cfg("value_query", 11))
                code = self._code(data)
                state = {"1": "开机", "0": "关机"}.get(code)
                if not state:
                    continue
                if self._last_state and state != self._last_state:
                    await self._push(f"设备状态变化：{self._last_state} → {state}")
                self._last_state = state
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[songguo] 巡检异常：%s", exc)

    async def _push(self, text: str) -> None:
        umo = str(self._cfg("monitor_umo", "")).strip() or getattr(self, "_last_umo", "")
        if not umo:
            logger.info("[songguo] 还没有可用的会话，巡检消息跳过")
            return
        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[songguo] 推送失败：%s", exc)
