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
        "REFERENCE_TABLE_ID": os.getenv("FEISHU_REFERENCE_TABLE_ID"), # 新增参考表ID
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")
    }

CONF = get_conf()

# ===================== 飞书客户端 (增强版) =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]})
        return res.json().get("tenant_access_token")

    def get_reference_tags(self):
        """从参考表读取所有历史标签"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"page_size": 500}
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        tags = [r['fields'].get('话题标签') for r in data.get('data', {}).get('items', []) if r['fields'].get('话题标签')]
        return list(set(tags)) # 去重

    def add_new_tags(self, new_tags):
        """将 AI 新生成的标签存入参考表"""
        if not self.token or not new_tags: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"records": [{"fields": {"话题标签": tag}} for tag in new_tags]}
        requests.post(url, headers=headers, json=payload)

    def batch_add_bitable_records(self, records_list):
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        formatted = [{"fields": {
            "日期": r.get("日期"), "模块分类": r.get("模块分类"), "二级分类": r.get("二级分类"),
            "热度分": int(r.get("热度分", 0)), "参与人数": int(r.get("参与人数", 0)),
            "AI核心总结": str(r.get("AI核心总结", "")), "情绪得分": int(r.get("情绪得分", 5)),
            "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
        }} for r in records_list]
        requests.post(url, headers=headers, json={"records": formatted})

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        requests.post(url, headers=headers, json=payload)

# ===================== 机器人核心逻辑 =====================
class AdvancedBot(discord.Client):
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统启动: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        genai.configure(api_key=CONF["AI_API_KEY"])
        ai_model = genai.GenerativeModel(CONF["AI_MODEL"])
        
        # 1. 读取历史标签
        history_tags = feishu.get_reference_tags()
        print(f"📚 已加载 {len(history_tags)} 个历史参考标签")

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
                    "id": index, "标题": thread.name, "内容": starter_msg.content[:500],
                    "热度分": int((thread.message_count * 3) + (reaction_count * 1)),
                    "参与人数_list": list(unique_users),
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
                    "日期": int(thread.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_threads_data:
            print("📭 无内容"); await self.close(); return

        # 2. AI 批量分析 (加入历史标签参考)
        print(f"🤖 正在调用 AI 分析建议...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_threads_data])
        tags_str = ", ".join(history_tags) if history_tags else "暂无"

        prompt = f"""
        你是一个资深游戏策划。请分析玩家建议并输出 JSON。
        
        【历史话题参考】：[{tags_str}]
        要求：优先使用参考列表中的标签。若描述的问题不在列表中，请自创简短标签(4-6字)。
        讨论同一个问题的帖子必须使用完全相同的 topic_label。

        【分类体系】：
        一级：战斗平衡、赛季机制、日常活动、系统优化、UI/交互、商业化。
        二级：具体的系统模块名。

        输出 JSON 列表 []，包含字段: id, sentiment, category, sub_category, summary, topic_label。
        数据：\n{items_str}
        """

        try:
            response = await ai_model.generate_content_async(prompt)
            json_str = response.text.replace('```json', '').replace('```', '').strip()
            ai_results = json.loads(json_str)
            ai_map = {item['id']: item for item in ai_results}
            
            all_enriched_data = []
            topic_groups = {}
            new_tags_to_save = set()

            for original in raw_threads_data:
                ai_info = ai_map.get(original['id'], {"sentiment": 5, "category": "其他", "sub_category": "通用", "summary": original['标题'], "topic_label": original['标题']})
                
                label = ai_info.get('topic_label', '未知')
                # 记录新出现的标签
                if label not in history_tags: new_tags_to_save.add(label)

                item = {
                    **original,
                    "参与人数": int(len(original.get("参与人数_list", []))),
                    "模块分类": str(ai_info.get('category')),
                    "二级分类": str(ai_info.get('sub_category')),
                    "情绪得分": int(ai_info.get('sentiment', 5)),
                    "AI核心总结": str(ai_info.get('summary')),
                    "话题标签": label
                }
                all_enriched_data.append(item)

                # 聚合逻辑
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].update(original["参与人数_list"])

            # 3. 数据操作：排序、批量写入、更新参考标签
            all_enriched_data.sort(key=lambda x: x['日期'], reverse=True)
            feishu.batch_add_bitable_records(all_enriched_data)
            feishu.add_new_tags(list(new_tags_to_save)) # 自动更新参考库

            # 4. 推送卡片
            summary_list = [{"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
            await self.send_weekly_card(summary_list, feishu, date_display)

        except Exception as e:
            print(f"❌ 分析失败: {e}")
        
        await self.close()

    async def send_weekly_card(self, summary_list, feishu_client, date_str):
        sorted_topics = sorted(summary_list, key=lambda x: x['total_heat'], reverse=True)[:5]
        elements = [{"tag": "markdown", "content": f"**📊 上周社区核心话题概览**\n共发现 **{len(summary_list)}** 个讨论主题。"}, {"tag": "hr"}]
        for i, item in enumerate(sorted_topics):
            elements.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n▫️ 诉求: {item['summary']}\n▫️ 统计: 🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人\n▫️ 分类: #{item['category']}"})
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        feishu_client.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({date_str})"}, "template": "blue"}, "elements": elements})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])