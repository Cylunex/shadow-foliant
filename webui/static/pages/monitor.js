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
    <div v-if="m.msg" class="ok-msg">{{m.msg}}</div>
    <div class="card">
      <h3>新增 / 更新监测</h3>
      <div class="row">
        <div><label>代码</label><input v-model="form.code" placeholder="600519" style="width:105px"/></div>
        <div><label>名称</label><input v-model="form.name" placeholder="可选" style="width:120px"/></div>
        <div><label>进场低</label><input v-model.number="form.entry_low" type="number" step="0.01" style="width:105px"/></div>
        <div><label>进场高</label><input v-model.number="form.entry_high" type="number" step="0.01" style="width:105px"/></div>
        <div><label>止盈</label><input v-model.number="form.take_profit" type="number" step="0.01" style="width:105px"/></div>
        <div><label>止损</label><input v-model.number="form.stop_loss" type="number" step="0.01" style="width:105px"/></div>
        <div><label>检查间隔(分钟)</label><input v-model.number="form.check_interval" type="number" min="10" style="width:95px"/></div>
        <label style="display:flex;align-items:center;gap:5px"><input v-model="form.notification_enabled" type="checkbox"/>通知</label>
        <label style="display:flex;align-items:center;gap:5px"><input v-model="form.trading_hours_only" type="checkbox"/>仅交易时段</label>
        <button :disabled="m.saving" @click="save">{{m.saving?'保存中…':'保存监测'}}</button>
      </div>
    </div>
    <div class="card">
      <h3>监测列表({{(m.stocks||[]).length}})</h3>
      <table v-if="m.stocks&&m.stocks.length"><thead><tr><th v-for="c in SCOLS" :key="c.k" @click="sortS(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrowS(c.k)}}</th><th>操作</th></tr></thead>
        <tbody><tr v-for="(r,i) in sortedS" :key="i"><td v-for="c in SCOLS">{{fmtCell(r,c)}}</td>
          <td><button class="ghost" style="padding:2px 8px" @click="fill(r)">编辑</button>
              <span class="link-del" style="margin-left:8px" @click="remove(r)">删除</span></td>
        </tr></tbody></table>
      <div v-else class="loading">暂无监测股。</div>
    </div>
    <div class="card">
      <h3>最近通知({{(m.notifs||[]).length}})</h3>
      <table v-if="m.notifs&&m.notifs.length"><thead><tr><th v-for="c in NCOLS" :key="c.k" @click="sortN(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrowN(c.k)}}</th></tr></thead>
        <tbody><tr v-for="(r,i) in sortedN" :key="i"><td v-for="c in NCOLS">{{fmtCell(r,c)}}</td></tr></tbody></table>
      <div v-else class="loading">暂无通知。</div>
    </div>
  </div>`,
  setup(){
    const m = reactive({ stocks:[], notifs:[], err:'', msg:'', saving:false })
    const form = reactive({code:'',name:'',rating:'持有',entry_low:null,entry_high:null,
      take_profit:null,stop_loss:null,check_interval:60,notification_enabled:true,trading_hours_only:true})
    const { sortBy:sortS, arrow:arrowS, sorted:sortedS } = useSort(()=> m.stocks, '', 1)
    const { sortBy:sortN, arrow:arrowN, sorted:sortedN } = useSort(()=> m.notifs, '', 1)

    function fmtCell(r, c){
      let v = r[c.k]
      if(v == null) return '—'
      // entry_range 格式化
      if(c.k==='entry_range' && v && typeof v==='object') return (v.min??v.low??'?')+' ~ '+(v.max??v.high??'?')
      // 时间格式化
      if(c.k==='triggered_at' && typeof v==='string') return v.slice(0,19).replace('T',' ')
      return typeof v==='object' ? JSON.stringify(v) : ''+v
    }

    async function load(){
      m.err=''
      try{ m.stocks = await api('/api/monitor/stocks') || [] }catch(e){ m.err=''+e }
      try{ m.notifs = await api('/api/monitor/notifications') || [] }catch(e){}
    }
    function fill(r){
      form.code=r.symbol;form.name=r.name||'';form.rating=r.rating||'持有'
      form.entry_low=r.entry_range?.min??r.entry_range?.low??null
      form.entry_high=r.entry_range?.max??r.entry_range?.high??null
      form.take_profit=r.take_profit??null;form.stop_loss=r.stop_loss??null
      form.check_interval=r.check_interval||60
      form.notification_enabled=r.notification_enabled!==false
      form.trading_hours_only=r.trading_hours_only!==false
    }
    async function save(){
      m.err='';m.msg=''
      if(!/^\d{6}$/.test(form.code||'')){m.err='请输入6位股票代码';return}
      if(form.entry_low==null||form.entry_low===''||form.entry_high==null||form.entry_high===''){m.err='请填写进场区间';return}
      m.saving=true
      try{
        const payload={...form,
          take_profit:form.take_profit===''?null:form.take_profit,
          stop_loss:form.stop_loss===''?null:form.stop_loss,
          dry_run:false}
        const r=await api('/api/monitor/stocks',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)})
        if(!r.ok) throw new Error(r.meta?.warnings?.join('；')||'保存失败')
        m.msg='监测条件已保存';await load()
      }catch(e){m.err=''+e}finally{m.saving=false}
    }
    async function remove(r){
      if(!confirm(`确认移除 ${r.symbol} ${r.name||''} 的监测？`)) return
      m.err='';m.msg=''
      try{
        const res=await api('/api/monitor/stocks/'+r.symbol,{method:'DELETE'})
        if(!res.ok||!res.data?.removed) throw new Error(res.meta?.warnings?.join('；')||'删除失败')
        m.msg='已移除监测';await load()
      }catch(e){m.err=''+e}
    }
    onMounted(load)
    return { m, form, SCOLS, NCOLS, sortedS, sortS, arrowS, sortedN, sortN, arrowN,
      fmtCell, fill, save, remove }
  }
}
