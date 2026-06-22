import { reactive, ref, computed } from 'vue'
import { api, fmt, zh, useSort, cls } from '../lib.js'

const CATEGORIES = ['短线突破','趋势跟踪','量价信号','稳健成长','反转捕捉','压缩爆发','多周期共振']
const STRATEGIES = [
  {id:'parking_apron', cn:'停机坪', cat:'短线突破'},
  {id:'high_tight_flag', cn:'高而窄旗形', cat:'短线突破'},
  {id:'breakthrough_platform', cn:'突破平台', cat:'短线突破'},
  {id:'turtle_trade', cn:'海龟交易', cat:'趋势跟踪'},
  {id:'keep_increasing', cn:'均线多头', cat:'趋势跟踪'},
  {id:'backtrace_ma250', cn:'回踩年线', cat:'趋势跟踪'},
  {id:'enter', cn:'放量上涨', cat:'量价信号'},
  {id:'climax_limitdown', cn:'放量跌停', cat:'量价信号'},
  {id:'low_backtrace_increase', cn:'无大幅回撤', cat:'稳健成长'},
  {id:'low_atr', cn:'低ATR成长', cat:'稳健成长'},
  {id:'rsi_oversold_bounce', cn:'RSI超卖反弹', cat:'反转捕捉'},
  {id:'bollinger_squeeze_breakout', cn:'布林收窄突破', cat:'压缩爆发'},
  {id:'weekly_trend_daily_signal', cn:'周线趋势+日线', cat:'多周期共振'},
]
const CN = Object.fromEntries(STRATEGIES.map(s=>[s.id, s.cn]))

