import os
import discord
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# --- 配置获取 (保持不变) ---
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

# --- 飞书客户端 (优化了错误处理) ---
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
        response = requests.post(url, headers=headers, json={"fields": fields})
        if response.status_code != 200:
            print(f"写入多维表格失败: {response.text}")

# --- 机器人主逻辑 (增加了容错机制) ---
class AdvancedBot(discord.Client):
    async def on_ready(self):
        print(f"✅ 系统启动: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        start_time, end_time = self.get_range()
        feishu = FeishuClient()
        genai.configure(api_key=CONF["AI_API_KEY"])
        model = genai.GenerativeModel(CONF["AI_MODEL"])

        # 获取帖子列表
        threads = []
        async for thread in channel.archived_threads(before=end_time, limit=100):
            if thread.created_at >= start_time: threads.append(thread)
        for thread in channel.threads:
            if start_time <= thread.created_at <= end_time: threads.append(thread)

        print(f"找到上周帖子共 {len(threads)} 个")
        all_analysis_data = []
        
        for thread in threads:
            try:
                # --- 重点修复：更稳健的消息获取逻辑 ---
                starter_msg = None
                try:
                    # 尝试通过 ID 直接获取
                    starter_msg = await thread.fetch_message(thread.id)
                except discord.NotFound:
                    # 如果 404，尝试抓取历史记录中的第一条
                    async for m in thread.history(limit=1, oldest_first=True):
                        starter_msg = m
                
                if not starter_msg:
                    print(f"⚠️ 帖子 {thread.name} 无法找到正文，跳过")
                    continue

                # 活跃度与参与面计算
                reaction_count = sum([r.count for r in starter_msg.reactions])
                users = {starter_msg.author.id}
                replies_text = [f"楼主: {starter_msg.content}"]
                
                async for msg in thread.history(limit=15): # 增加采样深度到15条
                    users.add(msg.author.id)
                    if msg.id != starter_msg.id:
                        replies_text.append(f"{msg.author.name}: {msg.content}")

                heat_score = (thread.message_count * 3) + (reaction_count * 1)
                unique_users = len(users)

                # AI 深度分析
                prompt = f"""
                分析游戏建议帖子【{thread.name}】
                正文：{starter_msg.content[:1000]}
                讨论摘录：{" ".join(replies_text)[:1000]}
                要求：
                1. 情绪评分 (1-10，1分最愤怒/Bug，10分最满意)。
                2. 模块分类 (仅选其一：战斗平衡、赛季机制、日常活动、BUG反馈、UI交互)。
                3. 用一句话总结核心诉求，必须包含“建议”二字。
                格式：分数|分类|总结
                """
                response = model.generate_content(prompt)
                ai_res = response.text.strip().split("|")
                sentiment = int(ai_res[0]) if ai_res[0].isdigit() else 5
                category = ai_res[1]
                summary = ai_res[2]

                # 记录数据
                record_fields = {
                    "日期": int(thread.created_at.timestamp() * 1000),
                    "模块分类": category,
                    "热度分": heat_score,
                    "参与人数": unique_users,
                    "AI核心总结": summary,
                    "情绪得分": sentiment,
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}"
                }
                feishu.add_bitable_record(record_fields)
                all_analysis_data.append(record_fields)
                print(f"✅ 处理完成: {thread.name}")

            except Exception as e:
                print(f"❌ 处理帖子 {thread.name} 时出错: {e}")
                continue

        # 发送汇总卡片
        if all_analysis_data:
            await self.send_weekly_card(all_analysis_data)
        
        await self.close()

    def get_range(self):
        now = datetime.now(timezone.utc)
        this_mon = (now - timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        return this_mon - timedelta(days=7), this_mon - timedelta(seconds=1)

    async def send_weekly_card(self, data):
        top_3 = sorted(data, key=lambda x: x['热度分'], reverse=True)[:3]
        elements = [
            {"tag": "markdown", "content": f"**📈 本周概览**\n共收集有效建议: {len(data)} 条\n数据已自动归档至多维表格。"},
            {"tag": "hr"}
        ]
        
        for i, item in enumerate(top_3):
            # 根据情绪得分显示不同颜色的状态灯
            status_emoji = "🔴" if item['情绪得分'] <= 3 else "🟡" if item['情绪得分'] <= 6 else "🟢"
            elements.append({
                "tag": "markdown",
                "content": f"{status_emoji} **TOP{i+1}: {item['AI核心总结']}**\n🔥 热度分: {item['热度分']} | 👥 独立参与: {item['参与人数']}人"
            })

        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔍 查看多维表格全文"},
                "type": "primary",
                "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"
            }]
        })

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "🎮 玩家建议周报汇总"}, "template": "blue"},
                "elements": elements
            }
        }
        requests.post(CONF["FEISHU_WEBHOOK"], json=payload)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])