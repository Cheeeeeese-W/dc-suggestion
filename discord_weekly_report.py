import os
import discord
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# --- 环境校验与配置 ---
def get_conf():
    return {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
        "CHANNEL_ID": int(os.getenv("DISCORD_CHANNEL_ID")),
        "AI_API_KEY": os.getenv("AI_API_KEY"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_WEBHOOK": os.getenv("FEISHU_WEBHOOK_URL"),
        "BITABLE_TOKEN": os.getenv("FEISHU_BITABLE_APP_TOKEN"),
        "BITABLE_TABLE_ID": os.getenv("FEISHU_BITABLE_TABLE_ID"),
        "APP_ID": os.getenv("FEISHU_APP_ID"),
        "APP_SECRET": os.getenv("FEISHU_APP_SECRET")
    }

CONF = get_conf()

# --- 飞书 API 工具 ---
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        res = requests.post(url, json={"app_id": CONF["APP_ID"], "app_secret": CONF["APP_SECRET"]})
        return res.json().get("tenant_access_token")

    def add_bitable_record(self, fields):
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"fields": fields})

# --- 核心逻辑 ---
class AdvancedBot(discord.Client):
    async def on_ready(self):
        print(f"✅ 系统启动: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        start_time, end_time = self.get_range()
        feishu = FeishuClient()
        genai.configure(api_key=CONF["AI_API_KEY"])
        model = genai.GenerativeModel(CONF["AI_MODEL"])

        # 1. 获取帖子列表
        threads = []
        async for thread in channel.archived_threads(before=end_time, limit=50):
            if thread.created_at >= start_time: threads.append(thread)
        for thread in channel.threads:
            if start_time <= thread.created_at <= end_time: threads.append(thread)

        all_analysis_data = []
        
        for thread in threads:
            # 2. 热度模型计算
            starter = await thread.fetch_message(thread.id)
            users = set()
            reaction_count = sum([r.count for r in starter.reactions])
            
            # 抓取前10条回复计算参与面和情绪
            replies_text = []
            async for msg in thread.history(limit=10):
                users.add(msg.author.id)
                replies_text.append(f"{msg.author.name}: {msg.content}")

            heat_score = (thread.message_count * 3) + (reaction_count * 1)
            unique_users = len(users)

            # 3. AI 单帖深度分析 (情绪 + 总结 + 分类)
            prompt = f"""
            分析帖子【{thread.name}】正文：{starter.content[:500]}
            及其讨论内容：{" ".join(replies_text)[:500]}
            要求：
            1. 情绪评分 (1-10，1分最愤怒，10分最满意)。
            2. 模块分类 (仅选其一：战斗平衡、赛季机制、日常活动、BUG反馈、UI交互)。
            3. 一句话核心总结。
            格式：分数|分类|总结
            """
            try:
                ai_res = model.generate_content(prompt).text.strip().split("|")
                sentiment, category, summary = ai_res[0], ai_res[1], ai_res[2]
            except:
                sentiment, category, summary = 5, "未知", "无法总结"

            # 4. 写入飞书多维表格
            record = {
                "日期": int(thread.created_at.timestamp() * 1000),
                "模块分类": category,
                "热度分": heat_score,
                "参与人数": unique_users,
                "AI核心总结": summary,
                "情绪得分": int(sentiment),
                "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}"
            }
            feishu.add_bitable_record(record)
            all_analysis_data.append(record)

        # 5. 生成总体周报并发送卡片
        if all_analysis_data:
            await self.send_weekly_card(all_analysis_data)
        
        await self.close()

    def get_range(self):
        now = datetime.now(timezone.utc)
        this_mon = (now - timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        return this_mon - timedelta(days=7), this_mon - timedelta(seconds=1)

    async def send_weekly_card(self, data):
        # 按热度排序取前三
        top_3 = sorted(data, key=lambda x: x['热度分'], reverse=True)[:3]
        
        # 构造卡片内容
        elements = [
            {"tag": "markdown", "content": f"**📈 本周概览**\n收集建议: {len(data)} 条\n高参与讨论: {len(top_3)} 处"},
            {"tag": "hr"}
        ]
        
        for i, item in enumerate(top_3):
            color = "🔴" if item['情绪得分'] < 4 else "🟡" if item['情绪得分'] < 7 else "🟢"
            elements.append({
                "tag": "markdown",
                "content": f"{color} **TOP {i+1}: {item['AI核心总结']}**\n🔥 热度: {item['热度分']} | 👥 参与: {item['参与人数']}人"
            })

        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看多维表格详情"},
                "type": "primary",
                "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"
            }]
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "🗓️ 玩家建议周报汇总"}, "template": "wathet"},
                "elements": elements
            }
        }
        requests.post(CONF["FEISHU_WEBHOOK"], json=payload)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])