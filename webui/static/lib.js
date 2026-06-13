// 共享工具:API 调用、格式化、图表助手(ECharts 走全局 window.echarts)

import { reactive, computed } from 'vue'

// 通用列表排序:点表头切换排序列/方向。数字、数字型字符串、文本(中文)都支持。
// 用法:const { sortBy, arrow, sorted } = useSort(()=>state.rows, 'mv', -1)
//   模板表头:<th @click="sortBy(key)" style="cursor:pointer">{{title}}{{arrow(key)}}</th>
//   表体遍历 sorted.value 而非原数组。
export function useSort(getRows, defKey='', defDir=-1){
  const sort = reactive({ k:defKey, dir:defDir })
  function sortBy(k){ if(sort.k===k) sort.dir = -sort.dir; else { sort.k=k; sort.dir=-1 } }
  function arrow(k){ return sort.k===k ? (sort.dir<0?' ↓':' ↑') : '' }
  const sorted = computed(()=>{
    const arr = (getRows()||[]).slice()
    if(!sort.k) return arr
    arr.sort((a,b)=>{
      let va=a[sort.k], vb=b[sort.k]
      if(va==null && vb==null) return 0
      if(typeof va==='number' && typeof vb==='number') return sort.dir*(va-vb)
      const fa=parseFloat(va), fb=parseFloat(vb)
      const numeric = !isNaN(fa) && !isNaN(fb) && String(va).trim()!=='' && String(vb).trim()!==''
      if(numeric && fa!==fb) return sort.dir*(fa-fb)
      if(va==null) va=''; if(vb==null) vb=''
      return sort.dir*String(va).localeCompare(String(vb),'zh')
    })
    return arr
  })
  return { sort, sortBy, arrow, sorted }
}

