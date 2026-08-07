# -*- coding: utf-8 -*-
"""
黄金价格实时看板 V5.1 - 折线图坐标轴与鼠标提示版
运行：py gold_local_server_v5_1_fixed_cache.py
打开：http://127.0.0.1:8000

【功能概述】
这是一个本地运行的 HTTP 小服务器，启动后浏览器实时展示：
  1) 国际金价 XAU/USD（美元/盎司）；
  2) 人民币汇率 USD/CNY；
  3) 换算后的国际金价（元/克）= XAU/USD × USD/CNY ÷ 31.1034768；
  4) 上海黄金交易所 Au99.99 延时价格（元/克）；
  5) 内外价差及刷新走势折线图（带坐标轴、网格线和鼠标悬浮提示）。

【V5.1 变化】
- 固定缓存目录：缓存和日志固定存放在 D:\\桌面\\golden-local，不再随运行目录变化。
- 走势图增加坐标轴刻度、网格线、图例和鼠标悬浮提示。

【V4 修复】
1) Stooq 的 h 参数返回“无表头 CSV”，旧版用 DictReader 会把数据行误当表头。
2) XAUUSD 与 USDCNY 改为分开请求，不再用逗号合并请求。
3) XAUUSD 增加 Stooq HTML 抓取、FreeGoldAPI 日更备用源。
4) USD/CNY 增加 ExchangeRate-API open access 备用源。
5) 接口报错会写入 gold_debug.log，页面也会显示错误。

【数据源优先级】
- XAU/USD：Stooq 当前 CSV → Stooq HTML → GoldPrice.org → Metals.live
  → Yahoo Finance → Stooq 日线 → FreeGoldAPI 日更 CSV
- USD/CNY：ExchangeRate-API → ExchangeRate.host → Stooq 当前 CSV
- Au99.99：SGE 中文延时行情 → SGE 英文延时行情 → SGE 日报（最近 10 天）

【故障处理】
所有实时源都失败时，自动读取上次成功保存的 gold_cache.json 缓存数据，
并在页面上标注“缓存数据”以及失败原因；没有缓存才返回 500 错误。
"""

# ---------- 标准库导入 ----------
# HTTP 服务器：ThreadingHTTPServer 是多线程的，可同时处理多个浏览器请求
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# 构造 HTTP 请求对象并发送/接收请求
from urllib.request import Request, urlopen
# 时间处理：当前时间、日期加减（SGE 日报回溯用）
from datetime import datetime, timedelta
import csv        # 解析 Stooq / FreeGoldAPI 返回的 CSV 数据
import io         # 把字符串包装成文件流，供 csv.DictReader 使用
import json       # 解析 JSON 接口响应，序列化 API 返回数据
import os         # 判断缓存文件是否存在
import re         # 正则表达式：从 HTML 页面文本中提取价格
import socket     # 设置全局网络超时
import ssl        # 创建不校验证书的 SSL 上下文（部分免费接口证书不完整）
import traceback  # 输出完整异常堆栈到日志
import webbrowser # 服务器启动后自动打开浏览器
from pathlib import Path  # 跨平台路径对象

# ---------- 服务器基础配置 ----------
HOST = "127.0.0.1"  # 只监听本机，不会暴露给局域网其他设备
PORT = 8000         # HTTP 服务端口

# 一盎司黄金的克数：国际金价（美元/盎司）换算成人民币/克时的除数
TROY_OUNCE_GRAMS = 31.1034768

# 应用工作目录：缓存与日志固定存放在这里（V5.1 起不再随运行目录变化）
APP_DIR = Path(r"D:\桌面\golden-local")
APP_DIR.mkdir(parents=True, exist_ok=True)  # 目录不存在时自动创建

# 日志与缓存文件路径
DEBUG_LOG = str(APP_DIR / "gold_debug.log")    # 调试日志，记录每个接口的失败原因
CACHE_FILE = str(APP_DIR / "gold_cache.json")  # 最近一次成功获取的行情缓存

# ---------- 数据源 URL ----------
# Stooq：当前报价 CSV（h 参数表示无表头）、HTML 报价页、日线 CSV
URL_STOOQ_QUOTE_ONE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
URL_STOOQ_XAU_PAGE = "https://stooq.com/q/?s=xauusd"
URL_STOOQ_XAU_DAILY = "https://stooq.com/q/d/l/?s=xauusd&i=d"
# FreeGoldAPI：日更黄金价格 CSV
URL_FREE_GOLD_CSV = "https://freegoldapi.com/data/latest.csv"
# ExchangeRate-API：美元基础汇率 JSON（免费开放接口）
URL_ER_API_USD = "https://open.er-api.com/v6/latest/USD"
# 上海黄金交易所（SGE）：中文延时行情、英文延时行情、日度数据接口
URL_SGE_CN = "https://www.sge.com.cn/sjzx/yshqbg"
URL_SGE_EN = "https://en.sge.com.cn/h5_data_DelayedQuotes"
URL_SGE_DAILY = "https://en.sge.com.cn/data/data_daily_international_new?start_date={d}&end_date={d}"
# Yahoo Finance：XAU/USD 与 COMEX 黄金期货（GC=F）JSON 行情
URL_YAHOO_XAU = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=XAUUSD=X"
URL_YAHOO_GC = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=GC=F"
# GoldPrice.org：黄金现货 JSON
URL_GOLDPRICE_JSON = "https://data-asg.goldprice.org/dbXRates/USD"
# Metals.live：现货黄金 JSON
URL_METALSLIVE_GOLD = "https://api.metals.live/v1/spot/gold"
# ExchangeRate.host：备用汇率接口
URL_EXCHANGE_RATE_HOST = "https://api.exchangerate.host/latest?base=USD&symbols=CNY"

# 免费数据源证书可能不完整，这里创建“不校验证书”的 SSL 上下文，
# 避免因证书问题导致请求失败。仅用于读取公开行情，安全性足够。
SSL_CONTEXT = ssl._create_unverified_context()

