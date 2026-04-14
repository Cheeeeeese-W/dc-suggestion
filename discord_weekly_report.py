"""
Discord 社区监控工具 V2 — 日报升级
基于原有 discord_weekly_report.py 改造

V2 改动：
- 调度改为每日（去掉 10:30 补偿检查）
- 飞书多维表格（Bitable）作为唯一数据源，帖子去重 + 增量判断
- 拉取范围：活跃帖子 + 归档帖子，thread_id 去重
- 三层过滤漏斗：规则过滤 → AI 预筛 → 深度分析
- 增量分析：reply_count 变化才重新分析，silent_days >= 3 跳过
- 满意度重定义：1-10（越高越满意）
- 热度公式：消息数×1 + 反应数×1 + 参与人数×3，满意度≤3 热度×2
- AI 分析取前 10 条消息（跳过 bot），每条截前 200 字符
- 日报卡片：按帖子独立展示，分「新发现」和「有显著变化」两个区
- 飞书多维表格：新帖子 batch_create，旧帖子 batch_update
"""

import asyncio
import os
import json
import re
import discord
import requests
from openai import AsyncOpenAI
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ===================== 1. 配置中心 =====================
def get_conf():
    return {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
        "CHANNEL_ID": int(os.getenv("DISCORD_CHANNEL_ID")),
        "AI_API_KEY": os.getenv("AI_API_KEY"),
        "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://api.999789.best/v1"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_APP_ID": os.getenv("FEISHU_APP_ID"),
        "FEISHU_APP_SECRET": os.getenv("FEISHU_APP_SECRET"),
        "BITABLE_TOKEN": os.getenv("FEISHU_BITABLE_APP_TOKEN"),
        "BITABLE_TABLE_ID": os.getenv("FEISHU_BITABLE_TABLE_ID"),
        "REFERENCE_TABLE_ID": os.getenv("FEISHU_REFERENCE_TABLE_ID"),
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID"),
        "FEISHU_SEND_MODE": os.getenv("FEISHU_SEND_MODE", "prod"),
        "FEISHU_TEST_RECEIVE_ID": os.getenv("FEISHU_TEST_RECEIVE_ID"),
        "FEISHU_TEST_RECEIVE_ID_TYPE": os.getenv("FEISHU_TEST_RECEIVE_ID_TYPE", "open_id"),
    }

CONF = get_conf()

