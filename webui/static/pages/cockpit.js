import { reactive, computed, onMounted, onUnmounted } from 'vue'
import { api, cls, fmt } from '../lib.js'

const STATUS_CN = {
  success:'正常', ready:'就绪', degraded:'降级', partial:'部分成功', stale:'已过期',
  error:'失败', failed:'失败', missing:'缺失', running:'运行中', queued:'排队中',
  skipped:'已跳过', interrupted:'已中断', timeout:'超时',
}
const FINAL_BAD = new Set(['error','failed','timeout','interrupted'])

export default {
  template: `
  <div>
    <section class="command-hero">
      <div>
        <span class="badge info">AGENT CONTROL CENTER</span>
        <h1 style="margin-top:10px">项目总览</h1>
        <p>运行状态、今日选股、任务依赖和策略部署集集中在一屏；页面只读取现有产物。</p>
      </div>
      <div class="hero-status">
        <span class="status-dot" :class="s.health?.ready?'ready':'offline'"></span>
        <div>
          <strong>{{s.health?.ready?'生产服务就绪':'服务状态未知'}}</strong>
          <small>{{revision}} · {{s.health?.checks?.database?.backend||'数据库未知'}}</small>
        </div>
        <button class="ghost" :disabled="s.loading" @click="load">{{s.loading?'同步中…':'刷新'}}</button>
      </div>
    </section>

    <div v-if="s.err" class="err" style="margin-top:14px">{{s.err}}</div>
    <div class="stat-grid" style="margin:16px 0">
      <div class="stat-card"><div class="stat-label">持仓标的</div><div class="stat-value">{{d.holding_count??'—'}}</div><div class="stat-note">当前组合</div></div>
      <div class="stat-card"><div class="stat-label">最终优选</div><div class="stat-value">{{finalRows.length}}</div><div class="stat-note">{{selectionDate||'尚无快照'}}</div></div>
      <div class="stat-card"><div class="stat-label">活跃推荐</div><div class="stat-value">{{d.active_recommendation_count??'—'}}</div><div class="stat-note">真实盈亏跟踪中</div></div>
      <div class="stat-card"><div class="stat-label">活跃信号</div><div class="stat-value">{{d.active_signal_count??'—'}}</div><div class="stat-note">结构化操作主张</div></div>
      <div class="stat-card"><div class="stat-label">后台任务</div><div class="stat-value">{{d.tasks?.total??s.jobs.length}}</div><div class="stat-note">异常 {{d.tasks?.failed_recent?.length||0}} · 运行 {{runningJobs.length}}</div></div>
      <div class="stat-card"><div class="stat-label">在线策略</div><div class="stat-value">{{d.strategy_deployment?.base_total??'—'}}</div><div class="stat-note">进化 {{d.strategy_deployment?.evolved_base??0}} · 组合 {{d.strategy_deployment?.composed?.length||0}}</div></div>
    </div>

    <div v-if="warnings.length" class="notice">
      <b>需要留意</b>
      <span v-for="(w,i) in warnings" :key="i" style="margin-left:12px">{{w}}</span>
    </div>

    <section class="card">
      <div class="section-head">
        <div><h2>最终优选 TOP5</h2><p>在完整 TOP15 内按本地确定性指标独立复排；红蓝、问财和妙想只作复核</p></div>
        <span class="badge" :class="selectionStatus==='success'?'success':'warning'">{{selectionDate||'等待选股'}}</span>
      </div>
      <div v-if="finalRows.length" class="pick-grid">
        <article v-for="(r,i) in finalRows" :key="r.code" class="pick-card" @click="openStock(r.code)" style="cursor:pointer">
          <span class="pick-rank">{{i+1}}</span>
          <div class="pick-code">{{r.code}}</div>
          <div class="pick-name">{{r.name||'未命名'}}</div>
          <div class="pick-score">{{fmt(r.final_score)}}<small>优选分</small></div>
          <div class="pick-tags">
            <span class="badge" :class="debateClass(r.debate_verdict)">{{r.debate_verdict||'规则优选'}}</span>
            <span class="pill">{{laneText(r.assigned_lane)}}</span>
          </div>
          <div class="pick-reason">{{r.final_reason||'等待结构化优选依据'}}</div>
        </article>
      </div>
      <div v-else class="empty-state"><div><b>暂无最终优选</b><br>下一次综合选股完成后自动生成。</div></div>
    </section>

    <div class="dashboard-grid">
      <section class="card">
        <div class="section-head">
          <div><h2>综合选股 TOP15</h2><p>唯一正式候选集，用于盘中复核、盘后扫描和后验评估</p></div>
          <button class="ghost" @click="go('screen')">进入选股中心</button>
        </div>
        <div v-if="topRows.length" class="table-wrap">
          <table><thead><tr><th>#</th><th>代码 / 名称</th><th>赛道分</th><th>现价</th><th>涨跌</th><th>红蓝参考</th><th>正式赛道</th></tr></thead>
          <tbody><tr v-for="r in topRows" :key="r.code" @click="openStock(r.code)" style="cursor:pointer">
            <td>{{r.rank}}</td><td><b>{{r.code}}</b><span style="margin-left:7px;color:var(--muted)">{{r.name}}</span></td>
            <td>{{fmt(r.score)}}</td><td>{{r.price??'—'}}</td><td :class="cls(r.change_pct)">{{signed(r.change_pct)}}%</td>
            <td><span class="badge" :class="debateClass(r.debate_verdict)">{{r.debate_verdict||'—'}}</span></td>
            <td style="text-align:left">{{laneText(r.assigned_lane)}} · {{r.primary_strategy_name||'本地PIT'}}</td>
          </tr></tbody></table>
        </div>
        <div v-else class="empty-state">暂无 TOP15 结构化产物。</div>
      </section>

      <div class="dashboard-stack">
        <section class="card">
          <div class="section-head"><div><h2>组合动作</h2><p>高仓位模式下缺少判断即保守关闭自动买入</p></div></div>
          <template v-if="d.portfolio_policy">
            <div class="list-item"><span class="status-dot" :class="d.portfolio_policy.fail_closed?'offline':'ready'"></span><div class="list-body"><div class="list-title">{{policyTitle}}</div><div class="list-meta">{{policyReason}}</div></div></div>
            <div class="list-item"><div class="list-body"><div class="list-title">今日判断</div><div class="list-meta">{{d.portfolio_policy.market_action_cn||d.portfolio_policy.action_cn||d.portfolio_policy.market_action||'等待 10:05 组合研判'}}</div></div></div>
          </template>
          <div v-else class="empty-state">尚无组合策略状态。</div>
        </section>
        <section class="card">
          <div class="section-head"><div><h2>能力状态</h2><p>只显示是否配置，不回显任何密钥</p></div></div>
          <div class="list-item"><span class="status-dot" :class="s.health?.features?.llm_configured?'ready':'offline'"></span><div class="list-body"><div class="list-title">LLM 路由</div><div class="list-meta">{{s.health?.features?.llm_configured?'已配置':'未配置'}}</div></div></div>
          <div class="list-item"><span class="status-dot" :class="s.health?.features?.notification_configured?'ready':'offline'"></span><div class="list-body"><div class="list-title">通知通道</div><div class="list-meta">{{s.health?.features?.notification_configured?'已配置':'未配置'}}</div></div></div>
          <div class="list-item"><span class="status-dot" :class="s.health?.features?.rag_enabled?'running':'ready'"></span><div class="list-body"><div class="list-title">RAG</div><div class="list-meta">{{s.health?.features?.rag_enabled?'实验性开启':'按项目策略关闭'}}</div></div></div>
        </section>
      </div>
    </div>

    <div class="dashboard-grid" style="margin-top:16px">
      <section class="card">
        <div class="section-head"><div><h2>任务依赖链</h2><p>借鉴参考项目运行流：等待依赖的任务不占 worker，上游失败则下游明确跳过</p></div><button class="ghost" @click="go('settings')">管理任务</button></div>
        <div v-if="dependencyRows.length" class="flow-map">
          <div v-for="row in dependencyRows" :key="row.key" class="flow-row">
            <div class="flow-node"><span class="status-dot" :class="runDot(row.parent)"></span><b>{{row.parent.cn||row.parent.name}}</b><small>{{statusText(effectiveRun(row.parent)?.status)}}</small></div>
            <div class="flow-arrow">→</div>
            <div class="flow-node"><span class="status-dot" :class="runDot(row.child)"></span><b>{{row.child.cn||row.child.name}}</b><small>{{statusText(effectiveRun(row.child)?.status)}}</small></div>
          </div>
        </div>
        <div v-else class="empty-state">任务依赖信息加载中。</div>
      </section>
      <div class="dashboard-stack">
        <section class="card">
          <div class="section-head"><div><h2>当前运行</h2><p>手动队列与定时任务</p></div></div>
          <div v-for="r in runningJobs" :key="r.run_id||r.name" class="list-item"><span class="status-dot running"></span><div class="list-body"><div class="list-title">{{r.cn||r.task_name||r.name}}</div><div class="list-meta">{{statusText(r.status)}} · {{r.run_id?r.run_id.slice(0,8):'定时任务'}}</div></div></div>
          <div v-if="!runningJobs.length" class="empty-state" style="min-height:70px">当前没有运行中的任务。</div>
        </section>
        <section class="card">
          <div class="section-head"><div><h2>最近异常</h2><p>仅显示最近一次异常状态</p></div></div>
          <div v-for="r in d.tasks?.failed_recent||[]" :key="r.name" class="list-item"><span class="status-dot offline"></span><div class="list-body"><div class="list-title">{{r.name}} · {{statusText(r.status)}}</div><div class="list-meta">{{r.error||'无错误详情'}}</div></div></div>
          <div v-if="!d.tasks?.failed_recent?.length" class="empty-state" style="min-height:70px">最近任务没有异常。</div>
        </section>
      </div>
    </div>

    <div class="row stretch" style="margin-top:16px">
      <section class="card flex1"><div class="section-head"><div><h2>活跃推荐</h2><p>进入真实盈亏跟踪的候选</p></div></div><div v-for="r in d.active_recommendations||[]" :key="r.id" class="list-item"><div class="list-body"><div class="list-title">{{r.symbol}} {{r.name}} <span class="badge" :class="ratingClass(r.rating)">{{r.rating||'—'}}</span></div><div class="list-meta">{{r.source||'未知来源'}}</div></div></div><div v-if="!d.active_recommendations?.length" class="empty-state">暂无活跃推荐。</div></section>
      <section class="card flex1"><div class="section-head"><div><h2>活跃决策信号</h2><p>当前仍有效的结构化动作</p></div></div><div v-for="r in d.active_signals||[]" :key="r.id" class="list-item"><div class="list-body"><div class="list-title">{{r.code}} {{r.name}} <span class="badge" :class="actionClass(r.action)">{{r.action_cn||r.action}}</span></div><div class="list-meta">{{r.source_type||'未知来源'}}</div></div></div><div v-if="!d.active_signals?.length" class="empty-state">暂无活跃信号。</div></section>
    </div>
  </div>`,
  setup(){
    const s=reactive({res:null,health:null,jobs:[],loading:false,err:'',loadedAt:''})
    const d=computed(()=>s.res?.data||{})
    const finalRows=computed(()=>d.value.selection?.data?.final_rows||[])
    const topRows=computed(()=>d.value.selection?.data?.rows||[])
    const selectionDate=computed(()=>d.value.selection?.meta?.snapshot_date||'')
    const selectionStatus=computed(()=>d.value.selection?.status||'missing')
    const revision=computed(()=>s.health?.revision?s.health.revision.slice(0,12):'版本未知')
    const warnings=computed(()=>[...new Set(s.res?.meta?.warnings||[])].slice(0,5))
    const policyTitle=computed(()=>d.value.portfolio_policy?.fail_closed?'保守买入门已生效':'组合交易门正常')
    const policyReason=computed(()=>d.value.portfolio_policy?.reason||d.value.portfolio_policy?.message||d.value.portfolio_policy?.mode||'等待今日组合判断')
    const effectiveRun=j=>{
      const m=j?.manual_run, r=j?.last_run
      if(m&&['queued','running'].includes(m.status)) return m
      const mt=m&&(m.finished_at||m.started_at||m.requested_at), rt=r&&(r.finished_at||r.started_at)
      return mt&&(!rt||String(mt)>=String(rt))?m:r
    }
    const dependencyRows=computed(()=>{
      const map=Object.fromEntries(s.jobs.map(j=>[j.name,j])), rows=[]
      s.jobs.forEach(child=>(child.depends_on||[]).forEach(name=>rows.push({key:name+'>'+child.name,parent:map[name]||{name,cn:name},child})))
      return rows.slice(0,8)
    })
    const runningJobs=computed(()=>{
      const out=[...(d.value.tasks?.running_manual||[])]
      s.jobs.forEach(j=>{const r=effectiveRun(j);if(r&&['queued','running'].includes(r.status)&&!out.some(x=>x.run_id&&x.run_id===r.run_id))out.push({...r,cn:j.cn,name:j.name})})
      return out.slice(0,6)
    })
    const statusText=v=>STATUS_CN[v]||v||'暂无记录'
    const runDot=j=>{const x=effectiveRun(j)?.status;return ['queued','running'].includes(x)?'running':FINAL_BAD.has(x)?'offline':'ready'}
    const debateClass=v=>String(v||'').includes('否决')?'danger':String(v||'').includes('谨慎')?'warning':'success'
    const laneText=v=>({core:'PIT核心',satellite:'本地策略',timing:'技术基因组'}[v]||v||'本地')
    const actionClass=v=>['buy','add'].includes(String(v||'').toLowerCase())?'market-buy':['sell','reduce','avoid'].includes(String(v||'').toLowerCase())?'market-sell':'info'
    const ratingClass=v=>String(v||'').includes('买')?'market-buy':String(v||'').includes('卖')?'market-sell':'info'
    const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${Number(v).toFixed(2)}`
    function go(hash){location.hash=hash}
    function openStock(code){if(code){try{sessionStorage.setItem('sf-stock-code',code)}catch(e){};location.hash='stock'}}
    async function load(){
      s.loading=true;s.err=''
      try{
        const [res,health,jobs]=await Promise.all([api('/api/agent/cockpit?compact=false'),api('/api/health'),api('/api/jobs')])
        s.res=res;s.health=health;s.jobs=jobs||[];s.loadedAt=new Date().toLocaleTimeString('zh-CN',{hour12:false})
      }catch(e){s.err='总览加载失败：'+e}finally{s.loading=false}
    }
    let timer=null
    onMounted(()=>{load();timer=setInterval(()=>{if(!document.hidden)load()},60000)})
    onUnmounted(()=>{if(timer)clearInterval(timer)})
    return {s,d,finalRows,topRows,selectionDate,selectionStatus,revision,warnings,policyTitle,policyReason,
      effectiveRun,dependencyRows,runningJobs,statusText,runDot,debateClass,laneText,actionClass,ratingClass,signed,go,openStock,load,cls,fmt}
  }
}
