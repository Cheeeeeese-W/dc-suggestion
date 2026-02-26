import asyncio
import os
import json
import discord
import requests
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
        "FEISHU_REFERENCE_TABLE_ID": os.getenv("FEISHU_REFERENCE_TABLE_ID"),
        "FEISHU_CHAT_ID": os.getenv("FEISHU_CHAT_ID")
    }

CONF = get_conf()

# ===================== 2. 飞书客户端 =====================
class FeishuClient:
    def __init__(self):
        self.token = self._get_tenant_access_token()

    def _get_tenant_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        res = requests.post(url, json={"app_id": CONF["FEISHU_APP_ID"], "app_secret": CONF["FEISHU_APP_SECRET"]}, timeout=10)
        return res.json().get("tenant_access_token")

    def get_reference_pairs(self):
        ref_id = CONF.get('FEISHU_REFERENCE_TABLE_ID')
        if not self.token or not ref_id: return []
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{ref_id}/records"
        headers = {"Authorization": f"Bearer {self.token}"}
        res = requests.get(url, headers=headers, params={"page_size": 500}, timeout=10)
        items = res.json().get('data', {}).get('items', [])
        return [{"cat": r['fields'].get('二级分类'), "tag": r['fields'].get('话题标签')} for r in items if r['fields'].get('话题标签')]

    def add_new_reference_pairs(self, new_pairs_list):
        ref_id = CONF.get('FEISHU_REFERENCE_TABLE_ID')
        if not self.token or not ref_id or not new_pairs_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{ref_id}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}"}
        records = [{"fields": {"二级分类": str(p[0]), "话题标签": str(p[1])}} for p in new_pairs_list if p[0] and p[1]]
        requests.post(url, headers=headers, json={"records": records}, timeout=10)

    def batch_add_bitable_records(self, records_list):
        if not self.token or not records_list: return
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{CONF['BITABLE_TOKEN']}/tables/{CONF['BITABLE_TABLE_ID']}/records/batch_create"
        headers = {"Authorization": f"Bearer {self.token}"}
        formatted = [{"fields": {
            "日期": r.get("日期"), "模块分类": r.get("模块分类"), "二级分类": r.get("二级分类"),
            "话题标签": r.get("话题标签"), "热度分": int(r.get("热度分", 0)), "参与人数": int(r.get("参与人数", 0)),
            "AI核心总结": str(r.get("AI核心总结", "")), "情绪得分": int(r.get("情绪得分", 5)),
            "帖子链接": {"text": "点击查看帖子", "link": r.get("帖子链接")}
        }} for r in records_list]
        res = requests.post(url, headers=headers, json={"records": formatted}, timeout=15)
        print(f"✅ 主表录入状态: {res.json().get('msg')}")

    def send_group_card(self, card_content):
        if not self.token: return
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {self.token}"}
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
        
        # 修正 Base URL
        base_url = CONF["AI_BASE_URL"].strip().rstrip('/') + '/v1'
        ai_client = AsyncOpenAI(api_key=CONF["AI_API_KEY"], base_url=base_url)
        
        # 1. 读取参考库
        ref_pairs = feishu.get_reference_pairs()
        print(f"📚 已成功加载历史参考标签数量: {len(ref_pairs)}")
        ref_text = "\n".join([f"- {p['cat']}: {p['tag']}" for p in ref_pairs[:50]])

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
                    "id": index, "标题": t.name, "内容": msg.content[:500],
                    "热度分": int((t.message_count * 3) + (sum([r.count for r in msg.reactions]) * 1)),
                    "owner_id": msg.author.id,
                    "帖子链接": f"https://discord.com/channels/{t.guild.id}/{t.id}",
                    "日期": int(t.created_at.timestamp() * 1000)
                })
            except: continue

        if not raw_data:
            print("📭 上周无有效帖子"); await self.close(); return

        # ---------------- 核心 AI 处理区 (无 Try-Except 拦截错误) ----------------
        print(f"🤖 正在发起 AI 批量分析请求...")
        items_str = "\n".join([f"ID: {i['id']} | 标题: {i['标题']} | 内容: {i['内容']}" for i in raw_data])
        
        prompt = f"""你是一个专业的游戏策划。请分析以下《DarkWar》玩家建议。
        
        【任务要求】：
        1. 必须将总结(summary)翻译为中文。
        2. 话题标签(topic_label)优先从历史记忆库中匹配。
        3. 严格输出 JSON 格式。

        【参考记忆（二级分类: 话题标签）】：
        {ref_text if ref_text else "暂无"}

        输出字段: id, sentiment(1-10数字), category(list), sub_category(list), summary(中文总结), topic_label(list)。
        数据：\n{items_str}"""

        # 获取 AI 响应 (直接运行，如果报错 GitHub Actions 会捕获堆栈)
        res = await ai_client.chat.completions.create(
            model=CONF["AI_MODEL"], 
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        js_raw = res.choices[0].message.content.strip()
        print(f"DEBUG: AI 原始返回内容 -> {js_raw[:200]}...") # 打印开头部分用于调试

        # 解析 JSON
        if "```json" in js_raw: js_raw = js_raw.split("```json")[1].split("```")[0].strip()
        ai_res = json.loads(js_raw)
        if isinstance(ai_res, dict):
            for k in ai_res:
                if isinstance(ai_res[k], list): ai_res = ai_res[k]; break
        
        ai_map = {item['id']: item for item in ai_res if isinstance(item, dict)}
        
        all_enriched, new_ref_pairs = [], set()
        for o in raw_data:
            ai_info = ai_map.get(o['id'], {})
            
            # 安全提取
            def to_l(v, d): return [str(i).strip()[:15] for i in (v if isinstance(v, list) else [v] if v else [d])]
            sent = 5
            try: sent = int(ai_info.get('sentiment', 5))
            except: pass

            c1 = to_l(ai_info.get('category'), "其他")
            c2 = to_l(ai_info.get('sub_category'), "通用")
            tp = to_l(ai_info.get('topic_label'), o['标题'])

            # 记录新对
            for sub in c2:
                for tag in tp:
                    if not any(p['cat'] == sub and p['tag'] == tag for p in ref_pairs):
                        new_ref_pairs.add((sub, tag))

            all_enriched.append({
                **o, "模块分类": c1, "二级分类": c2, "话题标签": tp,
                "情绪得分": sent, "AI核心总结": str(ai_info.get('summary', o['标题'])), "参与人数": 1
            })

        # ---------------- 录入与推送 ----------------
        all_enriched.sort(key=lambda x: x['日期'], reverse=True)
        feishu.batch_add_bitable_records(all_enriched)
        feishu.add_new_reference_pairs(list(new_ref_pairs))

        # 聚合推送
        topic_groups = {}
        for item in all_enriched:
            for label in item["话题标签"]:
                if label not in topic_groups:
                    topic_groups[label] = {"topic": label, "cat": item["模块分类"][0], "heat": 0, "count": 0, "users": set(), "sum": item["AI核心总结"]}
                g = topic_groups[label]
                g["heat"] += item["热度分"]; g["count"] += 1; g["users"].add(item["owner_id"])

        summary_list = []
        seen_summaries = set()
        for k in sorted(topic_groups.keys(), key=lambda x: topic_groups[x]['heat'], reverse=True):
            v = topic_groups[k]
            if v['sum'][:20] not in seen_summaries:
                summary_list.append(v); seen_summaries.add(v['sum'][:20])

        if summary_list:
            st = summary_list[:5]
            el = [{"tag": "markdown", "content": f"**📊 上周社区话题概览**\n共识别主题: {len(summary_list)} 个"}, {"tag": "hr"}]
            for i, item in enumerate(st):
                el.append({"tag": "markdown", "content": f"**TOP {i+1}: {item['topic']}**\n诉求: {item['sum'][:150]}\n🔥 热度 {item['heat']} | 📑 {item['count']} 篇 | 👥 {len(item['users'])} 人提及 | #{item['cat']}"})
            el.append({"tag": "hr"})
            el.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "🔍 查看多维表格详情"}, "type": "primary", "url": f"https://feishu.cn/base/{CONF['BITABLE_TOKEN']}"}]})
            feishu.send_group_card({"header": {"title": {"tag": "plain_text", "content": f"🗓️ 玩家建议周报 ({start_time.strftime('%m/%d')}-{end_time.strftime('%m/%d')})"}, "template": "blue"}, "elements": el})
        
        await self.close()

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content, intents.guilds = True, True
    AdvancedBot(intents=intents).run(CONF["DISCORD_TOKEN"])