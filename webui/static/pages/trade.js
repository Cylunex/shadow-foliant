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
    <p class="sub">股票买卖成交记录与持仓变动日志。录入前先预览，确认后才更新成交与持仓。</p>
    <div class="card" style="margin-bottom:14px">
      <h3>录入成交</h3>
      <div class="row" style="gap:8px;flex-wrap:wrap;align-items:end">
        <div><label>代码</label><input v-model.trim="entry.code" maxlength="6" placeholder="600519" style="width:105px"/></div>
        <div><label>名称</label><input v-model.trim="entry.name" placeholder="可选" style="width:120px"/></div>
        <div><label>方向</label><select v-model="entry.trade_type" style="width:86px"><option>买入</option><option>卖出</option></select></div>
        <div><label>成交价</label><input type="number" min="0" step="0.0001" v-model.number="entry.price" style="width:110px"/></div>
        <div><label>数量</label><input type="number" min="1" step="1" v-model.number="entry.quantity" style="width:100px"/></div>
        <div><label>成交时间</label><input type="datetime-local" v-model="entry.trade_time" style="width:190px"/></div>
        <div><label>佣金</label><input type="number" min="0" step="0.01" v-model.number="entry.commission" style="width:90px"/></div>
        <div><label>印花税</label><input type="number" min="0" step="0.01" v-model.number="entry.tax" style="width:90px"/></div>
        <div><label>备注</label><input v-model.trim="entry.note" maxlength="200" style="width:150px"/></div>
      </div>
      <label style="display:flex;gap:6px;align-items:center;margin:10px 0">
        <input type="checkbox" v-model="entry.update_position" style="width:auto"/> 同步更新股票持仓
      </label>
      <details style="margin:8px 0">
        <summary class="sub" style="cursor:pointer">批量粘贴 Markdown 成交表（填写后优先使用表格）</summary>
        <textarea v-model="entry.table" rows="5" style="width:100%;margin-top:8px" placeholder="| 成交时间 | 股票代码 | 股票名称 | 成交价 | 成交量 | 交易类型 |"></textarea>
      </details>
      <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
        <button :disabled="entry.busy" @click="previewEntry">{{entry.busy?'校验中…':'预览成交'}}</button>
        <span v-if="entry.message" :class="entry.ok?'pill':'err'">{{entry.message}}</span>
      </div>
      <div v-if="entry.preview" style="margin-top:12px">
        <div class="sub" style="margin-bottom:6px">批次 {{entry.preview.batch_id}} · 将录入 {{entry.preview.prepared}} 笔；请核对代码、方向、价格、数量和持仓更新选项。</div>
        <table v-if="entry.preview.rows&&entry.preview.rows.length">
          <thead><tr><th>时间</th><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>数量</th><th>金额</th></tr></thead>
          <tbody><tr v-for="(x,i) in entry.preview.rows" :key="i">
            <td>{{x.trade_time||'当前时间'}}</td><td :class="x.trade_type==='买入'?'red':'green'">{{x.trade_type}}</td>
            <td>{{x.code}}</td><td>{{x.name}}</td><td>{{fmt(x.price)}}</td><td>{{x.quantity}}</td><td>{{money(x.amount)}}</td>
          </tr></tbody>
        </table>
        <table v-if="entry.preview.effects&&entry.preview.effects.length" style="margin-top:8px">
          <thead><tr><th>代码</th><th>持仓前</th><th>持仓后</th><th>费用</th><th>现金净影响</th><th>模式</th></tr></thead>
          <tbody><tr v-for="x in entry.preview.effects" :key="'effect-'+x.row">
            <td>{{x.code}}</td><td>{{x.position_before}}</td><td>{{x.position_after}}</td>
            <td>{{money(x.fees)}}</td><td :class="cls(x.net_cash_effect)">{{money(x.net_cash_effect)}}</td>
            <td>{{x.position_effect==='record_only'?'仅记历史':'更新持仓'}}</td>
          </tr></tbody>
        </table>
        <div v-if="entry.preview.warnings&&entry.preview.warnings.length" class="sub" style="color:var(--amber);margin-top:6px">{{entry.preview.warnings.join('；')}}</div>
        <button style="margin-top:10px" :disabled="entry.busy" @click="confirmEntry">确认录入并{{entry.update_position?'更新持仓':'仅记流水'}}</button>
      </div>
    </div>
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
        <td :class="x.trade_type==='买入'?'red':'green'">{{x.trade_type}}</td>
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
    const entry = reactive({
      code:'', name:'', trade_type:'买入', price:null, quantity:null, trade_time:'',
      commission:0, tax:0, note:'', update_position:true, table:'', busy:false,
      preview:null, idempotency_key:'', message:'', ok:false,
    })

    function requestBody(){
      if(entry.table.trim()) return { table:entry.table, rows:null, update_position:entry.update_position }
      return { rows:[{
        code:entry.code, name:entry.name, trade_type:entry.trade_type,
        price:entry.price, quantity:entry.quantity, trade_time:entry.trade_time||null,
        commission:entry.commission||0, tax:entry.tax||0, note:entry.note||null,
      }], table:'', update_position:entry.update_position }
    }

    async function previewEntry(){
      entry.busy=true; entry.preview=null; entry.message=''; entry.ok=false
      try{
        const result = await api('/api/portfolio/trade-records/preview',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(requestBody())
        })
        if(result.errors&&result.errors.length) throw new Error(result.errors.join('；'))
        entry.preview=result
        entry.idempotency_key=(globalThis.crypto&&crypto.randomUUID)?crypto.randomUUID():('trade-'+Date.now()+'-'+Math.random().toString(16).slice(2))
        entry.ok=true; entry.message='预览已生成，尚未写入'
      }catch(e){ entry.message=''+e }
      entry.busy=false
    }

    async function confirmEntry(){
      if(!entry.preview) return
      entry.busy=true; entry.message=''; entry.ok=false
      try{
        const body={...requestBody(),preview_hash:entry.preview.preview_hash,confirmed:true}
        const result=await api('/api/portfolio/trade-records',{
          method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':entry.idempotency_key},body:JSON.stringify(body)
        })
        entry.ok=true
        entry.message=`已录入 ${result.imported||0} 笔，更新持仓 ${result.positions_updated||0} 笔`
        entry.preview=null; entry.table=''; entry.price=null; entry.quantity=null; entry.note=''; entry.commission=0; entry.tax=0
        await load()
      }catch(e){ entry.message=''+e }
      entry.busy=false
    }

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
    return { f, rows, busy, err, cols:COLS, sorted, sortBy, arrow, totalPnl, realized, behavior,
      entry, previewEntry, confirmEntry, load, fmt, money, cls }
  }
}
