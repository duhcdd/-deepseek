import hashlib
import json
import time
import urllib.parse

import aiohttp

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
]


def _fmt_count(n) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _fmt_dur(sec) -> str:
    if isinstance(sec, str):
        return sec if ":" in sec else "?"
    try:
        sec = int(sec)
    except Exception:
        return "?"
    return f"{sec // 60}:{sec % 60:02d}"


class Main(star.Star):
    """B站工具集：让机器人自己刷B站、读视频、念字幕，还能在群里 @ 你发消息。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._session = None
        self._mixin_key = None
        self._mixin_ts = 0.0

    # ---------- 基础设施 ----------
    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def _json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> dict:
        sess = await self._sess()
        async with sess.get(url, params=params, headers=headers) as resp:
            return await resp.json(content_type=None)

    async def _wbi(self) -> str:
        if self._mixin_key and time.time() - self._mixin_ts < 600:
            return self._mixin_key
        d = await self._json("https://api.bilibili.com/x/web-interface/nav")
        wbi = (d.get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            raise RuntimeError("拿不到 B站 wbi key（可能被风控）")
        self._mixin_key = "".join(raw[i] for i in MIXIN)[:32]
        self._mixin_ts = time.time()
        return self._mixin_key

    async def _wbi_json(self, url: str, params: dict) -> dict:
        mk = await self._wbi()
        p = dict(params)
        p["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + mk).encode()).hexdigest()
        return await self._json(url, {k: str(v) for k, v in sorted(p.items())})

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="bili_hot")
    async def bili_hot(self, event: AstrMessageEvent, limit: int = 5) -> str:
        """看 B站 此刻的热门视频榜。想知道 B站 现在在流行什么、找选题、随便刷刷时用它。

        Args:
            limit(number): 返回几条，1-10，默认 5
        """
        try:
            n = max(1, min(10, int(limit or 5)))
            d = await self._json(
                "https://api.bilibili.com/x/web-interface/popular",
                {"ps": n, "pn": 1},
            )
            items = (d.get("data") or {}).get("list") or []
            if not items:
                return "热门榜暂时拿不到（可能被风控）。"
            lines = []
            for i, v in enumerate(items[:n], 1):
                lines.append(
                    f"{i}. {v.get('title')} | UP:{v.get('owner', {}).get('name')} "
                    f"| 播放:{_fmt_count(v.get('stat', {}).get('view'))} "
                    f"| 弹幕:{_fmt_count(v.get('stat', {}).get('danmaku'))} "
                    f"| {_fmt_dur(v.get('duration'))} | {v.get('bvid')} "
                    f"| https://www.bilibili.com/video/{v.get('bvid')}"
                )
            return "B站热门榜：\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"bili_hot failed: {e}")
            return f"刷热门榜失败：{e}"

    @filter.llm_tool(name="bili_search")
    async def bili_search(
        self, event: AstrMessageEvent, keyword: str, limit: int = 5
    ) -> str:
        """按关键词搜 B站 视频，返回标题/UP主/播放量/BV号。想看某个话题下有什么视频时用它。

        Args:
            keyword(string): 搜索关键词
            limit(number): 返回几条，1-10，默认 5
        """
        try:
            n = max(1, min(10, int(limit or 5)))
            d = await self._wbi_json(
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                {"search_type": "video", "keyword": keyword, "page": 1, "page_size": n},
            )
            items = (d.get("data") or {}).get("result") or []
            if not items:
                return f"没搜到「{keyword}」的视频。"
            lines = []
            for i, v in enumerate(items[:n], 1):
                title = (
                    str(v.get("title", ""))
                    .replace('<em class="keyword">', "")
                    .replace("</em>", "")
                )
                lines.append(
                    f"{i}. {title} | UP:{v.get('author')} | 播放:{_fmt_count(v.get('play'))} "
                    f"| {_fmt_dur(v.get('duration'))} | {v.get('bvid')} "
                    f"| https://www.bilibili.com/video/{v.get('bvid')}"
                )
            return f"「{keyword}」的搜索结果：\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"bili_search failed: {e}")
            return f"搜索失败：{e}"

    @filter.llm_tool(name="bili_video")
    async def bili_video(self, event: AstrMessageEvent, bvid: str) -> str:
        """看某个 B站 视频的详细信息（标题、简介、UP主、播放/点赞、分P）。

        Args:
            bvid(string): 视频的 BV 号，例如 BV1GJ411x7h7
        """
        try:
            d = await self._json(
                "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid.strip()}
            )
            if d.get("code") != 0:
                return f"查不到这个视频：{d.get('message')}"
            v = d["data"]
            desc = (v.get("desc") or "").strip().replace("\n", " ")
            if len(desc) > 300:
                desc = desc[:300] + "…"
            st = v.get("stat") or {}
            return (
                f"标题：{v.get('title')}\n"
                f"UP主：{v.get('owner', {}).get('name')}\n"
                f"发布：{time.strftime('%Y-%m-%d', time.localtime(v.get('pubdate', 0)))}\n"
                f"数据：播放 {_fmt_count(st.get('view'))} / 点赞 {_fmt_count(st.get('like'))} "
                f"/ 投币 {_fmt_count(st.get('coin'))} / 收藏 {_fmt_count(st.get('favorite'))} "
                f"/ 弹幕 {_fmt_count(st.get('danmaku'))} / 评论 {_fmt_count(st.get('reply'))}\n"
                f"时长：{_fmt_dur(v.get('duration'))}，分P：{len(v.get('pages') or [])}\n"
                f"简介：{desc or '（无）'}\n"
                f"链接：https://www.bilibili.com/video/{v.get('bvid')}"
            )
        except Exception as e:
            logger.warning(f"bili_video failed: {e}")
            return f"查视频失败：{e}"

    @filter.llm_tool(name="bili_subtitle")
    async def bili_subtitle(
        self, event: AstrMessageEvent, bvid: str, max_chars: int = 2500
    ) -> str:
        """取某个 B站 视频的 AI 字幕原文，用来判断视频到底讲了什么。

        Args:
            bvid(string): 视频的 BV 号
            max_chars(number): 最多返回多少字，默认 2500
        """
        try:
            p = await self._json(
                "https://api.bilibili.com/x/player/pagelist", {"bvid": bvid.strip()}
            )
            pages = p.get("data") or []
            if not pages:
                return "拿不到分P信息，视频可能不存在。"
            cid = pages[0].get("cid")
            d = await self._wbi_json(
                "https://api.bilibili.com/x/player/wbi/v2",
                {"bvid": bvid.strip(), "cid": cid},
            )
            subs = ((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not subs:
                return "这个视频没有字幕（未开启 AI 字幕或 UP 没传）。"
            sub = subs[0]
            body = await self._json(
                sub.get("subtitle_url")
                if str(sub.get("subtitle_url", "")).startswith("http")
                else "https:" + str(sub.get("subtitle_url"))
            )
            text = "".join(
                str(x.get("content", "")) for x in (body.get("body") or [])
            ).strip()
            if not text:
                return "字幕是空的。"
            cap = max(200, int(max_chars or 2500))
            return text[:cap] + ("…（已截断）" if len(text) > cap else "")
        except Exception as e:
            logger.warning(f"bili_subtitle failed: {e}")
            return f"取字幕失败：{e}"

    @filter.llm_tool(name="bili_at_me")
    async def bili_at_me(self, event: AstrMessageEvent, text: str) -> str:
        """主动给当前会话发一条消息；在群里会 @ 发消息的人。适合把刷到的好东西推给他。

        Args:
            text(string): 要发送的内容
        """
        try:
            uid = event.get_sender_id()
            chain = []
            group_id = getattr(getattr(event, "message_obj", None), "group_id", None)
            if group_id:
                chain.append(At(qq=uid))
                chain.append(Plain(" " + text))
            else:
                chain.append(Plain(text))
            ok = await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=chain)
            )
            return "已发送。" if ok else "没找到对应平台，发送失败。"
        except Exception as e:
            logger.warning(f"bili_at_me failed: {e}")
            return f"发送失败：{e}"


    @filter.llm_tool(name="douyin_hot")
    async def douyin_hot(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """看抖音此刻的热搜榜（含热度值、相关视频数）。想知道抖音现在在讨论什么时用它。

        Args:
            limit(number): 返回几条，1-20，默认 8
        """
        try:
            n = max(1, min(20, int(limit or 8)))
            d = await self._json(
                "https://www.douyin.com/aweme/v1/web/hot/search/list/",
                {
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "detail_list": "1",
                },
                headers={"Referer": "https://www.douyin.com/"},
            )
            words = ((d.get("data") or {}).get("word_list")) or []
            if not words:
                return "抖音热榜暂时拿不到（可能被风控）。"
            lines = []
            for i, w in enumerate(words[:n], 1):
                lines.append(
                    f"{i}. {w.get('word')} | 热度:{_fmt_count(w.get('hot_value'))} "
                    f"| 视频:{_fmt_count(w.get('discuss_video_count'))}"
                )
            return "抖音热搜榜：\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"douyin_hot failed: {e}")
            return f"刷抖音热榜失败：{e}"


    @filter.llm_tool(name="qq_set_avatar")
    async def qq_set_avatar(self, event: AstrMessageEvent, image: str) -> str:
        """给自己换 QQ 头像。走 OneBot 的 set_qq_avatar，不需要 NapCat 开 HTTP 接口。

        Args:
            image(string): 图片地址，可以是 http/https 链接，也可以是本机绝对路径
        """
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前平台拿不到 OneBot 客户端，换不了头像。"
            res = await bot.call_action("set_qq_avatar", file=image)
            return f"头像已提交给 NapCat：{res}"
        except Exception as e:
            logger.warning(f"qq_set_avatar failed: {e}")
            return f"换头像失败：{e}"

    @filter.llm_tool(name="qq_set_nickname")
    async def qq_set_nickname(self, event: AstrMessageEvent, nickname: str) -> str:
        """给自己改 QQ 昵称。走 OneBot 的 set_qq_profile。

        Args:
            nickname(string): 新的昵称
        """
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前平台拿不到 OneBot 客户端，改不了昵称。"
            res = await bot.call_action("set_qq_profile", nickname=nickname)
            return f"昵称已提交给 NapCat：{res}"
        except Exception as e:
            logger.warning(f"qq_set_nickname failed: {e}")
            return f"改昵称失败：{e}"


    @filter.llm_tool(name="qq_api")
    async def qq_api(self, event: AstrMessageEvent, action: str, params: str = "{}") -> str:
        """调用任意 OneBot v11 接口，等于直接支配这个 QQ 号。比如 send_private_msg、send_group_msg、get_friend_list、get_group_list、set_group_card、delete_msg、set_qq_avatar。

        Args:
            action(string): 接口名，例如 send_group_msg
            params(string): JSON 格式的参数字符串，例如 {"group_id":123,"message":[{"type":"text","data":{"text":"hi"}}]}
        """
        try:
            p = json.loads(params) if str(params).strip() else {}
            if not isinstance(p, dict):
                return "params 必须是 JSON 对象。"
        except Exception as e:
            return "params 不是合法 JSON：%s" % e
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前拿不到 OneBot 客户端。"
        try:
            res = await bot.call_action(str(action), **p)
            s = json.dumps(res, ensure_ascii=False)
            return "%s 成功：%s" % (action, s[:900])
        except Exception as e:
            return "%s 调用失败：%s" % (action, str(e)[:300])

    @filter.llm_tool(name="qq_send")
    async def qq_send(
        self, event: AstrMessageEvent, text: str, user_id: str = "", group_id: str = ""
    ) -> str:
        """主动给某个 QQ 好友或群发消息。不填 user_id/group_id 就发给当前会话。

        Args:
            text(string): 要发送的文字
            user_id(string): 好友 QQ 号，可空
            group_id(string): 群号，可空
        """
        chain = [{"type": "text", "data": {"text": text}}]
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前拿不到 OneBot 客户端。"
        try:
            if str(group_id).strip():
                res = await bot.call_action(
                    "send_group_msg", group_id=int(str(group_id).strip()), message=chain
                )
            elif str(user_id).strip():
                res = await bot.call_action(
                    "send_private_msg", user_id=int(str(user_id).strip()), message=chain
                )
            else:
                ok = await self.context.send_message(
                    event.unified_msg_origin, MessageChain(chain=[Plain(text)])
                )
                return "已发到当前会话。" if ok else "发送失败：找不到对应平台。"
            return "已发送：%s" % json.dumps(res, ensure_ascii=False)[:300]
        except Exception as e:
            return "发送失败：%s" % str(e)[:300]


    @filter.llm_tool(name="qzone_post")
    async def qzone_post(
        self,
        event: AstrMessageEvent,
        content: str,
        images: str = "",
        ugc_right: int = 1,
    ) -> str:
        """发一条 QQ空间说说（可以带图）。想分享心情、好玩的事时用它。

        Args:
            content(string): 说说正文
            images(string): 图片地址，多个用逗号分隔，支持 http(s):// file:// base64://，可留空
            ugc_right(number): 可见范围 1 所有人 4 好友 64 仅自己，默认 1
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前拿不到 OneBot 客户端。"
        payload = {"content": content, "ugc_right": int(ugc_right or 1)}
        imgs = [x.strip() for x in str(images or "").split(",") if x.strip()]
        if imgs:
            payload["images"] = imgs
        try:
            res = await bot.call_action("send_qzone_msg", **payload)
            return "说说已发出：%s" % json.dumps(res, ensure_ascii=False)[:400]
        except Exception as e:
            return "发说说失败：%s" % str(e)[:300]

    # ---------- 命令兜底 ----------
    @filter.command("bili")
    async def bili_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        """ /bili 关键词 —— 不带工具调用的纯命令版搜索 """
        if not keyword.strip():
            yield event.plain_result(await self.bili_hot(event, 5))
            return
        yield event.plain_result(await self.bili_search(event, keyword.strip(), 5))
