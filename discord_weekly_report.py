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
        """获取历史参考库中的 分类-标签 对"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            # 返回格式: [{"cat": "xxx", "tag": "yyy"}, ...]
            return [{"cat": r['fields'].get('二级分类'), "tag": r['fields'].get('话题标签')} for r in items if r['fields'].get('话题标签')]
        except: return []

    def add_new_reference_pairs(self, new_pairs):
        """成对同步新标签到参考表"""
        if not self.token or not new_pairs: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        
        records = []
        for cat, tag in new_pairs:
            if cat and tag:
                records.append({"fields": {"二级分类": str(cat), "话题标签": str(tag)}})
        
        if not records: return
        res = requests.post(url, headers=headers, json={"records": records}, timeout=10)
        print(f"📊 历史参考库更新结果: {res.json().get('msg')} (写入 {len(records)} 条)")

    def batch_add_bitable_records(self, records_list):
        """批量同步主数据表"""
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
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=15)
        print(f"✅ 主表同步状态: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"🚀 飞书群推送结果: {res.json().get('msg')}")

# ===================== 3. 机器人核心逻辑 =====================
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
        
        # 1. 加载历史层级记忆
        ref_pairs = feishu.get_reference_data()
        ref_text = "\n".join([f"- {p['cat']}: {p['tag']}" for p in ref_pairs if p['cat'] and p['tag']])
        print(f"📚 已加载 {len(ref_pairs)} 条历史分类-话题关系")

        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 2. 抓取数据
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
                raw_data.append({
                    "id": index, "标题": t.name, "内容": msg.content[:600],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "owner_id": msg.author.id,
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                    "日期": int(t.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_data:
            print("📭 无数据，结束"); await self.close(); return

        # 3. AI 分析
        print(f"🤖 AI 分析中...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        
        prompt = f"""你是一个游戏策划。分析建议并输出 JSON []。
        
        【历史库（二级分类: 话题标签）】：
        {ref_text if ref_text else "暂无"}

        【规则】：
        - 优先使用库中已有的“二级分类”和“话题标签”。
        - 二级分类(sub_category): 系统模块名（名词，2-4字）。
        - 话题标签(topic_label): 具体痛点短语（对象+动作，4-6字）。
        - 一个建议只给1-2个最相关的标签。

        输出字段: id, sentiment(1-10), category(list), sub_category(list), summary, topic_label(list)。
        数据：\n{items_str}"""

        all_enriched = []
        new_ref_pairs = set()

        try:
            res = await ai_client.chat.completions.create(
                model=CONF["AI_MODEL"], 
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            js = res.choices[0].message.content.strip()
            if "```json" in js: js = js.split("```json")[1].split("```")[0].strip()
            ai_res = json.loads(js)
            if isinstance(ai_res, dict):
                for k in ai_res:
                    if isinstance(ai_res[k], list): ai_res = ai_res[k]; break
            
            ai_map = {item['id']: item for item in ai_res if isinstance(item, dict)}
            
            for o in raw_data:
                ai_info = ai_map.get(o['id'], {})
                
                def to_list(val, default, limit=2):
                    v_list = val if isinstance(val, list) else [val] if val else [default]
                    return [str(v).strip()[:10] for v in v_list if v][:limit]

                c1 = to_list(ai_info.get('category'), "其他")
                c2 = to_list(ai_info.get('sub_category'), "通用")
                tp = to_list(ai_info.get('topic_label'), o['标题'])

                # 记录新出现的 组合
                for sub in c2:
                    for t_label in tp:
                        # 检查是否已存在于历史库中
                        exists = any(p['cat'] == sub and p['tag'] == t_label for p in ref_pairs)
                        if not exists:
                            new_ref_pairs.add((sub, t_label))

                all_enriched.append({
                    **o, "模块分类": c1, "二级分类": c2, "话题标签": tp,
                    "情绪得分": int(ai_info.get('sentiment', 5)),
                    "AI核心总结": str(ai_info.get('summary', o['标题'])),
                    "参与人数": 1 # 简化为1，防止多选字段计数混乱
                })
        except Exception as e:
            print(f"⚠️ AI 分析失败: {e}"); await self.close(); return

        # 4. 同步数据
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference_pairs(list(new_ref_pairs))

        # 5. 话题聚合（核心修复：防止重复卡片）
        topic_groups = {}
        for item in all_enriched:
            for label in item["话题标签"]:
                if label not in topic_groups:
                    topic_groups[label] = {
                        "topic": label, 
                        "cat": item["模块分类"][0], 
                        "heat": 0, 
                        "count": 0, 
                        "users": set(), 
                        "sum": item["AI核心总结"]
                    }
                g = topic_groups[label]
                g["heat"] += item["热度分"]
                g["count"] += 1
                g["users"].add(item["owner_id"])

        # 生成周报列表
        summary_list = []
        for v in topic_groups.values():
            summary_list.append({
                "topic": v["topic"],
                "category": v["cat"],
                "total_heat": v["heat"],
                "thread_count": v["count"],
                "user_count": len(v["users"]),
                "summary": v["sum"]
            })

        if summary_list:
            await self.send_weekly_card(summary_list, feishu, date_display)
        
        await self.close()

    async def send_weekly_card(self, sl, fs, dt):
        # 按热度分降序排列，取前 5
        st = sorted(sl, key=lambda x: x['total_heat'], reverse=True)[:5]
        
        # 进一步去重：如果两个话题的总结（Summary）完全一样，只取热度最高的那个话题显示
        seen_summaries = set()
        unique_st = []
        for item in st:
            if item['summary'] not in seen_summaries:
                unique_st.append(item)
                seen_summaries.add(item['summary'])
        
        el = [{"tag": "markdown", "content": f"**📊 上周社区核心话题概览**\n共识别主题: {len(sl)} 个，数据已录入多维表格。"}, {"tag": "hr"}]
        for i, item in enumerate(unique_st):
            el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary']}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人提及 | #{item['category']}"})
        
        el.append({"tag": "hr"})
        el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格明细"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        
        fs.send_group_card({
            "config": {"enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({dt})"}, "template": "blue"},
            "elements": el
        })

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])