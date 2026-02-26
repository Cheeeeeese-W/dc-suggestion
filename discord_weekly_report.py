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
        "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://api.999789.best"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_APP_ID": os.getenv("FEISHU_APP_ID"),
        "FEISHU_APP_SECRET": os.getenv("FEISHU_APP_SECRET"),
        "BITABLE_TOKEN": os.getenv("FEISHU_BITABLE_APP_TOKEN"),
        "BITABLE_TABLE_ID": os.getenv("FEISHU_BITABLE_TABLE_ID"),
        "REFERENCE_TABLE_ID": os.getenv("FEISHU_REFERENCE_TABLE_ID"),
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

    def get_reference_tags(self):
        """读取历史参考标签"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500})
            tags = [r['fields'].get('话题标签') for r in res.json().get('data', {}).get('items', []) if r['fields'].get('话题标签')]
            return list(set(tags))
        except: return []

    def add_new_tags(self, new_tags):
        """同步新标签到参考表"""
        if not self.token or not new_tags: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"records": [{"fields": {"话题标签": tag}} for tag in new_tags]}
        requests.post(url, headers=headers, json=payload)

    def batch_add_bitable_records(self, records_list):
        """批量同步主表数据"""
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        
        formatted = []
        for r in records_list:
            formatted.append({
                "fields": {
                    "日期": r.get("日期"),
                    "模块分类": r.get("模块分类"),
                    "二级分类": r.get("二级分类"),
                    "热度分": int(r.get("热度分", 0)),
                    "参与人数": int(r.get("参与人数", 0)),
                    "AI核心总结": str(r.get("AI核心总结", "")),
                    "情绪得分": int(r.get("情绪得分", 5)),
                    "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
                }
            })
        res = requests.post(url, headers=headers, json={"records": formatted})
        print(f"表格同步记录: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        requests.post(url, headers=headers, json=payload)

# ===================== Discord 机器人主逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_mon - timedelta(days=7), this_mon - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统启动，登录身份: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        
        # 初始化 OpenAI 异步客户端 (适配中转地址)
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=CONF["AI_BASE_URL"])
        
        # 加载历史参考标签
        history_tags = feishu.get_reference_tags()
        print(f"📚 已读取 {len(history_tags)} 个历史标签。")

        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 1. 获取帖子
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
            print("📭 无有效帖子数据"); await self.close(); return

        # 2. AI 批量聚合分析 (OpenAI 兼容接口)
        print(f"🤖 正在调用 AI 进行同类归纳分析...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_threads_data])
        tags_str = ", ".join(history_tags) if history_tags else "暂无"

        prompt = f"""
        你是一个专业的游戏策划分析师。请分析玩家建议。
        
        【话题命名规范】：
        - 优先从历史参考中选择匹配的标签：[{tags_str}]。
        - 讨论同一个问题的建议必须使用完全相同的 topic_label。
        
        【分类参考】：
        - 一级分类：战斗平衡、赛季机制、日常活动、系统优化、UI/交互、商业化。
        - 二级分类：具体模块名(如：移民门槛)。

        请严格输出 JSON 列表格式 []，字段包含: id, sentiment(1-10), category, sub_category, summary, topic_label。
        数据：\n{items_str}
        """

        try:
            response = await ai_client.chat.completions.create(
                model=CONF["AI_MODEL"],
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            
            json_str = response.choices[0].message.content.strip()
            # 兼容性清理
            if "```json" in json_str: json_str = json_str.split("```json")[1].split("```")[0].strip()
            
            ai_results = json.loads(json_str)
            if isinstance(ai_results, dict): # 适配某些 AI 的包装格式
                for k in ai_results:
                    if isinstance(ai_results[k], list): ai_results = ai_results[k]; break
            
            ai_map = {item['id']: item for item in ai_results}
            all_enriched_data = []
            topic_groups = {}
            new_tags = set()

            for original in raw_threads_data:
                ai_info = ai_map.get(original['id'], {"sentiment": 5, "category": "其他", "sub_category": "通用", "summary": original['标题'], "topic_label": original['标题']})
                
                label = ai_info.get('topic_label', '未分类话题')
                if label not in history_tags: new_tags.add(label)

                item = {
                    **original,
                    "模块分类": str(ai_info.get('category')),
                    "二级分类": str(ai_info.get('sub_category')),
                    "情绪得分": int(ai_info.get('sentiment', 5)),
                    "AI核心总结": str(ai_info.get('summary')),
                    "参与人数": int(len(original["参与人数_list"])),
                    "话题标签": label
                }
                all_enriched_data.append(item)

                # 话题聚合逻辑 (卡片展示)
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].update(original["参与人数_list"])

            # --- 排序并写入 ---
            all_enriched_data.sort(key=lambda x: x['日期'], reverse=True)
            feishu.batch_add_bitable_records(all_enriched_data)
            feishu.add_new_tags(list(new_tags))

            # --- 推送概览卡片 ---
            summary_list = [{"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
            await self.send_weekly_card(summary_list, feishu, date_display)

        except Exception as e:
            print(f"❌ 分析或同步失败: {e}")
        
        await self.close()

    async def send_weekly_card(self, summary_list, feishu_client, date_str):
        sorted_topics = sorted(summary_list, key=lambda x: x['total_heat'], reverse=True)[:5]
        elements = [{"tag": "markdown", "content": f"**📊 上周社区话题聚合概览**\n识别到主题: {len(summary_list)} 个"}, {"tag": "hr"}]
        for i, item in enumerate(sorted_topics):
            elements.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary']}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人讨论 | #{item['category']}"})
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情库"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        
        card = {"config": {"enable_forward": True}, "header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({date_str})"}, "template": "blue"}, "elements": elements}
        feishu_client.send_group_card(card)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])