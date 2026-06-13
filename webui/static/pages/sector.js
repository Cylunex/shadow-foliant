import { reactive, computed, onMounted } from 'vue'
import { api, zh, cell, cls, useSort } from '../lib.js'

const SOURCES = [
  { k:'sina', t:'新浪行业' }, { k:'ths', t:'同花顺行业' },
]

export default {
  template: `
  <div>
    <div class="h1">📈 板块</div>
    <p class="sub">行业板块强弱排行(新浪/同花顺源,稳定可达)。点表头排序。红涨绿跌。</p>
    <div class="tabs">
      <div v-for="s in sources" :key="s.k" class="tab" :class="{active:cur===s.k}" @click="go(s.k)">{{s.t}}</div>
      <button class="ghost" style="margin-left:auto" :disabled="ai.loading" @click="aiRotation">{{ai.loading?'研判中…':'🤖 AI轮动研判'}}</button>
    </div>
    <div v-if="ai.err" class="err">{{ai.err}}</div>
    <div v-if="ai.text" class="card" style="border-left:3px solid var(--accent)">
      <h3>🤖 板块轮动 AI 研判 <span style="color:var(--muted);font-weight:400;font-size:12px">{{ai.provider}}</span></h3>
      <pre style="white-space:pre-wrap;color:var(--txt);margin:0;font:13px/1.7 inherit">{{ai.text}}</pre>
    </div>
    <div v-if="m.err" class="err">{{m.err}}</div>
    <div class="card">
      <div v-if="m.loading" class="loading">加载中…</div>
      <table v-else-if="rows.length"><thead><tr>
        <th v-for="c in cols" :key="c" @click="sortBy(c)" style="cursor:pointer;user-select:none">{{zh(c)}}{{arrow(c)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in sorted" :key="i">
          <td v-for="c in cols" :class="(c==='涨跌幅'||c==='领涨幅'||c==='净流入')?cls(r[c]):''">{{cell(r[c])}}</td>
        </tr></tbody></table>
      <div v-else class="loading">暂无数据。</div>
    </div>
  </div>`,
  setup(){
    const m = reactive({ data:[], err:'', loading:false })
    const cur = reactive({ v:'sina' })
    const rows = computed(()=> Array.isArray(m.data)?m.data:[])
    const cols = computed(()=> rows.value.length ? Object.keys(rows.value[0]) : [])
    const { sortBy, arrow, sorted } = useSort(()=> rows.value, '涨跌幅', -1)
    async function load(src){
      m.loading=true; m.err=''; m.data=[]
      try{ m.data = await api('/api/sector/board?source='+src) || [] }
      catch(e){ m.err=''+e }finally{ m.loading=false }
    }
    function go(k){ cur.v=k; load(k) }
    const ai = reactive({ text:'', provider:'', err:'', loading:false })
    async function aiRotation(){
      ai.loading=true; ai.err=''
      try{ const r = await api('/api/sector/ai-rotation'); if(r.error){ai.err=r.error}else{ai.text=r.analysis; ai.provider=r.provider} }
      catch(e){ ai.err=''+e }finally{ ai.loading=false }
    }
    onMounted(()=> load('sina'))
    return { m, ai, sources:SOURCES, cur:computed(()=>cur.v), rows, cols, sorted, sortBy, arrow, go, aiRotation, zh, cell, cls }
  }
}
