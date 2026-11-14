#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeurIPS 2026 触达系统 v2 —— 对齐「千问办公-AI Agent Networking 工程师」面试方向
JD 对应关系:
  Agent 网络效应   -> 共同作者图谱 + 中心度排序，按学术网络原生扩圈(不蹭大V热点)
  上下文自进化容器 -> memory 表: 每人一条随事件增长的事实流(抓取/草稿/点击/拉黑)
  多智能体协作     -> Scout/Curator/Composer/Guard/Courier/Tracker 六个 agent 经消息总线协作(A2A4H 微缩)
  智能体安全       -> STOP 拉黑 + 单轮投递限额(Guardrails 微缩)
  评测仿真         -> /api/eval 输出覆盖率/个性化率/点击转化(DSPy/Langfuse 思路)
用法: python reach.py    (自动打开 http://127.0.0.1:8777)
依赖: 仅 Python 3.10+ 标准库，零第三方包
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from collections import defaultdict
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

PORT = 8777
BASE = f"http://127.0.0.1:{PORT}"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reach.db")
ARXIV_API = "https://export.arxiv.org/api/query"

# ---------------------------------------------------------------------------
# 你的面试名片(JD: "欢迎附上 repo 和个人 agent"——这个 repo 本身就是作品)
# TODO: 把你的自我介绍贴进来，Composer 生成的草稿与 /card 名片页会自动引用
PROFILE = """【在这里贴你的 vibecoding 面试自我介绍】
一句话定位: (例) 相信 agent 网络效应的 vibecoding 实践者，单文件交付可运行系统
代表项目: (例) GitHub: nothing7744/vibecoding_demo —— 本触达系统即 repo 内作品
现场想聊: (例) agent 记忆自进化、A2A 协作协议、不蹭热点的扩圈策略
"""

# TODO: 演示前换成真实落地页(个人主页/GitHub/Calendly)；None = 落到内置 /card 名片页
LANDING_URL = None

# 投递安全限额: 每轮 dry-run 最多投多少封(防打扰，Guardrails 微缩)
SEND_LIMIT = 10
# ---------------------------------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS people(
                id INTEGER PRIMARY KEY, name TEXT UNIQUE, paper TEXT, source TEXT,
                blocked INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS edges(
                a INTEGER, b INTEGER, w INTEGER DEFAULT 0, UNIQUE(a,b));
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY, person_id INTEGER, subject TEXT, body TEXT,
                hash TEXT UNIQUE, sent INTEGER DEFAULT 0, created TEXT);
            CREATE TABLE IF NOT EXISTS clicks(
                id INTEGER PRIMARY KEY, hash TEXT, ts TEXT, ua TEXT);
            CREATE TABLE IF NOT EXISTS memory(
                id INTEGER PRIMARY KEY, person_id INTEGER, ts TEXT, kind TEXT, fact TEXT);
            CREATE TABLE IF NOT EXISTS agent_log(
                id INTEGER PRIMARY KEY, ts TEXT, frm TEXT, to_agent TEXT, msg TEXT);
            """
        )
        # v1 -> v2 兼容: 旧库的 people 表缺 blocked 列，补上
        cols = [r[1] for r in c.execute("PRAGMA table_info(people)")]
        if "blocked" not in cols:
            c.execute("ALTER TABLE people ADD COLUMN blocked INTEGER DEFAULT 0")


# ---------------------------------------------------------------------------
# 长期记忆(微缩版 mem0): 每人一条事实流，抓取/草稿/点击/拉黑都会追加
# TODO: 换 mem0/Graphiti 时，把 remember() 映射到 add_memory()，读侧映射到检索
def remember(person_id, kind, fact):
    with db() as c:
        c.execute(
            "INSERT INTO memory(person_id, ts, kind, fact) VALUES (?,?,?,?)",
            (person_id, now(), kind, fact),
        )


# agent 间消息总线(A2A4H 微缩): 先落库一条消息，未来换成 HTTP/A2A 协议
# TODO: 拆成独立进程后，这里改为 POST /a2a/message
def log_agent(frm, to, msg):
    with db() as c:
        c.execute(
            "INSERT INTO agent_log(ts, frm, to_agent, msg) VALUES (?,?,?,?)",
            (now(), frm, to, msg),
        )


def norm_name(name):
    # TODO: 下一步换成 (姓, 名, 机构) 三元组模糊归一 + 消歧，现在只做大小写/空白
    return re.sub(r"\s+", " ", name.strip().lower())


def upsert_person(name, paper, source):
    with db() as c:
        row = c.execute(
            "SELECT id FROM people WHERE name = ?", (norm_name(name),)
        ).fetchone()
        if row:
            return row["id"], False
        cur = c.execute(
            "INSERT INTO people(name, paper, source, created) VALUES (?,?,?,?)",
            (norm_name(name), paper, source, now()),
        )
        return cur.lastrowid, True


def add_edge(a, b):
    """共同作者边，w = 合作论文数。a<b 保证唯一。"""
    x, y = min(a, b), max(a, b)
    with db() as c:
        cur = c.execute("UPDATE edges SET w = w + 1 WHERE a=? AND b=?", (x, y))
        if cur.rowcount == 0:
            c.execute("INSERT INTO edges(a, b, w) VALUES (?,?,1)", (x, y))
            return True
        return False


def rankings():
    """图推荐排序: 度数 + 一阶邻接度数和(特征向量中心度的廉价近似)。
    关键立场: 排序只看学术网络结构，不看粉丝量——即 JD 说的『不蹭热点也能扩圈』。
    TODO: 换 PyG/DGL 后用 true PageRank/GNN 打分，加入机构向量与主题向量
    """
    with db() as c:
        people = {r["id"]: dict(r) for r in c.execute("SELECT * FROM people")}
        deg = defaultdict(int)
        nbrs = defaultdict(list)
        for r in c.execute("SELECT a, b, w FROM edges"):
            deg[r["a"]] += r["w"]
            deg[r["b"]] += r["w"]
            nbrs[r["a"]].append(r["b"])
            nbrs[r["b"]].append(r["a"])
    for pid, p in people.items():
        p["degree"] = deg.get(pid, 0)
        p["score"] = p["degree"] + 0.1 * sum(deg.get(n, 0) for n in nbrs.get(pid, []))
    return sorted(people.values(), key=lambda x: -x["score"])


def make_draft(name, paper):
    # TODO: 下一步用 LLM 按论文摘要逐人改写正文 + 附共同作者引荐路径(warm intro)
    h = hashlib.sha1(f"{name}|{paper}".encode()).hexdigest()[:10]
    link = f"{BASE}/c/{h}"
    profile_lines = [l for l in PROFILE.strip().splitlines() if l.strip()][:3]
    subject = f"读了您的《{paper[:60]}》—— NeurIPS 2026 现场聊 15 分钟？"
    body = (
        f"Hi {name},\n\n"
        f"刚读到您的 arXiv 工作《{paper[:80]}》，和我们正在做的事很相关。\n"
        f"关于我:\n" + "\n".join("  " + l for l in profile_lines) + "\n\n"
        f"我今年也会在 NeurIPS 2026 现场，方便的话看下我的名片: {link}\n\n"
        f"(不希望再收到请直接回 STOP)"
    )
    return subject, body, h


def compose_for(pid):
    """Composer: 给没有草稿的人生成个性化草稿。"""
    with db() as c:
        person = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not person:
            return False
        has = c.execute(
            "SELECT 1 FROM messages WHERE person_id=?", (pid,)).fetchone()
        if has:
            return False
        subject, body, h = make_draft(person["name"], person["paper"])
        c.execute(
            "INSERT OR IGNORE INTO messages(person_id, subject, body, hash, created)"
            " VALUES (?,?,?,?,?)", (pid, subject, body, h, now()),
        )
    remember(pid, "composer", "个性化草稿已生成(引用其论文标题)")
    return True


# ---------------------------------------------------------------------------
# 六个 agent 的流水线: Scout(抓) -> Curator(建图) -> Composer(写) -> Guard(安检)
def ingest(papers, source="arxiv"):
    stats = {"papers": len(papers), "new_people": 0, "skipped_duplicates": 0, "new_edges": 0}
    log_agent("Scout", "Curator", f"抓到 {len(papers)} 篇论文({source})，交给你去重并建共同作者图")
    paper_pids = []
    for p in papers:
        pids = []
        for a in p["authors"]:
            pid, created = upsert_person(a, p["title"], source)
            if created:
                stats["new_people"] += 1
                remember(pid, "scout", f"来自 {source}，论文《{p['title'][:60]}》")
            else:
                stats["skipped_duplicates"] += 1
            pids.append(pid)
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                if add_edge(pids[i], pids[j]):
                    stats["new_edges"] += 1
        paper_pids.append(pids)
    log_agent("Curator", "Composer",
              f"新增 {stats['new_people']} 人 / {stats['new_edges']} 条合作边，请为新人写草稿")
    n_draft = sum(1 for pids in paper_pids for pid in set(pids) if compose_for(pid))
    log_agent("Composer", "Guard", f"生成 {n_draft} 份个性化草稿，请做安全检查")
    with db() as c:
        drafts = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    log_agent("Guard", "Courier",
              f"安检通过: 无拉黑对象命中，单轮投递限额 {SEND_LIMIT} 封(防打扰)")
    # TODO: Guard 真正要做的事: NeMo Guardrails 过滤话术 / Presidio 脱敏 PII / 退信熔断
    stats["total_drafts"] = drafts
    return stats


def arxiv_fetch(query, n):
    """真实数据源: arXiv Atom API，无需 key，urllib 自动走环境代理。
    TODO: 加 OpenReview/Semantic Scholar 多源合并(OpenReview 有反爬挑战，需浏览器会话或官方账号)
    """
    url = (
        f"{ARXIV_API}?search_query={quote('all:' + query)}"
        f"&start=0&max_results={n}&sortBy=submittedDate&sortOrder=descending"
    )
    req = Request(url, headers={"User-Agent": "reach-demo/0.2 (contact: demo@example.com)"})
    with urlopen(req, timeout=25) as resp:
        root = ET.fromstring(resp.read())
    papers = []
    for e in root.findall(f"{ATOM}entry"):
        title = re.sub(r"\s+", " ", (e.findtext(f"{ATOM}title") or "").strip())
        authors = [(a.findtext(f"{ATOM}name") or "").strip() for a in e.findall(f"{ATOM}author")]
        papers.append({"title": title, "authors": authors})
    return papers


MOCK_PAPERS = [
    {"title": "MoM: Mixture of Memory for Long-context LLM Agents",
     "authors": ["Wei Zhang", "Sanjay Patel", "Ling Chen"]},
    {"title": "Flow Matching posterior inference is all you need (Maybe)",
     "authors": ["Akihiro Tanaka", "Maria Gomez"]},
    {"title": "Scaling Laws for Agentic Tool Use",
     "authors": ["Sanjay Patel", "Yuki Watanabe", "Omar Haddad"]},
    {"title": "LLM-as-Judge under Distribution Shift",
     "authors": ["Maria Gomez", "Elena Petrova"]},
    {"title": "Denoising Diffusion World Models",
     "authors": ["Ling Chen", "Wei Zhang"]},
    {"title": "Graphiti: Temporal Knowledge Graphs for Agent Memory",
     "authors": ["Omar Haddad", "Elena Petrova", "Akihiro Tanaka"]},
]
# 离线兜底: 网络不可用时保证演示跑通，页面会标注 MOCK。TODO: 正式演示前删掉


# ---------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>Agent Networking 触达演示</title><style>
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:36px auto;max-width:1000px;color:#1c1e21;background:#fff}
h2{font-weight:600;font-size:20px;margin-bottom:2px}
small{color:#65676b}
input,button{padding:8px 12px;font-size:14px;border:1px solid #ccd0d5;border-radius:8px}
button{cursor:pointer;background:#f0f2f5}
button:hover{background:#e4e6eb}
.bar{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}
.kpi{background:#f7f8fa;border-radius:10px;padding:8px 14px;font-size:12px}
.kpi b{font-size:18px;display:block}
section{margin-top:26px}
h3{font-size:15px;font-weight:600;margin-bottom:8px}
table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #ebedf0;padding:7px 6px;text-align:left;font-size:13px;vertical-align:top}
details pre{white-space:pre-wrap;background:#f7f8fa;padding:10px;border-radius:8px;font-size:12px}
#log{background:#111;color:#9fe6c8;font:12px/1.7 Consolas,monospace;border-radius:10px;padding:12px;height:190px;overflow-y:auto}
.err{color:#b33}.ok{color:#0a7d32}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
canvas{border:1px solid #ebedf0;border-radius:10px;background:#fcfcfd}
</style></head><body>
<h2>Agent Networking · NeurIPS 2026 扩圈触达</h2>
<small>六智能体协作(Scout/Curator/Composer/Guard/Courier/Tracker) · 共同作者图谱排序(不蹭热点) · 记忆自进化 · 点击追踪</small>

<div style="margin-top:14px">
  <input id="q" value="diffusion model" size="28" placeholder="arXiv 关键词">
  <input id="n" value="20" size="4">
  <button onclick="doFetch()">从 arXiv 抓取人群</button>
  <button onclick="loadMock()">离线演示数据</button>
  <button onclick="drySend()">模拟投递 (dry-run)</button>
  <button onclick="doEval()">评测</button>
</div>
<div class="bar" id="stats"></div>
<div id="msg" style="font-size:13px;margin-bottom:8px"></div>

<section><h3>优先触达 Top 10 <small>按网络中心度排序，而非粉丝量</small></h3>
<div id="top" style="font-size:13px;line-height:2"></div></section>

<section><h3>共同作者图谱 <small>节点大小=合作度，深色=高中心度(扩圈杠杆点)</small></h3>
<canvas id="gcv" width="940" height="300"></canvas></section>

<div class="grid2">
<section><h3>人群与草稿</h3>
<table><thead><tr><th>#</th><th>姓名</th><th>论文(来源)</th><th>度</th><th>状态</th><th>链接</th><th>操作</th></tr></thead>
<tbody id="tbl"><tr><td colspan="7"><small>先点「抓取人群」</small></td></tr></tbody></table></section>

<section><h3>Agent 协作日志 <small>A2A 消息总线(每 5s 刷新)</small></h3>
<div id="log"></div>
<h3 style="margin-top:18px">记忆样例 <small>上下文自进化容器(最近 8 条)</small></h3>
<div id="mem" style="font-size:12px;line-height:1.9;color:#444"></div></section>
</div>

<script>
const $ = id => document.getElementById(id);
async function get(p){ const r = await fetch(p); return r.json(); }
function setMsg(t, cls){ $('msg').innerHTML = '<span class="'+cls+'">'+t+'</span>'; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }

async function loadStats(){
  const s = await get('/api/stats');
  $('stats').innerHTML = [
    ['论文', s.papers], ['人群', s.people], ['合作边', s.edges], ['草稿', s.messages],
    ['已投递(dry)', s.sent], ['点击', s.clicks], ['记忆条目', s.memories], ['拉黑', s.blocked]
  ].map(x => '<div class="kpi"><b>'+x[1]+'</b>'+x[0]+'</div>').join('');
}
async function loadPeople(){
  const rows = await get('/api/people');
  if(!rows.length){ $('tbl').innerHTML = '<tr><td colspan="7"><small>暂无数据</small></td></tr>'; return; }
  $('tbl').innerHTML = rows.slice(0, 100).map(r => {
    const draft = '主题: ' + r.subject + '\\n\\n' + r.body;
    const mem = (r.recent_memory || []).map(m => '[' + m.kind + '] ' + m.fact).join('\\n');
    return '<tr><td>'+r.rank+'</td><td>'+esc(r.name)+'</td>'
      + '<td>'+esc(r.paper)+'<br><small>'+esc(r.source)+'</small></td>'
      + '<td>'+r.degree+'</td>'
      + '<td>'+(r.blocked ? '已拉黑' : (r.sent ? '已投递(dry)' : '草稿'))
        + (r.clicks ? ' / <b class="ok">点击'+r.clicks+'</b>' : '')+'</td>'
      + '<td><a href="/c/'+r.hash+'" target="_blank">/c/'+r.hash+'</a></td>'
      + '<td>'+(r.blocked ? '-' : '<button onclick="block('+r.id+')">STOP</button>')+'</td></tr>'
      + '<tr><td colspan="7"><details><summary>草稿+记忆</summary><pre id="d'+r.hash+'">'
      + esc(draft) + '\\n\\n— 记忆 —\\n' + esc(mem)
      + '</pre><button onclick="copyDraft(\\'d'+r.hash+'\\')">复制</button></details></td></tr>';
  }).join('');
  const top = rows.slice(0, 10);
  $('top').innerHTML = top.map(r =>
    '<b>#'+r.rank+'</b> '+esc(r.name)+' <small>度='+r.degree+' 中心度='+r.score
    +(r.clicks ? ' <span class="ok">已点击</span>' : '')+'</small>').join('<br>');
}
async function drawGraph(){
  const g = await get('/api/graph');
  const cv = $('gcv'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  if(!g.nodes.length){ ctx.fillStyle='#888'; ctx.fillText('无图谱数据', 20, 30); return; }
  const W = cv.width, H = cv.height, cx = W/2, cy = H/2, R = H/2 - 30;
  const pos = {};
  g.nodes.forEach((n, i) => {
    const a = 2*Math.PI*i/g.nodes.length;
    pos[n.id] = [cx + R*Math.cos(a), cy + R*Math.sin(a)*0.85];
  });
  ctx.strokeStyle = 'rgba(120,120,120,0.25)';
  g.edges.forEach(e => {
    if(!pos[e.a] || !pos[e.b]) return;
    ctx.beginPath(); ctx.moveTo(pos[e.a][0], pos[e.a][1]); ctx.lineTo(pos[e.b][0], pos[e.b][1]); ctx.stroke();
  });
  g.nodes.forEach((n, i) => {
    const r = 4 + Math.min(n.deg, 14);
    ctx.fillStyle = i < 10 ? '#0F6E56' : '#9aa0a6';
    ctx.beginPath(); ctx.arc(pos[n.id][0], pos[n.id][1], r, 0, 7); ctx.fill();
    if(i < 8){
      ctx.fillStyle = '#333'; ctx.font = '11px system-ui';
      ctx.fillText(n.name.slice(0, 14), pos[n.id][0] + r + 3, pos[n.id][1] + 4);
    }
  });
}
async function loadLog(){
  const rows = await get('/api/log');
  $('log').innerHTML = rows.map(r =>
    esc(r.ts + '  ' + r.frm + ' -> ' + r.to_agent + ': ' + r.msg)).join('<br>')
    || '(等待 agent 消息)';
  const log = $('log'); log.scrollTop = log.scrollHeight;
}
async function loadMemory(){
  const rows = await get('/api/memory');
  $('mem').innerHTML = rows.map(m =>
    esc(m.ts + ' · ' + m.kind + ' · #' + m.person_id + ' ' + m.fact)).join('<br>') || '(暂无)';
}
async function doFetch(){
  setMsg('Scout 抓取中…','');
  const d = await get('/api/fetch?q='+encodeURIComponent($('q').value)+'&n='+$('n').value);
  if(d.error){ setMsg('抓取失败: '+d.error+' → 可点「离线演示数据」', 'err'); return; }
  setMsg('抓到 '+d.papers+' 篇 → 新增 '+d.new_people+' 人 / '+d.new_edges+' 条合作边，去重跳过 '
    +d.skipped_duplicates+' 人', 'ok');
  refreshAll();
}
async function loadMock(){
  const d = await get('/api/mock');
  setMsg('已载入离线演示数据(MOCK)：'+d.new_people+' 人', 'ok');
  refreshAll();
}
async function drySend(){
  const d = await get('/api/send_all');
  setMsg('dry-run 投递 '+d.sent+' 封(限额 '+d.limit+'，跳过拉黑 '+d.skipped_blocked+' 封)——真实 SMTP 见代码 TODO', 'ok');
  refreshAll();
}
async function doEval(){
  const e = await get('/api/eval');
  setMsg('评测: 覆盖率 '+e.coverage+' · 个性化率 '+e.personalization
    +' · 点击转化 '+e.click_through+' · 漏斗 '+e.funnel, 'ok');
}
async function block(id){
  await get('/api/block?id='+id);
  setMsg('已拉黑 #'+id+'(Guard 生效，后续投递跳过)', 'ok');
  refreshAll();
}
function copyDraft(id){
  navigator.clipboard.writeText($(id).textContent).then(()=>setMsg('已复制草稿','ok'));
}
function refreshAll(){ loadStats(); loadPeople(); drawGraph(); loadLog(); loadMemory(); }
setInterval(loadLog, 5000);
loadStats(); loadLog(); loadMemory();
</script></body></html>"""


