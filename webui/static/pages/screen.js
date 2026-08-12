import { reactive, ref, computed, onMounted } from 'vue'
import { api, fmt, zh, cls, useSort } from '../lib.js'

const INDEXES = [
  {code:'000300', name:'沪深300'}, {code:'000905', name:'中证500'},
  {code:'000906', name:'中证800'}, {code:'000852', name:'中证1000'},
  {code:'000016', name:'上证50'}, {code:'000010', name:'上证180'},
  {code:'399006', name:'创业板指'}, {code:'000688', name:'科创50'},
]
const STYLES = [
  {k:'balanced', t:'均衡'}, {k:'value', t:'价值'}, {k:'growth', t:'成长'},
  {k:'quality', t:'质量'}, {k:'dividend', t:'红利'},
]
const RECIPES = ['主升浪起涨','超跌反弹','强势突破','低估值蓝筹','缠论一买','均线金叉起步']
const STRATS = [
  {k:'value', t:'低估值'}, {k:'main_force', t:'主力资金'}, {k:'small_cap', t:'小市值'},
  {k:'profit_growth', t:'净利增长'}, {k:'low_price_bull', t:'低价擒牛'},
]

export default {
  template: `
  <div>
    <div class="h1">🎯 选股</div>
    <p class="sub">先看每日综合 TOP15 与最终 TOP5；需要专项研究时再运行多因子、问财或配方选股。</p>
    <div class="tabs">
      <div class="tab" :class="{active:tab==='latest'}" @click="tab='latest';loadLatest()">今日综合优选</div>
      <div class="tab" :class="{active:tab==='mf'}" @click="tab='mf'">多因子选股</div>
      <div class="tab" :class="{active:tab==='wc'}" @click="tab='wc'">问财策略</div>
      <div class="tab" :class="{active:tab==='rp'}" @click="tab='rp'">配方选股</div>
    </div>

    <!-- 每日综合选股产物 -->
    <div v-if="tab==='latest'">
      <section class="command-hero" style="margin-bottom:16px">
        <div><span class="badge info">UNIFIED SELECTION</span><h1 style="margin-top:10px">综合选股</h1><p>TOP15 保留完整候选，最终 TOP5 再经过技术结构、红蓝证伪、追高和组合集中度复核。</p></div>
        <div class="hero-status"><div><strong>{{latest.meta?.snapshot_date||'暂无快照'}}</strong><small>{{latest.status==='success'?'今日产物可用':statusCn(latest.status)}}</small></div><button class="ghost" :disabled="latest.loading" @click="loadLatest">{{latest.loading?'读取中…':'刷新'}}</button></div>
      </section>
      <div v-if="latest.err" class="err">{{latest.err}}</div>
      <section class="card">
        <div class="section-head"><div><h2>最终优选 TOP5</h2><p>这是需要优先研究的短名单，不等于直接买入指令</p></div><span class="badge warning">买入前核对盘面与价格</span></div>
        <div v-if="latestFinal.length" class="pick-grid">
          <article v-for="(r,i) in latestFinal" :key="r.code" class="pick-card" style="cursor:pointer" @click="openStock(r.code)">
            <span class="pick-rank">{{i+1}}</span><div class="pick-code">{{r.code}}</div><div class="pick-name">{{r.name||'未命名'}}</div>
            <div class="pick-score">{{fmt(r.final_score)}}<small>优选分</small></div>
            <div class="pick-tags"><span class="badge" :class="debateClass(r.debate_verdict)">{{r.debate_verdict||'规则优选'}}</span><span v-if="r.source_count" class="pill">{{r.source_count}} 源</span></div>
            <div class="pick-reason">{{r.final_reason||'等待优选依据'}}</div>
          </article>
        </div>
        <div v-else class="empty-state"><div><b>尚无最终 TOP5</b><br>综合选股完成后会在这里显示。</div></div>
      </section>
      <section class="card">
        <div class="section-head"><div><h2>完整候选 TOP15</h2><p>点击任意标的进入股票研究</p></div><span class="pill">{{latestRows.length}} 只</span></div>
        <div v-if="latestRows.length" class="table-wrap"><table><thead><tr><th>#</th><th>代码 / 名称</th><th>综合分</th><th>现价</th><th>涨跌</th><th>红蓝结论</th><th>来源</th></tr></thead>
          <tbody><tr v-for="r in latestRows" :key="r.code" @click="openStock(r.code)" style="cursor:pointer"><td>{{r.rank}}</td><td><b>{{r.code}}</b><span style="margin-left:7px;color:var(--muted)">{{r.name}}</span></td><td>{{fmt(r.score)}}</td><td>{{r.price??'—'}}</td><td :class="cls(r.change_pct)">{{signed(r.change_pct)}}%</td><td><span class="badge" :class="debateClass(r.debate_verdict)">{{r.debate_verdict||'—'}}</span></td><td style="text-align:left">{{(r.sources||[]).join(' / ')||'—'}}</td></tr></tbody>
        </table></div><div v-else class="empty-state">尚无综合选股快照。</div>
      </section>
    </div>

    <!-- 多因子 -->
    <div v-if="tab==='mf'">
      <div class="card">
        <div class="row">
          <div><label>股票池(指数)</label><select v-model="s.index"><option v-for="i in idx" :value="i.code">{{i.name}}</option></select></div>
          <div><label>取前 N</label><input type="number" v-model.number="s.n" style="width:90px"/></div>
          <button :disabled="s.loading" @click="runMf(false)">{{s.loading?'计算中…':'选股'}}</button>
          <button v-if="s.res" :disabled="s.loading" class="ghost" @click="runMf(true)" title="跳过缓存,重新抓因子">↻ 强制刷新</button>
        </div>
        <div style="margin-top:10px"><label style="display:block;margin-bottom:5px">因子风格(同池重新加权,切换零成本)</label>
          <div class="tabs" style="flex-wrap:wrap">
            <div v-for="st in styles" :key="st.k" class="tab" :class="{active:s.style===st.k}" @click="setStyle(st.k)">{{st.t}}</div>
          </div>
        </div>
        <p class="sub" style="margin:6px 0 0">8 因子:PE/PEG/PB/负债率/ROE/净利增长/股息率/现金流。风格=偏重不同因子(价值偏低估、成长偏增速、质量偏ROE现金流、红利偏股息)。同指数池缓存 6h,首算约 15-40s,之后秒回。</p>
      </div>
      <div v-if="s.err" class="err">{{s.err}}</div>
      <div v-if="s.res" class="card">
        <h3>{{s.res.top.length}} 只 · 因子:{{(s.res.factors_used||[]).join(' / ')}}
          <span class="pill" style="margin-left:8px">{{s.res.cached?'缓存':'实时'}}{{s.res.cached_at?' · '+s.res.cached_at.slice(5,16).replace('T',' '):''}}</span></h3>
        <table><thead><tr>
          <th @click="sortMf('rank')" style="cursor:pointer;user-select:none">#{{arrowMf('rank')}}</th>
          <th @click="sortMf('symbol')" style="cursor:pointer;user-select:none">代码{{arrowMf('symbol')}}</th>
          <th @click="sortMf('composite')" style="cursor:pointer;user-select:none">综合分{{arrowMf('composite')}}</th>
          <th v-for="f in mcols" :key="f" @click="sortMf(f)" style="cursor:pointer;user-select:none">{{zh(f)}}{{arrowMf(f)}}</th></tr></thead>
          <tbody><tr v-for="r in sortedTop" :key="r.symbol"><td>{{r.rank}}</td><td>{{r.symbol}}</td><td>{{fmt(r.composite)}}</td><td v-for="f in mcols">{{fmt(r[f])}}</td></tr></tbody></table>
      </div>
    </div>

    <!-- 问财策略 -->
    <div v-if="tab==='wc'">
      <div class="card">
        <label>策略(问财,需联网较慢)</label>
        <div class="tabs">
          <div v-for="st in strats" :key="st.k" class="tab" :class="{active:w.strat===st.k}" @click="runWc(st.k)">{{st.t}}</div>
        </div>
      </div>
      <div v-if="w.err" class="err">{{w.err}}</div>
      <div v-if="w.loading" class="loading">选股中…(问财查询,约 5-15s)</div>
      <div v-else-if="w.rows" class="card">
        <h3>{{w.msg}} · {{w.rows.length}} 只</h3>
        <table v-if="w.rows.length"><thead><tr><th v-for="c in wcols" :key="c" @click="sortWc(c)" style="cursor:pointer;user-select:none">{{zh(c)}}{{arrowWc(c)}}</th></tr></thead>
          <tbody><tr v-for="(r,i) in sortedWc" :key="i"><td v-for="c in wcols">{{disp(r[c])}}</td></tr></tbody></table>
        <div v-else class="loading">无结果。</div>
      </div>
    </div>

    <!-- 配方选股 -->
    <div v-if="tab==='rp'">
      <div class="card">
        <div class="row">
          <div><label>配方</label><select v-model="rp.recipe"><option v-for="r in recipes" :value="r">{{r}}</option></select></div>
          <button :disabled="rp.loading" @click="runRecipe">{{rp.loading?'筛选中…':'跑配方'}}</button>
          <button class="ghost" :disabled="rp.loading" @click="rpHoldings">用持仓</button>
          <button class="ghost" :disabled="rp.loading" @click="rpHs300">用沪深300多因子Top</button>
          <span class="pill">{{rpCount}} 只候选</span>
        </div>
        <label style="display:block;margin-top:10px">候选池(代码,逗号/空格/换行分隔)</label>
        <textarea v-model="rp.codesText" rows="3" placeholder="如 600519 000858 601318 600036" style="width:100%;margin-top:6px"></textarea>
        <p class="sub" style="margin:6px 0 0">配方=条件库组合(如低估值蓝筹=PE≤25且PB≤1且ROE>15%)。在你给的候选池里筛出全部满足的。部分依赖外部接口的条件本机可能跳过。</p>
      </div>
      <div v-if="rp.err" class="err">{{rp.err}}</div>
      <div v-if="rp.res" class="card">
        <h3>{{rp.res.recipe}} · 命中 {{(rp.res.hits||[]).length}}/{{rp.res.universe_size}}</h3>
        <div v-if="(rp.res.hits||[]).length" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <span v-for="c in rp.res.hits" :key="c" class="pill" style="font-size:14px">{{c}}</span>
        </div>
        <div v-else class="loading">候选池中无满足该配方的标的。</div>
        <div style="color:var(--muted);font-size:13px">
          <div><b>已评估条件:</b> {{(rp.res.evaluated_conditions||rp.res.conditions||[]).join(' · ')}}</div>
          <div v-if="(rp.res.skipped_external||[]).length" style="margin-top:4px">⚠️ 跳过(需外部接口): {{rp.res.skipped_external.join(' · ')}}</div>
        </div>
      </div>
    </div>
  </div>`,
  setup(){
    const tab = ref('latest')
    const latest = reactive({ status:'missing', data:null, meta:null, loading:false, err:'' })
    const latestFinal = computed(()=>latest.data?.final_rows||[])
    const latestRows = computed(()=>latest.data?.rows||[])
    async function loadLatest(){
      latest.loading=true;latest.err=''
      try{const r=await api('/api/screen/latest');latest.status=r.status;latest.data=r.data||{};latest.meta=r.meta||{}}
      catch(e){latest.err=''+e}finally{latest.loading=false}
    }
    const statusCn=v=>({success:'今日可用',stale:'快照已过期',missing:'尚无快照',failed:'读取失败'}[v]||v||'未知')
    const debateClass=v=>String(v||'').includes('否决')?'danger':String(v||'').includes('谨慎')?'warning':'success'
    const signed=v=>v==null?'—':`${Number(v)>=0?'+':''}${Number(v).toFixed(2)}`
    function openStock(code){if(code){try{sessionStorage.setItem('sf-stock-code',code)}catch(e){};location.hash='stock'}}
    const s = reactive({ index:'000300', n:15, style:'balanced', res:null, err:'', loading:false })
    const mcols = reactive([])
    const w = reactive({ strat:'', rows:null, msg:'', err:'', loading:false })
    const wcols = computed(()=> (w.rows && w.rows.length) ? Object.keys(w.rows[0]).slice(0,9) : [])
    const { sortBy:sortMf, arrow:arrowMf, sorted:sortedTop } = useSort(()=> s.res ? s.res.top : [], 'rank', 1)
    const { sortBy:sortWc, arrow:arrowWc, sorted:sortedWc } = useSort(()=> w.rows, '', 1)
    const disp = v => v==null?'—':(typeof v==='object'?JSON.stringify(v).slice(0,40):''+v)
    async function runMf(refresh){
      s.loading=true; s.err=''; if(refresh) s.res=null
      try{ const r = await api('/api/screen/multifactor?index='+s.index+'&n='+s.n+'&style='+s.style+(refresh?'&refresh=1':'')); mcols.length=0; (r.factors_used||[]).forEach(f=>mcols.push(f)); s.res=r }
      catch(e){ s.err=''+e }finally{ s.loading=false }
    }
    function setStyle(k){ s.style=k; if(s.res) runMf(false) }   // 切风格即重算(复用缓存,秒回)
    // 配方选股
    const rp = reactive({ recipe:'低估值蓝筹', codesText:'', res:null, err:'', loading:false })
    const rpParse = () => (rp.codesText.match(/\d{6}/g) || [])
    const rpCount = computed(()=> rpParse().length)
    async function runRecipe(){
      const codes = rpParse()
      if(!codes.length){ rp.err='请填候选池(至少一个6位代码)'; return }
      rp.loading=true; rp.err=''; rp.res=null
      try{ rp.res = await api('/api/screen/recipe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipe:rp.recipe, codes})}) }
      catch(e){ rp.err=''+e }finally{ rp.loading=false }
    }
    async function rpHoldings(){
      try{ const h = await api('/api/portfolio/overview'); rp.codesText=(h.stocks||h||[]).map(x=>x.code||x.symbol).filter(Boolean).join(' ') }
      catch(e){ rp.err='取持仓失败: '+e }
    }
    async function rpHs300(){
      try{ const m = await api('/api/screen/multifactor?index=000300&n=30'); rp.codesText=(m.top||[]).map(x=>x.symbol).join(' ') }
      catch(e){ rp.err='取多因子失败: '+e }
    }
    async function runWc(k){
      w.strat=k; w.loading=true; w.err=''; w.rows=null
      try{ const r = await api('/api/screen/strategy/'+k+'?top_n=10'); w.rows=r.rows; w.msg=r.msg }
      catch(e){ w.err=''+e }finally{ w.loading=false }
    }
    onMounted(loadLatest)
    return { tab, latest, latestFinal, latestRows, loadLatest, statusCn, debateClass, signed, openStock,
             s, mcols, w, wcols, sortedTop, sortMf, arrowMf, sortedWc, sortWc, arrowWc,
             idx:INDEXES, styles:STYLES, strats:STRATS, runMf, setStyle, runWc,
             rp, recipes:RECIPES, rpCount, runRecipe, rpHoldings, rpHs300,
             fmt, zh, cls, disp }
  }
}
