import { reactive, computed, onMounted } from 'vue'
import { api, zh, cell, useSort } from '../lib.js'

const TABS = [
  { k: 'flash', t: '财经快讯' }, { k: 'north', t: '北向资金' }, { k: 'hot', t: '热门题材' },
  { k: 'dragon', t: '龙虎榜' }, { k: 'inst', t: '龙虎榜·机构' }, { k: 'news', t: '个股新闻' },
]
const DATED = new Set(['hot', 'dragon'])   // 支持按日期查询的 tab

export default {
  template: `
  <div>
    <div class="h1">📡 市场</div>
    <p class="sub">北向资金(自2024.8起多源断供) / 热门题材 / 龙虎榜 / 个股新闻 一站式。</p>
    <div class="tabs">
      <div v-for="t in tabs" :key="t.k" class="tab" :class="{active:cur===t.k}" @click="go(t.k)">{{t.t}}</div>
      <button v-if="cur==='flash'||cur==='inst'" class="ghost" style="margin-left:auto" :disabled="mai.loading" @click="runAi">{{mai.loading?'研判中…':'🤖 AI研判'}}</button>
    </div>
    <div v-if="mai.err" class="err">{{mai.err}}</div>
    <div v-if="mai.text&&(cur==='flash'||cur==='inst')" class="card" style="border-left:3px solid var(--accent)">
      <h3>🤖 AI 研判 <span style="color:var(--muted);font-weight:400;font-size:12px">{{mai.provider}}</span></h3>
      <pre style="white-space:pre-wrap;color:var(--txt);margin:0;font:13px/1.7 inherit">{{mai.text}}</pre>
    </div>
    <div v-if="dated" class="card"><div class="row">
      <div><label>日期</label>
        <select v-model="m.date" @change="reload">
          <option v-for="d in m.dates" :value="d">{{d}}</option>
        </select>
      </div>
      <span class="pill">默认最新交易日</span>
    </div></div>
    <div v-if="cur==='news'" class="card"><div class="row">
      <div><label>股票代码</label><input v-model="m.code" @keyup.enter="loadNews" placeholder="如 600519"/></div>
      <button :disabled="m.loading" @click="loadNews">{{m.loading?'加载中…':'查新闻'}}</button>
    </div></div>
    <div v-if="m.err" class="err">{{m.err}}</div>
    <div class="card">
      <div v-if="m.loading" class="loading">加载中…</div>
      <!-- 财经快讯:新闻流 -->
      <div v-else-if="cur==='flash'">
        <div v-if="senti.index!=null" style="display:flex;gap:16px;align-items:center;padding:10px 12px;margin-bottom:12px;background:var(--panel2);border-radius:9px">
          <div><span style="color:var(--muted);font-size:12px">新闻情绪</span>
            <span style="font-size:22px;font-weight:700;margin-left:8px" :class="senti.index>=60?'red':(senti.index<=40?'green':'')">{{senti.index}}</span>
            <b style="margin-left:6px" :class="senti.index>=60?'red':(senti.index<=40?'green':'')">{{senti.class}}</b></div>
          <div style="color:var(--muted);font-size:13px">利好 {{senti.positive}} · 利空 {{senti.negative}}（{{senti.total_news}} 条快讯关键词计数）</div>
        </div>
        <div v-for="(n,i) in rows" :key="i" style="padding:11px 0;border-bottom:1px solid var(--line)">
          <div style="display:flex;gap:12px;align-items:baseline">
            <a v-if="n.url" :href="n.url" target="_blank" rel="noopener" style="color:var(--txt);font-weight:600;text-decoration:none;flex:1">{{n.title}}</a>
            <b v-else style="flex:1">{{n.title}}</b>
            <span style="color:var(--muted);font-size:12px;white-space:nowrap">{{n.time}}</span>
          </div>
          <div v-if="n.summary&&n.summary!==n.title" style="color:var(--muted);margin-top:5px;font-size:13px;line-height:1.65">{{n.summary}}</div>
        </div>
        <div v-if="!rows.length" class="loading">暂无快讯(财经快讯源暂不可达)。</div>
      </div>
      <!-- 其他:通用表格 -->
      <table v-else-if="rows.length"><thead><tr><th v-for="c in cols" :key="c" @click="sortBy(c)" style="cursor:pointer;user-select:none">{{zh(c)}}{{arrow(c)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in sorted" :key="i"><td v-for="c in cols">{{cell(r[c])}}</td></tr></tbody></table>
      <div v-else class="loading">暂无数据。</div>
    </div>
  </div>`,
  setup() {
    const m = reactive({ data: [], code: '600519', date: '', dates: [], err: '', loading: false })
    const cur = reactive({ v: 'flash' })
    const senti = reactive({ index: null, class: '', positive: 0, negative: 0, total_news: 0 })
    async function loadSentiment() {
      try { const s = await api('/api/market/news-sentiment'); Object.assign(senti, s) } catch (e) {}
    }
    const mai = reactive({ text: '', provider: '', err: '', loading: false })
    async function runAi() {
      mai.loading = true; mai.err = ''
      const url = cur.v === 'inst' ? '/api/market/lhb-ai' : '/api/market/news-ai'
      try { const r = await api(url); if (r.error) { mai.err = r.error } else { mai.text = r.analysis; mai.provider = r.provider } }
      catch (e) { mai.err = '' + e } finally { mai.loading = false }
    }
    const rows = computed(() => Array.isArray(m.data) ? m.data : [])
    const cols = computed(() => rows.value.length ? Object.keys(rows.value[0]) : [])
    const dated = computed(() => DATED.has(cur.v))
    const { sortBy, arrow, sorted } = useSort(() => rows.value, '', 1)

    async function ensureDates() {
      if (m.dates.length) return
      try {
        m.dates = await api('/api/market/trade-dates') || []
        if (m.dates.length && !m.date) m.date = m.dates[0]
      } catch (e) {}
    }
    async function load(url) {
      m.loading = true; m.err = ''; m.data = []
      try { m.data = await api(url) || [] } catch (e) { m.err = '' + e } finally { m.loading = false }
    }
    function urlFor(k) {
      if (k === 'flash') return '/api/market/news'
      if (k === 'north') return '/api/market/north?days=30'
      if (k === 'hot') return '/api/market/hot' + (m.date ? '?date=' + m.date : '')
      if (k === 'dragon') return '/api/market/dragon' + (m.date ? '?date=' + m.date : '')
      if (k === 'inst') return '/api/market/lhb-inst?days=14'
      return null
    }
    async function go(k) {
      cur.v = k
      if (DATED.has(k)) await ensureDates()
      const u = urlFor(k)
      if (u) load(u); else m.data = []   // news 需输代码
      if (k === 'flash') loadSentiment()
    }
    function reload() { const u = urlFor(cur.v); if (u) load(u) }
    function loadNews() { load('/api/market/news/' + m.code) }
    onMounted(() => { load(urlFor('flash')); loadSentiment() })
    return { m, senti, mai, runAi, tabs: TABS, cur: computed(() => cur.v), dated, rows, cols, sorted, sortBy, arrow, go, reload, loadNews, zh, cell }
  }
}
