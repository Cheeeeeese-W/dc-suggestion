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

# ===================== 飞书客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]}, timeout=10)
            return res.json().get("tenant_access_token")
        except: return None

    def get_reference_pairs(self):
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            return [{"cat": r['fields'].get('二级分类'), "tag": r['fields'].get('话题标签')} for r in items if r['fields'].get('话题标签')]
        except: return []

    def add_new_reference_pairs(self, new_pairs_list):
        """同步新标签，包含详细错误侦察"""
        if not self.token or not new_pairs_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        
        records = []
        for p in new_pairs_list:
            if p[0] and p[1]:
                # 强制清理：确保字段名对应你的飞书参考表列名
                records.append({"fields": {"二级分类": str(p[0]), "话题标签": str(p[1])}})
        
        if not records: return
        res = requests.post(url, headers=headers, json={"records": records}, timeout=10)
        res_data = res.json()
        
        if res_data.get("code") == 0:
            print(f"📊 历史参考表成功更新: 写入 {len(records)} 条")
        else:
            print(f"❌ 历史参考表写入失败: {res_data.get('msg')}")
            # 调试雷达：获取该表的所有字段名，帮助用户比对
            print(f"🔍 正在检索参考表字段名以定位问题...")
            meta_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/fields"
            meta_res = requests.get(meta_url, headers=headers)
            field_names = [f.get('field_name') for f in meta_res.json().get('data', {}).get('items', [])]
            print(f"💡 参考表里真实的列名是: {field_names}")

    def batch_add_bitable_records(self, records_list):
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        formatted = []
        for r in records_list:
            formatted.append({"fields": {
                "日期": r.get("日期"), "模块分类": r.get("模块分类"), "二级分类": r.get("二级分类"),
                "话题标签": r.get("话题标签"), "热度分": int(r.get("热度分", 0)), "参与人数": int(r.get("参与人数", 0)),
                "AI核心总结": str(r.get("AI核心总结", "")), "情绪得分": int(r.get("情绪得分", 5)),
                "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
            }})
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=15)
        print(f"✅ 主表录入状态: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"🚀 飞书卡片推送: {res.json().get('msg')}")

# ===================== 机器人核心逻辑 =====================
class AdvancedBot(discord.Client):
    def get_range(self):
        now = datetime.now(timezone.utc)
        this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_mon - timedelta(days=7), this_mon - timedelta(seconds=1)

    async def on_ready(self):
        print(f"🚀 系统就绪: {self.user}")
        channel = self.get_channel(CONF["CHANNEL_ID"])
        feishu = FeishuClient()
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=CONF["AI_BASE_URL"].strip().rstrip('/') + '/v1')
        
        ref_pairs = feishu.get_reference_pairs()
        print(f"📚 已加载 {len(ref_pairs)} 条参考关系")
        ref_text = "\n".join([f"- {p['cat']}: {p['tag']}" for p in ref_pairs[:50]])

        start_time, end_time = self.get_range()
        date_display = f"{start_time.strftime('%Y/%m/%d')} - {end_time.strftime('%m/%d')}"
        
        threads = []
        async for t in channel.archived_threads(before=end_time, limit=100):
            if t.created_at >= start_time: threads.append(t)
        for t in channel.threads:
            if start_time <= t.created_at <= end_time: threads.append(t)

        raw_data = []
        for index, t in enumerate(threads):
            try:
                msg = None
                try: msg = await t.fetch_message(t.id)
                except: 
                    async for m in t.history(limit=1, oldest_first=True): msg = m
                if not msg: continue
                raw_data.append({
                    "id": index, "标题": t.name, "内容": msg.content[:500],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "owner_id": msg.author.id, "参与人数": 1,
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                    "日期": int(t.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_data: await self.close(); return

        all_enriched, new_ref_pairs = [], set()
        print(f"🤖 正在调用 AI 分析...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        prompt = f"""分析反馈并输出 JSON []。参考记忆：\n{ref_text}\n数据：\n{items_str}"""

        try:
            res = await ai_client.chat.completions.create(model=CONF["AI_MODEL"], messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"})
            js = res.choices[0].message.content.strip()
            if "```json" in js: js = js.split("```json")[1].split("```")[0].strip()
            ai_res = json.loads(js)
            if isinstance(ai_res, dict):
                for k in ai_res:
                    if isinstance(ai_res[k], list): ai_res = ai_res[k]; break
            
            ai_map = {item['id']: item for item in ai_res if isinstance(item, dict)}
            for o in raw_data:
                ai_info = ai_map.get(o['id'], {})
                
                # 情绪得分安全性转换 (修复 'Neutral' 问题)
                sent = ai_info.get('sentiment', 5)
                try: 
                    sentiment_val = int(sent)
                except: 
                    sentiment_val = 5 # 如果是文字（如 Neutral）则给中间值

                def to_l(v, d): return [str(i).strip()[:10] for i in (v if isinstance(v, list) else [v] if v else [d])]
                c1, c2, tp = to_l(ai_info.get('category'), "其他"), to_l(ai_info.get('sub_category'), "通用"), to_l(ai_info.get('topic_label'), o['标题'])
                
                for sub in c2:
                    for tag in tp:
                        if not any(p['cat'] == sub and p['tag'] == tag for p in ref_pairs):
                            new_ref_pairs.add((sub, tag))
                all_enriched.append({**o, "模块分类": c1, "二级分类": c2, "话题标签": tp, "情绪得分": sentiment_val, "AI核心总结": str(ai_info.get('summary', o['标题']))})
        
        except Exception as e:
            print(f"⚠️ AI 降级模式: {e}")
            for o in raw_data:
                tag_label = o['标题'][:15]
                all_enriched.append({**o, "模块分类": ["未分类"], "二级分类": ["待定"], "话题标签": [tag_label], "情绪得分": 5, "AI核心总结": o['标题']})
                if not any(p['tag'] == tag_label for p in ref_pairs):
                    new_ref_pairs.add(("待定系统", tag_label))

        # 3. 排序与同步
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference_pairs(list(new_ref_pairs))

        # 4. 汇总卡片逻辑
        topic_groups = {}
        for item in all_enriched:
            for label in item["话题标签"]:
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"][0], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].add(item["owner_id"])

        summary_list = []
        seen_summaries = set()
        sorted_keys = sorted(topic_groups.keys(), key=lambda k: topic_groups[k]['heat'], reverse=True)
        for k in sorted_keys:
            v = topic_groups[k]
            # 强化总结去重，防止降级模式下重复显示
            if v['sum'][:30] not in seen_summaries:
                summary_list.append({"topic": v["topic"], "category": v["cat"], "total_heat": v["heat"], "thread_count": v["count"], "user_count": len(v["users"]), "summary": v["sum"]})
                seen_summaries.add(v['sum'][:30])

        if summary_list: await self.send_weekly_card(summary_list, feishu, date_display)
        await self.close()

    async def send_weekly_card(self, sl, fs, dt):
        st = sl[:5]
        el = [{"tag": "markdown", "content": f"**📊 上周社区核心话题概览**\n共识别主题: {len(sl)} 个"}, {"tag": "hr"}]
        for i, item in enumerate(st):
            el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['summary'][:120]}\n🔥 热度 {item['total_heat']} | 📑 {item['thread_count']} 篇 | 👥 {item['user_count']} 人提及 | #{item['category']}"})
        el.append({"tag": "hr"})
        el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        fs.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({dt})"}, "template": "blue"}, "elements": el})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])