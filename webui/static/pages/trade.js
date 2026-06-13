import { reactive, ref, onMounted } from 'vue'
import { api, fmt, money, cls, useSort } from '../lib.js'

const COLS = [
  { k: 'trade_time', t: '时间' }, { k: 'trade_type', t: '方向' },
  { k: 'stock_code', t: '代码' }, { k: 'stock_name', t: '名称' },
  { k: 'price', t: '价格' }, { k: 'quantity', t: '数量' },
  { k: 'amount', t: '金额' }, { k: 'commission', t: '佣金' },
  { k: 'profit_loss', t: '盈亏' }, { k: 'source', t: '来源' },
]

export default {
  template: `
  <div>
    <div class="h1">📋 成交记录</div>
    <p class="sub">股票买卖成交记录与持仓变动日志。支持筛选与排序。</p>
    <div class="row" style="gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center">
      <input v-model="f.code" placeholder="代码/逗号分隔" style="width:140px"/>
      <select v-model="f.ttype" style="width:90px">
        <option value="">全部方向</option>
        <option value="买入">买入</option>
        <option value="卖出">卖出</option>
      </select>
      <select v-model.number="f.days" style="width:90px">
        <option :value="7">7天</option>
        <option :value="30">30天</option>
        <option :value="90">90天</option>
        <option :value="365">1年</option>
        <option :value="0">全部</option>
      </select>
      <button class="ghost" @click="load" :disabled="busy">🔍 查询</button>
      <span v-if="rows" class="pill">共 {{rows.length}} 条</span>
      <span v-if="totalPnl" class="pill">合计盈亏 {{totalPnl}}</span>
    </div>
    <div v-if="realized" class="card" style="margin-bottom:12px;padding:10px 14px">
      <b>💰 已实现盈亏(累计)</b>
      <span :class="cls(realized.total)" style="margin-left:8px;font-weight:600">
        {{realized.total>0?'+':''}}{{money(realized.total)}}</span>
      <span class="sub" style="margin-left:12px">
        {{realized.count}}笔 · 胜率{{realized.win_rate}}%<template v-if="realized.profit_factor"> · 盈亏比{{realized.profit_factor}}</template>
      </span>
    </div>
    <div v-if="behavior && !behavior.error" class="card" style="margin-bottom:12px">
      <h3>🪞 交易行为诊断
        <span :style="{fontWeight:700,marginLeft:8,color:behavior.score>=80?'var(--accent)':behavior.score>=60?'var(--amber)':'var(--danger,#e5534b)'}">{{behavior.score}}分</span>
        <span class="sub" style="margin-left:8px">{{behavior.summary}}</span>
      </h3>
      <div class="sub" style="margin-bottom:6px">
        {{behavior.n_trips}}回合 胜率{{behavior.win_rate}}% 盈亏比{{behavior.profit_factor}} ·
        盈利单 {{behavior.avg_win_pct}}%/{{behavior.avg_win_hold}}天 亏损单 {{behavior.avg_loss_pct}}%/{{behavior.avg_loss_hold}}天
      </div>
      <table style="width:100%"><tbody>
        <tr v-for="r in behavior.rules" :key="r.key" style="border-bottom:1px solid var(--bdr)">
          <td style="width:24px">{{r.severity==='alert'?'🔴':r.severity==='warn'?'🟡':'🟢'}}</td>
          <td style="width:96px"><b>{{r.name}}</b></td>
          <td style="color:var(--muted)">{{r.detail}}<span v-if="r.suggestion" style="color:var(--amber)"> → {{r.suggestion}}</span></td>
        </tr>
      </tbody></table>
    </div>
    <div v-if="err" class="err">{{err}}</div>
    <table v-if="rows&&rows.length">
      <thead><tr>
        <th v-for="c in cols" :key="c.k" @click="sortBy(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrow(c.k)}}</th>
      </tr></thead>
      <tbody><tr v-for="x in sorted" :key="x.id">
        <td>{{(x.trade_time||'').slice(0,16)}}</td>
        <td :class="x.trade_type==='买入'?'green':'red'">{{x.trade_type}}</td>
        <td>{{x.stock_code}}</td><td>{{x.stock_name}}</td>
        <td>{{fmt(x.price)}}</td><td>{{x.quantity}}</td>
        <td>{{money(x.amount)}}</td><td>{{fmt(x.commission)}}</td>
        <td :class="cls(x.profit_loss)">{{x.profit_loss!=null?(x.profit_loss>0?'+':'')+fmt(x.profit_loss):'—'}}</td>
        <td>{{x.source||''}}</td>
      </tr></tbody>
    </table>
    <div v-else class="loading">暂无成交记录</div>
  </div>`,
  setup(){
    const f = reactive({ code:'', ttype:'', days:30 })
    const rows = ref(null)
    const busy = ref(false)
    const err = ref('')
    const { sortBy, arrow, sorted } = useSort(()=> rows.value, 'trade_time', -1)
    const totalPnl = ref('')
    const realized = ref(null)
    const behavior = ref(null)

    async function load(){
      busy.value=true; err.value=''
      const p = []
      if(f.code) p.push('code='+encodeURIComponent(f.code))
      if(f.ttype) p.push('ttype='+encodeURIComponent(f.ttype))
      if(f.days>0) p.push('days='+f.days)
      try{ rows.value = await api('/api/portfolio/trade-records?'+p.join('&')) || [] }
      catch(e){ err.value=''+e; rows.value=[] }
      busy.value=false
      let sum=0
      if(rows.value) for(const x of rows.value) if(x.profit_loss!=null) sum+=x.profit_loss
      totalPnl.value = sum ? (sum>0?'+':'')+money(sum) : ''
      // 已实现盈亏汇总(顺带触发后端回填 profit_loss 列)
      try{ const r = await api('/api/trades/realized'); if(r && r.count) realized.value = r }
      catch(e){ /* 静默 */ }
      // 交易行为诊断(影子账户)
      try{ const b = await api('/api/trades/behavior'); if(b && !b.error) behavior.value = b }
      catch(e){ /* 静默 */ }
    }

    onMounted(load)
    return { f, rows, busy, err, cols:COLS, sorted, sortBy, arrow, totalPnl, realized, behavior, load, fmt, money, cls }
  }
}
