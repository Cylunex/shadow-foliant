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

    return { s, STRATEGIES, CATEGORIES, CN, fmtPct, runAll, runBatch }
  }
}
