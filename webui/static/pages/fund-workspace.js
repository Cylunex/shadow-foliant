import { reactive, computed, onMounted } from 'vue'
import { api, money, pct, fmt4 } from '../lib.js'

const post = (path, body, method='POST') => api('/api/fund/'+path, {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
const stateNames = {ready:'已确认',partial:'部分数据',pending_disclosure:'待披露',stale:'已超期',error:'数据异常',pending:'待确认',confirmed:'已确认',cancelled:'已撤销',failed:'失败'}
export default {
  props: {mode:{type:String,default:'overview'}},
  emits:['refresh'],
  template:`<section>
    <div v-if="s.error" class="err">{{s.error}}</div>
    <div v-if="s.message" class="ok-msg">{{s.message}}</div>
    <div v-if="mode==='overview'" class="card">
      <h3>收益确认进度 <button class="ghost" @click="reload">刷新</button></h3>
      <p v-if="s.workspace">{{s.workspace.overview.as_of}} · 已确认 {{s.workspace.overview.fresh_count}} / {{s.workspace.overview.total_count}} 只 · 已确认部分收益 {{money(s.workspace.overview.confirmed_daily_pnl)}}</p>
      <p v-if="s.workspace?.overview.investor_performance?.status==='ready'">已核验现金流收益 {{money(s.workspace.overview.investor_performance.net_profit)}} · 资金加权年化 {{pct(s.workspace.overview.investor_performance.annualized_irr)}}</p>
      <p class="sub">确认净值、盘中估值与成交净值分别记录。待披露基金不计入当日已确认收益。</p>
      <p v-if="pending.length">有 {{pending.length}} 笔定投待确认，请到“流水与计划”填写实际成交信息。</p>
    <details v-if="s.workspace?.weekly_reviews?.length"><summary>本周资料变化</summary><p v-for="r in s.workspace.weekly_reviews" :key="r.review_key">{{r.code}} · {{r.week}} · {{stateName(r.status)}} · {{r.changes.length}} 项变化</p></details>
    </div>
    <template v-if="mode==='plans'">
      <div class="card"><h3>待确认定投</h3>
        <table v-if="pending.length"><thead><tr><th>到期日</th><th>基金</th><th>金额</th><th>状态</th><th>操作</th></tr></thead>
        <tbody><tr v-for="r in pending" :key="r.occurrence_id"><td>{{r.scheduled_date}}</td><td>{{r.code}} {{r.name}}</td><td>{{money(r.amount)}}</td><td>{{stateName(r.status)}}</td>
          <td><button @click="fillOccurrence(r)">填写回执</button><button class="ghost" @click="cancel(r)">撤销待办</button></td></tr></tbody></table>
        <p v-else class="sub">暂无待确认事项。到期提醒不会直接增加真实持仓。</p>
      </div>
      <div class="card"><h3>成交导入预览</h3>
        <p class="sub">填写实际成交回执。支持 account、channel、external_id、申请/确认/到账日期、费用和份额；期初余额以前的记录需先核对迁移。</p>
        <textarea v-model="s.rowsText" rows="8" style="width:100%;box-sizing:border-box" aria-label="成交流水 JSON"></textarea>
        <button :disabled="s.busy" @click="preview">预览导入</button>
        <template v-if="s.preview">
          <p>新增 {{s.preview.imported}} 笔，跳过重复 {{s.preview.skipped}} 笔</p>
          <table><thead><tr><th>账户/渠道</th><th>基金</th><th>确认后份额</th><th>成本</th></tr></thead>
          <tbody><tr v-for="p in s.preview.positions" :key="p.account+p.channel+p.code"><td>{{p.account}} / {{p.channel}}</td><td>{{p.code}}</td><td>{{fmt4(p.shares)}}</td><td>{{money(p.cost)}}</td></tr></tbody></table>
          <button :disabled="s.busy" @click="commit">确认所预览的成交</button>
        </template>
      </div>
      <details class="card"><summary>核对期初余额与历史迁移</summary>
        <p class="sub">仅填写有凭证的期初份额、成本和日期；不会根据当前持仓倒造历史。</p>
        <textarea v-model="s.openingsText" rows="5" style="width:100%" aria-label="期初余额 JSON"></textarea>
        <button @click="migrationPreview">预览迁移</button>
        <template v-if="s.migration"><p>迁移将重建所列基金的持仓投影，保留历史记录与迁移前余额。</p><pre style="white-space:pre-wrap">{{JSON.stringify(s.migration.result.positions,null,2)}}</pre><button @click="migrationCommit">确认所预览的期初余额</button></template>
      </details>
    </template>
    <div v-if="mode==='experiments'" class="card"><h3>已保存的定投实验</h3>
      <p class="sub">每次成功回测保存输入净值、权益事件、预算和费用规则。重放使用保存数据。</p>
      <table v-if="experiments.length"><thead><tr><th>基金</th><th>策略</th><th>区间</th><th>收益</th><th>回撤</th><th></th></tr></thead>
        <tbody><tr v-for="r in experiments" :key="r.experiment_id"><td>{{r.code}}</td><td>{{r.config.strategy}}</td><td>{{r.config.start||'全部'}}～{{r.config.end||'最新'}}</td><td>{{pct(r.result.profit_pct)}}</td><td>{{pct(r.result.max_drawdown)}}</td><td><button @click="replay(r)">重放</button></td></tr></tbody></table>
      <p v-else class="sub">暂无实验记录。</p>
      <p v-if="s.replay">重放结果：收益 {{money(s.replay.profit)}}，最大回撤 {{pct(s.replay.max_drawdown)}}</p>
    </div>
    <div v-if="mode==='research'" class="card"><h3>基金研究档案</h3>
      <div class="row"><input v-model="s.code" placeholder="六位基金代码" aria-label="研究基金代码"/><button :disabled="s.busy" @click="research(false)">查看档案</button><button class="ghost" :disabled="s.busy" @click="research(true)">更新披露资料</button></div>
      <template v-if="s.research"><h4>{{s.research.profile.name}} · {{s.research.profile.ftype}}</h4>
      <p>{{s.research.score.advice}}</p><p class="sub">资料首次采集：{{s.research.profile.retrieved_at||'未知'}}；披露时间：{{s.research.profile.published_at||'来源未提供'}}。当前观察不能当作历史已知证据。</p>
      <ul><li v-for="r in s.research.artifact.invalidation_conditions" :key="r">复核条件：{{r}}</li></ul>
      <p v-for="c in s.research.profile.changes||[]" :key="c.field">{{c.field}}：{{c.before||'未知'}} → {{c.after}}</p></template>
      <details><summary>补充已核实的产品分类与交易限制</summary><textarea v-model="s.profileText" rows="4" style="width:100%" aria-label="产品字段 JSON"></textarea><button @click="saveProfile">保存已核实字段</button></details>
    </div>
    <div v-if="mode==='portfolio'" class="card"><h3>目标配置与调仓预览</h3>
      <p class="sub">按基金代码填写目标占比（%），优先使用新增现金。费率或持有期未知的调整会列出原因。</p>
      <div class="row"><div><label>可用现金</label><input type="number" min="0" v-model.number="s.cash"/></div></div>
      <table><thead><tr><th>基金代码</th><th>目标占比 %</th><th>申购费 %</th><th>赎回费 %</th><th>已核验事项</th><th></th></tr></thead>
        <tbody><tr v-for="(r,i) in s.allocations" :key="i"><td><input v-model="r.code" placeholder="六位代码" style="width:110px"/></td>
          <td><input type="number" min="0" max="100" v-model.number="r.weight" style="width:90px"/></td>
          <td><input type="number" min="0" step="0.01" v-model.number="r.purchase_fee" style="width:85px"/></td>
          <td><input type="number" min="0" step="0.01" v-model.number="r.redemption_fee" style="width:85px"/></td>
          <td><label><input type="checkbox" v-model="r.status_verified"/>申赎开放</label><label><input type="checkbox" v-model="r.holding_verified"/>赎回持有期</label></td>
          <td><button class="ghost" @click="s.allocations.splice(i,1)">移除</button></td></tr></tbody></table>
      <button class="ghost" @click="s.allocations.push({code:'',weight:null,purchase_fee:null,redemption_fee:null,status_verified:false,holding_verified:false})">添加基金</button>
      <label><input type="checkbox" v-model="s.redemptions"/>包含赎回预览</label>
      <button @click="rebalance(false)">预览</button><button class="ghost" @click="rebalance(true)">保存配置并预览</button>
      <table v-if="s.rebalance"><thead><tr><th>基金</th><th>动作</th><th>目标金额差</th><th>现金预算</th><th>预计费用</th><th>限制</th></tr></thead><tbody>
        <tr v-for="r in s.rebalance.orders" :key="r.code"><td>{{r.code}}</td><td>{{r.action==='subscribe'?'申购':'赎回'}}</td><td>{{money(r.gross_amount)}}</td><td>{{money(r.budgeted_amount)}}</td><td>{{money(r.budgeted_fee??r.fee)}}</td><td>{{r.blockers.join('；')||'可供人工核对'}}</td></tr></tbody></table>
      <p v-if="s.rebalance&&s.rebalance.reason">{{s.rebalance.reason}}</p>
    </div>
  </section>`,
  setup(props,{emit}){
    const s=reactive({workspace:null,initialized:false,error:'',message:'',busy:false,rowsText:'[]',preview:null,previewRows:null,
      openingsText:'[]',migration:null,migrationRows:null,code:'',research:null,profileText:'{}',
      cash:0,targetsText:'{}',feesText:'{}',allocations:[{code:'',weight:null,purchase_fee:null,redemption_fee:null,status_verified:false,holding_verified:false}],redemptions:false,rebalance:null,replay:null})
    const pending=computed(()=>s.workspace?.overview.pending||[])
    const experiments=computed(()=>s.workspace?.experiments||[])
    async function action(fn){s.busy=true;s.error='';s.message='';try{return await fn()}catch(e){s.error=String(e)}finally{s.busy=false}}
    async function reload(){return action(async()=>{s.workspace=await api('/api/fund/workspace');if(!s.initialized&&s.workspace.targets?.length){const t=s.workspace.targets[0];s.targetsText=JSON.stringify(t.targets,null,2);s.cash=t.available_cash;s.feesText=JSON.stringify(t.fee_rules,null,2);s.redemptions=t.allow_redemptions;s.allocations=Object.entries(t.targets).map(([code,w])=>({code,weight:w*100,purchase_fee:t.fee_rules?.[code]?.purchase_rate==null?null:t.fee_rules[code].purchase_rate*100,redemption_fee:t.fee_rules?.[code]?.redemption_rate==null?null:t.fee_rules[code].redemption_rate*100,status_verified:t.fee_rules?.[code]?.subscription_status==='open',holding_verified:!!t.fee_rules?.[code]?.holding_period_verified}))}s.initialized=true})}
    async function preview(){return action(async()=>{const rows=JSON.parse(s.rowsText);s.preview=await post('ledger/preview',{rows});s.previewRows=JSON.stringify(rows)})}
    async function commit(){return action(async()=>{if(JSON.stringify(JSON.parse(s.rowsText))!==s.previewRows)throw Error('流水已修改，请重新预览');await post('ledger/commit',{rows:JSON.parse(s.previewRows),expected_version:s.preview.version});s.preview=null;s.message='成交已确认';s.workspace=await api('/api/fund/workspace');emit('refresh')})}
    function fillOccurrence(r){s.rowsText=JSON.stringify([{code:r.code,account:r.account,channel:r.channel,txn_type:'定投',amount:r.amount,nav:null,fee:0,confirmation_date:new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Shanghai'}),occurrence_id:r.occurrence_id}],null,2);s.preview=null}
    async function cancel(r){await action(async()=>{await post('occurrences/'+r.occurrence_id+'/status',{status:'cancelled'});s.workspace=await api('/api/fund/workspace')})}
    async function migrationPreview(){await action(async()=>{const rows=JSON.parse(s.openingsText);s.migration=await post('migration/preview',{rows});s.migrationRows=JSON.stringify(rows)})}
    async function migrationCommit(){await action(async()=>{if(JSON.stringify(JSON.parse(s.openingsText))!==s.migrationRows)throw Error('期初余额已修改，请重新预览');await post('migration/commit',{rows:JSON.parse(s.migrationRows),expected_version:s.migration.version});s.migration=null;s.message='期初余额已确认';emit('refresh')})}
    async function replay(r){await action(async()=>{s.replay=await post('experiments/'+r.experiment_id+'/replay',{})})}
    async function research(refresh){await action(async()=>{s.research=await api('/api/fund/research/'+encodeURIComponent(s.code)+'?refresh='+refresh)})}
    async function saveProfile(){await action(async()=>{await post('profiles/'+s.code,{fields:JSON.parse(s.profileText)},'PUT');s.message='产品字段已保存，来源标记为人工核实'})}
    async function rebalance(save){await action(async()=>{
      const targets={},fee_rules={}
      for(const r of s.allocations){
        if(!r.code)continue
        if(r.weight==null||r.weight==='')throw Error('请填写目标占比')
        if(targets[r.code]!=null)throw Error('同一基金请合并成一行目标配置')
        targets[r.code]=Number(r.weight)/100
        const fees={holding_period_verified:!!r.holding_verified}
        if(r.purchase_fee!=null&&r.purchase_fee!=='')fees.purchase_rate=Number(r.purchase_fee)/100
        if(r.redemption_fee!=null&&r.redemption_fee!=='')fees.redemption_rate=Number(r.redemption_fee)/100
        if(r.status_verified){fees.subscription_status='open';fees.redemption_status='open'}
        fee_rules[r.code]=fees
      }
      s.rebalance=await post(save?'targets':'rebalance-preview',{targets,available_cash:s.cash,fee_rules,allow_redemptions:s.redemptions},save?'PUT':'POST')
    })}

    onMounted(reload)
    return {s,pending,experiments,money,pct,fmt4,JSON,stateName:v=>stateNames[v]||v,reload,preview,commit,fillOccurrence,cancel,migrationPreview,migrationCommit,replay,research,saveProfile,rebalance}
  }
}
