import asyncio
import os
import json
import discord
import requests
import google.generativeai as genai
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# 加载环境变量
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
        data = res.json()
        if data.get("code") != 0:
            print(f"❌ 飞书鉴权失败! 请检查 AppID/Secret。错误信息: {data.get('msg')}")
            return None
        return data.get("tenant_access_token")

    def add_bitable_record(self, fields):
        if not self.token: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        
        # 构造符合飞书“超链接”字段要求的对象
        post_link = fields.get("帖子链接")
        link_field = {"text": "点击查看帖子", "link": post_link} if post_link else None

        payload = {
            "fields": {
                "日期": fields.get("日期"),
                "模块分类": fields.get("模块分类"),
                "热度分": fields.get("热度分"),
                "参与人数": fields.get("参与人数"),
                "AI核心总结": fields.get("AI核心总结"),
                "情绪得分": fields.get("情绪得分"),
                "帖子链接": link_field  # 发送对象而不是纯字符串
            }
        }
        res = requests.post(url, headers=headers, json=payload)
        res_data = res.json()
        if res_data.get("code") != 0:
            print(f"❌ 多维表格写入失败! 标题: {fields.get('标题')} | 错误: {res_data.get('msg')}")
        else:
            print(f"✅ 已成功录入多维表格: {fields.get('标题')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {
            "receive_id": CONF["FEISHU_CHAT_ID"],
            "msg_type": "interactive",
            "content": json.dumps(card_content)
        }
        res = requests.post(url, headers=headers, json=payload)
        res_data = res.json()
        if res_data.get("code") != 0:
            print(f"❌ 飞书群卡片发送失败! 错误: {res_data.get('msg')} | 详情: {res_data}")
        else:
            print(f"✅ 飞书群周报卡片发送成功！")

# ===================== Discord 机器人主逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
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
        # 使用异步模型接口
        ai_model = genai.GenerativeModel(CONF["AI_MODEL"])
        
        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 1. 抓取帖子
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        print(f"📊 识别到上周帖子共 {len(threads)} 个，准备批量分析...")
        
        raw_threads_data = []
        for index, thread in enumerate(threads):
            try:
                starter_msg = None
                try:
                    starter_msg = await thread.fetch_message(thread.id)
                except:
                    async for m in thread.history(limit=1, oldest_first=True): starter_msg = m
                if not starter_msg: continue

                reaction_count = sum([r.count for r in starter_msg.reactions])
                unique_users = {starter_msg.author.id}
                async for msg in thread.history(limit=15): unique_users.add(msg.author.id)

                raw_threads_data.append({
                    "id": index, "标题": thread.name, "内容": starter_msg.content[:500],
                    "热度分": (thread.message_count * 3) + (reaction_count * 1),
                    "参与人数": len(unique_users),
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
                    "日期": int(thread.created_at.timestamp() * 1000)
                })
            except Exception as e:
                print(f"⚠️ 预处理帖子 {thread.name} 失败: {e}")

        if not raw_threads_data:
            print("📭 无有效内容"); await self.close(); return

        # 2. AI 批量分析 (关键修改：使用 generate_content_async)
        print(f"🤖 正在向 AI 发起批量请求 (包含 {len(raw_threads_data)} 条建议)...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_threads_data])

        prompt = f"""
        你是一个游戏社区数据分析师。请分析以下玩家建议。
        请严格按 JSON 格式输出一个列表 []，每个对象包含：
        - id: 对应的 ID
        - sentiment: 情绪打分(1-10)
        - category: 模块分类(选一：战斗平衡、赛季机制、日常活动、BUG反馈、UI交互)
        - summary: 一句话建议总结(含“建议”二字)
        数据：\n{items_str}
        """

        all_enriched_data = []
        try:
            # --- 使用 await 和 generate_content_async 防止阻塞心跳 ---
            response = await ai_model.generate_content_async(prompt)
            
            json_str = response.text.replace('```json', '').replace('```', '').strip()
            ai_results = json.loads(json_str)
            ai_map = {item['id']: item for item in ai_results}
            
            for original in raw_threads_data:
                ai_info = ai_map.get(original['id'], {"sentiment": 5, "category": "未分类", "summary": original['标题']})
                enriched_item = {
                    **original,
                    "情绪得分": ai_info.get('sentiment', 5),
                    "模块分类": ai_info.get('category', '未分类'),
                    "AI核心总结": ai_info.get('summary', original['标题'])
                }
                all_enriched_data.append(enriched_item)
                feishu.add_bitable_record(enriched_item)
                print(f"✅ 已归档: {original['标题']}")
                # 小技巧：每写一行稍微歇一下，让出心跳时间
                await asyncio.sleep(0.1) 

        except Exception as e:
            print(f"❌ AI 分析或归档失败: {e}")
            for original in raw_threads_data:
                all_enriched_data.append({**original, "情绪得分": 5, "模块分类": "未分类", "AI核心总结": original['标题']})

        # 4. 推送汇总卡片
        if all_enriched_data:
            print("🚀 准备推送飞书周报卡片...")
            try:
                await self.send_weekly_card(all_enriched_data, feishu, date_display)
            except Exception as e:
                print(f"❌ 飞书卡片推送失败: {e}")
        
        print("🎉 任务圆满完成。")
        await self.close()

    async def send_weekly_card(self, data, feishu_client, date_str):
        # 排序并取热度前三
        top_3 = sorted(data, key=lambda x: x.get('热度分', 0), reverse=True)[:3]
        
        elements = [
            {"tag": "markdown", "content": f"**📈 本周社区概览**\n共收集有效建议: {len(data)} 条\n数据已自动归档至多维表格。"},
            {"tag": "hr"}
        ]

        for i, item in enumerate(top_3):
            sentiment = item.get('情绪得分', 5)
            color = "🔴" if sentiment <= 3 else "🟡" if sentiment <= 6 else "🟢"
            summary = item.get('AI核心总结', '无总结')
            elements.append({
                "tag": "markdown",
                "content": f"{color} **TOP{i+1}: {summary}**\n🔥 热度分: {item.get('热度分', 0)} | 👥 独立参与: {item.get('参与人数', 0)}人"
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

# ===================== 启动 =====================
if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    bot = AdvancedBot(intents=intents)
    bot.run(CONF["DISCORD_TOKEN"])