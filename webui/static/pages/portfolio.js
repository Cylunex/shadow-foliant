import { reactive, ref, computed, nextTick, onMounted } from 'vue'
import { api, fmt, pct, money, cls, lineChart, pieChart, useSort } from '../lib.js'

const STOCK_COLS = [
  { k: 'code', t: '代码' }, { k: 'name', t: '名称' }, { k: 'qty', t: '数量' },
  { k: 'cost', t: '成本' }, { k: 'price', t: '现价' }, { k: 'mv', t: '市值' },
  { k: 'pnl_pct', t: '浮盈' },
  { k: 'today_change_pct', t: '今日%' }, { k: 'today_change', t: '今日收益' },
]

export default {
  template: `
  <div>
    <div class="h1">📊 持仓总览</div>
    <p class="sub">今日盈亏 / 大类资产 / 情景压力测试 / 组合净值曲线 / 股票持仓。</p>

    <!-- ===== 今日盈亏头条 ===== -->
    <div class="card" style="display:flex;gap:28px;flex-wrap:wrap;align-items:center">
      <div>
        <div class="sub">今日股票盈亏 · 盘中实时</div>
        <div :class="cls(todayStock)" style="font-size:26px;font-weight:700">{{money(todayStock)}}</div>
      </div>
      <template v-if="p.daily && p.daily.summary && p.daily.summary.latest">
        <div style="border-left:1px solid var(--line,#2a2a2a);padding-left:28px">
          <div class="sub">收盘合并(股票+基金) · {{p.daily.summary.latest.snap_date}}</div>
          <div :class="cls(p.daily.summary.latest.total_daily_pnl)" style="font-size:22px;font-weight:700">
            {{money(p.daily.summary.latest.total_daily_pnl)}}
            <span style="font-size:14px">({{pct(p.daily.summary.latest.total_daily_pct/100)}})</span>
          </div>
        </div>
        <div>
          <div class="sub">本月累计</div>
          <div :class="cls(p.daily.summary.mtd_pnl)" style="font-size:18px;font-weight:600">{{money(p.daily.summary.mtd_pnl)}}</div>
        </div>
        <div>
          <div class="sub">近{{p.daily.summary.period_days}}日 · 胜率{{p.daily.summary.win_rate}}%</div>
          <div :class="cls(p.daily.summary.period_pnl)" style="font-size:18px;font-weight:600">{{money(p.daily.summary.period_pnl)}}</div>
        </div>
      </template>
      <div style="flex:1;min-width:220px">
        <div class="sub">近30日每日盈亏(收盘口径)</div>
        <div ref="pnlchart" class="chart" style="height:84px"></div>
      </div>
    </div>
    <div class="row" style="margin-bottom:12px">
      <button class="ghost" :disabled="p.busy" @click="load">↻ 刷新</button>
      <button :disabled="p.busy" @click="snapshot" title="按当前持仓+实时价落一条净值快照">📸 落净值快照</button>
      <button :disabled="p.optBusy" @click="optimize" title="按历史波动/协方差给当前持仓建议配比(风险平价/最小方差等)">⚖️ 建议配比</button>
      <span v-if="p.snapMsg" class="pill">{{p.snapMsg}}</span>
    </div>
    <div v-if="p.opt" class="card">
      <h3>⚖️ 建议配比 <span class="sub">{{p.opt.error ? p.opt.error : (p.opt.used.length+'只 · '+p.opt.cov_days+'日协方差')}}</span></h3>
      <table v-if="!p.opt.error && p.opt.weights" style="width:100%">
        <thead><tr><th>标的</th><th>风险平价</th><th>逆波动</th><th>最小方差</th><th>等权</th></tr></thead>
        <tbody><tr v-for="code in p.opt.used" :key="code">
          <td>{{(p.opt.names&&p.opt.names[code])||code}} {{code}}</td>
          <td>{{p.opt.weights.risk_parity&&p.opt.weights.risk_parity[code]}}%</td>
          <td>{{p.opt.weights.inverse_vol&&p.opt.weights.inverse_vol[code]}}%</td>
          <td>{{p.opt.weights.min_variance&&p.opt.weights.min_variance[code]}}%</td>
          <td>{{p.opt.weights.equal&&p.opt.weights.equal[code]}}%</td>
        </tr></tbody>
      </table>
      <div v-if="p.opt.portfolio_vol_pct" class="sub" style="margin-top:6px">组合年化波动:风险平价 {{p.opt.portfolio_vol_pct.risk_parity}}% · 最小方差 {{p.opt.portfolio_vol_pct.min_variance}}% · 等权 {{p.opt.portfolio_vol_pct.equal}}%</div>
    </div>
    <div v-if="p.err" class="err">{{p.err}}</div>
    <div class="row stretch">
      <div class="card flex1"><h3>大类资产配置</h3><div ref="alloc" class="chart" style="height:240px"></div></div>
      <div class="card flex1">
        <h3>情景压力测试(股票+基金)</h3>
        <table v-if="p.stress&&p.stress.length"><thead><tr><th>情景</th><th>组合损益</th></tr></thead>
          <tbody><tr v-for="x in p.stress" :key="x.scenario"><td>{{x.scenario}}</td><td :class="cls(x.pnl_pct)">{{pct(x.pnl_pct)}}</td></tr></tbody></table>
        <div v-else class="loading">无数据</div>
      </div>
    </div>
    <div class="card"><h3>组合净值曲线</h3><div ref="curve" class="chart" style="height:240px"></div>
      <div v-if="!p.curve||!p.curve.length" class="loading">暂无快照(盘后任务落点)。</div></div>
    <div class="card" v-if="p.perf && p.perf.n_snapshots">
      <h3>📈 组合绩效 <span style="color:var(--muted);font-weight:400;font-size:12px">(TWR时间加权 / XIRR资金加权年化)</span></h3>
      <div class="row" style="gap:24px;flex-wrap:wrap">
        <div v-if="p.perf.twr"><div class="sub">时间加权 TWR</div>
          <b :class="cls(p.perf.twr.twr_pct)" style="font-size:18px">{{p.perf.twr.twr_pct>0?'+':''}}{{p.perf.twr.twr_pct}}%</b>
          <span v-if="p.perf.twr.twr_annual_pct!=null" class="sub"> 年化{{p.perf.twr.twr_annual_pct}}%</span></div>
        <div v-if="p.perf.xirr_pct!=null"><div class="sub">资金加权年化 XIRR</div>
          <b :class="cls(p.perf.xirr_pct)" style="font-size:18px">{{p.perf.xirr_pct>0?'+':''}}{{p.perf.xirr_pct}}%</b></div>
        <div v-if="p.perf.risk"><div class="sub">最大回撤<span v-if="p.perf.risk.volatility_pct!=null"> / 年化波动</span></div>
          <b style="font-size:18px"><span class="green">{{p.perf.risk.max_drawdown_pct}}%</span><span v-if="p.perf.risk.volatility_pct!=null"> / {{p.perf.risk.volatility_pct}}%</span></b>
          <span v-if="p.perf.risk.sharpe!=null" class="sub"> 夏普{{p.perf.risk.sharpe}}</span></div>
      </div>
      <div v-if="p.perf.attribution && p.perf.attribution.total!=null" class="sub" style="margin-top:10px">
        盈亏归因:已实现 <b :class="cls(p.perf.attribution.realized)">{{money(p.perf.attribution.realized)}}</b>
        + 浮动 <b :class="cls(p.perf.attribution.unrealized)">{{money(p.perf.attribution.unrealized)}}</b>
        = <b :class="cls(p.perf.attribution.total)">{{money(p.perf.attribution.total)}}</b> 元
      </div>
      <div v-if="p.mc && !p.mc.error" class="sub" style="margin-top:6px">
        🎲 蒙特卡洛(未来{{p.mc.horizon}}日):中位 <b>{{money(p.mc.percentiles.p50)}}</b>({{p.mc.ret_p50_pct>0?'+':''}}{{p.mc.ret_p50_pct}}%)
        · 区间 [{{money(p.mc.percentiles.p5)}}~{{money(p.mc.percentiles.p95)}}]
        · 亏损概率 <b :class="p.mc.prob_loss_pct>50?'green':''">{{p.mc.prob_loss_pct}}%</b>
        · VaR95 <b class="green">{{p.mc.var95_pct}}%</b>
      </div>
      <div v-if="p.bench && p.bench.excess_pct!=null" class="sub" style="margin-top:6px">
        📊 vs {{p.bench.benchmark_name}}:组合 <b :class="cls(p.bench.portfolio_return_pct)">{{p.bench.portfolio_return_pct}}%</b>
        / 基准 <b :class="cls(p.bench.benchmark_return_pct)">{{p.bench.benchmark_return_pct}}%</b>
        → 超额 <b :class="cls(p.bench.excess_pct)">{{p.bench.excess_pct>0?'+':''}}{{p.bench.excess_pct}}%</b>
      </div>
    </div>
    <div class="card" v-if="p.xray">
      <h3>🩺 组合体检 X-Ray
        <span :style="{fontWeight:700,marginLeft:8,color:p.xray.score>=80?'var(--accent)':p.xray.score>=60?'var(--amber)':'var(--danger,#e5534b)'}">{{p.xray.score}}分</span>
        <span style="color:var(--muted);font-weight:400;font-size:12px;margin-left:8px">{{p.xray.summary}}</span>
      </h3>
      <table style="width:100%">
        <tbody>
          <tr v-for="r in p.xray.rules" :key="r.key" style="border-bottom:1px solid var(--bdr)">
            <td style="width:24px">{{r.severity==='alert'?'🔴':r.severity==='warn'?'🟡':'🟢'}}</td>
            <td style="width:110px"><b>{{r.name}}</b></td>
            <td style="color:var(--muted)">{{r.detail}}<span v-if="r.suggestion" style="color:var(--amber)"> → {{r.suggestion}}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <!-- ===== 交易习惯洞察 ===== -->
    <div class="card" v-if="p.insights">
      <h3>🧭 交易习惯洞察 <span style="color:var(--muted);font-weight:400;font-size:12px">近{{insSince}}天 · 纯记录,秒回</span></h3>
      <div class="row stretch">
        <div class="flex1">
          <div class="sub" style="margin-bottom:4px">持有时长分布 <span v-if="p.insights.duration">(平均 {{p.insights.duration.avg_days}} 天)</span></div>
          <template v-for="(n,k) in (p.insights.duration&&p.insights.duration.buckets)" :key="k">
            <div v-if="n>0" style="margin:3px 0">
              <div style="display:flex;justify-content:space-between;font-size:12px"><span>{{durLabel(k)}}</span><span>{{n}}只</span></div>
              <div style="height:7px;background:var(--panel2);border-radius:4px;overflow:hidden"><div :style="{width:durPct(n)+'%',height:'100%',background:'#4f7cff'}"></div></div>
            </div>
          </template>
        </div>
        <div class="flex1" v-if="p.insights.frequency">
          <div class="sub" style="margin-bottom:4px">交易频次</div>
          <table style="font-size:13px;width:100%"><tbody>
            <tr><td>近{{insSince}}天变动</td><td style="text-align:right"><b>{{p.insights.frequency.total_changes}}</b> 次</td></tr>
            <tr><td>买入 / 卖出</td><td style="text-align:right"><span class="red">{{p.insights.frequency.buys}}</span> / <span class="green">{{p.insights.frequency.sells}}</span></td></tr>
            <tr><td>买卖比</td><td style="text-align:right">{{p.insights.frequency.buy_sell_ratio}}</td></tr>
            <tr><td>日均变动</td><td style="text-align:right">{{p.insights.frequency.daily_avg_changes}} 次</td></tr>
          </tbody></table>
        </div>
        <div class="flex1" v-if="p.insights.timeline">
          <div class="sub" style="margin-bottom:4px">最活跃(变动最多)</div>
          <table style="font-size:13px;width:100%"><tbody>
            <tr v-for="s in (p.insights.timeline.most_active||[]).slice(0,6)" :key="s.code"><td>{{s.code}} {{s.name}}</td><td style="text-align:right">{{s.count}}次</td></tr>
          </tbody></table>
        </div>
      </div>
    </div>

    <!-- ===== 操作信号 ===== -->
    <div class="card">
      <h3>🎯 操作信号
        <button class="ghost" style="float:right" :disabled="p.sigBusy" @click="loadSignals">{{p.sigBusy?'扫描中…':'扫描加/减仓'}}</button>
        <span v-if="p.sig" style="color:var(--muted);font-weight:400;font-size:12px;margin-left:8px">加仓 {{p.sig.add_ok===false?'⏱超时':p.sig.add_count}} · 减仓 {{p.sig.reduce_ok===false?'⏱超时':p.sig.reduce_count}}</span>
      </h3>
      <div v-if="p.sigErr" class="err">{{p.sigErr}}</div>
      <template v-if="p.sig">
        <div v-if="p.sig.reduce&&p.sig.reduce.length" style="margin-top:6px">
          <b>💰 减仓信号 ({{p.sig.reduce.length}})</b>
          <div v-for="x in p.sig.reduce" :key="'r'+x.symbol" style="font-size:13px;padding:3px 0;border-bottom:1px solid var(--bdr)">
            {{x.symbol}} {{x.name}} <span :class="cls(x.profit_pct)">{{pct((x.profit_pct||0)/100)}}</span>
            <span v-for="(a,i) in x.actions" :key="i" :style="{color:sigColor(a.severity)}"> · {{a.recommendation}}</span>
          </div>
        </div>
        <div v-if="p.sig.add&&p.sig.add.length" style="margin-top:8px">
          <b>📈 加仓审核 ({{p.sig.add.length}})</b>
          <div v-for="x in p.sig.add" :key="'a'+x.symbol" style="font-size:13px;padding:3px 0;border-bottom:1px solid var(--bdr)">
            <span :style="{color:x.verdict==='approve'?'#3da35d':'#e0a030',fontWeight:700}">{{x.verdict==='approve'?'✅可加':'⚠️勿加'}}</span>
            {{x.symbol}} {{x.name}} <span :class="cls(x.holding_pnl_pct)">{{pct((x.holding_pnl_pct||0)/100)}}</span>
            <span style="color:var(--muted)"> · {{x.verdict==='approve'?'触发跌幅且质地审核通过':(x.reason_codes||[]).join('; ')}}</span>
          </div>
        </div>
        <div v-if="p.sig.add_ok===false" class="sub" style="margin-top:6px;color:var(--amber,#e0a030)">⏱ 加仓审核扫描超时(个别持仓行情源较慢),可重试。</div>
        <div v-if="p.sig.reduce_ok===false" class="sub" style="margin-top:6px;color:var(--amber,#e0a030)">⏱ 减仓信号扫描超时,可重试。</div>
        <div v-if="(p.sig.add_ok!==false&&!(p.sig.add||[]).length) && (p.sig.reduce_ok!==false&&!(p.sig.reduce||[]).length)" class="sub" style="margin-top:6px">当前无加/减仓触发 🟢</div>
      </template>
      <div v-else-if="!p.sigBusy" class="loading">点"扫描加/减仓"查当前操作信号(跌幅触发的加仓审核 + 阶梯止盈/破位减仓)。仅提示不下单。</div>
    </div>

    <!-- ===== 持仓分级 ===== -->
    <div class="card">
      <h3>🚦 持仓分级
        <button class="ghost" style="float:right" :disabled="p.clsBusy" @click="classify">{{p.clsBusy?'分级中…(逐只基本面+形态,稍候)':'运行分级'}}</button>
        <span v-if="p.cls&&p.cls.counts" style="color:var(--muted);font-weight:400;font-size:12px;margin-left:8px">🟢{{p.cls.counts.healthy}} 🟡{{p.cls.counts.watch}} 🔴{{p.cls.counts.alert}} ⚪{{p.cls.counts.na}}</span>
      </h3>
      <div v-if="p.clsErr" class="err">{{p.clsErr}}</div>
      <template v-if="p.cls&&p.cls.by_class">
        <div v-for="grp in ['alert','watch']" :key="grp">
          <div v-if="p.cls.by_class[grp]&&p.cls.by_class[grp].length" style="margin-top:6px">
            <b>{{grp==='alert'?'🔴 警报':'🟡 观察'}} ({{p.cls.by_class[grp].length}})</b>
            <div v-for="x in p.cls.by_class[grp]" :key="x.symbol" style="font-size:13px;padding:2px 0;border-bottom:1px solid var(--bdr)">
              {{x.symbol}} {{x.name}} <span :class="cls(x.holding_pnl_pct)">{{pct((x.holding_pnl_pct||0)/100)}}</span>
              <span style="color:var(--muted)">· <span v-if="x.fundamental_grade&&x.fundamental_grade!=='N/A'">{{x.fundamental_grade}} · </span>{{(x.reasons||[]).join('; ')}}</span>
            </div>
          </div>
        </div>
        <div v-if="!(p.cls.by_class.alert||[]).length && !(p.cls.by_class.watch||[]).length" class="sub" style="margin-top:6px">无警报/观察持仓 🎉(健康 {{p.cls.counts.healthy}} 只)</div>
      </template>
      <div v-else-if="!p.clsBusy" class="loading">点"运行分级"扫描前15大持仓(持仓盈亏+趋势MA+看跌反转形态,结果缓存30分钟)。</div>
    </div>

    <!-- ===== AI 持仓诊断 ===== -->
    <div class="card">
      <h3>🧠 AI 持仓诊断
        <button class="ghost" style="float:right" :disabled="p.diagBusy" @click="aiDiag">{{p.diagBusy?'诊断中…':'AI诊断'}}</button>
        <span v-if="diagD&&diagD.risk_score" style="color:var(--muted);font-weight:400;font-size:12px;margin-left:8px">风险 {{diagD.risk_score}}/10 · 纪律 {{diagD.discipline_score}}/10</span>
      </h3>
      <div v-if="p.diagErr" class="err">{{p.diagErr}}</div>
      <template v-if="p.diag">
        <div v-if="diagD&&diagD.summary" style="margin-top:4px"><b>{{diagD.summary}}</b></div>
        <div v-if="diagD&&diagD.problems&&diagD.problems.length" style="margin-top:8px">
          <b>⚠️ 问题</b><ul style="margin:4px 0;padding-left:20px;line-height:1.8"><li v-for="(x,i) in diagD.problems" :key="i">{{x}}</li></ul>
        </div>
        <div v-if="diagD&&diagD.suggestions&&diagD.suggestions.length" style="margin-top:4px">
          <b>💡 建议</b><ul style="margin:4px 0;padding-left:20px;line-height:1.8"><li v-for="(x,i) in diagD.suggestions" :key="i">{{x}}</li></ul>
        </div>
        <div v-if="diagD&&(diagD.error||diagD.raw_text)" class="sub" style="margin-top:6px;color:var(--amber,#e0a030)">
          ⚠️ AI 暂不可用(未配置 LLM 或调用失败),以下为规则数据:总盈亏 {{money((p.diag.context&&p.diag.context.overview||{}).total_pnl)}}、近90天变动 {{(p.diag.context&&p.diag.context.trading_frequency||{}).total_changes}} 次。
        </div>
      </template>
      <div v-else-if="!p.diagBusy" class="loading">点"AI诊断":大模型基于你的 估值/交易频次/持有时长 给习惯诊断+改进建议(需 LLM key;无 key 也返回规则数据,不阻塞)。</div>
    </div>

    <div class="card">
      <h3>股票持仓 <span style="color:var(--muted);font-weight:400;font-size:12px">(点表头排序)</span></h3>
      <table v-if="p.stocks&&p.stocks.length">
        <thead><tr>
          <th v-for="c in scols" :key="c.k" @click="sortBy(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrow(c.k)}}</th>
        </tr></thead>
        <tbody><tr v-for="x in sortedStocks" :key="x.code"><td>{{x.code}}</td><td>{{x.name}}</td><td>{{x.qty}}</td><td>{{fmt(x.cost)}}</td><td>{{fmt(x.price)}}</td><td>{{money(x.mv)}}</td><td :class="cls(x.pnl_pct)">{{pct(x.pnl_pct)}}</td><td :class="cls(x.today_change_pct)">{{pct(x.today_change_pct)}}</td><td :class="cls(x.today_change)">{{x.today_change!=null ? (x.today_change>0?'+':'')+fmt(x.today_change) : '—'}}</td></tr></tbody>
      </table>
      <div v-else class="loading">无股票持仓</div>
    </div>
  </div>`,
  setup(){
    const p = reactive({ stocks:null, stress:null, curve:null, overview:null, xray:null, perf:null, bench:null, mc:null, opt:null, optBusy:false, err:'', busy:false, snapMsg:'', daily:null, insights:null, cls:null, clsBusy:false, clsErr:'', sig:null, sigBusy:false, sigErr:'', diag:null, diagBusy:false, diagErr:'' })
    const alloc = ref(), curve = ref(), pnlchart = ref()
    const insSince = 90
    const DUR_LABELS = { '<7d':'7天内', '7-30d':'1周-1月', '30-90d':'1-3月', '90-180d':'3-6月', '>180d':'半年以上', 'unknown':'未知' }
    const durLabel = k => DUR_LABELS[k] || k
    const durPct = n => { const b=(p.insights&&p.insights.duration&&p.insights.duration.buckets)||{}; const mx=Math.max(1,...Object.values(b)); return Math.round(n/mx*100) }
    async function classify(){
      p.clsBusy=true; p.clsErr=''
      try{ p.cls = await api('/api/portfolio/classify') }
      catch(e){ p.clsErr=''+e }finally{ p.clsBusy=false }
    }
    const sigColor = sev => ({critical:'#e0533d', warning:'#e0a030', info:'#3da35d'}[sev] || 'var(--muted)')
    async function loadSignals(){
      p.sigBusy=true; p.sigErr=''
      try{ p.sig = await api('/api/portfolio/signals') }
      catch(e){ p.sigErr=''+e }finally{ p.sigBusy=false }
    }
    const diagD = computed(()=> (p.diag&&p.diag.diagnosis)||null)
    async function aiDiag(){
      p.diagBusy=true; p.diagErr=''
      try{ p.diag = await api('/api/portfolio/diagnose-ai') }
      catch(e){ p.diagErr=''+e }finally{ p.diagBusy=false }
    }
    const { sortBy, arrow, sorted: sortedStocks } = useSort(()=> p.stocks, 'mv', -1)
    // 今日股票盈亏(盘中实时):Σ 持仓股 数量×(现价-昨收)
    const todayStock = computed(()=> (p.stocks||[]).reduce((a,s)=> a + (Number(s.today_change)||0), 0))   // today_change 已是持仓口径(×数量)

    async function load(){
      p.err=''; p.busy=true
      // 各请求独立并行:慢的(stress/xray 走外部行情)不阻塞其他,到达即渲染
      api('/api/portfolio/stocks').then(v=>p.stocks=v).catch(e=>p.err=''+e)
      api('/api/portfolio/stress').then(v=>p.stress=v).catch(()=>{})
      api('/api/portfolio/xray').then(v=>p.xray=v).catch(()=>{})
      api('/api/portfolio/performance').then(v=>p.perf=v).catch(()=>{})
      api('/api/portfolio/benchmark').then(v=>p.bench=v).catch(()=>{})
      api('/api/portfolio/montecarlo').then(v=>p.mc=v).catch(()=>{})
      api('/api/portfolio/insights').then(v=>p.insights=v).catch(()=>{})
      api('/api/portfolio/curve').then(async v=>{
        p.curve=v; await nextTick()
        if(v&&v.length) lineChart(curve.value, v.map(s=>({date:s.snap_date,mv:s.total_mv})),'date','mv','#4f7cff')
      }).catch(()=>{})
      api('/api/portfolio/overview').then(async v=>{
        p.overview=v; await nextTick()
        if(v&&v.allocation) pieChart(alloc.value, Object.entries(v.allocation))
      }).catch(()=>{})
      api('/api/portfolio/daily-pnl?days=30').then(async v=>{
        p.daily=v; await nextTick()
        if(v&&v.series&&v.series.length) lineChart(pnlchart.value, v.series.map(s=>({date:s.snap_date,pnl:s.total_daily_pnl})),'date','pnl','#4f7cff')
      }).catch(()=>{})
      p.busy=false
    }

    async function snapshot(){
      p.busy=true; p.snapMsg=''
      try{
        const r = await api('/api/portfolio/snapshot',{method:'POST'})
        p.snapMsg = r.saved ? ('已落快照 '+r.snap_date+' · 市值 '+money(r.total_mv)) : (r.msg||'无持仓')
        if(r.saved){ try{ p.curve = await api('/api/portfolio/curve') }catch(e){}
          await nextTick()
          if(p.curve && p.curve.length) lineChart(curve.value, p.curve.map(s=>({date:s.snap_date,mv:s.total_mv})),'date','mv','#4f7cff') }
      }catch(e){ p.snapMsg=''+e }finally{ p.busy=false }
    }
    async function optimize(){
      p.optBusy=true; p.opt=null
      try{ p.opt = await api('/api/portfolio/optimize') }
      catch(e){ p.opt={error:''+e, used:[]} }
      finally{ p.optBusy=false }
    }
    onMounted(load)
    return { p, alloc, curve, pnlchart, todayStock, scols:STOCK_COLS, sortedStocks, sortBy, arrow,
             snapshot, optimize, load, fmt, pct, money, cls,
             insSince, durLabel, durPct, classify, sigColor, loadSignals,
             diagD, aiDiag }
  }
}
