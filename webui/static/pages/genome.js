import { reactive, ref, computed, onMounted } from 'vue'
import { api, cls } from '../lib.js'

const CN_MAP = {
  enter:'放量上涨', keep_increasing:'均线多头', turtle_trade:'海龟交易',
  parking_apron:'停机坪', low_atr:'低ATR成长', high_tight_flag:'高而窄旗形',
  breakthrough_platform:'突破平台', backtrace_ma250:'回踩年线',
  climax_limitdown:'放量跌停', low_backtrace_increase:'无大幅回撤',
  rsi_oversold_bounce:'RSI超卖反弹', bollinger_squeeze_breakout:'布林收窄突破',
  weekly_trend_daily_signal:'周线趋势+日线',
  composed:'🧪组合新策略',
}

export default {
  template: `<div>
    <h1 class="h1">🧬 策略基因组</h1>
    <p class="sub">策略自动进化追踪 · 每日 16:30 更新</p>

    <!-- 策略评分面板 -->
    <div class="card">
      <h3>📊 全市场策略效能排行 <span style="font-weight:400;color:var(--muted);font-size:12px">（近30天横截面回测）</span></h3>
      <div v-if="s.loading" class="loading">加载中...</div>
      <div v-else-if="s.err" class="err">{{s.err}}</div>
      <table v-else style="width:100%">
        <tr style="color:var(--muted);font-size:12px">
          <th align=left>策略</th><th align=right>评分</th><th align=right>胜率</th>
          <th align=right>均收益</th><th align=right>最大回撤</th><th align=right>最佳</th>
          <th align=right>最差</th><th align=right>触发/池</th>
        </tr>
        <tr v-for="r in s.scores" :key="r.strategy_id" style="border-bottom:1px solid var(--bdr)">
          <td><b>{{CN_MAP[r.strategy_id] || r.strategy_id}}</b></td>
          <td align=right><span :style="{fontWeight:700,color:(r.score||0)>=70?'var(--amber)':(r.score||0)>=55?'var(--accent)':'var(--muted)'}">{{r.score||'--'}}</span></td>
          <td align=right>{{r.win_rate_pct!=null ? (r.win_rate_pct|0)+'%' : '--'}}</td>
          <td align=right :class="cls(r.avg_ret_pct)">{{r.avg_ret_pct!=null ? (r.avg_ret_pct>=0?'+':'')+r.avg_ret_pct.toFixed(1)+'%' : '--'}}</td>
          <td align=right>{{r.max_dd_pct!=null ? r.max_dd_pct.toFixed(1)+'%' : '--'}}</td>
          <td align=right class="red">{{r.best_ret_pct!=null ? '+'+r.best_ret_pct.toFixed(1)+'%' : '--'}}</td>
          <td align=right class="green">{{r.worst_ret_pct!=null ? r.worst_ret_pct.toFixed(1)+'%' : '--'}}</td>
          <td align=right style="color:var(--muted)">{{r.triggered_n||0}}/{{r.stock_pool_n||0}}</td>
        </tr>
      </table>
    </div>

    <!-- 进化效果 A/B(进化到底有没有用) -->
    <div class="card" style="margin-top:16px">
      <h3>🧬 进化效果 A/B <span style="font-weight:400;color:var(--muted);font-size:12px">（进化集 vs 全默认集组合回测;超额&gt;0=进化更优;周更）</span></h3>
      <div v-if="!s.ab" class="loading">加载中…</div>
      <div v-else-if="!s.ab.length" class="sub">尚无 A/B 数据(周任务 wf_weekly_backtest 跑后显示)</div>
      <template v-else>
        <div style="margin-bottom:10px">
          <span style="font-size:13px;color:var(--muted)">最新（{{s.ab[0].eval_date}}）：</span>
          <b :class="cls(s.ab[0].excess_return_pct)" style="font-size:16px">超额 {{s.ab[0].excess_return_pct>=0?'+':''}}{{fmt(s.ab[0].excess_return_pct)}}%</b>
          <span style="font-size:12px;color:var(--muted)">（进化 {{fmt(s.ab[0].evolved_return_pct)}}% vs 默认 {{fmt(s.ab[0].default_return_pct)}}%）</span>
          <span v-if="abUnderperform" class="pill" style="background:var(--amber);color:#000;margin-left:8px">⚠️ 近期跑输，已自动回退默认</span>
        </div>
        <table style="width:100%">
          <tr style="color:var(--muted);font-size:12px">
            <th align=left>日期</th><th align=right>进化收益</th><th align=right>默认收益</th>
            <th align=right>超额</th><th align=right>进化夏普</th><th align=right>默认夏普</th><th align=right>股池</th>
          </tr>
          <tr v-for="r in s.ab" :key="r.eval_date" style="border-bottom:1px solid var(--bdr)">
            <td>{{r.eval_date}}</td>
            <td align=right :class="cls(r.evolved_return_pct)">{{fmt(r.evolved_return_pct)}}%</td>
            <td align=right :class="cls(r.default_return_pct)">{{fmt(r.default_return_pct)}}%</td>
            <td align=right style="font-weight:700" :class="cls(r.excess_return_pct)">{{r.excess_return_pct>=0?'+':''}}{{fmt(r.excess_return_pct)}}%</td>
            <td align=right style="color:var(--muted)">{{fmt(r.evolved_sharpe)}}</td>
            <td align=right style="color:var(--muted)">{{fmt(r.default_sharpe)}}</td>
            <td align=right style="color:var(--muted)">{{r.pool_n||0}}</td>
          </tr>
        </table>
      </template>
    </div>

    <!-- 变体列表 -->
    <div class="card" style="margin-top:16px">
      <h3>🧪 活跃策略变体 <span style="font-weight:400;color:var(--muted);font-size:12px">（含变异后代）</span></h3>
      <div v-if="s.varLoading" class="loading">加载中...</div>
      <table v-else-if="s.variants.length" style="width:100%">
        <tr style="color:var(--muted);font-size:12px">
          <th align=left>基础策略</th><th align=left>中文名</th><th align=right>代数</th>
          <th align=right>评分</th><th align=right>胜率</th><th align=right>均收益</th>
          <th align=right>样本股</th><th align=left>参数</th><th align=left>状态</th>
        </tr>
        <tr v-for="v in s.variants" :key="v.id" style="border-bottom:1px solid var(--bdr)"
            :style="{background:v.generation>0?'rgba(79,124,255,0.06)':''}">
          <td><b>{{v.base_strategy}}</b></td>
          <td>{{v.strategy_cn||''}}</td>
          <td align=right>
            <span :style="{fontWeight:v.generation>0?700:400,color:v.generation>0?'var(--amber)':'var(--muted)'}">
              gen{{v.generation}}
            </span>
          </td>
          <td align=right :style="{fontWeight:700,color:(v.score||0)>=70?'var(--amber)':(v.score||0)>=55?'var(--accent)':'var(--muted)'}">
            {{v.score!=null ? v.score.toFixed(0) : '--'}}
          </td>
          <td align=right>{{v.win_rate_pct!=null ? (v.win_rate_pct|0)+'%' : '--'}}</td>
          <td align=right :class="cls(v.avg_ret_pct)">{{v.avg_ret_pct!=null ? (v.avg_ret_pct>=0?'+':'')+v.avg_ret_pct.toFixed(1)+'%' : '--'}}</td>
          <td align=right style="color:var(--muted)">{{v.sample_stocks||0}}</td>
          <td style="font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            {{formatParams(v.params)}}
          </td>
          <td>
            <span :class="v.status==='promoted'?'pill':''" :style="v.status==='promoted'?{background:'var(--amber)',color:'#000'}:{color:'var(--muted)'}">
              {{v.status||'active'}}
            </span>
          </td>
        </tr>
      </table>
      <div v-else class="sub" style="margin-top:12px">尚无变体数据，随回测运行后自动生成</div>
    </div>

    <!-- 因子 IC 评估 -->
    <div class="card" style="margin-top:16px">
      <h3>🔬 因子效能(IC评估) <span style="font-weight:400;color:var(--muted);font-size:12px">（RankIC/IC-IR/置换检验p值+FDR,科学衡量价量因子预测力）</span></h3>
      <div v-if="!s.factors" class="loading">加载中…(首次较慢,需拉股池K线;周任务会预热)</div>
      <div v-else-if="s.factorsErr" class="sub">{{s.factorsErr}}</div>
      <table v-else-if="s.factors.length" style="width:100%">
        <tr style="color:var(--muted);font-size:12px">
          <th align=left>因子</th><th align=left>类别</th><th align=center>判定</th>
          <th align=right>RankIC</th><th align=right>IC-IR</th><th align=right>胜率</th>
          <th align=right>p值(FDR)</th><th align=right>噪声p95</th>
        </tr>
        <tr v-for="f in s.factors" :key="f.key" style="border-bottom:1px solid var(--bdr)">
          <td><b>{{f.name}}</b></td><td style="color:var(--muted)">{{f.category}}</td>
          <td align=center>{{f.verdict}}</td>
          <td align=right :class="cls(f.rank_ic)">{{f.rank_ic>0?'+':''}}{{f.rank_ic}}</td>
          <td align=right :style="{fontWeight:700,color:Math.abs(f.ic_ir)>=0.5?'var(--amber)':Math.abs(f.ic_ir)>=0.3?'var(--accent)':'var(--muted)'}">{{f.ic_ir>0?'+':''}}{{f.ic_ir}}</td>
          <td align=right>{{f.win_rate}}%</td>
          <td align=right :style="{color:f.fdr_significant?'var(--accent)':'var(--muted)'}">{{f.p_value!=null?f.p_value:'—'}}<span v-if="f.fdr_significant"> ✓</span></td>
          <td align=right style="color:var(--muted)">{{f.random_ic!=null?f.random_ic:'—'}}</td>
        </tr>
      </table>
      <div v-else class="sub">尚无因子评估数据(周任务 factor_eval 预热后显示)</div>
      <div v-if="s.factors&&s.factors.length" class="sub" style="margin-top:8px;font-size:11px">⚠️ 未做行业/市值中性化:动量/位置类 IC 仍含 β/规模暴露,非纯 alpha。</div>
    </div>

    <!-- 价量因子 IC 加权选股(闭环:用上面评出的 IC 给因子加权选股) -->
    <div class="card" style="margin-top:16px">
      <h3>🎯 价量因子选股(IC加权) <span style="font-weight:400;color:var(--muted);font-size:12px">（用上面的因子IC给价量因子加权 → TopN;❌噪声因子权重置0。与基本面选股互补）</span></h3>
      <div v-if="!s.pv" class="loading">加载中…(首次较慢,需拉股池K线;周任务会预热)</div>
      <div v-else-if="s.pvErr" class="sub">{{s.pvErr}}</div>
      <template v-else-if="s.pv.top && s.pv.top.length">
        <div class="sub" style="margin-bottom:8px;font-size:12px">
          股池 {{s.pv.universe_size}} 只 ·
          <span v-if="s.pv.ic_weighted">IC加权(权重最高:{{topWeightFactors}})</span>
          <span v-else>等权(factor_eval 缓存未就绪,回退等权)</span>
        </div>
        <table style="width:100%">
          <tr style="color:var(--muted);font-size:12px">
            <th align=left>#</th><th align=left>代码</th><th align=right>合成分</th>
            <th align=right>20日动量</th><th align=right>RSI14</th><th align=right>52周高点</th>
          </tr>
          <tr v-for="r in s.pv.top.slice(0,15)" :key="r.symbol" style="border-bottom:1px solid var(--bdr)">
            <td>{{r.rank}}</td><td><b>{{r.symbol}}</b></td>
            <td align=right :style="{fontWeight:700}" :class="cls(r.composite)">{{fmt(r.composite)}}</td>
            <td align=right :class="cls(r.mom_20)">{{r.mom_20!=null?(r.mom_20*100).toFixed(1)+'%':'—'}}</td>
            <td align=right>{{r.rsi_14!=null?r.rsi_14.toFixed(0):'—'}}</td>
            <td align=right>{{r.high_52w!=null?(r.high_52w*100).toFixed(0)+'%':'—'}}</td>
          </tr>
        </table>
      </template>
      <div v-else class="sub">尚无价量选股数据(周任务预热后显示)</div>
    </div>

    <!-- 个股适配度查询 -->
    <div class="card" style="margin-top:16px">
      <h3>🎯 个股策略适配度查询</h3>
      <div class="row" style="margin-bottom:12px">
        <input v-model="s.searchCode" placeholder="股票代码 e.g. 600519" style="width:160px" @keyup.enter="searchAffinity">
        <button @click="searchAffinity" :disabled="!s.searchCode">查询</button>
      </div>
      <div v-if="s.affinity" style="margin-top:8px">
        <p style="color:var(--muted);font-size:13px">{{s.searchCode}} 策略适配度：</p>
        <div v-if="s.affinity.length===0" class="sub">该股暂无策略适配数据</div>
        <table v-else style="width:100%">
          <tr style="color:var(--muted);font-size:12px">
            <th align=left>策略</th><th align=right>评分</th><th align=right>胜率</th>
            <th align=right>均收益</th><th align=right>触发次数</th>
          </tr>
          <tr v-for="a in s.affinity" :key="a.strategy_id" style="border-bottom:1px solid var(--bdr)">
            <td><b>{{CN_MAP[a.strategy_id]||a.strategy_id}}</b></td>
            <td align=right :style="{fontWeight:700,color:(a.score||0)>=60?'var(--amber)':'var(--muted)'}">{{a.score!=null?a.score.toFixed(0):'--'}}</td>
            <td align=right>{{a.win_rate_pct!=null?(a.win_rate_pct|0)+'%':'--'}}</td>
            <td align=right :class="cls(a.avg_ret_pct)">{{a.avg_ret_pct!=null?(a.avg_ret_pct>=0?'+':'')+a.avg_ret_pct.toFixed(1)+'%':'--'}}</td>
            <td align=right>{{a.trigger_count||0}}</td>
          </tr>
        </table>
      </div>
    </div>
  </div>`,

  setup() {
    const s = reactive({
      scores: [], variants: [], affinity: null,
      loading: false, varLoading: false,
      err: '', searchCode: '',
      factors: null, factorsErr: '',
      ab: null, pv: null, pvErr: '',
    })

    async function loadFactors() {
      try {
        const r = await api('factors/eval')
        if (r && r.error && (!r.factors || !r.factors.length)) s.factorsErr = r.error
        s.factors = (r && r.factors) || []
      } catch(e) { s.factors = []; s.factorsErr = String(e) }
    }

    async function loadAB() {
      try {
        const r = await api('strategy-genome/ab?limit=12')
        s.ab = (r && r.rows) || []
      } catch(e) { s.ab = [] }
    }

    async function loadPV() {
      try {
        const r = await api('factors/pv-screen?n=15')
        if (r && r.error && (!r.top || !r.top.length)) s.pvErr = r.error
        s.pv = r || { top: [] }
      } catch(e) { s.pv = { top: [] }; s.pvErr = String(e) }
    }

    // null 安全数字格式化(A/B 任一指标可能缺)
    const fmt = (v) => (v == null ? '--' : (typeof v === 'number' ? v.toFixed(2) : v))

    // 与后端 _ab_excess_is_negative 同口径:近 6 期≥3 条且 excess 均值<0 → 已自动回退
    const abUnderperform = computed(() => {
      const xs = (s.ab || []).slice(0, 6).map(r => r.excess_return_pct).filter(v => v != null)
      return xs.length >= 3 && (xs.reduce((a, b) => a + b, 0) / xs.length) < 0
    })

    // 价量选股权重 Top3 因子名(展示"按什么加权选的")
    const topWeightFactors = computed(() => {
      const w = (s.pv && s.pv.weights) || {}
      const NM = { mom_20:'20日动量', mom_60:'60日动量', mom_accel:'动量加速', reversal_5:'5日反转',
        vol_20:'20日波动', range_20:'20日振幅', max_ret_20:'彩票', ma_bias_20:'乖离MA20',
        close_pos_20:'价格分位', high_52w:'52周高点', rsi_14:'RSI14', vol_trend:'量能趋势',
        amihud:'非流动性', ret_skew:'收益偏度' }
      return Object.entries(w).sort((a,b)=>b[1]-a[1]).slice(0,3).map(([k])=>NM[k]||k).join('、') || '—'
    })

    const formatParams = (p) => {
      if (!p) return ''
      const d = typeof p === 'string' ? JSON.parse(p) : p
      return Object.entries(d).map(([k,v])=>`${k}=${v}`).join(', ')
    }

    async function loadScores() {
      s.loading = true
      try {
        const r = await api('strategy-genome/scores?days=30')
        s.scores = r.rows || []
      } catch(e) { s.err = String(e) }
      s.loading = false
    }

    async function loadVariants() {
      s.varLoading = true
      try {
        const r = await api('strategy-genome/variants?limit=100')
        s.variants = r.rows || []
      } catch(e) {}
      s.varLoading = false
    }

    async function searchAffinity() {
      const code = s.searchCode.trim()
      if (!code) return
      try {
        const r = await api(`strategy-genome/affinity?stock_code=${code}`)
        s.affinity = r.rows || []
      } catch(e) { s.affinity = [{ strategy_id: 'error', score: 0 }] }
    }

    onMounted(() => { loadScores(); loadVariants(); loadFactors(); loadAB(); loadPV() })

    // cls 必须 return 给模板(模板里多处 :class="cls(...)";漏 return → 运行时 cls is not a function)
    return { s, CN_MAP, formatParams, searchAffinity, cls, fmt, abUnderperform, topWeightFactors }
  }
}