export default {
  template: `
  <div>
    <div class="h1">📐 策略回测</div>
    <p class="sub">对持仓/自选跑 13 套策略历史回测，查看触发频率、胜率、平均收益。</p>

    <div class="card">
      <div class="row" style="flex-wrap:wrap;gap:12px">
        <div><label>股票代码</label><input v-model="s.code" placeholder="如 600519" style="width:100px"/></div>
        <div><label>持有天数</label><input type="number" v-model.number="s.hold" style="width:70px"/></div>
        <div><label>止损%</label><input type="number" v-model.number="s.stop" style="width:70px"/></div>
        <div><label>止盈%</label><input type="number" v-model.number="s.target" style="width:70px"/></div>
        <button :disabled="s.loading" @click="runAll">{{s.loading?'回测中…':'单股回测'}}</button>
      </div>
      <div style="margin-top:12px">
        <label style="margin-right:12px;font-weight:600">策略</label>
        <template v-for="cat in CATEGORIES" :key="cat">
          <span style="color:var(--muted);font-size:12px;margin-right:4px">{{cat}}</span>
          <label v-for="st in STRATEGIES.filter(x=>x.cat===cat)" :key="st.id" style="margin-right:10px;cursor:pointer;font-size:13px">
            <input type="checkbox" :value="st.id" v-model="s.strats" style="vertical-align:middle"/>{{st.cn}}
          </label>
          <span style="margin-right:8px"></span>
        </template>
        <button class="ghost" style="margin-left:8px;font-size:12px" @click="s.strats=STRATEGIES.map(x=>x.id)">全选</button>
        <button class="ghost" style="font-size:12px" @click="s.strats=['enter','keep_increasing','turtle_trade']">核心3策略</button>
      </div>
      <p class="sub" style="margin-top:8px">止损止盈为相对入场价的百分比，回测区间近 2 年，未含手续费/滑点。</p>
    </div>

    <div class="card" style="margin-top:12px">
      <div class="row" style="align-items:center;gap:12px">
        <h3 style="margin:0">📊 批量回测持仓</h3>
        <select v-model="s.batchN" style="width:100px"><option :value="5">TOP5</option><option :value="10">TOP10</option><option :value="20">TOP20</option></select>
        <button :disabled="s.batchLoading" @click="runBatch">{{s.batchLoading?'回测中…':'跑持仓回测'}}</button>
        <span style="color:var(--muted);font-size:12px">逐只逐策略跑，慢的话缩小策略数或股票数</span>
      </div>
    </div>

    <div v-if="s.err" class="err">{{s.err}}</div>

    <!-- 单股结果 -->
    <div v-if="s.res && s.res.length" class="card" style="margin-top:12px">
      <h3>{{s.code}} &nbsp;<span class="pill">{{s.res.length}} 策略</span></h3>
      <table style="width:100%;margin-top:8px">
        <tr style="color:var(--muted);font-size:12px"><th align=left>策略</th><th align=right>触发</th><th align=right>胜率</th><th align=right>均收益</th><th align=right>最大回撤</th><th align=right>最佳</th><th align=right>最差</th></tr>
        <tr v-for="r in s.res" :key="r.strategy" style="border-bottom:1px solid var(--bdr)">
          <td><b>{{CN[r.strategy]||r.strategy}}</b></td>
          <td align=right><span :style="{color:r.summary?.count>0?'inherit':'var(--muted)'}">{{r.summary?.count||0}}</span></td>
          <td align=right>{{r.summary?.count?fmtPct(r.summary?.win_rate):'--'}}</td>
          <td align=right :class="cls(r.summary?.avg_ret_pct)">{{r.summary?.count?fmtPct(r.summary?.avg_ret_pct):'--'}}</td>
          <td align=right>{{r.summary?.count?fmtPct(r.summary?.avg_max_dd_pct):'--'}}</td>
          <td align=right><span class="red">{{r.summary?.count?fmtPct(r.summary?.max_win_pct):'--'}}</span></td>
          <td align=right><span class="green">{{r.summary?.count?fmtPct(r.summary?.max_loss_pct):'--'}}</span></td>
        </tr>
      </table>
      <div v-if="s.res.every(r=>!r.summary?.count)" class="sub" style="margin-top:8px">所有策略近 2 年均无触发信号</div>
    </div>

    <!-- 批量结果 -->
    <div v-if="s.batchRes && s.batchRes.length" class="card" style="margin-top:12px">
      <h3>批量结果 ({{s.batchRes.length}} 只)</h3>
      <div v-for="r in s.batchRes" :key="r.symbol" style="padding:5px 0;border-bottom:1px solid var(--bdr)">
        <b>{{r.symbol}}</b> {{r.name||''}} &nbsp;
        <template v-for="(st,k) in r.results" :key="k">
          <span v-if="!st.error&&st.summary?.count" style="margin-right:12px;font-size:13px">
            {{CN[st.strategy]||st.strategy}}
            <span style="color:var(--muted)">{{st.summary.count}}次</span>
            {{fmtPct(st.summary?.win_rate)}}
            <span :class="cls(st.summary?.avg_ret_pct)">{{fmtPct(st.summary?.avg_ret_pct)}}</span>
          </span>
        </template>
        <span v-if="r.results.every(st=>!st.summary?.count||st.error)" style="color:var(--muted)">无有效信号</span>
      </div>
    </div>

    <!-- ════ 组合级回测(实盘口径) ════ -->
    <div class="card" style="margin-top:18px;border-left:3px solid var(--accent,#4a9)">
      <h3 style="margin:0 0 4px">🧮 组合级回测 <span class="pill">实盘口径</span></h3>
      <p class="sub" style="margin-top:0">一个现金账户·并发持仓上限·先卖后买·含佣金+印花税+滑点·无前视(次日开盘建仓)。单股回测各信号独立全仓→系统性高估;组合回测才是"这套策略实际能赚多少、回撤多大"。</p>
      <div class="row" style="flex-wrap:wrap;gap:12px;align-items:flex-end">
        <div><label>股票池</label><select v-model="p.universe" style="width:120px">
          <option value="holdings">我的持仓</option><option value="index">沪深300成分</option><option value="custom">自定义</option>
        </select></div>
        <div v-if="p.universe==='custom'"><label>代码(逗号分隔)</label><input v-model="p.codes" placeholder="600519,000858" style="width:200px"/></div>
        <div v-if="p.universe==='index'"><label>取数上限</label><input type="number" v-model.number="p.limit" style="width:70px"/></div>
        <div><label>策略</label>
          <select v-model="p.strategy" :disabled="p.useLive" style="width:130px">
            <option v-for="st in STRATEGIES" :key="st.id" :value="st.id">{{st.cn}}</option>
          </select></div>
        <div><label style="cursor:pointer"><input type="checkbox" v-model="p.useLive" style="vertical-align:middle"/>用进化最优集</label></div>
        <div><label>并发上限</label><input type="number" v-model.number="p.maxPos" style="width:60px"/></div>
        <div><label>持有天数</label><input type="number" v-model.number="p.hold" style="width:60px"/></div>
        <div><label>止损%</label><input type="number" v-model.number="p.stop" style="width:60px"/></div>
        <div><label>止盈%</label><input type="number" v-model.number="p.target" style="width:60px"/></div>
        <div><label>分配</label><select v-model="p.alloc" style="width:90px"><option value="equal">等权</option><option value="signal">信号加权</option></select></div>
        <div><label>初始资金</label><input type="number" v-model.number="p.cash" style="width:100px"/></div>
        <div><label style="cursor:pointer" title="附交易/β回归/市况/蒙特卡洛分层归因,判断业绩是真本事还是运气"><input type="checkbox" v-model="p.attribution" style="vertical-align:middle"/>分层归因</label></div>
        <button :disabled="p.loading" @click="runPortfolio">{{p.loading?'回测中…(逐股拉K线,稍候)':'跑组合回测'}}</button>
      </div>
      <p class="sub" style="margin-top:6px">区间默认近2年。注:行情主源单次约返回~1年日线,实际回测深度受数据源限制。</p>
    </div>

    <div v-if="p.err" class="err">{{p.err}}</div>

    <div v-if="p.res" class="card" style="margin-top:12px">
      <h3 style="margin:0 0 8px">组合回测结果
        <span class="pill">{{p.res.config?.stocks_count}}只 · {{p.res.config?.trigger_count}}信号 · {{p.res.config?.period}}</span></h3>
      <div class="grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:12px">
        <div v-for="m in metrics" :key="m.k" class="card" style="margin:0;padding:8px 10px;text-align:center">
          <div style="color:var(--muted);font-size:11px">{{m.k}}</div>
          <div :class="m.cls" style="font-size:18px;font-weight:700">{{m.v}}</div>
        </div>
      </div>
      <!-- 净值曲线: 策略 vs 沪深300 -->
      <svg v-if="curve.pts" :viewBox="'0 0 '+curve.W+' '+curve.H" style="width:100%;height:200px;background:var(--bg2,#111);border-radius:6px">
        <polyline :points="curve.base" fill="none" stroke="var(--bdr,#555)" stroke-width="1" stroke-dasharray="4 3"/>
        <polyline v-if="curve.bench" :points="curve.bench" fill="none" stroke="#e0a030" stroke-width="1.5"/>
        <polyline :points="curve.pts" fill="none" :stroke="curve.up?'#e05050':'#30b070'" stroke-width="2"/>
      </svg>
      <div style="font-size:12px;color:var(--muted);margin-top:4px">
        <span :style="{color:curve.up?'#e05050':'#30b070'}">━ 组合净值</span>
        <span v-if="curve.bench" style="color:#e0a030;margin-left:14px">━ 沪深300</span>
        <span style="color:var(--bdr,#888);margin-left:14px">┄ 本金</span>
      </div>
      <!-- 最近成交 -->
      <table v-if="p.res.trades?.length" style="width:100%;margin-top:12px;font-size:12px">
        <tr style="color:var(--muted)"><th align=left>买入</th><th align=left>卖出</th><th align=left>代码</th><th align=right>入场</th><th align=right>出场</th><th align=right>收益</th><th align=center>了结</th></tr>
        <tr v-for="(t,i) in p.res.trades.slice().reverse().slice(0,20)" :key="i" style="border-bottom:1px solid var(--bdr)">
          <td>{{t.entry_date}}</td><td>{{t.exit_date}}</td><td>{{t.symbol}} {{t.name||''}}</td>
          <td align=right>{{t.entry_price}}</td><td align=right>{{t.exit_price}}</td>
          <td align=right :class="cls(t.ret_pct)">{{fmtPct(t.ret_pct)}}</td>
          <td align=center>{{REASON[t.exit_reason]||t.exit_reason}}</td>
        </tr>
      </table>

      <!-- 分层归因 -->
      <div v-if="attr && attr.ok" style="margin-top:16px;border-top:1px solid var(--bdr);padding-top:12px">
        <h4 style="margin:0 0 6px">🔬 分层归因</h4>
        <div :class="attr.verdict && attr.verdict.indexOf('✅')>=0 ? 'green':'red'" style="font-weight:600;margin-bottom:10px">{{attr.verdict}}</div>
        <div class="row" style="gap:24px;flex-wrap:wrap;align-items:flex-start">
          <div v-if="attr.beta_regression" style="min-width:240px">
            <div class="sub" style="margin-bottom:4px">β回归 vs 沪深300</div>
            <div style="font-size:13px;line-height:1.7">
              年化α <b :class="cls(attr.beta_regression.alpha_annual_pct)">{{fmtPct(attr.beta_regression.alpha_annual_pct)}}</b> ·
              β {{attr.beta_regression.beta}} · R² {{attr.beta_regression.r_squared}} · t(α) {{attr.beta_regression.t_alpha}}
              <div :class="attr.beta_regression.alpha_significant?'green':'red'" style="font-size:12px">{{attr.beta_regression.note}}</div>
            </div>
          </div>
          <div v-if="attr.monte_carlo" style="min-width:240px">
            <div class="sub" style="margin-bottom:4px">蒙特卡洛({{attr.monte_carlo.n_shuffles}}次置换)</div>
            <div style="font-size:13px;line-height:1.7">
              MaxDD {{fmtPct(attr.monte_carlo.maxdd_actual_pct)}} (p={{attr.monte_carlo.maxdd_permutation_p}}) ·
              Sharpe {{attr.monte_carlo.sharpe_annual}} (t={{attr.monte_carlo.sharpe_t_stat}})
              <div :class="attr.monte_carlo.sharpe_significant?'green':'red'" style="font-size:12px">{{attr.monte_carlo.sharpe_note}}</div>
            </div>
          </div>
        </div>
        <div v-if="attr.regime_attribution" style="margin-top:10px">
          <div class="sub" style="margin-bottom:4px">市况归因</div>
          <span v-for="r in attr.regime_attribution.by_regime" :key="r.regime" style="margin-right:16px;font-size:13px">
            {{r.regime}}: {{r.trades}}笔 胜率{{r.win_rate_pct}}% <span :class="cls(r.total_pnl)">{{fmtPct0(r.total_pnl)}}</span>
          </span>
          <div class="sub" style="font-size:12px;margin-top:2px">{{attr.regime_attribution.note}}</div>
        </div>
        <div v-if="attr.trade_attribution && attr.trade_attribution.robustness" class="sub" style="font-size:12px;margin-top:8px">
          鲁棒性:Top5盈利单占总盈利 {{attr.trade_attribution.robustness.top5_pnl_share_pct ?? '--'}}%,
          剔除后<span :class="attr.trade_attribution.robustness.profitable_ex_top5?'green':'red'">{{attr.trade_attribution.robustness.profitable_ex_top5?'仍盈利':'转亏'}}</span>
        </div>
      </div>
      <div v-else-if="p.res.attribution && !p.res.attribution.ok" class="sub" style="margin-top:12px;color:var(--muted)">归因不可用:{{p.res.attribution.error}}</div>
    </div>
  </div>`,

  setup(){
    const s = reactive({
      code: '', hold: 10, stop: 8, target: 15,
      strats: STRATEGIES.map(x=>x.id),
      batchN: 10,
      loading: false, batchLoading: false,
      res: null, batchRes: null, err: ''
    })

    function buildUrl(code, strat){
      let u = `/api/stock/${code}/backtest?strategy=${strat}&hold_days=${s.hold}`
      if(s.stop) u += `&stop_pct=${s.stop}`
      if(s.target) u += `&target_pct=${s.target}`
      return u
    }

    function fmtPct(v){ return v!=null ? (v>0?'+':'')+v.toFixed(1)+'%' : '--' }

    async function runAll(){
      if(!s.code){ s.err='请输入股票代码'; return }
      s.err=''; s.loading=true; s.res=null
      try{
        const results = []
        for(const sid of s.strats){
          try{
            const r = await api(buildUrl(s.code, sid))
            results.push({strategy:sid, summary:r.summary})
          }catch(e){ results.push({strategy:sid, error:e.toString()}) }
        }
        s.res = results
      }catch(e){ s.err=e.toString() }
      s.loading=false
    }

    async function runBatch(){
      s.err=''; s.batchLoading=true; s.batchRes=null
      try{
        const pf = await api('/api/portfolio/stocks')
        const stocks = (pf||[]).slice(0,s.batchN)
        if(!stocks.length){ s.err='无持仓'; s.batchLoading=false; return }

        const strats = s.strats.length===10
          ? ['enter','keep_increasing','turtle_trade']
          : s.strats

        const out = []
        for(const st of stocks){
          const code = st.code||st.symbol||''
          if(!code) continue
          const results = []
          for(const sid of strats){
            try{
              const r = await api(buildUrl(code, sid))
              results.push({strategy:sid, summary:r.summary})
            }catch(e){ results.push({strategy:sid, error:e.toString()}) }
          }
          out.push({symbol:code, name:st.name, results})
        }
        s.batchRes = out
      }catch(e){ s.err=e.toString() }
      s.batchLoading=false
    }

    // ════ 组合级回测 ════
    const REASON = {stop:'止损', target:'止盈', expiry:'到期', ambiguous:'止损?', final_close:'清盘'}
    const p = reactive({
      universe:'holdings', codes:'', limit:50,
      strategy:'enter', useLive:false,
      maxPos:5, hold:10, stop:8, target:15, alloc:'equal', cash:1000000,
      attribution:false,
      loading:false, err:'', res:null
    })

    async function runPortfolio(){
      p.err=''; p.loading=true; p.res=null
      try{
        const body = {
          universe:p.universe, index_code:'000300', limit:p.limit,
          codes: p.universe==='custom' ? p.codes.split(/[,，\s]+/).map(x=>x.trim()).filter(Boolean) : [],
          strategy:p.strategy, use_live:p.useLive,
          hold_days:p.hold, stop_pct:p.stop, target_pct:p.target,
          max_positions:p.maxPos, initial_cash:p.cash, allocation:p.alloc, benchmark:'000300',
          attribution:p.attribution
        }
        p.res = await api('/api/backtest/portfolio', {
          method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
        })
      }catch(e){ p.err=e.toString() }
      p.loading=false
    }

    const metrics = computed(()=>{
      const m = p.res?.summary; if(!m) return []
      const pf = m.profit_factor
      return [
        {k:'总收益', v:fmtPct(m.total_return_pct), cls:cls(m.total_return_pct)},
        {k:'CAGR年化', v:fmtPct(m.cagr_pct), cls:cls(m.cagr_pct)},
        {k:'最大回撤', v:fmtPct(m.max_dd_pct), cls:''},
        {k:'夏普', v:m.sharpe!=null?m.sharpe.toFixed(2):'--', cls:cls(m.sharpe)},
        {k:'年化波动', v:fmtPct(m.volatility_pct), cls:''},
        {k:'胜率', v:m.win_rate_pct!=null?m.win_rate_pct+'%':'--', cls:''},
        {k:'盈亏比', v:pf!=null?pf.toFixed(2):'--', cls:''},
        {k:'超额(vs300)', v:m.excess_return_pct!=null?fmtPct(m.excess_return_pct):'--', cls:cls(m.excess_return_pct)},
        {k:'成交笔数', v:m.trade_count??'--', cls:''},
        {k:'平均仓位', v:m.avg_exposure_pct!=null?m.avg_exposure_pct+'%':'--', cls:''},
      ]
    })

    const curve = computed(()=>{
      const c = p.res?.equity_curve; if(!c||c.length<2) return {pts:null}
      const W=600, H=180, pad=6
      const navs = c.map(x=>x.nav)
      const benchVals = c.map(x=>x.bench_nav).filter(v=>v!=null)
      const all = navs.concat(benchVals, [1.0])
      let lo=Math.min(...all), hi=Math.max(...all); if(hi===lo) hi=lo+1
      const X=i=> pad + i*(W-2*pad)/(c.length-1)
      const Y=v=> H-pad - (v-lo)*(H-2*pad)/(hi-lo)
      const toLine = arr => arr.map((v,i)=> v==null?null:(X(i)+','+Y(v))).filter(Boolean).join(' ')
      const baseY = Y(1.0)
      return {
        W, H,
        pts: toLine(navs),
        bench: benchVals.length ? toLine(c.map(x=>x.bench_nav)) : null,
        base: `${pad},${baseY} ${W-pad},${baseY}`,
        up: navs[navs.length-1] >= 1.0
      }
    })

    const attr = computed(()=> p.res?.attribution || null)
    function fmtPct0(v){ return v!=null ? (v>0?'+':'')+Math.round(v).toLocaleString('en-US') : '--' }

    return { s, p, STRATEGIES, CATEGORIES, CN, REASON, fmtPct, fmtPct0, cls, runAll, runBatch, runPortfolio, metrics, curve, attr }
  }
}
