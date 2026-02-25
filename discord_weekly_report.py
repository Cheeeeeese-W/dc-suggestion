import os
import discord
import requests
import json
import openai
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# 加载同目录下的 .env 文件
load_dotenv()

# ===================== 从环境变量获取配置 =====================
def get_env_config():
    config = {
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
        "CHANNEL_ID": int(os.getenv("DISCORD_CHANNEL_ID", 0)),
        "AI_API_KEY": os.getenv("AI_API_KEY"),
        "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://api.openai.com/v1"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_URL": os.getenv("FEISHU_WEBHOOK_URL"),
        "KEYWORD": os.getenv("FEISHU_KEYWORD", "建议")
    }
    
    # 基础校验
    missing = [k for k, v in config.items() if not v]
    if missing:
        raise ValueError(f"缺少关键环境变量设置: {', '.join(missing)}")
    return config

CONF = get_env_config()

# ===================== 工具函数 =====================

def get_last_week_range():
    """获取上周一 00:00 到 上周日 23:59 (UTC)"""
    now = datetime.now(timezone.utc)
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(seconds=1)
    return last_monday, last_sunday

def analyze_with_ai(content):
    """调用 AI 分析内容"""
    client = openai.OpenAI(api_key=CONF["AI_API_KEY"], base_url=CONF["AI_BASE_URL"])
    prompt = f"""
    你是一个游戏社区数据分析师。请对以下 Discord 玩家提给出的{CONF['KEYWORD']}进行周报总结。
    
    要求：
    1. 归纳 3 个最核心的玩家关注点。
    2. 按分类（Bug、体验优化、新功能、平衡性）列出要点。
    3. 必须包含关键词：{CONF['KEYWORD']}。
    4. 采用 Markdown 格式，输出要精简、专业。

    数据：
    {content}
    """
    try:
        response = client.chat.completions.create(
            model=CONF["AI_MODEL"],
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"AI 分析过程中出现异常: {str(e)}"

def push_to_feishu(text, date_str):
    """推送飞书卡片"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"📊 玩家{CONF['KEYWORD']}周报 ({date_str})"}
            },
            "elements": [
                {"tag": "markdown", "content": text},
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "自动分析自 Discord 频道"}]
                }
            ]
        }
    }
    try:
        res = requests.post(CONF["FEISHU_URL"], json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"飞书推送失败: {e}")

# ===================== Discord 客户端 =====================

class ReportBot(discord.Client):
    async def on_ready(self):
        print(f"已连接 Discord: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        if not channel:
            print("错误：无法访问指定频道，请检查 ID 或机器人权限。")
            await self.close()
            return

        start_time, end_time = get_last_week_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        print(f"正在读取 {date_display} 期间的消息...")
        
        messages = []
        async for message in channel.history(limit=1000, after=start_time, before=end_time):
            if not message.author.bot and len(message.content) > 2:
                messages.append(f"[{message.author.name}]: {message.content}")

        if messages:
            report_summary = analyze_with_ai("\n".join(messages))
            push_to_feishu(report_summary, date_display)
            print("报告已生成并发送至飞书。")
        else:
            print("该时间段内无新消息，跳过报告生成。")
        
        await self.close()

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    bot = ReportBot(intents=intents)
    bot.run(CONF["DISCORD_TOKEN"])