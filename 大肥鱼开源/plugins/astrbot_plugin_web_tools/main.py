import asyncio
import json
import os
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

PROFILE_DIR = r"D:\NapCat\browser_profile"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class Main(star.Star):
    """真上网：本机 Edge 常驻浏览器，能搜索、跳转、点击、输入、滚动、开标签页。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._pw = None
        self._browser = None
        self._ctx = None
        self._pages = []
        self._idx = 0
        self._lock = asyncio.Lock()

    # ---------- 基础设施 ----------
    def _data_dir(self) -> str:
        base = None
        try:
            base = self.context.get_data_dir()
        except Exception:
            base = None
        if not base:
            base = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(base, exist_ok=True)
        return base

    def _last_path(self) -> str:
        return os.path.join(self._data_dir(), "last_url.json")

    def _save_last(self, url: str) -> None:
        try:
            with open(self._last_path(), "w", encoding="utf-8") as f:
                json.dump({"url": url, "at": time.time()}, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_last(self) -> str:
        try:
            with open(self._last_path(), "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("url") or ""
        except Exception:
            return ""

    async def _ensure(self):
        if self._pages:
            page = self._pages[self._idx]
            if not page.is_closed():
                return page
            self._pages.pop(self._idx)
            self._idx = max(0, min(self._idx, len(self._pages) - 1))
            if self._pages:
                return self._pages[self._idx]
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._ctx is None:
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    channel="msedge",
                    headless=True,
                    locale="zh-CN",
                    user_agent=UA,
                    viewport={"width": 1366, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as e:
                # 登录窗口正占用同一个 profile 时，退回到临时配置，至少还能看公开页面
                logger.warning("persistent profile busy (%s), fallback to temp profile" % e)
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR + "_tmp",
                    channel="msedge",
                    headless=True,
                    locale="zh-CN",
                    user_agent=UA,
                    viewport={"width": 1366, "height": 900},
                )
        page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._pages.append(page)
        self._idx = len(self._pages) - 1
        return page

    async def _read(self, page, limit: int) -> str:
        try:
            t = await page.inner_text("body")
        except Exception:
            t = ""
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{2,}", "\n", t).strip()
        if not t:
            return "（这页没有可读文字，可能是纯图片或需要登录）"
        cut = t[:limit]
        if len(t) > limit:
            cut += "\n…（已截断，整页共 %d 字，可以调大 limit 或用 web_scroll 继续）" % len(t)
        return cut

    # ---------- 工具 ----------
    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str, limit: int = 2000) -> str:
        """用 Bing 搜一下。想了解任何事都从这一步开始。

        Args:
            query(string): 搜索词
            limit(number): 最多返回多少字，默认 2000
        """
        try:
            async with self._lock:
                page = await self._ensure()
                await page.goto(
                    "https://cn.bing.com/search?q=" + str(query),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(1400)
                body = await self._read(page, max(200, int(limit or 2000)))
                return "搜索「%s」：\n%s" % (query, body)
        except Exception as e:
            logger.warning("web_search failed: %s" % e)
            return "搜索失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_open")
    async def web_open(self, event: AstrMessageEvent, url: str, limit: int = 2500) -> str:
        """打开一个网址，读正文。想细看某条结果时用它。

        Args:
            url(string): 完整网址，http/https 开头
            limit(number): 最多返回多少字，默认 2500
        """
        try:
            async with self._lock:
                page = await self._ensure()
                if not str(url).startswith("http"):
                    url = "https://" + str(url)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1100)
                self._save_last(page.url)
                return "标题：%s\n地址：%s\n正文：\n%s" % (
                    await page.title(),
                    page.url,
                    await self._read(page, max(200, int(limit or 2500))),
                )
        except Exception as e:
            return "打开失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_click")
    async def web_click(self, event: AstrMessageEvent, label: str, limit: int = 2000) -> str:
        """在当前页面按可见文字点一下（进详情、翻页、看下一条）。

        Args:
            label(string): 要点的文字，比如「下一页」「评论」「展开」
            limit(number): 点击后返回多少字，默认 2000
        """
        try:
            async with self._lock:
                page = await self._ensure()
                await page.get_by_text(str(label), exact=False).first.click(timeout=8000)
                await page.wait_for_timeout(1400)
                self._save_last(page.url)
                return "点「%s」之后：\n标题：%s\n地址：%s\n正文：\n%s" % (
                    label,
                    await page.title(),
                    page.url,
                    await self._read(page, max(200, int(limit or 2000))),
                )
        except Exception as e:
            return "点击失败（页面上可能没有这段文字）：%s" % str(e)[:200]

    @filter.llm_tool(name="web_type")
    async def web_type(
        self, event: AstrMessageEvent, text: str, target: str = "", submit: bool = True
    ) -> str:
        """在搜索框/输入框里打字并回车。用来在站内搜索。

        Args:
            text(string): 要输入的内容
            target(string): 输入框的提示文字或标签，留空则用页面里第一个输入框
            submit(boolean): 打完是否回车，默认 true
        """
        try:
            async with self._lock:
                page = await self._ensure()
                box = None
                if str(target).strip():
                    for loc in (
                        page.get_by_placeholder(str(target), exact=False),
                        page.get_by_label(str(target), exact=False),
                    ):
                        try:
                            if await loc.count() > 0:
                                box = loc.first
                                break
                        except Exception:
                            continue
                if box is None:
                    box = page.locator(
                        "input[type=text],input[type=search],textarea,"
                        "input:not([type]),[role=searchbox]"
                    ).first
                await box.click(timeout=6000)
                await box.fill("")
                await box.type(str(text), delay=40)
                if submit:
                    before = page.url
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(1600)
                    if page.url == before:
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(1800)
                self._save_last(page.url)
                return "输入「%s」之后：\n地址：%s\n正文：\n%s" % (
                    text,
                    page.url,
                    await self._read(page, 2200),
                )
        except Exception as e:
            return "输入失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_scroll")
    async def web_scroll(self, event: AstrMessageEvent, times: int = 3, limit: int = 2200) -> str:
        """往下滚几屏，把新的内容读出来（信息流、长文章用）。

        Args:
            times(number): 滚几次，默认 3
            limit(number): 滚动后返回多少字，默认 2200
        """
        try:
            async with self._lock:
                page = await self._ensure()
                for _ in range(max(1, min(15, int(times or 3)))):
                    await page.mouse.wheel(0, 900)
                    await page.wait_for_timeout(700)
                return "往下滚了 %s 屏：\n%s" % (
                    times,
                    await self._read(page, max(300, int(limit or 2200))),
                )
        except Exception as e:
            return "滚动失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_back")
    async def web_back(self, event: AstrMessageEvent, limit: int = 2000) -> str:
        """退回上一页。

        Args:
            limit(number): 返回多少字，默认 2000
        """
        try:
            async with self._lock:
                page = await self._ensure()
                await page.go_back(wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(900)
                self._save_last(page.url)
                return "退回上一页：\n标题：%s\n地址：%s\n正文：\n%s" % (
                    await page.title(),
                    page.url,
                    await self._read(page, max(200, int(limit or 2000))),
                )
        except Exception as e:
            return "返回失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_tab")
    async def web_tab(
        self, event: AstrMessageEvent, action: str = "new", url: str = "", index: int = -1
    ) -> str:
        """管理标签页：new 开新标签（可带 url）、list 列出现有标签、switch 切换、close 关闭。

        Args:
            action(string): new / list / switch / close
            url(string): action=new 时要打开的网址，可留空
            index(number): switch/close 时的标签序号（从 0 开始）
        """
        try:
            async with self._lock:
                act = str(action or "new").strip().lower()
                if act == "list":
                    if not self._pages:
                        return "现在没有打开的标签页。"
                    out = []
                    for i, p in enumerate(self._pages):
                        try:
                            out.append("%d.%s %s" % (i, "*" if i == self._idx else " ", (p.url or "about:blank")[:120]))
                        except Exception:
                            out.append("%d. ?" % i)
                    return "标签页：\n" + "\n".join(out)
                if act == "switch":
                    if 0 <= int(index) < len(self._pages):
                        self._idx = int(index)
                        p = self._pages[self._idx]
                        return "切到第 %d 个：%s\n%s" % (
                            self._idx,
                            p.url,
                            await self._read(p, 1800),
                        )
                    return "序号不对，现有 %d 个标签。" % len(self._pages)
                if act == "close":
                    if 0 <= int(index) < len(self._pages):
                        await self._pages[int(index)].close()
                        return "关掉了第 %d 个。" % int(index)
                    return "序号不对。"
                # new
                page = await self._ensure()
                if str(url).strip():
                    u = str(url).strip()
                    if not u.startswith("http"):
                        u = "https://" + u
                    await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(1100)
                    self._save_last(page.url)
                return "新标签 %d：%s\n%s" % (
                    self._idx,
                    page.url,
                    await self._read(page, 2000),
                )
        except Exception as e:
            return "标签操作失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_read")
    async def web_read(self, event: AstrMessageEvent, limit: int = 2500) -> str:
        """再读一遍当前页面（比如刚才截断了）。

        Args:
            limit(number): 最多返回多少字，默认 2500
        """
        try:
            async with self._lock:
                page = await self._ensure()
                return "当前页面：%s\n%s" % (page.url, await self._read(page, max(200, int(limit or 2500))))
        except Exception as e:
            return "读取失败：%s" % str(e)[:200]

    @filter.llm_tool(name="web_screenshot")
    async def web_screenshot(self, event: AstrMessageEvent) -> str:
        """截当前页面一张图，存到本机，返回路径。"""
        try:
            async with self._lock:
                page = await self._ensure()
                d = os.path.join(r"D:\大肥鱼的文件夹", "图片", "网页截图")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "shot_%d.png" % int(time.time()))
                await page.screenshot(path=path, full_page=False)
                return "截图：%s（%s）" % (path, page.url)
        except Exception as e:
            return "截图失败：%s" % str(e)[:200]

    # ---------- B站写操作（用登录态调接口） ----------
    async def _bili_post(self, path: str, data: dict) -> dict:
        page = await self._ensure()
        if "bilibili.com" not in (page.url or ""):
            await page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)
        js = """async (args) => {
            const csrf = (document.cookie.match(/bili_jct=([^;]+)/) || [])[1] || '';
            const body = new URLSearchParams(args.data);
            body.set('csrf', csrf);
            const r = await fetch(args.url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
            return await r.text();
        }"""
        try:
            txt = await page.evaluate(js, {"url": "https://api.bilibili.com" + path, "data": data})
            return json.loads(txt)
        except Exception as e:
            return {"code": -999, "message": "请求失败: %s" % str(e)[:150]}

    async def _bili_aid(self, bvid: str) -> int:
        page = await self._ensure()
        js = """async (bvid) => {
            const r = await fetch('https://api.bilibili.com/x/web-interface/view?bvid=' + bvid, {credentials:'include'});
            const d = await r.json();
            return (d && d.data && d.data.aid) ? String(d.data.aid) : '';
        }"""
        try:
            aid = await page.evaluate(js, str(bvid))
            return int(aid) if aid else 0
        except Exception:
            return 0

    @filter.llm_tool(name="bili_like")
    async def bili_like(self, event: AstrMessageEvent, bvid: str, like: bool = True) -> str:
        """给B站视频点赞或取消点赞（用你自己的账号）。

        Args:
            bvid(string): 视频 BV 号
            like(boolean): true 点赞，false 取消，默认 true
        """
        try:
            async with self._lock:
                d = await self._bili_post(
                    "/x/web-interface/archive/like",
                    {"bvid": str(bvid).strip(), "like": 1 if like else 2},
                )
            return "点赞成功（%s）" % bvid if d.get("code") == 0 else "没点成：%s" % d.get("message")
        except Exception as e:
            return "点赞失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_coin")
    async def bili_coin(
        self, event: AstrMessageEvent, bvid: str, count: int = 1, also_like: bool = True
    ) -> str:
        """给B站视频投币。

        Args:
            bvid(string): 视频 BV 号
            count(number): 投几个币，1 或 2，默认 1
            also_like(boolean): 顺便点赞，默认 true
        """
        try:
            async with self._lock:
                d = await self._bili_post(
                    "/x/web-interface/coin/add",
                    {
                        "bvid": str(bvid).strip(),
                        "multiply": max(1, min(2, int(count or 1))),
                        "select_like": 1 if also_like else 0,
                    },
                )
            return "投币成功（%s）" % bvid if d.get("code") == 0 else "没投成：%s" % d.get("message")
        except Exception as e:
            return "投币失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_reply")
    async def bili_reply(self, event: AstrMessageEvent, bvid: str, text: str) -> str:
        """在B站视频下面发一条评论（用自己的账号，谨慎，会公开可见）。

        Args:
            bvid(string): 视频 BV 号
            text(string): 评论内容
        """
        try:
            async with self._lock:
                aid = await self._bili_aid(bvid)
                if not aid:
                    return "拿不到这个视频的 aid。"
                d = await self._bili_post(
                    "/x/v2/reply/add",
                    {"oid": aid, "type": 1, "message": str(text)[:900]},
                )
            return "评论已发出" if d.get("code") == 0 else "评论失败：%s" % d.get("message")
        except Exception as e:
            return "评论失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_follow")
    async def bili_follow(self, event: AstrMessageEvent, uid: str, follow: bool = True) -> str:
        """关注或取关一个B站UP主。

        Args:
            uid(string): UP主的 UID（数字）
            follow(boolean): true 关注，false 取关，默认 true
        """
        try:
            async with self._lock:
                d = await self._bili_post(
                    "/x/relation/modify",
                    {"fid": str(uid).strip(), "act": 1 if follow else 2, "re_src": 11},
                )
            return "已关注 %s" % uid if d.get("code") == 0 else "没成功：%s" % d.get("message")
        except Exception as e:
            return "关注失败：%s" % str(e)[:150]


    async def _bili_cookies(self) -> dict:
        page = await self._ensure()
        cs = await page.context.cookies()
        return {c["name"]: c["value"] for c in cs}

    async def _bili_post_py(self, path: str, data: dict) -> dict:
        import aiohttp

        jar = await self._bili_cookies()
        body = dict(data)
        body.setdefault("csrf", jar.get("bili_jct", ""))
        try:
            async with aiohttp.ClientSession(cookies=jar) as s:
                async with s.post(
                    "https://api.bilibili.com" + path,
                    data=body,
                    headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    txt = await r.text()
            return json.loads(txt)
        except Exception as e:
            return {"code": -999, "message": "请求失败: %s" % str(e)[:150]}

    async def _bili_avatar_upload(self, raw: bytes) -> dict:
        import aiohttp

        jar = await self._bili_cookies()
        form = aiohttp.FormData()
        form.add_field("file", raw, filename="avatar.png", content_type="image/png")
        try:
            async with aiohttp.ClientSession(cookies=jar) as s:
                async with s.post(
                    "https://api.bilibili.com/x/member/web/face/upload",
                    params={"csrf": jar.get("bili_jct", "")},
                    data=form,
                    headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as r:
                    txt = await r.text()
            return json.loads(txt)
        except Exception as e:
            return {"code": -999, "message": "上传失败: %s" % str(e)[:150]}

    async def _bili_get(self, path: str) -> dict:
        page = await self._ensure()
        if "bilibili.com" not in (page.url or ""):
            await page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(800)
        js = """async (u) => { const r = await fetch(u, {credentials:'include'}); return await r.text(); }"""
        try:
            txt = await page.evaluate(js, "https://api.bilibili.com" + path)
            return json.loads(txt)
        except Exception as e:
            return {"code": -999, "message": str(e)[:120]}

    async def _bili_my_mid(self) -> int:
        d = await self._bili_get("/x/web-interface/nav")
        try:
            return int((d.get("data") or {}).get("mid") or 0)
        except Exception:
            return 0

    async def _bili_fav_folder(self) -> str:
        mid = await self._bili_my_mid()
        d = await self._bili_get("/x/v3/fav/folder/created/list-all?up_mid=%d" % mid)
        lst = ((d.get("data") or {}).get("list")) or []
        return str(lst[0]["id"]) if lst else ""

    @filter.llm_tool(name="bili_favorite")
    async def bili_favorite(self, event: AstrMessageEvent, bvid: str) -> str:
        """把B站视频收藏进默认收藏夹（用自己的账号）。

        Args:
            bvid(string): 视频 BV 号
        """
        try:
            async with self._lock:
                aid = await self._bili_aid(bvid)
                folder = await self._bili_fav_folder()
                if not aid:
                    return "拿不到这个视频的 aid。"
                if not folder:
                    return "你还没有收藏夹，先去 B站建一个。"
                d = await self._bili_post(
                    "/x/v3/fav/resource/deal",
                    {"rid": aid, "type": 2, "add_media_ids": folder},
                )
            return "收藏好了（%s）" % bvid if d.get("code") == 0 else "没收藏成：%s" % d.get("message")
        except Exception as e:
            return "收藏失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_danmaku")
    async def bili_danmaku(
        self, event: AstrMessageEvent, bvid: str, text: str, progress: int = 5
    ) -> str:
        """在B站视频上发一条弹幕。

        Args:
            bvid(string): 视频 BV 号
            text(string): 弹幕内容（短一点）
            progress(number): 出现在第几秒，默认 5
        """
        try:
            async with self._lock:
                pg = await self._bili_get("/x/player/pagelist?bvid=%s" % str(bvid).strip())
                pages = pg.get("data") or []
                if not pages:
                    return "拿不到这个视频的分P。"
                cid = pages[0].get("cid")
                d = await self._bili_post(
                    "/x/v2/dm/post",
                    {
                        "type": 1, "oid": cid, "msg": str(text)[:90],
                        "progress": max(0, int(progress or 5)) * 1000,
                        "color": 16777215, "fontsize": 25, "pool": 0, "mode": 1, "plat": 1,
                    },
                )
            return "弹幕发出去了" if d.get("code") == 0 else "发弹幕失败：%s" % d.get("message")
        except Exception as e:
            return "发弹幕失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_sign")
    async def bili_sign(self, event: AstrMessageEvent, text: str) -> str:
        """改自己B站账号的个性签名。

        Args:
            text(string): 新签名
        """
        try:
            async with self._lock:
                d = await self._bili_post("/x/member/web/sign/update", {"user_sign": str(text)[:70]})
            return "签名改好了" if d.get("code") == 0 else "改签名失败：%s" % d.get("message")
        except Exception as e:
            return "改签名失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_set_name")
    async def bili_set_name(self, event: AstrMessageEvent, name: str) -> str:
        """改自己B站账号的昵称（B站有名额/冷却限制，改不动会告诉你原因）。

        Args:
            name(string): 新昵称
        """
        try:
            async with self._lock:
                d = await self._bili_post("/x/member/web/update", {"uname": str(name)[:20]})
                if d.get("code") == 0:
                    return "昵称改好了：%s" % name
                d2 = await self._bili_post("/x/member/web/update", {"name": str(name)[:20]})
                if d2.get("code") == 0:
                    return "昵称改好了：%s" % name
                return "改昵称失败：%s" % (d.get("message") or d2.get("message"))
        except Exception as e:
            return "改昵称失败：%s" % str(e)[:150]

    @filter.llm_tool(name="bili_set_avatar")
    async def bili_set_avatar(self, event: AstrMessageEvent, image: str) -> str:
        """换自己B站账号的头像（走网页上传框，和真人操作一样）。

        Args:
            image(string): 图片地址（http/https）或本机绝对路径，建议 180x180 以上的正方形，小于 2MB
        """
        try:
            import urllib.request

            if str(image).startswith("http"):
                req = urllib.request.Request(str(image), headers={"User-Agent": UA})
                raw = urllib.request.urlopen(req, timeout=30).read()
            else:
                raw = open(str(image), "rb").read()
            if len(raw) > 2 * 1024 * 1024:
                return "图片超过 2MB，B站 传不上去，换一张小点的。"
            tmp = os.path.join(os.environ.get("TEMP", "."), "bili_avatar_%d.png" % int(time.time()))
            with open(tmp, "wb") as f:
                f.write(raw)
            async with self._lock:
                page = await self._ensure()
                await page.goto("https://account.bilibili.com/account/face/vip",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                await page.get_by_text("更换头像", exact=False).first.click(timeout=8000)
                await page.wait_for_timeout(2000)
                await page.locator("input[type=file]").first.set_input_files(tmp)
                await page.wait_for_timeout(4000)
                # 试着点“确定/保存”（有些版本会弹出来）
                clicked = ""
                for label in ("确定", "保存", "确认", "上传"):
                    try:
                        loc = page.get_by_text(label, exact=True)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=4000)
                            clicked = label
                            break
                    except Exception:
                        continue
                await page.wait_for_timeout(2500)
                body = await page.inner_text("body")
            tail = body[-400:].replace("\n", " ")
            if clicked:
                return "头像已提交（点了「%s」）。页面返回：%s" % (clicked, tail[:160])
            return ("已经把图片选进B站的更换头像框了，但没找到“确定”按钮"
                    "（B站这步可能是图片裁剪控件，程序点不到）。你手动点一下确定就行。")
        except Exception as e:
            return "换头像失败：%s" % str(e)[:200]


