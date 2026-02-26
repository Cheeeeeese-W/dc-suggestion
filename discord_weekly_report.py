import asyncio
import os
import json
import discord
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ===================== 配置中心 =====================
def get_conf():
    return {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
        "CHANNEL_ID": int(os.getenv("DISCORD_CHANNEL_ID")),
        "AI_API_KEY": os.getenv("AI_API_KEY"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_APP_ID": os.getenv("FEISHU_APP_ID"),
        "FEISHU_APP_SECRET": os.getenv("FEISHU_APP_SECRET"),
        "BITABLE_TOKEN": os.getenv("FEISHU_BITABLE_APP_TOKEN"),
        "BITABLE_TABLE_ID": os.getenv("FEISHU_BITABLE_TABLE_ID"),
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")
    }

CONF = get_conf()

# ===================== 飞书自建应用客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]})
        return res.json().get("tenant_access_token")

    def batch_add_bitable_records(self, records_list):
        """批量写入记录到多维表格"""
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        
        formatted_records = []
        for item in records_list:
            formatted_records.append({
                "fields": {
                    "日期": item.get("日期"),
                    "模块分类": item.get("模块分类"),
                    "二级分类": item.get("二级分类"),
                    "热度分": item.get("热度分"),
                    "参与人数": item.get("参与人数"), # 这里现在是数字了
                    "AI核心总结": item.get("AI核心总结"),
                    "情绪得分": item.get("情绪得分"),
                    "帖子链接": {"text": "点击查看帖子", "link": item.get("帖子链接")}
                }
            })
        
        res = requests.post(url, headers=headers, json={"records": formatted_records})
        if res.json().get("code") == 0:
            print(f"✅ 成功同步 {len(records_list)} 条记录到多维表格")
        else:
            print(f"❌ 多维表格同步失败: {res.text}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {
            "receive_id": CONF["FEISHU_CHAT_ID"],
            "msg_type": "interactive",
            "content": json.dumps(card_content)
        }
        requests.post(url, headers=headers, json=payload)

# ===================== Discord 机器人主逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统启动，登录身份: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        genai.configure(api_key=CONF["AI_API_KEY"])
        ai_model = genai.GenerativeModel(CONF["AI_MODEL"])
        
        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        raw_threads_data = []
        for index, thread in enumerate(threads):
            try:
                starter_msg = None
                try: starter_msg = await thread.fetch_message(thread.id)
                except: 
                    async for m in thread.history(limit=1, oldest_first=True): starter_msg = m
                if not starter_msg: continue

                reaction_count = sum([r.count for r in starter_msg.reactions])
                unique_users = {starter_msg.author.id}
                async for msg in thread.history(limit=20): unique_users.add(msg.author.id)

                raw_threads_data.append({
                    "id": index, 
                    "标题": thread.name, 
                    "内容": starter_msg.content[:600],
                    "热度分": int((thread.message_count * 3) + (reaction_count * 1)),
                    "参与人数_list": list(unique_users),
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
                    "日期": int(thread.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_threads_data:
            print("📭 无有效内容"); await self.close(); return

        print(f"🤖 正在调用 AI 进行同类归纳分析...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_threads_data])

        prompt = f"""
        你是一个资深的游戏数据分析师。分析建议并输出 JSON 列表 []。
        包含字段: id, sentiment(数字1-10), category, sub_category, summary, topic_label。
        数据：\n{items_str}
        """

        try:
            response = await ai_model.generate_content_async(prompt)
            ai_results = json.loads(response.text.replace('```json', '').replace('```', '').strip())
            ai_map = {item['id']: item for item in ai_results}
            
            all_enriched_data = []
            topic_groups = {}

            for original in raw_threads_data:
                ai_info = ai_map.get(original['id'], {})
                
                # --- 强制数字转换逻辑 ---
                try:
                    sentiment_val = int(ai_info.get('sentiment', 5))
                except:
                    sentiment_val = 5

                item = {
                    **original,
                    "参与人数": int(len(original.get("参与人数_list", []))),
                    "模块分类": str(ai_info.get('category', '其他')),
                    "二级分类": str(ai_info.get('sub_category', '通用')),
                    "情绪得分": sentiment_val,
                    "AI核心总结": str(ai_info.get('summary', original['标题'])),
                    "话题标签": str(ai_info.get('topic_label', original['标题']))
                }
                all_enriched_data.append(item)

                label = item["话题标签"]
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]
                g["count"] += 1
                g["users"].update(original["参与人数_list"])

            all_enriched_data.sort(key=lambda x: x['日期'], reverse=True)
            feishu.batch_add_bitable_records(all_enriched_data)

            summary_list = [{"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
            await self.send_weekly_card(summary_list, feishu, date_display)

        except Exception as e:
            print(f"❌ 处理失败: {e}")
        
        await self.close()

    async def send_weekly_card(self, summary_list, feishu_client, date_str):
        sorted_topics = sorted(summary_list, key=lambda x: x['total_heat'], reverse=True)[:5]
        elements = [{"tag": "markdown", "content": f"**📊 上周社区核心话题概览**\n共发现 **{len(summary_list)}** 个讨论主题。"}, {"tag": "hr"}]

        for i, item in enumerate(sorted_topics):
            elements.append({
                "tag": "markdown",
                "content": f"**TOP {i+1}: {item['topic']}**\n▫️ 诉求: {item['summary']}\n▫️ 统计: 🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人讨论\n▫️ 分类: #{item['category']}"
            })

        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📂 查看多维表格明细"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})

        feishu_client.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({date_str})"}, "template": "blue"}, "elements": elements})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])