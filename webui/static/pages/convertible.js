import { reactive, computed, onMounted } from 'vue'
import { api, cell, cls, useSort } from '../lib.js'

// 列定义(中文表头 + 取值键)
const COLS = [
  { k:'code', t:'代码' }, { k:'name', t:'转债' },
  { k:'price', t:'现价' }, { k:'change_pct', t:'涨跌幅', color:true },
  { k:'premium_pct', t:'溢价率%' }, { k:'double_low', t:'双低' },
  { k:'conv_value', t:'转股价值' }, { k:'rating', t:'评级' },
  { k:'remain_scale_yi', t:'剩余规模(亿)' }, { k:'remain_years', t:'剩余年限' },
  { k:'ytm_pct', t:'到期收益%' }, { k:'stock_name', t:'正股' },
]

export default {
  template: `
  <div>
    <div class="h1">💎 可转债 · 双低</div>
    <p class="sub">双低 = 现价 + 转股溢价率(越低越好,兼顾债底保护 + 跟涨弹性)。按双低升序排行;护栏:价≤上限、溢价≤上限、规模≥1亿、评级≥下限。点表头可排序。</p>
    <div class="card" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">
      <label>双低 TopN <input type="number" v-model.number="f.top_n" style="width:70px"></label>
      <label>现价上限 <input type="number" v-model.number="f.max_price" style="width:80px"></label>
      <label>溢价率上限% <input type="number" v-model.number="f.max_premium" style="width:80px"></label>
      <label>评级下限
        <select v-model="f.min_rating">
          <option value="AAA">AAA</option><option value="AA+">AA+</option>
          <option value="AA">AA</option><option value="AA-">AA-</option>
          <option value="A+">A+</option><option value="">不限</option>
        </select>
      </label>
      <button @click="load(true)" :disabled="m.loading">{{m.loading?'加载中…':'🔄 刷新'}}</button>
    </div>

    <div v-if="m.summary" class="card" style="display:flex;gap:28px;flex-wrap:wrap">
      <div><div class="sub">全市场只数</div><div style="font-size:20px;font-weight:600">{{m.summary.count}}</div></div>
      <div><div class="sub">中位现价</div><div style="font-size:20px;font-weight:600">{{m.summary.median_price}}</div></div>
      <div><div class="sub">中位双低</div><div style="font-size:20px;font-weight:600">{{m.summary.median_double_low}}</div></div>
      <div><div class="sub">中位溢价率</div><div style="font-size:20px;font-weight:600">{{m.summary.median_premium_pct}}%</div></div>
    </div>

    <div v-if="m.err" class="err">{{m.err}}</div>
    <div class="card">
      <div v-if="m.loading" class="loading">加载中…</div>
      <table v-else-if="rows.length"><thead><tr>
        <th v-for="c in cols" :key="c.k" @click="sortBy(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrow(c.k)}}</th>
      </tr></thead><tbody>
        <tr v-for="(r,i) in sorted" :key="i">
          <td v-for="c in cols" :key="c.k" :class="c.color?cls(r[c.k]):''">{{cell(r[c.k])}}</td>
        </tr>
      </tbody></table>
      <div v-else class="loading">暂无数据(本机东财 push2 受限时仅集思录约30只;生产为全市场)。</div>
    </div>
  </div>`,
  setup(){
    const m = reactive({ picks:[], summary:null, err:'', loading:false })
    const f = reactive({ top_n:30, max_price:135, max_premium:40, min_rating:'A+' })
    const rows = computed(()=> Array.isArray(m.picks)?m.picks:[])
    const cols = COLS
    const { sortBy, arrow, sorted } = useSort(()=> rows.value, 'double_low', 1)
    async function load(refresh){
      m.loading=true; m.err=''
      try{
        const q = `?top_n=${f.top_n}&max_price=${f.max_price}&max_premium=${f.max_premium}&min_rating=${encodeURIComponent(f.min_rating)}${refresh?'&refresh=1':''}`
        const r = await api('/api/convertible/screen'+q)
        m.picks = r.picks || []; m.summary = r.summary || null
      }catch(e){ m.err=''+e }finally{ m.loading=false }
    }
    onMounted(()=> load(false))
    return { m, f, rows, cols, sorted, sortBy, arrow, load, cell, cls }
  }
}
