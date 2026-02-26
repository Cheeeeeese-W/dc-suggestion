import asyncio
import os
import json
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
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")
    }

CONF = get_conf()

# ===================== 2. 飞书 API 客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]}, timeout=10)
            return res.json().get("tenant_access_token")
        except: return None

    def get_reference_data(self):
        """同步历史分类和标签"""
        if not self.token: return [], []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            cats = [r['fields'].get('二级分类') for r in items if r['fields'].get('二级分类')]
            tags = [r['fields'].get('话题标签') for r in items if r['fields'].get('话题标签')]
            return list(set(cats)), list(set(tags))
        except: return [], []

    def add_new_reference(self, new_cats, new_tags):
        """增量更新参考库"""
        if not self.token: return
        records = []
        for c in new_cats: records.append({"fields": {"二级分类": str(c)}})
        for t in new_tags: records.append({"fields": {"话题标签": str(t)}})
        if not records: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"records": records}, timeout=10)

    def batch_add_bitable_records(self, records_list):
        """批量写入主数据表"""
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        formatted = []
        for r in records_list:
            formatted.append({"fields": {
                "日期": r.get("日期"),
                "模块分类": r.get("模块分类"), 
                "二级分类": r.get("二级分类"),
                "话题标签": r.get("话题标签"),
                "热度分": int(r.get("热度分", 0)),
                "参与人数": int(r.get("参与人数", 0)),
                "AI核心总结": str(r.get("AI核心总结", "")),
                "情绪得分": int(r.get("情绪得分", 5)),
                "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
            }})
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=10)
        print(f"📊 多维表格同步结果: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"🚀 飞书卡片推送: {res.json().get('msg')}")

# ===================== 3. Discord 机器人核心逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_mon - timedelta(days=7), this_mon - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统就绪: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        
        base_url = CONF["AI_BASE_URL"].strip().rstrip('/') + '/v1'
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=base_url)
        
        # 加载记忆
        hist_cats, hist_tags = feishu.get_reference_data()
        print(f"📚 已加载 {len(hist_cats)} 个分类和 {len(hist_tags)} 个标签")

        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 1. 抓取数据
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        raw_data = []
        for index, t in enumerate(threads):
            try:
                msg = None
                try: msg = await t.fetch_message(t.id)
                except: 
                    async for m in t.history(limit=1, oldest_first=True): msg = m
                if not msg: continue
                
                unique_users = {msg.author.id}
                async for m in t.history(limit=20): unique_users.add(m.author.id)

                raw_data.append({
                    "id": index, "标题": t.name, "内容": msg.content[:600],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "参与人数": len(unique_users), "owner_id": msg.author.id,
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                    "日期": int(t.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_data:
            print("📭 无数据"); await self.close(); return

        # 2. AI 深度分析
        print(f"🤖 正在调用 AI 进行多维聚合分析...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        
        prompt = f"""你是一个资深游戏策划，请分析《DarkWar》玩家建议。
        
        【命名规范 - 极其重要】：
        1. 二级分类 (sub_category)：必须是系统模块名词（2-4字）。优先使用：[{', '.join(hist_cats)}]。
        2. 话题标签 (topic_label)：必须是具体的痛点短语（对象+问题，4-6字）。优先使用：[{', '.join(hist_tags)}]。
        示例：二级分类为“移民系统”，话题标签为“移民票价过高”。
        
        【分类参考】：战斗平衡、赛季机制、日常活动、系统优化、UI/交互、商业化。

        请输出 JSON 列表 []。每个对象包含: id, sentiment(1-10), category(list), sub_category(list), summary, topic_label(list)。
        数据：\n{items_str}"""

        all_enriched = []
        new_cats, new_tags = set(), set()
        try:
            res = await ai_client.chat.completions.create(model=CONF["AI_MODEL"], messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"})
            js = res.choices[0].message.content.strip()
            if "<html" in js.lower(): raise ValueError("AI 返回了网页 HTML")
            if "```json" in js: js = js.split("```json")[1].split("```")[0].strip()
            
            ai_res = json.loads(js)
            if isinstance(ai_res, dict):
                for k in ai_res:
                    if isinstance(ai_res[k], list): ai_res = ai_res[k]; break
            
            ai_map = {item['id']: item for item in ai_res if isinstance(item, dict)}
            for o in raw_data:
                ai_info = ai_map.get(o['id'], {})
                
                # 列表化处理并限制长度防止产生句子
                def clean(val, default, max_len=8):
                    v_list = val if isinstance(val, list) else [val] if val else [default]
                    return [str(v).strip()[:max_len] for v in v_list if v]

                c1 = clean(ai_info.get('category'), "其他", 10)
                c2 = clean(ai_info.get('sub_category'), "通用", 6)
                tp = clean(ai_info.get('topic_label'), o['标题'], 10)

                # 记录新词
                for c in c2: 
                    if c not in hist_cats: new_cats.add(c)
                for t in tp: 
                    if t not in hist_tags: new_tags.add(t)

                all_enriched.append({
                    **o, "模块分类": c1, "二级分类": c2, "话题标签": tp,
                    "情绪得分": int(ai_info.get('sentiment', 5)), "AI核心总结": str(ai_info.get('summary', o['标题']))
                })
        except Exception as e:
            print(f"⚠️ AI 降级模式触发: {e}")
            for o in raw_data:
                all_enriched.append({
                    **o, "模块分类": ["未分类"], "二级分类": ["待定"], "话题标签": [o['标题'][:10]],
                    "情绪得分": 5, "AI核心总结": o['标题']
                })

        # 3. 数据同步
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference(list(new_cats), list(new_tags))

        # 4. 卡片汇总逻辑
        topic_groups = {}
        for item in all_enriched:
            for label in item["话题标签"]:
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"][0], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].add(item["owner_id"])

        summary_list = [{"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
        await self.send_weekly_card(summary_list, feishu, date_display)
        await self.close()

    async def send_weekly_card(self, sl, fs, dt):
        st = sorted(sl, key=lambda x: x['total_heat'], reverse=True)[:5]
        el = [{"tag": "markdown", "content": f"**📊 上周社区核心话题概览**\n识别到主题: {len(sl)} 个，数据已同步至多维表格。"}, {"tag": "hr"}]
        for i, item in enumerate(st):
            el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary']}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人提及 | #{item['category']}"})
        el.append({"tag": "hr"})
        el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        fs.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({dt})"}, "template": "blue"}, "elements": el})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])