# ============================================================================
# 前端页面（单文件 HTML + 内嵌 CSS/JavaScript）
# 下面这一大段原始字符串就是浏览器里看到的整个页面，主要包含：
#   1) 深色主题样式：3 张价格卡片（国际换算价 / 中国金价 / 内外价差）；
#   2) 刷新历史表格、数据摘要、接口来源、状态说明；
#   3) canvas 折线走势图：Y 轴为价格（元/克）、X 轴为刷新时间，
#      支持坐标轴刻度、网格线、图例和鼠标悬浮提示；
#   4) 自动刷新：默认 10 秒一次，可通过下拉框切换 30/60 秒；
#   5) 页面通过 fetch("/api/gold") 从本地服务器获取行情 JSON 并渲染。
# 注意：这是 r 原始字符串，内部反斜杠不会被转义，可直接书写 CSS/JS。
# ============================================================================
HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>黄金价格实时看板</title>
<style>
:root{--bg:#0f172a;--card:rgba(255,255,255,.08);--line:rgba(255,255,255,.14);--text:#f8fafc;--muted:#94a3b8;--gold:#fbbf24;--blue:#38bdf8;--green:#22c55e;--red:#ef4444;--shadow:0 24px 80px rgba(0,0,0,.26)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:radial-gradient(circle at top left,rgba(251,191,36,.18),transparent 36%),radial-gradient(circle at bottom right,rgba(56,189,248,.13),transparent 34%),var(--bg)}
.container{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:36px 0 42px}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:22px;margin-bottom:24px}.badge{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--line);border-radius:999px;color:#fde68a;background:rgba(255,255,255,.08);font-size:13px;margin-bottom:14px}.dot{width:8px;height:8px;border-radius:999px;background:var(--gold);box-shadow:0 0 18px var(--gold);animation:pulse 1.2s infinite ease-in-out;display:inline-block;margin-right:6px}@keyframes pulse{0%,100%{opacity:.55;transform:scale(.75)}50%{opacity:1;transform:scale(1)}}h1{margin:0;font-size:clamp(30px,5vw,58px);line-height:1.05;letter-spacing:-.06em}.desc{max-width:840px;margin:14px 0 0;color:var(--muted);line-height:1.75;font-size:15px}.controls{min-width:280px;display:grid;gap:12px}button{border:0;outline:0;border-radius:18px;padding:15px 18px;background:linear-gradient(135deg,#b7791f,#fbbf24);color:#111827;font-weight:900;cursor:pointer;box-shadow:0 14px 36px rgba(251,191,36,.23)}button:disabled{opacity:.62;cursor:not-allowed}.status{padding:13px 15px;border-radius:18px;border:1px solid var(--line);background:rgba(255,255,255,.08);color:var(--muted);font-size:13px;line-height:1.55;white-space:pre-wrap}.history-list{min-height:150px;max-height:240px;overflow:auto;border-radius:18px;border:1px solid rgba(255,255,255,.12);padding:12px;background:rgba(255,255,255,.04);font-size:13px;line-height:1.6;color:#e2e8f0}.history-list table{width:100%;border-collapse:collapse}.history-list th,.history-list td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}.history-list th{color:#e2e8f0;background:rgba(255,255,255,.06)}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:16px}.card,.panel,.errorbox{border:1px solid var(--line);border-radius:28px;background:var(--card);box-shadow:var(--shadow);backdrop-filter:blur(18px)}.card{min-height:215px;padding:24px;position:relative;overflow:hidden}.card:after{content:"";position:absolute;width:160px;height:160px;right:-45px;bottom:-62px;border-radius:50%;background:rgba(251,191,36,.13)}.card.blue:after{background:rgba(56,189,248,.13)}.card.green:after{background:rgba(34,197,94,.13)}.label{position:relative;z-index:1;display:flex;justify-content:space-between;align-items:center;gap:12px;color:var(--muted);font-size:15px;margin-bottom:22px}.pill{padding:6px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.08);color:#e2e8f0;font-size:12px;white-space:nowrap}.price{position:relative;z-index:1;font-size:clamp(36px,5vw,54px);font-weight:950;letter-spacing:-.06em;line-height:1}.unit{margin-left:6px;color:var(--muted);font-size:16px;font-weight:800;letter-spacing:0}.sub{position:relative;z-index:1;margin-top:18px;color:var(--muted);font-size:13px;line-height:1.65}.delta{display:inline-flex;margin-top:14px;padding:7px 11px;border-radius:999px;font-size:13px;font-weight:900;position:relative;z-index:1}.up{color:#bbf7d0;background:rgba(34,197,94,.15)}.down{color:#fecaca;background:rgba(239,68,68,.15)}.panel{padding:24px;display:grid;grid-template-columns:1.25fr .75fr;gap:18px}h2{margin:0 0 14px;font-size:20px}canvas{width:100%;height:290px;display:block;border-radius:22px;border:1px solid rgba(255,255,255,.09);background:rgba(255,255,255,.045);cursor:crosshair}.chart-wrap{position:relative}.chart-tooltip{position:absolute;display:none;z-index:20;min-width:190px;padding:10px 12px;border-radius:14px;background:rgba(15,23,42,.94);border:1px solid rgba(255,255,255,.18);box-shadow:0 16px 36px rgba(0,0,0,.35);color:#e2e8f0;font-size:12px;line-height:1.65;pointer-events:none;white-space:nowrap}.chart-tooltip b{color:#f8fafc}.chart-tooltip .gold{color:#fbbf24}.chart-tooltip .blue{color:#38bdf8}table{width:100%;border-collapse:collapse;font-size:13px;margin-top:14px}th,td{padding:12px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left;color:var(--muted)}th{color:#e2e8f0;background:rgba(255,255,255,.07)}.tips{display:grid;gap:12px}.tip{padding:14px;border-radius:18px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);color:var(--muted);line-height:1.65;font-size:13px}.tip strong{display:block;color:var(--text);margin-bottom:5px}.errorbox{display:none;margin-bottom:16px;padding:16px;color:#fecaca;font-size:13px;line-height:1.7;white-space:pre-wrap}.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:18px;line-height:1.7}@media(max-width:900px){.hero,.panel{grid-template-columns:1fr;flex-direction:column}.grid{grid-template-columns:1fr}.controls{width:100%}}
</style>
</head>
<body>
<main class="container">
<section class="hero"><div><div class="badge"><span class="dot"></span>本地服务版 V5.1 · 固定缓存目录</div><h1>黄金价格实时看板</h1><p class="desc">国际金价按 XAU/USD 与 USD/CNY 换算为人民币/克；中国金价取上海黄金交易所 Au99.99 延时报价。V4 修复了 Stooq CSV 无表头解析问题，并加入缓存与备用源。</p></div><div class="controls"><button id="refreshBtn">立即刷新</button><div style="display:grid;gap:10px;min-width:260px"><div class="status" id="statusBox">正在初始化...</div><div class="status" id="infoBox">数据来源：--；最后刷新：--</div></div><select id="intervalSelect" style="min-width:150px;border-radius:18px;padding:12px 14px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.08);color:#e2e8f0;font-size:13px"><option value="10" selected>自动刷新：10 秒</option><option value="30">自动刷新：30 秒</option><option value="60">自动刷新：60 秒</option></select></div></section>
<div class="errorbox" id="errorBox"></div>
<section class="grid"><article class="card"><div class="label"><span>国际金价换算</span><span class="pill">XAU/USD × 汇率</span></div><div class="price" id="intlPrice">--<span class="unit">元/克</span></div><div class="sub" id="intlSub">等待数据中...</div></article><article class="card blue"><div class="label"><span>中国金价</span><span class="pill">SGE Au99.99</span></div><div class="price" id="chinaPrice">--<span class="unit">元/克</span></div><div class="sub" id="chinaSub">等待数据中...</div></article><article class="card green"><div class="label"><span>内外价差</span><span class="pill">中国 - 国际</span></div><div class="price" id="spreadPrice">--<span class="unit">元/克</span></div><div class="delta" id="spreadDelta">等待计算</div><div class="sub" id="spreadSub">价差为正，表示国内报价高于换算后的国际报价。</div></article></section>
<section class="grid"><article class="card"><div class="label"><span>当前数据摘要</span><span class="pill">状态一览</span></div><div class="sub" id="statusDetail">等待首次刷新，或页面刷新后显示最新状态。</div></article><article class="card blue"><div class="label"><span>最近刷新历史</span><span class="pill">最多 6 条</span></div><div class="history-list" id="historyList">暂无刷新记录。</div></article><article class="card green"><div class="label"><span>接口来源</span><span class="pill">XAU / USD / SGE</span></div><div class="sub" id="detailSources">等待刷新后显示。</div></article></section>
<section class="panel"><div><h2>刷新走势</h2><div class="chart-wrap"><canvas id="chart" width="900" height="320"></canvas><div id="chartTooltip" class="chart-tooltip"></div></div><table><thead><tr><th>项目</th><th>数值</th><th>来源/说明</th></tr></thead><tbody><tr><td>XAU/USD</td><td id="xauCell">--</td><td id="xauSource">--</td></tr><tr><td>USD/CNY</td><td id="fxCell">--</td><td id="fxSource">--</td></tr><tr><td>中国金价</td><td id="sgeCell">--</td><td id="sgeSource">--</td></tr><tr><td>换算公式</td><td colspan="2">国际金价人民币/克 = XAU/USD × USD/CNY ÷ 31.1034768</td></tr></tbody></table></div><aside><h2>状态说明</h2><div class="tips"><div class="tip"><strong>这版修了什么</strong>旧版把 Stooq 无表头 CSV 当成有表头 CSV 解析，所以 XAUUSD 会被解析乱。V4 已重写解析逻辑。</div><div class="tip"><strong>缓存机制</strong>成功获取一次后会保存 gold_cache.json。以后临时断网也能显示上一次价格，并标注缓存。</div><div class="tip"><strong>免费边界</strong>免费数据可能延迟、休市或偶发不可用，适合个人查看，不适合作为交易依据。</div></div></aside></section><div class="footer">页面仅供个人学习和查看，不构成投资建议。</div>
</main>
<script>
const el=id=>document.getElementById(id);const historyData=[];let refreshHistory=[];let chartPoints=[];let loading=false;let refreshTimer=null;let refreshInterval=10000;function fmt(num,digits=2){const n=Number(num);if(!Number.isFinite(n))return"--";return n.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits})}function nowText(){return new Date().toLocaleString("zh-CN",{hour12:false})}function formatRefreshText(){return `下一次自动刷新约 ${refreshInterval/1000} 秒后。`}function setStatus(text,ok=true){el("statusBox").innerHTML=ok?text:`<span style="color:#fecaca">${text}</span>`}function showError(text){const box=el("errorBox");if(!text){box.style.display="none";box.textContent="";return}box.style.display="block";box.textContent=text}function drawChart(){
const canvas=el("chart"),ctx=canvas.getContext("2d");
const w=canvas.width,h=canvas.height;
const left=72,right=26,top=34,bottom=58;
const plotW=w-left-right,plotH=h-top-bottom;
chartPoints=[];
ctx.clearRect(0,0,w,h);
ctx.fillStyle="rgba(255,255,255,.03)";
ctx.fillRect(0,0,w,h);

// 坐标轴标题
ctx.fillStyle="rgba(226,232,240,.72)";
ctx.font="13px Microsoft YaHei, sans-serif";
ctx.fillText("价格（元/克）",left,20);
ctx.fillText("刷新时间",w-right-54,h-16);

// 数据不足时也画出坐标轴
let values=historyData.flatMap(p=>[p.intl,p.china]).filter(Number.isFinite);
let min=values.length?Math.min(...values):0;
let max=values.length?Math.max(...values):1;
let padValue=Math.max((max-min)*0.12,1);
min-=padValue;
max+=padValue;
let range=Math.max(max-min,1);

function yMap(v){return top+plotH-((v-min)/range)*plotH}
function xMap(i){return historyData.length<=1?left+plotW/2:left+plotW*i/(historyData.length-1)}

// 网格线 + Y轴刻度
ctx.strokeStyle="rgba(255,255,255,.10)";
ctx.lineWidth=1;
ctx.textAlign="right";
ctx.textBaseline="middle";
for(let i=0;i<=5;i++){
  const value=min+(range*i/5);
  const y=yMap(value);
  ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();
  ctx.fillStyle="rgba(226,232,240,.65)";
  ctx.fillText(fmt(value,2),left-10,y);
}

// X/Y 坐标轴
ctx.strokeStyle="rgba(226,232,240,.48)";
ctx.lineWidth=1.4;
ctx.beginPath();
ctx.moveTo(left,top);
ctx.lineTo(left,h-bottom);
ctx.lineTo(w-right,h-bottom);
ctx.stroke();

if(historyData.length<2){
  ctx.textAlign="center";
  ctx.textBaseline="middle";
  ctx.fillStyle="rgba(255,255,255,.55)";
  ctx.font="16px Microsoft YaHei, sans-serif";
  ctx.fillText("刷新两次后显示走势线",left+plotW/2,top+plotH/2);
  return;
}

// X轴刻度：首、中、尾
ctx.font="12px Microsoft YaHei, sans-serif";
ctx.textBaseline="top";
const xTickIndexes=[0,Math.floor((historyData.length-1)/2),historyData.length-1];
[...new Set(xTickIndexes)].forEach(i=>{
  const x=xMap(i);
  const label=historyData[i].time||`第 ${i+1} 次`;
  ctx.strokeStyle="rgba(226,232,240,.28)";
  ctx.beginPath();ctx.moveTo(x,h-bottom);ctx.lineTo(x,h-bottom+6);ctx.stroke();
  ctx.fillStyle="rgba(226,232,240,.62)";
  ctx.textAlign=i===0?"left":(i===historyData.length-1?"right":"center");
  ctx.fillText(label,x,h-bottom+12);
});

function line(key,color,name){
  ctx.strokeStyle=color;
  ctx.lineWidth=3;
  ctx.beginPath();
  historyData.forEach((p,i)=>{
    const x=xMap(i),y=yMap(p[key]);
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  });
  ctx.stroke();

  historyData.forEach((p,i)=>{
    const x=xMap(i),y=yMap(p[key]);
    ctx.beginPath();
    ctx.arc(x,y,4.2,0,Math.PI*2);
    ctx.fillStyle=color;
    ctx.fill();
    ctx.lineWidth=2;
    ctx.strokeStyle="rgba(15,23,42,.92)";
    ctx.stroke();
    chartPoints.push({x,y,time:p.time||`第 ${i+1} 次`,name,value:p[key],color});
  });
}

line("intl","#fbbf24","国际换算价");
line("china","#38bdf8","中国 Au99.99");

// 图例
ctx.textAlign="left";
ctx.textBaseline="middle";
ctx.font="13px Microsoft YaHei, sans-serif";
ctx.fillStyle="#fbbf24";ctx.fillRect(left,16,12,12);
ctx.fillStyle="#e2e8f0";ctx.fillText("国际换算价",left+18,22);
ctx.fillStyle="#38bdf8";ctx.fillRect(left+126,16,12,12);
ctx.fillStyle="#e2e8f0";ctx.fillText("中国 Au99.99",left+144,22);
ctx.fillStyle="rgba(255,255,255,.55)";
ctx.textAlign="left";
ctx.fillText(`区间：${fmt(min,2)} - ${fmt(max,2)} 元/克`,left,h-16);
}function updateUI(d){showError("");el("intlPrice").innerHTML=`${fmt(d.international_cny_per_g)}<span class="unit">元/克</span>`;el("chinaPrice").innerHTML=`${fmt(d.china_au9999_cny_per_g)}<span class="unit">元/克</span>`;el("spreadPrice").innerHTML=`${fmt(d.spread_cny_per_g)}<span class="unit">元/克</span>`;el("intlSub").textContent=`XAU/USD ${fmt(d.xauusd,2)}，USD/CNY ${fmt(d.usdcny,4)}；行情时间：${d.market_time||"--"}`;el("chinaSub").textContent=`最高 ${fmt(d.sge_high)}，最低 ${fmt(d.sge_low)}，开盘 ${fmt(d.sge_open)}；刷新时间：${nowText()}`;el("spreadSub").textContent=`价差比例：${fmt(d.spread_pct,2)}%。正数代表国内价更高，负数代表国内价更低。`;const delta=el("spreadDelta");delta.className=`delta ${Number(d.spread_cny_per_g)>=0?"up":"down"}`;delta.textContent=`${Number(d.spread_cny_per_g)>=0?"↑":"↓"} ${fmt(d.spread_pct,2)}%`;el("xauCell").textContent=`${fmt(d.xauusd,2)} 美元/盎司`;el("fxCell").textContent=`${fmt(d.usdcny,4)} 人民币/美元`;el("sgeCell").textContent=`${fmt(d.china_au9999_cny_per_g)} 元/克`;el("xauSource").textContent=d.xau_source||"--";el("fxSource").textContent=d.fx_source||"--";el("sgeSource").textContent=d.sge_source||"--";historyData.push({time:nowText(),intl:Number(d.international_cny_per_g),china:Number(d.china_au9999_cny_per_g)});if(historyData.length>40)historyData.shift();refreshHistory.unshift({time:nowText(),intl:fmt(d.international_cny_per_g),china:fmt(d.china_au9999_cny_per_g),source:d.from_cache?"缓存数据":`${d.xau_source||"--"} / ${d.fx_source||"--"} / ${d.sge_source||"--"}`});if(refreshHistory.length>6)refreshHistory.pop();drawChart();const cache=d.from_cache?"（缓存）":"";el("infoBox").textContent=`数据来源：${d.from_cache?"缓存数据":"实时数据"}；最后刷新：${d.updated_at||"--"}`+(d.cache_reason?`；缓存原因：${d.cache_reason}`:" ");el("detailSources").textContent=`XAU/USD：${d.xau_source||"--"}；USD/CNY：${d.fx_source||"--"}；SGE：${d.sge_source||"--"}`;let statusText=`数据更新时间：${d.updated_at||"--"}；行情时间：${d.market_time||"--"}；${d.from_cache?"当前正在显示缓存数据。":"当前为实时数据。"}`;if(!d.from_cache&&d.market_time){const marketTs=Date.parse(d.market_time.replace(/-/g,"/"));if(!Number.isNaN(marketTs)&&Date.now()-marketTs>24*3600*1000){statusText+=` 注意：行情时间已超过 24 小时，数据可能未及时更新。`;}}if(d.from_cache&&d.cache_age_seconds!=null){statusText+=` 缓存已保存：${d.cache_saved_at||"--"}，${Math.floor(d.cache_age_seconds/60)} 分钟之前。`}el("statusDetail").textContent=statusText;renderHistory();if(d.from_cache&&d.cache_reason){showError("当前显示缓存数据，原因：\n"+d.cache_reason)}else if(d.from_cache){showError("当前显示缓存数据，实时接口暂时不可用。" )}else{showError("")}setStatus(`已更新${cache}：${nowText()}，${formatRefreshText()}`)}function renderHistory(){const container=el("historyList");if(!refreshHistory.length){container.innerHTML="暂无刷新记录。";return}container.innerHTML=`<table><thead><tr><th>时间</th><th>国际</th><th>中国</th><th>来源</th></tr></thead><tbody>${refreshHistory.map(item=>`<tr><td>${item.time}</td><td>${item.intl}</td><td>${item.china}</td><td>${item.source}</td></tr>`).join("")}</tbody></table>`;}function scheduleRefresh(){if(refreshTimer)clearInterval(refreshTimer);refreshTimer=setInterval(refresh,refreshInterval)}function onIntervalChange(){refreshInterval=Number(el("intervalSelect").value)*1000;scheduleRefresh();setStatus(`刷新间隔已切换为 ${refreshInterval/1000} 秒。`)}async function refresh(){if(loading)return;loading=true;el("refreshBtn").disabled=true;setStatus(`<span class="dot"></span>正在获取最新行情...`);try{const res=await fetch("/api/gold?t="+Date.now(),{cache:"no-store"});const text=await res.text();let data;try{data=JSON.parse(text)}catch(e){throw new Error("接口返回不是 JSON："+text.slice(0,300))}if(!res.ok||!data.ok){const msg=data.error||"本地接口返回异常";showError("接口错误：\n"+msg+"\n\n看运行目录下 gold_debug.log 最后 20 行。");throw new Error(msg)}updateUI(data)}catch(err){setStatus("刷新失败："+(err.message||err),false)}finally{loading=false;el("refreshBtn").disabled=false}}function setupChartHover(){
const canvas=el("chart");
const tooltip=el("chartTooltip");
canvas.addEventListener("mousemove",ev=>{
  if(!chartPoints.length){tooltip.style.display="none";return}
  const rect=canvas.getBoundingClientRect();
  const sx=canvas.width/rect.width;
  const sy=canvas.height/rect.height;
  const mx=(ev.clientX-rect.left)*sx;
  const my=(ev.clientY-rect.top)*sy;
  let nearest=null;
  let best=999999;
  chartPoints.forEach(p=>{
    const dist=Math.hypot(mx-p.x,my-p.y);
    if(dist<best){best=dist;nearest=p}
  });
  if(!nearest||best>14){
    tooltip.style.display="none";
    return;
  }
  tooltip.innerHTML=`<b>${nearest.time}</b><br><span style="color:${nearest.color}">${nearest.name}</span>：${fmt(nearest.value,2)} 元/克`;
  tooltip.style.display="block";
  let left=ev.clientX-rect.left+14;
  let top=ev.clientY-rect.top+14;
  const maxLeft=rect.width-220;
  const maxTop=rect.height-78;
  if(left>maxLeft)left=Math.max(8,ev.clientX-rect.left-220);
  if(top>maxTop)top=Math.max(8,ev.clientY-rect.top-78);
  tooltip.style.left=left+"px";
  tooltip.style.top=top+"px";
});
canvas.addEventListener("mouseleave",()=>{tooltip.style.display="none"});
}el("refreshBtn").addEventListener("click",refresh);el("intervalSelect").addEventListener("change",onIntervalChange);setupChartHover();window.addEventListener("resize",drawChart);refresh();scheduleRefresh();
</script>
</body></html>'''


def log_debug(message: str):
    """把调试信息同时输出到控制台和日志文件（gold_debug.log）。

    每条日志带 [YYYY-MM-DD HH:MM:SS] 时间戳，方便排查问题顺序。
    写日志文件失败时静默忽略，不能因为写日志拖垮主程序。
    """
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    print(line, end="")  # 输出到控制台
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)  # 以追加方式写入日志文件
    except Exception:
        pass  # 日志写失败时静默跳过


def fetch_text(url: str, timeout: int = 20) -> str:
    """请求一个 URL 并返回解码后的文本内容。

    要点：
    - 伪装成浏览器请求头，提高免费接口的兼容性；
    - 优先使用不校验证书的 SSL 上下文；部分环境不支持该参数时自动回退；
    - 响应字节依次尝试 utf-8 / gbk / gb2312 解码，最后兜底忽略错误字符。
    """
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
        "Accept": "text/html,text/plain,application/json,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    try:
        with urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            raw = resp.read()
    except TypeError:
        # 某些 Python/SSL 组合不接受 context 参数，回退到普通请求
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    # 依次尝试常见编码，中文接口常用 gbk/gb2312
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")  # 最后兜底：忽略无法解码的字节


def to_float(value):
    """把各种形式的字符串安全地转成 float。

    - 自动去掉千分位逗号（如 "4,600.53" → 4600.53）；
    - 空值以及 N/D、N/A、NULL、- 等占位符返回 None；
    - 转换失败返回 None 而不是抛异常，方便调用方做兜底判断。
    """
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s.upper() in {"N/D", "N/A", "NA", "NULL", "-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_stooq_single_csv(text: str, symbol: str):
    """解析 Stooq q/l 接口的单品种 CSV 报价。

    带 h 参数时 CSV 没有表头，列顺序固定为：
        Symbol, Date, Time, Open, High, Low, Close, Volume
    所以收盘价在下标 6（Close），开盘价在下标 3（Open）。
    同时兼容旧格式：如果第一行表头是 symbol，则自动跳到第二行数据。
    """
    lines = [x.strip() for x in text.strip().splitlines() if x.strip()]
    if not lines:
        return None
    row = next(csv.reader([lines[0]]))
    # 旧格式带表头：跳过第一行，取真正的数据行
    if row and row[0].lower() == "symbol" and len(lines) > 1:
        row = next(csv.reader([lines[1]]))
    if len(row) < 7:
        log_debug(f"Stooq {symbol} CSV 列不足：{row}")
        return None
    # 优先用收盘价 Close，取不到时退回开盘价 Open
    price = to_float(row[6]) or to_float(row[3])
    if not price:
        log_debug(f"Stooq {symbol} CSV 价格不可用：{row}")
        return None
    return {"price": price, "time": f"{row[1]} {row[2]}".strip(), "source": "Stooq current CSV"}


def get_stooq_symbol(symbol: str):
    """按品种代码（如 XAUUSD、USDCNY）获取 Stooq 当前报价。

    先请求带 h 参数的无表头 CSV；解析失败时再试一次不带 h 参数的旧格式，
    避免部分环境下无表头 CSV 解析异常。
    """
    url = URL_STOOQ_QUOTE_ONE.format(symbol=symbol.lower())
    text = fetch_text(url)
    result = parse_stooq_single_csv(text, symbol)
    if result:
        return result
    # 尝试不带 h 参数的旧格式，避免部分环境因无表头 CSV 解析失败
    try:
        fallback_url = f"https://stooq.com/q/l/?s={symbol.lower()}&f=sd2t2ohlcv&e=csv"
        fallback_text = fetch_text(fallback_url)
        return parse_stooq_single_csv(fallback_text, symbol)
    except Exception:
        return None


def get_xauusd_goldprice():
    """从 GoldPrice.org 的 JSON 接口读取现货黄金价格（美元/盎司）。

    返回的 items 数组第一项通常包含 xauPrice 等字段，
    这里按常见字段名逐个尝试取值。
    """
    try:
        text = fetch_text(URL_GOLDPRICE_JSON)
        data = json.loads(text)
        items = data.get("items") or []
        if items:
            item = items[0]
            price = None
            # 依次尝试几个常见字段名，取到第一个有效价格
            for key in ("xauPrice", "price", "last", "bid", "ask"):
                price = to_float(item.get(key))
                if price:
                    break
            if price:
                time_str = item.get("ts") or item.get("date") or item.get("time") or ""
                return {"price": price, "time": str(time_str), "source": "GoldPrice.org JSON"}
    except Exception as e:
        log_debug(f"XAUUSD GoldPrice.org 失败：{repr(e)}")
    return None


def get_xauusd_metalslive():
    """从 Metals.live 的 JSON 接口读取现货黄金价格。

    该接口返回一个数组，第一项形如 [品种名, 价格, ...]，
    价格通常位于下标 1。
    """
    try:
        text = fetch_text(URL_METALSLIVE_GOLD)
        data = json.loads(text)
        if isinstance(data, list) and data:
            row = data[0]
            if isinstance(row, list) and len(row) >= 2:
                price = to_float(row[1])  # 下标 1 为价格
                if price:
                    source = str(row[0]) if row[0] else "Metals.live"
                    return {"price": price, "time": "", "source": f"Metals.live {source}"}
    except Exception as e:
        log_debug(f"XAUUSD Metals.live 失败：{repr(e)}")
    return None


def get_xauusd_yahoo():
    """从 Yahoo Finance 读取黄金价格（美元/盎司）。

    先试 XAU/USD（XAUUSD=X），失败再试 COMEX 黄金期货（GC=F）。
    regularMarketTime 是 Unix 秒时间戳，这里转换成可读的时间字符串。
    """
    for url, label in ((URL_YAHOO_XAU, "XAUUSD=X"), (URL_YAHOO_GC, "GC=F")):
        try:
            text = fetch_text(url)
            data = json.loads(text)
            result = (data.get("quoteResponse") or {}).get("result") or []
            if result:
                item = result[0]
                price = to_float(item.get("regularMarketPrice") or item.get("bid") or item.get("ask"))
                if price:
                    time_val = item.get("regularMarketTime")
                    time_str = ""
                    if time_val:
                        try:
                            # Unix 秒时间戳 → 可读字符串（UTC，仅作展示，未转本地时区）
                            time_str = datetime.utcfromtimestamp(int(time_val)).strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:
                            time_str = str(time_val)
                    return {"price": price, "time": time_str, "source": f"Yahoo Finance quote {label}"}
        except Exception as e:
            log_debug(f"XAUUSD Yahoo {label} 失败：{repr(e)}")
    return None


def get_xauusd():
    """获取 XAU/USD 国际金价（美元/盎司），按优先级依次尝试多个来源。

    顺序：Stooq 当前 CSV → Stooq HTML 抓取 → GoldPrice.org → Metals.live
          → Yahoo Finance → Stooq 日线 → FreeGoldAPI 日更 CSV
    每个来源失败都只记日志并继续尝试下一个，全部失败才抛 ValueError。
    """
    # 1. Stooq 当前 CSV
    try:
        data = get_stooq_symbol("XAUUSD")
        if data:
            return data
    except Exception as e:
        log_debug(f"XAUUSD Stooq CSV 失败：{repr(e)}")

    # 2. Stooq HTML 页面抓取，页面通常含 4600.53 $/ozt 这种文本
    try:
        html = fetch_text(URL_STOOQ_XAU_PAGE)
        text = re.sub(r"<[^>]+>", " ", html)      # 去掉所有 HTML 标签
        text = re.sub(r"\s+", " ", text).strip()  # 压缩连续空白，便于正则匹配
        # 第一层：精准匹配形如 "4600.53 $/ozt" 的报价文本
        m = re.search(r"(\d{3,5}(?:\.\d+)?)\s*\$/ozt", text)
        if m:
            return {"price": float(m.group(1)), "time": "", "source": "Stooq HTML scrape"}
        # 第二层：匹配形如 "XAU/USD 4600.53" 的文本
        m = re.search(r"XAU/USD[^0-9A-Za-z]{0,30}([\d,]{4,7}(?:\.\d+)?)", text, re.I)
        if m:
            return {"price": float(m.group(1).replace(",", "")), "time": "", "source": "Stooq HTML tight scrape"}
        # 第三层：宽松匹配页面中第一个 1000~10000 之间的数字（黄金大致价位区间）
        nums = [float(x.replace(",", "")) for x in re.findall(r"\b[\d,]{4,7}(?:\.\d+)?\b", text)]
        nums = [x for x in nums if 1000 <= x <= 10000]
        if nums:
            return {"price": nums[0], "time": "", "source": "Stooq HTML loose scrape"}
        log_debug("XAUUSD Stooq HTML 未解析到价格")
    except Exception as e:
        log_debug(f"XAUUSD Stooq HTML 失败：{repr(e)}")

    # 3. GoldPrice.org JSON 备用报价
    try:
        data = get_xauusd_goldprice()
        if data:
            return data
    except Exception as e:
        log_debug(f"XAUUSD GoldPrice.org 备用失败：{repr(e)}")

    # 4. Metals.live 备用报价
    try:
        data = get_xauusd_metalslive()
        if data:
            return data
    except Exception as e:
        log_debug(f"XAUUSD Metals.live 备用失败：{repr(e)}")

    # 5. Yahoo Finance 备用报价
    try:
        data = get_xauusd_yahoo()
        if data:
            return data
    except Exception as e:
        log_debug(f"XAUUSD Yahoo 备用失败：{repr(e)}")

    # 6. Stooq 日线 CSV，取最后一条 Close
    try:
        daily = fetch_text(URL_STOOQ_XAU_DAILY)
        rows = list(csv.DictReader(io.StringIO(daily.strip())))
        for row in reversed(rows):  # 数据按日期升序，从最后一行往前找最近收盘价
            close = to_float(row.get("Close"))
            if close:
                return {"price": close, "time": row.get("Date") or "", "source": "Stooq daily fallback"}
        log_debug("XAUUSD Stooq 日线无有效 Close")
    except Exception as e:
        log_debug(f"XAUUSD Stooq 日线失败：{repr(e)}")

    # 7. FreeGoldAPI 日更 CSV，字段名可能是 price，也可能第二列为价格
    try:
        csv_text = fetch_text(URL_FREE_GOLD_CSV)
        rows = list(csv.DictReader(io.StringIO(csv_text.strip())))
        for row in reversed(rows):
            val = None
            # 优先找列名像 price / gold / usd / close 的列
            for k, v in row.items():
                if k and k.lower() in {"price", "gold", "usd", "close"}:
                    val = to_float(v)
                    if val:
                        break
            # 找不到就尝试第二列及以后的所有数值列
            if not val:
                vals = list(row.values())
                for v in vals[1:]:
                    val = to_float(v)
                    if val:
                        break
            if val:
                return {"price": val, "time": row.get("date") or row.get("Date") or "", "source": "FreeGoldAPI daily fallback"}
        log_debug("FreeGoldAPI 未找到有效黄金价格")
    except Exception as e:
        log_debug(f"FreeGoldAPI 失败：{repr(e)}")

    raise ValueError("XAU/USD 获取失败：Stooq 当前价、HTML、日线和 FreeGoldAPI 都不可用")


def get_usdcny():
    """获取 USD/CNY 汇率（1 美元 = 多少人民币），按优先级尝试多个来源。

    顺序：ExchangeRate-API → ExchangeRate.host → Stooq 当前 CSV
    """
    # 1. ExchangeRate-API open access，通常更稳
    try:
        text = fetch_text(URL_ER_API_USD)
        data = json.loads(text)
        rate = to_float((data.get("rates") or {}).get("CNY"))  # rates 字典里取 CNY
        if rate:
            return {"price": rate, "time": data.get("time_last_update_utc") or "", "source": "ExchangeRate-API open access"}
    except Exception as e:
        log_debug(f"USD/CNY ExchangeRate-API 失败：{repr(e)}")

    # 2. ExchangeRate.host 备用
    try:
        text = fetch_text(URL_EXCHANGE_RATE_HOST)
        data = json.loads(text)
        rate = to_float((data.get("rates") or {}).get("CNY"))
        if rate:
            return {"price": rate, "time": data.get("date") or "", "source": "ExchangeRate.host fallback"}
    except Exception as e:
        log_debug(f"USD/CNY ExchangeRate.host 失败：{repr(e)}")

    # 3. Stooq 当前 CSV
    try:
        data = get_stooq_symbol("USDCNY")
        if data:
            return data
    except Exception as e:
        log_debug(f"USD/CNY Stooq CSV 失败：{repr(e)}")

    raise ValueError("USD/CNY 获取失败：ExchangeRate-API 和 Stooq 都不可用")


def parse_sge_delayed_quotes(text: str):
    """从 SGE 延时行情页面 HTML 中解析 Au99.99 数据。

    页面中通常有一段形如：
        Au99.99 最新价 最高价 最低价 开盘价
    的文本，正则按此顺序提取四个数字。
    返回 {"latest", "high", "low", "open"}，解析不到返回 None。
    """
    clean = re.sub(r"<[^>]+>", " ", text).replace(",", "")
    clean = re.sub(r"\s+", " ", clean).strip()  # 去标签、去千分位逗号、压缩空白
    m = re.search(r"\bAu99\.99\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", clean, re.I)
    if not m:
        return None
    latest, high, low, open_ = map(float, m.groups())
    if latest <= 0:  # 最新价非正数说明数据无效
        return None
    return {"latest": latest, "high": high, "low": low, "open": open_}


def parse_sge_daily_report(text: str):
    """从 SGE 日报数据中解析 Au99.99。

    日报数据行形如：
        YYYY-MM-DD Au99.99 开盘价 最高价 最低价 收盘价
    返回 {"latest": 收盘价, "high", "low", "open"}，解析不到返回 None。
    """
    clean = re.sub(r"<[^>]+>", " ", text).replace(",", "")
    clean = re.sub(r"\s+", " ", clean).strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}\s+Au99\.99\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", clean, re.I)
    if not m:
        return None
    open_, high, low, close = map(float, m.groups())
    if close <= 0:  # 收盘价非正数说明数据无效
        return None
    return {"latest": close, "high": high, "low": low, "open": open_}


def get_sge_au9999():
    """获取上海黄金交易所 Au99.99 价格（元/克）。

    先试中文/英文延时行情页；都失败时回退到日报接口，
    从今天开始往前尝试最近 10 天，取第一个有数据的日子。
    """
    errors = []
    # 1. 延时行情页（中文、英文两个入口）
    for name, url in (("SGE 中文延时行情", URL_SGE_CN), ("SGE English Delayed Quotes", URL_SGE_EN)):
        try:
            parsed = parse_sge_delayed_quotes(fetch_text(url))
            if parsed:
                parsed["source"] = name
                return parsed
            errors.append(f"{name}：未解析到 Au99.99")
        except Exception as e:
            errors.append(f"{name}：{repr(e)}")

    # 2. 日报备用：尝试最近 10 天，找到有数据的日期即返回
    for i in range(10):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            parsed = parse_sge_daily_report(fetch_text(URL_SGE_DAILY.format(d=d)))
            if parsed:
                parsed["source"] = f"SGE daily report fallback {d}"
                return parsed
            errors.append(f"SGE 日报 {d}：未解析到 Au99.99")
        except Exception as e:
            errors.append(f"SGE 日报 {d}：{repr(e)}")
    raise ValueError("Au99.99 获取失败：" + "；".join(errors[:10]))


def save_cache(data: dict):
    """把最近一次成功获取的行情保存到 gold_cache.json。

    保存失败只写日志，不影响本次正常返回结果；
    下次所有实时源都失败时，这里的数据会被读取出来兜底。
    """
    try:
        # 记录缓存保存时间，页面据此计算并展示缓存有多旧
        data["cache_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_debug(f"保存缓存失败：{repr(e)}")


def load_cache():
    """读取上次保存的缓存行情（gold_cache.json）。

    返回的 dict 会补上 from_cache=True、ok=True，并计算缓存已保存的秒数
    （cache_age_seconds，供页面显示）。文件不存在或解析失败返回 None。
    """
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["from_cache"] = True  # 标记当前数据来自缓存
        data["ok"] = True          # 让页面认为这次请求是成功的
        if data.get("cache_saved_at"):
            try:
                saved = datetime.strptime(data["cache_saved_at"], "%Y-%m-%d %H:%M:%S")
                # 缓存已保存秒数 = 现在 - 保存时间
                data["cache_age_seconds"] = int((datetime.now() - saved).total_seconds())
            except Exception:
                data["cache_age_seconds"] = None
        else:
            data["cache_age_seconds"] = None
        return data
    except Exception as e:
        log_debug(f"读取缓存失败：{repr(e)}")
        return None


def get_gold_data() -> dict:
    """组装一次完整的行情数据（本程序的核心入口）。

    流程：
    1) 分别获取 XAU/USD、USD/CNY、SGE Au99.99；
    2) 计算国际金价的人民币/克：XAU/USD × USD/CNY ÷ 31.1034768；
    3) 计算内外价差（中国金价 - 国际换算价）及其百分比；
    4) 成功则保存缓存并返回；
    5) 任一来源失败则读取缓存兜底，并把失败原因写入 cache_reason；
    6) 连缓存都没有时把异常抛给上层，由 HTTP 层返回 500。
    """
    try:
        xau = get_xauusd()      # 国际金价（美元/盎司）
        fx = get_usdcny()       # 汇率（1 美元 = ? 人民币）
        sge = get_sge_au9999()  # 上海金交所 Au99.99（元/克）

        # 核心换算公式：美元/盎司 → 人民币/克
        international = xau["price"] * fx["price"] / TROY_OUNCE_GRAMS
        china = sge["latest"]
        spread = china - international           # 内外价差（元/克）
        spread_pct = spread / international * 100 if international else 0.0  # 价差比例（%）
        data = {
            "ok": True,
            "from_cache": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # 本次数据组装时间
            "xauusd": xau["price"],
            "usdcny": fx["price"],
            "market_time": xau.get("time") or fx.get("time") or "",      # 行情源自带的时间
            "xau_source": xau.get("source", ""),
            "fx_source": fx.get("source", ""),
            "sge_source": sge.get("source", ""),
            "international_cny_per_g": international,
            "china_au9999_cny_per_g": china,
            "sge_high": sge["high"],
            "sge_low": sge["low"],
            "sge_open": sge["open"],
            "spread_cny_per_g": spread,
            "spread_pct": spread_pct,
        }
        save_cache(data)  # 成功获取后立即落盘，供断网时兜底
        return data
    except Exception as e:
        log_debug("实时数据失败，尝试缓存：" + repr(e))
        cached = load_cache()
        if cached:
            cached["cache_reason"] = str(e)  # 记录失败原因，页面会展示给用户
            return cached
        raise  # 无缓存可用时向上抛出，由 HTTP 层返回 500


class GoldHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：负责把行情 JSON 和前端页面返回给浏览器。"""

    def send_text(self, body: str, content_type: str = "text/html; charset=utf-8", status: int = 200):
        """发送一段文本响应，并设置 utf-8 编码、禁用缓存等响应头。

        客户端提前断开时捕获常见网络异常并记日志，避免服务器崩溃。
        """
        body_bytes = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store")  # 禁止浏览器缓存行情数据
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as e:
            log_debug(f"客户端连接中断，响应未完成：{repr(e)}")
        
    def do_GET(self):
        """处理浏览器 GET 请求。

        - /api/gold：返回最新行情 JSON（成功 200，失败 500 + 错误详情）；
        - 其他任意路径：返回整个前端页面 HTML。
        """
        if self.path.startswith("/api/gold"):
            try:
                data = get_gold_data()
                self.send_text(json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                detail = traceback.format_exc()  # 完整堆栈写入日志，便于排查
                log_debug("API ERROR:\n" + detail)
                self.send_text(json.dumps({"ok": False, "error": str(e), "detail": detail}, ensure_ascii=False), "application/json; charset=utf-8", status=500)
            return
        self.send_text(HTML)  # 其余路径统一返回前端页面

    def log_message(self, fmt, *args):
        """把服务器的自带访问日志重定向到 log_debug，统一写入日志文件。"""
        log_debug(fmt % args)


def main():
    """启动本地 HTTP 服务器并自动打开浏览器。

    使用 ThreadingHTTPServer（多线程），多个浏览器请求不会互相阻塞；
    一直运行直到按 Ctrl+C，结束后释放端口。
    """
    server = ThreadingHTTPServer((HOST, PORT), GoldHandler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 64)
    print("黄金价格实时看板 V5.1 已启动")
    print("浏览器地址：", url)
    print("停止运行：在此窗口按 Ctrl + C")
    print("如果仍失败，把 gold_debug.log 最后 20 行发给我")
    print("=" * 64)
    try:
        webbrowser.open(url)  # 自动打开默认浏览器，失败则忽略，可手动访问
    except Exception:
        pass
    try:
        server.serve_forever()  # 一直监听，直到收到 Ctrl+C
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()  # 释放端口，程序干净退出


if __name__ == "__main__":
    # 设置全局网络超时 25 秒，防止某个免费接口卡住整个请求
    socket.setdefaulttimeout(25)
    main()
