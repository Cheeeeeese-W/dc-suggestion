import asyncio
import os
import json
import discord
import requests
from openai import AsyncOpenAI
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ===================== 配置中心 =====================
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

# ===================== 飞书客户端 =====================
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
        """读取历史参考库：返回已有的 二级分类-话题标签 对应关系"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            pairs = []
            for r in items:
                f = r['fields']
                if f.get('二级分类') and f.get('话题标签'):
                    pairs.append({"sub_category": f['二级分类'], "topic_label": f['话题标签']})
            return pairs
        except: return []

    def add_new_reference_pairs(self, new_pairs):
        """将新生成的 组合 同步到参考库"""
        if not self.token or not new_pairs: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        records = [{"fields": {"二级分类": p[0], "话题标签": p[1]}} for p in new_pairs]
        requests.post(url, headers=headers, json={"records": records}, timeout=10)

    def batch_add_bitable_records(self, records_list):
        """批量同步主表（适配多选字段）"""
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        formatted = []
        for r in records_list:
            formatted.append({"fields": {
                "日期": r.get("日期"),
                "模块分类": r.get("模块分类"), # 必须是 list
                "二级分类": r.get("二级分类"), # 必须是 list
                "话题标签": r.get("话题标签"), # 必须是 list
                "热度分": int(r.get("热度分", 0)),
                "参与人数": int(r.get("参与人数", 0)),
                "AI核心总结": str(r.get("AI核心总结", "")),
                "情绪得分": int(r.get("情绪得分", 5)),
                "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
            }})
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=10)
        print(f"主表同步结果: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        requests.post(url, headers=headers, json=payload, timeout=10)

# ===================== Discord 机器人 =====================
class AdvancedBot(discord.Client):
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统就绪: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=CONF["AI_BASE_URL"].strip().rstrip('/') + '/v1')
        
        # 1. 加载层级记忆
        ref_pairs = feishu.get_reference_data()
        ref_text = "\n".join([f"- {p['sub_category']}: {p['topic_label']}" for p in ref_pairs])
        print(f"📚 已加载 {len(ref_pairs)} 条分类-话题对应关系")

        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
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
                except: async for m in t.history(limit=1, oldest_first=True): msg = m
                if not msg: continue
                raw_data.append({
                    "id": index, "标题": t.name, "内容": msg.content[:500],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "参与人数": 1, "owner_id": msg.author.id,
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                    "日期": int(t.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_data: await self.close(); return

        # 2. AI 分析：允许识别多个建议，并参考层级结构
        print(f"🤖 AI 分析中...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        
        prompt = f"""你是一个资深游戏策划。请分析玩家建议并输出 JSON。
        
        【历史参考库（二级分类: 话题标签）】：
        {ref_text if ref_text else "暂无数据"}

        【规则】：
        1. 一个帖子可能包含多个诉求。如果是，请在 category, sub_category, topic_label 中使用列表/数组。
        2. **强制一致性**：如果诉求匹配参考库，必须使用库中的 二级分类 和 话题标签。
        3. 如果是新问题，请自创简短精准的名称。
        4. 一级分类选一或多：战斗平衡、赛季机制、日常活动、系统优化、UI/交互、商业化。

        输出 JSON [] 包含: id, sentiment(1-10), category(list), sub_category(list), summary, topic_label(list)。
        数据：\n{items_str}"""

        all_enriched = []
        new_ref_pairs = set() # 存储新发现的 (sub_cat, topic) 元组

        try:
            res = await ai_client.chat.completions.create(model=CONF["AI_MODEL"], messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"})
            js = res.choices[0].message.content.strip()
            if "```json" in js: js = js.split("```json")[1].split("```")[0].strip()
            ai_res = json.loads(js)
            if isinstance(ai_res, dict):
                for k in ai_res:
                    if isinstance(ai_res[k], list): ai_res = ai_res[k]; break
            
            ai_map = {item['id']: item for item in ai_res if isinstance(item, dict)}
            
            for o in raw_data:
                ai_info = ai_map.get(o['id'], {})
                
                # 辅助函数：统一转化为列表并清洗
                def clean_list(val, default):
                    if isinstance(val, list): return [str(v).strip() for v in val if v]
                    if val: return [str(val).strip()]
                    return [default]

                c1 = clean_list(ai_info.get('category'), "其他")
                c2 = clean_list(ai_info.get('sub_category'), "通用")
                tp = clean_list(ai_info.get('topic_label'), o['标题'])

                # 记录新的对应关系（用于更新库）
                # 假设 c2 和 tp 是一一对应的或交叉的，这里简单记录所有出现的对
                for sub in c2:
                    for t_label in tp:
                        if not any(p['sub_category'] == sub and p['topic_label'] == t_label for p in ref_pairs):
                            new_ref_pairs.add((sub, t_label))

                all_enriched.append({
                    **o, "模块分类": c1, "二级分类": c2, "话题标签": tp,
                    "情绪得分": int(ai_info.get('sentiment', 5)),
                    "AI核心总结": str(ai_info.get('summary', o['标题']))
                })
        except Exception as e:
            print(f"⚠️ AI 失败: {e}"); await self.close(); return

        # 3. 操作同步
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference_pairs(list(new_ref_pairs))

        # 4. 话题聚合统计
        topic_groups = {}
        for item in all_enriched:
            for label in item["话题标签"]:
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].add(item["owner_id"])

        sl = [{"topic": v["topic"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
        await self.send_weekly_card(sl, feishu, date_display)
        await self.close()

    async def send_weekly_card(self, sl, fs, dt):
        st = sorted(sl, key=lambda x: x['total_heat'], reverse=True)[:5]
        el = [{"tag": "markdown", "content": f"**📊 上周社区话题聚合概览**\n识别到主题: {len(sl)} 个"}, {"tag": "hr"}]
        for i, item in enumerate(st):
            el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary']}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人提及"})
        el.append({"tag": "hr"})
        el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看明细记录"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        fs.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({dt})"}, "template": "blue"}, "elements": el})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])