# ===================== 2. 飞书 API 客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={
                "app_id": CONF["FEISHU_APP_ID"],
                "app_secret": CONF["FEISHU_APP_SECRET"]
            }, timeout=10)
            return res.json().get("tenant_access_token")
        except Exception:
            return None

    # ---- 知识库 ----
    def get_knowledge_base(self):
        """从参考表读取 模块-二级分类-关键字 映射"""
        if not self.token:
            return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get("data", {}).get("items", [])
            kb = []
            for r in items:
                f = r["fields"]
                if f.get("模块分类") and f.get("二级分类"):
                    cat1 = f.get("模块分类")
                    cat2 = f.get("二级分类")
                    raw_kw = str(f.get("关键字", "")).replace("，", ",")
                    kw_list = [k.strip() for k in raw_kw.split(",") if k.strip()]
                    if not kw_list:
                        kw_list = [str(cat2).strip()]
                    kb.append({"cat1": cat1, "cat2": cat2, "keywords": kw_list})
            return kb
        except Exception:
            return []

    def add_new_reference(self, new_records):
        if not self.token or not new_records:
            return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        records = [{"fields": {"模块分类": p[0], "二级分类": p[1], "关键字": ""}} for p in new_records]
        try:
            res = requests.post(url, headers=headers, json={"records": records}, timeout=10)
            resp = res.json()
            if resp.get("code") == 0:
                print(f"📝 术语库新增 {len(records)} 条: {[f'{p[0]}-{p[1]}' for p in new_records]}")
            else:
                print(f"❌ 术语库新增失败: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            print(f"❌ 术语库新增异常: {e}")

    # ---- Bitable 主表 ----
    def _extract_thread_id_from_url(self, link_obj):
        """从帖子链接 URL 中提取 thread_id"""
        if isinstance(link_obj, dict):
            url = link_obj.get("link", "")
        elif isinstance(link_obj, str):
            url = link_obj
        else:
            return None
        m = re.search(r"/channels/\d+/(\d+)", url)
        return m.group(1) if m else None

    def query_bitable_existing(self) -> dict:
        """
        查询主表中所有已有记录，从帖子链接提取 thread_id。
        返回 {thread_id: {record_id, reply_count, sentiment, participant_count,
                          heat_score, category, sub_category, summary, short_title}}
        """
        if not self.token:
            return {}
        def _parse_int(val, default=0):
            if val is None:
                return default
            try:
                return int(float(val))
            except (ValueError, TypeError):
                return default

        result = {}
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        page_token = None
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            try:
                res = requests.get(url, headers=headers, params=params, timeout=15)
                data = res.json().get("data", {})
                for item in data.get("items", []):
                    fields = item.get("fields", {})
                    link = fields.get("帖子链接")
                    tid = self._extract_thread_id_from_url(link)
                    if tid:
                        result[tid] = {
                            "record_id": item["record_id"],
                            "reply_count": _parse_int(fields.get("回复数"), 0),
                            "sentiment": _parse_int(fields.get("满意度"), None) if fields.get("满意度") is not None else None,
                            "participant_count": _parse_int(fields.get("参与人数"), 1),
                            "heat_score": _parse_int(fields.get("热度分"), 0),
                            "category": str(fields.get("模块分类", "")),
                            "sub_category": str(fields.get("二级分类", "")),
                            "summary": str(fields.get("AI核心总结", "")),
                            "short_title": str(fields.get("AI短标题", "")),
                        }
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token")
            except Exception as e:
                print(f"⚠️ Bitable 查询异常: {e}")
                break
        print(f"📋 Bitable 已有记录: {len(result)} 条")
        return result

    def sync_bitable(self, enriched_list: list, existing: dict = None):
        """
        同步数据到飞书多维表格。
        - 新帖子（Bitable 中无记录）→ batch_create
        - 旧帖子（Bitable 中有记录）→ batch_update
        返回 {thread_id: record_id} 映射。
        """
        if not self.token or not enriched_list:
            return {}

        # 1) 查询 Bitable 中已有记录（复用外部传入的 existing 或重新查询）
        if existing is None:
            existing = self.query_bitable_existing()

        # 2) 分组：新增 vs 更新
        new_records = []
        update_records = []
        for e in enriched_list:
            tid = str(e["thread_id"])
            fields = {
                "日期": e.get("date_ms"),
                "模块分类": e.get("模块分类"),
                "二级分类": e.get("二级分类"),
                "具体建议": str(e.get("具体建议", "")),
                "AI短标题": str(e.get("AI短标题", ""))[:10],
                "热度分": int(e.get("heat_score", 0)),
                "参与人数": int(e.get("participant_count", 1)),
                "AI核心总结": str(e.get("AI核心总结", "")),
                "满意度": int(e.get("sentiment", 5)) if e.get("sentiment") is not None else 5,
                "回复数": int(e.get("reply_count", 0)),
                "帖子链接": {"text": "点击查看帖子", "link": e.get("帖子链接")},
            }
            # 变化指标（仅更新时有意义）
            sd = e.get("sentiment_delta")
            rd = e.get("reply_delta")
            if sd is not None:
                fields["满意度变化"] = int(sd)
            if rd is not None:
                fields["回复数变化"] = int(rd)
            is_new = e.get("is_new", False)
            fields["状态"] = "新发现" if is_new else "有变化"

            if tid in existing:
                fields["状态"] = "有变化"
                update_records.append({"record_id": existing[tid]["record_id"], "fields": fields})
            else:
                new_records.append({"fields": fields})

        # 3) batch_create（每批 500 条）
        if new_records:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            for i in range(0, len(new_records), 500):
                batch = new_records[i:i + 500]
                try:
                    res = requests.post(url, headers=headers, json={"records": batch}, timeout=15)
                    resp = res.json()
                    if resp.get("code") == 0:
                        created = resp.get("data", {}).get("records", [])
                        print(f"✅ Bitable 新增 {len(created)} 条")
                    else:
                        print(f"❌ Bitable batch_create 失败: {resp.get('msg')}")
                except Exception as e:
                    print(f"❌ Bitable batch_create 异常: {e}")

        # 4) batch_update（每批 500 条）
        if update_records:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_update"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            for i in range(0, len(update_records), 500):
                batch = update_records[i:i + 500]
                try:
                    res = requests.post(url, headers=headers, json={"records": batch}, timeout=15)
                    resp = res.json()
                    if resp.get("code") == 0:
                        print(f"✅ Bitable 更新 {len(batch)} 条")
                    else:
                        print(f"❌ Bitable batch_update 失败: {resp.get('msg')}")
                except Exception as e:
                    print(f"❌ Bitable batch_update 异常: {e}")

    # ---- 发送卡片 ----
    def send_group_card(self, card_content):
        if not self.token:
            return False
        mode = str(CONF.get("FEISHU_SEND_MODE", "prod")).strip().lower()
        if mode == "test":
            receive_id = CONF.get("FEISHU_TEST_RECEIVE_ID")
            receive_id_type = str(CONF.get("FEISHU_TEST_RECEIVE_ID_TYPE", "open_id")).strip() or "open_id"
            if not receive_id:
                print("⚠️ FEISHU_SEND_MODE=test 但未配置 FEISHU_TEST_RECEIVE_ID，已回退群聊发送")
                receive_id = CONF.get("FEISHU_CHAT_ID")
                receive_id_type = "chat_id"
        else:
            receive_id = CONF.get("FEISHU_CHAT_ID")
            receive_id_type = "chat_id"

        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card_content)}
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            data = res.json()
            if data.get("code") != 0:
                print(f"❌ 飞书卡片发送失败: code={data.get('code')} msg={data.get('msg')}")
                return False
            print(f"✅ 飞书卡片发送成功 (mode={mode}, receive_id_type={receive_id_type})")
            return True
        except Exception as e:
            print(f"❌ 飞书卡片发送异常: {e}")
            return False


