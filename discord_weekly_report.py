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
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")  # 机器人所在的飞书群ID
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

    def add_bitable_record(self, fields):
        """往多维表格追加记录"""
        if not self.token: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"fields": fields})

    def send_group_card(self, card_content):
        """以机器人身份向群组发送卡片"""
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {
            "receive_id": CONF["FEISHU_CHAT_ID"],
            "msg_type": "interactive",
            "content": json.dumps(card_content) # 飞书要求content为JSON字符串
        }
        res = requests.post(url, headers=headers, json=payload)
        print(f"飞书推送结果: {res.json().get('msg')}")

# ===================== Discord 机器人主逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
        """获取上周一 00:00 到 上周日 23:59 (UTC)"""
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统启动，登录身份: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        if not channel:
            print("❌ 无法找到指定的频道ID"); await self.close(); return

        feishu = FeishuClient()
        genai.configure(api_key=CONF["AI_API_KEY"])
        ai_model = genai.GenerativeModel(CONF["AI_MODEL"])
        
        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 1. 获取帖子 (支持活跃和归档)
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        print(f"📊 识别到上周帖子共 {len(threads)} 个，开始深度分析...")
        all_analysis_data = []

        for thread in threads:
            try:
                # 获取第一条正文 (Starter Message)
                starter_msg = None
                try:
                    starter_msg = await thread.fetch_message(thread.id)
                except discord.NotFound:
                    async for m in thread.history(limit=1, oldest_first=True):
                        starter_msg = m
                
                if not starter_msg: continue

                # --- 热度模型计算 ---
                # 活跃度 (Activity)
                reaction_count = sum([r.count for r in starter_msg.reactions])
                heat_score = (thread.message_count * 3) + (reaction_count * 1)
                
                # 参与面 (Breadth)
                unique_users = {starter_msg.author.id}
                replies_sample = [f"楼主: {starter_msg.content}"]
                async for msg in thread.history(limit=20): # 采样前20条讨论
                    unique_users.add(msg.author.id)
                    if msg.id != starter_msg.id:
                        replies_sample.append(f"{msg.author.name}: {msg.content}")

                # --- AI 深度分析 (情绪/分类/总结) ---
                prompt = f"""
                你是策划助理。分析玩家建议帖子【{thread.name}】
                正文：{starter_msg.content[:800]}
                讨论摘录：{" ".join(replies_sample)[:800]}
                要求输出三部分内容，用 | 分隔：
                1. 情绪打分(1-10，1分最愤怒/Bug，10分最满意)。
                2. 模块分类(选一：战斗平衡、赛季机制、日常活动、BUG反馈、UI交互)。
                3. 一句话建议总结(必须含“建议”二字)。
                格式：分数|分类|总结
                """
                ai_res = ai_model.generate_content(prompt).text.strip().split("|")
                sentiment = int(ai_res[0]) if ai_res[0].strip().isdigit() else 5
                category = ai_res[1].strip()
                summary = ai_res[2].strip()

                # --- 写入多维表格 ---
                record_fields = {
                    "日期": int(thread.created_at.timestamp() * 1000),
                    "模块分类": category,
                    "热度分": heat_score,
                    "参与人数": len(unique_users),
                    "AI核心总结": summary,
                    "情绪得分": sentiment,
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}"
                }
                feishu.add_bitable_record(record_fields)
                all_analysis_data.append(record_fields)
                print(f"✅ 已归档: {thread.name} (热度:{heat_score})")

            except Exception as e:
                print(f"⚠️ 帖子 {thread.name} 处理失败: {e}")

        # 2. 推送飞书群卡片汇总
        if all_analysis_data:
            await self.send_weekly_card(all_analysis_data, feishu, date_display)
        
        print("🎉 本周报任务处理完毕。")
        await self.close()

    async def send_weekly_card(self, data, feishu_client, date_str):
        # 按热度分降序排列
        top_3 = sorted(data, key=lambda x: x['热度分'], reverse=True)[:3]
        
        elements = [
            {"tag": "markdown", "content": f"**📈 本周社区概览**\n共收集有效建议: {len(data)} 条\n高参与讨论: {len(top_3)} 处"},
            {"tag": "hr"}
        ]

        for i, item in enumerate(top_3):
            # 情绪灯提示
            color = "🔴" if item['情绪得分'] <= 3 else "🟡" if item['情绪得分'] <= 6 else "🟢"
            elements.append({
                "tag": "markdown",
                "content": f"{color} **TOP{i+1}: {item['AI核心总结']}**\n🔥 热度分: {item['热度分']} | 👥 独立参与: {item['参与人数']}人"
            })

        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔍 查看多维表格建议库"},
                "type": "primary",
                "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"
            }]
        })

        card_content = {
            "config": {"enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({date_str})"}, "template": "blue"},
            "elements": elements
        }
        feishu_client.send_group_card(card_content)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])