import { reactive } from 'vue'
import { api, zh } from '../lib.js'

// 通用渲染:数组→表格,对象→键值,标量→文本
function isObj(v){ return v && typeof v==='object' && !Array.isArray(v) }

export default {
  template: `
  <div>
    <div class="h1">🌍 宏观</div>
    <p class="sub">宏观周期快照:GDP / CPI / PMI / 货币 / 利率 / 大类资产等。抓取较多,首次较慢。</p>
    <div class="card"><button :disabled="m.loading" @click="load">{{m.loading?'抓取中…(较慢)':'获取宏观快照'}}</button></div>
    <div v-if="m.err" class="err">{{m.err}}</div>
    <div v-for="[key,val] in entries" :key="key" class="card">
      <h3>{{key}}</h3>
      <table v-if="Array.isArray(val) && val.length && isObj(val[0])">
        <thead><tr><th v-for="c in keysOf(val[0])">{{zh(c)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in val.slice(0,30)" :key="i"><td v-for="c in keysOf(val[0])">{{disp(r[c])}}</td></tr></tbody>
      </table>
      <table v-else-if="isObj(val)">
        <tbody><tr v-for="[k,v] in Object.entries(val)" :key="k"><td style="color:var(--muted)">{{zh(k)}}</td><td style="text-align:left">{{disp(v)}}</td></tr></tbody>
      </table>
      <div v-else>{{disp(val)}}</div>
    </div>
  </div>`,
  setup(){
    const m = reactive({ data:null, err:'', loading:false })
    const entries = reactive([])
    const keysOf = o => Object.keys(o).slice(0,8)
    const disp = v => v==null?'—':(typeof v==='object'?JSON.stringify(v).slice(0,80):''+v)
    async function load(){
      m.loading=true; m.err=''; entries.length=0
      try{
        m.data = await api('/api/macro')
        Object.entries(m.data||{}).forEach(e=>entries.push(e))
      }catch(e){ m.err=''+e }finally{ m.loading=false }
    }
    return { m, entries, isObj, keysOf, disp, load, zh }
  }
}
