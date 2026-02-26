import asyncio
import os
import json
import discord
import requests
import re
from openai import AsyncOpenAI
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ===================== 1. 配置中心 =====================
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

# ===================== 2. 飞书 API 客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]}, timeout=10)
            return res.json().get("tenant_access_token")
        except: return None

    def get_knowledge_base(self):
        """从参考表读取 模块-二级分类-关键字 映射"""
        if not self.token: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
            items = res.json().get('data', {}).get('items', [])
            kb = []
            for r in items:
                f = r['fields']
                if f.get('模块分类') and f.get('二级分类'):
                    kb.append({
                        "cat1": f.get('模块分类'),
                        "cat2": f.get('二级分类'),
                        "keywords": str(f.get('关键字', '')).replace('，', ',').split(',')
                    })
            return kb
        except: return []

    def add_new_reference(self, new_records):
        """发现新分类组合时自动录入"""
        if not self.token or not new_records: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['REFERENCE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        records = [{"fields": {"模块分类": p[0], "二级分类": p[1], "关键字": ""}} for p in new_records]
        requests.post(url, headers=headers, json={"records": records}, timeout=10)

    def batch_add_bitable_records(self, records_list):
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        formatted = []
        for r in records_list:
            formatted.append({"fields": {
                "日期": r.get("日期"),
                "模块分类": r.get("模块分类"), 
                "二级分类": r.get("二级分类"),
                "热度分": int(r.get("热度分", 0)),
                "参与人数": int(r.get("参与人数", 0)),
                "AI核心总结": str(r.get("AI核心总结", "")),
                "情绪得分": int(r.get("情绪得分", 5)),
                "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
            }})
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=15)
        print(f"✅ 主表同步: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"receive_id": CONF["FEISHU_CHAT_ID"], "msg_type": "interactive", "content": json.dumps(card_content)}
        requests.post(url, headers=headers, json=payload, timeout=10)

# ===================== 3. 机器人核心逻辑 =====================
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
        
        # 1. 加载术语参考库
        kb = feishu.get_knowledge_base()
        print(f"📚 已从飞书加载 {len(kb)} 条分类映射规则")
        
        start_time, end_time = self.get_range()
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
                    "id": index, "标题": t.name, "内容": msg.content[:600],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "owner_id": msg.author.id, "日期": int(t.created_at.timestamp() * 1000),
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}"
                })
            except: continue

        if not raw_data: await self.close(); return

        # 2. AI 深度分析
        print(f"🤖 正在调用 AI 进行分类总结...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        
        # 构造参考列表供 AI 学习
        ref_guide = "\n".join([f"- {e['cat1']} | {e['cat2']} (关键字: {','.join(e['keywords'])})" for e in kb])

        prompt = f"""分析《DarkWar》玩家建议并输出 JSON []。
        
        【分类参考库】：
        {ref_guide if ref_guide else "暂无，请根据内容自创规范的中文分类"}

        【要求】：
        1. 输出必须为简体中文。
        2. 如果建议涉及多个系统，category 和 sub_category 请使用列表。
        3. summary 必须是对建议的一句话精炼总结。

        输出字段: id, sentiment(1-10), category(list), sub_category(list), summary(中文)。
        数据：\n{items_str}"""

        all_enriched, new_kb_records = [], set()
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
                
                # 将各种输入统一为「字符串列表」，便于后续处理，d 为默认值
                def to_l(v, d):
                    return [str(i).strip()[:15] for i in (v if isinstance(v, list) else [v] if v else [d])]

                # 1）AI 原始分类结果（只作为兜底 & 关键字匹配的“标签源”）
                ai_c1 = to_l(ai_info.get('category'), "其他")
                ai_c2 = to_l(ai_info.get('sub_category'), "通用")

                # 2）只用「AI 的分类文本 + 总结」做关键字匹配，不直接用玩家原始文本，避免误伤
                label_text = (
                    " ".join(ai_c1) + " " +
                    " ".join(ai_c2) + " " +
                    str(ai_info.get("summary", ""))
                ).upper()

                # 3）基于飞书参考表关键字，将 AI 的各种说法统一到标准分类（允许多模块多二级）
                matched_pairs = set()  # (cat1, cat2)
                for entry in kb:
                    for kw in entry['keywords']:
                        kw = (kw or "").strip()
                        if not kw:
                            continue
                        if kw.upper() in label_text:
                            matched_pairs.add((entry['cat1'], entry['cat2']))

                if matched_pairs:
                    # 命中了参考表：完全采用标准分类（支持多个模块多个二级）
                    c1 = [p[0] for p in matched_pairs]
                    c2 = [p[1] for p in matched_pairs]
                else:
                    # 没命中任何关键字：退回 AI 自己的分类结果
                    c1, c2 = ai_c1, ai_c2

                    # 同时识别 AI 新产出的分类组合，如果在参考表不存在则准备写入（关键字留空）
                    for i in range(len(c1)):
                        cat1_val = c1[i]
                        cat2_val = c2[i] if i < len(c2) else c2[0]
                        # 跳过占位默认值
                        if cat1_val in ("其他", "", None) or cat2_val in ("通用", "", None):
                            continue
                        if not any(e['cat1'] == cat1_val and e['cat2'] == cat2_val for e in kb):
                            new_kb_records.add((cat1_val, cat2_val))

                # 如果分类里包含了初始的“其他”或“通用”且有新分类进来了，把默认值删掉
                if len(c1) > 1 and "其他" in c1: c1.remove("其他")
                if len(c2) > 1 and "通用" in c2: c2.remove("通用")

                all_enriched.append({
                    **o, "模块分类": c1, "二级分类": c2,
                    "情绪得分": int(ai_info.get('sentiment', 5)),
                    "AI核心总结": str(ai_info.get('summary', o['标题'])),
                    "参与人数": 1
                })
        except Exception as e:
            print(f"❌ AI 分析异常: {e}"); await self.close(); return

        # 3. 排序、录入主表、更新参考库
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference(list(new_kb_records))

        # 4. 话题聚合卡片逻辑 (现在基于“二级分类”聚合)
        cat_groups = {}
        for item in all_enriched:
            for sub_cat in item["二级分类"]:
                if sub_cat not in cat_groups:
                    cat_groups[sub_cat] = {
                        "name": sub_cat, "cat1": item["模块分类"][0],
                        "heat": 0, "count": 0, "users": set(), "summary": item["AI核心总结"]
                    }
                g = cat_groups[sub_cat]
                g["heat"] += item["热度分"]
                g["count"] += 1
                g["users"].add(item["owner_id"])

        summary_list = []
        for v in cat_groups.values():
            summary_list.append({
                "topic": v["name"], "category": v["cat1"],
                "total_heat": v["heat"], "thread_count": v["count"],
                "user_count": len(v["users"]), "summary": v["summary"]
            })

        if summary_list:
            # 卡片按总热度分排序
            summary_list.sort(key=lambda x: x['total_heat'], reverse=True)
            await self.send_weekly_card(summary_list[:5], feishu, start_time)
        
        await self.close()

    async def send_weekly_card(self, st, fs, start_dt):
        el = [{"tag": "markdown", "content": f"**📊 上周社区二级分类热度榜**\n根据玩家反馈热度自动聚合。"}, {"tag": "hr"}]
        for i, item in enumerate(st):
            el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求摘要: {item['summary'][:100]}\n🔥 总热度 {item['total_heat']} | 📑 {item['thread_count']} 篇建议 | 👥 {item['user_count']} 人关注 | #{item['category']}"})
        el.append({"tag": "hr"})
        el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
        fs.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({start_dt.strftime('%m/%d')}-{(start_dt + timedelta(days=6)).strftime('%m/%d')})"}, "template": "blue"}, "elements": el})

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])