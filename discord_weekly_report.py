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
        "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://api.999789.best/v1"),
        "AI_MODEL": os.getenv("AI_MODEL", "gemini-1.5-flash"),
        "FEISHU_APP_ID": os.getenv("FEISHU_APP_ID"),
        "FEISHU_APP_SECRET": os.getenv("FEISHU_APP_SECRET"),
        "BITABLE_TOKEN": os.getenv("FEISHU_BITABLE_APP_TOKEN"),
        "BITABLE_TABLE_ID": os.getenv("FEISHU_BITABLE_TABLE_ID"),
        "REFERENCE_TABLE_ID": os.getenv("FEISHU_REFERENCE_TABLE_ID"),
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")
    }

CONF = get_conf()

# ===================== 飞书客户端 (自建应用版) =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]}, timeout=10)
            data = res.json()
            if data.get("code") != 0:
                print(f"❌ 飞书鉴权失败: {data.get('msg')}")
                return None
            return data.get("tenant_access_token")
        except Exception as e:
            print(f"❌ 无法连接飞书服务器: {e}")
            return None

    def get_reference_tags(self):
        """获取历史标签，确保跨周一致性"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            tags = [r['fields'].get('话题标签') for r in items if r['fields'].get('话题标签')]
            return list(set(tags))
        except: return []

    def add_new_tags(self, new_tags):
        """自动增量更新话题库"""
        if not self.token or not new_tags: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"records": [{"fields": {"话题标签": tag}} for tag in new_tags]}
        requests.post(url, headers=headers, json=payload, timeout=10)

    def batch_add_bitable_records(self, records_list):
        """批量同步主数据表"""
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
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=10)
        print(f"📊 多维表格录入状态: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        """通过自建应用机器人发送卡片"""
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"🚀 飞书卡片推送结果: {res.json().get('msg')}")

# ===================== Discord 机器人主逻辑 =====================
class AdvancedBot(discord.Client):
    
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_monday - timedelta(days=7), this_monday - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统就绪: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        
        # 修正 Base URL
        base_url = CONF["AI_BASE_URL"].strip()
        if not base_url.endswith('/v1'): base_url = base_url.rstrip('/') + '/v1'
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=base_url)
        
        history_tags = feishu.get_reference_tags()
        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        # 1. 获取帖子
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        print(f"🔍 扫描到上周帖子: {len(threads)} 个")
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
                async for msg in thread.history(limit=15): unique_users.add(msg.author.id)

                raw_threads_data.append({
                    "id": index, "标题": thread.name, "内容": starter_msg.content[:500],
                    "热度分": int((thread.message_count * 3) + (reaction_count * 1)),
                    "参与人数_list": list(unique_users),
                    "帖子链接": f"https://discord.com/channels/{thread.guild.id}/{thread.id}",
                    "日期": int(thread.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_threads_data:
            print("📭 上周无有效数据。"); await self.close(); return

        # 2. AI 批量分析 (增加 HTML 拦截与 JSON 容错)
        print(f"🤖 正在向 AI 发起批量分析 (包含 {len(raw_threads_data)} 条帖子)...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_threads_data])
        tags_str = ", ".join(history_tags) if history_tags else "暂无"

        prompt = f"""
        你是一个资深游戏策划。请分析以下《DarkWar》玩家建议并输出 JSON。
        
        【参考话题】：[{tags_str}] (优先使用匹配的历史标签)
        【分类体系】：战斗平衡、赛季机制、日常活动、系统优化、UI/交互、商业化。

        严格输出 JSON 列表 []，字段: id, sentiment(1-10), category, sub_category, summary, topic_label。
        不要输出任何 Markdown 标记以外的文字。
        数据清单：\n{items_str}
        """

        all_enriched_data = []
        new_tags = set()
        try:
            response = await ai_client.chat.completions.create(
                model=CONF["AI_MODEL"],
                messages=[{"role": "user", "content": prompt}]
            )
            json_str = response.choices[0].message.content.strip()

            # HTML 检查与清理
            if "<html" in json_str.lower(): raise ValueError("AI 返回了网页 HTML")
            if "```json" in json_str: json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str: json_str = json_str.split("```")[1].strip()
            
            ai_results = json.loads(json_str)
            if isinstance(ai_results, dict):
                for k in ai_results:
                    if isinstance(ai_results[k], list): ai_results = ai_results[k]; break
            
            ai_map = {item['id']: item for item in ai_results if isinstance(item, dict)}
            
            for o in raw_threads_data:
                ai_info = ai_map.get(o['id'], {})
                label = str(ai_info.get('topic_label', o['标题']))
                if label not in history_tags: new_tags.add(label)

                all_enriched_data.append({
                    **o, "模块分类": ai_info.get('category', '其他'), "二级分类": ai_info.get('sub_category', '通用'),
                    "情绪得分": int(ai_info.get('sentiment', 5)), "AI核心总结": ai_info.get('summary', o['标题']),
                    "话题标签": label, "参与人数": len(o["参与人数_list"])
                })
        except Exception as e:
            print(f"⚠️ AI 分析失效 ({e})，进入降级模式。")
            for o in raw_threads_data:
                all_enriched_data.append({
                    **o, "模块分类": "未分类", "二级分类": "待定", "情绪得分": 5,
                    "AI核心总结": o['标题'], "话题标签": o['标题'], "参与人数": len(o["参与人数_list"])
                })

        # 3. 排序、录入表格、更新标签库
        all_enriched_data.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched_data)
        feishu.add_new_tags(list(new_tags))

        # 4. 话题聚合统计
        topic_groups = {}
        for item in all_enriched_data:
            label = item["话题标签"]
            if label not in topic_groups:
                topic_groups[label] = {"topic": label, "cat": item["模块分类"], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
            g = topic_groups[label]
            g["heat"] += item["热度分"]; g["count"] += 1; g["users"].update(item.get("参与人数_list", []))

        # 5. 推送汇总卡片
        summary_list = [{"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]} for v in topic_groups.values()]
        if summary_list:
            await self.send_weekly_card(summary_list, feishu, date_display)
        
        await self.close()

    async def send_weekly_card(self, summary_list, feishu_client, date_str):
        sorted_topics = sorted(summary_list, key=lambda x: x['total_heat'], reverse=True)[:5]
        elements = [{"tag": "markdown", "content": f"**📊 上周社区话题聚合概览**\n共识别主题: {len(summary_list)} 个"}, {"tag": "hr"}]
        for i, item in enumerate(sorted_topics):
            elements.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary']}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人 | #{item['category']}"})
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情库"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        
        card = {"config": {"enable_forward": True}, "header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({date_str})"}, "template": "blue"}, "elements": elements}
        feishu_client.send_group_card(card)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])