// 轻量 markdown → HTML(先转义防XSS,再处理标题/粗体/列表/换行)。LLM输出多为markdown。
export function mdLite(s){
  if(!s) return ''
  const esc = String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  const out = []
  let inList = false
  for(let line of esc.split('\n')){
    let m
    if((m = line.match(/^#{1,3}\s+(.*)$/))){ if(inList){out.push('</ul>');inList=false} out.push('<h4 style="margin:10px 0 4px">'+inline(m[1])+'</h4>'); continue }
    if((m = line.match(/^\s*[-*•]\s+(.*)$/)) || (m = line.match(/^\s*\d+[\.、]\s+(.*)$/))){ if(!inList){out.push('<ul style="margin:4px 0 4px 18px">');inList=true} out.push('<li>'+inline(m[1])+'</li>'); continue }
    if(inList){out.push('</ul>');inList=false}
    out.push(line.trim()? '<div>'+inline(line)+'</div>' : '<div style="height:6px"></div>')
  }
  if(inList) out.push('</ul>')
  function inline(t){ return t.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`(.+?)`/g,'<code>$1</code>') }
  return out.join('')
}

export async function api(path, opts){
  // 统一接口前缀:任何写法都规整为「相对」路径 api/xxx
  //  · 兼容历史三种写法:'/api/xxx'、'api/xxx'、'xxx'(genome 页就是裸 'strategy-genome/..')
  //  · 关键:用相对路径(无前导 /),浏览器基于当前文档 URL 解析。本应用是 hash 路由,
  //    pathname 不变 → 部署在 nginx 子路径(如 /stock/)时,fetch('api/x') 自动解析为
  //    /stock/api/x,单个 `location /stock/` 即可转发「页面+静态+接口」全部,无需为 /api 单配规则;
  //    本地根路径(localhost:8601/)下解析为 /api/x,照常工作。
  let p = String(path).replace(/^\/+/, '').replace(/^api\//, '')
  const r = await fetch('api/' + p, opts)
  const j = await r.json()
  if(!j.ok) throw new Error(j.error || '请求失败')
  return j.data
}

export const fmt  = v => (v==null||v==='')?'—':(+v).toFixed(2)
export const fmt4 = v => (v==null)?'—':(+v).toFixed(4)
export const pct  = v => (v==null)?'—':((v>=0?'+':'')+(v*100).toFixed(2)+'%')
export const money= v => (v==null)?'—':(+v).toLocaleString('zh',{minimumFractionDigits:3,maximumFractionDigits:3})
// A股惯例:红涨绿跌 —— 正(涨)→红,负(跌)→绿(与欧美相反)
export const cls  = v => v==null?'':(v>=0?'red':'green')

// 字段名 → 中文表头(通用表格用;查不到回退原名)
const ZH = {
  // 北向资金
  trade_date:'交易日', hgt_yi:'沪股通(亿)', sgt_yi:'深股通(亿)', net_total:'北向合计(亿)',
  net_hgt:'沪净流入', net_sgt:'深净流入', net_tgt:'港股通净', source:'来源', last_time:'更新时间',
  // 通用
  date:'日期', code:'代码', name:'名称', symbol:'代码', price:'现价', close:'收盘',
  current_price:'现价', open:'开盘', high:'最高', low:'最低', volume:'成交量', amount:'成交额',
  change_pct:'涨跌%', change_amt:'涨跌额', pct:'涨跌%', turnover_pct:'换手%', mcap_yi:'市值(亿)',
  pe_ttm:'市盈率', pe:'市盈率', pb:'市净率', roe:'ROE', peg:'PEG', composite:'综合分', rank:'排名',
  revenue_growth:'营收增速', profit_growth:'净利增速', debt_ratio:'负债率', dividend_yield:'股息率',
  momentum:'动量', volatility:'波动', ocf:'经营现金流', net_inflow:'主力净流入',
  // 监测 / 通知
  entry_range:'进场区间', take_profit:'止盈', stop_loss:'止损', notification_enabled:'通知',
  quant_enabled:'量化', created_at:'创建时间', triggered_at:'触发时间', updated_at:'更新时间',
  type:'类型', message:'内容', is_read:'已读', stock_id:'股票', id:'ID', note:'备注',
  // 基金
  unit_nav:'单位净值', acc_nav:'累计净值', nav:'净值', gsz:'估算净值', gszzl:'估算涨跌%',
}
export const zh = k => ZH[k] || k

// 枚举/分类值 → 中文显示
const VAL = {
  entry: '进场', take_profit: '止盈', stop_loss: '止损',
  BUY: '买入', SELL: '卖出', HOLD: '持有',
  success: '成功', error: '失败', skipped: '跳过', running: '运行中',
  buy: '买入', sell: '卖出', hold: '持有',
}

// 通用表格单元格显示:数字去浮点噪声(≤4位小数),枚举值中文化,对象转片段
export function cell(v) {
  if (v == null || v === '') return '—'
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString('zh')
    const r = Math.round(v * 10000) / 10000   // 去 1.89000000001 噪声
    return Math.abs(r) >= 10000 ? r.toLocaleString('zh', { maximumFractionDigits: 2 }) : String(r)
  }
  if (typeof v === 'string') return VAL[v] || v
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 60)
  return '' + v
}

const _charts = new WeakMap()
function _inst(el){
  let c = _charts.get(el)
  if(!c){ c = window.echarts.init(el); _charts.set(el, c) }
  return c
}

export function lineChart(el, data, xKey, yKey, color){
  if(!el || !window.echarts) return
  const c = _inst(el)
  c.setOption({
    backgroundColor:'transparent', grid:{left:54,right:18,top:18,bottom:30},
    tooltip:{trigger:'axis'},
    xAxis:{type:'category',data:data.map(d=>d[xKey]),axisLabel:{color:'#8b93ad'},axisLine:{lineStyle:{color:'#2a3350'}}},
    yAxis:{type:'value',scale:true,axisLabel:{color:'#8b93ad'},splitLine:{lineStyle:{color:'#222a42'}}},
    series:[{type:'line',showSymbol:false,smooth:true,data:data.map(d=>d[yKey]),
      lineStyle:{color:color||'#4f7cff',width:2},areaStyle:{color:'rgba(79,124,255,.12)'}}]
  })
  c.resize()
}

export function pieChart(el, entries){
  if(!el || !window.echarts) return
  const c = _inst(el)
  c.setOption({
    backgroundColor:'transparent',
    tooltip:{trigger:'item',formatter:'{b}: {d}%'},
    series:[{type:'pie',radius:['45%','72%'],
      data:entries.map(([k,v])=>({name:k,value:+(v*100).toFixed(1)})),
      label:{color:'#e6e9f2'}}]
  })
  c.resize()
}

window.addEventListener('resize', ()=>{
  document.querySelectorAll('.chart').forEach(el=>{ const c=_charts.get(el); if(c) c.resize() })
})
