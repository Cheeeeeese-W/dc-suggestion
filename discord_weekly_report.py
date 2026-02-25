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
            feishu = FeishuClient()
            genai.configure(api_key=CONF["AI_API_KEY"])
            ai_model = genai.GenerativeModel(CONF["AI_MODEL"])
            
            start_time, end_time = self.get_range()
            date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
            
            # 1. 第一步：抓取所有帖子
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

                    # --- 统一使用中文键名，确保与后续逻辑一致 ---
                    raw_threads_data.append({
                        "id": index,
                        "标题": thread.name,
                        "内容": starter_msg.content[:500],
                        "热度分": (thread.message_count * 3) + (reaction_count * 1),
                        "参与人数": len(unique_users),
                        "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
                        "日期": int(thread.created_at.timestamp() * 1000)
                    })
                except Exception as e:
                    print(f"⚠️ 预处理帖子 {thread.name} 失败: {e}")

            if not raw_threads_data:
                print("📭 无有效内容，任务结束"); await self.close(); return

            # 2. 第二步：AI 批量分析
            print(f"🤖 正在向 AI 发起批量请求 (包含 {len(raw_threads_data)} 条帖子)...")
            items_str = ""
            for item in raw_threads_data:
                items_str += f"ID: {item['id']} | 标题: {item['标题']} | 内容: {item['内容']}\n"

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
                response = ai_model.generate_content(prompt)
                json_str = response.text.replace('```json', '').replace('```', '').strip()
                ai_results = json.loads(json_str)
                ai_map = {item['id']: item for item in ai_results}
                
                for original in raw_threads_data:
                    ai_info = ai_map.get(original['id'], {"sentiment": 5, "category": "未分类", "summary": original['标题']})
                    # 合并数据，统一键名
                    enriched_item = {
                        **original,
                        "情绪得分": ai_info.get('sentiment', 5),
                        "模块分类": ai_info.get('category', '未分类'),
                        "AI核心总结": ai_info.get('summary', original['标题'])
                    }
                    all_enriched_data.append(enriched_item)

                    # 3. 第三步：写入多维表格
                    feishu.add_bitable_record(enriched_item)
                    print(f"✅ 已归档: {original['标题']}")

            except Exception as e:
                print(f"❌ AI 分析失败: {e}，将使用基础数据推送")
                # 失败兜底逻辑
                for original in raw_threads_data:
                    enriched_item = {**original, "情绪得分": 5, "模块分类": "未分类", "AI核心总结": original['标题']}
                    all_enriched_data.append(enriched_item)

            # 4. 第四步：推送卡片
            if all_enriched_data:
                await self.send_weekly_card(all_enriched_data, feishu, date_display)
            
            await self.close()

        async def send_weekly_card(self, data, feishu_client, date_str):
            # 现在所有 item 都有 '热度分' 这个键了
            top_3 = sorted(data, key=lambda x: x.get('热度分', 0), reverse=True)[:3]
            
            elements = [
                {"tag": "markdown", "content": f"**📈 本周社区概览**\n共收集有效建议: {len(data)} 条\n数据已自动归档至多维表格。"},
                {"tag": "hr"}
            ]
            
            for i, item in enumerate(top_3):
                # 获取 AI 总结和情绪，如果缺失则提供默认值
                summary = item.get('AI核心总结', item.get('标题', '无总结'))
                sentiment = item.get('情绪得分', 5)
                status_emoji = "🔴" if sentiment <= 3 else "🟡" if sentiment <= 6 else "🟢"
                
                elements.append({
                    "tag": "markdown",
                    "content": f"{status_emoji} **TOP{i+1}: {summary}**\n🔥 热度分: {item.get('热度分', 0)} | 👥 独立参与: {item.get('参与人数', 0)}人"
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