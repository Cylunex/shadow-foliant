import { reactive, ref, nextTick, onMounted, computed } from 'vue'
import { api, fmt, pct, cls, zh, cell, lineChart } from '../lib.js'

const ITABS = [
  { k:'chan', t:'缠论' }, { k:'chip', t:'筹码分布' }, { k:'signals', t:'策略信号' },
  { k:'forensics', t:'财务排雷' }, { k:'flow', t:'资金流' },
]
const BT_STRATS = [
  { k:'enter', t:'进场点' }, { k:'breakthrough_platform', t:'平台突破' },
  { k:'backtrace_ma250', t:'回踩年线' }, { k:'high_tight_flag', t:'高位旗形' },
  { k:'turtle_trade', t:'海龟交易' }, { k:'low_backtrace_increase', t:'低位回踩放量' },
  { k:'keep_increasing', t:'持续放量' }, { k:'parking_apron', t:'停机坪' },
]
const REGIME = { trending_up:'上升趋势', trending_down:'下降趋势', sideways:'震荡', volatile:'高波动' }

export default {
  template: `
  <div>
    <div class="h1">🏠 股票分析</div>
    <p class="sub">输入 A 股代码,看实时行情 + K线。</p>
    <div class="card"><div class="row">
      <div><label>股票代码</label><input v-model="s.code" @keyup.enter="load" placeholder="如 600519"/></div>
      <button :disabled="s.loading" @click="load">{{s.loading?'加载中…':'查询'}}</button>
    </div></div>
    <div v-if="s.err" class="err">{{s.err}}</div>
    <div v-if="s.info" class="card">
      <h3>{{s.info.name}} ({{s.code}})</h3>
      <div class="metrics">
        <div class="metric"><div class="k">现价</div><div class="v">{{fmt(s.info.price)}}</div></div>
        <div class="metric"><div class="k">涨跌幅</div><div class="v" :class="cls(s.info.change_pct)">{{pct(s.info.change_pct/100)}}</div></div>
        <div class="metric"><div class="k">PE(TTM)</div><div class="v">{{fmt(s.info.pe_ttm)}}</div></div>
        <div class="metric"><div class="k">PB</div><div class="v">{{fmt(s.info.pb)}}</div></div>
        <div class="metric"><div class="k">换手率</div><div class="v">{{fmt(s.info.turnover_pct)}}%</div></div>
        <div class="metric"><div class="k">市值(亿)</div><div class="v">{{fmt(s.info.mcap_yi)}}</div></div>
      </div>
    </div>
    <div v-if="s.info" class="card"><h3>K线(近一年收盘)</h3><div ref="chart" class="chart"></div></div>
    <div v-if="s.info" class="card">
      <h3>🤖 多智能体 AI 深度分析
        <button v-if="ai.res" class="ghost" style="float:right" @click="exportReport">📄 导出研报(MD)</button></h3>
      <button :disabled="ai.loading" @click="deep">{{ai.loading?'分析中…(技术+基本面+风险→讨论→决策,数十秒)':'生成深度分析'}}</button>
      <div v-if="ai.err" class="err" style="margin-top:12px">{{ai.err}}</div>
      <div v-if="ai.res" style="margin-top:16px">
        <div class="metrics" v-if="ai.res.decision">
          <div class="metric"><div class="k">评级</div><div class="v">{{ai.res.decision.action||ai.res.decision.rating||'—'}}</div></div>
          <div class="metric" v-if="ai.res.decision.target_price"><div class="k">目标价</div><div class="v">{{ai.res.decision.target_price}}</div></div>
          <div class="metric" v-if="ai.res.decision.stop_loss"><div class="k">止损</div><div class="v">{{ai.res.decision.stop_loss}}</div></div>
        </div>
        <pre style="white-space:pre-wrap;color:var(--muted);margin-top:12px;font:13px/1.6 inherit">{{summary}}</pre>
        <div v-if="ai.res.rag_evidence" style="margin-top:12px;padding:12px;background:var(--panel2);border-radius:9px">
          <div style="color:var(--accent);font-size:12px;margin-bottom:6px">🔎 向量检索增强证据</div>
          <pre style="white-space:pre-wrap;color:var(--muted);margin:0;font:12px/1.6 inherit">{{ai.res.rag_evidence}}</pre>
        </div>
      </div>
    </div>

    <!-- 个股研究:缠论/筹码/信号/排雷/资金流 -->
    <div v-if="s.info" class="card">
      <h3>📊 个股研究 <span style="color:var(--muted);font-weight:400;font-size:12px">缠论 / 筹码 / 信号 / 排雷 / 资金流</span>
        <button class="ghost" style="float:right" :disabled="ins.loading" @click="loadInsights">{{ins.loading?'分析中…':(ins.data?'↻ 刷新':'加载')}}</button></h3>
      <div v-if="ins.err" class="err">{{ins.err}}</div>
      <div v-if="ins.data">
        <div class="tabs" style="flex-wrap:wrap"><div v-for="t in itabs" :key="t.k" class="tab" :class="{active:icur===t.k}" @click="icur=t.k">{{t.t}}</div></div>

        <div v-if="icur==='chan'" style="margin-top:12px">
          <div v-if="ins.data.chan&&ins.data.chan.available" class="metrics">
            <div class="metric"><div class="k">当前方向</div><div class="v">{{ins.data.chan.current_direction||'—'}}</div></div>
            <div class="metric"><div class="k">分型/笔</div><div class="v">{{ins.data.chan.fractal_count}}/{{ins.data.chan.bi_count}}</div></div>
            <div class="metric"><div class="k">中枢数</div><div class="v">{{ins.data.chan.zhongshu_count}}</div></div>
            <div class="metric" v-if="ins.data.chan.last_close"><div class="k">最新收盘</div><div class="v">{{fmt(ins.data.chan.last_close)}}</div></div>
          </div>
          <pre v-if="chanText" style="white-space:pre-wrap;color:var(--muted);margin-top:10px;font:13px/1.6 inherit">{{chanText}}</pre>
          <div v-if="!ins.data.chan||!ins.data.chan.available" class="loading">缠论数据不足。</div>
        </div>

        <div v-else-if="icur==='chip'" style="margin-top:12px">
          <div v-if="ins.data.chip&&ins.data.chip.available!==false" class="metrics">
            <div class="metric"><div class="k">获利盘</div><div class="v" :class="cls(ins.data.chip.profit_ratio_pct-50)">{{fmt(ins.data.chip.profit_ratio_pct)}}%</div></div>
            <div class="metric"><div class="k">平均成本</div><div class="v">{{fmt(ins.data.chip.avg_cost)}}</div></div>
            <div class="metric"><div class="k">90%成本区</div><div class="v" style="font-size:14px">{{fmt(ins.data.chip.cost_90_low)}}~{{fmt(ins.data.chip.cost_90_high)}}</div></div>
            <div class="metric"><div class="k">集中度</div><div class="v">{{fmt(ins.data.chip.concentration_90_pct)}}%</div></div>
          </div>
          <p v-if="ins.data.chip&&ins.data.chip.summary" style="color:var(--muted);margin-top:10px">{{ins.data.chip.summary}}</p>
          <div v-if="!ins.data.chip||ins.data.chip.available===false" class="loading">筹码数据不足。</div>
        </div>

        <div v-else-if="icur==='signals'" style="margin-top:12px">
          <div style="margin-bottom:8px">行情阶段:<b class="pill" style="margin-left:6px">{{regimeCn(ins.data.signals&&ins.data.signals.regime)}}</b></div>
          <div v-for="sg in sigList" :key="sg.k" style="padding:8px 0;border-bottom:1px solid var(--line)">
            <b :class="sg.on?'red':''">{{sg.on?'✅':'⚪'}} {{sg.t}}</b>
            <span style="color:var(--muted);margin-left:8px;font-size:13px">{{sg.reason}}</span>
          </div>
        </div>

        <div v-else-if="icur==='forensics'" style="margin-top:12px">
          <div v-if="ins.data.forensics&&!ins.data.forensics.error">
            <div>红旗数:<b :class="(ins.data.forensics.flag_count||0)>0?'green':'red'">{{ins.data.forensics.flag_count||0}}</b></div>
            <ul v-if="(ins.data.forensics.red_flags||[]).length" style="color:var(--muted);margin:8px 0">
              <li v-for="(f,i) in ins.data.forensics.red_flags" :key="i">🚩 {{typeof f==='string'?f:(f.msg||f.name||JSON.stringify(f))}}</li>
            </ul>
            <p v-if="ins.data.forensics.summary" style="color:var(--muted);margin-top:8px;white-space:pre-wrap">{{ins.data.forensics.summary}}</p>
          </div>
          <div v-else class="loading">财务数据不足(本机东财受限,生产可用)。</div>
        </div>

        <div v-else-if="icur==='flow'" style="margin-top:12px">
          <table v-if="flowRows.length"><thead><tr><th v-for="c in flowCols" :key="c">{{zh(c)}}</th></tr></thead>
            <tbody><tr v-for="(r,i) in flowRows" :key="i"><td v-for="c in flowCols">{{cell(r[c])}}</td></tr></tbody></table>
          <div v-else class="loading">资金流暂无数据(数据源未返回/本机东财受限)。</div>
        </div>
      </div>
    </div>

    <!-- 策略回测 -->
    <div v-if="s.info" class="card">
      <h3>📉 策略回测 <span style="color:var(--muted);font-weight:400;font-size:12px">裸持有 vs 带止损止盈纪律(双收益)</span></h3>
      <div class="row">
        <div><label>策略</label><select v-model="bt.strategy"><option v-for="st in btStrats" :value="st.k">{{st.t}}</option></select></div>
        <div><label>持有(天)</label><input type="number" v-model.number="bt.hold" style="width:80px"/></div>
        <div><label>止损%</label><input type="number" v-model.number="bt.stop" style="width:80px"/></div>
        <div><label>止盈%</label><input type="number" v-model.number="bt.target" style="width:80px"/></div>
        <button :disabled="bt.loading" @click="runBacktest">{{bt.loading?'回测中…':'回测'}}</button>
      </div>
      <div v-if="bt.err" class="err" style="margin-top:10px">{{bt.err}}</div>
      <div v-if="bt.res" style="margin-top:14px">
        <div class="metrics">
          <div class="metric"><div class="k">触发次数</div><div class="v">{{bt.res.count}}</div></div>
          <div class="metric"><div class="k">胜率(裸)</div><div class="v">{{fmt(bt.res.win_rate)}}%</div></div>
          <div class="metric"><div class="k">平均收益(裸)</div><div class="v" :class="cls(bt.res.avg_ret_pct)">{{fmt(bt.res.avg_ret_pct)}}%</div></div>
          <div class="metric"><div class="k">平均收益(纪律)</div><div class="v" :class="cls(bt.res.avg_ret_net_pct)">{{fmt(bt.res.avg_ret_net_pct)}}%</div></div>
          <div class="metric"><div class="k">最大盈/亏</div><div class="v" style="font-size:14px"><span class="red">{{fmt(bt.res.max_win_pct)}}%</span> / <span class="green">{{fmt(bt.res.max_loss_pct)}}%</span></div></div>
        </div>
        <p class="sub" style="margin-top:8px">回测区间 {{bt.period||'近一年'}}。"纪律"=触发后按止损/止盈离场。样本少时仅供参考。</p>
      </div>
    </div>

    <div v-if="s.info" class="card">
      <h3>💰 DCF 内在价值 <span style="color:var(--muted);font-weight:400;font-size:12px">两阶段现金流折现(净利近似FCF)· 补 PE/PB 之外的绝对估值</span></h3>
      <div class="row">
        <div><label>高速增速%</label><input type="number" v-model.number="dcf.growth" style="width:84px"/></div>
        <div><label>高速年数</label><input type="number" v-model.number="dcf.years" style="width:70px"/></div>
        <div><label>永续增速%</label><input type="number" step="0.5" v-model.number="dcf.terminal" style="width:84px"/></div>
        <div><label>折现率%</label><input type="number" step="0.5" v-model.number="dcf.discount" style="width:84px"/></div>
        <button :disabled="dcf.loading" @click="runDcf">{{dcf.loading?'计算中…':'估值'}}</button>
      </div>
      <div v-if="dcf.err" class="err" style="margin-top:10px">{{dcf.err}}</div>
      <div v-if="dcf.res" style="margin-top:14px">
        <div class="metrics">
          <div class="metric"><div class="k">每股内在价值</div><div class="v">{{dcf.res.intrinsic_per_share}}</div></div>
          <div class="metric"><div class="k">现价</div><div class="v">{{dcf.res.current_price}}</div></div>
          <div class="metric"><div class="k">安全边际</div><div class="v" :class="cls(dcf.res.margin_of_safety)">{{dcf.res.margin_of_safety>=0?'+':''}}{{(dcf.res.margin_of_safety*100).toFixed(1)}}%</div></div>
          <div class="metric"><div class="k">判断</div><div class="v" style="font-size:15px">{{dcf.res.verdict}}</div></div>
          <div class="metric"><div class="k">永续价值占比</div><div class="v">{{pr(dcf.res.terminal_pct)}}</div></div>
        </div>
        <p class="sub" style="margin-top:8px">{{dcf.res.summary}}</p>
        <div v-if="dcf.res.caution" class="sub" style="color:var(--amber,#e0a030)">⚠️ {{dcf.res.caution}}</div>
        <div style="margin-top:10px">
          <div class="sub" style="margin-bottom:4px">敏感性(每股内在价值:折现率 × 永续增速)</div>
          <table v-if="dcf.res.sensitivity&&dcf.res.sensitivity.length" style="font-size:12px">
            <thead><tr><th>折现＼永续</th><th v-for="c in dcf.res.sensitivity[0].cells" :key="c.tg">{{(c.tg*100).toFixed(1)}}%</th></tr></thead>
            <tbody><tr v-for="row in dcf.res.sensitivity" :key="row.wacc">
              <td><b>{{(row.wacc*100).toFixed(1)}}%</b></td>
              <td v-for="c in row.cells" :key="c.tg" :style="{fontWeight: row.wacc===dcf.res.assumptions.discount_rate ? 600:400}">{{c.value}}</td>
            </tr></tbody>
          </table>
        </div>
      </div>
      <p v-else class="sub" style="margin-top:8px">基于 市值/现价/PE 推导股本与基准利润,默认保守假设。点"估值"查看内在价值与安全边际。</p>
    </div>
  </div>`,
  setup(){
    const s = reactive({ code:'600519', info:null, err:'', loading:false })
    const ai = reactive({ res:null, err:'', loading:false })
    const chart = ref()
    // 个股研究
    const ins = reactive({ data:null, err:'', loading:false })
    const icur = ref('chan')
    async function loadInsights(){
      ins.loading=true; ins.err=''
      try{ ins.data = await api('/api/stock/'+s.code+'/insights') }
      catch(e){ ins.err=''+e }finally{ ins.loading=false }
    }
    const chanText = computed(()=>{ const c=ins.data&&ins.data.chan; if(!c) return ''; return c.summary||(c.latest_zhongshu?('最新中枢: '+JSON.stringify(c.latest_zhongshu)):'') })
    const regimeCn = k => REGIME[k]||k||'—'
    const sigList = computed(()=>{ const sg=ins.data&&ins.data.signals; if(!sg) return []
      return [
        { k:'shrink', t:'缩量回踩', on:sg.shrink_pullback&&sg.shrink_pullback.signal, reason:(sg.shrink_pullback||{}).reason },
        { k:'bottom', t:'底部放量', on:sg.bottom_volume&&sg.bottom_volume.signal, reason:(sg.bottom_volume||{}).reason },
        { k:'emotop', t:'情绪顶预警', on:sg.emotion_top_warning&&sg.emotion_top_warning.signal, reason:(sg.emotion_top_warning||{}).reason },
      ]
    })
    const flowRows = computed(()=>{ const f=ins.data&&ins.data.flow; return Array.isArray(f)?f.slice(0,10):(f&&typeof f==='object'&&!f.error?[f]:[]) })
    const flowCols = computed(()=> flowRows.value.length ? Object.keys(flowRows.value[0]).slice(0,8) : [])
    // 回测
    const bt = reactive({ strategy:'enter', hold:10, stop:8, target:15, res:null, period:'', err:'', loading:false })
    async function runBacktest(){
      bt.loading=true; bt.err=''; bt.res=null
      try{ const r = await api('/api/stock/'+s.code+'/backtest?strategy='+bt.strategy+'&hold_days='+bt.hold+'&stop_pct='+bt.stop+'&target_pct='+bt.target); bt.res=r.summary; bt.period=r.period }
      catch(e){ bt.err=''+e }finally{ bt.loading=false }
    }
    // DCF 估值
    const dcf = reactive({ growth:10, years:5, terminal:3, discount:10, res:null, err:'', loading:false })
    const pr = v => v==null?'—':(v*100).toFixed(1)+'%'
    async function runDcf(){
      dcf.loading=true; dcf.err=''; dcf.res=null
      try{
        const u='/api/stock/'+s.code+'/dcf?growth='+(dcf.growth/100)+'&years='+dcf.years+'&terminal='+(dcf.terminal/100)+'&discount='+(dcf.discount/100)
        dcf.res = await api(u)
      }catch(e){ dcf.err=''+e }finally{ dcf.loading=false }
    }
    const summary = computed(()=>{
      if(!ai.res) return ''
      const d = ai.res.decision
      if(d && typeof d==='object') return (d.reason||d.summary||d.analysis||JSON.stringify(d,null,2))
      return ''+ (d||'')
    })
    async function load(){
      s.loading=true; s.err=''; s.info=null; ai.res=null; ins.data=null; ins.err=''; bt.res=null; dcf.res=null; dcf.err=''
      try{
        s.info = await api('/api/stock/'+s.code)
        const k = await api('/api/stock/'+s.code+'/kline')
        await nextTick(); lineChart(chart.value, k, 'date','close','#4f7cff')
      }catch(e){ s.err=''+e }finally{ s.loading=false }
    }
    async function deep(){
      ai.loading=true; ai.err=''; ai.res=null
      try{ ai.res = await api('/api/stock/'+s.code+'/deep-analysis',{method:'POST'}) }
      catch(e){ ai.err=''+e }finally{ ai.loading=false }
    }
    function exportReport(){
      const r=ai.res||{}, d=r.decision||{}, info=s.info||{}
      const today=new Date().toISOString().slice(0,10)
      let md='# '+(info.name||'')+'('+s.code+') 多智能体研报\n\n'
      md+='> 生成日期 '+today+' · 现价 '+(info.price??'—')+' · PE '+(info.pe_ttm??'—')+' · PB '+(info.pb??'—')+'\n\n'
      md+='## 决策\n\n- 评级:'+(d.action||d.rating||'—')+'\n'
      if(d.target_price) md+='- 目标价:'+d.target_price+'\n'
      if(d.stop_loss) md+='- 止损:'+d.stop_loss+'\n'
      md+='\n## 决策依据\n\n'+(summary.value||'—')+'\n'
      for(const k of ['technical','fundamental','risk']){ if(r[k]) md+='\n## '+({technical:'技术面',fundamental:'基本面',risk:'风险'}[k])+'\n\n'+(typeof r[k]==='string'?r[k]:JSON.stringify(r[k],null,2))+'\n' }
      if(r.rag_evidence) md+='\n## 检索证据\n\n'+r.rag_evidence+'\n'
      md+='\n---\n*本研报由 shadow-foliant 多智能体生成,仅供参考,不构成投资建议。*\n'
      const blob=new Blob([md],{type:'text/markdown;charset=utf-8'})
      const a=document.createElement('a'); a.href=URL.createObjectURL(blob)
      a.download=(info.name||s.code)+'_'+s.code+'_研报_'+today+'.md'; a.click(); URL.revokeObjectURL(a.href)
    }
    onMounted(load)
    return { s, ai, chart, summary, load, deep, exportReport,
             ins, icur, itabs:ITABS, loadInsights, chanText, regimeCn, sigList, flowRows, flowCols,
             bt, btStrats:BT_STRATS, runBacktest,
             dcf, runDcf, pr,
             fmt, pct, cls, zh, cell }
  }
}
