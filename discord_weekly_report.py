import os
import discord
import requests
import json
import google.generativeai as genai
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
    """直接使用 Google 官方 Gemini 库进行分析"""
    try:
        # 配置 API Key
        genai.configure(api_key=CONF["AI_API_KEY"])
        
        # 初始化模型 (CONF["AI_MODEL"] 填 gemini-1.5-flash 即可)
        model = genai.GenerativeModel(model_name=CONF["AI_MODEL"])
        
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
        
        # 生成内容
        response = model.generate_content(prompt)
        
        # 返回结果文本
        return response.text
        
    except Exception as e:
        return f"Gemini 分析过程中出现异常: {str(e)}"

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
            
            messages = []

            # --- 情况 A: 普通文本频道 ---
            if isinstance(channel, discord.TextChannel):
                print(f"正在读取文本频道消息: {date_display}...")
                async for message in channel.history(limit=2000, after=start_time, before=end_time):
                    if not message.author.bot and len(message.content) > 2:
                        messages.append(f"[{message.author.name}]: {message.content}")

            # --- 情况 B: 论坛频道 (Forum Channel) ---
            elif isinstance(channel, discord.ForumChannel):
                print(f"检测到论坛频道，正在抓取上周帖子的正文内容...")
                
                # 合并活跃帖子和归档帖子进行处理
                all_threads = []
                
                # 1. 获取活跃帖子
                for thread in channel.threads:
                    if start_time <= thread.created_at <= end_time:
                        all_threads.append(thread)
                
                # 2. 获取归档帖子
                async for thread in channel.archived_threads(before=end_time, limit=100):
                    if thread.created_at < start_time:
                        break
                    all_threads.append(thread)

                print(f"找到上周创建的帖子共 {len(all_threads)} 个，开始提取正文...")

                for thread in all_threads:
                    try:
                        # 在 Discord 中，帖子的 ID 就是第一条消息的 ID
                        starter_message = await thread.fetch_message(thread.id)
                        content = starter_message.content
                        # 将标题和正文组合在一起
                        messages.append(f"【建议标题：{thread.name}】\n内容：{content}")
                    except Exception as e:
                        print(f"无法获取帖子 '{thread.name}' 的正文: {e}")
                        # 如果获取不到正文，至少保留标题
                        messages.append(f"【建议标题：{thread.name}】 (正文提取失败)")
            
            else:
                print(f"暂不支持的频道类型: {type(channel)}")
                await self.close()
                return

            # --- AI 分析与推送 ---
            if messages:
                print(f"共获取到 {len(messages)} 条内容，正在调用 AI 分析...")
                raw_data = "\n\n---\n\n".join(messages)
                
                # 防止数据过长导致 AI 接口报错（针对 Gemini 1.5 Flash 这个问题不严重，但建议保留）
                if len(raw_data) > 30000:
                    raw_data = raw_data[:30000] + "\n...(内容过多已截断)"
                    
                report_summary = analyze_with_ai(raw_data)
                push_to_feishu(report_summary, date_display)
                print("✅ 报告已生成并发送至飞书。")
            else:
                print("📭 该时间段内无新建议，跳过报告生成。")
            
            await self.close()

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    bot = ReportBot(intents=intents)
    bot.run(CONF["DISCORD_TOKEN"])