# ===================== 4. 机器人核心逻辑 =====================
class DailyBot(discord.Client):

    # ---- 时间范围：最近 7 天（每日跑，覆盖足够窗口） ----
    def get_range(self):
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now

    # ---- 表情正负向统计 ----
    def _reaction_polarity_counts(self, msg):
        positive = 0
        negative = 0
        for r in (getattr(msg, "reactions", None) or []):
            cnt = int(getattr(r, "count", 0) or 0)
            emoji_obj = getattr(r, "emoji", None)
            emoji_raw = str(emoji_obj).lower() if emoji_obj else ""
            emoji_name = str(getattr(emoji_obj, "name", "")).lower()

            # 负向：精确匹配，避免 "-1" 误匹配 "stage-1" 等
            if "👎" in emoji_raw or emoji_name in ("thumbsdown", "thumb_down", "downvote", "-1", "minus_one"):
                negative += cnt
                continue
            # 正向：精确匹配
            if "👍" in emoji_raw or emoji_name in ("thumbsup", "thumb_up", "upvote", "+1", "plus_one"):
                positive += cnt
                continue
            if "💯" in emoji_raw or emoji_name == "100":
                positive += cnt
                continue
            if emoji_name == "up" or emoji_name.startswith("up_") or emoji_name.endswith("_up"):
                positive += cnt
                continue
        return positive, negative

    # ---- 第一层：规则过滤 ----
    def _rule_filter(self, thread_data: dict) -> bool:
        """返回 True = 通过过滤"""
        # 回复数 < 1（完全没人回的帖子跳过）
        if thread_data.get("reply_count", 0) < 1:
            return False
        # 首帖内容 < 10 字符
        content = thread_data.get("first_content", "")
        if len(content) < 10:
            return False
        # 黑名单关键词
        blacklist = ["测试", "哈哈", "dddd"]
        name_lower = thread_data.get("title", "").lower()
        content_lower = content.lower()
        for kw in blacklist:
            if kw in name_lower or kw in content_lower:
                return False
        # 负向表情 > 正向
        if thread_data.get("neg_reactions", 0) > thread_data.get("pos_reactions", 0):
            return False
        return True

    # ---- 第二层：AI 预筛（~100 token/帖） ----
    async def _ai_prescreen(self, thread_list: list, ai_client) -> set:
        """返回通过预筛的 thread_id 集合"""
        if not thread_list:
            return set()
        lines = []
        for t in thread_list:
            content_preview = t.get("first_content", "")[:50]
            lines.append(f"[{t['thread_id']}] {t['title']} | {content_preview}")
        prompt = (
            "你是 DarkWar 游戏的社区分析助手。你的任务是**筛掉垃圾帖**，不是找精品帖。\n"
            "默认判断为 yes（放行）。只有以下情况标 no：\n"
            "- 纯水帖（只有测试、哈哈、dddd、纯数字）\n"
            "- 纯表情/图片无文字\n"
            "- 纯闲聊且与游戏完全无关（天气、午饭、打招呼）\n"
            "- 明显的广告或刷屏\n"
            "其他情况一律 yes。包括但不限于：玩家讨论、抱怨、提问、建议、分享体验、情绪表达。\n"
            "记住：宁可错放一百，不要漏掉一个。\n"
            "对每个帖子回复一行：thread_id|yes/no|一句话理由\n"
            "帖子列表：\n" + "\n".join(lines)
        )
        try:
            res = await ai_client.chat.completions.create(
                model=CONF["AI_MODEL"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4000,
            )
            text = res.choices[0].message.content.strip()
            print(f"🤖 AI 预筛原始输出 (前500字): {text[:500]}")
            passed = set()
            for line in text.split("\n"):
                parts = line.strip().split("|")
                if len(parts) >= 2:
                    tid = parts[0].strip()
                    decision = parts[1].strip().lower()
                    if "yes" in decision:
                        passed.add(tid)
                else:
                    # 尝试宽松匹配：整行包含 yes/no
                    lower_line = line.lower()
                    for t_item in thread_list:
                        if str(t_item["thread_id"]) in lower_line and "yes" in lower_line:
                            passed.add(str(t_item["thread_id"]))
            print(f"🤖 解析后通过: {len(passed)} 个 (输入{len(thread_list)} 个)")
            return passed
        except Exception as e:
            print(f"⚠️ AI 预筛异常: {e}")
            # 预筛失败时放行所有帖子
            return {t["thread_id"] for t in thread_list}

    # ---- 计算热度 ----
    def _calc_heat(self, msg_count, reaction_count, participant_count, sentiment):
        """热度 = 消息数×1 + 反应数×1 + 参与人数×3，满意度≤3 热度×2"""
        heat = msg_count * 1 + reaction_count * 1 + participant_count * 3
        if sentiment <= 3:
            heat *= 2
        elif sentiment <= 5:
            heat *= 1.5
        return int(heat)

    # ---- 满意度颜色 ----
    def _sentiment_color(self, score):
        """满意度：1-10 越高越满意"""
        if score <= 3:
            return "red"
        if score <= 5:
            return "orange"
        return "green"

    def _template_by_sentiment(self, score):
        return self._sentiment_color(score)

    # ==================== 主流程 ====================
    async def on_ready(self):
        print(f"🚀 V2 日报系统就绪: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        base_url = CONF["AI_BASE_URL"].strip().rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=base_url)

        # 0. 从 Bitable 加载已有记录（去重 + 增量判断的数据源）
        bitable_existing = feishu.query_bitable_existing()

        # 1. 加载术语参考库
        kb = feishu.get_knowledge_base()
        print(f"📚 已从飞书加载 {len(kb)} 条分类映射规则")

        # 2. 拉取帖子：活跃 + 归档，去重
        start_time, end_time = self.get_range()
        threads_map = {}  # thread_id -> thread 对象

        # 2a. 活跃帖子（最近 7 天创建的）
        for t in channel.threads:
            if start_time <= t.created_at <= end_time:
                threads_map[str(t.id)] = t
        print(f"📡 活跃帖子: {len(threads_map)} 个")

        # 2b. 归档帖子（最近 7 天创建的）
        archived_count = 0
        try:
            before = end_time
            for _ in range(20):  # 最多 20 页
                got_any = False
                async for t in channel.archived_threads(before=before, limit=100):
                    got_any = True
                    tid = str(t.id)
                    if tid not in threads_map and t.created_at >= start_time:
                        threads_map[tid] = t
                    before = min(before, t.created_at)
                if not got_any:
                    break
                archived_count += 1
        except Exception as e:
            print(f"⚠️ 归档拉取异常: {e}")
        print(f"📡 归档帖子: {archived_count} 页, 去重后总计 {len(threads_map)} 个")

        if not threads_map:
            print("📭 无帖子，退出")
            await self.close()
            return

        # 3. 基础数据采集 + 三层过滤
        raw_data = []
        ai_prescreen_list = []  # 通过第一层的帖子列表（给 AI 预筛）

        for tid, t in threads_map.items():
            try:
                # 获取首帖
                msg = None
                try:
                    msg = await t.fetch_message(t.id)
                except Exception:
                    msg = None

                if not msg:
                    continue

                first_content = str(msg.content or "").strip()

                # 统计回复数（首帖不算回复）
                reply_count = max(0, (t.message_count or 1) - 1)

                # 表情统计
                pos_reactions, neg_reactions = self._reaction_polarity_counts(msg)
                total_reactions = sum(r.count for r in (msg.reactions or []))

                # 采集前 10 条非 bot 消息 + 统计真实参与人数
                msg_texts = []
                real_participants = set()
                try:
                    async for m in t.history(limit=20, oldest_first=True):
                        if getattr(m.author, "bot", False):
                            continue
                        real_participants.add(m.author.id)
                        text = str(m.content or "")[:200].strip()
                        if text and len(msg_texts) < 10:
                            msg_texts.append(text)
                except Exception:
                    if first_content:
                        msg_texts = [first_content[:200]]
                if msg.author:
                    real_participants.add(msg.author.id)
                real_participant_count = max(1, len(real_participants))

                thread_data = {
                    "thread_id": tid,
                    "title": t.name,
                    "first_content": first_content,
                    "msg_texts": msg_texts,
                    "reply_count": reply_count,
                    "participant_count": real_participant_count,
                    "pos_reactions": pos_reactions,
                    "neg_reactions": neg_reactions,
                    "total_reactions": total_reactions,
                    "message_count": t.message_count or 1,
                    "created_at": t.created_at,
                    "is_archived": getattr(t, "archived", False),
                    "author_name": str(msg.author) if msg.author else "unknown",
                    "author_id": str(msg.author.id) if msg.author else "0",
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                }

                # 第一层：规则过滤
                if self._rule_filter(thread_data):
                    ai_prescreen_list.append(thread_data)
                else:
                    # 标记为已过滤，但仍然入库（基础数据用于增量对比）
                    raw_data.append({**thread_data, "filtered": True, "skip_reason": "rule_filter"})
            except Exception:
                continue

        print(f"🔍 第一层规则过滤通过: {len(ai_prescreen_list)} 个")

        # 4. 第二层：AI 预筛
        passed_ids = set()
        if ai_prescreen_list:
            passed_ids = await self._ai_prescreen(ai_prescreen_list, ai_client)
            print(f"🤖 第二层 AI 预筛通过: {len(passed_ids)} 个")

        candidates = [t for t in ai_prescreen_list if t["thread_id"] in passed_ids]
        # 深度分析上限，按热度综合排序取 top 30
        MAX_DEEP_ANALYSIS = 30
        if len(candidates) > MAX_DEEP_ANALYSIS:
            for c in candidates:
                c["_sort_score"] = c.get("reply_count", 0) + c.get("total_reactions", 0) + c.get("message_count", 0)
            candidates.sort(key=lambda x: x.get("_sort_score", 0), reverse=True)
            candidates = candidates[:MAX_DEEP_ANALYSIS]
            print(f"⚠️ 预筛通过 {len(passed_ids)} 个，深度分析上限 {MAX_DEEP_ANALYSIS}，按热度取 top {MAX_DEEP_ANALYSIS}")
        filtered_out = [t for t in ai_prescreen_list if t["thread_id"] not in passed_ids]
        for t in filtered_out:
            raw_data.append({**t, "filtered": True, "skip_reason": "ai_prescreen"})

        if not candidates:
            print("📭 无候选帖子进入深度分析")

        # 5. 增量判断：哪些帖子需要深度分析（数据源：Bitable）
        need_analysis = []
        skip_analysis = []

        for t in candidates:
            existing = bitable_existing.get(t["thread_id"])
            if existing:
                # existing: {record_id, reply_count, sentiment, participant_count,
                #            heat_score, category, sub_category, summary, short_title}
                old_reply_count = existing.get("reply_count") or 0
                old_sentiment = existing.get("sentiment")
                old_silent_days = 0  # Bitable 中暂无 silent_days 字段，默认 0

                # 保存旧值用于后续计算
                t["prev_reply_count"] = old_reply_count
                t["prev_sentiment"] = old_sentiment
                t["old_category"] = existing.get("category", "")
                t["old_sub_category"] = existing.get("sub_category", "")
                t["old_summary"] = existing.get("summary", "")
                t["old_short_title"] = existing.get("short_title", "")
                t["old_participant_count"] = existing.get("participant_count", 0)
                t["old_heat_score"] = existing.get("heat_score", 0)
                t["record_id"] = existing.get("record_id")
                t["is_new"] = False

                if old_silent_days >= 3:
                    # 死帖：跳过分析，只更新基础数据
                    skip_analysis.append(t)
                elif (t["reply_count"] - old_reply_count) < 5:
                    # 回复数变化不足 5 → 跳过分析，更新 prev 为当前值
                    t["prev_reply_count"] = t["reply_count"]  # 更新基准
                    if t["reply_count"] == old_reply_count:
                        t["silent_days"] = (old_silent_days or 0) + 1
                    skip_analysis.append(t)
                else:
                    # 回复数变化 ≥ 5 → 需要重新分析
                    need_analysis.append(t)
            else:
                # 新帖子 → 必须分析
                t["prev_reply_count"] = 0
                t["prev_sentiment"] = None
                t["is_new"] = True
                t["record_id"] = None
                need_analysis.append(t)

        print(f"📊 需要深度分析: {len(need_analysis)} 个, 跳过分析: {len(skip_analysis)} 个")

        # 6. 第三层：深度分析（AI）
        all_enriched = []
        new_kb_records = set()

        if need_analysis:
            # 构造 AI 上下文：使用采集阶段预收集的 msg_texts
            items_for_ai = []
            for t in need_analysis:
                msg_texts = t.get("msg_texts") or []
                if not msg_texts and t.get("first_content"):
                    msg_texts = [t["first_content"][:200]]
                msg_block = "\n".join(msg_texts) if msg_texts else t.get("first_content", "")[:200]
                items_for_ai.append(
                    f"ID: {t['thread_id']} | 标题: {t['title']} | 消息:\n{msg_block}"
                )

            # 构造分类选项列表（按 cat1 分组展示）
            cat_options = {}
            for e in kb:
                cat_options.setdefault(e["cat1"], set()).add(e["cat2"])
            ref_guide = "\n".join([f"- {c1} > {' | '.join(sorted(c2s))}" for c1, c2s in cat_options.items()])

            prompt = f"""分析《DarkWar》玩家建议帖，按以下步骤处理每个帖子：

第1步 - 理解：玩家核心诉求是什么？
第2步 - 分类：从分类选项中选择最匹配的分类
第3步 - 提炼：用一句话写出具体可执行的建议

【分类规则】：
1. 优先从以下已有分类中选择：
{ref_guide if ref_guide else "暂无已有分类"}
2. 如果确实无法匹配任何已有分类，可以创建新分类，但必须设置 is_new_category: true
3. 新分类命名要求：简体中文，2-6字，不要过于具体
4. 无法判断时使用 category="其他", sub_category="待分类"

【输出要求】：
1. 输出必须为简体中文的 JSON 数组
2. category 和 sub_category 只输出单个最合适的分类（不要列表）
3. summary 概括帖子讨论的核心问题是什么（只描述问题，不要写建议）
4. suggestion_core 是玩家提出的具体改进方案，可以有多条用分号分隔（如"将X技能眩晕从3秒降至1.5秒；增加眩晕递减机制"）。如果帖子只描述了问题但没有提出建议，填"无"
**重要翻译规则：所有输出内容必须是完整的中文，不允许出现英文句子或短语。游戏专有名词也必须翻译成中文，可在中文后括号注明英文原文，例如"核心余烬(Core Ember)"、"黑金(Black Gold)"。上面的分类选项仅用于归类，不是游戏术语翻译参考。**
5. short_title 必须是 10 字以内的中文短标题
6. sentiment 为 1-10 分，分数越高代表玩家越满意（10 最满意，1 最愤怒）
   - 1-2：极度不满  3-4：明显不满  5-6：中性  7-8：比较满意  9-10：非常满意

输出字段: id, category, sub_category, summary(核心问题概括), suggestion_core(具体改进方案或"无"), short_title(10字以内), sentiment(1-10), is_new_category(布尔值,可选)
数据：
{chr(10).join(items_for_ai)}"""

            try:
                print(f"🤖 第三层深度分析中 ({len(need_analysis)} 帖)...")
                res = await ai_client.chat.completions.create(
                    model=CONF["AI_MODEL"],
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                js = res.choices[0].message.content.strip()
                if "```json" in js:
                    js = js.split("```json")[1].split("```")[0].strip()
                ai_res = json.loads(js)
                if isinstance(ai_res, dict):
                    for k in ai_res:
                        if isinstance(ai_res[k], list):
                            ai_res = ai_res[k]
                            break

                ai_map = {item["id"]: item for item in ai_res if isinstance(item, dict)}

                # 构建参考表查找集合
                kb_pairs = {(e["cat1"], e["cat2"]) for e in kb}

                def _to_str(v, d):
                    if isinstance(v, list):
                        return str(v[0]).strip()[:20] if v else d
                    return str(v).strip()[:20] if v else d

                for t in need_analysis:
                    ai_info = ai_map.get(t["thread_id"], {})

                    ai_cat1 = _to_str(ai_info.get("category"), "其他")
                    ai_cat2 = _to_str(ai_info.get("sub_category"), "通用")
                    is_new_flag = ai_info.get("is_new_category", False)
                    print(f"  📌 [{t['thread_id']}] AI分类: {ai_cat1}/{ai_cat2} | is_new={is_new_flag}")

                    # 精确匹配参考表
                    if (ai_cat1, ai_cat2) in kb_pairs:
                        c1, c2 = ai_cat1, ai_cat2
                        print(f"    → 精确匹配命中")
                    else:
                        # 没有精确匹配，尝试同 cat1 下的近似 cat2
                        matched = False
                        for entry in kb:
                            if entry["cat1"] == ai_cat1:
                                overlap = sum(1 for ch in ai_cat2 if ch in entry["cat2"])
                                similarity = overlap / max(len(entry["cat2"]), len(ai_cat2), 1)
                                if similarity >= 0.8:
                                    c1, c2 = entry["cat1"], entry["cat2"]
                                    matched = True
                                    print(f"    → 近似匹配: {ai_cat2} ≈ {entry['cat2']} (相似度{similarity:.2f})")
                                    break
                        if not matched:
                            c1, c2 = ai_cat1, ai_cat2
                            print(f"    → 未匹配，使用AI原始分类")
                            if is_new_flag or (c1, c2) not in kb_pairs:
                                if c1 not in ("其他", "", None) and c2 not in ("通用", "待分类", "", None):
                                    if len(new_kb_records) < 3:
                                        new_kb_records.add((c1, c2))

                    sentiment = int(ai_info.get("sentiment", 5))
                    heat = self._calc_heat(
                        t.get("message_count", 1),
                        t.get("total_reactions", 0),
                        t.get("participant_count", 1),
                        sentiment,
                    )

                    all_enriched.append({
                        **t,
                        "模块分类": c1,
                        "二级分类": c2,
                        "sentiment": sentiment,
                        "具体建议": str(ai_info.get("suggestion_core", "无")),
                        "AI短标题": str(ai_info.get("short_title") or ai_info.get("summary", ""))[:10],
                        "AI核心总结": str(ai_info.get("summary", t["title"])),
                        "heat_score": heat,
                        "filtered": False,
                    })
            except Exception as e:
                print(f"❌ AI 深度分析异常: {e}")
                # 降级：用默认值
                for t in need_analysis:
                    heat = self._calc_heat(
                        t.get("message_count", 1),
                        t.get("total_reactions", 0),
                        t.get("participant_count", 1),
                        5,
                    )
                    all_enriched.append({
                        **t,
                        "模块分类": "其他",
                        "二级分类": "通用",
                        "sentiment": 5,
                        "具体建议": "无",
                        "AI短标题": "",
                        "AI核心总结": t.get("first_content", "")[:50],
                        "heat_score": heat,
                        "filtered": False,
                    })

        # 7. 处理跳过分析的帖子（保留旧分析结果，更新基础数据）
        for t in skip_analysis:
            old_sentiment = t.get("prev_sentiment", 5) or 5
            old_category = t.get("old_category", "其他") or "其他"
            old_sub_category = t.get("old_sub_category", "通用") or "通用"
            old_summary = t.get("old_summary", "") or t.get("first_content", "")[:50] or ""
            old_short_title = t.get("old_short_title", "") or ""
            heat = self._calc_heat(
                t.get("message_count", 1),
                t.get("total_reactions", 0),
                t.get("participant_count", 1),
                old_sentiment,
            )

            all_enriched.append({
                **t,
                "模块分类": old_category,
                "二级分类": old_sub_category,
                "sentiment": old_sentiment,
                "具体建议": old_summary,
                "AI短标题": old_short_title,
                "AI核心总结": old_summary,
                "heat_score": heat,
                "filtered": False,
            })

        # 8. 处理被过滤的帖子（基础数据入库，不进入分析）
        filtered_enriched = []
        for t in [d for d in raw_data if d.get("filtered")]:
            filtered_enriched.append({
                **t,
                "模块分类": "",
                "二级分类": "",
                "sentiment": None,
                "AI短标题": "",
                "AI核心总结": "",
                "heat_score": 0,
            })

        # 合并所有帖子
        everything = all_enriched + filtered_enriched

        if not everything:
            print("📭 无数据，退出")
            await self.close()
            return

        # 9. 计算增量 & heat_trend
        for e in everything:
            if e.get("is_new"):
                e["sentiment_delta"] = None
                e["reply_delta"] = e.get("reply_count", 0)
            else:
                prev_s = e.get("prev_sentiment")
                e["sentiment_delta"] = (e["sentiment"] - prev_s) if (e.get("sentiment") is not None and prev_s is not None) else None
                e["reply_delta"] = (e.get("reply_count", 0) - (e.get("prev_reply_count") or 0))

            # heat_trend
            old_heat = e.get("old_heat_score")
            new_heat = e.get("heat_score", 0)
            if old_heat is None:
                e["heat_trend"] = "rising"
            elif new_heat > old_heat * 1.2:
                e["heat_trend"] = "rising"
            elif new_heat < old_heat * 0.8:
                e["heat_trend"] = "cooling"
            else:
                e["heat_trend"] = "stable"

            # silent_days
            if e.get("filtered"):
                e["silent_days"] = (e.get("old_silent_days") or 0) + 1 if not e.get("is_new") else 0
            elif e.get("is_new"):
                e["silent_days"] = 0
            elif e.get("reply_count") == e.get("prev_reply_count"):
                e["silent_days"] = (e.get("old_silent_days") or 0) + 1
            else:
                e["silent_days"] = 0

        # 10. 飞书多维表格同步（数据源：Bitable，无需本地 DB）
        sync_list = [e for e in everything if not e.get("filtered")]
        for e in sync_list:
            e["date_ms"] = int(e["created_at"].timestamp() * 1000) if isinstance(e.get("created_at"), datetime) else 0
        feishu.sync_bitable(sync_list, existing=bitable_existing)

        # 11. 更新参考库
        if new_kb_records:
            print(f"📝 准备新增 {len(new_kb_records)} 个分类: {list(new_kb_records)}")
        else:
            print("📝 本次无新分类需要新增（AI 分类全部匹配到已有术语库）")
        feishu.add_new_reference(list(new_kb_records))

        # 12. 发送日报卡片
        # 分类：新发现 vs 有显著变化
        new_discoveries = [e for e in all_enriched if e.get("is_new") and not e.get("filtered")]
        significant_changes = []
        for e in all_enriched:
            if e.get("is_new") or e.get("filtered"):
                continue
            sd = e.get("sentiment_delta")
            rd = e.get("reply_delta")
            is_significant = False
            if sd is not None and abs(sd) >= 3:
                is_significant = True
            if rd is not None and abs(rd) >= 10:
                is_significant = True
            if is_significant:
                significant_changes.append(e)

        # 按热度排序
        new_discoveries.sort(key=lambda x: x.get("heat_score", 0), reverse=True)
        significant_changes.sort(key=lambda x: x.get("heat_score", 0), reverse=True)

        report_date = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d")
        await self.send_daily_card(new_discoveries, significant_changes, feishu, report_date, len(sync_list))

        print(f"✅ 日报完成: 新发现={len(new_discoveries)}, 显著变化={len(significant_changes)}")
        await self.close()

    # ==================== 日报卡片 ====================
    async def send_daily_card(self, new_list, change_list, feishu, report_date, total_count):
        el = [{
            "tag": "markdown",
            "content": f"**📊 {report_date} Discord 建议日报**\n今日采集帖子：**{total_count}** 个"
        }]

        def build_thread_element(item):
            """构建单个帖子的卡片元素"""
            sentiment = item.get("sentiment")
            sd = item.get("sentiment_delta")
            rd = item.get("reply_delta", 0)

            # 颜色标记
            if sentiment is not None and sentiment <= 3:
                color = "red"
            elif sd is not None and sd <= -3:
                color = "red"
            elif rd is not None and abs(rd) >= 10:
                color = "orange"
            elif sd is not None and sd >= 3:
                color = "green"
            else:
                color = "grey"

            # Δ 文案
            if sd is not None:
                sd_str = f"{'↑' if sd > 0 else '↓'}{abs(sd)}" if sd != 0 else "→0"
            else:
                sd_str = "new"
            if rd is not None:
                rd_str = f"+{rd}" if rd > 0 else str(rd)
            else:
                rd_str = "0"

            short_title = str(item.get("AI短标题", ""))[:10] or str(item.get("AI核心总结", ""))[:10]
            summary = str(item.get("AI核心总结", ""))[:60]
            suggestion = str(item.get("具体建议", "")).strip()[:80]
            dc_link = str(item.get("帖子链接", ""))
            cat = str(item.get("模块分类", ""))
            sub_cat = str(item.get("二级分类", ""))
            tag = f"#{cat}-{sub_cat}" if cat and sub_cat else ""

            # 标题带 Discord 跳转链接
            if dc_link:
                title_md = f"<font color='{color}'>**[{short_title}]({dc_link})**</font>"
            else:
                title_md = f"<font color='{color}'>**{short_title}**</font>"

            # 构建左侧内容
            left_elements = [
                {"tag": "markdown", "content": title_md},
                {"tag": "markdown", "content": summary},
            ]
            if suggestion and suggestion not in ("无", "", summary):
                left_elements.append({"tag": "markdown", "content": f"💡 **建议：**{suggestion}"})
            left_elements.append({"tag": "note", "elements": [{"tag": "lark_md", "content": tag}]})

            return {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 4,
                        "vertical_align": "top",
                        "elements": left_elements,
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "div",
                                "fields": [
                                    {
                                        "is_short": True,
                                        "text": {"tag": "lark_md", "content": f"🙂 **满意度**\n<font color='{color}'>{sentiment if sentiment is not None else '-'}/10 ({sd_str})</font>"},
                                    },
                                    {
                                        "is_short": True,
                                        "text": {"tag": "lark_md", "content": f"💬 **回复数**\n{item.get('reply_count', 0)} ({rd_str})"},
                                    },
                                    {
                                        "is_short": True,
                                        "text": {"tag": "lark_md", "content": f"👥 **参与人数**\n{item.get('participant_count', 0)}"},
                                    },
                                    {
                                        "is_short": True,
                                        "text": {"tag": "lark_md", "content": f"🔥 **热度**\n{item.get('heat_score', 0)}"},
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }

        # 🆕 新发现
        if new_list:
            el.append({"tag": "hr"})
            el.append({"tag": "markdown", "content": f"**🆕 新发现的帖子 ({len(new_list)})**"})
            for i, item in enumerate(new_list[:10]):
                el.append(build_thread_element(item))
                if i < min(len(new_list), 10) - 1:
                    el.append({"tag": "hr"})

        # 📈 有显著变化
        if change_list:
            el.append({"tag": "hr"})
            el.append({"tag": "markdown", "content": f"**📈 有显著变化的帖子 ({len(change_list)})**"})
            for i, item in enumerate(change_list[:10]):
                el.append(build_thread_element(item))
                if i < min(len(change_list), 10) - 1:
                    el.append({"tag": "hr"})

        # 无内容
        if not new_list and not change_list:
            el.append({"tag": "hr"})
            el.append({"tag": "markdown", "content": "今日暂无需要关注的新帖子或显著变化。"})

        # 操作按钮
        el.append({"tag": "hr"})
        el.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"},
                    "type": "primary",
                    "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}",
                }
            ],
        })

        # 指标说明
        el.append({"tag": "hr"})
        el.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "💡"},
                {
                    "tag": "lark_md",
                    "content": "指标说明：满意度 1-10（越高越满意） ｜ 热度 = 消息×1 + 反应×1 + 人数×3 ｜ Δ 满意度≥3 或 Δ 回复≥10 标记为显著变化",
                },
            ],
        })

        # 确定卡片头部颜色
        all_items = new_list + change_list
        if all_items:
            worst = min(item.get("sentiment", 5) for item in all_items if item.get("sentiment") is not None)
            template = self._template_by_sentiment(worst)
        else:
            template = "blue"

        card_payload = {
            "header": {
                "title": {"tag": "plain_text", "content": f"🗓️ {report_date} Discord 建议日报"},
                "template": template,
            },
            "elements": el,
        }
        ok = feishu.send_group_card(card_payload)
        if not ok:
            # 降级：简版卡片
            fallback_el = [
                {"tag": "markdown", "content": f"**📊 {report_date} Discord 建议日报**\n采集帖子：{total_count}"},
                {"tag": "hr"},
            ]
            for item in (new_list + change_list)[:10]:
                short = str(item.get("AI短标题", "") or item.get("AI核心总结", ""))[:10]
                s = item.get("sentiment", "-")
                summary = str(item.get("AI核心总结", ""))[:60]
                fallback_el.append({
                    "tag": "markdown",
                    "content": f"**{short}** | 🙂{s}/10 | 🔥{item.get('heat_score', 0)}\n{summary}",
                })
            fallback_el.append({"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"},
                "type": "primary",
                "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}",
            }]})
            feishu.send_group_card({
                "header": {
                    "title": {"tag": "plain_text", "content": f"🗓️ {report_date} Discord 建议日报"},
                    "template": "blue",
                },
                "elements": fallback_el,
            })


# ===================== 5. 入口 =====================
if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    DailyBot(intents=intents).run(CONF["DISCORD_TOKEN"])
