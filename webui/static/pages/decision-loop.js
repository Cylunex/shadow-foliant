import { reactive, onMounted } from 'vue'
import { api, cls, fmt } from '../lib.js'

export default {
  template: `
  <section class="card" aria-label="研究决策闭环">
    <div class="section-head"><div><h2>研究 → 决策 → 验证</h2><p>选股名单、可成交模型与真实账户分开看；未成熟不代表零收益。</p></div>
      <button class="ghost" :disabled="s.loading" @click="load">刷新证据</button></div>
    <div v-if="s.error" class="notice">{{s.error}}</div>
    <div class="stat-grid">
      <article v-for="book in s.data?.books||[]" :key="book.kind" class="stat-card">
        <div class="stat-label">{{bookName(book.kind)}}</div><div class="stat-note">{{book.description}}</div>
      </article>
    </div>
    <div v-if="s.data?.capsule" class="list-item"><div class="list-body">
      <div class="list-title">冻结上下文 {{s.data.capsule.capsule_id.slice(0,15)}}…</div>
      <div class="list-meta">行情日期 {{s.data.capsule.market_as_of}} · 发布 {{s.data.capsule.published_at}}<br>
        最早执行 {{s.data.capsule.earliest_execution_at||'等待交易日确认'}} · {{s.data.capsule.recording_mode==='contemporaneous'?'当时记录':'历史补算'}}</div>
      <div v-if="s.previous && s.previous!==s.data.capsule.capsule_id" class="notice">正式上下文已变化，旧行动预览需要重新计算。</div>
    </div></div>
    <div class="table-wrap" v-if="s.data?.model_orders?.length"><table>
      <thead><tr><th>对照组</th><th>执行状态</th><th>数量</th></tr></thead>
      <tbody><tr v-for="r in s.data.model_orders" :key="r.baseline+r.state"><td>{{baseline(r.baseline)}}</td><td>{{status(r.state)}}</td><td>{{r.count}}</td></tr></tbody>
    </table></div>
    <div v-else class="empty-state">模型账本会从新版本发布后的正式选股开始累计。</div>
    <div class="table-wrap" v-if="s.data?.model_books?.length"><table>
      <thead><tr><th>连续模型账户</th><th>净收益</th><th>净值回撤</th><th>数据口径</th></tr></thead>
      <tbody><tr v-for="r in s.data.model_books" :key="r.baseline"><td>{{baseline(r.baseline)}}</td>
        <td :class="cls(r.latest?.net_return_pct)">{{percent(r.latest?.net_return_pct)}}</td>
        <td :class="cls(r.latest?.nav_max_drawdown_pct)">{{percent(r.latest?.nav_max_drawdown_pct)}}</td>
        <td>{{r.latest?.status==='verified'?'已核实':r.latest?'参考净值：公司行为或行情待核实':'等待首次执行'}}</td></tr></tbody>
    </table></div>
    <div class="table-wrap" v-if="s.data?.experiments?.length"><table>
      <thead><tr><th>假设</th><th>状态</th><th>有效样本</th><th>净超额下界</th></tr></thead>
      <tbody><tr v-for="r in s.data.experiments" :key="r.trial_id"><td>{{hypothesis(r.hypothesis_id)}}</td><td>{{status(r.state)}}</td>
        <td>{{r.evidence?.effective_samples??'—'}}</td><td :class="cls(r.evidence?.conservative_lower_bound_pct)">{{percent(r.evidence?.conservative_lower_bound_pct)}}</td></tr></tbody>
    </table></div>
    <details v-if="s.data?.context_diff && Object.keys(s.data.context_diff).length" style="margin-top:12px">
      <summary>本次上下文变化</summary><p v-for="(change,key) in s.data.context_diff" :key="key">{{key}}：{{change.before||'未知'}} → {{change.after||'未知'}}</p>
    </details>
    <details style="margin-top:12px"><summary>政策时间线与研究预算</summary>
      <p>{{s.data?.research_budget?'本周已预留一次委员会调用，输出预算 '+s.data.research_budget.max_output_tokens+' tokens':'本周尚无委员会预算记录'}}</p>
      <p v-for="(p,i) in s.data?.policy_timeline||[]" :key="i">{{p.created_at}} · {{status(p.status)}} · {{p.reason}}</p>
      <p v-if="!s.data?.policy_timeline?.length">暂无政策调整；沿用当前已发布策略。</p>
    </details>
    <details style="margin-top:16px"><summary>个股研究档案与待复核问题</summary>
      <p>论点不会改变正式排名；没有论点也不阻止止损或成交导入。</p>
      <article v-for="r in s.data?.research_cases?.cases||[]" :key="r.symbol" class="list-item"><div class="list-body">
        <div class="list-title">{{r.name}} · {{r.symbol}}</div><div v-for="q in r.questions" :key="q" class="list-meta">{{q}}</div>
      </div></article>
      <p v-for="e in s.data?.research_cases?.attention_top5||[]" :key="e.object_id">{{e.review==='urgent'?'⚠️ 优先复核':'待核实'}} {{e.symbol}} · {{e.title||e.event_id}}</p>
      <p v-if="!s.data?.research_cases?.cases?.length">等待下一次正式选股生成档案。</p>
    </details>
    <details style="margin-top:16px"><summary>人工确认研究论点（私人）</summary>
      <button class="ghost" @click="loadPrivate">读取已有草稿与论点</button>
      <button v-for="d in s.privateCases?.drafts||[]" :key="d.object_id" class="ghost" @click="s.draft=d;s.thesisSymbol=d.object_id;s.thesisText=d.text">{{d.object_id}} 草稿 v{{d.revision}}</button>
      <label style="display:block;margin:10px 0">股票代码 <input v-model="s.thesisSymbol" maxlength="6" placeholder="六位代码"></label>
      <label style="display:block;margin:10px 0">论点草稿 <textarea v-model="s.thesisText" rows="5" style="display:block;width:100%;box-sizing:border-box;margin-top:6px" maxlength="8000" placeholder="未附证据的草稿会保留未核实标记"></textarea></label>
      <button class="ghost" @click="saveThesis">保存草稿</button>
      <button class="ghost" @click="lockThesis" :disabled="!s.draft">确认锁定当前草稿</button>
      <p>{{s.thesisMessage}}</p>
    </details>
    <details style="margin-top:16px"><summary>现金 / 费用 / 权益记录导入（可选）</summary>
      <p>先预览再确认，不替代成交导入。每条需要 external_id、date、kind、amount。金额为人民币；未提供不猜余额。</p>
      <textarea v-model="s.accountRows" rows="5" style="width:100%;box-sizing:border-box" aria-label="账户事实 JSON" placeholder='[{"external_id":"cash-1","date":"2026-09-04","kind":"cash_balance","amount":"5000"}]'></textarea>
      <button class="ghost" @click="previewFacts">预览记录</button><button class="ghost" @click="confirmFacts" :disabled="!s.accountPreview">确认导入当前预览</button>
      <p>{{s.accountMessage}}</p>
    </details>
    <details style="margin-top:16px"><summary>账户行动预览（私人主组合）</summary>
      <p>只计算预览，不下单。现金未知可留空；缺失资金、行情或可卖数量时会说明原因。</p>
      <label>已确认可用现金 <input v-model="s.cash" type="number" min="0" step="100" placeholder="未知" style="max-width:160px"></label>
      <label style="margin-left:12px"><input type="checkbox" v-model="s.allowAdd">允许考虑增持</label>
      <button class="ghost" @click="preview" :disabled="s.previewing">{{s.previewing?'计算中…':'计算预览'}}</button>
      <div v-if="s.plan" class="notice" style="margin-top:12px"><b>{{s.plan.summary||'暂无正式选股上下文'}}</b><p>{{(s.plan.blockers||[]).join('；')}}</p>
        <small>有效期至 {{s.plan.expires_at||'—'}}；持仓、行情变化后需重算。</small></div>
      <article v-for="a in s.plan?.alternatives||[]" :key="a.kind" class="list-item"><div class="list-body">
        <div class="list-title">{{action(a.kind)}} · {{a.feasible?'可考虑':'暂不具备条件'}}</div><div class="list-meta">{{a.reason}}</div>
        <div v-for="r in a.actions" :key="r.symbol+r.side" :class="r.side==='buy'?'market-buy':'market-sell'">{{r.symbol}} {{r.side==='buy'?'买入':'卖出'}} {{r.quantity}} 股</div>
      </div></article>
      <div class="table-wrap" v-if="s.plan?.stress_scenarios?.length"><table><thead><tr><th>固定压力情景</th><th>估算影响</th></tr></thead>
        <tbody><tr v-for="r in s.plan.stress_scenarios" :key="r.scenario"><td>{{r.scenario}}</td><td :class="cls(r.return_pct)">{{percent(r.return_pct)}}</td></tr></tbody></table>
        <small>假设情景，不是涨跌预测；未假定卖出一定能成交。</small></div>
    </details>
  </section>`,
  setup() {
    const s=reactive({data:null,error:'',loading:false,previous:null,cash:'',allowAdd:false,previewing:false,plan:null,thesisSymbol:'',thesisText:'',draft:null,thesisMessage:'',privateCases:null,accountRows:'[]',accountPreview:null,accountMessage:''})
    const status=v=>({pending:'等待执行数据',filled:'模拟成交',partially_filled:'部分成交',unfilled:'未成交',expired:'已过期',registered:'已登记',evaluated:'已评估',failed:'失败留档',duplicate:'重复实验',budget_exhausted:'预算耗尽',retired:'已退役',applied:'已安排生效',no_change:'保持不变',rejected:'已拒绝',rolled_back:'已回滚'}[v]||v)
    const baseline=v=>({fusion:'融合 TOP15',top5:'最终 TOP5',pit_only:'纯 PIT',without_satellite:'去本地五策略',without_timing:'去技术基因组',low_turnover:'低换手'}[v]||v)
    const bookName=v=>({signal:'信号价格后验',model:'模拟成交账本',account:'真实账户收益'}[v]||v)
    const hypothesis=v=>({earnings_quality:'盈利质量',capital_persistence:'资金持续性',trend_exhaustion:'趋势衰竭'}[v]||v)
    const percent=v=>v==null?'—':fmt(v)+'%'
    const action=v=>({hold:'不动',reduce:'减风险',add:'增持',replace:'替换'}[v]||v)
    async function load(){s.loading=true;s.error='';try{s.previous=s.data?.capsule?.capsule_id;s.data=await api('/api/research/decision-loop')}catch(e){s.error='闭环数据暂不可用：'+e}finally{s.loading=false}}
    async function preview(){s.previewing=true;s.error='';try{const q=new URLSearchParams({allow_add:String(s.allowAdd)});if(s.cash!=='')q.set('available_cash',s.cash);s.plan=await api('/api/portfolio/action-plan?'+q)}catch(e){s.error='账户预览不可用：'+e}finally{s.previewing=false}}
    onMounted(load)
    async function saveThesis(){try{s.draft=await api('/api/research/thesis/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:s.thesisSymbol,text:s.thesisText,claims:[],expected_revision:s.draft?.object_id===s.thesisSymbol?s.draft.revision:0})});s.thesisMessage='草稿已保存，尚未锁定'}catch(e){s.thesisMessage=String(e)}}
    async function lockThesis(){if(!s.draft)return;if(s.thesisText!==s.draft.text||s.thesisSymbol!==s.draft.object_id){s.thesisMessage='内容已变更，请先保存新草稿';return}try{await api('/api/research/thesis/lock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:s.draft.object_id,draft_revision:s.draft.revision,confirm:true})});s.thesisMessage='已锁定；证据不足仍会标为未核实'}catch(e){s.thesisMessage=String(e)}}
    async function loadPrivate(){try{s.privateCases=await api('/api/research/cases')}catch(e){s.thesisMessage=String(e)}}
    const post=(url,value)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})
    async function previewFacts(){try{s.accountPreview=await post('/api/portfolio/account-facts/preview',{rows:JSON.parse(s.accountRows)});s.accountMessage='预览 '+s.accountPreview.rows.length+' 条，尚未入账';s.accountPreview.input=s.accountRows}catch(e){s.accountMessage=String(e)}}
    async function confirmFacts(){if(!s.accountPreview)return;if(s.accountPreview.input!==s.accountRows){s.accountMessage='输入已变化，请重新预览';return}try{const r=await post('/api/portfolio/account-facts/confirm',{preview_id:s.accountPreview.object_id,confirm:true});s.accountMessage='已确认 '+r.count+' 条；未据此推算完整账户收益';s.accountPreview=null}catch(e){s.accountMessage=String(e)}}
    return {s,status,baseline,bookName,hypothesis,percent,action,load,preview,cls,fmt,saveThesis,lockThesis,loadPrivate,previewFacts,confirmFacts}
  }
}
