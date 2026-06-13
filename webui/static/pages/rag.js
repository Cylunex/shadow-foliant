import { reactive, onMounted } from 'vue'
import { api } from '../lib.js'

const SOURCES = [
  { k: 'analysis', t: '历史分析' }, { k: 'news', t: '新闻' },
  { k: 'reco', t: '历史推荐' }, { k: 'report', t: '研报' }, { k: 'longhubang', t: '龙虎榜' },
]
const TAG = { analysis: '历史分析', news: '新闻', reco: '历史推荐', report: '研报', longhubang: '龙虎榜' }

export default {
  template: `
  <div>
    <div class="h1">🔎 语义搜索</div>
    <p class="sub">本地知识库向量检索:BGE-M3 嵌入 → pgvector 余弦召回 → TEI rerank 精排。私有、不发第三方。</p>
    <div class="card">
      <div class="row">
        <div style="flex:1;min-width:280px"><label>查询(自然语言)</label><input v-model="s.q" @keyup.enter="run" placeholder="如:大股东减持出货风险 / 新能源车产业链景气" style="width:100%"/></div>
        <button :disabled="s.loading" @click="run">{{s.loading?'检索中…':'搜索'}}</button>
      </div>
      <div style="margin-top:10px;display:flex;gap:14px;flex-wrap:wrap;align-items:center">
        <span style="color:var(--muted);font-size:12px">来源过滤:</span>
        <label v-for="src in sources" :key="src.k" style="display:flex;align-items:center;gap:5px;color:var(--muted);font-size:13px">
          <input type="checkbox" v-model="s.pick[src.k]"/> {{src.t}}
        </label>
        <span v-if="s.stat" class="pill" style="margin-left:auto">库存 {{s.stat.total||0}} · 嵌入 {{s.stat.embed_ok?'✅':'❌'}} rerank {{s.stat.rerank_ok?'✅':'❌'}}</span>
      </div>
    </div>
    <div v-if="s.err" class="err">{{s.err}}</div>
    <div v-if="s.hits" class="card">
      <h3>{{s.hits.length}} 条结果</h3>
      <div v-for="(h,i) in s.hits" :key="i" style="padding:12px 0;border-bottom:1px solid var(--line)">
        <div style="display:flex;gap:10px;align-items:center">
          <span class="pill">{{tag(h.source_type)}}</span>
          <b>{{h.title||'—'}}</b>
          <span style="margin-left:auto;color:var(--accent);font-weight:700">{{(h.score*100).toFixed(1)}}</span>
        </div>
        <div style="color:var(--muted);margin-top:6px;font-size:13px;line-height:1.6">{{h.content}}</div>
      </div>
      <div v-if="!s.hits.length" class="loading">无结果(或嵌入/rerank/pgvector 服务未就绪)。</div>
    </div>
  </div>`,
  setup() {
    const s = reactive({ q: '大股东减持出货风险', pick: {}, hits: null, err: '', loading: false, stat: null })
    const tag = k => TAG[k] || k
    async function run() {
      s.loading = true; s.err = ''; s.hits = null
      try {
        const srcs = Object.keys(s.pick).filter(k => s.pick[k]).join(',')
        s.hits = await api('/api/rag/search?q=' + encodeURIComponent(s.q) + '&top_k=10' + (srcs ? '&sources=' + srcs : ''))
      } catch (e) { s.err = '' + e } finally { s.loading = false }
    }
    onMounted(async () => {
      try {
        const d = await api('/api/rag/stats')
        s.stat = { total: d.store?.total, embed_ok: d.services?.embed_ok, rerank_ok: d.services?.rerank_ok }
      } catch (e) {}
    })
    return { s, sources: SOURCES, tag, run }
  }
}
