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
    const p = reactive({ stocks:null, stress:null, curve:null, overview:null, xray:null, perf:null, bench:null, mc:null, opt:null, optBusy:false, err:'', busy:false, snapMsg:'', daily:null })
    const alloc = ref(), curve = ref(), pnlchart = ref()
    const { sortBy, arrow, sorted: sortedStocks } = useSort(()=> p.stocks, 'mv', -1)
    // 今日股票盈亏(盘中实时):Σ 持仓股 数量×(现价-昨收)
    const todayStock = computed(()=> (p.stocks||[]).reduce((a,s)=> a + (Number(s.qty)||0)*(Number(s.today_change)||0), 0))

    async function load(){
      p.err=''; p.busy=true
      // 各请求独立并行:慢的(stress/xray 走外部行情)不阻塞其他,到达即渲染
      api('/api/portfolio/stocks').then(v=>p.stocks=v).catch(e=>p.err=''+e)
      api('/api/portfolio/stress').then(v=>p.stress=v).catch(()=>{})
      api('/api/portfolio/xray').then(v=>p.xray=v).catch(()=>{})
      api('/api/portfolio/performance').then(v=>p.perf=v).catch(()=>{})
      api('/api/portfolio/benchmark').then(v=>p.bench=v).catch(()=>{})
      api('/api/portfolio/montecarlo').then(v=>p.mc=v).catch(()=>{})
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
             snapshot, optimize, load, fmt, pct, money, cls }
  }
}