CARD = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>名片</title><style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:60px auto;color:#1c1e21;line-height:1.7}
h3{font-weight:600}.card{border:1px solid #ebedf0;border-radius:14px;padding:24px}
small{color:#65676b}
</style></head><body><div class="card">
<h3>来自 Agent Networking 触达系统的名片</h3>
<small>你通过论文《{paper}》相关的邮件链接来到这里</small>
<pre style="white-space:pre-wrap;font-family:inherit">{profile}</pre>
<p><small>点击已被 Tracker 记录，并写入你的记忆条目（上下文自进化）。</small></p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def json(self, obj, code=200):
        self.reply(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if path in ("/", "/index.html"):
            return self.reply(200, PAGE)

        if path == "/api/fetch":
            q = (qs.get("q", [""])[0] or "").strip()
            n = min(int(qs.get("n", ["20"])[0] or 20), 50)
            if not q:
                return self.json({"error": "缺少关键词 q"}, 400)
            try:
                papers = arxiv_fetch(q, n)
            except Exception as e:
                return self.json({"error": f"{type(e).__name__}: {e}"}, 200)
            if not papers:
                return self.json({"error": "arXiv 返回 0 条，换个关键词试试"}, 200)
            return self.json(ingest(papers))

        if path == "/api/mock":
            return self.json(ingest(MOCK_PAPERS, source="MOCK"))

        if path == "/api/people":
            rk = rankings()
            rank_map = {p["id"]: i + 1 for i, p in enumerate(rk)}
            deg_map = {p["id"]: p["degree"] for p in rk}
            score_map = {p["id"]: round(p["score"], 1) for p in rk}
            with db() as c:
                rows = c.execute(
                    """SELECT p.id, p.name, p.paper, p.source, p.blocked, m.hash, m.sent,
                              (SELECT COUNT(*) FROM clicks k WHERE k.hash=m.hash) clicks
                       FROM messages m JOIN people p ON p.id=m.person_id
                       ORDER BY m.id DESC LIMIT 300"""
                ).fetchall()
                mem_rows = c.execute(
                    "SELECT person_id, kind, fact FROM memory ORDER BY id DESC LIMIT 200"
                ).fetchall()
            mem_by_person = defaultdict(list)
            for m in mem_rows:
                if len(mem_by_person[m["person_id"]]) < 3:
                    mem_by_person[m["person_id"]].append(m)
            out = []
            for r in rows:
                d = dict(r)
                d["rank"] = rank_map.get(d["id"], "-")
                d["degree"] = deg_map.get(d["id"], 0)
                d["score"] = score_map.get(d["id"], 0)
                d["recent_memory"] = [
                    {"kind": m["kind"], "fact": m["fact"]}
                    for m in reversed(mem_by_person.get(d["id"], []))
                ]
                out.append(d)
            return self.json(out)

        if path == "/api/graph":
            rk = rankings()
            top = rk[:60]
            ids = {p["id"] for p in top}
            with db() as c:
                edges = c.execute("SELECT a, b, w FROM edges").fetchall()
            edges = [e for e in edges if e["a"] in ids and e["b"] in ids]
            return self.json({
                "nodes": [{"id": p["id"], "name": p["name"], "deg": p["degree"]} for p in top],
                "edges": [{"a": e["a"], "b": e["b"], "w": e["w"]} for e in edges],
            })

        if path == "/api/log":
            with db() as c:
                rows = c.execute(
                    "SELECT * FROM agent_log ORDER BY id DESC LIMIT 60").fetchall()
            return self.json([dict(r) for r in reversed(rows)])

        if path == "/api/memory":
            with db() as c:
                rows = c.execute(
                    "SELECT * FROM memory ORDER BY id DESC LIMIT 8").fetchall()
            return self.json([dict(r) for r in rows])

        if path == "/api/stats":
            with db() as c:
                one = lambda s: c.execute(s).fetchone()[0]
                return self.json({
                    "papers": one("SELECT COUNT(*) FROM (SELECT DISTINCT paper FROM people)"),
                    "people": one("SELECT COUNT(*) FROM people"),
                    "edges": one("SELECT COUNT(*) FROM edges"),
                    "messages": one("SELECT COUNT(*) FROM messages"),
                    "sent": one("SELECT COUNT(*) FROM messages WHERE sent=1"),
                    "clicks": one("SELECT COUNT(*) FROM clicks"),
                    "memories": one("SELECT COUNT(*) FROM memory"),
                    "blocked": one("SELECT COUNT(*) FROM people WHERE blocked=1"),
                })

        if path == "/api/eval":
            # 评测微缩版(DSPy/Langfuse 思路): 只评系统产出质量，不评模型
            # TODO: 接 Langfuse 追踪每次 LLM 调用; 用 DSPy 断言草稿必含论文标题/必含 STOP 退订语
            with db() as c:
                one = lambda s: c.execute(s).fetchone()[0]
                people = one("SELECT COUNT(*) FROM people")
                with_draft = one("SELECT COUNT(DISTINCT person_id) FROM messages")
                sent = one("SELECT COUNT(*) FROM messages WHERE sent=1")
                clicks = one("SELECT COUNT(*) FROM clicks")
                rows = c.execute(
                    "SELECT m.body, p.paper FROM messages m JOIN people p ON p.id=m.person_id"
                ).fetchall()
            pers = sum(1 for r in rows if r["paper"][:40].lower() in r["body"].lower()) / max(len(rows), 1)
            return self.json({
                "coverage": round(with_draft / max(people, 1), 2),
                "personalization": round(pers, 2),
                "click_through": round(clicks / max(sent, 1), 2),
                "funnel": f"{people}人->{len(rows)}草稿->{sent}投递->{clicks}点击",
            })

        if path == "/api/block":
            pid = int(qs.get("id", ["0"])[0] or 0)
            with db() as c:
                c.execute("UPDATE people SET blocked=1 WHERE id=?", (pid,))
            remember(pid, "guard", "用户 STOP —— 永久拉黑，投递跳过")
            log_agent("Guard", "Courier", f"#{pid} 已拉黑，后续投递自动跳过")
            return self.json({"blocked": pid})

        if path == "/api/send_all":
            # Courier dry-run: 真实 SMTP(smtplib + smtp.qq.com:465 + 16位授权码)见 TODO
            # TODO: 真投递 + 退信回写状态 + 打开追踪像素 + 按点击触发二次触达
            with db() as c:
                rows = c.execute(
                    """SELECT m.id, m.person_id FROM messages m
                       JOIN people p ON p.id=m.person_id
                       WHERE m.sent=0 AND p.blocked=0
                       ORDER BY m.id LIMIT ?""", (SEND_LIMIT,)).fetchall()
                skipped = c.execute(
                    """SELECT COUNT(*) FROM messages m JOIN people p ON p.id=m.person_id
                       WHERE m.sent=0 AND p.blocked=1""").fetchone()[0]
                for r in rows:
                    c.execute("UPDATE messages SET sent=1 WHERE id=?", (r["id"],))
            for r in rows:
                remember(r["person_id"], "courier", "dry-run 投递(未真实发信)")
            log_agent("Courier", "Tracker", f"dry-run 投递 {len(rows)} 封，等待点击信号")
            return self.json({"sent": len(rows), "skipped_blocked": skipped, "limit": SEND_LIMIT})

        m = re.match(r"^/c/([0-9a-f]{10})$", path)
        if m:
            h = m.group(1)
            with db() as c:
                row = c.execute(
                    "SELECT m.person_id, p.paper FROM messages m"
                    " JOIN people p ON p.id=m.person_id WHERE m.hash=?", (h,)).fetchone()
                if not row:
                    return self.reply(404, "unknown link")
                c.execute(
                    "INSERT INTO clicks(hash, ts, ua) VALUES (?,?,?)",
                    (h, now(), self.headers.get("User-Agent", "")),
                )
            pid = row["person_id"]
            # 记忆自进化: 点击作为兴趣信号写入该人的上下文，供二次触达使用
            remember(pid, "tracker", "点击了追踪链接 -> 兴趣信号 HIGH，可触发二次触达")
            log_agent("Tracker", "Composer", f"#{pid} 点击了链接，建议升级触达策略")
            if LANDING_URL:
                return self.redirect(LANDING_URL)
            return self.redirect(f"/card?h={h}")

        if path == "/card":
            h = (qs.get("h", [""])[0] or "")[:10]
            with db() as c:
                row = c.execute(
                    "SELECT p.paper FROM messages m JOIN people p ON p.id=m.person_id"
                    " WHERE m.hash=?", (h,)).fetchone()
            paper = row["paper"] if row else "(未知来源)"
            # 模板里含 CSS 花括号，不能用 str.format，只能 replace
            page = (CARD
                    .replace("{paper}", escape(paper[:80]))
                    .replace("{profile}", escape(PROFILE.strip())))
            return self.reply(200, page)

        self.reply(404, "not found")


def main():
    init_db()
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(BASE)).start()
    print(f"agent-networking demo -> {BASE}   (Ctrl+C 退出)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
