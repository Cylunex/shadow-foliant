import { reactive, computed, onMounted } from 'vue'
import { api, useSort } from '../lib.js'

const SCOLS = [
  { k:'symbol', t:'代码' }, { k:'name', t:'名称' }, { k:'rating', t:'评级' },
  { k:'entry_range', t:'进场区间' },
  { k:'take_profit', t:'止盈' }, { k:'stop_loss', t:'止损' },
  { k:'current_price', t:'现价' },
]
const NCOLS = [
  { k:'symbol', t:'代码' }, { k:'name', t:'名称' }, { k:'type', t:'类型' },
  { k:'message', t:'通知内容' },
  { k:'triggered_at', t:'触发时间' },
]

export default {
  template: `
  <div>
    <div class="h1">👁️ 监测 · 盯盘</div>
    <p class="sub">自选监测股 + 触发条件 + 最近通知。监测服务需常驻运行。</p>
    <div v-if="m.err" class="err">{{m.err}}</div>
    <div class="card">
      <h3>监测列表({{(m.stocks||[]).length}})</h3>
      <table v-if="m.stocks&&m.stocks.length"><thead><tr><th v-for="c in SCOLS" :key="c.k" @click="sortS(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrowS(c.k)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in sortedS" :key="i"><td v-for="c in SCOLS">{{fmtCell(r,c)}}</td></tr></tbody></table>
      <div v-else class="loading">暂无监测股(在监测服务里添加;本机 SQLite 为空属正常)。</div>
    </div>
    <div class="card">
      <h3>最近通知({{(m.notifs||[]).length}})</h3>
      <table v-if="m.notifs&&m.notifs.length"><thead><tr><th v-for="c in NCOLS" :key="c.k" @click="sortN(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrowN(c.k)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in sortedN" :key="i"><td v-for="c in NCOLS">{{fmtCell(r,c)}}</td></tr></tbody></table>
      <div v-else class="loading">暂无通知。</div>
    </div>
  </div>`,
  setup(){
    const m = reactive({ stocks:[], notifs:[], err:'' })
    const { sortBy:sortS, arrow:arrowS, sorted:sortedS } = useSort(()=> m.stocks, '', 1)
    const { sortBy:sortN, arrow:arrowN, sorted:sortedN } = useSort(()=> m.notifs, '', 1)

    function fmtCell(r, c){
      let v = r[c.k]
      if(v == null) return '—'
      // entry_range 格式化
      if(c.k==='entry_range' && v && typeof v==='object') return (v.min||'?')+' ~ '+(v.max||'?')
      // 时间格式化
      if(c.k==='triggered_at' && typeof v==='string') return v.slice(0,19).replace('T',' ')
      return typeof v==='object' ? JSON.stringify(v) : ''+v
    }

    async function load(){
      m.err=''
      try{ m.stocks = await api('/api/monitor/stocks') || [] }catch(e){ m.err=''+e }
      try{ m.notifs = await api('/api/monitor/notifications') || [] }catch(e){}
    }
    onMounted(load)
    return { m, SCOLS, NCOLS, sortedS, sortS, arrowS, sortedN, sortN, arrowN, fmtCell }
  }
}
